import json

from asr_attribution import (
    final_attribution_record,
    summarize_final_attribution,
)
from staged_models import AsrFinal


def timed_final(final_id=0, text="private transcript"):
    return AsrFinal(
        final_id=final_id,
        text=text,
        received_monotonic_ms=1_500,
        source_start_ms=100,
        source_end_ms=900,
        audio_processed_s=1.25,
        word_count=3,
        first_word_start_ms=100,
        last_word_end_ms=900,
        timing_basis="word_offsets",
    )


def test_final_attribution_record_is_privacy_safe_and_complete():
    record = final_attribution_record(timed_final())

    assert record == {
        "final_id": 0,
        "text_chars": 18,
        "word_count": 3,
        "audio_processed_s": 1.25,
        "first_word_start_ms": 100,
        "last_word_end_ms": 900,
        "source_start_ms": 100,
        "source_end_ms": 900,
        "timing_basis": "word_offsets",
        "received_monotonic_ms": 1_500,
    }
    encoded = json.dumps(record)
    assert "private transcript" not in encoded
    assert '"text"' not in encoded


def test_summary_counts_end_only_final_as_missing_without_borrowing_start():
    timed = final_attribution_record(timed_final())
    end_only = final_attribution_record(
        AsrFinal(
            final_id=1,
            text="another private transcript",
            received_monotonic_ms=2_500,
            source_start_ms=None,
            source_end_ms=2_000,
            audio_processed_s=2.0,
            timing_basis="audio_processed_end_only",
        )
    )

    summary = summarize_final_attribution([timed, end_only])

    assert summary["nonempty_final_count"] == 2
    assert summary["final_with_word_offsets_count"] == 1
    assert summary["final_missing_word_offsets_count"] == 1
    assert summary["missing_word_offset_final_ids"] == [1]
    assert summary["all_nonempty_finals_have_word_offsets"] is False
    assert summary["finals"][1]["source_start_ms"] is None
    assert "another private transcript" not in json.dumps(summary)


def test_empty_summary_is_not_gate_ready():
    summary = summarize_final_attribution([])

    assert summary["nonempty_final_count"] == 0
    assert summary["final_missing_word_offsets_count"] == 0
    assert summary["all_nonempty_finals_have_word_offsets"] is False


def test_summary_drops_unexpected_caller_fields():
    record = final_attribution_record(timed_final())
    record["transcript"] = "must not escape"
    record["arbitrary_partner_field"] = "must not escape"

    summary = summarize_final_attribution([record])
    encoded = json.dumps(summary)

    assert "must not escape" not in encoded
    assert "transcript" not in encoded
    assert "arbitrary_partner_field" not in encoded
