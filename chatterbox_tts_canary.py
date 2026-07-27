#!/usr/bin/env python3
"""Run an isolated Chatterbox TTS NIM exaggeration-factor canary.

The script targets the ``nvidia-riva-client==2.26.0`` streaming API. It keeps
the NMT target-language convention out of the TTS request by using an
independent ``es-ES`` TTS locale by default.

The JSON report intentionally excludes input text, input paths, service
hostnames, response payloads, and exception messages. WAV files necessarily
contain synthesized speech and must be reviewed separately before sharing.
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
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from tts_comparison_fixture import DEFAULT_TEXT, identify_text


DEFAULT_GRPC_URI = "localhost:50054"
DEFAULT_HTTP_BASE_URL = "http://localhost:9004"
DEFAULT_TTS_LOCALE = "es-ES"
DEFAULT_VOICE = "Chatterbox-Multilingual.es-ES.Male"
DEFAULT_SAMPLE_RATE_HZ = 22_050
DEFAULT_EXAGGERATION_FACTORS = (0.5, 0.7, 1.0, 1.5)
DEFAULT_REPEATS_PER_FACTOR = 3
DEFAULT_RPC_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_AUDIO_DURATION_SECONDS = 60.0
DEFAULT_IMAGE_DIGEST = (
    "sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6"
)
DEFAULT_CONTAINER_IMAGE = (
    "nvcr.io/nim/nvidia/chatterbox-tts-multilingual:"
    f"1.0.0@{DEFAULT_IMAGE_DIGEST}"
)
DEFAULT_NIM_PROFILE = "name=chatterbox-tts-multilingual"
REQUIRED_RIVA_CLIENT_VERSION = "2.26.0"
REPORT_FILENAME = "chatterbox-tts-canary.json"
VOICE_INVENTORY_PATH = "/v1/audio/list_voices"
MAX_INVENTORY_BYTES = 1024 * 1024
SUPPORTED_CHATTERBOX_LOCALES = frozenset(
    {
        "ar-SA",
        "da-DK",
        "de-DE",
        "el-GR",
        "en-US",
        "es-ES",
        "fi-FI",
        "fr-FR",
        "he-IL",
        "hi-IN",
        "it-IT",
        "ja-JP",
        "ko-KR",
        "ms-MY",
        "nb-NO",
        "nl-NL",
        "pl-PL",
        "pt-BR",
        "ru-RU",
        "sv-SE",
        "sw-KE",
        "tr-TR",
        "zh-CN",
    }
)
DOCUMENTED_CHATTERBOX_VOICES = {
    locale: f"Chatterbox-Multilingual.{locale}.Male"
    for locale in SUPPORTED_CHATTERBOX_LOCALES
}
_DOCUMENTED_VOICE_TO_LOCALE = {
    voice: locale
    for locale, voice in DOCUMENTED_CHATTERBOX_VOICES.items()
}
_IMAGE_RE = re.compile(
    r"^nvcr\.io/nim/nvidia/chatterbox-tts-multilingual:"
    r"(?P<tag>[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9][a-z0-9.-]*)?)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
_PROFILE_RE = re.compile(
    r"^name=chatterbox-tts-multilingual"
    r"(?:,batch_size=[1-9][0-9]{0,3})?$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINAL_PEP440_RELEASE_RE = re.compile(
    r"^\s*v?(?:(?P<epoch>0)!)?"
    r"(?P<release>[0-9]+(?:\.[0-9]+)*)\s*$",
    re.IGNORECASE,
)
_GRPC_STATUS_NAMES = {
    "OK",
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
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
_ERROR_CATEGORIES = {
    "rpc_deadline_exceeded",
    "audio_limit_exceeded",
    "invalid_audio_response",
    "rpc_or_runtime_error",
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
    """Raised after the watchdog actively cancels an over-deadline RPC."""


class AudioLimitExceeded(RuntimeError):
    """Raised after incremental PCM accounting exceeds the configured bound."""


def _env_or_default(name: str, default: str) -> str:
    return os.environ.get(name) or default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep Chatterbox TTS NIM emotion exaggeration while recording "
            "privacy-safe latency and duration metrics."
        )
    )
    text_group = parser.add_mutually_exclusive_group()
    text_group.add_argument(
        "--text",
        help=(
            "text to synthesize; omitted text uses a neutral Spanish fixture "
            "(the text is never copied to JSON)"
        ),
    )
    text_group.add_argument(
        "--text-file",
        type=Path,
        help=(
            "UTF-8 text file to synthesize (the path and text are never "
            "copied to JSON)"
        ),
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help=(
            "new artifact directory; default is a timestamped "
            "directory under experiment_results"
        ),
    )
    parser.add_argument("--grpc-uri", default=DEFAULT_GRPC_URI)
    parser.add_argument(
        "--http-base-url",
        default=DEFAULT_HTTP_BASE_URL,
        help=(
            "TTS NIM HTTP base URL used only for best-effort live voice "
            "inventory"
        ),
    )
    parser.add_argument(
        "--skip-voice-inventory",
        action="store_true",
        help="do not query the HTTP voice-inventory endpoint",
    )
    parser.add_argument(
        "--inventory-timeout-seconds",
        type=float,
        default=5.0,
    )
    parser.add_argument(
        "--tts-locale",
        "--language-code",
        dest="tts_locale",
        default=DEFAULT_TTS_LOCALE,
        help=(
            "TTS locale, independent of the NMT target-language code "
            "(default: es-ES)"
        ),
    )
    parser.add_argument(
        "--voice",
        help=(
            "explicit voice; otherwise select a matching discovered "
            "Chatterbox voice or derive the built-in locale voice"
        ),
    )
    parser.add_argument(
        "--sample-rate-hz",
        type=int,
        default=DEFAULT_SAMPLE_RATE_HZ,
    )
    parser.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
        help="hard per-request deadline enforced by cancelling the live RPC",
    )
    parser.add_argument(
        "--max-audio-duration-seconds",
        type=float,
        default=DEFAULT_MAX_AUDIO_DURATION_SECONDS,
        help="incremental PCM byte bound expressed as maximum audio duration",
    )
    parser.add_argument(
        "--repeats-per-factor",
        type=int,
        default=DEFAULT_REPEATS_PER_FACTOR,
        help="balanced measured repeats after one unrecorded warm-up request",
    )
    parser.add_argument(
        "--exaggeration-factors",
        type=float,
        nargs="+",
        default=list(DEFAULT_EXAGGERATION_FACTORS),
        metavar="FACTOR",
    )
    parser.add_argument(
        "--container-image",
        "--image",
        dest="container_image",
        default=_env_or_default(
            "CHATTERBOX_TTS_IMAGE", DEFAULT_CONTAINER_IMAGE
        ),
    )
    parser.add_argument(
        "--nim-profile",
        "--profile",
        dest="nim_profile",
        default=_env_or_default(
            "CHATTERBOX_TTS_NIM_TAGS_SELECTOR", DEFAULT_NIM_PROFILE
        ),
    )
    parser.add_argument(
        "--image-digest",
        default=_env_or_default(
            "CHATTERBOX_TTS_IMAGE_DIGEST", DEFAULT_IMAGE_DIGEST
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_artifact_directory(now: Optional[datetime] = None) -> Path:
    timestamp = (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return Path("experiment_results") / f"chatterbox-tts-canary-{timestamp}"


def load_text(args: argparse.Namespace) -> str:
    if args.text_file is not None:
        text = args.text_file.expanduser().read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = DEFAULT_TEXT
    text = text.strip()
    if not text:
        raise ValueError("synthesis text must contain non-whitespace content")
    return text


def validate_exaggeration_factors(values: Iterable[float]) -> tuple[float, ...]:
    factors = tuple(values)
    if not factors:
        raise ValueError("at least one exaggeration factor is required")
    for factor in factors:
        if (
            not isinstance(factor, (int, float))
            or isinstance(factor, bool)
            or not math.isfinite(factor)
            or factor < 0.25
            or factor > 2.0
        ):
            raise ValueError(
                "exaggeration factors must be finite values in [0.25, 2.0]"
            )
    if len(set(factors)) != len(factors):
        raise ValueError("exaggeration factors must be unique")
    return tuple(float(value) for value in factors)


def validate_audio_format(sample_rate_hz: int) -> None:
    if (
        not isinstance(sample_rate_hz, int)
        or isinstance(sample_rate_hz, bool)
        or sample_rate_hz <= 0
    ):
        raise ValueError("sample rate must be a positive integer")


def validate_positive_finite(name: str, value: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


def validate_repeats(repeats_per_factor: int) -> int:
    if (
        not isinstance(repeats_per_factor, int)
        or isinstance(repeats_per_factor, bool)
        or not 1 <= repeats_per_factor <= 100
    ):
        raise ValueError("repeats per factor must be an integer in [1, 100]")
    return repeats_per_factor


def validate_declared_model_provenance(
    *,
    container_image: str,
    nim_profile: str,
    image_digest: str,
) -> dict[str, str]:
    image_match = (
        _IMAGE_RE.fullmatch(container_image)
        if isinstance(container_image, str)
        else None
    )
    if image_match is None:
        raise ValueError(
            "container image must be the Chatterbox TTS NIM repository "
            "with a pinned semantic-version tag and sha256 digest"
        )
    if (
        not isinstance(nim_profile, str)
        or _PROFILE_RE.fullmatch(nim_profile) is None
    ):
        raise ValueError("unsupported or unsafe Chatterbox NIM profile")
    if (
        not isinstance(image_digest, str)
        or _DIGEST_RE.fullmatch(image_digest) is None
    ):
        raise ValueError("image digest must use sha256 followed by 64 hex digits")
    embedded_digest = image_match.group("digest")
    if embedded_digest != image_digest:
        raise ValueError(
            "container image reference digest must match the declared digest"
        )
    tag = image_match.group("tag")
    return {
        "verification_status": "declared_unverified",
        "runtime_attested": False,
        "declared_release": tag,
        "declared_container_image": container_image,
        "declared_image_repository": (
            "nvcr.io/nim/nvidia/chatterbox-tts-multilingual"
        ),
        "declared_image_tag": tag,
        "declared_image_digest": image_digest,
        "declared_nim_profile": nim_profile,
    }


def require_supported_riva_client(version: str) -> None:
    """Require the exact final PEP 440 release equivalent to 2.26.0."""
    match = (
        _FINAL_PEP440_RELEASE_RE.fullmatch(version)
        if isinstance(version, str)
        else None
    )
    if match is None:
        raise RuntimeError(
            f"nvidia-riva-client=={REQUIRED_RIVA_CLIENT_VERSION} is required "
            "as a final release for "
            "custom_configuration and bidirectional streaming TTS"
        )
    release = tuple(int(value) for value in match.group("release").split("."))
    target = tuple(
        int(value) for value in REQUIRED_RIVA_CLIENT_VERSION.split(".")
    )
    width = max(len(release), len(target))
    if release + (0,) * (width - len(release)) != target + (0,) * (
        width - len(target)
    ):
        raise RuntimeError(
            f"nvidia-riva-client=={REQUIRED_RIVA_CLIENT_VERSION} is required "
            "as a final release for custom_configuration and bidirectional "
            "streaming TTS"
        )


def require_local_client_origin(
    module_file: Optional[str],
    *,
    repository_root: Optional[Path] = None,
) -> Path:
    if not module_file:
        raise RuntimeError("cannot verify the imported Riva client location")
    root = (repository_root or Path(__file__).resolve().parent).resolve()
    expected = (root / ".python-packages-chatterbox").resolve()
    imported = Path(module_file).resolve()
    try:
        imported.relative_to(expected)
    except ValueError as exc:
        raise RuntimeError(
            "Riva client must be imported from repository-local "
            ".python-packages-chatterbox"
        ) from exc
    return imported


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


def _http_port(base_url: str) -> int:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--http-base-url must be an HTTP or HTTPS base URL")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("--http-base-url must not include a path, query, or fragment")
    try:
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("--http-base-url contains an invalid port") from exc


def _grpc_port(grpc_uri: str) -> int:
    host, separator, port_text = grpc_uri.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError("--grpc-uri must use host:port form")
    port = int(port_text)
    if not 1 <= port <= 65_535:
        raise ValueError("--grpc-uri contains an invalid port")
    return port


def _inventory_url(base_url: str) -> str:
    return base_url.rstrip("/") + VOICE_INVENTORY_PATH


def _read_json_response(response: Any) -> Any:
    payload = response.read(MAX_INVENTORY_BYTES + 1)
    if len(payload) > MAX_INVENTORY_BYTES:
        raise ValueError("voice inventory exceeded the response-size limit")
    return json.loads(payload.decode("utf-8"))


def fetch_voice_inventory(
    http_base_url: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Fetch and reduce the live inventory to safe Chatterbox voice names."""
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("voice inventory timeout must be positive and finite")
    _http_port(http_base_url)
    request = Request(
        _inventory_url(http_base_url),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=float(timeout_seconds)) as response:
            status_code = int(getattr(response, "status", 200))
            if status_code != 200:
                raise RuntimeError("voice inventory returned a non-success status")
            payload = _read_json_response(response)
        voices = normalize_voice_inventory(payload)
        return {
            "attempted": True,
            "succeeded": True,
            "matched_chatterbox_voice_count": len(voices),
            "voices": voices,
        }
    except Exception as exc:
        return {
            "attempted": True,
            "succeeded": False,
            "matched_chatterbox_voice_count": 0,
            "voices": [],
            "error": safe_error(exc),
        }


def normalize_voice_inventory(payload: Any) -> list[dict[str, str]]:
    """Extract only NVIDIA-documented built-in Chatterbox voices."""
    candidates: set[tuple[str, str]] = set()

    def add_candidate(name: Any, locale: Any = "") -> None:
        del locale
        if not isinstance(name, str):
            return
        documented_locale = _DOCUMENTED_VOICE_TO_LOCALE.get(name)
        if documented_locale is None:
            return
        candidates.add((documented_locale, name))

    def walk(value: Any, inherited_locale: str = "") -> None:
        if isinstance(value, Mapping):
            locale = inherited_locale
            for key in ("language_code", "locale", "language"):
                candidate = value.get(key)
                if candidate in SUPPORTED_CHATTERBOX_LOCALES:
                    locale = candidate
                    break
            for key in ("voice_name", "voice"):
                add_candidate(value.get(key), locale)
            voices = value.get("voices")
            if isinstance(voices, Sequence) and not isinstance(
                voices, (str, bytes, bytearray)
            ):
                for voice in voices:
                    if isinstance(voice, str):
                        add_candidate(voice, locale)
            for nested in value.values():
                walk(nested, locale)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for nested in value:
                if isinstance(nested, str):
                    add_candidate(nested, inherited_locale)
                else:
                    walk(nested, inherited_locale)

    walk(payload)
    return [
        {"locale": locale, "voice": voice}
        for locale, voice in sorted(candidates)
    ]


def resolve_voice(
    *,
    tts_locale: str,
    explicit_voice: Optional[str],
    inventory: Mapping[str, Any],
) -> str:
    if tts_locale not in SUPPORTED_CHATTERBOX_LOCALES:
        raise ValueError("unsupported Chatterbox TTS locale")
    documented_voice = DOCUMENTED_CHATTERBOX_VOICES[tts_locale]
    if explicit_voice is not None:
        voice = explicit_voice.strip()
        if voice != documented_voice:
            raise ValueError(
                "voice must be NVIDIA's documented built-in voice "
                "for the selected locale"
            )
        return voice
    for item in inventory.get("voices", []):
        if (
            isinstance(item, Mapping)
            and item.get("locale") == tts_locale
            and item.get("voice") == documented_voice
        ):
            return documented_voice
    return documented_voice


def validate_tts_voice(tts_locale: str, voice: str) -> None:
    resolved = resolve_voice(
        tts_locale=tts_locale,
        explicit_voice=voice,
        inventory={"voices": []},
    )
    if resolved != voice:
        raise ValueError("unsupported Chatterbox TTS voice")


def sanitize_inventory_for_report(
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate caller-provided inventory before copying it to JSON."""
    voices = normalize_voice_inventory(inventory.get("voices", []))
    attempted = inventory.get("attempted") is True
    succeeded = inventory.get("succeeded") is True
    result: dict[str, Any] = {
        "attempted": attempted,
        "succeeded": succeeded,
        "matched_chatterbox_voice_count": len(voices),
        "voices": voices,
    }
    error = inventory.get("error")
    if isinstance(error, Mapping):
        category = error.get("category", "")
        grpc_status = error.get("grpc_status", "")
        if category not in _ERROR_CATEGORIES:
            category = "rpc_or_runtime_error"
        if grpc_status not in _GRPC_STATUS_NAMES:
            grpc_status = ""
        result["error"] = {
            "category": category,
            "grpc_status": grpc_status,
        }
    return result


def factor_label(factor: float) -> str:
    return f"{factor:.8g}".replace(".", "p")


def wav_filename(
    *,
    factor_index: int,
    factor: float,
    repeat_index: int,
) -> str:
    return (
        f"chatterbox-factor-{factor_index:02d}-{factor_label(factor)}"
        f"-repeat-{repeat_index:02d}.wav"
    )


def _new_private_directory(path: Path) -> Path:
    """Create a normalized fresh 0700 tree without following symlinks."""
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


def _prepare_artifact_paths(
    artifact_dir: Path,
    factors: Sequence[float],
    repeats_per_factor: int,
) -> tuple[Path, dict[tuple[int, int], Path]]:
    artifact_dir = _new_private_directory(artifact_dir)
    report_path = artifact_dir / REPORT_FILENAME
    wav_paths = {
        (factor_index, repeat_index): artifact_dir
        / wav_filename(
            factor_index=factor_index,
            factor=factor,
            repeat_index=repeat_index,
        )
        for factor_index, factor in enumerate(factors, start=1)
        for repeat_index in range(1, repeats_per_factor + 1)
    }
    return report_path, wav_paths


def _private_temporary_file(path: Path) -> tuple[int, Path]:
    descriptor, raw_path = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    os.fchmod(descriptor, 0o600)
    return descriptor, Path(raw_path)


def _publish_private_file(temporary: Path, destination: Path) -> None:
    """Atomically publish without replacing a path created after preflight."""
    os.link(temporary, destination)
    os.chmod(destination, 0o600)
    temporary.unlink()
    directory_fd = os.open(
        destination.parent, os.O_RDONLY | os.O_DIRECTORY
    )
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


def _continuity_metrics(
    *,
    arrival_times: Sequence[float],
    chunk_bytes: Sequence[int],
    first_audio_at: float,
    completed_at: float,
    sample_rate_hz: int,
) -> dict[str, Any]:
    inter_chunk_gaps = [
        current - previous
        for previous, current in zip(arrival_times, arrival_times[1:])
    ]
    bytes_per_second = sample_rate_hz * 2
    cumulative_audio_s = chunk_bytes[0] / bytes_per_second
    margins = []
    for arrival, audio_bytes in zip(arrival_times[1:], chunk_bytes[1:]):
        elapsed = arrival - first_audio_at
        margins.append(cumulative_audio_s - elapsed)
        cumulative_audio_s += audio_bytes / bytes_per_second
    last_audio_at = arrival_times[-1]
    post_first_delivery_s = last_audio_at - first_audio_at
    produced_margin_s = cumulative_audio_s - post_first_delivery_s
    margins.append(produced_margin_s)
    minimum_margin_s = min(margins)
    underrun_risk_s = max(0.0, -minimum_margin_s)
    rpc_completion_tail_s = completed_at - last_audio_at
    return {
        "inter_audio_chunk_gap_count": len(inter_chunk_gaps),
        "inter_audio_chunk_gap_p50_seconds": percentile(
            inter_chunk_gaps, 0.50
        ),
        "inter_audio_chunk_gap_p95_seconds": percentile(
            inter_chunk_gaps, 0.95
        ),
        "inter_audio_chunk_gap_max_seconds": (
            max(inter_chunk_gaps) if inter_chunk_gaps else None
        ),
        "post_first_audio_delivery_seconds": post_first_delivery_s,
        "produced_audio_margin_seconds": produced_margin_s,
        "minimum_produced_audio_margin_seconds": minimum_margin_s,
        "underrun_risk_seconds": underrun_risk_s,
        "underrun_risk_detected": underrun_risk_s > 0,
        "rpc_completion_tail_seconds": rpc_completion_tail_s,
    }


def _cancel_rpc(call: Any) -> None:
    cancel = getattr(call, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            pass


def synthesize_factor(
    service: Any,
    *,
    riva_client_module: Any,
    text: str,
    tts_locale: str,
    voice: str,
    sample_rate_hz: int,
    exaggeration_factor: float,
    cfg_weight: Optional[float] = None,
    output_path: Optional[Path],
    rpc_timeout_seconds: float,
    max_audio_duration_seconds: float,
    clock: Callable[[], float] = time.perf_counter,
    timer_factory: Callable[[float, Callable[[], None]], Any] = (
        threading.Timer
    ),
) -> dict[str, Any]:
    timeout_s = validate_positive_finite(
        "RPC timeout", rpc_timeout_seconds
    )
    duration_limit_s = validate_positive_finite(
        "maximum audio duration", max_audio_duration_seconds
    )
    max_audio_bytes = int(sample_rate_hz * 2 * duration_limit_s)
    if max_audio_bytes <= 0:
        raise ValueError("maximum audio duration produces a zero-byte limit")

    custom_configuration = {
        "exaggeration_factor": f"{exaggeration_factor:g}"
    }
    if cfg_weight is not None:
        if (
            not isinstance(cfg_weight, (int, float))
            or isinstance(cfg_weight, bool)
            or not math.isfinite(cfg_weight)
        ):
            raise ValueError("cfg_weight must be finite when provided")
        custom_configuration["cfg_weight"] = f"{cfg_weight:g}"

    started = clock()
    responses = service.synthesize_online(
        text=text,
        voice_name=voice,
        language_code=tts_locale,
        encoding=riva_client_module.AudioEncoding.LINEAR_PCM,
        sample_rate_hz=sample_rate_hz,
        custom_configuration=custom_configuration,
    )
    if not callable(getattr(responses, "cancel", None)):
        raise RuntimeError(
            "streaming TTS call is not cancellable; cannot enforce deadline"
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

    timer = timer_factory(timeout_s, cancel_at_deadline)
    timer_cancel = getattr(timer, "cancel", None)
    timer_join = getattr(timer, "join", None)
    if not callable(timer_cancel) or not callable(timer_join):
        _cancel_rpc(responses)
        raise RuntimeError(
            "deadline timer must support cancel() and join()"
        )
    if hasattr(timer, "daemon"):
        timer.daemon = True
    try:
        timer.start()
    except Exception:
        with deadline_lock:
            deadline_state["armed"] = False
        timer_cancel()
        _cancel_rpc(responses)
        raise

    pcm_chunks: list[bytes] = []
    audio_arrival_times: list[float] = []
    audio_chunk_bytes: list[int] = []
    empty_response_count = 0
    first_audio_at: Optional[float] = None
    total_audio_bytes = 0
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
            next_total_audio_bytes = total_audio_bytes + len(audio)
            if next_total_audio_bytes > max_audio_bytes:
                raise AudioLimitExceeded(
                    "TTS audio exceeded the configured incremental byte limit"
                )
            if first_audio_at is None:
                first_audio_at = received_at
            total_audio_bytes = next_total_audio_bytes
            audio_arrival_times.append(received_at)
            audio_chunk_bytes.append(len(audio))
            pcm_chunks.append(audio)
        completed = clock()
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
            "streaming TTS RPC exceeded its configured deadline"
        ) from caught_exception
    if caught_exception is not None:
        _cancel_rpc(responses)
        raise caught_exception
    if not pcm_chunks or first_audio_at is None:
        raise RuntimeError("TTS returned no audio")

    pcm = b"".join(pcm_chunks)
    audio_duration_s = total_audio_bytes / (sample_rate_hz * 2)
    wall_time_s = completed - started
    ttfa_s = first_audio_at - started
    if (
        audio_duration_s <= 0
        or wall_time_s < 0
        or ttfa_s < 0
        or first_audio_at > completed
        or audio_arrival_times[-1] > completed
        or any(
            current < previous
            for previous, current in zip(
                audio_arrival_times, audio_arrival_times[1:]
            )
        )
    ):
        raise RuntimeError("invalid synthesis timing or audio duration")

    result = {
        "exaggeration_factor": exaggeration_factor,
        "status": "succeeded",
        "ttfa_seconds": ttfa_s,
        "wall_time_seconds": wall_time_s,
        "audio_chunk_count": len(pcm_chunks),
        "empty_response_count": empty_response_count,
        "audio_bytes": total_audio_bytes,
        "audio_duration_seconds": audio_duration_s,
        "real_time_factor": wall_time_s / audio_duration_s,
        **_continuity_metrics(
            arrival_times=audio_arrival_times,
            chunk_bytes=audio_chunk_bytes,
            first_audio_at=first_audio_at,
            completed_at=completed,
            sample_rate_hz=sample_rate_hz,
        ),
    }
    if output_path is not None:
        _write_wav_atomic(output_path, pcm, sample_rate_hz=sample_rate_hz)
        result["wav_file"] = output_path.name
    return result


def _metric_summary(
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


def build_factor_aggregates(
    results: Sequence[Mapping[str, Any]],
    factors: Sequence[float],
    repeats_per_factor: int,
) -> list[dict[str, Any]]:
    aggregates = []
    for factor_index, factor in enumerate(factors, start=1):
        factor_results = [
            result
            for result in results
            if result.get("factor_index") == factor_index
        ]
        successful = [
            result
            for result in factor_results
            if result.get("status") == "succeeded"
        ]
        aggregates.append(
            {
                "factor_index": factor_index,
                "exaggeration_factor": factor,
                "requested_repeats": repeats_per_factor,
                "succeeded": len(successful),
                "failed": len(factor_results) - len(successful),
                "underrun_risk_detected_count": sum(
                    result.get("underrun_risk_detected") is True
                    for result in successful
                ),
                "metrics": {
                    metric: _metric_summary(successful, metric)
                    for metric in _AGGREGATE_METRICS
                },
            }
        )
    return aggregates


def run_sweep(
    service: Any,
    *,
    riva_client_module: Any,
    text: str,
    tts_locale: str,
    voice: str,
    sample_rate_hz: int,
    factors: Sequence[float],
    artifact_dir: Path,
    inventory: Mapping[str, Any],
    grpc_uri: str,
    http_base_url: str,
    client_version: str,
    rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
    max_audio_duration_seconds: float = (
        DEFAULT_MAX_AUDIO_DURATION_SECONDS
    ),
    repeats_per_factor: int = DEFAULT_REPEATS_PER_FACTOR,
    container_image: str = DEFAULT_CONTAINER_IMAGE,
    nim_profile: str = DEFAULT_NIM_PROFILE,
    image_digest: str = DEFAULT_IMAGE_DIGEST,
    include_warmup: bool = True,
    custom_text_override: bool = False,
    clock: Callable[[], float] = time.perf_counter,
    timer_factory: Callable[[float, Callable[[], None]], Any] = (
        threading.Timer
    ),
    now: Callable[[], datetime] = utc_now,
    quiet: bool = False,
) -> tuple[dict[str, Any], Path]:
    require_supported_riva_client(client_version)
    require_local_client_origin(
        getattr(riva_client_module, "__file__", None)
    )
    validate_tts_voice(tts_locale, voice)
    validate_audio_format(sample_rate_hz)
    resolved_factors = validate_exaggeration_factors(factors)
    repeats = validate_repeats(repeats_per_factor)
    timeout_s = validate_positive_finite(
        "RPC timeout", rpc_timeout_seconds
    )
    duration_limit_s = validate_positive_finite(
        "maximum audio duration", max_audio_duration_seconds
    )
    declared_provenance = validate_declared_model_provenance(
        container_image=container_image,
        nim_profile=nim_profile,
        image_digest=image_digest,
    )
    grpc_port = _grpc_port(grpc_uri)
    http_port = _http_port(http_base_url)
    report_path, wav_paths = _prepare_artifact_paths(
        artifact_dir, resolved_factors, repeats
    )
    started_at = now()
    sanitized_inventory = sanitize_inventory_for_report(inventory)

    warmup_result: dict[str, Any]
    if include_warmup:
        try:
            synthesize_factor(
                service,
                riva_client_module=riva_client_module,
                text=text,
                tts_locale=tts_locale,
                voice=voice,
                sample_rate_hz=sample_rate_hz,
                exaggeration_factor=resolved_factors[0],
                output_path=None,
                rpc_timeout_seconds=timeout_s,
                max_audio_duration_seconds=duration_limit_s,
                clock=clock,
                timer_factory=timer_factory,
            )
            warmup_status = "succeeded"
        except Exception:
            warmup_status = "failed"
        warmup_result = {
            "status": warmup_status,
            "exaggeration_factor": resolved_factors[0],
            "audio_artifact_written": False,
        }
    else:
        warmup_result = {
            "status": "skipped",
            "exaggeration_factor": resolved_factors[0],
            "audio_artifact_written": False,
        }

    results = []
    execution_index = 0
    factor_count = len(resolved_factors)
    for repeat_index in range(1, repeats + 1):
        rotation = (repeat_index - 1) % factor_count
        ordered_factor_offsets = (
            list(range(rotation, factor_count))
            + list(range(0, rotation))
        )
        for factor_offset in ordered_factor_offsets:
            factor_index = factor_offset + 1
            factor = resolved_factors[factor_offset]
            execution_index += 1
            try:
                result = synthesize_factor(
                    service,
                    riva_client_module=riva_client_module,
                    text=text,
                    tts_locale=tts_locale,
                    voice=voice,
                    sample_rate_hz=sample_rate_hz,
                    exaggeration_factor=factor,
                    output_path=wav_paths[
                        (factor_index, repeat_index)
                    ],
                    rpc_timeout_seconds=timeout_s,
                    max_audio_duration_seconds=duration_limit_s,
                    clock=clock,
                    timer_factory=timer_factory,
                )
            except Exception as exc:
                result = {
                    "exaggeration_factor": factor,
                    "status": "failed",
                    "error": safe_error(exc),
                }
            result.update(
                {
                    "factor_index": factor_index,
                    "repeat_index": repeat_index,
                    "execution_index": execution_index,
                }
            )
            results.append(result)
            if not quiet:
                if result["status"] == "succeeded":
                    print(
                        f"factor={factor:g} repeat={repeat_index}/{repeats} "
                        f"ttfa={result['ttfa_seconds']:.3f}s "
                        f"wall={result['wall_time_seconds']:.3f}s "
                        f"audio={result['audio_duration_seconds']:.3f}s "
                        f"rtf={result['real_time_factor']:.3f} "
                        "margin="
                        f"{result['produced_audio_margin_seconds']:.3f}s"
                    )
                else:
                    print(
                        f"factor={factor:g} repeat={repeat_index}/{repeats} "
                        f"failed: {result['error']['category']}"
                    )

    factor_aggregates = build_factor_aggregates(
        results, resolved_factors, repeats
    )
    completed_at = now()
    report = {
        "schema_version": 2,
        "diagnostic": "chatterbox_tts_exaggeration_canary",
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": completed_at.isoformat(),
        "client": {
            "package": "nvidia-riva-client",
            "reported_version": client_version,
            "required_version": REQUIRED_RIVA_CLIENT_VERSION,
            "streaming_api": "bidirectional",
        },
        "declared_model_provenance": declared_provenance,
        "service": {
            "grpc_port": grpc_port,
            "http_port": http_port,
            "requested_model": "chatterbox-tts-multilingual",
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
            "exaggeration_factors": list(resolved_factors),
            "warmup_request_count": 1 if include_warmup else 0,
            "repeats_per_factor": repeats,
            "rpc_timeout_seconds": timeout_s,
            "max_audio_duration_seconds": duration_limit_s,
            "max_audio_bytes": int(
                sample_rate_hz * 2 * duration_limit_s
            ),
        },
        "voice_inventory": sanitized_inventory,
        "warmup": warmup_result,
        "results": results,
        "factor_aggregates": factor_aggregates,
        "summary": {
            "requested": len(results),
            "succeeded": sum(
                result["status"] == "succeeded" for result in results
            ),
            "failed": sum(result["status"] == "failed" for result in results),
            "warmup_status": warmup_result["status"],
        },
        "privacy": {
            "contains_input_text": False,
            "contains_input_path": False,
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


def _disabled_inventory() -> dict[str, Any]:
    return {
        "attempted": False,
        "succeeded": False,
        "matched_chatterbox_voice_count": 0,
        "voices": [],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = load_text(args)
        custom_text_override = (
            args.text is not None or args.text_file is not None
        )
        factors = validate_exaggeration_factors(args.exaggeration_factors)
        validate_audio_format(args.sample_rate_hz)
        validate_positive_finite(
            "RPC timeout", args.rpc_timeout_seconds
        )
        validate_positive_finite(
            "maximum audio duration",
            args.max_audio_duration_seconds,
        )
        validate_repeats(args.repeats_per_factor)
        validate_declared_model_provenance(
            container_image=args.container_image,
            nim_profile=args.nim_profile,
            image_digest=args.image_digest,
        )
        _grpc_port(args.grpc_uri)
        _http_port(args.http_base_url)
        if (
            not math.isfinite(args.inventory_timeout_seconds)
            or args.inventory_timeout_seconds <= 0
        ):
            raise ValueError(
                "--inventory-timeout-seconds must be positive and finite"
            )
        client_version = importlib.metadata.version("nvidia-riva-client")
        require_supported_riva_client(client_version)
        import riva.client as riva_client
        require_local_client_origin(riva_client.__file__)
    except (
        ImportError,
        importlib.metadata.PackageNotFoundError,
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
    ):
        print("Canary setup failed.", file=sys.stderr)
        return 2

    artifact_dir = args.artifact_dir or default_artifact_directory()
    inventory = (
        _disabled_inventory()
        if args.skip_voice_inventory
        else fetch_voice_inventory(
            args.http_base_url,
            timeout_seconds=args.inventory_timeout_seconds,
        )
    )
    try:
        voice = resolve_voice(
            tts_locale=args.tts_locale,
            explicit_voice=args.voice,
            inventory=inventory,
        )
        auth = riva_client.Auth(uri=args.grpc_uri)
        service = riva_client.SpeechSynthesisService(auth)
    except Exception:
        print("Canary connection setup failed.", file=sys.stderr)
        return 1

    try:
        report, report_path = run_sweep(
            service,
            riva_client_module=riva_client,
            text=text,
            tts_locale=args.tts_locale,
            voice=voice,
            sample_rate_hz=args.sample_rate_hz,
            factors=factors,
            artifact_dir=artifact_dir,
            inventory=inventory,
            grpc_uri=args.grpc_uri,
            http_base_url=args.http_base_url,
            client_version=client_version,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            max_audio_duration_seconds=(
                args.max_audio_duration_seconds
            ),
            repeats_per_factor=args.repeats_per_factor,
            container_image=args.container_image,
            nim_profile=args.nim_profile,
            image_digest=args.image_digest,
            custom_text_override=custom_text_override,
            quiet=args.quiet,
        )
    except Exception:
        print("Canary execution failed.", file=sys.stderr)
        return 1
    finally:
        channel = getattr(auth, "channel", None)
        if channel is not None:
            channel.close()

    if not args.quiet:
        print(f"Privacy-safe JSON report: {report_path}")
    return (
        0
        if report["summary"]["failed"] == 0
        and report["summary"]["warmup_status"] == "succeeded"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
