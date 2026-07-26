#!/usr/bin/env python3
"""Compare fixed 1.00x and adaptive playback on captured S2S event traces."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Sequence

from playback_simulation import (
    DEFAULT_PLAYBACK_POLICY,
    AudioChunk,
    PlaybackPolicy,
    PlaybackSimulation,
    PlaybackSummary,
    ScheduledChunk,
    simulate_playback,
)


SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
CAPACITY_LIMIT_SECONDS = 10.0
BURST_WINDOWS_SECONDS = (30, 60, 300)
# Retained legacy captures formatted arrival timestamps to 0.01 ms and
# source/receipt offsets to 0.001 ms. Their independent half-unit rounding
# errors can sum to 0.006 ms; new captures use exact round-trip floats, while
# this analyzer tolerance preserves validation of those older trace artifacts.
CSV_TIMING_SERIALIZATION_EPSILON_MS = 0.006001
REQUIRED_COLUMNS = {
    "source",
    "stage",
    "timestamp_ms",
    "chunk_index",
    "audio_bytes",
}


@dataclass(frozen=True)
class AudioFrameAttribution:
    """Protocol-v1 source attribution for one translated-audio frame."""

    protocol_version: int | None
    stream_generation: int | None
    parent_sequence_id: int | None
    audio_frame_id: int | None
    source_start_ms: float | None
    source_end_ms: float | None
    source_end_to_receipt_ms: float | None


@dataclass(frozen=True)
class PlaybackTrace:
    path: Path
    input_end_seconds: float
    input_boundary_source: str
    legacy_last_chunk_start_seconds: float | None
    chunks: tuple[AudioChunk, ...]
    sha256: str
    frame_attributions: tuple[AudioFrameAttribution, ...] = ()
    audio_metadata_protocol_version: int | None = None
    audio_metadata_stream_generation: int | None = None


def _optional_csv_int(
    row: dict[str, str],
    field_name: str,
    *,
    path: Path,
    row_number: int,
) -> int | None:
    raw_value = row.get(field_name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{path}:{row_number}: {field_name} must be an integer"
        ) from exc
    if value < 0:
        raise ValueError(
            f"{path}:{row_number}: {field_name} must be non-negative"
        )
    return value


def _optional_csv_float(
    row: dict[str, str],
    field_name: str,
    *,
    path: Path,
    row_number: int,
    non_negative: bool,
) -> float | None:
    raw_value = row.get(field_name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{path}:{row_number}: {field_name} must be a number"
        ) from exc
    if not math.isfinite(value) or (non_negative and value < 0):
        qualifier = "finite and non-negative" if non_negative else "finite"
        raise ValueError(
            f"{path}:{row_number}: {field_name} must be {qualifier}"
        )
    return value


def _load_audio_frame_attribution(
    row: dict[str, str],
    *,
    path: Path,
    row_number: int,
) -> AudioFrameAttribution:
    protocol_version = _optional_csv_int(
        row,
        "protocol_version",
        path=path,
        row_number=row_number,
    )
    stream_generation = _optional_csv_int(
        row,
        "stream_generation",
        path=path,
        row_number=row_number,
    )
    parent_sequence_id = _optional_csv_int(
        row,
        "parent_sequence_id",
        path=path,
        row_number=row_number,
    )
    audio_frame_id = _optional_csv_int(
        row,
        "audio_frame_id",
        path=path,
        row_number=row_number,
    )
    address_values = (
        protocol_version,
        stream_generation,
        parent_sequence_id,
        audio_frame_id,
    )
    if any(value is None for value in address_values) and any(
        value is not None for value in address_values
    ):
        raise ValueError(
            f"{path}:{row_number}: protocol_version, stream_generation, "
            "parent_sequence_id, and audio_frame_id must either all be "
            "present or all be absent"
        )
    source_start_ms = _optional_csv_float(
        row,
        "source_start_ms",
        path=path,
        row_number=row_number,
        non_negative=True,
    )
    source_end_ms = _optional_csv_float(
        row,
        "source_end_ms",
        path=path,
        row_number=row_number,
        non_negative=True,
    )
    if (
        source_start_ms is not None
        and source_end_ms is not None
        and source_end_ms < source_start_ms
    ):
        raise ValueError(
            f"{path}:{row_number}: source_end_ms cannot precede "
            "source_start_ms"
        )
    source_end_to_receipt_ms = _optional_csv_float(
        row,
        "source_end_to_receipt_ms",
        path=path,
        row_number=row_number,
        non_negative=False,
    )
    if protocol_version is None and any(
        value is not None
        for value in (
            source_start_ms,
            source_end_ms,
            source_end_to_receipt_ms,
        )
    ):
        raise ValueError(
            f"{path}:{row_number}: source timing metadata requires complete "
            "protocol-v1 frame metadata"
        )
    if source_start_ms is not None and source_end_ms is None:
        raise ValueError(
            f"{path}:{row_number}: source_start_ms requires source_end_ms"
        )
    if (source_end_ms is None) != (source_end_to_receipt_ms is None):
        raise ValueError(
            f"{path}:{row_number}: source_end_ms and "
            "source_end_to_receipt_ms must either both be present or both "
            "be absent"
        )
    return AudioFrameAttribution(
        protocol_version=protocol_version,
        stream_generation=stream_generation,
        parent_sequence_id=parent_sequence_id,
        audio_frame_id=audio_frame_id,
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
        source_end_to_receipt_ms=source_end_to_receipt_ms,
    )


def _validate_audio_frame_attributions(
    attributions: Sequence[AudioFrameAttribution],
    *,
    path: Path,
) -> tuple[int | None, int | None]:
    """Validate one ordered CSV receive stream as legacy or protocol-v1."""

    attributed = [
        attribution.protocol_version is not None
        for attribution in attributions
    ]
    if any(attributed) and not all(attributed):
        raise ValueError(
            f"{path}: every client audio_received row must either carry "
            "complete protocol-v1 metadata or every row must omit it"
        )
    if not any(attributed):
        return None, None

    protocol_version = attributions[0].protocol_version
    stream_generation = attributions[0].stream_generation
    if protocol_version != 1:
        raise ValueError(f"{path}: protocol_version must equal 1")
    if stream_generation is None or stream_generation <= 0:
        raise ValueError(
            f"{path}: stream_generation must be a positive integer"
        )

    active_parent = -1
    expected_frame = 0
    active_source_range: tuple[float | None, float | None] | None = None
    for attribution in attributions:
        if attribution.protocol_version != protocol_version:
            raise ValueError(
                f"{path}: protocol_version changed within one trace"
            )
        if attribution.stream_generation != stream_generation:
            raise ValueError(
                f"{path}: stream_generation changed within one trace"
            )
        parent_sequence_id = attribution.parent_sequence_id
        audio_frame_id = attribution.audio_frame_id
        if parent_sequence_id is None or audio_frame_id is None:
            raise RuntimeError(
                "complete protocol-v1 attribution unexpectedly became absent"
            )

        if parent_sequence_id == active_parent:
            if audio_frame_id != expected_frame:
                raise ValueError(
                    f"{path}: parent {parent_sequence_id} audio_frame_id "
                    f"must be contiguous from 0; expected {expected_frame}, "
                    f"received {audio_frame_id}"
                )
        elif parent_sequence_id == active_parent + 1:
            if audio_frame_id != 0:
                raise ValueError(
                    f"{path}: parent {parent_sequence_id} audio_frame_id "
                    "must start at 0"
                )
            active_parent = parent_sequence_id
            expected_frame = 0
            active_source_range = None
        elif parent_sequence_id <= active_parent:
            raise ValueError(
                f"{path}: parent {parent_sequence_id} re-entered after a "
                "later parent began"
            )
        else:
            raise ValueError(
                f"{path}: parent_sequence_id must be contiguous from 0; "
                f"expected {active_parent + 1}, received {parent_sequence_id}"
            )

        source_range = (
            attribution.source_start_ms,
            attribution.source_end_ms,
        )
        if active_source_range is None:
            active_source_range = source_range
        elif source_range != active_source_range:
            raise ValueError(
                f"{path}: parent {parent_sequence_id} has inconsistent "
                "source ranges"
            )
        expected_frame = audio_frame_id + 1

    return protocol_version, stream_generation


def load_event_trace(
    path: Path,
    *,
    sample_rate: int = SAMPLE_RATE,
    bytes_per_sample: int = BYTES_PER_SAMPLE,
) -> PlaybackTrace:
    """Load client send/receive events from a batch-test CSV."""

    if sample_rate <= 0 or bytes_per_sample <= 0:
        raise ValueError("sample_rate and bytes_per_sample must be positive")

    hasher = hashlib.sha256()
    with path.open("rb") as binary_handle:
        for block in iter(lambda: binary_handle.read(1024 * 1024), b""):
            hasher.update(block)
    digest = hasher.hexdigest()
    received: list[
        tuple[float, int, AudioChunk, AudioFrameAttribution]
    ] = []
    last_chunk_sent_seconds: float | None = None
    last_chunk_end_seconds: float | None = None
    explicit_input_end_seconds: float | None = None

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{path}: missing required columns: {', '.join(sorted(missing))}"
            )

        for row_number, row in enumerate(reader, start=2):
            if row["source"] != "client":
                continue
            try:
                timestamp_seconds = float(row["timestamp_ms"]) / 1000.0
                chunk_index = int(row["chunk_index"])
                audio_bytes = int(row["audio_bytes"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{path}:{row_number}: invalid numeric event value"
                ) from exc

            if not math.isfinite(timestamp_seconds) or timestamp_seconds < 0:
                raise ValueError(
                    f"{path}:{row_number}: timestamp must be finite and non-negative"
                )

            if row["stage"] == "chunk_sent":
                if audio_bytes <= 0:
                    raise ValueError(
                        f"{path}:{row_number}: chunk_sent bytes must be positive"
                    )
                last_chunk_sent_seconds = max(
                    last_chunk_sent_seconds or 0.0, timestamp_seconds
                )
                chunk_end_seconds = timestamp_seconds + (
                    audio_bytes / (sample_rate * bytes_per_sample)
                )
                last_chunk_end_seconds = max(
                    last_chunk_end_seconds or 0.0, chunk_end_seconds
                )
            elif row["stage"] == "input_ended":
                explicit_input_end_seconds = max(
                    explicit_input_end_seconds or 0.0, timestamp_seconds
                )
            elif row["stage"] == "audio_received":
                if audio_bytes <= 0:
                    raise ValueError(
                        f"{path}:{row_number}: audio_received bytes must be positive"
                    )
                received.append(
                    (
                        timestamp_seconds,
                        row_number,
                        AudioChunk(
                            arrival_seconds=timestamp_seconds,
                            duration_seconds=(
                                audio_bytes / (sample_rate * bytes_per_sample)
                            ),
                            audio_bytes=audio_bytes,
                            source_index=chunk_index,
                        ),
                        _load_audio_frame_attribution(
                            row,
                            path=path,
                            row_number=row_number,
                        ),
                    )
                )

    if explicit_input_end_seconds is not None:
        input_end_seconds = explicit_input_end_seconds
        input_boundary_source = "explicit_input_ended"
        legacy_last_chunk_start_seconds = None
    else:
        input_end_seconds = last_chunk_end_seconds
        input_boundary_source = "estimated_last_chunk_end"
        legacy_last_chunk_start_seconds = last_chunk_sent_seconds
    if input_end_seconds is None:
        raise ValueError(f"{path}: no client input boundary events found")
    if not received:
        raise ValueError(f"{path}: no client audio_received events found")

    received.sort(key=lambda item: (item[0], item[1]))
    frame_attributions = tuple(item[3] for item in received)
    (
        audio_metadata_protocol_version,
        audio_metadata_stream_generation,
    ) = _validate_audio_frame_attributions(
        frame_attributions,
        path=path,
    )
    return PlaybackTrace(
        path=path,
        input_end_seconds=input_end_seconds,
        input_boundary_source=input_boundary_source,
        legacy_last_chunk_start_seconds=legacy_last_chunk_start_seconds,
        chunks=tuple(item[2] for item in received),
        frame_attributions=frame_attributions,
        sha256=digest,
        audio_metadata_protocol_version=(
            audio_metadata_protocol_version
        ),
        audio_metadata_stream_generation=audio_metadata_stream_generation,
    )


def _round_floats(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _round_floats(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, digits) for item in value]
    return value


def _compact_summary(summary: PlaybackSummary) -> dict[str, Any]:
    return _round_floats(asdict(summary))


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    """Return a compact nearest-rank distribution without inventing zeros."""

    if not values:
        return {
            "count": 0,
            "p50": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(values),
        "p50": _nearest_rank(values, 0.50),
        "p95": _nearest_rank(values, 0.95),
        "max": max(values),
    }


def _quartile_drift(
    ordered_values: Sequence[float],
) -> dict[str, Any]:
    """Compare final- and first-quartile medians in source-frontier order."""

    sample_count = len(ordered_values)
    sufficient = sample_count >= 4
    if not sufficient:
        return {
            "count": sample_count,
            "minimum_count_required": 4,
            "sufficient_sample_count": False,
            "quartile_count": 0,
            "first_quartile_median_seconds": None,
            "final_quartile_median_seconds": None,
            "final_minus_first_seconds": None,
        }
    quartile_count = math.ceil(sample_count / 4)
    first_median = median(ordered_values[:quartile_count])
    final_median = median(ordered_values[-quartile_count:])
    return {
        "count": sample_count,
        "minimum_count_required": 4,
        "sufficient_sample_count": True,
        "quartile_count": quartile_count,
        "first_quartile_median_seconds": first_median,
        "final_quartile_median_seconds": final_median,
        "final_minus_first_seconds": final_median - first_median,
    }


def _source_frontier_metrics(
    trace: PlaybackTrace,
    simulation: PlaybackSimulation,
) -> dict[str, Any]:
    """Project parent source ends onto deterministic scheduled playback.

    For each protocol-v1 parent, the first frame supplies the scheduled-start
    projection and the last frame supplies the scheduled-end projection.
    ``source_end_to_receipt_ms`` and all scheduling offsets share the capture
    client's monotonic clock domain.
    """

    frame_attributions = trace.frame_attributions
    if not frame_attributions:
        frame_attributions = tuple(
            AudioFrameAttribution(
                protocol_version=None,
                stream_generation=None,
                parent_sequence_id=None,
                audio_frame_id=None,
                source_start_ms=None,
                source_end_ms=None,
                source_end_to_receipt_ms=None,
            )
            for _ in trace.chunks
        )
    if len(frame_attributions) != len(trace.chunks):
        raise ValueError(
            f"{trace.path}: audio attribution count does not match chunk count"
        )
    if len(simulation.schedule) != len(trace.chunks):
        raise ValueError(
            f"{trace.path}: scheduled chunk count does not match trace"
        )

    parent_frames: dict[
        int,
        list[
            tuple[
                AudioFrameAttribution,
                AudioChunk,
                ScheduledChunk,
            ]
        ],
    ] = {}
    attributed_frame_count = 0
    for attribution, chunk, scheduled in zip(
        frame_attributions,
        trace.chunks,
        simulation.schedule,
        strict=True,
    ):
        if attribution.parent_sequence_id is None:
            continue
        attributed_frame_count += 1
        parent_frames.setdefault(
            attribution.parent_sequence_id, []
        ).append((attribution, chunk, scheduled))

    start_records: list[tuple[float, int, float]] = []
    end_records: list[tuple[float, int, float]] = []
    bounded_start_records: list[tuple[float, int, float]] = []
    bounded_end_records: list[tuple[float, int, float]] = []
    semantic_proxy_eligible_parent_count = 0
    frame_addressable_parent_count = 0

    for parent_sequence_id, frames in parent_frames.items():
        frame_ids = [
            attribution.audio_frame_id
            for attribution, _, _ in frames
        ]
        if any(frame_id is None for frame_id in frame_ids):
            continue
        concrete_frame_ids = [
            int(frame_id) for frame_id in frame_ids if frame_id is not None
        ]
        if len(set(concrete_frame_ids)) != len(concrete_frame_ids):
            raise ValueError(
                f"{trace.path}: parent {parent_sequence_id} has duplicate "
                "audio_frame_id values"
            )
        frame_addressable_parent_count += 1
        ordered_frames = [
            frame
            for _, frame in sorted(
                zip(concrete_frame_ids, frames, strict=True),
                key=lambda item: item[0],
            )
        ]
        first_attribution, first_chunk, first_scheduled = ordered_frames[0]
        last_attribution, last_chunk, last_scheduled = ordered_frames[-1]

        source_ranges = {
            (attribution.source_start_ms, attribution.source_end_ms)
            for attribution, _, _ in ordered_frames
        }
        if len(source_ranges) > 1:
            raise ValueError(
                f"{trace.path}: parent {parent_sequence_id} has inconsistent "
                "source ranges"
            )
        has_bounded_source_range = all(
            attribution.source_start_ms is not None
            and attribution.source_end_ms is not None
            for attribution, _, _ in ordered_frames
        )

        start_delay_seconds: float | None = None
        if (
            first_attribution.source_end_ms is not None
            and first_attribution.source_end_to_receipt_ms is not None
        ):
            start_delay_seconds = (
                first_attribution.source_end_to_receipt_ms / 1000.0
                + first_scheduled.start_seconds
                - first_chunk.arrival_seconds
            )
            start_records.append(
                (
                    first_attribution.source_end_ms,
                    parent_sequence_id,
                    start_delay_seconds,
                )
            )

        end_delay_seconds: float | None = None
        if (
            last_attribution.source_end_ms is not None
            and last_attribution.source_end_to_receipt_ms is not None
        ):
            end_delay_seconds = (
                last_attribution.source_end_to_receipt_ms / 1000.0
                + last_scheduled.end_seconds
                - last_chunk.arrival_seconds
            )
            end_records.append(
                (
                    last_attribution.source_end_ms,
                    parent_sequence_id,
                    end_delay_seconds,
                )
            )

        if (
            has_bounded_source_range
            and start_delay_seconds is not None
            and end_delay_seconds is not None
        ):
            semantic_proxy_eligible_parent_count += 1
            bounded_start_records.append(start_records[-1])
            bounded_end_records.append(end_records[-1])

    start_delays = [record[2] for record in start_records]
    end_delays = [record[2] for record in end_records]
    ordered_start_delays = [
        record[2] for record in sorted(bounded_start_records)
    ]
    ordered_end_delays = [
        record[2] for record in sorted(bounded_end_records)
    ]
    measured_parent_envelope_count = len(
        {
            (source_end_ms, parent_sequence_id)
            for source_end_ms, parent_sequence_id, _ in start_records
        }.intersection(
            {
                (source_end_ms, parent_sequence_id)
                for source_end_ms, parent_sequence_id, _ in end_records
            }
        )
    )
    if measured_parent_envelope_count == 0:
        semantic_status = "unavailable"
    elif (
        semantic_proxy_eligible_parent_count
        == measured_parent_envelope_count
    ):
        semantic_status = "eligible_parent_range_proxy"
    elif semantic_proxy_eligible_parent_count:
        semantic_status = "partially_eligible_parent_range_proxy"
    else:
        semantic_status = "ineligible_missing_bounded_source_ranges"

    return _round_floats(
        {
            "availability": (
                "available"
                if start_records or end_records
                else "unavailable"
            ),
            "attributed_frame_count": attributed_frame_count,
            "attributed_parent_count": len(parent_frames),
            "frame_addressable_parent_count": (
                frame_addressable_parent_count
            ),
            "measured_parent_envelope_count": (
                measured_parent_envelope_count
            ),
            "completion_proven_by_trace_csv": False,
            "completion_evidence_note": (
                "The event CSV contains frame addresses but no validated "
                "parent-completion markers; completion must be bound from "
                "the adjacent summary audio_metadata_observation."
            ),
            "parent_envelope_selection": {
                "start": (
                    "scheduled start of the minimum audio_frame_id in each "
                    "parent"
                ),
                "end": (
                    "scheduled end of the maximum audio_frame_id in each "
                    "parent"
                ),
            },
            "source_end_to_first_frame_scheduled_start_seconds": (
                _distribution(start_delays)
            ),
            "source_end_to_last_frame_scheduled_end_seconds": (
                _distribution(end_delays)
            ),
            "accumulated_drift": {
                "definition": (
                    "final-quartile median minus first-quartile median, "
                    "ordered by source_end_ms then parent_sequence_id; only "
                    "parents with both source bounds are eligible and at "
                    "least four eligible parents are required"
                ),
                "first_frame_scheduled_start_seconds": (
                    _quartile_drift(ordered_start_delays)
                ),
                "last_frame_scheduled_end_seconds": (
                    _quartile_drift(ordered_end_delays)
                ),
            },
            "semantic_proxy_eligibility": {
                "status": semantic_status,
                "eligible_parent_count": (
                    semantic_proxy_eligible_parent_count
                ),
                "ineligible_measured_parent_count": (
                    measured_parent_envelope_count
                    - semantic_proxy_eligible_parent_count
                ),
                "criterion": (
                    "both source_start_ms and source_end_ms exist on every "
                    "frame in the measured parent"
                ),
            },
            "claim_scope": {
                "scheduled_digital_playback_projection": True,
                "dac_or_acoustic_audibility_measured": False,
                "exact_semantic_landmark_measured": False,
                "exact_joke_or_punchline_delay_measured": False,
                "caveat": (
                    "Even an eligible bounded parent is only a coarse "
                    "source-range envelope; exact semantic delay requires a "
                    "reviewed source marker and matching target-language "
                    "landmark on a common output clock."
                ),
            },
        }
    )


def _dedupe_positive_floats(
    values: Sequence[float] | None,
    *,
    option_name: str,
) -> tuple[float, ...]:
    """Validate positive finite values and preserve first-seen order."""

    normalized: list[float] = []
    seen: set[float] = set()
    for raw_value in values or ():
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{option_name} values must be numbers") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"{option_name} values must be finite and positive"
            )
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return tuple(normalized)


def normalize_capacity_sweep_options(
    constant_rates: Sequence[float] | None,
    media_duration_scales: Sequence[float] | None,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Validate and normalize optional constant-rate sweep dimensions."""

    rates = _dedupe_positive_floats(
        constant_rates,
        option_name="constant rate",
    )
    scales = _dedupe_positive_floats(
        media_duration_scales,
        option_name="media duration scale",
    )
    if not rates:
        if scales:
            raise ValueError(
                "media duration scales require at least one constant rate"
            )
        return (), ()
    return rates, scales or (1.0,)


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _rolling_arrival_rate_p95(
    chunks: Sequence[AudioChunk],
    *,
    window_seconds: float,
    observation_end_seconds: float,
) -> float:
    """Return nearest-rank p95 of wall-clock media arrival rate.

    Windows start at whole wall-clock seconds and use ``[s, s + window)``.
    Every normal window is fully contained before end-of-input. A trace shorter
    than the requested window uses one window starting at zero. The denominator
    is always the full requested window, so time without translated-media
    arrivals contributes zero.
    """

    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("rolling window must be finite and positive")
    if (
        not math.isfinite(observation_end_seconds)
        or observation_end_seconds < 0
    ):
        raise ValueError(
            "rolling observation end must be finite and non-negative"
        )

    starts = range(
        max(0, math.floor(observation_end_seconds - window_seconds)) + 1
    )
    left = 0
    right = 0
    window_media_seconds = 0.0
    rates: list[float] = []
    for start_seconds in starts:
        end_seconds = start_seconds + window_seconds
        captured_end_seconds = min(end_seconds, observation_end_seconds)
        while (
            right < len(chunks)
            and chunks[right].arrival_seconds < captured_end_seconds
        ):
            window_media_seconds += chunks[right].duration_seconds
            right += 1
        while (
            left < right
            and chunks[left].arrival_seconds < start_seconds
        ):
            window_media_seconds -= chunks[left].duration_seconds
            left += 1
        rates.append(window_media_seconds / window_seconds)
    return _nearest_rank(rates, 0.95)


def _burst_diagnostics(trace: PlaybackTrace) -> dict[str, Any]:
    durations = [chunk.duration_seconds for chunk in trace.chunks]
    return _round_floats(
        {
            "translated_audio_chunk_duration_seconds": {
                "p50": _nearest_rank(durations, 0.50),
                "p95": _nearest_rank(durations, 0.95),
                "max": max(durations),
            },
            "rolling_translated_media_arrival_rate_p95_x_realtime": {
                f"{window}_seconds": _rolling_arrival_rate_p95(
                    trace.chunks,
                    window_seconds=float(window),
                    observation_end_seconds=trace.input_end_seconds,
                )
                for window in BURST_WINDOWS_SECONDS
            },
        }
    )


def _constant_rate_scenario(
    trace: PlaybackTrace,
    *,
    constant_rate: float,
    media_duration_scale: float,
) -> dict[str, Any]:
    scaled_chunks = tuple(
        AudioChunk(
            arrival_seconds=chunk.arrival_seconds,
            duration_seconds=chunk.duration_seconds * media_duration_scale,
            audio_bytes=chunk.audio_bytes,
            source_index=chunk.source_index,
        )
        for chunk in trace.chunks
    )
    constant_policy = PlaybackPolicy(
        target_queue_seconds=5.0,
        urgent_queue_seconds=8.0,
        limit_queue_seconds=CAPACITY_LIMIT_SECONDS,
        catch_up_release_seconds=4.0,
        urgent_release_seconds=7.0,
        normal_rate=constant_rate,
        catch_up_rate=constant_rate,
        urgent_rate=constant_rate,
    )
    summary = simulate_playback(
        scaled_chunks,
        input_end_seconds=trace.input_end_seconds,
        adaptive=False,
        policy=constant_policy,
    ).summary
    return _round_floats(
        {
            "constant_rate": constant_rate,
            "media_duration_scale": media_duration_scale,
            "no_drop_listener_tail_seconds": summary.listener_tail_seconds,
            "time_weighted_queue_p50_seconds": (
                summary.time_weighted_queue_p50_seconds
            ),
            "time_weighted_queue_p95_seconds": (
                summary.time_weighted_queue_p95_seconds
            ),
            "peak_queue_depth_seconds": summary.peak_queue_depth_seconds,
            "seconds_above_10_seconds": summary.seconds_above_limit,
            "percent_playback_window_above_10_seconds": (
                summary.percent_playback_window_above_limit
            ),
            "chunks_dropped": summary.chunks_dropped,
        }
    )


def build_capacity_sweep(
    traces: Sequence[PlaybackTrace],
    *,
    constant_rates: Sequence[float],
    media_duration_scales: Sequence[float],
) -> dict[str, Any]:
    """Build an offline, no-drop constant-rate capacity sweep."""

    rates, scales = normalize_capacity_sweep_options(
        constant_rates,
        media_duration_scales,
    )
    if not rates:
        raise ValueError("at least one constant rate is required")

    return {
        "constant_rates": list(rates),
        "media_duration_scales": list(scales),
        "semantics": {
            "offline_replay_only": True,
            "preserve_every_chunk": True,
            "arrival_timestamps_and_input_end_are_unchanged": True,
            "media_duration_scale_multiplies_each_translated_chunk": True,
            "burst_diagnostics_use_captured_unscaled_media_durations": True,
            "queue_limit_seconds": CAPACITY_LIMIT_SECONDS,
            "queue_percentiles_are_exact_time_weighted_playback_window_values": True,
            "percent_above_10_denominator": (
                "wall-clock playback window from first translated-media "
                "arrival through final playback end"
            ),
            "rolling_windows_seconds": list(BURST_WINDOWS_SECONDS),
            "rolling_window_interval": "[s, s + window)",
            "rolling_rate_denominator": (
                "the full configured window in seconds; unavailable time "
                "without translated-media arrivals contributes zero"
            ),
            "rolling_rate_samples": (
                "one sample per whole-second start from zero through the "
                "last window fully contained before end-of-input; traces "
                "shorter than the window use one start at zero"
            ),
            "rolling_windows_exclude_post_input_arrivals": True,
            "rolling_rate_p95": (
                "nearest-rank p95 across wall-clock window samples"
            ),
            "diagnostics_exclude_transcript_and_audio_content": True,
        },
        "traces": [
            {
                "trace_csv": trace.path.name,
                "trace_sha256": trace.sha256,
                "burst_diagnostics": _burst_diagnostics(trace),
                "scenarios": [
                    _constant_rate_scenario(
                        trace,
                        constant_rate=rate,
                        media_duration_scale=scale,
                    )
                    for rate in rates
                    for scale in scales
                ],
            }
            for trace in traces
        ],
    }


def _summary_path_for_trace(csv_path: Path) -> Path:
    return csv_path.with_name(
        csv_path.name.removesuffix("_results.csv") + "_summary.json"
    )


def _load_adjacent_summary(csv_path: Path) -> dict[str, Any] | None:
    summary_path = _summary_path_for_trace(csv_path)
    if not summary_path.exists():
        return None
    with summary_path.open(encoding="utf-8") as handle:
        summary = json.load(handle)
    if not isinstance(summary, dict):
        raise ValueError(f"{summary_path}: summary must be a JSON object")
    return summary


def _load_recorded_tail(
    csv_path: Path,
    *,
    summary: dict[str, Any] | None = None,
) -> float | None:
    if summary is None:
        summary = _load_adjacent_summary(csv_path)
    if summary is None:
        return None
    value = summary.get("playback_tail_sec")
    return float(value) if value is not None else None


def _require_summary_integer(
    observation: dict[str, Any],
    field_name: str,
    *,
    summary_path: Path,
    minimum: int,
) -> int:
    value = observation.get(field_name)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(
            f"{summary_path}: audio_metadata_observation.{field_name} "
            f"must be a {qualifier} integer"
        )
    return value


def _require_summary_finite_nonnegative(
    observation: dict[str, Any],
    field_name: str,
    *,
    summary_path: Path,
) -> float:
    value = observation.get(field_name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(
            f"{summary_path}: audio_metadata_observation.{field_name} "
            "must be a finite non-negative number"
        )
    return float(value)


def _bind_audio_metadata_summary(
    trace: PlaybackTrace,
    summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind attributed CSV frames to tracker-reconciled summary evidence."""

    if trace.audio_metadata_protocol_version is None:
        return {
            "required": False,
            "status": "not_applicable_legacy_trace",
            "passed": None,
            "completion_proven_by_trace_csv": False,
            "completion_evidence": None,
        }

    summary_path = _summary_path_for_trace(trace.path)
    if summary is None:
        raise ValueError(
            f"{trace.path}: protocol-v1 attribution requires adjacent "
            f"{summary_path.name} audio_metadata_observation"
        )
    observation = summary.get("audio_metadata_observation")
    if not isinstance(observation, dict):
        raise ValueError(
            f"{summary_path}: protocol-v1 attribution requires an "
            "audio_metadata_observation object"
        )

    input_sample_zero_timestamp_ms = _require_summary_finite_nonnegative(
        observation,
        "input_sample_zero_timestamp_ms",
        summary_path=summary_path,
    )
    observed_protocol = _require_summary_integer(
        observation,
        "protocol_version",
        summary_path=summary_path,
        minimum=1,
    )
    observed_generation = _require_summary_integer(
        observation,
        "stream_generation",
        summary_path=summary_path,
        minimum=1,
    )
    observed_frames = _require_summary_integer(
        observation,
        "paired_frames",
        summary_path=summary_path,
        minimum=0,
    )
    observed_completed_parents = _require_summary_integer(
        observation,
        "completed_parents",
        summary_path=summary_path,
        minimum=0,
    )
    expected_frames = len(trace.frame_attributions)
    expected_parents = len(
        {
            attribution.parent_sequence_id
            for attribution in trace.frame_attributions
        }
    )
    expected = {
        "protocol_version": trace.audio_metadata_protocol_version,
        "stream_generation": trace.audio_metadata_stream_generation,
        "paired_frames": expected_frames,
        "completed_parents": expected_parents,
    }
    observed = {
        "protocol_version": observed_protocol,
        "stream_generation": observed_generation,
        "paired_frames": observed_frames,
        "completed_parents": observed_completed_parents,
    }
    if observed != expected:
        mismatches = ", ".join(
            f"{key}: expected {expected[key]}, observed {observed[key]}"
            for key in expected
            if expected[key] != observed[key]
        )
        raise ValueError(
            f"{summary_path}: audio metadata summary does not bind to "
            f"{trace.path.name} ({mismatches})"
        )

    if len(trace.frame_attributions) != len(trace.chunks):
        raise ValueError(
            f"{trace.path}: audio attribution count does not match chunk count"
        )
    receipt_delay_validated_frame_count = 0
    for attribution, chunk in zip(
        trace.frame_attributions,
        trace.chunks,
        strict=True,
    ):
        if attribution.source_end_ms is None:
            continue
        observed_receipt_delay_ms = (
            attribution.source_end_to_receipt_ms
        )
        if observed_receipt_delay_ms is None:
            raise ValueError(
                f"{trace.path}: parent "
                f"{attribution.parent_sequence_id} frame "
                f"{attribution.audio_frame_id} has source_end_ms without "
                "source_end_to_receipt_ms"
            )
        expected_receipt_delay_ms = (
            chunk.arrival_seconds * 1000.0
            - input_sample_zero_timestamp_ms
            - attribution.source_end_ms
        )
        if not math.isclose(
            observed_receipt_delay_ms,
            expected_receipt_delay_ms,
            rel_tol=0.0,
            abs_tol=CSV_TIMING_SERIALIZATION_EPSILON_MS,
        ):
            raise ValueError(
                f"{trace.path}: parent "
                f"{attribution.parent_sequence_id} frame "
                f"{attribution.audio_frame_id} source_end_to_receipt_ms "
                "does not bind to the adjacent summary input sample-zero "
                "clock"
            )
        receipt_delay_validated_frame_count += 1
    return {
        "required": True,
        "status": "bound",
        "passed": True,
        "summary_json": summary_path.name,
        "protocol_version": observed_protocol,
        "stream_generation": observed_generation,
        "paired_frame_count": observed_frames,
        "completed_parent_count": observed_completed_parents,
        "input_sample_zero_timestamp_ms": (
            input_sample_zero_timestamp_ms
        ),
        "receipt_delay_validation": {
            "performed": True,
            "passed": True,
            "validated_frame_count": (
                receipt_delay_validated_frame_count
            ),
            "absolute_tolerance_ms": (
                CSV_TIMING_SERIALIZATION_EPSILON_MS
            ),
        },
        "completion_proven_by_trace_csv": False,
        "completion_evidence": (
            "adjacent_summary.audio_metadata_observation"
        ),
    }


def analyze_trace(
    trace: PlaybackTrace,
    *,
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
    recorded_fixed_tail_seconds: float | None = None,
) -> dict[str, Any]:
    fixed_simulation = simulate_playback(
        trace.chunks,
        input_end_seconds=trace.input_end_seconds,
        adaptive=False,
        policy=policy,
    )
    adaptive_simulation = simulate_playback(
        trace.chunks,
        input_end_seconds=trace.input_end_seconds,
        adaptive=True,
        policy=policy,
    )
    fixed = fixed_simulation.summary
    adaptive = adaptive_simulation.summary
    reduction = fixed.listener_tail_seconds - adaptive.listener_tail_seconds
    reduction_percent = (
        reduction / fixed.listener_tail_seconds * 100.0
        if fixed.listener_tail_seconds > 0
        else 0.0
    )
    fixed_delta = (
        fixed.listener_tail_seconds - recorded_fixed_tail_seconds
        if recorded_fixed_tail_seconds is not None
        else None
    )
    legacy_fixed_tail = None
    legacy_fixed_delta = None
    if trace.legacy_last_chunk_start_seconds is not None:
        legacy_fixed_tail = simulate_playback(
            trace.chunks,
            input_end_seconds=trace.legacy_last_chunk_start_seconds,
            adaptive=False,
            policy=policy,
        ).summary.listener_tail_seconds
        legacy_fixed_delta = (
            legacy_fixed_tail - recorded_fixed_tail_seconds
            if recorded_fixed_tail_seconds is not None
            else None
        )

    fixed_output = _compact_summary(fixed)
    fixed_output["source_frontier_to_scheduled_playback"] = (
        _source_frontier_metrics(trace, fixed_simulation)
    )
    adaptive_output = _compact_summary(adaptive)
    adaptive_output["source_frontier_to_scheduled_playback"] = (
        _source_frontier_metrics(trace, adaptive_simulation)
    )

    return _round_floats(
        {
            "trace_csv": trace.path.name,
            "trace_sha256": trace.sha256,
            "input_end_seconds": trace.input_end_seconds,
            "input_boundary_source": trace.input_boundary_source,
            "legacy_last_chunk_start_seconds": (
                trace.legacy_last_chunk_start_seconds
            ),
            "translated_audio_seconds": fixed.total_source_duration_seconds,
            "recorded_fixed_listener_tail_seconds": recorded_fixed_tail_seconds,
            "reproduced_fixed_tail_delta_seconds": fixed_delta,
            "legacy_start_boundary_fixed_tail_seconds": legacy_fixed_tail,
            "legacy_start_boundary_recorded_delta_seconds": legacy_fixed_delta,
            "fixed_1x": fixed_output,
            "adaptive": adaptive_output,
            "comparison": {
                "listener_tail_reduction_seconds": reduction,
                "listener_tail_reduction_percent": reduction_percent,
                "peak_queue_reduction_seconds": (
                    fixed.peak_queue_depth_seconds
                    - adaptive.peak_queue_depth_seconds
                ),
            },
        }
    )


def build_analysis(
    csv_paths: Sequence[Path],
    *,
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
    validate_recorded: bool = True,
    validation_tolerance_seconds: float = 0.005,
    constant_rates: Sequence[float] | None = None,
    media_duration_scales: Sequence[float] | None = None,
) -> dict[str, Any]:
    rates, scales = normalize_capacity_sweep_options(
        constant_rates,
        media_duration_scales,
    )
    traces = []
    loaded_traces: list[PlaybackTrace] = []
    for path in sorted(csv_paths):
        trace = load_event_trace(path)
        if rates:
            loaded_traces.append(trace)
        summary = _load_adjacent_summary(path)
        recorded_tail = _load_recorded_tail(path, summary=summary)
        result = analyze_trace(
            trace,
            policy=policy,
            recorded_fixed_tail_seconds=recorded_tail,
        )
        result["audio_metadata_summary_binding"] = (
            _bind_audio_metadata_summary(trace, summary)
        )
        delta = result["reproduced_fixed_tail_delta_seconds"]
        legacy_delta = result[
            "legacy_start_boundary_recorded_delta_seconds"
        ]
        if not validate_recorded:
            validation = {
                "performed": False,
                "passed": None,
                "boundary": None,
                "note": "recorded-tail validation was disabled",
            }
        elif delta is None:
            validation = {
                "performed": False,
                "passed": None,
                "boundary": None,
                "note": "no adjacent recorded tail was available",
            }
        elif abs(delta) <= validation_tolerance_seconds:
            validation = {
                "performed": True,
                "passed": True,
                "boundary": "corrected_input_boundary",
                "note": "recorded tail matches the corrected input boundary",
            }
        elif (
            result["input_boundary_source"] == "estimated_last_chunk_end"
            and legacy_delta is not None
            and abs(legacy_delta) <= validation_tolerance_seconds
        ):
            validation = {
                "performed": True,
                "passed": True,
                "boundary": "legacy_last_chunk_start_compatibility",
                "note": (
                    "recorded tail used the historical last-chunk-start "
                    "boundary; reported fixed/adaptive metrics use the "
                    "corrected last-chunk-end boundary"
                ),
            }
        else:
            raise ValueError(
                f"{path}: reproduced fixed tail differs from recorded result "
                f"by {delta:.6f}s"
            )
        result["recorded_tail_validation"] = validation
        traces.append(result)

    if not traces:
        raise ValueError("no event CSV traces were supplied")

    fixed_total = sum(item["fixed_1x"]["listener_tail_seconds"] for item in traces)
    adaptive_total = sum(item["adaptive"]["listener_tail_seconds"] for item in traces)
    reduction_total = fixed_total - adaptive_total

    analysis = {
        "schema_version": 2,
        "source_format": "batch_latency_test client event CSV",
        "audio_format": {
            "sample_rate_hz": SAMPLE_RATE,
            "channels": 1,
            "bytes_per_sample": BYTES_PER_SAMPLE,
        },
        "policy": _round_floats(asdict(policy)),
        "semantics": {
            "preserve_every_chunk": True,
            "limit_is_sla_alarm_not_drop_boundary": True,
            "queue_time_above_threshold_is_exact_between_arrivals": True,
            "arrival_queue_percentiles_use_nearest_rank": True,
            "input_end_prefers_explicit_event": True,
            "legacy_missing_end_event_uses_last_chunk_end": True,
            "historical_last_chunk_start_validation_is_annotated_compatibility": True,
            "source_frontier_uses_client_monotonic_clock_only": True,
            "source_frontier_is_scheduled_digital_playback_not_audibility": True,
            "source_frontier_percentiles_use_nearest_rank": True,
            "source_frontier_quartile_size_is_ceiling_n_over_four": True,
            "source_frontier_drift_minimum_eligible_parents": 4,
            "semantic_proxy_requires_both_source_range_bounds": True,
            "trace_csv_does_not_prove_parent_completion": True,
            "attributed_trace_requires_bound_audio_metadata_summary": True,
            "source_receipt_delay_recomputed_from_bound_sample_zero": True,
            "source_receipt_csv_serialization_epsilon_ms": (
                CSV_TIMING_SERIALIZATION_EPSILON_MS
            ),
            "bounded_parent_range_is_not_an_exact_semantic_landmark": True,
            "exact_joke_or_punchline_delay_requires_common_clock_review": True,
        },
        "traces": traces,
        "aggregate": _round_floats(
            {
                "trace_count": len(traces),
                "fixed_listener_tail_seconds": fixed_total,
                "adaptive_listener_tail_seconds": adaptive_total,
                "listener_tail_reduction_seconds": reduction_total,
                "listener_tail_reduction_percent": (
                    reduction_total / fixed_total * 100.0
                    if fixed_total > 0
                    else 0.0
                ),
            }
        ),
    }
    if rates:
        analysis["capacity_sweep"] = build_capacity_sweep(
            loaded_traces,
            constant_rates=rates,
            media_duration_scales=scales,
        )
    return analysis


def _display_name(filename: str) -> str:
    path = Path(filename)
    filename = path.name
    normalized = filename.lower().replace("_", "-")
    display = filename.removesuffix("_results.csv")
    for index in range(1, 4):
        if f"long-form-{index:02d}" in normalized or f"sample-{index:02d}" in normalized:
            display = f"Long-form sample {index:02d}"
            break
    if path.parent.name.startswith("repeat-"):
        return f"{display} ({path.parent.name})"
    return display


def _format_optional_seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}s"


def render_markdown(analysis: dict[str, Any]) -> str:
    policy = analysis["policy"]
    lines = [
        "# Nemotron 3 adaptive playback simulation",
        "",
        (
            "This deterministic replay uses client-side translated-audio arrival "
            "timestamps and PCM byte counts from the captured Nemotron 3 traces."
        ),
        "",
        (
            f"Policy: target {policy['target_queue_seconds']:.0f}s, urgent "
            f"{policy['urgent_queue_seconds']:.0f}s, SLA limit "
            f"{policy['limit_queue_seconds']:.0f}s; rates "
            f"{policy['normal_rate']:.2f}x / {policy['catch_up_rate']:.2f}x / "
            f"{policy['urgent_rate']:.2f}x. Release hysteresis is "
            f"{policy['catch_up_release_seconds']:.0f}s / "
            f"{policy['urgent_release_seconds']:.0f}s."
        ),
        "",
        (
            "| Trace | Chunks | Fixed tail | Adaptive tail | Reduction | "
            "Adaptive peak queue | Time >10s | Accelerated audio |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for trace in analysis["traces"]:
        fixed = trace["fixed_1x"]
        adaptive = trace["adaptive"]
        comparison = trace["comparison"]
        lines.append(
            "| {name} | {chunks:,} | {fixed:.3f}s | {adaptive:.3f}s | "
            "{reduction:.1f}% | {peak:.3f}s | {above:.3f}s | {accelerated:.1f}% |".format(
                name=_display_name(trace["trace_csv"]),
                chunks=adaptive["chunks_scheduled"],
                fixed=fixed["listener_tail_seconds"],
                adaptive=adaptive["listener_tail_seconds"],
                reduction=comparison["listener_tail_reduction_percent"],
                peak=adaptive["peak_queue_depth_seconds"],
                above=adaptive["seconds_above_limit"],
                accelerated=adaptive["accelerated_source_percent"],
            )
        )

    aggregate = analysis["aggregate"]
    lines.extend(
        [
            "",
            (
                f"Across all {aggregate['trace_count']} replays, listener tail fell from "
                f"{aggregate['fixed_listener_tail_seconds']:.3f}s to "
                f"{aggregate['adaptive_listener_tail_seconds']:.3f}s "
                f"({aggregate['listener_tail_reduction_percent']:.1f}% reduction)."
            ),
            "",
            (
                "Every translated chunk is retained. The 10-second value is an "
                "audience-latency SLA alarm, not a hard cap: if translated audio "
                "arrives faster than 1.10x playback can consume it, the queue may "
                "still exceed 10 seconds."
            ),
            "",
            (
                "`Fixed tail` uses the explicit input-ended event when present, "
                "otherwise the exact end of the final source chunk. Historical "
                "summaries that used the final chunk's start are accepted only "
                "as annotated compatibility evidence. This metric is distinct "
                "from whole-file output/input duration drift."
            ),
            "",
        ]
    )
    lines.extend(
        [
            "## Source-frontier scheduling projection",
            "",
            (
                "When protocol-v1 attribution is present, this browser-independent "
                "projection measures each parent from its source end to the "
                "deterministic scheduled digital playback start of its first "
                "frame and end of its last frame."
            ),
            "",
            (
                "The trace CSV proves ordered frame envelopes, not parent "
                "completion. For attributed traces, this report requires the "
                "adjacent summary and binds its reconciled protocol version, "
                "stream generation, paired-frame count, and completed-parent "
                "count before reporting the projection. It also recomputes "
                "every available source-end receipt delay from the bound "
                "input sample-zero clock within CSV serialization tolerance."
            ),
            "",
            (
                "These values do not measure DAC output or acoustic audibility. "
                "A parent is eligible only as a coarse semantic-range proxy when "
                "both `source_start_ms` and `source_end_ms` exist on every frame. "
                "Even then, it is not an exact joke or punchline measurement; "
                "that requires reviewed source and target-language landmarks on "
                "a common output clock."
            ),
            "",
            (
                "| Trace | Policy | Measured parents | Semantic-range eligible | "
                "Start p50 | Start p95 | Start max | End p50 | End p95 | "
                "End max | Start drift |"
            ),
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for trace in analysis["traces"]:
        for policy_name, policy_key in (
            ("Fixed 1.00x", "fixed_1x"),
            ("Adaptive", "adaptive"),
        ):
            frontier = trace[policy_key][
                "source_frontier_to_scheduled_playback"
            ]
            start = frontier[
                "source_end_to_first_frame_scheduled_start_seconds"
            ]
            end = frontier[
                "source_end_to_last_frame_scheduled_end_seconds"
            ]
            eligibility = frontier["semantic_proxy_eligibility"]
            start_drift = frontier["accumulated_drift"][
                "first_frame_scheduled_start_seconds"
            ]["final_minus_first_seconds"]
            lines.append(
                "| {name} | {policy_name} | {measured:,} | "
                "{eligible:,} | {start_p50} | {start_p95} | "
                "{start_max} | {end_p50} | {end_p95} | {end_max} | "
                "{drift} |".format(
                    name=_display_name(trace["trace_csv"]),
                    policy_name=policy_name,
                    measured=frontier["measured_parent_envelope_count"],
                    eligible=eligibility["eligible_parent_count"],
                    start_p50=_format_optional_seconds(start["p50"]),
                    start_p95=_format_optional_seconds(start["p95"]),
                    start_max=_format_optional_seconds(start["max"]),
                    end_p50=_format_optional_seconds(end["p50"]),
                    end_p95=_format_optional_seconds(end["p95"]),
                    end_max=_format_optional_seconds(end["max"]),
                    drift=_format_optional_seconds(start_drift),
                )
            )
    lines.append("")

    capacity_sweep = analysis.get("capacity_sweep")
    if capacity_sweep is not None:
        lines.extend(
            [
                "## Offline constant-rate capacity sweep",
                "",
                (
                    "This optional no-drop replay keeps translated-audio "
                    "arrival timestamps and the source input boundary fixed. "
                    "The media-duration scale multiplies every translated "
                    "audio chunk before it is queued. Burst diagnostics use "
                    "the captured, unscaled chunk durations."
                ),
                "",
                (
                    "Burst rates are translated-media seconds per wall-clock "
                    "second. Windows start at whole wall-clock seconds and "
                    "use `[s, s + window)`. Each normal window is fully "
                    "contained before end-of-input; a trace shorter than the "
                    "window uses one start at zero. The denominator is always "
                    "the full 30, 60, or 300 seconds, and post-input arrivals "
                    "are excluded. Reported p95 values use nearest-rank over "
                    "those wall-clock samples."
                ),
                (
                    "Queue percentiles are exact time-weighted values. The "
                    "percentage above 10 seconds uses the wall-clock playback "
                    "window from first translated-media arrival through final "
                    "playback end."
                ),
                "",
                (
                    "| Trace | Chunk p50 | Chunk p95 | Chunk max | "
                    "30s arrival-rate p95 | 60s arrival-rate p95 | "
                    "300s arrival-rate p95 |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for trace in capacity_sweep["traces"]:
            diagnostics = trace["burst_diagnostics"]
            durations = diagnostics[
                "translated_audio_chunk_duration_seconds"
            ]
            rates = diagnostics[
                "rolling_translated_media_arrival_rate_p95_x_realtime"
            ]
            lines.append(
                "| {name} | {p50:.3f}s | {p95:.3f}s | {maximum:.3f}s | "
                "{rate30:.3f}x | {rate60:.3f}x | {rate300:.3f}x |".format(
                    name=_display_name(trace["trace_csv"]),
                    p50=durations["p50"],
                    p95=durations["p95"],
                    maximum=durations["max"],
                    rate30=rates["30_seconds"],
                    rate60=rates["60_seconds"],
                    rate300=rates["300_seconds"],
                )
            )

        lines.extend(
            [
                "",
                (
                    "| Trace | Constant rate | Media scale | No-drop tail | "
                    "Queue p50 | Queue p95 | Peak queue | Time >10s | "
                    "Window >10s | Dropped |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for trace in capacity_sweep["traces"]:
            for scenario in trace["scenarios"]:
                lines.append(
                    "| {name} | {rate:.3f}x | {scale:.3f}x | {tail:.3f}s | "
                    "{p50:.3f}s | {p95:.3f}s | {peak:.3f}s | "
                    "{above:.3f}s | {percent:.1f}% | {dropped:,} |".format(
                        name=_display_name(trace["trace_csv"]),
                        rate=scenario["constant_rate"],
                        scale=scenario["media_duration_scale"],
                        tail=scenario["no_drop_listener_tail_seconds"],
                        p50=scenario["time_weighted_queue_p50_seconds"],
                        p95=scenario["time_weighted_queue_p95_seconds"],
                        peak=scenario["peak_queue_depth_seconds"],
                        above=scenario["seconds_above_10_seconds"],
                        percent=scenario[
                            "percent_playback_window_above_10_seconds"
                        ],
                        dropped=scenario["chunks_dropped"],
                    )
                )
        lines.append("")
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay captured translation audio through the playback policy"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("test_results_nemotron"),
        help="directory containing *_results.csv traces",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("test_results_nemotron/playback_policy_analysis.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("test_results_nemotron/playback_policy_analysis.md"),
    )
    parser.add_argument(
        "--skip-recorded-validation",
        action="store_true",
        help="do not compare reproduced fixed tails with adjacent summary JSON",
    )
    parser.add_argument(
        "--constant-rate",
        action="append",
        dest="constant_rates",
        type=float,
        help=(
            "constant no-drop playback rate for an optional capacity sweep; "
            "repeat for multiple rates"
        ),
    )
    parser.add_argument(
        "--media-duration-scale",
        action="append",
        dest="media_duration_scales",
        type=float,
        help=(
            "translated-media duration multiplier for the optional capacity "
            "sweep; repeat for multiple scales (default: 1.0)"
        ),
    )
    return parser


def parse_cli_args(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    try:
        rates, scales = normalize_capacity_sweep_options(
            args.constant_rates,
            args.media_duration_scales,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.constant_rates = rates
    args.media_duration_scales = scales
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli_args(argv)

    csv_paths = list(args.input_dir.glob("*_results.csv"))
    analysis = build_analysis(
        csv_paths,
        validate_recorded=not args.skip_recorded_validation,
        constant_rates=args.constant_rates,
        media_duration_scales=args.media_duration_scales,
    )

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(analysis, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(analysis), encoding="utf-8")
    print(f"Wrote {args.json_output}")
    print(f"Wrote {args.markdown_output}")


if __name__ == "__main__":
    main()
