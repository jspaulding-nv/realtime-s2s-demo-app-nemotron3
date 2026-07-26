import copy
from dataclasses import fields
from types import SimpleNamespace

import pytest

from headless_playback_scheduler import (
    HeadlessPlaybackScheduler,
    ValidatedFrameObservation,
    ValidatedParentCompletion,
    validate_headless_playback_report,
)
from playback_simulation import DEFAULT_PLAYBACK_POLICY


def _validated_sink_frame(
    *,
    parent_sequence_id,
    arrival_seconds,
    duration_seconds,
    audio_frame_id=0,
    source_start_ms=None,
    source_end_ms=None,
    stream_generation=7,
):
    sample_rate_hz = 1_000
    channels = 1
    bytes_per_sample = 2
    return SimpleNamespace(
        arrival_seconds=arrival_seconds,
        audio_bytes=round(
            duration_seconds
            * sample_rate_hz
            * channels
            * bytes_per_sample
        ),
        protocol_version=1,
        stream_generation=stream_generation,
        parent_sequence_id=parent_sequence_id,
        audio_frame_id=audio_frame_id,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        bytes_per_sample=bytes_per_sample,
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
    )


def _complete_report():
    scheduler = HeadlessPlaybackScheduler()
    scheduler.accept(
        _validated_sink_frame(
            parent_sequence_id=0,
            arrival_seconds=1.0,
            duration_seconds=0.1,
            source_start_ms=0.0,
            source_end_ms=500.0,
        )
    )
    return scheduler.finalize(
        input_end_seconds=2.0,
        input_sample_zero_seconds=0.0,
    )


def test_validated_sink_frames_are_scheduled_incrementally_and_replayed():
    scheduler = HeadlessPlaybackScheduler()

    decisions = [
        scheduler.accept(
            _validated_sink_frame(
                parent_sequence_id=0,
                audio_frame_id=0,
                arrival_seconds=0.0,
                duration_seconds=4.0,
                source_start_ms=0.0,
                source_end_ms=1_000.0,
            )
        ),
        scheduler.accept(
            _validated_sink_frame(
                parent_sequence_id=0,
                audio_frame_id=1,
                arrival_seconds=0.0,
                duration_seconds=1.0,
                source_start_ms=0.0,
                source_end_ms=1_000.0,
            )
        ),
        scheduler.accept(
            _validated_sink_frame(
                parent_sequence_id=1,
                arrival_seconds=0.0,
                duration_seconds=3.0,
                source_start_ms=1_000.0,
                source_end_ms=2_000.0,
            )
        ),
    ]

    assert [decision.playback_mode for decision in decisions] == [
        "normal",
        "catch-up",
        "catch-up",
    ]
    assert [decision.playback_rate for decision in decisions] == [
        1.0,
        1.05,
        1.05,
    ]
    assert scheduler.schedule == tuple(decisions)

    report = scheduler.finalize(
        input_end_seconds=2.0,
        input_sample_zero_seconds=0.0,
    )

    assert report["policy"] == {
        "target_queue_seconds": 5.0,
        "urgent_queue_seconds": 8.0,
        "limit_queue_seconds": 10.0,
        "catch_up_release_seconds": 4.0,
        "urgent_release_seconds": 7.0,
        "normal_rate": 1.0,
        "catch_up_rate": 1.05,
        "urgent_rate": 1.1,
    }
    assert report["capture"]["frames_received"] == 3
    assert report["capture"]["frames_scheduled"] == 3
    assert report["capture"]["complete_parents"] == 2
    assert (
        report["capture"]["parent_reconciliation"]
        == "upstream_protocol_v1_tracker"
    )
    assert report["capture"]["canonical_replay_verified"] is True
    assert report["queue_gate"]["frames_dropped"] == 0
    assert report["queue_gate"]["frames_reordered"] == 0
    assert report["queue_gate"]["frames_duplicated"] == 0
    assert report["queue_gate"]["all_frames_preserved_once_in_order"] is True


def test_queue_gate_uses_exact_registered_five_and_ten_second_boundaries():
    scheduler = HeadlessPlaybackScheduler()
    for parent_sequence_id, duration_seconds in enumerate((4.0, 1.0, 3.0, 3.0)):
        scheduler.accept(
            _validated_sink_frame(
                parent_sequence_id=parent_sequence_id,
                arrival_seconds=0.0,
                duration_seconds=duration_seconds,
                source_start_ms=float(parent_sequence_id * 1_000),
                source_end_ms=float((parent_sequence_id + 1) * 1_000),
            )
        )

    report = scheduler.finalize(
        input_end_seconds=4.0,
        input_sample_zero_seconds=0.0,
    )
    gate = report["queue_gate"]

    assert gate["p95_objective_seconds"] == (
        DEFAULT_PLAYBACK_POLICY.target_queue_seconds
    )
    assert gate["peak_limit_seconds"] == (
        DEFAULT_PLAYBACK_POLICY.limit_queue_seconds
    )
    assert gate["p95_observed_seconds"] > 5.0
    assert gate["peak_observed_seconds"] > 10.0
    assert gate["p95_pass"] is False
    assert gate["peak_pass"] is False
    assert gate["result"] == "fail"
    assert scheduler.schedule[-1].playback_mode == "over-limit"
    assert scheduler.schedule[-1].playback_rate == 1.10
    # An honestly observed SLA failure remains a valid operational capture.
    validate_headless_playback_report(report)


def test_source_distributions_parent_envelope_and_frontier_drift():
    scheduler = HeadlessPlaybackScheduler()
    source_ends_ms = (1_000.0, 2_000.0, 3_000.0, 4_000.0)
    arrivals_seconds = (2.0, 4.0, 6.0, 8.0)
    for parent_sequence_id, (source_end_ms, arrival_seconds) in enumerate(
        zip(source_ends_ms, arrivals_seconds)
    ):
        scheduler.accept(
            _validated_sink_frame(
                parent_sequence_id=parent_sequence_id,
                arrival_seconds=arrival_seconds,
                duration_seconds=0.1,
                source_start_ms=source_end_ms - 500.0,
                source_end_ms=source_end_ms,
            )
        )

    report = scheduler.finalize(
        input_end_seconds=5.0,
        input_sample_zero_seconds=0.0,
    )
    source = report["source_boundary_observation"]
    envelope = report["parent_schedule_envelope"]
    drift = report["accumulated_frontier_drift"]

    assert source["sample_unit"] == (
        "first_translated_frame_per_complete_parent"
    )
    assert source["parents_with_asr_source_range"] == 4
    assert source["source_end_to_first_arrival_ms"] == {
        "sample_count": 4,
        "min_ms": 1_000.0,
        "p50_ms": 2_000.0,
        "p95_ms": 4_000.0,
        "max_ms": 4_000.0,
    }
    assert source["source_end_to_first_scheduled_start_ms"] == (
        source["source_end_to_first_arrival_ms"]
    )
    assert envelope["source_end_to_final_scheduled_end_ms"] == {
        "sample_count": 4,
        "min_ms": 1_100.0,
        "p50_ms": 2_100.0,
        "p95_ms": 4_100.0,
        "max_ms": 4_100.0,
    }
    assert envelope["scheduled_envelope_duration_ms"] == {
        "sample_count": 4,
        "min_ms": 100.0,
        "p50_ms": 100.0,
        "p95_ms": 100.0,
        "max_ms": 100.0,
    }
    assert drift == {
        "metric": "source_end_to_first_scheduled_start_ms",
        "population": "complete parents with both ASR source-range offsets",
        "sample_count": 4,
        "minimum_samples_required": 4,
        "sufficient_sample_count": True,
        "quartile_size": 1,
        "first_quartile_median_ms": 1_000.0,
        "final_quartile_median_ms": 4_000.0,
        "final_minus_first_ms": 3_000.0,
        "positive_delta_means": (
            "scheduled source-frontier delay accumulated later in the capture"
        ),
    }


def test_end_only_offsets_are_observed_but_excluded_from_asr_frontier_drift():
    scheduler = HeadlessPlaybackScheduler()
    scheduler.accept(
        _validated_sink_frame(
            parent_sequence_id=0,
            arrival_seconds=2.0,
            duration_seconds=0.1,
            source_start_ms=None,
            source_end_ms=1_000.0,
        )
    )
    scheduler.accept(
        _validated_sink_frame(
            parent_sequence_id=1,
            arrival_seconds=3.0,
            duration_seconds=0.1,
            source_start_ms=1_000.0,
            source_end_ms=2_000.0,
        )
    )

    report = scheduler.finalize(
        input_end_seconds=2.5,
        input_sample_zero_seconds=0.0,
    )

    source = report["source_boundary_observation"]
    assert source["parents_with_audio_processed_end_only"] == 1
    assert source["parents_with_asr_source_range"] == 1
    assert source["source_end_to_first_arrival_ms"]["sample_count"] == 2
    drift = report["accumulated_frontier_drift"]
    assert drift["sample_count"] == 1
    assert drift["sufficient_sample_count"] is False
    assert drift["quartile_size"] == 0
    assert drift["final_minus_first_ms"] is None


def test_explicit_completion_api_rejects_missing_or_duplicate_frames():
    scheduler = HeadlessPlaybackScheduler()
    scheduler.accept_frame(
        ValidatedFrameObservation(
            arrival_seconds=1.0,
            duration_seconds=0.1,
            stream_generation=1,
            parent_sequence_id=0,
            audio_frame_id=0,
            source_start_ms=0.0,
            source_end_ms=500.0,
        )
    )

    with pytest.raises(ValueError, match="contiguous and zero-based"):
        scheduler.accept_frame(
            ValidatedFrameObservation(
                arrival_seconds=1.1,
                duration_seconds=0.1,
                stream_generation=1,
                parent_sequence_id=0,
                audio_frame_id=0,
                source_start_ms=0.0,
                source_end_ms=500.0,
            )
        )
    with pytest.raises(ValueError, match="frame count does not reconcile"):
        scheduler.complete_parent(
            ValidatedParentCompletion(
                stream_generation=1,
                parent_sequence_id=0,
                audio_frame_count=2,
                source_start_ms=0.0,
                source_end_ms=500.0,
            )
        )
    with pytest.raises(ValueError, match="active parent completes"):
        scheduler.finalize(
            input_end_seconds=2.0,
            input_sample_zero_seconds=0.0,
        )


def test_public_report_has_fixed_claim_scope_and_no_payload_input_fields():
    observation_field_names = {
        field.name for field in fields(ValidatedFrameObservation)
    }
    assert observation_field_names == {
        "arrival_seconds",
        "duration_seconds",
        "stream_generation",
        "parent_sequence_id",
        "audio_frame_id",
        "source_start_ms",
        "source_end_ms",
    }

    scheduler = HeadlessPlaybackScheduler()
    scheduler.accept(
        _validated_sink_frame(
            parent_sequence_id=0,
            arrival_seconds=1.0,
            duration_seconds=0.1,
        )
    )
    report = scheduler.finalize(
        input_end_seconds=1.0,
        input_sample_zero_seconds=0.0,
    )

    assert set(report) == {
        "schema_version",
        "report_type",
        "policy",
        "capture",
        "queue_observation",
        "queue_gate",
        "source_boundary_observation",
        "parent_schedule_envelope",
        "accumulated_frontier_drift",
        "claim_scope",
        "privacy",
    }
    assert report["claim_scope"] == {
        "measurement": "scheduled_digital_playback",
        "semantic_source_boundary_proven": False,
        "target_language_landmark_proven": False,
        "dac_or_acoustic_audibility_proven": False,
        "room_reaction_synchronization_proven": False,
        "caveats": [
            (
                "source offsets are ASR source ranges or coarse "
                "audio-processed ends, not reviewed punchline markers"
            ),
            (
                "scheduled starts are deterministic software deadlines, "
                "not measured speaker output"
            ),
            (
                "an exact joke-delay claim requires reviewed source and "
                "target landmarks on a common-clock recording"
            ),
        ],
    }
    assert report["privacy"] == {
        "aggregate_only": True,
        "contains_pcm": False,
        "contains_transcript_or_translation_text": False,
        "contains_file_path_or_uri": False,
        "contains_wall_clock_timestamp": False,
    }


@pytest.mark.parametrize(
    "field_name",
    [
        "policy",
        "queue_observation",
        "source_boundary_observation",
        "parent_schedule_envelope",
        "accumulated_frontier_drift",
        "claim_scope",
        "privacy",
    ],
)
def test_strict_report_validator_rejects_missing_required_sections(field_name):
    report = _complete_report()
    report.pop(field_name)

    with pytest.raises(ValueError, match="missing"):
        validate_headless_playback_report(report)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda report: report.update(transcript="private"),
            "unknown transcript",
        ),
        (
            lambda report: report["policy"].update(
                target_queue_seconds=6.0
            ),
            "must equal 5.0",
        ),
        (
            lambda report: report["policy"].update(urgent_rate=1.2),
            "must equal 1.1",
        ),
        (
            lambda report: report["queue_observation"].update(
                peak_queue_depth_seconds=float("nan")
            ),
            "finite float",
        ),
        (
            lambda report: report["queue_gate"].update(result="fail"),
            "must equal 'pass'",
        ),
        (
            lambda report: report[
                "source_boundary_observation"
            ].update(parents_without_source_end=1),
            "parent counts are inconsistent",
        ),
        (
            lambda report: report["claim_scope"].update(
                semantic_source_boundary_proven=0
            ),
            "must equal False",
        ),
        (
            lambda report: report["privacy"].update(aggregate_only=1),
            "must equal True",
        ),
    ],
)
def test_strict_report_validator_rejects_tampering(mutation, reason):
    report = copy.deepcopy(_complete_report())
    mutation(report)

    with pytest.raises(ValueError, match=reason):
        validate_headless_playback_report(report)
