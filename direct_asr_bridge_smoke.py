#!/usr/bin/env python3
"""Repeatable live smoke for the bounded DirectASRClient.open_stream API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parent
AUDIO_DIR = Path(os.environ.get("S2S_TEST_AUDIO_DIR", ROOT / "test_audio")).expanduser()
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import audio_config, riva_config, staged_pipeline_config  # noqa: E402
from direct_asr_client import DirectASRClient  # noqa: E402
from punctuation_segmenter import PunctuationSegmenter  # noqa: E402
from staged_models import ASRStreamEventKind, TextSegment  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream a WAV file through the bounded DirectASRClient.open_stream "
            "API and punctuation segmenter."
        )
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=AUDIO_DIR / "preflight.wav",
        help="16 kHz mono 16-bit PCM WAV input",
    )
    parser.add_argument("--uri", default=riva_config.asr_uri)
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=20.0,
        help="audio prefix to send; use 0 for the complete file (default: 20)",
    )
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--event-queue-maxsize", type=int, default=4)
    parser.add_argument(
        "--event-timeout-seconds",
        type=float,
        default=30.0,
        help=(
            "terminal-event grace added after the expected real-time audio "
            "send duration (default: 30)"
        ),
    )
    parser.add_argument(
        "--max-segment-chars",
        type=int,
        default=staged_pipeline_config.segment_max_chars,
    )
    parser.add_argument(
        "--max-segment-age-ms",
        type=int,
        default=staged_pipeline_config.segment_max_age_ms,
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    return parser


class AsyncWaveAudioFeed:
    """Validate and pace WAV frames without a helper thread."""

    def __init__(
        self,
        path: Path,
        *,
        chunk_frames: int,
        realtime: bool,
        duration_seconds: Optional[float],
    ) -> None:
        self.path = path
        self.chunk_frames = chunk_frames
        self.realtime = realtime
        self.frames_sent = 0

        with wave.open(str(path), "rb") as handle:
            self.sample_rate = handle.getframerate()
            self.channels = handle.getnchannels()
            self.sample_width = handle.getsampwidth()
            if self.sample_rate != audio_config.sample_rate:
                raise ValueError(
                    f"expected {audio_config.sample_rate} Hz WAV, "
                    f"got {self.sample_rate} Hz"
                )
            if self.channels != audio_config.channels:
                raise ValueError(
                    f"expected {audio_config.channels} channel WAV, "
                    f"got {self.channels}"
                )
            if self.sample_width != audio_config.bytes_per_sample:
                raise ValueError(
                    f"expected {audio_config.bytes_per_sample * 8}-bit PCM WAV, "
                    f"got {self.sample_width * 8}-bit"
                )
            if handle.getcomptype() != "NONE":
                raise ValueError("bounded ASR smoke requires uncompressed PCM WAV")

            requested_frames = handle.getnframes()
            if duration_seconds is not None:
                requested_frames = min(
                    requested_frames,
                    int(duration_seconds * self.sample_rate),
                )
            self.expected_frames = requested_frames

    @property
    def duration_sent_seconds(self) -> float:
        return self.frames_sent / self.sample_rate

    @property
    def expected_duration_seconds(self) -> float:
        return self.expected_frames / self.sample_rate

    @property
    def input_completed(self) -> bool:
        return self.frames_sent >= self.expected_frames

    async def run(self, stream, stop_feeding: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with wave.open(str(self.path), "rb") as handle:
                while self.frames_sent < self.expected_frames:
                    if stop_feeding.is_set():
                        break
                    frames_to_read = min(
                        self.chunk_frames,
                        self.expected_frames - self.frames_sent,
                    )
                    data = handle.readframes(frames_to_read)
                    if not data:
                        break
                    if self.realtime:
                        target = started + self.frames_sent / self.sample_rate
                        delay = target - loop.time()
                        if delay > 0:
                            await asyncio.sleep(delay)
                    if stop_feeding.is_set():
                        break
                    stream.add_chunk(data)
                    self.frames_sent += len(data) // (
                        self.channels * self.sample_width
                    )
        finally:
            stream.finish_input()


def terminal_deadline_seconds(
    expected_audio_seconds: float,
    *,
    realtime: bool,
    terminal_grace_seconds: float,
) -> float:
    """Overall deadline to reach COMPLETE/ERROR, not a per-event timeout."""
    send_budget = expected_audio_seconds if realtime else 0.0
    return send_budget + terminal_grace_seconds


async def run(args: argparse.Namespace) -> int:
    audio_path = args.file.resolve()
    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        return 2
    if args.duration_seconds < 0:
        print("--duration-seconds cannot be negative", file=sys.stderr)
        return 2
    if args.event_queue_maxsize <= 0:
        print("--event-queue-maxsize must be positive", file=sys.stderr)
        return 2
    if args.event_timeout_seconds <= 0:
        print("--event-timeout-seconds must be positive", file=sys.stderr)
        return 2

    try:
        audio_feed = AsyncWaveAudioFeed(
            audio_path,
            chunk_frames=audio_config.chunk_size,
            realtime=not args.fast,
            duration_seconds=args.duration_seconds or None,
        )
    except (ValueError, wave.Error) as exc:
        print(f"Invalid WAV input: {exc}", file=sys.stderr)
        return 2
    segmenter = PunctuationSegmenter(
        max_chars=args.max_segment_chars,
        max_age_ms=args.max_segment_age_ms,
    )
    client = DirectASRClient(uri=args.uri)
    if not client.connect():
        client.disconnect()
        return 1

    stream = None
    producer = None
    stop_feeding = asyncio.Event()
    counts = {kind.value: 0 for kind in ASRStreamEventKind}
    finals = []
    segments: list[TextSegment] = []
    terminal = None
    started = time.monotonic()
    last_observed_ms = started * 1_000
    failure = None
    terminal_deadline = terminal_deadline_seconds(
        audio_feed.expected_duration_seconds,
        realtime=not args.fast,
        terminal_grace_seconds=args.event_timeout_seconds,
    )

    try:
        stream = await client.open_stream(
            event_queue_maxsize=args.event_queue_maxsize
        )
        producer = asyncio.create_task(
            audio_feed.run(stream, stop_feeding)
        )

        async def consume_until_terminal():
            nonlocal last_observed_ms
            while True:
                event = await stream.next_event()
                counts[event.kind.value] += 1
                if event.kind is ASRStreamEventKind.FINAL:
                    final = event.final
                    finals.append(final)
                    last_observed_ms = final.received_monotonic_ms
                    emitted = segmenter.push_final(final)
                    segments.extend(emitted)
                    if not args.quiet:
                        print(f"FINAL {final.final_id}: {final.text}")
                        for segment in emitted:
                            print(
                                f"SEGMENT {segment.sequence_id} "
                                f"[{segment.reason.value}]: {segment.text}"
                            )
                if event.kind in (
                    ASRStreamEventKind.COMPLETE,
                    ASRStreamEventKind.ERROR,
                ):
                    return event

        terminal = await asyncio.wait_for(
            consume_until_terminal(),
            timeout=terminal_deadline,
        )

        if terminal.kind is ASRStreamEventKind.ERROR:
            stop_feeding.set()
        await producer
        if terminal.kind is ASRStreamEventKind.ERROR:
            raise RuntimeError(terminal.error)
        if not audio_feed.input_completed:
            raise RuntimeError(
                "ASR stream completed before every requested WAV frame was sent"
            )

        flush_time_ms = max(last_observed_ms, time.monotonic() * 1_000)
        flushed = segmenter.flush(flush_time_ms)
        segments.extend(flushed)
        if not args.quiet:
            for segment in flushed:
                print(
                    f"SEGMENT {segment.sequence_id} "
                    f"[{segment.reason.value}]: {segment.text}"
                )
    except asyncio.TimeoutError:
        failure = RuntimeError(
            "no terminal ASR event within the overall "
            f"{terminal_deadline:.3f}s deadline"
        )
    except Exception as exc:
        failure = exc
    finally:
        stop_feeding.set()
        if producer is not None and not producer.done():
            try:
                await asyncio.wait_for(producer, timeout=1.0)
            except (Exception, asyncio.CancelledError):
                pass
        try:
            await asyncio.wait_for(client.aclose(timeout_s=5), timeout=12.0)
        except Exception as exc:
            if failure is None:
                failure = RuntimeError(f"direct ASR cleanup failed: {exc}")

    if failure is not None:
        print(f"Bounded direct ASR smoke failed: {failure}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    summary = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "api": "DirectASRClient.open_stream",
        "audio_path": str(audio_path),
        "asr_uri": args.uri,
        "audio_seconds_sent": audio_feed.duration_sent_seconds,
        "input_completed": audio_feed.input_completed,
        "wall_seconds": elapsed,
        "realtime_pacing": not args.fast,
        "eou_ms": riva_config.endpointing_history_ms,
        "automatic_punctuation": True,
        "event_queue_maxsize": args.event_queue_maxsize,
        "terminal_deadline_seconds": terminal_deadline,
        "event_counts": counts,
        "terminal": terminal.kind.value,
        "final_count": len(finals),
        "segment_count": len(segments),
        "segment_reason_counts": {
            reason: sum(segment.reason.value == reason for segment in segments)
            for reason in ("punctuation", "length", "age", "final_flush")
        },
    }
    report = {
        "summary": summary,
        "finals": [asdict(item) for item in finals],
        "segments": [item.to_dict() for item in segments],
    }
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2))

    if not finals or not segments:
        print("Smoke did not produce a final transcript and segment", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
