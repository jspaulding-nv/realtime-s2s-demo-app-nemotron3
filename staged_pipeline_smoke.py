#!/usr/bin/env python3
"""Run an opt-in end-to-end smoke through direct ASR, NMT, and TTS NIMs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parent
AUDIO_DIR = Path(os.environ.get("S2S_TEST_AUDIO_DIR", ROOT / "test_audio")).expanduser()
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import (  # noqa: E402
    StagedPipelineConfig,
    audio_config,
    riva_config,
    staged_pipeline_config,
)
from direct_asr_bridge_smoke import AsyncWaveAudioFeed  # noqa: E402
from direct_asr_client import DirectASRClient  # noqa: E402
from direct_nmt_client import DirectNMTClient  # noqa: E402
from direct_tts_client import DirectTTSClient  # noqa: E402
from staged_models import StagedOutputEventKind  # noqa: E402
from staged_pipeline import StagedPipelineSession, StagedPipelineState  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream a WAV prefix through direct Nemotron ASR, Riva NMT, and "
            "Magpie TTS using the bounded staged orchestrator."
        )
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=AUDIO_DIR / "preflight.wav",
        help="16 kHz mono 16-bit PCM WAV input",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=60.0,
        help="audio prefix to send; use 0 for the complete file (default: 60)",
    )
    parser.add_argument("--fast", action="store_true", help="disable real-time pacing")
    parser.add_argument("--asr-uri", default=riva_config.asr_uri)
    parser.add_argument("--nmt-uri", default=riva_config.uri)
    parser.add_argument("--tts-uri", default=riva_config.tts_uri)
    parser.add_argument("--nmt-model", default=riva_config.model)
    parser.add_argument("--source-language", default=riva_config.source_language)
    parser.add_argument("--target-language", default="es-US")
    parser.add_argument(
        "--terminal-grace-seconds",
        type=float,
        default=180.0,
        help="post-input budget for final ASR/NMT/TTS drain (default: 180)",
    )
    parser.add_argument(
        "--pcm-output",
        type=Path,
        default=Path("test_results_staged/staged-smoke-es-US.pcm"),
        help="raw 16 kHz mono Int16 PCM destination",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("test_results_staged/staged-smoke-report.json"),
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


class _SessionAudioSink:
    """Adapt ``AsyncWaveAudioFeed`` to the staged session input contract."""

    def __init__(self, session: StagedPipelineSession) -> None:
        self.session = session

    def add_chunk(self, chunk: bytes) -> None:
        self.session.add_audio(chunk)

    def finish_input(self) -> None:
        if self.session.state is StagedPipelineState.RUNNING:
            self.session.finish_input()


def _resolved_config() -> StagedPipelineConfig:
    return StagedPipelineConfig(
        pipeline_mode="staged",
        segment_max_chars=staged_pipeline_config.segment_max_chars,
        segment_max_age_ms=staged_pipeline_config.segment_max_age_ms,
        asr_event_queue_maxsize=staged_pipeline_config.asr_event_queue_maxsize,
        nmt_queue_maxsize=staged_pipeline_config.nmt_queue_maxsize,
        tts_queue_maxsize=staged_pipeline_config.tts_queue_maxsize,
        output_queue_maxsize=staged_pipeline_config.output_queue_maxsize,
        nmt_rpc_timeout_s=staged_pipeline_config.nmt_rpc_timeout_s,
        tts_rpc_timeout_s=staged_pipeline_config.tts_rpc_timeout_s,
        tts_max_segment_audio_s=staged_pipeline_config.tts_max_segment_audio_s,
        tts_max_retries=staged_pipeline_config.tts_max_retries,
        tts_response_chunk_telemetry_enabled=(
            staged_pipeline_config.tts_response_chunk_telemetry_enabled
        ),
        tts_incremental_publish_enabled=(
            staged_pipeline_config.tts_incremental_publish_enabled
        ),
        tts_incremental_frame_ms=(
            staged_pipeline_config.tts_incremental_frame_ms
        ),
        tts_incremental_atomic_fallback_max_chars=(
            staged_pipeline_config
            .tts_incremental_atomic_fallback_max_chars
        ),
        tts_subsegment_max_chars=(
            staged_pipeline_config.tts_subsegment_max_chars
        ),
        tts_subsegment_min_chars=(
            staged_pipeline_config.tts_subsegment_min_chars
        ),
        close_timeout_s=staged_pipeline_config.close_timeout_s,
    )


def _duration_stats(values) -> dict:
    resolved = list(values)
    if not resolved:
        return {"count": 0, "min": None, "max": None, "average": None}
    return {
        "count": len(resolved),
        "min": min(resolved),
        "max": max(resolved),
        "average": sum(resolved) / len(resolved),
    }


async def run(args: argparse.Namespace) -> int:
    audio_path = args.file.resolve()
    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        return 2
    if args.duration_seconds < 0:
        print("--duration-seconds cannot be negative", file=sys.stderr)
        return 2
    if args.terminal_grace_seconds <= 0:
        print("--terminal-grace-seconds must be positive", file=sys.stderr)
        return 2

    try:
        feed = AsyncWaveAudioFeed(
            audio_path,
            chunk_frames=audio_config.chunk_size,
            realtime=not args.fast,
            duration_seconds=args.duration_seconds or None,
        )
    except (ValueError, wave.Error) as exc:
        print(f"Invalid WAV input: {exc}", file=sys.stderr)
        return 2

    session = StagedPipelineSession(
        asr_client=DirectASRClient(uri=args.asr_uri),
        nmt_client=DirectNMTClient(
            uri=args.nmt_uri,
            model=args.nmt_model,
            source_language=args.source_language,
            rpc_timeout_s=staged_pipeline_config.nmt_rpc_timeout_s,
        ),
        tts_client=DirectTTSClient(
            uri=args.tts_uri,
            max_audio_duration_s=staged_pipeline_config.tts_max_segment_audio_s,
            max_retries=staged_pipeline_config.tts_max_retries,
            capture_response_chunk_metrics=(
                staged_pipeline_config.tts_response_chunk_telemetry_enabled
            ),
            incremental_frame_ms=(
                staged_pipeline_config.tts_incremental_frame_ms
            ),
            incremental_atomic_fallback_max_chars=(
                staged_pipeline_config
                .tts_incremental_atomic_fallback_max_chars
            ),
        ),
        target_language=args.target_language,
        config=_resolved_config(),
        owns_clients=True,
    )
    stop_feeding = asyncio.Event()
    producer = None
    started = time.monotonic()
    input_finished_at = None
    first_audio_at = None
    terminal_at = None
    pcm = bytearray()
    segment_reports = []
    parent_reason_by_sequence = {}
    failure = None

    async def feed_audio() -> None:
        nonlocal input_finished_at
        await feed.run(_SessionAudioSink(session), stop_feeding)
        input_finished_at = time.monotonic()

    async def consume_outputs() -> None:
        nonlocal first_audio_at, terminal_at
        while True:
            output = await session.next_output()
            if output.kind is StagedOutputEventKind.ERROR:
                raise RuntimeError(f"{output.stage} stage failed: {output.error}")
            if output.kind is StagedOutputEventKind.COMPLETE:
                terminal_at = time.monotonic()
                return
            if output.kind is StagedOutputEventKind.PARENT_COMPLETE:
                completion = output.completion
                source = completion.translation.segment
                parent_reason_by_sequence[source.sequence_id] = (
                    source.reason.value
                )
                report = completion.to_dict()
                report["source"] = {
                    "sequence_id": source.sequence_id,
                    "reason": source.reason.value,
                }
                segment_reports.append(report)
                continue
            synthesized = (
                output.frame
                if output.kind is StagedOutputEventKind.AUDIO_FRAME
                else output.segment
            )
            if first_audio_at is None:
                first_audio_at = time.monotonic()
            pcm.extend(synthesized.audio)
            if output.kind is StagedOutputEventKind.AUDIO_FRAME:
                continue
            segment_reports.append(synthesized.to_dict())
            if not args.quiet:
                source = synthesized.translation.segment
                parent_reason_by_sequence[source.sequence_id] = (
                    source.reason.value
                )
                print(
                    f"SEGMENT {source.sequence_id}."
                    f"{synthesized.subsequence_id + 1}/"
                    f"{synthesized.subsequence_count} "
                    f"[{source.reason.value}] "
                    f"{len(synthesized.audio)} bytes / "
                    f"{synthesized.audio_duration_ms / 1_000:.3f}s"
                )

    try:
        await session.start()
        producer = asyncio.create_task(feed_audio(), name="staged-smoke-audio-feed")
        overall_deadline = (
            (feed.expected_duration_seconds if not args.fast else 0.0)
            + args.terminal_grace_seconds
        )
        await asyncio.wait_for(consume_outputs(), timeout=overall_deadline)
        await producer
        if not feed.input_completed:
            raise RuntimeError(
                "pipeline completed before every requested WAV frame was sent"
            )
    except asyncio.TimeoutError:
        failure = RuntimeError("staged pipeline exceeded the overall terminal deadline")
    except Exception as exc:
        failure = exc
    finally:
        stop_feeding.set()
        if producer is not None and not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        try:
            await asyncio.wait_for(
                session.aclose(), timeout=staged_pipeline_config.close_timeout_s + 3
            )
        except Exception as exc:
            if failure is None:
                failure = RuntimeError(f"staged pipeline cleanup failed: {exc}")

    session_summary = session.summary()
    if failure is None and session_summary["cleanup_errors"]:
        first_cleanup = session_summary["cleanup_errors"][0]
        failure = RuntimeError(
            "staged pipeline cleanup failed at "
            f"{first_cleanup['stage']}: {first_cleanup['error']}"
        )
    if failure is None and (not segment_reports or not pcm):
        failure = RuntimeError("smoke did not produce translated audio")

    output_duration_s = len(pcm) / (
        audio_config.sample_rate
        * audio_config.channels
        * audio_config.bytes_per_sample
    )
    telemetry = session.telemetry
    if not parent_reason_by_sequence:
        parent_reason_by_sequence = {
            report["translation"]["segment"]["sequence_id"]: (
                report["translation"]["segment"]["reason"]
            )
            for report in segment_reports
            if "translation" in report
        }
    segment_reason_counts = {
        reason: sum(
            parent_reason == reason
            for parent_reason in parent_reason_by_sequence.values()
        )
        for reason in ("punctuation", "length", "age", "final_flush")
    }
    stage_metrics = {
        "nmt_processing_ms": _duration_stats(
            event.processing_duration_ms
            for event in telemetry
            if event.stage == "nmt" and event.event == "completed"
        ),
        "tts_first_audio_ms": _duration_stats(
            event.processing_duration_ms
            for event in telemetry
            if event.stage == "tts" and event.event == "first_audio"
        ),
        "tts_processing_ms": _duration_stats(
            event.processing_duration_ms
            for event in telemetry
            if event.stage == "tts" and event.event == "completed"
        ),
        "nmt_queue_residence_ms": _duration_stats(
            event.queue_residence_ms
            for event in telemetry
            if event.stage == "nmt" and event.event == "started"
        ),
        "tts_queue_residence_ms": _duration_stats(
            event.queue_residence_ms
            for event in telemetry
            if event.stage == "tts" and event.event == "started"
        ),
    }
    summary = {
        "schema_version": staged_pipeline_config.telemetry_schema_version,
        "success": failure is None,
        "error": str(failure) if failure is not None else None,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pipeline": "direct_staged_asr_nmt_tts",
        "audio_path": str(audio_path),
        "audio_seconds_sent": feed.duration_sent_seconds,
        "input_completed": feed.input_completed,
        "output_audio_seconds": output_duration_s,
        "output_to_input_duration_ratio": (
            output_duration_s / feed.duration_sent_seconds
            if feed.duration_sent_seconds
            else 0.0
        ),
        "duration_ratio_scope": (
            "whole WAV prefix including silence; not aligned utterance-only "
            "TTS expansion"
        ),
        "wall_seconds": time.monotonic() - started,
        "first_audio_wall_seconds": (
            first_audio_at - started if first_audio_at is not None else None
        ),
        "tail_drain_seconds": (
            max(0.0, terminal_at - input_finished_at)
            if terminal_at is not None and input_finished_at is not None
            else None
        ),
        "realtime_pacing": not args.fast,
        "asr_uri": args.asr_uri,
        "nmt_uri": args.nmt_uri,
        "tts_uri": args.tts_uri,
        "nmt_model": args.nmt_model,
        "source_language": args.source_language,
        "target_language": args.target_language,
        "eou_ms": riva_config.endpointing_history_ms,
        "segment_count": len(parent_reason_by_sequence),
        "tts_subsegment_count": len(segment_reports),
        "segment_reason_counts": segment_reason_counts,
        "pcm_bytes": len(pcm),
        "stage_metrics": stage_metrics,
        "session": session_summary,
    }
    report = {
        "summary": summary,
        "segments": segment_reports,
        "telemetry": [event.to_dict() for event in telemetry],
    }
    args.pcm_output.parent.mkdir(parents=True, exist_ok=True)
    args.pcm_output.write_bytes(bytes(pcm))
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    if failure is not None:
        print(
            f"Staged pipeline smoke failed; diagnostics written to "
            f"{args.json_output}: {failure}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
