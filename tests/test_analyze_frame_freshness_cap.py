from __future__ import annotations

import json

import pytest

from analyze_frame_freshness_cap import build_analysis, render_markdown
from freshness_trace import ParentFreshnessTrace
from playback_simulation import ParentAudioFrame


def _trace() -> ParentFreshnessTrace:
    frames = (
        ParentAudioFrame(
            arrival_seconds=0.0,
            duration_seconds=0.5,
            audio_bytes=16_000,
            source_index=0,
            parent_sequence_id=0,
            audio_frame_id=0,
            parent_frame_count=2,
        ),
        ParentAudioFrame(
            arrival_seconds=0.1,
            duration_seconds=0.5,
            audio_bytes=16_000,
            source_index=1,
            parent_sequence_id=0,
            audio_frame_id=1,
            parent_frame_count=2,
        ),
    )
    return ParentFreshnessTrace(
        trace_csv="PRIVATE_PATH_results.csv",
        summary_json="PRIVATE_PATH_summary.json",
        trace_sha256="a" * 64,
        summary_sha256="b" * 64,
        input_end_seconds=0.2,
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        frames=frames,
    )


def test_frame_cap_analysis_reports_partial_suffix_without_private_path() -> None:
    analysis = build_analysis(
        _trace(),
        caps_seconds=[0.7],
        cancellation_guard_seconds=0.0,
        guard_sensitivity_seconds=[0.0],
    )

    scenario = analysis["scenarios"][0]
    assert scenario["summary"]["hard_cap_achieved"] is True
    assert scenario["summary"]["parents_partially_dropped"] == 1
    assert scenario["drop_shapes"]["parent_shape_counts"]["partial_suffix"] == 1
    assert scenario["invariants"] == {
        "frame_accounting_reconciles": True,
        "byte_accounting_reconciles": True,
        "partial_parent_drop_is_possible": True,
        "single_tail_contract_holds": True,
        "live_audio_changed": False,
    }
    serialized = json.dumps(analysis)
    assert "PRIVATE_PATH" not in serialized
    assert analysis["privacy"]["contains_transcript_or_translation_text"] is False

    markdown = render_markdown(analysis)
    assert "Hard cap achieved" in markdown
    assert "partially cut 1 parents" in markdown


def test_tail_analysis_declares_suppression_contract() -> None:
    analysis = build_analysis(
        _trace(),
        caps_seconds=[0.7],
        cancellation_guard_seconds=0.0,
        guard_sensitivity_seconds=[0.0],
        strategy="truncate_parent_tail",
    )

    assert analysis["analysis_type"] == (
        "schema3_parent_tail_truncation_freshness_cap"
    )
    assert analysis["semantics"][
        "future_frames_suppressed_through_parent_completion"
    ] is True
    assert analysis["semantics"][
        "partial_parent_and_mid_speech_loss_allowed"
    ] is False
    assert analysis["semantics"]["single_suffix_loss_allowed"] is True
    assert analysis["scenarios"][0]["invariants"][
        "single_tail_contract_holds"
    ] is True
    assert analysis["scenarios"][0]["drop_shapes"][
        "parent_shape_counts"
    ]["partial_suffix"] == 1
    assert "parent-tail truncation" in render_markdown(analysis)


@pytest.mark.parametrize("cap", [0, -1, float("inf"), float("nan")])
def test_frame_cap_analysis_rejects_invalid_caps(cap: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        build_analysis(_trace(), caps_seconds=[cap])
