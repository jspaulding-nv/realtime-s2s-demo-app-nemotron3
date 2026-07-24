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
SUPPORTED_TELEMETRY_SCHEMA_VERSIONS = {1, 2}
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

    @property
    def sample_label(self) -> str:
        return f"sample_{self.sample_index:02d}"

    @property
    def tts_withheld_seconds(self) -> tuple[float, ...]:
        return tuple(
            (sent.pipeline_elapsed_ms - first.pipeline_elapsed_ms) / 1_000.0
            for first, sent in zip(
                self.tts_first_responses,
                self.websocket_sends,
            )
        )


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
    if schema_version == 1:
        if event.get("subsequence_id") is not None:
            raise ValueError(
                f"{label}: {field} has subsequence identity in schema v1"
            )
        if event.get("subsequence_count") is not None:
            raise ValueError(
                f"{label}: {field} has subsequence identity in schema v1"
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

    schema_version = staged.get("telemetry_schema_version", 1)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_TELEMETRY_SCHEMA_VERSIONS
    ):
        raise ValueError(
            f"{label}: telemetry_schema_version must be one or two"
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

    websocket_events = _require_list(
        staged.get("websocket_send_events"),
        field="staged_pipeline.websocket_send_events",
        label=label,
    )
    websocket_sends: dict[tuple[int, int, int], TimedObservation] = {}
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
        if (
            tts_first[key].pipeline_elapsed_ms
            > tts_full[key].pipeline_elapsed_ms
            or tts_full[key].pipeline_elapsed_ms
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
        ws_first = min(
            (websocket_sends[key] for key in keys),
            key=lambda item: item.pipeline_elapsed_ms,
        )
        ws_final = max(
            (websocket_sends[key] for key in keys),
            key=lambda item: item.pipeline_elapsed_ms,
        )
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
                pipeline_elapsed_ms=ws_first.pipeline_elapsed_ms,
            )
        )
        parent_ws_final.append(
            TimedObservation(
                identity=(parent_id,),
                source_end_ms=segments[parent_id].source_end_ms,
                pipeline_elapsed_ms=ws_final.pipeline_elapsed_ms,
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
        "tts_first_response_to_websocket_send_seconds": _distribution(
            seconds
            for sample in samples
            for seconds in sample.tts_withheld_seconds
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
    return {
        "asr_final": _initial_event_payload(first_asr),
        "segment_emitted": _initial_event_payload(first_segment),
        "tts_first_response": _initial_event_payload(first_tts),
        "tts_full_response": _initial_event_payload(full),
        "websocket_send": _initial_event_payload(websocket),
        "tts_first_response_to_websocket_send_seconds": (
            websocket.pipeline_elapsed_ms - first_tts.pipeline_elapsed_ms
        )
        / 1_000.0,
    }


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
        },
        "request_level": _observed_metrics((sample,)),
        "parent_level": _parent_metrics((sample,)),
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
                "the first TTS PCM response"
            ),
            "tts_withheld_is_an_upper_bound_on_recoverable_delay": True,
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
