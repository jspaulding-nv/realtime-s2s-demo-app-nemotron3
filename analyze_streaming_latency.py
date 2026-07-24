#!/usr/bin/env python3
"""Analyze privacy-safe staged S2S latency from batch summary JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


ANALYSIS_SCHEMA_VERSION = 1
SUPPORTED_TELEMETRY_SCHEMA_VERSIONS = {1, 2, 3}
SOURCE_TIME_TOLERANCE_MS = 1e-3
PCM_BYTES_PER_SAMPLE = 2


@dataclass(frozen=True)
class TimedObservation:
    """One transcript-free event aligned to a source-media boundary."""

    identity: tuple[int, ...]
    source_end_ms: float
    pipeline_elapsed_ms: float

    @property
    def boundary_to_event_seconds(self) -> float:
        return (self.pipeline_elapsed_ms - self.source_end_ms) / 1_000.0


@dataclass(frozen=True)
class SampleLatency:
    """Validated latency observations from one neutral input sample."""

    sample_index: int
    telemetry_schema_version: int
    harness_frame_duration_ms: float
    asr_finals: tuple[TimedObservation, ...]
    segments: tuple[TimedObservation, ...]
    tts_first_responses: tuple[TimedObservation, ...]
    tts_full_responses: tuple[TimedObservation, ...]
    websocket_sends: tuple[TimedObservation, ...]
    parent_tts_first_responses: tuple[TimedObservation, ...]
    parent_tts_full_responses: tuple[TimedObservation, ...]
    parent_websocket_first_sends: tuple[TimedObservation, ...]
    parent_websocket_final_sends: tuple[TimedObservation, ...]
    tts_response_series: tuple["TTSResponseSeries", ...]
    incremental_publications: tuple["IncrementalPublicationSeries", ...]

    @property
    def sample_label(self) -> str:
        return f"sample_{self.sample_index:02d}"

    @property
    def tts_withheld_seconds(self) -> tuple[float, ...]:
        if self.telemetry_schema_version == 3:
            return ()
        return tuple(
            (sent.pipeline_elapsed_ms - first.pipeline_elapsed_ms) / 1_000.0
            for first, sent in zip(
                self.tts_first_responses,
                self.websocket_sends,
            )
        )


@dataclass(frozen=True)
class IncrementalPublicationSeries:
    """Validated frame-publication evidence for one schema-v3 parent."""

    identity: tuple[int]
    source_end_ms: float
    total_audio_bytes: int
    frame_audio_bytes: tuple[int, ...]
    frame_send_elapsed_ms: tuple[float, ...]
    tts_first_elapsed_ms: float
    tts_completed_elapsed_ms: float

    @property
    def frame_count(self) -> int:
        return len(self.frame_audio_bytes)

    @property
    def first_send_elapsed_ms(self) -> float:
        return self.frame_send_elapsed_ms[0]

    @property
    def final_send_elapsed_ms(self) -> float:
        return self.frame_send_elapsed_ms[-1]

    @property
    def first_response_to_first_publish_seconds(self) -> float:
        return (
            self.first_send_elapsed_ms - self.tts_first_elapsed_ms
        ) / 1_000.0

    @property
    def atomic_withholding_equivalent_seconds(self) -> float:
        """Time an atomic publisher necessarily waits for RPC completion."""

        return (
            self.tts_completed_elapsed_ms - self.tts_first_elapsed_ms
        ) / 1_000.0

    @property
    def first_publish_lead_over_tts_completion_seconds(self) -> float:
        """Positive when the first frame was sent before RPC completion."""

        return (
            self.tts_completed_elapsed_ms - self.first_send_elapsed_ms
        ) / 1_000.0

    @property
    def tts_completion_to_final_publish_seconds(self) -> float:
        """Signed output-drain interval after the TTS iterator completed."""

        return (
            self.final_send_elapsed_ms - self.tts_completed_elapsed_ms
        ) / 1_000.0

    @property
    def first_to_final_publish_seconds(self) -> float:
        return (
            self.final_send_elapsed_ms - self.first_send_elapsed_ms
        ) / 1_000.0


@dataclass(frozen=True)
class TTSResponseSeries:
    """Validated response-cadence evidence for one atomic TTS request."""

    identity: tuple[int, int, int]
    response_count: int
    total_audio_bytes: int
    first_received_elapsed_ms: float
    last_received_elapsed_ms: float
    completed_elapsed_ms: float
    response_audio_duration_ms: tuple[float, ...]
    inter_response_arrival_ms: tuple[float, ...]

    @property
    def first_to_last_seconds(self) -> float:
        return (
            self.last_received_elapsed_ms - self.first_received_elapsed_ms
        ) / 1_000.0

    @property
    def first_to_complete_seconds(self) -> float:
        return (
            self.completed_elapsed_ms - self.first_received_elapsed_ms
        ) / 1_000.0

    @property
    def last_to_complete_seconds(self) -> float:
        return (
            self.completed_elapsed_ms - self.last_received_elapsed_ms
        ) / 1_000.0


def _require_object(value: Any, *, field: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: {field} must be an object")
    return value


def _require_list(value: Any, *, field: str, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label}: {field} must be a list")
    return value


def _require_nonnegative_int(value: Any, *, field: str, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError(f"{label}: {field} must be a non-negative integer")
    return value


def _require_positive_int(value: Any, *, field: str, label: str) -> int:
    parsed = _require_nonnegative_int(value, field=field, label=label)
    if parsed == 0:
        raise ValueError(f"{label}: {field} must be positive")
    return parsed


def _require_finite(value: Any, *, field: str, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label}: {field} must be finite")
    return float(value)


def _require_nonnegative_finite(
    value: Any,
    *,
    field: str,
    label: str,
) -> float:
    parsed = _require_finite(value, field=field, label=label)
    if parsed < 0:
        raise ValueError(f"{label}: {field} must be non-negative")
    return parsed


def _require_source_range(
    event: dict[str, Any],
    *,
    field: str,
    label: str,
) -> tuple[float | None, float]:
    source_start_raw = event.get("source_start_ms")
    source_start_ms = (
        None
        if source_start_raw is None
        else _require_nonnegative_finite(
            source_start_raw,
            field=f"{field}.source_start_ms",
            label=label,
        )
    )
    source_end_ms = _require_nonnegative_finite(
        event.get("source_end_ms"),
        field=f"{field}.source_end_ms",
        label=label,
    )
    if source_start_ms is not None and source_end_ms < source_start_ms:
        raise ValueError(
            f"{label}: {field}.source_end_ms cannot precede source_start_ms"
        )
    return source_start_ms, source_end_ms


def _pipeline_elapsed_ms(
    event: dict[str, Any],
    *,
    pipeline_start_ms: float,
    field: str,
    label: str,
) -> float:
    monotonic_ms = _require_nonnegative_finite(
        event.get("monotonic_ms"),
        field=f"{field}.monotonic_ms",
        label=label,
    )
    elapsed_ms = monotonic_ms - pipeline_start_ms
    if elapsed_ms < 0:
        raise ValueError(f"{label}: {field} precedes pipeline start")
    return elapsed_ms


def _require_contiguous_ids(
    values: Iterable[int],
    *,
    field: str,
    label: str,
) -> tuple[int, ...]:
    ordered = tuple(sorted(values))
    if ordered != tuple(range(len(ordered))):
        raise ValueError(f"{label}: {field} must be contiguous from zero")
    return ordered


def _tts_key(
    event: dict[str, Any],
    *,
    schema_version: int,
    field: str,
    label: str,
) -> tuple[int, int, int]:
    sequence_id = _require_nonnegative_int(
        event.get("sequence_id"),
        field=f"{field}.sequence_id",
        label=label,
    )
    if schema_version in {1, 3}:
        if event.get("subsequence_id") is not None:
            raise ValueError(
                f"{label}: {field} has subsequence identity in schema "
                f"v{schema_version}"
            )
        if event.get("subsequence_count") is not None:
            raise ValueError(
                f"{label}: {field} has subsequence identity in schema "
                f"v{schema_version}"
            )
        parent_sequence_id = event.get("parent_sequence_id")
        if (
            parent_sequence_id is not None
            and parent_sequence_id != sequence_id
        ):
            raise ValueError(
                f"{label}: {field}.parent_sequence_id does not match "
                "sequence_id"
            )
        return (sequence_id, 0, 1)

    parent_sequence_id = _require_nonnegative_int(
        event.get("parent_sequence_id"),
        field=f"{field}.parent_sequence_id",
        label=label,
    )
    if parent_sequence_id != sequence_id:
        raise ValueError(
            f"{label}: {field}.parent_sequence_id does not match sequence_id"
        )
    subsequence_id = _require_nonnegative_int(
        event.get("subsequence_id"),
        field=f"{field}.subsequence_id",
        label=label,
    )
    subsequence_count = _require_positive_int(
        event.get("subsequence_count"),
        field=f"{field}.subsequence_count",
        label=label,
    )
    if subsequence_id >= subsequence_count:
        raise ValueError(
            f"{label}: {field}.subsequence_id is outside subsequence_count"
        )
    return (parent_sequence_id, subsequence_id, subsequence_count)


def _audio_frame_key(
    event: dict[str, Any],
    *,
    field: str,
    label: str,
    require_sequence_alias: bool,
) -> tuple[int, int]:
    parent_sequence_id = _require_nonnegative_int(
        event.get("parent_sequence_id"),
        field=f"{field}.parent_sequence_id",
        label=label,
    )
    if require_sequence_alias:
        sequence_id = _require_nonnegative_int(
            event.get("sequence_id"),
            field=f"{field}.sequence_id",
            label=label,
        )
        if sequence_id != parent_sequence_id:
            raise ValueError(
                f"{label}: {field}.sequence_id does not match "
                "parent_sequence_id"
            )
    audio_frame_id = _require_nonnegative_int(
        event.get("audio_frame_id"),
        field=f"{field}.audio_frame_id",
        label=label,
    )
    if (
        event.get("subsequence_id") is not None
        or event.get("subsequence_count") is not None
    ):
        raise ValueError(
            f"{label}: {field} mixes frame and subsequence identity"
        )
    return parent_sequence_id, audio_frame_id


def _audio_frame_keys(
    value: Any,
    *,
    field: str,
    label: str,
) -> tuple[tuple[int, int], ...]:
    raw = _require_list(value, field=field, label=label)
    return tuple(
        _audio_frame_key(
            _require_object(
                item,
                field=f"{field}[{index}]",
                label=label,
            ),
            field=f"{field}[{index}]",
            label=label,
            require_sequence_alias=False,
        )
        for index, item in enumerate(raw)
    )


def _positive_int_list(
    value: Any,
    *,
    field: str,
    label: str,
) -> tuple[int, ...]:
    raw = _require_list(value, field=field, label=label)
    return tuple(
        _require_positive_int(
            item,
            field=f"{field}[{index}]",
            label=label,
        )
        for index, item in enumerate(raw)
    )


def _parent_summaries(
    value: Any,
    *,
    field: str,
    label: str,
) -> tuple[tuple[int, int, int, int], ...]:
    raw = _require_list(value, field=field, label=label)
    summaries: list[tuple[int, int, int, int]] = []
    for index, item in enumerate(raw):
        item_field = f"{field}[{index}]"
        summary = _require_object(item, field=item_field, label=label)
        parent_sequence_id = _require_nonnegative_int(
            summary.get("parent_sequence_id"),
            field=f"{item_field}.parent_sequence_id",
            label=label,
        )
        audio_frame_count = _require_positive_int(
            summary.get("audio_frame_count"),
            field=f"{item_field}.audio_frame_count",
            label=label,
        )
        audio_bytes = _require_positive_int(
            summary.get("audio_bytes"),
            field=f"{item_field}.audio_bytes",
            label=label,
        )
        retry_count = _require_nonnegative_int(
            summary.get("retry_count"),
            field=f"{item_field}.retry_count",
            label=label,
        )
        if retry_count not in {0, 1}:
            raise ValueError(
                f"{label}: {item_field}.retry_count must be zero or one"
            )
        summaries.append(
            (
                parent_sequence_id,
                audio_frame_count,
                audio_bytes,
                retry_count,
            )
        )
    return tuple(summaries)


def _event_observation(
    event: dict[str, Any],
    *,
    identity: tuple[int, ...],
    pipeline_start_ms: float,
    field: str,
    label: str,
) -> TimedObservation:
    _, source_end_ms = _require_source_range(
        event,
        field=field,
        label=label,
    )
    return TimedObservation(
        identity=identity,
        source_end_ms=source_end_ms,
        pipeline_elapsed_ms=_pipeline_elapsed_ms(
            event,
            pipeline_start_ms=pipeline_start_ms,
            field=field,
            label=label,
        ),
    )


def _store_unique(
    destination: dict[Any, Any],
    key: Any,
    value: Any,
    *,
    field: str,
    label: str,
) -> None:
    if key in destination:
        raise ValueError(f"{label}: duplicate {field} identity")
    destination[key] = value


def _validate_complete_summary(root: dict[str, Any], *, label: str) -> dict:
    integrity = _require_object(
        root.get("staged_integrity"),
        field="staged_integrity",
        label=label,
    )
    if (
        integrity.get("applicable") is not True
        or integrity.get("passed") is not True
        or integrity.get("errors") != []
    ):
        raise ValueError(f"{label}: staged integrity did not pass cleanly")

    staged = _require_object(
        root.get("staged_pipeline"),
        field="staged_pipeline",
        label=label,
    )
    if staged.get("state") != "closed":
        raise ValueError(f"{label}: staged pipeline is not closed")
    if staged.get("outcome") != "complete":
        raise ValueError(f"{label}: staged pipeline outcome is not complete")
    if staged.get("failure") is not None:
        raise ValueError(f"{label}: staged pipeline retained a failure")
    if staged.get("cleanup_errors") != []:
        raise ValueError(f"{label}: staged pipeline retained cleanup errors")
    if staged.get("incomplete_sequence_ids") != []:
        raise ValueError(f"{label}: staged pipeline has incomplete sequences")
    return staged


def _harness_frame_duration_ms(
    root: dict[str, Any],
    *,
    label: str,
) -> float:
    backend_config = _require_object(
        root.get("backend_config"),
        field="backend_config",
        label=label,
    )
    if backend_config.get("pipelineMode") != "staged":
        raise ValueError(f"{label}: backend_config.pipelineMode must be staged")
    sample_rate = _require_positive_int(
        backend_config.get("sampleRate"),
        field="backend_config.sampleRate",
        label=label,
    )
    chunk_size = _require_positive_int(
        backend_config.get("chunkSize"),
        field="backend_config.chunkSize",
        label=label,
    )
    return chunk_size / sample_rate * 1_000.0


def _pcm_bytes_per_second(
    root: dict[str, Any],
    *,
    label: str,
) -> int:
    """Return the staged Magpie LINEAR_PCM Int16 byte rate."""

    backend_config = _require_object(
        root.get("backend_config"),
        field="backend_config",
        label=label,
    )
    sample_rate = _require_positive_int(
        backend_config.get("sampleRate"),
        field="backend_config.sampleRate",
        label=label,
    )
    channels = _require_positive_int(
        backend_config.get("channels"),
        field="backend_config.channels",
        label=label,
    )
    return sample_rate * channels * PCM_BYTES_PER_SAMPLE


def _pcm_frame_alignment_bytes(
    root: dict[str, Any],
    *,
    label: str,
) -> int:
    backend_config = _require_object(
        root.get("backend_config"),
        field="backend_config",
        label=label,
    )
    channels = _require_positive_int(
        backend_config.get("channels"),
        field="backend_config.channels",
        label=label,
    )
    return channels * PCM_BYTES_PER_SAMPLE


def _validate_parent_children(
    keys: Iterable[tuple[int, int, int]],
    *,
    parent_ids: tuple[int, ...],
    label: str,
) -> None:
    grouped: dict[int, list[tuple[int, int]]] = {}
    for parent_id, subsequence_id, subsequence_count in keys:
        grouped.setdefault(parent_id, []).append(
            (subsequence_id, subsequence_count)
        )
    if tuple(sorted(grouped)) != parent_ids:
        raise ValueError(
            f"{label}: TTS parent identities do not match emitted segments"
        )
    for parent_id in parent_ids:
        children = sorted(grouped[parent_id])
        counts = {count for _, count in children}
        if len(counts) != 1:
            raise ValueError(
                f"{label}: subsequence_count changed within parent "
                f"{parent_id}"
            )
        count = next(iter(counts))
        if children != [(subsequence_id, count) for subsequence_id in range(count)]:
            raise ValueError(
                f"{label}: TTS subsequences for parent {parent_id} are "
                "not complete and contiguous"
            )


def _require_numeric_count(
    staged: dict[str, Any],
    *,
    field: str,
    expected: int,
    label: str,
) -> None:
    actual = _require_nonnegative_int(
        staged.get(field),
        field=f"staged_pipeline.{field}",
        label=label,
    )
    if actual != expected:
        raise ValueError(f"{label}: {field} does not match observed events")


def _require_sequence_list(
    staged: dict[str, Any],
    *,
    field: str,
    expected: tuple[int, ...],
    label: str,
) -> None:
    raw = _require_list(
        staged.get(field),
        field=f"staged_pipeline.{field}",
        label=label,
    )
    parsed = tuple(
        _require_nonnegative_int(
            value,
            field=f"staged_pipeline.{field}",
            label=label,
        )
        for value in raw
    )
    if parsed != expected:
        raise ValueError(f"{label}: {field} does not match emitted segments")


def _load_tts_response_series(
    staged: dict[str, Any],
    *,
    pipeline_start_ms: float,
    pcm_bytes_per_second: int,
    tts_started: dict[tuple[int, int, int], TimedObservation],
    tts_first: dict[tuple[int, int, int], TimedObservation],
    tts_full: dict[tuple[int, int, int], TimedObservation],
    tts_audio_bytes: dict[tuple[int, int, int], int],
    tts_retry_counts: dict[tuple[int, int, int], int],
    label: str,
) -> tuple[TTSResponseSeries, ...]:
    """Validate the optional measurement-only response-chunk sidecar."""

    enabled = staged.get("tts_response_chunk_telemetry_enabled", False)
    raw_sidecar = staged.get("tts_response_chunk_telemetry")
    if raw_sidecar is None:
        if enabled is not False:
            raise ValueError(
                f"{label}: enabled TTS response-chunk telemetry is missing"
            )
        return ()
    if enabled is not True:
        raise ValueError(
            f"{label}: TTS response-chunk sidecar requires its enabled flag"
        )
    if set(tts_started) != set(tts_full):
        raise ValueError(
            f"{label}: TTS response sidecar requires matching started events"
        )
    if set(tts_retry_counts) != set(tts_full):
        raise ValueError(
            f"{label}: TTS response sidecar requires retry attribution"
        )

    sidecar = _require_object(
        raw_sidecar,
        field="staged_pipeline.tts_response_chunk_telemetry",
        label=label,
    )
    if sidecar.get("schema_version") != 1:
        raise ValueError(
            f"{label}: TTS response-chunk schema_version must be one"
        )
    chunks = _require_list(
        sidecar.get("chunks"),
        field="staged_pipeline.tts_response_chunk_telemetry.chunks",
        label=label,
    )
    expected_chunk_count = _require_nonnegative_int(
        sidecar.get("response_chunk_count"),
        field="TTS response_chunk_count",
        label=label,
    )
    if expected_chunk_count != len(chunks):
        raise ValueError(
            f"{label}: TTS response_chunk_count does not match chunks"
        )

    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for index, raw_chunk in enumerate(chunks):
        field = (
            "staged_pipeline.tts_response_chunk_telemetry."
            f"chunks[{index}]"
        )
        chunk = _require_object(raw_chunk, field=field, label=label)
        key = (
            _require_nonnegative_int(
                chunk.get("parent_sequence_id"),
                field=f"{field}.parent_sequence_id",
                label=label,
            ),
            _require_nonnegative_int(
                chunk.get("subsequence_id"),
                field=f"{field}.subsequence_id",
                label=label,
            ),
            _require_positive_int(
                chunk.get("subsequence_count"),
                field=f"{field}.subsequence_count",
                label=label,
            ),
        )
        if key[1] >= key[2]:
            raise ValueError(
                f"{label}: {field} has invalid subsequence identity"
            )
        if key not in tts_full:
            raise ValueError(
                f"{label}: {field} has no matching TTS request"
            )
        normalized = {
            "response_index": _require_nonnegative_int(
                chunk.get("response_index"),
                field=f"{field}.response_index",
                label=label,
            ),
            "response_count": _require_positive_int(
                chunk.get("response_count"),
                field=f"{field}.response_count",
                label=label,
            ),
            "audio_bytes": _require_positive_int(
                chunk.get("audio_bytes"),
                field=f"{field}.audio_bytes",
                label=label,
            ),
            "cumulative_audio_bytes": _require_positive_int(
                chunk.get("cumulative_audio_bytes"),
                field=f"{field}.cumulative_audio_bytes",
                label=label,
            ),
            "audio_duration_ms": _require_nonnegative_finite(
                chunk.get("audio_duration_ms"),
                field=f"{field}.audio_duration_ms",
                label=label,
            ),
            "cumulative_audio_duration_ms": (
                _require_nonnegative_finite(
                    chunk.get("cumulative_audio_duration_ms"),
                    field=f"{field}.cumulative_audio_duration_ms",
                    label=label,
                )
            ),
            "received_monotonic_ms": _require_nonnegative_finite(
                chunk.get("received_monotonic_ms"),
                field=f"{field}.received_monotonic_ms",
                label=label,
            ),
            "since_request_start_ms": _require_nonnegative_finite(
                chunk.get("since_request_start_ms"),
                field=f"{field}.since_request_start_ms",
                label=label,
            ),
            "since_previous_response_ms": _require_nonnegative_finite(
                chunk.get("since_previous_response_ms"),
                field=f"{field}.since_previous_response_ms",
                label=label,
            ),
            "retry_count": _require_nonnegative_int(
                chunk.get("retry_count"),
                field=f"{field}.retry_count",
                label=label,
            ),
        }
        if normalized["retry_count"] not in {0, 1}:
            raise ValueError(
                f"{label}: {field}.retry_count must be zero or one"
            )
        grouped.setdefault(key, []).append(normalized)

    if set(grouped) != set(tts_full):
        raise ValueError(
            f"{label}: TTS response-chunk and completed-request identities "
            "do not match"
        )
    segments_observed = _require_nonnegative_int(
        sidecar.get("segments_observed"),
        field="TTS response segments_observed",
        label=label,
    )
    if segments_observed != len(grouped):
        raise ValueError(
            f"{label}: TTS response segments_observed does not match requests"
        )

    series: list[TTSResponseSeries] = []
    for key in sorted(grouped):
        observed = grouped[key]
        declared_count = observed[0]["response_count"]
        if declared_count != len(observed) or any(
            item["response_count"] != declared_count for item in observed
        ):
            raise ValueError(
                f"{label}: TTS response_count does not match request chunks"
            )
        if [item["response_index"] for item in observed] != list(
            range(declared_count)
        ):
            raise ValueError(
                f"{label}: TTS response indices must be contiguous from zero"
            )
        cumulative = 0
        pipeline_tts_started_ms = (
            pipeline_start_ms + tts_started[key].pipeline_elapsed_ms
        )
        request_started_ms = (
            observed[0]["received_monotonic_ms"]
            - observed[0]["since_request_start_ms"]
        )
        if (
            request_started_ms + SOURCE_TIME_TOLERANCE_MS
            < pipeline_tts_started_ms
        ):
            raise ValueError(
                f"{label}: TTS response request start precedes tts/started"
            )
        prior_received_ms = request_started_ms
        inter_arrivals: list[float] = []
        for response_index, item in enumerate(observed):
            cumulative += item["audio_bytes"]
            if item["cumulative_audio_bytes"] != cumulative:
                raise ValueError(
                    f"{label}: TTS response cumulative bytes do not reconcile"
                )
            if item["received_monotonic_ms"] < prior_received_ms:
                raise ValueError(
                    f"{label}: TTS response timestamps are not ordered"
                )
            measured_gap = item["received_monotonic_ms"] - prior_received_ms
            if not math.isclose(
                item["since_previous_response_ms"],
                measured_gap,
                abs_tol=SOURCE_TIME_TOLERANCE_MS,
            ):
                raise ValueError(
                    f"{label}: TTS response inter-arrival timing "
                    "does not reconcile"
                )
            measured_since_start = (
                item["received_monotonic_ms"] - request_started_ms
            )
            if not math.isclose(
                item["since_request_start_ms"],
                measured_since_start,
                abs_tol=SOURCE_TIME_TOLERANCE_MS,
            ):
                raise ValueError(
                    f"{label}: TTS response request timing does not reconcile"
                )
            expected_audio_duration_ms = (
                item["audio_bytes"] / pcm_bytes_per_second * 1_000.0
            )
            expected_cumulative_duration_ms = (
                cumulative / pcm_bytes_per_second * 1_000.0
            )
            if not math.isclose(
                item["audio_duration_ms"],
                expected_audio_duration_ms,
                abs_tol=SOURCE_TIME_TOLERANCE_MS,
            ) or not math.isclose(
                item["cumulative_audio_duration_ms"],
                expected_cumulative_duration_ms,
                abs_tol=SOURCE_TIME_TOLERANCE_MS,
            ):
                raise ValueError(
                    f"{label}: TTS response PCM duration does not "
                    "reconcile with bytes"
                )
            if item["retry_count"] != tts_retry_counts[key]:
                raise ValueError(
                    f"{label}: TTS response retry attribution does not match"
                )
            if response_index > 0:
                inter_arrivals.append(measured_gap)
            prior_received_ms = item["received_monotonic_ms"]
        if cumulative != tts_audio_bytes[key]:
            raise ValueError(
                f"{label}: TTS response bytes do not match completed request"
            )
        first_absolute_ms = (
            pipeline_start_ms + tts_first[key].pipeline_elapsed_ms
        )
        if not math.isclose(
            observed[0]["received_monotonic_ms"],
            first_absolute_ms,
            abs_tol=SOURCE_TIME_TOLERANCE_MS,
        ):
            raise ValueError(
                f"{label}: first TTS response timestamp does not match "
                "tts/first_audio"
            )
        completed_elapsed_ms = tts_full[key].pipeline_elapsed_ms
        last_elapsed_ms = (
            observed[-1]["received_monotonic_ms"] - pipeline_start_ms
        )
        if last_elapsed_ms > completed_elapsed_ms + SOURCE_TIME_TOLERANCE_MS:
            raise ValueError(
                f"{label}: final TTS response follows request completion"
            )
        series.append(
            TTSResponseSeries(
                identity=key,
                response_count=declared_count,
                total_audio_bytes=cumulative,
                first_received_elapsed_ms=(
                    observed[0]["received_monotonic_ms"]
                    - pipeline_start_ms
                ),
                last_received_elapsed_ms=last_elapsed_ms,
                completed_elapsed_ms=completed_elapsed_ms,
                response_audio_duration_ms=tuple(
                    item["audio_duration_ms"] for item in observed
                ),
                inter_response_arrival_ms=tuple(inter_arrivals),
            )
        )
    return tuple(series)


def _load_incremental_publication_series(
    staged: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    pipeline_start_ms: float,
    pcm_bytes_per_second: int,
    pcm_frame_alignment_bytes: int,
    parent_ids: tuple[int, ...],
    segments: dict[int, TimedObservation],
    tts_first: dict[tuple[int, int, int], TimedObservation],
    tts_full: dict[tuple[int, int, int], TimedObservation],
    tts_audio_bytes: dict[tuple[int, int, int], int],
    tts_first_frame_counts: dict[tuple[int, int, int], int],
    tts_audio_frame_counts: dict[tuple[int, int, int], int],
    tts_retry_counts: dict[tuple[int, int, int], int],
    label: str,
) -> tuple[
    dict[tuple[int, int, int], TimedObservation],
    tuple[IncrementalPublicationSeries, ...],
]:
    """Validate schema-v3 frame identity, bytes, and publication timing."""

    if staged.get("tts_incremental_publish_enabled") is not True:
        raise ValueError(
            f"{label}: schema v3 requires incremental TTS publication"
        )
    if staged.get("tts_subsegmentation_enabled") is not False:
        raise ValueError(
            f"{label}: schema v3 prohibits TTS subsegmentation"
        )
    frame_duration_ms = _require_positive_int(
        staged.get("tts_incremental_frame_ms"),
        field="staged_pipeline.tts_incremental_frame_ms",
        label=label,
    )
    configured_frame_bytes = _require_positive_int(
        staged.get("tts_incremental_frame_bytes"),
        field="staged_pipeline.tts_incremental_frame_bytes",
        label=label,
    )
    expected_frame_bytes = pcm_bytes_per_second * frame_duration_ms / 1_000.0
    if (
        not expected_frame_bytes.is_integer()
        or configured_frame_bytes != int(expected_frame_bytes)
    ):
        raise ValueError(
            f"{label}: incremental frame duration and PCM bytes do not "
            "reconcile"
        )

    frame_key_fields = (
        "published_audio_frame_keys",
        "dequeued_audio_frame_keys",
        "websocket_sent_audio_frame_keys",
    )
    parsed_key_layers = tuple(
        _audio_frame_keys(
            staged.get(field),
            field=f"staged_pipeline.{field}",
            label=label,
        )
        for field in frame_key_fields
    )
    if parsed_key_layers[1:] != parsed_key_layers[:-1]:
        raise ValueError(
            f"{label}: schema-v3 frame identity layers do not reconcile"
        )
    frame_keys = parsed_key_layers[0]

    frame_byte_fields = (
        "published_audio_frame_bytes",
        "dequeued_audio_frame_bytes",
        "websocket_sent_audio_frame_bytes",
    )
    parsed_byte_layers = tuple(
        _positive_int_list(
            staged.get(field),
            field=f"staged_pipeline.{field}",
            label=label,
        )
        for field in frame_byte_fields
    )
    if parsed_byte_layers[1:] != parsed_byte_layers[:-1]:
        raise ValueError(
            f"{label}: schema-v3 frame byte layers do not reconcile"
        )
    frame_bytes = parsed_byte_layers[0]
    if len(frame_keys) != len(frame_bytes):
        raise ValueError(
            f"{label}: schema-v3 frame identities and bytes differ in length"
        )
    _require_numeric_count(
        staged,
        field="audio_frames_produced",
        expected=len(frame_keys),
        label=label,
    )

    parent_fields = (
        "produced_parent_summaries",
        "completed_parent_summaries",
        "websocket_completed_parent_summaries",
    )
    parsed_parent_layers = tuple(
        _parent_summaries(
            staged.get(field),
            field=f"staged_pipeline.{field}",
            label=label,
        )
        for field in parent_fields
    )
    if parsed_parent_layers[1:] != parsed_parent_layers[:-1]:
        raise ValueError(
            f"{label}: schema-v3 parent completion layers do not reconcile"
        )
    parent_summaries = parsed_parent_layers[0]
    if tuple(item[0] for item in parent_summaries) != parent_ids:
        raise ValueError(
            f"{label}: schema-v3 parent summaries do not match segments"
        )

    expected_keys: list[tuple[int, int]] = []
    parent_slices: dict[int, slice] = {}
    offset = 0
    for parent_id, frame_count, audio_bytes, retry_count in parent_summaries:
        request_key = (parent_id, 0, 1)
        if request_key not in tts_full:
            raise ValueError(
                f"{label}: parent completion has no matching TTS request"
            )
        if (
            tts_audio_bytes[request_key] != audio_bytes
            or tts_first_frame_counts.get(request_key) != frame_count
            or tts_audio_frame_counts.get(request_key) != frame_count
            or tts_retry_counts[request_key] != retry_count
        ):
            raise ValueError(
                f"{label}: TTS completion and parent summary do not reconcile"
            )
        parent_slice = slice(offset, offset + frame_count)
        parent_slices[parent_id] = parent_slice
        sizes = frame_bytes[parent_slice]
        if len(sizes) != frame_count or sum(sizes) != audio_bytes:
            raise ValueError(
                f"{label}: parent {parent_id} frame bytes do not reconcile"
            )
        if (
            any(size != configured_frame_bytes for size in sizes[:-1])
            or sizes[-1] > configured_frame_bytes
            or any(size % pcm_frame_alignment_bytes for size in sizes)
        ):
            raise ValueError(
                f"{label}: parent {parent_id} violates incremental PCM "
                "framing"
            )
        expected_keys.extend(
            (parent_id, audio_frame_id)
            for audio_frame_id in range(frame_count)
        )
        offset += frame_count
    if tuple(expected_keys) != frame_keys or offset != len(frame_bytes):
        raise ValueError(
            f"{label}: schema-v3 frame identities are not contiguous by parent"
        )

    websocket_events = _require_list(
        staged.get("websocket_send_events"),
        field="staged_pipeline.websocket_send_events",
        label=label,
    )
    websocket_keys: list[tuple[int, int]] = []
    websocket_bytes: list[int] = []
    websocket_elapsed_ms: list[float] = []
    prior_sent_ms = pipeline_start_ms
    for index, raw_event in enumerate(websocket_events):
        field = f"staged_pipeline.websocket_send_events[{index}]"
        event = _require_object(raw_event, field=field, label=label)
        websocket_keys.append(
            _audio_frame_key(
                event,
                field=field,
                label=label,
                require_sequence_alias=True,
            )
        )
        websocket_bytes.append(
            _require_positive_int(
                event.get("audio_bytes"),
                field=f"{field}.audio_bytes",
                label=label,
            )
        )
        sent_ms = _require_nonnegative_finite(
            event.get("sent_monotonic_ms"),
            field=f"{field}.sent_monotonic_ms",
            label=label,
        )
        if sent_ms < prior_sent_ms:
            raise ValueError(
                f"{label}: schema-v3 WebSocket sends are not time-ordered"
            )
        prior_sent_ms = sent_ms
        websocket_elapsed_ms.append(sent_ms - pipeline_start_ms)
    if tuple(websocket_keys) != frame_keys:
        raise ValueError(
            f"{label}: WebSocket frame identities do not reconcile"
        )
    if tuple(websocket_bytes) != frame_bytes:
        raise ValueError(
            f"{label}: WebSocket frame bytes do not reconcile"
        )

    frame_event_names = (
        ("tts", "frame_received"),
        ("output", "frame_enqueued"),
        ("output", "frame_dequeued"),
    )
    frame_event_records: dict[
        tuple[str, str],
        tuple[list[tuple[int, int]], list[int]],
    ] = {
        name: ([], []) for name in frame_event_names
    }
    parent_event_names = (
        ("output", "parent_complete_enqueued"),
        ("output", "parent_complete_dequeued"),
    )
    parent_event_records: dict[
        tuple[str, str],
        list[tuple[int, int, int, int]],
    ] = {name: [] for name in parent_event_names}
    for index, event in enumerate(events):
        event_name = (event.get("stage"), event.get("event"))
        field = f"staged_pipeline.events[{index}]"
        if event_name in frame_event_records:
            keys, sizes = frame_event_records[event_name]
            keys.append(
                _audio_frame_key(
                    event,
                    field=field,
                    label=label,
                    require_sequence_alias=True,
                )
            )
            sizes.append(
                _require_positive_int(
                    event.get("audio_bytes"),
                    field=f"{field}.audio_bytes",
                    label=label,
                )
            )
        elif event_name in parent_event_records:
            summary = _parent_summaries(
                [
                    {
                        "parent_sequence_id": event.get(
                            "parent_sequence_id",
                            event.get("sequence_id"),
                        ),
                        "audio_frame_count": event.get("audio_frame_count"),
                        "audio_bytes": event.get("audio_bytes"),
                        "retry_count": event.get("retry_count"),
                    }
                ],
                field=field,
                label=label,
            )[0]
            if event.get("sequence_id") != summary[0]:
                raise ValueError(
                    f"{label}: {field}.sequence_id does not match parent"
                )
            parent_event_records[event_name].append(summary)
    for event_name, (keys, sizes) in frame_event_records.items():
        if tuple(keys) != frame_keys or tuple(sizes) != frame_bytes:
            raise ValueError(
                f"{label}: {event_name[0]}/{event_name[1]} frame evidence "
                "does not reconcile"
            )
    for event_name, summaries in parent_event_records.items():
        if tuple(summaries) != parent_summaries:
            raise ValueError(
                f"{label}: {event_name[0]}/{event_name[1]} parent evidence "
                "does not reconcile"
            )

    logical_sends: dict[tuple[int, int, int], TimedObservation] = {}
    series: list[IncrementalPublicationSeries] = []
    for parent_id, _, audio_bytes, _ in parent_summaries:
        request_key = (parent_id, 0, 1)
        parent_slice = parent_slices[parent_id]
        send_times = tuple(websocket_elapsed_ms[parent_slice])
        sizes = tuple(frame_bytes[parent_slice])
        first_tts_ms = tts_first[request_key].pipeline_elapsed_ms
        completed_tts_ms = tts_full[request_key].pipeline_elapsed_ms
        if first_tts_ms > completed_tts_ms:
            raise ValueError(
                f"{label}: TTS first/completed timing order is invalid"
            )
        if send_times[0] < first_tts_ms:
            raise ValueError(
                f"{label}: first schema-v3 frame was sent before first TTS PCM"
            )
        source_end_ms = segments[parent_id].source_end_ms
        logical_sends[request_key] = TimedObservation(
            identity=request_key,
            source_end_ms=source_end_ms,
            pipeline_elapsed_ms=send_times[-1],
        )
        series.append(
            IncrementalPublicationSeries(
                identity=(parent_id,),
                source_end_ms=source_end_ms,
                total_audio_bytes=audio_bytes,
                frame_audio_bytes=sizes,
                frame_send_elapsed_ms=send_times,
                tts_first_elapsed_ms=first_tts_ms,
                tts_completed_elapsed_ms=completed_tts_ms,
            )
        )
    return logical_sends, tuple(series)


def load_summary_latency(path: Path, *, sample_index: int) -> SampleLatency:
    """Load and validate one completed staged batch summary.

    Only numeric timing, identity, and count fields are retained. Input paths,
    transcript text, PCM, endpoints, and session identifiers never enter the
    returned record.
    """

    label = f"sample_{sample_index:02d}"
    if (
        not isinstance(sample_index, int)
        or isinstance(sample_index, bool)
        or sample_index <= 0
    ):
        raise ValueError("sample_index must be a positive integer")
    try:
        with path.open(encoding="utf-8") as handle:
            root = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: summary could not be read as JSON") from exc
    root = _require_object(root, field="summary root", label=label)
    staged = _validate_complete_summary(root, label=label)
    harness_frame_duration_ms = _harness_frame_duration_ms(root, label=label)
    pcm_bytes_per_second = _pcm_bytes_per_second(root, label=label)
    pcm_frame_alignment_bytes = _pcm_frame_alignment_bytes(
        root,
        label=label,
    )

    schema_version = staged.get("telemetry_schema_version", 1)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_TELEMETRY_SCHEMA_VERSIONS
    ):
        raise ValueError(
            f"{label}: telemetry_schema_version must be one, two, or three"
        )

    events = _require_list(
        staged.get("events"),
        field="staged_pipeline.events",
        label=label,
    )
    for index, event in enumerate(events):
        _require_object(
            event,
            field=f"staged_pipeline.events[{index}]",
            label=label,
        )
    pipeline_starts = [
        event
        for event in events
        if event.get("stage") == "pipeline"
        and event.get("event") == "started"
    ]
    if len(pipeline_starts) != 1:
        raise ValueError(
            f"{label}: exactly one pipeline started event is required"
        )
    pipeline_start_ms = _require_nonnegative_finite(
        pipeline_starts[0].get("monotonic_ms"),
        field="pipeline started monotonic_ms",
        label=label,
    )

    asr_finals: dict[int, TimedObservation] = {}
    segments: dict[int, TimedObservation] = {}
    segment_final_ids: dict[int, tuple[int, ...]] = {}
    tts_started: dict[tuple[int, int, int], TimedObservation] = {}
    tts_first: dict[tuple[int, int, int], TimedObservation] = {}
    tts_full: dict[tuple[int, int, int], TimedObservation] = {}
    tts_audio_bytes: dict[tuple[int, int, int], int] = {}
    tts_first_frame_counts: dict[tuple[int, int, int], int] = {}
    tts_audio_frame_counts: dict[tuple[int, int, int], int] = {}
    tts_retry_counts: dict[tuple[int, int, int], int] = {}

    for index, event in enumerate(events):
        stage = event.get("stage")
        event_name = event.get("event")
        field = f"staged_pipeline.events[{index}]"
        if stage == "asr" and event_name == "final":
            final_id = _require_nonnegative_int(
                event.get("asr_final_id"),
                field=f"{field}.asr_final_id",
                label=label,
            )
            _store_unique(
                asr_finals,
                final_id,
                _event_observation(
                    event,
                    identity=(final_id,),
                    pipeline_start_ms=pipeline_start_ms,
                    field=field,
                    label=label,
                ),
                field="ASR final",
                label=label,
            )
        elif stage == "segmenter" and event_name == "emitted":
            sequence_id = _require_nonnegative_int(
                event.get("sequence_id"),
                field=f"{field}.sequence_id",
                label=label,
            )
            final_ids_raw = _require_list(
                event.get("contributing_final_ids"),
                field=f"{field}.contributing_final_ids",
                label=label,
            )
            final_ids = tuple(
                _require_nonnegative_int(
                    value,
                    field=f"{field}.contributing_final_ids",
                    label=label,
                )
                for value in final_ids_raw
            )
            if not final_ids or len(set(final_ids)) != len(final_ids):
                raise ValueError(
                    f"{label}: {field}.contributing_final_ids must be "
                    "non-empty and unique"
                )
            _store_unique(
                segments,
                sequence_id,
                _event_observation(
                    event,
                    identity=(sequence_id,),
                    pipeline_start_ms=pipeline_start_ms,
                    field=field,
                    label=label,
                ),
                field="segment",
                label=label,
            )
            segment_final_ids[sequence_id] = final_ids
        elif stage == "tts" and event_name in {
            "started",
            "first_audio",
            "completed",
        }:
            key = _tts_key(
                event,
                schema_version=schema_version,
                field=field,
                label=label,
            )
            if schema_version == 3 and event_name != "started":
                parent_sequence_id = _require_nonnegative_int(
                    event.get("parent_sequence_id"),
                    field=f"{field}.parent_sequence_id",
                    label=label,
                )
                if parent_sequence_id != key[0]:
                    raise ValueError(
                        f"{label}: {field}.parent_sequence_id does not "
                        "match sequence_id"
                    )
            observation = _event_observation(
                event,
                identity=key,
                pipeline_start_ms=pipeline_start_ms,
                field=field,
                label=label,
            )
            if event_name == "started":
                _store_unique(
                    tts_started,
                    key,
                    observation,
                    field="TTS started",
                    label=label,
                )
            elif event_name == "first_audio":
                _store_unique(
                    tts_first,
                    key,
                    observation,
                    field="TTS first-response",
                    label=label,
                )
                if schema_version == 3:
                    tts_first_frame_counts[key] = _require_positive_int(
                        event.get("audio_frame_count"),
                        field=f"{field}.audio_frame_count",
                        label=label,
                    )
            else:
                _store_unique(
                    tts_full,
                    key,
                    observation,
                    field="TTS completed",
                    label=label,
                )
                tts_audio_bytes[key] = _require_positive_int(
                    event.get("audio_bytes"),
                    field=f"{field}.audio_bytes",
                    label=label,
                )
                if schema_version == 3:
                    tts_audio_frame_counts[key] = _require_positive_int(
                        event.get("audio_frame_count"),
                        field=f"{field}.audio_frame_count",
                        label=label,
                    )
                retry_count = _require_nonnegative_int(
                    event.get("retry_count"),
                    field=f"{field}.retry_count",
                    label=label,
                )
                if retry_count not in {0, 1}:
                    raise ValueError(
                        f"{label}: {field}.retry_count must be zero or one"
                    )
                tts_retry_counts[key] = retry_count

    if not asr_finals:
        raise ValueError(f"{label}: no ASR final events were observed")
    if not segments:
        raise ValueError(f"{label}: no emitted segments were observed")
    if not tts_first or set(tts_first) != set(tts_full):
        raise ValueError(
            f"{label}: TTS first-response/completed identities do not match"
        )

    final_ids = _require_contiguous_ids(
        asr_finals,
        field="ASR final IDs",
        label=label,
    )
    parent_ids = _require_contiguous_ids(
        segments,
        field="segment sequence IDs",
        label=label,
    )
    _validate_parent_children(
        tts_first,
        parent_ids=parent_ids,
        label=label,
    )
    for sequence_id, contributing_final_ids in segment_final_ids.items():
        if any(final_id not in asr_finals for final_id in contributing_final_ids):
            raise ValueError(
                f"{label}: segment {sequence_id} references an unknown ASR final"
            )
        expected_source_end_ms = max(
            asr_finals[final_id].source_end_ms
            for final_id in contributing_final_ids
        )
        if not math.isclose(
            segments[sequence_id].source_end_ms,
            expected_source_end_ms,
            abs_tol=SOURCE_TIME_TOLERANCE_MS,
        ):
            raise ValueError(
                f"{label}: segment {sequence_id} source boundary does not "
                "match its contributing ASR finals"
            )

    incremental_publications: tuple[
        IncrementalPublicationSeries,
        ...,
    ] = ()
    if schema_version == 3:
        websocket_sends, incremental_publications = (
            _load_incremental_publication_series(
                staged,
                events=events,
                pipeline_start_ms=pipeline_start_ms,
                pcm_bytes_per_second=pcm_bytes_per_second,
                pcm_frame_alignment_bytes=pcm_frame_alignment_bytes,
                parent_ids=parent_ids,
                segments=segments,
                tts_first=tts_first,
                tts_full=tts_full,
                    tts_audio_bytes=tts_audio_bytes,
                    tts_first_frame_counts=tts_first_frame_counts,
                    tts_audio_frame_counts=tts_audio_frame_counts,
                tts_retry_counts=tts_retry_counts,
                label=label,
            )
        )
    else:
        websocket_events = _require_list(
            staged.get("websocket_send_events"),
            field="staged_pipeline.websocket_send_events",
            label=label,
        )
        websocket_sends = {}
        for index, raw_event in enumerate(websocket_events):
            event = _require_object(
                raw_event,
                field=f"staged_pipeline.websocket_send_events[{index}]",
                label=label,
            )
            field = f"staged_pipeline.websocket_send_events[{index}]"
            key = _tts_key(
                event,
                schema_version=schema_version,
                field=field,
                label=label,
            )
            if key not in tts_full:
                raise ValueError(
                    f"{label}: WebSocket send has no matching TTS request"
                )
            sent_monotonic_ms = _require_nonnegative_finite(
                event.get("sent_monotonic_ms"),
                field=f"{field}.sent_monotonic_ms",
                label=label,
            )
            sent_elapsed_ms = sent_monotonic_ms - pipeline_start_ms
            if sent_elapsed_ms < 0:
                raise ValueError(f"{label}: {field} precedes pipeline start")
            audio_bytes = _require_positive_int(
                event.get("audio_bytes"),
                field=f"{field}.audio_bytes",
                label=label,
            )
            if audio_bytes != tts_audio_bytes[key]:
                raise ValueError(
                    f"{label}: WebSocket and TTS byte counts do not match"
                )
            _store_unique(
                websocket_sends,
                key,
                TimedObservation(
                    identity=key,
                    source_end_ms=tts_full[key].source_end_ms,
                    pipeline_elapsed_ms=sent_elapsed_ms,
                ),
                field="WebSocket send",
                label=label,
            )
        if set(websocket_sends) != set(tts_full):
            raise ValueError(
                f"{label}: WebSocket and TTS request identities do not match"
            )

    ordered_tts_keys = tuple(sorted(tts_first))
    for key in ordered_tts_keys:
        parent_id = key[0]
        source_end_ms = segments[parent_id].source_end_ms
        if not math.isclose(
            tts_first[key].source_end_ms,
            source_end_ms,
            abs_tol=SOURCE_TIME_TOLERANCE_MS,
        ) or not math.isclose(
            tts_full[key].source_end_ms,
            source_end_ms,
            abs_tol=SOURCE_TIME_TOLERANCE_MS,
        ):
            raise ValueError(
                f"{label}: TTS source boundary does not match its parent segment"
            )
        if tts_first[key].pipeline_elapsed_ms > tts_full[
            key
        ].pipeline_elapsed_ms or (
            schema_version != 3
            and tts_full[key].pipeline_elapsed_ms
            > websocket_sends[key].pipeline_elapsed_ms
        ):
            raise ValueError(
                f"{label}: TTS first/full/WebSocket timing order is invalid"
            )

    tts_response_series = _load_tts_response_series(
        staged,
        pipeline_start_ms=pipeline_start_ms,
        pcm_bytes_per_second=pcm_bytes_per_second,
        tts_started=tts_started,
        tts_first=tts_first,
        tts_full=tts_full,
        tts_audio_bytes=tts_audio_bytes,
        tts_retry_counts=tts_retry_counts,
        label=label,
    )

    _require_numeric_count(
        staged,
        field="segments_emitted",
        expected=len(parent_ids),
        label=label,
    )
    _require_numeric_count(
        staged,
        field="audio_segments_produced",
        expected=len(ordered_tts_keys),
        label=label,
    )
    if schema_version == 2:
        _require_numeric_count(
            staged,
            field="tts_subsegments_produced",
            expected=len(ordered_tts_keys),
            label=label,
        )
    _require_sequence_list(
        staged,
        field="completed_sequence_ids",
        expected=parent_ids,
        label=label,
    )
    _require_sequence_list(
        staged,
        field="websocket_sent_sequence_ids",
        expected=parent_ids,
        label=label,
    )

    def ordered(mapping: dict[Any, TimedObservation]) -> tuple:
        return tuple(mapping[key] for key in sorted(mapping))

    parent_first: list[TimedObservation] = []
    parent_full: list[TimedObservation] = []
    parent_ws_first: list[TimedObservation] = []
    parent_ws_final: list[TimedObservation] = []
    incremental_by_parent = {
        item.identity[0]: item for item in incremental_publications
    }
    for parent_id in parent_ids:
        keys = tuple(key for key in ordered_tts_keys if key[0] == parent_id)
        first = min(
            (tts_first[key] for key in keys),
            key=lambda item: item.pipeline_elapsed_ms,
        )
        full = max(
            (tts_full[key] for key in keys),
            key=lambda item: item.pipeline_elapsed_ms,
        )
        if schema_version == 3:
            publication = incremental_by_parent[parent_id]
            ws_first_elapsed_ms = publication.first_send_elapsed_ms
            ws_final_elapsed_ms = publication.final_send_elapsed_ms
        else:
            ws_first_elapsed_ms = min(
                (websocket_sends[key] for key in keys),
                key=lambda item: item.pipeline_elapsed_ms,
            ).pipeline_elapsed_ms
            ws_final_elapsed_ms = max(
                (websocket_sends[key] for key in keys),
                key=lambda item: item.pipeline_elapsed_ms,
            ).pipeline_elapsed_ms
        parent_first.append(
            TimedObservation(
                identity=(parent_id,),
                source_end_ms=segments[parent_id].source_end_ms,
                pipeline_elapsed_ms=first.pipeline_elapsed_ms,
            )
        )
        parent_full.append(
            TimedObservation(
                identity=(parent_id,),
                source_end_ms=segments[parent_id].source_end_ms,
                pipeline_elapsed_ms=full.pipeline_elapsed_ms,
            )
        )
        parent_ws_first.append(
            TimedObservation(
                identity=(parent_id,),
                source_end_ms=segments[parent_id].source_end_ms,
                pipeline_elapsed_ms=ws_first_elapsed_ms,
            )
        )
        parent_ws_final.append(
            TimedObservation(
                identity=(parent_id,),
                source_end_ms=segments[parent_id].source_end_ms,
                pipeline_elapsed_ms=ws_final_elapsed_ms,
            )
        )

    # ``final_ids`` is deliberately evaluated above even though the neutral
    # output only needs its count; contiguity is part of input validation.
    assert len(final_ids) == len(asr_finals)
    return SampleLatency(
        sample_index=sample_index,
        telemetry_schema_version=schema_version,
        harness_frame_duration_ms=harness_frame_duration_ms,
        asr_finals=ordered(asr_finals),
        segments=ordered(segments),
        tts_first_responses=tuple(tts_first[key] for key in ordered_tts_keys),
        tts_full_responses=tuple(tts_full[key] for key in ordered_tts_keys),
        websocket_sends=tuple(
            websocket_sends[key] for key in ordered_tts_keys
        ),
        parent_tts_first_responses=tuple(parent_first),
        parent_tts_full_responses=tuple(parent_full),
        parent_websocket_first_sends=tuple(parent_ws_first),
        parent_websocket_final_sends=tuple(parent_ws_final),
        tts_response_series=tts_response_series,
        incremental_publications=incremental_publications,
    )


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a distribution without observations")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _distribution(values: Iterable[float]) -> dict[str, Any]:
    observed = tuple(float(value) for value in values)
    if not observed or any(not math.isfinite(value) for value in observed):
        raise ValueError("distributions require finite observations")
    return {
        "observation_count": len(observed),
        "min": min(observed),
        "p50": _nearest_rank(observed, 0.50),
        "p95": _nearest_rank(observed, 0.95),
        "max": max(observed),
        "mean": statistics.fmean(observed),
        "cumulative": sum(observed),
    }


def _optional_distribution(values: Iterable[float]) -> dict[str, Any] | None:
    observed = tuple(values)
    return _distribution(observed) if observed else None


def _response_chunk_metrics(
    samples: Sequence[SampleLatency],
) -> dict[str, Any]:
    series = tuple(
        item
        for sample in samples
        for item in sample.tts_response_series
    )
    if not series:
        return {"available": False}
    multi_response_count = sum(
        item.response_count > 1 for item in series
    )
    response_audio_seconds = (
        duration_ms / 1_000.0
        for item in series
        for duration_ms in item.response_audio_duration_ms
    )
    inter_response_seconds = (
        gap_ms / 1_000.0
        for item in series
        for gap_ms in item.inter_response_arrival_ms
    )
    return {
        "available": True,
        "request_count": len(series),
        "response_chunk_count": sum(
            item.response_count for item in series
        ),
        "multi_response_request_count": multi_response_count,
        "multi_response_request_percent": (
            multi_response_count / len(series) * 100.0
        ),
        "responses_per_request": _distribution(
            item.response_count for item in series
        ),
        "response_audio_duration_seconds": _distribution(
            response_audio_seconds
        ),
        "inter_response_arrival_seconds": _optional_distribution(
            inter_response_seconds
        ),
        "first_response_to_last_response_seconds": _distribution(
            item.first_to_last_seconds for item in series
        ),
        "first_response_to_rpc_complete_seconds": _distribution(
            item.first_to_complete_seconds for item in series
        ),
        "last_response_to_rpc_complete_seconds": _distribution(
            item.last_to_complete_seconds for item in series
        ),
    }


def _observed_metrics(samples: Sequence[SampleLatency]) -> dict[str, Any]:
    def boundaries(attribute: str) -> Iterable[float]:
        for sample in samples:
            for observation in getattr(sample, attribute):
                yield observation.boundary_to_event_seconds

    withheld = tuple(
        seconds
        for sample in samples
        for seconds in sample.tts_withheld_seconds
    )
    return {
        "source_boundary_to_event_seconds": {
            "asr_final": _distribution(boundaries("asr_finals")),
            "segment_emitted": _distribution(boundaries("segments")),
            "tts_first_response": _distribution(
                boundaries("tts_first_responses")
            ),
            "tts_full_response": _distribution(
                boundaries("tts_full_responses")
            ),
            "websocket_send": _distribution(boundaries("websocket_sends")),
        },
        "tts_first_response_to_websocket_send_seconds": (
            _optional_distribution(withheld)
        ),
    }


def _incremental_publication_metrics(
    samples: Sequence[SampleLatency],
) -> dict[str, Any]:
    series = tuple(
        item
        for sample in samples
        for item in sample.incremental_publications
    )
    if not series:
        return {"available": False}
    return {
        "available": True,
        "comparison_basis": (
            "within-parent same generated PCM; TTS completion is the "
            "earliest atomic publication point"
        ),
        "cross_arm_audio_duration_comparison": False,
        "parent_count": len(series),
        "audio_frame_count": sum(item.frame_count for item in series),
        "audio_bytes": sum(item.total_audio_bytes for item in series),
        "frames_per_parent": _distribution(
            item.frame_count for item in series
        ),
        "tts_first_response_to_first_websocket_send_seconds": (
            _distribution(
                item.first_response_to_first_publish_seconds
                for item in series
            )
        ),
        "atomic_withholding_equivalent_seconds": _distribution(
            item.atomic_withholding_equivalent_seconds for item in series
        ),
        "first_publish_lead_over_tts_completion_seconds": _distribution(
            item.first_publish_lead_over_tts_completion_seconds
            for item in series
        ),
        "tts_completion_to_final_websocket_send_seconds": _distribution(
            item.tts_completion_to_final_publish_seconds for item in series
        ),
        "first_to_final_websocket_send_seconds": _distribution(
            item.first_to_final_publish_seconds for item in series
        ),
    }


def _parent_metrics(samples: Sequence[SampleLatency]) -> dict[str, Any]:
    def boundaries(attribute: str) -> Iterable[float]:
        for sample in samples:
            for observation in getattr(sample, attribute):
                yield observation.boundary_to_event_seconds

    return {
        "source_boundary_to_first_tts_response_seconds": _distribution(
            boundaries("parent_tts_first_responses")
        ),
        "source_boundary_to_parent_full_tts_response_seconds": _distribution(
            boundaries("parent_tts_full_responses")
        ),
        "source_boundary_to_first_websocket_send_seconds": _distribution(
            boundaries("parent_websocket_first_sends")
        ),
        "source_boundary_to_parent_final_websocket_send_seconds": _distribution(
            boundaries("parent_websocket_final_sends")
        ),
    }


def _initial_event_payload(observation: TimedObservation) -> dict[str, float]:
    return {
        "source_boundary_seconds": observation.source_end_ms / 1_000.0,
        "pipeline_elapsed_seconds": observation.pipeline_elapsed_ms / 1_000.0,
        "source_boundary_to_event_seconds": (
            observation.boundary_to_event_seconds
        ),
    }


def _initial_path(sample: SampleLatency) -> dict[str, Any]:
    first_asr = min(
        sample.asr_finals,
        key=lambda item: item.pipeline_elapsed_ms,
    )
    first_segment = min(
        sample.segments,
        key=lambda item: item.pipeline_elapsed_ms,
    )
    first_tts = min(
        sample.tts_first_responses,
        key=lambda item: item.pipeline_elapsed_ms,
    )
    request_index = sample.tts_first_responses.index(first_tts)
    full = sample.tts_full_responses[request_index]
    websocket = sample.websocket_sends[request_index]
    result = {
        "asr_final": _initial_event_payload(first_asr),
        "segment_emitted": _initial_event_payload(first_segment),
        "tts_first_response": _initial_event_payload(first_tts),
        "tts_full_response": _initial_event_payload(full),
        "websocket_send": _initial_event_payload(websocket),
        "tts_first_response_to_websocket_send_seconds": (
            None
            if sample.telemetry_schema_version == 3
            else (
                websocket.pipeline_elapsed_ms
                - first_tts.pipeline_elapsed_ms
            )
            / 1_000.0
        ),
    }
    if sample.telemetry_schema_version == 3:
        publication = next(
            item
            for item in sample.incremental_publications
            if item.identity[0] == first_tts.identity[0]
        )
        result.update(
            {
                "first_websocket_send": _initial_event_payload(
                    TimedObservation(
                        identity=publication.identity,
                        source_end_ms=publication.source_end_ms,
                        pipeline_elapsed_ms=(
                            publication.first_send_elapsed_ms
                        ),
                    )
                ),
                "atomic_withholding_equivalent_seconds": (
                    publication.atomic_withholding_equivalent_seconds
                ),
                "tts_first_response_to_first_websocket_send_seconds": (
                    publication.first_response_to_first_publish_seconds
                ),
                "first_publish_lead_over_tts_completion_seconds": (
                    publication
                    .first_publish_lead_over_tts_completion_seconds
                ),
            }
        )
    return result


def _structural_digest(samples: Sequence[SampleLatency]) -> str:
    records: list[list[Any]] = []
    attributes = (
        ("asr_final", "asr_finals"),
        ("segment_emitted", "segments"),
        ("tts_first_response", "tts_first_responses"),
        ("tts_full_response", "tts_full_responses"),
        ("websocket_send", "websocket_sends"),
    )
    for sample in samples:
        for event_name, attribute in attributes:
            for observation in getattr(sample, attribute):
                records.append(
                    [
                        sample.sample_index,
                        sample.telemetry_schema_version,
                        event_name,
                        list(observation.identity),
                        round(observation.source_end_ms, 6),
                        round(observation.pipeline_elapsed_ms, 6),
                    ]
                )
        for series in sample.tts_response_series:
            records.append(
                [
                    sample.sample_index,
                    sample.telemetry_schema_version,
                    "tts_response_series",
                    list(series.identity),
                    series.response_count,
                    series.total_audio_bytes,
                    round(series.first_received_elapsed_ms, 6),
                    round(series.last_received_elapsed_ms, 6),
                    round(series.completed_elapsed_ms, 6),
                    [
                        round(value, 6)
                        for value in series.response_audio_duration_ms
                    ],
                    [
                        round(value, 6)
                        for value in series.inter_response_arrival_ms
                    ],
                ]
            )
        for publication in sample.incremental_publications:
            records.append(
                [
                    sample.sample_index,
                    sample.telemetry_schema_version,
                    "incremental_publication_series",
                    list(publication.identity),
                    round(publication.source_end_ms, 6),
                    publication.total_audio_bytes,
                    list(publication.frame_audio_bytes),
                    [
                        round(value, 6)
                        for value in publication.frame_send_elapsed_ms
                    ],
                    round(publication.tts_first_elapsed_ms, 6),
                    round(publication.tts_completed_elapsed_ms, 6),
                ]
            )
    encoded = json.dumps(
        records,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _round_floats(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {
            key: _round_floats(item, digits) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, digits) for item in value]
    return value


def _sample_payload(sample: SampleLatency) -> dict[str, Any]:
    return {
        "sample": sample.sample_label,
        "telemetry_schema_version": sample.telemetry_schema_version,
        "harness_frame_duration_ms": sample.harness_frame_duration_ms,
        "counts": {
            "asr_finals": len(sample.asr_finals),
            "segments": len(sample.segments),
            "tts_requests": len(sample.tts_first_responses),
            "tts_parents": len(sample.parent_tts_first_responses),
            "websocket_sends": len(sample.websocket_sends),
            "websocket_audio_frames": sum(
                item.frame_count
                for item in sample.incremental_publications
            ),
        },
        "request_level": _observed_metrics((sample,)),
        "parent_level": _parent_metrics((sample,)),
        "incremental_tts_publication": (
            _incremental_publication_metrics((sample,))
        ),
        "tts_response_chunk_diagnostic": _response_chunk_metrics((sample,)),
        "initial_server_path": _initial_path(sample),
    }


def build_analysis(samples: Sequence[SampleLatency]) -> dict[str, Any]:
    """Build a deterministic analysis without copying private source fields."""

    normalized = tuple(samples)
    if not normalized:
        raise ValueError("at least one sample is required")
    if any(
        not isinstance(sample, SampleLatency) for sample in normalized
    ):
        raise ValueError("samples must contain SampleLatency records")
    expected_indices = tuple(range(1, len(normalized) + 1))
    if tuple(sample.sample_index for sample in normalized) != expected_indices:
        raise ValueError("sample indices must be contiguous from one")

    aggregate_counts = {
        "samples": len(normalized),
        "asr_finals": sum(len(sample.asr_finals) for sample in normalized),
        "segments": sum(len(sample.segments) for sample in normalized),
        "tts_requests": sum(
            len(sample.tts_first_responses) for sample in normalized
        ),
        "tts_parents": sum(
            len(sample.parent_tts_first_responses) for sample in normalized
        ),
        "websocket_sends": sum(
            len(sample.websocket_sends) for sample in normalized
        ),
        "websocket_audio_frames": sum(
            item.frame_count
            for sample in normalized
            for item in sample.incremental_publications
        ),
        "tts_response_diagnostic_requests": sum(
            len(sample.tts_response_series) for sample in normalized
        ),
        "tts_response_chunks": sum(
            item.response_count
            for sample in normalized
            for item in sample.tts_response_series
        ),
    }
    frame_durations = sorted(
        {round(sample.harness_frame_duration_ms, 6) for sample in normalized}
    )
    analysis = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "source_format": "completed staged-pipeline batch summary JSON",
        "privacy": {
            "contains_transcript_text": False,
            "contains_audio": False,
            "contains_input_paths_or_filenames": False,
            "contains_endpoints": False,
            "contains_session_ids": False,
            "contains_raw_events": False,
            "sample_labels_are_neutral_ordinals": True,
        },
        "semantics": {
            "latency_formula": (
                "(event monotonic time - pipeline start monotonic time) "
                "- source_end_ms"
            ),
            "source_boundary_unit": "ASR final-level source_end_ms",
            "request_level_tts_unit": (
                "one TTS RPC, identified by parent and subsequence in schema v2"
            ),
            "parent_level_v2_first": "earliest child event for each parent",
            "parent_level_v2_full": "latest child event for each parent",
            "tts_withheld_opportunity": (
                "corresponding WebSocket send time minus server receipt of "
                "the first TTS PCM response; this legacy metric applies only "
                "to atomic telemetry schemas 1 and 2"
            ),
            "tts_withheld_is_an_upper_bound_on_recoverable_delay": True,
            "schema_v3_logical_websocket_send": (
                "final incremental PCM frame for the parent"
            ),
            "schema_v3_atomic_withholding_equivalent": (
                "TTS completed time minus first TTS PCM response time"
            ),
            "schema_v3_first_publish_lead": (
                "TTS completed time minus first WebSocket PCM frame send; "
                "positive values show how much earlier incremental "
                "publication began"
            ),
            "negative_boundary_latency_is_retained": True,
        },
        "caveats": {
            "asr_final_level_source_ranges": (
                "Punctuation-split segments inherit the source range of the "
                "whole contributing ASR final, not a word-accurate boundary "
                "for each emitted segment."
            ),
            "schema_v2_parent_source_ranges": (
                "Every schema-v2 TTS subsequence inherits its parent segment "
                "source range, so request-level distributions weight parents "
                "with more subsequences more heavily."
            ),
            "schema_v3_counterfactual": (
                "The atomic-withholding equivalent measures server-side RPC "
                "buffering avoided by incremental publication within the same "
                "generated parent PCM. It does not compare audio duration "
                "across runs, is not a microphone-to-ear latency reduction, "
                "and does not include browser playback queue behavior."
            ),
            "batch_harness_frame_dispatch": (
                "The standard batch harness dispatches whole 300 ms PCM "
                "frames on a real-time schedule. Source-boundary alignment is "
                "therefore approximate by up to a frame, plus scheduler and "
                "transport effects; it is not a direct microphone-to-ear "
                "audience measurement."
            ),
        },
        "telemetry_schema_versions": sorted(
            {sample.telemetry_schema_version for sample in normalized}
        ),
        "harness_frame_durations_ms": frame_durations,
        "structural_records_sha256": _structural_digest(normalized),
        "aggregate": {
            "counts": aggregate_counts,
            "request_level": _observed_metrics(normalized),
            "parent_level": _parent_metrics(normalized),
            "incremental_tts_publication": (
                _incremental_publication_metrics(normalized)
            ),
            "tts_response_chunk_diagnostic": _response_chunk_metrics(
                normalized
            ),
        },
        "samples": [_sample_payload(sample) for sample in normalized],
    }
    return _round_floats(analysis)


def analyze_paths(paths: Sequence[Path]) -> dict[str, Any]:
    """Load one or more paths in caller-supplied order and build an analysis."""

    normalized = tuple(paths)
    if not normalized:
        raise ValueError("at least one input summary is required")
    resolved = tuple(path.resolve() for path in normalized)
    if len(set(resolved)) != len(resolved):
        raise ValueError("input summary paths must be unique")
    samples = tuple(
        load_summary_latency(path, sample_index=index)
        for index, path in enumerate(normalized, start=1)
    )
    return build_analysis(samples)


def _format_seconds(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}s"


def render_markdown(analysis: dict[str, Any]) -> str:
    """Render a concise, privacy-safe latency report."""

    aggregate = analysis["aggregate"]
    request = aggregate["request_level"]
    boundary = request["source_boundary_to_event_seconds"]
    rows = (
        ("ASR final", boundary["asr_final"]),
        ("Segment emitted", boundary["segment_emitted"]),
        ("TTS first response", boundary["tts_first_response"]),
        ("TTS full response", boundary["tts_full_response"]),
        ("WebSocket send", boundary["websocket_send"]),
        (
            "TTS first response → WebSocket send",
            request["tts_first_response_to_websocket_send_seconds"],
        ),
    )
    lines = [
        "# Streaming Latency Analysis",
        "",
        (
            f"Analyzed {aggregate['counts']['samples']} neutral sample(s), "
            f"{aggregate['counts']['tts_requests']} TTS request(s), and "
            f"{aggregate['counts']['tts_parents']} parent segment(s)."
        ),
        "",
        "## Aggregate request-level latency",
        "",
        "| Boundary or interval | Count | Min | p50 | p95 | Max | Mean | Sum |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, distribution in rows:
        if distribution is None:
            continue
        lines.append(
            "| {name} | {count:,} | {minimum} | {p50} | {p95} | "
            "{maximum} | {mean} | {total} |".format(
                name=name,
                count=distribution["observation_count"],
                minimum=_format_seconds(distribution["min"]),
                p50=_format_seconds(distribution["p50"]),
                p95=_format_seconds(distribution["p95"]),
                maximum=_format_seconds(distribution["max"]),
                mean=_format_seconds(distribution["mean"]),
                total=_format_seconds(distribution["cumulative"]),
            )
        )

    incremental = aggregate["incremental_tts_publication"]
    if incremental["available"]:
        lines.extend(
            [
                "",
                "## Incremental TTS publication (schema v3)",
                "",
                (
                    f"Observed {incremental['audio_frame_count']:,} PCM "
                    f"frames across {incremental['parent_count']:,} parent "
                    "TTS requests. The counterfactual uses each parent's own "
                    "generated PCM and treats TTS completion as the earliest "
                    "possible atomic publication point."
                ),
                "",
                "| Interval | Count | Min | p50 | p95 | Max | Mean |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        incremental_rows = (
            (
                "TTS first PCM → first WebSocket frame",
                incremental[
                    "tts_first_response_to_first_websocket_send_seconds"
                ],
            ),
            (
                "Atomic withholding equivalent (TTS first → complete)",
                incremental["atomic_withholding_equivalent_seconds"],
            ),
            (
                "First-publish lead over TTS completion",
                incremental[
                    "first_publish_lead_over_tts_completion_seconds"
                ],
            ),
            (
                "TTS complete → final WebSocket frame",
                incremental[
                    "tts_completion_to_final_websocket_send_seconds"
                ],
            ),
            (
                "First → final WebSocket frame",
                incremental["first_to_final_websocket_send_seconds"],
            ),
        )
        for name, distribution in incremental_rows:
            lines.append(
                "| {name} | {count:,} | {minimum} | {p50} | {p95} | "
                "{maximum} | {mean} |".format(
                    name=name,
                    count=distribution["observation_count"],
                    minimum=_format_seconds(distribution["min"]),
                    p50=_format_seconds(distribution["p50"]),
                    p95=_format_seconds(distribution["p95"]),
                    maximum=_format_seconds(distribution["max"]),
                    mean=_format_seconds(distribution["mean"]),
                )
            )

    response_diagnostic = aggregate["tts_response_chunk_diagnostic"]
    if response_diagnostic["available"]:
        responses = response_diagnostic["responses_per_request"]
        lines.extend(
            [
                "",
                "## TTS response-chunk diagnostic",
                "",
                (
                    f"Observed {response_diagnostic['response_chunk_count']:,} "
                    "PCM responses across "
                    f"{response_diagnostic['request_count']:,} atomic TTS "
                    "requests. "
                    f"{response_diagnostic['multi_response_request_percent']:.1f}% "
                    "of requests produced more than one response."
                ),
                "",
                (
                    "Responses per request: "
                    f"p50 {responses['p50']:.0f}, "
                    f"p95 {responses['p95']:.0f}, "
                    f"max {responses['max']:.0f}."
                ),
                "",
                "| Interval | Count | p50 | p95 | Max | Mean |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        chunk_rows = (
            (
                "PCM duration per response",
                response_diagnostic["response_audio_duration_seconds"],
            ),
            (
                "Inter-response arrival",
                response_diagnostic["inter_response_arrival_seconds"],
            ),
            (
                "First response → last response",
                response_diagnostic[
                    "first_response_to_last_response_seconds"
                ],
            ),
            (
                "First response → RPC complete",
                response_diagnostic[
                    "first_response_to_rpc_complete_seconds"
                ],
            ),
            (
                "Last response → RPC complete",
                response_diagnostic[
                    "last_response_to_rpc_complete_seconds"
                ],
            ),
        )
        for name, distribution in chunk_rows:
            if distribution is None:
                continue
            lines.append(
                "| {name} | {count:,} | {p50} | {p95} | {maximum} | "
                "{mean} |".format(
                    name=name,
                    count=distribution["observation_count"],
                    p50=_format_seconds(distribution["p50"]),
                    p95=_format_seconds(distribution["p95"]),
                    maximum=_format_seconds(distribution["max"]),
                    mean=_format_seconds(distribution["mean"]),
                )
            )

    lines.extend(
        [
            "",
            "## Initial server path",
            "",
            (
                "| Sample | Frame | First ASR final | First segment | "
                "First TTS PCM | Full TTS | WebSocket send | Withheld |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for sample in analysis["samples"]:
        initial = sample["initial_server_path"]
        lines.append(
            "| {sample} | {frame:.0f}ms | {asr} | {segment} | {first} | "
            "{full} | {sent} | {withheld} |".format(
                sample=sample["sample"],
                frame=sample["harness_frame_duration_ms"],
                asr=_format_seconds(
                    initial["asr_final"]["pipeline_elapsed_seconds"]
                ),
                segment=_format_seconds(
                    initial["segment_emitted"]["pipeline_elapsed_seconds"]
                ),
                first=_format_seconds(
                    initial["tts_first_response"]["pipeline_elapsed_seconds"]
                ),
                full=_format_seconds(
                    initial["tts_full_response"]["pipeline_elapsed_seconds"]
                ),
                sent=_format_seconds(
                    initial["websocket_send"]["pipeline_elapsed_seconds"]
                ),
                withheld=_format_seconds(
                    initial[
                        "tts_first_response_to_websocket_send_seconds"
                    ]
                ),
            )
        )

    caveats = analysis["caveats"]
    lines.extend(
        [
            "",
            "## Interpretation boundaries",
            "",
            (
                "Latency is `(event monotonic time - pipeline start) - "
                "source_end_ms`. The TTS first-response-to-WebSocket interval "
                "is an upper bound on delay recoverable by forwarding PCM "
                "responses incrementally; client scheduling can recover less."
            ),
            "",
            caveats["asr_final_level_source_ranges"],
            "",
            caveats["schema_v2_parent_source_ranges"],
            "",
            caveats["schema_v3_counterfactual"],
            "",
            caveats["batch_harness_frame_dispatch"],
            "",
            (
                "No transcript text, audio, input path or filename, endpoint, "
                "session identifier, or raw event is copied into this report."
            ),
            "",
            (
                "Structural record digest: "
                f"`{analysis['structural_records_sha256']}`."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze source-boundary and buffered-TTS latency from completed "
            "staged batch summaries"
        )
    )
    parser.add_argument(
        "summary_json",
        nargs="+",
        type=Path,
        help="one or more completed staged *_summary.json files",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def validate_output_destinations(
    input_paths: Sequence[Path],
    *,
    json_output: Path | None,
    markdown_output: Path | None,
) -> None:
    """Reject aliases that could overwrite input or another output."""

    outputs = tuple(
        path for path in (json_output, markdown_output) if path is not None
    )
    if len({path.resolve() for path in outputs}) != len(outputs):
        raise ValueError("JSON and Markdown outputs must be different files")
    resolved_inputs = {path.resolve() for path in input_paths}
    if any(path.resolve() in resolved_inputs for path in outputs):
        raise ValueError("an output file cannot overwrite an input summary")


def main(argv: Sequence[str] | None = None) -> None:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    try:
        validate_output_destinations(
            args.summary_json,
            json_output=args.json_output,
            markdown_output=args.markdown_output,
        )
        analysis = analyze_paths(args.summary_json)
    except ValueError as exc:
        parser.error(str(exc))

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(analysis, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {args.json_output}")
    if args.markdown_output is not None:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(
            render_markdown(analysis),
            encoding="utf-8",
        )
        print(f"Wrote {args.markdown_output}")
    if args.json_output is None and args.markdown_output is None:
        print(render_markdown(analysis), end="")


if __name__ == "__main__":
    main()
