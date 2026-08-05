#!/usr/bin/env python3
"""Validate private S2S audio evidence and bound scheduled semantic delay.

The analyzer consumes one exact-schema private schedule ledger, the two review
WAVs bound by that ledger, and an anonymous bilingual-review marker sidecar.
It independently replays the registered adaptive playback policy before
mapping each reviewed translated sample to its scheduled digital interval.

The resulting bounds include one source-sample interval and one translated
sample interval.  They measure scheduled digital playback, not DAC output,
physical audibility, room response, or translation quality.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import stat
import statistics
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from playback_simulation import (
    DEFAULT_PLAYBACK_POLICY,
    AudioChunk,
    ScheduledChunk,
    simulate_playback,
)


LEDGER_SCHEMA_VERSION = 1
MARKER_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
LEDGER_REPORT_TYPE = "private_pcm_schedule_ledger"
ANALYSIS_TYPE = "scheduled_semantic_delay"
DEFAULT_MINIMUM_REVIEWERS = 2
SOURCE_REVIEW_WAV_FILENAME = "source-review.wav"
TRANSLATED_REVIEW_WAV_FILENAME = "translated-review.wav"
SCHEDULE_LEDGER_FILENAME = "schedule-ledger.json"
REVIEWER_MARKERS_FILENAME = "reviewer-markers.json"
CHANNELS = 1
BYTES_PER_SAMPLE = 2
INPUT_PACING_MODE = "chunk_end_boundary_v1"
CAPTURE_CLOCK = "client_monotonic_from_capture_start"
EARLY_EMISSION_TOLERANCE_SECONDS = 0.001
SOURCE_DEADLINE_TOLERANCE_SECONDS = 1e-6
FLOAT_RECONCILIATION_TOLERANCE = 1e-9
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_WAV_BYTES = 1024 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
EVENT_ID_PATTERN = re.compile(r"event-[0-9]{3}\Z")
PLAYBACK_MODES = frozenset({"normal", "catch-up", "urgent", "over-limit"})
GATE_EXIT_CODES = {
    "pass": 0,
    "fail": 1,
    "inconclusive": 3,
}

LEDGER_KEYS = {
    "schema_version",
    "report_type",
    "capture",
    "source_pcm",
    "translated_pcm",
    "policy",
    "source_chunks",
    "frames",
    "parents",
    "privacy",
}
CAPTURE_KEYS = {
    "audio_metadata_protocol_version",
    "stream_generation",
    "terminal_completed",
    "input_pacing_mode",
    "clock",
    "input_sample_zero_seconds",
    "input_end_seconds",
    "source_chunk_count",
    "parent_count",
    "frame_count",
    "canonical_replay_verified",
}
PCM_KEYS = {
    "sample_rate_hz",
    "channels",
    "bytes_per_sample",
    "sample_count",
    "audio_bytes",
    "pcm_sha256",
    "review_wav_sha256",
}
POLICY_KEYS = set(asdict(DEFAULT_PLAYBACK_POLICY))
SOURCE_CHUNK_KEYS = {
    "chunk_index",
    "sample_start",
    "sample_end_exclusive",
    "audio_bytes",
    "deadline_seconds",
    "emitted_seconds",
}
FRAME_KEYS = {
    "source_index",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "audio_bytes",
    "sample_count",
    "translated_sample_start",
    "translated_sample_end_exclusive",
    "pcm_sha256",
    "source_start_ms",
    "source_end_ms",
    "arrival_seconds",
    "scheduled_start_seconds",
    "scheduled_end_seconds",
    "playback_rate",
    "playback_mode",
}
PARENT_KEYS = {
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_count",
    "audio_bytes",
    "translated_sample_start",
    "translated_sample_end_exclusive",
    "source_start_ms",
    "source_end_ms",
    "completion_received_seconds",
}
PRIVACY_KEYS = {
    "contains_private_audio_in_companion_wavs",
    "ledger_contains_pcm",
    "contains_transcript_or_translation_text",
    "contains_file_path_or_uri",
    "contains_wall_clock_timestamp",
    "review_required_before_sharing",
}
MARKER_DOCUMENT_KEYS = {
    "schema_version",
    "schedule_ledger_sha256",
    "source_pcm_sha256",
    "source_pcm_sample_count",
    "translated_pcm_sha256",
    "translated_pcm_sample_count",
    "events",
}
MARKER_EVENT_KEYS = {
    "event_id",
    "source_sample_index",
    "translated_sample_index",
    "source_independent_reviewer_count",
    "translated_independent_reviewer_count",
}


class ScheduledSemanticDelayError(ValueError):
    """Raised when private semantic-delay evidence is invalid."""


@dataclass(frozen=True)
class PCMContract:
    sample_rate_hz: int
    channels: int
    bytes_per_sample: int
    sample_count: int
    pcm_sha256: str
    review_wav_sha256: str

    @property
    def frame_width(self) -> int:
        return self.channels * self.bytes_per_sample


@dataclass(frozen=True)
class CaptureContract:
    stream_generation: int
    input_sample_zero_seconds: float
    input_end_seconds: float
    source_chunk_count: int
    parent_count: int
    frame_count: int


@dataclass(frozen=True)
class FrameEvidence:
    source_index: int
    stream_generation: int
    parent_sequence_id: int
    audio_frame_id: int
    audio_bytes: int
    sample_count: int
    translated_sample_start: int
    translated_sample_end_exclusive: int
    pcm_sha256: str
    source_start_ms: float | None
    source_end_ms: float | None
    arrival_seconds: float
    scheduled_start_seconds: float
    scheduled_end_seconds: float
    playback_rate: float
    playback_mode: str

    @property
    def identity(self) -> tuple[int, int, int]:
        return (
            self.stream_generation,
            self.parent_sequence_id,
            self.audio_frame_id,
        )


@dataclass(frozen=True)
class ParentEvidence:
    parent_sequence_id: int
    audio_frame_count: int
    audio_bytes: int
    translated_sample_start: int
    translated_sample_end_exclusive: int
    source_start_ms: float | None
    source_end_ms: float | None
    completion_received_seconds: float


@dataclass(frozen=True)
class ReviewedEvent:
    event_id: str
    source_sample_index: int
    translated_sample_index: int
    source_independent_reviewer_count: int
    translated_independent_reviewer_count: int


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _exact_mapping(
    value: object,
    *,
    expected_keys: set[str],
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScheduledSemanticDelayError(f"{context} must be an object")
    actual = set(value)
    if actual != expected_keys:
        missing = sorted(expected_keys - actual)
        unknown = sorted(actual - expected_keys)
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {len(missing)}")
        if unknown:
            details.append(f"unexpected fields: {len(unknown)}")
        raise ScheduledSemanticDelayError(
            f"{context} must use the exact schema ({'; '.join(details)})"
        )
    return value


def _plain_int(
    value: object,
    context: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int or value < minimum:
        raise ScheduledSemanticDelayError(
            f"{context} must be an integer greater than or equal to {minimum}"
        )
    return value


def _finite_float(
    value: object,
    context: str,
    *,
    minimum: float | None = None,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ScheduledSemanticDelayError(f"{context} must be finite")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ScheduledSemanticDelayError(
            f"{context} must be finite"
        ) from exc
    if not math.isfinite(normalized):
        raise ScheduledSemanticDelayError(f"{context} must be finite")
    if minimum is not None and normalized < minimum:
        raise ScheduledSemanticDelayError(
            f"{context} must be greater than or equal to {minimum}"
        )
    return normalized


def _literal(value: object, expected: object, context: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ScheduledSemanticDelayError(
            f"{context} must equal {expected!r}"
        )


def _sha256_field(value: object, context: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ScheduledSemanticDelayError(
            f"{context} must be 64 lowercase hexadecimal characters"
        )
    return value


def _optional_source_range(
    start: object,
    end: object,
    context: str,
) -> tuple[float | None, float | None]:
    normalized_start = (
        None
        if start is None
        else _finite_float(
            start,
            f"{context}.source_start_ms",
            minimum=0.0,
        )
    )
    normalized_end = (
        None
        if end is None
        else _finite_float(
            end,
            f"{context}.source_end_ms",
            minimum=0.0,
        )
    )
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_end < normalized_start
    ):
        raise ScheduledSemanticDelayError(
            f"{context} source range end precedes its start"
        )
    return (normalized_start, normalized_end)


def _require_close(
    actual: float,
    expected: float,
    context: str,
    *,
    tolerance: float = FLOAT_RECONCILIATION_TOLERANCE,
) -> None:
    if not math.isclose(
        actual,
        expected,
        rel_tol=0.0,
        abs_tol=tolerance,
    ):
        raise ScheduledSemanticDelayError(f"{context} is inconsistent")


def _read_regular_file(path: Path | str, *, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(Path(path), flags)
    except OSError as exc:
        raise ScheduledSemanticDelayError(
            "evidence file is missing or unsafe"
        ) from exc
    try:
        file_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or file_status.st_size < 0
            or file_status.st_size > maximum_bytes
        ):
            raise ScheduledSemanticDelayError(
                "evidence file type or size is outside the allowed bound"
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
            raise ScheduledSemanticDelayError(
                "evidence file exceeds the allowed size"
            )
        return payload
    finally:
        os.close(descriptor)


def _load_strict_json(
    payload: bytes,
    *,
    context: str,
) -> dict[str, Any]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ScheduledSemanticDelayError(
                    f"{context} contains a duplicate object key"
                )
            result[key] = value
        return result

    def reject_nonstandard_number(value: str) -> None:
        raise ScheduledSemanticDelayError(
            f"{context} contains a non-standard number"
        )

    try:
        text = payload.decode("utf-8-sig")
        document = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonstandard_number,
        )
    except ScheduledSemanticDelayError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ScheduledSemanticDelayError(
            f"{context} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(document, dict):
        raise ScheduledSemanticDelayError(f"{context} must be a JSON object")
    return document


def _load_pcm_contract(value: object, context: str) -> PCMContract:
    item = _exact_mapping(
        value,
        expected_keys=PCM_KEYS,
        context=context,
    )
    sample_rate_hz = _plain_int(
        item["sample_rate_hz"],
        f"{context}.sample_rate_hz",
        minimum=1,
    )
    channels = _plain_int(
        item["channels"],
        f"{context}.channels",
        minimum=1,
    )
    bytes_per_sample = _plain_int(
        item["bytes_per_sample"],
        f"{context}.bytes_per_sample",
        minimum=1,
    )
    if channels != CHANNELS or bytes_per_sample != BYTES_PER_SAMPLE:
        raise ScheduledSemanticDelayError(
            f"{context} must be mono 16-bit PCM"
        )
    sample_count = _plain_int(
        item["sample_count"],
        f"{context}.sample_count",
        minimum=1,
    )
    audio_bytes = _plain_int(
        item["audio_bytes"],
        f"{context}.audio_bytes",
        minimum=1,
    )
    if audio_bytes != sample_count * channels * bytes_per_sample:
        raise ScheduledSemanticDelayError(
            f"{context}.audio_bytes does not match sample_count"
        )
    return PCMContract(
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        bytes_per_sample=bytes_per_sample,
        sample_count=sample_count,
        pcm_sha256=_sha256_field(
            item["pcm_sha256"],
            f"{context}.pcm_sha256",
        ),
        review_wav_sha256=_sha256_field(
            item["review_wav_sha256"],
            f"{context}.review_wav_sha256",
        ),
    )


def _validate_policy(value: object) -> None:
    item = _exact_mapping(
        value,
        expected_keys=POLICY_KEYS,
        context="schedule ledger policy",
    )
    expected = asdict(DEFAULT_PLAYBACK_POLICY)
    for key, expected_value in expected.items():
        actual = _finite_float(
            item[key],
            f"schedule ledger policy.{key}",
            minimum=0.0,
        )
        if actual != expected_value:
            raise ScheduledSemanticDelayError(
                f"schedule ledger policy.{key} is inconsistent"
            )


def _validate_privacy(value: object) -> None:
    item = _exact_mapping(
        value,
        expected_keys=PRIVACY_KEYS,
        context="schedule ledger privacy",
    )
    _literal(
        item["contains_private_audio_in_companion_wavs"],
        True,
        "privacy.contains_private_audio_in_companion_wavs",
    )
    _literal(
        item["ledger_contains_pcm"],
        False,
        "privacy.ledger_contains_pcm",
    )
    _literal(
        item["review_required_before_sharing"],
        True,
        "privacy.review_required_before_sharing",
    )
    for key in (
        "contains_transcript_or_translation_text",
        "contains_file_path_or_uri",
        "contains_wall_clock_timestamp",
    ):
        _literal(item[key], False, f"privacy.{key}")


def _load_capture(value: object) -> CaptureContract:
    item = _exact_mapping(
        value,
        expected_keys=CAPTURE_KEYS,
        context="schedule ledger capture",
    )
    _literal(
        item["audio_metadata_protocol_version"],
        1,
        "capture.audio_metadata_protocol_version",
    )
    _literal(
        item["terminal_completed"],
        True,
        "capture.terminal_completed",
    )
    _literal(
        item["input_pacing_mode"],
        INPUT_PACING_MODE,
        "capture.input_pacing_mode",
    )
    _literal(item["clock"], CAPTURE_CLOCK, "capture.clock")
    _literal(
        item["canonical_replay_verified"],
        True,
        "capture.canonical_replay_verified",
    )
    sample_zero = _finite_float(
        item["input_sample_zero_seconds"],
        "capture.input_sample_zero_seconds",
        minimum=0.0,
    )
    input_end = _finite_float(
        item["input_end_seconds"],
        "capture.input_end_seconds",
        minimum=0.0,
    )
    if input_end < sample_zero:
        raise ScheduledSemanticDelayError(
            "capture input end precedes source sample zero"
        )
    return CaptureContract(
        stream_generation=_plain_int(
            item["stream_generation"],
            "capture.stream_generation",
            minimum=1,
        ),
        input_sample_zero_seconds=sample_zero,
        input_end_seconds=input_end,
        source_chunk_count=_plain_int(
            item["source_chunk_count"],
            "capture.source_chunk_count",
            minimum=1,
        ),
        parent_count=_plain_int(
            item["parent_count"],
            "capture.parent_count",
            minimum=1,
        ),
        frame_count=_plain_int(
            item["frame_count"],
            "capture.frame_count",
            minimum=1,
        ),
    )


def _validate_source_chunks(
    value: object,
    *,
    source: PCMContract,
    capture: CaptureContract,
) -> None:
    if not isinstance(value, list) or not value:
        raise ScheduledSemanticDelayError(
            "schedule ledger source_chunks must be a non-empty array"
        )
    if len(value) != capture.source_chunk_count:
        raise ScheduledSemanticDelayError(
            "capture.source_chunk_count does not match the source ledger"
        )
    next_sample = 0
    previous_emitted = -math.inf
    nominal_chunk_duration: float | None = None
    nominal_chunk_samples: int | None = None
    previous_chunk_samples: int | None = None
    for index, raw in enumerate(value):
        context = f"schedule ledger source_chunks[{index}]"
        item = _exact_mapping(
            raw,
            expected_keys=SOURCE_CHUNK_KEYS,
            context=context,
        )
        chunk_index = _plain_int(
            item["chunk_index"],
            f"{context}.chunk_index",
        )
        if chunk_index != index:
            raise ScheduledSemanticDelayError(
                "source chunk indices must be contiguous and zero-based"
            )
        start = _plain_int(
            item["sample_start"],
            f"{context}.sample_start",
        )
        end = _plain_int(
            item["sample_end_exclusive"],
            f"{context}.sample_end_exclusive",
            minimum=1,
        )
        if start != next_sample or end <= start:
            raise ScheduledSemanticDelayError(
                "source chunk sample ranges must be contiguous and non-empty"
            )
        audio_bytes = _plain_int(
            item["audio_bytes"],
            f"{context}.audio_bytes",
            minimum=1,
        )
        expected_bytes = (end - start) * source.frame_width
        if audio_bytes != expected_bytes:
            raise ScheduledSemanticDelayError(
                f"{context}.audio_bytes does not match its sample range"
            )
        deadline = _finite_float(
            item["deadline_seconds"],
            f"{context}.deadline_seconds",
            minimum=0.0,
        )
        emitted = _finite_float(
            item["emitted_seconds"],
            f"{context}.emitted_seconds",
            minimum=0.0,
        )
        if nominal_chunk_duration is None:
            nominal_chunk_duration = (
                deadline - capture.input_sample_zero_seconds
            )
            if nominal_chunk_duration <= 0.0:
                raise ScheduledSemanticDelayError(
                    "first source deadline must follow sample zero"
                )
            nominal_samples_float = (
                nominal_chunk_duration * source.sample_rate_hz
            )
            if not math.isfinite(nominal_samples_float):
                raise ScheduledSemanticDelayError(
                    "source chunk cadence must be finite"
                )
            nominal_chunk_samples = round(nominal_samples_float)
            if (
                nominal_chunk_samples <= 0
                or not math.isclose(
                    nominal_samples_float,
                    nominal_chunk_samples,
                    rel_tol=0.0,
                    abs_tol=SOURCE_DEADLINE_TOLERANCE_SECONDS,
                )
            ):
                raise ScheduledSemanticDelayError(
                    "source chunk cadence must be sample-aligned"
                )
        assert nominal_chunk_duration is not None
        assert nominal_chunk_samples is not None
        if (
            previous_chunk_samples is not None
            and previous_chunk_samples != nominal_chunk_samples
        ):
            raise ScheduledSemanticDelayError(
                "only the final source chunk may be partial"
            )
        current_chunk_samples = end - start
        if current_chunk_samples > nominal_chunk_samples:
            raise ScheduledSemanticDelayError(
                f"{context} exceeds the registered pacing interval"
            )
        expected_deadline = (
            capture.input_sample_zero_seconds
            + (index + 1) * nominal_chunk_duration
        )
        _require_close(
            deadline,
            expected_deadline,
            f"{context}.deadline_seconds",
            tolerance=SOURCE_DEADLINE_TOLERANCE_SECONDS,
        )
        if emitted + EARLY_EMISSION_TOLERANCE_SECONDS < deadline:
            raise ScheduledSemanticDelayError(
                f"{context} was emitted before its source-end deadline"
            )
        if emitted < previous_emitted:
            raise ScheduledSemanticDelayError(
                "source chunk emissions must be non-decreasing"
            )
        previous_emitted = emitted
        previous_chunk_samples = current_chunk_samples
        next_sample = end
    if next_sample != source.sample_count:
        raise ScheduledSemanticDelayError(
            "source chunk ledger does not cover the complete source PCM"
        )
    if (
        capture.input_end_seconds + FLOAT_RECONCILIATION_TOLERANCE
        < previous_emitted
    ):
        raise ScheduledSemanticDelayError(
            "capture input end precedes the completed source transmission"
        )


def _load_frames(
    value: object,
    *,
    capture: CaptureContract,
    translated: PCMContract,
    translated_pcm: bytes,
) -> tuple[FrameEvidence, ...]:
    if not isinstance(value, list) or not value:
        raise ScheduledSemanticDelayError(
            "schedule ledger frames must be a non-empty array"
        )
    if len(value) != capture.frame_count:
        raise ScheduledSemanticDelayError(
            "capture.frame_count does not match the frame ledger"
        )
    frames: list[FrameEvidence] = []
    next_translated_sample = 0
    previous_arrival = -math.inf
    previous_scheduled_end = -math.inf
    identities: set[tuple[int, int, int]] = set()
    for index, raw in enumerate(value):
        context = f"schedule ledger frames[{index}]"
        item = _exact_mapping(
            raw,
            expected_keys=FRAME_KEYS,
            context=context,
        )
        source_index = _plain_int(
            item["source_index"],
            f"{context}.source_index",
        )
        if source_index != index:
            raise ScheduledSemanticDelayError(
                "translated frame source indices must be contiguous and "
                "zero-based"
            )
        generation = _plain_int(
            item["stream_generation"],
            f"{context}.stream_generation",
            minimum=1,
        )
        if generation != capture.stream_generation:
            raise ScheduledSemanticDelayError(
                f"{context}.stream_generation changed within the capture"
            )
        parent_id = _plain_int(
            item["parent_sequence_id"],
            f"{context}.parent_sequence_id",
        )
        frame_id = _plain_int(
            item["audio_frame_id"],
            f"{context}.audio_frame_id",
        )
        sample_count = _plain_int(
            item["sample_count"],
            f"{context}.sample_count",
            minimum=1,
        )
        sample_start = _plain_int(
            item["translated_sample_start"],
            f"{context}.translated_sample_start",
        )
        sample_end = _plain_int(
            item["translated_sample_end_exclusive"],
            f"{context}.translated_sample_end_exclusive",
            minimum=1,
        )
        if (
            sample_start != next_translated_sample
            or sample_end - sample_start != sample_count
        ):
            raise ScheduledSemanticDelayError(
                "translated frame sample ranges must be contiguous and exact"
            )
        audio_bytes = _plain_int(
            item["audio_bytes"],
            f"{context}.audio_bytes",
            minimum=1,
        )
        if audio_bytes != sample_count * translated.frame_width:
            raise ScheduledSemanticDelayError(
                f"{context}.audio_bytes does not match sample_count"
            )
        pcm_sha256 = _sha256_field(
            item["pcm_sha256"],
            f"{context}.pcm_sha256",
        )
        byte_start = sample_start * translated.frame_width
        byte_end = sample_end * translated.frame_width
        if _sha256(translated_pcm[byte_start:byte_end]) != pcm_sha256:
            raise ScheduledSemanticDelayError(
                f"{context}.pcm_sha256 does not match translated PCM"
            )
        source_start, source_end = _optional_source_range(
            item["source_start_ms"],
            item["source_end_ms"],
            context,
        )
        arrival = _finite_float(
            item["arrival_seconds"],
            f"{context}.arrival_seconds",
            minimum=0.0,
        )
        if arrival < previous_arrival:
            raise ScheduledSemanticDelayError(
                "translated frame arrivals must be non-decreasing"
            )
        scheduled_start = _finite_float(
            item["scheduled_start_seconds"],
            f"{context}.scheduled_start_seconds",
            minimum=0.0,
        )
        scheduled_end = _finite_float(
            item["scheduled_end_seconds"],
            f"{context}.scheduled_end_seconds",
            minimum=0.0,
        )
        if scheduled_end <= scheduled_start:
            raise ScheduledSemanticDelayError(
                f"{context} scheduled end must follow scheduled start"
            )
        playback_rate = _finite_float(
            item["playback_rate"],
            f"{context}.playback_rate",
            minimum=sys.float_info.min,
        )
        expected_scheduled_end = (
            scheduled_start
            + sample_count
            / (translated.sample_rate_hz * playback_rate)
        )
        _require_close(
            scheduled_end,
            expected_scheduled_end,
            f"{context}.scheduled_end_seconds",
        )
        if (
            scheduled_start + FLOAT_RECONCILIATION_TOLERANCE < arrival
            or scheduled_start + FLOAT_RECONCILIATION_TOLERANCE
            < previous_scheduled_end
        ):
            raise ScheduledSemanticDelayError(
                f"{context} scheduled playback order is inconsistent"
            )
        playback_mode = item["playback_mode"]
        if (
            not isinstance(playback_mode, str)
            or playback_mode not in PLAYBACK_MODES
        ):
            raise ScheduledSemanticDelayError(
                f"{context}.playback_mode is invalid"
            )
        frame = FrameEvidence(
            source_index=source_index,
            stream_generation=generation,
            parent_sequence_id=parent_id,
            audio_frame_id=frame_id,
            audio_bytes=audio_bytes,
            sample_count=sample_count,
            translated_sample_start=sample_start,
            translated_sample_end_exclusive=sample_end,
            pcm_sha256=pcm_sha256,
            source_start_ms=source_start,
            source_end_ms=source_end,
            arrival_seconds=arrival,
            scheduled_start_seconds=scheduled_start,
            scheduled_end_seconds=scheduled_end,
            playback_rate=playback_rate,
            playback_mode=playback_mode,
        )
        if frame.identity in identities:
            raise ScheduledSemanticDelayError(
                "translated frame identities must be unique"
            )
        identities.add(frame.identity)
        frames.append(frame)
        previous_arrival = arrival
        previous_scheduled_end = scheduled_end
        next_translated_sample = sample_end
    if next_translated_sample != translated.sample_count:
        raise ScheduledSemanticDelayError(
            "frame ledger does not cover the complete translated PCM"
        )
    return tuple(frames)


def _load_parents(
    value: object,
    *,
    capture: CaptureContract,
    frames: Sequence[FrameEvidence],
) -> tuple[ParentEvidence, ...]:
    if not isinstance(value, list) or not value:
        raise ScheduledSemanticDelayError(
            "schedule ledger parents must be a non-empty array"
        )
    if len(value) != capture.parent_count:
        raise ScheduledSemanticDelayError(
            "capture.parent_count does not match the parent ledger"
        )
    parents: list[ParentEvidence] = []
    next_translated_sample = 0
    frame_cursor = 0
    previous_completion = -math.inf
    for index, raw in enumerate(value):
        context = f"schedule ledger parents[{index}]"
        item = _exact_mapping(
            raw,
            expected_keys=PARENT_KEYS,
            context=context,
        )
        generation = _plain_int(
            item["stream_generation"],
            f"{context}.stream_generation",
            minimum=1,
        )
        if generation != capture.stream_generation:
            raise ScheduledSemanticDelayError(
                f"{context}.stream_generation changed within the capture"
            )
        parent_id = _plain_int(
            item["parent_sequence_id"],
            f"{context}.parent_sequence_id",
        )
        if parent_id != index:
            raise ScheduledSemanticDelayError(
                "parent sequence IDs must be contiguous and zero-based"
            )
        frame_count = _plain_int(
            item["audio_frame_count"],
            f"{context}.audio_frame_count",
            minimum=1,
        )
        if frame_cursor + frame_count > len(frames):
            raise ScheduledSemanticDelayError(
                f"{context} references unavailable frames"
            )
        parent_frames = frames[frame_cursor : frame_cursor + frame_count]
        if any(
            frame.parent_sequence_id != parent_id
            or frame.audio_frame_id != frame_index
            for frame_index, frame in enumerate(parent_frames)
        ):
            raise ScheduledSemanticDelayError(
                f"{context} frame identities do not reconcile"
            )
        sample_start = _plain_int(
            item["translated_sample_start"],
            f"{context}.translated_sample_start",
        )
        sample_end = _plain_int(
            item["translated_sample_end_exclusive"],
            f"{context}.translated_sample_end_exclusive",
            minimum=1,
        )
        if (
            sample_start != next_translated_sample
            or sample_start != parent_frames[0].translated_sample_start
            or sample_end != parent_frames[-1].translated_sample_end_exclusive
        ):
            raise ScheduledSemanticDelayError(
                f"{context} translated sample range does not reconcile"
            )
        audio_bytes = _plain_int(
            item["audio_bytes"],
            f"{context}.audio_bytes",
            minimum=1,
        )
        if audio_bytes != sum(frame.audio_bytes for frame in parent_frames):
            raise ScheduledSemanticDelayError(
                f"{context}.audio_bytes does not reconcile"
            )
        source_start, source_end = _optional_source_range(
            item["source_start_ms"],
            item["source_end_ms"],
            context,
        )
        if any(
            (frame.source_start_ms, frame.source_end_ms)
            != (source_start, source_end)
            for frame in parent_frames
        ):
            raise ScheduledSemanticDelayError(
                f"{context} source range does not reconcile with its frames"
            )
        completion = _finite_float(
            item["completion_received_seconds"],
            f"{context}.completion_received_seconds",
            minimum=0.0,
        )
        if (
            completion + FLOAT_RECONCILIATION_TOLERANCE
            < parent_frames[-1].arrival_seconds
            or completion < previous_completion
        ):
            raise ScheduledSemanticDelayError(
                "parent completions must follow their frames in order"
            )
        parents.append(
            ParentEvidence(
                parent_sequence_id=parent_id,
                audio_frame_count=frame_count,
                audio_bytes=audio_bytes,
                translated_sample_start=sample_start,
                translated_sample_end_exclusive=sample_end,
                source_start_ms=source_start,
                source_end_ms=source_end,
                completion_received_seconds=completion,
            )
        )
        frame_cursor += frame_count
        next_translated_sample = sample_end
        previous_completion = completion
    if frame_cursor != len(frames):
        raise ScheduledSemanticDelayError(
            "parent ledger does not reconcile every translated frame"
        )
    return tuple(parents)


def _replay_and_validate_schedule(
    frames: Sequence[FrameEvidence],
    *,
    input_end_seconds: float,
    translated: PCMContract,
) -> tuple[ScheduledChunk, ...]:
    try:
        replay = simulate_playback(
            tuple(
                AudioChunk(
                    arrival_seconds=frame.arrival_seconds,
                    duration_seconds=(
                        frame.sample_count / translated.sample_rate_hz
                    ),
                    audio_bytes=frame.audio_bytes,
                    source_index=index,
                )
                for index, frame in enumerate(frames)
            ),
            input_end_seconds=input_end_seconds,
            adaptive=True,
            policy=DEFAULT_PLAYBACK_POLICY,
        )
    except (RuntimeError, ValueError) as exc:
        raise ScheduledSemanticDelayError(
            "canonical adaptive schedule replay rejected the frame ledger"
        ) from exc
    if len(replay.schedule) != len(frames):
        raise ScheduledSemanticDelayError(
            "canonical schedule replay omitted translated frames"
        )
    for index, (frame, scheduled) in enumerate(
        zip(frames, replay.schedule)
    ):
        context = f"schedule ledger frames[{index}]"
        if scheduled.source_index != index:
            raise ScheduledSemanticDelayError(
                "canonical schedule replay reordered translated frames"
            )
        _require_close(
            frame.arrival_seconds,
            scheduled.arrival_seconds,
            f"{context}.arrival_seconds canonical replay",
        )
        _require_close(
            frame.scheduled_start_seconds,
            scheduled.start_seconds,
            f"{context}.scheduled_start_seconds canonical replay",
        )
        _require_close(
            frame.scheduled_end_seconds,
            scheduled.end_seconds,
            f"{context}.scheduled_end_seconds canonical replay",
        )
        _require_close(
            frame.playback_rate,
            scheduled.playback_rate,
            f"{context}.playback_rate canonical replay",
        )
        if frame.playback_mode != scheduled.playback_mode:
            raise ScheduledSemanticDelayError(
                f"{context}.playback_mode differs from canonical replay"
            )
    return replay.schedule


def _validate_wav(
    path: Path | str,
    *,
    contract: PCMContract,
    context: str,
) -> tuple[bytes, str]:
    wav_bytes = _read_regular_file(path, maximum_bytes=MAX_WAV_BYTES)
    wav_sha256 = _sha256(wav_bytes)
    if wav_sha256 != contract.review_wav_sha256:
        raise ScheduledSemanticDelayError(
            f"{context} WAV hash does not match the schedule ledger"
        )
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            sample_count = audio.getnframes()
            compression = audio.getcomptype()
            pcm = audio.readframes(sample_count)
            if audio.readframes(1):
                raise ScheduledSemanticDelayError(
                    f"{context} WAV contains uncounted PCM frames"
                )
    except ScheduledSemanticDelayError:
        raise
    except (EOFError, wave.Error) as exc:
        raise ScheduledSemanticDelayError(
            f"{context} is not a valid PCM WAV"
        ) from exc
    if (
        channels != contract.channels
        or sample_width != contract.bytes_per_sample
        or sample_rate != contract.sample_rate_hz
        or sample_count != contract.sample_count
        or compression != "NONE"
        or len(pcm) != contract.sample_count * contract.frame_width
    ):
        raise ScheduledSemanticDelayError(
            f"{context} WAV does not match its PCM contract"
        )
    if _sha256(pcm) != contract.pcm_sha256:
        raise ScheduledSemanticDelayError(
            f"{context} PCM hash does not match the schedule ledger"
        )
    return pcm, wav_sha256


def _load_reviewed_events(
    marker_bytes: bytes,
    *,
    ledger_sha256: str,
    source: PCMContract,
    translated: PCMContract,
    minimum_reviewers: int,
) -> tuple[ReviewedEvent, ...]:
    document = _load_strict_json(
        marker_bytes,
        context="reviewer marker sidecar",
    )
    item = _exact_mapping(
        document,
        expected_keys=MARKER_DOCUMENT_KEYS,
        context="reviewer marker sidecar",
    )
    _literal(
        item["schema_version"],
        MARKER_SCHEMA_VERSION,
        "reviewer marker sidecar.schema_version",
    )
    if (
        _sha256_field(
            item["schedule_ledger_sha256"],
            "reviewer marker sidecar.schedule_ledger_sha256",
        )
        != ledger_sha256
    ):
        raise ScheduledSemanticDelayError(
            "reviewer marker sidecar does not bind the schedule ledger"
        )
    bindings = (
        (
            "source_pcm_sha256",
            source.pcm_sha256,
            _sha256_field,
        ),
        (
            "translated_pcm_sha256",
            translated.pcm_sha256,
            _sha256_field,
        ),
    )
    for key, expected, validator in bindings:
        actual = validator(
            item[key],
            f"reviewer marker sidecar.{key}",
        )
        if actual != expected:
            raise ScheduledSemanticDelayError(
                f"reviewer marker sidecar {key} does not match the ledger"
            )
    sample_bindings = (
        ("source_pcm_sample_count", source.sample_count),
        ("translated_pcm_sample_count", translated.sample_count),
    )
    for key, expected in sample_bindings:
        actual = _plain_int(
            item[key],
            f"reviewer marker sidecar.{key}",
            minimum=1,
        )
        if actual != expected:
            raise ScheduledSemanticDelayError(
                f"reviewer marker sidecar {key} does not match the ledger"
            )
    raw_events = item["events"]
    if not isinstance(raw_events, list) or not raw_events:
        raise ScheduledSemanticDelayError(
            "reviewer marker sidecar events must be a non-empty array"
        )
    events: list[ReviewedEvent] = []
    event_ids: set[str] = set()
    source_samples: set[int] = set()
    translated_samples: set[int] = set()
    for index, raw in enumerate(raw_events):
        context = f"reviewer marker sidecar events[{index}]"
        event = _exact_mapping(
            raw,
            expected_keys=MARKER_EVENT_KEYS,
            context=context,
        )
        event_id = event["event_id"]
        if (
            not isinstance(event_id, str)
            or EVENT_ID_PATTERN.fullmatch(event_id) is None
        ):
            raise ScheduledSemanticDelayError(
                f"{context}.event_id must match event-NNN"
            )
        if event_id in event_ids:
            raise ScheduledSemanticDelayError(
                "reviewer marker event IDs must be unique"
            )
        event_ids.add(event_id)
        source_sample = _plain_int(
            event["source_sample_index"],
            f"{context}.source_sample_index",
        )
        translated_sample = _plain_int(
            event["translated_sample_index"],
            f"{context}.translated_sample_index",
        )
        if source_sample >= source.sample_count:
            raise ScheduledSemanticDelayError(
                f"{event_id} source sample is outside the source PCM"
            )
        if translated_sample >= translated.sample_count:
            raise ScheduledSemanticDelayError(
                f"{event_id} translated sample is outside translated PCM"
            )
        if source_sample in source_samples:
            raise ScheduledSemanticDelayError(
                "reviewer marker source samples must be unique"
            )
        if translated_sample in translated_samples:
            raise ScheduledSemanticDelayError(
                "reviewer marker translated samples must be unique"
            )
        source_samples.add(source_sample)
        translated_samples.add(translated_sample)
        source_reviewer_count = _plain_int(
            event["source_independent_reviewer_count"],
            f"{context}.source_independent_reviewer_count",
            minimum=1,
        )
        translated_reviewer_count = _plain_int(
            event["translated_independent_reviewer_count"],
            f"{context}.translated_independent_reviewer_count",
            minimum=1,
        )
        if (
            source_reviewer_count < minimum_reviewers
            or translated_reviewer_count < minimum_reviewers
        ):
            raise ScheduledSemanticDelayError(
                f"{event_id} has fewer than {minimum_reviewers} "
                "independent reviewers on one or both landmarks"
            )
        events.append(
            ReviewedEvent(
                event_id=event_id,
                source_sample_index=source_sample,
                translated_sample_index=translated_sample,
                source_independent_reviewer_count=source_reviewer_count,
                translated_independent_reviewer_count=(
                    translated_reviewer_count
                ),
            )
        )
    events.sort(
        key=lambda event: (
            event.source_sample_index,
            event.translated_sample_index,
            event.event_id,
        )
    )
    return tuple(events)


def _frame_for_translated_sample(
    frames: Sequence[FrameEvidence],
    sample_index: int,
) -> tuple[int, FrameEvidence]:
    for index, frame in enumerate(frames):
        if (
            frame.translated_sample_start
            <= sample_index
            < frame.translated_sample_end_exclusive
        ):
            return index, frame
    raise ScheduledSemanticDelayError(
        "translated reviewer sample cannot be mapped to a frame"
    )


def _source_attribution_for_event(
    event: ReviewedEvent,
    frame: FrameEvidence,
    parents: Sequence[ParentEvidence],
    *,
    source_sample_rate_hz: int,
    source_sample_count: int,
) -> tuple[float, float, float]:
    """Resolve one marker only through its target frame's completed parent.

    ASR offsets represent closed attribution ranges: both the start and end
    offset are valid containment boundaries. The reviewed PCM sample itself
    remains a half-open one-sample interval in the delay calculation.
    """

    parent_id = frame.parent_sequence_id
    if (
        parent_id >= len(parents)
        or parents[parent_id].parent_sequence_id != parent_id
    ):
        raise ScheduledSemanticDelayError(
            f"{event.event_id} target frame has no completed parent"
        )
    parent = parents[parent_id]
    if parent.source_start_ms is None or parent.source_end_ms is None:
        raise ScheduledSemanticDelayError(
            f"{event.event_id} target parent lacks a complete attributed "
            "source range"
        )
    source_duration_ms = (
        source_sample_count / source_sample_rate_hz * 1000.0
    )
    one_source_sample_ms = 1000.0 / source_sample_rate_hz
    if parent.source_end_ms > source_duration_ms + one_source_sample_ms:
        raise ScheduledSemanticDelayError(
            f"{event.event_id} target parent's attributed source range "
            "exceeds the source PCM duration"
        )
    source_offset_ms = (
        event.source_sample_index / source_sample_rate_hz * 1000.0
    )
    if not (
        parent.source_start_ms
        <= source_offset_ms
        <= parent.source_end_ms
    ):
        raise ScheduledSemanticDelayError(
            f"{event.event_id} source marker is outside its target "
            "parent's attributed source range"
        )
    return (
        source_offset_ms,
        parent.source_start_ms,
        parent.source_end_ms,
    )


def analyze_scheduled_semantic_delay(
    schedule_ledger: Path | str,
    source_wav: Path | str,
    translated_wav: Path | str,
    markers_json: Path | str,
    max_latency_seconds: float,
    *,
    minimum_reviewers: int = DEFAULT_MINIMUM_REVIEWERS,
) -> dict[str, Any]:
    """Validate all evidence and compute conservative semantic-delay bounds."""

    normalized_sla = _finite_float(
        max_latency_seconds,
        "max_latency_seconds",
        minimum=sys.float_info.min,
    )
    normalized_minimum_reviewers = _plain_int(
        minimum_reviewers,
        "minimum_reviewers",
        minimum=DEFAULT_MINIMUM_REVIEWERS,
    )
    ledger_bytes = _read_regular_file(
        schedule_ledger,
        maximum_bytes=MAX_JSON_BYTES,
    )
    marker_bytes = _read_regular_file(
        markers_json,
        maximum_bytes=MAX_JSON_BYTES,
    )
    ledger_sha256 = _sha256(ledger_bytes)
    marker_sha256 = _sha256(marker_bytes)
    ledger_document = _load_strict_json(
        ledger_bytes,
        context="schedule ledger",
    )
    ledger = _exact_mapping(
        ledger_document,
        expected_keys=LEDGER_KEYS,
        context="schedule ledger",
    )
    _literal(
        ledger["schema_version"],
        LEDGER_SCHEMA_VERSION,
        "schedule ledger.schema_version",
    )
    _literal(
        ledger["report_type"],
        LEDGER_REPORT_TYPE,
        "schedule ledger.report_type",
    )
    capture = _load_capture(ledger["capture"])
    source_contract = _load_pcm_contract(
        ledger["source_pcm"],
        "schedule ledger source_pcm",
    )
    translated_contract = _load_pcm_contract(
        ledger["translated_pcm"],
        "schedule ledger translated_pcm",
    )
    _validate_policy(ledger["policy"])
    _validate_privacy(ledger["privacy"])
    source_pcm, source_wav_sha256 = _validate_wav(
        source_wav,
        contract=source_contract,
        context="source review",
    )
    translated_pcm, translated_wav_sha256 = _validate_wav(
        translated_wav,
        contract=translated_contract,
        context="translated review",
    )
    _validate_source_chunks(
        ledger["source_chunks"],
        source=source_contract,
        capture=capture,
    )
    frames = _load_frames(
        ledger["frames"],
        capture=capture,
        translated=translated_contract,
        translated_pcm=translated_pcm,
    )
    parents = _load_parents(
        ledger["parents"],
        capture=capture,
        frames=frames,
    )
    canonical_schedule = _replay_and_validate_schedule(
        frames,
        input_end_seconds=capture.input_end_seconds,
        translated=translated_contract,
    )
    events = _load_reviewed_events(
        marker_bytes,
        ledger_sha256=ledger_sha256,
        source=source_contract,
        translated=translated_contract,
        minimum_reviewers=normalized_minimum_reviewers,
    )

    del source_pcm
    sla_seconds = normalized_sla
    result_events: list[dict[str, Any]] = []
    for reviewed in events:
        frame_index, frame = _frame_for_translated_sample(
            frames,
            reviewed.translated_sample_index,
        )
        (
            source_offset_ms,
            attributed_source_start_ms,
            attributed_source_end_ms,
        ) = _source_attribution_for_event(
            reviewed,
            frame,
            parents,
            source_sample_rate_hz=source_contract.sample_rate_hz,
            source_sample_count=source_contract.sample_count,
        )
        scheduled = canonical_schedule[frame_index]
        frame_sample_offset = (
            reviewed.translated_sample_index
            - frame.translated_sample_start
        )
        source_interval_start = (
            capture.input_sample_zero_seconds
            + reviewed.source_sample_index / source_contract.sample_rate_hz
        )
        source_interval_end = (
            capture.input_sample_zero_seconds
            + (reviewed.source_sample_index + 1)
            / source_contract.sample_rate_hz
        )
        translated_interval_start = (
            scheduled.start_seconds
            + frame_sample_offset
            / (
                translated_contract.sample_rate_hz
                * scheduled.playback_rate
            )
        )
        translated_interval_end = (
            scheduled.start_seconds
            + (frame_sample_offset + 1)
            / (
                translated_contract.sample_rate_hz
                * scheduled.playback_rate
            )
        )
        for interval_name, interval_start, interval_end in (
            (
                "source sample interval",
                source_interval_start,
                source_interval_end,
            ),
            (
                "scheduled target sample interval",
                translated_interval_start,
                translated_interval_end,
            ),
        ):
            if (
                not math.isfinite(interval_start)
                or not math.isfinite(interval_end)
                or interval_end <= interval_start
            ):
                raise ScheduledSemanticDelayError(
                    f"{reviewed.event_id} {interval_name} is not a "
                    "representable non-empty interval"
                )
        lower_bound = translated_interval_start - source_interval_end
        upper_bound = translated_interval_end - source_interval_start
        if (
            not math.isfinite(lower_bound)
            or not math.isfinite(upper_bound)
            or upper_bound <= lower_bound
        ):
            raise ScheduledSemanticDelayError(
                f"{reviewed.event_id} semantic-delay bounds are not a "
                "representable non-empty interval"
            )
        if upper_bound <= 0.0:
            raise ScheduledSemanticDelayError(
                f"{reviewed.event_id} target interval precedes its source "
                "interval"
            )
        if upper_bound <= sla_seconds:
            status = "pass"
        elif lower_bound > sla_seconds:
            status = "fail"
        else:
            status = "inconclusive"
        result_events.append(
            {
                "event_id": reviewed.event_id,
                "source_sample_index": reviewed.source_sample_index,
                "translated_sample_index": (
                    reviewed.translated_sample_index
                ),
                "source_independent_reviewer_count": (
                    reviewed.source_independent_reviewer_count
                ),
                "translated_independent_reviewer_count": (
                    reviewed.translated_independent_reviewer_count
                ),
                "source_offset_ms": source_offset_ms,
                "attributed_source_range_ms": {
                    "start": attributed_source_start_ms,
                    "end": attributed_source_end_ms,
                    "endpoint_convention": "closed",
                },
                "mapped_frame": {
                    "stream_generation": frame.stream_generation,
                    "parent_sequence_id": frame.parent_sequence_id,
                    "audio_frame_id": frame.audio_frame_id,
                    "translated_frame_sample_offset": frame_sample_offset,
                },
                "source_sample_interval_seconds": {
                    "lower_inclusive": source_interval_start,
                    "upper_exclusive": source_interval_end,
                },
                "scheduled_target_sample_interval_seconds": {
                    "lower_inclusive": translated_interval_start,
                    "upper_exclusive": translated_interval_end,
                    "playback_rate": scheduled.playback_rate,
                    "playback_mode": scheduled.playback_mode,
                },
                "scheduled_semantic_delay_bounds_seconds": {
                    "lower": lower_bound,
                    "upper": upper_bound,
                    "bound_width": upper_bound - lower_bound,
                },
                "maximum_latency_seconds": sla_seconds,
                "status": status,
            }
        )

    status_counts = {
        status: sum(
            event["status"] == status for event in result_events
        )
        for status in ("pass", "inconclusive", "fail")
    }
    if status_counts["fail"]:
        overall_status = "fail"
    elif status_counts["inconclusive"]:
        overall_status = "inconclusive"
    else:
        overall_status = "pass"
    lower_bounds = [
        event["scheduled_semantic_delay_bounds_seconds"]["lower"]
        for event in result_events
    ]
    upper_bounds = [
        event["scheduled_semantic_delay_bounds_seconds"]["upper"]
        for event in result_events
    ]
    bound_distribution = {
        "lower_bound_seconds": {
            "minimum": min(lower_bounds),
            "median": statistics.median(lower_bounds),
            "maximum": max(lower_bounds),
        },
        "upper_bound_seconds": {
            "minimum": min(upper_bounds),
            "median": statistics.median(upper_bounds),
            "maximum": max(upper_bounds),
        },
    }
    result = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "analysis": ANALYSIS_TYPE,
        "evidence": {
            "schedule_ledger_sha256": ledger_sha256,
            "reviewer_markers_sha256": marker_sha256,
            "source_review_wav_sha256": source_wav_sha256,
            "translated_review_wav_sha256": translated_wav_sha256,
            "source_pcm_sha256": source_contract.pcm_sha256,
            "source_pcm_sample_count": source_contract.sample_count,
            "source_sample_rate_hz": source_contract.sample_rate_hz,
            "translated_pcm_sha256": translated_contract.pcm_sha256,
            "translated_pcm_sample_count": translated_contract.sample_count,
            "translated_sample_rate_hz": (
                translated_contract.sample_rate_hz
            ),
            "audio_metadata_protocol_version": 1,
            "stream_generation": capture.stream_generation,
            "parent_count": len(parents),
            "frame_count": len(frames),
            "minimum_independent_reviewers_per_landmark": (
                normalized_minimum_reviewers
            ),
            "canonical_adaptive_schedule_replay_verified": True,
            "registered_playback_policy": asdict(
                DEFAULT_PLAYBACK_POLICY
            ),
            "bound_method": (
                "source_and_scheduled_target_half_open_one_sample_intervals"
            ),
        },
        "claim_scope": {
            "reviewed_source_semantic_landmark": True,
            "reviewed_translated_semantic_landmark": True,
            "scheduled_digital_semantic_delay": True,
            "dac_or_acoustic_audibility_proven": False,
            "room_reaction_synchronization_proven": False,
            "translation_quality_proven": False,
        },
        "maximum_latency_seconds": sla_seconds,
        "events": result_events,
        "summary": {
            "event_count": len(result_events),
            "status_counts": status_counts,
            "overall_status": overall_status,
            "latency_bound_distribution_seconds": bound_distribution,
        },
        "privacy": {
            "contains_transcript_or_translation_text": False,
            "contains_file_path_or_uri": False,
            "contains_reviewer_identity": False,
            "contains_audio_payload": False,
            "contains_audio_hashes": True,
            "review_before_external_sharing": True,
        },
    }
    return result


def render_scheduled_semantic_delay_markdown(
    result: Mapping[str, Any],
) -> str:
    """Render a privacy-safe report without paths, text, or audio payloads."""

    summary = result["summary"]
    counts = summary["status_counts"]
    distribution = summary["latency_bound_distribution_seconds"]
    evidence = result["evidence"]
    lines = [
        "# Scheduled Semantic Delay",
        "",
        f"Overall result: **{summary['overall_status'].upper()}**",
        "",
        (
            f"Threshold: {result['maximum_latency_seconds']:.6f} seconds. "
            f"Events: {summary['event_count']} "
            f"({counts['pass']} PASS, {counts['inconclusive']} "
            f"INCONCLUSIVE, {counts['fail']} FAIL)."
        ),
        "",
        "## Evidence",
        "",
        (
            "- Canonical adaptive schedule replay verified: "
            f"{str(evidence['canonical_adaptive_schedule_replay_verified']).lower()}"
        ),
        (
            "- Minimum independent reviewers per landmark: "
            f"{evidence['minimum_independent_reviewers_per_landmark']}"
        ),
        f"- Schedule ledger SHA-256: `{evidence['schedule_ledger_sha256']}`",
        (
            "- Reviewer marker sidecar SHA-256: "
            f"`{evidence['reviewer_markers_sha256']}`"
        ),
        "",
        "## Events",
        "",
        (
            "| Event | Parent/frame | Source sample | Target sample | "
            "Lower bound (s) | Upper bound (s) | Result |"
        ),
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for event in result["events"]:
        frame = event["mapped_frame"]
        bounds = event["scheduled_semantic_delay_bounds_seconds"]
        lines.append(
            "| {event_id} | {parent}/{frame} | {source} | {target} | "
            "{lower:.6f} | {upper:.6f} | {status} |".format(
                event_id=event["event_id"],
                parent=frame["parent_sequence_id"],
                frame=frame["audio_frame_id"],
                source=event["source_sample_index"],
                target=event["translated_sample_index"],
                lower=bounds["lower"],
                upper=bounds["upper"],
                status=event["status"].upper(),
            )
        )
    lower = distribution["lower_bound_seconds"]
    upper = distribution["upper_bound_seconds"]
    lines.extend(
        [
            "",
            "## Bound distribution",
            "",
            "| Bound | Minimum (s) | Median (s) | Maximum (s) |",
            "| --- | ---: | ---: | ---: |",
            (
                f"| Lower | {lower['minimum']:.6f} | "
                f"{lower['median']:.6f} | {lower['maximum']:.6f} |"
            ),
            (
                f"| Upper | {upper['minimum']:.6f} | "
                f"{upper['median']:.6f} | {upper['maximum']:.6f} |"
            ),
            "",
            "## Claim boundary",
            "",
            (
                "These are reviewed source-to-scheduled-target digital "
                "bounds. They do not prove DAC output, physical audibility, "
                "room-reaction synchronization, or translation quality."
            ),
            (
                "ASR attributed source ranges use closed start/end "
                "containment. Source and target PCM samples use half-open "
                "one-sample timing intervals."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a private PCM schedule ledger and reviewed bilingual "
            "landmarks, then bound scheduled digital semantic delay."
        )
    )
    parser.add_argument(
        "--ledger",
        "--schedule-ledger",
        dest="schedule_ledger",
        type=Path,
        required=True,
    )
    parser.add_argument("--source-wav", type=Path, required=True)
    parser.add_argument("--translated-wav", type=Path, required=True)
    parser.add_argument(
        "--markers",
        "--markers-json",
        dest="markers_json",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--max-latency-seconds",
        type=float,
        required=True,
    )
    parser.add_argument(
        "--minimum-reviewers",
        type=int,
        default=DEFAULT_MINIMUM_REVIEWERS,
    )
    parser.add_argument(
        "--json-output",
        "--output-json",
        dest="json_output",
        type=Path,
    )
    parser.add_argument(
        "--markdown-output",
        "--output-markdown",
        dest="markdown_output",
        type=Path,
    )
    return parser


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    if (
        not math.isfinite(args.max_latency_seconds)
        or args.max_latency_seconds <= 0
    ):
        parser.error("--max-latency-seconds must be finite and positive")
    if args.minimum_reviewers < DEFAULT_MINIMUM_REVIEWERS:
        parser.error("--minimum-reviewers must be at least 2")
    outputs = [
        output
        for output in (args.json_output, args.markdown_output)
        if output is not None
    ]
    try:
        input_paths = {
            args.schedule_ledger.resolve(),
            args.source_wav.resolve(),
            args.translated_wav.resolve(),
            args.markers_json.resolve(),
        }
        resolved_outputs = {output.resolve() for output in outputs}
    except (OSError, RuntimeError):
        parser.error("evidence and report paths must resolve safely")
    if len(resolved_outputs) != len(outputs):
        parser.error("JSON and Markdown output paths must be different")
    if resolved_outputs & input_paths:
        parser.error("report output must not overwrite input evidence")
    return args


def _write_text_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="",
        ) as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, result: Mapping[str, Any]) -> None:
    _write_text_atomic(
        path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_cli_args(argv)
    try:
        result = analyze_scheduled_semantic_delay(
            args.schedule_ledger,
            args.source_wav,
            args.translated_wav,
            args.markers_json,
            args.max_latency_seconds,
            minimum_reviewers=args.minimum_reviewers,
        )
    except ScheduledSemanticDelayError as exc:
        print(f"Invalid semantic-delay evidence: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "Invalid semantic-delay evidence: evidence could not be read.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.json_output is not None:
            _write_json_atomic(args.json_output, result)
        if args.markdown_output is not None:
            _write_text_atomic(
                args.markdown_output,
                render_scheduled_semantic_delay_markdown(result),
            )
    except OSError:
        print("Could not write semantic-delay report.", file=sys.stderr)
        return 2
    if args.json_output is None and args.markdown_output is None:
        print(json.dumps(result, indent=2, sort_keys=True))
    status = result["summary"]["overall_status"]
    return GATE_EXIT_CODES[status]


if __name__ == "__main__":
    raise SystemExit(main())
