#!/usr/bin/env python3
"""Privacy-safe replay for a short staged-pipeline segment.

The selected ASR and NMT text remains in memory only. Persisted diagnostics
contain provenance, structural Unicode counts, a per-run keyed HMAC used only
to compare repeated payload equality, and model outcomes. The HMAC key is
discarded and is never written to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import audio_config, riva_config, staged_pipeline_config  # noqa: E402
from direct_asr_client import DirectASRClient  # noqa: E402
from direct_nmt_client import DirectNMTClient  # noqa: E402
from direct_tts_client import DirectTTSClient  # noqa: E402
from punctuation_segmenter import PunctuationSegmenter  # noqa: E402
from staged_models import ASRStreamEventKind, TextSegment  # noqa: E402


class DiagnosticTTSTimeout(TimeoutError):
    """Bounded, text-free failure for one diagnostic synthesis call."""

    def __init__(self, retry_count: int) -> None:
        self.retry_count = retry_count if retry_count in {0, 1} else 0
        super().__init__("diagnostic TTS call exceeded its deadline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay an audio prefix, select one ASR segment, and probe its "
            "isolated and next-context NMT/TTS behavior without persisting text"
        )
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=ROOT / "test_audio" / "long-form-01.mp3",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=39.3,
        help="audio prefix sent to ASR in seconds (default: 39.3)",
    )
    parser.add_argument(
        "--sequence-id",
        type=int,
        default=7,
        help="pre-coalescing segment sequence to inspect (default: 7)",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=5,
        help="exact TTS calls per isolated/context variant (default: 5)",
    )
    parser.add_argument(
        "--client-max-retries",
        type=int,
        choices=(0, 1),
        default=0,
        help="retries inside each probe call; zero isolates raw outcomes",
    )
    parser.add_argument(
        "--tts-timeout-seconds",
        type=float,
        default=staged_pipeline_config.tts_rpc_timeout_s,
        help="deadline for each exact TTS probe call (default: staged timeout)",
    )
    parser.add_argument(
        "--isolated-only",
        action="store_true",
        help="skip the adjacent-context comparison",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="disable real-time ASR pacing; not faithful to the formal run",
    )
    parser.add_argument("--json-output", type=Path)
    return parser


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - environment failure
        raise RuntimeError("ffmpeg or imageio-ffmpeg is required") from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def decode_prefix(path: Path, duration_seconds: float) -> bytes:
    command = [
        _ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-t",
        f"{duration_seconds:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        str(audio_config.channels),
        "-ar",
        str(audio_config.sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg could not decode the requested audio prefix")
    if not result.stdout:
        raise RuntimeError("decoded audio prefix was empty")
    return result.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_audio_metadata(
    path: Path,
    *,
    prefix_seconds: float,
    pcm_bytes: int,
    realtime: bool,
) -> dict:
    """Return reproducibility fields without retaining a path or filename."""
    return {
        "sha256": sha256_file(path),
        "prefix_seconds": prefix_seconds,
        "pcm_bytes": pcm_bytes,
        "realtime_pacing": realtime,
    }


def safe_service_metadata(
    *,
    client_max_retries: int,
    tts_timeout_seconds: float,
) -> dict:
    """Return model-policy fields without retaining endpoint hostnames."""
    return {
        "eou_ms": riva_config.endpointing_history_ms,
        "source_language": riva_config.source_language,
        "target_language": riva_config.target_language,
        "tts_client_max_retries": client_max_retries,
        "tts_probe_timeout_seconds": tts_timeout_seconds,
    }


async def feed_pcm(stream, pcm: bytes, *, realtime: bool) -> None:
    """Feed the same 300 ms chunks used by the staged WebSocket path."""
    chunk_bytes = (
        audio_config.chunk_size
        * audio_config.channels
        * audio_config.bytes_per_sample
    )
    bytes_per_second = (
        audio_config.sample_rate
        * audio_config.channels
        * audio_config.bytes_per_sample
    )
    started = asyncio.get_running_loop().time()
    try:
        for offset in range(0, len(pcm), chunk_bytes):
            if realtime:
                target = started + offset / bytes_per_second
                delay = target - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
            stream.add_chunk(pcm[offset : offset + chunk_bytes])
    finally:
        stream.finish_input()


async def collect_segments(
    pcm: bytes,
    *,
    realtime: bool,
) -> tuple[int, list[TextSegment]]:
    """Run the production-style bounded ASR bridge and age-polling segmenter."""
    client = DirectASRClient(uri=riva_config.asr_uri)
    if not client.connect():
        raise RuntimeError("direct ASR connection failed")
    segmenter = PunctuationSegmenter(
        max_chars=staged_pipeline_config.segment_max_chars,
        max_age_ms=staged_pipeline_config.segment_max_age_ms,
    )
    final_count = 0
    segments: list[TextSegment] = []
    last_observed_ms: Optional[float] = None
    stream = None
    producer = None

    def observed_ms(captured_ms: Optional[float] = None) -> float:
        nonlocal last_observed_ms
        value = time.monotonic_ns() / 1_000_000
        if captured_ms is not None:
            value = max(value, captured_ms)
        if last_observed_ms is not None:
            value = max(value, last_observed_ms)
        last_observed_ms = value
        return value

    try:
        stream = await client.open_stream(
            event_queue_maxsize=staged_pipeline_config.asr_event_queue_maxsize
        )
        producer = asyncio.create_task(
            feed_pcm(stream, pcm, realtime=realtime),
            name="short-segment-diagnostic-feed",
        )
        poll_s = min(
            0.1,
            staged_pipeline_config.segment_max_age_ms / 1_000,
        )
        while True:
            try:
                event = await stream.next_event(timeout_s=poll_s)
            except asyncio.TimeoutError:
                segments.extend(segmenter.emit_due(observed_ms()))
                continue

            if event.kind is ASRStreamEventKind.INTERIM:
                segments.extend(
                    segmenter.emit_due(
                        observed_ms(event.transcript.received_monotonic_ms)
                    )
                )
                continue
            if event.kind is ASRStreamEventKind.FINAL:
                final = event.final
                final_count += 1
                segments.extend(
                    segmenter.push_final(
                        final,
                        observed_monotonic_ms=observed_ms(
                            final.received_monotonic_ms
                        ),
                    )
                )
                continue
            if event.kind is ASRStreamEventKind.COMPLETE:
                segments.extend(segmenter.flush(observed_ms()))
                break
            raise RuntimeError("direct ASR returned a terminal error")

        await producer
        producer = None
    finally:
        if producer is not None and not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        await client.aclose(
            timeout_s=staged_pipeline_config.close_timeout_s
        )
    return final_count, segments


def merge_with_next(current: TextSegment, following: TextSegment) -> TextSegment:
    if following.sequence_id != current.sequence_id + 1:
        raise ValueError("context segment must immediately follow selected segment")
    starts = (current.source_start_ms, following.source_start_ms)
    ends = (current.source_end_ms, following.source_end_ms)
    return TextSegment(
        sequence_id=current.sequence_id,
        text=f"{current.text.rstrip()} {following.text.lstrip()}",
        reason=current.reason,
        emitted_monotonic_ms=max(
            current.emitted_monotonic_ms,
            following.emitted_monotonic_ms,
        ),
        buffered_since_monotonic_ms=min(
            current.buffered_since_monotonic_ms,
            following.buffered_since_monotonic_ms,
        ),
        source_start_ms=(
            min(value for value in starts if value is not None)
            if all(value is not None for value in starts)
            else None
        ),
        source_end_ms=(
            max(value for value in ends if value is not None)
            if all(value is not None for value in ends)
            else None
        ),
        contributing_final_ids=tuple(
            dict.fromkeys(
                current.contributing_final_ids
                + following.contributing_final_ids
            )
        ),
    )


def text_shape(text: str, *, correlation_key: bytes) -> dict:
    categories = Counter(unicodedata.category(character) for character in text)
    scripts = Counter(_script_class(character) for character in text)
    return {
        "characters": len(text),
        "letters_or_digits": sum(character.isalnum() for character in text),
        "unicode_category_counts": dict(sorted(categories.items())),
        "script_class_counts": dict(sorted(scripts.items())),
        "correlation_hmac_sha256": hmac.new(
            correlation_key,
            text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest(),
    }


def _script_class(character: str) -> str:
    category = unicodedata.category(character)
    name = unicodedata.name(character, "")
    if category.startswith("L"):
        return "Latin" if "LATIN" in name else "NonLatinLetter"
    if category == "Nd":
        return "DecimalDigit"
    if category.startswith("P"):
        return "Punctuation"
    if category.startswith("M"):
        return "CombiningMark"
    if character.isspace():
        return "Whitespace"
    return "Other"


def _grpc_status_name(exc: BaseException) -> Optional[str]:
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if callable(code):
            try:
                status = code()
            except Exception:
                status = None
            name = getattr(status, "name", None)
            if name:
                return str(name)
        current = current.__cause__ or current.__context__
    return None


def segment_metadata(segment: TextSegment) -> dict:
    return {
        "sequence_id": segment.sequence_id,
        "reason": segment.reason.value,
        "source_start_ms": segment.source_start_ms,
        "source_end_ms": segment.source_end_ms,
        "contributing_final_ids": list(segment.contributing_final_ids),
        "source_characters": len(segment.text),
        "source_letters_or_digits": sum(
            character.isalnum() for character in segment.text
        ),
    }


def synthesize_with_deadline(
    tts: DirectTTSClient,
    translation,
    *,
    timeout_s: float,
):
    """Run one blocking TTS call with channel-abort and bounded cleanup."""
    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="short-segment-tts-probe",
    )
    future = executor.submit(tts.synthesize_segment, translation)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeoutError as exc:
        retry_count = tts.active_retry_count
        tts.disconnect()
        try:
            future.result(timeout=staged_pipeline_config.close_timeout_s)
        except BaseException:
            pass
        if not future.done():
            raise RuntimeError(
                "diagnostic TTS worker did not stop after channel abort"
            ) from exc
        if not tts.connect():
            raise RuntimeError(
                "diagnostic TTS client could not reconnect after timeout"
            ) from exc
        raise DiagnosticTTSTimeout(retry_count) from exc
    finally:
        executor.shutdown(
            wait=future.done(),
            cancel_futures=True,
        )


def probe_variant(
    *,
    name: str,
    segment: TextSegment,
    repetitions: int,
    client_max_retries: int,
    tts_timeout_s: float,
    correlation_key: bytes,
    nmt: DirectNMTClient,
    tts: DirectTTSClient,
) -> dict:
    report = {
        "name": name,
        "source": segment_metadata(segment),
        "translation": None,
        "tts_attempts": [],
    }
    try:
        translation = nmt.translate_segment(segment, riva_config.target_language)
    except Exception as exc:
        report["translation"] = {
            "success": False,
            "error_code": type(exc).__name__,
            "grpc_status": _grpc_status_name(exc),
        }
        return report

    report["translation"] = {
        "success": True,
        "language": translation.language,
        "nmt_retry_count": translation.retry_count,
        "source_override_applied": translation.source_override_applied,
        "target_shape": text_shape(
            translation.text,
            correlation_key=correlation_key,
        ),
    }
    for repetition in range(1, repetitions + 1):
        started = time.monotonic()
        try:
            synthesized = synthesize_with_deadline(
                tts,
                translation,
                timeout_s=tts_timeout_s,
            )
        except Exception as exc:
            report["tts_attempts"].append(
                {
                    "repetition": repetition,
                    "success": False,
                    "error_code": type(exc).__name__,
                    "grpc_status": _grpc_status_name(exc),
                    "client_retry_count": getattr(exc, "retry_count", 0),
                    "wall_ms": (time.monotonic() - started) * 1_000,
                }
            )
            continue
        report["tts_attempts"].append(
            {
                "repetition": repetition,
                "success": True,
                "audio_bytes": len(synthesized.audio),
                "audio_duration_ms": synthesized.audio_duration_ms,
                "client_retry_count": synthesized.retry_count,
                "wall_ms": (time.monotonic() - started) * 1_000,
            }
        )
    report["client_max_retries"] = client_max_retries
    return report


def run(args: argparse.Namespace) -> dict:
    audio_path = args.file.resolve()
    if not audio_path.is_file():
        raise ValueError("audio file does not exist")
    if (
        not math.isfinite(args.duration_seconds)
        or args.duration_seconds <= 0
    ):
        raise ValueError("--duration-seconds must be positive and finite")
    if args.sequence_id < 0:
        raise ValueError("--sequence-id must be non-negative")
    if args.repetitions <= 0:
        raise ValueError("--repetitions must be positive")
    if (
        not math.isfinite(args.tts_timeout_seconds)
        or args.tts_timeout_seconds <= 0
    ):
        raise ValueError("--tts-timeout-seconds must be positive and finite")

    pcm = decode_prefix(audio_path, args.duration_seconds)
    final_count, segments = asyncio.run(
        asyncio.wait_for(
            collect_segments(pcm, realtime=not args.fast),
            timeout=(
                (args.duration_seconds if not args.fast else 0.0)
                + 60.0
            ),
        )
    )
    selected = next(
        (
            segment
            for segment in segments
            if segment.sequence_id == args.sequence_id
        ),
        None,
    )
    if selected is None:
        raise RuntimeError("requested ASR segment was not emitted")

    variants = [("isolated", selected)]
    following = next(
        (
            segment
            for segment in segments
            if segment.sequence_id == args.sequence_id + 1
        ),
        None,
    )
    if following is not None and not args.isolated_only:
        variants.append(("coalesced_with_next", merge_with_next(selected, following)))

    nmt = DirectNMTClient(
        uri=riva_config.uri,
        model=riva_config.model,
        source_language=riva_config.source_language,
        rpc_timeout_s=staged_pipeline_config.nmt_rpc_timeout_s,
    )
    tts = DirectTTSClient(
        uri=riva_config.tts_uri,
        max_audio_duration_s=staged_pipeline_config.tts_max_segment_audio_s,
        max_retries=args.client_max_retries,
    )
    if not nmt.connect():
        raise RuntimeError("direct NMT connection failed")
    if not tts.connect():
        nmt.disconnect()
        raise RuntimeError("direct TTS connection failed")

    correlation_key = secrets.token_bytes(32)
    try:
        variant_reports = [
            probe_variant(
                name=name,
                segment=segment,
                repetitions=args.repetitions,
                client_max_retries=args.client_max_retries,
                tts_timeout_s=args.tts_timeout_seconds,
                correlation_key=correlation_key,
                nmt=nmt,
                tts=tts,
            )
            for name, segment in variants
        ]
    finally:
        tts.disconnect()
        nmt.disconnect()

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "plaintext_retained": False,
            "fingerprint": (
                "HMAC-SHA256 with a random per-run key that is not exported; "
                "usable only for equality correlation within this report"
            ),
        },
        "audio": safe_audio_metadata(
            audio_path,
            prefix_seconds=args.duration_seconds,
            pcm_bytes=len(pcm),
            realtime=not args.fast,
        ),
        "services": safe_service_metadata(
            client_max_retries=args.client_max_retries,
            tts_timeout_seconds=args.tts_timeout_seconds,
        ),
        "asr": {
            "final_count": final_count,
            "segment_count": len(segments),
            "selected_sequence_id": args.sequence_id,
        },
        "variants": variant_reports,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run(args)
    except Exception as exc:
        print(
            "Short-segment diagnostic failed: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output is not None:
        output = args.json_output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
