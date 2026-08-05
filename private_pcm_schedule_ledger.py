#!/usr/bin/env python3
"""Private PCM and deterministic schedule evidence for semantic review.

This module is deliberately not wired into the batch runner by default.  A
caller must explicitly create a :class:`PrivatePcmScheduleCapture`, feed it
only protocol-validated PCM and the corresponding headless scheduling
decision, and install the resulting artifacts in a new private directory.

The JSON ledger contains numeric timing/identity evidence and hashes, never
PCM, text, paths, URIs, session identifiers, or wall-clock timestamps.  The
two companion WAV files contain private source and translated speech and must
remain ignored and access-restricted.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from headless_playback_scheduler import (
    HeadlessScheduledFrame,
    validate_headless_playback_report,
)
from playback_simulation import (
    DEFAULT_PLAYBACK_POLICY,
    AudioChunk,
    simulate_playback,
)


LEDGER_SCHEMA_VERSION = 1
LEDGER_REPORT_TYPE = "private_pcm_schedule_ledger"
AUDIO_METADATA_PROTOCOL_VERSION = 1
INPUT_PACING_MODE = "chunk_end_boundary_v1"
CAPTURE_CLOCK = "client_monotonic_from_capture_start"

SOURCE_WAV_FILENAME = "source-review.wav"
TRANSLATED_WAV_FILENAME = "translated-review.wav"
LEDGER_FILENAME = "schedule-ledger.json"

DEFAULT_MAX_SOURCE_PCM_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_TRANSLATED_PCM_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_SOURCE_CHUNKS = 100_000
DEFAULT_MAX_FRAMES = 250_000
DEFAULT_MAX_PARENTS = 50_000

SOURCE_EMISSION_EARLY_TOLERANCE_SECONDS = 0.001
FLOAT_TOLERANCE_SECONDS = 1e-9
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

LEDGER_KEYS = frozenset(
    {
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
)
CAPTURE_KEYS = frozenset(
    {
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
)
PCM_KEYS = frozenset(
    {
        "sample_rate_hz",
        "channels",
        "bytes_per_sample",
        "sample_count",
        "audio_bytes",
        "pcm_sha256",
        "review_wav_sha256",
    }
)
SOURCE_CHUNK_KEYS = frozenset(
    {
        "chunk_index",
        "sample_start",
        "sample_end_exclusive",
        "audio_bytes",
        "deadline_seconds",
        "emitted_seconds",
    }
)
FRAME_KEYS = frozenset(
    {
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
)
PARENT_KEYS = frozenset(
    {
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
)
PRIVACY = {
    "contains_private_audio_in_companion_wavs": True,
    "ledger_contains_pcm": False,
    "contains_transcript_or_translation_text": False,
    "contains_file_path_or_uri": False,
    "contains_wall_clock_timestamp": False,
    "review_required_before_sharing": True,
}

_FRAME_METADATA_KEYS = frozenset(
    {
        "type",
        "protocolVersion",
        "streamGeneration",
        "parentSequenceId",
        "audioFrameId",
        "audioBytes",
        "sampleRateHz",
        "channels",
        "bytesPerSample",
        "sourceStartMs",
        "sourceEndMs",
    }
)
_PARENT_METADATA_KEYS = frozenset(
    {
        "type",
        "protocolVersion",
        "streamGeneration",
        "parentSequenceId",
        "audioFrameCount",
        "audioBytes",
        "sourceStartMs",
        "sourceEndMs",
    }
)
_PLAYBACK_MODES = frozenset(
    {"normal", "catch-up", "urgent", "over-limit"}
)


class PrivatePcmScheduleLedgerError(ValueError):
    """Raised when private capture evidence is unsafe or inconsistent."""


@dataclass(frozen=True)
class PrivatePcmScheduleArtifacts:
    """Paths to one newly installed private artifact set."""

    directory: Path
    source_wav: Path
    translated_wav: Path
    ledger_json: Path
    ledger_sha256: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact_keys(
    value: object,
    expected: frozenset[str],
    *,
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} must be an object"
        )
    actual = set(value)
    if actual != set(expected):
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} must use the exact schema"
        )
    return value


def _plain_int(
    value: object,
    *,
    field_name: str,
    minimum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} must be an integer at least {minimum}"
        )
    return value


def _finite(
    value: object,
    *,
    field_name: str,
    minimum: float = 0.0,
    strictly_positive: bool = False,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < minimum
        or (strictly_positive and value <= minimum)
    ):
        qualifier = "positive " if strictly_positive else ""
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} must be a finite {qualifier}number"
        )
    return float(value)


def _source_offset(
    value: object,
    *,
    field_name: str,
) -> float | None:
    if value is None:
        return None
    return _finite(value, field_name=field_name)


def _source_range(
    start: object,
    end: object,
    *,
    field_name: str,
) -> tuple[float | None, float | None]:
    normalized_start = _source_offset(
        start,
        field_name=f"{field_name}.source_start_ms",
    )
    normalized_end = _source_offset(
        end,
        field_name=f"{field_name}.source_end_ms",
    )
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_end < normalized_start
    ):
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} source range is reversed"
        )
    return normalized_start, normalized_end


def _require_close(
    actual: float,
    expected: float,
    *,
    field_name: str,
    tolerance: float = FLOAT_TOLERANCE_SECONDS,
) -> None:
    if not math.isclose(
        actual,
        expected,
        rel_tol=0.0,
        abs_tol=tolerance,
    ):
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} does not reconcile"
        )


def _require_sha256(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or SHA256_PATTERN.fullmatch(value) is None
    ):
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} must be a lowercase SHA-256"
        )
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PrivatePcmScheduleLedgerError(
            "ledger is not strict JSON"
        ) from exc


def _build_pcm16_mono_wav(pcm: bytes, sample_rate_hz: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate_hz)
        writer.setcomptype("NONE", "not compressed")
        writer.writeframes(pcm)
    return output.getvalue()


def _read_pcm16_mono_wav(
    payload: bytes,
    *,
    field_name: str,
) -> tuple[int, bytes]:
    if not isinstance(payload, bytes) or not payload:
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} must contain WAV bytes"
        )
    try:
        with wave.open(io.BytesIO(payload), "rb") as reader:
            if (
                reader.getnchannels() != 1
                or reader.getsampwidth() != 2
                or reader.getcomptype() != "NONE"
                or reader.getframerate() <= 0
            ):
                raise PrivatePcmScheduleLedgerError(
                    f"{field_name} must be mono PCM16"
                )
            sample_rate_hz = reader.getframerate()
            frame_count = reader.getnframes()
            pcm = reader.readframes(frame_count)
            if len(pcm) != frame_count * 2:
                raise PrivatePcmScheduleLedgerError(
                    f"{field_name} data length is inconsistent"
                )
            if reader.readframes(1):
                raise PrivatePcmScheduleLedgerError(
                    f"{field_name} contains undeclared frames"
                )
    except (EOFError, wave.Error) as exc:
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} is not a valid WAV"
        ) from exc
    return sample_rate_hz, pcm


def _validate_pcm_section(
    value: object,
    *,
    field_name: str,
    wav_bytes: bytes | None,
) -> tuple[Mapping[str, Any], bytes | None]:
    section = _exact_keys(value, PCM_KEYS, field_name=field_name)
    sample_rate_hz = _plain_int(
        section["sample_rate_hz"],
        field_name=f"{field_name}.sample_rate_hz",
        minimum=1,
    )
    channels = _plain_int(
        section["channels"],
        field_name=f"{field_name}.channels",
        minimum=1,
    )
    bytes_per_sample = _plain_int(
        section["bytes_per_sample"],
        field_name=f"{field_name}.bytes_per_sample",
        minimum=1,
    )
    if channels != 1 or bytes_per_sample != 2:
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} must describe mono PCM16"
        )
    sample_count = _plain_int(
        section["sample_count"],
        field_name=f"{field_name}.sample_count",
        minimum=1,
    )
    audio_bytes = _plain_int(
        section["audio_bytes"],
        field_name=f"{field_name}.audio_bytes",
        minimum=1,
    )
    if audio_bytes != sample_count * channels * bytes_per_sample:
        raise PrivatePcmScheduleLedgerError(
            f"{field_name} byte and sample counts do not reconcile"
        )
    pcm_sha256 = _require_sha256(
        section["pcm_sha256"],
        field_name=f"{field_name}.pcm_sha256",
    )
    wav_sha256 = _require_sha256(
        section["review_wav_sha256"],
        field_name=f"{field_name}.review_wav_sha256",
    )
    pcm: bytes | None = None
    if wav_bytes is not None:
        if _sha256(wav_bytes) != wav_sha256:
            raise PrivatePcmScheduleLedgerError(
                f"{field_name} review WAV hash does not match"
            )
        wav_rate, pcm = _read_pcm16_mono_wav(
            wav_bytes,
            field_name=f"{field_name} review WAV",
        )
        if (
            wav_rate != sample_rate_hz
            or len(pcm) != audio_bytes
            or _sha256(pcm) != pcm_sha256
        ):
            raise PrivatePcmScheduleLedgerError(
                f"{field_name} review WAV does not match its PCM ledger"
            )
    return section, pcm


def validate_private_pcm_schedule_ledger(
    value: object,
    *,
    source_wav_bytes: bytes | None = None,
    translated_wav_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Validate one exact ledger, optionally binding its companion WAVs."""

    ledger = _exact_keys(value, LEDGER_KEYS, field_name="ledger")
    if ledger["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise PrivatePcmScheduleLedgerError(
            f"schema_version must equal {LEDGER_SCHEMA_VERSION}"
        )
    if ledger["report_type"] != LEDGER_REPORT_TYPE:
        raise PrivatePcmScheduleLedgerError("report_type is invalid")

    capture = _exact_keys(
        ledger["capture"],
        CAPTURE_KEYS,
        field_name="capture",
    )
    if (
        capture["audio_metadata_protocol_version"]
        != AUDIO_METADATA_PROTOCOL_VERSION
        or capture["terminal_completed"] is not True
        or capture["input_pacing_mode"] != INPUT_PACING_MODE
        or capture["clock"] != CAPTURE_CLOCK
        or capture["canonical_replay_verified"] is not True
    ):
        raise PrivatePcmScheduleLedgerError(
            "capture provenance is invalid"
        )
    stream_generation = _plain_int(
        capture["stream_generation"],
        field_name="capture.stream_generation",
        minimum=1,
    )
    sample_zero = _finite(
        capture["input_sample_zero_seconds"],
        field_name="capture.input_sample_zero_seconds",
    )
    input_end = _finite(
        capture["input_end_seconds"],
        field_name="capture.input_end_seconds",
    )
    if input_end < sample_zero:
        raise PrivatePcmScheduleLedgerError(
            "input end precedes input sample zero"
        )
    source_chunk_count = _plain_int(
        capture["source_chunk_count"],
        field_name="capture.source_chunk_count",
        minimum=1,
    )
    parent_count = _plain_int(
        capture["parent_count"],
        field_name="capture.parent_count",
        minimum=1,
    )
    frame_count = _plain_int(
        capture["frame_count"],
        field_name="capture.frame_count",
        minimum=1,
    )

    source_pcm, source_payload = _validate_pcm_section(
        ledger["source_pcm"],
        field_name="source_pcm",
        wav_bytes=source_wav_bytes,
    )
    translated_pcm, translated_payload = _validate_pcm_section(
        ledger["translated_pcm"],
        field_name="translated_pcm",
        wav_bytes=translated_wav_bytes,
    )

    expected_policy = asdict(DEFAULT_PLAYBACK_POLICY)
    if (
        not isinstance(ledger["policy"], Mapping)
        or dict(ledger["policy"]) != expected_policy
    ):
        raise PrivatePcmScheduleLedgerError(
            "policy must equal the registered playback policy"
        )

    chunks = ledger["source_chunks"]
    if not isinstance(chunks, list) or len(chunks) != source_chunk_count:
        raise PrivatePcmScheduleLedgerError(
            "source chunk count does not reconcile"
        )
    next_source_sample = 0
    previous_emitted = -math.inf
    nominal_chunk_duration: float | None = None
    nominal_chunk_samples: int | None = None
    previous_chunk_samples: int | None = None
    for index, raw_chunk in enumerate(chunks):
        chunk = _exact_keys(
            raw_chunk,
            SOURCE_CHUNK_KEYS,
            field_name=f"source_chunks[{index}]",
        )
        chunk_index = _plain_int(
            chunk["chunk_index"],
            field_name=f"source_chunks[{index}].chunk_index",
            minimum=0,
        )
        if chunk_index != index:
            raise PrivatePcmScheduleLedgerError(
                "source chunk IDs must be contiguous from zero"
            )
        start = _plain_int(
            chunk["sample_start"],
            field_name=f"source_chunks[{index}].sample_start",
            minimum=0,
        )
        end = _plain_int(
            chunk["sample_end_exclusive"],
            field_name=f"source_chunks[{index}].sample_end_exclusive",
            minimum=1,
        )
        if start != next_source_sample or end <= start:
            raise PrivatePcmScheduleLedgerError(
                "source chunk sample ranges must be a contiguous partition"
            )
        audio_bytes = _plain_int(
            chunk["audio_bytes"],
            field_name=f"source_chunks[{index}].audio_bytes",
            minimum=1,
        )
        if audio_bytes != (end - start) * 2:
            raise PrivatePcmScheduleLedgerError(
                "source chunk bytes do not match its sample range"
            )
        deadline = _finite(
            chunk["deadline_seconds"],
            field_name=f"source_chunks[{index}].deadline_seconds",
        )
        emitted = _finite(
            chunk["emitted_seconds"],
            field_name=f"source_chunks[{index}].emitted_seconds",
        )
        if nominal_chunk_duration is None:
            nominal_chunk_duration = deadline - sample_zero
            if nominal_chunk_duration <= 0:
                raise PrivatePcmScheduleLedgerError(
                    "first source deadline must follow sample zero"
                )
            nominal_samples_float = (
                nominal_chunk_duration
                * int(source_pcm["sample_rate_hz"])
            )
            nominal_chunk_samples = round(nominal_samples_float)
            if (
                nominal_chunk_samples <= 0
                or not math.isclose(
                    nominal_samples_float,
                    nominal_chunk_samples,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                raise PrivatePcmScheduleLedgerError(
                    "source chunk cadence is not sample-aligned"
                )
        assert nominal_chunk_samples is not None
        if (
            previous_chunk_samples is not None
            and previous_chunk_samples != nominal_chunk_samples
        ):
            raise PrivatePcmScheduleLedgerError(
                "only the final source chunk may be partial"
            )
        current_chunk_samples = end - start
        if current_chunk_samples > nominal_chunk_samples:
            raise PrivatePcmScheduleLedgerError(
                "source chunk exceeds the registered pacing interval"
            )
        expected_deadline = (
            sample_zero + (index + 1) * nominal_chunk_duration
        )
        _require_close(
            deadline,
            expected_deadline,
            field_name=f"source_chunks[{index}].deadline_seconds",
            tolerance=1e-6,
        )
        if (
            emitted + SOURCE_EMISSION_EARLY_TOLERANCE_SECONDS
            < deadline
            or emitted < previous_emitted
        ):
            raise PrivatePcmScheduleLedgerError(
                "source chunk emissions are early or out of order"
            )
        next_source_sample = end
        previous_emitted = emitted
        previous_chunk_samples = current_chunk_samples
    if next_source_sample != source_pcm["sample_count"]:
        raise PrivatePcmScheduleLedgerError(
            "source chunks do not cover the exact source PCM"
        )
    if input_end + FLOAT_TOLERANCE_SECONDS < previous_emitted:
        raise PrivatePcmScheduleLedgerError(
            "input end precedes the final source emission"
        )

    frames = ledger["frames"]
    if not isinstance(frames, list) or len(frames) != frame_count:
        raise PrivatePcmScheduleLedgerError(
            "translated frame count does not reconcile"
        )
    normalized_frames: list[Mapping[str, Any]] = []
    next_translated_sample = 0
    previous_arrival = -math.inf
    previous_scheduled_end = -math.inf
    active_parent: int | None = None
    next_parent = 0
    next_frame_id = 0
    active_source_range: tuple[float | None, float | None] | None = None
    for index, raw_frame in enumerate(frames):
        frame = _exact_keys(
            raw_frame,
            FRAME_KEYS,
            field_name=f"frames[{index}]",
        )
        source_index = _plain_int(
            frame["source_index"],
            field_name=f"frames[{index}].source_index",
            minimum=0,
        )
        if source_index != index:
            raise PrivatePcmScheduleLedgerError(
                "frame source indices must be contiguous from zero"
            )
        generation = _plain_int(
            frame["stream_generation"],
            field_name=f"frames[{index}].stream_generation",
            minimum=1,
        )
        if generation != stream_generation:
            raise PrivatePcmScheduleLedgerError(
                "frame stream generation does not reconcile"
            )
        parent_id = _plain_int(
            frame["parent_sequence_id"],
            field_name=f"frames[{index}].parent_sequence_id",
            minimum=0,
        )
        frame_id = _plain_int(
            frame["audio_frame_id"],
            field_name=f"frames[{index}].audio_frame_id",
            minimum=0,
        )
        source_range = _source_range(
            frame["source_start_ms"],
            frame["source_end_ms"],
            field_name=f"frames[{index}]",
        )
        if active_parent is None or parent_id != active_parent:
            if parent_id != next_parent or frame_id != 0:
                raise PrivatePcmScheduleLedgerError(
                    "parent/frame identity is not contiguous"
                )
            active_parent = parent_id
            active_source_range = source_range
            next_frame_id = 0
            next_parent += 1
        if (
            parent_id != active_parent
            or frame_id != next_frame_id
            or source_range != active_source_range
        ):
            raise PrivatePcmScheduleLedgerError(
                "frame identity or parent source range is inconsistent"
            )
        next_frame_id += 1

        audio_bytes = _plain_int(
            frame["audio_bytes"],
            field_name=f"frames[{index}].audio_bytes",
            minimum=1,
        )
        sample_count = _plain_int(
            frame["sample_count"],
            field_name=f"frames[{index}].sample_count",
            minimum=1,
        )
        if audio_bytes != sample_count * 2:
            raise PrivatePcmScheduleLedgerError(
                "frame byte and sample counts do not reconcile"
            )
        translated_start = _plain_int(
            frame["translated_sample_start"],
            field_name=f"frames[{index}].translated_sample_start",
            minimum=0,
        )
        translated_end = _plain_int(
            frame["translated_sample_end_exclusive"],
            field_name=(
                f"frames[{index}].translated_sample_end_exclusive"
            ),
            minimum=1,
        )
        if (
            translated_start != next_translated_sample
            or translated_end - translated_start != sample_count
        ):
            raise PrivatePcmScheduleLedgerError(
                "translated frame samples are not a contiguous partition"
            )
        frame_hash = _require_sha256(
            frame["pcm_sha256"],
            field_name=f"frames[{index}].pcm_sha256",
        )
        if translated_payload is not None:
            start_byte = translated_start * 2
            end_byte = translated_end * 2
            if _sha256(translated_payload[start_byte:end_byte]) != frame_hash:
                raise PrivatePcmScheduleLedgerError(
                    "translated frame PCM hash does not match"
                )

        arrival = _finite(
            frame["arrival_seconds"],
            field_name=f"frames[{index}].arrival_seconds",
        )
        scheduled_start = _finite(
            frame["scheduled_start_seconds"],
            field_name=f"frames[{index}].scheduled_start_seconds",
        )
        scheduled_end = _finite(
            frame["scheduled_end_seconds"],
            field_name=f"frames[{index}].scheduled_end_seconds",
        )
        playback_rate = _finite(
            frame["playback_rate"],
            field_name=f"frames[{index}].playback_rate",
            strictly_positive=True,
        )
        playback_mode = frame["playback_mode"]
        if playback_mode not in _PLAYBACK_MODES:
            raise PrivatePcmScheduleLedgerError(
                "frame playback mode is invalid"
            )
        if (
            arrival < previous_arrival
            or scheduled_start + FLOAT_TOLERANCE_SECONDS < arrival
            or scheduled_start + FLOAT_TOLERANCE_SECONDS
            < previous_scheduled_end
        ):
            raise PrivatePcmScheduleLedgerError(
                "frame arrival or schedule order is invalid"
            )
        source_duration = (
            sample_count / int(translated_pcm["sample_rate_hz"])
        )
        _require_close(
            scheduled_end,
            scheduled_start + source_duration / playback_rate,
            field_name=f"frames[{index}].scheduled_end_seconds",
        )
        previous_arrival = arrival
        previous_scheduled_end = scheduled_end
        next_translated_sample = translated_end
        normalized_frames.append(frame)

    if next_translated_sample != translated_pcm["sample_count"]:
        raise PrivatePcmScheduleLedgerError(
            "frames do not cover the exact translated PCM"
        )

    parents = ledger["parents"]
    if not isinstance(parents, list) or len(parents) != parent_count:
        raise PrivatePcmScheduleLedgerError(
            "translated parent count does not reconcile"
        )
    frame_cursor = 0
    next_parent_sample = 0
    previous_completion = -math.inf
    for index, raw_parent in enumerate(parents):
        parent = _exact_keys(
            raw_parent,
            PARENT_KEYS,
            field_name=f"parents[{index}]",
        )
        generation = _plain_int(
            parent["stream_generation"],
            field_name=f"parents[{index}].stream_generation",
            minimum=1,
        )
        parent_id = _plain_int(
            parent["parent_sequence_id"],
            field_name=f"parents[{index}].parent_sequence_id",
            minimum=0,
        )
        frame_total = _plain_int(
            parent["audio_frame_count"],
            field_name=f"parents[{index}].audio_frame_count",
            minimum=1,
        )
        audio_bytes = _plain_int(
            parent["audio_bytes"],
            field_name=f"parents[{index}].audio_bytes",
            minimum=1,
        )
        parent_start = _plain_int(
            parent["translated_sample_start"],
            field_name=f"parents[{index}].translated_sample_start",
            minimum=0,
        )
        parent_end = _plain_int(
            parent["translated_sample_end_exclusive"],
            field_name=(
                f"parents[{index}].translated_sample_end_exclusive"
            ),
            minimum=1,
        )
        source_range = _source_range(
            parent["source_start_ms"],
            parent["source_end_ms"],
            field_name=f"parents[{index}]",
        )
        completion = _finite(
            parent["completion_received_seconds"],
            field_name=f"parents[{index}].completion_received_seconds",
        )
        parent_frames = normalized_frames[
            frame_cursor : frame_cursor + frame_total
        ]
        if (
            generation != stream_generation
            or parent_id != index
            or len(parent_frames) != frame_total
            or any(
                frame["parent_sequence_id"] != parent_id
                for frame in parent_frames
            )
            or parent_start != next_parent_sample
            or parent_start
            != parent_frames[0]["translated_sample_start"]
            or parent_end
            != parent_frames[-1]["translated_sample_end_exclusive"]
            or audio_bytes != sum(
                int(frame["audio_bytes"]) for frame in parent_frames
            )
            or source_range
            != (
                parent_frames[0]["source_start_ms"],
                parent_frames[0]["source_end_ms"],
            )
            or completion + FLOAT_TOLERANCE_SECONDS
            < float(parent_frames[-1]["arrival_seconds"])
            or completion < previous_completion
        ):
            raise PrivatePcmScheduleLedgerError(
                "parent completion does not reconcile with its frames"
            )
        frame_cursor += frame_total
        next_parent_sample = parent_end
        previous_completion = completion
    if (
        frame_cursor != frame_count
        or next_parent_sample != translated_pcm["sample_count"]
    ):
        raise PrivatePcmScheduleLedgerError(
            "parents do not cover the exact translated frame ledger"
        )

    simulation = simulate_playback(
        tuple(
            AudioChunk(
                arrival_seconds=float(frame["arrival_seconds"]),
                duration_seconds=(
                    int(frame["sample_count"])
                    / int(translated_pcm["sample_rate_hz"])
                ),
                audio_bytes=int(frame["audio_bytes"]),
                source_index=index,
            )
            for index, frame in enumerate(normalized_frames)
        ),
        input_end_seconds=input_end,
        adaptive=True,
        policy=DEFAULT_PLAYBACK_POLICY,
    )
    for index, (frame, canonical) in enumerate(
        zip(normalized_frames, simulation.schedule, strict=True)
    ):
        if (
            frame["source_index"] != canonical.source_index
            or frame["playback_mode"] != canonical.playback_mode
        ):
            raise PrivatePcmScheduleLedgerError(
                "saved schedule identity differs from canonical replay"
            )
        for field_name, actual, expected in (
            (
                "arrival_seconds",
                frame["arrival_seconds"],
                canonical.arrival_seconds,
            ),
            (
                "scheduled_start_seconds",
                frame["scheduled_start_seconds"],
                canonical.start_seconds,
            ),
            (
                "scheduled_end_seconds",
                frame["scheduled_end_seconds"],
                canonical.end_seconds,
            ),
            (
                "playback_rate",
                frame["playback_rate"],
                canonical.playback_rate,
            ),
        ):
            _require_close(
                float(actual),
                float(expected),
                field_name=f"frames[{index}].{field_name}",
            )

    if ledger["privacy"] != PRIVACY:
        raise PrivatePcmScheduleLedgerError(
            "privacy declaration is invalid"
        )

    # JSON round-trip creates a plain detached result and rejects nonstandard
    # objects without retaining caller-owned mutable mappings.
    return json.loads(_json_bytes(ledger).decode("utf-8"))


class PrivatePcmScheduleCapture:
    """Accumulate one bounded, protocol-validated private capture."""

    def __init__(
        self,
        *,
        max_source_pcm_bytes: int = DEFAULT_MAX_SOURCE_PCM_BYTES,
        max_translated_pcm_bytes: int = DEFAULT_MAX_TRANSLATED_PCM_BYTES,
        max_source_chunks: int = DEFAULT_MAX_SOURCE_CHUNKS,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_parents: int = DEFAULT_MAX_PARENTS,
    ) -> None:
        self.max_source_pcm_bytes = _plain_int(
            max_source_pcm_bytes,
            field_name="max_source_pcm_bytes",
            minimum=1,
        )
        self.max_translated_pcm_bytes = _plain_int(
            max_translated_pcm_bytes,
            field_name="max_translated_pcm_bytes",
            minimum=1,
        )
        self.max_source_chunks = _plain_int(
            max_source_chunks,
            field_name="max_source_chunks",
            minimum=1,
        )
        self.max_frames = _plain_int(
            max_frames,
            field_name="max_frames",
            minimum=1,
        )
        self.max_parents = _plain_int(
            max_parents,
            field_name="max_parents",
            minimum=1,
        )

        self._source_pcm: bytearray | None = None
        self._source_sample_rate_hz: int | None = None
        self._source_sample_zero_seconds: float | None = None
        self._source_chunk_duration_seconds: float | None = None
        self._source_chunk_samples: int | None = None
        self._source_chunks: list[dict[str, Any]] = []

        self._translated_pcm = bytearray()
        self._translated_sample_rate_hz: int | None = None
        self._stream_generation: int | None = None
        self._frames: list[dict[str, Any]] = []
        self._parents: list[dict[str, Any]] = []

        self._active_parent_id: int | None = None
        self._active_parent_frame_count = 0
        self._active_parent_audio_bytes = 0
        self._active_parent_sample_start = 0
        self._active_source_range: tuple[
            float | None, float | None
        ] | None = None
        self._next_parent_id = 0
        self._previous_arrival_seconds = -math.inf
        self._previous_scheduled_end_seconds = -math.inf
        self._previous_completion_seconds = -math.inf

        self._sealed = False
        self._aborted = False
        self._ledger: dict[str, Any] | None = None
        self._ledger_bytes: bytes | None = None
        self._source_wav_bytes: bytes | None = None
        self._translated_wav_bytes: bytes | None = None
        self._artifacts_written = False

    def _require_open(self) -> None:
        if self._aborted:
            raise PrivatePcmScheduleLedgerError("capture was aborted")
        if self._sealed:
            raise PrivatePcmScheduleLedgerError("capture is already sealed")

    def bind_source_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate_hz: int,
        channels: int,
        bytes_per_sample: int,
    ) -> None:
        """Bind the exact source PCM bytes before source pacing begins."""

        self._require_open()
        if self._source_pcm is not None:
            raise PrivatePcmScheduleLedgerError(
                "source PCM is already bound"
            )
        if not isinstance(pcm, bytes) or not pcm:
            raise PrivatePcmScheduleLedgerError(
                "source PCM must be nonempty bytes"
            )
        normalized_rate = _plain_int(
            sample_rate_hz,
            field_name="sample_rate_hz",
            minimum=1,
        )
        normalized_channels = _plain_int(
            channels,
            field_name="channels",
            minimum=1,
        )
        normalized_width = _plain_int(
            bytes_per_sample,
            field_name="bytes_per_sample",
            minimum=1,
        )
        if normalized_channels != 1 or normalized_width != 2:
            raise PrivatePcmScheduleLedgerError(
                "source PCM must be mono PCM16"
            )
        if len(pcm) % 2:
            raise PrivatePcmScheduleLedgerError(
                "source PCM contains a partial sample"
            )
        if len(pcm) > self.max_source_pcm_bytes:
            raise PrivatePcmScheduleLedgerError(
                "source PCM exceeds the configured private-data limit"
            )
        self._source_pcm = bytearray(pcm)
        self._source_sample_rate_hz = normalized_rate

    def record_source_anchor(self, sample_zero_seconds: float) -> None:
        """Record the source sample-zero position on the capture clock."""

        self._require_open()
        if self._source_pcm is None:
            raise PrivatePcmScheduleLedgerError(
                "source PCM must be bound before its clock anchor"
            )
        if self._source_sample_zero_seconds is not None:
            raise PrivatePcmScheduleLedgerError(
                "source sample-zero anchor is already recorded"
            )
        self._source_sample_zero_seconds = _finite(
            sample_zero_seconds,
            field_name="sample_zero_seconds",
        )

    def record_source_chunk(
        self,
        *,
        chunk_index: int,
        sample_start: int,
        sample_end_exclusive: int,
        audio_bytes: int,
        emitted_seconds: float,
        deadline_seconds: float,
    ) -> None:
        """Record one successfully emitted contiguous source PCM chunk."""

        self._require_open()
        if (
            self._source_pcm is None
            or self._source_sample_rate_hz is None
            or self._source_sample_zero_seconds is None
        ):
            raise PrivatePcmScheduleLedgerError(
                "source PCM and sample-zero anchor are required"
            )
        if len(self._source_chunks) >= self.max_source_chunks:
            raise PrivatePcmScheduleLedgerError(
                "source chunk count exceeds the configured limit"
            )
        normalized_index = _plain_int(
            chunk_index,
            field_name="chunk_index",
            minimum=0,
        )
        if normalized_index != len(self._source_chunks):
            raise PrivatePcmScheduleLedgerError(
                "source chunk IDs must be contiguous from zero"
            )
        start = _plain_int(
            sample_start,
            field_name="sample_start",
            minimum=0,
        )
        end = _plain_int(
            sample_end_exclusive,
            field_name="sample_end_exclusive",
            minimum=1,
        )
        expected_start = (
            self._source_chunks[-1]["sample_end_exclusive"]
            if self._source_chunks
            else 0
        )
        total_samples = len(self._source_pcm) // 2
        if start != expected_start or end <= start or end > total_samples:
            raise PrivatePcmScheduleLedgerError(
                "source chunk range is not a valid contiguous partition"
            )
        normalized_audio_bytes = _plain_int(
            audio_bytes,
            field_name="audio_bytes",
            minimum=1,
        )
        if normalized_audio_bytes != (end - start) * 2:
            raise PrivatePcmScheduleLedgerError(
                "source chunk bytes do not match its sample range"
            )
        emitted = _finite(
            emitted_seconds,
            field_name="emitted_seconds",
        )
        deadline = _finite(
            deadline_seconds,
            field_name="deadline_seconds",
        )
        if self._source_chunk_duration_seconds is None:
            chunk_duration = deadline - self._source_sample_zero_seconds
            if chunk_duration <= 0:
                raise PrivatePcmScheduleLedgerError(
                    "first source deadline must follow sample zero"
                )
            nominal_samples_float = (
                chunk_duration * self._source_sample_rate_hz
            )
            nominal_samples = round(nominal_samples_float)
            if (
                nominal_samples <= 0
                or not math.isclose(
                    nominal_samples_float,
                    nominal_samples,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                raise PrivatePcmScheduleLedgerError(
                    "source chunk cadence is not sample-aligned"
                )
            self._source_chunk_duration_seconds = chunk_duration
            self._source_chunk_samples = nominal_samples
        assert self._source_chunk_duration_seconds is not None
        assert self._source_chunk_samples is not None
        if self._source_chunks:
            previous = self._source_chunks[-1]
            if (
                previous["sample_end_exclusive"]
                - previous["sample_start"]
                != self._source_chunk_samples
            ):
                raise PrivatePcmScheduleLedgerError(
                    "only the final source chunk may be partial"
                )
        if end - start > self._source_chunk_samples:
            raise PrivatePcmScheduleLedgerError(
                "source chunk exceeds the registered pacing interval"
            )
        expected_deadline = (
            self._source_sample_zero_seconds
            + (normalized_index + 1)
            * self._source_chunk_duration_seconds
        )
        _require_close(
            deadline,
            expected_deadline,
            field_name="deadline_seconds",
            tolerance=1e-6,
        )
        if (
            emitted + SOURCE_EMISSION_EARLY_TOLERANCE_SECONDS < deadline
            or (
                self._source_chunks
                and emitted < self._source_chunks[-1]["emitted_seconds"]
            )
        ):
            raise PrivatePcmScheduleLedgerError(
                "source chunk emission is early or out of order"
            )
        self._source_chunks.append(
            {
                "chunk_index": normalized_index,
                "sample_start": start,
                "sample_end_exclusive": end,
                "audio_bytes": normalized_audio_bytes,
                "deadline_seconds": deadline,
                "emitted_seconds": emitted,
            }
        )

    def _require_generation(self, generation: int) -> None:
        if self._stream_generation is None:
            self._stream_generation = generation
        elif generation != self._stream_generation:
            raise PrivatePcmScheduleLedgerError(
                "stream generation changed within one capture"
            )

    def accept_frame(
        self,
        *,
        metadata: Mapping[str, object],
        pcm: bytes,
        schedule: HeadlessScheduledFrame,
    ) -> None:
        """Accept one validated frame and its exact scheduling decision."""

        self._require_open()
        if self._source_sample_zero_seconds is None:
            raise PrivatePcmScheduleLedgerError(
                "source sample-zero anchor must precede translated PCM"
            )
        header = _exact_keys(
            metadata,
            _FRAME_METADATA_KEYS,
            field_name="audio_frame metadata",
        )
        if header["type"] != "audio_frame":
            raise PrivatePcmScheduleLedgerError(
                "audio_frame metadata type is invalid"
            )
        if (
            _plain_int(
                header["protocolVersion"],
                field_name="protocolVersion",
                minimum=1,
            )
            != AUDIO_METADATA_PROTOCOL_VERSION
        ):
            raise PrivatePcmScheduleLedgerError(
                "protocolVersion must equal one"
            )
        generation = _plain_int(
            header["streamGeneration"],
            field_name="streamGeneration",
            minimum=1,
        )
        self._require_generation(generation)
        parent_id = _plain_int(
            header["parentSequenceId"],
            field_name="parentSequenceId",
            minimum=0,
        )
        frame_id = _plain_int(
            header["audioFrameId"],
            field_name="audioFrameId",
            minimum=0,
        )
        audio_bytes = _plain_int(
            header["audioBytes"],
            field_name="audioBytes",
            minimum=1,
        )
        sample_rate_hz = _plain_int(
            header["sampleRateHz"],
            field_name="sampleRateHz",
            minimum=1,
        )
        channels = _plain_int(
            header["channels"],
            field_name="channels",
            minimum=1,
        )
        bytes_per_sample = _plain_int(
            header["bytesPerSample"],
            field_name="bytesPerSample",
            minimum=1,
        )
        source_range = _source_range(
            header["sourceStartMs"],
            header["sourceEndMs"],
            field_name="audio_frame metadata",
        )
        if not isinstance(pcm, bytes) or not pcm:
            raise PrivatePcmScheduleLedgerError(
                "translated PCM must be nonempty bytes"
            )
        if (
            channels != 1
            or bytes_per_sample != 2
            or len(pcm) != audio_bytes
            or audio_bytes % 2
        ):
            raise PrivatePcmScheduleLedgerError(
                "translated PCM does not match mono PCM16 metadata"
            )
        if (
            len(self._translated_pcm) + audio_bytes
            > self.max_translated_pcm_bytes
        ):
            raise PrivatePcmScheduleLedgerError(
                "translated PCM exceeds the configured private-data limit"
            )
        if len(self._frames) >= self.max_frames:
            raise PrivatePcmScheduleLedgerError(
                "translated frame count exceeds the configured limit"
            )
        if self._translated_sample_rate_hz is None:
            self._translated_sample_rate_hz = sample_rate_hz
        elif sample_rate_hz != self._translated_sample_rate_hz:
            raise PrivatePcmScheduleLedgerError(
                "translated sample rate changed within one capture"
            )

        if not isinstance(schedule, HeadlessScheduledFrame):
            raise PrivatePcmScheduleLedgerError(
                "schedule must be a HeadlessScheduledFrame"
            )
        source_index = len(self._frames)
        if (
            schedule.source_index != source_index
            or schedule.stream_generation != generation
            or schedule.parent_sequence_id != parent_id
            or schedule.audio_frame_id != frame_id
            or (
                schedule.source_start_ms,
                schedule.source_end_ms,
            )
            != source_range
        ):
            raise PrivatePcmScheduleLedgerError(
                "schedule identity does not match the validated frame"
            )

        sample_count = audio_bytes // 2
        source_duration = sample_count / sample_rate_hz
        _require_close(
            schedule.source_duration_seconds,
            source_duration,
            field_name="schedule.source_duration_seconds",
        )
        arrival = _finite(
            schedule.arrival_seconds,
            field_name="schedule.arrival_seconds",
        )
        scheduled_start = _finite(
            schedule.start_seconds,
            field_name="schedule.start_seconds",
        )
        scheduled_end = _finite(
            schedule.end_seconds,
            field_name="schedule.end_seconds",
        )
        playback_rate = _finite(
            schedule.playback_rate,
            field_name="schedule.playback_rate",
            strictly_positive=True,
        )
        if schedule.playback_mode not in _PLAYBACK_MODES:
            raise PrivatePcmScheduleLedgerError(
                "schedule playback mode is invalid"
            )
        _require_close(
            scheduled_end,
            scheduled_start + source_duration / playback_rate,
            field_name="schedule.end_seconds",
        )
        if (
            arrival < self._previous_arrival_seconds
            or scheduled_start + FLOAT_TOLERANCE_SECONDS < arrival
            or scheduled_start + FLOAT_TOLERANCE_SECONDS
            < self._previous_scheduled_end_seconds
        ):
            raise PrivatePcmScheduleLedgerError(
                "schedule arrival or playback order is invalid"
            )

        if self._active_parent_id is None:
            if parent_id != self._next_parent_id or frame_id != 0:
                raise PrivatePcmScheduleLedgerError(
                    "parent/frame identity must be contiguous from zero"
                )
            self._active_parent_id = parent_id
            self._active_parent_frame_count = 0
            self._active_parent_audio_bytes = 0
            self._active_parent_sample_start = len(
                self._translated_pcm
            ) // 2
            self._active_source_range = source_range
        elif (
            parent_id != self._active_parent_id
            or frame_id != self._active_parent_frame_count
            or source_range != self._active_source_range
        ):
            raise PrivatePcmScheduleLedgerError(
                "frame does not belong to the active parent"
            )

        translated_start = len(self._translated_pcm) // 2
        translated_end = translated_start + sample_count
        self._frames.append(
            {
                "source_index": source_index,
                "stream_generation": generation,
                "parent_sequence_id": parent_id,
                "audio_frame_id": frame_id,
                "audio_bytes": audio_bytes,
                "sample_count": sample_count,
                "translated_sample_start": translated_start,
                "translated_sample_end_exclusive": translated_end,
                "pcm_sha256": _sha256(pcm),
                "source_start_ms": source_range[0],
                "source_end_ms": source_range[1],
                "arrival_seconds": arrival,
                "scheduled_start_seconds": scheduled_start,
                "scheduled_end_seconds": scheduled_end,
                "playback_rate": playback_rate,
                "playback_mode": schedule.playback_mode,
            }
        )
        self._translated_pcm.extend(pcm)
        self._active_parent_frame_count += 1
        self._active_parent_audio_bytes += audio_bytes
        self._previous_arrival_seconds = arrival
        self._previous_scheduled_end_seconds = scheduled_end

    def complete_parent(
        self,
        metadata: Mapping[str, object],
        *,
        received_seconds: float,
    ) -> None:
        """Reconcile one validated parent-completion control message."""

        self._require_open()
        completion = _exact_keys(
            metadata,
            _PARENT_METADATA_KEYS,
            field_name="audio_parent_complete metadata",
        )
        if completion["type"] != "audio_parent_complete":
            raise PrivatePcmScheduleLedgerError(
                "audio_parent_complete metadata type is invalid"
            )
        if (
            _plain_int(
                completion["protocolVersion"],
                field_name="protocolVersion",
                minimum=1,
            )
            != AUDIO_METADATA_PROTOCOL_VERSION
        ):
            raise PrivatePcmScheduleLedgerError(
                "protocolVersion must equal one"
            )
        generation = _plain_int(
            completion["streamGeneration"],
            field_name="streamGeneration",
            minimum=1,
        )
        self._require_generation(generation)
        parent_id = _plain_int(
            completion["parentSequenceId"],
            field_name="parentSequenceId",
            minimum=0,
        )
        frame_count = _plain_int(
            completion["audioFrameCount"],
            field_name="audioFrameCount",
            minimum=1,
        )
        audio_bytes = _plain_int(
            completion["audioBytes"],
            field_name="audioBytes",
            minimum=1,
        )
        source_range = _source_range(
            completion["sourceStartMs"],
            completion["sourceEndMs"],
            field_name="audio_parent_complete metadata",
        )
        received = _finite(
            received_seconds,
            field_name="received_seconds",
        )
        if (
            self._active_parent_id is None
            or parent_id != self._active_parent_id
            or parent_id != self._next_parent_id
            or frame_count != self._active_parent_frame_count
            or audio_bytes != self._active_parent_audio_bytes
            or source_range != self._active_source_range
            or received + FLOAT_TOLERANCE_SECONDS
            < self._previous_arrival_seconds
            or received < self._previous_completion_seconds
        ):
            raise PrivatePcmScheduleLedgerError(
                "parent completion does not reconcile"
            )
        if len(self._parents) >= self.max_parents:
            raise PrivatePcmScheduleLedgerError(
                "translated parent count exceeds the configured limit"
            )
        self._parents.append(
            {
                "stream_generation": generation,
                "parent_sequence_id": parent_id,
                "audio_frame_count": frame_count,
                "audio_bytes": audio_bytes,
                "translated_sample_start": (
                    self._active_parent_sample_start
                ),
                "translated_sample_end_exclusive": (
                    len(self._translated_pcm) // 2
                ),
                "source_start_ms": source_range[0],
                "source_end_ms": source_range[1],
                "completion_received_seconds": received,
            }
        )
        self._next_parent_id += 1
        self._active_parent_id = None
        self._active_parent_frame_count = 0
        self._active_parent_audio_bytes = 0
        self._active_parent_sample_start = len(self._translated_pcm) // 2
        self._active_source_range = None
        self._previous_completion_seconds = received

    def seal(
        self,
        *,
        input_end_seconds: float,
        terminal_completed: bool,
        headless_report: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate, replay, and freeze the complete in-memory evidence."""

        self._require_open()
        if terminal_completed is not True:
            raise PrivatePcmScheduleLedgerError(
                "a completed terminal is required"
            )
        if (
            self._source_pcm is None
            or self._source_sample_rate_hz is None
            or self._source_sample_zero_seconds is None
            or not self._source_chunks
        ):
            raise PrivatePcmScheduleLedgerError(
                "source PCM pacing evidence is incomplete"
            )
        if (
            self._source_chunks[-1]["sample_end_exclusive"]
            != len(self._source_pcm) // 2
        ):
            raise PrivatePcmScheduleLedgerError(
                "source chunks do not cover the exact source PCM"
            )
        if (
            not self._frames
            or not self._parents
            or self._active_parent_id is not None
            or self._next_parent_id != len(self._parents)
            or self._stream_generation is None
            or self._translated_sample_rate_hz is None
        ):
            raise PrivatePcmScheduleLedgerError(
                "translated PCM parent evidence is incomplete"
            )
        input_end = _finite(
            input_end_seconds,
            field_name="input_end_seconds",
        )
        if (
            input_end < self._source_sample_zero_seconds
            or input_end + FLOAT_TOLERANCE_SECONDS
            < self._source_chunks[-1]["emitted_seconds"]
        ):
            raise PrivatePcmScheduleLedgerError(
                "input end does not follow the complete source ledger"
            )

        try:
            detached_headless = json.loads(
                json.dumps(headless_report, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise PrivatePcmScheduleLedgerError(
                "headless report is not strict JSON"
            ) from exc
        try:
            validate_headless_playback_report(
                detached_headless,
                expected_frames=len(self._frames),
                expected_parents=len(self._parents),
                expected_stream_generation=self._stream_generation,
            )
        except (TypeError, ValueError) as exc:
            raise PrivatePcmScheduleLedgerError(
                "headless report does not reconcile"
            ) from exc
        headless_capture = detached_headless["capture"]
        _require_close(
            float(headless_capture["input_end_seconds"]),
            input_end,
            field_name="headless capture input_end_seconds",
            tolerance=1e-6,
        )
        translated_duration = (
            len(self._translated_pcm)
            / (self._translated_sample_rate_hz * 2)
        )
        _require_close(
            float(headless_capture["translated_pcm_seconds"]),
            translated_duration,
            field_name="headless capture translated_pcm_seconds",
            tolerance=1e-6,
        )

        source_pcm = bytes(self._source_pcm)
        translated_pcm = bytes(self._translated_pcm)
        source_wav = _build_pcm16_mono_wav(
            source_pcm,
            self._source_sample_rate_hz,
        )
        translated_wav = _build_pcm16_mono_wav(
            translated_pcm,
            self._translated_sample_rate_hz,
        )
        ledger: dict[str, Any] = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "report_type": LEDGER_REPORT_TYPE,
            "capture": {
                "audio_metadata_protocol_version": (
                    AUDIO_METADATA_PROTOCOL_VERSION
                ),
                "stream_generation": self._stream_generation,
                "terminal_completed": True,
                "input_pacing_mode": INPUT_PACING_MODE,
                "clock": CAPTURE_CLOCK,
                "input_sample_zero_seconds": (
                    self._source_sample_zero_seconds
                ),
                "input_end_seconds": input_end,
                "source_chunk_count": len(self._source_chunks),
                "parent_count": len(self._parents),
                "frame_count": len(self._frames),
                "canonical_replay_verified": True,
            },
            "source_pcm": {
                "sample_rate_hz": self._source_sample_rate_hz,
                "channels": 1,
                "bytes_per_sample": 2,
                "sample_count": len(source_pcm) // 2,
                "audio_bytes": len(source_pcm),
                "pcm_sha256": _sha256(source_pcm),
                "review_wav_sha256": _sha256(source_wav),
            },
            "translated_pcm": {
                "sample_rate_hz": self._translated_sample_rate_hz,
                "channels": 1,
                "bytes_per_sample": 2,
                "sample_count": len(translated_pcm) // 2,
                "audio_bytes": len(translated_pcm),
                "pcm_sha256": _sha256(translated_pcm),
                "review_wav_sha256": _sha256(translated_wav),
            },
            "policy": asdict(DEFAULT_PLAYBACK_POLICY),
            "source_chunks": copy.deepcopy(self._source_chunks),
            "frames": copy.deepcopy(self._frames),
            "parents": copy.deepcopy(self._parents),
            "privacy": dict(PRIVACY),
        }
        validated = validate_private_pcm_schedule_ledger(
            ledger,
            source_wav_bytes=source_wav,
            translated_wav_bytes=translated_wav,
        )
        self._source_wav_bytes = source_wav
        self._translated_wav_bytes = translated_wav
        self._ledger = validated
        self._ledger_bytes = _json_bytes(validated)
        self._sealed = True
        return copy.deepcopy(validated)

    def write_new(
        self,
        artifact_dir: Path,
    ) -> PrivatePcmScheduleArtifacts:
        """Install the sealed artifact set in a fresh 0700 directory."""

        if self._aborted:
            raise PrivatePcmScheduleLedgerError("capture was aborted")
        if (
            not self._sealed
            or self._ledger is None
            or self._ledger_bytes is None
            or self._source_wav_bytes is None
            or self._translated_wav_bytes is None
        ):
            raise PrivatePcmScheduleLedgerError(
                "capture must be sealed before artifact installation"
            )
        if self._artifacts_written:
            raise PrivatePcmScheduleLedgerError(
                "private artifacts were already installed"
            )

        directory = _new_private_directory(artifact_dir)
        source_path = directory / SOURCE_WAV_FILENAME
        translated_path = directory / TRANSLATED_WAV_FILENAME
        ledger_path = directory / LEDGER_FILENAME
        installed: list[Path] = []
        try:
            # Publish the ledger last. Its presence is the commit marker for a
            # complete three-file artifact set.
            for destination, payload in (
                (source_path, self._source_wav_bytes),
                (translated_path, self._translated_wav_bytes),
                (ledger_path, self._ledger_bytes),
            ):
                _write_private_bytes_atomic(destination, payload)
                installed.append(destination)
            if (
                source_path.read_bytes() != self._source_wav_bytes
                or translated_path.read_bytes()
                != self._translated_wav_bytes
                or ledger_path.read_bytes() != self._ledger_bytes
            ):
                raise PrivatePcmScheduleLedgerError(
                    "installed private artifact bytes do not reconcile"
                )
            for path in installed:
                if path.stat().st_mode & 0o777 != 0o600:
                    raise PrivatePcmScheduleLedgerError(
                        "installed private artifact mode is not 0600"
                    )
            if directory.stat().st_mode & 0o777 != 0o700:
                raise PrivatePcmScheduleLedgerError(
                    "private artifact directory mode is not 0700"
                )
        except Exception:
            for path in installed:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass
            raise

        self._artifacts_written = True
        return PrivatePcmScheduleArtifacts(
            directory=directory,
            source_wav=source_path,
            translated_wav=translated_path,
            ledger_json=ledger_path,
            ledger_sha256=_sha256(self._ledger_bytes),
        )

    def abort(self) -> None:
        """Discard retained private bytes and permanently close the capture."""

        if self._artifacts_written:
            raise PrivatePcmScheduleLedgerError(
                "installed private artifacts cannot be aborted"
            )
        if self._source_pcm is not None:
            self._source_pcm.clear()
        self._translated_pcm.clear()
        self._source_wav_bytes = None
        self._translated_wav_bytes = None
        self._ledger_bytes = None
        self._ledger = None
        self._source_chunks.clear()
        self._frames.clear()
        self._parents.clear()
        self._aborted = True
        self._sealed = False


def _new_private_directory(path: Path) -> Path:
    """Create a fresh 0700 directory tree without following symlinks."""

    if not isinstance(path, Path):
        raise TypeError("artifact_dir must be a pathlib.Path")
    path = path.expanduser()
    if path.is_symlink():
        raise PrivatePcmScheduleLedgerError(
            "artifact directory must not be a symbolic link"
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if len(parts) < 2:
        raise PrivatePcmScheduleLedgerError(
            "artifact directory must not be the filesystem root"
        )
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
                raise PrivatePcmScheduleLedgerError(
                    "artifact path traverses a symbolic link or non-directory"
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


def _write_private_bytes_atomic(path: Path, payload: bytes) -> None:
    """Publish one 0600 file without replacing a concurrent destination."""

    if not isinstance(payload, bytes):
        raise TypeError("private artifact payload must be bytes")
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=".tmp-",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    os.fchmod(descriptor, 0o600)
    destination_created = False
    completed = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        destination_created = True
        os.chmod(path, 0o600)
        temporary.unlink()
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        completed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
        if destination_created and not completed:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
