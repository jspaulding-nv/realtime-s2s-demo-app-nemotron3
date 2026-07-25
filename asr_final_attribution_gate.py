#!/usr/bin/env python3
"""Replay one WAV twice in real time and verify final ASR word attribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence


ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from asr_attribution import (  # noqa: E402
    final_attribution_record,
    summarize_final_attribution,
)
from config import audio_config, riva_config  # noqa: E402
from direct_asr_client import DirectASRClient  # noqa: E402
from staged_models import AsrFinal  # noqa: E402


SHA256_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
RIFF_HEADER_BYTES = 12
RIFF_CHUNK_HEADER_BYTES = 8
PCM_FORMAT = 1
PCM16_BYTES_PER_SAMPLE = 2
REGISTERED_ASR_PROFILE = (
    "name=nemotron-asr-streaming,type=en-US,batch_size=32"
)
REGISTERED_ASR_PROFILE_SHA256 = (
    "8d3fa26a44c471552b7edac76372f3db66e5c1bd73e24abac701091717026c58"
)
MAX_CHUNK_RELEASE_LATENESS_MS = 250.0


class RealtimeWaveChunks:
    """Yield validated PCM chunks on the source-audio timeline."""

    def __init__(
        self,
        path: Path,
        *,
        run_number: int,
        progress_interval_s: float,
    ) -> None:
        self.path = path
        self.run_number = run_number
        self.progress_interval_s = progress_interval_s
        self.frames_sent = 0
        self.expected_frames: Optional[int] = None
        self.source_frames: Optional[int] = None
        self.sample_rate = 0
        self.padded_pcm_sha256: Optional[str] = None
        self.chunk_release_count = 0
        self.maximum_release_lateness_ms = 0.0
        self.total_release_lateness_ms = 0.0

    @property
    def input_completed(self) -> bool:
        return (
            self.expected_frames is not None
            and self.frames_sent == self.expected_frames
        )

    @property
    def audio_seconds_sent(self) -> float:
        return self.frames_sent / self.sample_rate if self.sample_rate else 0.0

    @property
    def mean_release_lateness_ms(self) -> float:
        if self.chunk_release_count == 0:
            return 0.0
        return self.total_release_lateness_ms / self.chunk_release_count

    @property
    def realtime_pacing_within_limit(self) -> bool:
        return (
            self.maximum_release_lateness_ms
            <= MAX_CHUNK_RELEASE_LATENESS_MS
        )

    def __iter__(self) -> Iterator[bytes]:
        pcm = _extract_exact_mono_pcm16_wav(
            self.path.read_bytes(),
            target_sample_rate=audio_config.sample_rate,
        )
        self.sample_rate = audio_config.sample_rate
        self.source_frames = len(pcm) // PCM16_BYTES_PER_SAMPLE
        chunk_size = audio_config.chunk_size
        self.expected_frames = (
            (self.source_frames + chunk_size - 1) // chunk_size
        ) * chunk_size
        bytes_per_frame = PCM16_BYTES_PER_SAMPLE
        digest = hashlib.sha256()
        started = time.monotonic()
        next_progress = self.progress_interval_s
        while self.frames_sent < self.expected_frames:
            source_start = self.frames_sent * bytes_per_frame
            source_end = min(
                source_start + chunk_size * bytes_per_frame,
                len(pcm),
            )
            data = pcm[source_start:source_end]
            expected_chunk_bytes = min(
                chunk_size,
                self.expected_frames - self.frames_sent,
            ) * bytes_per_frame
            data += b"\x00" * (expected_chunk_bytes - len(data))
            chunk_frames = len(data) // bytes_per_frame
            next_frames_sent = self.frames_sent + chunk_frames
            # Match the formal browser capture: a complete chunk is released
            # no earlier than its source-end boundary.
            target = started + next_frames_sent / self.sample_rate
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            released = time.monotonic()
            release_lateness_ms = max(0.0, (released - target) * 1000.0)
            self.chunk_release_count += 1
            self.total_release_lateness_ms += release_lateness_ms
            self.maximum_release_lateness_ms = max(
                self.maximum_release_lateness_ms,
                release_lateness_ms,
            )
            digest.update(data)
            self.frames_sent = next_frames_sent
            if self.audio_seconds_sent >= next_progress:
                print(
                    f"run {self.run_number}: "
                    f"{self.audio_seconds_sent:.1f}s streamed",
                    file=sys.stderr,
                    flush=True,
                )
                next_progress += self.progress_interval_s
            yield data
        self.padded_pcm_sha256 = digest.hexdigest()


def _extract_exact_mono_pcm16_wav(
    wav_bytes: bytes,
    *,
    target_sample_rate: int,
) -> bytes:
    """Apply the browser's strict RIFF passthrough contract byte for byte."""
    if (
        target_sample_rate <= 0
        or len(wav_bytes) < RIFF_HEADER_BYTES
        or wav_bytes[:4] != b"RIFF"
        or wav_bytes[8:12] != b"WAVE"
    ):
        raise ValueError("ASR attribution gate requires a strict RIFF/WAVE")
    riff_end = struct.unpack_from("<I", wav_bytes, 4)[0] + 8
    if riff_end != len(wav_bytes):
        raise ValueError("RIFF length does not match the exact input bytes")

    offset = RIFF_HEADER_BYTES
    format_fields: Optional[tuple[int, int, int, int, int, int]] = None
    pcm: Optional[bytes] = None
    while offset < riff_end:
        if riff_end - offset < RIFF_CHUNK_HEADER_BYTES:
            raise ValueError("truncated RIFF chunk header")
        chunk_id = wav_bytes[offset:offset + 4]
        chunk_length = struct.unpack_from("<I", wav_bytes, offset + 4)[0]
        payload_start = offset + RIFF_CHUNK_HEADER_BYTES
        payload_end = payload_start + chunk_length
        padded_end = payload_end + (chunk_length & 1)
        if payload_end > riff_end or padded_end > riff_end:
            raise ValueError("truncated RIFF chunk payload")
        if chunk_id == b"fmt ":
            if format_fields is not None or chunk_length < 16:
                raise ValueError("RIFF/WAVE must contain one valid fmt chunk")
            format_fields = struct.unpack_from(
                "<HHIIHH",
                wav_bytes,
                payload_start,
            )
        elif chunk_id == b"data":
            if pcm is not None:
                raise ValueError("RIFF/WAVE must contain one data chunk")
            pcm = wav_bytes[payload_start:payload_end]
        offset = padded_end

    if format_fields is None or pcm is None:
        raise ValueError("RIFF/WAVE is missing fmt or data")
    (
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    ) = format_fields
    expected_block_align = PCM16_BYTES_PER_SAMPLE
    if (
        audio_format != PCM_FORMAT
        or channels != 1
        or sample_rate != target_sample_rate
        or byte_rate != target_sample_rate * expected_block_align
        or block_align != expected_block_align
        or bits_per_sample != 16
        or len(pcm) % block_align != 0
    ):
        raise ValueError(
            "WAV does not match exact mono PCM16 passthrough requirements"
        )
    return pcm


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a full PCM WAV to the configured direct ASR endpoint at "
            "real-time pace. The gate passes only when at least two runs each "
            "produce nonempty finals and every final has a complete word-time "
            "envelope."
        )
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=ROOT / "test_audio" / "long-form-03-30min.wav",
        help="16 kHz mono 16-bit PCM WAV (default: tracked long-form WAV)",
    )
    parser.add_argument(
        "--uri",
        default=riva_config.asr_uri,
        help="direct ASR gRPC endpoint",
    )
    parser.add_argument(
        "--docker-container",
        required=True,
        help=(
            "local ASR container name used to attest its healthy image and "
            "host-port binding; the name is not retained in the report"
        ),
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=2,
        help="real-time replays; must be at least 2 (default: 2)",
    )
    parser.add_argument(
        "--progress-seconds",
        type=float,
        default=60.0,
        help="stderr progress interval in source seconds (default: 60)",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="write the privacy-safe machine-readable report",
    )
    return parser


def run_once(
    *,
    audio_path: Path,
    uri: str,
    run_number: int,
    progress_interval_s: float,
) -> dict:
    chunks = RealtimeWaveChunks(
        audio_path,
        run_number=run_number,
        progress_interval_s=progress_interval_s,
    )
    client = DirectASRClient(uri=uri)
    if not client.connect():
        raise RuntimeError("direct ASR connection failed")

    records = []
    interim_count = 0
    started = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    try:
        for transcript in client.iter_transcripts(chunks):
            if not transcript.is_final:
                interim_count += 1
                continue
            final = AsrFinal.from_transcript(len(records), transcript)
            records.append(final_attribution_record(final))
        if not chunks.input_completed:
            raise RuntimeError("ASR stream ended before the WAV was consumed")
    finally:
        if client.is_connected():
            client.disconnect()

    attribution = summarize_final_attribution(records)
    return {
        "run_number": run_number,
        "started_at_utc": started_utc,
        "wall_seconds": time.monotonic() - started,
        "audio_seconds_sent": chunks.audio_seconds_sent,
        "source_sample_count": chunks.source_frames,
        "padded_pcm_sample_count": chunks.expected_frames,
        "padded_pcm_sha256": chunks.padded_pcm_sha256,
        "input_completed": chunks.input_completed,
        "realtime_pacing": chunks.realtime_pacing_within_limit,
        "pacing_basis": "absolute_chunk_end_deadlines_v1",
        "chunk_release_count": chunks.chunk_release_count,
        "maximum_chunk_release_lateness_ms": (
            chunks.maximum_release_lateness_ms
        ),
        "mean_chunk_release_lateness_ms": chunks.mean_release_lateness_ms,
        "interim_count": interim_count,
        "attribution": attribution,
        "passed": (
            attribution["all_nonempty_finals_have_word_offsets"]
            and chunks.realtime_pacing_within_limit
        ),
    }


def build_report(
    *,
    audio_path: Path,
    uri: str,
    runs: list[dict],
    runtime_attestation: dict,
    requested_run_count: int,
    attempt_id: str,
    attempt_started_at_utc: str,
) -> dict:
    pcm_bindings = {
        (
            run.get("padded_pcm_sha256"),
            run.get("padded_pcm_sample_count"),
        )
        for run in runs
    }
    exact_pcm_binding = (
        len(pcm_bindings) == 1
        and next(iter(pcm_bindings), (None, None))[0] is not None
        and next(iter(pcm_bindings), (None, None))[1] is not None
    )
    passed = (
        requested_run_count >= 2
        and len(runs) == requested_run_count
        and all(run["input_completed"] for run in runs)
        and all(run.get("realtime_pacing") is True for run in runs)
        and all(run["passed"] for run in runs)
        and exact_pcm_binding
        and runtime_attestation.get("verified") is True
    )
    padded_pcm_sha256, padded_pcm_sample_count = (
        next(iter(pcm_bindings))
        if exact_pcm_binding
        else (None, None)
    )
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "attempt_id": attempt_id,
        "attempt_started_at_utc": attempt_started_at_utc,
        "gate": "asr_final_word_attribution",
        "passed": passed,
        "requirements": {
            "minimum_realtime_runs": 2,
            "requested_realtime_runs": requested_run_count,
            "missing_word_offsets_allowed_per_run": 0,
            "nonempty_finals_required_per_run": True,
            "exact_padded_pcm_binding_required": True,
            "maximum_chunk_release_lateness_ms": (
                MAX_CHUNK_RELEASE_LATENESS_MS
            ),
        },
        "input": {
            "pcm_preparation_basis": (
                "riff_pcm16le_passthrough_zero_pad_v1"
            ),
            "wav_sha256": _sha256(audio_path),
            "padded_pcm_sha256": padded_pcm_sha256,
            "padded_pcm_sample_count": padded_pcm_sample_count,
            "exact_padded_pcm_binding_verified": exact_pcm_binding,
        },
        "asr": {
            "endpoint_configured": bool(uri),
            "declared_image_digest": riva_config.asr_image_digest,
            "runtime_attestation": runtime_attestation,
            "language": riva_config.source_language,
            "eou_ms": riva_config.endpointing_history_ms,
            "word_time_offsets_requested": riva_config.asr_word_time_offsets,
        },
        "runs": runs,
    }


def attest_local_asr_runtime(
    *,
    container_name: str,
    uri: str,
) -> dict:
    """Verify a healthy local container without retaining its local name."""
    host, separator, raw_port = uri.rpartition(":")
    normalized_host = host.strip("[]")
    if (
        separator != ":"
        or normalized_host != "127.0.0.1"
        or not raw_port.isdigit()
    ):
        raise RuntimeError(
            "runtime attestation requires a literal 127.0.0.1 ASR URI"
        )
    host_port = int(raw_port)
    if not 1 <= host_port <= 65535:
        raise RuntimeError("runtime attestation has an invalid ASR port")
    expected_digest = riva_config.asr_image_digest
    if SHA256_DIGEST_PATTERN.fullmatch(expected_digest) is None:
        raise RuntimeError(
            "ASR_IMAGE_DIGEST must be one immutable sha256 digest"
        )

    try:
        container_result = subprocess.run(
            ["docker", "inspect", container_name],
            check=True,
            capture_output=True,
            text=True,
        )
        container_documents = json.loads(container_result.stdout)
        if (
            not isinstance(container_documents, list)
            or len(container_documents) != 1
            or not isinstance(container_documents[0], dict)
        ):
            raise RuntimeError("unexpected container inspection schema")
        container = container_documents[0]
        configured_image = container["Config"]["Image"]
        container_environment = container["Config"]["Env"]
        local_image_id = container["Image"]
        running = container["State"]["Running"] is True
        health = container["State"]["Health"]["Status"]
        port_bindings = container["NetworkSettings"]["Ports"]["50052/tcp"]

        image_result = subprocess.run(
            ["docker", "image", "inspect", configured_image],
            check=True,
            capture_output=True,
            text=True,
        )
        image_documents = json.loads(image_result.stdout)
        if (
            not isinstance(image_documents, list)
            or len(image_documents) != 1
            or not isinstance(image_documents[0], dict)
        ):
            raise RuntimeError("unexpected image inspection schema")
        image = image_documents[0]
        repo_digests = image["RepoDigests"]
    except (
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        raise RuntimeError("local ASR runtime attestation failed") from exc

    port_is_bound = (
        isinstance(port_bindings, list)
        and any(
            isinstance(binding, dict)
            and binding.get("HostPort") == str(host_port)
            and binding.get("HostIp") in {"127.0.0.1", "0.0.0.0"}
            for binding in port_bindings
        )
    )
    digest_is_bound = (
        isinstance(repo_digests, list)
        and any(
            isinstance(item, str)
            and item.endswith(f"@{expected_digest}")
            for item in repo_digests
        )
    )
    profile_prefix = "NIM_TAGS_SELECTOR="
    profile_values = (
        [
            entry[len(profile_prefix):]
            for entry in container_environment
            if isinstance(entry, str) and entry.startswith(profile_prefix)
        ]
        if isinstance(container_environment, list)
        else []
    )
    expected_profile = REGISTERED_ASR_PROFILE
    registered_profile_hash_is_valid = (
        hashlib.sha256(expected_profile.encode("utf-8")).hexdigest()
        == REGISTERED_ASR_PROFILE_SHA256
    )
    profile_is_bound = (
        registered_profile_hash_is_valid
        and riva_config.asr_profile == expected_profile
        and profile_values == [expected_profile]
    )
    verified = (
        configured_image == riva_config.asr_image
        and isinstance(local_image_id, str)
        and local_image_id == image.get("Id")
        and SHA256_DIGEST_PATTERN.fullmatch(local_image_id) is not None
        and running
        and health == "healthy"
        and port_is_bound
        and digest_is_bound
        and profile_is_bound
    )
    if not verified:
        raise RuntimeError(
            "local ASR runtime does not match the declared healthy image"
        )
    return {
        "schema_version": 1,
        "verified": True,
        "health": "healthy",
        "host_port": host_port,
        "container_port": 50052,
        "local_image_id": local_image_id,
        "repository_digest": expected_digest,
        "profile_selector_sha256": REGISTERED_ASR_PROFILE_SHA256,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    audio_path = args.file.resolve()
    attempt_id = uuid.uuid4().hex
    attempt_started_at_utc = datetime.now(timezone.utc).isoformat()
    output_path = (
        args.json_output.resolve()
        if args.json_output is not None
        else None
    )
    paths_match = output_path == audio_path
    if (
        not paths_match
        and output_path is not None
        and output_path.exists()
        and audio_path.exists()
    ):
        try:
            paths_match = output_path.samefile(audio_path)
        except OSError:
            paths_match = False
    if paths_match:
        print(
            "--json-output must not overwrite the input WAV",
            file=sys.stderr,
        )
        return 2
    if output_path is not None:
        # Invalidate any prior passing artifact before validation, Docker
        # inspection, or a long replay begins. If this process or the VM dies,
        # the current attempt remains visibly incomplete instead of exposing
        # stale evidence from an earlier invocation.
        _write_report(
            output_path,
            {
                "schema_version": 1,
                "generated_at_utc": attempt_started_at_utc,
                "attempt_id": attempt_id,
                "attempt_started_at_utc": attempt_started_at_utc,
                "gate": "asr_final_word_attribution",
                "passed": False,
                "status": "initializing",
                "runs": [],
            },
        )
    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        return 2
    if args.runs < 2:
        print("--runs must be at least 2", file=sys.stderr)
        return 2
    if args.progress_seconds <= 0:
        print("--progress-seconds must be positive", file=sys.stderr)
        return 2
    if not riva_config.asr_word_time_offsets:
        print(
            "RIVA_ASR_WORD_TIMES=1 is required for this gate",
            file=sys.stderr,
        )
        return 2
    runtime_attestation = {
        "schema_version": 1,
        "verified": False,
        "status": "pending",
    }
    if output_path is not None:
        _write_report(
            output_path,
            build_report(
                audio_path=audio_path,
                uri=args.uri,
                runs=[],
                runtime_attestation=runtime_attestation,
                requested_run_count=args.runs,
                attempt_id=attempt_id,
                attempt_started_at_utc=attempt_started_at_utc,
            ),
        )
    try:
        runtime_attestation = attest_local_asr_runtime(
            container_name=args.docker_container,
            uri=args.uri,
        )
    except Exception as exc:
        runtime_attestation = {
            "schema_version": 1,
            "verified": False,
            "failure": "attestation_failed",
        }
        if output_path is not None:
            _write_report(
                output_path,
                build_report(
                    audio_path=audio_path,
                    uri=args.uri,
                    runs=[],
                    runtime_attestation=runtime_attestation,
                    requested_run_count=args.runs,
                    attempt_id=attempt_id,
                    attempt_started_at_utc=attempt_started_at_utc,
                ),
            )
        print(
            "ASR runtime attestation failed "
            f"({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    if output_path is not None:
        _write_report(
            output_path,
            build_report(
                audio_path=audio_path,
                uri=args.uri,
                runs=[],
                runtime_attestation=runtime_attestation,
                requested_run_count=args.runs,
                attempt_id=attempt_id,
                attempt_started_at_utc=attempt_started_at_utc,
            ),
        )

    completed_runs = []
    for run_number in range(1, args.runs + 1):
        print(
            f"starting real-time ASR attribution run {run_number}/{args.runs}",
            file=sys.stderr,
            flush=True,
        )
        try:
            completed_run = run_once(
                audio_path=audio_path,
                uri=args.uri,
                run_number=run_number,
                progress_interval_s=args.progress_seconds,
            )
            current_attestation = attest_local_asr_runtime(
                container_name=args.docker_container,
                uri=args.uri,
            )
            if current_attestation != runtime_attestation:
                runtime_attestation = {
                    "schema_version": 1,
                    "verified": False,
                    "failure": "runtime_identity_changed",
                }
                print(
                    f"run {run_number} invalidated by a runtime identity "
                    "change",
                    file=sys.stderr,
                )
                break
            completed_runs.append(completed_run)
            if output_path is not None:
                _write_report(
                    output_path,
                    build_report(
                        audio_path=audio_path,
                        uri=args.uri,
                        runs=completed_runs,
                        runtime_attestation=runtime_attestation,
                        requested_run_count=args.runs,
                        attempt_id=attempt_id,
                        attempt_started_at_utc=attempt_started_at_utc,
                    ),
                )
        except Exception as exc:
            print(
                f"run {run_number} failed ({type(exc).__name__})",
                file=sys.stderr,
            )
            break

    report = build_report(
        audio_path=audio_path,
        uri=args.uri,
        runs=completed_runs,
        runtime_attestation=runtime_attestation,
        requested_run_count=args.runs,
        attempt_id=attempt_id,
        attempt_started_at_utc=attempt_started_at_utc,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output_path is not None:
        _write_report(output_path, report)
    print(encoded, end="")
    return 0 if report["passed"] else 1


def _write_report(path: Path, report: dict) -> None:
    """Durably checkpoint completed runs without transcript content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
