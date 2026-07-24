import hashlib
import json
from pathlib import Path

import pytest

from analyze_tts_duration import (
    TtsDurationObservation,
    build_analysis,
    fit_duration_model,
    load_summary_observations,
    parse_cli_args,
    render_markdown,
    validate_output_destinations,
)


def write_summary(
    path: Path,
    pairs: list[tuple[int, float]],
    *,
    outcome: str = "complete",
    raw_marker: str = "PRIVATE_MARKER",
) -> None:
    events = []
    for sequence_id, (text_chars, duration_seconds) in enumerate(pairs):
        events.extend(
            [
                {
                    "stage": "nmt",
                    "event": "completed",
                    "sequence_id": sequence_id,
                    "text_chars": text_chars,
                    "session_id": raw_marker,
                    "raw_text": raw_marker,
                },
                {
                    "stage": "tts",
                    "event": "started",
                    "sequence_id": sequence_id,
                    "text_chars": text_chars,
                    "session_id": raw_marker,
                    "raw_text": raw_marker,
                },
                {
                    "stage": "tts",
                    "event": "completed",
                    "sequence_id": sequence_id,
                    "audio_duration_ms": duration_seconds * 1_000,
                    "audio_bytes": max(1, round(duration_seconds * 32_000)),
                    "retry_count": 0,
                    "session_id": raw_marker,
                },
            ]
        )
    ids = list(range(len(pairs)))
    payload = {
        "audio_path": raw_marker,
        "backend_url": raw_marker,
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "staged_pipeline": {
            "state": "closed",
            "outcome": outcome,
            "failure": None,
            "cleanup_errors": [],
            "incomplete_sequence_ids": [],
            "segments_emitted": len(pairs),
            "audio_segments_produced": len(pairs),
            "completed_sequence_ids": ids,
            "websocket_sent_sequence_ids": ids,
            "tts_retry_count": 0,
            "session_id": raw_marker,
            "events": events,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_v2_summary(
    path: Path,
    parents: list[tuple[int, list[tuple[int, float]]]],
    *,
    raw_marker: str = "PRIVATE_MARKER",
) -> None:
    events = []
    keys = []
    retry_total = 0
    for parent_sequence_id, (parent_text_chars, children) in enumerate(parents):
        events.append(
            {
                "stage": "nmt",
                "event": "completed",
                "sequence_id": parent_sequence_id,
                "text_chars": parent_text_chars,
                "session_id": raw_marker,
                "raw_text": raw_marker,
            }
        )
        subsequence_count = len(children)
        for subsequence_id, (text_chars, duration_seconds) in enumerate(
            children
        ):
            identity = {
                "parent_sequence_id": parent_sequence_id,
                "subsequence_id": subsequence_id,
                "subsequence_count": subsequence_count,
            }
            keys.append(identity)
            events.extend(
                [
                    {
                        "stage": "tts",
                        "event": "started",
                        "sequence_id": parent_sequence_id,
                        **identity,
                        "text_chars": text_chars,
                        "parent_text_chars": parent_text_chars,
                        "session_id": raw_marker,
                        "raw_text": raw_marker,
                    },
                    {
                        "stage": "tts",
                        "event": "completed",
                        "sequence_id": parent_sequence_id,
                        **identity,
                        "audio_duration_ms": duration_seconds * 1_000,
                        "audio_bytes": max(
                            1,
                            round(duration_seconds * 32_000),
                        ),
                        "retry_count": 0,
                        "session_id": raw_marker,
                    },
                ]
            )

    parent_ids = list(range(len(parents)))
    payload = {
        "audio_path": raw_marker,
        "backend_url": raw_marker,
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "staged_pipeline": {
            "telemetry_schema_version": 2,
            "tts_subsegmentation_enabled": True,
            "state": "closed",
            "outcome": "complete",
            "failure": None,
            "cleanup_errors": [],
            "incomplete_sequence_ids": [],
            "incomplete_subsegment_keys": [],
            "segments_emitted": len(parents),
            "audio_segments_produced": len(keys),
            "tts_subsegments_planned": len(keys),
            "tts_subsegments_produced": len(keys),
            "completed_sequence_ids": parent_ids,
            "websocket_sent_sequence_ids": parent_ids,
            "planned_subsegment_keys": keys,
            "synthesized_subsegment_keys": keys,
            "completed_subsegment_keys": keys,
            "websocket_sent_subsegment_keys": keys,
            "tts_retry_count": retry_total,
            "session_id": raw_marker,
            "events": events,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def observations(
    sample_index: int,
    pairs: list[tuple[int, float]],
) -> tuple[TtsDurationObservation, ...]:
    return tuple(
        TtsDurationObservation(
            sample_index=sample_index,
            sequence_id=sequence_id,
            text_chars=text_chars,
            audio_duration_seconds=duration,
        )
        for sequence_id, (text_chars, duration) in enumerate(pairs)
    )


def v2_observations(
    sample_index: int,
    parents: list[list[tuple[int, float]]],
) -> tuple[TtsDurationObservation, ...]:
    return tuple(
        TtsDurationObservation(
            sample_index=sample_index,
            sequence_id=parent_sequence_id,
            subsequence_id=subsequence_id,
            subsequence_count=len(children),
            text_chars=text_chars,
            audio_duration_seconds=duration,
            telemetry_schema_version=2,
        )
        for parent_sequence_id, children in enumerate(parents)
        for subsequence_id, (text_chars, duration) in enumerate(children)
    )


def test_load_summary_pairs_structural_started_and_completed_events(tmp_path):
    path = tmp_path / "capture_summary.json"
    write_summary(path, [(10, 1.0), (20, 1.5), (30, 2.0)])

    loaded = load_summary_observations(path, sample_index=2)

    assert loaded == observations(2, [(10, 1.0), (20, 1.5), (30, 2.0)])
    assert "PRIVATE_MARKER" not in repr(loaded)


def test_load_v2_summary_pairs_multiple_children_by_composite_identity(
    tmp_path,
):
    path = tmp_path / "split_capture_summary.json"
    parents = [
        (51, [(20, 1.2), (30, 1.8)]),
        (18, [(18, 1.1)]),
    ]
    write_v2_summary(path, parents)

    loaded = load_summary_observations(path, sample_index=1)

    assert loaded == v2_observations(
        1,
        [
            [(20, 1.2), (30, 1.8)],
            [(18, 1.1)],
        ],
    )
    assert [item.composite_key for item in loaded] == [
        (0, 0, 2),
        (0, 1, 2),
        (1, 0, 1),
    ]
    assert "PRIVATE_MARKER" not in repr(loaded)


def test_load_v2_summary_rejects_noncontiguous_children(tmp_path):
    path = tmp_path / "split_gap_summary.json"
    write_v2_summary(
        path,
        [(40, [(18, 1.0), (21, 1.2)])],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    child_events = [
        event
        for event in payload["staged_pipeline"]["events"]
        if event.get("stage") == "tts"
        and event.get("subsequence_id") == 1
    ]
    for event in child_events:
        event["subsequence_id"] = 2
        event["subsequence_count"] = 3
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="not contiguous"):
        load_summary_observations(path, sample_index=1)


def test_load_v2_summary_rejects_parent_character_mismatch(tmp_path):
    path = tmp_path / "split_parent_chars_summary.json"
    write_v2_summary(
        path,
        [(40, [(18, 1.0), (21, 1.2)])],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    started = next(
        event
        for event in payload["staged_pipeline"]["events"]
        if event.get("stage") == "tts"
        and event.get("event") == "started"
    )
    started["parent_text_chars"] = 41
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="parent_text_chars differ"):
        load_summary_observations(path, sample_index=1)


def test_load_summary_rejects_failed_integrity(tmp_path):
    path = tmp_path / "failed_summary.json"
    write_summary(path, [(10, 1.0), (20, 1.5)], outcome="failed")

    with pytest.raises(ValueError, match="outcome is not complete"):
        load_summary_observations(path, sample_index=1)


def test_load_summary_rejects_mismatched_sequence_pairs(tmp_path):
    path = tmp_path / "mismatch_summary.json"
    write_summary(path, [(10, 1.0), (20, 1.5)])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["events"] = payload["staged_pipeline"]["events"][:-1]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="sequence sets do not match"):
        load_summary_observations(path, sample_index=1)


def test_load_summary_rejects_nmt_tts_character_mismatch(tmp_path):
    path = tmp_path / "mismatch_summary.json"
    write_summary(path, [(10, 1.0), (20, 1.5)])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["events"][0]["text_chars"] = 11
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="character counts differ"):
        load_summary_observations(path, sample_index=1)


def test_load_summary_rejects_retry_total_mismatch(tmp_path):
    path = tmp_path / "retry_mismatch_summary.json"
    write_summary(path, [(10, 1.0), (20, 1.5)])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["events"][2]["retry_count"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="tts_retry_count does not match"):
        load_summary_observations(path, sample_index=1)


def test_fit_duration_model_recovers_linear_relationship():
    model = fit_duration_model(
        observations(
            1,
            [(10, 1.0), (20, 1.5), (30, 2.0), (40, 2.5)],
        )
    )

    assert model.intercept_seconds == pytest.approx(0.5)
    assert model.seconds_per_char == pytest.approx(0.05)
    assert model.r_squared == pytest.approx(1.0)
    assert model.residual_p95_seconds == pytest.approx(0.0)


def test_build_analysis_selects_largest_grid_cap_across_every_model():
    pairs = [
        (10, 1.0),
        (20, 1.5),
        (30, 2.0),
        (40, 2.5),
        (50, 3.0),
        (60, 3.5),
        (70, 4.0),
        (80, 4.5),
    ]
    samples = [
        observations(1, pairs),
        observations(2, pairs),
    ]

    analysis = build_analysis(samples)

    assert analysis["recommendation"]["tts_subsegment_max_chars"] == 70
    assert analysis["aggregate"]["model"]["integer_cap_limits_chars"] == {
        "p95_target": 70,
        "observed_max_residual_target": 150,
        "combined": 70,
    }
    assert analysis["recommendation"][
        "all_aggregate_and_per_sample_constraints_pass"
    ] is True
    assert analysis["recommendation"][
        "cross_sample_envelope_constraint_passes"
    ] is True
    assert analysis["leave_one_sample_out"]["performed"] is True


def test_recommendation_is_constrained_by_leave_one_sample_out_envelope():
    samples = [
        observations(
            1,
            [
                (10, 1.6994084745926923),
                (20, 1.8034176325877365),
                (30, 2.207055452418665),
                (40, 2.4961692387873047),
                (60, 3.1919927925407565),
                (80, 3.861827734485699),
            ],
        ),
        observations(
            2,
            [
                (10, 0.5490801781181447),
                (20, 0.662968633131606),
                (30, 1.0698615083721494),
                (40, 1.0408795661488046),
                (60, 1.7683673612690571),
                (80, 2.429003835422225),
            ],
        ),
        observations(
            3,
            [
                (10, 1.9669597338364466),
                (20, 2.001634521021637),
                (30, 2.382586649221962),
                (40, 2.971983070958338),
                (60, 3.897097300985068),
                (80, 4.439938884981673),
            ],
        ),
    ]

    analysis = build_analysis(samples)

    assert analysis["leave_one_sample_out"][
        "integer_cap_limits_chars_using_aggregate_center_line"
    ]["combined"] == 50
    assert analysis["recommendation"]["tts_subsegment_max_chars"] == 50


def test_recommendation_does_not_extrapolate_below_observed_characters():
    sample = observations(1, [(100, 7.0), (110, 8.0)])

    analysis = build_analysis([sample])

    assert analysis["recommendation"]["tts_subsegment_max_chars"] is None
    assert analysis["recommendation"][
        "all_selection_constraints_pass"
    ] is False


def test_analysis_and_markdown_exclude_private_source_fields(tmp_path):
    marker = "DO_NOT_COPY_THIS_VALUE"
    first = tmp_path / "first_summary.json"
    second = tmp_path / "second_summary.json"
    pairs = [(10, 1.0), (20, 1.5), (30, 2.0), (40, 2.5)]
    write_summary(first, pairs, raw_marker=marker)
    write_summary(second, pairs, raw_marker=marker)
    loaded = [
        load_summary_observations(first, sample_index=1),
        load_summary_observations(second, sample_index=2),
    ]

    analysis = build_analysis(loaded)
    serialized = json.dumps(analysis)
    markdown = render_markdown(analysis)

    assert marker not in serialized
    assert marker not in markdown
    assert str(first) not in serialized
    assert str(second) not in markdown
    assert analysis["privacy"] == {
        "contains_transcript_text": False,
        "contains_audio": False,
        "contains_input_paths_or_filenames": False,
        "contains_endpoints": False,
        "contains_session_ids": False,
        "sample_labels_are_neutral_ordinals": True,
    }
    assert "Sample 01" in markdown
    assert "Sample 02" in markdown


def test_markdown_uses_resolved_recommendation_and_comparison_caps():
    pairs = [
        (10, 1.0),
        (20, 1.5),
        (30, 2.0),
        (40, 2.5),
        (50, 3.0),
        (60, 3.5),
        (70, 4.0),
        (80, 4.5),
    ]
    analysis = build_analysis(
        [observations(1, pairs)],
        comparison_caps=[15, 60],
    )

    markdown = render_markdown(analysis)

    assert [
        candidate["cap_chars"]
        for candidate in analysis["capacity_candidates"]
    ] == [15, 60, 70]
    assert "15-character, 60-character, and 70-character policies" in markdown
    assert "40-character" not in markdown


def test_analysis_is_deterministic_for_identical_structural_records():
    sample = observations(
        1,
        [(10, 1.0), (20, 1.5), (30, 2.0), (40, 2.5)],
    )

    first = build_analysis([sample])
    second = build_analysis([sample])

    assert first == second
    assert first["structural_records_sha256"] == second[
        "structural_records_sha256"
    ]


def test_v1_structural_digest_preserves_the_legacy_record_shape():
    sample = observations(
        1,
        [(10, 1.0), (20, 1.5), (30, 2.0), (40, 2.5)],
    )

    analysis = build_analysis([sample])

    assert analysis["schema_version"] == 1
    assert analysis["structural_records_sha256"] == (
        "53cb26306fec31b9f14a9b34eb4e3971553dc521866f4224ed6811c62188ba25"
    )
    assert "structural_digest_includes_composite_identity" not in analysis[
        "semantics"
    ]


def test_v2_structural_digest_includes_composite_identity():
    sample = v2_observations(
        1,
        [
            [(20, 1.2), (30, 1.8)],
            [(18, 1.1)],
        ],
    )
    expected_records = [
        {
            "sample_index": item.sample_index,
            "parent_sequence_id": item.parent_sequence_id,
            "subsequence_id": item.subsequence_id,
            "subsequence_count": item.subsequence_count,
            "text_chars": item.text_chars,
            "audio_duration_seconds": item.audio_duration_seconds,
        }
        for item in sample
    ]
    expected_digest = hashlib.sha256(
        json.dumps(
            expected_records,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    analysis = build_analysis([sample])

    assert analysis["schema_version"] == 2
    assert analysis["structural_records_sha256"] == expected_digest
    assert expected_digest == (
        "07aac68ed600f17fd9afa0291fc78d7a3f0ccb4f8a7da86d9dd7239b7b6a0de9"
    )
    assert analysis["semantics"][
        "structural_digest_includes_composite_identity"
    ] is True
    assert "parent/subsequence composite identity" in render_markdown(analysis)


def test_build_analysis_rejects_invalid_threshold_relationship():
    sample = observations(
        1,
        [(10, 1.0), (20, 1.5), (30, 2.0), (40, 2.5)],
    )

    with pytest.raises(
        ValueError,
        match="target_max_seconds must be at least",
    ):
        build_analysis([sample], target_p95_seconds=8, target_max_seconds=4)


def test_capacity_candidates_quantify_minimum_call_and_intercept_risk():
    sample = observations(
        1,
        [(10, 1.0), (20, 1.5), (30, 2.0), (40, 2.5)],
    )

    analysis = build_analysis([sample], comparison_caps=[15, 40])

    small, large = analysis["capacity_candidates"]
    assert small["cap_chars"] == 15
    assert small["minimum_subsegment_calls"] == 8
    assert small["minimum_additional_calls"] == 4
    assert small["minimum_call_increase_percent"] == pytest.approx(100.0)
    assert small["ols_intercept_counterfactual"][
        "modeled_extra_audio_seconds"
    ] == pytest.approx(2.0)
    assert large["minimum_subsegment_calls"] == 4
    assert large["minimum_additional_calls"] == 0
    assert analysis["leave_one_sample_out"] == {
        "performed": False,
        "reason": "at least two samples are required",
    }


def test_capacity_candidate_smaller_than_observed_min_has_empty_subset():
    sample = observations(
        1,
        [(10, 1.0), (20, 1.5), (30, 2.0), (40, 2.5)],
    )

    analysis = build_analysis([sample], comparison_caps=[1])

    observed = analysis["capacity_candidates"][0][
        "observed_original_chunks_at_or_below_cap"
    ]
    assert observed["observation_count"] == 0
    assert observed["audio_duration_seconds"] == {
        "p95": None,
        "max": None,
    }


def test_parse_cli_args_validates_capacity_grid():
    with pytest.raises(SystemExit):
        parse_cli_args(
            [
                "--input-dir",
                "captures",
                "--cap-step-chars",
                "50",
                "--max-candidate-chars",
                "40",
            ]
        )


def test_parse_cli_args_deduplicates_explicit_comparison_caps():
    args = parse_cli_args(
        [
            "--input-dir",
            "captures",
            "--comparison-cap",
            "45",
            "--comparison-cap",
            "45",
            "--comparison-cap",
            "60",
        ]
    )

    assert args.comparison_caps == (45, 60)


def test_output_destinations_cannot_alias_each_other_or_an_input(tmp_path):
    summary = tmp_path / "capture_summary.json"
    summary.write_text("{}", encoding="utf-8")
    report = tmp_path / "report.json"

    with pytest.raises(ValueError, match="must be different"):
        validate_output_destinations(
            [summary],
            json_output=report,
            markdown_output=report,
        )
    with pytest.raises(ValueError, match="cannot overwrite"):
        validate_output_destinations(
            [summary],
            json_output=summary,
            markdown_output=tmp_path / "report.md",
        )
