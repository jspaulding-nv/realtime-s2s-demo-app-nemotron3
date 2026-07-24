import csv
import hashlib
import json
import math
from pathlib import Path

import pytest

from analyze_playback_policy import (
    _rolling_arrival_rate_p95,
    analyze_trace,
    build_analysis,
    load_event_trace,
    normalize_capacity_sweep_options,
    parse_cli_args,
    render_markdown,
)
from playback_simulation import AudioChunk


CSV_HEADER = [
    "source",
    "stage",
    "timestamp_ms",
    "chunk_index",
    "source_position_sec",
    "audio_bytes",
]


def write_trace(path):
    rows = [
        ["client", "chunk_sent", "0", "0", "0.0", "9600"],
        ["backend", "audio_received", "50", "0", "0.0", "9600"],
        ["client", "audio_received", "2000", "1", "2.0", "32000"],
        ["client", "chunk_sent", "1000", "1", "0.3", "9600"],
        ["client", "audio_received", "1500", "0", "1.0", "32000"],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)


def write_capacity_trace(path):
    rows = [
        ["client", "chunk_sent", "0", "0", "0.0", "32000"],
        ["client", "input_ended", "50000", "1", "50.0", "0"],
        ["client", "audio_received", "0", "0", "0.0", "192000"],
        ["client", "audio_received", "0", "1", "0.0", "128000"],
        ["client", "audio_received", "10000", "2", "0.0", "320000"],
        ["client", "audio_received", "40000", "3", "0.0", "640000"],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)


def test_load_event_trace_filters_backend_and_sorts_client_arrivals(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)

    trace = load_event_trace(path)

    assert trace.input_end_seconds == 1.3
    assert trace.input_boundary_source == "estimated_last_chunk_end"
    assert trace.legacy_last_chunk_start_seconds == 1.0
    assert len(trace.chunks) == 2
    assert [chunk.arrival_seconds for chunk in trace.chunks] == [1.5, 2.0]
    assert [chunk.source_index for chunk in trace.chunks] == [0, 1]
    assert [chunk.duration_seconds for chunk in trace.chunks] == [1.0, 1.0]
    assert len(trace.sha256) == 64


def test_load_event_trace_prefers_explicit_input_end_boundary(tmp_path):
    path = tmp_path / "explicit_end_results.csv"
    rows = [
        ["client", "chunk_sent", "1000", "0", "0.0", "9600"],
        ["client", "audio_received", "1500", "0", "1.0", "32000"],
        ["client", "input_ended", "1300", "1", "0.3", "0"],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)

    trace = load_event_trace(path)

    assert trace.input_end_seconds == 1.3


def test_analyze_trace_reproduces_fixed_tail_and_preserves_every_chunk(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)
    trace = load_event_trace(path)

    result = analyze_trace(trace, recorded_fixed_tail_seconds=2.5)

    assert result["fixed_1x"]["listener_tail_seconds"] == 2.2
    assert result["reproduced_fixed_tail_delta_seconds"] == -0.3
    assert result["legacy_start_boundary_recorded_delta_seconds"] == 0.0
    assert result["fixed_1x"]["chunks_dropped"] == 0
    assert result["adaptive"]["chunks_dropped"] == 0
    assert result["adaptive"]["chunks_scheduled"] == 2


def test_build_analysis_validates_adjacent_recorded_summary(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)
    summary_path = tmp_path / "example_summary.json"
    summary_path.write_text(
        json.dumps({"playback_tail_sec": 2.5}), encoding="utf-8"
    )

    analysis = build_analysis([path])

    assert analysis["aggregate"]["trace_count"] == 1
    assert analysis["semantics"]["preserve_every_chunk"] is True
    assert analysis["traces"][0]["recorded_fixed_listener_tail_seconds"] == 2.5
    assert analysis["traces"][0]["recorded_tail_validation"] == {
        "performed": True,
        "passed": True,
        "boundary": "legacy_last_chunk_start_compatibility",
        "note": (
            "recorded tail used the historical last-chunk-start boundary; "
            "reported fixed/adaptive metrics use the corrected "
            "last-chunk-end boundary"
        ),
    }
    assert "Every translated chunk is retained" in render_markdown(analysis)


def test_build_analysis_rejects_recorded_tail_mismatch(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)
    summary_path = tmp_path / "example_summary.json"
    summary_path.write_text(
        json.dumps({"playback_tail_sec": 20.0}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="differs from recorded"):
        build_analysis([path])


def test_capacity_sweep_options_dedupe_and_default_scale():
    assert normalize_capacity_sweep_options(
        [1.0, 1.1, 1.0, 1.2, 1.1],
        None,
    ) == ((1.0, 1.1, 1.2), (1.0,))
    assert normalize_capacity_sweep_options(
        [1.0],
        [1.0, 0.9, 1.0],
    ) == ((1.0,), (1.0, 0.9))
    assert normalize_capacity_sweep_options(None, None) == ((), ())


@pytest.mark.parametrize("invalid", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_capacity_sweep_options_reject_non_positive_or_non_finite(invalid):
    with pytest.raises(ValueError, match="finite and positive"):
        normalize_capacity_sweep_options([invalid], None)
    with pytest.raises(ValueError, match="finite and positive"):
        normalize_capacity_sweep_options([1.0], [invalid])


def test_capacity_sweep_options_reject_scales_without_rates():
    with pytest.raises(ValueError, match="require at least one constant rate"):
        normalize_capacity_sweep_options(None, [1.0])


def test_parse_cli_args_collects_repeatable_sweep_options():
    args = parse_cli_args(
        [
            "--constant-rate",
            "1.0",
            "--constant-rate",
            "1.10",
            "--constant-rate",
            "1.0",
            "--media-duration-scale",
            "0.95",
            "--media-duration-scale",
            "0.95",
        ]
    )

    assert args.constant_rates == (1.0, 1.1)
    assert args.media_duration_scales == (0.95,)


def test_parse_cli_args_rejects_invalid_sweep_option(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_cli_args(["--constant-rate", "nan"])

    assert exc_info.value.code == 2
    assert "finite and positive" in capsys.readouterr().err


def test_rolling_arrival_rate_uses_wall_clock_grid_and_half_open_windows():
    chunks = (
        AudioChunk(arrival_seconds=0.0, duration_seconds=1.0),
        AudioChunk(arrival_seconds=30.0, duration_seconds=10.0),
        AudioChunk(arrival_seconds=30.0, duration_seconds=20.0),
    )

    assert _rolling_arrival_rate_p95(
        chunks,
        window_seconds=30.0,
        observation_end_seconds=31.0,
    ) == 1.0


def test_rolling_arrival_rate_excludes_post_input_drain_for_short_trace():
    chunks = (
        AudioChunk(arrival_seconds=10.0, duration_seconds=5.0),
        AudioChunk(arrival_seconds=25.0, duration_seconds=30.0),
    )

    assert _rolling_arrival_rate_p95(
        chunks,
        window_seconds=30.0,
        observation_end_seconds=20.0,
    ) == pytest.approx(5.0 / 30.0)


@pytest.mark.parametrize(
    ("window_seconds", "observation_end_seconds", "message"),
    [
        (0.0, 10.0, "window"),
        (math.nan, 10.0, "window"),
        (30.0, -1.0, "observation end"),
        (30.0, math.inf, "observation end"),
    ],
)
def test_rolling_arrival_rate_rejects_invalid_bounds(
    window_seconds,
    observation_end_seconds,
    message,
):
    with pytest.raises(ValueError, match=message):
        _rolling_arrival_rate_p95(
            (),
            window_seconds=window_seconds,
            observation_end_seconds=observation_end_seconds,
        )


def test_build_analysis_adds_capacity_sweep_values_only_when_requested(tmp_path):
    path = tmp_path / "capacity_results.csv"
    write_capacity_trace(path)

    analysis = build_analysis(
        [path],
        constant_rates=[1.0, 2.0, 1.0],
        media_duration_scales=[1.0, 2.0, 1.0],
    )
    sweep = analysis["capacity_sweep"]
    trace = sweep["traces"][0]

    assert sweep["constant_rates"] == [1.0, 2.0]
    assert sweep["media_duration_scales"] == [1.0, 2.0]
    assert sweep["semantics"]["rolling_window_interval"] == "[s, s + window)"
    assert len(trace["scenarios"]) == 4
    assert trace["burst_diagnostics"] == {
        "translated_audio_chunk_duration_seconds": {
            "p50": 6.0,
            "p95": 20.0,
            "max": 20.0,
        },
        "rolling_translated_media_arrival_rate_p95_x_realtime": {
            "30_seconds": 0.666667,
            "60_seconds": 0.666667,
            "300_seconds": 0.133333,
        },
    }

    base = trace["scenarios"][0]
    assert base == {
        "constant_rate": 1.0,
        "media_duration_scale": 1.0,
        "no_drop_listener_tail_seconds": 10.0,
        "time_weighted_queue_p50_seconds": 3.333333,
        "time_weighted_queue_p95_seconds": 17.0,
        "peak_queue_depth_seconds": 20.0,
        "seconds_above_10_seconds": 10.0,
        "percent_playback_window_above_10_seconds": 16.666667,
        "chunks_dropped": 0,
    }
    scale_only = trace["scenarios"][1]
    rate_only = trace["scenarios"][2]
    assert scale_only["peak_queue_depth_seconds"] > base["peak_queue_depth_seconds"]
    assert scale_only["no_drop_listener_tail_seconds"] > (
        base["no_drop_listener_tail_seconds"]
    )
    assert rate_only["peak_queue_depth_seconds"] < base["peak_queue_depth_seconds"]
    assert rate_only["no_drop_listener_tail_seconds"] < (
        base["no_drop_listener_tail_seconds"]
    )
    assert trace["scenarios"][3] == {
        **base,
        "constant_rate": 2.0,
        "media_duration_scale": 2.0,
    }


def test_capacity_sweep_markdown_is_optional(tmp_path):
    path = tmp_path / "capacity_results.csv"
    write_capacity_trace(path)

    default_markdown = render_markdown(build_analysis([path]))
    sweep_markdown = render_markdown(
        build_analysis([path], constant_rates=[1.1])
    )

    assert "Offline constant-rate capacity sweep" not in default_markdown
    assert "## Offline constant-rate capacity sweep" in sweep_markdown
    assert "No-drop tail" in sweep_markdown
    assert "`[s, s + window)`" in sweep_markdown
    assert "whole wall-clock seconds" in sweep_markdown


def test_default_analysis_and_markdown_remain_byte_compatible(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)

    analysis = build_analysis([path])
    json_bytes = (json.dumps(analysis, indent=2) + "\n").encode()
    markdown_bytes = render_markdown(analysis).encode()

    assert "capacity_sweep" not in analysis
    assert hashlib.sha256(json_bytes).hexdigest() == (
        "b3527728b8ac84e3fec78869d37ef14f9253ce50704c4b19065e387ef04b0ce0"
    )
    assert hashlib.sha256(markdown_bytes).hexdigest() == (
        "2e1c2b277b7062a315484955fe70da586619de23169cbffb0d030630c3493337"
    )


def test_load_event_trace_requires_event_columns(tmp_path):
    path = tmp_path / "bad_results.csv"
    path.write_text("source,stage\nclient,chunk_sent\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required columns"):
        load_event_trace(path)


def test_all_real_traces_reproduce_fixed_tail_when_available():
    """Local regression over ignored captures; skipped in a fresh checkout."""

    paths = sorted(Path("test_results_nemotron").glob("*_results.csv"))
    if not paths:
        pytest.skip("ignored Nemotron trace captures are not present")

    analysis = build_analysis(paths)
    assert len(analysis["traces"]) == 3
    for trace in analysis["traces"]:
        assert trace["recorded_tail_validation"]["passed"] is True
        boundary = trace["recorded_tail_validation"]["boundary"]
        assert boundary in {
            "corrected_input_boundary",
            "legacy_last_chunk_start_compatibility",
        }
