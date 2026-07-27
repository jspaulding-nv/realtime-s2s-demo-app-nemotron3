#!/usr/bin/env python3
"""Probe Chatterbox Speech NIM ``cfg_weight`` compatibility and effect.

This is a standalone, default-off diagnostic.  A successful request establishes
only that the pinned NIM accepted the field; it does not establish that the
field affected synthesis.  The balanced matrix applies conservative,
predeclared duration and real-time-candidate thresholds.

The JSON report excludes input text, input paths, service hostnames, exception
messages, response payloads, and audio fingerprints.  WAV files contain
synthesized speech and require separate review before sharing.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import chatterbox_tts_canary as canary
from tts_comparison_fixture import DEFAULT_TEXT, identify_text


REPORT_FILENAME = "chatterbox-cfg-weight-probe.json"
DIAGNOSTIC_NAME = "chatterbox_cfg_weight_probe"
SMOKE_EXAGGERATION_FACTOR = 0.5
SMOKE_CFG_SEQUENCE = (None, 0.3, 0.5, 0.7, None)
MATRIX_EXAGGERATION_FACTORS = (0.5, 0.7)
MATRIX_CFG_WEIGHTS = (None, 0.3, 0.5, 0.7)
DEFAULT_REPEATS_PER_CELL = 5
DETERMINISTIC_REJECTION_STATUSES = frozenset(
    {"INVALID_ARGUMENT", "UNIMPLEMENTED"}
)
MIN_DIRECTIONAL_REPEATS = 5
DIRECTIONAL_AGREEMENT_FRACTION = 0.8
MIN_DURATION_EFFECT_FRACTION = 0.05
MIN_DURATION_EFFECT_SECONDS = 0.25
MAX_TTFA_WORSENING_SECONDS = 0.25
BALANCED_GATE_NOT_MET_EXIT_CODE = 3


@dataclass(frozen=True)
class ProbeRequest:
    exaggeration_factor: float
    cfg_weight: Optional[float]
    repeat_index: int
    phase: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_artifact_directory(
    now: Optional[datetime] = None,
) -> Path:
    timestamp = (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return (
        Path("experiment_results")
        / f"chatterbox-cfg-weight-probe-{timestamp}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Test whether pinned Chatterbox Speech NIM accepts cfg_weight; "
            "use --balanced-matrix to test for a measurable effect."
        )
    )
    text_group = parser.add_mutually_exclusive_group()
    text_group.add_argument(
        "--text",
        help="text to synthesize; never copied to the JSON report",
    )
    text_group.add_argument(
        "--text-file",
        type=Path,
        help="UTF-8 text file; its path and contents are never reported",
    )
    parser.add_argument(
        "--balanced-matrix",
        action="store_true",
        help=(
            "run exag 0.5/0.7 x cfg omitted/0.3/0.5/0.7 instead "
            "of the five-request bracketed smoke"
        ),
    )
    parser.add_argument(
        "--repeats-per-cell",
        type=int,
        default=DEFAULT_REPEATS_PER_CELL,
        help="balanced-matrix repeats per cell (default: 5)",
    )
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--grpc-uri", default=canary.DEFAULT_GRPC_URI)
    parser.add_argument(
        "--tts-locale",
        "--language-code",
        dest="tts_locale",
        default=canary.DEFAULT_TTS_LOCALE,
    )
    parser.add_argument("--voice", default=canary.DEFAULT_VOICE)
    parser.add_argument(
        "--sample-rate-hz",
        type=int,
        default=canary.DEFAULT_SAMPLE_RATE_HZ,
    )
    parser.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=canary.DEFAULT_RPC_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-audio-duration-seconds",
        type=float,
        default=canary.DEFAULT_MAX_AUDIO_DURATION_SECONDS,
    )
    parser.add_argument(
        "--container-image",
        default=os.environ.get("CHATTERBOX_TTS_IMAGE")
        or canary.DEFAULT_CONTAINER_IMAGE,
    )
    parser.add_argument(
        "--nim-profile",
        default=os.environ.get("CHATTERBOX_TTS_NIM_TAGS_SELECTOR")
        or canary.DEFAULT_NIM_PROFILE,
    )
    parser.add_argument(
        "--image-digest",
        default=os.environ.get("CHATTERBOX_TTS_IMAGE_DIGEST")
        or canary.DEFAULT_IMAGE_DIGEST,
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def load_text(args: argparse.Namespace) -> str:
    if args.text_file is not None:
        text = args.text_file.expanduser().read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = DEFAULT_TEXT
    text = text.strip()
    if not text:
        raise ValueError("synthesis text must contain non-whitespace content")
    return text


def validate_repeats(repeats_per_cell: int) -> int:
    return canary.validate_repeats(repeats_per_cell)


def require_exact_riva_client(version: str) -> None:
    if version != canary.REQUIRED_RIVA_CLIENT_VERSION:
        raise RuntimeError(
            "nvidia-riva-client=="
            f"{canary.REQUIRED_RIVA_CLIENT_VERSION} is required"
        )
    canary.require_supported_riva_client(version)


def build_probe_plan(
    *,
    balanced_matrix: bool,
    repeats_per_cell: int = DEFAULT_REPEATS_PER_CELL,
) -> tuple[ProbeRequest, ...]:
    repeats = validate_repeats(repeats_per_cell)
    if not balanced_matrix:
        phases = (
            "control_before",
            "treatment",
            "treatment",
            "treatment",
            "control_after",
        )
        return tuple(
            ProbeRequest(
                exaggeration_factor=SMOKE_EXAGGERATION_FACTOR,
                cfg_weight=cfg_weight,
                repeat_index=1,
                phase=phase,
            )
            for cfg_weight, phase in zip(SMOKE_CFG_SEQUENCE, phases)
        )

    base = tuple(
        (factor, cfg_weight)
        for factor in MATRIX_EXAGGERATION_FACTORS
        for cfg_weight in MATRIX_CFG_WEIGHTS
    )
    requests = []
    for repeat_index in range(1, repeats + 1):
        rotation = (repeat_index - 1) % len(base)
        ordered = base[rotation:] + base[:rotation]
        requests.extend(
            ProbeRequest(
                exaggeration_factor=factor,
                cfg_weight=cfg_weight,
                repeat_index=repeat_index,
                phase="balanced_matrix",
            )
            for factor, cfg_weight in ordered
        )
    return tuple(requests)


def _setting_label(cfg_weight: Optional[float]) -> str:
    if cfg_weight is None:
        return "omitted"
    return f"{cfg_weight:g}".replace(".", "p")


def wav_filename(
    *,
    execution_index: int,
    request: ProbeRequest,
) -> str:
    factor = canary.factor_label(request.exaggeration_factor)
    return (
        f"chatterbox-cfg-{execution_index:03d}-exag-{factor}"
        f"-cfg-{_setting_label(request.cfg_weight)}"
        f"-repeat-{request.repeat_index:02d}.wav"
    )


def _is_success(result: Mapping[str, Any]) -> bool:
    return result.get("status") == "succeeded"


def _is_control(result: Mapping[str, Any]) -> bool:
    return result.get("cfg_weight_setting") == "omitted"


def classify_compatibility(
    results: Sequence[Mapping[str, Any]],
    *,
    warmup_status: str = "succeeded",
) -> dict[str, Any]:
    controls = [result for result in results if _is_control(result)]
    treatments = [result for result in results if not _is_control(result)]
    controls_healthy = bool(controls) and all(
        _is_success(result) for result in controls
    )
    treatment_successes = sum(_is_success(result) for result in treatments)
    deterministic_rejections = sum(
        result.get("status") == "failed"
        and isinstance(result.get("error"), Mapping)
        and result["error"].get("grpc_status")
        in DETERMINISTIC_REJECTION_STATUSES
        for result in treatments
    )

    warmup_healthy = warmup_status == "succeeded"
    if (
        warmup_healthy
        and controls_healthy
        and treatments
        and treatment_successes == len(treatments)
    ):
        classification = "accepted"
        reason = "all_weighted_requests_succeeded_with_healthy_controls"
    elif (
        warmup_healthy
        and controls_healthy
        and treatments
        and deterministic_rejections == len(treatments)
    ):
        classification = "rejected"
        reason = (
            "all_weighted_requests_deterministically_rejected_with_"
            "healthy_controls"
        )
    else:
        classification = "inconclusive"
        reason = "mixed_or_unhealthy_observations"

    return {
        "classification": classification,
        "reason": reason,
        "warmup_healthy": warmup_healthy,
        "controls_healthy": controls_healthy,
        "control_request_count": len(controls),
        "weighted_request_count": len(treatments),
        "weighted_succeeded": treatment_successes,
        "weighted_deterministically_rejected": deterministic_rejections,
        "acceptance_establishes_effect": False,
    }


def _cell_results(
    results: Sequence[Mapping[str, Any]],
    factor: float,
    cfg_weight: Optional[float],
) -> list[Mapping[str, Any]]:
    return [
        result
        for result in results
        if result.get("exaggeration_factor") == factor
        and (
            (
                cfg_weight is None
                and result.get("cfg_weight_setting") == "omitted"
            )
            or (
                cfg_weight is not None
                and result.get("cfg_weight_setting") == "provided"
                and result.get("cfg_weight") == cfg_weight
            )
        )
    ]


def _median_metric(
    results: Sequence[Mapping[str, Any]],
    metric: str,
) -> Optional[float]:
    values = [
        float(result[metric])
        for result in results
        if _is_success(result)
        and isinstance(result.get(metric), (int, float))
        and not isinstance(result.get(metric), bool)
        and math.isfinite(float(result[metric]))
    ]
    if len(values) != len(results) or not values:
        return None
    return statistics.median(values)


def _complete_balanced_cells(
    results: Sequence[Mapping[str, Any]],
    repeats_per_cell: int,
) -> bool:
    expected_repeats = set(range(1, repeats_per_cell + 1))
    return all(
        {
            result.get("repeat_index")
            for result in _cell_results(results, factor, cfg_weight)
        }
        == expected_repeats
        and len(_cell_results(results, factor, cfg_weight))
        == repeats_per_cell
        and all(
            _is_success(result)
            for result in _cell_results(results, factor, cfg_weight)
        )
        for factor in MATRIX_EXAGGERATION_FACTORS
        for cfg_weight in MATRIX_CFG_WEIGHTS
    )


def _balanced_cells_have_metrics(
    results: Sequence[Mapping[str, Any]],
    metrics: Sequence[str],
) -> bool:
    return all(
        all(
            isinstance(result.get(metric), (int, float))
            and not isinstance(result.get(metric), bool)
            and math.isfinite(float(result[metric]))
            for metric in metrics
        )
        for result in results
        if _is_success(result)
    )


def classify_effect(
    results: Sequence[Mapping[str, Any]],
    *,
    compatibility: Mapping[str, Any],
    balanced_matrix: bool,
    repeats_per_cell: int,
) -> dict[str, Any]:
    criteria = {
        "minimum_repeats_per_cell": MIN_DIRECTIONAL_REPEATS,
        "directional_agreement_fraction": (
            DIRECTIONAL_AGREEMENT_FRACTION
        ),
        "minimum_duration_difference_fraction": (
            MIN_DURATION_EFFECT_FRACTION
        ),
        "minimum_duration_difference_seconds": (
            MIN_DURATION_EFFECT_SECONDS
        ),
        "both_exaggeration_factors_required": True,
    }
    base: dict[str, Any] = {
        "classification": "inconclusive",
        "reason": "balanced_matrix_required",
        "criteria": criteria,
        "acceptance_alone_is_not_effect": True,
        "factor_evidence": [],
    }
    if not balanced_matrix:
        return base
    if compatibility.get("classification") != "accepted":
        base["reason"] = "compatibility_not_accepted"
        return base
    if repeats_per_cell < MIN_DIRECTIONAL_REPEATS:
        base["reason"] = "at_least_five_repeats_required"
        return base
    if not _complete_balanced_cells(results, repeats_per_cell):
        base["reason"] = "balanced_matrix_incomplete"
        return base
    if not _balanced_cells_have_metrics(
        results, ("audio_duration_seconds",)
    ):
        base["reason"] = "duration_metrics_incomplete"
        return base

    required_agreements = max(
        4,
        math.ceil(
            repeats_per_cell * DIRECTIONAL_AGREEMENT_FRACTION
        ),
    )
    factor_evidence = []
    directions = []
    thresholds = []
    for factor in MATRIX_EXAGGERATION_FACTORS:
        low_by_repeat = {
            int(result["repeat_index"]): float(
                result["audio_duration_seconds"]
            )
            for result in _cell_results(results, factor, 0.3)
        }
        high_by_repeat = {
            int(result["repeat_index"]): float(
                result["audio_duration_seconds"]
            )
            for result in _cell_results(results, factor, 0.7)
        }
        high_shorter = sum(
            high_by_repeat[index] < low_by_repeat[index]
            for index in low_by_repeat
        )
        low_shorter = sum(
            low_by_repeat[index] < high_by_repeat[index]
            for index in low_by_repeat
        )
        if high_shorter >= required_agreements:
            direction = "cfg_0_7_shorter"
            agreement_count = high_shorter
        elif low_shorter >= required_agreements:
            direction = "cfg_0_3_shorter"
            agreement_count = low_shorter
        else:
            direction = "no_consistent_direction"
            agreement_count = max(high_shorter, low_shorter)

        low_median = statistics.median(low_by_repeat.values())
        high_median = statistics.median(high_by_repeat.values())
        duration_difference_seconds = abs(low_median - high_median)
        duration_difference_fraction = (
            duration_difference_seconds / max(low_median, high_median)
        )
        threshold_met = (
            duration_difference_fraction
            >= MIN_DURATION_EFFECT_FRACTION
            or duration_difference_seconds
            >= MIN_DURATION_EFFECT_SECONDS
        )
        directions.append(direction)
        thresholds.append(threshold_met)
        factor_evidence.append(
            {
                "exaggeration_factor": factor,
                "required_directional_agreements": required_agreements,
                "direction": direction,
                "directional_agreement_count": agreement_count,
                "median_cfg_0_3_duration_seconds": low_median,
                "median_cfg_0_7_duration_seconds": high_median,
                "median_extreme_duration_difference_seconds": (
                    duration_difference_seconds
                ),
                "median_extreme_duration_difference_fraction": (
                    duration_difference_fraction
                ),
                "duration_threshold_met": threshold_met,
            }
        )

    same_supported_direction = (
        directions[0] == directions[1]
        and directions[0]
        in {"cfg_0_7_shorter", "cfg_0_3_shorter"}
    )
    if same_supported_direction and all(thresholds):
        classification = "effect_detected"
        reason = "direction_and_duration_thresholds_met"
    else:
        classification = "effect_not_demonstrated"
        reason = "direction_or_duration_threshold_not_met"

    base.update(
        {
            "classification": classification,
            "reason": reason,
            "consistent_direction": (
                directions[0] if same_supported_direction else None
            ),
            "factor_evidence": factor_evidence,
        }
    )
    return base


def classify_realtime_candidates(
    results: Sequence[Mapping[str, Any]],
    *,
    effect: Mapping[str, Any],
    balanced_matrix: bool,
    repeats_per_cell: int,
) -> dict[str, Any]:
    criteria = {
        "minimum_duration_reduction_fraction_vs_omitted": (
            MIN_DURATION_EFFECT_FRACTION
        ),
        "maximum_ttfa_worsening_seconds_vs_omitted": (
            MAX_TTFA_WORSENING_SECONDS
        ),
        "median_real_time_factor_must_not_worsen": True,
        "underrun_count_must_not_worsen": True,
        "median_underrun_risk_must_not_worsen": True,
        "effect_must_be_detected": True,
    }
    if (
        not balanced_matrix
        or effect.get("classification") != "effect_detected"
        or not _complete_balanced_cells(results, repeats_per_cell)
        or not _balanced_cells_have_metrics(
            results,
            (
                "audio_duration_seconds",
                "ttfa_seconds",
                "real_time_factor",
                "underrun_risk_seconds",
            ),
        )
    ):
        return {
            "classification": "not_evaluated",
            "reason": "detected_effect_and_complete_matrix_required",
            "criteria": criteria,
            "comparisons": [],
            "candidates": [],
        }

    comparisons = []
    candidates = []
    for factor in MATRIX_EXAGGERATION_FACTORS:
        controls = _cell_results(results, factor, None)
        control_duration = _median_metric(
            controls, "audio_duration_seconds"
        )
        control_ttfa = _median_metric(controls, "ttfa_seconds")
        control_rtf = _median_metric(controls, "real_time_factor")
        control_underruns = sum(
            result.get("underrun_risk_detected") is True
            for result in controls
        )
        control_underrun_risk = _median_metric(
            controls, "underrun_risk_seconds"
        )
        if (
            control_duration is None
            or control_ttfa is None
            or control_rtf is None
            or control_underrun_risk is None
        ):
            return {
                "classification": "not_evaluated",
                "reason": "detected_effect_and_complete_matrix_required",
                "criteria": criteria,
                "comparisons": [],
                "candidates": [],
            }
        for cfg_weight in MATRIX_CFG_WEIGHTS[1:]:
            cell = _cell_results(results, factor, cfg_weight)
            duration = _median_metric(cell, "audio_duration_seconds")
            ttfa = _median_metric(cell, "ttfa_seconds")
            rtf = _median_metric(cell, "real_time_factor")
            underrun_risk = _median_metric(
                cell, "underrun_risk_seconds"
            )
            if (
                duration is None
                or ttfa is None
                or rtf is None
                or underrun_risk is None
            ):
                return {
                    "classification": "not_evaluated",
                    "reason": (
                        "detected_effect_and_complete_matrix_required"
                    ),
                    "criteria": criteria,
                    "comparisons": [],
                    "candidates": [],
                }
            underruns = sum(
                result.get("underrun_risk_detected") is True
                for result in cell
            )
            duration_reduction = (
                control_duration - duration
            ) / control_duration
            ttfa_delta = ttfa - control_ttfa
            checks = {
                "duration_reduction_met": (
                    duration_reduction
                    >= MIN_DURATION_EFFECT_FRACTION
                ),
                "ttfa_not_worse_beyond_limit": (
                    ttfa_delta <= MAX_TTFA_WORSENING_SECONDS
                ),
                "real_time_factor_not_worse": rtf <= control_rtf,
                "underrun_count_not_worse": (
                    underruns <= control_underruns
                ),
                "median_underrun_risk_not_worse": (
                    underrun_risk <= control_underrun_risk
                ),
            }
            is_candidate = all(checks.values())
            comparison = {
                "exaggeration_factor": factor,
                "cfg_weight": cfg_weight,
                "median_duration_reduction_fraction_vs_omitted": (
                    duration_reduction
                ),
                "median_ttfa_delta_seconds_vs_omitted": ttfa_delta,
                "median_real_time_factor": rtf,
                "omitted_median_real_time_factor": control_rtf,
                "underrun_count": underruns,
                "omitted_underrun_count": control_underruns,
                "median_underrun_risk_seconds": underrun_risk,
                "omitted_median_underrun_risk_seconds": (
                    control_underrun_risk
                ),
                "checks": checks,
                "is_realtime_candidate": is_candidate,
            }
            comparisons.append(comparison)
            if is_candidate:
                candidates.append(
                    {
                        "exaggeration_factor": factor,
                        "cfg_weight": cfg_weight,
                    }
                )

    return {
        "classification": (
            "candidate_found"
            if candidates
            else "candidate_not_demonstrated"
        ),
        "reason": (
            "all_realtime_thresholds_met"
            if candidates
            else "one_or_more_realtime_thresholds_not_met"
        ),
        "criteria": criteria,
        "comparisons": comparisons,
        "candidates": candidates,
    }


def _report_provenance(
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Retain pinned identity without copying a registry hostname."""
    return {
        "verification_status": provenance["verification_status"],
        "runtime_attested": provenance["runtime_attested"],
        "declared_release": provenance["declared_release"],
        "declared_image_digest": provenance["declared_image_digest"],
        "declared_nim_profile": provenance["declared_nim_profile"],
    }


def run_probe(
    service: Any,
    *,
    riva_client_module: Any,
    text: str,
    tts_locale: str,
    voice: str,
    sample_rate_hz: int,
    artifact_dir: Path,
    grpc_uri: str,
    client_version: str,
    balanced_matrix: bool = False,
    repeats_per_cell: int = DEFAULT_REPEATS_PER_CELL,
    rpc_timeout_seconds: float = canary.DEFAULT_RPC_TIMEOUT_SECONDS,
    max_audio_duration_seconds: float = (
        canary.DEFAULT_MAX_AUDIO_DURATION_SECONDS
    ),
    container_image: str = canary.DEFAULT_CONTAINER_IMAGE,
    nim_profile: str = canary.DEFAULT_NIM_PROFILE,
    image_digest: str = canary.DEFAULT_IMAGE_DIGEST,
    custom_text_override: bool = False,
    clock: Callable[[], float] = time.perf_counter,
    timer_factory: Callable[[float, Callable[[], None]], Any] = (
        canary.threading.Timer
    ),
    now: Callable[[], datetime] = utc_now,
    quiet: bool = False,
) -> tuple[dict[str, Any], Path]:
    require_exact_riva_client(client_version)
    canary.require_local_client_origin(
        getattr(riva_client_module, "__file__", None)
    )
    canary.validate_tts_voice(tts_locale, voice)
    canary.validate_audio_format(sample_rate_hz)
    repeats = validate_repeats(repeats_per_cell)
    timeout_s = canary.validate_positive_finite(
        "RPC timeout", rpc_timeout_seconds
    )
    duration_limit_s = canary.validate_positive_finite(
        "maximum audio duration", max_audio_duration_seconds
    )
    provenance = canary.validate_declared_model_provenance(
        container_image=container_image,
        nim_profile=nim_profile,
        image_digest=image_digest,
    )
    grpc_port = canary._grpc_port(grpc_uri)
    plan = build_probe_plan(
        balanced_matrix=balanced_matrix,
        repeats_per_cell=repeats,
    )

    private_dir = canary._new_private_directory(artifact_dir)
    report_path = private_dir / REPORT_FILENAME
    started_at = now()

    try:
        canary.synthesize_factor(
            service,
            riva_client_module=riva_client_module,
            text=text,
            tts_locale=tts_locale,
            voice=voice,
            sample_rate_hz=sample_rate_hz,
            exaggeration_factor=SMOKE_EXAGGERATION_FACTOR,
            cfg_weight=None,
            output_path=None,
            rpc_timeout_seconds=timeout_s,
            max_audio_duration_seconds=duration_limit_s,
            clock=clock,
            timer_factory=timer_factory,
        )
        warmup_status = "succeeded"
    except Exception:
        warmup_status = "failed"
    warmup = {
        "status": warmup_status,
        "exaggeration_factor": SMOKE_EXAGGERATION_FACTOR,
        "cfg_weight_setting": "omitted",
        "audio_artifact_written": False,
    }

    results = []
    for execution_index, request in enumerate(plan, start=1):
        wav_path = private_dir / wav_filename(
            execution_index=execution_index,
            request=request,
        )
        try:
            result = canary.synthesize_factor(
                service,
                riva_client_module=riva_client_module,
                text=text,
                tts_locale=tts_locale,
                voice=voice,
                sample_rate_hz=sample_rate_hz,
                exaggeration_factor=request.exaggeration_factor,
                cfg_weight=request.cfg_weight,
                output_path=wav_path,
                rpc_timeout_seconds=timeout_s,
                max_audio_duration_seconds=duration_limit_s,
                clock=clock,
                timer_factory=timer_factory,
            )
        except Exception as exc:
            result = {
                "exaggeration_factor": request.exaggeration_factor,
                "status": "failed",
                "error": canary.safe_error(exc),
            }
        result.update(
            {
                "execution_index": execution_index,
                "repeat_index": request.repeat_index,
                "phase": request.phase,
                "cfg_weight_setting": (
                    "omitted"
                    if request.cfg_weight is None
                    else "provided"
                ),
            }
        )
        if request.cfg_weight is not None:
            result["cfg_weight"] = request.cfg_weight
        results.append(result)
        if not quiet:
            setting = _setting_label(request.cfg_weight)
            print(
                f"exag={request.exaggeration_factor:g} "
                f"cfg={setting} status={result['status']}"
            )

    compatibility = classify_compatibility(
        results,
        warmup_status=warmup_status,
    )
    effect = classify_effect(
        results,
        compatibility=compatibility,
        balanced_matrix=balanced_matrix,
        repeats_per_cell=repeats,
    )
    realtime = classify_realtime_candidates(
        results,
        effect=effect,
        balanced_matrix=balanced_matrix,
        repeats_per_cell=repeats,
    )
    completed_at = now()
    report = {
        "schema_version": 1,
        "diagnostic": DIAGNOSTIC_NAME,
        "mode": "balanced_matrix" if balanced_matrix else "bracketed_smoke",
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": completed_at.isoformat(),
        "client": {
            "package": "nvidia-riva-client",
            "reported_version": client_version,
            "required_version": canary.REQUIRED_RIVA_CLIENT_VERSION,
            "streaming_api": "bidirectional",
        },
        "declared_model_provenance": _report_provenance(provenance),
        "service": {
            "grpc_port": grpc_port,
            "requested_model": "chatterbox-tts-multilingual",
        },
        "request": {
            "tts_locale": tts_locale,
            "voice": voice,
            "sample_rate_hz": sample_rate_hz,
            "channels": 1,
            "sample_width_bytes": 2,
            "encoding": "LINEAR_PCM",
            "text": identify_text(
                text,
                custom_override=custom_text_override,
            ),
            "repeats_per_cell": repeats if balanced_matrix else 1,
            "rpc_timeout_seconds": timeout_s,
            "max_audio_duration_seconds": duration_limit_s,
            "max_audio_bytes": int(
                sample_rate_hz * 2 * duration_limit_s
            ),
        },
        "warmup": warmup,
        "results": results,
        "compatibility": compatibility,
        "effect": effect,
        "realtime_candidate": realtime,
        "summary": {
            "requested": len(results),
            "succeeded": sum(_is_success(result) for result in results),
            "failed": sum(not _is_success(result) for result in results),
            "warmup_status": warmup_status,
        },
        "privacy": {
            "contains_input_text": False,
            "contains_input_path": False,
            "contains_text_fingerprint": False,
            "contains_service_hostname": False,
            "contains_audio_payload_in_json": False,
            "contains_audio_fingerprint": False,
            "wav_files_contain_synthesized_speech": True,
            "audio_review_required_before_sharing": True,
            "artifact_directory_mode": "0700",
            "artifact_file_mode": "0600",
        },
    }
    canary._write_json_atomic(report_path, report)
    return report, report_path


def _load_client() -> tuple[str, Any]:
    client_version = importlib.metadata.version("nvidia-riva-client")
    require_exact_riva_client(client_version)
    import riva.client as riva_client

    canary.require_local_client_origin(riva_client.__file__)
    return client_version, riva_client


def report_exit_code(
    report: Mapping[str, Any],
    *,
    balanced_matrix: bool,
) -> int:
    """Return success only for the gate selected by the CLI mode."""
    compatibility = report.get("compatibility")
    if (
        not isinstance(compatibility, Mapping)
        or compatibility.get("classification") != "accepted"
    ):
        return 1
    if not balanced_matrix:
        return 0
    realtime = report.get("realtime_candidate")
    if (
        isinstance(realtime, Mapping)
        and realtime.get("classification") == "candidate_found"
    ):
        return 0
    return BALANCED_GATE_NOT_MET_EXIT_CODE


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = load_text(args)
        custom_text_override = (
            args.text is not None or args.text_file is not None
        )
        validate_repeats(args.repeats_per_cell)
        canary.validate_audio_format(args.sample_rate_hz)
        canary.validate_positive_finite(
            "RPC timeout", args.rpc_timeout_seconds
        )
        canary.validate_positive_finite(
            "maximum audio duration",
            args.max_audio_duration_seconds,
        )
        canary.validate_declared_model_provenance(
            container_image=args.container_image,
            nim_profile=args.nim_profile,
            image_digest=args.image_digest,
        )
        canary._grpc_port(args.grpc_uri)
        client_version, riva_client = _load_client()
    except (
        ImportError,
        importlib.metadata.PackageNotFoundError,
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
    ):
        print("cfg_weight probe setup failed.", file=sys.stderr)
        return 2

    artifact_dir = args.artifact_dir or default_artifact_directory()
    try:
        auth = riva_client.Auth(uri=args.grpc_uri)
        service = riva_client.SpeechSynthesisService(auth)
    except Exception:
        print("cfg_weight probe connection setup failed.", file=sys.stderr)
        return 1

    try:
        report, report_path = run_probe(
            service,
            riva_client_module=riva_client,
            text=text,
            tts_locale=args.tts_locale,
            voice=args.voice,
            sample_rate_hz=args.sample_rate_hz,
            artifact_dir=artifact_dir,
            grpc_uri=args.grpc_uri,
            client_version=client_version,
            balanced_matrix=args.balanced_matrix,
            repeats_per_cell=args.repeats_per_cell,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            max_audio_duration_seconds=(
                args.max_audio_duration_seconds
            ),
            container_image=args.container_image,
            nim_profile=args.nim_profile,
            image_digest=args.image_digest,
            custom_text_override=custom_text_override,
            quiet=args.quiet,
        )
    except Exception:
        print("cfg_weight probe execution failed.", file=sys.stderr)
        return 1
    finally:
        channel = getattr(auth, "channel", None)
        if channel is not None:
            channel.close()

    if not args.quiet:
        print(f"Privacy-safe JSON report: {report_path}")
    return report_exit_code(
        report,
        balanced_matrix=args.balanced_matrix,
    )


if __name__ == "__main__":
    raise SystemExit(main())
