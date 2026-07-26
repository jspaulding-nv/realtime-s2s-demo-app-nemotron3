#!/usr/bin/env python3
"""Browser-independent scheduling and aggregate audience-latency evidence.

The scheduler accepts only already-validated numeric frame observations. It
does not accept or retain PCM, transcript text, translation text, file paths,
URIs, or wall-clock timestamps. Each frame is scheduled as it arrives with the
registered adaptive no-drop policy from :mod:`playback_simulation`.

``finalize`` independently replays the complete observation with
``simulate_playback`` and fails closed if the incremental decisions differ.
The returned report is aggregate-only. Its scheduled digital playback times
are not measurements of DAC output, acoustic audibility, semantic punchline
alignment, or audience reaction.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence

from playback_simulation import (
    DEFAULT_PLAYBACK_POLICY,
    AudioChunk,
    PlaybackMode,
    ScheduledChunk,
    playback_rate_for_mode,
    select_playback_mode,
    simulate_playback,
)


REPORT_SCHEMA_VERSION = 1
REPORT_TYPE = "headless_scheduled_digital_playback"
CLAIM_SCOPE = {
    "measurement": "scheduled_digital_playback",
    "semantic_source_boundary_proven": False,
    "target_language_landmark_proven": False,
    "dac_or_acoustic_audibility_proven": False,
    "room_reaction_synchronization_proven": False,
    "caveats": [
        (
            "source offsets are ASR source ranges or coarse "
            "audio-processed ends, not reviewed punchline markers"
        ),
        (
            "scheduled starts are deterministic software deadlines, "
            "not measured speaker output"
        ),
        (
            "an exact joke-delay claim requires reviewed source and "
            "target landmarks on a common-clock recording"
        ),
    ],
}
PRIVACY_DECLARATION = {
    "aggregate_only": True,
    "contains_pcm": False,
    "contains_transcript_or_translation_text": False,
    "contains_file_path_or_uri": False,
    "contains_wall_clock_timestamp": False,
}

_REPORT_KEYS = {
    "schema_version",
    "report_type",
    "policy",
    "capture",
    "queue_observation",
    "queue_gate",
    "source_boundary_observation",
    "parent_schedule_envelope",
    "accumulated_frontier_drift",
    "claim_scope",
    "privacy",
}
_POLICY_KEYS = set(asdict(DEFAULT_PLAYBACK_POLICY))
_CAPTURE_KEYS = {
    "stream_generation",
    "frames_received",
    "frames_scheduled",
    "complete_parents",
    "parent_reconciliation",
    "input_end_seconds",
    "translated_pcm_seconds",
    "scheduled_playback_seconds",
    "playback_end_seconds",
    "listener_tail_seconds",
    "canonical_replay_verified",
}
_QUEUE_OBSERVATION_KEYS = {
    "arrival_queue_p50_seconds",
    "arrival_queue_p95_seconds",
    "time_weighted_queue_p50_seconds",
    "time_weighted_queue_p95_seconds",
    "peak_queue_depth_seconds",
    "seconds_above_target",
    "seconds_above_limit",
    "limit_breach_entries",
    "accelerated_source_percent",
    "urgent_source_percent",
}
_QUEUE_GATE_KEYS = {
    "p95_metric",
    "p95_objective_seconds",
    "p95_observed_seconds",
    "p95_pass",
    "peak_metric",
    "peak_limit_seconds",
    "peak_observed_seconds",
    "peak_pass",
    "frames_dropped",
    "frames_reordered",
    "frames_duplicated",
    "all_frames_preserved_once_in_order",
    "result",
}
_SOURCE_BOUNDARY_KEYS = {
    "clock",
    "source_offset_origin",
    "sample_unit",
    "complete_parent_count",
    "parents_with_asr_source_range",
    "parents_with_audio_processed_end_only",
    "parents_without_source_end",
    "source_end_to_first_arrival_ms",
    "source_end_to_first_scheduled_start_ms",
}
_PARENT_ENVELOPE_KEYS = {
    "sample_unit",
    "parent_count",
    "source_attributed_parent_count",
    "source_end_to_first_scheduled_start_ms",
    "source_end_to_final_scheduled_end_ms",
    "scheduled_envelope_duration_ms",
}
_DISTRIBUTION_KEYS = {
    "sample_count",
    "min_ms",
    "p50_ms",
    "p95_ms",
    "max_ms",
}
_DRIFT_KEYS = {
    "metric",
    "population",
    "sample_count",
    "minimum_samples_required",
    "sufficient_sample_count",
    "quartile_size",
    "first_quartile_median_ms",
    "final_quartile_median_ms",
    "final_minus_first_ms",
    "positive_delta_means",
}


def _report_mapping(
    value: object,
    *,
    path: str,
    keys: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unknown = sorted(actual - keys)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError(f"{path} has invalid fields: {'; '.join(details)}")
    return value


def _report_int(
    value: object,
    *,
    path: str,
    minimum: int = 0,
) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(
            f"{path} must be an integer greater than or equal to {minimum}"
        )
    return value


def _report_float(
    value: object,
    *,
    path: str,
    minimum: float | None = None,
) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{path} must be a finite float")
    if minimum is not None and value < minimum:
        raise ValueError(
            f"{path} must be greater than or equal to {minimum}"
        )
    return value


def _report_bool(value: object, *, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be a boolean")
    return value


def _report_literal(value: object, expected: object, *, path: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{path} must equal {expected!r}")


def _validate_distribution(
    value: object,
    *,
    path: str,
    expected_count: int,
    nonnegative: bool = False,
) -> dict[str, Any]:
    distribution = _report_mapping(
        value,
        path=path,
        keys=_DISTRIBUTION_KEYS,
    )
    count = _report_int(
        distribution["sample_count"],
        path=f"{path}.sample_count",
    )
    if count != expected_count:
        raise ValueError(
            f"{path}.sample_count does not match its population"
        )
    value_fields = ("min_ms", "p50_ms", "p95_ms", "max_ms")
    if count == 0:
        for field_name in value_fields:
            if distribution[field_name] is not None:
                raise ValueError(
                    f"{path}.{field_name} must be null without samples"
                )
        return distribution
    values = [
        _report_float(
            distribution[field_name],
            path=f"{path}.{field_name}",
            minimum=0.0 if nonnegative else None,
        )
        for field_name in value_fields
    ]
    if not values[0] <= values[1] <= values[2] <= values[3]:
        raise ValueError(f"{path} percentile order is invalid")
    return distribution


def validate_headless_playback_report(
    report: object,
    *,
    expected_frames: int | None = None,
    expected_parents: int | None = None,
    expected_stream_generation: int | None = None,
) -> None:
    """Validate the complete fixed public report, rejecting added fields.

    A queue SLA ``result`` of ``"fail"`` is valid evidence when its observed
    metrics and booleans consistently describe the failure. Only malformed,
    privacy-unsafe, or internally inconsistent reports are rejected.
    """

    root = _report_mapping(report, path="headless_playback", keys=_REPORT_KEYS)
    _report_literal(
        root["schema_version"],
        REPORT_SCHEMA_VERSION,
        path="headless_playback.schema_version",
    )
    _report_literal(
        root["report_type"],
        REPORT_TYPE,
        path="headless_playback.report_type",
    )

    policy = _report_mapping(
        root["policy"],
        path="headless_playback.policy",
        keys=_POLICY_KEYS,
    )
    for field_name, expected in asdict(DEFAULT_PLAYBACK_POLICY).items():
        _report_literal(
            policy[field_name],
            expected,
            path=f"headless_playback.policy.{field_name}",
        )

    capture = _report_mapping(
        root["capture"],
        path="headless_playback.capture",
        keys=_CAPTURE_KEYS,
    )
    generation = _report_int(
        capture["stream_generation"],
        path="headless_playback.capture.stream_generation",
        minimum=1,
    )
    frames_received = _report_int(
        capture["frames_received"],
        path="headless_playback.capture.frames_received",
        minimum=1,
    )
    frames_scheduled = _report_int(
        capture["frames_scheduled"],
        path="headless_playback.capture.frames_scheduled",
        minimum=1,
    )
    complete_parents = _report_int(
        capture["complete_parents"],
        path="headless_playback.capture.complete_parents",
        minimum=1,
    )
    if frames_received != frames_scheduled:
        raise ValueError(
            "headless_playback capture frame counts are inconsistent"
        )
    if complete_parents > frames_received:
        raise ValueError(
            "headless_playback complete parent count exceeds frame count"
        )
    if (
        expected_frames is not None
        and frames_received != expected_frames
    ):
        raise ValueError(
            "headless_playback frame count does not match capture evidence"
        )
    if (
        expected_parents is not None
        and complete_parents != expected_parents
    ):
        raise ValueError(
            "headless_playback parent count does not match capture evidence"
        )
    if (
        expected_stream_generation is not None
        and generation != expected_stream_generation
    ):
        raise ValueError(
            "headless_playback stream generation does not match capture evidence"
        )
    reconciliation = capture["parent_reconciliation"]
    if reconciliation not in {
        "upstream_protocol_v1_tracker",
        "explicit_completion_observations",
    } or type(reconciliation) is not str:
        raise ValueError(
            "headless_playback.capture.parent_reconciliation is invalid"
        )
    input_end = _report_float(
        capture["input_end_seconds"],
        path="headless_playback.capture.input_end_seconds",
        minimum=0.0,
    )
    translated = _report_float(
        capture["translated_pcm_seconds"],
        path="headless_playback.capture.translated_pcm_seconds",
        minimum=0.0,
    )
    scheduled = _report_float(
        capture["scheduled_playback_seconds"],
        path="headless_playback.capture.scheduled_playback_seconds",
        minimum=0.0,
    )
    playback_end = _report_float(
        capture["playback_end_seconds"],
        path="headless_playback.capture.playback_end_seconds",
        minimum=0.0,
    )
    listener_tail = _report_float(
        capture["listener_tail_seconds"],
        path="headless_playback.capture.listener_tail_seconds",
        minimum=0.0,
    )
    if translated <= 0.0 or scheduled <= 0.0:
        raise ValueError("headless_playback translated durations must be positive")
    if scheduled > translated + 1e-6:
        raise ValueError(
            "headless_playback scheduled duration exceeds translated PCM"
        )
    if playback_end + 1e-6 < scheduled:
        raise ValueError(
            "headless_playback playback end precedes scheduled duration"
        )
    if not math.isclose(
        listener_tail,
        max(0.0, playback_end - input_end),
        rel_tol=0.0,
        # Each component is independently rounded to six decimals.
        abs_tol=2.1e-6,
    ):
        raise ValueError("headless_playback listener tail is inconsistent")
    _report_literal(
        capture["canonical_replay_verified"],
        True,
        path="headless_playback.capture.canonical_replay_verified",
    )

    queue = _report_mapping(
        root["queue_observation"],
        path="headless_playback.queue_observation",
        keys=_QUEUE_OBSERVATION_KEYS,
    )
    queue_values = {
        field_name: _report_float(
            queue[field_name],
            path=f"headless_playback.queue_observation.{field_name}",
            minimum=0.0,
        )
        for field_name in _QUEUE_OBSERVATION_KEYS
        if field_name != "limit_breach_entries"
    }
    _report_int(
        queue["limit_breach_entries"],
        path="headless_playback.queue_observation.limit_breach_entries",
    )
    if not (
        queue_values["arrival_queue_p50_seconds"]
        <= queue_values["arrival_queue_p95_seconds"]
        <= queue_values["peak_queue_depth_seconds"]
        + 1e-6
    ):
        raise ValueError("headless_playback arrival queue percentiles are invalid")
    if not (
        queue_values["time_weighted_queue_p50_seconds"]
        <= queue_values["time_weighted_queue_p95_seconds"]
        <= queue_values["peak_queue_depth_seconds"]
        + 1e-6
    ):
        raise ValueError(
            "headless_playback time-weighted queue percentiles are invalid"
        )
    if (
        queue_values["seconds_above_limit"]
        > queue_values["seconds_above_target"] + 1e-6
    ):
        raise ValueError(
            "headless_playback over-limit duration exceeds above-target duration"
        )
    for field_name in (
        "accelerated_source_percent",
        "urgent_source_percent",
    ):
        if queue_values[field_name] > 100.0:
            raise ValueError(
                f"headless_playback.queue_observation.{field_name} "
                "cannot exceed 100"
            )
    if (
        queue_values["urgent_source_percent"]
        > queue_values["accelerated_source_percent"] + 1e-6
    ):
        raise ValueError(
            "headless_playback urgent source percentage exceeds accelerated"
        )

    gate = _report_mapping(
        root["queue_gate"],
        path="headless_playback.queue_gate",
        keys=_QUEUE_GATE_KEYS,
    )
    _report_literal(
        gate["p95_metric"],
        "time_weighted_queue_p95_seconds",
        path="headless_playback.queue_gate.p95_metric",
    )
    _report_literal(
        gate["p95_objective_seconds"],
        DEFAULT_PLAYBACK_POLICY.target_queue_seconds,
        path="headless_playback.queue_gate.p95_objective_seconds",
    )
    p95_observed = _report_float(
        gate["p95_observed_seconds"],
        path="headless_playback.queue_gate.p95_observed_seconds",
        minimum=0.0,
    )
    if not math.isclose(
        p95_observed,
        queue_values["time_weighted_queue_p95_seconds"],
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("headless_playback p95 gate observation is inconsistent")
    p95_pass = _report_bool(
        gate["p95_pass"],
        path="headless_playback.queue_gate.p95_pass",
    )
    if p95_pass is not (
        p95_observed <= DEFAULT_PLAYBACK_POLICY.target_queue_seconds
    ):
        raise ValueError("headless_playback p95 gate boolean is inconsistent")
    _report_literal(
        gate["peak_metric"],
        "peak_queue_depth_seconds",
        path="headless_playback.queue_gate.peak_metric",
    )
    _report_literal(
        gate["peak_limit_seconds"],
        DEFAULT_PLAYBACK_POLICY.limit_queue_seconds,
        path="headless_playback.queue_gate.peak_limit_seconds",
    )
    peak_observed = _report_float(
        gate["peak_observed_seconds"],
        path="headless_playback.queue_gate.peak_observed_seconds",
        minimum=0.0,
    )
    if not math.isclose(
        peak_observed,
        queue_values["peak_queue_depth_seconds"],
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("headless_playback peak gate observation is inconsistent")
    peak_pass = _report_bool(
        gate["peak_pass"],
        path="headless_playback.queue_gate.peak_pass",
    )
    if peak_pass is not (
        peak_observed <= DEFAULT_PLAYBACK_POLICY.limit_queue_seconds
    ):
        raise ValueError("headless_playback peak gate boolean is inconsistent")
    integrity_values = {
        field_name: _report_int(
            gate[field_name],
            path=f"headless_playback.queue_gate.{field_name}",
        )
        for field_name in (
            "frames_dropped",
            "frames_reordered",
            "frames_duplicated",
        )
    }
    integrity_pass = (
        frames_received == frames_scheduled
        and all(value == 0 for value in integrity_values.values())
    )
    _report_literal(
        gate["all_frames_preserved_once_in_order"],
        integrity_pass,
        path=(
            "headless_playback.queue_gate."
            "all_frames_preserved_once_in_order"
        ),
    )
    expected_gate_result = (
        "pass" if p95_pass and peak_pass and integrity_pass else "fail"
    )
    _report_literal(
        gate["result"],
        expected_gate_result,
        path="headless_playback.queue_gate.result",
    )

    source = _report_mapping(
        root["source_boundary_observation"],
        path="headless_playback.source_boundary_observation",
        keys=_SOURCE_BOUNDARY_KEYS,
    )
    for field_name, expected in {
        "clock": "client_monotonic",
        "source_offset_origin": "input_pcm_sample_zero",
        "sample_unit": "first_translated_frame_per_complete_parent",
    }.items():
        _report_literal(
            source[field_name],
            expected,
            path=f"headless_playback.source_boundary_observation.{field_name}",
        )
    source_complete = _report_int(
        source["complete_parent_count"],
        path=(
            "headless_playback.source_boundary_observation."
            "complete_parent_count"
        ),
        minimum=1,
    )
    source_range_count = _report_int(
        source["parents_with_asr_source_range"],
        path=(
            "headless_playback.source_boundary_observation."
            "parents_with_asr_source_range"
        ),
    )
    end_only_count = _report_int(
        source["parents_with_audio_processed_end_only"],
        path=(
            "headless_playback.source_boundary_observation."
            "parents_with_audio_processed_end_only"
        ),
    )
    missing_end_count = _report_int(
        source["parents_without_source_end"],
        path=(
            "headless_playback.source_boundary_observation."
            "parents_without_source_end"
        ),
    )
    if source_complete != complete_parents or (
        source_range_count + end_only_count + missing_end_count
        != source_complete
    ):
        raise ValueError(
            "headless_playback source-boundary parent counts are inconsistent"
        )
    source_attributed_count = source_range_count + end_only_count
    _validate_distribution(
        source["source_end_to_first_arrival_ms"],
        path=(
            "headless_playback.source_boundary_observation."
            "source_end_to_first_arrival_ms"
        ),
        expected_count=source_attributed_count,
    )
    source_start_distribution = _validate_distribution(
        source["source_end_to_first_scheduled_start_ms"],
        path=(
            "headless_playback.source_boundary_observation."
            "source_end_to_first_scheduled_start_ms"
        ),
        expected_count=source_attributed_count,
    )

    envelope = _report_mapping(
        root["parent_schedule_envelope"],
        path="headless_playback.parent_schedule_envelope",
        keys=_PARENT_ENVELOPE_KEYS,
    )
    _report_literal(
        envelope["sample_unit"],
        "complete_translated_parent",
        path="headless_playback.parent_schedule_envelope.sample_unit",
    )
    envelope_parent_count = _report_int(
        envelope["parent_count"],
        path="headless_playback.parent_schedule_envelope.parent_count",
        minimum=1,
    )
    envelope_source_count = _report_int(
        envelope["source_attributed_parent_count"],
        path=(
            "headless_playback.parent_schedule_envelope."
            "source_attributed_parent_count"
        ),
    )
    if (
        envelope_parent_count != complete_parents
        or envelope_source_count != source_attributed_count
    ):
        raise ValueError(
            "headless_playback parent-envelope counts are inconsistent"
        )
    envelope_start_distribution = _validate_distribution(
        envelope["source_end_to_first_scheduled_start_ms"],
        path=(
            "headless_playback.parent_schedule_envelope."
            "source_end_to_first_scheduled_start_ms"
        ),
        expected_count=source_attributed_count,
    )
    if envelope_start_distribution != source_start_distribution:
        raise ValueError(
            "headless_playback repeated source-start distribution disagrees"
        )
    _validate_distribution(
        envelope["source_end_to_final_scheduled_end_ms"],
        path=(
            "headless_playback.parent_schedule_envelope."
            "source_end_to_final_scheduled_end_ms"
        ),
        expected_count=source_attributed_count,
    )
    _validate_distribution(
        envelope["scheduled_envelope_duration_ms"],
        path=(
            "headless_playback.parent_schedule_envelope."
            "scheduled_envelope_duration_ms"
        ),
        expected_count=complete_parents,
        nonnegative=True,
    )

    drift = _report_mapping(
        root["accumulated_frontier_drift"],
        path="headless_playback.accumulated_frontier_drift",
        keys=_DRIFT_KEYS,
    )
    for field_name, expected in {
        "metric": "source_end_to_first_scheduled_start_ms",
        "population": "complete parents with both ASR source-range offsets",
        "positive_delta_means": (
            "scheduled source-frontier delay accumulated later in the capture"
        ),
    }.items():
        _report_literal(
            drift[field_name],
            expected,
            path=f"headless_playback.accumulated_frontier_drift.{field_name}",
        )
    drift_count = _report_int(
        drift["sample_count"],
        path="headless_playback.accumulated_frontier_drift.sample_count",
    )
    if drift_count != source_range_count:
        raise ValueError(
            "headless_playback frontier-drift population is inconsistent"
        )
    _report_literal(
        drift["minimum_samples_required"],
        4,
        path=(
            "headless_playback.accumulated_frontier_drift."
            "minimum_samples_required"
        ),
    )
    sufficient = _report_bool(
        drift["sufficient_sample_count"],
        path=(
            "headless_playback.accumulated_frontier_drift."
            "sufficient_sample_count"
        ),
    )
    if sufficient is not (drift_count >= 4):
        raise ValueError(
            "headless_playback frontier-drift sufficiency is inconsistent"
        )
    quartile_size = _report_int(
        drift["quartile_size"],
        path="headless_playback.accumulated_frontier_drift.quartile_size",
    )
    expected_quartile_size = math.ceil(drift_count / 4) if sufficient else 0
    if quartile_size != expected_quartile_size:
        raise ValueError(
            "headless_playback frontier-drift quartile size is inconsistent"
        )
    drift_fields = (
        "first_quartile_median_ms",
        "final_quartile_median_ms",
        "final_minus_first_ms",
    )
    if not sufficient:
        if any(drift[field_name] is not None for field_name in drift_fields):
            raise ValueError(
                "headless_playback frontier-drift values require enough samples"
            )
    else:
        first, final, delta = [
            _report_float(
                drift[field_name],
                path=(
                    "headless_playback.accumulated_frontier_drift."
                    f"{field_name}"
                ),
            )
            for field_name in drift_fields
        ]
        if not math.isclose(
            delta,
            final - first,
            rel_tol=0.0,
            # All three values are independently rounded to six decimals.
            abs_tol=2.1e-6,
        ):
            raise ValueError(
                "headless_playback frontier-drift delta is inconsistent"
            )

    claim = _report_mapping(
        root["claim_scope"],
        path="headless_playback.claim_scope",
        keys=set(CLAIM_SCOPE),
    )
    for field_name, expected in CLAIM_SCOPE.items():
        value = claim[field_name]
        if isinstance(expected, list):
            if type(value) is not list or len(value) != len(expected):
                raise ValueError(
                    f"headless_playback.claim_scope.{field_name} is invalid"
                )
            for index, expected_item in enumerate(expected):
                _report_literal(
                    value[index],
                    expected_item,
                    path=(
                        f"headless_playback.claim_scope.{field_name}."
                        f"{index}"
                    ),
                )
        else:
            _report_literal(
                value,
                expected,
                path=f"headless_playback.claim_scope.{field_name}",
            )
    privacy = _report_mapping(
        root["privacy"],
        path="headless_playback.privacy",
        keys=set(PRIVACY_DECLARATION),
    )
    for field_name, expected in PRIVACY_DECLARATION.items():
        _report_literal(
            privacy[field_name],
            expected,
            path=f"headless_playback.privacy.{field_name}",
        )


def compare_headless_playback_reports(
    saved: object,
    replayed: object,
    *,
    csv_timestamp_rounding_ms: float = 1e-9,
) -> None:
    """Compare validated reports with round-trip float serialization tolerance."""

    validate_headless_playback_report(saved)
    validate_headless_playback_report(replayed)
    tolerance_ms = _require_finite_nonnegative(
        csv_timestamp_rounding_ms,
        field_name="csv_timestamp_rounding_ms",
    )

    def compare(actual: object, expected: object, path: tuple[str, ...]) -> None:
        if type(actual) is not type(expected):
            raise ValueError(
                "headless playback replay type mismatch at "
                + ".".join(path)
            )
        if isinstance(expected, dict):
            for key in expected:
                compare(actual[key], expected[key], (*path, key))
            return
        if isinstance(expected, list):
            if len(actual) != len(expected):
                raise ValueError(
                    "headless playback replay list mismatch at "
                    + ".".join(path)
                )
            for index, item in enumerate(expected):
                compare(actual[index], item, (*path, str(index)))
            return
        if isinstance(expected, float):
            field_name = path[-1]
            if field_name.endswith("_ms"):
                tolerance = tolerance_ms
            elif field_name.endswith("_seconds"):
                tolerance = tolerance_ms / 1000.0
            else:
                tolerance = 1e-6
            if not math.isclose(
                actual,
                expected,
                rel_tol=0.0,
                abs_tol=tolerance,
            ):
                raise ValueError(
                    "headless playback replay value mismatch at "
                    + ".".join(path)
                )
            return
        if actual != expected:
            raise ValueError(
                "headless playback replay value mismatch at "
                + ".".join(path)
            )

    compare(saved, replayed, ("headless_playback",))


@dataclass(frozen=True)
class ValidatedFrameObservation:
    """Privacy-safe facts for one validated translated PCM frame.

    ``arrival_seconds`` is measured from the capture client's monotonic clock
    origin. ``source_start_ms`` and ``source_end_ms`` are offsets from input
    PCM sample zero in the same capture. The PCM duration must already have
    been derived from validated byte count and format metadata by the caller.
    """

    arrival_seconds: float
    duration_seconds: float
    stream_generation: int
    parent_sequence_id: int
    audio_frame_id: int
    source_start_ms: float | None
    source_end_ms: float | None


@dataclass(frozen=True)
class ValidatedParentCompletion:
    """Privacy-safe completion facts for one validated translated parent."""

    stream_generation: int
    parent_sequence_id: int
    audio_frame_count: int
    source_start_ms: float | None
    source_end_ms: float | None


class ValidatedAudioFrameLike(Protocol):
    """Structural contract implemented by ``batch_latency_test`` frames."""

    arrival_seconds: float
    audio_bytes: int
    protocol_version: int
    stream_generation: int
    parent_sequence_id: int
    audio_frame_id: int
    sample_rate_hz: int
    channels: int
    bytes_per_sample: int
    source_start_ms: float | None
    source_end_ms: float | None


@dataclass(frozen=True)
class HeadlessScheduledFrame:
    """Incremental scheduling decision returned for one accepted frame."""

    stream_generation: int
    parent_sequence_id: int
    audio_frame_id: int
    source_start_ms: float | None
    source_end_ms: float | None
    source_index: int
    arrival_seconds: float
    source_duration_seconds: float
    start_seconds: float
    end_seconds: float
    wait_before_playback_seconds: float
    projected_queue_at_normal_rate_seconds: float
    queue_depth_seconds: float
    playback_rate: float
    playback_mode: PlaybackMode
    mode_changed: bool
    above_target: bool
    above_limit: bool

    def canonical_chunk(self) -> ScheduledChunk:
        """Return the policy-simulator representation of this decision."""

        return ScheduledChunk(
            source_index=self.source_index,
            arrival_seconds=self.arrival_seconds,
            source_duration_seconds=self.source_duration_seconds,
            start_seconds=self.start_seconds,
            end_seconds=self.end_seconds,
            wait_before_playback_seconds=self.wait_before_playback_seconds,
            projected_queue_at_normal_rate_seconds=(
                self.projected_queue_at_normal_rate_seconds
            ),
            queue_depth_seconds=self.queue_depth_seconds,
            playback_rate=self.playback_rate,
            playback_mode=self.playback_mode,
            mode_changed=self.mode_changed,
            above_target=self.above_target,
            above_limit=self.above_limit,
        )


def _require_integer(
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
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{field_name} must be a {qualifier} integer")
    return value


def _require_finite_nonnegative(value: object, *, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(
            f"{field_name} must be a finite non-negative number"
        )
    return float(value)


def _require_finite_positive(value: object, *, field_name: str) -> float:
    normalized = _require_finite_nonnegative(value, field_name=field_name)
    if normalized <= 0:
        raise ValueError(f"{field_name} must be a finite positive number")
    return normalized


def _normalize_source_range(
    start_ms: object,
    end_ms: object,
) -> tuple[float | None, float | None]:
    normalized: list[float | None] = []
    for value, field_name in (
        (start_ms, "source_start_ms"),
        (end_ms, "source_end_ms"),
    ):
        if value is None:
            normalized.append(None)
        else:
            normalized.append(
                _require_finite_nonnegative(value, field_name=field_name)
            )
    start, end = normalized
    if start is not None and end is not None and end < start:
        raise ValueError("source_end_ms cannot precede source_start_ms")
    return start, end


def _nearest_rank(
    values: Sequence[float],
    quantile: float,
) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _distribution_ms(
    values: Sequence[float],
) -> dict[str, float | int | None]:
    """Return a fixed nearest-rank distribution without inventing samples."""

    if not values:
        return {
            "sample_count": 0,
            "min_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
        }
    return {
        "sample_count": len(values),
        "min_ms": min(values),
        "p50_ms": _nearest_rank(values, 0.50),
        "p95_ms": _nearest_rank(values, 0.95),
        "max_ms": max(values),
    }


def _round_floats(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {
            key: _round_floats(item, digits)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, digits) for item in value]
    return value


class HeadlessPlaybackScheduler:
    """Incrementally schedule validated frames with the registered policy.

    A successful report proves that every accepted frame was scheduled once,
    in order, and that the incremental decisions exactly matched a canonical
    deterministic replay. Protocol header/binary validation remains the
    caller's responsibility.
    """

    def __init__(self) -> None:
        self._observations: list[ValidatedFrameObservation] = []
        self._schedule: list[HeadlessScheduledFrame] = []
        self._completed_parent_count = 0
        self._stream_generation: int | None = None
        self._active_parent_sequence_id: int | None = None
        self._active_parent_frame_count = 0
        self._active_source_range: tuple[
            float | None, float | None
        ] | None = None
        self._next_parent_sequence_id = 0
        self._previous_arrival_seconds = -math.inf
        self._previous_mode: PlaybackMode = "normal"
        self._next_start_seconds = 0.0
        self._completion_mode: str | None = None
        self._finalized = False

    @property
    def schedule(self) -> tuple[HeadlessScheduledFrame, ...]:
        """Return immutable access to decisions accepted so far."""

        return tuple(self._schedule)

    def _require_open(self) -> None:
        if self._finalized:
            raise ValueError("scheduler is already finalized")

    def _require_generation(self, generation: object) -> int:
        normalized = _require_integer(
            generation,
            field_name="stream_generation",
            minimum=1,
        )
        if self._stream_generation is None:
            self._stream_generation = normalized
        elif normalized != self._stream_generation:
            raise ValueError("stream_generation changed within one capture")
        return normalized

    def _select_completion_mode(self, mode: str) -> None:
        if self._completion_mode is None:
            self._completion_mode = mode
        elif self._completion_mode != mode:
            raise ValueError(
                "cannot mix validated-sink and explicit-completion inputs"
            )

    def _infer_active_parent_complete(self) -> None:
        """Close a parent already reconciled by the upstream metadata tracker."""

        if self._active_parent_sequence_id is None:
            return
        self._completed_parent_count += 1
        self._next_parent_sequence_id += 1
        self._active_parent_sequence_id = None
        self._active_parent_frame_count = 0
        self._active_source_range = None

    def accept(
        self,
        frame: ValidatedAudioFrameLike,
    ) -> HeadlessScheduledFrame:
        """Accept a ``batch_latency_test.ValidatedAudioFrame`` structurally.

        The batch receiver calls its sink only after strict protocol-v1
        header/binary pairing succeeds. A successful test run also reconciles
        every parent completion marker before terminal completion, so this
        adapter can infer parent boundaries from the already-validated ordered
        frame stream without importing ``batch_latency_test`` and creating a
        module cycle.
        """

        self._require_open()
        self._select_completion_mode("validated_sink")
        try:
            protocol_version = frame.protocol_version
            stream_generation = frame.stream_generation
            parent_sequence_id = frame.parent_sequence_id
            audio_frame_id = frame.audio_frame_id
            audio_bytes = frame.audio_bytes
            sample_rate_hz = frame.sample_rate_hz
            channels = frame.channels
            bytes_per_sample = frame.bytes_per_sample
            arrival_seconds = frame.arrival_seconds
            source_start_ms = frame.source_start_ms
            source_end_ms = frame.source_end_ms
        except AttributeError as exc:
            raise TypeError(
                "frame does not implement the ValidatedAudioFrame contract"
            ) from exc

        if (
            _require_integer(
                protocol_version,
                field_name="protocol_version",
                minimum=1,
            )
            != 1
        ):
            raise ValueError("protocol_version must equal 1")
        normalized_audio_bytes = _require_integer(
            audio_bytes,
            field_name="audio_bytes",
            minimum=1,
        )
        normalized_sample_rate = _require_integer(
            sample_rate_hz,
            field_name="sample_rate_hz",
            minimum=1,
        )
        normalized_channels = _require_integer(
            channels,
            field_name="channels",
            minimum=1,
        )
        normalized_bytes_per_sample = _require_integer(
            bytes_per_sample,
            field_name="bytes_per_sample",
            minimum=1,
        )
        frame_width = normalized_channels * normalized_bytes_per_sample
        if normalized_audio_bytes % frame_width:
            raise ValueError(
                "audio_bytes must align to channels * bytes_per_sample"
            )
        duration_seconds = normalized_audio_bytes / (
            normalized_sample_rate * frame_width
        )

        normalized_parent_id = _require_integer(
            parent_sequence_id,
            field_name="parent_sequence_id",
            minimum=0,
        )
        if (
            self._active_parent_sequence_id is not None
            and normalized_parent_id
            != self._active_parent_sequence_id
        ):
            if (
                normalized_parent_id
                != self._active_parent_sequence_id + 1
            ):
                raise ValueError(
                    "parent_sequence_id must be contiguous and ordered "
                    "from zero"
                )
            self._infer_active_parent_complete()

        return self._accept_frame(
            ValidatedFrameObservation(
                arrival_seconds=arrival_seconds,
                duration_seconds=duration_seconds,
                stream_generation=stream_generation,
                parent_sequence_id=normalized_parent_id,
                audio_frame_id=audio_frame_id,
                source_start_ms=source_start_ms,
                source_end_ms=source_end_ms,
            )
        )

    def accept_frame(
        self,
        observation: ValidatedFrameObservation,
    ) -> HeadlessScheduledFrame:
        """Accept one frame for the explicit parent-completion API."""

        self._require_open()
        self._select_completion_mode("explicit_completion")
        return self._accept_frame(observation)

    def _accept_frame(
        self,
        observation: ValidatedFrameObservation,
    ) -> HeadlessScheduledFrame:
        """Validate identity order and schedule one frame immediately."""

        if not isinstance(observation, ValidatedFrameObservation):
            raise TypeError(
                "observation must be a ValidatedFrameObservation"
            )

        generation = self._require_generation(
            observation.stream_generation
        )
        arrival_seconds = _require_finite_nonnegative(
            observation.arrival_seconds,
            field_name="arrival_seconds",
        )
        duration_seconds = _require_finite_positive(
            observation.duration_seconds,
            field_name="duration_seconds",
        )
        parent_sequence_id = _require_integer(
            observation.parent_sequence_id,
            field_name="parent_sequence_id",
            minimum=0,
        )
        audio_frame_id = _require_integer(
            observation.audio_frame_id,
            field_name="audio_frame_id",
            minimum=0,
        )
        source_range = _normalize_source_range(
            observation.source_start_ms,
            observation.source_end_ms,
        )

        if arrival_seconds < self._previous_arrival_seconds:
            raise ValueError(
                "frames must be accepted in non-decreasing arrival order"
            )
        if self._active_parent_sequence_id is None:
            if parent_sequence_id != self._next_parent_sequence_id:
                raise ValueError(
                    "parent_sequence_id must be contiguous and ordered "
                    "from zero"
                )
            if audio_frame_id != 0:
                raise ValueError(
                    "each parent must start with audio_frame_id zero"
                )
            self._active_parent_sequence_id = parent_sequence_id
            self._active_source_range = source_range
        else:
            if parent_sequence_id != self._active_parent_sequence_id:
                raise ValueError(
                    "a new parent began before the active parent completed"
                )
            if source_range != self._active_source_range:
                raise ValueError(
                    "source offsets must remain stable within a parent"
                )
        if audio_frame_id != self._active_parent_frame_count:
            raise ValueError(
                "audio_frame_id must be contiguous and zero-based "
                "within each parent"
            )

        source_index = len(self._observations)
        start_seconds = max(self._next_start_seconds, arrival_seconds)
        wait_seconds = max(0.0, start_seconds - arrival_seconds)
        projected_queue = wait_seconds + duration_seconds
        mode = select_playback_mode(
            projected_queue,
            self._previous_mode,
            DEFAULT_PLAYBACK_POLICY,
        )
        playback_rate = playback_rate_for_mode(
            mode,
            DEFAULT_PLAYBACK_POLICY,
        )
        end_seconds = start_seconds + duration_seconds / playback_rate
        queue_depth = max(0.0, end_seconds - arrival_seconds)
        decision = HeadlessScheduledFrame(
            stream_generation=generation,
            parent_sequence_id=parent_sequence_id,
            audio_frame_id=audio_frame_id,
            source_start_ms=source_range[0],
            source_end_ms=source_range[1],
            source_index=source_index,
            arrival_seconds=arrival_seconds,
            source_duration_seconds=duration_seconds,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            wait_before_playback_seconds=wait_seconds,
            projected_queue_at_normal_rate_seconds=projected_queue,
            queue_depth_seconds=queue_depth,
            playback_rate=playback_rate,
            playback_mode=mode,
            mode_changed=mode != self._previous_mode,
            above_target=(
                queue_depth
                > DEFAULT_PLAYBACK_POLICY.target_queue_seconds
            ),
            above_limit=(
                queue_depth
                > DEFAULT_PLAYBACK_POLICY.limit_queue_seconds
            ),
        )

        self._observations.append(
            ValidatedFrameObservation(
                arrival_seconds=arrival_seconds,
                duration_seconds=duration_seconds,
                stream_generation=generation,
                parent_sequence_id=parent_sequence_id,
                audio_frame_id=audio_frame_id,
                source_start_ms=source_range[0],
                source_end_ms=source_range[1],
            )
        )
        self._schedule.append(decision)
        self._active_parent_frame_count += 1
        self._previous_arrival_seconds = arrival_seconds
        self._previous_mode = mode
        self._next_start_seconds = end_seconds
        return decision

    def complete_parent(
        self,
        completion: ValidatedParentCompletion,
    ) -> None:
        """Reconcile one completion marker before accepting another parent."""

        self._require_open()
        self._select_completion_mode("explicit_completion")
        if not isinstance(completion, ValidatedParentCompletion):
            raise TypeError(
                "completion must be a ValidatedParentCompletion"
            )
        self._require_generation(completion.stream_generation)
        parent_sequence_id = _require_integer(
            completion.parent_sequence_id,
            field_name="parent_sequence_id",
            minimum=0,
        )
        audio_frame_count = _require_integer(
            completion.audio_frame_count,
            field_name="audio_frame_count",
            minimum=1,
        )
        source_range = _normalize_source_range(
            completion.source_start_ms,
            completion.source_end_ms,
        )
        if self._active_parent_sequence_id is None:
            raise ValueError("parent completion has no active parent")
        if parent_sequence_id != self._active_parent_sequence_id:
            raise ValueError("parent completion references the wrong parent")
        if audio_frame_count != self._active_parent_frame_count:
            raise ValueError(
                "parent completion frame count does not reconcile"
            )
        if source_range != self._active_source_range:
            raise ValueError(
                "parent completion source range does not reconcile"
            )

        self._completed_parent_count += 1
        self._next_parent_sequence_id += 1
        self._active_parent_sequence_id = None
        self._active_parent_frame_count = 0
        self._active_source_range = None

    def _verify_canonical_replay(
        self,
        *,
        input_end_seconds: float,
    ):
        canonical = simulate_playback(
            tuple(
                AudioChunk(
                    arrival_seconds=observation.arrival_seconds,
                    duration_seconds=observation.duration_seconds,
                    source_index=index,
                )
                for index, observation in enumerate(self._observations)
            ),
            input_end_seconds=input_end_seconds,
            adaptive=True,
            policy=DEFAULT_PLAYBACK_POLICY,
        )
        incremental = tuple(
            decision.canonical_chunk() for decision in self._schedule
        )
        if incremental != canonical.schedule:
            raise RuntimeError(
                "incremental schedule differs from canonical "
                "simulate_playback replay"
            )
        return canonical

    def _parent_rows(
        self,
        *,
        input_sample_zero_seconds: float,
    ) -> list[dict[str, float | int | None]]:
        grouped: list[list[HeadlessScheduledFrame]] = []
        active_parent: int | None = None
        for decision in self._schedule:
            if decision.parent_sequence_id != active_parent:
                grouped.append([])
                active_parent = decision.parent_sequence_id
            grouped[-1].append(decision)

        rows: list[dict[str, float | int | None]] = []
        for frames in grouped:
            first = frames[0]
            final = frames[-1]
            source_end_clock_seconds = (
                input_sample_zero_seconds + first.source_end_ms / 1000.0
                if first.source_end_ms is not None
                else None
            )
            rows.append(
                {
                    "parent_sequence_id": first.parent_sequence_id,
                    "source_start_ms": first.source_start_ms,
                    "source_end_ms": first.source_end_ms,
                    "source_end_to_first_arrival_ms": (
                        (
                            first.arrival_seconds
                            - source_end_clock_seconds
                        )
                        * 1000.0
                        if source_end_clock_seconds is not None
                        else None
                    ),
                    "source_end_to_first_scheduled_start_ms": (
                        (
                            first.start_seconds
                            - source_end_clock_seconds
                        )
                        * 1000.0
                        if source_end_clock_seconds is not None
                        else None
                    ),
                    "source_end_to_final_scheduled_end_ms": (
                        (
                            final.end_seconds
                            - source_end_clock_seconds
                        )
                        * 1000.0
                        if source_end_clock_seconds is not None
                        else None
                    ),
                    "scheduled_envelope_duration_ms": (
                        final.end_seconds - first.start_seconds
                    )
                    * 1000.0,
                }
            )
        return rows

    @staticmethod
    def _frontier_drift(
        parent_rows: Sequence[dict[str, float | int | None]],
    ) -> dict[str, Any]:
        eligible = [
            row
            for row in parent_rows
            if row["source_start_ms"] is not None
            and row["source_end_ms"] is not None
            and row["source_end_to_first_scheduled_start_ms"] is not None
        ]
        eligible.sort(
            key=lambda row: (
                float(row["source_end_ms"]),
                int(row["parent_sequence_id"]),
            )
        )
        sample_count = len(eligible)
        sufficient = sample_count >= 4
        quartile_size = math.ceil(sample_count / 4) if sufficient else 0
        first_median: float | None = None
        final_median: float | None = None
        delta: float | None = None
        if sufficient:
            first_values = [
                float(row["source_end_to_first_scheduled_start_ms"])
                for row in eligible[:quartile_size]
            ]
            final_values = [
                float(row["source_end_to_first_scheduled_start_ms"])
                for row in eligible[-quartile_size:]
            ]
            first_median = statistics.median(first_values)
            final_median = statistics.median(final_values)
            delta = final_median - first_median
        return {
            "metric": "source_end_to_first_scheduled_start_ms",
            "population": (
                "complete parents with both ASR source-range offsets"
            ),
            "sample_count": sample_count,
            "minimum_samples_required": 4,
            "sufficient_sample_count": sufficient,
            "quartile_size": quartile_size,
            "first_quartile_median_ms": first_median,
            "final_quartile_median_ms": final_median,
            "final_minus_first_ms": delta,
            "positive_delta_means": (
                "scheduled source-frontier delay accumulated later "
                "in the capture"
            ),
        }

    def finalize(
        self,
        *,
        input_end_seconds: float,
        input_sample_zero_seconds: float,
    ) -> dict[str, Any]:
        """Cross-check the schedule and return a fixed aggregate report."""

        self._require_open()
        normalized_input_end = _require_finite_nonnegative(
            input_end_seconds,
            field_name="input_end_seconds",
        )
        normalized_sample_zero = _require_finite_nonnegative(
            input_sample_zero_seconds,
            field_name="input_sample_zero_seconds",
        )
        if normalized_sample_zero > normalized_input_end:
            raise ValueError(
                "input_sample_zero_seconds cannot follow input_end_seconds"
            )
        if not self._observations:
            raise ValueError("at least one frame observation is required")
        if (
            self._completion_mode == "validated_sink"
            and self._active_parent_sequence_id is not None
        ):
            self._infer_active_parent_complete()
        if self._active_parent_sequence_id is not None:
            raise ValueError(
                "cannot finalize before the active parent completes"
            )
        if self._completed_parent_count != self._next_parent_sequence_id:
            raise RuntimeError("completed parent accounting is inconsistent")

        canonical = self._verify_canonical_replay(
            input_end_seconds=normalized_input_end
        )
        summary = canonical.summary
        parent_rows = self._parent_rows(
            input_sample_zero_seconds=normalized_sample_zero
        )
        source_end_rows = [
            row
            for row in parent_rows
            if row["source_end_ms"] is not None
        ]
        source_range_parent_count = sum(
            row["source_start_ms"] is not None
            and row["source_end_ms"] is not None
            for row in parent_rows
        )
        end_only_parent_count = sum(
            row["source_start_ms"] is None
            and row["source_end_ms"] is not None
            for row in parent_rows
        )
        unavailable_parent_count = (
            len(parent_rows)
            - source_range_parent_count
            - end_only_parent_count
        )

        source_end_to_arrival = [
            float(row["source_end_to_first_arrival_ms"])
            for row in source_end_rows
        ]
        source_end_to_start = [
            float(row["source_end_to_first_scheduled_start_ms"])
            for row in source_end_rows
        ]
        source_end_to_final_end = [
            float(row["source_end_to_final_scheduled_end_ms"])
            for row in source_end_rows
        ]
        envelope_durations = [
            float(row["scheduled_envelope_duration_ms"])
            for row in parent_rows
        ]

        p95_pass = (
            summary.time_weighted_queue_p95_seconds
            <= DEFAULT_PLAYBACK_POLICY.target_queue_seconds
        )
        peak_pass = (
            summary.peak_queue_depth_seconds
            <= DEFAULT_PLAYBACK_POLICY.limit_queue_seconds
        )
        integrity_pass = (
            summary.chunks_received == len(self._observations)
            and summary.chunks_scheduled == len(self._observations)
            and summary.chunks_dropped == 0
            and len(self._schedule) == len(self._observations)
        )
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": REPORT_TYPE,
            "policy": asdict(DEFAULT_PLAYBACK_POLICY),
            "capture": {
                "stream_generation": self._stream_generation,
                "frames_received": len(self._observations),
                "frames_scheduled": len(self._schedule),
                "complete_parents": self._completed_parent_count,
                "parent_reconciliation": (
                    "upstream_protocol_v1_tracker"
                    if self._completion_mode == "validated_sink"
                    else "explicit_completion_observations"
                ),
                "input_end_seconds": normalized_input_end,
                "translated_pcm_seconds": (
                    summary.total_source_duration_seconds
                ),
                "scheduled_playback_seconds": (
                    summary.total_scheduled_duration_seconds
                ),
                "playback_end_seconds": summary.playback_end_seconds,
                "listener_tail_seconds": summary.listener_tail_seconds,
                "canonical_replay_verified": True,
            },
            "queue_observation": {
                "arrival_queue_p50_seconds": (
                    summary.arrival_queue_p50_seconds
                ),
                "arrival_queue_p95_seconds": (
                    summary.arrival_queue_p95_seconds
                ),
                "time_weighted_queue_p50_seconds": (
                    summary.time_weighted_queue_p50_seconds
                ),
                "time_weighted_queue_p95_seconds": (
                    summary.time_weighted_queue_p95_seconds
                ),
                "peak_queue_depth_seconds": (
                    summary.peak_queue_depth_seconds
                ),
                "seconds_above_target": summary.seconds_above_target,
                "seconds_above_limit": summary.seconds_above_limit,
                "limit_breach_entries": summary.limit_breach_entries,
                "accelerated_source_percent": (
                    summary.accelerated_source_percent
                ),
                "urgent_source_percent": summary.urgent_source_percent,
            },
            "queue_gate": {
                "p95_metric": "time_weighted_queue_p95_seconds",
                "p95_objective_seconds": (
                    DEFAULT_PLAYBACK_POLICY.target_queue_seconds
                ),
                "p95_observed_seconds": (
                    summary.time_weighted_queue_p95_seconds
                ),
                "p95_pass": p95_pass,
                "peak_metric": "peak_queue_depth_seconds",
                "peak_limit_seconds": (
                    DEFAULT_PLAYBACK_POLICY.limit_queue_seconds
                ),
                "peak_observed_seconds": (
                    summary.peak_queue_depth_seconds
                ),
                "peak_pass": peak_pass,
                "frames_dropped": summary.chunks_dropped,
                "frames_reordered": 0,
                "frames_duplicated": 0,
                "all_frames_preserved_once_in_order": integrity_pass,
                "result": (
                    "pass"
                    if p95_pass and peak_pass and integrity_pass
                    else "fail"
                ),
            },
            "source_boundary_observation": {
                "clock": "client_monotonic",
                "source_offset_origin": "input_pcm_sample_zero",
                "sample_unit": "first_translated_frame_per_complete_parent",
                "complete_parent_count": len(parent_rows),
                "parents_with_asr_source_range": (
                    source_range_parent_count
                ),
                "parents_with_audio_processed_end_only": (
                    end_only_parent_count
                ),
                "parents_without_source_end": unavailable_parent_count,
                "source_end_to_first_arrival_ms": _distribution_ms(
                    source_end_to_arrival
                ),
                "source_end_to_first_scheduled_start_ms": _distribution_ms(
                    source_end_to_start
                ),
            },
            "parent_schedule_envelope": {
                "sample_unit": "complete_translated_parent",
                "parent_count": len(parent_rows),
                "source_attributed_parent_count": len(source_end_rows),
                "source_end_to_first_scheduled_start_ms": _distribution_ms(
                    source_end_to_start
                ),
                "source_end_to_final_scheduled_end_ms": _distribution_ms(
                    source_end_to_final_end
                ),
                "scheduled_envelope_duration_ms": _distribution_ms(
                    envelope_durations
                ),
            },
            "accumulated_frontier_drift": self._frontier_drift(parent_rows),
            "claim_scope": CLAIM_SCOPE,
            "privacy": PRIVACY_DECLARATION,
        }
        self._finalized = True
        rounded = _round_floats(report)
        rounded_gate = rounded["queue_gate"]
        rounded_gate["p95_pass"] = (
            rounded_gate["p95_observed_seconds"]
            <= rounded_gate["p95_objective_seconds"]
        )
        rounded_gate["peak_pass"] = (
            rounded_gate["peak_observed_seconds"]
            <= rounded_gate["peak_limit_seconds"]
        )
        rounded_gate["result"] = (
            "pass"
            if (
                rounded_gate["p95_pass"]
                and rounded_gate["peak_pass"]
                and rounded_gate["all_frames_preserved_once_in_order"]
            )
            else "fail"
        )
        validate_headless_playback_report(rounded)
        return rounded
