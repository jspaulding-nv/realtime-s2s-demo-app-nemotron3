from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

import analyze_synthesized_silence as analyzer
from batch_latency_test import (
    TestResult as BatchTestResult,
    build_synthesized_pcm_silence_processing,
    generate_summary,
)
from synthesized_pcm_silence import (
    SAMPLE_RATE_HZ,
    StreamingPcmSilenceDiagnostic,
    WINDOW_SAMPLES,
)


PRIVATE_MARKER = "PRIVATE_PATH_URL_SESSION_TEXT"


def _frame(
    *,
    parent_id: int,
    frame_id: int,
    pcm: bytes,
    source_start_ms: float,
    source_end_ms: float,
) -> dict[str, object]:
    return {
        "type": "audio_frame",
        "protocolVersion": 1,
        "streamGeneration": 1,
        "parentSequenceId": parent_id,
        "audioFrameId": frame_id,
        "audioBytes": len(pcm),
        "sampleRateHz": SAMPLE_RATE_HZ,
        "channels": 1,
        "bytesPerSample": 2,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


def _completion(
    *,
    parent_id: int,
    frame_count: int,
    audio_bytes: int,
    source_start_ms: float,
    source_end_ms: float,
) -> dict[str, object]:
    return {
        "type": "audio_parent_complete",
        "protocolVersion": 1,
        "streamGeneration": 1,
        "parentSequenceId": parent_id,
        "audioFrameCount": frame_count,
        "audioBytes": audio_bytes,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


def _window(amplitude: int) -> np.ndarray:
    return np.full(WINDOW_SAMPLES, amplitude, dtype="<i2")


def _observation() -> dict[str, object]:
    diagnostic = StreamingPcmSilenceDiagnostic()

    # -60 dBFS: only the zero-valued edge windows are low-energy.
    # -50 dBFS: amplitude 58 expands both edges and creates one internal run.
    # -40 dBFS: amplitude 184 is also low-energy, making the parent all-low.
    parent_zero = np.concatenate(
        [
            _window(0),
            *(_window(58) for _ in range(4)),
            *(_window(184) for _ in range(5)),
            _window(58),
            *(_window(184) for _ in range(26)),
            *(_window(58) for _ in range(12)),
            _window(0),
        ]
    )
    assert parent_zero.size == SAMPLE_RATE_HZ
    parent_zero_bytes = parent_zero.tobytes()
    split_bytes = 7_000 * 2
    parent_zero_frames = (
        parent_zero_bytes[:split_bytes],
        parent_zero_bytes[split_bytes:],
    )
    for frame_id, pcm in enumerate(parent_zero_frames):
        diagnostic.accept_frame(
            _frame(
                parent_id=0,
                frame_id=frame_id,
                pcm=pcm,
                source_start_ms=0.0,
                source_end_ms=1_000.0,
            ),
            pcm,
        )
    diagnostic.complete_parent(
        _completion(
            parent_id=0,
            frame_count=2,
            audio_bytes=len(parent_zero_bytes),
            source_start_ms=0.0,
            source_end_ms=1_000.0,
        )
    )

    parent_one = np.zeros(9_600, dtype="<i2").tobytes()
    diagnostic.accept_frame(
        _frame(
            parent_id=1,
            frame_id=0,
            pcm=parent_one,
            source_start_ms=1_000.0,
            source_end_ms=1_600.0,
        ),
        parent_one,
    )
    diagnostic.complete_parent(
        _completion(
            parent_id=1,
            frame_count=1,
            audio_bytes=len(parent_one),
            source_start_ms=1_000.0,
            source_end_ms=1_600.0,
        )
    )
    return diagnostic.finalize()


def _result(
    observation: dict[str, object],
    *,
    processing_durations_ms: list[float] | None = None,
) -> BatchTestResult:
    totals = observation["totals"]
    if processing_durations_ms is None:
        processing_durations_ms = [1.0, 2.0, 3.0]
    result = BatchTestResult(
        audio_path=PRIVATE_MARKER,
        duration_sec=1.6,
        backend_url=f"https://{PRIVATE_MARKER}",
        backend_config_url=f"https://{PRIVATE_MARKER}/api/config",
    )
    result.pipeline_mode = "staged"
    result.pipeline_mode_source = "api_config"
    result.backend_config = {
        "pipelineMode": "staged",
        "stagedConfig": {
            "telemetrySchemaVersion": 3,
            "ttsIncrementalPublishEnabled": True,
        },
    }
    result.staged_pipeline = {
        "state": "closed",
        "outcome": "complete",
        "failure": None,
        "cleanup_errors": [],
        "session_id": PRIVATE_MARKER,
    }
    result.audio_metadata_protocol_version = 1
    result.audio_metadata_stream_generation = totals["stream_generation"]
    result.audio_metadata_paired_frames = totals["frame_count"]
    result.audio_metadata_completed_parents = totals["parent_count"]
    result.synthesized_pcm_silence_requested = True
    result.synthesized_pcm_silence = copy.deepcopy(observation)
    result.synthesized_pcm_silence_processing = (
        build_synthesized_pcm_silence_processing(
            processing_durations_ms
        )
    )
    result.audio_responses = totals["frame_count"]
    result.total_received_bytes = totals["audio_bytes"]
    result.output_duration_sec = totals["sample_count"] / SAMPLE_RATE_HZ
    result.input_completed = True
    result.translation_completed = True
    # Keep TestResult's real successful default: server_error == "".
    return result


def _write_generated_summary(
    path: Path,
    *,
    observation: dict[str, object] | None = None,
    processing_durations_ms: list[float] | None = None,
) -> None:
    selected = _observation() if observation is None else observation
    generate_summary(
        _result(
            selected,
            processing_durations_ms=processing_durations_ms,
        ),
        str(path),
    )


def _primary(value: dict[str, object]) -> dict[str, object]:
    return next(
        item for item in value["thresholds"] if item["is_primary"]
    )


def test_generated_success_summary_builds_primary_aggregate_and_privacy(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "private_summary.json"
    _write_generated_summary(summary)

    analysis = analyzer.analyze_paths([summary])

    assert analysis["privacy"] == analyzer.OUTPUT_PRIVACY
    assert analysis["aggregate"]["totals"] == {
        "parent_count": 2,
        "frame_count": 3,
        "audio_bytes": 51_200,
        "sample_count": 25_600,
        "full_window_count": 80,
        "partial_window_count": 0,
        "duration_ms": 1_600.0,
        "analysis_window_count": 80,
    }
    primary = _primary(analysis["aggregate"])
    assert primary["parent_counts"] == {
        "observed": 2,
        "all_low_energy": 1,
        "with_active_energy": 1,
        "with_leading_low_energy": 2,
        "with_trailing_low_energy": 1,
        "with_internal_low_energy": 1,
    }
    assert primary["duration_ms"] == {
        "active": 620.0,
        "leading_low_energy": 700.0,
        "trailing_low_energy": 260.0,
        "combined_edge_low_energy": 960.0,
        "internal_low_energy": 20.0,
        "total_low_energy": 980.0,
    }
    assert primary["percent_of_audio"]["combined_edge_low_energy"] == 60.0
    assert primary["edge_exclusion_counterfactual"] == {
        "excluded_sample_count": 15_360,
        "excluded_duration_ms": 960.0,
        "excluded_percent_of_audio": 60.0,
        "remaining_sample_count": 10_240,
        "remaining_duration_ms": 640.0,
        "remaining_fraction": 0.4,
    }
    assert primary["per_parent_duration_ms"][
        "combined_edge_low_energy"
    ] == {
        "observation_count": 2,
        "min": 360.0,
        "p50": 360.0,
        "p95": 600.0,
        "max": 600.0,
        "mean": 480.0,
        "cumulative": 960.0,
    }
    assert primary["parent_edge_threshold_counts"] == [
        {
            "minimum_duration_ms": 100,
            "leading": 2,
            "trailing": 1,
            "either_single_edge": 2,
            "combined_edges": 2,
        },
        {
            "minimum_duration_ms": 250,
            "leading": 1,
            "trailing": 1,
            "either_single_edge": 2,
            "combined_edges": 2,
        },
        {
            "minimum_duration_ms": 500,
            "leading": 1,
            "trailing": 0,
            "either_single_edge": 1,
            "combined_edges": 1,
        },
    ]
    assert analysis["aggregate"]["processing"][
        "all_samples_gate_passed"
    ] is True
    assert analysis["aggregate"]["processing"]["weighted_mean_ms"] == 2.0

    serialized = json.dumps(analysis)
    assert PRIVATE_MARKER not in serialized
    assert '"parent_sequence_id":' not in serialized
    assert '"stream_generation":' not in serialized
    assert "silence" not in serialized.lower()
    markdown = analyzer.render_markdown(analysis)
    assert PRIVATE_MARKER not in markdown
    assert "silence" not in markdown.lower()
    assert "Combined edges" in markdown
    assert "3.000 / 3.000 ms" in markdown


def test_multiple_samples_use_neutral_ordinals_and_weight_scan_aggregate(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / f"sample-{index}.json" for index in (1, 2)]
    _write_generated_summary(paths[0], processing_durations_ms=[1, 2, 3])
    _write_generated_summary(paths[1], processing_durations_ms=[2, 4, 6])

    analysis = analyzer.analyze_paths(paths)

    assert [sample["sample_index"] for sample in analysis["samples"]] == [1, 2]
    assert analysis["aggregate"]["totals"]["parent_count"] == 4
    assert analysis["aggregate"]["totals"]["duration_ms"] == 3_200.0
    processing = analysis["aggregate"]["processing"]
    assert processing["frame_count"] == 6
    assert processing["total_ms"] == 18.0
    assert processing["weighted_mean_ms"] == 3.0
    assert processing["maximum_ms"] == 6.0
    assert processing["per_sample_p95_ms"]["p50"] == 3.0
    assert processing["per_sample_p95_ms"]["p95"] == 6.0


def test_processing_gate_failure_is_reported_not_reclassified(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "summary.json"
    _write_generated_summary(
        summary,
        processing_durations_ms=[1.0, 2.0, 30.0],
    )

    analysis = analyzer.analyze_paths([summary])

    processing = analysis["aggregate"]["processing"]
    assert processing["all_samples_gate_passed"] is False
    assert processing["maximum_ms"] == 30.0
    assert "| FAIL |" in analyzer.render_markdown(analysis)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value.__setitem__(
                "translation_completed",
                False,
            ),
            "clean completed terminal",
        ),
        (
            lambda value: value.__setitem__(
                "synthesized_pcm_silence_requested",
                False,
            ),
            "diagnostic was requested",
        ),
        (
            lambda value: value["backend_config"][
                "stagedConfig"
            ].__setitem__("telemetrySchemaVersion", 2),
            "schema-3 incremental publication",
        ),
        (
            lambda value: value["backend_config"][
                "stagedConfig"
            ].__setitem__("ttsIncrementalPublishEnabled", False),
            "schema-3 incremental publication",
        ),
        (
            lambda value: value["audio_metadata_observation"].__setitem__(
                "paired_frames",
                99,
            ),
            "paired frame count",
        ),
        (
            lambda value: value[
                "synthesized_pcm_silence_processing"
            ].__setitem__("frame_count", 99),
            "frame_count does not reconcile",
        ),
        (
            lambda value: value["synthesized_pcm_silence"].__setitem__(
                "unexpected_private_field",
                PRIVATE_MARKER,
            ),
            "observation fields are invalid",
        ),
    ],
)
def test_loader_fails_closed_on_incomplete_or_inconsistent_evidence(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    summary = tmp_path / "summary.json"
    _write_generated_summary(summary)
    value = json.loads(summary.read_text(encoding="utf-8"))
    mutation(value)
    summary.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        analyzer.analyze_paths([summary])


def test_loader_rejects_nonstandard_json_numbers(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_generated_summary(summary)
    text = summary.read_text(encoding="utf-8")
    summary.write_text(
        text.replace('"average_drift_sec": 0.0', '"average_drift_sec": NaN'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-standard JSON constant"):
        analyzer.analyze_paths([summary])


def test_builder_rejects_mixed_methods() -> None:
    observation = _observation()
    changed = copy.deepcopy(observation)
    changed["method"] = {
        **changed["method"],
        "window_ms": 10,
    }
    processing = build_synthesized_pcm_silence_processing([1, 2, 3])

    with pytest.raises(ValueError, match="method.window_ms is invalid"):
        analyzer.build_low_energy_analysis(
            [
                analyzer.LowEnergySample(1, observation, processing),
                analyzer.LowEnergySample(2, changed, processing),
            ]
        )


def test_analyze_paths_rejects_duplicate_inputs(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_generated_summary(summary)

    with pytest.raises(ValueError, match="must be unique"):
        analyzer.analyze_paths([summary, summary])


def test_cli_writes_atomic_json_and_markdown(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "summary.json"
    json_output = tmp_path / "reports" / "analysis.json"
    markdown_output = tmp_path / "reports" / "analysis.md"
    _write_generated_summary(summary)

    assert (
        analyzer.main(
            [
                str(summary),
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(markdown_output),
            ]
        )
        == 0
    )

    report = json.loads(json_output.read_text(encoding="utf-8"))
    assert report["analysis_type"] == analyzer.ANALYSIS_TYPE
    assert markdown_output.read_text(encoding="utf-8").startswith(
        "# Synthesized PCM Low-Energy Analysis\n"
    )


def test_cli_stages_complete_output_set_before_replacing_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = tmp_path / "summary.json"
    json_output = tmp_path / "analysis.json"
    markdown_output = tmp_path / "analysis.md"
    _write_generated_summary(summary)
    json_output.write_text("old-json", encoding="utf-8")
    markdown_output.write_text("old-markdown", encoding="utf-8")
    original_stage = analyzer._stage_output
    calls = 0

    def fail_second_stage(path: Path, content: str) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic staging failure")
        return original_stage(path, content)

    monkeypatch.setattr(analyzer, "_stage_output", fail_second_stage)

    with pytest.raises(SystemExit, match="2"):
        analyzer.main(
            [
                str(summary),
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(markdown_output),
            ]
        )
    assert json_output.read_text(encoding="utf-8") == "old-json"
    assert markdown_output.read_text(encoding="utf-8") == "old-markdown"


@pytest.mark.parametrize("alias_input", [True, False])
def test_cli_rejects_output_aliasing(
    tmp_path: Path,
    alias_input: bool,
) -> None:
    summary = tmp_path / "summary.json"
    _write_generated_summary(summary)
    json_output = summary if alias_input else tmp_path / "same"
    markdown_output = (
        tmp_path / "different" if alias_input else json_output
    )

    with pytest.raises(SystemExit, match="2"):
        analyzer.parse_cli_args(
            [
                str(summary),
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(markdown_output),
            ]
        )


def test_cli_without_outputs_prints_markdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = tmp_path / "summary.json"
    _write_generated_summary(summary)
    capsys.readouterr()

    assert analyzer.main([str(summary)]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("# Synthesized PCM Low-Energy Analysis\n")
    assert PRIVATE_MARKER not in captured.out
