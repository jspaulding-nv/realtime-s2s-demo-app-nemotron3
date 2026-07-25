"""Privacy-safe diagnostics for finalized ASR source attribution."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from typing import Any, Dict, Iterable

from staged_models import (
    ASR_BOUNDARY_NUMERIC_CLASSES,
    ASR_BOUNDARY_PRESENCES,
    ASR_TIMING_BASES,
    ASR_TIMING_BASIS_WORD_OFFSETS,
    ASR_WORD_TIMING_RELATIONS,
    ASR_WORD_TIMING_SHAPES,
    AsrFinal,
)

ASR_ENVELOPE_SHAPES = ("no_word_entries", *ASR_WORD_TIMING_SHAPES)
FINAL_ATTRIBUTION_RECORD_KEYS = (
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
)


def final_attribution_record(final: AsrFinal) -> Dict[str, Any]:
    """Return final metadata without copying transcript content."""
    record = {
        "final_id": final.final_id,
        "text_chars": len(final.text),
        "word_count": final.word_count,
        "audio_processed_s": final.audio_processed_s,
        "first_word_start_ms": final.first_word_start_ms,
        "last_word_end_ms": final.last_word_end_ms,
        "source_start_ms": final.source_start_ms,
        "source_end_ms": final.source_end_ms,
        "timing_basis": final.timing_basis,
        "received_monotonic_ms": final.received_monotonic_ms,
        "word_timing_shape_diagnostics": (
            final.word_timing_shape_diagnostics.to_dict()
            if final.word_timing_shape_diagnostics is not None
            else None
        ),
    }
    return record


def summarize_final_attribution(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build deterministic counters for one ASR stream's nonempty finals."""
    # Copy only the explicit privacy-safe schema. An accidental caller field
    # such as transcript text must never flow into retained telemetry.
    copied = []
    for record in records:
        copied_record = {
            key: record.get(key)
            for key in FINAL_ATTRIBUTION_RECORD_KEYS
            if key != "word_timing_shape_diagnostics"
        }
        copied_record["word_timing_shape_diagnostics"] = (
            _copy_word_timing_shape_diagnostics(
                record.get("word_timing_shape_diagnostics")
            )
        )
        copied.append(copied_record)
    timing_counts = Counter(
        str(record.get("timing_basis", "")) for record in copied
    )
    missing_word_offset_ids = [
        int(record["final_id"])
        for record in copied
        if record.get("timing_basis") != ASR_TIMING_BASIS_WORD_OFFSETS
    ]
    diagnostic_unavailable_ids = [
        int(record["final_id"])
        for record in copied
        if record["word_timing_shape_diagnostics"] is None
    ]
    no_word_entry_ids = []
    envelope_shape_ids = {
        shape: []
        for shape in ASR_ENVELOPE_SHAPES
    }
    word_entry_shape_counts: Counter[str] = Counter()
    start_numeric_class_counts: Counter[str] = Counter()
    end_numeric_class_counts: Counter[str] = Counter()
    start_presence_counts: Counter[str] = Counter()
    end_presence_counts: Counter[str] = Counter()
    for record in copied:
        diagnostics = record["word_timing_shape_diagnostics"]
        if diagnostics is None:
            continue
        final_id = int(record["final_id"])
        if diagnostics["no_word_entries"]:
            envelope_shape = "no_word_entries"
            no_word_entry_ids.append(final_id)
        else:
            envelope_shape = diagnostics["envelope"]["shape"]
        envelope_shape_ids[envelope_shape].append(final_id)
        counts = diagnostics["counts"]
        word_entry_shape_counts.update(counts["entry_shape"])
        start_numeric_class_counts.update(counts["start_numeric_class"])
        end_numeric_class_counts.update(counts["end_numeric_class"])
        start_presence_counts.update(counts["start_presence"])
        end_presence_counts.update(counts["end_presence"])

    final_count = len(copied)
    return {
        "schema_version": 2,
        "nonempty_final_count": final_count,
        "final_with_word_offsets_count": (
            final_count - len(missing_word_offset_ids)
        ),
        "final_missing_word_offsets_count": len(missing_word_offset_ids),
        "missing_word_offset_final_ids": missing_word_offset_ids,
        "timing_basis_counts": {
            basis: timing_counts.get(basis, 0)
            for basis in sorted(ASR_TIMING_BASES)
        },
        "all_nonempty_finals_have_word_offsets": (
            final_count > 0 and not missing_word_offset_ids
        ),
        "word_timing_shape_diagnostic_final_count": (
            final_count - len(diagnostic_unavailable_ids)
        ),
        "word_timing_shape_diagnostic_unavailable_final_count": (
            len(diagnostic_unavailable_ids)
        ),
        "word_timing_shape_diagnostic_unavailable_final_ids": (
            diagnostic_unavailable_ids
        ),
        "no_word_entries_final_count": len(no_word_entry_ids),
        "no_word_entries_final_ids": no_word_entry_ids,
        "envelope_shape_counts": {
            shape: len(envelope_shape_ids[shape])
            for shape in ASR_ENVELOPE_SHAPES
        },
        "envelope_shape_final_ids": envelope_shape_ids,
        "word_entry_shape_counts": {
            shape: word_entry_shape_counts[shape]
            for shape in ASR_WORD_TIMING_SHAPES
        },
        "start_numeric_class_counts": {
            numeric_class: start_numeric_class_counts[numeric_class]
            for numeric_class in ASR_BOUNDARY_NUMERIC_CLASSES
        },
        "end_numeric_class_counts": {
            numeric_class: end_numeric_class_counts[numeric_class]
            for numeric_class in ASR_BOUNDARY_NUMERIC_CLASSES
        },
        "start_presence_counts": {
            presence: start_presence_counts[presence]
            for presence in sorted(ASR_BOUNDARY_PRESENCES)
        },
        "end_presence_counts": {
            presence: end_presence_counts[presence]
            for presence in sorted(ASR_BOUNDARY_PRESENCES)
        },
        "anomalous_word_entry_count": sum(
            word_entry_shape_counts[shape]
            for shape in ASR_WORD_TIMING_SHAPES
            if shape != "valid"
        ),
        "finals": copied,
    }


def _copy_word_timing_shape_diagnostics(
    value: object,
) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    word_entry_count = _copy_nonnegative_int(value.get("word_entry_count"))
    no_word_entries = value.get("no_word_entries")
    if word_entry_count is None or not isinstance(no_word_entries, bool):
        return None
    raw_counts = value.get("counts")
    if not isinstance(raw_counts, Mapping):
        return None
    counts = {
        "entry_shape": _copy_counter_map(
            raw_counts.get("entry_shape"),
            ASR_WORD_TIMING_SHAPES,
        ),
        "start_numeric_class": _copy_counter_map(
            raw_counts.get("start_numeric_class"),
            ASR_BOUNDARY_NUMERIC_CLASSES,
        ),
        "end_numeric_class": _copy_counter_map(
            raw_counts.get("end_numeric_class"),
            ASR_BOUNDARY_NUMERIC_CLASSES,
        ),
        "start_presence": _copy_counter_map(
            raw_counts.get("start_presence"),
            sorted(ASR_BOUNDARY_PRESENCES),
        ),
        "end_presence": _copy_counter_map(
            raw_counts.get("end_presence"),
            sorted(ASR_BOUNDARY_PRESENCES),
        ),
    }
    if any(counter is None for counter in counts.values()):
        return None
    raw_anomalies = value.get("anomalies")
    if not isinstance(raw_anomalies, (list, tuple)):
        return None
    anomalies = []
    for raw_anomaly in raw_anomalies:
        anomaly = _copy_word_timing_entry(raw_anomaly)
        if anomaly is None:
            return None
        anomalies.append(anomaly)
    envelope = _copy_word_timing_envelope(value.get("envelope"))
    if value.get("envelope") is not None and envelope is None:
        return None
    if no_word_entries != (word_entry_count == 0):
        return None
    if (envelope is None) != no_word_entries:
        return None
    if envelope is not None and (
        envelope["start_word_index"] != 0
        or envelope["end_word_index"] != word_entry_count - 1
    ):
        return None
    if any(
        sum(counter.values()) != word_entry_count
        for counter in counts.values()
    ):
        return None
    anomaly_indices = [item["word_index"] for item in anomalies]
    if (
        anomaly_indices != sorted(set(anomaly_indices))
        or any(index >= word_entry_count for index in anomaly_indices)
        or any(item["shape"] == "valid" for item in anomalies)
    ):
        return None
    entry_shape_counts = counts["entry_shape"]
    if len(anomalies) != word_entry_count - entry_shape_counts["valid"]:
        return None
    if any(
        sum(item["shape"] == shape for item in anomalies)
        != entry_shape_counts[shape]
        for shape in ASR_WORD_TIMING_SHAPES
        if shape != "valid"
    ):
        return None
    return {
        "word_entry_count": word_entry_count,
        "no_word_entries": no_word_entries,
        "envelope": envelope,
        "counts": counts,
        "anomalies": anomalies,
    }


def _copy_word_timing_entry(value: object) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    word_index = _copy_nonnegative_int(value.get("word_index"))
    start = _copy_boundary(value.get("start"))
    end = _copy_boundary(value.get("end"))
    relation = _copy_enum(value.get("numeric_relation"), ASR_WORD_TIMING_RELATIONS)
    shape = _copy_enum(value.get("shape"), ASR_WORD_TIMING_SHAPES)
    if (
        word_index is None
        or start is None
        or end is None
        or relation is None
        or shape is None
    ):
        return None
    return {
        "word_index": word_index,
        "start": start,
        "end": end,
        "numeric_relation": relation,
        "shape": shape,
    }


def _copy_word_timing_envelope(value: object) -> Dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    start_word_index = _copy_nonnegative_int(value.get("start_word_index"))
    end_word_index = _copy_nonnegative_int(value.get("end_word_index"))
    start = _copy_boundary(value.get("start"))
    end = _copy_boundary(value.get("end"))
    relation = _copy_enum(value.get("numeric_relation"), ASR_WORD_TIMING_RELATIONS)
    shape = _copy_enum(value.get("shape"), ASR_WORD_TIMING_SHAPES)
    usable = value.get("usable")
    if (
        start_word_index is None
        or end_word_index is None
        or start is None
        or end is None
        or relation is None
        or shape is None
        or not isinstance(usable, bool)
    ):
        return None
    return {
        "start_word_index": start_word_index,
        "end_word_index": end_word_index,
        "start": start,
        "end": end,
        "numeric_relation": relation,
        "shape": shape,
        "usable": usable,
    }


def _copy_boundary(value: object) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    presence = _copy_enum(value.get("presence"), ASR_BOUNDARY_PRESENCES)
    numeric_class = _copy_enum(
        value.get("numeric_class"),
        ASR_BOUNDARY_NUMERIC_CLASSES,
    )
    if presence is None or numeric_class is None:
        return None
    copied: Dict[str, Any] = {
        "presence": presence,
        "numeric_class": numeric_class,
    }
    if numeric_class in {"negative", "zero", "positive"}:
        finite_value = value.get("finite_value_ms")
        if (
            not isinstance(finite_value, (int, float))
            or isinstance(finite_value, bool)
            or not math.isfinite(finite_value)
        ):
            return None
        copied["finite_value_ms"] = finite_value
    return copied


def _copy_counter_map(
    value: object,
    keys: Iterable[str],
) -> Dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    copied = {}
    for key in keys:
        count = _copy_nonnegative_int(value.get(key))
        if count is None:
            return None
        copied[key] = count
    return copied


def _copy_nonnegative_int(value: object) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        return None
    return value


def _copy_enum(value: object, allowed: Iterable[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None
