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
