#!/usr/bin/env python3
"""Run and blind the fixed multi-text Magpie/Chatterbox TTS gate.

The two TTS arms require different pinned Riva client releases, so this
orchestrator launches the existing arm-specific runners in separate
``python3 -S`` subprocesses. Public diagnostic JSON excludes input text,
paths, text fingerprints, model-to-clip mappings, and raw errors.

Generated WAVs, the blinding key, and reviewer-level scores are private
artifacts and must never be committed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import secrets
import stat
import statistics
import subprocess
import sys
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from tts_multitext_corpus import (
    CORPUS_ID,
    CORPUS_VERSION,
    CorpusItem,
    corpus_identity,
    validate_corpus,
)


REPORT_FILENAME = "tts-multitext-gate.json"
BLINDING_KEY_FILENAME = "blinding-key.json"
DEFAULT_REPEATS = 5
LISTENING_REPEAT_INDEXES = (2, 4)
DEFAULT_REVIEWER_COUNT = 3
CHATTERBOX_EXAGGERATION_FACTOR = 0.5
MAX_CHILD_SECONDS = 900.0

MAX_OVERALL_DURATION_RATIO = 0.92
MAX_SHORTER_FIXTURE_RATIO = 0.95
MIN_SHORTER_FIXTURES = 4
MAX_CHATTERBOX_MEDIAN_TTFA_SECONDS = 1.25
MECHANICAL_PROTOCOL_VERSION = 2
MAX_CHATTERBOX_STARTUP_BUFFER_SECONDS = 1.25
SAMPLE_RATE_HZ = 22_050
MAX_REVIEW_WAV_BYTES = SAMPLE_RATE_HZ * 2 * 60 + 4096

MAGPIE_CLIENT_VERSION = "2.24.0"
MAGPIE_LOCALE = "es-US"
MAGPIE_VOICE = "Magpie-Multilingual.ES-US.Isabela"
MAGPIE_IMAGE = "nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0"
MAGPIE_PROFILE = "name=magpie-tts-multilingual,batch_size=8"
MAGPIE_DIGEST = (
    "sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d"
)

CHATTERBOX_CLIENT_VERSION = "2.26.0"
CHATTERBOX_LOCALE = "es-ES"
CHATTERBOX_VOICE = "Chatterbox-Multilingual.es-ES.Male"
CHATTERBOX_IMAGE = (
    "nvcr.io/nim/nvidia/chatterbox-tts-multilingual:1.0.0@"
    "sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6"
)
CHATTERBOX_PROFILE = "name=chatterbox-tts-multilingual"
CHATTERBOX_DIGEST = (
    "sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6"
)

_ARM_MAGPIE = "magpie"
_ARM_CHATTERBOX = "chatterbox"
_ARMS = (_ARM_MAGPIE, _ARM_CHATTERBOX)
_METRICS = (
    "audio_duration_seconds",
    "ttfa_seconds",
    "wall_time_seconds",
    "real_time_factor",
    "underrun_risk_seconds",
)
_PASS_ONE_FIELDS = (
    "reviewer_code",
    "pair_id",
    "clip_a_naturalness_1_5",
    "clip_a_pace_1_5",
    "clip_a_prosody_1_5",
    "clip_a_listening_ease_1_5",
    "clip_a_live_acceptable_yes_no",
    "clip_a_unrateable_yes_no",
    "clip_b_naturalness_1_5",
    "clip_b_pace_1_5",
    "clip_b_prosody_1_5",
    "clip_b_listening_ease_1_5",
    "clip_b_live_acceptable_yes_no",
    "clip_b_unrateable_yes_no",
    "preferred_clip_a_b_tie",
)
_PASS_TWO_FIELDS = (
    "reviewer_code",
    "pair_id",
    "fixture_id",
    "reference_text",
    "clip_a_intelligibility_1_5",
    "clip_a_pronunciation_1_5",
    "clip_a_text_fidelity_1_5",
    "clip_a_omission_yes_no",
    "clip_a_wrong_word_substitution_yes_no",
    "clip_a_hallucination_yes_no",
    "clip_a_mixed_language_yes_no",
    "clip_a_clipped_boundary_yes_no",
    "clip_a_glitch_or_distortion_yes_no",
    "clip_a_unnatural_pause_yes_no",
    "clip_a_unrateable_yes_no",
    "clip_b_intelligibility_1_5",
    "clip_b_pronunciation_1_5",
    "clip_b_text_fidelity_1_5",
    "clip_b_omission_yes_no",
    "clip_b_wrong_word_substitution_yes_no",
    "clip_b_hallucination_yes_no",
    "clip_b_mixed_language_yes_no",
    "clip_b_clipped_boundary_yes_no",
    "clip_b_glitch_or_distortion_yes_no",
    "clip_b_unnatural_pause_yes_no",
    "clip_b_unrateable_yes_no",
)

ProcessRunner = Callable[
    [Sequence[str], Mapping[str, str], Path, float],
    int,
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_artifact_directory(
    now: Optional[datetime] = None,
) -> Path:
    stamp = (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return Path("experiment_results") / f"tts-multitext-gate-{stamp}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixed Magpie/Chatterbox multi-text timing gate and "
            "build private model-label-blind native-listener bundles."
        )
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help=(
            "fresh artifact directory; defaults to a timestamped directory "
            "under experiment_results"
        ),
    )
    parser.add_argument(
        "--reviewer-count",
        type=int,
        default=DEFAULT_REVIEWER_COUNT,
        help="anonymous blinded reviewer bundles to create (2-8)",
    )
    parser.add_argument(
        "--child-timeout-seconds",
        type=float,
        default=MAX_CHILD_SECONDS,
        help="hard timeout for each arm/fixture subprocess",
    )
    parser.add_argument(
        "--allow-artifact-outside-results",
        action="store_true",
        help=(
            "allow private artifacts outside the repository's ignored "
            "experiment_results tree; use only for an approved private path"
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def validate_options(
    *,
    reviewer_count: int,
    child_timeout_seconds: float,
) -> None:
    if (
        not isinstance(reviewer_count, int)
        or isinstance(reviewer_count, bool)
        or not 2 <= reviewer_count <= 8
    ):
        raise ValueError("reviewer count must be an integer in [2, 8]")
    if (
        not isinstance(child_timeout_seconds, (int, float))
        or isinstance(child_timeout_seconds, bool)
        or not math.isfinite(child_timeout_seconds)
        or child_timeout_seconds <= 0
    ):
        raise ValueError("child timeout must be positive and finite")


def _resolve_artifact_directory(
    artifact_dir: Path,
    *,
    repository_root: Path,
    allow_outside_results: bool,
) -> Path:
    candidate = (
        artifact_dir
        if artifact_dir.is_absolute()
        else repository_root / artifact_dir
    )
    resolved = Path(
        os.path.abspath(os.fspath(candidate.expanduser()))
    )
    formal_root = (repository_root / "experiment_results").resolve()
    if not allow_outside_results:
        try:
            resolved.relative_to(formal_root)
        except ValueError as exc:
            raise ValueError(
                "formal artifacts must stay under ignored "
                "experiment_results"
            ) from exc
        if resolved == formal_root:
            raise ValueError(
                "artifact directory must be a fresh child of "
                "experiment_results"
            )
    return resolved


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


def _new_private_subdirectory(parent: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ValueError("invalid private subdirectory name")
    destination = parent / name
    if destination.is_symlink():
        raise ValueError("private subdirectory must not be a symbolic link")
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise FileExistsError(
            "refusing to reuse a private subdirectory"
        ) from exc
    os.chmod(destination, 0o700)
    return destination


def _private_temporary_file(parent: Path) -> tuple[int, Path]:
    descriptor, raw_path = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    os.fchmod(descriptor, 0o600)
    return descriptor, Path(raw_path)


def _publish_private_file(temporary: Path, destination: Path) -> None:
    os.link(temporary, destination)
    os.chmod(destination, 0o600)
    temporary.unlink()
    directory_fd = os.open(
        destination.parent,
        os.O_RDONLY | os.O_DIRECTORY,
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary = _private_temporary_file(path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _publish_private_file(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _write_private_text(path: Path, payload: str) -> None:
    _write_private_bytes(path, payload.encode("utf-8"))


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    _write_private_text(path, serialized + "\n")


def _csv_bytes(
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(fieldnames),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_limited_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            "expected child artifact is missing or unsafe"
        ) from exc
    try:
        file_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or file_status.st_size < 0
            or file_status.st_size > maximum_bytes
        ):
            raise RuntimeError(
                "child artifact size or type is outside the allowed bound"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise RuntimeError("child artifact exceeds its size limit")
        return payload
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> tuple[Mapping[str, Any], bytes]:
    payload = _read_limited_regular_file(
        path,
        maximum_bytes=8 * 1024 * 1024,
    )
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError("child report must contain a JSON object")
    return value, payload


def _safe_metric(result: Mapping[str, Any], name: str) -> float:
    value = result.get(name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise RuntimeError("child result contains an invalid metric")
    return float(value)


def _metric_summary(
    results: Sequence[Mapping[str, Any]],
    metric: str,
) -> dict[str, float]:
    values = [_safe_metric(result, metric) for result in results]
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def _minimal_child_environment(
    repository_root: Path,
    arm: str,
    source_environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    source = (
        source_environment
        if source_environment is not None
        else os.environ
    )
    allowed = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
        "LD_LIBRARY_PATH",
    )
    environment = {
        name: source[name]
        for name in allowed
        if isinstance(source.get(name), str) and source[name]
    }
    package_root = (
        repository_root / ".python-packages"
        if arm == _ARM_MAGPIE
        else repository_root / ".python-packages-chatterbox"
    )
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONPATH"] = (
        f"{package_root}{os.pathsep}{repository_root}"
    )
    return environment


def _arm_command(
    *,
    python_executable: str,
    repository_root: Path,
    arm: str,
    text_file: Path,
    artifact_dir: Path,
) -> list[str]:
    common = [
        python_executable,
        "-S",
    ]
    if arm == _ARM_MAGPIE:
        return common + [
            str(repository_root / "magpie_tts_control.py"),
            "--text-file",
            str(text_file),
            "--repeats",
            str(DEFAULT_REPEATS),
            "--artifact-dir",
            str(artifact_dir),
            "--quiet",
        ]
    if arm == _ARM_CHATTERBOX:
        return common + [
            str(repository_root / "chatterbox_tts_canary.py"),
            "--text-file",
            str(text_file),
            "--exaggeration-factors",
            str(CHATTERBOX_EXAGGERATION_FACTOR),
            "--repeats-per-factor",
            str(DEFAULT_REPEATS),
            "--skip-voice-inventory",
            "--artifact-dir",
            str(artifact_dir),
            "--quiet",
        ]
    raise ValueError("unknown TTS arm")


def _default_process_runner(
    command: Sequence[str],
    environment: Mapping[str, str],
    cwd: Path,
    timeout_seconds: float,
) -> int:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout_seconds,
        check=False,
    )
    return int(completed.returncode)


def _expected_report_path(artifact_dir: Path, arm: str) -> Path:
    if arm == _ARM_MAGPIE:
        return artifact_dir / "magpie-tts-control.json"
    if arm == _ARM_CHATTERBOX:
        return artifact_dir / "chatterbox-tts-canary.json"
    raise ValueError("unknown TTS arm")


def _expected_wav_path(
    artifact_dir: Path,
    arm: str,
    repeat_index: int,
) -> Path:
    if arm == _ARM_MAGPIE:
        return artifact_dir / (
            f"magpie-control-repeat-{repeat_index:03d}.wav"
        )
    if arm == _ARM_CHATTERBOX:
        return artifact_dir / (
            "chatterbox-factor-01-0p5-"
            f"repeat-{repeat_index:02d}.wav"
        )
    raise ValueError("unknown TTS arm")


def _require_mapping(
    parent: Mapping[str, Any],
    name: str,
) -> Mapping[str, Any]:
    value = parent.get(name)
    if not isinstance(value, Mapping):
        raise RuntimeError("child report has an invalid contract")
    return value


def _validate_child_contract(
    report: Mapping[str, Any],
    arm: str,
) -> None:
    client = _require_mapping(report, "client")
    request = _require_mapping(report, "request")
    service = _require_mapping(report, "service")
    if (
        request.get("sample_rate_hz") != SAMPLE_RATE_HZ
        or request.get("channels") != 1
        or request.get("sample_width_bytes") != 2
        or request.get("encoding") != "LINEAR_PCM"
    ):
        raise RuntimeError("child report used the wrong audio contract")

    if arm == _ARM_MAGPIE:
        provenance = _require_mapping(
            report,
            "declared_unverified_model_provenance",
        )
        expected = (
            report.get("schema_version") == 1
            and report.get("diagnostic")
            == "magpie_tts_matched_streaming_control"
            and client.get("version") == MAGPIE_CLIENT_VERSION
            and client.get("required_version", MAGPIE_CLIENT_VERSION)
            == MAGPIE_CLIENT_VERSION
            and service.get("model") == "magpie-tts-multilingual"
            and request.get("tts_locale") == MAGPIE_LOCALE
            and request.get("voice") == MAGPIE_VOICE
            and request.get("measured_repeat_count") == DEFAULT_REPEATS
            and request.get("warmup_request_count") == 1
            and provenance.get("container_image") == MAGPIE_IMAGE
            and provenance.get("nim_profile") == MAGPIE_PROFILE
            and provenance.get("image_digest") == MAGPIE_DIGEST
        )
    elif arm == _ARM_CHATTERBOX:
        provenance = _require_mapping(
            report,
            "declared_model_provenance",
        )
        warmup = _require_mapping(report, "warmup")
        expected = (
            report.get("schema_version") == 2
            and report.get("diagnostic")
            == "chatterbox_tts_exaggeration_canary"
            and client.get("reported_version")
            == CHATTERBOX_CLIENT_VERSION
            and client.get("required_version")
            == CHATTERBOX_CLIENT_VERSION
            and service.get("requested_model")
            == "chatterbox-tts-multilingual"
            and request.get("tts_locale") == CHATTERBOX_LOCALE
            and request.get("voice") == CHATTERBOX_VOICE
            and request.get("repeats_per_factor") == DEFAULT_REPEATS
            and request.get("warmup_request_count") == 1
            and request.get("exaggeration_factors")
            == [CHATTERBOX_EXAGGERATION_FACTOR]
            and warmup.get("status") == "succeeded"
            and warmup.get("exaggeration_factor")
            == CHATTERBOX_EXAGGERATION_FACTOR
            and provenance.get("declared_container_image")
            == CHATTERBOX_IMAGE
            and provenance.get("declared_nim_profile")
            == CHATTERBOX_PROFILE
            and provenance.get("declared_image_digest")
            == CHATTERBOX_DIGEST
        )
    else:
        raise ValueError("unknown TTS arm")
    if not expected:
        raise RuntimeError(
            "child report does not match the pinned TTS contract"
        )


def _validate_wav(
    path: Path,
    *,
    expected_duration_seconds: float,
) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            "expected child WAV is missing or unsafe"
        ) from exc
    try:
        file_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or file_status.st_size < 44
            or file_status.st_size > MAX_REVIEW_WAV_BYTES
        ):
            raise RuntimeError(
                "child WAV size or file type is outside the allowed bound"
            )
        with os.fdopen(descriptor, "rb") as raw_audio:
            descriptor = -1
            with wave.open(raw_audio, "rb") as audio:
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                sample_rate = audio.getframerate()
                frame_count = audio.getnframes()
                compression = audio.getcomptype()
                pcm = audio.readframes(frame_count)
                if audio.readframes(1):
                    raise RuntimeError(
                        "child WAV contains uncounted frames"
                    )
    except (EOFError, wave.Error) as exc:
        raise RuntimeError("child audio is not a valid WAV file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        channels != 1
        or sample_width != 2
        or sample_rate != SAMPLE_RATE_HZ
        or compression != "NONE"
        or frame_count <= 0
        or len(pcm) != frame_count * sample_width
    ):
        raise RuntimeError("child WAV has the wrong PCM format")
    duration = frame_count / sample_rate
    if abs(duration - expected_duration_seconds) > (1.0 / sample_rate):
        raise RuntimeError(
            "child WAV duration does not match its timing report"
        )
    return {
        "frame_count": frame_count,
        "duration_seconds": duration,
        "pcm": pcm,
    }


def _parse_arm_report(
    *,
    artifact_dir: Path,
    arm: str,
    item: CorpusItem,
) -> dict[str, Any]:
    report_path = _expected_report_path(artifact_dir, arm)
    report, report_bytes = _read_json(report_path)
    _validate_child_contract(report, arm)
    request = report.get("request")
    summary = report.get("summary")
    results = report.get("results")
    if (
        not isinstance(request, Mapping)
        or not isinstance(summary, Mapping)
        or not isinstance(results, Sequence)
        or isinstance(results, (str, bytes, bytearray))
    ):
        raise RuntimeError("child report has an invalid schema")
    text_identity = request.get("text")
    if not isinstance(text_identity, Mapping):
        raise RuntimeError("child report is missing text identity")
    if (
        text_identity.get("character_count") != len(item.text)
        or text_identity.get("utf8_byte_count")
        != len(item.text.encode("utf-8"))
        or text_identity.get("comparison_status") != "custom_unmatched"
    ):
        raise RuntimeError("child report does not match the corpus fixture")
    if (
        summary.get("requested") != DEFAULT_REPEATS
        or summary.get("succeeded") != DEFAULT_REPEATS
        or summary.get("failed") != 0
        or len(results) != DEFAULT_REPEATS
    ):
        raise RuntimeError("child report did not pass all measured requests")

    by_repeat: dict[int, Mapping[str, Any]] = {}
    for raw_result in results:
        if not isinstance(raw_result, Mapping):
            raise RuntimeError("child result must be an object")
        repeat_index = raw_result.get("repeat_index")
        if (
            not isinstance(repeat_index, int)
            or isinstance(repeat_index, bool)
            or repeat_index not in range(1, DEFAULT_REPEATS + 1)
            or repeat_index in by_repeat
            or raw_result.get("status") != "succeeded"
        ):
            raise RuntimeError("child report has invalid repeat results")
        if arm == _ARM_CHATTERBOX:
            factor = raw_result.get("exaggeration_factor")
            if (
                not isinstance(factor, (int, float))
                or isinstance(factor, bool)
                or float(factor) != CHATTERBOX_EXAGGERATION_FACTOR
            ):
                raise RuntimeError("Chatterbox report used the wrong factor")
        for metric in _METRICS:
            _safe_metric(raw_result, metric)
        by_repeat[repeat_index] = raw_result

    if set(by_repeat) != set(range(1, DEFAULT_REPEATS + 1)):
        raise RuntimeError("child report is missing repeat results")

    wav_paths: dict[int, Path] = {}
    wav_sha256: dict[int, str] = {}
    for repeat_index in range(1, DEFAULT_REPEATS + 1):
        wav_path = _expected_wav_path(
            artifact_dir,
            arm,
            repeat_index,
        )
        _validate_wav(
            wav_path,
            expected_duration_seconds=_safe_metric(
                by_repeat[repeat_index],
                "audio_duration_seconds",
            ),
        )
        wav_paths[repeat_index] = wav_path
        wav_sha256[repeat_index] = hashlib.sha256(
            _read_limited_regular_file(
                wav_path,
                maximum_bytes=MAX_REVIEW_WAV_BYTES,
            )
        ).hexdigest()

    ordered_results = [
        by_repeat[index] for index in range(1, DEFAULT_REPEATS + 1)
    ]
    return {
        "results": ordered_results,
        "by_repeat": by_repeat,
        "wav_paths": wav_paths,
        "wav_sha256": wav_sha256,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "aggregates": {
            metric: _metric_summary(ordered_results, metric)
            for metric in _METRICS
        },
        "underrun_count": sum(
            _safe_metric(result, "underrun_risk_seconds") > 0
            for result in ordered_results
        ),
    }


def _copy_private_wav(
    source: Path,
    destination: Path,
    *,
    expected_duration_seconds: float,
) -> str:
    validated = _validate_wav(
        source,
        expected_duration_seconds=expected_duration_seconds,
    )
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE_HZ)
        audio.writeframes(validated["pcm"])
    payload = output.getvalue()
    if len(payload) > MAX_REVIEW_WAV_BYTES:
        raise RuntimeError("rewritten review WAV exceeds the size bound")
    _write_private_bytes(destination, payload)
    return _sha256_bytes(payload)


def _blank_row(
    fieldnames: Sequence[str],
    initial: Mapping[str, str],
) -> dict[str, str]:
    row = {name: "" for name in fieldnames}
    row.update(initial)
    return row


def _reviewer_instructions(reviewer_code: str) -> str:
    return f"""# Model-Label-Blind TTS Review

Reviewer code: `{reviewer_code}`

Use headphones at one fixed, comfortable volume. Do not time-scale, normalize,
trim, or otherwise edit the WAV files. Do not inspect the private raw-artifact
tree or ask for the model key. Listen to each clip no more than twice in this
pass.

Complete `pass-1-audio-only.csv`. All 1-5 scales are positive orientation:
`1` means clearly unacceptable, `3` means adequate with noticeable problems,
and `5` means excellent for a live audience. For listening ease, `1` means
very tiring or difficult and `5` means effortless. Use only lowercase `yes`
or `no` for binary fields and only `a`, `b`, or `tie` for preference. If a
clip cannot be rated, mark its unrateable field `yes` and leave that clip's
other fields blank.

Return the completed first-pass file. The reference-visible second pass is
withheld until the first-pass file has been validated and hash-locked.

Use the preassigned reviewer code. Do not enter your name, organization,
contact details, customer context, or free-form comments in either file.
"""


def _review_root_instructions(reviewer_count: int) -> str:
    return f"""# Native-Spanish TTS Review Bundle

This tree contains {reviewer_count} independently randomized, model-label-blind
reviewer bundles. Assign one `reviewer-NN` directory to each native Spanish
reviewer. At least two completed reviewers are required; three are preferred.

Only the audio-only first pass is released initially. Validate and hash-lock
all returned first-pass files before releasing the separately retained
reference-visible templates. Do not share or inspect the private blinding key
until all reviewers have completed both ordered passes. The voices differ, so
this is a blinded system-candidate comparison rather than a controlled
same-voice model study.

The WAV files contain synthesized neutral Spanish speech. Review this tree
before sharing it outside the approved evaluation group.
"""


def _build_review_package(
    *,
    review_root: Path,
    private_root: Path,
    items: Sequence[CorpusItem],
    arm_results: Mapping[str, Mapping[str, Mapping[str, Any]]],
    reviewer_count: int,
    randomizer: Any,
) -> tuple[dict[str, Any], str]:
    source_pairs = [
        {
            "fixture": item,
            "repeat_index": repeat_index,
        }
        for item in items
        for repeat_index in LISTENING_REPEAT_INDEXES
    ]
    pair_count = len(source_pairs)
    if pair_count % 2:
        raise RuntimeError("blind pair count must be even")

    _write_private_text(
        review_root / "README.md",
        _review_root_instructions(reviewer_count),
    )
    phase_two_root = _new_private_subdirectory(
        private_root,
        "phase-2-reference-templates",
    )
    key_reviewers: list[dict[str, Any]] = []
    for reviewer_index in range(1, reviewer_count + 1):
        reviewer_code = f"reviewer-{reviewer_index:02d}"
        reviewer_dir = _new_private_subdirectory(
            review_root,
            reviewer_code,
        )
        clips_dir = _new_private_subdirectory(reviewer_dir, "clips")
        phase_two_reviewer_dir = _new_private_subdirectory(
            phase_two_root,
            reviewer_code,
        )

        ordered_pairs = list(source_pairs)
        randomizer.shuffle(ordered_pairs)
        a_is_chatterbox: dict[tuple[str, int], bool] = {}
        for fixture_index, item in enumerate(items):
            repeat_two_is_chatterbox = (
                fixture_index + reviewer_index
            ) % 2 == 0
            a_is_chatterbox[
                (item.fixture_id, LISTENING_REPEAT_INDEXES[0])
            ] = repeat_two_is_chatterbox
            a_is_chatterbox[
                (item.fixture_id, LISTENING_REPEAT_INDEXES[1])
            ] = not repeat_two_is_chatterbox

        pass_one_rows: list[dict[str, str]] = []
        pass_two_rows: list[dict[str, str]] = []
        reviewer_key: list[dict[str, Any]] = []
        for pair_index, pair in enumerate(ordered_pairs, start=1):
            pair_id = f"pair-{pair_index:03d}"
            item = pair["fixture"]
            repeat_index = pair["repeat_index"]
            if not isinstance(item, CorpusItem):
                raise RuntimeError("invalid review fixture")
            chatterbox_first = a_is_chatterbox[
                (item.fixture_id, repeat_index)
            ]
            if chatterbox_first:
                model_a, model_b = _ARM_CHATTERBOX, _ARM_MAGPIE
            else:
                model_a, model_b = _ARM_MAGPIE, _ARM_CHATTERBOX
            source_a = arm_results[item.fixture_id][model_a][
                "wav_paths"
            ][repeat_index]
            source_b = arm_results[item.fixture_id][model_b][
                "wav_paths"
            ][repeat_index]
            clip_a_name = f"{pair_id}-a.wav"
            clip_b_name = f"{pair_id}-b.wav"
            hash_a = _copy_private_wav(
                source_a,
                clips_dir / clip_a_name,
                expected_duration_seconds=_safe_metric(
                    arm_results[item.fixture_id][model_a]["by_repeat"][
                        repeat_index
                    ],
                    "audio_duration_seconds",
                ),
            )
            hash_b = _copy_private_wav(
                source_b,
                clips_dir / clip_b_name,
                expected_duration_seconds=_safe_metric(
                    arm_results[item.fixture_id][model_b]["by_repeat"][
                        repeat_index
                    ],
                    "audio_duration_seconds",
                ),
            )

            pass_one_rows.append(
                _blank_row(
                    _PASS_ONE_FIELDS,
                    {
                        "reviewer_code": reviewer_code,
                        "pair_id": pair_id,
                    },
                )
            )
            pass_two_rows.append(
                _blank_row(
                    _PASS_TWO_FIELDS,
                    {
                        "reviewer_code": reviewer_code,
                        "pair_id": pair_id,
                        "fixture_id": item.fixture_id,
                        "reference_text": item.text,
                    },
                )
            )
            reviewer_key.append(
                {
                    "pair_id": pair_id,
                    "fixture_id": item.fixture_id,
                    "source_repeat_index": repeat_index,
                    "clip_a": {
                        "model": model_a,
                        "sha256": hash_a,
                    },
                    "clip_b": {
                        "model": model_b,
                        "sha256": hash_b,
                    },
                }
            )

        _write_private_text(
            reviewer_dir / "INSTRUCTIONS.md",
            _reviewer_instructions(reviewer_code),
        )
        _write_private_bytes(
            reviewer_dir / "pass-1-audio-only.csv",
            _csv_bytes(_PASS_ONE_FIELDS, pass_one_rows),
        )
        phase_two_bytes = _csv_bytes(_PASS_TWO_FIELDS, pass_two_rows)
        _write_private_bytes(
            phase_two_reviewer_dir / "pass-2-reference-visible.csv",
            phase_two_bytes,
        )
        key_reviewers.append(
            {
                "reviewer_code": reviewer_code,
                "phase_two_template_sha256": _sha256_bytes(
                    phase_two_bytes
                ),
                "pairs": reviewer_key,
            }
        )

    blinding_key = {
        "schema_version": 1,
        "diagnostic": "tts_multitext_blinding_key",
        "corpus_id": CORPUS_ID,
        "corpus_version": CORPUS_VERSION,
        "selected_repeat_indexes": list(LISTENING_REPEAT_INDEXES),
        "reviewers": key_reviewers,
        "privacy": {
            "contains_model_to_clip_mapping": True,
            "contains_audio_hashes": True,
            "contains_input_text": False,
            "contains_reviewer_identity": False,
            "share_with_reviewers_before_completion": False,
        },
    }
    serialized_key = (
        json.dumps(
            blinding_key,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    key_path = private_root / BLINDING_KEY_FILENAME
    _write_private_bytes(key_path, serialized_key)
    key_sha256 = _sha256_bytes(serialized_key)
    return (
        {
            "reviewer_bundle_count": reviewer_count,
            "minimum_completed_reviewer_count": 2,
            "preferred_completed_reviewer_count": 3,
            "pair_count_per_reviewer": pair_count,
            "clip_count_per_reviewer": pair_count * 2,
            "source_clip_count": pair_count * 2,
            "selected_repeat_indexes": list(LISTENING_REPEAT_INDEXES),
            "blinding_key_sha256": key_sha256,
            "quality_status": "pending_native_listener_review",
            "phase_one_status": (
                "created_withheld_pending_runtime_attestation"
            ),
            "phase_two_status": "withheld_pending_phase_one_lock",
        },
        key_sha256,
    )


def _build_gate_summary(
    *,
    items: Sequence[CorpusItem],
    arm_results: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    ratios: list[float] = []
    chatterbox_ttfa: list[float] = []
    buffer_requirements: list[float] = []

    for item in items:
        fixture_results = arm_results[item.fixture_id]
        magpie = fixture_results[_ARM_MAGPIE]
        chatterbox = fixture_results[_ARM_CHATTERBOX]
        magpie_duration = magpie["aggregates"][
            "audio_duration_seconds"
        ]["median"]
        chatterbox_duration = chatterbox["aggregates"][
            "audio_duration_seconds"
        ]["median"]
        if magpie_duration <= 0:
            raise RuntimeError("Magpie median duration must be positive")
        ratio = chatterbox_duration / magpie_duration
        ratios.append(ratio)
        chatterbox_ttfa.extend(
            _safe_metric(result, "ttfa_seconds")
            for result in chatterbox["results"]
        )
        for repeat_index in range(1, DEFAULT_REPEATS + 1):
            buffer_requirements.append(
                _safe_metric(
                    chatterbox["by_repeat"][repeat_index],
                    "underrun_risk_seconds",
                )
            )
        fixtures.append(
            {
                **item.identity(),
                "magpie": {
                    "aggregates": magpie["aggregates"],
                    "underrun_count": magpie["underrun_count"],
                },
                "chatterbox": {
                    "aggregates": chatterbox["aggregates"],
                    "underrun_count": chatterbox["underrun_count"],
                },
                "chatterbox_to_magpie_duration_ratio": ratio,
                "chatterbox_duration_change_percent": (
                    (ratio - 1.0) * 100.0
                ),
            }
        )

    overall_ratio = statistics.median(ratios)
    shorter_fixture_count = sum(
        ratio <= MAX_SHORTER_FIXTURE_RATIO for ratio in ratios
    )
    median_ttfa = statistics.median(chatterbox_ttfa)
    max_buffer = max(buffer_requirements)
    conditions = {
        "all_measured_requests_succeeded": True,
        "overall_duration_ratio_at_most_0_92": (
            overall_ratio <= MAX_OVERALL_DURATION_RATIO
        ),
        "at_least_four_fixtures_five_percent_shorter": (
            shorter_fixture_count >= MIN_SHORTER_FIXTURES
        ),
        "median_chatterbox_ttfa_at_most_1_25_seconds": (
            median_ttfa <= MAX_CHATTERBOX_MEDIAN_TTFA_SECONDS
        ),
        "all_chatterbox_trials_fit_1_25_second_startup_buffer": (
            max_buffer <= MAX_CHATTERBOX_STARTUP_BUFFER_SECONDS
        ),
    }
    passed = all(conditions.values())
    return fixtures, {
        "status": (
            "client_metrics_passed_pending_runtime_attestation"
            if passed
            else "mechanical_gate_not_met"
        ),
        "passed": passed,
        "conditions": conditions,
        "thresholds": {
            "maximum_overall_duration_ratio": (
                MAX_OVERALL_DURATION_RATIO
            ),
            "maximum_shorter_fixture_ratio": (
                MAX_SHORTER_FIXTURE_RATIO
            ),
            "minimum_shorter_fixture_count": MIN_SHORTER_FIXTURES,
            "maximum_chatterbox_median_ttfa_seconds": (
                MAX_CHATTERBOX_MEDIAN_TTFA_SECONDS
            ),
            "maximum_chatterbox_startup_buffer_seconds": (
                MAX_CHATTERBOX_STARTUP_BUFFER_SECONDS
            ),
        },
        "observed": {
            "overall_median_fixture_duration_ratio": overall_ratio,
            "overall_median_duration_change_percent": (
                (overall_ratio - 1.0) * 100.0
            ),
            "shorter_fixture_count": shorter_fixture_count,
            "fixture_count": len(ratios),
            "chatterbox_median_ttfa_seconds": median_ttfa,
            "chatterbox_max_underrun_risk_seconds": (
                max_buffer
            ),
        },
        "quality_status": "pending_native_listener_review",
        "end_to_end_s2s_status": "not_evaluated",
    }


def _write_private_request_bindings(
    *,
    private_root: Path,
    items: Sequence[CorpusItem],
    arm_results: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> None:
    fixtures: list[dict[str, Any]] = []
    for item in items:
        arms: dict[str, Any] = {}
        for arm in _ARMS:
            result = arm_results[item.fixture_id][arm]
            arms[arm] = {
                "child_report_sha256": result["report_sha256"],
                "wav_sha256_by_repeat": {
                    str(index): result["wav_sha256"][index]
                    for index in range(1, DEFAULT_REPEATS + 1)
                },
            }
        fixtures.append(
            {
                "fixture_id": item.fixture_id,
                "request_text_sha256": hashlib.sha256(
                    item.text.encode("utf-8")
                ).hexdigest(),
                "character_count": len(item.text),
                "utf8_byte_count": len(item.text.encode("utf-8")),
                "arms": arms,
            }
        )
    _write_private_json(
        private_root / "request-bindings.json",
        {
            "schema_version": 1,
            "diagnostic": "tts_multitext_private_request_bindings",
            "corpus_id": CORPUS_ID,
            "corpus_version": CORPUS_VERSION,
            "fixtures": fixtures,
            "privacy": {
                "contains_candidate_matchable_text_fingerprints": True,
                "contains_input_text": False,
                "contains_audio_hashes": True,
                "share_publicly": False,
            },
        },
    )


def _verify_runtime_files(repository_root: Path) -> None:
    required_files = (
        repository_root / "magpie_tts_control.py",
        repository_root / "chatterbox_tts_canary.py",
    )
    required_directories = (
        repository_root / ".python-packages",
        repository_root / ".python-packages-chatterbox",
    )
    if any(not path.is_file() for path in required_files):
        raise RuntimeError("arm-specific runner is missing")
    if any(not path.is_dir() for path in required_directories):
        raise RuntimeError("arm-specific pinned client directory is missing")


def run_gate(
    *,
    artifact_dir: Path,
    repository_root: Optional[Path] = None,
    python_executable: Optional[str] = None,
    reviewer_count: int = DEFAULT_REVIEWER_COUNT,
    child_timeout_seconds: float = MAX_CHILD_SECONDS,
    allow_artifact_outside_results: bool = False,
    process_runner: ProcessRunner = _default_process_runner,
    source_environment: Optional[Mapping[str, str]] = None,
    randomizer: Optional[Any] = None,
    now: Callable[[], datetime] = utc_now,
) -> tuple[dict[str, Any], Path]:
    """Execute both arms, summarize mechanics, and build blind bundles."""
    validate_options(
        reviewer_count=reviewer_count,
        child_timeout_seconds=child_timeout_seconds,
    )
    items = validate_corpus()
    root = (
        repository_root or Path(__file__).resolve().parent
    ).resolve()
    _verify_runtime_files(root)
    resolved_artifact_dir = _resolve_artifact_directory(
        artifact_dir,
        repository_root=root,
        allow_outside_results=allow_artifact_outside_results,
    )
    executable = python_executable or sys.executable
    if not executable:
        raise RuntimeError("Python executable could not be resolved")

    artifact_root = _new_private_directory(resolved_artifact_dir)
    raw_root = _new_private_subdirectory(artifact_root, "raw")
    private_root = _new_private_subdirectory(artifact_root, "private")
    started_at = now()
    arm_results: dict[str, dict[str, dict[str, Any]]] = {}

    for item_index, item in enumerate(items):
        item_root = _new_private_subdirectory(
            raw_root,
            item.fixture_id,
        )
        input_path = private_root / f".input-{item.fixture_id}.txt"
        _write_private_text(input_path, item.text + "\n")
        order = (
            _ARMS
            if item_index % 2 == 0
            else tuple(reversed(_ARMS))
        )
        item_results: dict[str, dict[str, Any]] = {}
        try:
            for arm in order:
                arm_artifact_dir = item_root / arm
                command = _arm_command(
                    python_executable=executable,
                    repository_root=root,
                    arm=arm,
                    text_file=input_path,
                    artifact_dir=arm_artifact_dir,
                )
                environment = _minimal_child_environment(
                    root,
                    arm,
                    source_environment,
                )
                return_code = process_runner(
                    command,
                    environment,
                    root,
                    float(child_timeout_seconds),
                )
                if (
                    not isinstance(return_code, int)
                    or isinstance(return_code, bool)
                    or return_code != 0
                ):
                    raise RuntimeError(
                        "arm-specific subprocess failed"
                    )
                item_results[arm] = _parse_arm_report(
                    artifact_dir=arm_artifact_dir,
                    arm=arm,
                    item=item,
                )
        finally:
            try:
                input_path.unlink()
            except FileNotFoundError:
                pass
        if set(item_results) != set(_ARMS):
            raise RuntimeError("fixture did not complete both TTS arms")
        arm_results[item.fixture_id] = item_results

    fixture_summaries, mechanical_gate = _build_gate_summary(
        items=items,
        arm_results=arm_results,
    )
    _write_private_request_bindings(
        private_root=private_root,
        items=items,
        arm_results=arm_results,
    )
    if mechanical_gate["passed"]:
        review_root = _new_private_subdirectory(
            artifact_root,
            "review",
        )
        resolved_randomizer = randomizer or secrets.SystemRandom()
        review_summary, key_sha256 = _build_review_package(
            review_root=review_root,
            private_root=private_root,
            items=items,
            arm_results=arm_results,
            reviewer_count=reviewer_count,
            randomizer=resolved_randomizer,
        )
    else:
        key_sha256 = ""
        review_summary = {
            "reviewer_bundle_count": 0,
            "minimum_completed_reviewer_count": 2,
            "preferred_completed_reviewer_count": 3,
            "pair_count_per_reviewer": 0,
            "clip_count_per_reviewer": 0,
            "source_clip_count": 0,
            "selected_repeat_indexes": list(
                LISTENING_REPEAT_INDEXES
            ),
            "blinding_key_sha256": "",
            "quality_status": "not_evaluated_mechanical_gate_failed",
            "phase_one_status": "not_created",
            "phase_two_status": "not_created",
        }
    completed_at = now()
    report = {
        "schema_version": 2,
        "diagnostic": "tts_multitext_native_listener_gate",
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": completed_at.isoformat(),
        "corpus": corpus_identity(),
        "configuration": {
            "mechanical_protocol_version": MECHANICAL_PROTOCOL_VERSION,
            "measured_repeats_per_arm_fixture": DEFAULT_REPEATS,
            "discarded_warmups_per_arm_fixture": 1,
            "execution_order": "alternating_by_fixture",
            "chatterbox_exaggeration_factor": (
                CHATTERBOX_EXAGGERATION_FACTOR
            ),
            "chatterbox_cfg_weight": "omitted",
            "audio_postprocessing": "none",
            "listening_repeat_indexes": list(
                LISTENING_REPEAT_INDEXES
            ),
        },
        "fixtures": fixture_summaries,
        "mechanical_gate": mechanical_gate,
        "review_package": review_summary,
        "runtime_attestation": {
            "docker_health_checked_by_runner": False,
            "gpu_memory_checked_by_runner": False,
            "manual_pre_and_post_checks_required": True,
        },
        "privacy": {
            "contains_input_text": False,
            "contains_input_path": False,
            "contains_text_fingerprint": False,
            "contains_model_to_clip_mapping": False,
            "contains_reviewer_identity": False,
            "contains_raw_error_messages": False,
            "contains_audio_payload_in_json": False,
            "blinding_key_sha256": key_sha256,
            "wav_files_contain_synthesized_speech": True,
            "review_required_before_sharing": True,
            "artifact_directory_mode": "0700",
            "artifact_file_mode": "0600",
        },
    }
    report_path = artifact_root / REPORT_FILENAME
    _write_private_json(report_path, report)
    return report, report_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report, report_path = run_gate(
            artifact_dir=(
                args.artifact_dir or default_artifact_directory()
            ),
            reviewer_count=args.reviewer_count,
            child_timeout_seconds=args.child_timeout_seconds,
            allow_artifact_outside_results=(
                args.allow_artifact_outside_results
            ),
        )
    except (
        FileExistsError,
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ):
        print("Multi-text TTS gate failed.", file=sys.stderr)
        return 1
    if not args.quiet:
        gate = report["mechanical_gate"]
        observed = gate["observed"]
        print(
            f"status={gate['status']} "
            "duration_change="
            f"{observed['overall_median_duration_change_percent']:.2f}% "
            "shorter_fixtures="
            f"{observed['shorter_fixture_count']}/"
            f"{observed['fixture_count']}"
        )
        print(f"Privacy-safe report: {report_path}")
        if gate["passed"]:
            print(
                "Phase-one native-listener bundle: "
                f"{report_path.parent / 'review'}"
            )
    return 0 if report["mechanical_gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
