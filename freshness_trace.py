#!/usr/bin/env python3
"""Load parent-aware PCM arrivals from a validated schema-3 capture.

Legacy captures join anonymous browser PCM positionally only after every
schema-3 invariant has passed.  Opt-in audio-metadata protocol-v1 captures are
instead replayed through the strict receiver and reconciled against staged
send evidence and CSV metadata.  Both paths return only neutral filenames,
hashes, numeric timing, identity, and PCM metadata.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audio_metadata_protocol import (
    AUDIO_METADATA_PROTOCOL_VERSION,
    AudioMetadataProtocolError,
    AudioMetadataTracker,
)
from playback_simulation import ParentAudioFrame


DEFAULT_BYTES_PER_SAMPLE = 2
DEFAULT_TIMESTAMP_TOLERANCE_MS = 0.0051
PUBLIC_TRACE_LABEL = "schema3_client_events.csv"
PUBLIC_SUMMARY_LABEL = "schema3_capture_summary.json"
CSV_REQUIRED_COLUMNS = {
    "source",
    "stage",
    "timestamp_ms",
    "chunk_index",
    "audio_bytes",
}
CSV_AUDIO_METADATA_COLUMNS = {
    "protocol_version",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "source_start_ms",
    "source_end_ms",
    "source_end_to_receipt_ms",
}
CSV_METADATA_TOLERANCE_MS = 0.00051
FRAME_METADATA_FIELDS = (
    "protocolVersion",
    "streamGeneration",
    "parentSequenceId",
    "audioFrameId",
    "audioBytes",
    "sampleRateHz",
    "channels",
    "bytesPerSample",
    "sourceStartMs",
    "sourceEndMs",
)
PARENT_COMPLETE_METADATA_FIELDS = (
    "protocolVersion",
    "streamGeneration",
    "parentSequenceId",
    "audioFrameCount",
    "audioBytes",
    "sourceStartMs",
    "sourceEndMs",
)


@dataclass(frozen=True)
class ParentFreshnessTrace:
    """Privacy-safe inputs for a parent-aware freshness simulation."""

    trace_csv: str
    summary_json: str
    trace_sha256: str
    summary_sha256: str
    input_end_seconds: float
    sample_rate_hz: int
    channels: int
    bytes_per_sample: int
    frames: tuple[ParentAudioFrame, ...]
    input_sample_zero_timestamp_ms: float | None = None
    audio_metadata_protocol_version: int | None = None
    audio_metadata_stream_generation: int | None = None
    source_end_to_receipt_availability: str = "protocol_not_negotiated"


@dataclass(frozen=True)
class _ClientPcmReceive:
    """One privacy-safe CSV client receive row."""

    timestamp_ms: float
    chunk_index: int
    audio_bytes: int
    protocol_version: int | None = None
    stream_generation: int | None = None
    parent_sequence_id: int | None = None
    audio_frame_id: int | None = None
    source_start_ms: float | None = None
    source_end_ms: float | None = None
    source_end_to_receipt_ms: float | None = None


@dataclass(frozen=True)
class _WirePcmReceive:
    """One PCM receive reconstructed from ordered summary wire evidence."""

    timestamp_ms: float
    audio_bytes: int
    order: int
    metadata: dict[str, Any] | None = None
    source_end_to_receipt_ms: float | None = None


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _require_number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{field} must be a finite number")
    normalized = float(value)
    if minimum is not None and normalized < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return normalized


def _optional_csv_number(
    value: str | None,
    *,
    path: Path,
    row_number: int,
    field: str,
    nonnegative: bool = True,
) -> float | None:
    if value in (None, ""):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path.name}:{row_number}: {field} must be blank or numeric"
        ) from exc
    if not math.isfinite(normalized) or (nonnegative and normalized < 0):
        raise ValueError(
            f"{path.name}:{row_number}: {field} must be blank or "
            + ("finite and non-negative" if nonnegative else "finite")
        )
    return normalized


def _required_csv_int(
    value: str | None,
    *,
    path: Path,
    row_number: int,
    field: str,
    minimum: int,
) -> int:
    try:
        normalized = int(value) if value is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path.name}:{row_number}: {field} must be an integer"
        ) from exc
    if normalized is None or normalized < minimum:
        raise ValueError(
            f"{path.name}:{row_number}: {field} must be at least {minimum}"
        )
    return normalized


def _numbers_match(
    left: float | None,
    right: float | None,
    *,
    tolerance: float,
) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)


def _validate_parent_summaries(
    staged: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[int, tuple[int, int]]]:
    layer_names = (
        "completed_parent_summaries",
        "produced_parent_summaries",
        "websocket_completed_parent_summaries",
    )
    layers = [
        _require_list(staged.get(name), f"staged_pipeline.{name}")
        for name in layer_names
    ]
    if not layers[0]:
        raise ValueError("schema-3 capture contains no completed parents")
    if layers[1] != layers[0] or layers[2] != layers[0]:
        raise ValueError("schema-3 completed parent summary layers disagree")

    parents: list[dict[str, Any]] = []
    parent_shape: dict[int, tuple[int, int]] = {}
    for expected_id, raw_parent in enumerate(layers[0]):
        parent = _require_dict(
            raw_parent,
            f"staged_pipeline.completed_parent_summaries[{expected_id}]",
        )
        parent_id = _require_int(
            parent.get("parent_sequence_id"),
            f"parent[{expected_id}].parent_sequence_id",
            minimum=0,
        )
        if parent_id != expected_id:
            raise ValueError(
                "completed parent IDs must be contiguous and ordered from zero"
            )
        frame_count = _require_int(
            parent.get("audio_frame_count"),
            f"parent[{parent_id}].audio_frame_count",
            minimum=1,
        )
        audio_bytes = _require_int(
            parent.get("audio_bytes"),
            f"parent[{parent_id}].audio_bytes",
            minimum=1,
        )
        parents.append(parent)
        parent_shape[parent_id] = (frame_count, audio_bytes)

    expected_ids = list(range(len(parents)))
    for field in ("completed_sequence_ids", "websocket_sent_sequence_ids"):
        if staged.get(field) != expected_ids:
            raise ValueError(
                f"staged_pipeline.{field} must match completed parent IDs"
            )
    return parents, parent_shape


def _validate_frame_layers(
    staged: dict[str, Any],
    parent_shape: dict[int, tuple[int, int]],
) -> tuple[list[dict[str, int]], list[int]]:
    send_events = _require_list(
        staged.get("websocket_send_events"),
        "staged_pipeline.websocket_send_events",
    )
    if not send_events:
        raise ValueError("schema-3 capture contains no WebSocket PCM sends")

    frame_keys: list[dict[str, int]] = []
    frame_bytes: list[int] = []
    observed_by_parent: dict[int, list[int]] = {
        parent_id: [] for parent_id in parent_shape
    }
    observed_bytes_by_parent: dict[int, int] = {
        parent_id: 0 for parent_id in parent_shape
    }
    previous_key: tuple[int, int] | None = None

    for index, raw_event in enumerate(send_events):
        event = _require_dict(
            raw_event,
            f"staged_pipeline.websocket_send_events[{index}]",
        )
        parent_id = _require_int(
            event.get("parent_sequence_id"),
            f"websocket_send_events[{index}].parent_sequence_id",
            minimum=0,
        )
        frame_id = _require_int(
            event.get("audio_frame_id"),
            f"websocket_send_events[{index}].audio_frame_id",
            minimum=0,
        )
        sequence_id = _require_int(
            event.get("sequence_id"),
            f"websocket_send_events[{index}].sequence_id",
            minimum=0,
        )
        audio_bytes = _require_int(
            event.get("audio_bytes"),
            f"websocket_send_events[{index}].audio_bytes",
            minimum=1,
        )
        if parent_id not in parent_shape:
            raise ValueError(
                f"WebSocket frame references unknown parent {parent_id}"
            )
        if sequence_id != parent_id:
            raise ValueError("WebSocket sequence and parent IDs disagree")
        key = (parent_id, frame_id)
        if previous_key is not None and key <= previous_key:
            raise ValueError(
                "WebSocket parent/frame keys must be strictly ordered"
            )
        previous_key = key
        observed_by_parent[parent_id].append(frame_id)
        observed_bytes_by_parent[parent_id] += audio_bytes
        frame_keys.append(
            {
                "parent_sequence_id": parent_id,
                "audio_frame_id": frame_id,
            }
        )
        frame_bytes.append(audio_bytes)

    for parent_id, (expected_count, expected_bytes) in parent_shape.items():
        if observed_by_parent[parent_id] != list(range(expected_count)):
            raise ValueError(
                f"parent {parent_id} frame IDs/count do not reconcile"
            )
        if observed_bytes_by_parent[parent_id] != expected_bytes:
            raise ValueError(
                f"parent {parent_id} frame bytes do not reconcile"
            )

    key_layers = (
        "published_audio_frame_keys",
        "dequeued_audio_frame_keys",
        "websocket_sent_audio_frame_keys",
    )
    byte_layers = (
        "published_audio_frame_bytes",
        "dequeued_audio_frame_bytes",
        "websocket_sent_audio_frame_bytes",
    )
    for field in key_layers:
        if staged.get(field) != frame_keys:
            raise ValueError(
                f"staged_pipeline.{field} disagrees with WebSocket sends"
            )
    for field in byte_layers:
        if staged.get(field) != frame_bytes:
            raise ValueError(
                f"staged_pipeline.{field} disagrees with WebSocket sends"
            )

    if staged.get("audio_frames_produced") != len(frame_keys):
        raise ValueError(
            "staged_pipeline.audio_frames_produced does not match frame evidence"
        )
    if staged.get("audio_segments_produced") != len(parent_shape):
        raise ValueError(
            "staged_pipeline.audio_segments_produced does not match parents"
        )
    return frame_keys, frame_bytes


def _load_client_csv(
    path: Path,
    *,
    audio_metadata_protocol_version: int | None,
) -> tuple[list[_ClientPcmReceive], float]:
    received: list[_ClientPcmReceive] = []
    input_end_values: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = CSV_REQUIRED_COLUMNS.difference(reader.fieldnames or ())
        if audio_metadata_protocol_version == AUDIO_METADATA_PROTOCOL_VERSION:
            missing.update(
                CSV_AUDIO_METADATA_COLUMNS.difference(reader.fieldnames or ())
            )
        if missing:
            raise ValueError(
                f"{path.name}: missing required columns: "
                f"{', '.join(sorted(missing))}"
            )
        for row_number, row in enumerate(reader, start=2):
            if row["source"] != "client":
                continue
            if row["stage"] not in ("audio_received", "input_ended"):
                continue
            try:
                timestamp_ms = float(row["timestamp_ms"])
                chunk_index = int(row["chunk_index"])
                audio_bytes = int(row["audio_bytes"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{path.name}:{row_number}: invalid numeric client event"
                ) from exc
            if not math.isfinite(timestamp_ms) or timestamp_ms < 0:
                raise ValueError(
                    f"{path.name}:{row_number}: invalid client timestamp"
                )
            if row["stage"] == "input_ended":
                if audio_bytes != 0:
                    raise ValueError(
                        f"{path.name}:{row_number}: input_ended bytes must be zero"
                    )
                input_end_values.append(timestamp_ms)
                continue
            if audio_bytes <= 0:
                raise ValueError(
                    f"{path.name}:{row_number}: audio_received bytes must be positive"
                )
            if audio_metadata_protocol_version is None:
                received.append(
                    _ClientPcmReceive(
                        timestamp_ms=timestamp_ms,
                        chunk_index=chunk_index,
                        audio_bytes=audio_bytes,
                    )
                )
                continue

            protocol_version = _required_csv_int(
                row.get("protocol_version"),
                path=path,
                row_number=row_number,
                field="protocol_version",
                minimum=1,
            )
            if protocol_version != AUDIO_METADATA_PROTOCOL_VERSION:
                raise ValueError(
                    f"{path.name}:{row_number}: protocol_version must equal 1"
                )
            source_start_ms = _optional_csv_number(
                row.get("source_start_ms"),
                path=path,
                row_number=row_number,
                field="source_start_ms",
            )
            source_end_ms = _optional_csv_number(
                row.get("source_end_ms"),
                path=path,
                row_number=row_number,
                field="source_end_ms",
            )
            if (
                source_start_ms is not None
                and source_end_ms is not None
                and source_end_ms < source_start_ms
            ):
                raise ValueError(
                    f"{path.name}:{row_number}: source_end_ms cannot "
                    "precede source_start_ms"
                )
            received.append(
                _ClientPcmReceive(
                    timestamp_ms=timestamp_ms,
                    chunk_index=chunk_index,
                    audio_bytes=audio_bytes,
                    protocol_version=protocol_version,
                    stream_generation=_required_csv_int(
                        row.get("stream_generation"),
                        path=path,
                        row_number=row_number,
                        field="stream_generation",
                        minimum=1,
                    ),
                    parent_sequence_id=_required_csv_int(
                        row.get("parent_sequence_id"),
                        path=path,
                        row_number=row_number,
                        field="parent_sequence_id",
                        minimum=0,
                    ),
                    audio_frame_id=_required_csv_int(
                        row.get("audio_frame_id"),
                        path=path,
                        row_number=row_number,
                        field="audio_frame_id",
                        minimum=0,
                    ),
                    source_start_ms=source_start_ms,
                    source_end_ms=source_end_ms,
                    source_end_to_receipt_ms=_optional_csv_number(
                        row.get("source_end_to_receipt_ms"),
                        path=path,
                        row_number=row_number,
                        field="source_end_to_receipt_ms",
                        nonnegative=False,
                    ),
                )
            )

    if len(input_end_values) != 1:
        raise ValueError(
            f"{path.name}: exactly one client input_ended event is required"
        )
    if not received:
        raise ValueError(f"{path.name}: no client audio_received events found")
    indexes = [item.chunk_index for item in received]
    if indexes != list(range(len(received))):
        raise ValueError(
            f"{path.name}: client audio_received indexes must be contiguous"
        )
    timestamps = [item.timestamp_ms for item in received]
    if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
        raise ValueError(
            f"{path.name}: client audio_received timestamps must be ordered"
        )
    return received, input_end_values[0]


def _load_audio_metadata_observation(
    summary: dict[str, Any],
) -> tuple[int | None, dict[str, Any] | None]:
    raw_observation = summary.get("audio_metadata_observation")
    if raw_observation is None:
        return None, None
    observation = _require_dict(
        raw_observation,
        "audio_metadata_observation",
    )
    protocol_version = observation.get("protocol_version")
    if protocol_version is None:
        return None, observation
    protocol_version = _require_int(
        protocol_version,
        "audio_metadata_observation.protocol_version",
        minimum=1,
    )
    if protocol_version != AUDIO_METADATA_PROTOCOL_VERSION:
        raise ValueError(
            "unsupported audio metadata observation protocol version"
        )
    return protocol_version, observation


def _replay_receive_events(
    receive_events: list[Any],
    *,
    audio_metadata_protocol_version: int | None,
) -> tuple[
    list[_WirePcmReceive],
    tuple[int, float],
    AudioMetadataTracker | None,
]:
    tracker = (
        AudioMetadataTracker(
            enabled=True,
            protocol_version=audio_metadata_protocol_version,
        )
        if audio_metadata_protocol_version is not None
        else None
    )
    observed_orders: list[int] = []
    pcm_receives: list[_WirePcmReceive] = []
    completed_terminals: list[tuple[int, float]] = []

    for index, raw_event in enumerate(receive_events):
        event = _require_dict(raw_event, f"websocket_receive_events[{index}]")
        order = _require_int(
            event.get("order"),
            f"websocket_receive_events[{index}].order",
            minimum=0,
        )
        observed_orders.append(order)
        frame_type = event.get("frame_type")

        if frame_type == "pcm":
            timestamp_ms = _require_number(
                event.get("timestamp_ms"),
                f"websocket_receive_events[{index}].timestamp_ms",
                minimum=0,
            )
            audio_bytes = _require_int(
                event.get("audio_bytes"),
                f"websocket_receive_events[{index}].audio_bytes",
                minimum=1,
            )
            metadata: dict[str, Any] | None = None
            if tracker is not None:
                try:
                    metadata = tracker.accept_binary_size(audio_bytes)
                except AudioMetadataProtocolError as exc:
                    raise ValueError(
                        f"websocket_receive_events[{index}] metadata "
                        f"invalid: {exc}"
                    ) from exc
                if metadata is None:
                    raise ValueError(
                        "negotiated PCM did not pair with audio metadata"
                    )
                captured = {
                    field: event.get(field) for field in FRAME_METADATA_FIELDS
                }
                expected = {
                    field: metadata[field] for field in FRAME_METADATA_FIELDS
                }
                if captured != expected:
                    raise ValueError(
                        "PCM metadata does not match its preceding audio_frame"
                    )
            pcm_receives.append(
                _WirePcmReceive(
                    timestamp_ms=timestamp_ms,
                    audio_bytes=audio_bytes,
                    order=order,
                    metadata=metadata,
                    source_end_to_receipt_ms=(
                        _require_number(
                            event.get("sourceEndToReceiptMs"),
                            (
                                f"websocket_receive_events[{index}]"
                                ".sourceEndToReceiptMs"
                            ),
                        )
                        if event.get("sourceEndToReceiptMs") is not None
                        else None
                    ),
                )
            )
            continue

        if frame_type != "control":
            if tracker is not None:
                raise ValueError(
                    f"websocket_receive_events[{index}].frame_type is invalid"
                )
            continue

        message_type = event.get("message_type")
        if tracker is not None:
            try:
                if message_type == "audio_frame":
                    tracker.accept_control(
                        {
                            "type": "audio_frame",
                            **{
                                field: event.get(field)
                                for field in FRAME_METADATA_FIELDS
                            },
                        }
                    )
                elif message_type == "audio_parent_complete":
                    tracker.accept_control(
                        {
                            "type": "audio_parent_complete",
                            **{
                                field: event.get(field)
                                for field in PARENT_COMPLETE_METADATA_FIELDS
                            },
                        }
                    )
                else:
                    tracker.accept_control({"type": message_type})
            except AudioMetadataProtocolError as exc:
                raise ValueError(
                    f"websocket_receive_events[{index}] metadata "
                    f"invalid: {exc}"
                ) from exc
        elif message_type in {"audio_frame", "audio_parent_complete"}:
            raise ValueError(
                "audio metadata was captured without protocol negotiation"
            )

        if message_type == "status" and event.get("status") == "completed":
            completed_terminal = (
                order,
                _require_number(
                    event.get("timestamp_ms"),
                    f"websocket_receive_events[{index}].timestamp_ms",
                    minimum=0,
                ),
            )
            if tracker is not None:
                try:
                    tracker.assert_terminal_ready()
                except AudioMetadataProtocolError as exc:
                    raise ValueError(
                        f"websocket_receive_events[{index}] metadata "
                        f"invalid: {exc}"
                    ) from exc
            completed_terminals.append(completed_terminal)

    if observed_orders != list(range(len(observed_orders))):
        raise ValueError("WebSocket receive event order is not contiguous")
    if len(completed_terminals) != 1:
        raise ValueError(
            "exactly one completed WebSocket terminal is required"
        )
    completed_order, _ = completed_terminals[0]
    if any(event.order > completed_order for event in pcm_receives):
        raise ValueError("WebSocket PCM was received after completed terminal")
    if any(
        later.timestamp_ms < earlier.timestamp_ms
        for earlier, later in zip(pcm_receives, pcm_receives[1:])
    ):
        raise ValueError("WebSocket PCM receive timestamps are not ordered")
    if tracker is not None and not tracker.terminal_received:
        raise ValueError("audio metadata capture has no completed terminal")
    return pcm_receives, completed_terminals[0], tracker


def _nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _validate_observation_summary(
    observation: dict[str, Any],
    *,
    tracker: AudioMetadataTracker,
    pcm_receives: list[_WirePcmReceive],
    input_sample_zero_timestamp_ms: float,
) -> str:
    if observation.get("playback_behavior_changed") is not False:
        raise ValueError(
            "audio metadata observation must not change playback behavior"
        )
    if observation.get("contains_transcript_or_translation_text") is not False:
        raise ValueError(
            "audio metadata observation must not contain transcript or "
            "translation text"
        )
    stream_generation = _require_int(
        observation.get("stream_generation"),
        "audio_metadata_observation.stream_generation",
        minimum=1,
    )
    if stream_generation != tracker.stream_generation:
        raise ValueError(
            "audio metadata stream generation summary does not reconcile"
        )
    if _require_int(
        observation.get("paired_frames"),
        "audio_metadata_observation.paired_frames",
        minimum=0,
    ) != len(tracker.paired_frames):
        raise ValueError(
            "audio metadata paired-frame count does not reconcile"
        )
    if _require_int(
        observation.get("completed_parents"),
        "audio_metadata_observation.completed_parents",
        minimum=0,
    ) != len(tracker.completed_parents):
        raise ValueError(
            "audio metadata completed-parent count does not reconcile"
        )

    source_summary = _require_dict(
        observation.get("source_end_to_receipt"),
        "audio_metadata_observation.source_end_to_receipt",
    )
    if source_summary.get("clock") != "client_monotonic":
        raise ValueError("source-end receipt clock must be client_monotonic")
    if source_summary.get("source_offset_origin") != "input_pcm_sample_zero":
        raise ValueError(
            "source-end offsets must originate at input PCM sample zero"
        )
    if source_summary.get("semantic_boundary_proven") is not False:
        raise ValueError("source-end evidence is not a semantic boundary")
    if source_summary.get("actual_audibility_proven") is not False:
        raise ValueError("source-end evidence does not prove audibility")

    samples: list[float] = []
    source_end_frames: list[dict[str, Any]] = []
    for index, event in enumerate(pcm_receives):
        metadata = event.metadata
        if metadata is None:
            raise ValueError("protocol-v1 PCM is missing replayed metadata")
        source_end_ms = metadata["sourceEndMs"]
        observed_delay = event.source_end_to_receipt_ms
        if source_end_ms is None:
            if observed_delay is not None:
                raise ValueError(
                    "sourceEndToReceiptMs requires a sourceEndMs offset"
                )
            continue
        source_end_frames.append(metadata)
        expected_delay = (
            event.timestamp_ms
            - input_sample_zero_timestamp_ms
            - source_end_ms
        )
        if observed_delay is None or not math.isclose(
            observed_delay,
            expected_delay,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                f"wire sourceEndToReceiptMs {index} is inconsistent"
            )
        samples.append(expected_delay)

    if not samples:
        availability = "unavailable_missing_source_end_offsets"
    elif any(
        metadata["sourceStartMs"] is None
        for metadata in source_end_frames
    ):
        availability = (
            "available_audio_processed_end_offset_not_semantic_boundary"
        )
    else:
        availability = "available_asr_source_range_end_offset"
    if source_summary.get("availability") != availability:
        raise ValueError(
            "source-end receipt availability summary does not reconcile"
        )
    if _require_int(
        source_summary.get("sample_count"),
        "audio_metadata_observation.source_end_to_receipt.sample_count",
        minimum=0,
    ) != len(samples):
        raise ValueError(
            "source-end receipt sample count does not reconcile"
        )
    for field, expected in (
        ("p50_ms", _nearest_rank(samples, 0.50)),
        ("p95_ms", _nearest_rank(samples, 0.95)),
        ("max_ms", max(samples, default=None)),
    ):
        observed = source_summary.get(field)
        if expected is None:
            if observed is not None:
                raise ValueError(
                    f"source-end receipt {field} must be null without samples"
                )
        elif not math.isclose(
            _require_number(
                observed,
                f"audio_metadata_observation.source_end_to_receipt.{field}",
            ),
            expected,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ValueError(
                f"source-end receipt {field} does not reconcile"
            )
    return availability


def load_parent_freshness_trace(
    csv_path: Path,
    summary_path: Path | None = None,
    *,
    bytes_per_sample: int = DEFAULT_BYTES_PER_SAMPLE,
    timestamp_tolerance_ms: float = DEFAULT_TIMESTAMP_TOLERANCE_MS,
) -> ParentFreshnessTrace:
    """Join CSV client arrivals to schema-3 parent/frame evidence.

    Legacy evidence is joined positionally only after every count, byte,
    timestamp, and completion invariant has passed. Protocol-v1 evidence is
    joined by its replay-validated parent/frame identity. A malformed or
    incomplete capture raises ``ValueError`` rather than producing a partial
    simulation.
    """

    csv_path = Path(csv_path)
    if summary_path is None:
        summary_path = csv_path.with_name(
            csv_path.name.removesuffix("_results.csv") + "_summary.json"
        )
    else:
        summary_path = Path(summary_path)
    if not csv_path.is_file():
        raise ValueError(f"trace CSV does not exist: {csv_path.name}")
    if not summary_path.is_file():
        raise ValueError(f"summary JSON does not exist: {summary_path.name}")
    if (
        not isinstance(bytes_per_sample, int)
        or isinstance(bytes_per_sample, bool)
        or bytes_per_sample <= 0
    ):
        raise ValueError("bytes_per_sample must be a positive integer")
    if (
        not isinstance(timestamp_tolerance_ms, (int, float))
        or isinstance(timestamp_tolerance_ms, bool)
        or not math.isfinite(timestamp_tolerance_ms)
        or timestamp_tolerance_ms < 0
    ):
        raise ValueError(
            "timestamp_tolerance_ms must be finite and non-negative"
        )

    with summary_path.open(encoding="utf-8") as handle:
        summary = _require_dict(json.load(handle), "summary")
    (
        audio_metadata_protocol_version,
        audio_metadata_observation,
    ) = _load_audio_metadata_observation(summary)
    if summary.get("pipeline_mode") != "staged":
        raise ValueError("freshness replay requires a staged capture")
    integrity = _require_dict(summary.get("staged_integrity"), "staged_integrity")
    if (
        integrity.get("applicable") is not True
        or integrity.get("passed") is not True
        or integrity.get("errors") != []
    ):
        raise ValueError("staged capture integrity did not pass cleanly")

    backend_config = _require_dict(
        summary.get("backend_config"),
        "backend_config",
    )
    staged_config = _require_dict(
        backend_config.get("stagedConfig"),
        "backend_config.stagedConfig",
    )
    if staged_config.get("telemetrySchemaVersion") != 3:
        raise ValueError("backend config is not telemetry schema 3")
    if staged_config.get("ttsIncrementalPublishEnabled") is not True:
        raise ValueError("backend config did not enable incremental TTS")
    if (
        audio_metadata_protocol_version == AUDIO_METADATA_PROTOCOL_VERSION
        and backend_config.get("audioMetadataProtocolVersions")
        != [AUDIO_METADATA_PROTOCOL_VERSION]
    ):
        raise ValueError(
            "backend config did not advertise audio metadata protocol "
            "version 1"
        )
    sample_rate_hz = _require_int(
        backend_config.get("sampleRate"),
        "backend_config.sampleRate",
        minimum=1,
    )
    channels = _require_int(
        backend_config.get("channels"),
        "backend_config.channels",
        minimum=1,
    )

    staged = _require_dict(summary.get("staged_pipeline"), "staged_pipeline")
    if staged.get("telemetry_schema_version") != 3:
        raise ValueError("capture is not telemetry schema 3")
    if staged.get("tts_incremental_publish_enabled") is not True:
        raise ValueError("capture did not use incremental TTS publication")
    if staged.get("state") != "closed" or staged.get("outcome") != "complete":
        raise ValueError("schema-3 capture did not close successfully")
    if staged.get("failure") is not None:
        raise ValueError("schema-3 capture contains a pipeline failure")
    if staged.get("cleanup_errors") != []:
        raise ValueError("schema-3 capture contains cleanup errors")
    if staged.get("incomplete_sequence_ids") != []:
        raise ValueError("schema-3 capture contains incomplete parents")

    _, parent_shape = _validate_parent_summaries(staged)
    frame_keys, frame_bytes = _validate_frame_layers(staged, parent_shape)

    receive_events = _require_list(
        summary.get("websocket_receive_events"),
        "websocket_receive_events",
    )
    (
        pcm_receives,
        (_, completed_timestamp_ms),
        metadata_tracker,
    ) = _replay_receive_events(
        receive_events,
        audio_metadata_protocol_version=audio_metadata_protocol_version,
    )
    if [item.audio_bytes for item in pcm_receives] != frame_bytes:
        raise ValueError("WebSocket receive PCM bytes disagree with send evidence")

    csv_receives, csv_input_end_ms = _load_client_csv(
        csv_path,
        audio_metadata_protocol_version=audio_metadata_protocol_version,
    )
    if len(csv_receives) != len(frame_keys):
        raise ValueError("CSV client PCM count disagrees with schema-3 evidence")
    if [item.audio_bytes for item in csv_receives] != frame_bytes:
        raise ValueError("CSV client PCM bytes disagree with schema-3 evidence")
    if len(pcm_receives) != len(csv_receives):
        raise ValueError("summary and CSV client PCM counts disagree")
    for index, (csv_receive, wire_receive) in enumerate(
        zip(csv_receives, pcm_receives)
    ):
        if (
            abs(csv_receive.timestamp_ms - wire_receive.timestamp_ms)
            > float(timestamp_tolerance_ms)
        ):
            raise ValueError(
                f"summary and CSV client PCM timestamp {index} disagree"
            )

    summary_input_end_ms = _require_number(
        summary.get("input_end_timestamp_ms"),
        "input_end_timestamp_ms",
        minimum=0,
    )
    if (
        abs(csv_input_end_ms - summary_input_end_ms)
        > float(timestamp_tolerance_ms)
    ):
        raise ValueError("summary and CSV input-end timestamps disagree")
    if completed_timestamp_ms < summary_input_end_ms:
        raise ValueError(
            "completed WebSocket terminal arrived before input ended"
        )

    input_sample_zero_timestamp_ms: float | None = None
    audio_metadata_stream_generation: int | None = None
    source_end_to_receipt_availability = "protocol_not_negotiated"
    if audio_metadata_protocol_version is not None:
        if audio_metadata_observation is None or metadata_tracker is None:
            raise ValueError(
                "protocol-v1 capture is missing its observation summary"
            )
        input_sample_zero_timestamp_ms = _require_number(
            audio_metadata_observation.get(
                "input_sample_zero_timestamp_ms"
            ),
            "audio_metadata_observation.input_sample_zero_timestamp_ms",
            minimum=0,
        )
        if len(metadata_tracker.paired_frames) != len(frame_keys):
            raise ValueError(
                "audio metadata paired-frame count disagrees with staged evidence"
            )
        if len(metadata_tracker.completed_parents) != len(parent_shape):
            raise ValueError(
                "audio metadata parent completions disagree with staged evidence"
            )

        for index, (metadata, key, expected_bytes) in enumerate(
            zip(
                metadata_tracker.paired_frames,
                frame_keys,
                frame_bytes,
            )
        ):
            if (
                metadata["parentSequenceId"]
                != key["parent_sequence_id"]
                or metadata["audioFrameId"] != key["audio_frame_id"]
            ):
                raise ValueError(
                    f"audio metadata frame identity {index} disagrees "
                    "with staged send evidence"
                )
            if metadata["audioBytes"] != expected_bytes:
                raise ValueError(
                    f"audio metadata frame bytes {index} disagree with "
                    "staged send evidence"
                )
            if (
                metadata["sampleRateHz"] != sample_rate_hz
                or metadata["channels"] != channels
                or metadata["bytesPerSample"] != bytes_per_sample
            ):
                raise ValueError(
                    f"audio metadata PCM format {index} disagrees with "
                    "capture configuration"
                )

        for parent_id, completion in enumerate(
            metadata_tracker.completed_parents
        ):
            expected_count, expected_bytes = parent_shape[parent_id]
            if (
                completion["parentSequenceId"] != parent_id
                or completion["audioFrameCount"] != expected_count
                or completion["audioBytes"] != expected_bytes
            ):
                raise ValueError(
                    f"audio metadata parent completion {parent_id} "
                    "disagrees with staged evidence"
                )

        for index, (csv_receive, wire_receive) in enumerate(
            zip(csv_receives, pcm_receives)
        ):
            metadata = wire_receive.metadata
            if metadata is None:
                raise ValueError(
                    f"protocol-v1 PCM frame {index} is missing metadata"
                )
            if (
                csv_receive.protocol_version
                != metadata["protocolVersion"]
                or csv_receive.stream_generation
                != metadata["streamGeneration"]
                or csv_receive.parent_sequence_id
                != metadata["parentSequenceId"]
                or csv_receive.audio_frame_id
                != metadata["audioFrameId"]
            ):
                raise ValueError(
                    f"CSV audio metadata identity {index} disagrees "
                    "with wire evidence"
                )
            if not _numbers_match(
                csv_receive.source_start_ms,
                metadata["sourceStartMs"],
                tolerance=CSV_METADATA_TOLERANCE_MS,
            ) or not _numbers_match(
                csv_receive.source_end_ms,
                metadata["sourceEndMs"],
                tolerance=CSV_METADATA_TOLERANCE_MS,
            ):
                raise ValueError(
                    f"CSV source offsets {index} disagree with wire evidence"
                )
            if not _numbers_match(
                csv_receive.source_end_to_receipt_ms,
                wire_receive.source_end_to_receipt_ms,
                tolerance=CSV_METADATA_TOLERANCE_MS,
            ):
                raise ValueError(
                    f"CSV source-end receipt delay {index} disagrees "
                    "with wire evidence"
                )

        source_end_to_receipt_availability = (
            _validate_observation_summary(
                audio_metadata_observation,
                tracker=metadata_tracker,
                pcm_receives=pcm_receives,
                input_sample_zero_timestamp_ms=(
                    input_sample_zero_timestamp_ms
                ),
            )
        )
        audio_metadata_stream_generation = (
            metadata_tracker.stream_generation
        )

    frames: list[ParentAudioFrame] = []
    for source_index, (csv_receive, key, wire_receive) in enumerate(
        zip(csv_receives, frame_keys, pcm_receives)
    ):
        parent_id = key["parent_sequence_id"]
        metadata = wire_receive.metadata
        frames.append(
            ParentAudioFrame(
                arrival_seconds=(
                    wire_receive.timestamp_ms
                    if metadata is not None
                    else csv_receive.timestamp_ms
                )
                / 1000.0,
                duration_seconds=(
                    csv_receive.audio_bytes
                    / (sample_rate_hz * channels * bytes_per_sample)
                ),
                audio_bytes=csv_receive.audio_bytes,
                source_index=source_index,
                parent_sequence_id=parent_id,
                audio_frame_id=key["audio_frame_id"],
                parent_frame_count=parent_shape[parent_id][0],
                source_start_ms=(
                    metadata["sourceStartMs"]
                    if metadata is not None
                    else None
                ),
                source_end_ms=(
                    metadata["sourceEndMs"]
                    if metadata is not None
                    else None
                ),
            )
        )

    return ParentFreshnessTrace(
        trace_csv=PUBLIC_TRACE_LABEL,
        summary_json=PUBLIC_SUMMARY_LABEL,
        trace_sha256=_sha256(csv_path),
        summary_sha256=_sha256(summary_path),
        input_end_seconds=summary_input_end_ms / 1000.0,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        bytes_per_sample=bytes_per_sample,
        frames=tuple(frames),
        input_sample_zero_timestamp_ms=input_sample_zero_timestamp_ms,
        audio_metadata_protocol_version=audio_metadata_protocol_version,
        audio_metadata_stream_generation=audio_metadata_stream_generation,
        source_end_to_receipt_availability=(
            source_end_to_receipt_availability
        ),
    )
