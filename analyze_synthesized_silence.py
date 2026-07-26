#!/usr/bin/env python3
"""Aggregate privacy-safe synthesized PCM low-energy observations.

The private batch summaries contain one numeric row per synthesized parent and
threshold.  This analyzer validates those rows through the producer's
authoritative schema validator, then emits only neutral per-sample and
cross-sample aggregates.  It never copies PCM, parent identifiers, paths,
transcript data, endpoints, sessions, source timing, or raw events.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from batch_latency_test import (
    validate_synthesized_pcm_silence_processing,
)
from synthesized_pcm_silence import (
    SynthesizedPcmSilenceError,
    validate_synthesized_pcm_silence_observation,
)


ANALYSIS_SCHEMA_VERSION = 1
ANALYSIS_TYPE = "synthesized_pcm_low_energy_analysis"
PRIMARY_THRESHOLD_DBFS = -50.0
EDGE_DURATION_CUTOFFS_MS = (100, 250, 500)

OUTPUT_PRIVACY = {
    "contains_pcm": False,
    "contains_per_window_energy": False,
    "contains_per_frame_processing_timings": False,
    "contains_transcript_or_translation_text": False,
    "contains_input_paths_or_filenames": False,
    "contains_endpoints_or_uris": False,
    "contains_source_audio_hashes": False,
    "contains_session_identifiers": False,
    "contains_source_timing": False,
    "contains_stream_generation": False,
    "contains_parent_sequence_ids": False,
    "contains_per_parent_rows": False,
    "sample_labels_are_neutral_ordinals": True,
}

CLAIM_BOUNDARY = {
    "windowed_low_energy_measured": True,
    "perceptual_inaudibility_proven": False,
    "speech_absence_proven": False,
    "safe_edge_removal_proven": False,
    "listener_queue_recovery_proven": False,
    "edge_exclusion_is_duration_only_counterfactual": True,
}


@dataclass(frozen=True)
class LowEnergySample:
    """One validated private observation with a neutral public ordinal."""

    sample_index: int
    observation: dict[str, Any]
    processing: dict[str, Any]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise ValueError(f"{field} must be an integer at least {minimum}")
    return value


def _finite(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or float(value) < minimum
    ):
        raise ValueError(f"{field} must be finite and at least {minimum}")
    return float(value)


def _require_complete_capture(root: dict[str, Any]) -> None:
    if root.get("pipeline_mode") != "staged":
        raise ValueError("summary pipeline_mode must be staged")
    backend_config = _object(root.get("backend_config"), "backend_config")
    staged_config = _object(
        backend_config.get("stagedConfig"),
        "backend_config.stagedConfig",
    )
    if (
        staged_config.get("telemetrySchemaVersion") != 3
        or staged_config.get("ttsIncrementalPublishEnabled") is not True
    ):
        raise ValueError(
            "saved backend configuration must prove schema-3 incremental "
            "publication"
        )

    staged = _object(root.get("staged_pipeline"), "staged_pipeline")
    if (
        staged.get("state") != "closed"
        or staged.get("outcome") != "complete"
        or staged.get("failure") is not None
        or _list(
            staged.get("cleanup_errors"),
            "staged_pipeline.cleanup_errors",
        )
    ):
        raise ValueError("staged pipeline did not complete cleanly")

    integrity = _object(root.get("staged_integrity"), "staged_integrity")
    if (
        integrity.get("applicable") is not True
        or integrity.get("passed") is not True
        or _list(integrity.get("errors"), "staged_integrity.errors")
    ):
        raise ValueError("staged integrity did not pass")

    required_flags = {
        "input_completed": True,
        "connection_lost": False,
        "drain_timed_out": False,
        "translation_completed": True,
    }
    if any(root.get(key) is not expected for key, expected in required_flags.items()):
        raise ValueError("capture did not reach a clean completed terminal")
    server_error = root.get("server_error")
    if server_error is not None and server_error != "":
        raise ValueError("capture contains a server error")
    if root.get("synthesized_pcm_silence_requested") is not True:
        raise ValueError(
            "summary does not prove the low-energy diagnostic was requested"
        )


def _require_summary_reconciliation(
    root: dict[str, Any],
    observation: dict[str, Any],
) -> None:
    totals = observation["totals"]
    metadata = _object(
        root.get("audio_metadata_observation"),
        "audio_metadata_observation",
    )
    if metadata.get("protocol_version") != 1:
        raise ValueError("audio metadata protocol version must be one")
    if metadata.get("playback_behavior_changed") is not False:
        raise ValueError("low-energy measurement changed playback behavior")
    if metadata.get("contains_transcript_or_translation_text") is not False:
        raise ValueError("audio metadata privacy declaration is invalid")

    comparisons = (
        (
            _integer(
                metadata.get("stream_generation"),
                "audio_metadata_observation.stream_generation",
                minimum=1,
            ),
            totals["stream_generation"],
            "stream generation",
        ),
        (
            _integer(
                metadata.get("paired_frames"),
                "audio_metadata_observation.paired_frames",
            ),
            totals["frame_count"],
            "paired frame count",
        ),
        (
            _integer(
                metadata.get("completed_parents"),
                "audio_metadata_observation.completed_parents",
            ),
            totals["parent_count"],
            "completed parent count",
        ),
        (
            _integer(root.get("audio_responses"), "audio_responses"),
            totals["frame_count"],
            "translated response count",
        ),
        (
            _integer(
                root.get("total_received_bytes"),
                "total_received_bytes",
            ),
            totals["audio_bytes"],
            "translated byte count",
        ),
    )
    for observed, expected, label in comparisons:
        if observed != expected:
            raise ValueError(f"{label} does not reconcile with observation")

    output_duration = _finite(
        root.get("output_duration_sec"),
        "output_duration_sec",
    )
    expected_duration = totals["sample_count"] / observation["method"][
        "sample_rate_hz"
    ]
    if not math.isclose(
        output_duration,
        expected_duration,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("output duration does not reconcile with observation")


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r} is not allowed")


def load_low_energy_sample(
    path: Path,
    *,
    sample_index: int,
) -> LowEnergySample:
    """Load and fully validate one private batch summary."""

    if (
        not isinstance(sample_index, int)
        or isinstance(sample_index, bool)
        or sample_index < 1
    ):
        raise ValueError("sample_index must be a positive integer")
    root = _object(
        json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=_reject_nonstandard_json_constant,
        ),
        "summary",
    )
    _require_complete_capture(root)
    try:
        observation = validate_synthesized_pcm_silence_observation(
            root.get("synthesized_pcm_silence")
        )
    except SynthesizedPcmSilenceError as exc:
        raise ValueError(
            f"synthesized PCM low-energy observation is invalid: {exc}"
        ) from exc
    _require_summary_reconciliation(root, observation)
    processing = validate_synthesized_pcm_silence_processing(
        root.get("synthesized_pcm_silence_processing"),
        expected_frame_count=observation["totals"]["frame_count"],
    )
    return LowEnergySample(
        sample_index=sample_index,
        observation=observation,
        processing=processing,
    )


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _distribution(values: Iterable[int | float]) -> dict[str, int | float]:
    normalized = tuple(float(value) for value in values)
    if any(not math.isfinite(value) or value < 0 for value in normalized):
        raise ValueError("distribution values must be finite and non-negative")
    if not normalized:
        return {
            "observation_count": 0,
            "min": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "cumulative": 0.0,
        }
    total = sum(normalized)
    return {
        "observation_count": len(normalized),
        "min": min(normalized),
        "p50": _nearest_rank(normalized, 0.50),
        "p95": _nearest_rank(normalized, 0.95),
        "max": max(normalized),
        "mean": total / len(normalized),
        "cumulative": total,
    }


def _percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        raise ValueError("percentage denominator must be positive")
    return numerator / denominator * 100.0


def _duration_ms(sample_count: int, sample_rate_hz: int) -> float:
    return sample_count * 1000.0 / sample_rate_hz


def _threshold_payload(
    rows: Sequence[dict[str, Any]],
    *,
    threshold_dbfs: float,
    sample_rate_hz: int,
    total_sample_count: int,
) -> dict[str, Any]:
    selected = tuple(
        row for row in rows if row["threshold_dbfs"] == threshold_dbfs
    )
    if not selected:
        raise ValueError("threshold has no parent observations")

    active = sum(row["active_sample_count"] for row in selected)
    leading = sum(
        row["leading_low_energy_sample_count"] for row in selected
    )
    internal = sum(
        row["internal_low_energy_sample_count"] for row in selected
    )
    trailing = sum(
        row["trailing_low_energy_sample_count"] for row in selected
    )
    low_energy = sum(
        row["low_energy_sample_count"] for row in selected
    )
    combined_edge = leading + trailing
    if (
        active + low_energy != total_sample_count
        or leading + internal + trailing != low_energy
    ):
        raise ValueError("aggregate threshold partition is inconsistent")

    sample_frame_totals = {
        "active": active,
        "leading_low_energy": leading,
        "trailing_low_energy": trailing,
        "combined_edge_low_energy": combined_edge,
        "internal_low_energy": internal,
        "total_low_energy": low_energy,
    }
    duration_totals = {
        key: _duration_ms(value, sample_rate_hz)
        for key, value in sample_frame_totals.items()
    }
    percentages = {
        key: _percentage(value, total_sample_count)
        for key, value in sample_frame_totals.items()
    }

    edge_cutoff_counts = []
    for cutoff_ms in EDGE_DURATION_CUTOFFS_MS:
        cutoff_samples = math.ceil(
            cutoff_ms * sample_rate_hz / 1000.0
        )
        edge_cutoff_counts.append(
            {
                "minimum_duration_ms": cutoff_ms,
                "leading": sum(
                    row["leading_low_energy_sample_count"]
                    >= cutoff_samples
                    for row in selected
                ),
                "trailing": sum(
                    row["trailing_low_energy_sample_count"]
                    >= cutoff_samples
                    for row in selected
                ),
                "either_single_edge": sum(
                    max(
                        row["leading_low_energy_sample_count"],
                        row["trailing_low_energy_sample_count"],
                    )
                    >= cutoff_samples
                    for row in selected
                ),
                "combined_edges": sum(
                    (
                        row["leading_low_energy_sample_count"]
                        + row["trailing_low_energy_sample_count"]
                    )
                    >= cutoff_samples
                    for row in selected
                ),
            }
        )

    def per_parent_ms(field: str) -> list[float]:
        return [
            _duration_ms(row[field], sample_rate_hz)
            for row in selected
        ]

    combined_edge_ms = [
        _duration_ms(
            row["leading_low_energy_sample_count"]
            + row["trailing_low_energy_sample_count"],
            sample_rate_hz,
        )
        for row in selected
    ]
    per_parent_duration = {
        "active": _distribution(per_parent_ms("active_sample_count")),
        "leading_low_energy": _distribution(
            per_parent_ms("leading_low_energy_sample_count")
        ),
        "trailing_low_energy": _distribution(
            per_parent_ms("trailing_low_energy_sample_count")
        ),
        "combined_edge_low_energy": _distribution(combined_edge_ms),
        "internal_low_energy": _distribution(
            per_parent_ms("internal_low_energy_sample_count")
        ),
        "total_low_energy": _distribution(
            per_parent_ms("low_energy_sample_count")
        ),
        "longest_internal_low_energy_run": _distribution(
            per_parent_ms("longest_internal_low_energy_run_samples")
        ),
    }

    remaining = total_sample_count - combined_edge
    return {
        "threshold_dbfs": threshold_dbfs,
        "is_primary": threshold_dbfs == PRIMARY_THRESHOLD_DBFS,
        "parent_counts": {
            "observed": len(selected),
            "all_low_energy": sum(
                row["all_low_energy"] for row in selected
            ),
            "with_active_energy": sum(
                row["active_sample_count"] > 0 for row in selected
            ),
            "with_leading_low_energy": sum(
                row["leading_low_energy_sample_count"] > 0
                for row in selected
            ),
            "with_trailing_low_energy": sum(
                row["trailing_low_energy_sample_count"] > 0
                for row in selected
            ),
            "with_internal_low_energy": sum(
                row["internal_low_energy_sample_count"] > 0
                for row in selected
            ),
        },
        "sample_frame_totals": sample_frame_totals,
        "duration_ms": duration_totals,
        "percent_of_audio": percentages,
        "edge_exclusion_counterfactual": {
            "excluded_sample_count": combined_edge,
            "excluded_duration_ms": _duration_ms(
                combined_edge,
                sample_rate_hz,
            ),
            "excluded_percent_of_audio": _percentage(
                combined_edge,
                total_sample_count,
            ),
            "remaining_sample_count": remaining,
            "remaining_duration_ms": _duration_ms(
                remaining,
                sample_rate_hz,
            ),
            "remaining_fraction": remaining / total_sample_count,
        },
        "parent_edge_threshold_counts": edge_cutoff_counts,
        "per_parent_duration_ms": per_parent_duration,
        "per_parent_internal_low_energy_run_count": _distribution(
            row["internal_low_energy_run_count"] for row in selected
        ),
    }


def _totals_payload(
    observations: Sequence[dict[str, Any]],
    *,
    sample_rate_hz: int,
) -> dict[str, int | float]:
    keys = (
        "parent_count",
        "frame_count",
        "audio_bytes",
        "sample_count",
        "full_window_count",
        "partial_window_count",
    )
    totals = {
        key: sum(observation["totals"][key] for observation in observations)
        for key in keys
    }
    totals["duration_ms"] = _duration_ms(
        int(totals["sample_count"]),
        sample_rate_hz,
    )
    totals["analysis_window_count"] = (
        int(totals["full_window_count"])
        + int(totals["partial_window_count"])
    )
    return totals


def _summary_payload(
    observations: Sequence[dict[str, Any]],
    *,
    method: dict[str, Any],
) -> dict[str, Any]:
    sample_rate_hz = method["sample_rate_hz"]
    thresholds = tuple(method["thresholds_dbfs"])
    totals = _totals_payload(
        observations,
        sample_rate_hz=sample_rate_hz,
    )
    rows = tuple(
        row
        for observation in observations
        for row in observation["parent_threshold_rows"]
    )
    primary_rows = tuple(
        row
        for row in rows
        if row["threshold_dbfs"] == method["primary_threshold_dbfs"]
    )
    if len(primary_rows) != totals["parent_count"]:
        raise ValueError("primary threshold does not cover every parent")
    return {
        "totals": totals,
        "per_parent_audio_duration_ms": _distribution(
            _duration_ms(row["sample_count"], sample_rate_hz)
            for row in primary_rows
        ),
        "thresholds": [
            _threshold_payload(
                rows,
                threshold_dbfs=threshold,
                sample_rate_hz=sample_rate_hz,
                total_sample_count=int(totals["sample_count"]),
            )
            for threshold in thresholds
        ],
    }


def _public_sample_processing(
    processing: dict[str, Any],
) -> dict[str, Any]:
    return {
        "measurement_position": processing["measurement_position"],
        "clock": processing["clock"],
        "frame_count": processing["frame_count"],
        "total_ms": processing["total_ms"],
        "mean_ms": processing["mean_ms"],
        "p50_ms": processing["p50_ms"],
        "p95_ms": processing["p95_ms"],
        "max_ms": processing["max_ms"],
        "p95_limit_ms": processing["p95_limit_ms"],
        "max_limit_ms": processing["max_limit_ms"],
        "gate_passed": processing["gate_passed"],
    }


def _aggregate_processing(
    samples: Sequence[LowEnergySample],
) -> dict[str, Any]:
    processing = tuple(sample.processing for sample in samples)
    first = processing[0]
    if any(
        item["measurement_position"] != first["measurement_position"]
        or item["clock"] != first["clock"]
        or item["p95_limit_ms"] != first["p95_limit_ms"]
        or item["max_limit_ms"] != first["max_limit_ms"]
        for item in processing
    ):
        raise ValueError("processing measurements do not share one method")
    frame_count = sum(item["frame_count"] for item in processing)
    total_ms = sum(item["total_ms"] for item in processing)
    return {
        "measurement_position": first["measurement_position"],
        "clock": first["clock"],
        "frame_count": frame_count,
        "total_ms": total_ms,
        "weighted_mean_ms": total_ms / frame_count,
        "maximum_ms": max(item["max_ms"] for item in processing),
        "p95_limit_ms": first["p95_limit_ms"],
        "max_limit_ms": first["max_limit_ms"],
        "all_samples_gate_passed": all(
            item["gate_passed"] for item in processing
        ),
        "per_sample_p50_ms": _distribution(
            item["p50_ms"] for item in processing
        ),
        "per_sample_p95_ms": _distribution(
            item["p95_ms"] for item in processing
        ),
        "per_sample_max_ms": _distribution(
            item["max_ms"] for item in processing
        ),
    }


def _round_floats(value: Any) -> Any:
    if isinstance(value, float):
        rounded = round(value, 6)
        return 0.0 if rounded == -0.0 else rounded
    if isinstance(value, list):
        return [_round_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_floats(item) for key, item in value.items()}
    return value


def build_low_energy_analysis(
    samples: Sequence[LowEnergySample],
) -> dict[str, Any]:
    """Build a public aggregate report from validated private samples."""

    normalized = tuple(samples)
    if not normalized:
        raise ValueError("at least one sample is required")
    if any(not isinstance(sample, LowEnergySample) for sample in normalized):
        raise ValueError("samples must contain LowEnergySample records")
    if [sample.sample_index for sample in normalized] != list(
        range(1, len(normalized) + 1)
    ):
        raise ValueError("sample indices must be contiguous from one")

    normalized = tuple(
        LowEnergySample(
            sample_index=sample.sample_index,
            observation=validate_synthesized_pcm_silence_observation(
                sample.observation
            ),
            processing=validate_synthesized_pcm_silence_processing(
                sample.processing,
                expected_frame_count=sample.observation["totals"][
                    "frame_count"
                ],
            ),
        )
        for sample in normalized
    )
    observations = tuple(sample.observation for sample in normalized)
    method = observations[0]["method"]
    if any(observation["method"] != method for observation in observations):
        raise ValueError("all samples must use the same fixed method")
    if method["primary_threshold_dbfs"] != PRIMARY_THRESHOLD_DBFS:
        raise ValueError("fixed primary threshold must be -50 dBFS")

    analysis = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_type": ANALYSIS_TYPE,
        "source_format": (
            "completed batch summary synthesized PCM low-energy observation"
        ),
        "privacy": dict(OUTPUT_PRIVACY),
        "claim_boundary": dict(CLAIM_BOUNDARY),
        "method": copy.deepcopy(method),
        "aggregate": _summary_payload(
            observations,
            method=method,
        ),
        "samples": [
            {
                "sample_index": sample.sample_index,
                **_summary_payload(
                    (sample.observation,),
                    method=method,
                ),
                "processing": _public_sample_processing(
                    sample.processing
                ),
            }
            for sample in normalized
        ],
    }
    analysis["aggregate"]["processing"] = _aggregate_processing(normalized)
    return _round_floats(analysis)


def analyze_paths(paths: Sequence[Path]) -> dict[str, Any]:
    """Analyze unique caller-ordered private summary paths."""

    normalized = tuple(Path(path) for path in paths)
    if not normalized:
        raise ValueError("at least one input summary is required")
    resolved = [path.resolve() for path in normalized]
    if len(resolved) != len(set(resolved)):
        raise ValueError("input summary paths must be unique")
    samples = tuple(
        load_low_energy_sample(path, sample_index=index)
        for index, path in enumerate(normalized, start=1)
    )
    return build_low_energy_analysis(samples)


def _seconds(milliseconds: int | float) -> str:
    return f"{float(milliseconds) / 1000.0:.3f}s"


def render_markdown(analysis: dict[str, Any]) -> str:
    """Render the aggregate report without exposing private source fields."""

    method = analysis["method"]
    aggregate = analysis["aggregate"]
    totals = aggregate["totals"]
    primary = next(
        threshold
        for threshold in aggregate["thresholds"]
        if threshold["is_primary"]
    )
    processing = aggregate["processing"]
    lines = [
        "# Synthesized PCM Low-Energy Analysis",
        "",
        "## Claim boundary",
        "",
        (
            "A window at or below an RMS threshold is classified as "
            "low-energy. This does not prove perceptual inaudibility, absence "
            "of speech, or that removing the window is safe."
        ),
        "",
        (
            "The edge-exclusion values below are duration-only arithmetic "
            "counterfactuals. They do not prove listener-queue recovery."
        ),
        "",
        "## Fixed method",
        "",
        (
            f"- PCM: {method['sample_rate_hz']:,} Hz, "
            f"{method['channels']} channel, "
            f"{method['bytes_per_sample'] * 8}-bit little-endian"
        ),
        (
            f"- Analysis window: {method['window_ms']} ms "
            f"({method['window_samples']} samples), non-overlapping"
        ),
        (
            "- Thresholds: "
            + ", ".join(
                f"{value:.0f} dBFS"
                for value in method["thresholds_dbfs"]
            )
            + f"; primary {method['primary_threshold_dbfs']:.0f} dBFS"
        ),
        "",
        "## Evidence totals",
        "",
        "| Samples | Parents | Transport frames | PCM bytes | Duration |",
        "|---:|---:|---:|---:|---:|",
        (
            f"| {len(analysis['samples'])} | {totals['parent_count']:,} | "
            f"{totals['frame_count']:,} | {totals['audio_bytes']:,} | "
            f"{_seconds(totals['duration_ms'])} |"
        ),
        "",
        "## Inline scan overhead",
        "",
        (
            "The scan ran after arrival and playback scheduling and before "
            "the next receive. Only aggregate durations were retained."
        ),
        "",
        "| Frames | Weighted mean | Worst sample p95 / limit | "
        "Maximum / limit | Gate |",
        "|---:|---:|---:|---:|---|",
        (
            f"| {processing['frame_count']:,} | "
            f"{processing['weighted_mean_ms']:.3f} ms | "
            f"{processing['per_sample_p95_ms']['max']:.3f} / "
            f"{processing['p95_limit_ms']:.3f} ms | "
            f"{processing['maximum_ms']:.3f} / "
            f"{processing['max_limit_ms']:.3f} ms | "
            f"{'PASS' if processing['all_samples_gate_passed'] else 'FAIL'} |"
        ),
        "",
        "## Threshold comparison",
        "",
        (
            "| Threshold | Role | All-low parents | Leading | Trailing | "
            "Combined edges | Edge share | Internal | Remaining | "
            "Edge p50 / p95 / max |"
        ),
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for threshold in aggregate["thresholds"]:
        edge_distribution = threshold["per_parent_duration_ms"][
            "combined_edge_low_energy"
        ]
        remaining_duration_ms = threshold[
            "edge_exclusion_counterfactual"
        ]["remaining_duration_ms"]
        lines.append(
            f"| {threshold['threshold_dbfs']:.0f} dBFS | "
            f"{'primary' if threshold['is_primary'] else 'sensitivity'} | "
            f"{threshold['parent_counts']['all_low_energy']:,} | "
            f"{_seconds(threshold['duration_ms']['leading_low_energy'])} | "
            f"{_seconds(threshold['duration_ms']['trailing_low_energy'])} | "
            f"{_seconds(threshold['duration_ms']['combined_edge_low_energy'])} | "
            f"{threshold['percent_of_audio']['combined_edge_low_energy']:.2f}% | "
            f"{_seconds(threshold['duration_ms']['internal_low_energy'])} | "
            f"{_seconds(remaining_duration_ms)} | "
            f"{_seconds(edge_distribution['p50'])} / "
            f"{_seconds(edge_distribution['p95'])} / "
            f"{_seconds(edge_distribution['max'])} |"
        )

    lines.extend(
        [
            "",
            "## Primary-threshold edge prevalence",
            "",
            (
                "| Minimum edge duration | Leading | Trailing | "
                "Either single edge | Combined edges |"
            ),
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for cutoff in primary["parent_edge_threshold_counts"]:
        lines.append(
            f"| {cutoff['minimum_duration_ms']} ms | "
            f"{cutoff['leading']:,} | {cutoff['trailing']:,} | "
            f"{cutoff['either_single_edge']:,} | "
            f"{cutoff['combined_edges']:,} |"
        )

    lines.extend(
        [
            "",
            "## Neutral samples at the primary threshold",
            "",
            (
                "| Sample | Parents | Audio | Combined edges | Edge share | "
                "Internal | All-low parents | Scan p95 / max |"
            ),
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for sample in analysis["samples"]:
        threshold = next(
            value
            for value in sample["thresholds"]
            if value["is_primary"]
        )
        lines.append(
            f"| {sample['sample_index']:02d} | "
            f"{sample['totals']['parent_count']:,} | "
            f"{_seconds(sample['totals']['duration_ms'])} | "
            f"{_seconds(threshold['duration_ms']['combined_edge_low_energy'])} | "
            f"{threshold['percent_of_audio']['combined_edge_low_energy']:.2f}% | "
            f"{_seconds(threshold['duration_ms']['internal_low_energy'])} | "
            f"{threshold['parent_counts']['all_low_energy']:,} | "
            f"{sample['processing']['p95_ms']:.3f} / "
            f"{sample['processing']['max_ms']:.3f} ms |"
        )
    lines.extend(
        [
            "",
            (
                "Interpret the -50 dBFS row first. The -60 and -40 dBFS rows "
                "are sensitivity bounds, not independent trials."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate synthesized PCM low-energy observations without "
            "retaining generated audio or parent-level rows."
        )
    )
    parser.add_argument(
        "summary_paths",
        nargs="+",
        type=Path,
        help="Completed batch summary JSON paths in neutral sample order.",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    input_paths = {path.resolve() for path in args.summary_paths}
    output_paths = [
        path.resolve()
        for path in (args.json_output, args.markdown_output)
        if path is not None
    ]
    if any(path in input_paths for path in output_paths):
        parser.error("an output path cannot alias input evidence")
    if len(output_paths) != len(set(output_paths)):
        parser.error("JSON and Markdown output paths must be different")
    return args


def _stage_output(path: Path, content: str) -> Path:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
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


def _reserve_backup(path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".bak",
    )
    os.close(descriptor)
    return Path(name)


def _write_outputs_atomically(
    outputs: Sequence[tuple[Path, str]],
) -> None:
    """Stage all bytes and roll back the output set on install failure."""

    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path] = {}
    installed: set[Path] = set()
    succeeded = False
    try:
        for requested, content in outputs:
            destination = requested.resolve()
            if destination.exists() and not destination.is_file():
                raise OSError("report output destination is not a file")
            staged.append(
                (destination, _stage_output(destination, content))
            )
        for destination, _temporary in staged:
            if not destination.exists():
                continue
            backup = _reserve_backup(destination)
            try:
                os.replace(destination, backup)
            except OSError:
                backup.unlink(missing_ok=True)
                raise
            backups[destination] = backup
        for destination, temporary in staged:
            os.replace(temporary, destination)
            installed.add(destination)
        succeeded = True
    except OSError as exc:
        rollback_errors: list[OSError] = []
        for destination, _temporary in reversed(staged):
            backup = backups.get(destination)
            try:
                if backup is not None:
                    os.replace(backup, destination)
                elif destination in installed:
                    destination.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise OSError(
                "report output installation and rollback failed"
            ) from exc
        raise
    finally:
        for _destination, temporary in staged:
            temporary.unlink(missing_ok=True)
        if succeeded:
            for backup in backups.values():
                backup.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_argument_parser()
    args = parse_cli_args(argv)
    try:
        analysis = analyze_paths(args.summary_paths)
        markdown = render_markdown(analysis)
        outputs: list[tuple[Path, str]] = []
        if args.json_output is not None:
            outputs.append(
                (
                    args.json_output,
                    json.dumps(
                        analysis,
                        indent=2,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    + "\n",
                )
            )
        if args.markdown_output is not None:
            outputs.append((args.markdown_output, markdown))
        _write_outputs_atomically(outputs)
    except (OSError, ValueError, SynthesizedPcmSilenceError) as exc:
        parser.error(str(exc))
    if not outputs:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
