import math

import pytest

from playback_simulation import (
    AudioChunk,
    ParentAudioFrame,
    PlaybackPolicy,
    playback_rate_for_mode,
    select_playback_mode,
    simulate_parent_freshness_cap,
    simulate_playback,
)


@pytest.mark.parametrize(
    ("queue", "current_mode", "expected"),
    [
        (4.999, "normal", "normal"),
        (5.0, "normal", "catch-up"),
        (8.0, "normal", "urgent"),
        (10.0, "normal", "urgent"),
        (10.001, "normal", "over-limit"),
        (4.0, "catch-up", "catch-up"),
        (3.999, "catch-up", "normal"),
        (8.0, "catch-up", "urgent"),
        (7.0, "urgent", "urgent"),
        (6.999, "urgent", "catch-up"),
        (3.999, "urgent", "normal"),
        (10.0, "over-limit", "urgent"),
        (7.0, "over-limit", "urgent"),
        (6.999, "over-limit", "catch-up"),
    ],
)
def test_select_playback_mode_matches_frontend_boundaries(
    queue, current_mode, expected
):
    assert select_playback_mode(queue, current_mode) == expected


@pytest.mark.parametrize(
    ("mode", "rate"),
    [
        ("normal", 1.0),
        ("catch-up", 1.05),
        ("urgent", 1.10),
        ("over-limit", 1.10),
    ],
)
def test_playback_rate_for_mode_matches_frontend(mode, rate):
    assert playback_rate_for_mode(mode) == rate


def test_fixed_playback_preserves_chunks_and_computes_exact_queue_time():
    policy = PlaybackPolicy(
        target_queue_seconds=1.0,
        urgent_queue_seconds=3.0,
        limit_queue_seconds=4.0,
        catch_up_release_seconds=0.5,
        urgent_release_seconds=2.0,
    )
    chunks = [
        AudioChunk(arrival_seconds=2.0, duration_seconds=2.0, audio_bytes=64_000),
        AudioChunk(arrival_seconds=3.0, duration_seconds=2.0, audio_bytes=64_000),
    ]

    result = simulate_playback(
        chunks, input_end_seconds=4.0, adaptive=False, policy=policy
    )

    assert result.summary.chunks_received == 2
    assert result.summary.chunks_scheduled == 2
    assert result.summary.chunks_dropped == 0
    assert result.summary.total_source_duration_seconds == 4.0
    assert result.summary.total_scheduled_duration_seconds == 4.0
    assert result.summary.playback_end_seconds == 6.0
    assert result.summary.listener_tail_seconds == 2.0
    assert result.summary.peak_queue_depth_seconds == 3.0
    assert result.summary.arrival_queue_p50_seconds == 2.0
    assert result.summary.arrival_queue_p95_seconds == 3.0
    assert math.isclose(
        result.summary.time_weighted_queue_p50_seconds,
        1.5,
        abs_tol=1e-12,
    )
    assert math.isclose(
        result.summary.time_weighted_queue_p95_seconds,
        2.8,
        abs_tol=1e-12,
    )
    assert result.summary.seconds_above_target == 3.0
    assert result.summary.percent_playback_window_above_target == 75.0
    assert [chunk.start_seconds for chunk in result.schedule] == [2.0, 4.0]
    assert [chunk.end_seconds for chunk in result.schedule] == [4.0, 6.0]


def test_adaptive_schedule_uses_projected_normal_queue_and_never_drops_audio():
    chunks = [
        AudioChunk(arrival_seconds=0.0, duration_seconds=4.0),
        AudioChunk(arrival_seconds=0.0, duration_seconds=1.0),
        AudioChunk(arrival_seconds=0.0, duration_seconds=3.0),
        AudioChunk(arrival_seconds=0.0, duration_seconds=3.0),
    ]

    result = simulate_playback(chunks, input_end_seconds=1.0, adaptive=True)

    assert [chunk.playback_mode for chunk in result.schedule] == [
        "normal",
        "catch-up",
        "catch-up",
        "over-limit",
    ]
    assert [chunk.playback_rate for chunk in result.schedule] == [
        1.0,
        1.05,
        1.05,
        1.10,
    ]
    assert result.summary.chunks_scheduled == len(chunks)
    assert result.summary.chunks_dropped == 0
    assert result.summary.total_source_duration_seconds == 11.0
    assert result.summary.limit_breach_entries == 1
    assert result.summary.mode_chunk_counts == {
        "normal": 1,
        "catch-up": 2,
        "urgent": 0,
        "over-limit": 1,
    }
    expected_scheduled = 4.0 + 1.0 / 1.05 + 3.0 / 1.05 + 3.0 / 1.10
    assert math.isclose(
        result.summary.total_scheduled_duration_seconds,
        expected_scheduled,
        abs_tol=1e-12,
    )
    assert math.isclose(
        result.summary.rate_source_duration_seconds["1.10x"],
        3.0,
        abs_tol=1e-12,
    )


def test_idle_gap_starts_next_chunk_at_arrival_without_inventing_queue():
    chunks = [
        AudioChunk(arrival_seconds=1.0, duration_seconds=0.5),
        AudioChunk(arrival_seconds=5.0, duration_seconds=0.5),
    ]

    result = simulate_playback(chunks, input_end_seconds=5.0, adaptive=True)

    assert [chunk.start_seconds for chunk in result.schedule] == [1.0, 5.0]
    assert [chunk.wait_before_playback_seconds for chunk in result.schedule] == [
        0.0,
        0.0,
    ]
    assert result.summary.playback_end_seconds == 5.5
    assert result.summary.listener_tail_seconds == 0.5
    assert result.summary.seconds_above_target == 0.0


def test_reports_longest_continuous_urgent_playback_interval():
    chunks = [
        AudioChunk(arrival_seconds=0.0, duration_seconds=8.0),
        AudioChunk(arrival_seconds=0.0, duration_seconds=1.0),
        AudioChunk(arrival_seconds=20.0, duration_seconds=8.0),
    ]

    result = simulate_playback(
        chunks, input_end_seconds=20.0, adaptive=True
    )

    assert [chunk.playback_rate for chunk in result.schedule] == [1.1, 1.1, 1.1]
    assert math.isclose(
        result.summary.max_continuous_urgent_playback_seconds,
        (8.0 + 1.0) / 1.1,
        abs_tol=1e-12,
    )


@pytest.mark.parametrize(
    "chunks,error",
    [
        ([], "at least one"),
        ([AudioChunk(-0.1, 1.0)], "arrival"),
        ([AudioChunk(0.0, 0.0)], "durations"),
        (
            [AudioChunk(1.0, 1.0), AudioChunk(0.5, 1.0)],
            "non-decreasing",
        ),
    ],
)
def test_invalid_traces_are_rejected(chunks, error):
    with pytest.raises(ValueError, match=error):
        simulate_playback(chunks, input_end_seconds=1.0, adaptive=True)


def test_invalid_policy_is_rejected():
    with pytest.raises(ValueError, match="thresholds"):
        PlaybackPolicy(target_queue_seconds=8.0, urgent_queue_seconds=5.0)


def _parent_frame(
    parent_sequence_id,
    arrival_seconds,
    duration_seconds,
    *,
    audio_frame_id=0,
    parent_frame_count=1,
    source_index=-1,
    source_start_ms=None,
    source_end_ms=None,
):
    return ParentAudioFrame(
        arrival_seconds=arrival_seconds,
        duration_seconds=duration_seconds,
        parent_sequence_id=parent_sequence_id,
        audio_frame_id=audio_frame_id,
        parent_frame_count=parent_frame_count,
        audio_bytes=round(duration_seconds * 32_000),
        source_index=source_index,
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
    )


def test_parent_freshness_cap_preserves_an_under_cap_trace():
    frames = [
        _parent_frame(0, 1.0, 0.5, source_index=10),
        _parent_frame(1, 3.0, 0.5),
    ]

    result = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=3.0,
        hard_cap_seconds=2.0,
        strategy="oldest_first",
    )

    assert result.summary.hard_cap_achieved
    assert result.summary.frames_received == 2
    assert result.summary.frames_retained == 2
    assert result.summary.frames_dropped == 0
    assert result.summary.parents_partially_dropped == 0
    assert result.summary.retained_source_percent == 100.0
    assert result.summary.dropped_source_percent == 0.0
    assert result.summary.retained_audio_bytes_percent == 100.0
    assert result.summary.dropped_audio_bytes_percent == 0.0
    assert result.summary.eviction_event_count == 0
    assert result.summary.discontinuity_count == 0
    assert result.summary.listener_tail_seconds == 0.5
    assert [frame.source_index for frame in result.schedule] == [10, 1]
    assert [frame.start_seconds for frame in result.schedule] == [1.0, 3.0]
    assert not result.dropped_frames


def test_parent_freshness_cap_matches_playback_simulation_without_a_breach():
    frames = [
        _parent_frame(0, 0.0, 4.0),
        _parent_frame(1, 0.0, 1.0),
        _parent_frame(2, 0.0, 3.0),
        _parent_frame(3, 0.0, 3.0),
    ]
    baseline = simulate_playback(
        [
            AudioChunk(
                arrival_seconds=frame.arrival_seconds,
                duration_seconds=frame.duration_seconds,
                audio_bytes=frame.audio_bytes,
                source_index=index,
            )
            for index, frame in enumerate(frames)
        ],
        input_end_seconds=1.0,
        adaptive=True,
    )

    freshness = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=1.0,
        hard_cap_seconds=100.0,
        strategy="oldest_first",
    )

    assert [
        (
            frame.source_index,
            frame.arrival_seconds,
            frame.source_duration_seconds,
            frame.start_seconds,
            frame.end_seconds,
            frame.wait_before_playback_seconds,
            frame.projected_queue_at_normal_rate_seconds,
            frame.queue_depth_seconds,
            frame.playback_rate,
            frame.playback_mode,
            frame.mode_changed,
        )
        for frame in freshness.schedule
    ] == [
        (
            chunk.source_index,
            chunk.arrival_seconds,
            chunk.source_duration_seconds,
            chunk.start_seconds,
            chunk.end_seconds,
            chunk.wait_before_playback_seconds,
            chunk.projected_queue_at_normal_rate_seconds,
            chunk.queue_depth_seconds,
            chunk.playback_rate,
            chunk.playback_mode,
            chunk.mode_changed,
        )
        for chunk in baseline.schedule
    ]
    assert (
        freshness.summary.total_source_duration_seconds
        == baseline.summary.total_source_duration_seconds
    )
    assert (
        freshness.summary.total_scheduled_duration_seconds
        == baseline.summary.total_scheduled_duration_seconds
    )
    assert (
        freshness.summary.playback_end_seconds
        == baseline.summary.playback_end_seconds
    )
    assert (
        freshness.summary.peak_queue_depth_seconds
        == baseline.summary.peak_queue_depth_seconds
    )
    assert (
        freshness.summary.arrival_queue_p50_seconds
        == baseline.summary.arrival_queue_p50_seconds
    )
    assert (
        freshness.summary.arrival_queue_p95_seconds
        == baseline.summary.arrival_queue_p95_seconds
    )
    assert (
        freshness.summary.accelerated_source_duration_seconds
        == baseline.summary.accelerated_source_duration_seconds
    )
    assert (
        freshness.summary.urgent_source_duration_seconds
        == baseline.summary.urgent_source_duration_seconds
    )


def test_oldest_first_drops_minimum_complete_parent_prefix_and_compacts():
    frames = [
        _parent_frame(0, 0.0, 2.0),
        _parent_frame(1, 0.1, 2.0),
        _parent_frame(2, 0.2, 2.0),
        _parent_frame(3, 0.3, 2.0),
    ]

    result = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=1.0,
        hard_cap_seconds=6.0,
        strategy="oldest_first",
    )

    assert result.summary.hard_cap_achieved
    assert result.summary.dropped_parent_sequence_ids == (1,)
    assert result.summary.retained_parent_sequence_ids == (0, 2, 3)
    assert result.summary.frames_dropped == 1
    assert result.summary.dropped_source_duration_seconds == 2.0
    assert result.summary.dropped_audio_bytes == 64_000
    assert result.summary.retained_source_percent == 75.0
    assert result.summary.retained_audio_bytes_percent == 75.0
    assert result.summary.dropped_audio_bytes_percent == 25.0
    assert result.summary.parents_partially_dropped == 0
    assert result.summary.eviction_event_count == 1
    assert result.summary.discontinuity_count == 1
    assert result.summary.max_consecutive_dropped_parents == 1
    assert (
        result.summary.max_dropped_source_duration_per_discontinuity_seconds
        == 2.0
    )
    assert result.summary.accelerated_source_duration_seconds == 4.0
    assert math.isclose(
        result.summary.accelerated_source_percent,
        200.0 / 3.0,
        abs_tol=1e-12,
    )
    assert result.summary.rate_frame_counts == {
        "1.00x": 1,
        "1.05x": 2,
        "1.10x": 0,
    }

    assert [frame.parent_sequence_id for frame in result.schedule] == [
        0,
        2,
        3,
    ]
    assert [frame.parent_sequence_id for frame in result.dropped_frames] == [1]
    assert result.schedule[0].start_seconds == 0.0
    assert result.schedule[1].start_seconds == 2.0
    assert math.isclose(
        result.schedule[2].start_seconds,
        2.0 + 2.0 / 1.05,
        abs_tol=1e-12,
    )
    assert result.schedule[1].scheduled_at_seconds == 0.2
    assert result.schedule[2].scheduled_at_seconds == 0.3
    assert result.evictions[0].eligible_parent_sequence_ids == (1, 2, 3)
    assert result.evictions[0].dropped_parent_sequence_ids == (1,)
    assert result.evictions[0].queue_before_eviction_seconds > 6.0
    assert result.evictions[0].queue_after_eviction_seconds < 6.0
    assert result.summary.peak_queue_before_eviction_seconds > 6.0
    assert result.summary.peak_queue_depth_seconds < 6.0


def test_jump_to_latest_complete_discards_all_older_eligible_parents():
    frames = [
        _parent_frame(0, 0.0, 2.0),
        _parent_frame(1, 0.1, 2.0),
        _parent_frame(2, 0.2, 2.0),
        _parent_frame(3, 0.3, 2.0),
    ]

    result = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=1.0,
        hard_cap_seconds=6.0,
        strategy="jump_to_latest_complete",
    )

    assert result.summary.hard_cap_achieved
    assert result.summary.dropped_parent_sequence_ids == (1, 2)
    assert result.summary.retained_parent_sequence_ids == (0, 3)
    assert result.summary.retained_source_percent == 50.0
    assert result.summary.dropped_source_percent == 50.0
    assert result.summary.discontinuity_count == 1
    assert result.summary.max_consecutive_dropped_parents == 2
    assert (
        result.summary.max_dropped_source_duration_per_discontinuity_seconds
        == 4.0
    )
    assert result.evictions[0].eligible_parent_sequence_ids == (1, 2, 3)
    assert result.evictions[0].dropped_parent_sequence_ids == (1, 2)
    assert [frame.parent_sequence_id for frame in result.schedule] == [0, 3]
    assert [frame.start_seconds for frame in result.schedule] == [0.0, 2.0]


def test_incomplete_parent_causes_residual_breach_until_whole_parent_is_safe():
    frames = [
        _parent_frame(0, 0.0, 4.0),
        _parent_frame(
            1,
            0.1,
            2.0,
            audio_frame_id=0,
            parent_frame_count=2,
        ),
        _parent_frame(
            1,
            0.2,
            2.0,
            audio_frame_id=1,
            parent_frame_count=2,
        ),
    ]

    result = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=1.0,
        hard_cap_seconds=5.0,
        strategy="oldest_first",
    )

    first_parent_one_decision = result.decisions[1]
    assert not first_parent_one_decision.eligible_parent_sequence_ids
    assert first_parent_one_decision.residual_hard_cap_breach
    assert first_parent_one_decision.residual_over_cap_seconds > 0

    completion_decision = result.decisions[2]
    assert completion_decision.eligible_parent_sequence_ids == (1,)
    assert completion_decision.dropped_parent_sequence_ids == (1,)
    assert not completion_decision.residual_hard_cap_breach
    assert [frame.audio_frame_id for frame in result.dropped_frames] == [0, 1]
    assert result.summary.parents_partially_dropped == 0
    assert not result.summary.hard_cap_achieved
    assert result.summary.residual_breach_events == 1
    assert result.summary.residual_breach_entries == 1
    assert result.summary.seconds_above_hard_cap == 0.1


def test_started_parent_is_protected_while_later_complete_parent_can_drop():
    frames = [
        _parent_frame(0, 0.0, 1.0),
        _parent_frame(1, 0.0, 4.0),
        _parent_frame(2, 1.5, 4.0),
    ]

    result = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=2.0,
        hard_cap_seconds=5.5,
        strategy="oldest_first",
    )

    assert result.decisions[-1].eligible_parent_sequence_ids == (2,)
    assert result.decisions[-1].dropped_parent_sequence_ids == (2,)
    assert result.summary.dropped_parent_sequence_ids == (2,)
    assert result.summary.retained_parent_sequence_ids == (0, 1)
    assert result.summary.hard_cap_achieved


def test_cancellation_guard_protects_parent_at_boundary_and_zero_is_default():
    frames = [
        _parent_frame(0, 0.0, 2.0),
        _parent_frame(1, 1.9, 2.0),
    ]
    default = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=2.0,
        hard_cap_seconds=1.5,
        strategy="oldest_first",
    )
    explicit_zero = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=2.0,
        hard_cap_seconds=1.5,
        strategy="oldest_first",
        cancellation_guard_seconds=0.0,
    )
    outside_guard = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=2.0,
        hard_cap_seconds=1.5,
        strategy="oldest_first",
        cancellation_guard_seconds=0.099,
    )
    at_guard_boundary = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=2.0,
        hard_cap_seconds=1.5,
        strategy="oldest_first",
        cancellation_guard_seconds=0.1,
    )

    assert default == explicit_zero
    assert default.summary.cancellation_guard_seconds == 0.0
    assert default.summary.dropped_parent_sequence_ids == (1,)
    assert outside_guard.summary.dropped_parent_sequence_ids == (1,)

    assert at_guard_boundary.summary.cancellation_guard_seconds == 0.1
    assert not at_guard_boundary.decisions[-1].eligible_parent_sequence_ids
    assert not at_guard_boundary.decisions[-1].dropped_parent_sequence_ids
    assert at_guard_boundary.decisions[-1].residual_hard_cap_breach
    assert not at_guard_boundary.summary.hard_cap_achieved
    assert at_guard_boundary.summary.dropped_parent_sequence_ids == ()


def test_jump_strategy_reports_residual_when_latest_parent_alone_exceeds_cap():
    frames = [
        _parent_frame(0, 0.0, 2.0),
        _parent_frame(1, 0.1, 10.0),
    ]

    result = simulate_parent_freshness_cap(
        frames,
        input_end_seconds=1.0,
        hard_cap_seconds=5.0,
        strategy="jump_to_latest_complete",
    )

    assert result.decisions[-1].eligible_parent_sequence_ids == (1,)
    assert not result.decisions[-1].dropped_parent_sequence_ids
    assert result.decisions[-1].residual_hard_cap_breach
    assert not result.summary.hard_cap_achieved
    assert result.summary.residual_breach_events == 1
    assert result.summary.peak_residual_over_cap_seconds > 5.0
    assert result.summary.parents_dropped == 0


@pytest.mark.parametrize(
    ("frames", "error"),
    [
        ([], "at least one"),
        ([_parent_frame(0, -0.1, 1.0)], "arrival"),
        ([_parent_frame(0, 0.0, 0.0)], "durations"),
        (
            [
                _parent_frame(0, 1.0, 1.0),
                _parent_frame(1, 0.5, 1.0),
            ],
            "non-decreasing",
        ),
        (
            [
                _parent_frame(
                    0,
                    0.0,
                    1.0,
                    audio_frame_id=0,
                    parent_frame_count=2,
                )
            ],
            "final parent",
        ),
        (
            [
                _parent_frame(
                    0,
                    0.0,
                    1.0,
                    audio_frame_id=1,
                    parent_frame_count=2,
                )
            ],
            "start with audio_frame_id zero",
        ),
        (
            [
                _parent_frame(
                    0,
                    0.0,
                    1.0,
                    audio_frame_id=0,
                    parent_frame_count=2,
                ),
                _parent_frame(
                    0,
                    0.1,
                    1.0,
                    audio_frame_id=0,
                    parent_frame_count=2,
                ),
            ],
            "contiguous",
        ),
        (
            [
                _parent_frame(1, 0.0, 1.0),
                _parent_frame(0, 0.1, 1.0),
            ],
            "strictly increasing",
        ),
        (
            [
                _parent_frame(
                    0,
                    0.0,
                    1.0,
                    source_end_ms=-1.0,
                )
            ],
            "source_end_ms",
        ),
        (
            [
                _parent_frame(
                    0,
                    0.0,
                    1.0,
                    source_start_ms=20.0,
                    source_end_ms=10.0,
                )
            ],
            "cannot precede",
        ),
        (
            [
                _parent_frame(
                    0,
                    0.0,
                    1.0,
                    audio_frame_id=0,
                    parent_frame_count=2,
                    source_end_ms=10.0,
                ),
                _parent_frame(
                    0,
                    0.1,
                    1.0,
                    audio_frame_id=1,
                    parent_frame_count=2,
                    source_end_ms=20.0,
                ),
            ],
            "consistent within a parent",
        ),
    ],
)
def test_invalid_parent_frame_traces_are_rejected(frames, error):
    with pytest.raises(ValueError, match=error):
        simulate_parent_freshness_cap(
            frames,
            input_end_seconds=1.0,
            hard_cap_seconds=10.0,
            strategy="oldest_first",
        )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"input_end_seconds": -1.0}, "input_end_seconds"),
        ({"hard_cap_seconds": 0.0}, "hard_cap_seconds"),
        ({"hard_cap_seconds": math.inf}, "hard_cap_seconds"),
        (
            {"cancellation_guard_seconds": -0.1},
            "cancellation_guard_seconds",
        ),
        (
            {"cancellation_guard_seconds": math.inf},
            "cancellation_guard_seconds",
        ),
        (
            {"cancellation_guard_seconds": math.nan},
            "cancellation_guard_seconds",
        ),
        (
            {"cancellation_guard_seconds": True},
            "cancellation_guard_seconds",
        ),
        (
            {"cancellation_guard_seconds": "0.1"},
            "cancellation_guard_seconds",
        ),
        ({"strategy": "newest_first"}, "strategy"),
        ({"adaptive": 1}, "adaptive"),
    ],
)
def test_invalid_parent_freshness_cap_options_are_rejected(kwargs, error):
    options = {
        "input_end_seconds": 1.0,
        "hard_cap_seconds": 10.0,
        "strategy": "oldest_first",
    }
    options.update(kwargs)
    with pytest.raises(ValueError, match=error):
        simulate_parent_freshness_cap([_parent_frame(0, 0.0, 1.0)], **options)
