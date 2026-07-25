"""Privacy-safe diagnostics for finalized ASR source attribution."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable

from staged_models import (
    ASR_TIMING_BASES,
    ASR_TIMING_BASIS_WORD_OFFSETS,
    AsrFinal,
)

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
)


def final_attribution_record(final: AsrFinal) -> Dict[str, Any]:
    """Return final metadata without copying transcript content."""
    return {
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
    }


def summarize_final_attribution(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build deterministic counters for one ASR stream's nonempty finals."""
    # Copy only the explicit privacy-safe schema. An accidental caller field
    # such as transcript text must never flow into retained telemetry.
    copied = [
        {
            key: record.get(key)
            for key in FINAL_ATTRIBUTION_RECORD_KEYS
        }
        for record in records
    ]
    timing_counts = Counter(
        str(record.get("timing_basis", "")) for record in copied
    )
    missing_word_offset_ids = [
        int(record["final_id"])
        for record in copied
        if record.get("timing_basis") != ASR_TIMING_BASIS_WORD_OFFSETS
    ]
    final_count = len(copied)
    return {
        "schema_version": 1,
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
        "finals": copied,
    }
