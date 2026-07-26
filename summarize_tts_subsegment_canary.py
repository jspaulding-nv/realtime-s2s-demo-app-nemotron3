#!/usr/bin/env python3
"""Build a privacy-safe comparison of four TTS subsegment canary arms."""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


EXPECTED_CAPS = (0, 40, 45, 60)
INPUT_DURATION_MATCH_TOLERANCE_SECONDS = 1e-6
SOURCE_TIME_MATCH_PRECISION_DECIMALS = 3
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
INTENDED_CONFIG_DIFFERENCES = (
    "stagedConfig.telemetrySchemaVersion",
    "stagedConfig.ttsSubsegmentMaxChars",
    "stagedConfig.ttsSubsegmentationEnabled",
)
SENSITIVE_PRIVACY_FLAGS = (
    "contains_transcript_text",
    "contains_audio",
    "contains_input_paths_or_filenames",
    "contains_endpoints",
    "contains_session_ids",
)


@dataclass(frozen=True)
class PromotionGates:
    """Complete, explicit thresholds required for automatic selection."""

    min_child_p95_reduction_percent: float
    min_adaptive_queue_p95_reduction_percent: float
    min_adaptive_listener_tail_reduction_percent: float
    max_child_p95_seconds: float
    max_child_max_seconds: float
    max_output_ratio_increase_percent: float
    max_first_audio_latency_increase_seconds: float
    max_captured_listener_tail_increase_seconds: float
    max_service_tail_lag_increase_seconds: float
    max_adaptive_queue_p95_seconds: float
    max_adaptive_time_above_10_percent: float
    max_adaptive_urgent_source_percent: float
    max_adaptive_urgent_source_increase_percent_points: float
    max_tts_retries: int

    def __post_init__(self) -> None:
        for name in (
            "min_child_p95_reduction_percent",
            "min_adaptive_queue_p95_reduction_percent",
            "min_adaptive_listener_tail_reduction_percent",
        ):
            value = _positive_finite(
                getattr(self, name),
                field=name,
                label="gates",
            )
            if value > 100.0:
                raise ValueError(f"gates: {name} must not exceed 100")
        for name in (
            "max_child_p95_seconds",
            "max_child_max_seconds",
            "max_adaptive_queue_p95_seconds",
        ):
            _positive_finite(getattr(self, name), field=name, label="gates")
        for name in (
            "max_output_ratio_increase_percent",
            "max_first_audio_latency_increase_seconds",
            "max_captured_listener_tail_increase_seconds",
            "max_service_tail_lag_increase_seconds",
            "max_adaptive_time_above_10_percent",
            "max_adaptive_urgent_source_percent",
            "max_adaptive_urgent_source_increase_percent_points",
        ):
            _nonnegative_finite(getattr(self, name), field=name, label="gates")
        _nonnegative_int(
            self.max_tts_retries,
            field="max_tts_retries",
            label="gates",
        )
        if self.max_child_max_seconds < self.max_child_p95_seconds:
            raise ValueError(
                "gates: max_child_max_seconds must be at least "
                "max_child_p95_seconds"
            )


def _nonnegative_int(value: Any, *, field: str, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError(f"{label}: {field} must be a non-negative integer")
    return value


def _positive_int(value: Any, *, field: str, label: str) -> int:
    parsed = _nonnegative_int(value, field=field, label=label)
    if parsed == 0:
        raise ValueError(f"{label}: {field} must be positive")
    return parsed


def _nonnegative_finite(value: Any, *, field: str, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(
            f"{label}: {field} must be non-negative and finite"
        )
    return float(value)


def _positive_finite(value: Any, *, field: str, label: str) -> float:
    parsed = _nonnegative_finite(value, field=field, label=label)
    if parsed == 0:
        raise ValueError(f"{label}: {field} must be positive")
    return parsed


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: required JSON could not be read") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}: JSON root must be an object")
    return value


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot summarize no observations")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _distribution(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50": _nearest_rank(values, 0.50),
        "p95": _nearest_rank(values, 0.95),
        "max": max(values),
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


def _config_provenance(
    backend_config: dict[str, Any],
    *,
    cap: int,
    label: str,
) -> dict[str, Any]:
    """Return exact comparable config with only intended arm fields removed."""

    if backend_config.get("pipelineMode") != "staged":
        raise ValueError(f"{label}: backend pipeline mode must be staged")
    for field in ("sampleRate", "chunkSize", "channels"):
        _positive_int(
            backend_config.get(field),
            field=field,
            label=label,
        )
    model_config = backend_config.get("modelConfig")
    if not isinstance(model_config, dict):
        raise ValueError(f"{label}: modelConfig must be an object")
    for service_name in ("asr", "nmt", "tts"):
        service = model_config.get(service_name)
        if not isinstance(service, dict):
            raise ValueError(
                f"{label}: modelConfig.{service_name} must be an object"
            )
        for field in ("image", "endpoint"):
            value = service.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"{label}: modelConfig.{service_name}.{field} must be "
                    "a non-empty string"
                )
        image_digest = service.get("imageDigest")
        if (
            not isinstance(image_digest, str)
            or IMAGE_DIGEST_PATTERN.fullmatch(image_digest) is None
        ):
            raise ValueError(
                f"{label}: modelConfig.{service_name}.imageDigest must be "
                "an immutable sha256 digest"
            )
    staged_config = backend_config.get("stagedConfig")
    if not isinstance(staged_config, dict):
        raise ValueError(f"{label}: stagedConfig must be an object")

    expected_schema = 1 if cap == 0 else 2
    schema = _positive_int(
        staged_config.get("telemetrySchemaVersion"),
        field="telemetrySchemaVersion",
        label=label,
    )
    if schema != expected_schema:
        raise ValueError(
            f"{label}: backend telemetry schema is incompatible with cap {cap}"
        )
    reported_cap = _nonnegative_int(
        staged_config.get("ttsSubsegmentMaxChars"),
        field="ttsSubsegmentMaxChars",
        label=label,
    )
    if reported_cap != cap:
        raise ValueError(
            f"{label}: reported TTS cap {reported_cap} does not match "
            f"directory cap {cap}"
        )
    if "ttsSubsegmentationEnabled" in staged_config:
        enabled = staged_config["ttsSubsegmentationEnabled"]
        if not isinstance(enabled, bool) or enabled is not (cap > 0):
            raise ValueError(
                f"{label}: backend subsegmentation state is incompatible "
                f"with cap {cap}"
            )
    _positive_int(
        staged_config.get("ttsSubsegmentMinChars"),
        field="ttsSubsegmentMinChars",
        label=label,
    )

    projection = copy.deepcopy(backend_config)
    comparable_staged = projection["stagedConfig"]
    comparable_staged.pop("telemetrySchemaVersion")
    comparable_staged.pop("ttsSubsegmentMaxChars")
    comparable_staged.pop("ttsSubsegmentationEnabled", None)
    return projection


def _optional_source_time(
    value: Any,
    *,
    field: str,
    label: str,
) -> float | None:
    if value is None:
        return None
    parsed = _nonnegative_finite(value, field=field, label=label)
    return round(parsed, SOURCE_TIME_MATCH_PRECISION_DECIMALS)


def _upstream_structure(
    staged: dict[str, Any],
    *,
    parent_calls: int,
    label: str,
) -> dict[str, Any]:
    """Extract privacy-safe evidence that all work before TTS was identical."""

    events = staged.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{label}: staged events must be a list")

    asr_finals: list[tuple[Any, ...]] = []
    segments: list[tuple[Any, ...]] = []
    nmt_parents: list[tuple[int, int]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"{label}: staged event {index} must be an object")
        event_type = (event.get("stage"), event.get("event"))
        event_label = f"{label}: staged event {index}"
        if event_type == ("asr", "final"):
            final_id = _nonnegative_int(
                event.get("asr_final_id"),
                field="asr_final_id",
                label=event_label,
            )
            text_chars = _positive_int(
                event.get("text_chars"),
                field="text_chars",
                label=event_label,
            )
            asr_finals.append(
                (
                    final_id,
                    text_chars,
                    _optional_source_time(
                        event.get("source_start_ms"),
                        field="source_start_ms",
                        label=event_label,
                    ),
                    _optional_source_time(
                        event.get("source_end_ms"),
                        field="source_end_ms",
                        label=event_label,
                    ),
                )
            )
        elif event_type == ("segmenter", "emitted"):
            sequence_id = _nonnegative_int(
                event.get("sequence_id"),
                field="sequence_id",
                label=event_label,
            )
            contributing = event.get("contributing_final_ids")
            if not isinstance(contributing, list) or any(
                not isinstance(final_id, int)
                or isinstance(final_id, bool)
                or final_id < 0
                for final_id in contributing
            ):
                raise ValueError(
                    f"{event_label}: contributing_final_ids must contain "
                    "non-negative integers"
                )
            emission_reason = event.get("emission_reason")
            if not isinstance(emission_reason, str) or not emission_reason:
                raise ValueError(
                    f"{event_label}: emission_reason must be a non-empty string"
                )
            text_chars = _positive_int(
                event.get("text_chars"),
                field="text_chars",
                label=event_label,
            )
            segments.append(
                (
                    sequence_id,
                    tuple(contributing),
                    emission_reason,
                    text_chars,
                    _optional_source_time(
                        event.get("source_start_ms"),
                        field="source_start_ms",
                        label=event_label,
                    ),
                    _optional_source_time(
                        event.get("source_end_ms"),
                        field="source_end_ms",
                        label=event_label,
                    ),
                )
            )
        elif event_type == ("nmt", "completed"):
            nmt_parents.append(
                (
                    _nonnegative_int(
                        event.get("sequence_id"),
                        field="sequence_id",
                        label=event_label,
                    ),
                    _positive_int(
                        event.get("text_chars"),
                        field="text_chars",
                        label=event_label,
                    ),
                )
            )

    expected_parent_ids = list(range(parent_calls))
    if [record[0] for record in segments] != expected_parent_ids:
        raise ValueError(
            f"{label}: segmenter parent order does not match completed parents"
        )
    if [record[0] for record in nmt_parents] != expected_parent_ids:
        raise ValueError(
            f"{label}: NMT parent order does not match completed parents"
        )
    if [record[0] for record in asr_finals] != list(range(len(asr_finals))):
        raise ValueError(f"{label}: ASR final IDs are not contiguous")
    if not asr_finals:
        raise ValueError(f"{label}: no ASR final structure was retained")

    return {
        "asr_finals": asr_finals,
        "segments": segments,
        "nmt_parent_text_chars": nmt_parents,
    }


def _capture_metrics(
    summary: dict[str, Any],
    *,
    cap: int,
    label: str,
) -> dict[str, Any]:
    integrity = summary.get("staged_integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("applicable") is not True
        or integrity.get("passed") is not True
        or integrity.get("errors") != []
    ):
        raise ValueError(f"{label}: staged integrity did not pass cleanly")

    backend_config = summary.get("backend_config")
    if not isinstance(backend_config, dict):
        raise ValueError(f"{label}: backend_config must be an object")
    config_provenance = _config_provenance(
        backend_config,
        cap=cap,
        label=label,
    )

    staged = summary.get("staged_pipeline")
    if not isinstance(staged, dict):
        raise ValueError(f"{label}: staged_pipeline must be an object")
    if (
        staged.get("state") != "closed"
        or staged.get("outcome") != "complete"
        or staged.get("failure") is not None
        or staged.get("cleanup_errors") != []
        or staged.get("incomplete_sequence_ids") != []
    ):
        raise ValueError(f"{label}: staged pipeline did not close cleanly")

    expected_schema = 1 if cap == 0 else 2
    schema = staged.get("telemetry_schema_version", 1)
    if schema != expected_schema:
        raise ValueError(
            f"{label}: telemetry schema is incompatible with cap {cap}"
        )
    enabled = staged.get("tts_subsegmentation_enabled", cap > 0)
    if enabled is not (cap > 0):
        raise ValueError(
            f"{label}: subsegmentation state is incompatible with cap {cap}"
        )
    if cap > 0:
        pipeline_cap = _positive_int(
            staged.get("tts_subsegment_max_chars"),
            field="tts_subsegment_max_chars",
            label=label,
        )
        if pipeline_cap != cap:
            raise ValueError(
                f"{label}: pipeline TTS cap does not match directory cap"
            )

    parent_calls = _positive_int(
        staged.get("segments_emitted"),
        field="segments_emitted",
        label=label,
    )
    audio_segments = _positive_int(
        staged.get("audio_segments_produced"),
        field="audio_segments_produced",
        label=label,
    )
    child_calls = (
        _positive_int(
            staged.get("tts_subsegments_produced"),
            field="tts_subsegments_produced",
            label=label,
        )
        if cap > 0
        else audio_segments
    )
    if audio_segments != child_calls:
        raise ValueError(
            f"{label}: audio segment count does not match TTS child calls"
        )
    completed = staged.get("completed_sequence_ids")
    if not isinstance(completed, list) or completed != list(
        range(parent_calls)
    ):
        raise ValueError(
            f"{label}: completed parent IDs are not contiguous and complete"
        )
    upstream_structure = _upstream_structure(
        staged,
        parent_calls=parent_calls,
        label=label,
    )

    audio_responses = _positive_int(
        summary.get("audio_responses"),
        field="audio_responses",
        label=label,
    )
    if audio_responses != child_calls:
        raise ValueError(
            f"{label}: received PCM frame count does not match TTS child calls"
        )

    input_seconds = _positive_finite(
        summary.get("input_duration_sec"),
        field="input_duration_sec",
        label=label,
    )
    output_seconds = _positive_finite(
        summary.get("output_duration_sec"),
        field="output_duration_sec",
        label=label,
    )
    ratio = _positive_finite(
        summary.get("output_to_input_duration_ratio"),
        field="output_to_input_duration_ratio",
        label=label,
    )
    expected_ratio = output_seconds / input_seconds
    if not math.isclose(ratio, expected_ratio, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError(f"{label}: output/input duration ratio is inconsistent")
    if summary.get("translation_completed") is not True:
        raise ValueError(f"{label}: translation did not complete")
    if summary.get("server_error") not in {"", None}:
        raise ValueError(f"{label}: capture retained a server error")
    if summary.get("input_completed") is not True:
        raise ValueError(f"{label}: input did not complete")
    if summary.get("connection_lost") is not False:
        raise ValueError(f"{label}: capture lost its connection")
    if summary.get("drain_timed_out") is not False:
        raise ValueError(f"{label}: capture drain timed out")
    input_reference = summary.get("audio_path")
    if not isinstance(input_reference, str) or not input_reference:
        raise ValueError(f"{label}: input reference must be a non-empty string")

    return {
        "parent_calls": parent_calls,
        "child_calls": child_calls,
        "input_duration_seconds": input_seconds,
        "output_duration_seconds": output_seconds,
        "output_to_input_duration_ratio": ratio,
        "first_audio_latency_seconds": _nonnegative_finite(
            summary.get("first_audio_latency_sec"),
            field="first_audio_latency_sec",
            label=label,
        ),
        "captured_listener_tail_seconds": _nonnegative_finite(
            summary.get("playback_tail_sec"),
            field="playback_tail_sec",
            label=label,
        ),
        "service_tail_lag_seconds": _nonnegative_finite(
            summary.get("tail_lag_sec"),
            field="tail_lag_sec",
            label=label,
        ),
        "tts_retry_count": _nonnegative_int(
            staged.get("tts_retry_count"),
            field="tts_retry_count",
            label=label,
        ),
        "_match_evidence": {
            "input_reference": input_reference,
            "chunks_sent": _positive_int(
                summary.get("chunks_sent"),
                field="chunks_sent",
                label=label,
            ),
            "backend_config": config_provenance,
            "fillers_discarded": _nonnegative_int(
                staged.get("fillers_discarded"),
                field="fillers_discarded",
                label=label,
            ),
            **upstream_structure,
        },
    }


def _duration_model_metrics(
    model: dict[str, Any],
    *,
    cap: int,
    capture_count: int,
    child_calls: int,
    label: str,
) -> dict[str, Any]:
    expected_schema = 1 if cap == 0 else 2
    if model.get("schema_version") != expected_schema:
        raise ValueError(
            f"{label}: duration-model schema is incompatible with cap {cap}"
        )
    privacy = model.get("privacy")
    if not isinstance(privacy, dict) or any(
        privacy.get(field) is not False for field in SENSITIVE_PRIVACY_FLAGS
    ):
        raise ValueError(f"{label}: duration model is not privacy-safe")
    samples = model.get("samples")
    if not isinstance(samples, list) or len(samples) != capture_count:
        raise ValueError(
            f"{label}: duration-model sample count does not match captures"
        )
    aggregate = model.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ValueError(f"{label}: duration-model aggregate is missing")
    observed = aggregate.get("observed_distribution")
    if not isinstance(observed, dict):
        raise ValueError(
            f"{label}: duration-model observed distribution is missing"
        )
    if _positive_int(
        observed.get("observation_count"),
        field="observation_count",
        label=label,
    ) != child_calls:
        raise ValueError(
            f"{label}: duration observations do not match TTS child calls"
        )
    durations = observed.get("audio_duration_seconds")
    if not isinstance(durations, dict):
        raise ValueError(f"{label}: child duration distribution is missing")
    parsed = {
        key: _positive_finite(
            durations.get(key),
            field=f"audio_duration_seconds.{key}",
            label=label,
        )
        for key in ("p50", "p95", "max")
    }
    if not parsed["p50"] <= parsed["p95"] <= parsed["max"]:
        raise ValueError(f"{label}: child duration quantiles are inconsistent")

    targets = model.get("targets")
    target_payload = None
    if isinstance(targets, dict):
        target_payload = {
            "p95_audio_duration_seconds": _positive_finite(
                targets.get("p95_audio_duration_seconds"),
                field="p95_audio_duration_seconds",
                label=label,
            ),
            "observed_max_residual_envelope_seconds": _positive_finite(
                targets.get("observed_max_residual_envelope_seconds"),
                field="observed_max_residual_envelope_seconds",
                label=label,
            ),
        }
    return {
        "actual_child_duration_seconds": parsed,
        "model_targets_seconds": target_payload,
    }


def _optional_metric(
    value: dict[str, Any],
    key: str,
    *,
    label: str,
) -> float | None:
    if key not in value:
        return None
    return _nonnegative_finite(value[key], field=key, label=label)


def _playback_mode_metrics(
    value: Any,
    *,
    label: str,
    limit_is_10_seconds: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: playback mode must be an object")
    result: dict[str, Any] = {}
    for key in (
        "listener_tail_seconds",
        "time_weighted_queue_p50_seconds",
        "time_weighted_queue_p95_seconds",
        "peak_queue_depth_seconds",
        "accelerated_source_percent",
        "urgent_source_percent",
        "max_continuous_urgent_playback_seconds",
    ):
        metric = _optional_metric(value, key, label=label)
        if metric is not None:
            result[key] = metric
    if limit_is_10_seconds:
        for source_key, output_key in (
            ("seconds_above_limit", "seconds_above_10_seconds"),
            (
                "percent_playback_window_above_limit",
                "percent_playback_window_above_10_seconds",
            ),
        ):
            metric = _optional_metric(value, source_key, label=label)
            if metric is not None:
                result[output_key] = metric

    result["chunks_scheduled"] = _positive_int(
        value.get("chunks_scheduled"),
        field="chunks_scheduled",
        label=label,
    )
    result["chunks_dropped"] = _nonnegative_int(
        value.get("chunks_dropped"),
        field="chunks_dropped",
        label=label,
    )
    rate_exposure = value.get("rate_source_duration_seconds")
    if isinstance(rate_exposure, dict):
        result["rate_source_duration_seconds"] = {
            str(rate): _nonnegative_finite(
                seconds,
                field=f"rate_source_duration_seconds.{rate}",
                label=label,
            )
            for rate, seconds in sorted(rate_exposure.items())
        }
    return result


def _playback_metrics(
    analysis: dict[str, Any],
    *,
    summaries: Sequence[Path],
    captures: Sequence[dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    if analysis.get("schema_version") not in {1, 2}:
        raise ValueError(f"{label}: unsupported playback analysis schema")
    traces = analysis.get("traces")
    if not isinstance(traces, list) or len(traces) != len(captures):
        raise ValueError(
            f"{label}: playback trace count does not match captures"
        )
    policy = analysis.get("policy")
    if not isinstance(policy, dict):
        raise ValueError(f"{label}: playback policy is missing")
    limit = _positive_finite(
        policy.get("limit_queue_seconds"),
        field="limit_queue_seconds",
        label=label,
    )
    limit_is_10_seconds = math.isclose(limit, 10.0, abs_tol=1e-9)

    samples = []
    for index, (trace, summary_path, capture) in enumerate(
        zip(traces, summaries, captures),
        start=1,
    ):
        if not isinstance(trace, dict):
            raise ValueError(f"{label}: playback trace must be an object")
        expected_trace_name = (
            summary_path.name.removesuffix("_summary.json")
            + "_results.csv"
        )
        if trace.get("trace_csv") != expected_trace_name:
            raise ValueError(
                f"{label}: playback trace order does not match captures"
            )
        translated_audio = _positive_finite(
            trace.get("translated_audio_seconds"),
            field="translated_audio_seconds",
            label=label,
        )
        if not math.isclose(
            translated_audio,
            capture["output_duration_seconds"],
            rel_tol=1e-6,
            abs_tol=0.005,
        ):
            raise ValueError(
                f"{label}: playback audio duration does not match capture"
            )
        fixed = _playback_mode_metrics(
            trace.get("fixed_1x"),
            label=label,
            limit_is_10_seconds=limit_is_10_seconds,
        )
        adaptive = _playback_mode_metrics(
            trace.get("adaptive"),
            label=label,
            limit_is_10_seconds=limit_is_10_seconds,
        )
        if (
            fixed["chunks_scheduled"] != capture["child_calls"]
            or adaptive["chunks_scheduled"] != capture["child_calls"]
        ):
            raise ValueError(
                f"{label}: playback chunks do not match TTS child calls"
            )
        samples.append(
            {
                "sample": f"sample_{index:02d}",
                "fixed_1x": fixed,
                "adaptive": adaptive,
            }
        )

    worst_case: dict[str, Any] = {}
    for mode in ("fixed_1x", "adaptive"):
        mode_result: dict[str, Any] = {}
        keys = sorted(
            {
                key
                for sample in samples
                for key, value in sample[mode].items()
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and key not in {"chunks_scheduled", "chunks_dropped"}
            }
        )
        for key in keys:
            observed = [
                sample[mode][key]
                for sample in samples
                if key in sample[mode]
            ]
            if len(observed) == len(samples):
                mode_result[key] = max(observed)
        mode_result["chunks_dropped"] = sum(
            sample[mode]["chunks_dropped"] for sample in samples
        )
        worst_case[mode] = mode_result

    return {
        "queue_limit_seconds": limit,
        "samples": samples,
        "worst_case": worst_case,
        "_policy_provenance": copy.deepcopy(policy),
    }


def _load_arm(
    arm_dir: Path,
    *,
    cap: int,
    arm_index: int,
) -> dict[str, Any]:
    arm_label = f"arm_{arm_index:02d}"
    summary_paths = sorted(arm_dir.glob("*_summary.json"))
    if not summary_paths:
        raise ValueError(f"{arm_label}: no capture summaries were found")
    captures = [
        _capture_metrics(
            _load_json(path, label=f"{arm_label}/sample_{index:02d}"),
            cap=cap,
            label=f"{arm_label}/sample_{index:02d}",
        )
        for index, path in enumerate(summary_paths, start=1)
    ]
    parent_calls = sum(item["parent_calls"] for item in captures)
    child_calls = sum(item["child_calls"] for item in captures)
    input_seconds = sum(item["input_duration_seconds"] for item in captures)
    output_seconds = sum(item["output_duration_seconds"] for item in captures)

    duration_metrics = _duration_model_metrics(
        _load_json(
            arm_dir / "tts_duration_model.json",
            label=f"{arm_label}/duration_model",
        ),
        cap=cap,
        capture_count=len(captures),
        child_calls=child_calls,
        label=f"{arm_label}/duration_model",
    )
    playback = _playback_metrics(
        _load_json(
            arm_dir / "playback_policy_analysis.json",
            label=f"{arm_label}/playback",
        ),
        summaries=summary_paths,
        captures=captures,
        label=f"{arm_label}/playback",
    )
    playback_policy_provenance = playback.pop("_policy_provenance")

    return {
        "arm": arm_label,
        "configured_cap_chars": cap,
        "capture_count": len(captures),
        "parent_translation_calls": parent_calls,
        "tts_child_calls": child_calls,
        "child_calls_per_parent": child_calls / parent_calls,
        **duration_metrics,
        "input_duration_seconds": input_seconds,
        "output_duration_seconds": output_seconds,
        "output_to_input_duration_ratio": output_seconds / input_seconds,
        "first_audio_latency_seconds": _distribution(
            [item["first_audio_latency_seconds"] for item in captures]
        ),
        "captured_listener_tail_seconds": _distribution(
            [item["captured_listener_tail_seconds"] for item in captures]
        ),
        "service_tail_lag_seconds": _distribution(
            [item["service_tail_lag_seconds"] for item in captures]
        ),
        "tts_retry_count": sum(
            item["tts_retry_count"] for item in captures
        ),
        "playback": playback,
        "_match_evidence": {
            "capture_names": [path.name for path in summary_paths],
            "captures": [
                {
                    "input_duration_seconds": capture[
                        "input_duration_seconds"
                    ],
                    "parent_calls": capture["parent_calls"],
                    **capture["_match_evidence"],
                }
                for capture in captures
            ],
            "playback_policy": playback_policy_provenance,
        },
    }


def _validate_matched_arms(
    arms: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Fail closed unless every arm used the same privacy-safe workload."""

    if not arms:
        raise ValueError("no canary arms were loaded")
    control = arms[0]["_match_evidence"]
    expected_capture_count = len(control["captures"])

    for arm in arms[1:]:
        label = arm["arm"]
        evidence = arm["_match_evidence"]
        if len(evidence["captures"]) != expected_capture_count:
            raise ValueError(
                f"{label}: capture count does not match the control arm"
            )
        if evidence["capture_names"] != control["capture_names"]:
            raise ValueError(
                f"{label}: corresponding capture order does not match "
                "the control arm"
            )
        if evidence["playback_policy"] != control["playback_policy"]:
            raise ValueError(
                f"{label}: playback-analysis policy provenance does not "
                "match the control arm"
            )

        for index, (candidate, baseline) in enumerate(
            zip(evidence["captures"], control["captures"]),
            start=1,
        ):
            sample_label = f"{label}/sample_{index:02d}"
            if candidate["input_reference"] != baseline["input_reference"]:
                raise ValueError(
                    f"{sample_label}: input reference does not match control"
                )
            if candidate["chunks_sent"] != baseline["chunks_sent"]:
                raise ValueError(
                    f"{sample_label}: input chunk count does not match control"
                )
            if not math.isclose(
                candidate["input_duration_seconds"],
                baseline["input_duration_seconds"],
                rel_tol=0.0,
                abs_tol=INPUT_DURATION_MATCH_TOLERANCE_SECONDS,
            ):
                raise ValueError(
                    f"{sample_label}: input duration does not match control"
                )
            if candidate["backend_config"] != baseline["backend_config"]:
                raise ValueError(
                    f"{sample_label}: backend configuration provenance does "
                    "not match control outside intended arm differences"
                )
            if candidate["parent_calls"] != baseline["parent_calls"]:
                raise ValueError(
                    f"{sample_label}: parent count does not match control"
                )
            if (
                candidate["fillers_discarded"]
                != baseline["fillers_discarded"]
            ):
                raise ValueError(
                    f"{sample_label}: upstream filler handling does not "
                    "match control"
                )
            for key, description in (
                ("asr_finals", "ASR-final structure"),
                ("segments", "parent segmentation"),
                ("nmt_parent_text_chars", "NMT parent structure"),
            ):
                if candidate[key] != baseline[key]:
                    raise ValueError(
                        f"{sample_label}: {description} does not match control"
                    )

    return {
        "passed": True,
        "control_arm": arms[0]["arm"],
        "capture_count_per_arm": expected_capture_count,
        "corresponding_capture_order_matched": True,
        "input_references_matched": True,
        "input_chunk_counts_matched": True,
        "input_durations_matched": True,
        "input_duration_tolerance_seconds": (
            INPUT_DURATION_MATCH_TOLERANCE_SECONDS
        ),
        "backend_config_provenance_matched": True,
        "playback_policy_provenance_matched": True,
        "upstream_asr_structure_matched": True,
        "parent_counts_and_segmentation_matched": True,
        "nmt_parent_structure_matched": True,
        "intended_backend_config_differences": list(
            INTENDED_CONFIG_DIFFERENCES
        ),
    }


def _percent_reduction(
    control: float | None,
    candidate: float | None,
) -> float | None:
    if control is None or candidate is None or control <= 0:
        return None
    return (control - candidate) / control * 100.0


def _promotion_recommendation(
    arms: Sequence[dict[str, Any]],
    gates: PromotionGates | None,
) -> dict[str, Any]:
    if gates is None:
        return {
            "explicit_gates_configured": False,
            "eligible_arms": [],
            "recommended_arm": None,
            "status": "no_automatic_winner_without_explicit_gates",
            "native_language_review_required": True,
        }

    control = next(
        arm for arm in arms if arm["configured_cap_chars"] == 0
    )
    control_adaptive = control["playback"]["worst_case"]["adaptive"]
    evaluations = []
    eligible = []
    for arm in arms:
        if arm["configured_cap_chars"] == 0:
            continue
        adaptive = arm["playback"]["worst_case"]["adaptive"]
        ratio_increase = (
            arm["output_to_input_duration_ratio"]
            / control["output_to_input_duration_ratio"]
            - 1.0
        ) * 100.0
        child_p95 = arm["actual_child_duration_seconds"]["p95"]
        adaptive_queue_p95 = adaptive.get(
            "time_weighted_queue_p95_seconds"
        )
        adaptive_listener_tail = adaptive.get("listener_tail_seconds")
        adaptive_urgent = adaptive.get("urgent_source_percent")
        control_urgent = control_adaptive.get("urgent_source_percent")
        metrics = {
            "child_p95_seconds": child_p95,
            "child_max_seconds": arm[
                "actual_child_duration_seconds"
            ]["max"],
            "child_p95_reduction_percent_vs_control": _percent_reduction(
                control["actual_child_duration_seconds"]["p95"],
                child_p95,
            ),
            "output_ratio_increase_percent_vs_control": ratio_increase,
            "first_audio_p95_increase_seconds_vs_control": (
                arm["first_audio_latency_seconds"]["p95"]
                - control["first_audio_latency_seconds"]["p95"]
            ),
            "captured_listener_tail_p95_increase_seconds_vs_control": (
                arm["captured_listener_tail_seconds"]["p95"]
                - control["captured_listener_tail_seconds"]["p95"]
            ),
            "service_tail_lag_p95_increase_seconds_vs_control": (
                arm["service_tail_lag_seconds"]["p95"]
                - control["service_tail_lag_seconds"]["p95"]
            ),
            "adaptive_queue_p95_seconds": adaptive_queue_p95,
            "adaptive_queue_p95_reduction_percent_vs_control": (
                _percent_reduction(
                    control_adaptive.get(
                        "time_weighted_queue_p95_seconds"
                    ),
                    adaptive_queue_p95,
                )
            ),
            "adaptive_listener_tail_reduction_percent_vs_control": (
                _percent_reduction(
                    control_adaptive.get("listener_tail_seconds"),
                    adaptive_listener_tail,
                )
            ),
            "adaptive_time_above_10_percent": adaptive.get(
                "percent_playback_window_above_10_seconds"
            ),
            "adaptive_urgent_source_percent": adaptive_urgent,
            "adaptive_urgent_source_increase_percent_points_vs_control": (
                None
                if adaptive_urgent is None or control_urgent is None
                else adaptive_urgent - control_urgent
            ),
            "tts_retry_count": arm["tts_retry_count"],
        }
        missing = [key for key, value in metrics.items() if value is None]
        gate_results = {
            "child_p95_relative_benefit": metrics[
                "child_p95_reduction_percent_vs_control"
            ]
            is not None
            and metrics["child_p95_reduction_percent_vs_control"]
            >= gates.min_child_p95_reduction_percent,
            "child_p95": metrics["child_p95_seconds"]
            <= gates.max_child_p95_seconds,
            "child_max": metrics["child_max_seconds"]
            <= gates.max_child_max_seconds,
            "output_ratio": metrics[
                "output_ratio_increase_percent_vs_control"
            ]
            <= gates.max_output_ratio_increase_percent,
            "first_audio_latency_nonregression": metrics[
                "first_audio_p95_increase_seconds_vs_control"
            ]
            <= gates.max_first_audio_latency_increase_seconds,
            "captured_listener_tail_nonregression": metrics[
                "captured_listener_tail_p95_increase_seconds_vs_control"
            ]
            <= gates.max_captured_listener_tail_increase_seconds,
            "service_tail_lag_nonregression": metrics[
                "service_tail_lag_p95_increase_seconds_vs_control"
            ]
            <= gates.max_service_tail_lag_increase_seconds,
            "adaptive_queue_p95": (
                metrics["adaptive_queue_p95_seconds"] is not None
                and metrics["adaptive_queue_p95_seconds"]
                <= gates.max_adaptive_queue_p95_seconds
            ),
            "adaptive_queue_relative_benefit": metrics[
                "adaptive_queue_p95_reduction_percent_vs_control"
            ]
            is not None
            and metrics["adaptive_queue_p95_reduction_percent_vs_control"]
            >= gates.min_adaptive_queue_p95_reduction_percent,
            "adaptive_tail_relative_benefit": metrics[
                "adaptive_listener_tail_reduction_percent_vs_control"
            ]
            is not None
            and metrics[
                "adaptive_listener_tail_reduction_percent_vs_control"
            ]
            >= gates.min_adaptive_listener_tail_reduction_percent,
            "adaptive_time_above_10": (
                metrics["adaptive_time_above_10_percent"] is not None
                and metrics["adaptive_time_above_10_percent"]
                <= gates.max_adaptive_time_above_10_percent
            ),
            "adaptive_urgent_exposure": (
                metrics["adaptive_urgent_source_percent"] is not None
                and metrics["adaptive_urgent_source_percent"]
                <= gates.max_adaptive_urgent_source_percent
            ),
            "adaptive_urgent_exposure_nonregression": metrics[
                "adaptive_urgent_source_increase_percent_points_vs_control"
            ]
            is not None
            and metrics[
                "adaptive_urgent_source_increase_percent_points_vs_control"
            ]
            <= gates.max_adaptive_urgent_source_increase_percent_points,
            "tts_retries": metrics["tts_retry_count"]
            <= gates.max_tts_retries,
        }
        passed = not missing and all(gate_results.values())
        evaluation = {
            "arm": arm["arm"],
            "configured_cap_chars": arm["configured_cap_chars"],
            "metrics": metrics,
            "gates": gate_results,
            "missing_required_metrics": missing,
            "pass": passed,
        }
        evaluations.append(evaluation)
        if passed:
            eligible.append(arm)

    ranked = sorted(
        eligible,
        key=lambda arm: (
            arm["playback"]["worst_case"]["adaptive"][
                "time_weighted_queue_p95_seconds"
            ],
            arm["output_to_input_duration_ratio"],
            arm["actual_child_duration_seconds"]["p95"],
            arm["configured_cap_chars"],
        ),
    )
    recommended = ranked[0]["arm"] if ranked else None
    return {
        "explicit_gates_configured": True,
        "thresholds": asdict(gates),
        "evaluations": evaluations,
        "eligible_arms": [arm["arm"] for arm in ranked],
        "recommended_arm": recommended,
        "status": (
            "explicit_gates_selected_an_arm"
            if recommended is not None
            else "no_arm_passed_explicit_gates"
        ),
        "selection_rule": (
            "lowest worst-case adaptive queue p95, then output/input ratio, "
            "child-duration p95, and configured cap"
        ),
        "native_language_review_required": True,
    }


def build_canary_summary(
    input_dir: Path,
    *,
    promotion_gates: PromotionGates | None = None,
) -> dict[str, Any]:
    """Load, validate, and compare the four expected canary arms."""

    arms = []
    for index, cap in enumerate(EXPECTED_CAPS, start=1):
        arm_dir = input_dir / f"cap-{cap}"
        if not arm_dir.is_dir():
            raise ValueError(f"arm_{index:02d}: required arm directory is missing")
        arms.append(_load_arm(arm_dir, cap=cap, arm_index=index))

    matched_design = _validate_matched_arms(arms)
    for arm in arms:
        arm.pop("_match_evidence")

    result = {
        "schema_version": 2,
        "source_format": (
            "validated staged summaries plus privacy-safe duration and "
            "playback analyses"
        ),
        "privacy": {
            "contains_transcript_text": False,
            "contains_audio": False,
            "contains_input_paths_or_filenames": False,
            "contains_endpoints": False,
            "contains_session_ids": False,
            "arm_and_sample_labels_are_neutral_ordinals": True,
        },
        "matched_design": matched_design,
        "arms": arms,
        "recommendation": _promotion_recommendation(
            arms,
            promotion_gates,
        ),
    }
    return _round_floats(result)


def render_markdown(summary: dict[str, Any]) -> str:
    """Render the privacy-safe comparison without source identifiers."""

    lines = [
        "# TTS subsegment canary comparison",
        "",
        (
            "All arms passed staged integrity and the matched-design gate: "
            "capture order, input duration/chunks, backend provenance, "
            "playback policy, ASR structure, parent segmentation, and NMT "
            "parent structure matched the control. Labels are neutral; no "
            "transcript, audio, path, endpoint, or session identifier is "
            "included."
        ),
        "",
        (
            "| Arm | Cap | Parent / child calls | Child duration p50 / p95 / "
            "max | Output/input | First audio p95 | Captured tail p95 | "
            "Adaptive queue p95 (worst) | Adaptive tail (worst) |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in summary["arms"]:
        duration = arm["actual_child_duration_seconds"]
        adaptive = arm["playback"]["worst_case"]["adaptive"]
        lines.append(
            "| {arm} | {cap} | {parents:,} / {children:,} | "
            "{p50:.3f}s / {p95:.3f}s / {maximum:.3f}s | {ratio:.4f} | "
            "{first:.3f}s | {tail:.3f}s | {queue} | {adaptive_tail} |".format(
                arm=arm["arm"],
                cap=arm["configured_cap_chars"],
                parents=arm["parent_translation_calls"],
                children=arm["tts_child_calls"],
                p50=duration["p50"],
                p95=duration["p95"],
                maximum=duration["max"],
                ratio=arm["output_to_input_duration_ratio"],
                first=arm["first_audio_latency_seconds"]["p95"],
                tail=arm["captured_listener_tail_seconds"]["p95"],
                queue=_format_seconds(
                    adaptive.get("time_weighted_queue_p95_seconds")
                ),
                adaptive_tail=_format_seconds(
                    adaptive.get("listener_tail_seconds")
                ),
            )
        )

    lines.extend(["", "## Playback exposure", ""])
    lines.append(
        "| Arm | Fixed queue p95 / tail | Adaptive queue p95 / tail | "
        "Adaptive time >10s | Adaptive urgent-source exposure | TTS retries |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|")
    for arm in summary["arms"]:
        fixed = arm["playback"]["worst_case"]["fixed_1x"]
        adaptive = arm["playback"]["worst_case"]["adaptive"]
        lines.append(
            "| {arm} | {fixed_queue} / {fixed_tail} | "
            "{adaptive_queue} / {adaptive_tail} | {above} | {urgent} | "
            "{retries} |".format(
                arm=arm["arm"],
                fixed_queue=_format_seconds(
                    fixed.get("time_weighted_queue_p95_seconds")
                ),
                fixed_tail=_format_seconds(
                    fixed.get("listener_tail_seconds")
                ),
                adaptive_queue=_format_seconds(
                    adaptive.get("time_weighted_queue_p95_seconds")
                ),
                adaptive_tail=_format_seconds(
                    adaptive.get("listener_tail_seconds")
                ),
                above=_format_percent(
                    adaptive.get(
                        "percent_playback_window_above_10_seconds"
                    )
                ),
                urgent=_format_percent(
                    adaptive.get("urgent_source_percent")
                ),
                retries=arm["tts_retry_count"],
            )
        )

    recommendation = summary["recommendation"]
    lines.extend(["", "## Recommendation", ""])
    if not recommendation["explicit_gates_configured"]:
        lines.append(
            "No automatic winner was selected because a complete explicit "
            "promotion-gate set was not supplied."
        )
    elif recommendation["recommended_arm"] is None:
        lines.append("No arm passed every explicit promotion gate.")
    else:
        lines.append(
            f"`{recommendation['recommended_arm']}` ranks first among arms "
            "that passed every explicit absolute, control-relative benefit, "
            "and non-regression promotion gate."
        )
    lines.extend(
        [
            "",
            (
                "Native-language review remains required before promotion "
                "because request boundaries can change pauses, prosody, and "
                "intelligibility."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _format_seconds(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}s"


def _format_percent(value: Any) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize cap-0/40/45/60 TTS subsegment canaries"
    )
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--min-child-p95-reduction-percent", type=float)
    parser.add_argument(
        "--min-adaptive-queue-p95-reduction-percent",
        type=float,
    )
    parser.add_argument(
        "--min-adaptive-listener-tail-reduction-percent",
        type=float,
    )
    parser.add_argument("--max-child-p95-seconds", type=float)
    parser.add_argument("--max-child-max-seconds", type=float)
    parser.add_argument("--max-output-ratio-increase-percent", type=float)
    parser.add_argument(
        "--max-first-audio-latency-increase-seconds",
        type=float,
    )
    parser.add_argument(
        "--max-captured-listener-tail-increase-seconds",
        type=float,
    )
    parser.add_argument(
        "--max-service-tail-lag-increase-seconds",
        type=float,
    )
    parser.add_argument("--max-adaptive-queue-p95-seconds", type=float)
    parser.add_argument("--max-adaptive-time-above-10-percent", type=float)
    parser.add_argument("--max-adaptive-urgent-source-percent", type=float)
    parser.add_argument(
        "--max-adaptive-urgent-source-increase-percent-points",
        type=float,
    )
    parser.add_argument("--max-tts-retries", type=int)
    return parser


def _promotion_gates_from_args(
    args: argparse.Namespace,
) -> PromotionGates | None:
    names = (
        "min_child_p95_reduction_percent",
        "min_adaptive_queue_p95_reduction_percent",
        "min_adaptive_listener_tail_reduction_percent",
        "max_child_p95_seconds",
        "max_child_max_seconds",
        "max_output_ratio_increase_percent",
        "max_first_audio_latency_increase_seconds",
        "max_captured_listener_tail_increase_seconds",
        "max_service_tail_lag_increase_seconds",
        "max_adaptive_queue_p95_seconds",
        "max_adaptive_time_above_10_percent",
        "max_adaptive_urgent_source_percent",
        "max_adaptive_urgent_source_increase_percent_points",
        "max_tts_retries",
    )
    supplied = [getattr(args, name) is not None for name in names]
    if any(supplied) and not all(supplied):
        raise ValueError(
            "all promotion-gate options are required when any gate is supplied"
        )
    if not any(supplied):
        return None
    return PromotionGates(**{name: getattr(args, name) for name in names})


def main(argv: Sequence[str] | None = None) -> None:
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    try:
        gates = _promotion_gates_from_args(args)
        summary = build_canary_summary(
            args.input_dir,
            promotion_gates=gates,
        )
    except ValueError as exc:
        parser.error(str(exc))

    json_output = (
        args.json_output
        or args.input_dir / "tts_subsegment_canary_comparison.json"
    )
    markdown_output = (
        args.markdown_output
        or args.input_dir / "tts_subsegment_canary_comparison.md"
    )
    if json_output.resolve() == markdown_output.resolve():
        parser.error("JSON and Markdown outputs must be different files")
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(
        render_markdown(summary),
        encoding="utf-8",
    )
    print(f"Wrote {json_output}")
    print(f"Wrote {markdown_output}")


if __name__ == "__main__":
    main()
