from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import analyze_stage_burst_attribution as analyzer
from freshness_trace import ParentFreshnessTrace
from playback_simulation import ParentAudioFrame, simulate_playback


def _sample() -> analyzer.StageBurstSample:
    frames = (
        ParentAudioFrame(
            arrival_seconds=5.0,
            duration_seconds=40.0,
            parent_sequence_id=0,
            audio_frame_id=0,
            parent_frame_count=1,
            source_index=0,
            source_end_ms=4_000.0,
        ),
        ParentAudioFrame(
            arrival_seconds=30.0,
            duration_seconds=1.0,
            parent_sequence_id=1,
            audio_frame_id=0,
            parent_frame_count=1,
            source_index=1,
            source_end_ms=29_000.0,
        ),
        ParentAudioFrame(
            arrival_seconds=65.0,
            duration_seconds=20.0,
            parent_sequence_id=2,
            audio_frame_id=0,
            parent_frame_count=1,
            source_index=2,
            source_end_ms=64_000.0,
        ),
    )
    trace = ParentFreshnessTrace(
        trace_csv="private.csv",
        summary_json="private.json",
        trace_sha256="private-trace-hash",
        summary_sha256="private-summary-hash",
        input_end_seconds=100.0,
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        frames=frames,
        input_sample_zero_timestamp_ms=0.0,
    )
    playback = simulate_playback(
        frames,
        input_end_seconds=trace.input_end_seconds,
        adaptive=True,
    )
    parent_stages = tuple(
        analyzer._ParentStage(
            source_end_seconds=float(index),
            source_end_to_latest_asr_final_seconds=0.1,
            asr_final_to_segment_seconds=0.1,
            segment_to_nmt_enqueue_seconds=0.01,
            nmt_queue_seconds=0.1 * index,
            nmt_processing_seconds=0.2,
            nmt_complete_to_tts_enqueue_seconds=0.01,
            tts_queue_seconds=0.05 * index,
            tts_start_to_first_seconds=0.1,
            tts_processing_seconds=0.3,
            tts_first_to_final_frame_seconds=0.0,
            tts_audio_seconds=frames[index].duration_seconds,
            audio_frame_count=1,
            nmt_blocked_put_seconds=0.0,
            tts_blocked_put_seconds=0.0,
            nmt_retry_count=0,
            tts_retry_count=0,
            atomic_fallback_count=0,
        )
        for index in range(3)
    )
    parent_bursts = (
        analyzer._ParentBurst(5.0, 5.0, 40.0, 36.0, 1.0, 1.0),
        analyzer._ParentBurst(30.0, 30.0, 1.0, 0.0, 1.0, 1.0),
        analyzer._ParentBurst(65.0, 65.0, 20.0, 18.0, 1.0, 1.0),
    )
    stage_values = {
        key: (0.0, 0.0, 0.0)
        for key in analyzer.STAGE_METRIC_KEYS
    }
    stage_values.update(
        {
            "nmt_queue_seconds": (0.0, 0.1, 0.2),
            "tts_audio_seconds": (40.0, 1.0, 20.0),
            "audio_frame_count": (1.0, 1.0, 1.0),
            "tts_retry_count": (0.0, 1.0, 2.0),
            "tts_synthesis_realtime_factor": (0.5, 1.0, 1.5),
            "tts_frame_to_output_enqueue_seconds": (0.001, 0.2, 1.2),
        }
    )
    return analyzer.StageBurstSample(
        sample_index=1,
        input_end_seconds=100.0,
        stage_values=stage_values,
        blocked_put_values={"nmt": (), "tts": (), "output": ()},
        parent_stages=parent_stages,
        parent_bursts=parent_bursts,
        trace=trace,
        playback=playback,
    )


def _assert_numeric_leaves(value: object) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _assert_numeric_leaves(child)
    elif isinstance(value, list):
        for child in value:
            _assert_numeric_leaves(child)
    else:
        assert isinstance(value, (int, float))
        assert not isinstance(value, bool)


def test_fixed_windows_are_half_open_and_whole_second_aligned() -> None:
    windows = analyzer._window_records(
        _sample(),
        30,
        stride_seconds=30,
    )

    assert [item["start_seconds"] for item in windows] == [0, 30, 60]
    assert windows[0]["end_seconds"] == 30
    assert windows[0]["arrived_audio_seconds"] == pytest.approx(40.0)
    assert windows[1]["arrived_audio_seconds"] == pytest.approx(1.0)
    assert windows[2]["arrived_audio_seconds"] == pytest.approx(20.0)
    for frame in _sample().trace.frames:
        assert (
            sum(
                window["start_seconds"]
                <= frame.arrival_seconds
                < window["end_seconds"]
                for window in windows
            )
            == 1
        )


def test_analysis_is_numeric_only_and_does_not_copy_private_fields() -> None:
    analysis = analyzer.build_stage_burst_analysis(
        [_sample()],
        window_seconds=30,
        top_window_count=2,
    )

    _assert_numeric_leaves(analysis)
    serialized = json.dumps(analysis)
    for private_value in (
        "private.csv",
        "private.json",
        "private-trace-hash",
        "private-summary-hash",
    ):
        assert private_value not in serialized
    top = analysis["samples"][0]["top_aligned_positive_growth_windows"]
    assert top[0]["start_seconds"] == 0
    assert top[0]["end_seconds"] == 30
    assert top[0]["frame_count"] == 1
    assert top[0]["unique_parent_count"] == 1
    assert "nmt_queue_seconds" in top[0]["associated_stage_distributions"]
    assert analysis["samples"][0]["diagnostics"] == {
        "tts_frame_to_output_enqueue_over_100ms_count": 2,
        "tts_frame_to_output_enqueue_over_1s_count": 1,
    }
    markdown = analyzer.render_markdown(analysis)
    assert "| audio frame count | 3 | 1 | 1 | 1 |" in markdown
    assert "| tts retry count | 3 | 1 | 2 | 2 |" in markdown
    assert "| tts synthesis realtime factor | 3 | 1.000x | 1.500x |" in markdown


def test_builder_rejects_non_whitelisted_metric_key() -> None:
    sample = _sample()
    contaminated = replace(
        sample,
        stage_values={
            **sample.stage_values,
            "PRIVATE_PATH_SENTINEL": (1.0,),
        },
    )

    with pytest.raises(ValueError, match="fixed whitelist"):
        analyzer.build_stage_burst_analysis([contaminated])


def test_stage_chain_guards_fail_closed() -> None:
    with pytest.raises(ValueError, match="not monotonic"):
        analyzer._require_monotonic((0.0, 2.0, 1.0), "synthetic stage")
    with pytest.raises(ValueError, match="queue residence"):
        analyzer._require_queue_accounting(((1.0, 0.5),))
    with pytest.raises(ValueError, match="processing duration"):
        analyzer._require_processing_envelopes(((1.0, 1.1),))

    analyzer._require_monotonic((0.0, 0.0, 1.0), "synthetic stage")
    analyzer._require_queue_accounting(((1.0, 1.009),))
    analyzer._require_processing_envelopes(((1.0, 1.009),))


def test_schema_gate_rejects_subsegmentation() -> None:
    root = {
        "pipeline_mode": "staged",
        "backend_config": {
            "stagedConfig": {
                "telemetrySchemaVersion": 3,
                "ttsIncrementalPublishEnabled": True,
                "ttsSubsegmentMaxChars": 10,
            }
        },
        "staged_pipeline": {
            "telemetry_schema_version": 3,
            "tts_incremental_publish_enabled": True,
            "tts_subsegmentation_enabled": True,
            "state": "closed",
            "outcome": "complete",
            "failure": None,
        },
    }

    with pytest.raises(ValueError, match="subsegmentation"):
        analyzer._validate_schema3_mode(root)


def test_cli_writes_requested_json_and_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = {"schema_version": 1, "aggregate": {"counts": {"samples": 1}}}
    monkeypatch.setattr(analyzer, "analyze_paths", lambda *args, **kwargs: analysis)
    monkeypatch.setattr(analyzer, "render_markdown", lambda value: "# Report\n")
    json_output = tmp_path / "report.json"
    markdown_output = tmp_path / "report.md"

    analyzer.main(
        [
            "neutral_results.csv",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )

    assert json.loads(json_output.read_text(encoding="utf-8")) == analysis
    assert markdown_output.read_text(encoding="utf-8") == "# Report\n"


@pytest.mark.parametrize("same_as_input", [True, False])
def test_cli_rejects_output_path_aliasing(
    tmp_path: Path,
    same_as_input: bool,
) -> None:
    input_path = tmp_path / "neutral_results.csv"
    json_output = input_path if same_as_input else tmp_path / "same-output"
    markdown_output = (
        tmp_path / "different-output" if same_as_input else json_output
    )

    with pytest.raises(SystemExit, match="2"):
        analyzer.main(
            [
                str(input_path),
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(markdown_output),
            ]
        )


def test_cli_rejects_output_aliasing_inferred_summary(tmp_path: Path) -> None:
    input_path = tmp_path / "neutral_results.csv"
    summary_path = tmp_path / "neutral_summary.json"

    with pytest.raises(SystemExit, match="2"):
        analyzer.main(
            [
                str(input_path),
                "--json-output",
                str(summary_path),
            ]
        )


def test_formal_three_sample_regression_when_private_run_is_present() -> None:
    run = (
        Path(__file__).parents[1]
        / "experiment_results"
        / "formal-headless-v1-20260726T053311Z-4ab2da9"
        / "repeat-01"
    )
    paths = [run / f"long-form-{index:02d}_results.csv" for index in range(1, 4)]
    if not all(path.is_file() for path in paths):
        pytest.skip("ignored formal traces are not present")

    analysis = analyzer.analyze_paths(paths)

    assert analysis["aggregate"]["counts"] == {
        "samples": 3,
        "parents": 2020,
        "audio_frames": 14209,
    }
    top = [
        sample["top_aligned_positive_growth_windows"][0]
        for sample in analysis["samples"]
    ]
    assert [item["net_queue_growth_seconds"] for item in top] == pytest.approx(
        [36.325114, 21.109186, 25.842495],
        abs=1e-6,
    )
    assert [item["arrived_audio_seconds"] for item in top] == pytest.approx(
        [72.957625, 56.053438, 49.412437],
        abs=1e-6,
    )
    assert [
        sample["descriptive_aligned_window_spearman"][
            "arrived_audio_seconds"
        ]["coefficient"]
        for sample in analysis["samples"]
    ] == pytest.approx(
        [0.849310, 0.934101, 0.793347],
        abs=1e-6,
    )
    assert [
        sample["descriptive_parent_spearman"][
            "audio_seconds_to_immediate_queue_change"
        ]["coefficient"]
        for sample in analysis["samples"]
    ] == pytest.approx(
        [0.998797, 0.994996, 0.996643],
        abs=1e-6,
    )
    assert [
        sample["descriptive_parent_spearman"][
            "source_to_first_frame_to_immediate_queue_change"
        ]["coefficient"]
        for sample in analysis["samples"]
    ] == pytest.approx(
        [0.158292, 0.145132, 0.105005],
        abs=1e-6,
    )
