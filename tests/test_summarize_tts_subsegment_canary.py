import json

import pytest

from summarize_tts_subsegment_canary import (
    PromotionGates,
    _promotion_gates_from_args,
    build_canary_summary,
    create_argument_parser,
    render_markdown,
)


MARKER = "PRIVATE_CANARY_MARKER"


def promotion_gates(**overrides):
    values = {
        "min_child_p95_reduction_percent": 50.0,
        "min_adaptive_queue_p95_reduction_percent": 25.0,
        "min_adaptive_listener_tail_reduction_percent": 30.0,
        "max_child_p95_seconds": 4.0,
        "max_child_max_seconds": 8.0,
        "max_output_ratio_increase_percent": 1.0,
        "max_first_audio_latency_increase_seconds": 0.5,
        "max_captured_listener_tail_increase_seconds": 5.0,
        "max_service_tail_lag_increase_seconds": 0.5,
        "max_adaptive_queue_p95_seconds": 10.0,
        "max_adaptive_time_above_10_percent": 1.0,
        "max_adaptive_urgent_source_percent": 50.0,
        "max_adaptive_urgent_source_increase_percent_points": 0.0,
        "max_tts_retries": 0,
    }
    values.update(overrides)
    return PromotionGates(**values)


def write_arm(root, cap):
    arm = root / f"cap-{cap}"
    arm.mkdir()
    parent_calls = 2
    child_calls = {
        0: 2,
        40: 4,
        45: 3,
        60: 3,
    }[cap]
    ratio = {
        0: 1.10,
        40: 1.09,
        45: 1.08,
        60: 1.07,
    }[cap]
    duration = {
        0: {"p50": 5.0, "p95": 8.0, "max": 12.0},
        40: {"p50": 2.0, "p95": 3.0, "max": 5.0},
        45: {"p50": 2.2, "p95": 3.5, "max": 6.0},
        60: {"p50": 2.8, "p95": 4.5, "max": 9.0},
    }[cap]
    adaptive_queue_p95 = {
        0: 9.0,
        40: 5.0,
        45: 6.0,
        60: 7.0,
    }[cap]
    adaptive_tail = {
        0: 8.0,
        40: 4.0,
        45: 5.0,
        60: 6.0,
    }[cap]

    pipeline = {
        "telemetry_schema_version": 1 if cap == 0 else 2,
        "tts_subsegmentation_enabled": cap > 0,
        "state": "closed",
        "outcome": "complete",
        "failure": None,
        "cleanup_errors": [],
        "incomplete_sequence_ids": [],
        "segments_emitted": parent_calls,
        "audio_segments_produced": child_calls,
        "completed_sequence_ids": [0, 1],
        "fillers_discarded": 0,
        "tts_retry_count": 0,
        "events": [
            {
                "stage": "asr",
                "event": "final",
                "asr_final_id": 0,
                "text_chars": 20,
                "source_start_ms": 0.0,
                "source_end_ms": 1000.0,
            },
            {
                "stage": "segmenter",
                "event": "emitted",
                "sequence_id": 0,
                "contributing_final_ids": [0],
                "emission_reason": "punctuation",
                "text_chars": 20,
                "source_start_ms": 0.0,
                "source_end_ms": 1000.0,
            },
            {
                "stage": "nmt",
                "event": "completed",
                "sequence_id": 0,
                "text_chars": 22,
            },
            {
                "stage": "asr",
                "event": "final",
                "asr_final_id": 1,
                "text_chars": 24,
                "source_start_ms": 1100.0,
                "source_end_ms": 2200.0,
            },
            {
                "stage": "segmenter",
                "event": "emitted",
                "sequence_id": 1,
                "contributing_final_ids": [1],
                "emission_reason": "flush",
                "text_chars": 24,
                "source_start_ms": 1100.0,
                "source_end_ms": 2200.0,
            },
            {
                "stage": "nmt",
                "event": "completed",
                "sequence_id": 1,
                "text_chars": 26,
            },
        ],
    }
    if cap > 0:
        pipeline.update(
            {
                "tts_subsegment_max_chars": cap,
                "tts_subsegments_produced": child_calls,
            }
        )
    summary = {
        "audio_path": MARKER,
        "backend_url": MARKER,
        "backend_config": {
            "sampleRate": 16000,
            "chunkSize": 4800,
            "channels": 1,
            "pipelineMode": "staged",
            "modelConfig": {
                "asr": {
                    "image": "asr:1",
                    "imageDigest": "sha256:" + "a" * 64,
                    "endpoint": MARKER,
                },
                "nmt": {
                    "image": "nmt:1",
                    "imageDigest": "sha256:" + "b" * 64,
                    "endpoint": MARKER,
                },
                "tts": {
                    "image": "tts:1",
                    "imageDigest": "sha256:" + "c" * 64,
                    "endpoint": MARKER,
                },
            },
            "stagedConfig": {
                "telemetrySchemaVersion": 1 if cap == 0 else 2,
                "ttsSubsegmentMaxChars": cap,
                "ttsSubsegmentationEnabled": cap > 0,
                "ttsSubsegmentMinChars": 12,
                "segmentMaxChars": 240,
                "segmentMaxAgeMs": 2000,
            },
            "private": MARKER,
        },
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "staged_pipeline": pipeline,
        "input_duration_sec": 100.0,
        "output_duration_sec": 100.0 * ratio,
        "output_to_input_duration_ratio": ratio,
        "first_audio_latency_sec": 1.0 + cap / 100.0,
        "playback_tail_sec": 10.0 + cap / 10.0,
        "tail_lag_sec": cap / 100.0,
        "audio_responses": child_calls,
        "chunks_sent": 334,
        "input_completed": True,
        "connection_lost": False,
        "drain_timed_out": False,
        "translation_completed": True,
        "server_error": "",
    }
    (arm / "capture_summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )

    model = {
        "schema_version": 1 if cap == 0 else 2,
        "privacy": {
            "contains_transcript_text": False,
            "contains_audio": False,
            "contains_input_paths_or_filenames": False,
            "contains_endpoints": False,
            "contains_session_ids": False,
            "private": MARKER,
        },
        "samples": [{"sample": "sample_01"}],
        "aggregate": {
            "observed_distribution": {
                "observation_count": child_calls,
                "audio_duration_seconds": duration,
            }
        },
        "targets": {
            "p95_audio_duration_seconds": 4.0,
            "observed_max_residual_envelope_seconds": 8.0,
        },
        "private": MARKER,
    }
    (arm / "tts_duration_model.json").write_text(
        json.dumps(model),
        encoding="utf-8",
    )

    def mode_metrics(*, adaptive, queue_p95, tail):
        return {
            "adaptive": adaptive,
            "chunks_scheduled": child_calls,
            "chunks_dropped": 0,
            "listener_tail_seconds": tail,
            "time_weighted_queue_p50_seconds": queue_p95 / 2,
            "time_weighted_queue_p95_seconds": queue_p95,
            "peak_queue_depth_seconds": queue_p95 + 1,
            "seconds_above_limit": 0.5,
            "percent_playback_window_above_limit": 0.5,
            "accelerated_source_percent": 25.0 if adaptive else 0.0,
            "urgent_source_percent": 20.0 if adaptive else 0.0,
            "max_continuous_urgent_playback_seconds": (
                2.0 if adaptive else 0.0
            ),
            "rate_source_duration_seconds": {
                "1.00x": 50.0,
                "1.10x": 50.0 if adaptive else 0.0,
            },
        }

    playback = {
        "schema_version": 1,
        "policy": {
            "limit_queue_seconds": 10.0,
        },
        "traces": [
            {
                "trace_csv": "capture_results.csv",
                "trace_sha256": MARKER,
                "translated_audio_seconds": 100.0 * ratio,
                "fixed_1x": mode_metrics(
                    adaptive=False,
                    queue_p95=12.0,
                    tail=14.0,
                ),
                "adaptive": mode_metrics(
                    adaptive=True,
                    queue_p95=adaptive_queue_p95,
                    tail=adaptive_tail,
                ),
                "private": MARKER,
            }
        ],
        "private": MARKER,
    }
    (arm / "playback_policy_analysis.json").write_text(
        json.dumps(playback),
        encoding="utf-8",
    )


def write_matrix(root):
    for cap in (0, 40, 45, 60):
        write_arm(root, cap)


def test_builds_deterministic_privacy_safe_cross_arm_summary(tmp_path):
    write_matrix(tmp_path)

    first = build_canary_summary(tmp_path)
    second = build_canary_summary(tmp_path)
    markdown = render_markdown(first)
    serialized = json.dumps(first)

    assert first == second
    assert first["schema_version"] == 2
    assert first["matched_design"] == {
        "passed": True,
        "control_arm": "arm_01",
        "capture_count_per_arm": 1,
        "corresponding_capture_order_matched": True,
        "input_references_matched": True,
        "input_chunk_counts_matched": True,
        "input_durations_matched": True,
        "input_duration_tolerance_seconds": 0.000001,
        "backend_config_provenance_matched": True,
        "playback_policy_provenance_matched": True,
        "upstream_asr_structure_matched": True,
        "parent_counts_and_segmentation_matched": True,
        "nmt_parent_structure_matched": True,
        "intended_backend_config_differences": [
            "stagedConfig.telemetrySchemaVersion",
            "stagedConfig.ttsSubsegmentMaxChars",
            "stagedConfig.ttsSubsegmentationEnabled",
        ],
    }
    assert [arm["arm"] for arm in first["arms"]] == [
        "arm_01",
        "arm_02",
        "arm_03",
        "arm_04",
    ]
    assert [arm["configured_cap_chars"] for arm in first["arms"]] == [
        0,
        40,
        45,
        60,
    ]
    strict = first["arms"][1]
    assert strict["parent_translation_calls"] == 2
    assert strict["tts_child_calls"] == 4
    assert strict["actual_child_duration_seconds"] == {
        "p50": 2.0,
        "p95": 3.0,
        "max": 5.0,
    }
    assert strict["output_to_input_duration_ratio"] == 1.09
    assert strict["playback"]["worst_case"]["adaptive"][
        "time_weighted_queue_p95_seconds"
    ] == 5.0
    assert strict["playback"]["worst_case"]["adaptive"][
        "percent_playback_window_above_10_seconds"
    ] == 0.5
    assert first["recommendation"] == {
        "explicit_gates_configured": False,
        "eligible_arms": [],
        "recommended_arm": None,
        "status": "no_automatic_winner_without_explicit_gates",
        "native_language_review_required": True,
    }
    assert MARKER not in serialized
    assert MARKER not in markdown
    assert str(tmp_path) not in serialized
    assert "capture_summary" not in serialized
    assert "No automatic winner" in markdown


def test_explicit_gates_select_only_from_passing_noncontrol_arms(tmp_path):
    write_matrix(tmp_path)
    gates = promotion_gates()

    summary = build_canary_summary(tmp_path, promotion_gates=gates)
    recommendation = summary["recommendation"]

    assert recommendation["eligible_arms"] == ["arm_02", "arm_03"]
    assert recommendation["recommended_arm"] == "arm_02"
    assert recommendation["status"] == "explicit_gates_selected_an_arm"
    assert recommendation["evaluations"][2]["arm"] == "arm_04"
    assert recommendation["evaluations"][2]["pass"] is False
    assert recommendation["evaluations"][2]["gates"]["child_p95"] is False
    assert recommendation["evaluations"][0]["gates"][
        "child_p95_relative_benefit"
    ] is True
    assert recommendation["evaluations"][0]["gates"][
        "adaptive_queue_relative_benefit"
    ] is True
    assert recommendation["evaluations"][0]["gates"][
        "first_audio_latency_nonregression"
    ] is True
    assert "arm_02" in render_markdown(summary)


def test_rejects_capture_that_does_not_report_directory_cap(tmp_path):
    write_matrix(tmp_path)
    path = tmp_path / "cap-40" / "capture_summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["backend_config"]["stagedConfig"]["ttsSubsegmentMaxChars"] = 45
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match directory cap"):
        build_canary_summary(tmp_path)


def test_rejects_capture_without_immutable_image_digest(tmp_path):
    write_matrix(tmp_path)
    path = tmp_path / "cap-40" / "capture_summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["backend_config"]["modelConfig"]["asr"]["imageDigest"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable sha256 digest"):
        build_canary_summary(tmp_path)


def test_rejects_capture_without_clean_staged_integrity(tmp_path):
    write_matrix(tmp_path)
    path = tmp_path / "cap-45" / "capture_summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_integrity"]["passed"] = False
    payload["staged_integrity"]["errors"] = ["synthetic failure"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="integrity did not pass"):
        build_canary_summary(tmp_path)


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda value: value.update(
                {
                    "input_duration_sec": 101.0,
                    "output_duration_sec": 109.0,
                    "output_to_input_duration_ratio": 109.0 / 101.0,
                }
            ),
            "input duration does not match control",
        ),
        (
            lambda value: value.update({"chunks_sent": 335}),
            "input chunk count does not match control",
        ),
            (
                lambda value: value["backend_config"]["modelConfig"]["asr"].update(
                    {"imageDigest": "sha256:" + "d" * 64}
                ),
                "configuration provenance does not match control",
            ),
        (
            lambda value: value["staged_pipeline"].update(
                {"fillers_discarded": 1}
            ),
            "upstream filler handling does not match control",
        ),
        (
            lambda value: value["staged_pipeline"]["events"][1].update(
                {"text_chars": 21}
            ),
            "parent segmentation does not match control",
        ),
        (
            lambda value: value["staged_pipeline"]["events"][2].update(
                {"text_chars": 23}
            ),
            "NMT parent structure does not match control",
        ),
    ],
)
def test_rejects_unmatched_corresponding_capture_evidence(
    tmp_path,
    mutation,
    error,
):
    write_matrix(tmp_path)
    path = tmp_path / "cap-40" / "capture_summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        build_canary_summary(tmp_path)


def test_rejects_unmatched_capture_count(tmp_path):
    write_matrix(tmp_path)
    arm = tmp_path / "cap-60"
    source = json.loads(
        (arm / "capture_summary.json").read_text(encoding="utf-8")
    )
    (arm / "zz_summary.json").write_text(
        json.dumps(source),
        encoding="utf-8",
    )

    model_path = arm / "tts_duration_model.json"
    model = json.loads(model_path.read_text(encoding="utf-8"))
    model["samples"].append({"sample": "sample_02"})
    model["aggregate"]["observed_distribution"]["observation_count"] *= 2
    model_path.write_text(json.dumps(model), encoding="utf-8")

    playback_path = arm / "playback_policy_analysis.json"
    playback = json.loads(playback_path.read_text(encoding="utf-8"))
    extra_trace = dict(playback["traces"][0])
    extra_trace["trace_csv"] = "zz_results.csv"
    playback["traces"].append(extra_trace)
    playback_path.write_text(json.dumps(playback), encoding="utf-8")

    with pytest.raises(ValueError, match="capture count does not match"):
        build_canary_summary(tmp_path)


def test_rejects_unmatched_playback_policy_provenance(tmp_path):
    write_matrix(tmp_path)
    path = tmp_path / "cap-45" / "playback_policy_analysis.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["policy"]["extra_policy_setting"] = 1.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="playback-analysis policy provenance"):
        build_canary_summary(tmp_path)


@pytest.mark.parametrize(
    "artifact,mutation,error",
    [
        (
            "tts_duration_model.json",
            lambda value: value["aggregate"]["observed_distribution"].update(
                {"observation_count": 99}
            ),
            "duration observations do not match",
        ),
        (
            "playback_policy_analysis.json",
            lambda value: value["traces"][0]["adaptive"].update(
                {"chunks_scheduled": 99}
            ),
            "playback chunks do not match",
        ),
    ],
)
def test_rejects_cross_artifact_child_count_mismatch(
    tmp_path,
    artifact,
    mutation,
    error,
):
    write_matrix(tmp_path)
    path = tmp_path / "cap-60" / artifact
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        build_canary_summary(tmp_path)


def test_promotion_gate_set_must_be_valid_and_complete_semantically():
    with pytest.raises(ValueError, match="must be at least"):
        promotion_gates(
            max_child_p95_seconds=8.0,
            max_child_max_seconds=4.0,
        )

    with pytest.raises(ValueError, match="must be positive"):
        promotion_gates(min_child_p95_reduction_percent=0.0)

    with pytest.raises(ValueError, match="must not exceed 100"):
        promotion_gates(
            min_adaptive_queue_p95_reduction_percent=100.01,
        )


def test_absolute_pass_does_not_override_relative_benefit_gate(tmp_path):
    write_matrix(tmp_path)
    path = tmp_path / "cap-40" / "playback_policy_analysis.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["traces"][0]["adaptive"][
        "time_weighted_queue_p95_seconds"
    ] = 9.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    recommendation = build_canary_summary(
        tmp_path,
        promotion_gates=promotion_gates(),
    )["recommendation"]
    evaluation = recommendation["evaluations"][0]

    assert evaluation["gates"]["adaptive_queue_p95"] is True
    assert evaluation["gates"]["adaptive_queue_relative_benefit"] is False
    assert evaluation["pass"] is False
    assert "arm_02" not in recommendation["eligible_arms"]


def test_absolute_pass_does_not_override_relative_nonregression_gate(tmp_path):
    write_matrix(tmp_path)
    path = tmp_path / "cap-40" / "capture_summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["first_audio_latency_sec"] = 1.51
    path.write_text(json.dumps(payload), encoding="utf-8")

    recommendation = build_canary_summary(
        tmp_path,
        promotion_gates=promotion_gates(),
    )["recommendation"]
    evaluation = recommendation["evaluations"][0]

    assert evaluation["gates"]["first_audio_latency_nonregression"] is False
    assert evaluation["pass"] is False
    assert "arm_02" not in recommendation["eligible_arms"]


def test_cli_requires_the_extended_promotion_gate_set_to_be_complete():
    parser = create_argument_parser()
    args = parser.parse_args(
        [
            "--input-dir",
            "unused",
            "--min-child-p95-reduction-percent",
            "10",
        ]
    )

    with pytest.raises(ValueError, match="all promotion-gate options"):
        _promotion_gates_from_args(args)
