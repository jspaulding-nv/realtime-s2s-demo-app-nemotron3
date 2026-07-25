#!/usr/bin/env python3
"""Load parent-aware PCM arrivals from a validated schema-3 capture.

The live browser protocol currently carries anonymous binary PCM.  A schema-3
test artifact records the server-side parent/frame identity separately from the
client receive timestamps, however, so an offline policy replay can join the
two evidence streams.  This module performs that join fail-closed and returns
only neutral filenames, hashes, numeric timing, and PCM metadata.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
) -> tuple[list[tuple[float, int, int]], float]:
    received: list[tuple[float, int, int]] = []
    input_end_values: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = CSV_REQUIRED_COLUMNS.difference(reader.fieldnames or ())
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
            received.append((timestamp_ms, chunk_index, audio_bytes))

    if len(input_end_values) != 1:
        raise ValueError(
            f"{path.name}: exactly one client input_ended event is required"
        )
    if not received:
        raise ValueError(f"{path.name}: no client audio_received events found")
    indexes = [item[1] for item in received]
    if indexes != list(range(len(received))):
        raise ValueError(
            f"{path.name}: client audio_received indexes must be contiguous"
        )
    timestamps = [item[0] for item in received]
    if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
        raise ValueError(
            f"{path.name}: client audio_received timestamps must be ordered"
        )
    return received, input_end_values[0]


def load_parent_freshness_trace(
    csv_path: Path,
    summary_path: Path | None = None,
    *,
    bytes_per_sample: int = DEFAULT_BYTES_PER_SAMPLE,
    timestamp_tolerance_ms: float = DEFAULT_TIMESTAMP_TOLERANCE_MS,
) -> ParentFreshnessTrace:
    """Join CSV client arrivals to schema-3 parent/frame evidence.

    The join is positional only after every count, identity, byte, timestamp,
    and completion invariant has been checked.  A malformed or incomplete
    capture raises ``ValueError`` rather than producing a partial simulation.
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
    observed_orders: list[int] = []
    pcm_receives: list[tuple[float, int, int]] = []
    completed_terminals: list[tuple[int, float]] = []
    for index, raw_event in enumerate(receive_events):
        event = _require_dict(raw_event, f"websocket_receive_events[{index}]")
        order = _require_int(
            event.get("order"),
            f"websocket_receive_events[{index}].order",
            minimum=0,
        )
        observed_orders.append(order)
        if (
            event.get("frame_type") == "control"
            and event.get("message_type") == "status"
            and event.get("status") == "completed"
        ):
            completed_terminals.append(
                (
                    order,
                    _require_number(
                        event.get("timestamp_ms"),
                        (
                            f"websocket_receive_events[{index}]"
                            ".timestamp_ms"
                        ),
                        minimum=0,
                    ),
                )
            )
        if event.get("frame_type") != "pcm":
            continue
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
        pcm_receives.append((timestamp_ms, audio_bytes, order))
    if observed_orders != list(range(len(observed_orders))):
        raise ValueError("WebSocket receive event order is not contiguous")
    if len(completed_terminals) != 1:
        raise ValueError(
            "exactly one completed WebSocket terminal is required"
        )
    completed_order, completed_timestamp_ms = completed_terminals[0]
    if any(order > completed_order for _, _, order in pcm_receives):
        raise ValueError("WebSocket PCM was received after completed terminal")
    if [item[1] for item in pcm_receives] != frame_bytes:
        raise ValueError("WebSocket receive PCM bytes disagree with send evidence")
    if any(
        later[0] < earlier[0]
        for earlier, later in zip(pcm_receives, pcm_receives[1:])
    ):
        raise ValueError("WebSocket PCM receive timestamps are not ordered")

    csv_receives, csv_input_end_ms = _load_client_csv(csv_path)
    if len(csv_receives) != len(frame_keys):
        raise ValueError("CSV client PCM count disagrees with schema-3 evidence")
    if [item[2] for item in csv_receives] != frame_bytes:
        raise ValueError("CSV client PCM bytes disagree with schema-3 evidence")
    if len(pcm_receives) != len(csv_receives):
        raise ValueError("summary and CSV client PCM counts disagree")
    for index, ((csv_ms, _, _), (summary_ms, _, _)) in enumerate(
        zip(csv_receives, pcm_receives)
    ):
        if abs(csv_ms - summary_ms) > float(timestamp_tolerance_ms):
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

    frames: list[ParentAudioFrame] = []
    for source_index, (
        (arrival_ms, _, audio_bytes),
        key,
    ) in enumerate(zip(csv_receives, frame_keys)):
        parent_id = key["parent_sequence_id"]
        frames.append(
            ParentAudioFrame(
                arrival_seconds=arrival_ms / 1000.0,
                duration_seconds=(
                    audio_bytes
                    / (sample_rate_hz * channels * bytes_per_sample)
                ),
                audio_bytes=audio_bytes,
                source_index=source_index,
                parent_sequence_id=parent_id,
                audio_frame_id=key["audio_frame_id"],
                parent_frame_count=parent_shape[parent_id][0],
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
    )
