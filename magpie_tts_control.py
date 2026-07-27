#!/usr/bin/env python3
"""Run a privacy-safe, repeated Magpie TTS streaming control.

This control deliberately targets only ``nvidia-riva-client==2.24.0`` from
the repository-local ``.python-packages`` directory. The JSON report omits
input text, local paths, text fingerprints, service hostnames, audio payloads,
and exception messages. Generated WAV files still contain synthesized speech
and therefore require review before sharing.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import re
import statistics
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from packaging.version import InvalidVersion, Version

from tts_comparison_fixture import DEFAULT_TEXT, identify_text


EXPECTED_CLIENT_VERSION = "2.24.0"
DEFAULT_GRPC_URI = "localhost:50053"
DEFAULT_TTS_LOCALE = "es-US"
DEFAULT_VOICE = "Magpie-Multilingual.ES-US.Isabela"
DEFAULT_SAMPLE_RATE_HZ = 22_050
DEFAULT_REPEATS = 3
DEFAULT_RPC_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_AUDIO_DURATION_SECONDS = 60.0
DEFAULT_CONTAINER_IMAGE = (
    "nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0"
)
DEFAULT_NIM_PROFILE = "name=magpie-tts-multilingual,batch_size=8"
DEFAULT_IMAGE_DIGEST = (
    "sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d"
)
REPORT_FILENAME = "magpie-tts-control.json"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCALE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}$")
_GRPC_STATUS_NAMES = {
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "ABORTED",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
    "INTERNAL",
    "UNAVAILABLE",
    "DATA_LOSS",
    "UNAUTHENTICATED",
}
_AGGREGATE_METRICS = (
    "ttfa_seconds",
    "wall_time_seconds",
    "audio_chunk_count",
    "audio_bytes",
    "audio_duration_seconds",
    "real_time_factor",
    "inter_audio_chunk_gap_p50_seconds",
    "inter_audio_chunk_gap_p95_seconds",
    "inter_audio_chunk_gap_max_seconds",
    "post_first_audio_delivery_seconds",
    "produced_audio_margin_seconds",
    "minimum_produced_audio_margin_seconds",
    "underrun_risk_seconds",
    "rpc_completion_tail_seconds",
)


class RPCDeadlineExceeded(TimeoutError):
    """The watchdog cancelled a streaming RPC at its deadline."""


class AudioLimitExceeded(RuntimeError):
    """Streaming PCM exceeded the configured incremental byte limit."""


def _env_or_default(name: str, default: str) -> str:
    return os.environ.get(name) or default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a warm, repeated Magpie streaming-TTS control."
    )
    text_group = parser.add_mutually_exclusive_group()
    text_group.add_argument("--text")
    text_group.add_argument("--text-file", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--grpc-uri", default=DEFAULT_GRPC_URI)
    parser.add_argument("--tts-locale", default=DEFAULT_TTS_LOCALE)
    parser.add_argument("--sample-rate-hz", type=int, default=DEFAULT_SAMPLE_RATE_HZ)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-audio-duration-seconds",
        type=float,
        default=DEFAULT_MAX_AUDIO_DURATION_SECONDS,
    )
    parser.add_argument(
        "--image-digest",
        default=_env_or_default("TTS_IMAGE_DIGEST", DEFAULT_IMAGE_DIGEST),
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_artifact_directory(now: Optional[datetime] = None) -> Path:
    stamp = (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return Path("experiment_results") / f"magpie-tts-control-{stamp}"


def load_text(args: argparse.Namespace) -> str:
    if args.text_file is not None:
        text = args.text_file.expanduser().read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = DEFAULT_TEXT
    text = text.strip()
    if not text:
        raise ValueError("synthesis text must not be empty")
    return text


def require_exact_client_version(version: str) -> None:
    try:
        actual = Version(version)
        expected = Version(EXPECTED_CLIENT_VERSION)
    except InvalidVersion as exc:
        raise RuntimeError("unrecognized nvidia-riva-client version") from exc
    if actual != expected:
        raise RuntimeError(
            f"nvidia-riva-client=={EXPECTED_CLIENT_VERSION} is required"
        )


def require_local_client_origin(
    module_file: Optional[str],
    *,
    repository_root: Optional[Path] = None,
) -> Path:
    if not module_file:
        raise RuntimeError("cannot verify the imported Riva client location")
    root = (repository_root or Path(__file__).resolve().parent).resolve()
    expected = (root / ".python-packages").resolve()
    imported = Path(module_file).resolve()
    try:
        imported.relative_to(expected)
    except ValueError as exc:
        raise RuntimeError(
            "Riva client must be imported from repository-local "
            ".python-packages"
        ) from exc
    return imported


def validate_declared_provenance(
    *,
    container_image: str,
    nim_profile: str,
    image_digest: str,
) -> dict[str, str]:
    if container_image != DEFAULT_CONTAINER_IMAGE:
        raise ValueError("formal Magpie control requires the pinned 1.7.0 image")
    if nim_profile != DEFAULT_NIM_PROFILE:
        raise ValueError("formal Magpie control requires the pinned profile")
    if (
        not isinstance(image_digest, str)
        or _DIGEST_RE.fullmatch(image_digest) is None
    ):
        raise ValueError("image digest must be a lowercase sha256 digest")
    return {
        "verification_status": "declared_unverified",
        "container_image": container_image,
        "image_tag": "1.7.0",
        "image_digest": image_digest,
        "nim_profile": nim_profile,
    }


def validate_request(
    *,
    tts_locale: str,
    voice: str,
    sample_rate_hz: int,
    repeats: int,
    rpc_timeout_seconds: float,
    max_audio_duration_seconds: float,
) -> None:
    if _LOCALE_RE.fullmatch(tts_locale) is None:
        raise ValueError("unsupported TTS locale format")
    if voice != DEFAULT_VOICE:
        raise ValueError("formal Magpie control requires the public default voice")
    if (
        not isinstance(sample_rate_hz, int)
        or isinstance(sample_rate_hz, bool)
        or sample_rate_hz <= 0
    ):
        raise ValueError("sample rate must be a positive integer")
    if (
        not isinstance(repeats, int)
        or isinstance(repeats, bool)
        or not 1 <= repeats <= 100
    ):
        raise ValueError("repeats must be an integer in [1, 100]")
    for name, value in (
        ("RPC timeout", rpc_timeout_seconds),
        ("maximum audio duration", max_audio_duration_seconds),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be positive and finite")


def grpc_port(grpc_uri: str) -> int:
    host, separator, port_text = grpc_uri.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError("--grpc-uri must use host:port form")
    port = int(port_text)
    if not 1 <= port <= 65_535:
        raise ValueError("--grpc-uri contains an invalid port")
    return port


def safe_error(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, RPCDeadlineExceeded):
        category = "rpc_deadline_exceeded"
    elif isinstance(exc, AudioLimitExceeded):
        category = "audio_limit_exceeded"
    elif isinstance(exc, ValueError):
        category = "invalid_audio_response"
    else:
        category = "rpc_or_runtime_error"
    status = ""
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            value = code()
            status = getattr(value, "name", "") or str(value)
        except Exception:
            status = ""
    if status.startswith("StatusCode."):
        status = status.split(".", 1)[1]
    if status not in _GRPC_STATUS_NAMES:
        status = ""
    return {"category": category, "grpc_status": status}


def percentile(values: Sequence[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def continuity_metrics(
    *,
    arrival_times: Sequence[float],
    chunk_bytes: Sequence[int],
    first_audio_at: float,
    last_audio_at: float,
    terminal_completed_at: float,
    sample_rate_hz: int,
) -> dict[str, Any]:
    gaps = [
        current - previous
        for previous, current in zip(arrival_times, arrival_times[1:])
    ]
    bytes_per_second = sample_rate_hz * 2
    delivered_seconds = chunk_bytes[0] / bytes_per_second
    pre_arrival_margins: list[float] = []
    for arrival, size in zip(arrival_times[1:], chunk_bytes[1:]):
        elapsed = arrival - first_audio_at
        pre_arrival_margins.append(delivered_seconds - elapsed)
        delivered_seconds += size / bytes_per_second
    delivery_window = last_audio_at - first_audio_at
    last_arrival_margin = delivered_seconds - delivery_window
    margins = pre_arrival_margins + [last_arrival_margin]
    minimum_margin = min(margins)
    underrun_risk = max(0.0, -minimum_margin)
    return {
        "inter_audio_chunk_gap_count": len(gaps),
        "inter_audio_chunk_gap_p50_seconds": percentile(gaps, 0.50),
        "inter_audio_chunk_gap_p95_seconds": percentile(gaps, 0.95),
        "inter_audio_chunk_gap_max_seconds": max(gaps) if gaps else None,
        "post_first_audio_delivery_seconds": delivery_window,
        "produced_audio_margin_seconds": last_arrival_margin,
        "minimum_produced_audio_margin_seconds": (
            minimum_margin
        ),
        "underrun_risk_seconds": underrun_risk,
        "underrun_risk_detected": underrun_risk > 0,
        "rpc_completion_tail_seconds": (
            terminal_completed_at - last_audio_at
        ),
    }


def _cancel_rpc(call: Any) -> None:
    cancel = getattr(call, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            pass


def _new_private_directory(path: Path) -> Path:
    """Create a new 0700 tree without following symlinks or reusing a leaf."""
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("artifact directory must not be a symbolic link")
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if len(parts) < 2:
        raise ValueError("artifact directory must not be the filesystem root")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_fd = os.open(parts[0], directory_flags)
    try:
        for component in parts[1:-1]:
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            try:
                next_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise ValueError(
                    "artifact path must not traverse a symbolic link "
                    "or non-directory"
                ) from exc
            if created:
                os.fchmod(next_fd, 0o700)
            os.close(parent_fd)
            parent_fd = next_fd

        leaf = parts[-1]
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise FileExistsError(
                "refusing to reuse or overwrite an artifact directory"
            ) from exc
        leaf_fd = os.open(leaf, directory_flags, dir_fd=parent_fd)
        try:
            os.fchmod(leaf_fd, 0o700)
        finally:
            os.close(leaf_fd)
    finally:
        os.close(parent_fd)
    return absolute


def _private_temporary_file(path: Path) -> tuple[int, Path]:
    descriptor, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    os.fchmod(descriptor, 0o600)
    return descriptor, Path(name)


def _publish_private_file(temporary: Path, final_path: Path) -> None:
    os.link(temporary, final_path)
    os.chmod(final_path, 0o600)
    temporary.unlink()
    directory_fd = os.open(final_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_wav_atomic(
    path: Path,
    pcm: bytes,
    *,
    sample_rate_hz: int,
) -> None:
    descriptor, temporary = _private_temporary_file(path)
    try:
        with os.fdopen(descriptor, "w+b") as raw_output:
            descriptor = -1
            with wave.open(raw_output, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate_hz)
                output.writeframes(pcm)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        _publish_private_file(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary = _private_temporary_file(path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        _publish_private_file(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def synthesize_once(
    service: Any,
    *,
    riva_client_module: Any,
    text: str,
    tts_locale: str,
    voice: str,
    sample_rate_hz: int,
    output_path: Optional[Path],
    rpc_timeout_seconds: float,
    max_audio_duration_seconds: float,
    clock: Callable[[], float] = time.perf_counter,
    timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
) -> dict[str, Any]:
    max_audio_bytes = int(
        sample_rate_hz * 2 * max_audio_duration_seconds
    )
    if max_audio_bytes <= 0:
        raise ValueError("maximum audio duration produces a zero-byte limit")

    started = clock()
    responses = service.synthesize_online(
        text=text,
        voice_name=voice,
        language_code=tts_locale,
        encoding=riva_client_module.AudioEncoding.LINEAR_PCM,
        sample_rate_hz=sample_rate_hz,
    )
    if not callable(getattr(responses, "cancel", None)):
        raise RuntimeError(
            "nvidia-riva-client 2.24 streaming call is not cancellable"
        )

    deadline_lock = threading.Lock()
    deadline_state = {"armed": True, "fired": False}

    def cancel_at_deadline() -> None:
        with deadline_lock:
            should_cancel = deadline_state["armed"]
            if should_cancel:
                deadline_state["fired"] = True
        if should_cancel:
            _cancel_rpc(responses)

    timer = timer_factory(rpc_timeout_seconds, cancel_at_deadline)
    timer_start = getattr(timer, "start", None)
    timer_cancel = getattr(timer, "cancel", None)
    timer_join = getattr(timer, "join", None)
    if (
        not callable(timer_start)
        or not callable(timer_cancel)
        or not callable(timer_join)
    ):
        _cancel_rpc(responses)
        raise RuntimeError(
            "deadline timer must support start(), cancel(), and join()"
        )
    if hasattr(timer, "daemon"):
        timer.daemon = True
    try:
        timer_start()
    except Exception:
        with deadline_lock:
            deadline_state["armed"] = False
        timer_cancel()
        _cancel_rpc(responses)
        raise

    pcm_chunks: list[bytes] = []
    arrival_times: list[float] = []
    chunk_bytes: list[int] = []
    empty_response_count = 0
    total_audio_bytes = 0
    first_audio_at: Optional[float] = None
    last_audio_at: Optional[float] = None
    caught_exception: Optional[BaseException] = None
    try:
        for response in responses:
            received_at = clock()
            audio = bytes(getattr(response, "audio", b""))
            if not audio:
                empty_response_count += 1
                continue
            if len(audio) % 2:
                raise ValueError("TTS returned a partial 16-bit PCM frame")
            next_total = total_audio_bytes + len(audio)
            if next_total > max_audio_bytes:
                raise AudioLimitExceeded("streaming PCM limit exceeded")
            if first_audio_at is None:
                first_audio_at = received_at
            last_audio_at = received_at
            total_audio_bytes = next_total
            arrival_times.append(received_at)
            chunk_bytes.append(len(audio))
            pcm_chunks.append(audio)
        terminal_completed_at = clock()
    except BaseException as exc:
        caught_exception = exc
    finally:
        with deadline_lock:
            deadline_state["armed"] = False
        timer_cancel()
        timer_join()
        with deadline_lock:
            deadline_did_fire = deadline_state["fired"]

    if deadline_did_fire:
        raise RPCDeadlineExceeded(
            "streaming RPC deadline exceeded"
        ) from caught_exception
    if caught_exception is not None:
        _cancel_rpc(responses)
        raise caught_exception
    if (
        not pcm_chunks
        or first_audio_at is None
        or last_audio_at is None
    ):
        raise RuntimeError("TTS returned no audio")

    audio_duration = total_audio_bytes / (sample_rate_hz * 2)
    wall_time = terminal_completed_at - started
    ttfa = first_audio_at - started
    if (
        audio_duration <= 0
        or ttfa < 0
        or wall_time < 0
        or last_audio_at < first_audio_at
        or terminal_completed_at < last_audio_at
        or any(
            current < previous
            for previous, current in zip(arrival_times, arrival_times[1:])
        )
    ):
        raise RuntimeError("invalid synthesis timing or duration")

    result = {
        "status": "succeeded",
        "ttfa_seconds": ttfa,
        "audio_duration_seconds": audio_duration,
        "wall_time_seconds": wall_time,
        "real_time_factor": wall_time / audio_duration,
        "audio_bytes": total_audio_bytes,
        "audio_chunk_count": len(pcm_chunks),
        "empty_response_count": empty_response_count,
        **continuity_metrics(
            arrival_times=arrival_times,
            chunk_bytes=chunk_bytes,
            first_audio_at=first_audio_at,
            last_audio_at=last_audio_at,
            terminal_completed_at=terminal_completed_at,
            sample_rate_hz=sample_rate_hz,
        ),
    }
    if output_path is not None:
        _write_wav_atomic(
            output_path,
            b"".join(pcm_chunks),
            sample_rate_hz=sample_rate_hz,
        )
    return result


def metric_summary(
    results: Sequence[Mapping[str, Any]],
    metric: str,
) -> Optional[dict[str, float]]:
    values = [
        float(result[metric])
        for result in results
        if isinstance(result.get(metric), (int, float))
        and not isinstance(result.get(metric), bool)
        and math.isfinite(float(result[metric]))
    ]
    if not values:
        return None
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def run_control(
    service: Any,
    *,
    riva_client_module: Any,
    text: str,
    artifact_dir: Path,
    grpc_uri: str,
    tts_locale: str = DEFAULT_TTS_LOCALE,
    voice: str = DEFAULT_VOICE,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
    repeats: int = DEFAULT_REPEATS,
    rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
    max_audio_duration_seconds: float = DEFAULT_MAX_AUDIO_DURATION_SECONDS,
    client_version: str = EXPECTED_CLIENT_VERSION,
    container_image: str = DEFAULT_CONTAINER_IMAGE,
    nim_profile: str = DEFAULT_NIM_PROFILE,
    image_digest: str = DEFAULT_IMAGE_DIGEST,
    custom_text_override: bool = False,
    clock: Callable[[], float] = time.perf_counter,
    timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    now: Callable[[], datetime] = utc_now,
    quiet: bool = False,
) -> tuple[dict[str, Any], Path]:
    require_exact_client_version(client_version)
    require_local_client_origin(
        getattr(riva_client_module, "__file__", None)
    )
    validate_request(
        tts_locale=tts_locale,
        voice=voice,
        sample_rate_hz=sample_rate_hz,
        repeats=repeats,
        rpc_timeout_seconds=rpc_timeout_seconds,
        max_audio_duration_seconds=max_audio_duration_seconds,
    )
    provenance = validate_declared_provenance(
        container_image=container_image,
        nim_profile=nim_profile,
        image_digest=image_digest,
    )
    port = grpc_port(grpc_uri)
    artifact_dir = _new_private_directory(artifact_dir)
    report_path = artifact_dir / REPORT_FILENAME

    # One mandatory warm-up is intentionally discarded and never summarized.
    synthesize_once(
        service,
        riva_client_module=riva_client_module,
        text=text,
        tts_locale=tts_locale,
        voice=voice,
        sample_rate_hz=sample_rate_hz,
        output_path=None,
        rpc_timeout_seconds=rpc_timeout_seconds,
        max_audio_duration_seconds=max_audio_duration_seconds,
        clock=clock,
        timer_factory=timer_factory,
    )

    started_at = now()
    results: list[dict[str, Any]] = []
    for repeat_index in range(1, repeats + 1):
        try:
            result = synthesize_once(
                service,
                riva_client_module=riva_client_module,
                text=text,
                tts_locale=tts_locale,
                voice=voice,
                sample_rate_hz=sample_rate_hz,
                output_path=(
                    artifact_dir
                    / f"magpie-control-repeat-{repeat_index:03d}.wav"
                ),
                rpc_timeout_seconds=rpc_timeout_seconds,
                max_audio_duration_seconds=max_audio_duration_seconds,
                clock=clock,
                timer_factory=timer_factory,
            )
        except Exception as exc:
            result = {"status": "failed", "error": safe_error(exc)}
        result["repeat_index"] = repeat_index
        results.append(result)
        if not quiet:
            if result["status"] == "succeeded":
                print(
                    f"repeat={repeat_index}/{repeats} "
                    f"ttfa={result['ttfa_seconds']:.3f}s "
                    f"wall={result['wall_time_seconds']:.3f}s "
                    f"audio={result['audio_duration_seconds']:.3f}s "
                    f"rtf={result['real_time_factor']:.3f} "
                    "margin="
                    f"{result['produced_audio_margin_seconds']:.3f}s"
                )
            else:
                print(
                    f"repeat={repeat_index}/{repeats} failed: "
                    f"{result['error']['category']}"
                )

    successful = [
        result for result in results if result["status"] == "succeeded"
    ]
    report = {
        "schema_version": 1,
        "diagnostic": "magpie_tts_matched_streaming_control",
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": now().isoformat(),
        "client": {
            "package": "nvidia-riva-client",
            "version": EXPECTED_CLIENT_VERSION,
            "version_gate": "exact_pep440_equality",
            "import_origin_verified": "repository_local_dot_python_packages",
            "streaming_api": "unary_stream",
        },
        "declared_unverified_model_provenance": provenance,
        "service": {
            "grpc_port": port,
            "model": "magpie-tts-multilingual",
        },
        "request": {
            "tts_locale": tts_locale,
            "voice": voice,
            "sample_rate_hz": sample_rate_hz,
            "channels": 1,
            "sample_width_bytes": 2,
            "encoding": "LINEAR_PCM",
            "text": identify_text(
                text,
                custom_override=custom_text_override,
            ),
            "warmup_request_count": 1,
            "measured_repeat_count": repeats,
            "rpc_timeout_seconds": rpc_timeout_seconds,
            "max_audio_duration_seconds": max_audio_duration_seconds,
            "max_audio_bytes": int(
                sample_rate_hz * 2 * max_audio_duration_seconds
            ),
        },
        "results": results,
        "aggregates": {
            metric: metric_summary(successful, metric)
            for metric in _AGGREGATE_METRICS
        },
        "summary": {
            "requested": repeats,
            "succeeded": len(successful),
            "failed": repeats - len(successful),
            "underrun_risk_detected_count": sum(
                result.get("underrun_risk_detected") is True
                for result in successful
            ),
            "wav_artifact_count": len(successful),
        },
        "privacy": {
            "contains_input_text": False,
            "contains_input_or_artifact_paths": False,
            "contains_text_fingerprint": False,
            "contains_service_hostname": False,
            "contains_audio_payload_in_json": False,
            "wav_files_contain_synthesized_speech": True,
            "audio_review_required_before_sharing": True,
            "artifact_directory_mode": "0700",
            "artifact_file_mode": "0600",
        },
    }
    _write_json_atomic(report_path, report)
    return report, report_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = load_text(args)
        custom_text_override = (
            args.text is not None or args.text_file is not None
        )
        require_exact_client_version(
            importlib.metadata.version("nvidia-riva-client")
        )
        validate_request(
            tts_locale=args.tts_locale,
            voice=DEFAULT_VOICE,
            sample_rate_hz=args.sample_rate_hz,
            repeats=args.repeats,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            max_audio_duration_seconds=args.max_audio_duration_seconds,
        )
        validate_declared_provenance(
            container_image=DEFAULT_CONTAINER_IMAGE,
            nim_profile=DEFAULT_NIM_PROFILE,
            image_digest=args.image_digest,
        )
        grpc_port(args.grpc_uri)
        import riva.client as riva_client

        require_local_client_origin(riva_client.__file__)
        auth = riva_client.Auth(uri=args.grpc_uri)
        service = riva_client.SpeechSynthesisService(auth)
    except Exception:
        print("Magpie control setup failed.", file=sys.stderr)
        return 2

    artifact_dir = args.artifact_dir or default_artifact_directory()
    try:
        report, report_path = run_control(
            service,
            riva_client_module=riva_client,
            text=text,
            artifact_dir=artifact_dir,
            grpc_uri=args.grpc_uri,
            tts_locale=args.tts_locale,
            voice=DEFAULT_VOICE,
            sample_rate_hz=args.sample_rate_hz,
            repeats=args.repeats,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            max_audio_duration_seconds=args.max_audio_duration_seconds,
            client_version=EXPECTED_CLIENT_VERSION,
            container_image=DEFAULT_CONTAINER_IMAGE,
            nim_profile=DEFAULT_NIM_PROFILE,
            image_digest=args.image_digest,
            custom_text_override=custom_text_override,
            quiet=args.quiet,
        )
    except Exception:
        print("Magpie control execution failed.", file=sys.stderr)
        return 1

    if not args.quiet:
        print(
            f"report={report_path} "
            f"succeeded={report['summary']['succeeded']} "
            f"failed={report['summary']['failed']}"
        )
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
