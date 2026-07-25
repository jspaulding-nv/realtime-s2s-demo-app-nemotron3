#!/usr/bin/env python3
"""Capture one privacy-safe ASR word-timing-shape diagnostic replay.

This executable is deliberately separate from the two-run attribution
qualification.  Its output describes whether one diagnostic capture completed;
it never expresses a qualification outcome.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from asr_final_attribution_gate import (
    MAX_CHUNK_RELEASE_LATENESS_MS,
    ROOT,
    _compute_exact_wav_pcm_binding,
    _sha256,
    _write_report,
    attest_local_asr_runtime,
    riva_config,
    run_once,
)
from staged_models import (
    ASR_BOUNDARY_NUMERIC_CLASSES,
    ASR_BOUNDARY_PRESENCES,
    ASR_TIMING_BASES,
    ASR_WORD_TIMING_RELATIONS,
    ASR_WORD_TIMING_SHAPES,
)


ARTIFACT_KIND = "asr_word_timing_shape_diagnostic"
QUALIFICATION_STATUS = "not_evaluated"
REQUESTED_RUN_COUNT = 1
REGISTERED_SOURCE_LANGUAGE = "en-US"
REGISTERED_EOU_MS = 800
PCM_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
PREFIXED_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
ATTEMPT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
FORBIDDEN_QUALIFICATION_KEYS = frozenset({"gate", "passed"})
FORBIDDEN_ATTRIBUTION_CONTENT_KEYS = frozenset(
    {
        "text",
        "transcript",
        "token",
    }
)
VERIFIED_ATTESTATION_KEYS = frozenset(
    {
        "schema_version",
        "verified",
        "health",
        "host_port",
        "container_port",
        "local_image_id",
        "repository_digest",
        "profile_selector_sha256",
        "container_instance_sha256",
    }
)
ATTRIBUTION_KEYS = frozenset(
    {
        "schema_version",
        "nonempty_final_count",
        "final_with_word_offsets_count",
        "final_missing_word_offsets_count",
        "missing_word_offset_final_ids",
        "timing_basis_counts",
        "all_nonempty_finals_have_word_offsets",
        "word_timing_shape_diagnostic_final_count",
        "word_timing_shape_diagnostic_unavailable_final_count",
        "word_timing_shape_diagnostic_unavailable_final_ids",
        "no_word_entries_final_count",
        "no_word_entries_final_ids",
        "envelope_shape_counts",
        "envelope_shape_final_ids",
        "word_entry_shape_counts",
        "start_numeric_class_counts",
        "end_numeric_class_counts",
        "start_presence_counts",
        "end_presence_counts",
        "anomalous_word_entry_count",
        "finals",
    }
)
FINAL_ATTRIBUTION_KEYS = frozenset(
    {
        "final_id",
        "text_chars",
        "word_count",
        "audio_processed_s",
        "first_word_start_ms",
        "last_word_end_ms",
        "source_start_ms",
        "source_end_ms",
        "timing_basis",
        "received_monotonic_ms",
        "word_timing_shape_diagnostics",
    }
)
WORD_TIMING_DIAGNOSTIC_KEYS = frozenset(
    {
        "word_entry_count",
        "no_word_entries",
        "envelope",
        "counts",
        "anomalies",
    }
)
WORD_TIMING_COUNT_GROUP_KEYS = frozenset(
    {
        "entry_shape",
        "start_numeric_class",
        "end_numeric_class",
        "start_presence",
        "end_presence",
    }
)
WORD_TIMING_ENVELOPE_KEYS = frozenset(
    {
        "start_word_index",
        "end_word_index",
        "start",
        "end",
        "numeric_relation",
        "shape",
        "usable",
    }
)
WORD_TIMING_ANOMALY_KEYS = frozenset(
    {
        "word_index",
        "start",
        "end",
        "numeric_relation",
        "shape",
    }
)
BOUNDARY_BASE_KEYS = frozenset({"presence", "numeric_class"})
BOUNDARY_FINITE_KEYS = frozenset(
    {"presence", "numeric_class", "finite_value_ms"}
)
ASR_ENVELOPE_SHAPES = ("no_word_entries", *ASR_WORD_TIMING_SHAPES)
COMMON_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "artifact_kind",
        "generated_at_utc",
        "attempt_id",
        "attempt_started_at_utc",
        "diagnostic_only",
        "qualification_eligible",
        "qualification_status",
        "completion_status",
        "requested_run_count",
        "completed_run_count",
        "capture_complete",
    }
)
INITIAL_REPORT_KEYS = COMMON_REPORT_KEYS | frozenset(
    {"failure_reason", "runs"}
)
FULL_REPORT_KEYS = COMMON_REPORT_KEYS | frozenset(
    {"requirements", "input", "asr", "runs"}
)
REQUIREMENTS_KEYS = frozenset(
    {
        "exact_pcm_binding_required",
        "exact_source_wav_binding_required",
        "realtime_pacing_required",
        "runtime_attestation_before_and_after_required",
        "word_time_offsets_requested_required",
        "source_language_required",
        "eou_ms_required",
        "maximum_chunk_release_lateness_ms",
    }
)
INPUT_KEYS = frozenset(
    {
        "pcm_preparation_basis",
        "wav_sha256_before",
        "capture_wav_sha256",
        "wav_sha256_after",
        "source_sample_count",
        "exact_source_wav_binding_verified",
        "padded_pcm_sha256",
        "padded_pcm_sample_count",
        "exact_padded_pcm_binding_verified",
    }
)
ASR_KEYS = frozenset(
    {
        "endpoint_configured",
        "declared_image_digest",
        "runtime_attestation",
        "word_time_offsets_requested",
        "language",
        "eou_ms",
        "registered_configuration_verified",
    }
)
RUNTIME_ATTESTATION_PAIR_KEYS = frozenset(
    {"before", "after", "identity_stable"}
)
SAFE_RUN_KEYS = frozenset(
    {
        "run_number",
        "started_at_utc",
        "wall_seconds",
        "audio_seconds_sent",
        "source_sample_count",
        "source_wav_sha256",
        "padded_pcm_sample_count",
        "padded_pcm_sha256",
        "input_completed",
        "realtime_pacing",
        "pacing_basis",
        "chunk_release_count",
        "maximum_chunk_release_lateness_ms",
        "mean_chunk_release_lateness_ms",
        "interim_count",
        "attribution",
        "capture_complete",
    }
)
INITIAL_FAILURE_REASONS = frozenset(
    {
        "initializing_or_interrupted",
        "input_not_found",
        "invalid_progress_interval",
        "word_time_offsets_not_enabled",
        "asr_configuration_mismatch",
        "invalid_input_wav",
    }
)
FULL_FAILURE_REASONS = frozenset(
    {
        "pre_capture_attestation_pending",
        "pre_capture_attestation_failed",
        "capture_not_completed",
        "capture_pending_or_interrupted",
        "capture_failed",
        "capture_integrity_failed",
        "input_identity_changed",
        "padded_pcm_binding_mismatch",
        "asr_configuration_mismatch",
        "endpoint_not_configured",
        "runtime_image_binding_mismatch",
        "post_capture_attestation_pending",
        "post_capture_attestation_failed",
        "runtime_identity_changed",
    }
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one full PCM WAV to direct ASR at real-time pace and "
            "capture transcript-free word-timing shapes. This is a diagnostic "
            "only and is not qualification evidence."
        )
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=ROOT / "test_audio" / "long-form-03-30min.wav",
        help="16 kHz mono 16-bit PCM WAV (default: tracked long-form WAV)",
    )
    parser.add_argument(
        "--uri",
        default=riva_config.asr_uri,
        help="direct ASR gRPC endpoint",
    )
    parser.add_argument(
        "--docker-container",
        required=True,
        help=(
            "local ASR container name used only for runtime attestation; "
            "the name is not retained"
        ),
    )
    parser.add_argument(
        "--progress-seconds",
        type=float,
        default=60.0,
        help="stderr progress interval in source seconds (default: 60)",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="write the privacy-safe diagnostic report",
    )
    return parser


def _contains_key(value: Any, forbidden: frozenset[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in forbidden or _contains_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _safe_attestation(value: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if value is None:
        return {
            "schema_version": 2,
            "verified": False,
            "status": "pending",
        }
    if not isinstance(value, Mapping):
        raise ValueError("runtime attestation must be an object")
    if value.get("verified") is True:
        mapping = _require_exact_keys(
            value,
            VERIFIED_ATTESTATION_KEYS,
            field="runtime_attestation",
        )
        host_port = _copy_nonnegative_int(
            mapping["host_port"],
            field="runtime_attestation.host_port",
        )
        container_port = _copy_nonnegative_int(
            mapping["container_port"],
            field="runtime_attestation.container_port",
        )
        local_image_id = mapping["local_image_id"]
        repository_digest = mapping["repository_digest"]
        profile_hash = mapping["profile_selector_sha256"]
        instance_hash = mapping["container_instance_sha256"]
        if (
            mapping["schema_version"] != 2
            or mapping["health"] != "healthy"
            or host_port != 50052
            or container_port != 50052
            or not isinstance(local_image_id, str)
            or PREFIXED_SHA256_PATTERN.fullmatch(local_image_id) is None
            or not isinstance(repository_digest, str)
            or PREFIXED_SHA256_PATTERN.fullmatch(repository_digest) is None
            or not isinstance(profile_hash, str)
            or PCM_SHA256_PATTERN.fullmatch(profile_hash) is None
            or not isinstance(instance_hash, str)
            or PCM_SHA256_PATTERN.fullmatch(instance_hash) is None
        ):
            raise ValueError("verified runtime attestation is incomplete")
        return {
            "schema_version": 2,
            "verified": True,
            "health": "healthy",
            "host_port": host_port,
            "container_port": container_port,
            "local_image_id": local_image_id,
            "repository_digest": repository_digest,
            "profile_selector_sha256": profile_hash,
            "container_instance_sha256": instance_hash,
        }
    allowed_states = (
        (
            frozenset({"schema_version", "verified", "status"}),
            "status",
            frozenset({"pending"}),
        ),
        (
            frozenset({"schema_version", "verified", "failure"}),
            "failure",
            frozenset({"attestation_failed"}),
        ),
    )
    for keys, state_key, allowed_values in allowed_states:
        if set(value) != keys:
            continue
        state_value = value.get(state_key)
        if (
            value.get("schema_version") != 2
            or value.get("verified") is not False
            or state_value not in allowed_values
        ):
            break
        return {
            "schema_version": 2,
            "verified": False,
            state_key: state_value,
        }
    raise ValueError("runtime attestation state is outside the schema")


def _finite_number(value: Any) -> Optional[float]:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _nonnegative_int(value: Any) -> Optional[int]:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        return None
    return value


def _copy_utc_timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UTC timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field} must be a UTC timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _require_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{field} does not match the diagnostic schema")
    return value


def _copy_nonnegative_int(value: Any, *, field: str) -> int:
    copied = _nonnegative_int(value)
    if copied is None:
        raise ValueError(f"{field} must be a non-negative integer")
    return copied


def _copy_nonnegative_finite(
    value: Any,
    *,
    field: str,
    allow_none: bool = False,
) -> Optional[float]:
    if value is None and allow_none:
        return None
    copied = _finite_number(value)
    if copied is None or copied < 0:
        raise ValueError(f"{field} must be a non-negative finite number")
    return copied


def _copy_boolean(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _copy_enum(value: Any, allowed: frozenset[str], *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} is outside the diagnostic schema")
    return value


def _safe_prefixed_sha256(value: Any) -> Optional[str]:
    if (
        isinstance(value, str)
        and PREFIXED_SHA256_PATTERN.fullmatch(value) is not None
    ):
        return value
    return None


def _requirements_record() -> dict[str, Any]:
    return {
        "exact_pcm_binding_required": True,
        "exact_source_wav_binding_required": True,
        "realtime_pacing_required": True,
        "runtime_attestation_before_and_after_required": True,
        "word_time_offsets_requested_required": True,
        "source_language_required": REGISTERED_SOURCE_LANGUAGE,
        "eou_ms_required": REGISTERED_EOU_MS,
        "maximum_chunk_release_lateness_ms": (
            MAX_CHUNK_RELEASE_LATENESS_MS
        ),
    }


def _runtime_identity_is_stable(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    before_instance = before.get("container_instance_sha256")
    return (
        before.get("schema_version") == 2
        and after.get("schema_version") == 2
        and before.get("verified") is True
        and after.get("verified") is True
        and isinstance(before_instance, str)
        and PCM_SHA256_PATTERN.fullmatch(before_instance) is not None
        and before_instance == after.get("container_instance_sha256")
        and before == after
    )


def _copy_id_list(value: Any, *, field: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    copied = [
        _copy_nonnegative_int(item, field=f"{field} item")
        for item in value
    ]
    if copied != sorted(set(copied)):
        raise ValueError(f"{field} must contain unique, ordered IDs")
    return copied


def _copy_counter_map(
    value: Any,
    keys: Sequence[str],
    *,
    field: str,
) -> dict[str, int]:
    expected = frozenset(keys)
    mapping = _require_exact_keys(value, expected, field=field)
    return {
        key: _copy_nonnegative_int(mapping[key], field=f"{field}.{key}")
        for key in keys
    }


def _copy_id_map(
    value: Any,
    keys: Sequence[str],
    *,
    field: str,
) -> dict[str, list[int]]:
    expected = frozenset(keys)
    mapping = _require_exact_keys(value, expected, field=field)
    return {
        key: _copy_id_list(mapping[key], field=f"{field}.{key}")
        for key in keys
    }


def _copy_boundary(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    presence = _copy_enum(
        value.get("presence"),
        frozenset(ASR_BOUNDARY_PRESENCES),
        field=f"{field}.presence",
    )
    numeric_class = _copy_enum(
        value.get("numeric_class"),
        frozenset(ASR_BOUNDARY_NUMERIC_CLASSES),
        field=f"{field}.numeric_class",
    )
    finite_class = numeric_class in {"negative", "zero", "positive"}
    expected_keys = BOUNDARY_FINITE_KEYS if finite_class else BOUNDARY_BASE_KEYS
    _require_exact_keys(value, expected_keys, field=field)
    copied: dict[str, Any] = {
        "presence": presence,
        "numeric_class": numeric_class,
    }
    if presence == "absent" and numeric_class != "not_available":
        raise ValueError(f"{field} cannot observe an absent boundary")
    if finite_class:
        finite_value = _finite_number(value.get("finite_value_ms"))
        if finite_value is None:
            raise ValueError(f"{field}.finite_value_ms must be finite")
        if (
            (numeric_class == "negative" and finite_value >= 0)
            or (numeric_class == "zero" and finite_value != 0)
            or (numeric_class == "positive" and finite_value <= 0)
        ):
            raise ValueError(
                f"{field}.finite_value_ms does not match its numeric class"
            )
        copied["finite_value_ms"] = finite_value
    return copied


def _expected_relation_and_shape(
    start: Mapping[str, Any],
    end: Mapping[str, Any],
) -> tuple[str, str]:
    numeric_classes = {start["numeric_class"], end["numeric_class"]}
    if "not_available" in numeric_classes:
        return "not_comparable", "absent_boundary"
    if "unparseable" in numeric_classes:
        return "not_comparable", "unparseable_boundary"
    if "nonfinite" in numeric_classes:
        return "not_comparable", "nonfinite_boundary"
    start_value = start.get("finite_value_ms")
    end_value = end.get("finite_value_ms")
    if start_value is None or end_value is None:
        return "not_comparable", "unparseable_boundary"
    if end_value > start_value:
        relation = "end_after_start"
    elif end_value == start_value:
        relation = "equal"
    else:
        relation = "end_before_start"
    if "negative" in numeric_classes:
        return relation, "negative_boundary"
    if relation == "equal":
        return relation, "zero_length"
    if relation == "end_before_start":
        return relation, "reversed"
    return relation, "valid"


def _copy_word_timing_entry(
    value: Any,
    *,
    field: str,
) -> dict[str, Any]:
    mapping = _require_exact_keys(
        value,
        WORD_TIMING_ANOMALY_KEYS,
        field=field,
    )
    shape = _copy_enum(
        mapping["shape"],
        frozenset(ASR_WORD_TIMING_SHAPES),
        field=f"{field}.shape",
    )
    if shape == "valid":
        raise ValueError(f"{field} cannot retain a valid entry as an anomaly")
    start = _copy_boundary(
        mapping["start"],
        field=f"{field}.start",
    )
    end = _copy_boundary(
        mapping["end"],
        field=f"{field}.end",
    )
    relation = _copy_enum(
        mapping["numeric_relation"],
        frozenset(ASR_WORD_TIMING_RELATIONS),
        field=f"{field}.numeric_relation",
    )
    expected_relation, expected_shape = _expected_relation_and_shape(
        start,
        end,
    )
    if relation != expected_relation or shape != expected_shape:
        raise ValueError(f"{field} shape contradicts its raw boundaries")
    return {
        "word_index": _copy_nonnegative_int(
            mapping["word_index"],
            field=f"{field}.word_index",
        ),
        "start": start,
        "end": end,
        "numeric_relation": relation,
        "shape": shape,
    }


def _copy_word_timing_envelope(
    value: Any,
    *,
    field: str,
) -> dict[str, Any]:
    mapping = _require_exact_keys(
        value,
        WORD_TIMING_ENVELOPE_KEYS,
        field=field,
    )
    shape = _copy_enum(
        mapping["shape"],
        frozenset(ASR_WORD_TIMING_SHAPES),
        field=f"{field}.shape",
    )
    usable = _copy_boolean(mapping["usable"], field=f"{field}.usable")
    if usable != (shape == "valid"):
        raise ValueError(f"{field}.usable must match the envelope shape")
    start = _copy_boundary(
        mapping["start"],
        field=f"{field}.start",
    )
    end = _copy_boundary(
        mapping["end"],
        field=f"{field}.end",
    )
    relation = _copy_enum(
        mapping["numeric_relation"],
        frozenset(ASR_WORD_TIMING_RELATIONS),
        field=f"{field}.numeric_relation",
    )
    expected_relation, expected_shape = _expected_relation_and_shape(
        start,
        end,
    )
    if relation != expected_relation or shape != expected_shape:
        raise ValueError(f"{field} shape contradicts its raw boundaries")
    return {
        "start_word_index": _copy_nonnegative_int(
            mapping["start_word_index"],
            field=f"{field}.start_word_index",
        ),
        "end_word_index": _copy_nonnegative_int(
            mapping["end_word_index"],
            field=f"{field}.end_word_index",
        ),
        "start": start,
        "end": end,
        "numeric_relation": relation,
        "shape": shape,
        "usable": usable,
    }


def _copy_word_timing_diagnostics(
    value: Any,
    *,
    field: str,
) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    mapping = _require_exact_keys(
        value,
        WORD_TIMING_DIAGNOSTIC_KEYS,
        field=field,
    )
    word_entry_count = _copy_nonnegative_int(
        mapping["word_entry_count"],
        field=f"{field}.word_entry_count",
    )
    no_word_entries = _copy_boolean(
        mapping["no_word_entries"],
        field=f"{field}.no_word_entries",
    )
    if no_word_entries != (word_entry_count == 0):
        raise ValueError(f"{field}.no_word_entries is inconsistent")

    raw_counts = _require_exact_keys(
        mapping["counts"],
        WORD_TIMING_COUNT_GROUP_KEYS,
        field=f"{field}.counts",
    )
    counts = {
        "entry_shape": _copy_counter_map(
            raw_counts["entry_shape"],
            ASR_WORD_TIMING_SHAPES,
            field=f"{field}.counts.entry_shape",
        ),
        "start_numeric_class": _copy_counter_map(
            raw_counts["start_numeric_class"],
            ASR_BOUNDARY_NUMERIC_CLASSES,
            field=f"{field}.counts.start_numeric_class",
        ),
        "end_numeric_class": _copy_counter_map(
            raw_counts["end_numeric_class"],
            ASR_BOUNDARY_NUMERIC_CLASSES,
            field=f"{field}.counts.end_numeric_class",
        ),
        "start_presence": _copy_counter_map(
            raw_counts["start_presence"],
            tuple(sorted(ASR_BOUNDARY_PRESENCES)),
            field=f"{field}.counts.start_presence",
        ),
        "end_presence": _copy_counter_map(
            raw_counts["end_presence"],
            tuple(sorted(ASR_BOUNDARY_PRESENCES)),
            field=f"{field}.counts.end_presence",
        ),
    }
    if any(sum(group.values()) != word_entry_count for group in counts.values()):
        raise ValueError(f"{field}.counts do not cover every word entry")

    raw_anomalies = mapping["anomalies"]
    if not isinstance(raw_anomalies, list):
        raise ValueError(f"{field}.anomalies must be a list")
    anomalies = [
        _copy_word_timing_entry(
            item,
            field=f"{field}.anomalies[{index}]",
        )
        for index, item in enumerate(raw_anomalies)
    ]
    anomaly_indices = [item["word_index"] for item in anomalies]
    if (
        anomaly_indices != sorted(set(anomaly_indices))
        or any(index >= word_entry_count for index in anomaly_indices)
        or len(anomalies) != word_entry_count - counts["entry_shape"]["valid"]
    ):
        raise ValueError(f"{field}.anomalies are inconsistent")
    for shape in ASR_WORD_TIMING_SHAPES:
        if shape == "valid":
            continue
        if (
            sum(item["shape"] == shape for item in anomalies)
            != counts["entry_shape"][shape]
        ):
            raise ValueError(f"{field}.anomaly shapes are inconsistent")
    for counter_name, boundary_name, attribute, categories in (
        (
            "start_numeric_class",
            "start",
            "numeric_class",
            ASR_BOUNDARY_NUMERIC_CLASSES,
        ),
        (
            "end_numeric_class",
            "end",
            "numeric_class",
            ASR_BOUNDARY_NUMERIC_CLASSES,
        ),
        (
            "start_presence",
            "start",
            "presence",
            tuple(sorted(ASR_BOUNDARY_PRESENCES)),
        ),
        (
            "end_presence",
            "end",
            "presence",
            tuple(sorted(ASR_BOUNDARY_PRESENCES)),
        ),
    ):
        anomaly_counts = {
            category: sum(
                item[boundary_name][attribute] == category
                for item in anomalies
            )
            for category in categories
        }
        counter = counts[counter_name]
        if any(
            anomaly_counts[category] > counter[category]
            for category in categories
        ):
            raise ValueError(f"{field}.{counter_name} undercounts anomalies")
        forbidden_for_valid = (
            (
                {
                    "not_available",
                    "unparseable",
                    "nonfinite",
                    "negative",
                }
                | ({"zero"} if boundary_name == "end" else set())
            )
            if attribute == "numeric_class"
            else {"absent"}
        )
        if any(
            counter[category] != anomaly_counts[category]
            for category in forbidden_for_valid
        ):
            raise ValueError(
                f"{field}.{counter_name} has impossible valid residuals"
            )

    if mapping["envelope"] is None:
        envelope = None
    else:
        envelope = _copy_word_timing_envelope(
            mapping["envelope"],
            field=f"{field}.envelope",
        )
    if (envelope is None) != no_word_entries:
        raise ValueError(f"{field}.envelope is inconsistent")
    if envelope is not None and (
        envelope["start_word_index"] != 0
        or envelope["end_word_index"] != word_entry_count - 1
    ):
        raise ValueError(f"{field}.envelope does not span the word entries")
    if envelope is not None and (
        counts["start_numeric_class"][
            envelope["start"]["numeric_class"]
        ]
        == 0
        or counts["end_numeric_class"][
            envelope["end"]["numeric_class"]
        ]
        == 0
        or counts["start_presence"][envelope["start"]["presence"]] == 0
        or counts["end_presence"][envelope["end"]["presence"]] == 0
    ):
        raise ValueError(f"{field}.counts omit an envelope observation")
    return {
        "word_entry_count": word_entry_count,
        "no_word_entries": no_word_entries,
        "envelope": envelope,
        "counts": counts,
        "anomalies": anomalies,
    }


def _copy_final_attribution(
    value: Any,
    *,
    index: int,
) -> dict[str, Any]:
    field = f"attribution.finals[{index}]"
    mapping = _require_exact_keys(
        value,
        FINAL_ATTRIBUTION_KEYS,
        field=field,
    )
    final_id = _copy_nonnegative_int(
        mapping["final_id"],
        field=f"{field}.final_id",
    )
    text_chars = _copy_nonnegative_int(
        mapping["text_chars"],
        field=f"{field}.text_chars",
    )
    if text_chars == 0:
        raise ValueError(f"{field}.text_chars must be positive")
    word_count = _copy_nonnegative_int(
        mapping["word_count"],
        field=f"{field}.word_count",
    )
    first_word_start_ms = _copy_nonnegative_finite(
        mapping["first_word_start_ms"],
        field=f"{field}.first_word_start_ms",
        allow_none=True,
    )
    last_word_end_ms = _copy_nonnegative_finite(
        mapping["last_word_end_ms"],
        field=f"{field}.last_word_end_ms",
        allow_none=True,
    )
    timing_basis = _copy_enum(
        mapping["timing_basis"],
        frozenset(ASR_TIMING_BASES),
        field=f"{field}.timing_basis",
    )
    diagnostics = _copy_word_timing_diagnostics(
        mapping["word_timing_shape_diagnostics"],
        field=f"{field}.word_timing_shape_diagnostics",
    )
    if (
        diagnostics is not None
        and diagnostics["word_entry_count"] != word_count
    ):
        raise ValueError(f"{field} diagnostic word count is inconsistent")
    complete_offsets = (
        first_word_start_ms is not None
        and last_word_end_ms is not None
        and last_word_end_ms > first_word_start_ms
    )
    if timing_basis == "word_offsets":
        envelope = diagnostics["envelope"] if diagnostics is not None else None
        if (
            word_count == 0
            or not complete_offsets
            or envelope is None
            or envelope["shape"] != "valid"
            or envelope["start"].get("finite_value_ms")
            != first_word_start_ms
            or envelope["end"].get("finite_value_ms")
            != last_word_end_ms
        ):
            raise ValueError(f"{field} word-offset evidence is inconsistent")
    elif timing_basis == "incomplete_word_offsets":
        envelope = diagnostics["envelope"] if diagnostics is not None else None
        if (
            word_count == 0
            or complete_offsets
            or envelope is None
            or envelope["shape"] == "valid"
        ):
            raise ValueError(
                f"{field} incomplete word-offset evidence is inconsistent"
            )
        expected_first = (
            envelope["start"].get("finite_value_ms")
            if envelope["start"]["numeric_class"] in {"zero", "positive"}
            else None
        )
        expected_last = (
            envelope["end"].get("finite_value_ms")
            if envelope["end"]["numeric_class"] in {"zero", "positive"}
            else None
        )
        if (
            expected_first is not None
            and expected_last is not None
            and expected_last <= expected_first
        ):
            expected_last = None
        if (
            first_word_start_ms != expected_first
            or last_word_end_ms != expected_last
        ):
            raise ValueError(
                f"{field} normalized offsets contradict the raw envelope"
            )
    elif (
        word_count != 0
        or first_word_start_ms is not None
        or last_word_end_ms is not None
        or (
            diagnostics is not None
            and diagnostics["no_word_entries"] is not True
        )
    ):
        raise ValueError(f"{field} end-only/unavailable evidence is inconsistent")

    return {
        "final_id": final_id,
        "text_chars": text_chars,
        "word_count": word_count,
        "audio_processed_s": _copy_nonnegative_finite(
            mapping["audio_processed_s"],
            field=f"{field}.audio_processed_s",
        ),
        "first_word_start_ms": first_word_start_ms,
        "last_word_end_ms": last_word_end_ms,
        "source_start_ms": _copy_nonnegative_finite(
            mapping["source_start_ms"],
            field=f"{field}.source_start_ms",
            allow_none=True,
        ),
        "source_end_ms": _copy_nonnegative_finite(
            mapping["source_end_ms"],
            field=f"{field}.source_end_ms",
            allow_none=True,
        ),
        "timing_basis": timing_basis,
        "received_monotonic_ms": _copy_nonnegative_finite(
            mapping["received_monotonic_ms"],
            field=f"{field}.received_monotonic_ms",
        ),
        "word_timing_shape_diagnostics": diagnostics,
    }


def _privacy_safe_attribution(value: Any) -> dict[str, Any]:
    mapping = _require_exact_keys(
        value,
        ATTRIBUTION_KEYS,
        field="attribution",
    )
    if _contains_key(value, FORBIDDEN_QUALIFICATION_KEYS):
        raise ValueError("qualification fields are forbidden in a diagnostic")
    if _contains_key(value, FORBIDDEN_ATTRIBUTION_CONTENT_KEYS):
        raise ValueError("content-bearing fields are forbidden in attribution")
    if mapping["schema_version"] != 2:
        raise ValueError("attribution schema_version must be 2")

    raw_finals = mapping["finals"]
    if not isinstance(raw_finals, list):
        raise ValueError("attribution.finals must be a list")
    finals = [
        _copy_final_attribution(item, index=index)
        for index, item in enumerate(raw_finals)
    ]
    final_ids = [item["final_id"] for item in finals]
    if final_ids != list(range(len(finals))):
        raise ValueError(
            "attribution final IDs must be sequential from zero"
        )
    final_id_set = set(final_ids)

    scalar_count_keys = (
        "nonempty_final_count",
        "final_with_word_offsets_count",
        "final_missing_word_offsets_count",
        "word_timing_shape_diagnostic_final_count",
        "word_timing_shape_diagnostic_unavailable_final_count",
        "no_word_entries_final_count",
        "anomalous_word_entry_count",
    )
    scalar_counts = {
        key: _copy_nonnegative_int(
            mapping[key],
            field=f"attribution.{key}",
        )
        for key in scalar_count_keys
    }
    if scalar_counts["nonempty_final_count"] != len(finals):
        raise ValueError("attribution final count does not match finals")
    if (
        scalar_counts["final_with_word_offsets_count"]
        + scalar_counts["final_missing_word_offsets_count"]
        != len(finals)
    ):
        raise ValueError("attribution timing-basis counts are inconsistent")
    if (
        scalar_counts["word_timing_shape_diagnostic_final_count"]
        + scalar_counts[
            "word_timing_shape_diagnostic_unavailable_final_count"
        ]
        != len(finals)
    ):
        raise ValueError("attribution diagnostic counts are inconsistent")

    id_list_keys = (
        "missing_word_offset_final_ids",
        "word_timing_shape_diagnostic_unavailable_final_ids",
        "no_word_entries_final_ids",
    )
    id_lists = {
        key: _copy_id_list(mapping[key], field=f"attribution.{key}")
        for key in id_list_keys
    }
    if any(
        not set(ids).issubset(final_id_set)
        for ids in id_lists.values()
    ):
        raise ValueError("attribution references an unknown final ID")
    if (
        len(id_lists["missing_word_offset_final_ids"])
        != scalar_counts["final_missing_word_offsets_count"]
        or len(
            id_lists["word_timing_shape_diagnostic_unavailable_final_ids"]
        )
        != scalar_counts[
            "word_timing_shape_diagnostic_unavailable_final_count"
        ]
        or len(id_lists["no_word_entries_final_ids"])
        != scalar_counts["no_word_entries_final_count"]
    ):
        raise ValueError("attribution ID lists do not match their counts")

    timing_basis_counts = _copy_counter_map(
        mapping["timing_basis_counts"],
        tuple(sorted(ASR_TIMING_BASES)),
        field="attribution.timing_basis_counts",
    )
    envelope_shape_counts = _copy_counter_map(
        mapping["envelope_shape_counts"],
        ASR_ENVELOPE_SHAPES,
        field="attribution.envelope_shape_counts",
    )
    envelope_shape_final_ids = _copy_id_map(
        mapping["envelope_shape_final_ids"],
        ASR_ENVELOPE_SHAPES,
        field="attribution.envelope_shape_final_ids",
    )
    aggregate_count_maps = {
        "word_entry_shape_counts": _copy_counter_map(
            mapping["word_entry_shape_counts"],
            ASR_WORD_TIMING_SHAPES,
            field="attribution.word_entry_shape_counts",
        ),
        "start_numeric_class_counts": _copy_counter_map(
            mapping["start_numeric_class_counts"],
            ASR_BOUNDARY_NUMERIC_CLASSES,
            field="attribution.start_numeric_class_counts",
        ),
        "end_numeric_class_counts": _copy_counter_map(
            mapping["end_numeric_class_counts"],
            ASR_BOUNDARY_NUMERIC_CLASSES,
            field="attribution.end_numeric_class_counts",
        ),
        "start_presence_counts": _copy_counter_map(
            mapping["start_presence_counts"],
            tuple(sorted(ASR_BOUNDARY_PRESENCES)),
            field="attribution.start_presence_counts",
        ),
        "end_presence_counts": _copy_counter_map(
            mapping["end_presence_counts"],
            tuple(sorted(ASR_BOUNDARY_PRESENCES)),
            field="attribution.end_presence_counts",
        ),
    }
    if sum(timing_basis_counts.values()) != len(finals):
        raise ValueError("attribution timing-basis counters are inconsistent")
    if (
        sum(envelope_shape_counts.values())
        != scalar_counts["word_timing_shape_diagnostic_final_count"]
    ):
        raise ValueError("attribution envelope-shape counters are inconsistent")
    for shape, ids in envelope_shape_final_ids.items():
        if (
            len(ids) != envelope_shape_counts[shape]
            or not set(ids).issubset(final_id_set)
        ):
            raise ValueError(
                "attribution envelope-shape IDs are inconsistent"
            )

    all_have_offsets = _copy_boolean(
        mapping["all_nonempty_finals_have_word_offsets"],
        field="attribution.all_nonempty_finals_have_word_offsets",
    )
    expected_timing_basis_counts = {
        basis: 0
        for basis in sorted(ASR_TIMING_BASES)
    }
    expected_missing_word_offset_ids = []
    expected_diagnostic_unavailable_ids = []
    expected_no_word_entry_ids = []
    expected_envelope_shape_ids = {
        shape: []
        for shape in ASR_ENVELOPE_SHAPES
    }
    expected_aggregate_count_maps = {
        "word_entry_shape_counts": {
            shape: 0
            for shape in ASR_WORD_TIMING_SHAPES
        },
        "start_numeric_class_counts": {
            numeric_class: 0
            for numeric_class in ASR_BOUNDARY_NUMERIC_CLASSES
        },
        "end_numeric_class_counts": {
            numeric_class: 0
            for numeric_class in ASR_BOUNDARY_NUMERIC_CLASSES
        },
        "start_presence_counts": {
            presence: 0
            for presence in sorted(ASR_BOUNDARY_PRESENCES)
        },
        "end_presence_counts": {
            presence: 0
            for presence in sorted(ASR_BOUNDARY_PRESENCES)
        },
    }
    for final in finals:
        final_id = final["final_id"]
        timing_basis = final["timing_basis"]
        expected_timing_basis_counts[timing_basis] += 1
        if timing_basis != "word_offsets":
            expected_missing_word_offset_ids.append(final_id)
        diagnostics = final["word_timing_shape_diagnostics"]
        if diagnostics is None:
            expected_diagnostic_unavailable_ids.append(final_id)
            continue
        if diagnostics["no_word_entries"]:
            envelope_shape = "no_word_entries"
            expected_no_word_entry_ids.append(final_id)
        else:
            envelope_shape = diagnostics["envelope"]["shape"]
        expected_envelope_shape_ids[envelope_shape].append(final_id)
        for target_name, source_name in (
            ("word_entry_shape_counts", "entry_shape"),
            ("start_numeric_class_counts", "start_numeric_class"),
            ("end_numeric_class_counts", "end_numeric_class"),
            ("start_presence_counts", "start_presence"),
            ("end_presence_counts", "end_presence"),
        ):
            for key, count in diagnostics["counts"][source_name].items():
                expected_aggregate_count_maps[target_name][key] += count

    expected_envelope_shape_counts = {
        shape: len(ids)
        for shape, ids in expected_envelope_shape_ids.items()
    }
    expected_anomalous_word_entry_count = sum(
        count
        for shape, count in expected_aggregate_count_maps[
            "word_entry_shape_counts"
        ].items()
        if shape != "valid"
    )
    expected_scalar_counts = {
        "nonempty_final_count": len(finals),
        "final_with_word_offsets_count": (
            len(finals) - len(expected_missing_word_offset_ids)
        ),
        "final_missing_word_offsets_count": (
            len(expected_missing_word_offset_ids)
        ),
        "word_timing_shape_diagnostic_final_count": (
            len(finals) - len(expected_diagnostic_unavailable_ids)
        ),
        "word_timing_shape_diagnostic_unavailable_final_count": (
            len(expected_diagnostic_unavailable_ids)
        ),
        "no_word_entries_final_count": len(expected_no_word_entry_ids),
        "anomalous_word_entry_count": (
            expected_anomalous_word_entry_count
        ),
    }
    expected_id_lists = {
        "missing_word_offset_final_ids": expected_missing_word_offset_ids,
        "word_timing_shape_diagnostic_unavailable_final_ids": (
            expected_diagnostic_unavailable_ids
        ),
        "no_word_entries_final_ids": expected_no_word_entry_ids,
    }
    if scalar_counts != expected_scalar_counts:
        raise ValueError("attribution scalar aggregates contradict finals")
    if id_lists != expected_id_lists:
        raise ValueError("attribution ID aggregates contradict finals")
    if timing_basis_counts != expected_timing_basis_counts:
        raise ValueError("attribution timing-basis aggregates contradict finals")
    if envelope_shape_counts != expected_envelope_shape_counts:
        raise ValueError("attribution envelope aggregates contradict finals")
    if envelope_shape_final_ids != expected_envelope_shape_ids:
        raise ValueError("attribution envelope IDs contradict finals")
    if aggregate_count_maps != expected_aggregate_count_maps:
        raise ValueError("attribution word-timing aggregates contradict finals")
    if all_have_offsets != (
        bool(finals) and not expected_missing_word_offset_ids
    ):
        raise ValueError("attribution offset boolean contradicts finals")

    copied = {
        "schema_version": 2,
        **scalar_counts,
        **id_lists,
        "timing_basis_counts": timing_basis_counts,
        "all_nonempty_finals_have_word_offsets": all_have_offsets,
        "envelope_shape_counts": envelope_shape_counts,
        "envelope_shape_final_ids": envelope_shape_final_ids,
        **aggregate_count_maps,
        "finals": finals,
    }
    json.dumps(copied, allow_nan=False)
    return copied


def _diagnostics_are_present(attribution: Mapping[str, Any]) -> bool:
    final_count = _nonnegative_int(attribution.get("nonempty_final_count"))
    finals = attribution.get("finals")
    if (
        final_count is None
        or final_count <= 0
        or not isinstance(finals, list)
        or len(finals) != final_count
    ):
        return False
    return all(
        isinstance(final, Mapping)
        and isinstance(
            final.get("word_timing_shape_diagnostics"),
            Mapping,
        )
        for final in finals
    )


def _safe_run_record(run: Mapping[str, Any]) -> dict[str, Any]:
    attribution = _privacy_safe_attribution(run.get("attribution"))
    source_wav_digest = run.get("source_wav_sha256")
    if (
        not isinstance(source_wav_digest, str)
        or PCM_SHA256_PATTERN.fullmatch(source_wav_digest) is None
    ):
        source_wav_digest = None
    padded_digest = run.get("padded_pcm_sha256")
    if (
        not isinstance(padded_digest, str)
        or PCM_SHA256_PATTERN.fullmatch(padded_digest) is None
    ):
        padded_digest = None

    source_sample_count = _nonnegative_int(run.get("source_sample_count"))
    padded_sample_count = _nonnegative_int(
        run.get("padded_pcm_sample_count")
    )
    chunk_release_count = _nonnegative_int(run.get("chunk_release_count"))
    wall_seconds = _finite_number(run.get("wall_seconds"))
    audio_seconds_sent = _finite_number(run.get("audio_seconds_sent"))
    maximum_lateness_ms = _finite_number(
        run.get("maximum_chunk_release_lateness_ms")
    )
    mean_lateness_ms = _finite_number(
        run.get("mean_chunk_release_lateness_ms")
    )
    input_completed = run.get("input_completed") is True
    realtime_pacing = run.get("realtime_pacing") is True
    pacing_basis = (
        "absolute_chunk_end_deadlines_v1"
        if run.get("pacing_basis") == "absolute_chunk_end_deadlines_v1"
        else "unverified"
    )
    exact_pcm_binding = (
        source_wav_digest is not None
        and padded_digest is not None
        and source_sample_count is not None
        and padded_sample_count is not None
        and source_sample_count > 0
        and padded_sample_count >= source_sample_count
    )
    pacing_integrity = (
        realtime_pacing
        and chunk_release_count is not None
        and chunk_release_count > 0
        and maximum_lateness_ms is not None
        and 0 <= maximum_lateness_ms <= MAX_CHUNK_RELEASE_LATENESS_MS
        and mean_lateness_ms is not None
        and mean_lateness_ms >= 0
        and pacing_basis == "absolute_chunk_end_deadlines_v1"
    )
    capture_complete = (
        input_completed
        and exact_pcm_binding
        and pacing_integrity
        and wall_seconds is not None
        and wall_seconds >= 0
        and audio_seconds_sent is not None
        and audio_seconds_sent > 0
        and _diagnostics_are_present(attribution)
    )

    started_at_utc = _copy_utc_timestamp(
        run.get("started_at_utc"),
        field="run.started_at_utc",
    )
    return {
        "run_number": 1,
        "started_at_utc": started_at_utc,
        "wall_seconds": wall_seconds,
        "audio_seconds_sent": audio_seconds_sent,
        "source_sample_count": source_sample_count,
        "source_wav_sha256": source_wav_digest,
        "padded_pcm_sample_count": padded_sample_count,
        "padded_pcm_sha256": padded_digest,
        "input_completed": input_completed,
        "realtime_pacing": realtime_pacing,
        "pacing_basis": pacing_basis,
        "chunk_release_count": chunk_release_count,
        "maximum_chunk_release_lateness_ms": maximum_lateness_ms,
        "mean_chunk_release_lateness_ms": mean_lateness_ms,
        "interim_count": _nonnegative_int(run.get("interim_count")),
        "attribution": attribution,
        "capture_complete": capture_complete,
    }


def build_report(
    *,
    audio_path: Path,
    uri: str,
    run: Optional[Mapping[str, Any]],
    runtime_attestation_before: Optional[Mapping[str, Any]],
    runtime_attestation_after: Optional[Mapping[str, Any]],
    input_wav_sha256_before: Optional[str],
    attempt_id: str,
    attempt_started_at_utc: str,
    failure_reason: Optional[str] = None,
) -> dict[str, Any]:
    safe_run = _safe_run_record(run) if run is not None else None
    before = _safe_attestation(runtime_attestation_before)
    after = _safe_attestation(runtime_attestation_after)
    runtime_identity_stable = _runtime_identity_is_stable(before, after)
    expected_input_binding = _compute_exact_wav_pcm_binding(audio_path)
    input_wav_sha256_after = expected_input_binding["wav_sha256"]
    capture_wav_sha256 = (
        safe_run.get("source_wav_sha256")
        if safe_run is not None
        else None
    )
    exact_source_wav_binding = (
        isinstance(input_wav_sha256_before, str)
        and PCM_SHA256_PATTERN.fullmatch(input_wav_sha256_before) is not None
        and input_wav_sha256_before == capture_wav_sha256
        and capture_wav_sha256 == input_wav_sha256_after
    )
    exact_padded_pcm_binding = (
        safe_run is not None
        and safe_run["source_sample_count"]
        == expected_input_binding["source_sample_count"]
        and safe_run["padded_pcm_sample_count"]
        == expected_input_binding["padded_pcm_sample_count"]
        and safe_run["padded_pcm_sha256"]
        == expected_input_binding["padded_pcm_sha256"]
    )
    registered_asr_config = (
        riva_config.source_language == REGISTERED_SOURCE_LANGUAGE
        and riva_config.endpointing_history_ms == REGISTERED_EOU_MS
        and riva_config.asr_word_time_offsets is True
    )
    declared_image_digest = _safe_prefixed_sha256(
        riva_config.asr_image_digest
    )
    declared_image_is_attested = (
        declared_image_digest is not None
        and before.get("repository_digest") == declared_image_digest
        and after.get("repository_digest") == declared_image_digest
    )
    endpoint_configured = bool(uri)
    safe_language = (
        REGISTERED_SOURCE_LANGUAGE
        if riva_config.source_language == REGISTERED_SOURCE_LANGUAGE
        else "unregistered"
    )
    safe_eou_ms = _nonnegative_int(riva_config.endpointing_history_ms)
    word_time_offsets_requested = (
        riva_config.asr_word_time_offsets is True
    )
    complete = (
        failure_reason is None
        and safe_run is not None
        and safe_run["capture_complete"] is True
        and exact_source_wav_binding
        and exact_padded_pcm_binding
        and runtime_identity_stable
        and registered_asr_config
        and declared_image_is_attested
        and endpoint_configured
    )
    if not complete and failure_reason is None:
        if before.get("verified") is not True:
            failure_reason = "pre_capture_attestation_failed"
        elif safe_run is None:
            failure_reason = "capture_not_completed"
        elif safe_run["capture_complete"] is not True:
            failure_reason = "capture_integrity_failed"
        elif not exact_source_wav_binding:
            failure_reason = "input_identity_changed"
        elif not exact_padded_pcm_binding:
            failure_reason = "padded_pcm_binding_mismatch"
        elif not registered_asr_config:
            failure_reason = "asr_configuration_mismatch"
        elif not endpoint_configured:
            failure_reason = "endpoint_not_configured"
        elif after.get("verified") is not True:
            failure_reason = "post_capture_attestation_failed"
        elif not declared_image_is_attested:
            failure_reason = "runtime_image_binding_mismatch"
        else:
            failure_reason = "runtime_identity_changed"

    report: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": ARTIFACT_KIND,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "attempt_id": attempt_id,
        "attempt_started_at_utc": attempt_started_at_utc,
        "diagnostic_only": True,
        "qualification_eligible": False,
        "qualification_status": QUALIFICATION_STATUS,
        "completion_status": "complete" if complete else "failed",
        "requested_run_count": REQUESTED_RUN_COUNT,
        "completed_run_count": 1 if safe_run is not None else 0,
        "capture_complete": complete,
        "requirements": _requirements_record(),
        "input": {
            "pcm_preparation_basis": (
                "riff_pcm16le_passthrough_zero_pad_v1"
            ),
            "wav_sha256_before": input_wav_sha256_before,
            "capture_wav_sha256": capture_wav_sha256,
            "wav_sha256_after": input_wav_sha256_after,
            "source_sample_count": (
                expected_input_binding["source_sample_count"]
            ),
            "exact_source_wav_binding_verified": (
                exact_source_wav_binding
            ),
            "padded_pcm_sha256": (
                safe_run["padded_pcm_sha256"]
                if safe_run is not None
                else None
            ),
            "padded_pcm_sample_count": (
                safe_run["padded_pcm_sample_count"]
                if safe_run is not None
                else None
            ),
            "exact_padded_pcm_binding_verified": (
                exact_padded_pcm_binding
            ),
        },
        "asr": {
            "endpoint_configured": endpoint_configured,
            "declared_image_digest": declared_image_digest,
            "runtime_attestation": {
                "before": before,
                "after": after,
                "identity_stable": runtime_identity_stable,
            },
            "word_time_offsets_requested": word_time_offsets_requested,
            "language": safe_language,
            "eou_ms": safe_eou_ms,
            "registered_configuration_verified": registered_asr_config,
        },
        "runs": [safe_run] if safe_run is not None else [],
    }
    if failure_reason is not None:
        report["failure_reason"] = failure_reason
    _validate_report(report)
    return report


def _initial_report(
    *,
    attempt_id: str,
    attempt_started_at_utc: str,
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "artifact_kind": ARTIFACT_KIND,
        "generated_at_utc": attempt_started_at_utc,
        "attempt_id": attempt_id,
        "attempt_started_at_utc": attempt_started_at_utc,
        "diagnostic_only": True,
        "qualification_eligible": False,
        "qualification_status": QUALIFICATION_STATUS,
        "completion_status": "failed",
        "requested_run_count": REQUESTED_RUN_COUNT,
        "completed_run_count": 0,
        "capture_complete": False,
        "failure_reason": "initializing_or_interrupted",
        "runs": [],
    }
    _validate_report(report)
    return report


def _validate_common_report_fields(report: Mapping[str, Any]) -> None:
    generated_at = _copy_utc_timestamp(
        report.get("generated_at_utc"),
        field="generated_at_utc",
    )
    attempt_started_at = _copy_utc_timestamp(
        report.get("attempt_started_at_utc"),
        field="attempt_started_at_utc",
    )
    attempt_id = report.get("attempt_id")
    completed_run_count = report.get("completed_run_count")
    if (
        report.get("schema_version") != 1
        or report.get("artifact_kind") != ARTIFACT_KIND
        or report.get("diagnostic_only") is not True
        or report.get("qualification_eligible") is not False
        or report.get("qualification_status") != QUALIFICATION_STATUS
        or report.get("completion_status") not in {"complete", "failed"}
        or report.get("requested_run_count") != REQUESTED_RUN_COUNT
        or isinstance(completed_run_count, bool)
        or completed_run_count not in {0, 1}
        or not isinstance(report.get("capture_complete"), bool)
        or not isinstance(attempt_id, str)
        or ATTEMPT_ID_PATTERN.fullmatch(attempt_id) is None
        or generated_at < attempt_started_at
    ):
        raise ValueError("diagnostic report does not match its outer schema")


def _validate_requirements(value: Any) -> None:
    requirements = _require_exact_keys(
        value,
        REQUIREMENTS_KEYS,
        field="requirements",
    )
    expected = _requirements_record()
    boolean_keys = REQUIREMENTS_KEYS - frozenset(
        {
            "source_language_required",
            "eou_ms_required",
            "maximum_chunk_release_lateness_ms",
        }
    )
    if (
        any(requirements[key] is not True for key in boolean_keys)
        or requirements["source_language_required"]
        != expected["source_language_required"]
        or requirements["eou_ms_required"] != expected["eou_ms_required"]
        or _finite_number(
            requirements["maximum_chunk_release_lateness_ms"]
        )
        != expected["maximum_chunk_release_lateness_ms"]
    ):
        raise ValueError("requirements do not match the registered diagnostic")


def _validate_safe_run(value: Any) -> dict[str, Any]:
    run = _require_exact_keys(value, SAFE_RUN_KEYS, field="run")
    if (
        isinstance(run["run_number"], bool)
        or run["run_number"] != 1
        or not isinstance(run["input_completed"], bool)
        or not isinstance(run["realtime_pacing"], bool)
        or not isinstance(run["capture_complete"], bool)
        or _nonnegative_int(run["interim_count"]) is None
    ):
        raise ValueError("run does not match the diagnostic schema")
    normalized = _safe_run_record(run)
    if normalized != dict(run):
        raise ValueError("run does not match the diagnostic schema")
    return normalized


def _optional_pcm_digest(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or PCM_SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a SHA-256 digest or null")
    return value


def _validate_input_record(
    value: Any,
    runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    input_record = _require_exact_keys(value, INPUT_KEYS, field="input")
    wav_before = _optional_pcm_digest(
        input_record["wav_sha256_before"],
        field="input.wav_sha256_before",
    )
    capture_wav = _optional_pcm_digest(
        input_record["capture_wav_sha256"],
        field="input.capture_wav_sha256",
    )
    wav_after = _optional_pcm_digest(
        input_record["wav_sha256_after"],
        field="input.wav_sha256_after",
    )
    padded_digest = _optional_pcm_digest(
        input_record["padded_pcm_sha256"],
        field="input.padded_pcm_sha256",
    )
    source_sample_count = _copy_nonnegative_int(
        input_record["source_sample_count"],
        field="input.source_sample_count",
    )
    padded_sample_count = input_record["padded_pcm_sample_count"]
    if padded_sample_count is not None:
        padded_sample_count = _copy_nonnegative_int(
            padded_sample_count,
            field="input.padded_pcm_sample_count",
        )
    exact_source = _copy_boolean(
        input_record["exact_source_wav_binding_verified"],
        field="input.exact_source_wav_binding_verified",
    )
    exact_padded = _copy_boolean(
        input_record["exact_padded_pcm_binding_verified"],
        field="input.exact_padded_pcm_binding_verified",
    )
    if input_record["pcm_preparation_basis"] != (
        "riff_pcm16le_passthrough_zero_pad_v1"
    ):
        raise ValueError("input PCM preparation basis is unregistered")

    run = runs[0] if runs else None
    expected_capture_wav = (
        run["source_wav_sha256"] if run is not None else None
    )
    expected_padded_digest = (
        run["padded_pcm_sha256"] if run is not None else None
    )
    expected_padded_count = (
        run["padded_pcm_sample_count"] if run is not None else None
    )
    expected_exact_source = (
        wav_before is not None
        and wav_before == capture_wav
        and capture_wav == wav_after
    )
    padded_fields_are_consistent = (
        run is not None
        and run["source_sample_count"] == source_sample_count
        and padded_digest is not None
        and padded_sample_count is not None
        and padded_digest == expected_padded_digest
        and padded_sample_count == expected_padded_count
    )
    if (
        capture_wav != expected_capture_wav
        or padded_digest != expected_padded_digest
        or padded_sample_count != expected_padded_count
        or exact_source is not expected_exact_source
        or (exact_padded and not padded_fields_are_consistent)
    ):
        raise ValueError("input binding fields contradict the run")
    return dict(input_record)


def _validate_asr_record(value: Any) -> dict[str, Any]:
    asr = _require_exact_keys(value, ASR_KEYS, field="asr")
    endpoint_configured = _copy_boolean(
        asr["endpoint_configured"],
        field="asr.endpoint_configured",
    )
    declared_digest = asr["declared_image_digest"]
    if declared_digest is not None:
        declared_digest = _safe_prefixed_sha256(declared_digest)
        if declared_digest is None:
            raise ValueError("asr.declared_image_digest is invalid")
    runtime = _require_exact_keys(
        asr["runtime_attestation"],
        RUNTIME_ATTESTATION_PAIR_KEYS,
        field="asr.runtime_attestation",
    )
    before = _safe_attestation(runtime["before"])
    after = _safe_attestation(runtime["after"])
    identity_stable = _copy_boolean(
        runtime["identity_stable"],
        field="asr.runtime_attestation.identity_stable",
    )
    if identity_stable is not _runtime_identity_is_stable(before, after):
        raise ValueError("ASR runtime identity fields contradict attestations")
    word_times = _copy_boolean(
        asr["word_time_offsets_requested"],
        field="asr.word_time_offsets_requested",
    )
    language = _copy_enum(
        asr["language"],
        frozenset({REGISTERED_SOURCE_LANGUAGE, "unregistered"}),
        field="asr.language",
    )
    eou_ms = asr["eou_ms"]
    if eou_ms is not None:
        eou_ms = _copy_nonnegative_int(eou_ms, field="asr.eou_ms")
    registered = _copy_boolean(
        asr["registered_configuration_verified"],
        field="asr.registered_configuration_verified",
    )
    expected_registered = (
        word_times
        and language == REGISTERED_SOURCE_LANGUAGE
        and eou_ms == REGISTERED_EOU_MS
    )
    if registered is not expected_registered:
        raise ValueError("ASR registered-configuration fields contradict")
    return {
        "endpoint_configured": endpoint_configured,
        "declared_image_digest": declared_digest,
        "runtime_attestation": {
            "before": before,
            "after": after,
            "identity_stable": identity_stable,
        },
        "word_time_offsets_requested": word_times,
        "language": language,
        "eou_ms": eou_ms,
        "registered_configuration_verified": registered,
    }


def _validate_initial_report(report: Mapping[str, Any]) -> None:
    _require_exact_keys(
        report,
        INITIAL_REPORT_KEYS,
        field="initial report",
    )
    _validate_common_report_fields(report)
    if (
        report["completion_status"] != "failed"
        or report["capture_complete"] is not False
        or report["completed_run_count"] != 0
        or report["failure_reason"] not in INITIAL_FAILURE_REASONS
        or report["runs"] != []
    ):
        raise ValueError("initial report does not match its checkpoint schema")


def _validate_full_report(report: Mapping[str, Any]) -> None:
    expected_keys = FULL_REPORT_KEYS
    if report.get("completion_status") == "failed":
        expected_keys |= frozenset({"failure_reason"})
    _require_exact_keys(report, expected_keys, field="full report")
    _validate_common_report_fields(report)
    _validate_requirements(report["requirements"])
    if not isinstance(report["runs"], list):
        raise ValueError("runs must be a list")
    runs = [_validate_safe_run(run) for run in report["runs"]]
    if len(runs) != report["completed_run_count"]:
        raise ValueError("completed run count contradicts runs")
    input_record = _validate_input_record(report["input"], runs)
    asr = _validate_asr_record(report["asr"])

    before = asr["runtime_attestation"]["before"]
    after = asr["runtime_attestation"]["after"]
    declared_digest = asr["declared_image_digest"]
    image_is_attested = (
        declared_digest is not None
        and before.get("repository_digest") == declared_digest
        and after.get("repository_digest") == declared_digest
    )
    complete_evidence = (
        len(runs) == 1
        and runs[0]["capture_complete"] is True
        and input_record["exact_source_wav_binding_verified"] is True
        and input_record["exact_padded_pcm_binding_verified"] is True
        and asr["endpoint_configured"] is True
        and asr["registered_configuration_verified"] is True
        and asr["runtime_attestation"]["identity_stable"] is True
        and image_is_attested
    )
    if report["completion_status"] == "complete":
        if (
            report["capture_complete"] is not True
            or not complete_evidence
        ):
            raise ValueError("complete report lacks complete evidence")
    elif (
        report["capture_complete"] is not False
        or report["failure_reason"] not in FULL_FAILURE_REASONS
    ):
        raise ValueError("failed report does not match its checkpoint schema")


def _validate_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping):
        raise ValueError("diagnostic report must be an object")
    if _contains_key(report, FORBIDDEN_QUALIFICATION_KEYS):
        raise ValueError("diagnostic report contains qualification fields")
    report_keys = set(report)
    if report_keys == INITIAL_REPORT_KEYS:
        _validate_initial_report(report)
    elif report_keys in {
        FULL_REPORT_KEYS,
        FULL_REPORT_KEYS | frozenset({"failure_reason"}),
    }:
        _validate_full_report(report)
    else:
        raise ValueError("diagnostic report does not match its outer schema")
    json.dumps(report, allow_nan=False)


def _write_diagnostic_report(path: Path, report: dict[str, Any]) -> None:
    _validate_report(report)
    _write_report(path, report)


def _write_initial_failure(
    output_path: Optional[Path],
    initial_report: Mapping[str, Any],
    failure_reason: str,
) -> None:
    if output_path is None:
        return
    report = dict(initial_report)
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["failure_reason"] = failure_reason
    _write_diagnostic_report(output_path, report)


def _is_formal_artifact(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(document, Mapping) and _contains_key(
        document,
        frozenset({"gate"}),
    )


def _paths_refer_to_same_file(first: Path, second: Path) -> bool:
    if first == second:
        return True
    if not first.exists() or not second.exists():
        return False
    try:
        return first.samefile(second)
    except OSError:
        return False


def _emit_report(
    report: dict[str, Any],
    *,
    output_path: Optional[Path],
) -> None:
    encoded = json.dumps(
        report,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    if output_path is not None:
        _write_diagnostic_report(output_path, report)
    print(encoded, end="")


def _last_cli_option_value(
    arguments: Sequence[str],
    option: str,
) -> Optional[str]:
    value = None
    for index, argument in enumerate(arguments):
        if argument.startswith(f"{option}="):
            value = argument.split("=", 1)[1]
        elif (
            argument == option
            and index + 1 < len(arguments)
            and not arguments[index + 1].startswith("--")
        ):
            value = arguments[index + 1]
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_arguments = list(argv) if argv is not None else sys.argv[1:]
    attempt_id = uuid.uuid4().hex
    attempt_started_at_utc = datetime.now(timezone.utc).isoformat()
    initial_report = _initial_report(
        attempt_id=attempt_id,
        attempt_started_at_utc=attempt_started_at_utc,
    )
    preinitialized_output_path = None
    if "-h" not in raw_arguments and "--help" not in raw_arguments:
        raw_output = _last_cli_option_value(
            raw_arguments,
            "--json-output",
        )
        raw_audio = _last_cli_option_value(raw_arguments, "--file")
        if raw_output:
            candidate_output = Path(raw_output).resolve()
            candidate_audio = (
                Path(raw_audio).resolve()
                if raw_audio
                else (ROOT / "test_audio" / "long-form-03-30min.wav").resolve()
            )
            if (
                not _paths_refer_to_same_file(
                    candidate_output,
                    candidate_audio,
                )
                and not _is_formal_artifact(candidate_output)
            ):
                _write_diagnostic_report(
                    candidate_output,
                    initial_report,
                )
                preinitialized_output_path = candidate_output

    args = build_parser().parse_args(raw_arguments)
    audio_path = args.file.resolve()
    output_path = (
        args.json_output.resolve()
        if args.json_output is not None
        else None
    )
    if (
        output_path is not None
        and _paths_refer_to_same_file(output_path, audio_path)
    ):
        print(
            "--json-output must not overwrite the input WAV",
            file=sys.stderr,
        )
        return 2
    if output_path is not None and _is_formal_artifact(output_path):
        print(
            "--json-output must not overwrite formal qualification evidence",
            file=sys.stderr,
        )
        return 2

    # Invalidate stale diagnostic output before input validation, Docker
    # inspection, or a long replay. A VM interruption therefore leaves a
    # current, explicitly failed attempt instead of stale completed evidence.
    if (
        output_path is not None
        and output_path != preinitialized_output_path
    ):
        _write_diagnostic_report(output_path, initial_report)

    if not audio_path.is_file():
        _write_initial_failure(
            output_path,
            initial_report,
            "input_not_found",
        )
        print("Audio file not found", file=sys.stderr)
        return 2
    if args.progress_seconds <= 0:
        _write_initial_failure(
            output_path,
            initial_report,
            "invalid_progress_interval",
        )
        print("--progress-seconds must be positive", file=sys.stderr)
        return 2
    if not riva_config.asr_word_time_offsets:
        _write_initial_failure(
            output_path,
            initial_report,
            "word_time_offsets_not_enabled",
        )
        print(
            "RIVA_ASR_WORD_TIMES=1 is required for this diagnostic",
            file=sys.stderr,
        )
        return 2
    if (
        riva_config.source_language != REGISTERED_SOURCE_LANGUAGE
        or riva_config.endpointing_history_ms != REGISTERED_EOU_MS
    ):
        _write_initial_failure(
            output_path,
            initial_report,
            "asr_configuration_mismatch",
        )
        print(
            "registered en-US / 800 ms ASR configuration is required",
            file=sys.stderr,
        )
        return 2
    try:
        input_wav_sha256_before = _compute_exact_wav_pcm_binding(
            audio_path
        )["wav_sha256"]
    except ValueError:
        _write_initial_failure(
            output_path,
            initial_report,
            "invalid_input_wav",
        )
        print(
            "input WAV does not match the exact PCM contract",
            file=sys.stderr,
        )
        return 2

    before: Mapping[str, Any] = {
        "schema_version": 2,
        "verified": False,
        "status": "pending",
    }
    after: Mapping[str, Any] = {
        "schema_version": 2,
        "verified": False,
        "status": "pending",
    }
    checkpoint = build_report(
        audio_path=audio_path,
        uri=args.uri,
        run=None,
        runtime_attestation_before=before,
        runtime_attestation_after=after,
        input_wav_sha256_before=input_wav_sha256_before,
        attempt_id=attempt_id,
        attempt_started_at_utc=attempt_started_at_utc,
        failure_reason="pre_capture_attestation_pending",
    )
    if output_path is not None:
        _write_diagnostic_report(output_path, checkpoint)

    try:
        before = attest_local_asr_runtime(
            container_name=args.docker_container,
            uri=args.uri,
        )
    except Exception as exc:
        before = {
            "schema_version": 2,
            "verified": False,
            "failure": "attestation_failed",
        }
        report = build_report(
            audio_path=audio_path,
            uri=args.uri,
            run=None,
            runtime_attestation_before=before,
            runtime_attestation_after=after,
            input_wav_sha256_before=input_wav_sha256_before,
            attempt_id=attempt_id,
            attempt_started_at_utc=attempt_started_at_utc,
            failure_reason="pre_capture_attestation_failed",
        )
        _emit_report(report, output_path=output_path)
        print(
            "ASR runtime attestation failed "
            f"({type(exc).__name__}); NOT QUALIFICATION EVIDENCE",
            file=sys.stderr,
        )
        return 2

    checkpoint = build_report(
        audio_path=audio_path,
        uri=args.uri,
        run=None,
        runtime_attestation_before=before,
        runtime_attestation_after=after,
        input_wav_sha256_before=input_wav_sha256_before,
        attempt_id=attempt_id,
        attempt_started_at_utc=attempt_started_at_utc,
        failure_reason="capture_pending_or_interrupted",
    )
    if output_path is not None:
        _write_diagnostic_report(output_path, checkpoint)

    print(
        "starting one real-time ASR word-timing-shape diagnostic",
        file=sys.stderr,
        flush=True,
    )
    try:
        raw_run = run_once(
            audio_path=audio_path,
            uri=args.uri,
            run_number=1,
            progress_interval_s=args.progress_seconds,
        )
        safe_run = _safe_run_record(raw_run)
    except Exception as exc:
        report = build_report(
            audio_path=audio_path,
            uri=args.uri,
            run=None,
            runtime_attestation_before=before,
            runtime_attestation_after=after,
            input_wav_sha256_before=input_wav_sha256_before,
            attempt_id=attempt_id,
            attempt_started_at_utc=attempt_started_at_utc,
            failure_reason="capture_failed",
        )
        _emit_report(report, output_path=output_path)
        print(
            f"DIAGNOSTIC FAILED ({type(exc).__name__}) "
            "— NOT QUALIFICATION EVIDENCE",
            file=sys.stderr,
        )
        return 1

    # Checkpoint the completed capture before the second Docker inspection.
    checkpoint = build_report(
        audio_path=audio_path,
        uri=args.uri,
        run=safe_run,
        runtime_attestation_before=before,
        runtime_attestation_after=after,
        input_wav_sha256_before=input_wav_sha256_before,
        attempt_id=attempt_id,
        attempt_started_at_utc=attempt_started_at_utc,
        failure_reason="post_capture_attestation_pending",
    )
    if output_path is not None:
        _write_diagnostic_report(output_path, checkpoint)

    try:
        after = attest_local_asr_runtime(
            container_name=args.docker_container,
            uri=args.uri,
        )
        failure_reason = (
            None if after == before else "runtime_identity_changed"
        )
    except Exception as exc:
        after = {
            "schema_version": 2,
            "verified": False,
            "failure": "attestation_failed",
        }
        failure_reason = "post_capture_attestation_failed"
        print(
            "post-capture ASR runtime attestation failed "
            f"({type(exc).__name__})",
            file=sys.stderr,
        )

    report = build_report(
        audio_path=audio_path,
        uri=args.uri,
        run=safe_run,
        runtime_attestation_before=before,
        runtime_attestation_after=after,
        input_wav_sha256_before=input_wav_sha256_before,
        attempt_id=attempt_id,
        attempt_started_at_utc=attempt_started_at_utc,
        failure_reason=failure_reason,
    )
    _emit_report(report, output_path=output_path)
    if report["completion_status"] == "complete":
        print(
            "DIAGNOSTIC COMPLETE — NOT QUALIFICATION EVIDENCE",
            file=sys.stderr,
        )
        return 0
    print(
        "DIAGNOSTIC FAILED — NOT QUALIFICATION EVIDENCE",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
