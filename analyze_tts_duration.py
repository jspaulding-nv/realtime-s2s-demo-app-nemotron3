#!/usr/bin/env python3
"""Fit a privacy-safe translated-character to TTS-duration capacity model."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


DEFAULT_TARGET_P95_SECONDS = 4.0
DEFAULT_TARGET_MAX_SECONDS = 8.0
DEFAULT_CAP_STEP_CHARS = 5
DEFAULT_MAX_CANDIDATE_CHARS = 240
DEFAULT_COMPARISON_CAPS = (40, 45, 60)


@dataclass(frozen=True)
class TtsDurationObservation:
    """One transcript-free TTS input/output measurement."""

    sample_index: int
    sequence_id: int
    text_chars: int
    audio_duration_seconds: float
    subsequence_id: int = 0
    subsequence_count: int = 1
    telemetry_schema_version: int = 1

    def __post_init__(self) -> None:
        for name, value, allow_zero in (
            ("sample_index", self.sample_index, False),
            ("sequence_id", self.sequence_id, True),
            ("text_chars", self.text_chars, False),
            ("subsequence_id", self.subsequence_id, True),
            ("subsequence_count", self.subsequence_count, False),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < (0 if allow_zero else 1)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a {qualifier} integer")
        if (
            not isinstance(self.audio_duration_seconds, (int, float))
            or isinstance(self.audio_duration_seconds, bool)
            or not math.isfinite(self.audio_duration_seconds)
            or self.audio_duration_seconds <= 0
        ):
            raise ValueError(
                "audio_duration_seconds must be positive and finite"
            )
        if self.subsequence_id >= self.subsequence_count:
            raise ValueError(
                "subsequence_id must be within the subsequence_count range"
            )
        if (
            not isinstance(self.telemetry_schema_version, int)
            or isinstance(self.telemetry_schema_version, bool)
            or self.telemetry_schema_version not in {1, 2}
        ):
            raise ValueError("telemetry_schema_version must be one or two")
        if self.telemetry_schema_version == 1 and (
            self.subsequence_id != 0 or self.subsequence_count != 1
        ):
            raise ValueError(
                "version-one observations must use the legacy (0, 1) "
                "subsequence identity"
            )

    @property
    def parent_sequence_id(self) -> int:
        return self.sequence_id

    @property
    def composite_key(self) -> tuple[int, int, int]:
        return (
            self.parent_sequence_id,
            self.subsequence_id,
            self.subsequence_count,
        )


@dataclass(frozen=True)
class DurationModel:
    """OLS center line with empirical one-sided residual envelopes."""

    observation_count: int
    intercept_seconds: float
    seconds_per_char: float
    r_squared: float
    residual_p50_seconds: float
    residual_p95_seconds: float
    residual_p99_seconds: float
    residual_max_seconds: float

    def p95_envelope_seconds(self, text_chars: int) -> float:
        return (
            self.intercept_seconds
            + self.seconds_per_char * text_chars
            + self.residual_p95_seconds
        )

    def observed_max_residual_envelope_seconds(self, text_chars: int) -> float:
        return (
            self.intercept_seconds
            + self.seconds_per_char * text_chars
            + self.residual_max_seconds
        )


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a quantile from no observations")
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be greater than zero and at most one")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


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


def _require_positive_finite(
    value: Any,
    *,
    field: str,
    label: str,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label}: {field} must be positive and finite")
    return float(value)


def _require_sequence_ids(
    value: Any,
    *,
    field: str,
    expected: list[int],
    label: str,
) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label}: {field} must be a list")
    parsed = [
        _require_nonnegative_int(item, field=field, label=label)
        for item in value
    ]
    if parsed != expected:
        raise ValueError(
            f"{label}: {field} does not match the complete ordered sequence"
        )


def _event_composite_key(
    event: dict[str, Any],
    *,
    label: str,
) -> tuple[int, int, int]:
    sequence_id = _require_nonnegative_int(
        event.get("sequence_id"),
        field="sequence_id",
        label=label,
    )
    parent_sequence_id = _require_nonnegative_int(
        event.get("parent_sequence_id"),
        field="parent_sequence_id",
        label=label,
    )
    if sequence_id != parent_sequence_id:
        raise ValueError(
            f"{label}: parent_sequence_id does not match sequence_id"
        )
    subsequence_id = _require_nonnegative_int(
        event.get("subsequence_id"),
        field="subsequence_id",
        label=label,
    )
    subsequence_count = _require_positive_int(
        event.get("subsequence_count"),
        field="subsequence_count",
        label=label,
    )
    if subsequence_id >= subsequence_count:
        raise ValueError(
            f"{label}: subsequence_id is outside subsequence_count"
        )
    return (parent_sequence_id, subsequence_id, subsequence_count)


def _summary_composite_key(
    value: Any,
    *,
    field: str,
    label: str,
) -> tuple[int, int, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: {field} entries must be objects")
    parent_sequence_id = _require_nonnegative_int(
        value.get("parent_sequence_id"),
        field=f"{field}.parent_sequence_id",
        label=label,
    )
    subsequence_id = _require_nonnegative_int(
        value.get("subsequence_id"),
        field=f"{field}.subsequence_id",
        label=label,
    )
    subsequence_count = _require_positive_int(
        value.get("subsequence_count"),
        field=f"{field}.subsequence_count",
        label=label,
    )
    if subsequence_id >= subsequence_count:
        raise ValueError(
            f"{label}: {field}.subsequence_id is outside subsequence_count"
        )
    return (parent_sequence_id, subsequence_id, subsequence_count)


def _require_composite_keys(
    value: Any,
    *,
    field: str,
    expected: Sequence[tuple[int, int, int]],
    label: str,
) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label}: {field} must be a list")
    parsed = [
        _summary_composite_key(item, field=field, label=label)
        for item in value
    ]
    if parsed != list(expected):
        raise ValueError(
            f"{label}: {field} does not match the complete ordered "
            "subsequence list"
        )


def _parent_ids_from_contiguous_composite_keys(
    keys: Sequence[tuple[int, int, int]],
    *,
    label: str,
) -> list[int]:
    if not keys:
        raise ValueError(f"{label}: no TTS subsequences were observed")

    expected_parent = 0
    expected_subsequence = 0
    expected_count: int | None = None
    for parent, subsequence, count in keys:
        if parent != expected_parent or subsequence != expected_subsequence:
            raise ValueError(
                f"{label}: TTS composite identities are not contiguous "
                "from parent zero"
            )
        if expected_subsequence == 0:
            expected_count = count
        elif count != expected_count:
            raise ValueError(
                f"{label}: subsequence_count changed within a parent"
            )

        if subsequence + 1 == count:
            expected_parent += 1
            expected_subsequence = 0
            expected_count = None
        else:
            expected_subsequence += 1

    if expected_subsequence != 0:
        raise ValueError(
            f"{label}: final parent has an incomplete subsequence list"
        )
    return list(range(expected_parent))


def _load_v2_summary_observations(
    staged: dict[str, Any],
    events: list[Any],
    *,
    sample_index: int,
    label: str,
) -> tuple[TtsDurationObservation, ...]:
    """Load strict parent/child telemetry from a schema-v2 capture."""

    if staged.get("tts_subsegmentation_enabled") is not True:
        raise ValueError(
            f"{label}: schema-v2 telemetry must enable TTS subsegmentation"
        )

    nmt_completed_chars: dict[int, int] = {}
    nmt_completed_order: list[int] = []
    started: dict[tuple[int, int, int], tuple[int, int]] = {}
    started_order: list[tuple[int, int, int]] = []
    completed: dict[tuple[int, int, int], tuple[float, int]] = {}
    completed_order: list[tuple[int, int, int]] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("stage") == "nmt" and event.get("event") == "completed":
            sequence_id = _require_nonnegative_int(
                event.get("sequence_id"),
                field="sequence_id",
                label=label,
            )
            if sequence_id in nmt_completed_chars:
                raise ValueError(
                    f"{label}: duplicate NMT completed event for a parent"
                )
            nmt_completed_chars[sequence_id] = _require_positive_int(
                event.get("text_chars"),
                field="text_chars",
                label=label,
            )
            nmt_completed_order.append(sequence_id)
            continue
        if event.get("stage") != "tts":
            continue
        event_name = event.get("event")
        if event_name not in {"started", "completed"}:
            continue

        key = _event_composite_key(event, label=label)
        if event_name == "started":
            if key in started:
                raise ValueError(
                    f"{label}: duplicate TTS started event for a subsequence"
                )
            started[key] = (
                _require_positive_int(
                    event.get("text_chars"),
                    field="text_chars",
                    label=label,
                ),
                _require_positive_int(
                    event.get("parent_text_chars"),
                    field="parent_text_chars",
                    label=label,
                ),
            )
            started_order.append(key)
            continue

        if key in completed:
            raise ValueError(
                f"{label}: duplicate TTS completed event for a subsequence"
            )
        audio_duration_ms = _require_positive_finite(
            event.get("audio_duration_ms"),
            field="audio_duration_ms",
            label=label,
        )
        _require_positive_int(
            event.get("audio_bytes"),
            field="audio_bytes",
            label=label,
        )
        retry_count = _require_nonnegative_int(
            event.get("retry_count"),
            field="retry_count",
            label=label,
        )
        if retry_count not in {0, 1}:
            raise ValueError(f"{label}: retry_count must be zero or one")
        completed[key] = (audio_duration_ms / 1_000.0, retry_count)
        completed_order.append(key)

    if not started or set(started) != set(completed):
        raise ValueError(
            f"{label}: TTS started/completed composite sets do not match"
        )
    if completed_order != started_order:
        raise ValueError(
            f"{label}: TTS started/completed composite order does not match"
        )

    parent_ids = _parent_ids_from_contiguous_composite_keys(
        started_order,
        label=label,
    )
    if nmt_completed_order != parent_ids:
        raise ValueError(
            f"{label}: NMT parent IDs do not match the ordered TTS parents"
        )
    if any(
        started[key][1] != nmt_completed_chars[key[0]]
        for key in started_order
    ):
        raise ValueError(
            f"{label}: TTS parent_text_chars differ from NMT parent counts"
        )

    parent_count = len(parent_ids)
    child_count = len(started_order)
    for field, expected_count in (
        ("segments_emitted", parent_count),
        ("audio_segments_produced", child_count),
        ("tts_subsegments_planned", child_count),
        ("tts_subsegments_produced", child_count),
    ):
        count = _require_nonnegative_int(
            staged.get(field),
            field=field,
            label=label,
        )
        if count != expected_count:
            raise ValueError(
                f"{label}: {field} does not match paired TTS events"
            )

    for field in (
        "completed_sequence_ids",
        "websocket_sent_sequence_ids",
    ):
        _require_sequence_ids(
            staged.get(field),
            field=field,
            expected=parent_ids,
            label=label,
        )
    for field in (
        "planned_subsegment_keys",
        "synthesized_subsegment_keys",
        "completed_subsegment_keys",
        "websocket_sent_subsegment_keys",
    ):
        _require_composite_keys(
            staged.get(field),
            field=field,
            expected=started_order,
            label=label,
        )
    _require_composite_keys(
        staged.get("incomplete_subsegment_keys"),
        field="incomplete_subsegment_keys",
        expected=(),
        label=label,
    )

    tts_retry_count = _require_nonnegative_int(
        staged.get("tts_retry_count"),
        field="tts_retry_count",
        label=label,
    )
    if tts_retry_count != sum(item[1] for item in completed.values()):
        raise ValueError(
            f"{label}: tts_retry_count does not match completed events"
        )

    return tuple(
        TtsDurationObservation(
            sample_index=sample_index,
            sequence_id=parent_sequence_id,
            subsequence_id=subsequence_id,
            subsequence_count=subsequence_count,
            text_chars=started[key][0],
            audio_duration_seconds=completed[key][0],
            telemetry_schema_version=2,
        )
        for key in started_order
        for parent_sequence_id, subsequence_id, subsequence_count in (key,)
    )


def load_summary_observations(
    path: Path,
    *,
    sample_index: int,
) -> tuple[TtsDurationObservation, ...]:
    """Load paired structural TTS events from one completed batch summary."""

    label = f"sample_{sample_index:02d}"
    try:
        with path.open(encoding="utf-8") as handle:
            root = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: summary could not be read as JSON") from exc
    if not isinstance(root, dict):
        raise ValueError(f"{label}: summary root must be an object")

    integrity = root.get("staged_integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("applicable") is not True
        or integrity.get("passed") is not True
        or integrity.get("errors") != []
    ):
        raise ValueError(f"{label}: staged integrity did not pass cleanly")

    staged = root.get("staged_pipeline")
    if not isinstance(staged, dict):
        raise ValueError(f"{label}: staged pipeline telemetry is required")
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

    events = staged.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{label}: staged events must be a list")

    telemetry_schema_version = staged.get("telemetry_schema_version", 1)
    if (
        not isinstance(telemetry_schema_version, int)
        or isinstance(telemetry_schema_version, bool)
        or telemetry_schema_version not in {1, 2}
    ):
        raise ValueError(
            f"{label}: telemetry_schema_version must be one or two"
        )
    if telemetry_schema_version == 2:
        return _load_v2_summary_observations(
            staged,
            events,
            sample_index=sample_index,
            label=label,
        )

    nmt_completed_chars: dict[int, int] = {}
    started: dict[int, int] = {}
    completed: dict[int, tuple[float, int]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("stage") == "nmt" and event.get("event") == "completed":
            sequence_id = _require_nonnegative_int(
                event.get("sequence_id"),
                field="sequence_id",
                label=label,
            )
            if sequence_id in nmt_completed_chars:
                raise ValueError(
                    f"{label}: duplicate NMT completed event for a sequence"
                )
            nmt_completed_chars[sequence_id] = _require_positive_int(
                event.get("text_chars"),
                field="text_chars",
                label=label,
            )
            continue
        if event.get("stage") != "tts":
            continue
        event_name = event.get("event")
        if event_name not in {"started", "completed"}:
            continue
        sequence_id = _require_nonnegative_int(
            event.get("sequence_id"),
            field="sequence_id",
            label=label,
        )
        if event_name == "started":
            if sequence_id in started:
                raise ValueError(
                    f"{label}: duplicate TTS started event for a sequence"
                )
            started[sequence_id] = _require_positive_int(
                event.get("text_chars"),
                field="text_chars",
                label=label,
            )
            continue

        if sequence_id in completed:
            raise ValueError(
                f"{label}: duplicate TTS completed event for a sequence"
            )
        audio_duration_ms = _require_positive_finite(
            event.get("audio_duration_ms"),
            field="audio_duration_ms",
            label=label,
        )
        _require_positive_int(
            event.get("audio_bytes"),
            field="audio_bytes",
            label=label,
        )
        retry_count = _require_nonnegative_int(
            event.get("retry_count"),
            field="retry_count",
            label=label,
        )
        if retry_count not in {0, 1}:
            raise ValueError(f"{label}: retry_count must be zero or one")
        completed[sequence_id] = (audio_duration_ms / 1_000.0, retry_count)

    if not started or set(started) != set(completed):
        raise ValueError(
            f"{label}: TTS started/completed sequence sets do not match"
        )
    if set(nmt_completed_chars) != set(started):
        raise ValueError(
            f"{label}: NMT-completed and TTS-started sequence sets do not match"
        )
    if any(
        nmt_completed_chars[sequence_id] != text_chars
        for sequence_id, text_chars in started.items()
    ):
        raise ValueError(
            f"{label}: NMT-completed and TTS-started character counts differ"
        )
    sequence_ids = sorted(started)
    expected_ids = list(range(len(sequence_ids)))
    if sequence_ids != expected_ids:
        raise ValueError(f"{label}: TTS sequence IDs are not contiguous from zero")

    for field in (
        "segments_emitted",
        "audio_segments_produced",
    ):
        count = _require_nonnegative_int(
            staged.get(field),
            field=field,
            label=label,
        )
        if count != len(sequence_ids):
            raise ValueError(f"{label}: {field} does not match paired TTS events")
    for field in (
        "completed_sequence_ids",
        "websocket_sent_sequence_ids",
    ):
        _require_sequence_ids(
            staged.get(field),
            field=field,
            expected=expected_ids,
            label=label,
        )
    tts_retry_count = _require_nonnegative_int(
        staged.get("tts_retry_count"),
        field="tts_retry_count",
        label=label,
    )
    if tts_retry_count != sum(item[1] for item in completed.values()):
        raise ValueError(
            f"{label}: tts_retry_count does not match completed events"
        )

    return tuple(
        TtsDurationObservation(
            sample_index=sample_index,
            sequence_id=sequence_id,
            text_chars=started[sequence_id],
            audio_duration_seconds=completed[sequence_id][0],
        )
        for sequence_id in sequence_ids
    )


def fit_duration_model(
    observations: Sequence[TtsDurationObservation],
) -> DurationModel:
    """Fit an OLS center line and retain empirical residual quantiles."""

    if len(observations) < 2:
        raise ValueError("at least two TTS observations are required")
    text_chars = [item.text_chars for item in observations]
    durations = [item.audio_duration_seconds for item in observations]
    mean_chars = statistics.mean(text_chars)
    mean_duration = statistics.mean(durations)
    centered_chars = sum((value - mean_chars) ** 2 for value in text_chars)
    if centered_chars <= 0:
        raise ValueError("TTS observations must contain varied character counts")
    seconds_per_char = (
        sum(
            (chars - mean_chars) * (duration - mean_duration)
            for chars, duration in zip(text_chars, durations)
        )
        / centered_chars
    )
    if seconds_per_char <= 0:
        raise ValueError("fitted TTS seconds per character must be positive")
    intercept_seconds = mean_duration - seconds_per_char * mean_chars
    residuals = [
        duration - (intercept_seconds + seconds_per_char * chars)
        for chars, duration in zip(text_chars, durations)
    ]
    total_sum_squares = sum(
        (duration - mean_duration) ** 2 for duration in durations
    )
    residual_sum_squares = sum(value**2 for value in residuals)
    r_squared = (
        1.0 - residual_sum_squares / total_sum_squares
        if total_sum_squares > 0
        else 0.0
    )
    return DurationModel(
        observation_count=len(observations),
        intercept_seconds=intercept_seconds,
        seconds_per_char=seconds_per_char,
        r_squared=r_squared,
        residual_p50_seconds=_nearest_rank(residuals, 0.50),
        residual_p95_seconds=_nearest_rank(residuals, 0.95),
        residual_p99_seconds=_nearest_rank(residuals, 0.99),
        residual_max_seconds=max(residuals),
    )


def _largest_integer_cap(
    *,
    target_seconds: float,
    intercept_seconds: float,
    seconds_per_char: float,
    residual_seconds: float,
) -> int:
    raw_limit = (
        target_seconds - intercept_seconds - residual_seconds
    ) / seconds_per_char
    return max(0, math.floor(raw_limit + 1e-12))


def _distribution(
    observations: Sequence[TtsDurationObservation],
) -> dict[str, Any]:
    text_chars = [item.text_chars for item in observations]
    durations = [item.audio_duration_seconds for item in observations]
    return {
        "observation_count": len(observations),
        "text_chars": {
            "min": min(text_chars),
            "p50": _nearest_rank(text_chars, 0.50),
            "p95": _nearest_rank(text_chars, 0.95),
            "max": max(text_chars),
        },
        "audio_duration_seconds": {
            "p50": _nearest_rank(durations, 0.50),
            "p95": _nearest_rank(durations, 0.95),
            "p99": _nearest_rank(durations, 0.99),
            "max": max(durations),
        },
    }


def _model_payload(
    model: DurationModel,
    *,
    target_p95_seconds: float,
    target_max_seconds: float,
) -> dict[str, Any]:
    p95_limit = _largest_integer_cap(
        target_seconds=target_p95_seconds,
        intercept_seconds=model.intercept_seconds,
        seconds_per_char=model.seconds_per_char,
        residual_seconds=model.residual_p95_seconds,
    )
    max_limit = _largest_integer_cap(
        target_seconds=target_max_seconds,
        intercept_seconds=model.intercept_seconds,
        seconds_per_char=model.seconds_per_char,
        residual_seconds=model.residual_max_seconds,
    )
    return {
        "observation_count": model.observation_count,
        "ols": {
            "intercept_seconds": model.intercept_seconds,
            "seconds_per_char": model.seconds_per_char,
            "r_squared": model.r_squared,
        },
        "one_sided_residual_seconds": {
            "p50": model.residual_p50_seconds,
            "p95": model.residual_p95_seconds,
            "p99": model.residual_p99_seconds,
            "observed_max": model.residual_max_seconds,
        },
        "integer_cap_limits_chars": {
            "p95_target": p95_limit,
            "observed_max_residual_target": max_limit,
            "combined": min(p95_limit, max_limit),
        },
    }


def _validate_analysis_options(
    *,
    target_p95_seconds: float,
    target_max_seconds: float,
    cap_step_chars: int,
    max_candidate_chars: int,
    comparison_caps: Sequence[int],
) -> None:
    for name, value in (
        ("target_p95_seconds", target_p95_seconds),
        ("target_max_seconds", target_max_seconds),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be positive and finite")
    if target_max_seconds < target_p95_seconds:
        raise ValueError("target_max_seconds must be at least target_p95_seconds")
    for name, value in (
        ("cap_step_chars", cap_step_chars),
        ("max_candidate_chars", max_candidate_chars),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive integer")
    if cap_step_chars > max_candidate_chars:
        raise ValueError("cap_step_chars cannot exceed max_candidate_chars")
    if not comparison_caps:
        raise ValueError("at least one comparison cap is required")
    for value in comparison_caps:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError("comparison caps must be positive integers")


def _cross_validated_residuals(
    samples: Sequence[Sequence[TtsDurationObservation]],
) -> tuple[float, ...]:
    """Return leave-one-sample-out residuals without fitting on the holdout."""

    if len(samples) < 2:
        return ()
    residuals: list[float] = []
    for holdout_index, holdout in enumerate(samples):
        training = tuple(
            item
            for sample_index, sample in enumerate(samples)
            if sample_index != holdout_index
            for item in sample
        )
        model = fit_duration_model(training)
        residuals.extend(
            item.audio_duration_seconds
            - (
                model.intercept_seconds
                + model.seconds_per_char * item.text_chars
            )
            for item in holdout
        )
    return tuple(residuals)


def _residual_envelope_payload(
    center_model: DurationModel,
    residuals: Sequence[float],
    *,
    target_p95_seconds: float,
    target_max_seconds: float,
) -> dict[str, Any]:
    p95_residual = _nearest_rank(residuals, 0.95)
    max_residual = max(residuals)
    p95_limit = _largest_integer_cap(
        target_seconds=target_p95_seconds,
        intercept_seconds=center_model.intercept_seconds,
        seconds_per_char=center_model.seconds_per_char,
        residual_seconds=p95_residual,
    )
    max_limit = _largest_integer_cap(
        target_seconds=target_max_seconds,
        intercept_seconds=center_model.intercept_seconds,
        seconds_per_char=center_model.seconds_per_char,
        residual_seconds=max_residual,
    )
    return {
        "residual_seconds": {
            "p50": _nearest_rank(residuals, 0.50),
            "p95": p95_residual,
            "p99": _nearest_rank(residuals, 0.99),
            "observed_max": max_residual,
        },
        "integer_cap_limits_chars_using_aggregate_center_line": {
            "p95_target": p95_limit,
            "observed_max_residual_target": max_limit,
            "combined": min(p95_limit, max_limit),
        },
    }


def _candidate_payload(
    cap: int,
    *,
    observations: Sequence[TtsDurationObservation],
    aggregate_model: DurationModel,
    constraint_models: Sequence[DurationModel],
    envelope_residual_p95: float,
    envelope_residual_max: float,
    target_p95_seconds: float,
    target_max_seconds: float,
) -> dict[str, Any]:
    original_call_count = len(observations)
    minimum_call_count = sum(
        math.ceil(item.text_chars / cap) for item in observations
    )
    additional_calls = minimum_call_count - original_call_count
    fixed_duration_per_extra_call = max(
        0.0,
        aggregate_model.intercept_seconds,
    )
    modeled_extra_seconds = additional_calls * fixed_duration_per_extra_call
    observed_audio_seconds = sum(
        item.audio_duration_seconds for item in observations
    )
    observed_at_or_below = [
        item for item in observations if item.text_chars <= cap
    ]
    observed_duration = [
        item.audio_duration_seconds for item in observed_at_or_below
    ]
    observed_duration_summary = (
        {
            "p95": _nearest_rank(observed_duration, 0.95),
            "max": max(observed_duration),
        }
        if observed_duration
        else {
            "p95": None,
            "max": None,
        }
    )
    p95_envelope = (
        aggregate_model.intercept_seconds
        + aggregate_model.seconds_per_char * cap
        + envelope_residual_p95
    )
    max_envelope = (
        aggregate_model.intercept_seconds
        + aggregate_model.seconds_per_char * cap
        + envelope_residual_max
    )
    fitted_constraints_pass = all(
        model.p95_envelope_seconds(cap) <= target_p95_seconds
        and model.observed_max_residual_envelope_seconds(cap)
        <= target_max_seconds
        for model in constraint_models
    )
    cross_sample_constraints_pass = (
        p95_envelope <= target_p95_seconds
        and max_envelope <= target_max_seconds
    )
    within_observed_character_range = (
        min(item.text_chars for item in observations)
        <= cap
        <= max(item.text_chars for item in observations)
    )
    return {
        "cap_chars": cap,
        "minimum_subsegment_calls": minimum_call_count,
        "minimum_additional_calls": additional_calls,
        "minimum_call_increase_percent": (
            additional_calls / original_call_count * 100.0
        ),
        "ols_intercept_counterfactual": {
            "seconds_per_additional_call": fixed_duration_per_extra_call,
            "modeled_extra_audio_seconds": modeled_extra_seconds,
            "modeled_extra_audio_percent_of_captured_output": (
                modeled_extra_seconds / observed_audio_seconds * 100.0
            ),
        },
        "cross_sample_or_in_sample_envelopes": {
            "p95_seconds": p95_envelope,
            "observed_max_residual_seconds": max_envelope,
            "passes_p95_target": p95_envelope <= target_p95_seconds,
            "passes_observed_max_residual_target": (
                max_envelope <= target_max_seconds
            ),
        },
        "passes_every_aggregate_and_per_sample_fitted_constraint": (
            fitted_constraints_pass
        ),
        "passes_cross_sample_envelope_constraints": (
            cross_sample_constraints_pass
        ),
        "passes_all_fitted_and_cross_sample_constraints": (
            fitted_constraints_pass and cross_sample_constraints_pass
        ),
        "within_observed_character_range": within_observed_character_range,
        "passes_all_selection_constraints": (
            fitted_constraints_pass
            and cross_sample_constraints_pass
            and within_observed_character_range
        ),
        "observed_original_chunks_at_or_below_cap": {
            "observation_count": len(observed_at_or_below),
            "coverage_percent": (
                len(observed_at_or_below) / original_call_count * 100.0
            ),
            "audio_duration_seconds": observed_duration_summary,
            "count_above_p95_target": sum(
                value > target_p95_seconds for value in observed_duration
            ),
            "count_above_max_target": sum(
                value > target_max_seconds for value in observed_duration
            ),
        },
    }


def _structural_sha256(
    observations: Sequence[TtsDurationObservation],
    *,
    schema_version: int | None = None,
) -> str:
    resolved_schema_version = (
        _analysis_schema_version(observations)
        if schema_version is None
        else schema_version
    )
    if resolved_schema_version == 1:
        # This exact record shape produced the published v1 matrix digest.
        # Do not add even default composite fields to this branch.
        records = [
            {
                "sample_index": item.sample_index,
                "sequence_id": item.sequence_id,
                "text_chars": item.text_chars,
                "audio_duration_seconds": item.audio_duration_seconds,
            }
            for item in observations
        ]
    elif resolved_schema_version == 2:
        records = [
            {
                "sample_index": item.sample_index,
                "parent_sequence_id": item.parent_sequence_id,
                "subsequence_id": item.subsequence_id,
                "subsequence_count": item.subsequence_count,
                "text_chars": item.text_chars,
                "audio_duration_seconds": item.audio_duration_seconds,
            }
            for item in observations
        ]
    else:
        raise ValueError("structural digest schema version must be one or two")
    encoded = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _analysis_schema_version(
    observations: Sequence[TtsDurationObservation],
) -> int:
    return (
        2
        if any(item.telemetry_schema_version == 2 for item in observations)
        else 1
    )


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


def _normalize_observation_samples(
    sample_observations: Sequence[Sequence[TtsDurationObservation]],
) -> list[tuple[TtsDurationObservation, ...]]:
    if not sample_observations:
        raise ValueError("at least one non-empty sample is required")
    samples: list[tuple[TtsDurationObservation, ...]] = []
    for expected_sample_index, raw_sample in enumerate(
        sample_observations,
        start=1,
    ):
        sample = tuple(raw_sample)
        if not sample:
            raise ValueError("at least one non-empty sample is required")
        if any(
            not isinstance(item, TtsDurationObservation)
            for item in sample
        ):
            raise ValueError("samples must contain TtsDurationObservation records")
        if any(
            item.sample_index != expected_sample_index for item in sample
        ):
            raise ValueError(
                "observation sample_index must match its neutral sample ordinal"
            )
        sample_versions = {
            item.telemetry_schema_version for item in sample
        }
        if len(sample_versions) != 1:
            raise ValueError(
                "one sample cannot mix telemetry schema versions"
            )
        sample_version = next(iter(sample_versions))
        if sample_version == 1:
            sequence_ids = [item.sequence_id for item in sample]
            if sequence_ids != list(range(len(sample))):
                raise ValueError(
                    "observation sequence IDs must be contiguous from zero"
                )
        else:
            _parent_ids_from_contiguous_composite_keys(
                [item.composite_key for item in sample],
                label=f"sample_{expected_sample_index:02d}",
            )
        samples.append(sample)
    return samples


def build_analysis(
    sample_observations: Sequence[Sequence[TtsDurationObservation]],
    *,
    target_p95_seconds: float = DEFAULT_TARGET_P95_SECONDS,
    target_max_seconds: float = DEFAULT_TARGET_MAX_SECONDS,
    cap_step_chars: int = DEFAULT_CAP_STEP_CHARS,
    max_candidate_chars: int = DEFAULT_MAX_CANDIDATE_CHARS,
    comparison_caps: Sequence[int] = DEFAULT_COMPARISON_CAPS,
) -> dict[str, Any]:
    """Build a deterministic model without copying text or source metadata."""

    samples = _normalize_observation_samples(sample_observations)
    comparison_caps = tuple(dict.fromkeys(comparison_caps))
    _validate_analysis_options(
        target_p95_seconds=target_p95_seconds,
        target_max_seconds=target_max_seconds,
        cap_step_chars=cap_step_chars,
        max_candidate_chars=max_candidate_chars,
        comparison_caps=comparison_caps,
    )

    all_observations = tuple(item for sample in samples for item in sample)
    analysis_schema_version = _analysis_schema_version(all_observations)
    aggregate_model = fit_duration_model(all_observations)
    sample_models = [fit_duration_model(sample) for sample in samples]
    all_models = [aggregate_model, *sample_models]
    cross_validated_residuals = _cross_validated_residuals(samples)
    if cross_validated_residuals:
        envelope_residual_p95 = _nearest_rank(
            cross_validated_residuals,
            0.95,
        )
        envelope_residual_max = max(cross_validated_residuals)
    else:
        envelope_residual_p95 = aggregate_model.residual_p95_seconds
        envelope_residual_max = aggregate_model.residual_max_seconds

    observed_min_chars = min(item.text_chars for item in all_observations)
    observed_max_chars = max(item.text_chars for item in all_observations)

    def cross_sample_envelope_passes(cap: int) -> bool:
        return (
            aggregate_model.intercept_seconds
            + aggregate_model.seconds_per_char * cap
            + envelope_residual_p95
            <= target_p95_seconds
            and aggregate_model.intercept_seconds
            + aggregate_model.seconds_per_char * cap
            + envelope_residual_max
            <= target_max_seconds
        )

    candidate_caps = range(
        cap_step_chars,
        max_candidate_chars + 1,
        cap_step_chars,
    )
    valid_caps = [
        cap
        for cap in candidate_caps
        if observed_min_chars <= cap <= observed_max_chars
        and all(
            model.p95_envelope_seconds(cap) <= target_p95_seconds
            and model.observed_max_residual_envelope_seconds(cap)
            <= target_max_seconds
            for model in all_models
        )
        and cross_sample_envelope_passes(cap)
    ]
    recommended_cap = max(valid_caps) if valid_caps else None
    resolved_comparison_caps = sorted(
        {
            *comparison_caps,
            *([recommended_cap] if recommended_cap is not None else []),
        }
    )

    sample_payloads = []
    for index, (observations, model) in enumerate(
        zip(samples, sample_models),
        start=1,
    ):
        sample_payloads.append(
            {
                "sample": f"sample_{index:02d}",
                "observed_distribution": _distribution(observations),
                "model": _model_payload(
                    model,
                    target_p95_seconds=target_p95_seconds,
                    target_max_seconds=target_max_seconds,
                ),
            }
        )

    recommendation: dict[str, Any] = {
        "tts_subsegment_max_chars": recommended_cap,
        "selection_grid_step_chars": cap_step_chars,
        "selection_max_candidate_chars": max_candidate_chars,
        "selection_observed_character_range": {
            "min": observed_min_chars,
            "max": observed_max_chars,
        },
        "all_aggregate_and_per_sample_constraints_pass": (
            recommended_cap is not None
        ),
        "cross_sample_envelope_constraint_passes": (
            recommended_cap is not None
        ),
        "all_selection_constraints_pass": recommended_cap is not None,
        "live_validation_required": True,
    }
    if recommended_cap is not None:
        envelopes = []
        for label, model in [
            ("aggregate", aggregate_model),
            *[
                (f"sample_{index:02d}", model)
                for index, model in enumerate(sample_models, start=1)
            ],
        ]:
            envelopes.append(
                {
                    "scope": label,
                    "p95_residual_envelope_seconds": (
                        model.p95_envelope_seconds(recommended_cap)
                    ),
                    "observed_max_residual_envelope_seconds": (
                        model.observed_max_residual_envelope_seconds(
                            recommended_cap
                        )
                    ),
                }
            )
        observed_at_or_below = [
            item
            for item in all_observations
            if item.text_chars <= recommended_cap
        ]
        recommendation["fitted_envelopes_at_cap"] = envelopes
        recommendation["observed_original_chunks_at_or_below_cap"] = {
            **_distribution(observed_at_or_below),
            "coverage_percent": (
                len(observed_at_or_below) / len(all_observations) * 100.0
            ),
        }

    semantics = {
        "tts_started_text_chars_are_paired_with_same_sequence_completed_audio_duration": True,
        "nmt_completed_and_tts_started_text_chars_must_match": True,
        "malformed_duplicate_incomplete_or_failed_sequences_are_rejected": True,
        "model_center_line": "ordinary least squares with intercept",
        "p95_envelope": "center line plus nearest-rank p95 residual",
        "max_envelope": "center line plus largest residual observed in this matrix",
        "recommendation_must_pass_aggregate_and_every_sample_model": True,
        "recommendation_must_pass_leave_one_sample_out_envelope_when_available": True,
        "recommendation_must_remain_within_observed_character_range": True,
        "recommendation_is_rounded_down_to_configured_character_grid": True,
        "original_chunks_at_or_below_cap_are_not_a_split_simulation": True,
        "punctuation_and_word_boundary_behavior_is_not_modeled": True,
        "fitted_envelopes_are_experimental_estimates_not_hard_guarantees": True,
        "minimum_call_counts_assume_ideal_character_packing": True,
        "ols_intercept_counterfactual_is_not_a_measured_split_result": True,
    }
    if analysis_schema_version == 2:
        semantics.update(
            {
                "tts_events_are_paired_by_parent_and_subsequence_identity": True,
                "subsequences_must_be_contiguous_with_constant_parent_count": True,
                "tts_parent_text_chars_must_match_nmt_parent_text_chars": True,
                "structural_digest_includes_composite_identity": True,
            }
        )

    analysis = {
        "schema_version": analysis_schema_version,
        "source_format": "completed staged-pipeline summary JSON",
        "privacy": {
            "contains_transcript_text": False,
            "contains_audio": False,
            "contains_input_paths_or_filenames": False,
            "contains_endpoints": False,
            "contains_session_ids": False,
            "sample_labels_are_neutral_ordinals": True,
        },
        "semantics": semantics,
        "targets": {
            "p95_audio_duration_seconds": float(target_p95_seconds),
            "observed_max_residual_envelope_seconds": float(target_max_seconds),
        },
        "structural_records_sha256": _structural_sha256(
            all_observations,
            schema_version=analysis_schema_version,
        ),
        "aggregate": {
            "observed_distribution": _distribution(all_observations),
            "model": _model_payload(
                aggregate_model,
                target_p95_seconds=target_p95_seconds,
                target_max_seconds=target_max_seconds,
            ),
        },
        "leave_one_sample_out": (
            {
                "performed": True,
                "holdout_observation_count": len(
                    cross_validated_residuals
                ),
                **_residual_envelope_payload(
                    aggregate_model,
                    cross_validated_residuals,
                    target_p95_seconds=target_p95_seconds,
                    target_max_seconds=target_max_seconds,
                ),
            }
            if cross_validated_residuals
            else {
                "performed": False,
                "reason": "at least two samples are required",
            }
        ),
        "samples": sample_payloads,
        "capacity_candidates": [
            _candidate_payload(
                cap,
                observations=all_observations,
                aggregate_model=aggregate_model,
                constraint_models=all_models,
                envelope_residual_p95=envelope_residual_p95,
                envelope_residual_max=envelope_residual_max,
                target_p95_seconds=target_p95_seconds,
                target_max_seconds=target_max_seconds,
            )
            for cap in resolved_comparison_caps
        ],
        "recommendation": recommendation,
    }
    return _round_floats(analysis)


def _format_character_policies(caps: Sequence[int]) -> str:
    labels = [f"{cap}-character" for cap in caps]
    if not labels:
        return "no capped"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def render_markdown(analysis: dict[str, Any]) -> str:
    """Render the transcript-free analysis as a concise experiment report."""

    targets = analysis["targets"]
    recommendation = analysis["recommendation"]
    cap = recommendation["tts_subsegment_max_chars"]
    comparison_caps = [
        candidate["cap_chars"]
        for candidate in analysis["capacity_candidates"]
    ]
    if analysis.get("schema_version") == 2:
        pairing_description = (
            "This analysis pairs translated character counts from TTS-start "
            "telemetry with synthesized PCM duration from the matching "
            "parent/subsequence composite identity. It copies no transcript, "
            "audio, filename, endpoint, or session identifier."
        )
    else:
        # Preserve the published v1 Markdown byte-for-byte.
        pairing_description = (
            "This analysis pairs translated character counts from TTS-start "
            "telemetry with synthesized PCM duration from the matching "
            "completed sequence. It copies no transcript, audio, filename, "
            "endpoint, or session identifier."
        )
    lines = [
        "# Privacy-safe TTS duration model",
        "",
        pairing_description,
        "",
    ]
    if cap is None:
        lines.append(
            "No tested character cap satisfied every aggregate and per-sample "
            "fitted envelope. Do not enable post-NMT splitting from this result."
        )
    else:
        lines.append(
            f"Recommended experimental starting cap: **{cap} translated "
            "characters per TTS subsegment**. On the configured character "
            f"grid, every aggregate, per-sample, and available cross-sample "
            f"constraint remains at or below "
            f"the {targets['p95_audio_duration_seconds']:.1f}-second p95 "
            f"target and {targets['observed_max_residual_envelope_seconds']:.1f}-second "
            "observed-max-residual envelope."
        )
    lines.extend(
        [
            "",
            (
                "The cap is a sizing estimate, not a hard duration guarantee. "
                "The live splitter must prefer punctuation, fall back to word "
                "boundaries, preserve parent/subsequence order, and be "
                "validated against newly synthesized audio."
            ),
            "",
            "## Observed distributions and fitted limits",
            "",
            (
                "| Scope | Observations | Character p95 / max | Audio p95 / max "
                "| Seconds/character | R² | P95 cap | Max-envelope cap |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    scopes = [
        ("Aggregate", analysis["aggregate"]),
        *[
            (sample["sample"].replace("_", " ").title(), sample)
            for sample in analysis["samples"]
        ],
    ]
    for label, scope in scopes:
        distribution = scope["observed_distribution"]
        model = scope["model"]
        char_stats = distribution["text_chars"]
        duration_stats = distribution["audio_duration_seconds"]
        limits = model["integer_cap_limits_chars"]
        lines.append(
            "| {label} | {count:,} | {char_p95} / {char_max} | "
            "{audio_p95:.3f}s / {audio_max:.3f}s | {slope:.6f} | "
            "{r2:.3f} | {p95_cap} | {max_cap} |".format(
                label=label,
                count=distribution["observation_count"],
                char_p95=char_stats["p95"],
                char_max=char_stats["max"],
                audio_p95=duration_stats["p95"],
                audio_max=duration_stats["max"],
                slope=model["ols"]["seconds_per_char"],
                r2=model["ols"]["r_squared"],
                p95_cap=limits["p95_target"],
                max_cap=limits["observed_max_residual_target"],
            )
        )

    cross_sample = analysis["leave_one_sample_out"]
    if cross_sample["performed"]:
        residuals = cross_sample["residual_seconds"]
        limits = cross_sample[
            "integer_cap_limits_chars_using_aggregate_center_line"
        ]
        lines.extend(
            [
                "",
                (
                    "Leave-one-sample-out validation produced a nearest-rank "
                    f"p95 residual of {residuals['p95']:.3f}s and an observed "
                    f"maximum residual of {residuals['observed_max']:.3f}s. "
                    "Applied to the aggregate center line, their integer cap "
                    f"limits are {limits['p95_target']} and "
                    f"{limits['observed_max_residual_target']} characters."
                ),
            ]
        )

    if cap is not None:
        lines.extend(
            [
                "",
                f"## Fitted envelopes at {cap} characters",
                "",
                "| Scope | P95-residual envelope | Observed-max-residual envelope |",
                "|---|---:|---:|",
            ]
        )
        for envelope in recommendation["fitted_envelopes_at_cap"]:
            label = envelope["scope"].replace("_", " ").title()
            lines.append(
                "| {label} | {p95:.3f}s | {maximum:.3f}s |".format(
                    label=label,
                    p95=envelope["p95_residual_envelope_seconds"],
                    maximum=envelope[
                        "observed_max_residual_envelope_seconds"
                    ],
                )
            )
        observed = recommendation[
            "observed_original_chunks_at_or_below_cap"
        ]
        duration = observed["audio_duration_seconds"]
        lines.extend(
            [
                "",
                (
                    f"{observed['observation_count']:,} original chunks "
                    f"({observed['coverage_percent']:.1f}%) were already at "
                    f"or below {cap} characters. Their observed audio-duration "
                    f"p95 was {duration['p95']:.3f}s and maximum was "
                    f"{duration['max']:.3f}s. This subset is supporting "
                    "evidence only; it does not emulate splitting longer text."
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Capacity versus added-call tradeoff",
            "",
            (
                "Minimum call counts use ideal character packing; punctuation "
                "and word boundaries can require more calls. The extra-audio "
                "column multiplies additional calls by the aggregate OLS "
                "intercept. It is a counterfactual risk indicator, not a "
                "measured split result."
            ),
            "",
            (
                "| Cap | Minimum calls | Call increase | Residual-envelope p95 / max "
                "| Extra audio estimate | Every selection constraint passes |"
            ),
            "|---:|---:|---:|---:|---:|:---:|",
        ]
    )
    for candidate in analysis["capacity_candidates"]:
        envelope = candidate["cross_sample_or_in_sample_envelopes"]
        counterfactual = candidate["ols_intercept_counterfactual"]
        lines.append(
            "| {cap} | {calls:,} | {increase:.1f}% | {p95:.3f}s / "
            "{maximum:.3f}s | {extra:.1f}% | {passes} |".format(
                cap=candidate["cap_chars"],
                calls=candidate["minimum_subsegment_calls"],
                increase=candidate["minimum_call_increase_percent"],
                p95=envelope["p95_seconds"],
                maximum=envelope["observed_max_residual_seconds"],
                extra=counterfactual[
                    "modeled_extra_audio_percent_of_captured_output"
                ],
                passes=(
                    "yes"
                    if candidate["passes_all_selection_constraints"]
                    else "no"
                ),
            )
        )

    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            (
                "The OLS line captures the strong average relationship between "
                "character count and PCM duration. One-sided empirical residuals "
                "make cap selection conservative across the aggregate and each "
                "neutral sample, but the observed maximum is not a population "
                "bound. Punctuation placement, very short-call behavior, "
                "prosody, pauses, and synthesis overhead can change after "
                "splitting."
            ),
            "",
            (
                "The next live experiment should therefore keep the current "
                "NMT segment intact for translation context, split only its "
                "translated text into ordered atomic TTS subsegments, and "
                "retain exact PCM and terminal accounting. Compare splitting "
                f"disabled with {_format_character_policies(comparison_caps)} "
                "policies on a short replay before selecting a cap. Measure "
                "actual subsegment p95/max, total output expansion, call "
                "count, failures, and same-clock source-event to audible delay "
                "before a full long-form matrix."
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
            "Fit a transcript-free translated-character to TTS-duration model"
        )
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="directory containing completed *_summary.json captures",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument(
        "--target-p95-seconds",
        type=float,
        default=DEFAULT_TARGET_P95_SECONDS,
    )
    parser.add_argument(
        "--target-max-seconds",
        type=float,
        default=DEFAULT_TARGET_MAX_SECONDS,
    )
    parser.add_argument(
        "--cap-step-chars",
        type=int,
        default=DEFAULT_CAP_STEP_CHARS,
    )
    parser.add_argument(
        "--max-candidate-chars",
        type=int,
        default=DEFAULT_MAX_CANDIDATE_CHARS,
    )
    parser.add_argument(
        "--comparison-cap",
        action="append",
        dest="comparison_caps",
        type=int,
        help=(
            "character cap for the call-overhead comparison; repeat for "
            "multiple caps (defaults: 40, 45, 60)"
        ),
    )
    return parser


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    try:
        args.comparison_caps = tuple(
            dict.fromkeys(args.comparison_caps or DEFAULT_COMPARISON_CAPS)
        )
        _validate_analysis_options(
            target_p95_seconds=args.target_p95_seconds,
            target_max_seconds=args.target_max_seconds,
            cap_step_chars=args.cap_step_chars,
            max_candidate_chars=args.max_candidate_chars,
            comparison_caps=args.comparison_caps,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return args


def validate_output_destinations(
    summary_paths: Sequence[Path],
    *,
    json_output: Path,
    markdown_output: Path,
) -> None:
    """Reject output aliases that could overwrite input or each other."""

    resolved_json = json_output.resolve()
    resolved_markdown = markdown_output.resolve()
    if resolved_json == resolved_markdown:
        raise ValueError("JSON and Markdown outputs must be different files")
    resolved_inputs = {path.resolve() for path in summary_paths}
    if resolved_json in resolved_inputs or resolved_markdown in resolved_inputs:
        raise ValueError("an output file cannot overwrite an input summary")


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli_args(argv)
    summary_paths = sorted(args.input_dir.glob("*_summary.json"))
    if not summary_paths:
        raise SystemExit("no *_summary.json files found in input directory")
    json_output = args.json_output or args.input_dir / "tts_duration_model.json"
    markdown_output = (
        args.markdown_output or args.input_dir / "tts_duration_model.md"
    )
    try:
        validate_output_destinations(
            summary_paths,
            json_output=json_output,
            markdown_output=markdown_output,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    samples = [
        load_summary_observations(path, sample_index=index)
        for index, path in enumerate(summary_paths, start=1)
    ]
    analysis = build_analysis(
        samples,
        target_p95_seconds=args.target_p95_seconds,
        target_max_seconds=args.target_max_seconds,
        cap_step_chars=args.cap_step_chars,
        max_candidate_chars=args.max_candidate_chars,
        comparison_caps=args.comparison_caps,
    )
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(analysis, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(
        render_markdown(analysis),
        encoding="utf-8",
    )
    print(f"Wrote {json_output}")
    print(f"Wrote {markdown_output}")


if __name__ == "__main__":
    main()
