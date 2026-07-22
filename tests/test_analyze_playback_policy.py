import csv
import json
import math
from pathlib import Path

import pytest

from analyze_playback_policy import (
    analyze_trace,
    build_analysis,
    load_event_trace,
    render_markdown,
)


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


def test_load_event_trace_filters_backend_and_sorts_client_arrivals(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)

    trace = load_event_trace(path)

    assert trace.input_end_seconds == 1.0
    assert len(trace.chunks) == 2
    assert [chunk.arrival_seconds for chunk in trace.chunks] == [1.5, 2.0]
    assert [chunk.source_index for chunk in trace.chunks] == [0, 1]
    assert [chunk.duration_seconds for chunk in trace.chunks] == [1.0, 1.0]
    assert len(trace.sha256) == 64


def test_analyze_trace_reproduces_fixed_tail_and_preserves_every_chunk(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)
    trace = load_event_trace(path)

    result = analyze_trace(trace, recorded_fixed_tail_seconds=2.5)

    assert result["fixed_1x"]["listener_tail_seconds"] == 2.5
    assert result["reproduced_fixed_tail_delta_seconds"] == 0.0
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
        delta = trace["reproduced_fixed_tail_delta_seconds"]
        assert delta is not None
        assert math.isclose(delta, 0.0, abs_tol=0.005)
