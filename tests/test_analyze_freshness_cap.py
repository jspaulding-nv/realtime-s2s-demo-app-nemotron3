import json
import math
from pathlib import Path

import pytest

from analyze_freshness_cap import (
    build_freshness_cap_analysis,
    normalize_cancellation_guards,
    normalize_freshness_caps,
    normalize_freshness_strategies,
    parse_cli_args,
    render_freshness_cap_markdown,
)
from freshness_trace import ParentFreshnessTrace, load_parent_freshness_trace
from playback_simulation import ParentAudioFrame


def _trace():
    frames = (
        ParentAudioFrame(
            arrival_seconds=0.0,
            duration_seconds=4.0,
            audio_bytes=128_000,
            source_index=0,
            parent_sequence_id=0,
            audio_frame_id=0,
            parent_frame_count=1,
        ),
        ParentAudioFrame(
            arrival_seconds=0.0,
            duration_seconds=4.0,
            audio_bytes=128_000,
            source_index=1,
            parent_sequence_id=1,
            audio_frame_id=0,
            parent_frame_count=1,
        ),
    )
    return ParentFreshnessTrace(
        trace_csv="Person_Customer_Sermon_results.csv",
        summary_json="Person_Customer_Sermon_summary.json",
        trace_sha256="a" * 64,
        summary_sha256="b" * 64,
        input_end_seconds=2.0,
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        frames=frames,
    )


def _source_clock_trace():
    frames = (
        ParentAudioFrame(
            arrival_seconds=1.0,
            duration_seconds=0.1,
            audio_bytes=3200,
            source_index=0,
            parent_sequence_id=0,
            audio_frame_id=0,
            parent_frame_count=1,
            source_start_ms=None,
            source_end_ms=500.0,
        ),
        ParentAudioFrame(
            arrival_seconds=1.05,
            duration_seconds=0.1,
            audio_bytes=3200,
            source_index=1,
            parent_sequence_id=1,
            audio_frame_id=0,
            parent_frame_count=1,
            source_start_ms=600.0,
            source_end_ms=800.0,
        ),
    )
    return ParentFreshnessTrace(
        trace_csv="schema3_client_events.csv",
        summary_json="schema3_capture_summary.json",
        trace_sha256="c" * 64,
        summary_sha256="d" * 64,
        input_end_seconds=2.0,
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        frames=frames,
        input_sample_zero_timestamp_ms=100.0,
        audio_metadata_protocol_version=1,
        audio_metadata_stream_generation=7,
        source_end_to_receipt_availability=(
            "available_audio_processed_end_offset_not_semantic_boundary"
        ),
    )


def test_normalize_freshness_options_deduplicates_and_preserves_order():
    assert normalize_freshness_caps(None) == (5.0, 8.0, 10.0)
    assert normalize_freshness_caps([10, 5, 10, 8]) == (10.0, 5.0, 8.0)
    assert normalize_freshness_strategies(None) == (
        "oldest_first",
        "jump_to_latest_complete",
    )
    assert normalize_freshness_strategies(
        ["jump_to_latest_complete", "oldest_first", "jump_to_latest_complete"]
    ) == ("jump_to_latest_complete", "oldest_first")
    assert normalize_cancellation_guards(
        [0.1, 0.0, 0.1, 0.25],
        default=(),
    ) == (0.1, 0.0, 0.25)


@pytest.mark.parametrize("invalid", [0, -1, math.inf, -math.inf, math.nan])
def test_normalize_freshness_caps_rejects_invalid_values(invalid):
    with pytest.raises(ValueError, match="finite and positive"):
        normalize_freshness_caps([invalid])


def test_normalize_freshness_options_reject_empty_or_unknown():
    with pytest.raises(ValueError, match="at least one freshness cap"):
        normalize_freshness_caps([])
    with pytest.raises(ValueError, match="at least one freshness strategy"):
        normalize_freshness_strategies([])
    with pytest.raises(ValueError, match="strategy must be"):
        normalize_freshness_strategies(["unknown"])
    with pytest.raises(ValueError, match="at least one cancellation guard"):
        normalize_cancellation_guards([], default=())


@pytest.mark.parametrize("invalid", [-1, math.inf, -math.inf, math.nan])
def test_normalize_cancellation_guards_rejects_invalid_values(invalid):
    with pytest.raises(ValueError, match="finite and non-negative"):
        normalize_cancellation_guards([invalid], default=())


def test_normalize_cancellation_guards_rejects_boolean():
    with pytest.raises(ValueError, match="must be numbers"):
        normalize_cancellation_guards([True], default=())


def test_build_analysis_reports_loss_and_residuals_without_paths():
    trace = _trace()

    analysis = build_freshness_cap_analysis(
        trace,
        freshness_caps=[5],
        strategies=["oldest_first", "jump_to_latest_complete"],
    )

    assert analysis["schema_version"] == 1
    assert analysis["source"] == {
        "trace_csv": "schema3_client_events.csv",
        "summary_json": "schema3_capture_summary.json",
        "trace_sha256": "a" * 64,
        "summary_sha256": "b" * 64,
    }
    assert analysis["semantics"]["intentionally_lossy"] is True
    assert analysis["semantics"]["live_browser_support_present"] is False
    assert analysis["semantics"]["cancellation_guard_seconds"] == 0.1
    assert analysis["source_timing_observation"]["availability"] == (
        "protocol_not_negotiated"
    )
    assert analysis["source_timing_observation"][
        "semantic_boundary_proven"
    ] is False
    assert analysis["source_timing_observation"][
        "actual_audibility_proven"
    ] is False
    assert analysis["adaptive_no_drop_baseline"]["chunks_dropped"] == 0
    assert analysis["captured_trace"]["parent_count"] == 2
    assert analysis["captured_trace"][
        "parents_with_source_pcm_longer_than_cap"
    ] == {
        "5_seconds": 0
    }
    assert len(analysis["scenarios"]) == 2
    assert analysis["cancellation_guard_sensitivity"]["guards_seconds"] == [
        0.0,
        0.05,
        0.1,
        0.25,
    ]

    oldest = analysis["scenarios"][0]
    assert oldest["strategy"] == "oldest_first"
    assert oldest["summary"]["parents_dropped"] == 1
    assert oldest["summary"]["retained_source_percent"] == 50.0
    assert oldest["whole_parent_invariants"] == {
        "partial_parent_drops": 0,
        "all_drops_are_complete_whole_parents": True,
        "frame_accounting_reconciles": True,
        "byte_accounting_reconciles": True,
    }
    assert oldest["evictions"][0]["dropped_parent_sequence_ids"] == [1]

    jump = analysis["scenarios"][1]
    assert jump["strategy"] == "jump_to_latest_complete"
    assert jump["summary"]["parents_dropped"] == 0
    assert jump["summary"]["hard_cap_achieved"] is False
    assert jump["residual_breach_decisions"]

    serialized = json.dumps(analysis)
    assert "/tmp/" not in serialized
    assert "Person_Customer_Sermon" not in serialized
    assert "translated text" not in serialized


def test_analysis_reports_source_receipt_and_deterministic_scheduled_start():
    analysis = build_freshness_cap_analysis(
        _source_clock_trace(),
        freshness_caps=[5],
        strategies=["oldest_first"],
    )

    observation = analysis["source_timing_observation"]
    assert observation["availability"] == (
        "available_audio_processed_end_offset_not_semantic_boundary"
    )
    assert observation["source_end_offset_basis"] == (
        "mixed_audio_processed_end_only_and_asr_source_range_end_offsets"
        "_not_semantic_boundaries"
    )
    assert observation["frames_with_audio_processed_end_only"] == 1
    assert observation["frames_with_asr_source_range"] == 1
    assert observation["semantic_boundary_proven"] is False
    assert observation["actual_audibility_proven"] is False
    assert observation["playback_behavior_changed"] is False
    assert observation["source_end_to_client_receipt_ms"] == {
        "sample_count": 2,
        "p50_ms": 150.0,
        "p95_ms": 400.0,
        "max_ms": 400.0,
    }
    assert observation[
        "source_end_to_deterministic_scheduled_playback_start_ms"
    ] == {
        "sample_count": 2,
        "p50_ms": 200.0,
        "p95_ms": 400.0,
        "max_ms": 400.0,
    }
    assert analysis["semantics"]["audio_metadata_observation_present"] is True
    assert "observation-only" in " ".join(
        analysis["semantics"]["live_browser_blockers"]
    )


def test_markdown_labels_source_offsets_as_nonsemantic_and_nonaudibility():
    markdown = render_freshness_cap_markdown(
        build_freshness_cap_analysis(
            _source_clock_trace(),
            freshness_caps=[5],
            strategies=["oldest_first"],
        )
    )

    assert "Protocol-v1 source-clock observation" in markdown
    assert "source end to client receipt" in markdown
    assert "deterministic adaptive no-drop scheduled playback start" in markdown
    assert "audio_processed_end_only" in markdown
    assert "do not prove a phrase/punchline boundary" in markdown
    assert "does not prove physical audibility" in markdown
    assert "receives validated parent/frame metadata" in markdown
    assert "parent-completion markers" in markdown
    assert "retains no cancellable parent-aware queue" in markdown
    assert "anonymous binary PCM" not in markdown


def test_markdown_does_not_claim_residual_breaches_when_all_caps_achieve():
    analysis = build_freshness_cap_analysis(
        _source_clock_trace(),
        freshness_caps=[5, 10],
        strategies=["oldest_first", "jump_to_latest_complete"],
    )
    assert all(
        scenario["summary"]["hard_cap_achieved"]
        for scenario in analysis["scenarios"]
    )

    markdown = render_freshness_cap_markdown(analysis)

    assert (
        "achieved every tested cap/strategy scenario on this capture"
        in markdown
    )
    assert "did not achieve every tested" not in markdown
    assert "observed residual breaches" not in markdown
    assert "does not establish a universal live hard-cap guarantee" in markdown


def test_render_markdown_prominently_labels_loss_scope_and_browser_gate():
    markdown = render_freshness_cap_markdown(
        build_freshness_cap_analysis(
            _trace(),
            freshness_caps=[5],
            strategies=["oldest_first", "jump_to_latest_complete"],
        )
    )

    assert "Lossy offline counterfactual" in markdown
    assert "intentionally skip" in markdown
    assert "not yet a direct" in markdown
    assert "Parents skipped" in markdown
    assert "cannot enforce" in markdown
    assert "No live playback behavior was changed" in markdown
    assert "100 ms cancellation guard" in markdown
    assert "Cancellation-guard sensitivity" in markdown
    assert "anonymous binary PCM" in markdown
    assert "did not achieve every tested cap/strategy scenario" in markdown


def test_parse_cli_args_applies_defaults_dedupes_and_derives_outputs():
    args = parse_cli_args(
        [
            "--results-csv",
            "captures/Person_Customer_Sermon_results.csv",
            "--freshness-cap",
            "10",
            "--freshness-cap",
            "5",
            "--freshness-cap",
            "10",
            "--strategy",
            "oldest_first",
            "--strategy",
            "oldest_first",
        ]
    )

    assert args.freshness_caps == (10.0, 5.0)
    assert args.strategy == ("oldest_first",)
    assert args.cancellation_guard_seconds == 0.1
    assert args.guard_sensitivity_seconds == (0.0, 0.05, 0.1, 0.25)
    assert args.json_output == Path(
        "captures/schema3_freshness_cap_analysis.json"
    )
    assert args.markdown_output == Path(
        "captures/schema3_freshness_cap_analysis.md"
    )


def test_parse_cli_args_rejects_output_collision(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_cli_args(
            [
                "--results-csv",
                "neutral_results.csv",
                "--json-output",
                "same.out",
                "--markdown-output",
                "same.out",
            ]
        )

    assert exc_info.value.code == 2
    assert "must be different" in capsys.readouterr().err


def test_real_five_minute_trace_runs_all_six_whole_parent_scenarios():
    """Local regression over ignored evidence; skipped in a fresh clone."""

    base = Path(
        "experiment_results/"
        "streaming-tts-canary-20260724T232158Z-29cdf4e/"
        "streaming"
    )
    csv_path = base / "shared-prefix_results.csv"
    summary_path = base / "shared-prefix_summary.json"
    if not csv_path.exists() or not summary_path.exists():
        pytest.skip("ignored five-minute schema-3 evidence is not present")

    analysis = build_freshness_cap_analysis(
        load_parent_freshness_trace(csv_path, summary_path)
    )

    assert analysis["captured_trace"]["frame_count"] == 2782
    assert analysis["captured_trace"]["parent_count"] == 74
    assert len(analysis["scenarios"]) == 6
    for scenario in analysis["scenarios"]:
        assert scenario["whole_parent_invariants"] == {
            "partial_parent_drops": 0,
            "all_drops_are_complete_whole_parents": True,
            "frame_accounting_reconciles": True,
            "byte_accounting_reconciles": True,
        }
