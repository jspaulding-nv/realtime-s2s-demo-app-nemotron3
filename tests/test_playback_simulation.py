import math

import pytest

from playback_simulation import (
    AudioChunk,
    PlaybackPolicy,
    playback_rate_for_mode,
    select_playback_mode,
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
