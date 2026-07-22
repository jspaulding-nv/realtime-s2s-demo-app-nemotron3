#!/usr/bin/env python3
"""Standalone live compatibility smoke for direct Nemotron streaming ASR."""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence


ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import audio_config, riva_config, staged_pipeline_config  # noqa: E402
from direct_asr_client import DirectASRClient  # noqa: E402
from punctuation_segmenter import PunctuationSegmenter  # noqa: E402
from staged_models import ASRTranscript, AsrFinal, TextSegment  # noqa: E402


class WaveAudioChunks:
    """Yield header-free PCM chunks from a WAV file, optionally in real time."""

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
        self.duration_seconds = duration_seconds
        self.frames_sent = 0
        self.sample_rate = 0
        self.expected_frames: Optional[int] = None

    @property
    def duration_sent_seconds(self) -> float:
        if not self.sample_rate:
            return 0.0
        return self.frames_sent / self.sample_rate

    @property
    def input_completed(self) -> bool:
        return (
            self.expected_frames is not None
            and self.frames_sent >= self.expected_frames
        )

    def __iter__(self) -> Iterator[bytes]:
        with wave.open(str(self.path), "rb") as handle:
            self.sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            if self.sample_rate != audio_config.sample_rate:
                raise ValueError(
                    f"expected {audio_config.sample_rate} Hz WAV, got {self.sample_rate} Hz"
                )
            if channels != audio_config.channels:
                raise ValueError(
                    f"expected {audio_config.channels} channel WAV, got {channels}"
                )
            if sample_width != audio_config.bytes_per_sample:
                raise ValueError(
                    f"expected {audio_config.bytes_per_sample * 8}-bit PCM WAV, "
                    f"got {sample_width * 8}-bit"
                )
            if handle.getcomptype() != "NONE":
                raise ValueError("direct ASR smoke requires uncompressed PCM WAV input")

            frame_limit = None
            if self.duration_seconds is not None:
                frame_limit = int(self.duration_seconds * self.sample_rate)
            self.expected_frames = min(
                handle.getnframes(),
                frame_limit if frame_limit is not None else handle.getnframes(),
            )
            started = time.monotonic()

            while frame_limit is None or self.frames_sent < frame_limit:
                frames_to_read = self.chunk_frames
                if frame_limit is not None:
                    frames_to_read = min(
                        frames_to_read,
                        frame_limit - self.frames_sent,
                    )
                data = handle.readframes(frames_to_read)
                if not data:
                    break
                if self.realtime:
                    target = started + self.frames_sent / self.sample_rate
                    delay = target - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                self.frames_sent += len(data) // (channels * sample_width)
                yield data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream a WAV file directly to the pinned Nemotron ASR endpoint, "
            "then run finalized text through the punctuation segmenter."
        )
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=Path("test_audio/test-1min.wav"),
        help="16 kHz mono 16-bit PCM WAV input",
    )
    parser.add_argument(
        "--uri",
        default=riva_config.asr_uri,
        help="direct ASR gRPC endpoint",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=20.0,
        help="audio prefix to send; use 0 for the complete file (default: 20)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="send chunks without real-time pacing",
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
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only the final summary",
    )
    return parser


def transcript_to_dict(transcript: ASRTranscript) -> dict:
    payload = asdict(transcript)
    payload["detected_languages"] = list(transcript.detected_languages)
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    audio_path = args.file.resolve()
    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        return 2
    if args.duration_seconds < 0:
        print("--duration-seconds cannot be negative", file=sys.stderr)
        return 2

    duration_limit = args.duration_seconds or None
    chunks = WaveAudioChunks(
        audio_path,
        chunk_frames=audio_config.chunk_size,
        realtime=not args.fast,
        duration_seconds=duration_limit,
    )
    segmenter = PunctuationSegmenter(
        max_chars=args.max_segment_chars,
        max_age_ms=args.max_segment_age_ms,
    )
    client = DirectASRClient(uri=args.uri)
    if not client.connect():
        return 1

    transcripts = []
    segments: list[TextSegment] = []
    final_count = 0
    started = time.monotonic()
    last_observed_ms = started * 1_000

    try:
        for transcript in client.iter_transcripts(chunks):
            transcripts.append(transcript)
            last_observed_ms = transcript.received_monotonic_ms
            if not transcript.is_final:
                continue
            final = AsrFinal.from_transcript(final_count, transcript)
            final_count += 1
            emitted = segmenter.push_final(final)
            segments.extend(emitted)
            if not args.quiet:
                print(f"FINAL {final.final_id}: {final.text}")
                for segment in emitted:
                    print(
                        f"SEGMENT {segment.sequence_id} "
                        f"[{segment.reason.value}]: {segment.text}"
                    )

        if not chunks.input_completed:
            expected_seconds = (
                chunks.expected_frames / chunks.sample_rate
                if chunks.expected_frames is not None and chunks.sample_rate
                else 0.0
            )
            raise RuntimeError(
                "ASR stream ended before the WAV input was consumed: "
                f"sent {chunks.duration_sent_seconds:.3f}s of "
                f"{expected_seconds:.3f}s"
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
    except Exception as exc:
        print(f"Direct ASR smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.disconnect()

    elapsed = time.monotonic() - started
    summary = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "audio_path": str(audio_path),
        "asr_uri": args.uri,
        "audio_seconds_sent": chunks.duration_sent_seconds,
        "input_completed": chunks.input_completed,
        "wall_seconds": elapsed,
        "realtime_pacing": not args.fast,
        "eou_ms": riva_config.endpointing_history_ms,
        "automatic_punctuation": True,
        "interim_count": sum(not item.is_final for item in transcripts),
        "final_count": final_count,
        "segment_count": len(segments),
        "segment_reason_counts": {
            reason: sum(segment.reason.value == reason for segment in segments)
            for reason in ("punctuation", "length", "age", "final_flush")
        },
    }
    report = {
        "summary": summary,
        "transcripts": [transcript_to_dict(item) for item in transcripts],
        "segments": [segment.to_dict() for segment in segments],
    }

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2))

    if final_count == 0 or not segments:
        print("Smoke did not produce a final transcript and segment", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
