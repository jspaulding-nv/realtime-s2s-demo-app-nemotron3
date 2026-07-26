#!/usr/bin/env python3
"""Attribute schema-3 listener queue growth to privacy-safe stage evidence.

The report deliberately contains numeric aggregates only.  Source text,
``text_chars``, paths, filenames, endpoints, URLs, session identifiers, raw
events, and audio are never copied into the analysis payload.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from analyze_streaming_latency import load_summary_latency
from freshness_trace import ParentFreshnessTrace, load_parent_freshness_trace
from playback_simulation import PlaybackSimulation, simulate_playback


ANALYSIS_SCHEMA_VERSION = 1
DEFAULT_WINDOW_SECONDS = 30
DEFAULT_TOP_WINDOWS = 5
ACCOUNTING_TOLERANCE_SECONDS = 0.010
STAGE_METRIC_KEYS = (
    "source_end_to_asr_final_seconds",
    "source_end_to_latest_contributing_asr_final_seconds",
    "asr_final_to_segment_seconds",
    "source_end_to_segment_seconds",
    "nmt_queue_seconds",
    "segment_to_nmt_enqueue_seconds",
    "nmt_processing_seconds",
    "nmt_complete_to_tts_enqueue_seconds",
    "tts_queue_seconds",
    "tts_start_to_first_audio_seconds",
    "tts_start_to_first_output_enqueue_seconds",
    "tts_processing_seconds",
    "tts_first_to_final_frame_seconds",
    "output_queue_seconds",
    "output_dequeue_to_websocket_seconds",
    "source_end_to_browser_first_frame_seconds",
    "source_end_to_scheduled_start_seconds",
    "tts_frame_to_output_enqueue_seconds",
    "tts_audio_seconds",
    "audio_frame_count",
    "nmt_retry_count",
    "tts_retry_count",
    "atomic_fallback_count",
    "tts_synthesis_realtime_factor",
    "server_client_interframe_gap_delta_seconds",
    "parent_immediate_queue_change_seconds",
    "parent_positive_queue_increase_seconds",
    "client_first_to_final_frame_seconds",
)
BLOCKED_PUT_KEYS = ("nmt", "tts", "output")


@dataclass(frozen=True)
class _ParentStage:
    """Numeric server-stage observations for one parent."""

    source_end_seconds: float
    source_end_to_latest_asr_final_seconds: float
    asr_final_to_segment_seconds: float
    segment_to_nmt_enqueue_seconds: float
    nmt_queue_seconds: float
    nmt_processing_seconds: float
    nmt_complete_to_tts_enqueue_seconds: float
    tts_queue_seconds: float
    tts_start_to_first_seconds: float
    tts_processing_seconds: float
    tts_first_to_final_frame_seconds: float
    tts_audio_seconds: float
    audio_frame_count: int
    nmt_blocked_put_seconds: float
    tts_blocked_put_seconds: float
    nmt_retry_count: int
    tts_retry_count: int
    atomic_fallback_count: int


@dataclass(frozen=True)
class _ParentBurst:
    """Numeric browser-arrival observations for one parent."""

    first_arrival_seconds: float
    last_arrival_seconds: float
    audio_seconds: float
    immediate_queue_change_seconds: float
    source_to_first_frame_seconds: float
    source_to_scheduled_start_seconds: float


@dataclass(frozen=True)
class StageBurstSample:
    """Validated private-in-memory input for one neutral sample."""

    sample_index: int
    input_end_seconds: float
    stage_values: dict[str, tuple[float, ...]]
    blocked_put_values: dict[str, tuple[float, ...]]
    parent_stages: tuple[_ParentStage, ...]
    parent_bursts: tuple[_ParentBurst, ...]
    trace: ParentFreshnessTrace
    playback: PlaybackSimulation


def _finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{field} must be a finite number")
    parsed = float(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return parsed


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise ValueError(f"{field} must be an integer at least {minimum}")
    return value


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _inferred_summary_path(csv_path: Path) -> Path:
    suffix = "_results.csv"
    if not csv_path.name.endswith(suffix):
        raise ValueError("input CSV name must end in _results.csv")
    return csv_path.with_name(
        csv_path.name.removesuffix(suffix) + "_summary.json"
    )


def _event_key(event: dict[str, Any]) -> tuple[str, str]:
    stage = event.get("stage")
    name = event.get("event")
    if not isinstance(stage, str) or not isinstance(name, str):
        raise ValueError("every telemetry event must have stage and event names")
    return stage, name


def _sequence_id(event: dict[str, Any], field: str) -> int:
    return _integer(event.get("sequence_id"), f"{field}.sequence_id")


def _frame_key(event: dict[str, Any], field: str) -> tuple[int, int]:
    parent_id = _integer(
        event.get("parent_sequence_id"),
        f"{field}.parent_sequence_id",
    )
    sequence_id = _sequence_id(event, field)
    if parent_id != sequence_id:
        raise ValueError(f"{field} parent and sequence identifiers disagree")
    return parent_id, _integer(
        event.get("audio_frame_id"),
        f"{field}.audio_frame_id",
    )


def _unique_by_sequence(
    events: Sequence[dict[str, Any]],
    stage: str,
    name: str,
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for index, event in enumerate(events):
        if _event_key(event) != (stage, name):
            continue
        sequence_id = _sequence_id(event, f"event[{index}]")
        if sequence_id in result:
            raise ValueError(f"duplicate {stage}/{name} sequence identity")
        result[sequence_id] = event
    return result


def _unique_by_frame(
    events: Sequence[dict[str, Any]],
    stage: str,
    name: str,
) -> dict[tuple[int, int], dict[str, Any]]:
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for index, event in enumerate(events):
        if _event_key(event) != (stage, name):
            continue
        key = _frame_key(event, f"event[{index}]")
        if key in result:
            raise ValueError(f"duplicate {stage}/{name} frame identity")
        result[key] = event
    return result


def _elapsed_seconds(event: dict[str, Any], pipeline_start_ms: float) -> float:
    elapsed = (
        _finite(event.get("monotonic_ms"), "event.monotonic_ms", minimum=0)
        - pipeline_start_ms
    ) / 1000.0
    if elapsed < 0:
        raise ValueError("telemetry event precedes pipeline start")
    return elapsed


def _duration_field_seconds(event: dict[str, Any], field: str) -> float:
    return _finite(event.get(field), f"event.{field}", minimum=0) / 1000.0


def _require_monotonic(values: Sequence[float], field: str) -> None:
    if any(later < earlier for earlier, later in zip(values, values[1:])):
        raise ValueError(f"{field} timestamps are not monotonic")


def _require_queue_accounting(
    pairs: Sequence[tuple[float, float]],
) -> None:
    if any(
        not math.isclose(
            observed,
            reported,
            rel_tol=0.0,
            abs_tol=ACCOUNTING_TOLERANCE_SECONDS,
        )
        for observed, reported in pairs
    ):
        raise ValueError("reported queue residence disagrees with timestamps")


def _require_processing_envelopes(
    pairs: Sequence[tuple[float, float]],
) -> None:
    if any(
        reported > enclosing + ACCOUNTING_TOLERANCE_SECONDS
        for enclosing, reported in pairs
    ):
        raise ValueError(
            "reported processing duration exceeds its timestamp envelope"
        )


def _require_contiguous(mapping: dict[int, Any], field: str) -> None:
    if sorted(mapping) != list(range(len(mapping))):
        raise ValueError(f"{field} identities must be contiguous from zero")


def _validate_schema3_mode(root: dict[str, Any]) -> dict[str, Any]:
    if root.get("pipeline_mode") != "staged":
        raise ValueError("stage/burst attribution requires staged mode")
    staged = _object(root.get("staged_pipeline"), "staged_pipeline")
    config = _object(root.get("backend_config"), "backend_config")
    staged_config = _object(config.get("stagedConfig"), "stagedConfig")
    if staged.get("telemetry_schema_version") != 3:
        raise ValueError("telemetry schema version must be 3")
    if staged_config.get("telemetrySchemaVersion") != 3:
        raise ValueError("backend telemetry schema version must be 3")
    if staged.get("tts_incremental_publish_enabled") is not True:
        raise ValueError("incremental TTS publication must be enabled")
    if staged_config.get("ttsIncrementalPublishEnabled") is not True:
        raise ValueError("backend incremental TTS publication must be enabled")
    if staged.get("tts_subsegmentation_enabled") is not False:
        raise ValueError("TTS subsegmentation must be disabled")
    if staged_config.get("ttsSubsegmentMaxChars") != 0:
        raise ValueError("backend TTS subsegmentation must be disabled")
    if (
        staged.get("state") != "closed"
        or staged.get("outcome") != "complete"
        or staged.get("failure") is not None
    ):
        raise ValueError("capture must be closed and complete without failure")
    return staged


def _queue_before(
    playback: PlaybackSimulation,
    at_seconds: float,
) -> float:
    """Return the causal queue just before arrivals at ``at_seconds``."""

    end_seconds = 0.0
    for item in playback.schedule:
        if item.arrival_seconds >= at_seconds:
            break
        end_seconds = item.end_seconds
    return max(0.0, end_seconds - at_seconds)


def _parent_bursts(
    trace: ParentFreshnessTrace,
    playback: PlaybackSimulation,
) -> tuple[_ParentBurst, ...]:
    frames_by_parent: dict[int, list[tuple[Any, Any]]] = {}
    if len(trace.frames) != len(playback.schedule):
        raise ValueError("playback schedule does not cover every trace frame")
    for frame, scheduled in zip(trace.frames, playback.schedule):
        if frame.source_index != scheduled.source_index:
            raise ValueError("trace and playback frame ordering disagree")
        frames_by_parent.setdefault(frame.parent_sequence_id, []).append(
            (frame, scheduled)
        )
    _require_contiguous(frames_by_parent, "parent")
    if trace.input_sample_zero_timestamp_ms is None:
        raise ValueError("schema-3 source-aligned browser clock is unavailable")

    parents: list[_ParentBurst] = []
    for parent_id in range(len(frames_by_parent)):
        pairs = frames_by_parent[parent_id]
        first_frame, first_scheduled = pairs[0]
        last_frame, last_scheduled = pairs[-1]
        if first_frame.source_end_ms is None:
            raise ValueError("parent source boundary is unavailable")
        first_arrival = first_frame.arrival_seconds
        queue_before = _queue_before(playback, first_arrival)
        source_end_client_seconds = (
            trace.input_sample_zero_timestamp_ms + first_frame.source_end_ms
        ) / 1000.0
        parents.append(
            _ParentBurst(
                first_arrival_seconds=first_arrival,
                last_arrival_seconds=last_frame.arrival_seconds,
                audio_seconds=sum(frame.duration_seconds for frame, _ in pairs),
                immediate_queue_change_seconds=(
                    last_scheduled.queue_depth_seconds - queue_before
                ),
                source_to_first_frame_seconds=(
                    first_arrival - source_end_client_seconds
                ),
                source_to_scheduled_start_seconds=(
                    first_scheduled.start_seconds - source_end_client_seconds
                ),
            )
        )
    return tuple(parents)


def load_stage_burst_sample(
    csv_path: Path,
    *,
    sample_index: int,
) -> StageBurstSample:
    """Strictly load one schema-3 CSV/adjacent-summary capture pair."""

    if (
        not isinstance(sample_index, int)
        or isinstance(sample_index, bool)
        or sample_index <= 0
    ):
        raise ValueError("sample_index must be a positive integer")
    csv_path = Path(csv_path)
    summary_path = _inferred_summary_path(csv_path)

    # These existing loaders provide the capture integrity, frame, clock, and
    # staged-latency invariants.  Their path/hash fields remain private here.
    trace = load_parent_freshness_trace(csv_path, summary_path)
    latency = load_summary_latency(summary_path, sample_index=sample_index)
    if latency.telemetry_schema_version != 3:
        raise ValueError("latency evidence is not telemetry schema 3")

    try:
        with summary_path.open(encoding="utf-8") as handle:
            root = _object(json.load(handle), "summary")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("summary could not be read as JSON") from exc
    staged = _validate_schema3_mode(root)
    raw_events = _list(staged.get("events"), "staged_pipeline.events")
    events = tuple(
        _object(event, f"staged_pipeline.events[{index}]")
        for index, event in enumerate(raw_events)
    )
    starts = [
        event
        for event in events
        if _event_key(event) == ("pipeline", "started")
    ]
    if len(starts) != 1:
        raise ValueError("exactly one pipeline start event is required")
    pipeline_start_ms = _finite(
        starts[0].get("monotonic_ms"),
        "pipeline start monotonic_ms",
        minimum=0,
    )

    # Schema 3 is parent-atomic.  Any subsequence identity is rejected even if
    # an upstream validator later becomes more permissive.
    for event in events:
        if (
            event.get("subsequence_id") is not None
            or event.get("subsequence_count") is not None
        ):
            raise ValueError("schema-3 telemetry cannot contain subsequences")

    segment = _unique_by_sequence(events, "segmenter", "emitted")
    nmt_enqueued = _unique_by_sequence(events, "nmt", "enqueued")
    nmt_started = _unique_by_sequence(events, "nmt", "started")
    nmt_completed = _unique_by_sequence(events, "nmt", "completed")
    tts_enqueued = _unique_by_sequence(events, "tts", "enqueued")
    tts_started = _unique_by_sequence(events, "tts", "started")
    tts_first = _unique_by_sequence(events, "tts", "first_audio")
    tts_completed = _unique_by_sequence(events, "tts", "completed")
    layers = (
        segment,
        nmt_enqueued,
        nmt_started,
        nmt_completed,
        tts_enqueued,
        tts_started,
        tts_first,
        tts_completed,
    )
    for layer in layers:
        _require_contiguous(layer, "stage")
    if len({len(layer) for layer in layers}) != 1:
        raise ValueError("parent stage layers do not have identical coverage")

    output_enqueued = _unique_by_frame(events, "output", "frame_enqueued")
    output_dequeued = _unique_by_frame(events, "output", "frame_dequeued")
    tts_frames = _unique_by_frame(events, "tts", "frame_received")
    if output_enqueued.keys() != output_dequeued.keys():
        raise ValueError("output enqueue/dequeue frame coverage disagrees")
    if output_enqueued.keys() != tts_frames.keys():
        raise ValueError("TTS/output frame coverage disagrees")

    sends: dict[tuple[int, int], float] = {}
    for index, raw_send in enumerate(
        _list(staged.get("websocket_send_events"), "websocket_send_events")
    ):
        send = _object(raw_send, f"websocket_send_events[{index}]")
        key = (
            _integer(send.get("parent_sequence_id"), "send.parent_sequence_id"),
            _integer(send.get("audio_frame_id"), "send.audio_frame_id"),
        )
        if _integer(send.get("sequence_id"), "send.sequence_id") != key[0]:
            raise ValueError("WebSocket parent and sequence identifiers disagree")
        if key in sends:
            raise ValueError("duplicate WebSocket frame identity")
        sends[key] = _finite(
            send.get("sent_monotonic_ms"),
            "send.sent_monotonic_ms",
            minimum=0,
        )
    if sends.keys() != output_dequeued.keys():
        raise ValueError("output/WebSocket frame coverage disagrees")

    asr_by_id: dict[int, dict[str, Any]] = {}
    for index, event in enumerate(events):
        if _event_key(event) != ("asr", "final"):
            continue
        final_id = _integer(event.get("asr_final_id"), f"event[{index}].asr_final_id")
        if final_id in asr_by_id:
            raise ValueError("duplicate ASR final identity")
        asr_by_id[final_id] = event
    _require_contiguous(asr_by_id, "ASR final")

    parent_stages: list[_ParentStage] = []
    asr_to_segment: list[float] = []
    frame_received_to_output: list[float] = []
    tts_start_to_first_output: list[float] = []
    output_queue: list[float] = []
    output_to_websocket: list[float] = []
    tts_frame_keys_by_parent: dict[int, list[tuple[int, int]]] = {}
    for key in sorted(tts_frames):
        tts_frame_keys_by_parent.setdefault(key[0], []).append(key)
    for sequence_id in range(len(segment)):
        segment_event = segment[sequence_id]
        final_ids_raw = _list(
            segment_event.get("contributing_final_ids"),
            "segment.contributing_final_ids",
        )
        final_ids = [
            _integer(value, "segment.contributing_final_ids")
            for value in final_ids_raw
        ]
        if not final_ids or len(final_ids) != len(set(final_ids)):
            raise ValueError("segment ASR attribution must be non-empty and unique")
        try:
            contributing = [asr_by_id[value] for value in final_ids]
        except KeyError as exc:
            raise ValueError("segment references an unknown ASR final") from exc
        segment_elapsed = _elapsed_seconds(segment_event, pipeline_start_ms)
        latest_final_elapsed = max(
            _elapsed_seconds(item, pipeline_start_ms) for item in contributing
        )
        asr_segment_seconds = segment_elapsed - latest_final_elapsed
        if asr_segment_seconds < 0:
            raise ValueError("segment emission precedes contributing ASR final")
        asr_to_segment.append(asr_segment_seconds)
        source_end_seconds = _finite(
            segment_event.get("source_end_ms"),
            "segment.source_end_ms",
            minimum=0,
        ) / 1000.0
        source_end_to_latest_final = latest_final_elapsed - source_end_seconds
        if (
            source_end_to_latest_final
            < -(latency.harness_frame_duration_ms / 1000.0)
        ):
            raise ValueError(
                "latest contributing ASR final is earlier than its "
                "source-boundary tolerance"
            )
        frame_keys = tts_frame_keys_by_parent.get(sequence_id, [])
        frame_count = _integer(
            tts_completed[sequence_id].get("audio_frame_count"),
            "tts completed audio_frame_count",
            minimum=1,
        )
        if len(frame_keys) != frame_count:
            raise ValueError("TTS completed frame count disagrees with frames")
        tts_frame_times_ms = [
            _finite(
                tts_frames[key].get("monotonic_ms"),
                "TTS frame monotonic_ms",
                minimum=0,
            )
            for key in frame_keys
        ]
        _require_monotonic(tts_frame_times_ms, "TTS frame")
        first_frame_ms = tts_frame_times_ms[0]
        final_frame_ms = tts_frame_times_ms[-1]
        nmt_enqueue_elapsed = _elapsed_seconds(
            nmt_enqueued[sequence_id],
            pipeline_start_ms,
        )
        nmt_started_elapsed = _elapsed_seconds(
            nmt_started[sequence_id],
            pipeline_start_ms,
        )
        nmt_completed_elapsed = _elapsed_seconds(
            nmt_completed[sequence_id],
            pipeline_start_ms,
        )
        tts_enqueue_elapsed = _elapsed_seconds(
            tts_enqueued[sequence_id],
            pipeline_start_ms,
        )
        tts_started_elapsed = _elapsed_seconds(
            tts_started[sequence_id],
            pipeline_start_ms,
        )
        tts_first_elapsed = _elapsed_seconds(
            tts_first[sequence_id],
            pipeline_start_ms,
        )
        tts_completed_elapsed = _elapsed_seconds(
            tts_completed[sequence_id],
            pipeline_start_ms,
        )
        timeline = (
            segment_elapsed,
            nmt_enqueue_elapsed,
            nmt_started_elapsed,
            nmt_completed_elapsed,
            tts_enqueue_elapsed,
            tts_started_elapsed,
            tts_first_elapsed,
            tts_completed_elapsed,
        )
        _require_monotonic(timeline, "parent stage")
        first_frame_elapsed = (first_frame_ms - pipeline_start_ms) / 1000.0
        final_frame_elapsed = (final_frame_ms - pipeline_start_ms) / 1000.0
        if (
            first_frame_elapsed < tts_first_elapsed
            or final_frame_elapsed
            > tts_completed_elapsed + ACCOUNTING_TOLERANCE_SECONDS
        ):
            raise ValueError("TTS frame timestamps fall outside TTS response")

        nmt_queue_seconds = _duration_field_seconds(
            nmt_started[sequence_id],
            "queue_residence_ms",
        )
        nmt_processing_seconds = _duration_field_seconds(
            nmt_completed[sequence_id],
            "processing_duration_ms",
        )
        tts_queue_seconds = _duration_field_seconds(
            tts_started[sequence_id],
            "queue_residence_ms",
        )
        tts_start_to_first_seconds = _duration_field_seconds(
            tts_first[sequence_id],
            "processing_duration_ms",
        )
        tts_processing_seconds = _duration_field_seconds(
            tts_completed[sequence_id],
            "processing_duration_ms",
        )
        queue_accounting_pairs = (
            (nmt_started_elapsed - nmt_enqueue_elapsed, nmt_queue_seconds),
            (tts_started_elapsed - tts_enqueue_elapsed, tts_queue_seconds),
        )
        _require_queue_accounting(queue_accounting_pairs)
        processing_envelopes = (
            (
                nmt_completed_elapsed - nmt_started_elapsed,
                nmt_processing_seconds,
            ),
            (
                tts_first_elapsed - tts_started_elapsed,
                tts_start_to_first_seconds,
            ),
            (
                tts_completed_elapsed - tts_started_elapsed,
                tts_processing_seconds,
            ),
        )
        _require_processing_envelopes(processing_envelopes)
        nmt_blocked_put_seconds = _duration_field_seconds(
            nmt_enqueued[sequence_id],
            "blocked_put_ms",
        )
        tts_blocked_put_seconds = _duration_field_seconds(
            tts_enqueued[sequence_id],
            "blocked_put_ms",
        )
        if (
            nmt_blocked_put_seconds
            > nmt_enqueue_elapsed
            - segment_elapsed
            + ACCOUNTING_TOLERANCE_SECONDS
            or tts_blocked_put_seconds
            > tts_enqueue_elapsed
            - nmt_completed_elapsed
            + ACCOUNTING_TOLERANCE_SECONDS
        ):
            raise ValueError(
                "reported blocked-put duration exceeds its timestamp envelope"
            )
        fallback = tts_completed[sequence_id].get("atomic_fallback_applied")
        if not isinstance(fallback, bool):
            raise ValueError("atomic fallback flag must be boolean")
        nmt_retry_count = _integer(
            nmt_completed[sequence_id].get("retry_count"),
            "nmt completed retry_count",
        )
        tts_retry_count = _integer(
            tts_completed[sequence_id].get("retry_count"),
            "tts completed retry_count",
        )
        if nmt_retry_count not in {0, 1} or tts_retry_count not in {0, 1}:
            raise ValueError("stage retry count must be zero or one")
        parent_stages.append(
            _ParentStage(
                source_end_seconds=source_end_seconds,
                source_end_to_latest_asr_final_seconds=(
                    source_end_to_latest_final
                ),
                asr_final_to_segment_seconds=asr_segment_seconds,
                segment_to_nmt_enqueue_seconds=(
                    nmt_enqueue_elapsed - segment_elapsed
                ),
                nmt_queue_seconds=nmt_queue_seconds,
                nmt_processing_seconds=nmt_processing_seconds,
                nmt_complete_to_tts_enqueue_seconds=(
                    tts_enqueue_elapsed - nmt_completed_elapsed
                ),
                tts_queue_seconds=tts_queue_seconds,
                tts_start_to_first_seconds=tts_start_to_first_seconds,
                tts_processing_seconds=tts_processing_seconds,
                tts_first_to_final_frame_seconds=(
                    final_frame_ms - first_frame_ms
                )
                / 1000.0,
                tts_audio_seconds=_duration_field_seconds(
                    tts_completed[sequence_id], "audio_duration_ms"
                ),
                audio_frame_count=frame_count,
                nmt_blocked_put_seconds=nmt_blocked_put_seconds,
                tts_blocked_put_seconds=tts_blocked_put_seconds,
                nmt_retry_count=nmt_retry_count,
                tts_retry_count=tts_retry_count,
                atomic_fallback_count=int(fallback),
            )
        )

    for key in sorted(output_enqueued):
        received_ms = _finite(
            tts_frames[key].get("monotonic_ms"),
            "tts frame monotonic_ms",
            minimum=0,
        )
        enqueued_ms = _finite(
            output_enqueued[key].get("monotonic_ms"),
            "output enqueue monotonic_ms",
            minimum=0,
        )
        dequeued_ms = _finite(
            output_dequeued[key].get("monotonic_ms"),
            "output dequeue monotonic_ms",
            minimum=0,
        )
        if enqueued_ms < received_ms or dequeued_ms < enqueued_ms:
            raise ValueError("frame stage timestamps are not monotonic")
        if sends[key] < dequeued_ms:
            raise ValueError("WebSocket send precedes output dequeue")
        frame_handoff_seconds = (enqueued_ms - received_ms) / 1000.0
        frame_received_to_output.append(frame_handoff_seconds)
        output_queue_seconds = _duration_field_seconds(
            output_dequeued[key],
            "queue_residence_ms",
        )
        if not math.isclose(
            (dequeued_ms - enqueued_ms) / 1000.0,
            output_queue_seconds,
            rel_tol=0.0,
            abs_tol=ACCOUNTING_TOLERANCE_SECONDS,
        ):
            raise ValueError(
                "reported output queue residence disagrees with timestamps"
            )
        output_blocked_seconds = _duration_field_seconds(
            output_enqueued[key],
            "blocked_put_ms",
        )
        if (
            output_blocked_seconds
            > frame_handoff_seconds + ACCOUNTING_TOLERANCE_SECONDS
        ):
            raise ValueError(
                "output blocked-put duration exceeds its timestamp envelope"
            )
        output_queue.append(output_queue_seconds)
        output_to_websocket.append((sends[key] - dequeued_ms) / 1000.0)
        if key[1] == 0:
            tts_start_ms = _finite(
                tts_started[key[0]].get("monotonic_ms"),
                "tts start monotonic_ms",
                minimum=0,
            )
            if enqueued_ms < tts_start_ms:
                raise ValueError("first output frame precedes TTS start")
            tts_start_to_first_output.append(
                (enqueued_ms - tts_start_ms) / 1000.0
            )

    playback = simulate_playback(
        trace.frames,
        input_end_seconds=trace.input_end_seconds,
        adaptive=True,
    )
    bursts = _parent_bursts(trace, playback)
    if len(bursts) != len(parent_stages):
        raise ValueError("server parents and browser parents disagree")

    server_client_gap_delta: list[float] = []
    for parent_id in range(len(parent_stages)):
        keys = sorted(key for key in sends if key[0] == parent_id)
        if len(keys) < 2:
            continue
        trace_frames = [
            frame for frame in trace.frames if frame.parent_sequence_id == parent_id
        ]
        for index in range(1, len(keys)):
            server_gap = (sends[keys[index]] - sends[keys[index - 1]]) / 1000.0
            client_gap = (
                trace_frames[index].arrival_seconds
                - trace_frames[index - 1].arrival_seconds
            )
            server_client_gap_delta.append(abs(client_gap - server_gap))

    stage_values = {
        "source_end_to_asr_final_seconds": tuple(
            item.boundary_to_event_seconds for item in latency.asr_finals
        ),
        "source_end_to_latest_contributing_asr_final_seconds": tuple(
            item.source_end_to_latest_asr_final_seconds
            for item in parent_stages
        ),
        "asr_final_to_segment_seconds": tuple(asr_to_segment),
        "source_end_to_segment_seconds": tuple(
            item.boundary_to_event_seconds for item in latency.segments
        ),
        "nmt_queue_seconds": tuple(item.nmt_queue_seconds for item in parent_stages),
        "segment_to_nmt_enqueue_seconds": tuple(
            item.segment_to_nmt_enqueue_seconds for item in parent_stages
        ),
        "nmt_processing_seconds": tuple(
            item.nmt_processing_seconds for item in parent_stages
        ),
        "nmt_complete_to_tts_enqueue_seconds": tuple(
            item.nmt_complete_to_tts_enqueue_seconds for item in parent_stages
        ),
        "tts_queue_seconds": tuple(item.tts_queue_seconds for item in parent_stages),
        "tts_start_to_first_audio_seconds": tuple(
            item.tts_start_to_first_seconds for item in parent_stages
        ),
        "tts_start_to_first_output_enqueue_seconds": tuple(
            tts_start_to_first_output
        ),
        "tts_processing_seconds": tuple(
            item.tts_processing_seconds for item in parent_stages
        ),
        "tts_first_to_final_frame_seconds": tuple(
            item.tts_first_to_final_frame_seconds for item in parent_stages
        ),
        "output_queue_seconds": tuple(output_queue),
        "output_dequeue_to_websocket_seconds": tuple(output_to_websocket),
        "source_end_to_browser_first_frame_seconds": tuple(
            item.source_to_first_frame_seconds for item in bursts
        ),
        "source_end_to_scheduled_start_seconds": tuple(
            item.source_to_scheduled_start_seconds for item in bursts
        ),
        "tts_frame_to_output_enqueue_seconds": tuple(frame_received_to_output),
        "tts_audio_seconds": tuple(item.tts_audio_seconds for item in parent_stages),
        "audio_frame_count": tuple(
            float(item.audio_frame_count) for item in parent_stages
        ),
        "nmt_retry_count": tuple(
            float(item.nmt_retry_count) for item in parent_stages
        ),
        "tts_retry_count": tuple(
            float(item.tts_retry_count) for item in parent_stages
        ),
        "atomic_fallback_count": tuple(
            float(item.atomic_fallback_count) for item in parent_stages
        ),
        "tts_synthesis_realtime_factor": tuple(
            item.tts_processing_seconds / item.tts_audio_seconds
            for item in parent_stages
            if item.tts_audio_seconds > 0
        ),
        "server_client_interframe_gap_delta_seconds": tuple(
            server_client_gap_delta
        ),
        "parent_immediate_queue_change_seconds": tuple(
            item.immediate_queue_change_seconds for item in bursts
        ),
        "parent_positive_queue_increase_seconds": tuple(
            max(0.0, item.immediate_queue_change_seconds) for item in bursts
        ),
        "client_first_to_final_frame_seconds": tuple(
            item.last_arrival_seconds - item.first_arrival_seconds
            for item in bursts
        ),
    }
    blocked_values: dict[str, tuple[float, ...]] = {}
    for stage in ("nmt", "tts", "output"):
        blocked_values[stage] = tuple(
            _duration_field_seconds(event, "blocked_put_ms")
            for event in events
            if event.get("stage") == stage
            and _finite(
                event.get("blocked_put_ms"),
                "event.blocked_put_ms",
                minimum=0,
            )
            > 0
        )

    return StageBurstSample(
        sample_index=sample_index,
        input_end_seconds=trace.input_end_seconds,
        stage_values=stage_values,
        blocked_put_values=blocked_values,
        parent_stages=tuple(parent_stages),
        parent_bursts=bursts,
        trace=trace,
        playback=playback,
    )


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _distribution(values: Iterable[float]) -> dict[str, int | float]:
    normalized = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in normalized):
        raise ValueError("distribution values must be finite")
    if not normalized:
        return {
            "observation_count": 0,
            "min": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "cumulative": 0.0,
        }
    total = sum(normalized)
    return {
        "observation_count": len(normalized),
        "min": min(normalized),
        "p50": _nearest_rank(normalized, 0.50),
        "p95": _nearest_rank(normalized, 0.95),
        "max": max(normalized),
        "mean": total / len(normalized),
        "cumulative": total,
    }


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = ((cursor + 1) + end) / 2.0
        for index in order[cursor:end]:
            ranks[index] = rank
        cursor = end
    return ranks


def _spearman(
    left: Sequence[float],
    right: Sequence[float],
) -> dict[str, int | float]:
    if len(left) != len(right):
        raise ValueError("Spearman inputs must have equal length")
    count = len(left)
    if count < 2:
        return {"observation_count": count, "defined": 0, "coefficient": 0.0}
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = sum(left_ranks) / count
    right_mean = sum(right_ranks) / count
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left_ranks, right_ranks)
    )
    left_scale = sum((value - left_mean) ** 2 for value in left_ranks)
    right_scale = sum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_scale * right_scale)
    if denominator == 0:
        return {"observation_count": count, "defined": 0, "coefficient": 0.0}
    return {
        "observation_count": count,
        "defined": 1,
        "coefficient": numerator / denominator,
    }


def _window_records(
    sample: StageBurstSample,
    window_seconds: int,
    *,
    stride_seconds: int,
) -> list[dict[str, int | float]]:
    final_arrival_seconds = sample.trace.frames[-1].arrival_seconds
    if stride_seconds == window_seconds:
        # Fixed-grid windows partition every observed frame, including the
        # final full grid bucket whose observed-arrival portion may be partial.
        max_start = (
            math.floor(final_arrival_seconds / window_seconds)
            * window_seconds
        )
    else:
        final_second = math.ceil(
            max(sample.input_end_seconds, final_arrival_seconds)
        )
        # Rolling statistics retain only complete observation windows.
        max_start = max(0, final_second - window_seconds)
    records: list[dict[str, int | float]] = []
    frames = sample.trace.frames
    for start in range(0, max_start + 1, stride_seconds):
        end = start + window_seconds
        queue_start = _queue_before(sample.playback, float(start))
        queue_end = _queue_before(sample.playback, float(end))
        frame_indices = [
            index
            for index, frame in enumerate(frames)
            if start <= frame.arrival_seconds < end
        ]
        parent_ids = sorted(
            {frames[index].parent_sequence_id for index in frame_indices}
        )
        parents = [
            (sample.parent_stages[parent_id], sample.parent_bursts[parent_id])
            for parent_id in parent_ids
        ]
        media_seconds = sum(
            frames[index].duration_seconds for index in frame_indices
        )
        scheduled_workload_seconds = sum(
            sample.playback.schedule[index].end_seconds
            - sample.playback.schedule[index].start_seconds
            for index in frame_indices
        )
        queue_peak = max(
            [queue_start]
            + [
                sample.playback.schedule[index].queue_depth_seconds
                for index in frame_indices
            ]
        )
        preceding_gaps = [
            (
                frames[index].arrival_seconds
                - frames[index - 1].arrival_seconds
                if index > 0
                else 0.0
            )
            for index in frame_indices
        ]
        records.append(
            {
                "start_seconds": start,
                "end_seconds": end,
                "queue_start_seconds": queue_start,
                "queue_end_seconds": queue_end,
                "net_queue_growth_seconds": queue_end - queue_start,
                "arrived_audio_seconds": media_seconds,
                "arrival_realtime_factor": media_seconds / window_seconds,
                "arrived_scheduled_workload_seconds": (
                    scheduled_workload_seconds
                ),
                "scheduled_workload_realtime_factor": (
                    scheduled_workload_seconds / window_seconds
                ),
                "frame_count": len(frame_indices),
                "unique_parent_count": len(parents),
                "queue_peak_seconds": queue_peak,
                "max_preceding_client_gap_seconds": max(
                    preceding_gaps,
                    default=0.0,
                ),
                "nmt_queue_seconds": sum(
                    stage.nmt_queue_seconds for stage, _ in parents
                ),
                "nmt_processing_seconds": sum(
                    stage.nmt_processing_seconds for stage, _ in parents
                ),
                "tts_queue_seconds": sum(
                    stage.tts_queue_seconds for stage, _ in parents
                ),
                "tts_processing_seconds": sum(
                    stage.tts_processing_seconds for stage, _ in parents
                ),
                "nmt_blocked_put_seconds": sum(
                    stage.nmt_blocked_put_seconds for stage, _ in parents
                ),
                "tts_blocked_put_seconds": sum(
                    stage.tts_blocked_put_seconds for stage, _ in parents
                ),
            }
        )
    return records


def _top_nonoverlapping(
    windows: Sequence[dict[str, int | float]],
    count: int,
) -> list[dict[str, int | float]]:
    selected: list[dict[str, int | float]] = []
    candidates = sorted(
        (
            window
            for window in windows
            if float(window["net_queue_growth_seconds"]) > 0
        ),
        key=lambda item: (
            -float(item["net_queue_growth_seconds"]),
            int(item["start_seconds"]),
        ),
    )
    for candidate in candidates:
        start = int(candidate["start_seconds"])
        end = int(candidate["end_seconds"])
        if any(
            not (
                end <= int(existing["start_seconds"])
                or start >= int(existing["end_seconds"])
            )
            for existing in selected
        ):
            continue
        selected.append(dict(candidate))
        if len(selected) == count:
            break
    return selected


def _enrich_top_windows(
    sample: StageBurstSample,
    windows: Sequence[dict[str, int | float]],
    count: int,
) -> list[dict[str, Any]]:
    selected = _top_nonoverlapping(windows, count)
    enriched: list[dict[str, Any]] = []
    for window in selected:
        start = float(window["start_seconds"])
        end = float(window["end_seconds"])
        parent_ids = sorted(
            {
                frame.parent_sequence_id
                for frame in sample.trace.frames
                if start <= frame.arrival_seconds < end
            }
        )
        parents = [sample.parent_stages[parent_id] for parent_id in parent_ids]
        item: dict[str, Any] = dict(window)
        bursts = [sample.parent_bursts[parent_id] for parent_id in parent_ids]
        item["associated_stage_distributions"] = {
            "source_end_to_latest_asr_final_seconds": _distribution(
                parent.source_end_to_latest_asr_final_seconds
                for parent in parents
            ),
            "asr_final_to_segment_seconds": _distribution(
                parent.asr_final_to_segment_seconds for parent in parents
            ),
            "segment_to_nmt_enqueue_seconds": _distribution(
                parent.segment_to_nmt_enqueue_seconds for parent in parents
            ),
            "nmt_queue_seconds": _distribution(
                parent.nmt_queue_seconds for parent in parents
            ),
            "nmt_processing_seconds": _distribution(
                parent.nmt_processing_seconds for parent in parents
            ),
            "nmt_complete_to_tts_enqueue_seconds": _distribution(
                parent.nmt_complete_to_tts_enqueue_seconds
                for parent in parents
            ),
            "tts_queue_seconds": _distribution(
                parent.tts_queue_seconds for parent in parents
            ),
            "tts_start_to_first_audio_seconds": _distribution(
                parent.tts_start_to_first_seconds for parent in parents
            ),
            "tts_processing_seconds": _distribution(
                parent.tts_processing_seconds for parent in parents
            ),
            "tts_first_to_final_frame_seconds": _distribution(
                parent.tts_first_to_final_frame_seconds for parent in parents
            ),
            "tts_audio_seconds": _distribution(
                parent.tts_audio_seconds for parent in parents
            ),
            "audio_frame_count": _distribution(
                float(parent.audio_frame_count) for parent in parents
            ),
            "client_first_to_final_frame_seconds": _distribution(
                burst.last_arrival_seconds - burst.first_arrival_seconds
                for burst in bursts
            ),
            "nmt_blocked_put_seconds": _distribution(
                parent.nmt_blocked_put_seconds for parent in parents
            ),
            "tts_blocked_put_seconds": _distribution(
                parent.tts_blocked_put_seconds for parent in parents
            ),
            "nmt_retry_count": _distribution(
                float(parent.nmt_retry_count) for parent in parents
            ),
            "tts_retry_count": _distribution(
                float(parent.tts_retry_count) for parent in parents
            ),
            "atomic_fallback_count": _distribution(
                float(parent.atomic_fallback_count) for parent in parents
            ),
        }
        enriched.append(item)
    return enriched


def _sample_payload(
    sample: StageBurstSample,
    *,
    window_seconds: int,
    top_window_count: int,
) -> dict[str, Any]:
    sliding_windows = _window_records(
        sample,
        window_seconds,
        stride_seconds=1,
    )
    aligned_windows = _window_records(
        sample,
        window_seconds,
        stride_seconds=window_seconds,
    )
    growth = [
        float(item["net_queue_growth_seconds"]) for item in sliding_windows
    ]
    window_associations = {
        "arrived_audio_seconds": _spearman(
            growth,
            [
                float(item["arrived_audio_seconds"])
                for item in sliding_windows
            ],
        ),
        "arrived_scheduled_workload_seconds": _spearman(
            growth,
            [
                float(item["arrived_scheduled_workload_seconds"])
                for item in sliding_windows
            ],
        ),
        "nmt_queue_seconds": _spearman(
            growth,
            [float(item["nmt_queue_seconds"]) for item in sliding_windows],
        ),
        "nmt_processing_seconds": _spearman(
            growth,
            [
                float(item["nmt_processing_seconds"])
                for item in sliding_windows
            ],
        ),
        "tts_queue_seconds": _spearman(
            growth,
            [float(item["tts_queue_seconds"]) for item in sliding_windows],
        ),
        "tts_processing_seconds": _spearman(
            growth,
            [
                float(item["tts_processing_seconds"])
                for item in sliding_windows
            ],
        ),
        "nmt_blocked_put_seconds": _spearman(
            growth,
            [
                float(item["nmt_blocked_put_seconds"])
                for item in sliding_windows
            ],
        ),
        "tts_blocked_put_seconds": _spearman(
            growth,
            [
                float(item["tts_blocked_put_seconds"])
                for item in sliding_windows
            ],
        ),
    }
    aligned_growth = [
        float(item["net_queue_growth_seconds"]) for item in aligned_windows
    ]
    aligned_associations = {
        "arrived_audio_seconds": _spearman(
            aligned_growth,
            [float(item["arrived_audio_seconds"]) for item in aligned_windows],
        ),
        "arrived_scheduled_workload_seconds": _spearman(
            aligned_growth,
            [
                float(item["arrived_scheduled_workload_seconds"])
                for item in aligned_windows
            ],
        ),
        "nmt_queue_seconds": _spearman(
            aligned_growth,
            [float(item["nmt_queue_seconds"]) for item in aligned_windows],
        ),
        "tts_queue_seconds": _spearman(
            aligned_growth,
            [float(item["tts_queue_seconds"]) for item in aligned_windows],
        ),
    }
    parent_change = [
        item.immediate_queue_change_seconds for item in sample.parent_bursts
    ]
    publisher_handoff = sample.stage_values[
        "tts_frame_to_output_enqueue_seconds"
    ]
    return {
        "sample_index": sample.sample_index,
        "counts": {
            "parents": len(sample.parent_stages),
            "audio_frames": len(sample.trace.frames),
            "sliding_windows": len(sliding_windows),
            "aligned_windows": len(aligned_windows),
        },
        "stages": {
            key: _distribution(values)
            for key, values in sample.stage_values.items()
        },
        "blocked_puts": {
            key: _distribution(values)
            for key, values in sample.blocked_put_values.items()
        },
        "playback": {
            "input_end_seconds": sample.input_end_seconds,
            "listener_tail_seconds": (
                sample.playback.summary.listener_tail_seconds
            ),
            "peak_queue_seconds": (
                sample.playback.summary.peak_queue_depth_seconds
            ),
            "arrival_queue_p95_seconds": (
                sample.playback.summary.arrival_queue_p95_seconds
            ),
        },
        "diagnostics": {
            "tts_frame_to_output_enqueue_over_100ms_count": sum(
                value > 0.100 for value in publisher_handoff
            ),
            "tts_frame_to_output_enqueue_over_1s_count": sum(
                value > 1.0 for value in publisher_handoff
            ),
        },
        "window_seconds": window_seconds,
        "sliding_window_arrival_realtime_factor": _distribution(
            float(item["arrival_realtime_factor"])
            for item in sliding_windows
        ),
        "top_aligned_positive_growth_windows": _enrich_top_windows(
            sample,
            aligned_windows,
            top_window_count,
        ),
        "descriptive_aligned_window_spearman": aligned_associations,
        "descriptive_window_spearman": window_associations,
        "descriptive_parent_spearman": {
            "audio_seconds_to_immediate_queue_change": _spearman(
                [item.audio_seconds for item in sample.parent_bursts],
                parent_change,
            ),
            "source_to_first_frame_to_immediate_queue_change": _spearman(
                [
                    item.source_to_first_frame_seconds
                    for item in sample.parent_bursts
                ],
                parent_change,
            ),
        },
    }


def _round_floats(value: Any) -> Any:
    if isinstance(value, float):
        rounded = round(value, 6)
        return 0.0 if rounded == -0.0 else rounded
    if isinstance(value, list):
        return [_round_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_floats(item) for key, item in value.items()}
    return value


def build_stage_burst_analysis(
    samples: Sequence[StageBurstSample],
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    top_window_count: int = DEFAULT_TOP_WINDOWS,
) -> dict[str, Any]:
    """Build a numeric-only analysis for validated samples."""

    normalized = tuple(samples)
    if not normalized:
        raise ValueError("at least one sample is required")
    if any(not isinstance(item, StageBurstSample) for item in normalized):
        raise ValueError("samples must contain StageBurstSample records")
    if [item.sample_index for item in normalized] != list(
        range(1, len(normalized) + 1)
    ):
        raise ValueError("sample indices must be contiguous from one")
    if (
        not isinstance(window_seconds, int)
        or isinstance(window_seconds, bool)
        or window_seconds <= 0
    ):
        raise ValueError("window_seconds must be a positive integer")
    if (
        not isinstance(top_window_count, int)
        or isinstance(top_window_count, bool)
        or top_window_count <= 0
    ):
        raise ValueError("top_window_count must be a positive integer")

    if any(
        tuple(item.stage_values) != STAGE_METRIC_KEYS
        for item in normalized
    ):
        raise ValueError("sample stage metrics do not match the fixed whitelist")
    if any(
        tuple(item.blocked_put_values) != BLOCKED_PUT_KEYS
        for item in normalized
    ):
        raise ValueError(
            "sample blocked-put metrics do not match the fixed whitelist"
        )

    payload = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "privacy": {
            "numeric_leaf_values_only": 1,
            "contains_text_chars": 0,
            "contains_paths_or_filenames": 0,
            "contains_sessions": 0,
            "contains_urls_or_endpoints": 0,
            "contains_raw_events": 0,
        },
        "configuration": {
            "window_seconds": window_seconds,
            "top_window_count": top_window_count,
            "window_start_quantum_seconds": 1,
            "window_end_exclusive": 1,
            "aligned_window_stride_seconds": window_seconds,
            "sliding_window_stride_seconds": 1,
            "adaptive_playback": 1,
        },
        "aggregate": {
            "counts": {
                "samples": len(normalized),
                "parents": sum(len(item.parent_stages) for item in normalized),
                "audio_frames": sum(len(item.trace.frames) for item in normalized),
            },
            "stages": {
                key: _distribution(
                    value
                    for item in normalized
                    for value in item.stage_values[key]
                )
                for key in STAGE_METRIC_KEYS
            },
            "blocked_puts": {
                key: _distribution(
                    value
                    for item in normalized
                    for value in item.blocked_put_values[key]
                )
                for key in BLOCKED_PUT_KEYS
            },
        },
        "samples": [
            _sample_payload(
                item,
                window_seconds=window_seconds,
                top_window_count=top_window_count,
            )
            for item in normalized
        ],
    }
    return _round_floats(payload)


def analyze_paths(
    paths: Sequence[Path],
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    top_window_count: int = DEFAULT_TOP_WINDOWS,
) -> dict[str, Any]:
    """Analyze caller-ordered CSV paths and infer adjacent summary paths."""

    normalized = tuple(Path(path) for path in paths)
    if not normalized:
        raise ValueError("at least one input CSV is required")
    resolved = [path.resolve() for path in normalized]
    if len(resolved) != len(set(resolved)):
        raise ValueError("input CSV paths must be unique")
    samples = tuple(
        load_stage_burst_sample(path, sample_index=index)
        for index, path in enumerate(normalized, start=1)
    )
    return build_stage_burst_analysis(
        samples,
        window_seconds=window_seconds,
        top_window_count=top_window_count,
    )


def _seconds(value: int | float) -> str:
    return f"{float(value):.3f}s"


def _metric_value(name: str, value: int | float) -> str:
    normalized = float(value)
    if name.endswith("_count"):
        return (
            str(int(normalized))
            if normalized.is_integer()
            else f"{normalized:.3f}"
        )
    if name.endswith("_realtime_factor"):
        return f"{normalized:.3f}x"
    return _seconds(normalized)


def render_markdown(analysis: dict[str, Any]) -> str:
    """Render a concise interpretation of a numeric analysis payload."""

    aggregate = analysis["aggregate"]
    lines = [
        "# Stage and Burst Attribution",
        "",
        (
            "This report describes timing associations; it does not establish "
            "causality or treat stage intervals as additive."
        ),
        "",
        (
            f"Analyzed {aggregate['counts']['samples']} neutral sample(s), "
            f"{aggregate['counts']['parents']} parents, and "
            f"{aggregate['counts']['audio_frames']} browser audio frames."
        ),
        "",
        "## Whole-sample stage distributions",
        "",
        "| Metric | Count | p50 | p95 | Max |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metric in aggregate["stages"].items():
        lines.append(
            f"| {name.replace('_', ' ')} | "
            f"{metric['observation_count']:,} | "
            f"{_metric_value(name, metric['p50'])} | "
            f"{_metric_value(name, metric['p95'])} | "
            f"{_metric_value(name, metric['max'])} |"
        )

    lines.extend(
        [
            "",
        "## Positive queue-growth windows",
            "",
            (
                "Windows are aligned, fixed-grid, half-open intervals. "
                "The rows below are the largest positive listener-queue "
                "changes in each sample. Sliding whole-second windows are "
                "used separately for the descriptive associations."
            ),
            "",
            "| Sample | Start | End | Queue growth | Arrived audio | Arrival RTF |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for sample in analysis["samples"]:
        for window in sample["top_aligned_positive_growth_windows"]:
            lines.append(
                f"| {sample['sample_index']:02d} | "
                f"{window['start_seconds']:.0f}s | "
                f"{window['end_seconds']:.0f}s | "
                f"{_seconds(window['net_queue_growth_seconds'])} | "
                f"{_seconds(window['arrived_audio_seconds'])} | "
                f"{window['arrival_realtime_factor']:.3f}x |"
            )

    lines.extend(
        [
            "",
            "## Descriptive associations",
            "",
            "| Sample | Aligned-window audio ↔ queue growth | "
            "Rolling-window audio ↔ queue growth | "
            "Parent audio ↔ immediate queue change | "
            "First-frame latency ↔ immediate queue change |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for sample in analysis["samples"]:
        aligned_window_audio = sample[
            "descriptive_aligned_window_spearman"
        ]["arrived_audio_seconds"]
        rolling_window_audio = sample["descriptive_window_spearman"][
            "arrived_audio_seconds"
        ]
        parent_audio = sample["descriptive_parent_spearman"][
            "audio_seconds_to_immediate_queue_change"
        ]
        first_frame = sample["descriptive_parent_spearman"][
            "source_to_first_frame_to_immediate_queue_change"
        ]
        lines.append(
            f"| {sample['sample_index']:02d} | "
            f"{aligned_window_audio['coefficient']:.3f} | "
            f"{rolling_window_audio['coefficient']:.3f} | "
            f"{parent_audio['coefficient']:.3f} | "
            f"{first_frame['coefficient']:.3f} |"
        )

    lines.extend(
        [
            "",
            (
                "Spearman coefficients are descriptive associations from "
                "one capture per sample. Overlapping windows, segment length, "
                "and workload timing are confounders."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Attribute schema-3 listener queue growth to privacy-safe stage "
            "and translated-audio burst evidence."
        )
    )
    parser.add_argument(
        "csv_paths",
        nargs="+",
        type=Path,
        help="Schema-3 *_results.csv paths; adjacent summaries are inferred.",
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=DEFAULT_WINDOW_SECONDS,
    )
    parser.add_argument(
        "--top-window-count",
        "--top-windows",
        dest="top_window_count",
        type=int,
        default=DEFAULT_TOP_WINDOWS,
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    input_paths = {path.resolve() for path in args.csv_paths}
    protected_evidence_paths = input_paths | {
        _inferred_summary_path(path).resolve() for path in args.csv_paths
    }
    output_paths = [
        path.resolve()
        for path in (args.json_output, args.markdown_output)
        if path is not None
    ]
    if any(path in protected_evidence_paths for path in output_paths):
        parser.error("an output path cannot alias input CSV or summary evidence")
    if len(output_paths) != len(set(output_paths)):
        parser.error("JSON and Markdown output paths must be different")
    analysis = analyze_paths(
        args.csv_paths,
        window_seconds=args.window_seconds,
        top_window_count=args.top_window_count,
    )
    markdown = render_markdown(analysis)
    if args.json_output is not None:
        args.json_output.write_text(
            json.dumps(analysis, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.markdown_output is not None:
        args.markdown_output.write_text(markdown, encoding="utf-8")
    if args.json_output is None and args.markdown_output is None:
        print(markdown)


if __name__ == "__main__":
    main()
