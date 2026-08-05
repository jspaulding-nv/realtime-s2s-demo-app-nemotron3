#!/usr/bin/env python3
"""Deterministic simulation of the browser translated-audio playback queue.

The adaptive policy in this module intentionally mirrors
``frontend/src/utils/playbackPolicy.ts``. Every received PCM chunk is
scheduled in arrival order; the 10-second queue limit is reported as an SLA
breach and never used as a destructive drop boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence


PlaybackMode = Literal["normal", "catch-up", "urgent", "over-limit"]
FreshnessStrategy = Literal[
    "oldest_first",
    "jump_to_latest_complete",
    "oldest_frame_first",
    "truncate_parent_tail",
]


@dataclass(frozen=True)
class PlaybackPolicy:
    """Adaptive queue thresholds, release hysteresis, and playback rates."""

    target_queue_seconds: float = 5.0
    urgent_queue_seconds: float = 8.0
    limit_queue_seconds: float = 10.0
    catch_up_release_seconds: float = 4.0
    urgent_release_seconds: float = 7.0
    normal_rate: float = 1.0
    catch_up_rate: float = 1.05
    urgent_rate: float = 1.10

    def __post_init__(self) -> None:
        thresholds = (
            self.catch_up_release_seconds,
            self.target_queue_seconds,
            self.urgent_release_seconds,
            self.urgent_queue_seconds,
            self.limit_queue_seconds,
        )
        if any(value < 0 for value in thresholds):
            raise ValueError("queue thresholds must be non-negative")
        if not (
            self.catch_up_release_seconds < self.target_queue_seconds
            <= self.urgent_release_seconds < self.urgent_queue_seconds
            <= self.limit_queue_seconds
        ):
            raise ValueError("queue thresholds and release points are inconsistent")
        if not (0 < self.normal_rate <= self.catch_up_rate <= self.urgent_rate):
            raise ValueError("playback rates must be positive and non-decreasing")


DEFAULT_PLAYBACK_POLICY = PlaybackPolicy()


@dataclass(frozen=True)
class AudioChunk:
    """One translated PCM chunk delivered to the browser."""

    arrival_seconds: float
    duration_seconds: float
    audio_bytes: int = 0
    source_index: int = -1


@dataclass(frozen=True)
class ScheduledChunk:
    """Scheduling decision for one translated PCM chunk."""

    source_index: int
    arrival_seconds: float
    source_duration_seconds: float
    start_seconds: float
    end_seconds: float
    wait_before_playback_seconds: float
    projected_queue_at_normal_rate_seconds: float
    queue_depth_seconds: float
    playback_rate: float
    playback_mode: PlaybackMode
    mode_changed: bool
    above_target: bool
    above_limit: bool


@dataclass(frozen=True)
class PlaybackSummary:
    """Audience-facing playback metrics for one simulation."""

    adaptive: bool
    chunks_received: int
    chunks_scheduled: int
    chunks_dropped: int
    total_source_duration_seconds: float
    total_scheduled_duration_seconds: float
    first_arrival_seconds: float
    input_end_seconds: float
    playback_end_seconds: float
    listener_tail_seconds: float
    peak_queue_depth_seconds: float
    arrival_queue_p50_seconds: float
    arrival_queue_p95_seconds: float
    time_weighted_queue_p50_seconds: float
    time_weighted_queue_p95_seconds: float
    seconds_above_target: float
    seconds_above_limit: float
    percent_playback_window_above_target: float
    percent_playback_window_above_limit: float
    limit_breach_entries: int
    accelerated_source_duration_seconds: float
    accelerated_source_percent: float
    urgent_source_duration_seconds: float
    urgent_source_percent: float
    max_continuous_urgent_playback_seconds: float
    rate_chunk_counts: dict[str, int]
    rate_source_duration_seconds: dict[str, float]
    mode_chunk_counts: dict[str, int]


@dataclass(frozen=True)
class PlaybackSimulation:
    """Full schedule plus compact summary."""

    schedule: tuple[ScheduledChunk, ...]
    summary: PlaybackSummary


@dataclass(frozen=True)
class ParentAudioFrame:
    """One browser-delivered PCM frame with its complete parent identity.

    ``parent_frame_count`` is repeated on every frame so the simulator can
    establish parent completion causally when the final frame arrives. Parent
    identifiers are non-negative integers, and frame identifiers are
    contiguous and zero-based within each parent.
    """

    arrival_seconds: float
    duration_seconds: float
    parent_sequence_id: int
    audio_frame_id: int
    parent_frame_count: int
    audio_bytes: int = 0
    source_index: int = -1
    source_start_ms: float | None = None
    source_end_ms: float | None = None


@dataclass(frozen=True)
class FreshnessScheduledFrame:
    """Final scheduling decision for one retained parent audio frame."""

    source_index: int
    parent_sequence_id: int
    audio_frame_id: int
    parent_frame_count: int
    arrival_seconds: float
    source_duration_seconds: float
    audio_bytes: int
    scheduled_at_seconds: float
    start_seconds: float
    end_seconds: float
    wait_before_playback_seconds: float
    projected_queue_at_normal_rate_seconds: float
    queue_depth_seconds: float
    playback_rate: float
    playback_mode: PlaybackMode
    mode_changed: bool


@dataclass(frozen=True)
class FreshnessCapDecision:
    """Post-scheduling freshness decision at one frame arrival."""

    source_index: int
    parent_sequence_id: int
    audio_frame_id: int
    arrival_seconds: float
    queue_before_eviction_seconds: float
    queue_after_eviction_seconds: float
    eligible_parent_sequence_ids: tuple[int, ...]
    dropped_parent_sequence_ids: tuple[int, ...]
    residual_hard_cap_breach: bool
    residual_over_cap_seconds: float


@dataclass(frozen=True)
class FreshnessEviction:
    """One atomic whole-parent eviction and retained-queue compaction."""

    at_seconds: float
    trigger_source_index: int
    trigger_parent_sequence_id: int
    trigger_audio_frame_id: int
    eligible_parent_sequence_ids: tuple[int, ...]
    dropped_parent_sequence_ids: tuple[int, ...]
    dropped_frame_count: int
    dropped_audio_bytes: int
    dropped_source_duration_seconds: float
    queue_before_eviction_seconds: float
    queue_after_eviction_seconds: float


@dataclass(frozen=True)
class FreshnessCapSummary:
    """Listener-facing retention, freshness, and fidelity metrics."""

    strategy: FreshnessStrategy
    adaptive: bool
    hard_cap_seconds: float
    cancellation_guard_seconds: float
    hard_cap_achieved: bool
    frames_received: int
    frames_retained: int
    frames_dropped: int
    parents_received: int
    parents_retained: int
    parents_dropped: int
    parents_partially_dropped: int
    retained_parent_sequence_ids: tuple[int, ...]
    dropped_parent_sequence_ids: tuple[int, ...]
    total_source_duration_seconds: float
    retained_source_duration_seconds: float
    dropped_source_duration_seconds: float
    retained_source_percent: float
    dropped_source_percent: float
    total_audio_bytes: int
    retained_audio_bytes: int
    dropped_audio_bytes: int
    retained_audio_bytes_percent: float
    dropped_audio_bytes_percent: float
    first_arrival_seconds: float
    input_end_seconds: float
    playback_end_seconds: float
    listener_tail_seconds: float
    total_scheduled_duration_seconds: float
    peak_queue_before_eviction_seconds: float
    peak_queue_depth_seconds: float
    arrival_queue_p50_seconds: float
    arrival_queue_p95_seconds: float
    time_weighted_queue_p50_seconds: float
    time_weighted_queue_p95_seconds: float
    seconds_above_target: float
    seconds_above_hard_cap: float
    percent_playback_window_above_target: float
    percent_playback_window_above_hard_cap: float
    residual_breach_events: int
    residual_breach_entries: int
    peak_residual_over_cap_seconds: float
    eviction_event_count: int
    discontinuity_count: int
    max_consecutive_dropped_parents: int
    max_dropped_source_duration_per_discontinuity_seconds: float
    accelerated_source_duration_seconds: float
    accelerated_source_percent: float
    urgent_source_duration_seconds: float
    urgent_source_percent: float
    max_continuous_urgent_playback_seconds: float
    rate_frame_counts: dict[str, int]
    rate_source_duration_seconds: dict[str, float]
    mode_frame_counts: dict[str, int]


@dataclass(frozen=True)
class FreshnessCapSimulation:
    """Final retained schedule, causal decisions, evictions, and summary."""

    schedule: tuple[FreshnessScheduledFrame, ...]
    decisions: tuple[FreshnessCapDecision, ...]
    evictions: tuple[FreshnessEviction, ...]
    dropped_frames: tuple[ParentAudioFrame, ...]
    summary: FreshnessCapSummary


@dataclass
class _MutableFreshnessFrame:
    """Internal schedule state that can be canceled and recreated."""

    frame: ParentAudioFrame
    source_index: int
    scheduled_at_seconds: float
    start_seconds: float
    end_seconds: float
    projected_queue_at_normal_rate_seconds: float
    playback_rate: float
    playback_mode: PlaybackMode
    mode_changed: bool


def select_playback_mode(
    projected_queue_seconds: float,
    current_mode: PlaybackMode = "normal",
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
) -> PlaybackMode:
    """Select a mode using the frontend's exact boundaries and hysteresis."""

    if projected_queue_seconds > policy.limit_queue_seconds:
        return "over-limit"

    if current_mode in ("urgent", "over-limit"):
        if projected_queue_seconds >= policy.urgent_release_seconds:
            return "urgent"
        if projected_queue_seconds >= policy.catch_up_release_seconds:
            return "catch-up"
        return "normal"

    if current_mode == "catch-up":
        if projected_queue_seconds >= policy.urgent_queue_seconds:
            return "urgent"
        if projected_queue_seconds >= policy.catch_up_release_seconds:
            return "catch-up"
        return "normal"

    if projected_queue_seconds >= policy.urgent_queue_seconds:
        return "urgent"
    if projected_queue_seconds >= policy.target_queue_seconds:
        return "catch-up"
    return "normal"


def playback_rate_for_mode(
    mode: PlaybackMode,
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
) -> float:
    """Map a frontend playback mode to its configured rate."""

    if mode in ("urgent", "over-limit"):
        return policy.urgent_rate
    if mode == "catch-up":
        return policy.catch_up_rate
    return policy.normal_rate


def _nearest_rank_percentile(values: Sequence[float], quantile: float) -> float:
    """Match the nearest-rank percentile used by the TypeScript policy."""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _seconds_above_threshold(
    schedule: Sequence[ScheduledChunk], threshold_seconds: float
) -> float:
    """Integrate exact queue time above a threshold between arrivals."""

    total = 0.0
    for index, chunk in enumerate(schedule):
        interval_end = (
            schedule[index + 1].arrival_seconds
            if index + 1 < len(schedule)
            else chunk.end_seconds
        )
        interval_seconds = max(0.0, interval_end - chunk.arrival_seconds)
        total += min(
            interval_seconds,
            max(0.0, chunk.queue_depth_seconds - threshold_seconds),
        )
    return total


def _time_weighted_queue_percentile(
    schedule: Sequence[ScheduledChunk],
    playback_window_seconds: float,
    quantile: float,
) -> float:
    """Calculate an exact wall-clock percentile of listener queue depth."""

    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between zero and one")
    if not schedule or playback_window_seconds <= 0:
        return 0.0

    target_seconds_above = (1.0 - quantile) * playback_window_seconds
    low = 0.0
    high = max(chunk.queue_depth_seconds for chunk in schedule)
    for _ in range(64):
        midpoint = (low + high) / 2.0
        if _seconds_above_threshold(schedule, midpoint) > target_seconds_above:
            low = midpoint
        else:
            high = midpoint
    return high


def _rate_label(rate: float) -> str:
    return f"{rate:.2f}x"


def simulate_playback(
    chunks: Sequence[AudioChunk],
    *,
    input_end_seconds: float,
    adaptive: bool,
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
) -> PlaybackSimulation:
    """Schedule every translated chunk and calculate listener-facing metrics.

    ``arrival_seconds`` values must be in the order in which the browser saw
    the chunks. The caller should sort trace rows by timestamp while retaining
    file order for equal timestamps.
    """

    if input_end_seconds < 0 or not math.isfinite(input_end_seconds):
        raise ValueError("input_end_seconds must be finite and non-negative")
    if not chunks:
        raise ValueError("at least one audio chunk is required")

    previous_arrival = -math.inf
    previous_mode: PlaybackMode = "normal"
    next_start_seconds = 0.0
    scheduled: list[ScheduledChunk] = []
    limit_breach_entries = 0

    for fallback_index, chunk in enumerate(chunks):
        if (
            not math.isfinite(chunk.arrival_seconds)
            or chunk.arrival_seconds < 0
        ):
            raise ValueError("chunk arrival times must be finite and non-negative")
        if chunk.arrival_seconds < previous_arrival:
            raise ValueError("chunks must be ordered by non-decreasing arrival time")
        if (
            not math.isfinite(chunk.duration_seconds)
            or chunk.duration_seconds <= 0
        ):
            raise ValueError("chunk durations must be finite and positive")
        if chunk.audio_bytes < 0:
            raise ValueError("audio_bytes must be non-negative")

        start_seconds = max(next_start_seconds, chunk.arrival_seconds)
        wait_seconds = max(0.0, start_seconds - chunk.arrival_seconds)
        projected_queue = wait_seconds + chunk.duration_seconds
        mode: PlaybackMode = (
            select_playback_mode(projected_queue, previous_mode, policy)
            if adaptive
            else "normal"
        )
        playback_rate = playback_rate_for_mode(mode, policy)
        scheduled_duration = chunk.duration_seconds / playback_rate
        end_seconds = start_seconds + scheduled_duration
        queue_depth = max(0.0, end_seconds - chunk.arrival_seconds)
        mode_changed = mode != previous_mode

        if mode == "over-limit" and previous_mode != "over-limit":
            limit_breach_entries += 1

        scheduled.append(
            ScheduledChunk(
                source_index=(
                    chunk.source_index
                    if chunk.source_index >= 0
                    else fallback_index
                ),
                arrival_seconds=chunk.arrival_seconds,
                source_duration_seconds=chunk.duration_seconds,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                wait_before_playback_seconds=wait_seconds,
                projected_queue_at_normal_rate_seconds=projected_queue,
                queue_depth_seconds=queue_depth,
                playback_rate=playback_rate,
                playback_mode=mode,
                mode_changed=mode_changed,
                above_target=queue_depth > policy.target_queue_seconds,
                above_limit=queue_depth > policy.limit_queue_seconds,
            )
        )
        next_start_seconds = end_seconds
        previous_mode = mode
        previous_arrival = chunk.arrival_seconds

    queue_depths = [chunk.queue_depth_seconds for chunk in scheduled]
    total_source_duration = sum(
        chunk.source_duration_seconds for chunk in scheduled
    )
    total_scheduled_duration = sum(
        chunk.end_seconds - chunk.start_seconds for chunk in scheduled
    )
    first_arrival = scheduled[0].arrival_seconds
    playback_end = scheduled[-1].end_seconds
    playback_window = max(0.0, playback_end - first_arrival)
    seconds_above_target = _seconds_above_threshold(
        scheduled, policy.target_queue_seconds
    )
    seconds_above_limit = _seconds_above_threshold(
        scheduled, policy.limit_queue_seconds
    )

    rates = (policy.normal_rate, policy.catch_up_rate, policy.urgent_rate)
    rate_chunk_counts = {_rate_label(rate): 0 for rate in rates}
    rate_source_durations = {_rate_label(rate): 0.0 for rate in rates}
    mode_chunk_counts = {
        "normal": 0,
        "catch-up": 0,
        "urgent": 0,
        "over-limit": 0,
    }
    accelerated_source_duration = 0.0
    urgent_source_duration = 0.0
    max_continuous_urgent_playback = 0.0
    continuous_urgent_playback = 0.0
    previous_scheduled_end: float | None = None

    for chunk in scheduled:
        label = _rate_label(chunk.playback_rate)
        rate_chunk_counts[label] = rate_chunk_counts.get(label, 0) + 1
        rate_source_durations[label] = (
            rate_source_durations.get(label, 0.0)
            + chunk.source_duration_seconds
        )
        mode_chunk_counts[chunk.playback_mode] += 1
        if chunk.playback_rate > policy.normal_rate:
            accelerated_source_duration += chunk.source_duration_seconds
        if chunk.playback_rate >= policy.urgent_rate:
            urgent_source_duration += chunk.source_duration_seconds
            scheduled_duration = chunk.end_seconds - chunk.start_seconds
            is_contiguous = (
                previous_scheduled_end is not None
                and math.isclose(
                    chunk.start_seconds,
                    previous_scheduled_end,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            )
            continuous_urgent_playback = (
                continuous_urgent_playback + scheduled_duration
                if is_contiguous
                else scheduled_duration
            )
            max_continuous_urgent_playback = max(
                max_continuous_urgent_playback,
                continuous_urgent_playback,
            )
        else:
            continuous_urgent_playback = 0.0
        previous_scheduled_end = chunk.end_seconds

    summary = PlaybackSummary(
        adaptive=adaptive,
        chunks_received=len(chunks),
        chunks_scheduled=len(scheduled),
        chunks_dropped=0,
        total_source_duration_seconds=total_source_duration,
        total_scheduled_duration_seconds=total_scheduled_duration,
        first_arrival_seconds=first_arrival,
        input_end_seconds=input_end_seconds,
        playback_end_seconds=playback_end,
        listener_tail_seconds=max(0.0, playback_end - input_end_seconds),
        peak_queue_depth_seconds=max(queue_depths),
        arrival_queue_p50_seconds=_nearest_rank_percentile(queue_depths, 0.50),
        arrival_queue_p95_seconds=_nearest_rank_percentile(queue_depths, 0.95),
        time_weighted_queue_p50_seconds=_time_weighted_queue_percentile(
            scheduled, playback_window, 0.50
        ),
        time_weighted_queue_p95_seconds=_time_weighted_queue_percentile(
            scheduled, playback_window, 0.95
        ),
        seconds_above_target=seconds_above_target,
        seconds_above_limit=seconds_above_limit,
        percent_playback_window_above_target=(
            seconds_above_target / playback_window * 100.0
            if playback_window > 0
            else 0.0
        ),
        percent_playback_window_above_limit=(
            seconds_above_limit / playback_window * 100.0
            if playback_window > 0
            else 0.0
        ),
        limit_breach_entries=limit_breach_entries,
        accelerated_source_duration_seconds=accelerated_source_duration,
        accelerated_source_percent=(
            accelerated_source_duration / total_source_duration * 100.0
        ),
        urgent_source_duration_seconds=urgent_source_duration,
        urgent_source_percent=(
            urgent_source_duration / total_source_duration * 100.0
        ),
        max_continuous_urgent_playback_seconds=(
            max_continuous_urgent_playback
        ),
        rate_chunk_counts=rate_chunk_counts,
        rate_source_duration_seconds=rate_source_durations,
        mode_chunk_counts=mode_chunk_counts,
    )
    return PlaybackSimulation(schedule=tuple(scheduled), summary=summary)


_FRESHNESS_TIME_EPSILON = 1e-12


def _validate_integer(
    value: object,
    *,
    field_name: str,
    minimum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{field_name} must be a {qualifier} integer")
    return value


def _validate_parent_audio_frames(
    frames: Sequence[ParentAudioFrame],
) -> tuple[int, ...]:
    if not frames:
        raise ValueError("at least one parent audio frame is required")

    previous_arrival = -math.inf
    previous_parent: int | None = None
    previous_parent_frame_count = 0
    previous_source_range: tuple[float | None, float | None] | None = None
    next_audio_frame_id = 0
    parent_order: list[int] = []

    for frame in frames:
        if (
            not math.isfinite(frame.arrival_seconds)
            or frame.arrival_seconds < 0
        ):
            raise ValueError(
                "frame arrival times must be finite and non-negative"
            )
        if frame.arrival_seconds < previous_arrival:
            raise ValueError(
                "frames must be ordered by non-decreasing arrival time"
            )
        if (
            not math.isfinite(frame.duration_seconds)
            or frame.duration_seconds <= 0
        ):
            raise ValueError("frame durations must be finite and positive")

        parent_sequence_id = _validate_integer(
            frame.parent_sequence_id,
            field_name="parent_sequence_id",
            minimum=0,
        )
        audio_frame_id = _validate_integer(
            frame.audio_frame_id,
            field_name="audio_frame_id",
            minimum=0,
        )
        parent_frame_count = _validate_integer(
            frame.parent_frame_count,
            field_name="parent_frame_count",
            minimum=1,
        )
        _validate_integer(
            frame.audio_bytes,
            field_name="audio_bytes",
            minimum=0,
        )
        if (
            not isinstance(frame.source_index, int)
            or isinstance(frame.source_index, bool)
            or frame.source_index < -1
        ):
            raise ValueError(
                "source_index must be an integer greater than or equal to -1"
            )
        for field_name, source_offset_ms in (
            ("source_start_ms", frame.source_start_ms),
            ("source_end_ms", frame.source_end_ms),
        ):
            if source_offset_ms is not None and (
                not isinstance(source_offset_ms, (int, float))
                or isinstance(source_offset_ms, bool)
                or not math.isfinite(source_offset_ms)
                or source_offset_ms < 0
            ):
                raise ValueError(
                    f"{field_name} must be null or a finite non-negative number"
                )
        if (
            frame.source_start_ms is not None
            and frame.source_end_ms is not None
            and frame.source_end_ms < frame.source_start_ms
        ):
            raise ValueError("source_end_ms cannot precede source_start_ms")
        if audio_frame_id >= parent_frame_count:
            raise ValueError(
                "audio_frame_id must be less than parent_frame_count"
            )

        if parent_sequence_id != previous_parent:
            if (
                previous_parent is not None
                and next_audio_frame_id != previous_parent_frame_count
            ):
                raise ValueError(
                    "each parent must be complete before the next parent begins"
                )
            if (
                previous_parent is not None
                and parent_sequence_id <= previous_parent
            ):
                raise ValueError(
                    "parent_sequence_id values must be strictly increasing"
                )
            if audio_frame_id != 0:
                raise ValueError(
                    "each parent must start with audio_frame_id zero"
                )
            parent_order.append(parent_sequence_id)
            previous_parent = parent_sequence_id
            previous_parent_frame_count = parent_frame_count
            previous_source_range = (
                frame.source_start_ms,
                frame.source_end_ms,
            )
            next_audio_frame_id = 0
        elif parent_frame_count != previous_parent_frame_count:
            raise ValueError(
                "parent_frame_count must be consistent within a parent"
            )
        elif previous_source_range != (
            frame.source_start_ms,
            frame.source_end_ms,
        ):
            raise ValueError(
                "source offsets must be consistent within a parent"
            )

        if audio_frame_id != next_audio_frame_id:
            raise ValueError(
                "audio_frame_id values must be contiguous and zero-based "
                "within each parent"
            )
        next_audio_frame_id += 1
        previous_arrival = frame.arrival_seconds

    if next_audio_frame_id != previous_parent_frame_count:
        raise ValueError("the final parent audio frame sequence is incomplete")
    return tuple(parent_order)


def _freshness_queue_depth(
    states: Sequence[_MutableFreshnessFrame],
    at_seconds: float,
) -> float:
    if not states:
        return 0.0
    return max(0.0, states[-1].end_seconds - at_seconds)


def _append_freshness_frame(
    states: list[_MutableFreshnessFrame],
    frame: ParentAudioFrame,
    source_index: int,
    *,
    adaptive: bool,
    policy: PlaybackPolicy,
) -> None:
    previous_mode: PlaybackMode = (
        states[-1].playback_mode if states else "normal"
    )
    start_seconds = max(
        states[-1].end_seconds if states else 0.0,
        frame.arrival_seconds,
    )
    projected_queue = (
        start_seconds - frame.arrival_seconds + frame.duration_seconds
    )
    mode: PlaybackMode = (
        select_playback_mode(projected_queue, previous_mode, policy)
        if adaptive
        else "normal"
    )
    playback_rate = playback_rate_for_mode(mode, policy)
    states.append(
        _MutableFreshnessFrame(
            frame=frame,
            source_index=source_index,
            scheduled_at_seconds=frame.arrival_seconds,
            start_seconds=start_seconds,
            end_seconds=(
                start_seconds + frame.duration_seconds / playback_rate
            ),
            projected_queue_at_normal_rate_seconds=projected_queue,
            playback_rate=playback_rate,
            playback_mode=mode,
            mode_changed=mode != previous_mode,
        )
    )


def _reschedule_freshness_future(
    states: list[_MutableFreshnessFrame],
    *,
    at_seconds: float,
) -> None:
    """Compact retained sources without revising their causal rate decisions."""

    protected_count = 0
    for state in states:
        if state.start_seconds <= at_seconds + _FRESHNESS_TIME_EPSILON:
            protected_count += 1
        else:
            break

    protected = states[:protected_count]
    future = states[protected_count:]
    next_start_seconds = max(
        at_seconds,
        protected[-1].end_seconds if protected else at_seconds,
    )

    for state in future:
        state.start_seconds = next_start_seconds
        state.end_seconds = (
            next_start_seconds
            + state.frame.duration_seconds / state.playback_rate
        )
        next_start_seconds = state.end_seconds


def _eligible_complete_parents(
    states: Sequence[_MutableFreshnessFrame],
    *,
    complete_parent_ids: set[int],
    at_seconds: float,
    cancellation_guard_seconds: float,
    parent_order: Sequence[int],
) -> tuple[int, ...]:
    starts_by_parent: dict[int, list[float]] = {}
    for state in states:
        starts_by_parent.setdefault(
            state.frame.parent_sequence_id, []
        ).append(state.start_seconds)
    return tuple(
        parent_sequence_id
        for parent_sequence_id in parent_order
        if parent_sequence_id in complete_parent_ids
        and parent_sequence_id in starts_by_parent
        and all(
            start_seconds
            > (
                at_seconds
                + cancellation_guard_seconds
                + _FRESHNESS_TIME_EPSILON
            )
            for start_seconds in starts_by_parent[parent_sequence_id]
        )
    )


def _remove_parent_states(
    states: list[_MutableFreshnessFrame],
    parent_sequence_id: int,
) -> tuple[_MutableFreshnessFrame, ...]:
    dropped = tuple(
        state
        for state in states
        if state.frame.parent_sequence_id == parent_sequence_id
    )
    states[:] = [
        state
        for state in states
        if state.frame.parent_sequence_id != parent_sequence_id
    ]
    return dropped


def _seconds_above_freshness_threshold(
    decisions: Sequence[FreshnessCapDecision],
    *,
    threshold_seconds: float,
    playback_end_seconds: float,
) -> float:
    total = 0.0
    for index, decision in enumerate(decisions):
        interval_end = (
            decisions[index + 1].arrival_seconds
            if index + 1 < len(decisions)
            else playback_end_seconds
        )
        interval_seconds = max(
            0.0, interval_end - decision.arrival_seconds
        )
        total += min(
            interval_seconds,
            max(
                0.0,
                decision.queue_after_eviction_seconds - threshold_seconds,
            ),
        )
    return total


def _time_weighted_freshness_queue_percentile(
    decisions: Sequence[FreshnessCapDecision],
    *,
    playback_window_seconds: float,
    playback_end_seconds: float,
    quantile: float,
) -> float:
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between zero and one")
    if not decisions or playback_window_seconds <= 0:
        return 0.0

    target_seconds_above = (1.0 - quantile) * playback_window_seconds
    low = 0.0
    high = max(
        decision.queue_after_eviction_seconds for decision in decisions
    )
    for _ in range(64):
        midpoint = (low + high) / 2.0
        if (
            _seconds_above_freshness_threshold(
                decisions,
                threshold_seconds=midpoint,
                playback_end_seconds=playback_end_seconds,
            )
            > target_seconds_above
        ):
            low = midpoint
        else:
            high = midpoint
    return high


def _dropped_parent_run_metrics(
    *,
    parent_order: Sequence[int],
    dropped_parent_ids: set[int],
    source_duration_by_parent: dict[int, float],
) -> tuple[int, int, float]:
    run_count = 0
    max_run_length = 0
    max_run_duration = 0.0
    current_run_length = 0
    current_run_duration = 0.0

    for parent_sequence_id in parent_order:
        if parent_sequence_id in dropped_parent_ids:
            if current_run_length == 0:
                run_count += 1
            current_run_length += 1
            current_run_duration += source_duration_by_parent[
                parent_sequence_id
            ]
            max_run_length = max(max_run_length, current_run_length)
            max_run_duration = max(max_run_duration, current_run_duration)
        else:
            current_run_length = 0
            current_run_duration = 0.0
    return run_count, max_run_length, max_run_duration


def _to_freshness_scheduled_frame(
    state: _MutableFreshnessFrame,
) -> FreshnessScheduledFrame:
    queue_depth = max(0.0, state.end_seconds - state.scheduled_at_seconds)
    return FreshnessScheduledFrame(
        source_index=state.source_index,
        parent_sequence_id=state.frame.parent_sequence_id,
        audio_frame_id=state.frame.audio_frame_id,
        parent_frame_count=state.frame.parent_frame_count,
        arrival_seconds=state.frame.arrival_seconds,
        source_duration_seconds=state.frame.duration_seconds,
        audio_bytes=state.frame.audio_bytes,
        scheduled_at_seconds=state.scheduled_at_seconds,
        start_seconds=state.start_seconds,
        end_seconds=state.end_seconds,
        wait_before_playback_seconds=max(
            0.0, state.start_seconds - state.scheduled_at_seconds
        ),
        projected_queue_at_normal_rate_seconds=(
            state.projected_queue_at_normal_rate_seconds
        ),
        queue_depth_seconds=queue_depth,
        playback_rate=state.playback_rate,
        playback_mode=state.playback_mode,
        mode_changed=state.mode_changed,
    )


def simulate_parent_freshness_cap(
    frames: Sequence[ParentAudioFrame],
    *,
    input_end_seconds: float,
    hard_cap_seconds: float,
    strategy: FreshnessStrategy,
    cancellation_guard_seconds: float = 0.0,
    adaptive: bool = True,
    policy: PlaybackPolicy = DEFAULT_PLAYBACK_POLICY,
) -> FreshnessCapSimulation:
    """Apply a causal whole-parent freshness cap to translated PCM frames.

    Each frame is first scheduled with the same adaptive policy used by
    :func:`simulate_playback`. If the resulting queue exceeds
    ``hard_cap_seconds``, only complete parents for which no retained frame has
    started or entered the cancellation guard window are eligible for atomic
    eviction. Retained future frames are then compacted and rescheduled at the
    same arrival timestamp.

    ``oldest_first`` evicts the minimum oldest-parent prefix that reaches the
    cap, if possible. Retained future frames keep the causal mode/rate selected
    when they arrived; compaction shifts only their start and end times.
    ``jump_to_latest_complete`` evicts all eligible parents older than the
    newest complete parent and intentionally retains that freshest parent.
    ``oldest_frame_first`` is a more destructive frame-boundary
    counterfactual: it evicts the oldest not-yet-audible frame regardless of
    parent completion. It can therefore cut through spoken content and exists
    only to quantify what a hard bound would cost.
    ``truncate_parent_tail`` chooses the oldest not-yet-audible frame, removes
    that frame and every later scheduled frame in the same parent, then
    suppresses future frames through the parent's completion marker. This
    permits at most one retained-prefix/truncated-suffix transition per parent.
    Protected or incomplete audio can therefore leave a residual breach; the
    result reports this explicitly instead of claiming a cap that whole-parent
    eviction cannot guarantee.
    """

    if input_end_seconds < 0 or not math.isfinite(input_end_seconds):
        raise ValueError("input_end_seconds must be finite and non-negative")
    if hard_cap_seconds <= 0 or not math.isfinite(hard_cap_seconds):
        raise ValueError("hard_cap_seconds must be finite and positive")
    if (
        not isinstance(cancellation_guard_seconds, (int, float))
        or isinstance(cancellation_guard_seconds, bool)
        or not math.isfinite(cancellation_guard_seconds)
        or cancellation_guard_seconds < 0
    ):
        raise ValueError(
            "cancellation_guard_seconds must be finite and non-negative"
        )
    if strategy not in (
        "oldest_first",
        "jump_to_latest_complete",
        "oldest_frame_first",
        "truncate_parent_tail",
    ):
        raise ValueError(
            "strategy must be 'oldest_first', "
            "'jump_to_latest_complete', 'oldest_frame_first', or "
            "'truncate_parent_tail'"
        )
    if not isinstance(adaptive, bool):
        raise ValueError("adaptive must be a boolean")

    parent_order = _validate_parent_audio_frames(frames)
    states: list[_MutableFreshnessFrame] = []
    complete_parent_ids: set[int] = set()
    suppressed_parent_ids: set[int] = set()
    dropped_states: list[_MutableFreshnessFrame] = []
    decisions: list[FreshnessCapDecision] = []
    evictions: list[FreshnessEviction] = []

    for fallback_index, frame in enumerate(frames):
        source_index = (
            frame.source_index
            if frame.source_index >= 0
            else fallback_index
        )
        _append_freshness_frame(
            states,
            frame,
            source_index,
            adaptive=adaptive,
            policy=policy,
        )
        if frame.audio_frame_id + 1 == frame.parent_frame_count:
            complete_parent_ids.add(frame.parent_sequence_id)

        queue_before = _freshness_queue_depth(
            states, frame.arrival_seconds
        )
        suppressed_arrival = (
            strategy == "truncate_parent_tail"
            and frame.parent_sequence_id in suppressed_parent_ids
        )
        if suppressed_arrival:
            eligible = (frame.parent_sequence_id,)
        elif strategy in {"oldest_frame_first", "truncate_parent_tail"}:
            eligible = tuple(
                dict.fromkeys(
                    state.frame.parent_sequence_id
                    for state in states
                    if state.start_seconds
                    > (
                        frame.arrival_seconds
                        + cancellation_guard_seconds
                        + _FRESHNESS_TIME_EPSILON
                    )
                )
            )
        else:
            eligible = _eligible_complete_parents(
                states,
                complete_parent_ids=complete_parent_ids,
                at_seconds=frame.arrival_seconds,
                cancellation_guard_seconds=cancellation_guard_seconds,
                parent_order=parent_order,
            )
        dropped_now: list[int] = []
        event_dropped_states: list[_MutableFreshnessFrame] = []

        if suppressed_arrival:
            suppressed_state = states.pop()
            if suppressed_state.frame is not frame:
                raise RuntimeError(
                    "suppressed tail frame was not the newest schedule state"
                )
            event_dropped_states.append(suppressed_state)
            dropped_now.append(frame.parent_sequence_id)
        elif (
            queue_before
            > hard_cap_seconds + _FRESHNESS_TIME_EPSILON
            and eligible
        ):
            if strategy == "truncate_parent_tail":
                while (
                    _freshness_queue_depth(states, frame.arrival_seconds)
                    > hard_cap_seconds + _FRESHNESS_TIME_EPSILON
                ):
                    victim = next(
                        (
                            state
                            for state in states
                            if state.start_seconds
                            > (
                                frame.arrival_seconds
                                + cancellation_guard_seconds
                                + _FRESHNESS_TIME_EPSILON
                            )
                        ),
                        None,
                    )
                    if victim is None:
                        break
                    parent_sequence_id = victim.frame.parent_sequence_id
                    eligible_parent_states = [
                        state
                        for state in states
                        if state.frame.parent_sequence_id
                        == parent_sequence_id
                        and state.start_seconds
                        > (
                            frame.arrival_seconds
                            + cancellation_guard_seconds
                            + _FRESHNESS_TIME_EPSILON
                        )
                    ]
                    if not eligible_parent_states:
                        break
                    removed: list[_MutableFreshnessFrame] = []
                    # Remove the latest available suffix first. Stop as soon
                    # as the cap is restored so the maximum buffered prefix
                    # survives. If more relief is needed, the outer loop
                    # advances to the next oldest eligible parent.
                    for suffix_state in reversed(eligible_parent_states):
                        states.remove(suffix_state)
                        removed.append(suffix_state)
                        _reschedule_freshness_future(
                            states,
                            at_seconds=frame.arrival_seconds,
                        )
                        if (
                            _freshness_queue_depth(
                                states, frame.arrival_seconds
                            )
                            <= hard_cap_seconds + _FRESHNESS_TIME_EPSILON
                        ):
                            break
                    event_dropped_states.extend(reversed(removed))
                    if parent_sequence_id not in dropped_now:
                        dropped_now.append(parent_sequence_id)
                    if parent_sequence_id not in complete_parent_ids:
                        suppressed_parent_ids.add(parent_sequence_id)
            elif strategy == "oldest_frame_first":
                while (
                    _freshness_queue_depth(states, frame.arrival_seconds)
                    > hard_cap_seconds + _FRESHNESS_TIME_EPSILON
                ):
                    victim = next(
                        (
                            state
                            for state in states
                            if state.start_seconds
                            > (
                                frame.arrival_seconds
                                + cancellation_guard_seconds
                                + _FRESHNESS_TIME_EPSILON
                            )
                        ),
                        None,
                    )
                    if victim is None:
                        break
                    states.remove(victim)
                    event_dropped_states.append(victim)
                    parent_sequence_id = victim.frame.parent_sequence_id
                    if parent_sequence_id not in dropped_now:
                        dropped_now.append(parent_sequence_id)
                    _reschedule_freshness_future(
                        states,
                        at_seconds=frame.arrival_seconds,
                    )
            elif strategy == "oldest_first":
                for parent_sequence_id in eligible:
                    removed = _remove_parent_states(
                        states, parent_sequence_id
                    )
                    if not removed:
                        continue
                    event_dropped_states.extend(removed)
                    dropped_now.append(parent_sequence_id)
                    _reschedule_freshness_future(
                        states,
                        at_seconds=frame.arrival_seconds,
                    )
                    if (
                        _freshness_queue_depth(
                            states, frame.arrival_seconds
                        )
                        <= hard_cap_seconds + _FRESHNESS_TIME_EPSILON
                    ):
                        break
            else:
                for parent_sequence_id in eligible[:-1]:
                    removed = _remove_parent_states(
                        states, parent_sequence_id
                    )
                    if not removed:
                        continue
                    event_dropped_states.extend(removed)
                    dropped_now.append(parent_sequence_id)
                if dropped_now:
                    _reschedule_freshness_future(
                        states,
                        at_seconds=frame.arrival_seconds,
                    )

        queue_after = _freshness_queue_depth(states, frame.arrival_seconds)
        if dropped_now:
            dropped_states.extend(event_dropped_states)
            evictions.append(
                FreshnessEviction(
                    at_seconds=frame.arrival_seconds,
                    trigger_source_index=source_index,
                    trigger_parent_sequence_id=frame.parent_sequence_id,
                    trigger_audio_frame_id=frame.audio_frame_id,
                    eligible_parent_sequence_ids=eligible,
                    dropped_parent_sequence_ids=tuple(dropped_now),
                    dropped_frame_count=len(event_dropped_states),
                    dropped_audio_bytes=sum(
                        state.frame.audio_bytes
                        for state in event_dropped_states
                    ),
                    dropped_source_duration_seconds=sum(
                        state.frame.duration_seconds
                        for state in event_dropped_states
                    ),
                    queue_before_eviction_seconds=queue_before,
                    queue_after_eviction_seconds=queue_after,
                )
            )

        residual_over_cap = max(0.0, queue_after - hard_cap_seconds)
        decisions.append(
            FreshnessCapDecision(
                source_index=source_index,
                parent_sequence_id=frame.parent_sequence_id,
                audio_frame_id=frame.audio_frame_id,
                arrival_seconds=frame.arrival_seconds,
                queue_before_eviction_seconds=queue_before,
                queue_after_eviction_seconds=queue_after,
                eligible_parent_sequence_ids=eligible,
                dropped_parent_sequence_ids=tuple(dropped_now),
                residual_hard_cap_breach=(
                    residual_over_cap > _FRESHNESS_TIME_EPSILON
                ),
                residual_over_cap_seconds=residual_over_cap,
            )
        )
        if frame.audio_frame_id + 1 == frame.parent_frame_count:
            suppressed_parent_ids.discard(frame.parent_sequence_id)

    schedule = tuple(_to_freshness_scheduled_frame(state) for state in states)
    dropped_frames = tuple(state.frame for state in dropped_states)
    retained_parent_id_set = {
        frame.parent_sequence_id for frame in schedule
    }
    dropped_parent_id_set = {
        frame.parent_sequence_id for frame in dropped_frames
    }
    fully_dropped_parent_ids = dropped_parent_id_set - retained_parent_id_set
    partially_dropped_parent_ids = (
        dropped_parent_id_set & retained_parent_id_set
    )
    retained_parent_ids = tuple(
        parent_sequence_id
        for parent_sequence_id in parent_order
        if parent_sequence_id in retained_parent_id_set
    )
    ordered_dropped_parent_ids = tuple(
        parent_sequence_id
        for parent_sequence_id in parent_order
        if parent_sequence_id in fully_dropped_parent_ids
    )

    total_source_duration = sum(frame.duration_seconds for frame in frames)
    retained_source_duration = sum(
        frame.source_duration_seconds for frame in schedule
    )
    dropped_source_duration = sum(
        frame.duration_seconds for frame in dropped_frames
    )
    total_audio_bytes = sum(frame.audio_bytes for frame in frames)
    retained_audio_bytes = sum(frame.audio_bytes for frame in schedule)
    dropped_audio_bytes = sum(frame.audio_bytes for frame in dropped_frames)
    playback_end = schedule[-1].end_seconds if schedule else frames[-1].arrival_seconds
    first_arrival = frames[0].arrival_seconds
    playback_window = max(0.0, playback_end - first_arrival)
    queue_depths = [
        decision.queue_after_eviction_seconds for decision in decisions
    ]
    seconds_above_target = _seconds_above_freshness_threshold(
        decisions,
        threshold_seconds=policy.target_queue_seconds,
        playback_end_seconds=playback_end,
    )
    seconds_above_hard_cap = _seconds_above_freshness_threshold(
        decisions,
        threshold_seconds=hard_cap_seconds,
        playback_end_seconds=playback_end,
    )

    residual_breach_events = sum(
        decision.residual_hard_cap_breach for decision in decisions
    )
    residual_breach_entries = 0
    previous_residual_breach = False
    for decision in decisions:
        if (
            decision.residual_hard_cap_breach
            and not previous_residual_breach
        ):
            residual_breach_entries += 1
        previous_residual_breach = decision.residual_hard_cap_breach

    rates = (policy.normal_rate, policy.catch_up_rate, policy.urgent_rate)
    rate_frame_counts = {_rate_label(rate): 0 for rate in rates}
    rate_source_durations = {_rate_label(rate): 0.0 for rate in rates}
    mode_frame_counts = {
        "normal": 0,
        "catch-up": 0,
        "urgent": 0,
        "over-limit": 0,
    }
    accelerated_source_duration = 0.0
    urgent_source_duration = 0.0
    max_continuous_urgent_playback = 0.0
    continuous_urgent_playback = 0.0
    previous_scheduled_end: float | None = None

    for frame in schedule:
        label = _rate_label(frame.playback_rate)
        rate_frame_counts[label] = rate_frame_counts.get(label, 0) + 1
        rate_source_durations[label] = (
            rate_source_durations.get(label, 0.0)
            + frame.source_duration_seconds
        )
        mode_frame_counts[frame.playback_mode] += 1
        if frame.playback_rate > policy.normal_rate:
            accelerated_source_duration += frame.source_duration_seconds
        if frame.playback_rate >= policy.urgent_rate:
            urgent_source_duration += frame.source_duration_seconds
            scheduled_duration = frame.end_seconds - frame.start_seconds
            is_contiguous = (
                previous_scheduled_end is not None
                and math.isclose(
                    frame.start_seconds,
                    previous_scheduled_end,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            )
            continuous_urgent_playback = (
                continuous_urgent_playback + scheduled_duration
                if is_contiguous
                else scheduled_duration
            )
            max_continuous_urgent_playback = max(
                max_continuous_urgent_playback,
                continuous_urgent_playback,
            )
        else:
            continuous_urgent_playback = 0.0
        previous_scheduled_end = frame.end_seconds

    source_duration_by_parent: dict[int, float] = {}
    for frame in frames:
        source_duration_by_parent[frame.parent_sequence_id] = (
            source_duration_by_parent.get(frame.parent_sequence_id, 0.0)
            + frame.duration_seconds
        )
    (
        discontinuity_count,
        max_consecutive_dropped_parents,
        max_dropped_duration_per_discontinuity,
    ) = _dropped_parent_run_metrics(
        parent_order=parent_order,
        dropped_parent_ids=fully_dropped_parent_ids,
        source_duration_by_parent=source_duration_by_parent,
    )

    summary = FreshnessCapSummary(
        strategy=strategy,
        adaptive=adaptive,
        hard_cap_seconds=hard_cap_seconds,
        cancellation_guard_seconds=cancellation_guard_seconds,
        hard_cap_achieved=residual_breach_events == 0,
        frames_received=len(frames),
        frames_retained=len(schedule),
        frames_dropped=len(dropped_frames),
        parents_received=len(parent_order),
        parents_retained=len(retained_parent_ids),
        parents_dropped=len(ordered_dropped_parent_ids),
        parents_partially_dropped=len(partially_dropped_parent_ids),
        retained_parent_sequence_ids=retained_parent_ids,
        dropped_parent_sequence_ids=ordered_dropped_parent_ids,
        total_source_duration_seconds=total_source_duration,
        retained_source_duration_seconds=retained_source_duration,
        dropped_source_duration_seconds=dropped_source_duration,
        retained_source_percent=(
            retained_source_duration / total_source_duration * 100.0
        ),
        dropped_source_percent=(
            dropped_source_duration / total_source_duration * 100.0
        ),
        total_audio_bytes=total_audio_bytes,
        retained_audio_bytes=retained_audio_bytes,
        dropped_audio_bytes=dropped_audio_bytes,
        retained_audio_bytes_percent=(
            retained_audio_bytes / total_audio_bytes * 100.0
            if total_audio_bytes > 0
            else 0.0
        ),
        dropped_audio_bytes_percent=(
            dropped_audio_bytes / total_audio_bytes * 100.0
            if total_audio_bytes > 0
            else 0.0
        ),
        first_arrival_seconds=first_arrival,
        input_end_seconds=input_end_seconds,
        playback_end_seconds=playback_end,
        listener_tail_seconds=max(0.0, playback_end - input_end_seconds),
        total_scheduled_duration_seconds=sum(
            frame.end_seconds - frame.start_seconds for frame in schedule
        ),
        peak_queue_before_eviction_seconds=max(
            decision.queue_before_eviction_seconds
            for decision in decisions
        ),
        peak_queue_depth_seconds=max(queue_depths),
        arrival_queue_p50_seconds=_nearest_rank_percentile(
            queue_depths, 0.50
        ),
        arrival_queue_p95_seconds=_nearest_rank_percentile(
            queue_depths, 0.95
        ),
        time_weighted_queue_p50_seconds=(
            _time_weighted_freshness_queue_percentile(
                decisions,
                playback_window_seconds=playback_window,
                playback_end_seconds=playback_end,
                quantile=0.50,
            )
        ),
        time_weighted_queue_p95_seconds=(
            _time_weighted_freshness_queue_percentile(
                decisions,
                playback_window_seconds=playback_window,
                playback_end_seconds=playback_end,
                quantile=0.95,
            )
        ),
        seconds_above_target=seconds_above_target,
        seconds_above_hard_cap=seconds_above_hard_cap,
        percent_playback_window_above_target=(
            seconds_above_target / playback_window * 100.0
            if playback_window > 0
            else 0.0
        ),
        percent_playback_window_above_hard_cap=(
            seconds_above_hard_cap / playback_window * 100.0
            if playback_window > 0
            else 0.0
        ),
        residual_breach_events=residual_breach_events,
        residual_breach_entries=residual_breach_entries,
        peak_residual_over_cap_seconds=max(
            decision.residual_over_cap_seconds for decision in decisions
        ),
        eviction_event_count=len(evictions),
        discontinuity_count=discontinuity_count,
        max_consecutive_dropped_parents=(
            max_consecutive_dropped_parents
        ),
        max_dropped_source_duration_per_discontinuity_seconds=(
            max_dropped_duration_per_discontinuity
        ),
        accelerated_source_duration_seconds=accelerated_source_duration,
        accelerated_source_percent=(
            accelerated_source_duration / retained_source_duration * 100.0
            if retained_source_duration > 0
            else 0.0
        ),
        urgent_source_duration_seconds=urgent_source_duration,
        urgent_source_percent=(
            urgent_source_duration / retained_source_duration * 100.0
            if retained_source_duration > 0
            else 0.0
        ),
        max_continuous_urgent_playback_seconds=(
            max_continuous_urgent_playback
        ),
        rate_frame_counts=rate_frame_counts,
        rate_source_duration_seconds=rate_source_durations,
        mode_frame_counts=mode_frame_counts,
    )
    return FreshnessCapSimulation(
        schedule=schedule,
        decisions=tuple(decisions),
        evictions=tuple(evictions),
        dropped_frames=dropped_frames,
        summary=summary,
    )
