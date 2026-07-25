#!/usr/bin/env python3
"""Bound semantic-event latency from a protocol-v1 TestDashboard capture.

The analyzer deliberately does not inspect transcripts or translated text.  A
reviewed source-audio marker sidecar identifies the source sample for each
event. SHA-256 digests bind the sidecar to one exact CSV capture and its exact
padded Int16 input PCM wire image.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


MARKER_SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1
DEFAULT_MINIMUM_REVIEWERS = 2
INPUT_EMISSION_EARLY_TOLERANCE_MS = 1.0
INPUT_PACING_MODE = "chunk_end_boundary_v1"
OUTPUT_SAMPLE_RATE_HZ = 16_000
OUTPUT_BYTES_PER_SAMPLE = 2
GATE_EXIT_CODES = {
    "pass": 0,
    "fail": 1,
    "inconclusive": 3,
}
CSV_RECONCILIATION_TOLERANCE_MS = 0.01
CSV_RECONCILIATION_TOLERANCE_SEC = 0.00001
PLAYBACK_CLOCK_LINK_TOLERANCE_MS = 25.0
PLAYBACK_CLOCK_OFFSET_SPAN_LIMIT_MS = 50.0
EVENT_ID_PATTERN = re.compile(r"event-[0-9]{3}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

MARKER_DOCUMENT_KEYS = {
    "schema_version",
    "capture_csv_sha256",
    "source_sample_rate_hz",
    "source_pcm_sha256",
    "source_pcm_sample_count",
    "markers",
}
MARKER_KEYS = {
    "event_id",
    "source_sample_index",
    "independent_reviewer_count",
}
REQUIRED_CSV_COLUMNS = {
    "source",
    "stage",
    "chunk_index",
    "audio_bytes",
    "media_duration_sec",
    "scheduled_duration_sec",
    "playback_rate",
    "audio_metadata_protocol_version",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "audio_frame_count",
    "source_start_ms",
    "source_end_ms",
    "source_timing_basis",
    "binary_receipt_client_ms",
    "parent_complete_received_client_ms",
    "input_sample_zero_client_ms",
    "input_chunk_emitted_client_ms",
    "input_source_sample_start",
    "input_source_sample_end_exclusive",
    "input_sample_rate_hz",
    "input_pcm_sha256",
    "input_pcm_sample_count",
    "input_ledger_valid",
    "source_end_boundary_client_ms",
    "source_end_to_binary_receipt_ms",
    "source_end_to_parent_complete_ms",
    "schedule_performance_client_ms",
    "audio_context_time_at_schedule_sec",
    "scheduled_start_context_sec",
    "scheduled_end_context_sec",
    "projected_scheduled_start_client_ms",
    "source_end_to_projected_scheduled_start_ms",
}
RELEVANT_STAGES = {
    "chunk_sent",
    "audio_received",
    "playback_chunk_scheduled",
    "audio_parent_complete",
}


class SemanticEventLatencyError(ValueError):
    """Raised when evidence is incomplete, corrupt, or ambiguous."""


@dataclass(frozen=True)
class Marker:
    event_id: str
    source_sample_index: int
    independent_reviewer_count: int


@dataclass(frozen=True)
class InputLedger:
    sample_rate_hz: int
    sample_zero_client_ms: float
    source_pcm_sha256: str
    source_pcm_sample_count: int
    final_sample_end_exclusive: int
    chunk_count: int


@dataclass(frozen=True)
class Frame:
    generation: int
    parent_sequence_id: int
    audio_frame_id: int
    audio_bytes: int
    source_start_ms: float
    source_end_ms: float
    binary_receipt_client_ms: float
    schedule_performance_client_ms: float
    audio_context_time_at_schedule_sec: float
    scheduled_start_context_sec: float
    scheduled_end_context_sec: float
    projected_start_client_ms: float
    scheduled_duration_sec: float
    schedule_csv_line: int

    @property
    def key(self) -> tuple[int, int, int]:
        return (
            self.generation,
            self.parent_sequence_id,
            self.audio_frame_id,
        )

    @property
    def source_range(self) -> tuple[float, float]:
        return (self.source_start_ms, self.source_end_ms)

    @property
    def projected_end_client_ms(self) -> float:
        return (
            self.projected_start_client_ms
            + self.scheduled_duration_sec * 1000.0
        )

    @property
    def audio_context_wait_ms(self) -> float:
        return (
            self.scheduled_start_context_sec
            - self.audio_context_time_at_schedule_sec
        ) * 1000.0

    @property
    def projected_wait_ms(self) -> float:
        return (
            self.projected_start_client_ms
            - self.schedule_performance_client_ms
        )

    @property
    def clock_link_residual_ms(self) -> float:
        return self.audio_context_wait_ms - self.projected_wait_ms

    @property
    def clock_offset_ms(self) -> float:
        return (
            self.schedule_performance_client_ms
            - self.audio_context_time_at_schedule_sec * 1000.0
        )


@dataclass(frozen=True)
class ParentComplete:
    generation: int
    parent_sequence_id: int
    audio_frame_count: int
    audio_bytes: int
    source_start_ms: float
    source_end_ms: float
    received_client_ms: float
    csv_line: int

    @property
    def key(self) -> tuple[int, int]:
        return (self.generation, self.parent_sequence_id)

    @property
    def source_range(self) -> tuple[float, float]:
        return (self.source_start_ms, self.source_end_ms)


@dataclass(frozen=True)
class PlaybackClockEvidence:
    maximum_absolute_link_residual_ms: float
    offset_span_ms: float


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_plain_int(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticEventLatencyError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise SemanticEventLatencyError(
            f"{label} must be at least {minimum}"
        )
    return value


def _csv_int(
    row: dict[str, str],
    key: str,
    context: str,
    *,
    minimum: int | None = None,
) -> int:
    raw = row.get(key, "")
    if raw == "" or not re.fullmatch(r"-?[0-9]+", raw):
        raise SemanticEventLatencyError(
            f"{context} has invalid or missing {key}"
        )
    value = int(raw)
    if minimum is not None and value < minimum:
        raise SemanticEventLatencyError(
            f"{context} has invalid {key}"
        )
    return value


def _csv_float(
    row: dict[str, str],
    key: str,
    context: str,
    *,
    minimum: float | None = None,
    strictly_positive: bool = False,
) -> float:
    raw = row.get(key, "")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SemanticEventLatencyError(
            f"{context} has invalid or missing {key}"
        ) from exc
    if not math.isfinite(value):
        raise SemanticEventLatencyError(
            f"{context} has non-finite {key}"
        )
    if minimum is not None and value < minimum:
        raise SemanticEventLatencyError(
            f"{context} has invalid {key}"
        )
    if strictly_positive and value <= 0:
        raise SemanticEventLatencyError(
            f"{context} has invalid {key}"
        )
    return value


def _validate_exact_keys(
    value: dict[str, Any],
    expected: set[str],
    context: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing_count = len(expected - actual)
        extra_count = len(actual - expected)
        raise SemanticEventLatencyError(
            f"{context} must use the exact schema "
            f"(missing fields: {missing_count}; unexpected fields: "
            f"{extra_count})"
        )


def _require_close(
    actual: float,
    expected: float,
    context: str,
    label: str,
    *,
    tolerance: float,
) -> None:
    if not math.isclose(
        actual,
        expected,
        rel_tol=0.0,
        abs_tol=tolerance,
    ):
        raise SemanticEventLatencyError(
            f"{context} has inconsistent {label}"
        )


def _load_marker_document(
    marker_bytes: bytes,
    *,
    capture_csv_sha256: str,
    minimum_reviewers: int,
) -> tuple[int, str, int, tuple[Marker, ...]]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise SemanticEventLatencyError(
                    "marker sidecar contains a duplicate object key"
                )
            value[key] = item
        return value

    def reject_nonstandard_number(value: str) -> None:
        raise SemanticEventLatencyError(
            f"marker sidecar contains non-standard number {value}"
        )

    try:
        marker_text = marker_bytes.decode("utf-8-sig")
        document = json.loads(
            marker_text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonstandard_number,
        )
    except SemanticEventLatencyError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticEventLatencyError(
            "marker sidecar is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(document, dict):
        raise SemanticEventLatencyError(
            "marker sidecar must be a JSON object"
        )
    _validate_exact_keys(
        document,
        MARKER_DOCUMENT_KEYS,
        "marker sidecar",
    )

    version = _require_plain_int(
        document["schema_version"],
        "schema_version",
    )
    if version != MARKER_SCHEMA_VERSION:
        raise SemanticEventLatencyError(
            f"schema_version must be {MARKER_SCHEMA_VERSION}"
        )

    declared_hash = document["capture_csv_sha256"]
    if (
        not isinstance(declared_hash, str)
        or SHA256_PATTERN.fullmatch(declared_hash) is None
    ):
        raise SemanticEventLatencyError(
            "capture_csv_sha256 must be 64 lowercase hexadecimal characters"
        )
    if declared_hash != capture_csv_sha256:
        raise SemanticEventLatencyError(
            "capture_csv_sha256 does not match the CSV bytes"
        )

    sample_rate_hz = _require_plain_int(
        document["source_sample_rate_hz"],
        "source_sample_rate_hz",
        minimum=1,
    )
    source_pcm_sha256 = document["source_pcm_sha256"]
    if (
        not isinstance(source_pcm_sha256, str)
        or SHA256_PATTERN.fullmatch(source_pcm_sha256) is None
    ):
        raise SemanticEventLatencyError(
            "source_pcm_sha256 must be 64 lowercase hexadecimal characters"
        )
    source_pcm_sample_count = _require_plain_int(
        document["source_pcm_sample_count"],
        "source_pcm_sample_count",
        minimum=1,
    )
    raw_markers = document["markers"]
    if not isinstance(raw_markers, list) or not raw_markers:
        raise SemanticEventLatencyError(
            "markers must be a non-empty array"
        )

    markers: list[Marker] = []
    seen_event_ids: set[str] = set()
    seen_source_sample_indices: set[int] = set()
    for index, raw_marker in enumerate(raw_markers):
        context = f"markers[{index}]"
        if not isinstance(raw_marker, dict):
            raise SemanticEventLatencyError(
                f"{context} must be an object"
            )
        _validate_exact_keys(raw_marker, MARKER_KEYS, context)
        event_id = raw_marker["event_id"]
        if (
            not isinstance(event_id, str)
            or EVENT_ID_PATTERN.fullmatch(event_id) is None
        ):
            raise SemanticEventLatencyError(
                f"{context}.event_id must match event-NNN"
            )
        if event_id in seen_event_ids:
            raise SemanticEventLatencyError(
                f"duplicate event_id {event_id}"
            )
        seen_event_ids.add(event_id)
        source_sample_index = _require_plain_int(
            raw_marker["source_sample_index"],
            f"{context}.source_sample_index",
            minimum=0,
        )
        if source_sample_index in seen_source_sample_indices:
            raise SemanticEventLatencyError(
                f"duplicate source_sample_index {source_sample_index}"
            )
        seen_source_sample_indices.add(source_sample_index)
        reviewer_count = _require_plain_int(
            raw_marker["independent_reviewer_count"],
            f"{context}.independent_reviewer_count",
            minimum=1,
        )
        if reviewer_count < minimum_reviewers:
            raise SemanticEventLatencyError(
                f"{event_id} has fewer than {minimum_reviewers} "
                "independent reviewers"
            )
        markers.append(
            Marker(
                event_id=event_id,
                source_sample_index=source_sample_index,
                independent_reviewer_count=reviewer_count,
            )
        )
    markers.sort(key=lambda marker: (marker.source_sample_index, marker.event_id))
    return (
        sample_rate_hz,
        source_pcm_sha256,
        source_pcm_sample_count,
        tuple(markers),
    )


def _read_csv_rows(csv_bytes: bytes) -> list[dict[str, str]]:
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SemanticEventLatencyError(
            "results CSV is not valid UTF-8"
        ) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise SemanticEventLatencyError("results CSV has no header")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise SemanticEventLatencyError(
            "results CSV has duplicate column names"
        )
    missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames)
    if missing:
        raise SemanticEventLatencyError(
            "results CSV is missing required columns: "
            + ", ".join(sorted(missing))
        )
    rows: list[dict[str, str]] = []
    for line_number, row in enumerate(reader, start=2):
        if None in row:
            raise SemanticEventLatencyError(
                f"results CSV row {line_number} has extra fields"
            )
        normalized = {
            key: "" if value is None else value
            for key, value in row.items()
        }
        normalized["_line_number"] = str(line_number)
        rows.append(normalized)
    if not rows:
        raise SemanticEventLatencyError("results CSV has no data rows")
    return rows


def _context(row: dict[str, str]) -> str:
    return (
        f"CSV row {row['_line_number']} "
        f"({row.get('stage') or 'unknown stage'})"
    )


def _validate_input_ledger(
    rows: list[dict[str, str]],
    *,
    declared_sample_rate_hz: int,
    declared_source_pcm_sha256: str,
    declared_source_pcm_sample_count: int,
) -> InputLedger:
    ledger_rows = [
        row
        for row in rows
        if row["source"] == "client" and row["stage"] == "chunk_sent"
    ]
    if not ledger_rows:
        raise SemanticEventLatencyError(
            "input ledger has no client chunk_sent rows"
        )

    parsed: list[
        tuple[int, int, int, int, float, float, str, int]
    ] = []
    for row in ledger_rows:
        context = _context(row)
        if row["input_ledger_valid"] != "true":
            raise SemanticEventLatencyError(
                f"{context} marks the input ledger invalid"
            )
        chunk_index = _csv_int(
            row, "chunk_index", context, minimum=0
        )
        start = _csv_int(
            row, "input_source_sample_start", context, minimum=0
        )
        end = _csv_int(
            row,
            "input_source_sample_end_exclusive",
            context,
            minimum=1,
        )
        if end <= start:
            raise SemanticEventLatencyError(
                f"{context} has an empty or reversed input sample range"
            )
        sample_rate = _csv_int(
            row, "input_sample_rate_hz", context, minimum=1
        )
        if sample_rate != declared_sample_rate_hz:
            raise SemanticEventLatencyError(
                f"{context} input sample rate does not match the sidecar"
            )
        source_pcm_sha256 = row["input_pcm_sha256"]
        if SHA256_PATTERN.fullmatch(source_pcm_sha256) is None:
            raise SemanticEventLatencyError(
                f"{context} has invalid input_pcm_sha256"
            )
        if source_pcm_sha256 != declared_source_pcm_sha256:
            raise SemanticEventLatencyError(
                f"{context} input PCM digest does not match the sidecar"
            )
        source_pcm_sample_count = _csv_int(
            row,
            "input_pcm_sample_count",
            context,
            minimum=1,
        )
        if source_pcm_sample_count != declared_source_pcm_sample_count:
            raise SemanticEventLatencyError(
                f"{context} input PCM sample count does not match the sidecar"
            )
        if end > source_pcm_sample_count:
            raise SemanticEventLatencyError(
                f"{context} extends beyond the declared input PCM"
            )
        audio_bytes = _csv_int(
            row, "audio_bytes", context, minimum=1
        )
        if audio_bytes != (end - start) * 2:
            raise SemanticEventLatencyError(
                f"{context} audio byte count does not match its PCM ledger"
            )
        anchor = _csv_float(
            row,
            "input_sample_zero_client_ms",
            context,
            minimum=0,
        )
        emitted = _csv_float(
            row,
            "input_chunk_emitted_client_ms",
            context,
            minimum=0,
        )
        parsed.append(
            (
                chunk_index,
                start,
                end,
                sample_rate,
                anchor,
                emitted,
                source_pcm_sha256,
                source_pcm_sample_count,
            )
        )

    parsed.sort(key=lambda item: item[0])
    expected_start = 0
    anchor = parsed[0][4]
    previous_emitted = -math.inf
    for expected_index, (
        chunk_index,
        start,
        end,
        _sample_rate,
        row_anchor,
        emitted,
        row_pcm_sha256,
        row_pcm_sample_count,
    ) in enumerate(parsed):
        if chunk_index != expected_index:
            raise SemanticEventLatencyError(
                "input ledger chunk indices are duplicated or fragmented"
            )
        if start != expected_start:
            raise SemanticEventLatencyError(
                "input ledger sample ranges are duplicated or fragmented"
            )
        if row_anchor != anchor:
            raise SemanticEventLatencyError(
                "input ledger uses inconsistent sample-zero anchors"
            )
        if row_pcm_sha256 != declared_source_pcm_sha256:
            raise SemanticEventLatencyError(
                "input ledger uses inconsistent PCM digests"
            )
        if row_pcm_sample_count != declared_source_pcm_sample_count:
            raise SemanticEventLatencyError(
                "input ledger uses inconsistent PCM sample counts"
            )
        if emitted < previous_emitted:
            raise SemanticEventLatencyError(
                "input ledger emission timestamps are not monotonic"
            )
        source_end_boundary_client_ms = (
            anchor + end / declared_sample_rate_hz * 1000.0
        )
        if (
            emitted + INPUT_EMISSION_EARLY_TOLERANCE_MS
            < source_end_boundary_client_ms
        ):
            raise SemanticEventLatencyError(
                "input ledger violates chunk-end boundary pacing"
            )
        expected_start = end
        previous_emitted = emitted

    if expected_start != declared_source_pcm_sample_count:
        raise SemanticEventLatencyError(
            "input ledger final sample does not match the declared input PCM"
        )

    return InputLedger(
        sample_rate_hz=declared_sample_rate_hz,
        sample_zero_client_ms=anchor,
        source_pcm_sha256=declared_source_pcm_sha256,
        source_pcm_sample_count=declared_source_pcm_sample_count,
        final_sample_end_exclusive=expected_start,
        chunk_count=len(parsed),
    )


def _protocol_fields(
    row: dict[str, str],
) -> tuple[int, int, int, tuple[float, float]]:
    context = _context(row)
    protocol = _csv_int(
        row,
        "audio_metadata_protocol_version",
        context,
        minimum=1,
    )
    if protocol != PROTOCOL_VERSION:
        raise SemanticEventLatencyError(
            f"{context} uses protocol version {protocol}, expected 1"
        )
    generation = _csv_int(
        row, "stream_generation", context, minimum=1
    )
    parent_id = _csv_int(
        row, "parent_sequence_id", context, minimum=0
    )
    if row["source_timing_basis"] != "attributed_range":
        raise SemanticEventLatencyError(
            f"{context} lacks an attributed source range"
        )
    source_start = _csv_float(
        row, "source_start_ms", context, minimum=0
    )
    source_end = _csv_float(
        row, "source_end_ms", context, minimum=0
    )
    if source_end < source_start:
        raise SemanticEventLatencyError(
            f"{context} has a reversed source range"
        )
    return protocol, generation, parent_id, (source_start, source_end)


def _source_boundary_fields(
    row: dict[str, str],
    *,
    source_end_ms: float,
    ledger: InputLedger,
) -> float:
    context = _context(row)
    if row["input_ledger_valid"] != "true":
        raise SemanticEventLatencyError(
            f"{context} lacks valid input-ledger provenance"
        )
    anchor = _csv_float(
        row,
        "input_sample_zero_client_ms",
        context,
        minimum=0,
    )
    _require_close(
        anchor,
        ledger.sample_zero_client_ms,
        context,
        "input sample-zero anchor",
        tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
    )
    boundary = _csv_float(
        row,
        "source_end_boundary_client_ms",
        context,
        minimum=0,
    )
    _require_close(
        boundary,
        ledger.sample_zero_client_ms + source_end_ms,
        context,
        "source-end boundary",
        tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
    )
    sent_duration_ms = (
        ledger.final_sample_end_exclusive
        / ledger.sample_rate_hz
        * 1000.0
    )
    if source_end_ms > (
        sent_duration_ms + CSV_RECONCILIATION_TOLERANCE_MS
    ):
        raise SemanticEventLatencyError(
            f"{context} source range extends beyond the input ledger"
        )
    return boundary


def _validate_protocol_trace(
    rows: list[dict[str, str]],
    ledger: InputLedger,
) -> tuple[
    int,
    dict[tuple[int, int, int], Frame],
    dict[tuple[int, int], ParentComplete],
    PlaybackClockEvidence,
]:
    client_rows = [
        row
        for row in rows
        if row["source"] == "client" and row["stage"] in RELEVANT_STAGES
    ]
    receive_rows = [
        row for row in client_rows if row["stage"] == "audio_received"
    ]
    schedule_rows = [
        row
        for row in client_rows
        if row["stage"] == "playback_chunk_scheduled"
    ]
    complete_rows = [
        row
        for row in client_rows
        if row["stage"] == "audio_parent_complete"
    ]
    if not receive_rows or not schedule_rows or not complete_rows:
        raise SemanticEventLatencyError(
            "protocol trace is missing receive, schedule, or parent-complete "
            "rows"
        )

    receives: dict[tuple[int, int, int], dict[str, Any]] = {}
    observed_generations: set[int] = set()
    for row in receive_rows:
        context = _context(row)
        _protocol, generation, parent_id, source_range = _protocol_fields(
            row
        )
        source_boundary = _source_boundary_fields(
            row,
            source_end_ms=source_range[1],
            ledger=ledger,
        )
        frame_id = _csv_int(
            row, "audio_frame_id", context, minimum=0
        )
        key = (generation, parent_id, frame_id)
        if key in receives:
            raise SemanticEventLatencyError(
                f"duplicate audio_received row for frame {key}"
            )
        observed_generations.add(generation)
        binary_receipt = _csv_float(
            row,
            "binary_receipt_client_ms",
            context,
            minimum=0,
        )
        if (
            binary_receipt + INPUT_EMISSION_EARLY_TOLERANCE_MS
            < source_boundary
        ):
            raise SemanticEventLatencyError(
                f"{context} receives output before its source-end boundary"
            )
        source_end_to_receipt = _csv_float(
            row,
            "source_end_to_binary_receipt_ms",
            context,
        )
        _require_close(
            source_end_to_receipt,
            binary_receipt - source_boundary,
            context,
            "source-end-to-receipt latency",
            tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
        )
        receives[key] = {
            "audio_bytes": _csv_int(
                row, "audio_bytes", context, minimum=1
            ),
            "source_range": source_range,
            "source_timing_basis": row["source_timing_basis"],
            "binary_receipt_client_ms": binary_receipt,
            "source_end_boundary_client_ms": source_boundary,
            "csv_line": int(row["_line_number"]),
        }

    schedules: dict[tuple[int, int, int], Frame] = {}
    for row in schedule_rows:
        context = _context(row)
        _protocol, generation, parent_id, source_range = _protocol_fields(
            row
        )
        source_boundary = _source_boundary_fields(
            row,
            source_end_ms=source_range[1],
            ledger=ledger,
        )
        frame_id = _csv_int(
            row, "audio_frame_id", context, minimum=0
        )
        key = (generation, parent_id, frame_id)
        if key in schedules:
            raise SemanticEventLatencyError(
                f"duplicate playback schedule row for frame {key}"
            )
        observed_generations.add(generation)
        binary_receipt = _csv_float(
            row,
            "binary_receipt_client_ms",
            context,
            minimum=0,
        )
        if (
            binary_receipt + INPUT_EMISSION_EARLY_TOLERANCE_MS
            < source_boundary
        ):
            raise SemanticEventLatencyError(
                f"{context} receives output before its source-end boundary"
            )
        source_end_to_receipt = _csv_float(
            row,
            "source_end_to_binary_receipt_ms",
            context,
        )
        _require_close(
            source_end_to_receipt,
            binary_receipt - source_boundary,
            context,
            "source-end-to-receipt latency",
            tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
        )
        schedule_performance = _csv_float(
            row,
            "schedule_performance_client_ms",
            context,
            minimum=0,
        )
        audio_context_time = _csv_float(
            row,
            "audio_context_time_at_schedule_sec",
            context,
            minimum=0,
        )
        scheduled_start_context = _csv_float(
            row,
            "scheduled_start_context_sec",
            context,
            minimum=0,
        )
        scheduled_end_context = _csv_float(
            row,
            "scheduled_end_context_sec",
            context,
            minimum=0,
        )
        scheduled_duration = _csv_float(
            row,
            "scheduled_duration_sec",
            context,
            strictly_positive=True,
        )
        media_duration = _csv_float(
            row,
            "media_duration_sec",
            context,
            strictly_positive=True,
        )
        playback_rate = _csv_float(
            row,
            "playback_rate",
            context,
            strictly_positive=True,
        )
        audio_bytes = _csv_int(
            row, "audio_bytes", context, minimum=1
        )
        _require_close(
            media_duration,
            audio_bytes
            / (OUTPUT_SAMPLE_RATE_HZ * OUTPUT_BYTES_PER_SAMPLE),
            context,
            "media duration/output PCM byte count",
            tolerance=CSV_RECONCILIATION_TOLERANCE_SEC,
        )
        if (
            scheduled_start_context
            + CSV_RECONCILIATION_TOLERANCE_SEC
            < audio_context_time
        ):
            raise SemanticEventLatencyError(
                f"{context} schedules playback before AudioContext time"
            )
        _require_close(
            scheduled_end_context - scheduled_start_context,
            scheduled_duration,
            context,
            "scheduled context duration",
            tolerance=CSV_RECONCILIATION_TOLERANCE_SEC,
        )
        _require_close(
            media_duration / playback_rate,
            scheduled_duration,
            context,
            "media duration/playback rate",
            tolerance=CSV_RECONCILIATION_TOLERANCE_SEC,
        )
        projected_start = _csv_float(
            row,
            "projected_scheduled_start_client_ms",
            context,
            minimum=0,
        )
        source_end_to_projected_start = _csv_float(
            row,
            "source_end_to_projected_scheduled_start_ms",
            context,
        )
        _require_close(
            source_end_to_projected_start,
            projected_start - source_boundary,
            context,
            "source-end-to-projected-start latency",
            tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
        )
        schedules[key] = Frame(
            generation=generation,
            parent_sequence_id=parent_id,
            audio_frame_id=frame_id,
            audio_bytes=audio_bytes,
            source_start_ms=source_range[0],
            source_end_ms=source_range[1],
            binary_receipt_client_ms=binary_receipt,
            schedule_performance_client_ms=schedule_performance,
            audio_context_time_at_schedule_sec=audio_context_time,
            scheduled_start_context_sec=scheduled_start_context,
            scheduled_end_context_sec=scheduled_end_context,
            projected_start_client_ms=projected_start,
            scheduled_duration_sec=scheduled_duration,
            schedule_csv_line=int(row["_line_number"]),
        )

    if len(observed_generations) != 1:
        raise SemanticEventLatencyError(
            "protocol trace contains a stream-generation change"
        )
    if set(receives) != set(schedules):
        raise SemanticEventLatencyError(
            "audio_received and playback schedule frame keys do not match"
        )
    for key, frame in schedules.items():
        received = receives[key]
        if frame.audio_bytes != received["audio_bytes"]:
            raise SemanticEventLatencyError(
                f"receive/schedule audio bytes mismatch for frame {key}"
            )
        if frame.source_range != received["source_range"]:
            raise SemanticEventLatencyError(
                f"receive/schedule source range mismatch for frame {key}"
            )
        if (
            frame.binary_receipt_client_ms
            != received["binary_receipt_client_ms"]
        ):
            raise SemanticEventLatencyError(
                f"receive/schedule receipt timestamp mismatch for frame {key}"
            )
        _require_close(
            received["source_end_boundary_client_ms"],
            frame.source_end_ms + ledger.sample_zero_client_ms,
            f"frame {key}",
            "receive/schedule source-end boundary",
            tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
        )
        if frame.schedule_performance_client_ms < (
            frame.binary_receipt_client_ms
        ):
            raise SemanticEventLatencyError(
                f"frame {key} was scheduled before its receipt"
            )
        if frame.projected_start_client_ms < (
            frame.schedule_performance_client_ms
        ):
            raise SemanticEventLatencyError(
                f"frame {key} projects playback before scheduling"
            )

    completions: dict[tuple[int, int], ParentComplete] = {}
    for row in complete_rows:
        context = _context(row)
        _protocol, generation, parent_id, source_range = _protocol_fields(
            row
        )
        source_boundary = _source_boundary_fields(
            row,
            source_end_ms=source_range[1],
            ledger=ledger,
        )
        key = (generation, parent_id)
        if key in completions:
            raise SemanticEventLatencyError(
                f"duplicate parent-complete row for parent {key}"
            )
        observed_generations.add(generation)
        complete_receipt = _csv_float(
            row,
            "parent_complete_received_client_ms",
            context,
            minimum=0,
        )
        if (
            complete_receipt + INPUT_EMISSION_EARLY_TOLERANCE_MS
            < source_boundary
        ):
            raise SemanticEventLatencyError(
                f"{context} completes before its source-end boundary"
            )
        source_end_to_complete = _csv_float(
            row,
            "source_end_to_parent_complete_ms",
            context,
        )
        _require_close(
            source_end_to_complete,
            complete_receipt - source_boundary,
            context,
            "source-end-to-parent-complete latency",
            tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
        )
        completions[key] = ParentComplete(
            generation=generation,
            parent_sequence_id=parent_id,
            audio_frame_count=_csv_int(
                row, "audio_frame_count", context, minimum=1
            ),
            audio_bytes=_csv_int(
                row, "audio_bytes", context, minimum=1
            ),
            source_start_ms=source_range[0],
            source_end_ms=source_range[1],
            received_client_ms=complete_receipt,
            csv_line=int(row["_line_number"]),
        )

    if len(observed_generations) != 1:
        raise SemanticEventLatencyError(
            "protocol trace contains a stream-generation change"
        )
    generation = next(iter(observed_generations))

    frame_parent_keys = {
        (frame.generation, frame.parent_sequence_id)
        for frame in schedules.values()
    }
    if frame_parent_keys != set(completions):
        raise SemanticEventLatencyError(
            "frame and parent-complete parent keys do not match"
        )

    parent_ids = sorted(parent_id for _, parent_id in frame_parent_keys)
    if parent_ids != list(range(len(parent_ids))):
        raise SemanticEventLatencyError(
            "parent sequence IDs are duplicated or fragmented"
        )

    for parent_key in sorted(frame_parent_keys):
        frames = sorted(
            (
                frame
                for frame in schedules.values()
                if (
                    frame.generation,
                    frame.parent_sequence_id,
                )
                == parent_key
            ),
            key=lambda frame: frame.audio_frame_id,
        )
        frame_ids = [frame.audio_frame_id for frame in frames]
        if frame_ids != list(range(len(frames))):
            raise SemanticEventLatencyError(
                f"parent {parent_key} has fragmented frame IDs"
            )
        complete = completions[parent_key]
        if complete.audio_frame_count != len(frames):
            raise SemanticEventLatencyError(
                f"parent {parent_key} frame count does not reconcile"
            )
        if complete.audio_bytes != sum(frame.audio_bytes for frame in frames):
            raise SemanticEventLatencyError(
                f"parent {parent_key} audio bytes do not reconcile"
            )
        if any(frame.source_range != complete.source_range for frame in frames):
            raise SemanticEventLatencyError(
                f"parent {parent_key} source ranges do not reconcile"
            )
        if complete.received_client_ms < max(
            frame.binary_receipt_client_ms for frame in frames
        ):
            raise SemanticEventLatencyError(
                f"parent {parent_key} completed before its final frame"
            )
        if complete.received_client_ms < max(
            frame.schedule_performance_client_ms for frame in frames
        ):
            raise SemanticEventLatencyError(
                f"parent {parent_key} completed before its final schedule"
            )
        previous_receipt = -math.inf
        previous_schedule = -math.inf
        previous_start = -math.inf
        for frame in frames:
            if frame.binary_receipt_client_ms < previous_receipt:
                raise SemanticEventLatencyError(
                    f"parent {parent_key} has non-monotonic frame receipts"
                )
            if frame.schedule_performance_client_ms < previous_schedule:
                raise SemanticEventLatencyError(
                    f"parent {parent_key} has non-monotonic schedule times"
                )
            if frame.projected_start_client_ms < previous_start:
                raise SemanticEventLatencyError(
                    f"parent {parent_key} has non-monotonic playback starts"
                )
            previous_receipt = frame.binary_receipt_client_ms
            previous_schedule = frame.schedule_performance_client_ms
            previous_start = frame.projected_start_client_ms

    ordered_frames = sorted(
        schedules.values(),
        key=lambda frame: (
            frame.parent_sequence_id,
            frame.audio_frame_id,
        ),
    )
    expected_csv_lines: list[int] = []
    protocol_clock: list[float] = []
    clock_offsets_ms: list[float] = []
    absolute_clock_link_residuals_ms: list[float] = []
    previous_context_end: float | None = None
    previous_projected_end: float | None = None
    frame_index = 0
    for parent_id in parent_ids:
        parent_frames: list[Frame] = []
        while (
            frame_index < len(ordered_frames)
            and ordered_frames[frame_index].parent_sequence_id == parent_id
        ):
            parent_frames.append(ordered_frames[frame_index])
            frame_index += 1
        for frame in parent_frames:
            received = receives[frame.key]
            expected_csv_lines.extend(
                [received["csv_line"], frame.schedule_csv_line]
            )
            protocol_clock.extend(
                [
                    frame.binary_receipt_client_ms,
                    frame.schedule_performance_client_ms,
                ]
            )
            expected_context_start = (
                frame.audio_context_time_at_schedule_sec
                if previous_context_end is None
                else max(
                    frame.audio_context_time_at_schedule_sec,
                    previous_context_end,
                )
            )
            _require_close(
                frame.scheduled_start_context_sec,
                expected_context_start,
                f"frame {frame.key}",
                "global scheduled context start recurrence",
                tolerance=CSV_RECONCILIATION_TOLERANCE_SEC,
            )
            expected_projected_start = (
                frame.schedule_performance_client_ms
                if previous_projected_end is None
                else max(
                    frame.schedule_performance_client_ms,
                    previous_projected_end,
                )
            )
            _require_close(
                frame.projected_start_client_ms,
                expected_projected_start,
                f"frame {frame.key}",
                "global projected start recurrence",
                tolerance=CSV_RECONCILIATION_TOLERANCE_MS,
            )
            _require_close(
                frame.audio_context_wait_ms,
                frame.projected_wait_ms,
                f"frame {frame.key}",
                "client/AudioContext playback-wait linkage",
                tolerance=PLAYBACK_CLOCK_LINK_TOLERANCE_MS,
            )
            clock_offsets_ms.append(frame.clock_offset_ms)
            absolute_clock_link_residuals_ms.append(
                abs(frame.clock_link_residual_ms)
            )
            previous_context_end = frame.scheduled_end_context_sec
            previous_projected_end = frame.projected_end_client_ms
        complete = completions[(generation, parent_id)]
        expected_csv_lines.append(complete.csv_line)
        protocol_clock.append(complete.received_client_ms)

    if expected_csv_lines != sorted(expected_csv_lines):
        raise SemanticEventLatencyError(
            "protocol rows are not in parent/frame wire order"
        )
    if any(
        later + CSV_RECONCILIATION_TOLERANCE_MS < earlier
        for earlier, later in zip(protocol_clock, protocol_clock[1:])
    ):
        raise SemanticEventLatencyError(
            "protocol timestamps are not in parent/frame wire order"
        )

    _reject_non_monotonic_parent_source_ranges(completions)
    _reject_unsupported_distinct_range_overlaps(completions)
    clock_offset_span_ms = max(clock_offsets_ms) - min(clock_offsets_ms)
    if (
        clock_offset_span_ms
        > PLAYBACK_CLOCK_OFFSET_SPAN_LIMIT_MS
        + CSV_RECONCILIATION_TOLERANCE_MS
    ):
        raise SemanticEventLatencyError(
            "protocol trace client/AudioContext clock offset span exceeds "
            "the capture limit"
        )
    return (
        generation,
        schedules,
        completions,
        PlaybackClockEvidence(
            maximum_absolute_link_residual_ms=max(
                absolute_clock_link_residuals_ms
            ),
            offset_span_ms=clock_offset_span_ms,
        ),
    )


def _reject_non_monotonic_parent_source_ranges(
    completions: dict[tuple[int, int], ParentComplete],
) -> None:
    """Require source chronology to advance with parent sequence order.

    Equal ranges remain valid because punctuation splitting may emit sibling
    parents attributed to the same ASR-final source span.
    """

    previous_range: tuple[float, float] | None = None
    for parent_key in sorted(completions):
        source_range = completions[parent_key].source_range
        if (
            previous_range is not None
            and (
                source_range[0] < previous_range[0]
                or source_range[1] < previous_range[1]
            )
        ):
            raise SemanticEventLatencyError(
                "protocol trace source ranges move backward in parent "
                "sequence order"
            )
        previous_range = source_range


def _reject_unsupported_distinct_range_overlaps(
    completions: dict[tuple[int, int], ParentComplete],
) -> None:
    """Allow only ordered same-end suffix nesting between distinct ranges.

    Nemotron word envelopes can legitimately produce this shape when a
    punctuation cut consumes part of a later ASR final: the emitted aggregate
    uses the earlier start while the residual keeps the later final's narrower
    range. Exact-range siblings are also valid. All other distinct overlaps
    remain invalid evidence.

    The preceding monotonicity check guarantees nondecreasing starts and ends,
    so comparing adjacent parents is sufficient to detect every unsupported
    overlap.
    """

    previous_range: tuple[float, float] | None = None
    for parent_key in sorted(completions):
        source_range = completions[parent_key].source_range
        if previous_range is None or source_range == previous_range:
            previous_range = source_range
            continue

        overlaps = source_range[0] < previous_range[1]
        is_same_end_suffix = (
            source_range[0] > previous_range[0]
            and source_range[1] == previous_range[1]
        )
        if overlaps and not is_same_end_suffix:
            raise SemanticEventLatencyError(
                "protocol trace contains unsupported overlapping distinct "
                "source ranges"
            )
        previous_range = source_range


def _round_numbers(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_numbers(item) for item in value]
    return value


def analyze_semantic_event_latency(
    results_csv: Path | str,
    markers_json: Path | str,
    max_latency_seconds: float,
    *,
    minimum_reviewers: int = DEFAULT_MINIMUM_REVIEWERS,
) -> dict[str, Any]:
    """Validate evidence and compute conservative per-event latency bounds."""

    if isinstance(max_latency_seconds, bool):
        raise SemanticEventLatencyError(
            "max_latency_seconds must be finite and positive"
        )
    try:
        normalized_sla = float(max_latency_seconds)
    except (TypeError, ValueError) as exc:
        raise SemanticEventLatencyError(
            "max_latency_seconds must be finite and positive"
        ) from exc
    if not math.isfinite(normalized_sla) or normalized_sla <= 0:
        raise SemanticEventLatencyError(
            "max_latency_seconds must be finite and positive"
        )
    minimum_reviewers = _require_plain_int(
        minimum_reviewers,
        "minimum_reviewers",
        minimum=1,
    )

    csv_bytes = Path(results_csv).read_bytes()
    marker_bytes = Path(markers_json).read_bytes()
    csv_sha256 = _sha256(csv_bytes)
    marker_sha256 = _sha256(marker_bytes)
    (
        sample_rate_hz,
        declared_source_pcm_sha256,
        declared_source_pcm_sample_count,
        markers,
    ) = _load_marker_document(
        marker_bytes,
        capture_csv_sha256=csv_sha256,
        minimum_reviewers=minimum_reviewers,
    )
    rows = _read_csv_rows(csv_bytes)
    ledger = _validate_input_ledger(
        rows,
        declared_sample_rate_hz=sample_rate_hz,
        declared_source_pcm_sha256=declared_source_pcm_sha256,
        declared_source_pcm_sample_count=declared_source_pcm_sample_count,
    )
    (
        generation,
        frames,
        completions,
        playback_clock_evidence,
    ) = _validate_protocol_trace(rows, ledger)

    events: list[dict[str, Any]] = []
    sla_ms = normalized_sla * 1000.0
    for marker in markers:
        if marker.source_sample_index >= ledger.final_sample_end_exclusive:
            raise SemanticEventLatencyError(
                f"{marker.event_id} is beyond the sent input ledger"
            )
        source_offset_ms = (
            marker.source_sample_index / sample_rate_hz * 1000.0
        )
        source_event_client_ms = (
            ledger.sample_zero_client_ms + source_offset_ms
        )
        candidate_completions = [
            complete
            for complete in completions.values()
            if (
                complete.source_start_ms
                <= source_offset_ms
                <= complete.source_end_ms
            )
        ]
        if not candidate_completions:
            raise SemanticEventLatencyError(
                f"{marker.event_id} cannot be resolved to an attributed "
                "source range"
            )
        candidate_ranges = {
            complete.source_range for complete in candidate_completions
        }
        if len(candidate_ranges) != 1:
            raise SemanticEventLatencyError(
                f"{marker.event_id} resolves to distinct source ranges"
            )
        candidate_parent_keys = {
            complete.key for complete in candidate_completions
        }
        candidate_frames = [
            frame
            for frame in frames.values()
            if (
                frame.generation,
                frame.parent_sequence_id,
            )
            in candidate_parent_keys
        ]
        if not candidate_frames:
            raise SemanticEventLatencyError(
                f"{marker.event_id} has no candidate audio frames"
            )

        first_receipt = min(
            frame.binary_receipt_client_ms for frame in candidate_frames
        )
        last_receipt = max(
            frame.binary_receipt_client_ms for frame in candidate_frames
        )
        parent_complete_bound = max(
            complete.received_client_ms
            for complete in candidate_completions
        )
        projected_start = min(
            frame.projected_start_client_ms for frame in candidate_frames
        )
        projected_end = max(
            frame.projected_end_client_ms for frame in candidate_frames
        )
        conservative_projected_start = (
            projected_start - PLAYBACK_CLOCK_LINK_TOLERANCE_MS
        )
        conservative_projected_end = (
            projected_end + PLAYBACK_CLOCK_LINK_TOLERANCE_MS
        )
        start_delay_ms = (
            conservative_projected_start - source_event_client_ms
        )
        end_delay_ms = (
            conservative_projected_end - source_event_client_ms
        )
        if end_delay_ms <= sla_ms:
            status = "pass"
        elif start_delay_ms > sla_ms:
            status = "fail"
        else:
            status = "inconclusive"

        source_range = next(iter(candidate_ranges))
        events.append(
            {
                "event_id": marker.event_id,
                "source_sample_index": marker.source_sample_index,
                "independent_reviewer_count": (
                    marker.independent_reviewer_count
                ),
                "source_offset_ms": source_offset_ms,
                "source_event_client_ms": source_event_client_ms,
                "attributed_source_range_ms": {
                    "start": source_range[0],
                    "end": source_range[1],
                },
                "candidate_parent_sequence_ids": sorted(
                    complete.parent_sequence_id
                    for complete in candidate_completions
                ),
                "_candidate_group_key": tuple(sorted(
                    complete.parent_sequence_id
                    for complete in candidate_completions
                )),
                "candidate_frame_count": len(candidate_frames),
                "first_candidate_frame_receipt_client_ms": first_receipt,
                "last_candidate_frame_receipt_client_ms": last_receipt,
                "parent_complete_receipt_bound_client_ms": (
                    parent_complete_bound
                ),
                "projected_first_frame_start_client_ms": projected_start,
                "projected_final_frame_end_client_ms": projected_end,
                "conservative_projected_first_frame_start_client_ms": (
                    conservative_projected_start
                ),
                "conservative_projected_final_frame_end_client_ms": (
                    conservative_projected_end
                ),
                "latency_bounds_ms": {
                    "first_candidate_frame_receipt": (
                        first_receipt - source_event_client_ms
                    ),
                    "last_candidate_frame_receipt": (
                        last_receipt - source_event_client_ms
                    ),
                    "parent_complete_receipt": (
                        parent_complete_bound - source_event_client_ms
                    ),
                    "conservative_projected_first_frame_start": (
                        start_delay_ms
                    ),
                    "conservative_projected_final_frame_end": end_delay_ms,
                },
                "status": status,
                "semantic_source_marker_reviewed": True,
                "source_pcm_binding_verified": True,
                "target_landmark_proven": False,
                "actual_audibility_proven": False,
            }
        )

    candidate_group_keys = sorted({
        event["_candidate_group_key"] for event in events
    })
    candidate_group_ids = {
        key: f"group-{index:03d}"
        for index, key in enumerate(candidate_group_keys, start=1)
    }
    candidate_groups: list[dict[str, Any]] = []
    for key in candidate_group_keys:
        group_id = candidate_group_ids[key]
        marker_count = sum(
            event["_candidate_group_key"] == key for event in events
        )
        candidate_groups.append(
            {
                "candidate_group_id": group_id,
                "marker_count": marker_count,
            }
        )
    for event in events:
        key = event.pop("_candidate_group_key")
        event["candidate_group_id"] = candidate_group_ids[key]

    counts = {
        status: sum(event["status"] == status for event in events)
        for status in ("pass", "inconclusive", "fail")
    }
    if counts["fail"]:
        overall_status = "fail"
    elif counts["inconclusive"]:
        overall_status = "inconclusive"
    else:
        overall_status = "pass"

    return _round_numbers(
        {
            "schema_version": 1,
            "analysis": "semantic_event_latency_bound",
            "evidence": {
                "capture_csv_sha256": csv_sha256,
                "markers_json_sha256": marker_sha256,
                "marker_schema_version": MARKER_SCHEMA_VERSION,
                "source_sample_rate_hz": sample_rate_hz,
                "source_pcm_sha256": ledger.source_pcm_sha256,
                "source_pcm_sample_count": ledger.source_pcm_sample_count,
                "source_pcm_binding_verified": True,
                "minimum_independent_reviewers": minimum_reviewers,
                "input_ledger_chunk_count": ledger.chunk_count,
                "input_ledger_final_sample_end_exclusive": (
                    ledger.final_sample_end_exclusive
                ),
                "input_pacing_mode": INPUT_PACING_MODE,
                "audio_metadata_protocol_version": PROTOCOL_VERSION,
                "stream_generation": generation,
                "output_sample_rate_hz": OUTPUT_SAMPLE_RATE_HZ,
                "output_channels": 1,
                "output_bytes_per_sample": OUTPUT_BYTES_PER_SAMPLE,
                "playback_clock_link_tolerance_ms": (
                    PLAYBACK_CLOCK_LINK_TOLERANCE_MS
                ),
                "playback_clock_offset_span_limit_ms": (
                    PLAYBACK_CLOCK_OFFSET_SPAN_LIMIT_MS
                ),
                "playback_clock_maximum_absolute_link_residual_ms": (
                    playback_clock_evidence.maximum_absolute_link_residual_ms
                ),
                "playback_clock_offset_span_ms": (
                    playback_clock_evidence.offset_span_ms
                ),
            },
            "claim_scope": {
                "semantic_source_marker_reviewed": True,
                "source_pcm_binding_verified": True,
                "target_landmark_proven": False,
                "actual_audibility_proven": False,
            },
            "maximum_latency_seconds": normalized_sla,
            "events": events,
            "summary": {
                "marker_count": len(events),
                "unique_candidate_group_count": len(candidate_groups),
                "candidate_groups": candidate_groups,
                "status_counts": counts,
                "overall_status": overall_status,
            },
        }
    )


def render_semantic_event_latency_markdown(
    analysis: dict[str, Any],
) -> str:
    """Render a privacy-safe human-readable latency-gate report."""

    evidence = analysis["evidence"]
    summary = analysis["summary"]
    lines = [
        "# Semantic-event latency gate",
        "",
        (
            f"**Overall status: {summary['overall_status'].upper()}** "
            f"(SLA: {analysis['maximum_latency_seconds']:.3f} seconds)"
        ),
        "",
        "## Evidence and claim boundary",
        "",
        f"- Capture SHA-256: `{evidence['capture_csv_sha256']}`",
        f"- Marker SHA-256: `{evidence['markers_json_sha256']}`",
        f"- Source PCM SHA-256: `{evidence['source_pcm_sha256']}`",
        (
            "- Source PCM padded samples: "
            f"{evidence['source_pcm_sample_count']}"
        ),
        (
            "- Protocol / generation: "
            f"{evidence['audio_metadata_protocol_version']} / "
            f"{evidence['stream_generation']}"
        ),
        (
            "- Playback clock-link residual / allowed: "
            f"{evidence['playback_clock_maximum_absolute_link_residual_ms']:.3f}"
            " ms / "
            f"{evidence['playback_clock_link_tolerance_ms']:.3f} ms"
        ),
        (
            "- Playback clock-offset span / allowed: "
            f"{evidence['playback_clock_offset_span_ms']:.3f} ms / "
            f"{evidence['playback_clock_offset_span_limit_ms']:.3f} ms"
        ),
        (
            "- Semantic source marker reviewed: "
            f"{str(analysis['claim_scope']['semantic_source_marker_reviewed']).lower()}"
        ),
        (
            "- Source PCM binding verified: "
            f"{str(analysis['claim_scope']['source_pcm_binding_verified']).lower()}"
        ),
        (
            "- Target-language landmark proven: "
            f"{str(analysis['claim_scope']['target_landmark_proven']).lower()}"
        ),
        (
            "- Actual physical audibility proven: "
            f"{str(analysis['claim_scope']['actual_audibility_proven']).lower()}"
        ),
        "",
        (
            "This gate bounds captured client receipt and projected browser "
            "playback. It does not claim a target-language semantic landmark "
            "or prove when sound reached a listener."
        ),
        "",
        "## Events",
        "",
        (
            f"Markers: {summary['marker_count']} across "
            f"{summary['unique_candidate_group_count']} unique candidate "
            "groups. Marker counts are reviewed annotations, not "
            "independent parent-level trials."
        ),
        "",
        (
            "| Event | Group | Source sample / offset | First receipt | "
            "Last receipt | Parent complete | Projected start lower | "
            "Projected end upper | Status |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for event in analysis["events"]:
        latency = event["latency_bounds_ms"]
        lines.append(
            "| {event_id} | {group_id} | {sample} / {source:.3f} ms | "
            "{first:.3f} ms | "
            "{last:.3f} ms | {complete:.3f} ms | {start:.3f} ms | "
            "{end:.3f} ms | {status} |".format(
                event_id=event["event_id"],
                group_id=event["candidate_group_id"],
                sample=event["source_sample_index"],
                source=event["source_offset_ms"],
                first=latency["first_candidate_frame_receipt"],
                last=latency["last_candidate_frame_receipt"],
                complete=latency["parent_complete_receipt"],
                start=latency[
                    "conservative_projected_first_frame_start"
                ],
                end=latency[
                    "conservative_projected_final_frame_end"
                ],
                status=event["status"],
            )
        )
    lines.extend(
        [
            "",
            "## Candidate groups",
            "",
            "| Group | Marker count |",
            "|---|---:|",
        ]
    )
    for group in summary["candidate_groups"]:
        lines.append(
            f"| {group['candidate_group_id']} | {group['marker_count']} |"
        )
    counts = summary["status_counts"]
    lines.extend(
        [
            "",
            "## Decision rule",
            "",
            (
                "PASS means the conservative projected final-frame end is at "
                "or below the SLA. FAIL means even the conservative projected "
                "first-frame start lower bound exceeds the SLA. Values "
                "between those bounds are INCONCLUSIVE."
            ),
            "",
            (
                f"Marker status counts: {counts['pass']} pass, "
                f"{counts['inconclusive']} inconclusive, "
                f"{counts['fail']} fail."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply a fail-closed semantic-event latency gate to a "
            "TestDashboard CSV and reviewed marker sidecar"
        )
    )
    parser.add_argument("--results-csv", type=Path, required=True)
    parser.add_argument("--markers-json", type=Path, required=True)
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
    if args.minimum_reviewers < 1:
        parser.error("--minimum-reviewers must be positive")
    if (
        args.json_output is not None
        and args.markdown_output is not None
        and args.json_output.resolve() == args.markdown_output.resolve()
    ):
        parser.error("JSON and Markdown output paths must be different")
    input_paths = {
        args.results_csv.resolve(),
        args.markers_json.resolve(),
    }
    for output in (args.json_output, args.markdown_output):
        if output is not None and output.resolve() in input_paths:
            parser.error("output paths must not overwrite input evidence")
    return args


def _stage_report_output(path: Path, content: str) -> Path:
    """Write and sync one report to a temporary sibling file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="",
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _reserve_report_backup(path: Path) -> Path:
    """Reserve an adjacent path that can atomically receive an old report."""

    descriptor, backup_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".bak",
    )
    os.close(descriptor)
    return Path(backup_name)


def _write_report_outputs_atomically(
    outputs: Sequence[tuple[Path, str]],
) -> None:
    """Stage every report, then install the complete set with rollback.

    A filesystem cannot atomically rename multiple paths as one transaction.
    This routine gets as close as practical: all bytes are written and synced
    before any destination changes, existing destinations are backed up with
    same-directory atomic renames, and an expected install failure rolls the
    already-installed members back.
    """

    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path] = {}
    installed: set[Path] = set()
    commit_succeeded = False
    try:
        for path, content in outputs:
            if path.exists() and not path.is_file():
                raise OSError("report output destination is not a file")
            staged.append((path, _stage_report_output(path, content)))

        for path, _temporary_path in staged:
            if not path.exists():
                continue
            backup_path = _reserve_report_backup(path)
            try:
                os.replace(path, backup_path)
            except OSError:
                backup_path.unlink(missing_ok=True)
                raise
            backups[path] = backup_path

        for path, temporary_path in staged:
            os.replace(temporary_path, path)
            installed.add(path)
        commit_succeeded = True
    except OSError as exc:
        rollback_errors: list[OSError] = []
        for path, _temporary_path in reversed(staged):
            backup_path = backups.get(path)
            try:
                if backup_path is not None:
                    os.replace(backup_path, path)
                elif path in installed:
                    path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise OSError(
                "report output installation and rollback failed"
            ) from exc
        raise
    finally:
        for _path, temporary_path in staged:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        if commit_succeeded:
            for backup_path in backups.values():
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_argument_parser()
    args = parse_cli_args(argv)
    try:
        analysis = analyze_semantic_event_latency(
            args.results_csv,
            args.markers_json,
            args.max_latency_seconds,
            minimum_reviewers=args.minimum_reviewers,
        )
    except (OSError, SemanticEventLatencyError) as exc:
        parser.error(str(exc))

    outputs: list[tuple[Path, str]] = []
    wrote_formats: list[str] = []
    if args.json_output is not None:
        outputs.append(
            (
                args.json_output,
                json.dumps(analysis, indent=2) + "\n",
            )
        )
        wrote_formats.append("json")
    if args.markdown_output is not None:
        outputs.append(
            (
                args.markdown_output,
                render_semantic_event_latency_markdown(analysis),
            )
        )
        wrote_formats.append("markdown")
    try:
        _write_report_outputs_atomically(outputs)
    except OSError:
        parser.error("could not write complete report output set")
    status = analysis["summary"]["overall_status"]
    exit_code = GATE_EXIT_CODES[status]
    if not wrote_formats:
        print(json.dumps(analysis, indent=2))
    else:
        print(
            f"status={status.upper()} exit_code={exit_code} "
            f"outputs={','.join(wrote_formats)}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
