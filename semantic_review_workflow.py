#!/usr/bin/env python3
"""Private, hash-bound bilingual semantic review workflow.

The coordinator never edits the captured ledger or WAVs.  It prepares a
deterministic assignment, validates anonymous browser exports, reconciles
only explicitly selected reviewer-supported landmarks, and runs the existing
scheduled semantic-delay analyzer at the registered five- and ten-second
thresholds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from analyze_scheduled_semantic_delay import (
    GATE_EXIT_CODES,
    MARKER_DOCUMENT_KEYS,
    MARKER_EVENT_KEYS,
    ScheduledSemanticDelayError,
    analyze_scheduled_semantic_delay,
    render_scheduled_semantic_delay_markdown,
)
from private_pcm_schedule_ledger import (
    LEDGER_FILENAME,
    SOURCE_WAV_FILENAME,
    TRANSLATED_WAV_FILENAME,
    PrivatePcmScheduleLedgerError,
    validate_private_pcm_schedule_ledger,
)


SCHEMA_VERSION = 1
REPOSITORY_ROOT = Path(__file__).resolve().parent
EXPERIMENT_RESULTS_NAME = "experiment_results"

ASSIGNMENT_FILENAME = "review-assignment.json"
ASSISTANT_SOURCE_FILENAME = "semantic_review_assistant.html"
ASSISTANT_FILENAME = "semantic-review-assistant.html"
REVIEWER_MARKERS_FILENAME = "reviewer-markers.json"
REVIEWER_FEEDBACK_FILENAME = "reviewer-feedback.json"
FIVE_SECOND_JSON_FILENAME = "semantic-delay-5s.json"
FIVE_SECOND_MARKDOWN_FILENAME = "semantic-delay-5s.md"
TEN_SECOND_JSON_FILENAME = "semantic-delay-10s.json"
TEN_SECOND_MARKDOWN_FILENAME = "semantic-delay-10s.md"

ASSIGNMENT_TYPE = "private_semantic_review_assignment"
OBSERVATION_TYPE = "private_semantic_reviewer_observation"
FEEDBACK_TYPE = "private_semantic_reviewer_feedback_aggregate"
COMPARISON_TYPE = "private_semantic_review_comparison"
LANDMARK_RULE = (
    "source_idea_completion_word_onset_to_corresponding_"
    "translated_word_onset_v1"
)
MINIMUM_REVIEWERS = 2
MINIMUM_BOUNDARY_CONFIDENCE = 3
MAXIMUM_REVIEWERS = 20

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_WAV_BYTES = 1024 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
EVENT_ID_PATTERN = re.compile(r"event-[0-9]{3}\Z")
UUID4_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
SECONDS_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
CANONICAL_PATTERN = re.compile(
    r"(event-[0-9]{3}):([0-9]+):([0-9]+)\Z"
)

SEMANTIC_EQUIVALENCE_VALUES = (
    "accepted",
    "uncertain",
    "rejected",
)
ISSUE_FLAGS = (
    "meaning_mismatch",
    "omission",
    "addition",
    "pronunciation",
    "unnatural_prosody",
    "too_fast",
    "too_slow",
    "boundary_ambiguous",
)

ASSIGNMENT_KEYS = frozenset(
    {
        "schema_version",
        "assignment_type",
        "schedule_ledger_sha256",
        "source_pcm_sha256",
        "source_pcm_sample_count",
        "source_sample_rate_hz",
        "translated_pcm_sha256",
        "translated_pcm_sample_count",
        "translated_sample_rate_hz",
        "minimum_independent_reviewers",
        "minimum_boundary_confidence",
        "landmark_rule",
        "events",
        "privacy",
    }
)
ASSIGNMENT_EVENT_KEYS = frozenset(
    {
        "event_id",
        "source_window_start_sample",
        "source_window_end_sample_exclusive",
    }
)
OBSERVATION_KEYS = frozenset(
    {
        "schema_version",
        "observation_type",
        "assignment_sha256",
        "schedule_ledger_sha256",
        "source_pcm_sha256",
        "source_pcm_sample_count",
        "translated_pcm_sha256",
        "translated_pcm_sample_count",
        "reviewer_session_id",
        "independence_attestation",
        "events",
        "privacy",
    }
)
OBSERVATION_EVENT_KEYS = frozenset(
    {
        "event_id",
        "source_sample_index",
        "translated_sample_index",
        "semantic_equivalence",
        "source_boundary_confidence",
        "translated_boundary_confidence",
        "translation_quality_rating",
        "intelligibility_rating",
        "naturalness_rating",
        "issue_flags",
    }
)
ASSIGNMENT_PRIVACY = {
    "contains_audio": False,
    "contains_transcript_or_translation_text": False,
    "contains_reviewer_identity": False,
    "contains_file_path_or_uri": False,
    "contains_wall_clock_timestamp": False,
    "private_review_artifact": True,
}
OBSERVATION_PRIVACY = {
    **ASSIGNMENT_PRIVACY,
    "contains_free_text": False,
}


class SemanticReviewWorkflowError(ValueError):
    """Raised when a private review artifact is unsafe or inconsistent."""


@dataclass(frozen=True)
class PrivateFile:
    payload: bytes
    identity: tuple[int, int]


@dataclass(frozen=True)
class CaptureBundle:
    directory: Path
    repository_root: Path
    ledger: dict[str, Any]
    ledger_bytes: bytes
    source_wav_bytes: bytes
    translated_wav_bytes: bytes

    @property
    def ledger_sha256(self) -> str:
        return _sha256(self.ledger_bytes)


@dataclass(frozen=True)
class Assignment:
    document: dict[str, Any]
    payload: bytes

    @property
    def sha256(self) -> str:
        return _sha256(self.payload)


@dataclass(frozen=True)
class Observation:
    document: dict[str, Any]
    payload: bytes
    identity: tuple[int, int]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SemanticReviewWorkflowError(
            "artifact is not strict JSON"
        ) from exc
    return (text + "\n").encode("utf-8")


def _load_strict_json(payload: bytes, context: str) -> dict[str, Any]:
    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SemanticReviewWorkflowError(
                    f"{context} contains a duplicate object key"
                )
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise SemanticReviewWorkflowError(
            f"{context} contains a non-standard number"
        )

    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except SemanticReviewWorkflowError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise SemanticReviewWorkflowError(
            f"{context} is not valid UTF-8 JSON"
        ) from exc
    if type(value) is not dict:
        raise SemanticReviewWorkflowError(f"{context} must be an object")
    return value


def _exact(
    value: object,
    keys: frozenset[str] | set[str],
    context: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        raise SemanticReviewWorkflowError(
            f"{context} must use the exact schema"
        )
    return value


def _plain_int(
    value: object,
    context: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise SemanticReviewWorkflowError(
            f"{context} must be a plain integer in range"
        )
    return value


def _literal(value: object, expected: object, context: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise SemanticReviewWorkflowError(
            f"{context} has an invalid literal value"
        )


def _require_hash(value: object, context: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise SemanticReviewWorkflowError(
            f"{context} must be a lowercase SHA-256"
        )
    return value


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _normalized_capture_path(
    capture_dir: Path | str,
    repository_root: Path | None,
) -> tuple[Path, Path, tuple[str, ...]]:
    root = Path(
        os.path.abspath(
            os.fspath(
                (repository_root or REPOSITORY_ROOT).expanduser()
            )
        )
    )
    raw = Path(capture_dir).expanduser()
    candidate = Path(
        os.path.abspath(
            os.fspath(raw if raw.is_absolute() else root / raw)
        )
    )
    results = root / EXPERIMENT_RESULTS_NAME
    try:
        relative = candidate.relative_to(results)
    except ValueError as exc:
        raise SemanticReviewWorkflowError(
            "capture directory must be under repository experiment_results"
        ) from exc
    if not relative.parts:
        raise SemanticReviewWorkflowError(
            "capture directory must be a child of experiment_results"
        )
    return root, candidate, relative.parts


def _open_capture_directory(
    capture_dir: Path | str,
    repository_root: Path | None = None,
) -> tuple[Path, Path, int]:
    root, candidate, relative = _normalized_capture_path(
        capture_dir,
        repository_root,
    )
    flags = _directory_flags()
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise SemanticReviewWorkflowError(
            "repository root is missing or unsafe"
        ) from exc
    current_fd = -1
    try:
        current_fd = os.open(
            EXPERIMENT_RESULTS_NAME,
            flags,
            dir_fd=root_fd,
        )
        os.close(root_fd)
        root_fd = -1
        for component in relative:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            file_status = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(file_status.st_mode)
                or stat.S_IMODE(file_status.st_mode) != 0o700
            ):
                os.close(next_fd)
                raise SemanticReviewWorkflowError(
                    "private capture directories must have mode 0700"
                )
            os.close(current_fd)
            current_fd = next_fd
        return root, candidate, current_fd
    except SemanticReviewWorkflowError:
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except OSError as exc:
        if current_fd >= 0:
            os.close(current_fd)
        raise SemanticReviewWorkflowError(
            "capture directory is missing, a symlink, or unsafe"
        ) from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _relative_private_path(
    capture: Path,
    value: Path | str,
) -> tuple[str, ...]:
    raw = Path(value).expanduser()
    candidate = Path(
        os.path.abspath(
            os.fspath(raw if raw.is_absolute() else capture / raw)
        )
    )
    try:
        relative = candidate.relative_to(capture)
    except ValueError as exc:
        raise SemanticReviewWorkflowError(
            "review artifacts must remain inside the private capture"
        ) from exc
    if not relative.parts:
        raise SemanticReviewWorkflowError(
            "review artifact must be a file"
        )
    return relative.parts


def _read_capture_file(
    capture_dir: Path | str,
    value: Path | str,
    *,
    maximum_bytes: int,
    repository_root: Path | None = None,
) -> PrivateFile:
    _root, capture, directory_fd = _open_capture_directory(
        capture_dir,
        repository_root,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    current_fd = directory_fd
    descriptor = -1
    try:
        relative = _relative_private_path(capture, value)
        for component in relative[:-1]:
            next_fd = os.open(
                component,
                _directory_flags(),
                dir_fd=current_fd,
            )
            folder_status = os.fstat(next_fd)
            if stat.S_IMODE(folder_status.st_mode) != 0o700:
                os.close(next_fd)
                raise SemanticReviewWorkflowError(
                    "private review directories must have mode 0700"
                )
            if current_fd != directory_fd:
                os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(relative[-1], flags, dir_fd=current_fd)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum_bytes
        ):
            raise SemanticReviewWorkflowError(
                "private artifact must be a bounded single-link regular "
                "0600 file"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != before.st_size
            or len(payload) > maximum_bytes
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_mode,
                before.st_nlink,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_mode,
                after.st_nlink,
            )
        ):
            raise SemanticReviewWorkflowError(
                "private artifact changed while it was read"
            )
        return PrivateFile(
            payload=payload,
            identity=(before.st_dev, before.st_ino),
        )
    except SemanticReviewWorkflowError:
        raise
    except OSError as exc:
        raise SemanticReviewWorkflowError(
            "private artifact is missing, a symlink, or unsafe"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if current_fd != directory_fd:
            os.close(current_fd)
        os.close(directory_fd)


def validate_capture_bundle(
    capture_dir: Path | str,
    *,
    repository_root: Path | None = None,
) -> CaptureBundle:
    root, directory, descriptor = _open_capture_directory(
        capture_dir,
        repository_root,
    )
    os.close(descriptor)
    try:
        ignored = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--",
                os.fspath(directory),
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SemanticReviewWorkflowError(
            "could not verify private capture ignore status"
        ) from exc
    if ignored.returncode != 0:
        raise SemanticReviewWorkflowError(
            "private capture must be ignored by Git"
        )
    ledger_file = _read_capture_file(
        directory,
        LEDGER_FILENAME,
        maximum_bytes=MAX_JSON_BYTES,
        repository_root=root,
    )
    source_file = _read_capture_file(
        directory,
        SOURCE_WAV_FILENAME,
        maximum_bytes=MAX_WAV_BYTES,
        repository_root=root,
    )
    translated_file = _read_capture_file(
        directory,
        TRANSLATED_WAV_FILENAME,
        maximum_bytes=MAX_WAV_BYTES,
        repository_root=root,
    )
    ledger = _load_strict_json(ledger_file.payload, "schedule ledger")
    try:
        validated = validate_private_pcm_schedule_ledger(
            ledger,
            source_wav_bytes=source_file.payload,
            translated_wav_bytes=translated_file.payload,
        )
    except PrivatePcmScheduleLedgerError as exc:
        raise SemanticReviewWorkflowError(
            "schedule ledger and review WAVs do not reconcile"
        ) from exc
    return CaptureBundle(
        directory=directory,
        repository_root=root,
        ledger=validated,
        ledger_bytes=ledger_file.payload,
        source_wav_bytes=source_file.payload,
        translated_wav_bytes=translated_file.payload,
    )


def _read_template(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SemanticReviewWorkflowError(
            "tracked semantic review assistant is missing or unsafe"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > MAX_HTML_BYTES
        ):
            raise SemanticReviewWorkflowError(
                "tracked semantic review assistant is invalid"
            )
        payload = b""
        while len(payload) <= MAX_HTML_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_HTML_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            len(payload) != info.st_size
            or (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
        ):
            raise SemanticReviewWorkflowError(
                "tracked semantic review assistant changed while read"
            )
        return payload
    finally:
        os.close(descriptor)


def _publish_exclusive(
    bundle: CaptureBundle,
    artifacts: Mapping[str, bytes],
) -> None:
    _root, _directory, directory_fd = _open_capture_directory(
        bundle.directory,
        bundle.repository_root,
    )
    created: list[str] = []
    try:
        for name in artifacts:
            if Path(name).name != name or name in {"", ".", ".."}:
                raise SemanticReviewWorkflowError(
                    "private output name is unsafe"
                )
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SemanticReviewWorkflowError(
                    "refusing to overwrite a private review artifact"
                )
        for name, payload in artifacts.items():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
            created.append(name)
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short private artifact write")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(directory_fd)
    except Exception:
        for name in created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(directory_fd)


def _seconds_to_sample(value: str, rate: int, context: str) -> int:
    if len(value) > 64 or SECONDS_PATTERN.fullmatch(value) is None:
        raise SemanticReviewWorkflowError(
            f"{context} must be a finite non-negative decimal"
        )
    try:
        samples = Fraction(value) * rate
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        raise SemanticReviewWorkflowError(
            f"{context} is not a valid decimal"
        ) from exc
    if samples.denominator != 1:
        raise SemanticReviewWorkflowError(
            f"{context} must resolve to an exact source sample"
        )
    return samples.numerator


def _event_windows(
    bundle: CaptureBundle,
    *,
    default_three_windows: bool,
    event_window_specs: Sequence[str] | None,
) -> list[dict[str, Any]]:
    count = bundle.ledger["source_pcm"]["sample_count"]
    rate = bundle.ledger["source_pcm"]["sample_rate_hz"]
    specs = list(event_window_specs or ())
    if default_three_windows == bool(specs):
        raise SemanticReviewWorkflowError(
            "choose exactly one event-window mode"
        )
    if default_three_windows:
        events = [
            {
                "event_id": f"event-{index + 1:03d}",
                "source_window_start_sample": count * start // 100,
                "source_window_end_sample_exclusive": count * end // 100,
            }
            for index, (start, end) in enumerate(
                ((10, 30), (40, 60), (70, 90))
            )
        ]
    else:
        events = []
        for index, spec in enumerate(specs):
            parts = spec.split(":")
            if len(parts) != 3:
                raise SemanticReviewWorkflowError(
                    f"event window {index + 1} has invalid syntax"
                )
            event_id, start_text, end_text = parts
            if EVENT_ID_PATTERN.fullmatch(event_id) is None:
                raise SemanticReviewWorkflowError(
                    f"event window {index + 1} has an invalid event ID"
                )
            events.append(
                {
                    "event_id": event_id,
                    "source_window_start_sample": _seconds_to_sample(
                        start_text,
                        rate,
                        f"event window {index + 1} start",
                    ),
                    "source_window_end_sample_exclusive": (
                        _seconds_to_sample(
                            end_text,
                            rate,
                            f"event window {index + 1} end",
                        )
                    ),
                }
            )
    _validate_assignment_events(events, count)
    return events


def _validate_assignment_events(
    value: object,
    source_sample_count: int,
) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) < 3 or len(value) > 999:
        raise SemanticReviewWorkflowError(
            "assignment must contain at least three events"
        )
    previous_id = ""
    previous_end = 0
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        event = _exact(
            raw,
            ASSIGNMENT_EVENT_KEYS,
            f"assignment event {index + 1}",
        )
        event_id = event["event_id"]
        if (
            not isinstance(event_id, str)
            or EVENT_ID_PATTERN.fullmatch(event_id) is None
            or (previous_id and event_id <= previous_id)
        ):
            raise SemanticReviewWorkflowError(
                "assignment event IDs must be unique and ordered"
            )
        start = _plain_int(
            event["source_window_start_sample"],
            f"{event_id} source window start",
        )
        end = _plain_int(
            event["source_window_end_sample_exclusive"],
            f"{event_id} source window end",
            minimum=1,
        )
        if start < previous_end or end <= start or end > source_sample_count:
            raise SemanticReviewWorkflowError(
                "assignment windows must be ordered, non-overlapping, "
                "non-empty, and inside source PCM"
            )
        previous_id = event_id
        previous_end = end
        result.append(dict(event))
    return result


def _assignment_document(
    bundle: CaptureBundle,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source = bundle.ledger["source_pcm"]
    translated = bundle.ledger["translated_pcm"]
    return {
        "schema_version": SCHEMA_VERSION,
        "assignment_type": ASSIGNMENT_TYPE,
        "schedule_ledger_sha256": bundle.ledger_sha256,
        "source_pcm_sha256": source["pcm_sha256"],
        "source_pcm_sample_count": source["sample_count"],
        "source_sample_rate_hz": source["sample_rate_hz"],
        "translated_pcm_sha256": translated["pcm_sha256"],
        "translated_pcm_sample_count": translated["sample_count"],
        "translated_sample_rate_hz": translated["sample_rate_hz"],
        "minimum_independent_reviewers": MINIMUM_REVIEWERS,
        "minimum_boundary_confidence": MINIMUM_BOUNDARY_CONFIDENCE,
        "landmark_rule": LANDMARK_RULE,
        "events": [dict(event) for event in events],
        "privacy": dict(ASSIGNMENT_PRIVACY),
    }


def prepare_review(
    capture_dir: Path | str,
    *,
    default_three_windows: bool = False,
    event_windows: Sequence[str] | None = None,
    repository_root: Path | None = None,
) -> Assignment:
    """Create a deterministic assignment and private assistant copy."""

    bundle = validate_capture_bundle(
        capture_dir,
        repository_root=repository_root,
    )
    events = _event_windows(
        bundle,
        default_three_windows=default_three_windows,
        event_window_specs=event_windows,
    )
    document = _assignment_document(bundle, events)
    payload = _json_bytes(document)
    template = _read_template(
        bundle.repository_root / ASSISTANT_SOURCE_FILENAME
    )
    rechecked = validate_capture_bundle(
        bundle.directory,
        repository_root=bundle.repository_root,
    )
    if (
        rechecked.ledger_bytes != bundle.ledger_bytes
        or rechecked.source_wav_bytes != bundle.source_wav_bytes
        or rechecked.translated_wav_bytes != bundle.translated_wav_bytes
    ):
        raise SemanticReviewWorkflowError(
            "capture bundle changed during preparation"
        )
    _publish_exclusive(
        bundle,
        {
            ASSIGNMENT_FILENAME: payload,
            ASSISTANT_FILENAME: template,
        },
    )
    return Assignment(document=document, payload=payload)


def _validate_assignment(
    document: object,
    payload: bytes,
    bundle: CaptureBundle,
) -> Assignment:
    item = _exact(document, ASSIGNMENT_KEYS, "review assignment")
    _literal(item["schema_version"], SCHEMA_VERSION, "assignment version")
    _literal(
        item["assignment_type"],
        ASSIGNMENT_TYPE,
        "assignment type",
    )
    _literal(item["landmark_rule"], LANDMARK_RULE, "landmark rule")
    _literal(
        item["minimum_independent_reviewers"],
        MINIMUM_REVIEWERS,
        "minimum independent reviewers",
    )
    _literal(
        item["minimum_boundary_confidence"],
        MINIMUM_BOUNDARY_CONFIDENCE,
        "minimum boundary confidence",
    )
    source = bundle.ledger["source_pcm"]
    translated = bundle.ledger["translated_pcm"]
    bindings = (
        ("schedule_ledger_sha256", bundle.ledger_sha256, _require_hash),
        ("source_pcm_sha256", source["pcm_sha256"], _require_hash),
        (
            "source_pcm_sample_count",
            source["sample_count"],
            _plain_int,
        ),
        ("source_sample_rate_hz", source["sample_rate_hz"], _plain_int),
        (
            "translated_pcm_sha256",
            translated["pcm_sha256"],
            _require_hash,
        ),
        (
            "translated_pcm_sample_count",
            translated["sample_count"],
            _plain_int,
        ),
        (
            "translated_sample_rate_hz",
            translated["sample_rate_hz"],
            _plain_int,
        ),
    )
    for key, expected, validator in bindings:
        actual = validator(item[key], f"assignment {key}")
        if actual != expected:
            raise SemanticReviewWorkflowError(
                "review assignment does not bind the current capture"
            )
    privacy = _exact(
        item["privacy"],
        frozenset(ASSIGNMENT_PRIVACY),
        "assignment privacy",
    )
    for key, expected in ASSIGNMENT_PRIVACY.items():
        _literal(privacy[key], expected, f"assignment privacy {key}")
    events = _validate_assignment_events(
        item["events"],
        source["sample_count"],
    )
    normalized = dict(item)
    normalized["events"] = events
    return Assignment(document=normalized, payload=payload)


def _load_assignment(bundle: CaptureBundle) -> Assignment:
    file = _read_capture_file(
        bundle.directory,
        ASSIGNMENT_FILENAME,
        maximum_bytes=MAX_JSON_BYTES,
        repository_root=bundle.repository_root,
    )
    document = _load_strict_json(file.payload, "review assignment")
    return _validate_assignment(document, file.payload, bundle)


def _validate_observation(
    document: object,
    payload: bytes,
    identity: tuple[int, int],
    *,
    assignment: Assignment,
    bundle: CaptureBundle,
    ordinal: int,
) -> Observation:
    context = f"review observation {ordinal}"
    item = _exact(document, OBSERVATION_KEYS, context)
    _literal(item["schema_version"], SCHEMA_VERSION, f"{context} version")
    _literal(
        item["observation_type"],
        OBSERVATION_TYPE,
        f"{context} type",
    )
    reviewer_session_id = item["reviewer_session_id"]
    if (
        not isinstance(reviewer_session_id, str)
        or UUID4_PATTERN.fullmatch(reviewer_session_id) is None
    ):
        raise SemanticReviewWorkflowError(
            f"{context} reviewer session must be a lowercase UUIDv4"
        )
    _literal(
        item["independence_attestation"],
        True,
        f"{context} independence attestation",
    )
    source = bundle.ledger["source_pcm"]
    translated = bundle.ledger["translated_pcm"]
    bindings = (
        ("assignment_sha256", assignment.sha256, _require_hash),
        ("schedule_ledger_sha256", bundle.ledger_sha256, _require_hash),
        ("source_pcm_sha256", source["pcm_sha256"], _require_hash),
        (
            "source_pcm_sample_count",
            source["sample_count"],
            _plain_int,
        ),
        (
            "translated_pcm_sha256",
            translated["pcm_sha256"],
            _require_hash,
        ),
        (
            "translated_pcm_sample_count",
            translated["sample_count"],
            _plain_int,
        ),
    )
    for key, expected, validator in bindings:
        actual = validator(item[key], f"{context} {key}")
        if actual != expected:
            raise SemanticReviewWorkflowError(
                f"{context} does not bind the current assignment and capture"
            )
    privacy = _exact(
        item["privacy"],
        frozenset(OBSERVATION_PRIVACY),
        f"{context} privacy",
    )
    for key, expected in OBSERVATION_PRIVACY.items():
        _literal(privacy[key], expected, f"{context} privacy {key}")

    raw_events = item["events"]
    assignment_events = assignment.document["events"]
    if (
        type(raw_events) is not list
        or len(raw_events) != len(assignment_events)
    ):
        raise SemanticReviewWorkflowError(
            f"{context} must contain every assigned event exactly once"
        )
    normalized_events: list[dict[str, Any]] = []
    for event_index, (raw, assigned) in enumerate(
        zip(raw_events, assignment_events)
    ):
        event_context = f"{context} event {event_index + 1}"
        event = _exact(raw, OBSERVATION_EVENT_KEYS, event_context)
        _literal(
            event["event_id"],
            assigned["event_id"],
            f"{event_context} ID",
        )
        source_sample = _plain_int(
            event["source_sample_index"],
            f"{event_context} source sample",
        )
        translated_sample = _plain_int(
            event["translated_sample_index"],
            f"{event_context} translated sample",
        )
        if not (
            assigned["source_window_start_sample"]
            <= source_sample
            < assigned["source_window_end_sample_exclusive"]
        ):
            raise SemanticReviewWorkflowError(
                f"{event_context} source sample is outside its assignment"
            )
        if translated_sample >= translated["sample_count"]:
            raise SemanticReviewWorkflowError(
                f"{event_context} translated sample is outside PCM"
            )
        equivalence = event["semantic_equivalence"]
        if equivalence not in SEMANTIC_EQUIVALENCE_VALUES:
            raise SemanticReviewWorkflowError(
                f"{event_context} has invalid semantic equivalence"
            )
        ratings: dict[str, int] = {}
        for key in (
            "source_boundary_confidence",
            "translated_boundary_confidence",
            "translation_quality_rating",
            "intelligibility_rating",
            "naturalness_rating",
        ):
            ratings[key] = _plain_int(
                event[key],
                f"{event_context} {key}",
                minimum=1,
                maximum=5,
            )
        flags = event["issue_flags"]
        if (
            type(flags) is not list
            or any(type(flag) is not str for flag in flags)
            or len(flags) != len(set(flags))
            or flags != sorted(flags)
            or any(flag not in ISSUE_FLAGS for flag in flags)
        ):
            raise SemanticReviewWorkflowError(
                f"{event_context} issue flags must be unique, sorted, "
                "and registered"
            )
        if (
            "omission" in flags
            and (
                equivalence != "rejected"
                or ratings["translated_boundary_confidence"] != 1
            )
        ):
            raise SemanticReviewWorkflowError(
                f"{event_context} omission must be rejected with "
                "translated boundary confidence 1"
            )
        normalized_events.append(dict(event))
    normalized = dict(item)
    normalized["events"] = normalized_events
    return Observation(
        document=normalized,
        payload=payload,
        identity=identity,
    )


def _load_observations(
    bundle: CaptureBundle,
    assignment: Assignment,
    reviewer_files: Sequence[Path | str],
) -> list[Observation]:
    if (
        isinstance(reviewer_files, (str, bytes))
        or len(reviewer_files) < MINIMUM_REVIEWERS
        or len(reviewer_files) > MAXIMUM_REVIEWERS
    ):
        raise SemanticReviewWorkflowError(
            "at least two distinct review files are required"
        )
    observations: list[Observation] = []
    identities: set[tuple[int, int]] = set()
    payload_hashes: set[str] = set()
    sessions: set[str] = set()
    for index, reviewer_file in enumerate(reviewer_files, 1):
        private_file = _read_capture_file(
            bundle.directory,
            reviewer_file,
            maximum_bytes=MAX_JSON_BYTES,
            repository_root=bundle.repository_root,
        )
        document = _load_strict_json(
            private_file.payload,
            f"review observation {index}",
        )
        observation = _validate_observation(
            document,
            private_file.payload,
            private_file.identity,
            assignment=assignment,
            bundle=bundle,
            ordinal=index,
        )
        payload_hash = _sha256(observation.payload)
        session = observation.document["reviewer_session_id"]
        if (
            observation.identity in identities
            or payload_hash in payload_hashes
            or session in sessions
        ):
            raise SemanticReviewWorkflowError(
                "review files must represent distinct independent sessions"
            )
        identities.add(observation.identity)
        payload_hashes.add(payload_hash)
        sessions.add(session)
        observations.append(observation)
    return observations


def compare_reviews(
    capture_dir: Path | str,
    reviewer_files: Sequence[Path | str],
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Return a path- and identity-free structured comparison."""

    bundle = validate_capture_bundle(
        capture_dir,
        repository_root=repository_root,
    )
    assignment = _load_assignment(bundle)
    observations = _load_observations(
        bundle,
        assignment,
        reviewer_files,
    )
    labels = [f"review-{index}" for index in range(1, len(observations) + 1)]
    events: list[dict[str, Any]] = []
    for event_index, assigned in enumerate(assignment.document["events"]):
        reviews: list[dict[str, Any]] = []
        for label, observation in zip(labels, observations):
            event = observation.document["events"][event_index]
            reviews.append(
                {
                    "review": label,
                    "source_sample_index": event["source_sample_index"],
                    "translated_sample_index": (
                        event["translated_sample_index"]
                    ),
                    "semantic_equivalence": event[
                        "semantic_equivalence"
                    ],
                    "source_boundary_confidence": event[
                        "source_boundary_confidence"
                    ],
                    "translated_boundary_confidence": event[
                        "translated_boundary_confidence"
                    ],
                    "translation_quality_rating": event[
                        "translation_quality_rating"
                    ],
                    "intelligibility_rating": event[
                        "intelligibility_rating"
                    ],
                    "naturalness_rating": event["naturalness_rating"],
                    "issue_flags": list(event["issue_flags"]),
                }
            )
        deltas: list[dict[str, Any]] = []
        for left, right in combinations(range(len(reviews)), 2):
            deltas.append(
                {
                    "left_review": labels[left],
                    "right_review": labels[right],
                    "source_sample_delta_right_minus_left": (
                        reviews[right]["source_sample_index"]
                        - reviews[left]["source_sample_index"]
                    ),
                    "translated_sample_delta_right_minus_left": (
                        reviews[right]["translated_sample_index"]
                        - reviews[left]["translated_sample_index"]
                    ),
                }
            )
        events.append(
            {
                "event_id": assigned["event_id"],
                "reviews": reviews,
                "marker_deltas": deltas,
            }
        )
    comparison = {
        "schema_version": SCHEMA_VERSION,
        "comparison_type": COMPARISON_TYPE,
        "independent_reviewer_count": len(observations),
        "events": events,
        "privacy": dict(OBSERVATION_PRIVACY),
    }
    current = validate_capture_bundle(
        bundle.directory,
        repository_root=bundle.repository_root,
    )
    current_assignment = _load_assignment(current)
    current_observations = _load_observations(
        current,
        current_assignment,
        reviewer_files,
    )
    if (
        current.ledger_bytes != bundle.ledger_bytes
        or current.source_wav_bytes != bundle.source_wav_bytes
        or current.translated_wav_bytes != bundle.translated_wav_bytes
        or current_assignment.payload != assignment.payload
        or [item.payload for item in current_observations]
        != [item.payload for item in observations]
    ):
        raise SemanticReviewWorkflowError(
            "capture, assignment, or review changed during comparison"
        )
    return comparison


def _parse_canonicals(
    specs: Sequence[str],
    assignment: Assignment,
    bundle: CaptureBundle,
) -> list[tuple[str, int, int]]:
    assigned_events = assignment.document["events"]
    if len(specs) != len(assigned_events):
        raise SemanticReviewWorkflowError(
            "an explicit canonical is required for every assigned event"
        )
    assigned_by_id = {
        event["event_id"]: (index, event)
        for index, event in enumerate(assigned_events)
    }
    seen: set[str] = set()
    previous_assignment_index = -1
    previous_source = -1
    previous_translated = -1
    result: list[tuple[str, int, int]] = []
    translated_count = bundle.ledger["translated_pcm"]["sample_count"]
    for ordinal, spec in enumerate(specs, 1):
        match = (
            None
            if len(spec) > 128
            else CANONICAL_PATTERN.fullmatch(spec)
        )
        if match is None:
            raise SemanticReviewWorkflowError(
                f"canonical selection {ordinal} has invalid syntax"
            )
        event_id, source_text, translated_text = match.groups()
        if event_id in seen or event_id not in assigned_by_id:
            raise SemanticReviewWorkflowError(
                "canonical event IDs must be unique assigned events"
            )
        assignment_index, assigned = assigned_by_id[event_id]
        source = int(source_text)
        translated = int(translated_text)
        if (
            assignment_index <= previous_assignment_index
            or source <= previous_source
            or translated <= previous_translated
            or not (
                assigned["source_window_start_sample"]
                <= source
                < assigned["source_window_end_sample_exclusive"]
            )
            or translated >= translated_count
        ):
            raise SemanticReviewWorkflowError(
                "canonical events must be ordered and inside assignment "
                "and PCM bounds"
            )
        seen.add(event_id)
        previous_assignment_index = assignment_index
        previous_source = source
        previous_translated = translated
        result.append((event_id, source, translated))
    if seen != set(assigned_by_id):
        raise SemanticReviewWorkflowError(
            "an explicit canonical is required for every assigned event"
        )
    return result


def _count_scale(events: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    return {
        str(value): sum(event[key] == value for event in events)
        for value in range(1, 6)
    }


def _feedback_event(
    event_id: str,
    source: int,
    translated: int,
    reviewer_events: Sequence[Mapping[str, Any]],
    eligible_source: Sequence[Mapping[str, Any]],
    eligible_translated: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "canonical_source_sample_index": source,
        "canonical_translated_sample_index": translated,
        "eligible_source_reviewer_count": len(eligible_source),
        "eligible_translated_reviewer_count": len(eligible_translated),
        "eligible_source_sample_range": {
            "minimum": min(
                event["source_sample_index"] for event in eligible_source
            ),
            "maximum": max(
                event["source_sample_index"] for event in eligible_source
            ),
        },
        "eligible_translated_sample_range": {
            "minimum": min(
                event["translated_sample_index"]
                for event in eligible_translated
            ),
            "maximum": max(
                event["translated_sample_index"]
                for event in eligible_translated
            ),
        },
        "semantic_equivalence_counts": {
            value: sum(
                event["semantic_equivalence"] == value
                for event in reviewer_events
            )
            for value in SEMANTIC_EQUIVALENCE_VALUES
        },
        "source_boundary_confidence_counts": _count_scale(
            reviewer_events,
            "source_boundary_confidence",
        ),
        "translated_boundary_confidence_counts": _count_scale(
            reviewer_events,
            "translated_boundary_confidence",
        ),
        "translation_quality_rating_counts": _count_scale(
            reviewer_events,
            "translation_quality_rating",
        ),
        "intelligibility_rating_counts": _count_scale(
            reviewer_events,
            "intelligibility_rating",
        ),
        "naturalness_rating_counts": _count_scale(
            reviewer_events,
            "naturalness_rating",
        ),
        "issue_flag_counts": {
            flag: sum(
                flag in event["issue_flags"] for event in reviewer_events
            )
            for flag in sorted(ISSUE_FLAGS)
        },
    }


def reconcile_reviews(
    capture_dir: Path | str,
    reviewer_files: Sequence[Path | str],
    canonical_specs: Sequence[str],
    *,
    repository_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish analyzer markers and aggregate feedback without reviewer IDs."""

    bundle = validate_capture_bundle(
        capture_dir,
        repository_root=repository_root,
    )
    assignment = _load_assignment(bundle)
    observations = _load_observations(
        bundle,
        assignment,
        reviewer_files,
    )
    canonicals = _parse_canonicals(canonical_specs, assignment, bundle)
    assignment_index = {
        event["event_id"]: index
        for index, event in enumerate(assignment.document["events"])
    }
    marker_events: list[dict[str, Any]] = []
    feedback_events: list[dict[str, Any]] = []
    for event_id, source, translated in canonicals:
        index = assignment_index[event_id]
        reviewer_events = [
            observation.document["events"][index]
            for observation in observations
        ]
        eligible_source = [
            event
            for event in reviewer_events
            if (
                event["semantic_equivalence"] == "accepted"
                and event["source_boundary_confidence"]
                >= MINIMUM_BOUNDARY_CONFIDENCE
            )
        ]
        eligible_translated = [
            event
            for event in reviewer_events
            if (
                event["semantic_equivalence"] == "accepted"
                and event["translated_boundary_confidence"]
                >= MINIMUM_BOUNDARY_CONFIDENCE
            )
        ]
        if (
            len(eligible_source) < MINIMUM_REVIEWERS
            or len(eligible_translated) < MINIMUM_REVIEWERS
        ):
            raise SemanticReviewWorkflowError(
                f"{event_id} lacks two accepted high-confidence reviewers "
                "for one or both boundaries"
            )
        source_values = [
            event["source_sample_index"] for event in eligible_source
        ]
        translated_values = [
            event["translated_sample_index"]
            for event in eligible_translated
        ]
        if not (
            min(source_values) <= source <= max(source_values)
            and min(translated_values)
            <= translated
            <= max(translated_values)
        ):
            raise SemanticReviewWorkflowError(
                f"{event_id} canonical samples must remain within eligible "
                "reviewer ranges"
            )
        marker_events.append(
            {
                "event_id": event_id,
                "source_sample_index": source,
                "translated_sample_index": translated,
                "source_independent_reviewer_count": len(eligible_source),
                "translated_independent_reviewer_count": (
                    len(eligible_translated)
                ),
            }
        )
        feedback_events.append(
            _feedback_event(
                event_id,
                source,
                translated,
                reviewer_events,
                eligible_source,
                eligible_translated,
            )
        )
    source_pcm = bundle.ledger["source_pcm"]
    translated_pcm = bundle.ledger["translated_pcm"]
    markers = {
        "schema_version": SCHEMA_VERSION,
        "schedule_ledger_sha256": bundle.ledger_sha256,
        "source_pcm_sha256": source_pcm["pcm_sha256"],
        "source_pcm_sample_count": source_pcm["sample_count"],
        "translated_pcm_sha256": translated_pcm["pcm_sha256"],
        "translated_pcm_sample_count": translated_pcm["sample_count"],
        "events": marker_events,
    }
    feedback = {
        "schema_version": SCHEMA_VERSION,
        "feedback_type": FEEDBACK_TYPE,
        "assignment_sha256": assignment.sha256,
        "schedule_ledger_sha256": bundle.ledger_sha256,
        "source_pcm_sha256": source_pcm["pcm_sha256"],
        "source_pcm_sample_count": source_pcm["sample_count"],
        "translated_pcm_sha256": translated_pcm["pcm_sha256"],
        "translated_pcm_sample_count": translated_pcm["sample_count"],
        "independent_reviewer_count": len(observations),
        "minimum_boundary_confidence": MINIMUM_BOUNDARY_CONFIDENCE,
        "events": feedback_events,
        "privacy": dict(OBSERVATION_PRIVACY),
    }
    # Detect capture/assignment drift before committing either output.
    current = validate_capture_bundle(
        bundle.directory,
        repository_root=bundle.repository_root,
    )
    current_assignment = _load_assignment(current)
    current_observations = _load_observations(
        current,
        current_assignment,
        reviewer_files,
    )
    if (
        current.ledger_bytes != bundle.ledger_bytes
        or current.source_wav_bytes != bundle.source_wav_bytes
        or current.translated_wav_bytes != bundle.translated_wav_bytes
        or current_assignment.payload != assignment.payload
        or [item.payload for item in current_observations]
        != [item.payload for item in observations]
    ):
        raise SemanticReviewWorkflowError(
            "review evidence changed during reconciliation"
        )
    _publish_exclusive(
        bundle,
        {
            REVIEWER_MARKERS_FILENAME: _json_bytes(markers),
            REVIEWER_FEEDBACK_FILENAME: _json_bytes(feedback),
        },
    )
    return markers, feedback


def _load_markers(
    bundle: CaptureBundle,
    assignment: Assignment,
) -> tuple[dict[str, Any], bytes]:
    private_file = _read_capture_file(
        bundle.directory,
        REVIEWER_MARKERS_FILENAME,
        maximum_bytes=MAX_JSON_BYTES,
        repository_root=bundle.repository_root,
    )
    document = _load_strict_json(
        private_file.payload,
        "reviewer markers",
    )
    item = _exact(document, set(MARKER_DOCUMENT_KEYS), "reviewer markers")
    _literal(item["schema_version"], SCHEMA_VERSION, "marker version")
    source = bundle.ledger["source_pcm"]
    translated = bundle.ledger["translated_pcm"]
    bindings = (
        ("schedule_ledger_sha256", bundle.ledger_sha256, _require_hash),
        ("source_pcm_sha256", source["pcm_sha256"], _require_hash),
        (
            "source_pcm_sample_count",
            source["sample_count"],
            _plain_int,
        ),
        (
            "translated_pcm_sha256",
            translated["pcm_sha256"],
            _require_hash,
        ),
        (
            "translated_pcm_sample_count",
            translated["sample_count"],
            _plain_int,
        ),
    )
    for key, expected, validator in bindings:
        actual = validator(item[key], f"reviewer markers {key}")
        if actual != expected:
            raise SemanticReviewWorkflowError(
                "reviewer markers do not bind the current capture"
            )
    events = item["events"]
    if type(events) is not list or len(events) < 3:
        raise SemanticReviewWorkflowError(
            "reviewer markers require at least three events"
        )
    assignment_by_id = {
        event["event_id"]: (index, event)
        for index, event in enumerate(assignment.document["events"])
    }
    previous_assignment_index = -1
    previous_source = -1
    previous_translated = -1
    seen: set[str] = set()
    normalized_events: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(events, 1):
        event = _exact(
            raw,
            set(MARKER_EVENT_KEYS),
            f"reviewer marker event {ordinal}",
        )
        event_id = event["event_id"]
        if (
            not isinstance(event_id, str)
            or event_id in seen
            or event_id not in assignment_by_id
        ):
            raise SemanticReviewWorkflowError(
                "reviewer marker event IDs must be unique assigned events"
            )
        assignment_index, assigned = assignment_by_id[event_id]
        source_sample = _plain_int(
            event["source_sample_index"],
            f"{event_id} source sample",
        )
        translated_sample = _plain_int(
            event["translated_sample_index"],
            f"{event_id} translated sample",
        )
        source_reviewers = _plain_int(
            event["source_independent_reviewer_count"],
            f"{event_id} source reviewer count",
            minimum=MINIMUM_REVIEWERS,
        )
        translated_reviewers = _plain_int(
            event["translated_independent_reviewer_count"],
            f"{event_id} translated reviewer count",
            minimum=MINIMUM_REVIEWERS,
        )
        if (
            assignment_index <= previous_assignment_index
            or source_sample <= previous_source
            or translated_sample <= previous_translated
            or not (
                assigned["source_window_start_sample"]
                <= source_sample
                < assigned["source_window_end_sample_exclusive"]
            )
            or translated_sample >= translated["sample_count"]
        ):
            raise SemanticReviewWorkflowError(
                "reviewer markers must be ordered and inside assignment "
                "and PCM bounds"
            )
        previous_assignment_index = assignment_index
        previous_source = source_sample
        previous_translated = translated_sample
        seen.add(event_id)
        normalized_events.append(
            {
                "event_id": event_id,
                "source_sample_index": source_sample,
                "translated_sample_index": translated_sample,
                "source_independent_reviewer_count": source_reviewers,
                "translated_independent_reviewer_count": (
                    translated_reviewers
                ),
            }
        )
    normalized = dict(item)
    normalized["events"] = normalized_events
    return normalized, private_file.payload


def _load_feedback(
    bundle: CaptureBundle,
    assignment: Assignment,
    markers: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    top_keys = frozenset(
        {
            "schema_version",
            "feedback_type",
            "assignment_sha256",
            "schedule_ledger_sha256",
            "source_pcm_sha256",
            "source_pcm_sample_count",
            "translated_pcm_sha256",
            "translated_pcm_sample_count",
            "independent_reviewer_count",
            "minimum_boundary_confidence",
            "events",
            "privacy",
        }
    )
    event_keys = frozenset(
        {
            "event_id",
            "canonical_source_sample_index",
            "canonical_translated_sample_index",
            "eligible_source_reviewer_count",
            "eligible_translated_reviewer_count",
            "eligible_source_sample_range",
            "eligible_translated_sample_range",
            "semantic_equivalence_counts",
            "source_boundary_confidence_counts",
            "translated_boundary_confidence_counts",
            "translation_quality_rating_counts",
            "intelligibility_rating_counts",
            "naturalness_rating_counts",
            "issue_flag_counts",
        }
    )
    private_file = _read_capture_file(
        bundle.directory,
        REVIEWER_FEEDBACK_FILENAME,
        maximum_bytes=MAX_JSON_BYTES,
        repository_root=bundle.repository_root,
    )
    document = _load_strict_json(
        private_file.payload,
        "reviewer feedback",
    )
    item = _exact(document, top_keys, "reviewer feedback")
    _literal(item["schema_version"], SCHEMA_VERSION, "feedback version")
    _literal(item["feedback_type"], FEEDBACK_TYPE, "feedback type")
    _literal(
        item["minimum_boundary_confidence"],
        MINIMUM_BOUNDARY_CONFIDENCE,
        "feedback minimum boundary confidence",
    )
    reviewer_count = _plain_int(
        item["independent_reviewer_count"],
        "feedback independent reviewer count",
        minimum=MINIMUM_REVIEWERS,
        maximum=MAXIMUM_REVIEWERS,
    )
    source = bundle.ledger["source_pcm"]
    translated = bundle.ledger["translated_pcm"]
    bindings = (
        ("assignment_sha256", assignment.sha256, _require_hash),
        ("schedule_ledger_sha256", bundle.ledger_sha256, _require_hash),
        ("source_pcm_sha256", source["pcm_sha256"], _require_hash),
        (
            "source_pcm_sample_count",
            source["sample_count"],
            _plain_int,
        ),
        (
            "translated_pcm_sha256",
            translated["pcm_sha256"],
            _require_hash,
        ),
        (
            "translated_pcm_sample_count",
            translated["sample_count"],
            _plain_int,
        ),
    )
    for key, expected, validator in bindings:
        actual = validator(item[key], f"reviewer feedback {key}")
        if actual != expected:
            raise SemanticReviewWorkflowError(
                "reviewer feedback does not bind current evidence"
            )
    privacy = _exact(
        item["privacy"],
        frozenset(OBSERVATION_PRIVACY),
        "reviewer feedback privacy",
    )
    for key, expected in OBSERVATION_PRIVACY.items():
        _literal(privacy[key], expected, f"feedback privacy {key}")
    feedback_events = item["events"]
    marker_events = markers["events"]
    assigned_by_id = {
        event["event_id"]: event
        for event in assignment.document["events"]
    }
    if (
        type(feedback_events) is not list
        or len(feedback_events) != len(marker_events)
    ):
        raise SemanticReviewWorkflowError(
            "reviewer feedback must reconcile every marker event"
        )
    scale_keys = {str(value) for value in range(1, 6)}
    for ordinal, (raw, marker) in enumerate(
        zip(feedback_events, marker_events),
        1,
    ):
        event = _exact(raw, event_keys, f"feedback event {ordinal}")
        _literal(event["event_id"], marker["event_id"], "feedback event ID")
        assigned = assigned_by_id[marker["event_id"]]
        _literal(
            event["canonical_source_sample_index"],
            marker["source_sample_index"],
            "feedback canonical source sample",
        )
        _literal(
            event["canonical_translated_sample_index"],
            marker["translated_sample_index"],
            "feedback canonical translated sample",
        )
        for feedback_key, marker_key in (
            (
                "eligible_source_reviewer_count",
                "source_independent_reviewer_count",
            ),
            (
                "eligible_translated_reviewer_count",
                "translated_independent_reviewer_count",
            ),
        ):
            count = _plain_int(
                event[feedback_key],
                f"feedback event {ordinal} {feedback_key}",
                minimum=MINIMUM_REVIEWERS,
                maximum=reviewer_count,
            )
            _literal(
                count,
                marker[marker_key],
                f"feedback event {ordinal} eligible count",
            )
        for range_key, canonical_key in (
            (
                "eligible_source_sample_range",
                "canonical_source_sample_index",
            ),
            (
                "eligible_translated_sample_range",
                "canonical_translated_sample_index",
            ),
        ):
            sample_range = _exact(
                event[range_key],
                frozenset({"minimum", "maximum"}),
                f"feedback event {ordinal} {range_key}",
            )
            minimum = _plain_int(
                sample_range["minimum"],
                f"feedback event {ordinal} range minimum",
            )
            maximum = _plain_int(
                sample_range["maximum"],
                f"feedback event {ordinal} range maximum",
            )
            if not minimum <= event[canonical_key] <= maximum:
                raise SemanticReviewWorkflowError(
                    "feedback canonical sample is outside eligible range"
                )
            if range_key == "eligible_source_sample_range":
                range_is_bound = (
                    assigned["source_window_start_sample"]
                    <= minimum
                    <= maximum
                    < assigned["source_window_end_sample_exclusive"]
                )
            else:
                range_is_bound = (
                    minimum <= maximum
                    < bundle.ledger["translated_pcm"]["sample_count"]
                )
            if not range_is_bound:
                raise SemanticReviewWorkflowError(
                    "feedback eligible range is outside review evidence"
                )
        equivalence = _exact(
            event["semantic_equivalence_counts"],
            frozenset(SEMANTIC_EQUIVALENCE_VALUES),
            f"feedback event {ordinal} equivalence counts",
        )
        normalized_equivalence = {
            key: _plain_int(
                equivalence[key],
                f"feedback event {ordinal} equivalence count",
            )
            for key in equivalence
        }
        if sum(normalized_equivalence.values()) != reviewer_count:
            raise SemanticReviewWorkflowError(
                "feedback equivalence counts do not reconcile"
            )
        if normalized_equivalence["accepted"] < max(
            event["eligible_source_reviewer_count"],
            event["eligible_translated_reviewer_count"],
        ):
            raise SemanticReviewWorkflowError(
                "feedback accepted count cannot support eligible reviewers"
            )
        high_confidence_counts: dict[str, int] = {}
        for key in (
            "source_boundary_confidence_counts",
            "translated_boundary_confidence_counts",
            "translation_quality_rating_counts",
            "intelligibility_rating_counts",
            "naturalness_rating_counts",
        ):
            counts = _exact(
                event[key],
                scale_keys,
                f"feedback event {ordinal} {key}",
            )
            normalized_counts = {
                score: _plain_int(
                    value,
                    f"feedback event {ordinal} rating count",
                )
                for score, value in counts.items()
            }
            if sum(normalized_counts.values()) != reviewer_count:
                raise SemanticReviewWorkflowError(
                    "feedback rating counts do not reconcile"
                )
            if key == "source_boundary_confidence_counts":
                eligible_count = event[
                    "eligible_source_reviewer_count"
                ]
                boundary = "source"
            elif key == "translated_boundary_confidence_counts":
                eligible_count = event[
                    "eligible_translated_reviewer_count"
                ]
                boundary = "translated"
            else:
                eligible_count = 0
                boundary = ""
            high_confidence_count = (
                normalized_counts["3"]
                + normalized_counts["4"]
                + normalized_counts["5"]
            )
            if boundary:
                high_confidence_counts[boundary] = high_confidence_count
            if (
                high_confidence_count < eligible_count
            ):
                raise SemanticReviewWorkflowError(
                    "feedback confidence counts cannot support eligible "
                    "reviewers"
                )
        accepted_count = normalized_equivalence["accepted"]
        for boundary in ("source", "translated"):
            eligible_count = event[
                f"eligible_{boundary}_reviewer_count"
            ]
            high_confidence_count = high_confidence_counts[boundary]
            minimum_intersection = max(
                0,
                accepted_count + high_confidence_count - reviewer_count,
            )
            if not (
                minimum_intersection
                <= eligible_count
                <= min(accepted_count, high_confidence_count)
            ):
                raise SemanticReviewWorkflowError(
                    "feedback eligible count is not a possible accepted "
                    "high-confidence intersection"
                )
        issue_counts = _exact(
            event["issue_flag_counts"],
            frozenset(ISSUE_FLAGS),
            f"feedback event {ordinal} issue counts",
        )
        for value in issue_counts.values():
            _plain_int(
                value,
                f"feedback event {ordinal} issue count",
                maximum=reviewer_count,
            )
    return item, private_file.payload


def analyze_review(
    capture_dir: Path | str,
    *,
    repository_root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Run and privately publish both registered semantic-delay gates."""

    bundle = validate_capture_bundle(
        capture_dir,
        repository_root=repository_root,
    )
    assignment = _load_assignment(bundle)
    markers, marker_bytes = _load_markers(bundle, assignment)
    _feedback, feedback_bytes = _load_feedback(
        bundle,
        assignment,
        markers,
    )
    results: dict[str, dict[str, Any]] = {}
    statuses: dict[str, str] = {}
    for label, maximum in (("5_seconds", 5.0), ("10_seconds", 10.0)):
        result = analyze_scheduled_semantic_delay(
            bundle.directory / LEDGER_FILENAME,
            bundle.directory / SOURCE_WAV_FILENAME,
            bundle.directory / TRANSLATED_WAV_FILENAME,
            bundle.directory / REVIEWER_MARKERS_FILENAME,
            maximum,
            minimum_reviewers=MINIMUM_REVIEWERS,
        )
        if type(result) is not dict:
            raise SemanticReviewWorkflowError(
                "analyzer returned an invalid report"
            )
        evidence = result.get("evidence")
        summary = result.get("summary")
        if type(evidence) is not dict or type(summary) is not dict:
            raise SemanticReviewWorkflowError(
                "analyzer returned an invalid report"
            )
        status = summary.get("overall_status")
        if type(status) is not str or status not in GATE_EXIT_CODES:
            raise SemanticReviewWorkflowError(
                "analyzer returned an invalid gate status"
            )
        if (
            evidence.get("schedule_ledger_sha256") != bundle.ledger_sha256
            or evidence.get("reviewer_markers_sha256")
            != _sha256(marker_bytes)
        ):
            raise SemanticReviewWorkflowError(
                "analyzer result does not bind the validated evidence"
            )
        results[label] = result
        statuses[label] = status
    # Re-read all bound evidence before publishing the four-report set.
    current = validate_capture_bundle(
        bundle.directory,
        repository_root=bundle.repository_root,
    )
    current_assignment = _load_assignment(current)
    current_markers, current_marker_bytes = _load_markers(
        current,
        current_assignment,
    )
    _current_feedback, current_feedback_bytes = _load_feedback(
        current,
        current_assignment,
        current_markers,
    )
    if (
        current.ledger_bytes != bundle.ledger_bytes
        or current.source_wav_bytes != bundle.source_wav_bytes
        or current.translated_wav_bytes != bundle.translated_wav_bytes
        or current_assignment.payload != assignment.payload
        or current_marker_bytes != marker_bytes
        or current_feedback_bytes != feedback_bytes
    ):
        raise SemanticReviewWorkflowError(
            "semantic review evidence changed during analysis"
        )
    _publish_exclusive(
        bundle,
        {
            FIVE_SECOND_JSON_FILENAME: _json_bytes(results["5_seconds"]),
            FIVE_SECOND_MARKDOWN_FILENAME: (
                render_scheduled_semantic_delay_markdown(
                    results["5_seconds"]
                ).encode("utf-8")
            ),
            TEN_SECOND_JSON_FILENAME: _json_bytes(results["10_seconds"]),
            TEN_SECOND_MARKDOWN_FILENAME: (
                render_scheduled_semantic_delay_markdown(
                    results["10_seconds"]
                ).encode("utf-8")
            ),
        },
    )
    if "fail" in statuses.values():
        code = GATE_EXIT_CODES["fail"]
    elif "inconclusive" in statuses.values():
        code = GATE_EXIT_CODES["inconclusive"]
    else:
        code = GATE_EXIT_CODES["pass"]
    return {"results": results, "statuses": statuses}, code


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare, compare, reconcile, and analyze a private bilingual "
            "semantic review."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="validate a capture and create the private review bundle",
    )
    prepare.add_argument(
        "--capture-dir",
        "--artifact-dir",
        dest="capture_dir",
        type=Path,
        required=True,
    )
    windows = prepare.add_mutually_exclusive_group(required=True)
    windows.add_argument("--default-three-windows", action="store_true")
    windows.add_argument(
        "--event-window",
        action="append",
        metavar="event-NNN:START_SEC:END_SEC",
    )

    compare = subparsers.add_parser(
        "compare",
        help="print an anonymous structured comparison",
    )
    compare.add_argument(
        "--capture-dir",
        "--artifact-dir",
        dest="capture_dir",
        type=Path,
        required=True,
    )
    compare.add_argument(
        "--review",
        "--review-file",
        dest="reviews",
        type=Path,
        action="append",
        required=True,
    )

    reconcile = subparsers.add_parser(
        "reconcile",
        help="publish reviewer-supported canonical markers",
    )
    reconcile.add_argument(
        "--capture-dir",
        "--artifact-dir",
        dest="capture_dir",
        type=Path,
        required=True,
    )
    reconcile.add_argument(
        "--review",
        "--review-file",
        dest="reviews",
        type=Path,
        action="append",
        required=True,
    )
    reconcile.add_argument(
        "--canonical",
        action="append",
        required=True,
        metavar="event-NNN:SOURCE_SAMPLE:TRANSLATED_SAMPLE",
    )

    analyze = subparsers.add_parser(
        "analyze",
        help="run the five- and ten-second semantic-delay gates",
    )
    analyze.add_argument(
        "--capture-dir",
        "--artifact-dir",
        dest="capture_dir",
        type=Path,
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_argument_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            assignment = prepare_review(
                args.capture_dir,
                default_three_windows=args.default_three_windows,
                event_windows=args.event_window,
            )
            print(
                json.dumps(
                    {
                        "assignment_sha256": assignment.sha256,
                        "event_count": len(
                            assignment.document["events"]
                        ),
                        "status": "prepared",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "compare":
            result = compare_reviews(args.capture_dir, args.reviews)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "reconcile":
            markers, feedback = reconcile_reviews(
                args.capture_dir,
                args.reviews,
                args.canonical,
            )
            print(
                json.dumps(
                    {
                        "event_count": len(markers["events"]),
                        "reviewer_markers_sha256": _sha256(
                            _json_bytes(markers)
                        ),
                        "reviewer_feedback_sha256": _sha256(
                            _json_bytes(feedback)
                        ),
                        "status": "reconciled",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "analyze":
            outcome, code = analyze_review(args.capture_dir)
            print(
                json.dumps(
                    {
                        "five_second_status": outcome["statuses"][
                            "5_seconds"
                        ],
                        "ten_second_status": outcome["statuses"][
                            "10_seconds"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return code
        raise SemanticReviewWorkflowError("unknown workflow command")
    except (
        SemanticReviewWorkflowError,
        ScheduledSemanticDelayError,
        PrivatePcmScheduleLedgerError,
    ) as exc:
        print(f"Semantic review workflow failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "Semantic review workflow failed: private artifact I/O error",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
