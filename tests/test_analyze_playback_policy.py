import csv
import json
import math
from pathlib import Path

import pytest

from analyze_playback_policy import (
    AudioFrameAttribution,
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

PROTOCOL_V1_CSV_HEADER = CSV_HEADER + [
    "protocol_version",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "source_start_ms",
    "source_end_ms",
    "source_end_to_receipt_ms",
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


def write_protocol_v1_trace(path):
    rows = [
        [
            "client", "chunk_sent", "0", "0", "0.0", "32000",
            "", "", "", "", "", "", "",
        ],
        [
            "client", "input_ended", "6000", "1", "6.0", "0",
            "", "", "", "", "", "", "",
        ],
        [
            "client", "audio_received", "1000", "0", "1.0", "32000",
            "1", "1", "0", "0", "0", "500", "500",
        ],
        [
            "client", "audio_received", "1100", "1", "1.1", "32000",
            "1", "1", "0", "1", "0", "500", "600",
        ],
        [
            "client", "audio_received", "1200", "2", "1.2", "32000",
            "1", "1", "1", "0", "", "1500", "-300",
        ],
        [
            "client", "audio_received", "1300", "3", "1.3", "32000",
            "1", "1", "1", "1", "", "1500", "-200",
        ],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PROTOCOL_V1_CSV_HEADER)
        writer.writerows(rows)


def mutate_protocol_rows(path, mutation):
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    mutation(
        [row for row in rows if row["stage"] == "audio_received"]
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROTOCOL_V1_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def write_protocol_v1_summary(
    csv_path,
    *,
    protocol_version=1,
    stream_generation=1,
    paired_frames=4,
    completed_parents=2,
    input_sample_zero_timestamp_ms=0.004,
):
    summary_path = csv_path.with_name(
        csv_path.name.removesuffix("_results.csv") + "_summary.json"
    )
    summary_path.write_text(
        json.dumps(
            {
                "audio_metadata_observation": {
                    "protocol_version": protocol_version,
                    "stream_generation": stream_generation,
                    "paired_frames": paired_frames,
                    "completed_parents": completed_parents,
                    "input_sample_zero_timestamp_ms": (
                        input_sample_zero_timestamp_ms
                    ),
                }
            }
        ),
        encoding="utf-8",
    )
    return summary_path


def write_four_parent_protocol_v1_trace(path):
    rows = [
        [
            "client", "chunk_sent", "0", "0", "0.0", "32000",
            "", "", "", "", "", "", "",
        ],
        [
            "client", "input_ended", "5000", "1", "5.0", "0",
            "", "", "", "", "", "", "",
        ],
    ]
    for parent, (arrival_ms, source_start_ms, source_end_ms, receipt_ms) in (
        (0, (1000, 0, 500, 500)),
        (1, (1100, 500, 1000, 100)),
        (2, (1200, 1000, 1500, -300)),
        (3, (1300, 1500, 2000, -700)),
    ):
        rows.append(
            [
                "client", "audio_received", str(arrival_ms), str(parent),
                str(arrival_ms / 1000), "32000", "1", "7",
                str(parent), "0", str(source_start_ms),
                str(source_end_ms), str(receipt_ms),
            ]
        )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PROTOCOL_V1_CSV_HEADER)
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


def test_load_event_trace_preserves_protocol_v1_frame_attribution(tmp_path):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)

    trace = load_event_trace(path)

    assert trace.frame_attributions == (
        AudioFrameAttribution(
            protocol_version=1,
            stream_generation=1,
            parent_sequence_id=0,
            audio_frame_id=0,
            source_start_ms=0.0,
            source_end_ms=500.0,
            source_end_to_receipt_ms=500.0,
        ),
        AudioFrameAttribution(
            protocol_version=1,
            stream_generation=1,
            parent_sequence_id=0,
            audio_frame_id=1,
            source_start_ms=0.0,
            source_end_ms=500.0,
            source_end_to_receipt_ms=600.0,
        ),
        AudioFrameAttribution(
            protocol_version=1,
            stream_generation=1,
            parent_sequence_id=1,
            audio_frame_id=0,
            source_start_ms=None,
            source_end_ms=1500.0,
            source_end_to_receipt_ms=-300.0,
        ),
        AudioFrameAttribution(
            protocol_version=1,
            stream_generation=1,
            parent_sequence_id=1,
            audio_frame_id=1,
            source_start_ms=None,
            source_end_ms=1500.0,
            source_end_to_receipt_ms=-200.0,
        ),
    )


def test_protocol_v1_rejects_untrusted_header_address_values(tmp_path):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)

    def corrupt(rows):
        rows[0].update(
            protocol_version="999",
            parent_sequence_id="12",
            audio_frame_id="4",
        )

    mutate_protocol_rows(path, corrupt)

    with pytest.raises(ValueError, match="protocol_version must equal 1"):
        load_event_trace(path)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("partial_address", "must either all be present or all be absent"),
        ("mixed_legacy", "every client audio_received row"),
        ("zero_generation", "stream_generation must be a positive integer"),
        ("generation_change", "stream_generation changed"),
        ("parent_gap", "parent_sequence_id must be contiguous from 0"),
        ("frame_gap", "audio_frame_id must be contiguous from 0"),
        ("source_range_change", "inconsistent source ranges"),
        ("parent_reentry", "re-entered after a later parent began"),
    ],
)
def test_protocol_v1_rejects_malformed_frame_sequences(
    tmp_path,
    case,
    message,
):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)

    def corrupt(rows):
        if case == "partial_address":
            rows[0]["stream_generation"] = ""
        elif case == "mixed_legacy":
            for field_name in PROTOCOL_V1_CSV_HEADER[len(CSV_HEADER):]:
                rows[1][field_name] = ""
        elif case == "zero_generation":
            for row in rows:
                row["stream_generation"] = "0"
        elif case == "generation_change":
            rows[1]["stream_generation"] = "2"
        elif case == "parent_gap":
            rows[2]["parent_sequence_id"] = "2"
            rows[3]["parent_sequence_id"] = "2"
        elif case == "frame_gap":
            rows[1]["audio_frame_id"] = "2"
        elif case == "source_range_change":
            rows[1]["source_end_ms"] = "600"
            rows[1]["source_end_to_receipt_ms"] = "500"
        elif case == "parent_reentry":
            rows[3]["parent_sequence_id"] = "0"
            rows[3]["audio_frame_id"] = "2"
        else:  # pragma: no cover - parameter table is exhaustive
            raise AssertionError(case)

    mutate_protocol_rows(path, corrupt)

    with pytest.raises(ValueError, match=message):
        load_event_trace(path)


def test_source_frontier_keeps_end_only_parent_in_mechanical_distributions(
    tmp_path,
):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)

    result = analyze_trace(load_event_trace(path))
    fixed = result["fixed_1x"]["source_frontier_to_scheduled_playback"]
    adaptive = result["adaptive"]["source_frontier_to_scheduled_playback"]

    assert fixed["attributed_frame_count"] == 4
    assert fixed["attributed_parent_count"] == 2
    assert fixed["measured_parent_envelope_count"] == 2
    assert fixed["completion_proven_by_trace_csv"] is False
    assert fixed[
        "source_end_to_first_frame_scheduled_start_seconds"
    ] == {
        "count": 2,
        "p50": 0.5,
        "p95": 1.5,
        "max": 1.5,
    }
    assert fixed[
        "source_end_to_last_frame_scheduled_end_seconds"
    ] == {
        "count": 2,
        "p50": 2.5,
        "p95": 3.5,
        "max": 3.5,
    }
    assert fixed["accumulated_drift"][
        "first_frame_scheduled_start_seconds"
    ] == {
        "count": 1,
        "minimum_count_required": 4,
        "sufficient_sample_count": False,
        "quartile_count": 0,
        "first_quartile_median_seconds": None,
        "final_quartile_median_seconds": None,
        "final_minus_first_seconds": None,
    }
    assert fixed["accumulated_drift"][
        "last_frame_scheduled_end_seconds"
    ]["final_minus_first_seconds"] is None
    assert fixed["semantic_proxy_eligibility"] == {
        "status": "partially_eligible_parent_range_proxy",
        "eligible_parent_count": 1,
        "ineligible_measured_parent_count": 1,
        "criterion": (
            "both source_start_ms and source_end_ms exist on every frame "
            "in the measured parent"
        ),
    }
    assert fixed["claim_scope"]["dac_or_acoustic_audibility_measured"] is False
    assert fixed["claim_scope"]["exact_joke_or_punchline_delay_measured"] is False
    assert adaptive[
        "source_end_to_first_frame_scheduled_start_seconds"
    ]["count"] == 2


def test_frontier_drift_requires_four_bounded_source_range_parents(tmp_path):
    path = tmp_path / "four_parent_results.csv"
    write_four_parent_protocol_v1_trace(path)

    frontier = analyze_trace(load_event_trace(path))["fixed_1x"][
        "source_frontier_to_scheduled_playback"
    ]
    drift = frontier["accumulated_drift"][
        "first_frame_scheduled_start_seconds"
    ]

    assert drift == {
        "count": 4,
        "minimum_count_required": 4,
        "sufficient_sample_count": True,
        "quartile_count": 1,
        "first_quartile_median_seconds": 0.5,
        "final_quartile_median_seconds": 2.0,
        "final_minus_first_seconds": 1.5,
    }


def test_legacy_trace_reports_frontier_metrics_as_unavailable(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)

    result = analyze_trace(load_event_trace(path))
    frontier = result["fixed_1x"][
        "source_frontier_to_scheduled_playback"
    ]

    assert frontier["availability"] == "unavailable"
    assert frontier["attributed_parent_count"] == 0
    assert frontier[
        "source_end_to_first_frame_scheduled_start_seconds"
    ] == {
        "count": 0,
        "p50": None,
        "p95": None,
        "max": None,
    }
    assert frontier["semantic_proxy_eligibility"]["status"] == "unavailable"


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
    assert analysis["traces"][0]["audio_metadata_summary_binding"] == {
        "required": False,
        "status": "not_applicable_legacy_trace",
        "passed": None,
        "completion_proven_by_trace_csv": False,
        "completion_evidence": None,
    }
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


def test_build_analysis_binds_protocol_v1_trace_to_adjacent_summary(tmp_path):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)
    summary_path = write_protocol_v1_summary(path)

    analysis = build_analysis([path])
    binding = analysis["traces"][0]["audio_metadata_summary_binding"]

    assert binding == {
        "required": True,
        "status": "bound",
        "passed": True,
        "summary_json": summary_path.name,
        "protocol_version": 1,
        "stream_generation": 1,
        "paired_frame_count": 4,
        "completed_parent_count": 2,
        "input_sample_zero_timestamp_ms": 0.004,
        "receipt_delay_validation": {
            "performed": True,
            "passed": True,
            "validated_frame_count": 4,
            "absolute_tolerance_ms": 0.006001,
        },
        "completion_proven_by_trace_csv": False,
        "completion_evidence": (
            "adjacent_summary.audio_metadata_observation"
        ),
    }
    assert analysis["traces"][0]["fixed_1x"][
        "source_frontier_to_scheduled_playback"
    ]["completion_proven_by_trace_csv"] is False


def test_build_analysis_requires_summary_for_protocol_v1_trace(tmp_path):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)

    with pytest.raises(
        ValueError,
        match="protocol-v1 attribution requires adjacent",
    ):
        build_analysis([path])


@pytest.mark.parametrize(
    "input_sample_zero_timestamp_ms",
    [None, -0.001, math.inf, "0", True],
)
def test_build_analysis_rejects_invalid_summary_sample_zero(
    tmp_path,
    input_sample_zero_timestamp_ms,
):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)
    write_protocol_v1_summary(
        path,
        input_sample_zero_timestamp_ms=(
            input_sample_zero_timestamp_ms
        ),
    )

    with pytest.raises(
        ValueError,
        match=(
            "input_sample_zero_timestamp_ms must be a finite "
            "non-negative number"
        ),
    ):
        build_analysis([path])


def test_build_analysis_rejects_fabricated_source_receipt_delay(tmp_path):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)

    def fabricate(rows):
        rows[0]["source_end_to_receipt_ms"] = "999999"

    mutate_protocol_rows(path, fabricate)
    write_protocol_v1_summary(path)

    with pytest.raises(
        ValueError,
        match=(
            "source_end_to_receipt_ms does not bind to the adjacent "
            "summary input sample-zero clock"
        ),
    ):
        build_analysis([path])


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("protocol_version", 2),
        ("stream_generation", 2),
        ("paired_frames", 3),
        ("completed_parents", 1),
    ],
)
def test_build_analysis_rejects_audio_metadata_summary_mismatch(
    tmp_path,
    override,
    value,
):
    path = tmp_path / "protocol_results.csv"
    write_protocol_v1_trace(path)
    kwargs = {override: value}
    write_protocol_v1_summary(path, **kwargs)

    with pytest.raises(ValueError, match="does not bind"):
        build_analysis([path])


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


def test_default_analysis_exposes_versioned_frontier_claim_scope(tmp_path):
    path = tmp_path / "example_results.csv"
    write_trace(path)

    analysis = build_analysis([path])
    markdown = render_markdown(analysis)

    assert analysis["schema_version"] == 2
    assert "capacity_sweep" not in analysis
    assert analysis["semantics"][
        "source_frontier_is_scheduled_digital_playback_not_audibility"
    ] is True
    assert analysis["semantics"][
        "exact_joke_or_punchline_delay_requires_common_clock_review"
    ] is True
    assert "## Source-frontier scheduling projection" in markdown
    assert "do not measure DAC output or acoustic audibility" in markdown
    assert "not an exact joke or punchline measurement" in markdown


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
