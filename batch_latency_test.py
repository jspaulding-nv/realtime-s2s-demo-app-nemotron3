#!/usr/bin/env python3
"""
Batch latency test for real-time audio translation.

Streams audio files through the backend WebSocket, measures translation
drift, and generates per-file latency plots and CSV exports.

Prerequisites:
  1. Riva gRPC services running at the backend's configured RIVA_URI
  2. Backend: cd backend && uvicorn main:app --host 0.0.0.0 --port 8000

Usage:
  python batch_latency_test.py                          # All 3 MP3 files
  python batch_latency_test.py --preflight              # Pre-flight only
  python batch_latency_test.py --file test_audio/X.mp3  # Single file
  python batch_latency_test.py --backend http://host:port
"""

import argparse
import asyncio
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
import websockets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
CHUNK_SAMPLES = 4800
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE  # 9600
CHUNK_DURATION = CHUNK_SAMPLES / SAMPLE_RATE      # 0.3 s
DRAIN_MAX_SECONDS = 300
TERMINAL_SETTLE_SECONDS = 0.25
EXPORT_POLL_INTERVAL_SECONDS = 0.1
TARGET_LANGUAGE = "es-US"

AUDIO_DIR = Path(os.environ.get("S2S_TEST_AUDIO_DIR", "test_audio")).expanduser()
LONG_FORM_FILES = [
    str(AUDIO_DIR / "long-form-01.mp3"),
    str(AUDIO_DIR / "long-form-02.mp3"),
    str(AUDIO_DIR / "long-form-03.mp3"),
]
PREFLIGHT_FILE = str(AUDIO_DIR / "preflight.wav")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TimingEvent:
    source: str            # "client" or "backend"
    stage: str
    timestamp_ms: float
    chunk_index: int
    source_position_sec: float
    audio_bytes: int


@dataclass
class DriftSample:
    elapsed_sec: float
    drift_sec: float


@dataclass
class TestResult:
    audio_path: str
    duration_sec: float
    backend_url: str = ""
    backend_config_url: str = ""
    backend_config: dict = field(default_factory=dict)
    target_language: str = TARGET_LANGUAGE
    pipeline_mode: str = "monolithic"
    pipeline_mode_source: str = "legacy_default"
    staged_pipeline: Any = None
    staged_integrity_errors: list[str] = field(default_factory=list)
    websocket_receive_events: list[dict[str, Any]] = field(default_factory=list)
    chunks_sent: int = 0
    audio_responses: int = 0
    total_received_bytes: int = 0
    client_events: list = field(default_factory=list)
    backend_events: list = field(default_factory=list)
    drift_samples: list = field(default_factory=list)
    avg_drift: float = 0.0
    max_drift: float = 0.0
    final_drift: float = 0.0
    output_duration_sec: float = 0.0
    tts_expansion_ratio: float = 0.0
    tail_lag_sec: float = 0.0
    first_audio_latency_sec: float = 0.0
    duration_excess_sec: float = 0.0
    playback_tail_sec: float = 0.0
    post_input_responses: int = 0
    input_completed: bool = False
    connection_lost: bool = False
    drain_timed_out: bool = False
    drain_duration_sec: float = 0.0
    input_end_timestamp_ms: float = 0.0
    terminal_arrival_timestamp_ms: float = 0.0
    terminal_arrival_lag_sec: float = 0.0
    translation_completed: bool = False
    server_error: str = ""


# ---------------------------------------------------------------------------
# Evidence provenance and staged-pipeline integrity
# ---------------------------------------------------------------------------
def resolve_pipeline_mode(backend_config: dict) -> tuple[str, str]:
    """Resolve the server-selected path from an ``/api/config`` snapshot.

    Older backends did not expose ``pipelineMode`` and only supported the
    monolithic route. Treating a missing value as that legacy default keeps
    historical runs compatible while recording that the mode was inferred.
    """
    if "pipelineMode" not in backend_config:
        return "monolithic", "legacy_default"

    mode = backend_config["pipelineMode"]
    if not isinstance(mode, str) or mode not in {"monolithic", "staged"}:
        raise ValueError(
            "/api/config.pipelineMode must be 'monolithic' or 'staged'"
        )
    return mode, "api_config"


def fetch_backend_config(backend_url: str) -> tuple[dict, str, str, str]:
    """Capture the active backend configuration used as test provenance."""
    config_url = f"{backend_url.rstrip('/')}/api/config"
    response = requests.get(config_url, timeout=10)
    response.raise_for_status()
    config = response.json()
    if not isinstance(config, dict):
        raise ValueError("/api/config must return a JSON object")
    mode, mode_source = resolve_pipeline_mode(config)
    return config, mode, mode_source, config_url


def _sequence_ids(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
) -> list[int] | None:
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list")
        return None
    if any(
        not isinstance(sequence_id, int)
        or isinstance(sequence_id, bool)
        or sequence_id < 0
        for sequence_id in value
    ):
        errors.append(f"{field_name} must contain non-negative integer IDs")
        return None
    return value


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


_MISSING = object()
_SUPPORTED_STAGED_TELEMETRY_SCHEMAS = {1, 2, 3}
_SUBSEGMENT_LIFECYCLE_EVENTS = (
    ("target_splitter", "emitted"),
    ("tts", "enqueued"),
    ("tts", "started"),
    ("tts", "first_audio"),
    ("tts", "completed"),
    ("output", "enqueued"),
    ("output", "dequeued"),
)


def _is_terminal_output_dequeue(
    event_name: tuple[Any, Any],
    event: dict[str, Any],
) -> bool:
    """Return whether this is the identity-free COMPLETE queue dequeue.

    The pipeline records ``output/dequeued`` for both AUDIO children and the
    terminal COMPLETE event. Only AUDIO dequeues carry composite identity and
    positive PCM bytes. Keep validating every other output dequeue as a child,
    so an identity-free positive-audio event still fails closed.
    """

    audio_bytes = event.get("audio_bytes")
    return (
        event_name == ("output", "dequeued")
        and event.get("sequence_id") is None
        and event.get("parent_sequence_id") is None
        and event.get("subsequence_id") is None
        and event.get("subsequence_count") is None
        and isinstance(audio_bytes, int)
        and not isinstance(audio_bytes, bool)
        and audio_bytes == 0
    )


def _staged_telemetry_schema_version(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
) -> int | None:
    """Return an explicitly valid version; a missing field is legacy v1."""
    if value is _MISSING:
        return 1
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value not in _SUPPORTED_STAGED_TELEMETRY_SCHEMAS
    ):
        errors.append(f"{field_name} must be 1, 2, or 3")
        return None
    return value


def _subsegment_key(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
    sequence_alias: Any = _MISSING,
) -> tuple[int, int, int] | None:
    if not isinstance(value, dict):
        errors.append(f"{field_name} must be an object")
        return None
    parent = value.get("parent_sequence_id")
    subsequence_id = value.get("subsequence_id")
    subsequence_count = value.get("subsequence_count")
    if (
        not isinstance(parent, int)
        or isinstance(parent, bool)
        or parent < 0
    ):
        errors.append(f"{field_name}.parent_sequence_id is invalid")
        return None
    if (
        not isinstance(subsequence_id, int)
        or isinstance(subsequence_id, bool)
        or subsequence_id < 0
    ):
        errors.append(f"{field_name}.subsequence_id is invalid")
        return None
    if not _positive_int(subsequence_count):
        errors.append(f"{field_name}.subsequence_count is invalid")
        return None
    if subsequence_id >= subsequence_count:
        errors.append(
            f"{field_name}.subsequence_id must be less than subsequence_count"
        )
        return None
    if sequence_alias is not _MISSING and sequence_alias != parent:
        errors.append(
            f"{field_name}.sequence_id must equal parent_sequence_id"
        )
        return None
    return parent, subsequence_id, subsequence_count


def _subsegment_keys(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
) -> list[tuple[int, int, int]] | None:
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list")
        return None
    keys: list[tuple[int, int, int]] = []
    valid = True
    for index, item in enumerate(value):
        key = _subsegment_key(
            item,
            field_name=f"{field_name}[{index}]",
            errors=errors,
        )
        if key is None:
            valid = False
        else:
            keys.append(key)
    return keys if valid else None


def _audio_frame_key(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
    sequence_alias: Any = _MISSING,
) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        errors.append(f"{field_name} must be an object")
        return None
    parent = value.get("parent_sequence_id")
    frame_id = value.get("audio_frame_id")
    if (
        not isinstance(parent, int)
        or isinstance(parent, bool)
        or parent < 0
    ):
        errors.append(f"{field_name}.parent_sequence_id is invalid")
        return None
    if (
        not isinstance(frame_id, int)
        or isinstance(frame_id, bool)
        or frame_id < 0
    ):
        errors.append(f"{field_name}.audio_frame_id is invalid")
        return None
    if sequence_alias is not _MISSING and sequence_alias != parent:
        errors.append(
            f"{field_name}.sequence_id must equal parent_sequence_id"
        )
        return None
    return parent, frame_id


def _audio_frame_keys(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
) -> list[tuple[int, int]] | None:
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list")
        return None
    keys: list[tuple[int, int]] = []
    valid = True
    for index, item in enumerate(value):
        key = _audio_frame_key(
            item,
            field_name=f"{field_name}[{index}]",
            errors=errors,
        )
        if key is None:
            valid = False
        else:
            keys.append(key)
    return keys if valid else None


def _positive_int_list(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
) -> list[int] | None:
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list")
        return None
    if any(not _positive_int(item) for item in value):
        errors.append(f"{field_name} must contain positive integers")
        return None
    return list(value)


def _parent_summaries(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
) -> list[dict[str, int | bool]] | None:
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list")
        return None
    resolved: list[dict[str, int]] = []
    valid = True
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{field_name}[{index}] must be an object")
            valid = False
            continue
        parent = item.get("parent_sequence_id")
        frame_count = item.get("audio_frame_count")
        audio_bytes = item.get("audio_bytes")
        retry_count = item.get("retry_count")
        atomic_fallback_applied = item.get(
            "atomic_fallback_applied",
            False,
        )
        if (
            not isinstance(parent, int)
            or isinstance(parent, bool)
            or parent < 0
        ):
            errors.append(
                f"{field_name}[{index}].parent_sequence_id is invalid"
            )
            valid = False
            continue
        if not _positive_int(frame_count):
            errors.append(
                f"{field_name}[{index}].audio_frame_count is invalid"
            )
            valid = False
            continue
        if not _positive_int(audio_bytes):
            errors.append(
                f"{field_name}[{index}].audio_bytes is invalid"
            )
            valid = False
            continue
        if (
            not isinstance(retry_count, int)
            or isinstance(retry_count, bool)
            or retry_count not in {0, 1}
        ):
            errors.append(
                f"{field_name}[{index}].retry_count must be zero or one"
            )
            valid = False
            continue
        if not isinstance(atomic_fallback_applied, bool):
            errors.append(
                f"{field_name}[{index}].atomic_fallback_applied must be "
                "a boolean"
            )
            valid = False
            continue
        resolved.append(
            {
                "parent_sequence_id": parent,
                "audio_frame_count": frame_count,
                "audio_bytes": audio_bytes,
                "retry_count": retry_count,
                "atomic_fallback_applied": atomic_fallback_applied,
            }
        )
    return resolved if valid else None


def _validate_complete_subsegment_order(
    keys: list[tuple[int, int, int]] | None,
    completed_parent_ids: list[int] | None,
    *,
    field_name: str,
    errors: list[str],
) -> None:
    """Require lexical parent/child order with one stable count per parent."""
    if keys is None or completed_parent_ids is None:
        return
    counts: dict[int, int] = {}
    parent_order: list[int] = []
    for parent, _subsequence_id, count in keys:
        if parent not in counts:
            counts[parent] = count
            parent_order.append(parent)
        elif counts[parent] != count:
            errors.append(
                f"{field_name} subsequence_count changed within parent {parent}"
            )
            return

    expected: list[tuple[int, int, int]] = []
    for parent in completed_parent_ids:
        count = counts.get(parent)
        if count is None:
            errors.append(
                f"{field_name} has no subsegments for completed parent {parent}"
            )
            return
        expected.extend((parent, child, count) for child in range(count))

    if parent_order != completed_parent_ids or keys != expected:
        errors.append(
            f"{field_name} must contain every parent in ordered contiguous "
            "subsequence order"
        )


def _validate_staged_pipeline_integrity_v2(
    staged_pipeline: dict[str, Any],
    staged_config: dict[str, Any] | None,
    websocket_receive_events: Any,
    input_end_timestamp_ms: Any,
    errors: list[str],
) -> list[str]:
    """Validate the explicit composite-child telemetry contract."""
    if staged_pipeline.get("state") != "closed":
        errors.append(
            "staged state must be 'closed' "
            f"(got {staged_pipeline.get('state')!r})"
        )
    if staged_pipeline.get("outcome") != "complete":
        errors.append(
            "staged outcome must be 'complete' "
            f"(got {staged_pipeline.get('outcome')!r})"
        )
    if staged_pipeline.get("failure") is not None:
        errors.append("staged failure must be null")
    cleanup_errors = staged_pipeline.get("cleanup_errors")
    if not isinstance(cleanup_errors, list):
        errors.append("cleanup_errors must be a list")
    elif cleanup_errors:
        errors.append("cleanup_errors must be empty")

    received_pcm_bytes: list[int] = []
    if not isinstance(websocket_receive_events, list):
        errors.append("websocket_receive_events must be a list")
    else:
        completed_orders: list[int] = []
        completed_timestamps: list[float] = []
        observed_orders: list[int] = []
        valid_orders = True
        for index, event in enumerate(websocket_receive_events):
            if not isinstance(event, dict):
                errors.append(
                    f"websocket_receive_events[{index}] must be an object"
                )
                valid_orders = False
                continue
            order = event.get("order")
            if (
                not isinstance(order, int)
                or isinstance(order, bool)
                or order < 0
            ):
                errors.append(
                    f"websocket_receive_events[{index}].order is invalid"
                )
                valid_orders = False
                continue
            observed_orders.append(order)
            if event.get("frame_type") == "pcm":
                audio_bytes = event.get("audio_bytes")
                if not _positive_int(audio_bytes):
                    errors.append(
                        f"websocket_receive_events[{index}].audio_bytes is invalid"
                    )
                else:
                    received_pcm_bytes.append(audio_bytes)
            if (
                event.get("frame_type") == "control"
                and event.get("message_type") == "status"
                and event.get("status") == "completed"
            ):
                completed_orders.append(order)
                timestamp_ms = event.get("timestamp_ms")
                if (
                    not isinstance(timestamp_ms, (int, float))
                    or isinstance(timestamp_ms, bool)
                    or not math.isfinite(timestamp_ms)
                    or timestamp_ms < 0
                ):
                    errors.append(
                        f"websocket_receive_events[{index}].timestamp_ms is invalid"
                    )
                else:
                    completed_timestamps.append(float(timestamp_ms))

        if valid_orders and observed_orders != list(range(len(observed_orders))):
            errors.append(
                "websocket_receive_events order must be contiguous from zero"
            )
        if len(completed_orders) != 1:
            errors.append(
                "exactly one completed WebSocket terminal is required "
                f"(got {len(completed_orders)})"
            )
        elif any(
            isinstance(event, dict)
            and event.get("frame_type") == "pcm"
            and isinstance(event.get("order"), int)
            and event["order"] > completed_orders[0]
            for event in websocket_receive_events
        ):
            errors.append("PCM was received after the completed WebSocket terminal")
        if (
            not isinstance(input_end_timestamp_ms, (int, float))
            or isinstance(input_end_timestamp_ms, bool)
            or not math.isfinite(input_end_timestamp_ms)
            or input_end_timestamp_ms <= 0
        ):
            errors.append("input_end_timestamp_ms must be positive and finite")
        elif len(completed_timestamps) == 1 and (
            completed_timestamps[0] < float(input_end_timestamp_ms)
        ):
            errors.append("completed WebSocket terminal arrived before end_input")

    incomplete = _sequence_ids(
        staged_pipeline.get("incomplete_sequence_ids"),
        field_name="incomplete_sequence_ids",
        errors=errors,
    )
    if incomplete:
        errors.append(f"incomplete sequence IDs remain: {incomplete}")
    completed = _sequence_ids(
        staged_pipeline.get("completed_sequence_ids"),
        field_name="completed_sequence_ids",
        errors=errors,
    )
    websocket_sent = _sequence_ids(
        staged_pipeline.get("websocket_sent_sequence_ids"),
        field_name="websocket_sent_sequence_ids",
        errors=errors,
    )
    if completed is not None:
        expected_parents = list(range(len(completed)))
        if completed != expected_parents:
            errors.append(
                "completed_sequence_ids must be contiguous and ordered from zero "
                f"(got {completed})"
            )
    if (
        completed is not None
        and websocket_sent is not None
        and websocket_sent != completed
    ):
        errors.append(
            "websocket_sent_sequence_ids must exactly match completed_sequence_ids"
        )

    planned = _subsegment_keys(
        staged_pipeline.get("planned_subsegment_keys"),
        field_name="planned_subsegment_keys",
        errors=errors,
    )
    synthesized = _subsegment_keys(
        staged_pipeline.get("synthesized_subsegment_keys"),
        field_name="synthesized_subsegment_keys",
        errors=errors,
    )
    completed_subsegments = _subsegment_keys(
        staged_pipeline.get("completed_subsegment_keys"),
        field_name="completed_subsegment_keys",
        errors=errors,
    )
    incomplete_subsegments = _subsegment_keys(
        staged_pipeline.get("incomplete_subsegment_keys"),
        field_name="incomplete_subsegment_keys",
        errors=errors,
    )
    websocket_sent_subsegments = _subsegment_keys(
        staged_pipeline.get("websocket_sent_subsegment_keys"),
        field_name="websocket_sent_subsegment_keys",
        errors=errors,
    )
    _validate_complete_subsegment_order(
        planned,
        completed,
        field_name="planned_subsegment_keys",
        errors=errors,
    )
    for field_name, observed in (
        ("synthesized_subsegment_keys", synthesized),
        ("completed_subsegment_keys", completed_subsegments),
        ("websocket_sent_subsegment_keys", websocket_sent_subsegments),
    ):
        if planned is not None and observed is not None and observed != planned:
            errors.append(f"{field_name} must exactly match planned_subsegment_keys")
    if incomplete_subsegments:
        errors.append(
            "incomplete_subsegment_keys must be empty for a complete pipeline"
        )

    parent_count = len(completed) if completed is not None else None
    child_count = len(planned) if planned is not None else None
    segments_emitted = staged_pipeline.get("segments_emitted")
    if (
        parent_count is not None
        and (
            not isinstance(segments_emitted, int)
            or isinstance(segments_emitted, bool)
            or segments_emitted != parent_count
        )
    ):
        errors.append(
            "segments_emitted must equal the completed parent count "
            f"({parent_count})"
        )
    for count_name in (
        "tts_subsegments_planned",
        "tts_subsegments_produced",
        "audio_segments_produced",
    ):
        count = staged_pipeline.get(count_name)
        if (
            child_count is not None
            and (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count != child_count
            )
        ):
            errors.append(
                f"{count_name} must equal the completed subsegment count "
                f"({child_count})"
            )

    if staged_pipeline.get("tts_subsegmentation_enabled") is not True:
        errors.append("schema-v2 telemetry requires tts_subsegmentation_enabled=true")
    max_chars = None
    min_chars = None
    if staged_config is None:
        errors.append("/api/config.stagedConfig must be an object for schema v2")
    else:
        max_chars = staged_config.get("ttsSubsegmentMaxChars")
        min_chars = staged_config.get("ttsSubsegmentMinChars")
        if not _positive_int(max_chars):
            errors.append(
                "/api/config.stagedConfig.ttsSubsegmentMaxChars must be "
                "a positive integer for schema v2"
            )
            max_chars = None
        if not _positive_int(min_chars):
            errors.append(
                "/api/config.stagedConfig.ttsSubsegmentMinChars must be "
                "a positive integer"
            )
            min_chars = None
    if (
        max_chars is not None
        and staged_pipeline.get("tts_subsegment_max_chars") != max_chars
    ):
        errors.append(
            "staged tts_subsegment_max_chars must match "
            "/api/config.stagedConfig.ttsSubsegmentMaxChars"
        )
    if (
        min_chars is not None
        and staged_pipeline.get("tts_subsegment_min_chars") != min_chars
    ):
        errors.append(
            "staged tts_subsegment_min_chars must match "
            "/api/config.stagedConfig.ttsSubsegmentMinChars"
        )

    websocket_events = staged_pipeline.get("websocket_send_events")
    websocket_event_keys: list[tuple[int, int, int]] | None = []
    websocket_event_audio_bytes: list[int] = []
    if not isinstance(websocket_events, list):
        errors.append("websocket_send_events must be a list")
        websocket_event_keys = None
    else:
        valid_websocket_events = True
        for index, event in enumerate(websocket_events):
            if not isinstance(event, dict):
                errors.append(f"websocket_send_events[{index}] must be an object")
                valid_websocket_events = False
                continue
            key = _subsegment_key(
                event,
                field_name=f"websocket_send_events[{index}]",
                errors=errors,
                sequence_alias=event.get("sequence_id"),
            )
            if key is None:
                valid_websocket_events = False
            else:
                websocket_event_keys.append(key)
            audio_bytes = event.get("audio_bytes")
            if not _positive_int(audio_bytes):
                errors.append(
                    f"websocket_send_events[{index}].audio_bytes is invalid"
                )
                valid_websocket_events = False
            else:
                websocket_event_audio_bytes.append(audio_bytes)
            sent_ms = event.get("sent_monotonic_ms")
            if (
                not isinstance(sent_ms, (int, float))
                or isinstance(sent_ms, bool)
                or not math.isfinite(sent_ms)
                or sent_ms < 0
            ):
                errors.append(
                    f"websocket_send_events[{index}].sent_monotonic_ms is invalid"
                )
                valid_websocket_events = False
        if not valid_websocket_events:
            websocket_event_keys = None

    if (
        websocket_event_keys is not None
        and websocket_sent_subsegments is not None
        and websocket_event_keys != websocket_sent_subsegments
    ):
        errors.append(
            "websocket_send_events composite order must exactly match "
            "websocket_sent_subsegment_keys"
        )
    if isinstance(websocket_receive_events, list):
        if len(received_pcm_bytes) != len(websocket_event_audio_bytes):
            errors.append(
                "WebSocket PCM receive count must exactly match successful "
                "send count "
                f"({len(received_pcm_bytes)} != "
                f"{len(websocket_event_audio_bytes)})"
            )
        elif received_pcm_bytes != websocket_event_audio_bytes:
            errors.append(
                "WebSocket PCM received bytes must exactly match successful "
                "send bytes frame-by-frame"
            )

    events = staged_pipeline.get("events")
    if not isinstance(events, list):
        errors.append("staged events must be a list")
        events = []
    parent_event_sequences: dict[tuple[str, str], list[int]] = {
        ("segmenter", "emitted"): [],
        ("nmt", "completed"): [],
    }
    child_event_records: dict[
        tuple[str, str],
        list[tuple[tuple[int, int, int], dict[str, Any], int]],
    ] = {key: [] for key in _SUBSEGMENT_LIFECYCLE_EVENTS}
    nmt_completed_chars: dict[int, int] = {}
    nmt_event_retry_total = 0
    tts_event_retry_total = 0
    tts_retry_observed = False
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{index}] must be an object")
            continue
        event_name = (event.get("stage"), event.get("event"))
        sequence_id = event.get("sequence_id")
        if event_name in parent_event_sequences:
            if (
                not isinstance(sequence_id, int)
                or isinstance(sequence_id, bool)
                or sequence_id < 0
            ):
                errors.append(f"events[{index}].sequence_id is invalid")
            else:
                parent_event_sequences[event_name].append(sequence_id)
                if event_name == ("nmt", "completed"):
                    text_chars = event.get("text_chars")
                    if not _positive_int(text_chars):
                        errors.append(f"events[{index}].text_chars is invalid")
                    elif sequence_id in nmt_completed_chars:
                        errors.append(
                            f"duplicate nmt/completed event for parent {sequence_id}"
                        )
                    else:
                        nmt_completed_chars[sequence_id] = text_chars

        if (
            event_name in child_event_records
            and not _is_terminal_output_dequeue(event_name, event)
        ):
            key = _subsegment_key(
                event,
                field_name=f"events[{index}]",
                errors=errors,
                sequence_alias=sequence_id,
            )
            if key is not None:
                child_event_records[event_name].append((key, event, index))

        if event_name in {
            ("nmt", "completed"),
            ("nmt", "error"),
            ("tts", "completed"),
            ("tts", "error"),
        }:
            retry_count = event.get("retry_count")
            if (
                not isinstance(retry_count, int)
                or isinstance(retry_count, bool)
                or retry_count not in {0, 1}
            ):
                errors.append(
                    f"events[{index}].retry_count must be zero or one"
                )
            elif event_name == ("nmt", "completed"):
                nmt_event_retry_total += retry_count
            elif event_name == ("tts", "completed"):
                tts_event_retry_total += retry_count
                tts_retry_observed = tts_retry_observed or retry_count == 1
            elif event_name == ("tts", "error"):
                tts_retry_observed = tts_retry_observed or retry_count == 1

        queue_depth = event.get("queue_depth")
        queue_capacity = event.get("queue_capacity")
        if queue_depth is None and queue_capacity is None:
            continue
        if (
            not isinstance(queue_depth, int)
            or isinstance(queue_depth, bool)
            or queue_depth < 0
        ):
            errors.append(f"events[{index}].queue_depth is invalid")
            continue
        if not _positive_int(queue_capacity):
            errors.append(f"events[{index}].queue_capacity is invalid")
            continue
        if queue_depth > queue_capacity:
            errors.append(
                f"events[{index}] queue depth {queue_depth} exceeds "
                f"capacity {queue_capacity}"
            )

    if completed is not None:
        for (stage, event_name), observed in parent_event_sequences.items():
            if observed != completed:
                errors.append(
                    f"{stage}/{event_name} parent sequence order must exactly "
                    "match completed_sequence_ids"
                )
    if planned is not None:
        for (stage, event_name), records in child_event_records.items():
            observed = [record[0] for record in records]
            if observed != planned:
                errors.append(
                    f"{stage}/{event_name} composite order must exactly match "
                    "planned_subsegment_keys"
                )

    child_char_maps: dict[
        tuple[str, str], dict[tuple[int, int, int], int]
    ] = {}
    for event_name in (
        ("target_splitter", "emitted"),
        ("tts", "enqueued"),
        ("tts", "started"),
    ):
        child_chars: dict[tuple[int, int, int], int] = {}
        for key, event, index in child_event_records[event_name]:
            text_chars = event.get("text_chars")
            if not _positive_int(text_chars):
                errors.append(f"events[{index}].text_chars is invalid")
                continue
            if max_chars is not None and text_chars > max_chars:
                errors.append(
                    f"events[{index}].text_chars exceeds configured "
                    f"ttsSubsegmentMaxChars={max_chars}"
                )
            child_chars[key] = text_chars
        child_char_maps[event_name] = child_chars
    splitter_chars = child_char_maps[("target_splitter", "emitted")]
    for event_name in (("tts", "enqueued"), ("tts", "started")):
        if child_char_maps[event_name] != splitter_chars:
            errors.append(
                f"{event_name[0]}/{event_name[1]} text_chars must exactly "
                "match target_splitter/emitted"
            )
    for event_name in (
        ("target_splitter", "emitted"),
        ("tts", "started"),
    ):
        for key, event, index in child_event_records[event_name]:
            parent_chars = event.get("parent_text_chars")
            expected_parent_chars = nmt_completed_chars.get(key[0])
            if not _positive_int(parent_chars):
                errors.append(
                    f"events[{index}].parent_text_chars is invalid"
                )
            elif (
                expected_parent_chars is None
                or parent_chars != expected_parent_chars
            ):
                errors.append(
                    f"events[{index}].parent_text_chars must match "
                    "nmt/completed.text_chars"
                )

    lifecycle_audio_bytes: dict[tuple[str, str], list[int]] = {}
    for event_name in (
        ("tts", "completed"),
        ("output", "enqueued"),
        ("output", "dequeued"),
    ):
        values: list[int] = []
        for _key, event, index in child_event_records[event_name]:
            audio_bytes = event.get("audio_bytes")
            if not _positive_int(audio_bytes):
                errors.append(f"events[{index}].audio_bytes is invalid")
            else:
                values.append(audio_bytes)
        lifecycle_audio_bytes[event_name] = values
    tts_audio_bytes = lifecycle_audio_bytes[("tts", "completed")]
    for event_name in (("output", "enqueued"), ("output", "dequeued")):
        if lifecycle_audio_bytes[event_name] != tts_audio_bytes:
            errors.append(
                f"{event_name[0]}/{event_name[1]} audio_bytes must exactly "
                "match tts/completed"
            )
    if websocket_event_audio_bytes != tts_audio_bytes:
        errors.append(
            "websocket_send_events audio_bytes must exactly match "
            "tts/completed frame-by-frame"
        )

    nmt_retry_count = staged_pipeline.get("nmt_retry_count")
    if (
        not isinstance(nmt_retry_count, int)
        or isinstance(nmt_retry_count, bool)
        or nmt_retry_count < 0
    ):
        errors.append("nmt_retry_count must be a non-negative integer")
    elif nmt_retry_count != nmt_event_retry_total:
        errors.append(
            "nmt_retry_count must equal the sum of nmt/completed event retries "
            f"({nmt_retry_count} != {nmt_event_retry_total})"
        )
    tts_retry_count = staged_pipeline.get("tts_retry_count")
    if (
        not isinstance(tts_retry_count, int)
        or isinstance(tts_retry_count, bool)
        or tts_retry_count < 0
    ):
        errors.append("tts_retry_count must be a non-negative integer")
    elif tts_retry_count != tts_event_retry_total:
        errors.append(
            "tts_retry_count must equal the sum of tts/completed event retries "
            f"({tts_retry_count} != {tts_event_retry_total})"
        )

    if staged_config is not None:
        tts_max_retries = staged_config.get("ttsMaxRetries")
        if (
            not isinstance(tts_max_retries, int)
            or isinstance(tts_max_retries, bool)
            or tts_max_retries not in {0, 1}
        ):
            errors.append(
                "/api/config.stagedConfig.ttsMaxRetries must be zero or one"
            )
        elif tts_max_retries == 0 and tts_retry_observed:
            errors.append(
                "TTS retry telemetry is incompatible with "
                "/api/config.stagedConfig.ttsMaxRetries=0"
            )

    max_depths = staged_pipeline.get("max_queue_depths")
    if not isinstance(max_depths, dict):
        errors.append("max_queue_depths must be an object")
    elif staged_config is not None:
        for queue_name, config_key in {
            "nmt": "nmtQueueMaxSize",
            "tts": "ttsQueueMaxSize",
            "output": "outputQueueMaxSize",
        }.items():
            capacity = staged_config.get(config_key)
            depth = max_depths.get(queue_name)
            if not _positive_int(capacity):
                errors.append(
                    f"/api/config.stagedConfig.{config_key} must be a positive integer"
                )
                continue
            if (
                not isinstance(depth, int)
                or isinstance(depth, bool)
                or depth < 0
            ):
                errors.append(f"max_queue_depths.{queue_name} is invalid")
                continue
            if depth > capacity:
                errors.append(
                    f"max_queue_depths.{queue_name}={depth} exceeds configured "
                    f"capacity {capacity}"
                )
    return errors


def _validate_staged_pipeline_integrity_v3(
    staged_pipeline: dict[str, Any],
    staged_config: dict[str, Any] | None,
    websocket_receive_events: Any,
    input_end_timestamp_ms: Any,
    errors: list[str],
) -> list[str]:
    """Validate ordered frame publication and parent-completion barriers."""
    configured_fallback_max_chars = 0
    if staged_pipeline.get("state") != "closed":
        errors.append(
            "staged state must be 'closed' "
            f"(got {staged_pipeline.get('state')!r})"
        )
    if staged_pipeline.get("outcome") != "complete":
        errors.append(
            "staged outcome must be 'complete' "
            f"(got {staged_pipeline.get('outcome')!r})"
        )
    if staged_pipeline.get("failure") is not None:
        errors.append("staged failure must be null")
    cleanup_errors = staged_pipeline.get("cleanup_errors")
    if not isinstance(cleanup_errors, list):
        errors.append("cleanup_errors must be a list")
    elif cleanup_errors:
        errors.append("cleanup_errors must be empty")

    if staged_pipeline.get("tts_incremental_publish_enabled") is not True:
        errors.append(
            "schema-v3 telemetry requires tts_incremental_publish_enabled=true"
        )
    if staged_pipeline.get("tts_subsegmentation_enabled") is not False:
        errors.append(
            "schema-v3 telemetry requires tts_subsegmentation_enabled=false"
        )
    if staged_config is None:
        errors.append("/api/config.stagedConfig must be an object for schema v3")
    else:
        if staged_config.get("ttsIncrementalPublishEnabled") is not True:
            errors.append(
                "schema-v3 config requires ttsIncrementalPublishEnabled=true"
            )
        frame_ms = staged_config.get("ttsIncrementalFrameMs")
        if not _positive_int(frame_ms):
            errors.append(
                "/api/config.stagedConfig.ttsIncrementalFrameMs must be "
                "a positive integer"
            )
        elif staged_pipeline.get("tts_incremental_frame_ms") != frame_ms:
            errors.append(
                "staged tts_incremental_frame_ms must match "
                "/api/config.stagedConfig.ttsIncrementalFrameMs"
            )
        cap = staged_config.get("ttsSubsegmentMaxChars")
        if (
            not isinstance(cap, int)
            or isinstance(cap, bool)
            or cap != 0
        ):
            errors.append(
                "schema-v3 config requires ttsSubsegmentMaxChars=0"
            )
        configured_fallback_max_chars = staged_config.get(
            "ttsIncrementalAtomicFallbackMaxChars",
            0,
        )
        if (
            not isinstance(configured_fallback_max_chars, int)
            or isinstance(configured_fallback_max_chars, bool)
            or configured_fallback_max_chars < 0
        ):
            errors.append(
                "/api/config.stagedConfig."
                "ttsIncrementalAtomicFallbackMaxChars must be a "
                "non-negative integer"
            )
            configured_fallback_max_chars = 0

    fallback_max_chars = staged_pipeline.get(
        "tts_incremental_atomic_fallback_max_chars",
        0,
    )
    if (
        not isinstance(fallback_max_chars, int)
        or isinstance(fallback_max_chars, bool)
        or fallback_max_chars < 0
    ):
        errors.append(
            "tts_incremental_atomic_fallback_max_chars must be a "
            "non-negative integer"
        )
        fallback_max_chars = 0
    elif fallback_max_chars != configured_fallback_max_chars:
        errors.append(
            "staged tts_incremental_atomic_fallback_max_chars must match "
            "/api/config.stagedConfig."
            "ttsIncrementalAtomicFallbackMaxChars"
        )
    fallback_parent_count = staged_pipeline.get(
        "tts_incremental_atomic_fallback_parent_count",
        0,
    )
    if (
        not isinstance(fallback_parent_count, int)
        or isinstance(fallback_parent_count, bool)
        or fallback_parent_count < 0
    ):
        errors.append(
            "tts_incremental_atomic_fallback_parent_count must be a "
            "non-negative integer"
        )
        fallback_parent_count = 0
    fallback_parent_ids = _sequence_ids(
        staged_pipeline.get(
            "tts_incremental_atomic_fallback_parent_sequence_ids",
            [],
        ),
        field_name=(
            "tts_incremental_atomic_fallback_parent_sequence_ids"
        ),
        errors=errors,
    )
    if (
        fallback_parent_ids is not None
        and fallback_parent_count != len(fallback_parent_ids)
    ):
        errors.append(
            "tts_incremental_atomic_fallback_parent_count must equal "
            "the fallback parent sequence ID count"
        )

    received_pcm_bytes: list[int] = []
    if not isinstance(websocket_receive_events, list):
        errors.append("websocket_receive_events must be a list")
    else:
        observed_orders: list[int] = []
        completed_orders: list[int] = []
        completed_timestamps: list[float] = []
        valid_orders = True
        for index, event in enumerate(websocket_receive_events):
            if not isinstance(event, dict):
                errors.append(
                    f"websocket_receive_events[{index}] must be an object"
                )
                valid_orders = False
                continue
            order = event.get("order")
            if (
                not isinstance(order, int)
                or isinstance(order, bool)
                or order < 0
            ):
                errors.append(
                    f"websocket_receive_events[{index}].order is invalid"
                )
                valid_orders = False
                continue
            observed_orders.append(order)
            if event.get("frame_type") == "pcm":
                audio_bytes = event.get("audio_bytes")
                if not _positive_int(audio_bytes):
                    errors.append(
                        f"websocket_receive_events[{index}].audio_bytes is invalid"
                    )
                else:
                    received_pcm_bytes.append(audio_bytes)
            if (
                event.get("frame_type") == "control"
                and event.get("message_type") == "status"
                and event.get("status") == "completed"
            ):
                completed_orders.append(order)
                timestamp_ms = event.get("timestamp_ms")
                if (
                    not isinstance(timestamp_ms, (int, float))
                    or isinstance(timestamp_ms, bool)
                    or not math.isfinite(timestamp_ms)
                    or timestamp_ms < 0
                ):
                    errors.append(
                        f"websocket_receive_events[{index}].timestamp_ms is invalid"
                    )
                else:
                    completed_timestamps.append(float(timestamp_ms))
        if valid_orders and observed_orders != list(range(len(observed_orders))):
            errors.append(
                "websocket_receive_events order must be contiguous from zero"
            )
        if len(completed_orders) != 1:
            errors.append(
                "exactly one completed WebSocket terminal is required "
                f"(got {len(completed_orders)})"
            )
        elif any(
            isinstance(event, dict)
            and event.get("frame_type") == "pcm"
            and isinstance(event.get("order"), int)
            and event["order"] > completed_orders[0]
            for event in websocket_receive_events
        ):
            errors.append("PCM was received after the completed WebSocket terminal")
        if (
            not isinstance(input_end_timestamp_ms, (int, float))
            or isinstance(input_end_timestamp_ms, bool)
            or not math.isfinite(input_end_timestamp_ms)
            or input_end_timestamp_ms <= 0
        ):
            errors.append("input_end_timestamp_ms must be positive and finite")
        elif (
            len(completed_timestamps) == 1
            and completed_timestamps[0] < float(input_end_timestamp_ms)
        ):
            errors.append("completed WebSocket terminal arrived before end_input")

    incomplete = _sequence_ids(
        staged_pipeline.get("incomplete_sequence_ids"),
        field_name="incomplete_sequence_ids",
        errors=errors,
    )
    if incomplete:
        errors.append(f"incomplete sequence IDs remain: {incomplete}")
    completed = _sequence_ids(
        staged_pipeline.get("completed_sequence_ids"),
        field_name="completed_sequence_ids",
        errors=errors,
    )
    websocket_sent = _sequence_ids(
        staged_pipeline.get("websocket_sent_sequence_ids"),
        field_name="websocket_sent_sequence_ids",
        errors=errors,
    )
    if completed is not None:
        expected = list(range(len(completed)))
        if completed != expected:
            errors.append(
                "completed_sequence_ids must be contiguous and ordered from zero "
                f"(got {completed})"
            )
        if (
            fallback_parent_ids is not None
            and (
                fallback_parent_ids != sorted(set(fallback_parent_ids))
                or any(
                    parent_id not in completed
                    for parent_id in fallback_parent_ids
                )
            )
        ):
            errors.append(
                "tts_incremental_atomic_fallback_parent_sequence_ids "
                "must be unique, ordered, and completed"
            )
    if (
        completed is not None
        and websocket_sent is not None
        and websocket_sent != completed
    ):
        errors.append(
            "websocket_sent_sequence_ids must exactly match completed_sequence_ids"
        )
    for count_name in ("segments_emitted", "audio_segments_produced"):
        count = staged_pipeline.get(count_name)
        if (
            completed is not None
            and (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count != len(completed)
            )
        ):
            errors.append(
                f"{count_name} must equal the completed sequence count "
                f"({len(completed)})"
            )

    frame_key_fields = (
        "published_audio_frame_keys",
        "dequeued_audio_frame_keys",
        "websocket_sent_audio_frame_keys",
    )
    frame_keys = {
        field: _audio_frame_keys(
            staged_pipeline.get(field),
            field_name=field,
            errors=errors,
        )
        for field in frame_key_fields
    }
    canonical_keys = frame_keys["published_audio_frame_keys"]
    for field in frame_key_fields[1:]:
        observed = frame_keys[field]
        if (
            canonical_keys is not None
            and observed is not None
            and observed != canonical_keys
        ):
            errors.append(
                f"{field} must exactly match published_audio_frame_keys"
            )

    frame_byte_fields = (
        "published_audio_frame_bytes",
        "dequeued_audio_frame_bytes",
        "websocket_sent_audio_frame_bytes",
    )
    frame_bytes = {
        field: _positive_int_list(
            staged_pipeline.get(field),
            field_name=field,
            errors=errors,
        )
        for field in frame_byte_fields
    }
    canonical_bytes = frame_bytes["published_audio_frame_bytes"]
    for field in frame_byte_fields[1:]:
        observed = frame_bytes[field]
        if (
            canonical_bytes is not None
            and observed is not None
            and observed != canonical_bytes
        ):
            errors.append(
                f"{field} must exactly match published_audio_frame_bytes"
            )
    if (
        canonical_keys is not None
        and canonical_bytes is not None
        and len(canonical_keys) != len(canonical_bytes)
    ):
        errors.append(
            "published frame identity and byte lists must have equal length"
        )
    frame_count = staged_pipeline.get("audio_frames_produced")
    if (
        canonical_keys is not None
        and (
            not isinstance(frame_count, int)
            or isinstance(frame_count, bool)
            or frame_count != len(canonical_keys)
        )
    ):
        errors.append(
            "audio_frames_produced must equal published frame count"
        )

    parent_fields = (
        "produced_parent_summaries",
        "completed_parent_summaries",
        "websocket_completed_parent_summaries",
    )
    parent_summaries = {
        field: _parent_summaries(
            staged_pipeline.get(field),
            field_name=field,
            errors=errors,
        )
        for field in parent_fields
    }
    canonical_parents = parent_summaries["produced_parent_summaries"]
    for field in parent_fields[1:]:
        observed = parent_summaries[field]
        if (
            canonical_parents is not None
            and observed is not None
            and observed != canonical_parents
        ):
            errors.append(
                f"{field} must exactly match produced_parent_summaries"
            )
    if canonical_parents is not None and completed is not None:
        parent_ids = [
            item["parent_sequence_id"] for item in canonical_parents
        ]
        if parent_ids != completed:
            errors.append(
                "parent completion summaries must exactly match "
                "completed_sequence_ids"
            )
        flagged_parent_ids = [
            item["parent_sequence_id"]
            for item in canonical_parents
            if item["atomic_fallback_applied"]
        ]
        if (
            fallback_parent_ids is not None
            and flagged_parent_ids != fallback_parent_ids
        ):
            errors.append(
                "parent atomic_fallback_applied flags must exactly match "
                "tts_incremental_atomic_fallback_parent_sequence_ids"
            )

    if (
        canonical_keys is not None
        and canonical_bytes is not None
        and canonical_parents is not None
    ):
        offset = 0
        expected_keys: list[tuple[int, int]] = []
        for summary in canonical_parents:
            parent = summary["parent_sequence_id"]
            count = summary["audio_frame_count"]
            sizes = canonical_bytes[offset : offset + count]
            expected_keys.extend((parent, frame_id) for frame_id in range(count))
            if len(sizes) != count or sum(sizes) != summary["audio_bytes"]:
                errors.append(
                    f"parent {parent} frame bytes do not match its completion"
                )
            offset += count
        if canonical_keys != expected_keys:
            errors.append(
                "published audio frame keys must be contiguous within every parent"
            )
        if offset != len(canonical_bytes):
            errors.append(
                "published frame lists contain bytes outside parent completions"
            )

    websocket_events = staged_pipeline.get("websocket_send_events")
    websocket_event_keys: list[tuple[int, int]] | None = []
    websocket_event_bytes: list[int] = []
    websocket_event_times: list[float] = []
    if not isinstance(websocket_events, list):
        errors.append("websocket_send_events must be a list")
        websocket_event_keys = None
    else:
        valid = True
        for index, event in enumerate(websocket_events):
            key = _audio_frame_key(
                event,
                field_name=f"websocket_send_events[{index}]",
                errors=errors,
                sequence_alias=(
                    event.get("sequence_id")
                    if isinstance(event, dict)
                    else _MISSING
                ),
            )
            if key is None:
                valid = False
            else:
                websocket_event_keys.append(key)
            audio_bytes = event.get("audio_bytes") if isinstance(event, dict) else None
            if not _positive_int(audio_bytes):
                errors.append(
                    f"websocket_send_events[{index}].audio_bytes is invalid"
                )
                valid = False
            else:
                websocket_event_bytes.append(audio_bytes)
            sent_ms = (
                event.get("sent_monotonic_ms")
                if isinstance(event, dict)
                else None
            )
            if (
                not isinstance(sent_ms, (int, float))
                or isinstance(sent_ms, bool)
                or not math.isfinite(sent_ms)
                or sent_ms < 0
            ):
                errors.append(
                    f"websocket_send_events[{index}].sent_monotonic_ms is invalid"
                )
                valid = False
            else:
                websocket_event_times.append(float(sent_ms))
        if not valid:
            websocket_event_keys = None
    if (
        websocket_event_keys is not None
        and canonical_keys is not None
        and websocket_event_keys != canonical_keys
    ):
        errors.append(
            "websocket_send_events frame order must exactly match "
            "published_audio_frame_keys"
        )
    if (
        canonical_bytes is not None
        and websocket_event_bytes != canonical_bytes
    ):
        errors.append(
            "websocket_send_events audio bytes must exactly match "
            "published_audio_frame_bytes"
        )
    if isinstance(websocket_receive_events, list):
        if received_pcm_bytes != websocket_event_bytes:
            errors.append(
                "WebSocket PCM received bytes must exactly match successful "
                "send bytes frame-by-frame"
            )

    events = staged_pipeline.get("events")
    if not isinstance(events, list):
        errors.append("staged events must be a list")
        events = []
    parent_event_sequences = {
        ("segmenter", "emitted"): [],
        ("nmt", "completed"): [],
        ("tts", "completed"): [],
        ("output", "parent_complete_enqueued"): [],
        ("output", "parent_complete_dequeued"): [],
    }
    frame_event_records = {
        ("tts", "frame_received"): ([], []),
        ("output", "frame_enqueued"): ([], []),
        ("output", "frame_dequeued"): ([], []),
    }
    frame_event_times = {
        event_name: [] for event_name in frame_event_records
    }
    fallback_parent_event_records = {
        ("tts", "completed"): [],
        ("output", "parent_complete_enqueued"): [],
        ("output", "parent_complete_dequeued"): [],
    }
    tts_completion_times: dict[int, float] = {}
    tts_started_text_chars: dict[int, int] = {}
    nmt_retry_total = 0
    tts_retry_total = 0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{index}] must be an object")
            continue
        event_name = (event.get("stage"), event.get("event"))
        sequence_id = event.get("sequence_id")
        if (
            event_name == ("tts", "started")
            and fallback_max_chars > 0
        ):
            text_chars = event.get("text_chars")
            if (
                not isinstance(sequence_id, int)
                or isinstance(sequence_id, bool)
                or sequence_id < 0
                or not _positive_int(text_chars)
                or sequence_id in tts_started_text_chars
            ):
                errors.append(
                    f"events[{index}] fallback policy requires one valid "
                    "tts/started text_chars record per parent"
                )
            else:
                tts_started_text_chars[sequence_id] = text_chars
        if event_name in parent_event_sequences:
            if (
                not isinstance(sequence_id, int)
                or isinstance(sequence_id, bool)
                or sequence_id < 0
            ):
                errors.append(f"events[{index}].sequence_id is invalid")
            else:
                parent_event_sequences[event_name].append(sequence_id)
                if event_name in fallback_parent_event_records:
                    fallback_applied = event.get(
                        "atomic_fallback_applied",
                        False,
                    )
                    if not isinstance(fallback_applied, bool):
                        errors.append(
                            f"events[{index}].atomic_fallback_applied "
                            "must be a boolean"
                        )
                    else:
                        fallback_parent_event_records[event_name].append(
                            (sequence_id, fallback_applied)
                        )
                    if event_name == ("tts", "completed"):
                        completed_ms = event.get("monotonic_ms")
                        if (
                            not isinstance(completed_ms, (int, float))
                            or isinstance(completed_ms, bool)
                            or not math.isfinite(completed_ms)
                            or completed_ms < 0
                        ):
                            if fallback_applied is True:
                                errors.append(
                                    f"events[{index}].monotonic_ms must be "
                                    "non-negative and finite for atomic fallback"
                                )
                        else:
                            tts_completion_times[sequence_id] = float(
                                completed_ms
                            )
        if event_name in frame_event_records:
            key = _audio_frame_key(
                event,
                field_name=f"events[{index}]",
                errors=errors,
                sequence_alias=sequence_id,
            )
            audio_bytes = event.get("audio_bytes")
            if key is not None:
                frame_event_records[event_name][0].append(key)
            if not _positive_int(audio_bytes):
                errors.append(f"events[{index}].audio_bytes is invalid")
            else:
                frame_event_records[event_name][1].append(audio_bytes)
            frame_ms = event.get("monotonic_ms")
            if (
                not isinstance(frame_ms, (int, float))
                or isinstance(frame_ms, bool)
                or not math.isfinite(frame_ms)
                or frame_ms < 0
            ):
                frame_event_times[event_name].append(None)
            else:
                frame_event_times[event_name].append(float(frame_ms))
        if event_name in {
            ("nmt", "completed"),
            ("nmt", "error"),
            ("tts", "completed"),
            ("tts", "error"),
        }:
            retry_count = event.get("retry_count")
            if (
                not isinstance(retry_count, int)
                or isinstance(retry_count, bool)
                or retry_count not in {0, 1}
            ):
                errors.append(
                    f"events[{index}].retry_count must be zero or one"
                )
            elif event_name == ("nmt", "completed"):
                nmt_retry_total += retry_count
            elif event_name == ("tts", "completed"):
                tts_retry_total += retry_count
        queue_depth = event.get("queue_depth")
        queue_capacity = event.get("queue_capacity")
        if queue_depth is None and queue_capacity is None:
            continue
        if (
            not isinstance(queue_depth, int)
            or isinstance(queue_depth, bool)
            or queue_depth < 0
        ):
            errors.append(f"events[{index}].queue_depth is invalid")
            continue
        if not _positive_int(queue_capacity):
            errors.append(f"events[{index}].queue_capacity is invalid")
            continue
        if queue_depth > queue_capacity:
            errors.append(
                f"events[{index}] queue depth {queue_depth} exceeds "
                f"capacity {queue_capacity}"
            )
    if completed is not None:
        for (stage, event_name), observed in parent_event_sequences.items():
            if observed != completed:
                errors.append(
                    f"{stage}/{event_name} parent sequence order must exactly "
                    "match completed_sequence_ids"
                )
    if canonical_keys is not None and canonical_bytes is not None:
        for (stage, event_name), (keys, sizes) in frame_event_records.items():
            if keys != canonical_keys:
                errors.append(
                    f"{stage}/{event_name} frame order must exactly match "
                    "published_audio_frame_keys"
                )
            if sizes != canonical_bytes:
                errors.append(
                    f"{stage}/{event_name} frame bytes must exactly match "
                    "published_audio_frame_bytes"
                )
    if canonical_parents is not None:
        expected_fallback_records = [
            (
                item["parent_sequence_id"],
                item["atomic_fallback_applied"],
            )
            for item in canonical_parents
        ]
        for (stage, event_name), records in (
            fallback_parent_event_records.items()
        ):
            if records != expected_fallback_records:
                errors.append(
                    f"{stage}/{event_name} atomic fallback flags must "
                    "match parent completion summaries"
                )
    fallback_parent_id_set = set(fallback_parent_ids or [])
    if fallback_max_chars > 0 and completed is not None:
        if sorted(tts_started_text_chars) != completed:
            errors.append(
                "atomic fallback policy requires tts/started text_chars "
                "for every completed parent"
            )
        else:
            policy_fallback_ids = [
                parent_id
                for parent_id in completed
                if tts_started_text_chars[parent_id]
                <= fallback_max_chars
            ]
            if policy_fallback_ids != (fallback_parent_ids or []):
                errors.append(
                    "atomic fallback parent IDs must exactly match the "
                    "configured tts/started text_chars threshold"
                )
    if (
        fallback_parent_id_set
        and canonical_keys is not None
        and websocket_event_keys == canonical_keys
        and len(websocket_event_times) == len(canonical_keys)
    ):
        for index, (parent_id, _frame_id) in enumerate(canonical_keys):
            if parent_id not in fallback_parent_id_set:
                continue
            completed_ms = tts_completion_times.get(parent_id)
            if completed_ms is None:
                continue
            if websocket_event_times[index] < completed_ms:
                errors.append(
                    f"atomic fallback parent {parent_id} WebSocket frame "
                    "was published before TTS completion"
                )
            for (stage, event_name), times in frame_event_times.items():
                if len(times) != len(canonical_keys):
                    continue
                frame_ms = times[index]
                if frame_ms is None:
                    errors.append(
                        f"{stage}/{event_name} fallback frame timestamp "
                        "must be non-negative and finite"
                    )
                elif frame_ms < completed_ms:
                    errors.append(
                        f"atomic fallback parent {parent_id} "
                        f"{stage}/{event_name} frame was published before "
                        "TTS completion"
                    )

    for summary_name, event_total in (
        ("nmt_retry_count", nmt_retry_total),
        ("tts_retry_count", tts_retry_total),
    ):
        value = staged_pipeline.get(summary_name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            errors.append(f"{summary_name} must be a non-negative integer")
        elif value != event_total:
            errors.append(
                f"{summary_name} must equal completed event retry total"
            )

    max_depths = staged_pipeline.get("max_queue_depths")
    if not isinstance(max_depths, dict):
        errors.append("max_queue_depths must be an object")
    elif staged_config is not None:
        for queue_name, config_key in {
            "nmt": "nmtQueueMaxSize",
            "tts": "ttsQueueMaxSize",
            "output": "outputQueueMaxSize",
        }.items():
            capacity = staged_config.get(config_key)
            depth = max_depths.get(queue_name)
            if not _positive_int(capacity):
                errors.append(
                    f"/api/config.stagedConfig.{config_key} must be "
                    "a positive integer"
                )
            elif (
                not isinstance(depth, int)
                or isinstance(depth, bool)
                or depth < 0
                or depth > capacity
            ):
                errors.append(
                    f"max_queue_depths.{queue_name} is outside configured capacity"
                )
    return errors


def validate_staged_pipeline_integrity(
    staged_pipeline: Any,
    backend_config: dict,
    websocket_receive_events: Any,
    input_end_timestamp_ms: Any,
) -> list[str]:
    """Return hard integrity failures for a staged pipeline export.

    The raw export is retained even when this validation fails. That lets a
    failed batch run preserve enough evidence to diagnose the exact lifecycle,
    ordering, or backpressure violation instead of discarding the trace.
    """
    errors: list[str] = []
    if not isinstance(staged_pipeline, dict):
        return ["/api/test/export.stagedPipeline must be a JSON object"]

    staged_config = (
        backend_config.get("stagedConfig")
        if isinstance(backend_config, dict)
        else None
    )
    if staged_config is not None and not isinstance(staged_config, dict):
        errors.append("/api/config.stagedConfig must be an object")
        staged_config = None
    summary_schema = _staged_telemetry_schema_version(
        staged_pipeline.get("telemetry_schema_version", _MISSING),
        field_name="telemetry_schema_version",
        errors=errors,
    )
    config_schema = _staged_telemetry_schema_version(
        (
            staged_config.get("telemetrySchemaVersion", _MISSING)
            if staged_config is not None
            else _MISSING
        ),
        field_name="/api/config.stagedConfig.telemetrySchemaVersion",
        errors=errors,
    )
    if (
        summary_schema is not None
        and config_schema is not None
        and summary_schema != config_schema
    ):
        errors.append(
            "staged telemetry schema version must match "
            "/api/config.stagedConfig.telemetrySchemaVersion "
            f"({summary_schema} != {config_schema})"
        )
    if summary_schema == 2:
        return _validate_staged_pipeline_integrity_v2(
            staged_pipeline,
            staged_config,
            websocket_receive_events,
            input_end_timestamp_ms,
            errors,
        )
    if summary_schema == 3:
        return _validate_staged_pipeline_integrity_v3(
            staged_pipeline,
            staged_config,
            websocket_receive_events,
            input_end_timestamp_ms,
            errors,
        )

    configured_cap = (
        staged_config.get("ttsSubsegmentMaxChars", 0)
        if staged_config is not None
        else 0
    )
    if (
        not isinstance(configured_cap, int)
        or isinstance(configured_cap, bool)
        or configured_cap < 0
    ):
        errors.append(
            "/api/config.stagedConfig.ttsSubsegmentMaxChars must be "
            "a non-negative integer"
        )
    elif configured_cap != 0:
        errors.append(
            "schema-v1 telemetry requires "
            "/api/config.stagedConfig.ttsSubsegmentMaxChars=0"
        )
    enabled = staged_pipeline.get("tts_subsegmentation_enabled", False)
    if enabled is not False:
        errors.append(
            "schema-v1 telemetry requires tts_subsegmentation_enabled=false"
        )

    if staged_pipeline.get("state") != "closed":
        errors.append(
            "staged state must be 'closed' "
            f"(got {staged_pipeline.get('state')!r})"
        )
    if not isinstance(websocket_receive_events, list):
        errors.append("websocket_receive_events must be a list")
    else:
        completed_orders: list[int] = []
        completed_timestamps: list[float] = []
        received_pcm_bytes: list[int] = []
        valid_orders = True
        observed_orders: list[int] = []
        for index, event in enumerate(websocket_receive_events):
            if not isinstance(event, dict):
                errors.append(f"websocket_receive_events[{index}] must be an object")
                valid_orders = False
                continue
            order = event.get("order")
            if (
                not isinstance(order, int)
                or isinstance(order, bool)
                or order < 0
            ):
                errors.append(
                    f"websocket_receive_events[{index}].order is invalid"
                )
                valid_orders = False
                continue
            observed_orders.append(order)
            if event.get("frame_type") == "pcm":
                audio_bytes = event.get("audio_bytes")
                if (
                    not isinstance(audio_bytes, int)
                    or isinstance(audio_bytes, bool)
                    or audio_bytes <= 0
                ):
                    errors.append(
                        f"websocket_receive_events[{index}].audio_bytes is invalid"
                    )
                else:
                    received_pcm_bytes.append(audio_bytes)
            if (
                event.get("frame_type") == "control"
                and event.get("message_type") == "status"
                and event.get("status") == "completed"
            ):
                completed_orders.append(order)
                timestamp_ms = event.get("timestamp_ms")
                if (
                    not isinstance(timestamp_ms, (int, float))
                    or isinstance(timestamp_ms, bool)
                    or not math.isfinite(timestamp_ms)
                    or timestamp_ms < 0
                ):
                    errors.append(
                        f"websocket_receive_events[{index}].timestamp_ms is invalid"
                    )
                else:
                    completed_timestamps.append(float(timestamp_ms))

        if valid_orders and observed_orders != list(range(len(observed_orders))):
            errors.append(
                "websocket_receive_events order must be contiguous from zero"
            )
        if len(completed_orders) != 1:
            errors.append(
                "exactly one completed WebSocket terminal is required "
                f"(got {len(completed_orders)})"
            )
        elif any(
            isinstance(event, dict)
            and event.get("frame_type") == "pcm"
            and isinstance(event.get("order"), int)
            and event["order"] > completed_orders[0]
            for event in websocket_receive_events
        ):
            errors.append("PCM was received after the completed WebSocket terminal")
        if (
            not isinstance(input_end_timestamp_ms, (int, float))
            or isinstance(input_end_timestamp_ms, bool)
            or not math.isfinite(input_end_timestamp_ms)
            or input_end_timestamp_ms <= 0
        ):
            errors.append("input_end_timestamp_ms must be positive and finite")
        elif len(completed_timestamps) == 1 and (
            completed_timestamps[0] < float(input_end_timestamp_ms)
        ):
            errors.append("completed WebSocket terminal arrived before end_input")

    if staged_pipeline.get("outcome") != "complete":
        errors.append(
            "staged outcome must be 'complete' "
            f"(got {staged_pipeline.get('outcome')!r})"
        )
    if staged_pipeline.get("failure") is not None:
        errors.append("staged failure must be null")

    cleanup_errors = staged_pipeline.get("cleanup_errors")
    if not isinstance(cleanup_errors, list):
        errors.append("cleanup_errors must be a list")
    elif cleanup_errors:
        errors.append("cleanup_errors must be empty")

    incomplete = _sequence_ids(
        staged_pipeline.get("incomplete_sequence_ids"),
        field_name="incomplete_sequence_ids",
        errors=errors,
    )
    if incomplete:
        errors.append(f"incomplete sequence IDs remain: {incomplete}")

    completed = _sequence_ids(
        staged_pipeline.get("completed_sequence_ids"),
        field_name="completed_sequence_ids",
        errors=errors,
    )
    websocket_sent = _sequence_ids(
        staged_pipeline.get("websocket_sent_sequence_ids"),
        field_name="websocket_sent_sequence_ids",
        errors=errors,
    )
    if completed is not None:
        expected = list(range(len(completed)))
        if completed != expected:
            errors.append(
                "completed_sequence_ids must be contiguous and ordered from zero "
                f"(got {completed})"
            )
    if completed is not None and websocket_sent is not None:
        if websocket_sent != completed:
            errors.append(
                "websocket_sent_sequence_ids must exactly match completed_sequence_ids"
            )

    for count_name in ("segments_emitted", "audio_segments_produced"):
        count = staged_pipeline.get(count_name)
        if (
            completed is not None
            and (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count != len(completed)
            )
        ):
            errors.append(
                f"{count_name} must equal the completed sequence count "
                f"({len(completed)})"
            )

    websocket_events = staged_pipeline.get("websocket_send_events")
    if not isinstance(websocket_events, list):
        errors.append("websocket_send_events must be a list")
    else:
        websocket_event_ids = []
        websocket_event_audio_bytes: list[int] = []
        valid_websocket_events = True
        for index, event in enumerate(websocket_events):
            if not isinstance(event, dict):
                errors.append(f"websocket_send_events[{index}] must be an object")
                valid_websocket_events = False
                continue
            sequence_id = event.get("sequence_id")
            if (
                not isinstance(sequence_id, int)
                or isinstance(sequence_id, bool)
                or sequence_id < 0
            ):
                errors.append(
                    f"websocket_send_events[{index}].sequence_id is invalid"
                )
                valid_websocket_events = False
                continue
            websocket_event_ids.append(sequence_id)
            audio_bytes = event.get("audio_bytes")
            if (
                not isinstance(audio_bytes, int)
                or isinstance(audio_bytes, bool)
                or audio_bytes <= 0
            ):
                errors.append(
                    f"websocket_send_events[{index}].audio_bytes is invalid"
                )
                valid_websocket_events = False
            else:
                websocket_event_audio_bytes.append(audio_bytes)
        if (
            valid_websocket_events
            and completed is not None
            and websocket_event_ids != completed
        ):
            errors.append(
                "websocket_send_events sequence order must exactly match "
                "completed_sequence_ids"
            )
        if valid_websocket_events and isinstance(websocket_receive_events, list):
            if len(received_pcm_bytes) != len(websocket_event_audio_bytes):
                errors.append(
                    "WebSocket PCM receive count must exactly match successful "
                    "send count "
                    f"({len(received_pcm_bytes)} != "
                    f"{len(websocket_event_audio_bytes)})"
                )
            elif received_pcm_bytes != websocket_event_audio_bytes:
                errors.append(
                    "WebSocket PCM received bytes must exactly match successful "
                    "send bytes frame-by-frame"
                )

    events = staged_pipeline.get("events")
    if not isinstance(events, list):
        errors.append("staged events must be a list")
        events = []

    event_sequences: dict[tuple[str, str], list[int]] = {
        ("segmenter", "emitted"): [],
        ("nmt", "completed"): [],
        ("tts", "completed"): [],
        ("output", "dequeued"): [],
    }
    nmt_event_retry_total = 0
    tts_event_retry_total = 0
    tts_retry_observed = False
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{index}] must be an object")
            continue

        key = (event.get("stage"), event.get("event"))
        sequence_id = event.get("sequence_id")
        if key in event_sequences and sequence_id is not None:
            if (
                not isinstance(sequence_id, int)
                or isinstance(sequence_id, bool)
                or sequence_id < 0
            ):
                errors.append(f"events[{index}].sequence_id is invalid")
            else:
                event_sequences[key].append(sequence_id)

        if key in {
            ("nmt", "completed"),
            ("nmt", "error"),
            ("tts", "completed"),
            ("tts", "error"),
        }:
            retry_count = event.get("retry_count")
            if (
                not isinstance(retry_count, int)
                or isinstance(retry_count, bool)
                or retry_count not in {0, 1}
            ):
                errors.append(
                    f"events[{index}].retry_count must be zero or one"
                )
            elif key == ("nmt", "completed"):
                nmt_event_retry_total += retry_count
            elif key == ("tts", "completed"):
                tts_event_retry_total += retry_count
                tts_retry_observed = tts_retry_observed or retry_count == 1
            elif key == ("tts", "error"):
                tts_retry_observed = tts_retry_observed or retry_count == 1

        queue_depth = event.get("queue_depth")
        queue_capacity = event.get("queue_capacity")
        if queue_depth is None and queue_capacity is None:
            continue
        if (
            not isinstance(queue_depth, int)
            or isinstance(queue_depth, bool)
            or queue_depth < 0
        ):
            errors.append(f"events[{index}].queue_depth is invalid")
            continue
        if not _positive_int(queue_capacity):
            errors.append(f"events[{index}].queue_capacity is invalid")
            continue
        if queue_depth > queue_capacity:
            errors.append(
                f"events[{index}] queue depth {queue_depth} exceeds "
                f"capacity {queue_capacity}"
            )

    if completed is not None:
        for (stage, event_name), observed in event_sequences.items():
            if observed != completed:
                errors.append(
                    f"{stage}/{event_name} sequence order must exactly match "
                    "completed_sequence_ids"
                )

    nmt_retry_count = staged_pipeline.get("nmt_retry_count")
    if (
        not isinstance(nmt_retry_count, int)
        or isinstance(nmt_retry_count, bool)
        or nmt_retry_count < 0
    ):
        errors.append("nmt_retry_count must be a non-negative integer")
    elif nmt_retry_count != nmt_event_retry_total:
        errors.append(
            "nmt_retry_count must equal the sum of nmt/completed event retries "
            f"({nmt_retry_count} != {nmt_event_retry_total})"
        )

    tts_retry_count = staged_pipeline.get("tts_retry_count")
    if (
        not isinstance(tts_retry_count, int)
        or isinstance(tts_retry_count, bool)
        or tts_retry_count < 0
    ):
        errors.append("tts_retry_count must be a non-negative integer")
    elif tts_retry_count != tts_event_retry_total:
        errors.append(
            "tts_retry_count must equal the sum of tts/completed event retries "
            f"({tts_retry_count} != {tts_event_retry_total})"
        )

    staged_config = backend_config.get("stagedConfig")
    if staged_config is not None and not isinstance(staged_config, dict):
        errors.append("/api/config.stagedConfig must be an object")
        staged_config = None
    if staged_config is not None:
        tts_max_retries = staged_config.get("ttsMaxRetries")
        if (
            not isinstance(tts_max_retries, int)
            or isinstance(tts_max_retries, bool)
            or tts_max_retries not in {0, 1}
        ):
            errors.append(
                "/api/config.stagedConfig.ttsMaxRetries must be zero or one"
            )
        elif tts_max_retries == 0 and tts_retry_observed:
            errors.append(
                "TTS retry telemetry is incompatible with "
                "/api/config.stagedConfig.ttsMaxRetries=0"
            )

    max_depths = staged_pipeline.get("max_queue_depths")
    if not isinstance(max_depths, dict):
        errors.append("max_queue_depths must be an object")
    elif staged_config is not None:
        queue_config_keys = {
            "nmt": "nmtQueueMaxSize",
            "tts": "ttsQueueMaxSize",
            "output": "outputQueueMaxSize",
        }
        for queue_name, config_key in queue_config_keys.items():
            capacity = staged_config.get(config_key)
            depth = max_depths.get(queue_name)
            if not _positive_int(capacity):
                errors.append(
                    f"/api/config.stagedConfig.{config_key} must be a positive integer"
                )
                continue
            if (
                not isinstance(depth, int)
                or isinstance(depth, bool)
                or depth < 0
            ):
                errors.append(f"max_queue_depths.{queue_name} is invalid")
                continue
            if depth > capacity:
                errors.append(
                    f"max_queue_depths.{queue_name}={depth} exceeds configured "
                    f"capacity {capacity}"
                )

    return errors


def validate_capture_result(result: TestResult) -> list[str]:
    """Return operational failures that make a batch capture incomplete."""
    errors: list[str] = []
    if result.pipeline_mode not in {"monolithic", "staged"}:
        errors.append(f"invalid pipeline mode: {result.pipeline_mode!r}")
    if result.duration_sec <= 0:
        errors.append("source audio duration is empty")
    if result.chunks_sent <= 0:
        errors.append("no source chunks were sent")
    if not result.input_completed:
        errors.append("input did not complete")
    if result.connection_lost:
        errors.append("WebSocket connection was lost")
    if result.drain_timed_out:
        errors.append("translated tail drain timed out")
    if not result.translation_completed:
        errors.append("backend did not confirm translated-stream completion")
    if result.server_error:
        errors.append(f"backend error: {result.server_error}")
    if result.audio_responses <= 0:
        errors.append("no translated audio responses were received")
    if result.total_received_bytes <= 0:
        errors.append("translated audio was empty")
    if result.translation_completed:
        if result.input_end_timestamp_ms <= 0:
            errors.append("input end timestamp is missing")
        if result.terminal_arrival_timestamp_ms <= 0:
            errors.append("terminal arrival timestamp is missing")
        elif result.terminal_arrival_timestamp_ms < result.input_end_timestamp_ms:
            errors.append("completed terminal arrived before end_input")
        expected_lag = max(
            0.0,
            (
                result.terminal_arrival_timestamp_ms
                - result.input_end_timestamp_ms
            )
            / 1000,
        )
        if not math.isclose(
            result.terminal_arrival_lag_sec,
            expected_lag,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            errors.append("terminal arrival lag is inconsistent with timestamps")
    errors.extend(result.staged_integrity_errors)
    return errors


def compute_playback_metrics(result: TestResult) -> None:
    """Compute arrival-replay first-audio latency and listener-visible tail.

    ``chunk_sent`` timestamps identify the start of each source chunk, not the
    end of input. Prefer the observed ``end_input`` send timestamp; for older
    or partial traces, add each chunk's exact PCM duration to its send time.
    """
    playback_end_sec = 0.0
    estimated_input_end_sec = 0.0
    explicit_input_end_sec = (
        result.input_end_timestamp_ms / 1000
        if result.input_end_timestamp_ms > 0
        else None
    )
    result.first_audio_latency_sec = 0.0

    for event in sorted(result.client_events, key=lambda event: event.timestamp_ms):
        event_time_sec = event.timestamp_ms / 1000
        if event.stage == "chunk_sent":
            estimated_input_end_sec = max(
                estimated_input_end_sec,
                event_time_sec
                + event.audio_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE),
            )
        elif event.stage == "input_ended":
            explicit_input_end_sec = event_time_sec
        elif event.stage == "audio_received":
            if result.first_audio_latency_sec == 0.0:
                result.first_audio_latency_sec = event_time_sec
            playback_end_sec = max(playback_end_sec, event_time_sec)
            playback_end_sec += event.audio_bytes / (
                SAMPLE_RATE * BYTES_PER_SAMPLE
            )

    input_end_sec = (
        explicit_input_end_sec
        if explicit_input_end_sec is not None
        else estimated_input_end_sec
    )
    result.playback_tail_sec = max(0.0, playback_end_sec - input_end_sec)


# ---------------------------------------------------------------------------
# Audio decoding
# ---------------------------------------------------------------------------
def decode_audio(path: str) -> np.ndarray:
    """Decode any audio file to 16 kHz mono Int16 PCM via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise RuntimeError(
                "ffmpeg is not installed; install imageio-ffmpeg in the venv"
            ) from exc

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {path}: {result.stderr.decode().strip()}"
        )
    return np.frombuffer(result.stdout, dtype=np.int16)


# ---------------------------------------------------------------------------
# Progress bar helper
# ---------------------------------------------------------------------------
def progress_bar(current: float, total: float, width: int = 20) -> str:
    frac = min(current / total, 1.0) if total > 0 else 0
    filled = int(frac * width)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return bar


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------
async def run_test(audio_path: str, backend_url: str) -> TestResult:
    """Run a single latency test against one audio file."""

    ws_url = backend_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/translate"

    # -- Decode audio -------------------------------------------------------
    print(f"Decoding {audio_path}...", end=" ", flush=True)
    pcm = decode_audio(audio_path)
    duration_sec = len(pcm) / SAMPLE_RATE
    print(f"{len(pcm)} samples ({duration_sec:.1f}s)")

    result = TestResult(
        audio_path=audio_path,
        duration_sec=duration_sec,
        backend_url=backend_url.rstrip("/"),
        target_language=TARGET_LANGUAGE,
    )
    (
        result.backend_config,
        result.pipeline_mode,
        result.pipeline_mode_source,
        result.backend_config_url,
    ) = fetch_backend_config(backend_url)
    model_config = result.backend_config.get("modelConfig")
    if isinstance(model_config, dict):
        nmt_config = model_config.get("nmt")
        configured_target = (
            nmt_config.get("targetLanguage")
            if isinstance(nmt_config, dict)
            else None
        )
        if configured_target != result.target_language:
            raise RuntimeError(
                "backend target-language provenance does not match the batch "
                f"request: configured={configured_target!r}, "
                f"requested={result.target_language!r}"
            )
    print(
        f"Backend pipeline: {result.pipeline_mode} "
        f"(source: {result.pipeline_mode_source})"
    )
    pcm_bytes = pcm.tobytes()
    total_chunks = (len(pcm_bytes) + CHUNK_BYTES - 1) // CHUNK_BYTES

    # -- Start backend timing session ---------------------------------------
    print("Starting test session...", flush=True)
    resp = requests.post(f"{backend_url}/api/test/start", timeout=10)
    resp.raise_for_status()

    # -- Connect WebSocket --------------------------------------------------
    print(f"Connecting to {ws_url}...", flush=True)
    test_start_time = time.monotonic()
    client_start_epoch = time.time()
    receive_order = 0

    def record_receive(
        *,
        frame_type: str,
        received_at: float,
        control: dict[str, Any] | None = None,
        audio_bytes: int = 0,
    ) -> dict[str, Any]:
        nonlocal receive_order
        event: dict[str, Any] = {
            "order": receive_order,
            "timestamp_ms": (received_at - client_start_epoch) * 1000,
            "frame_type": frame_type,
            "audio_bytes": audio_bytes,
        }
        if control is not None:
            event["message_type"] = control.get("type")
            event["status"] = control.get("status")
            if control.get("type") == "error":
                event["message"] = control.get("message")
        result.websocket_receive_events.append(event)
        receive_order += 1
        return event

    # Disable websockets library auto-ping (uvicorn/starlette doesn't
    # respond to protocol-level pings). We send app-level pings instead.
    async with websockets.connect(
        ws_url,
        max_size=2**22,
        ping_interval=None,
        ping_timeout=None,
        close_timeout=10,
    ) as ws:

        # Wait for "connected" status
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        record_receive(
            frame_type="control",
            received_at=time.time(),
            control=msg,
        )
        if msg.get("status") != "connected":
            raise RuntimeError(f"Unexpected initial message: {msg}")

        # Send start_stream
        await ws.send(json.dumps({
            "type": "start_stream",
            "targetLanguage": TARGET_LANGUAGE,
        }))

        # Wait for "listening"
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        record_receive(
            frame_type="control",
            received_at=time.time(),
            control=msg,
        )
        if msg.get("status") != "listening":
            raise RuntimeError(f"Expected 'listening', got: {msg}")

        # -- Shared state for concurrent tasks ------------------------------
        chunks_sent = 0
        audio_responses = 0
        total_recv_bytes = 0
        last_audio_time = time.monotonic()
        stream_abort = asyncio.Event()
        terminal_received = asyncio.Event()
        translation_complete = asyncio.Event()
        input_end_sent = asyncio.Event()
        connection_lost = False
        server_error = ""
        last_print_time = 0.0

        def current_drift() -> float:
            input_pos = chunks_sent * CHUNK_DURATION
            output_dur = total_recv_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            return input_pos - output_dur

        # -- App-level keepalive ping task ----------------------------------
        async def keepalive():
            """Send app-level ping every 10s to keep connection alive."""
            try:
                while True:
                    await asyncio.sleep(10)
                    await ws.send(json.dumps({"type": "ping"}))
            except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                pass

        # -- Send task ------------------------------------------------------
        async def send_audio():
            nonlocal chunks_sent, last_print_time, connection_lost
            offset = 0
            idx = 0
            loop_start = time.monotonic()

            while offset < len(pcm_bytes):
                if stream_abort.is_set():
                    break

                chunk = pcm_bytes[offset : offset + CHUNK_BYTES]
                send_ts = time.time()

                try:
                    await ws.send(chunk)
                except websockets.exceptions.ConnectionClosed:
                    connection_lost = True
                    stream_abort.set()
                    terminal_received.set()
                    print(f"\nConnection lost at chunk {idx} "
                          f"({idx * CHUNK_DURATION:.1f}s)")
                    break

                result.client_events.append(TimingEvent(
                    source="client",
                    stage="chunk_sent",
                    timestamp_ms=(send_ts - client_start_epoch) * 1000,
                    chunk_index=idx,
                    source_position_sec=idx * CHUNK_DURATION,
                    audio_bytes=len(chunk),
                ))

                chunks_sent = idx + 1
                offset += CHUNK_BYTES
                idx += 1

                # Progress reporting (every 1s)
                now = time.monotonic()
                if now - last_print_time >= 1.0:
                    elapsed = now - test_start_time
                    pos = chunks_sent * CHUNK_DURATION
                    bar = progress_bar(pos, duration_sec)
                    drift = current_drift()

                    # Record drift sample
                    result.drift_samples.append(DriftSample(
                        elapsed_sec=elapsed, drift_sec=drift,
                    ))

                    print(
                        f"\rStreaming: [{bar}] {pos:.1f}/{duration_sec:.1f}s"
                        f" | Sent: {chunks_sent} | Recv: {audio_responses}"
                        f" | Drift: {drift:.1f}s   ",
                        end="", flush=True,
                    )
                    last_print_time = now

                # Self-correcting timer: sleep until next chunk boundary
                expected = loop_start + idx * CHUNK_DURATION
                sleep_for = expected - time.monotonic()
                if sleep_for > 0:
                    try:
                        await asyncio.wait_for(
                            stream_abort.wait(),
                            timeout=sleep_for,
                        )
                    except asyncio.TimeoutError:
                        pass

        # -- Receive task ---------------------------------------------------
        async def receive_audio():
            nonlocal audio_responses, total_recv_bytes, connection_lost, last_audio_time, server_error
            recv_idx = 0
            try:
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        recv_ts = time.time()
                        record_receive(
                            frame_type="pcm",
                            received_at=recv_ts,
                            audio_bytes=len(raw),
                        )
                        audio_responses += 1
                        total_recv_bytes += len(raw)
                        last_audio_time = time.monotonic()
                        result.client_events.append(TimingEvent(
                            source="client",
                            stage="audio_received",
                            timestamp_ms=(recv_ts - client_start_epoch) * 1000,
                            chunk_index=recv_idx,
                            source_position_sec=total_recv_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE),
                            audio_bytes=len(raw),
                        ))
                        recv_idx += 1
                    elif isinstance(raw, str):
                        try:
                            control = json.loads(raw)
                        except json.JSONDecodeError:
                            record_receive(
                                frame_type="text",
                                received_at=time.time(),
                            )
                            continue
                        receive_event = record_receive(
                            frame_type="control",
                            received_at=time.time(),
                            control=control,
                        )
                        if (
                            control.get("type") == "status"
                            and control.get("status") == "completed"
                        ):
                            result.terminal_arrival_timestamp_ms = float(
                                receive_event["timestamp_ms"]
                            )
                            if not input_end_sent.is_set():
                                server_error = (
                                    "backend completed before client end_input"
                                )
                                stream_abort.set()
                            else:
                                translation_complete.set()
                            terminal_received.set()
                        elif control.get("type") == "error":
                            server_error = control.get("message", "backend error")
                            stream_abort.set()
                            terminal_received.set()
            except websockets.exceptions.ConnectionClosed:
                connection_lost = True
                stream_abort.set()
                terminal_received.set()
            except asyncio.CancelledError:
                pass

        # -- Run send + receive + keepalive concurrently --------------------
        ping_task = asyncio.create_task(keepalive())
        recv_task = asyncio.create_task(receive_audio())
        await send_audio()

        # Final progress line
        pos = chunks_sent * CHUNK_DURATION
        print(
            f"\rStreaming: [{progress_bar(pos, duration_sec)}] "
            f"{pos:.1f}/{duration_sec:.1f}s"
            f" | Sent: {chunks_sent} | Recv: {audio_responses}"
            f" | Drift: {current_drift():.1f}s   ",
        )

        if connection_lost or server_error or stream_abort.is_set():
            if server_error:
                print(
                    "(backend error — stopped input early and saving "
                    f"partial results: {server_error})"
                )
            elif connection_lost:
                print("(connection lost — saving partial results)")
        else:
            # Close only the input side so Riva can flush final ASR/NMT/TTS
            # responses while this client keeps receiving translated audio.
            input_end_time = time.monotonic()
            input_end_epoch = time.time()
            result.input_end_timestamp_ms = (
                input_end_epoch - client_start_epoch
            ) * 1000
            result.client_events.append(TimingEvent(
                source="client",
                stage="input_ended",
                timestamp_ms=result.input_end_timestamp_ms,
                chunk_index=chunks_sent,
                source_position_sec=duration_sec,
                audio_bytes=0,
            ))
            responses_at_input_end = audio_responses
            # Mark the causal boundary before yielding in ws.send(). A valid
            # completion cannot precede the invocation that sends end_input.
            input_end_sent.set()
            try:
                await ws.send(json.dumps({"type": "end_input"}))
            except websockets.exceptions.ConnectionClosed:
                connection_lost = True
                stream_abort.set()
                terminal_received.set()

            print(
                "Draining translated tail until Riva confirms completion "
                f"(max {DRAIN_MAX_SECONDS}s)...",
                flush=True,
            )
            drain_start = time.monotonic()
            while time.monotonic() - drain_start < DRAIN_MAX_SECONDS:
                if connection_lost:
                    print("\n(connection lost during drain)")
                    break
                if server_error:
                    print(f"\n(backend error during drain: {server_error})")
                    break
                elapsed = time.monotonic() - drain_start
                idle = time.monotonic() - last_audio_time
                drift = current_drift()
                print(
                    f"\rDraining: {elapsed:.0f}s | idle: {idle:.0f}s"
                    f" | Recv: {audio_responses} | Drift: {drift:.1f}s"
                    f" | complete: {translation_complete.is_set()}   ",
                    end="", flush=True,
                )
                result.drift_samples.append(DriftSample(
                    elapsed_sec=time.monotonic() - test_start_time,
                    drift_sec=drift,
                ))
                if terminal_received.is_set():
                    # Keep the receiver alive briefly so a duplicate terminal or
                    # protocol-invalid PCM queued behind the terminal is captured.
                    if translation_complete.is_set():
                        await asyncio.sleep(TERMINAL_SETTLE_SECONDS)
                    break
                await asyncio.sleep(1.0)
            print()

            result.drain_duration_sec = time.monotonic() - drain_start
            result.translation_completed = (
                translation_complete.is_set() and not server_error
            )
            result.drain_timed_out = not terminal_received.is_set()
            result.tail_lag_sec = max(0.0, last_audio_time - input_end_time)
            result.post_input_responses = audio_responses - responses_at_input_end
            if result.terminal_arrival_timestamp_ms > 0:
                result.terminal_arrival_lag_sec = max(
                    0.0,
                    (
                        result.terminal_arrival_timestamp_ms
                        - result.input_end_timestamp_ms
                    )
                    / 1000,
                )

        result.server_error = server_error

        # Stop any still-open stream. On an error this performs best-effort
        # backend cleanup without falsely signaling normal end-of-input.
        if not connection_lost:
            try:
                await ws.send(json.dumps({"type": "stop_stream"}))
            except websockets.exceptions.ConnectionClosed:
                pass

        stream_abort.set()
        ping_task.cancel()
        recv_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    # -- Stop backend timing & export ---------------------------------------
    requests.post(f"{backend_url}/api/test/stop", timeout=10)

    try:
        export_data = await fetch_backend_export(
            backend_url,
            pipeline_mode=result.pipeline_mode,
            backend_config=result.backend_config,
        )
        result.staged_pipeline = export_data.get("stagedPipeline")
        for ev in export_data.get("events", []):
            result.backend_events.append(TimingEvent(
                source="backend",
                stage=ev["stage"],
                timestamp_ms=ev.get("wall_clock", 0) * 1000,
                chunk_index=ev.get("chunk_index", -1),
                source_position_sec=ev.get("source_position_sec", 0),
                audio_bytes=ev.get("audio_bytes_len", 0),
            ))
    except Exception as e:
        print(f"Warning: could not export backend timing: {e}")

    if result.pipeline_mode == "staged":
        result.staged_integrity_errors = validate_staged_pipeline_integrity(
            result.staged_pipeline,
            result.backend_config,
            result.websocket_receive_events,
            result.input_end_timestamp_ms,
        )

    # -- Compute summary stats ----------------------------------------------
    result.chunks_sent = chunks_sent
    result.audio_responses = audio_responses
    result.total_received_bytes = total_recv_bytes
    result.input_completed = chunks_sent == total_chunks
    result.connection_lost = connection_lost
    result.output_duration_sec = total_recv_bytes / (
        SAMPLE_RATE * BYTES_PER_SAMPLE
    )
    result.duration_excess_sec = max(
        0.0, result.output_duration_sec - duration_sec
    )
    if duration_sec:
        result.tts_expansion_ratio = result.output_duration_sec / duration_sec

    if result.drift_samples:
        drifts = [s.drift_sec for s in result.drift_samples]
        result.avg_drift = sum(drifts) / len(drifts)
        result.max_drift = max(drifts)
        result.final_drift = drifts[-1]

    # Simulate the browser's gapless playback queue using actual arrival
    # timestamps. This captures initial latency and delivery gaps as well as
    # output-duration expansion, yielding the listener-visible tail.
    compute_playback_metrics(result)

    return result


async def fetch_backend_export(
    backend_url: str,
    *,
    pipeline_mode: str,
    backend_config: dict[str, Any],
) -> dict[str, Any]:
    """Fetch a finalized staged snapshot without discarding failure evidence."""
    wait_seconds = 0.0
    if pipeline_mode == "staged":
        close_timeout = (
            backend_config.get("stagedConfig", {}).get("closeTimeoutSeconds")
            if isinstance(backend_config.get("stagedConfig"), dict)
            else None
        )
        if (
            not isinstance(close_timeout, (int, float))
            or isinstance(close_timeout, bool)
            or not math.isfinite(close_timeout)
            or close_timeout <= 0
        ):
            close_timeout = 10.0
        wait_seconds = max(5.0, 3.0 * float(close_timeout))

    deadline = time.monotonic() + wait_seconds
    latest_export: dict[str, Any] | None = None
    last_error: Exception | None = None
    while True:
        try:
            response = requests.get(
                f"{backend_url.rstrip('/')}/api/test/export",
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("/api/test/export must return a JSON object")
            latest_export = payload
            staged = payload.get("stagedPipeline")
            if pipeline_mode != "staged" or (
                isinstance(staged, dict) and staged.get("state") == "closed"
            ):
                return payload
        except Exception as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            if latest_export is not None:
                return latest_export
            if last_error is not None:
                raise last_error
            raise RuntimeError("backend timing export was unavailable")
        await asyncio.sleep(EXPORT_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------
def generate_plot(result: TestResult, output_path: str):
    """Create a matplotlib drift-over-time plot."""
    if not result.drift_samples:
        print(f"  No drift data to plot for {result.audio_path}")
        return

    elapsed_min = [s.elapsed_sec / 60 for s in result.drift_samples]
    drift_sec = [s.drift_sec for s in result.drift_samples]

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(elapsed_min, drift_sec, color="#3b82f6", linewidth=1.5, label="Drift")
    ax.axhline(y=20, color="orange", linestyle="--", linewidth=1, label="Warning (20s)")
    ax.axhline(y=30, color="red", linestyle="--", linewidth=1, label="Danger (30s)")
    ax.set_xlabel("Elapsed Time (minutes)")
    ax.set_ylabel("Translation Delay (seconds)")
    ax.set_title(Path(result.audio_path).stem)
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {output_path}")


def generate_csv(result: TestResult, output_path: str):
    """Write combined client + backend timing events to CSV."""
    all_events = result.client_events + result.backend_events
    all_events.sort(key=lambda e: e.timestamp_ms)

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "source", "stage", "timestamp_ms",
            "chunk_index", "source_position_sec", "audio_bytes",
        ])
        for ev in all_events:
            writer.writerow([
                ev.source, ev.stage, f"{ev.timestamp_ms:.2f}",
                ev.chunk_index, f"{ev.source_position_sec:.3f}",
                ev.audio_bytes,
            ])
    print(f"Saved: {output_path}")


def generate_summary(result: TestResult, output_path: str):
    """Write metrics, provenance, and staged evidence for one audio file."""
    summary = {
        "audio_path": result.audio_path,
        "backend_url": result.backend_url,
        "backend_config_url": result.backend_config_url,
        "backend_config": result.backend_config,
        "target_language": result.target_language,
        "pipeline_mode": result.pipeline_mode,
        "pipeline_mode_source": result.pipeline_mode_source,
        # Preserve the complete direct-stage summary and event trace returned
        # by /api/test/export. This remains null for monolithic runs.
        "staged_pipeline": result.staged_pipeline,
        "staged_integrity": {
            "applicable": result.pipeline_mode == "staged",
            "passed": (
                not result.staged_integrity_errors
                if result.pipeline_mode == "staged"
                else None
            ),
            "errors": result.staged_integrity_errors,
        },
        # Ordered receive-side protocol evidence. This proves where the sole
        # completed terminal occurred relative to every translated PCM frame.
        "websocket_receive_events": result.websocket_receive_events,
        "input_duration_sec": result.duration_sec,
        "chunks_sent": result.chunks_sent,
        "audio_responses": result.audio_responses,
        "total_received_bytes": result.total_received_bytes,
        "output_duration_sec": result.output_duration_sec,
        # This is a whole-file output/input proxy. Source silence is included
        # in the denominator, so it is not a speech-only prosody measurement.
        "output_to_input_duration_ratio": result.tts_expansion_ratio,
        "average_drift_sec": result.avg_drift,
        "max_drift_sec": result.max_drift,
        "final_drift_sec": result.final_drift,
        "tail_lag_sec": result.tail_lag_sec,
        "first_audio_latency_sec": result.first_audio_latency_sec,
        "duration_excess_sec": result.duration_excess_sec,
        "playback_tail_sec": result.playback_tail_sec,
        "post_input_responses": result.post_input_responses,
        "input_completed": result.input_completed,
        "connection_lost": result.connection_lost,
        "drain_timed_out": result.drain_timed_out,
        "drain_duration_sec": result.drain_duration_sec,
        "input_end_timestamp_ms": result.input_end_timestamp_ms,
        "terminal_arrival_timestamp_ms": result.terminal_arrival_timestamp_ms,
        "terminal_arrival_lag_sec": result.terminal_arrival_lag_sec,
        "translation_completed": result.translation_completed,
        "server_error": result.server_error,
    }
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(f"Saved: {output_path}")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def check_backend(backend_url: str):
    """Verify backend is reachable."""
    try:
        resp = requests.get(f"{backend_url}/api/config", timeout=5)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR: Cannot reach backend at {backend_url}: {e}")
        print("Make sure the backend is running:")
        print("  cd backend && uvicorn main:app --host 0.0.0.0 --port 8000")
        return False


async def run_preflight(backend_url: str) -> bool:
    """Run pre-flight validation with the local neutral fixture."""
    print("\n=== Pre-flight Validation ===")
    if not Path(PREFLIGHT_FILE).exists():
        print(f"ERROR: Pre-flight file not found: {PREFLIGHT_FILE}")
        return False

    try:
        result = await run_test(PREFLIGHT_FILE, backend_url)
    except Exception as e:
        print(f"\nPre-flight FAILED: {e}")
        return False

    if result.audio_responses == 0:
        print(
            f"\nPre-flight FAILED: No audio responses received."
            f" Sent {result.chunks_sent} chunks but got 0 translated audio back."
            f"\nCheck that Riva services are running at the configured RIVA_URI."
        )
        return False

    capture_errors = validate_capture_result(result)
    if capture_errors:
        print(
            "\nPre-flight FAILED: capture did not complete cleanly: "
            f"input_completed={result.input_completed}, "
            f"connection_lost={result.connection_lost}, "
            f"drain_timed_out={result.drain_timed_out}, "
            f"translation_completed={result.translation_completed}, "
            f"server_error={result.server_error or 'none'}, "
            "staged_integrity_errors="
            f"{result.staged_integrity_errors or 'none'}, "
            f"validation_errors={capture_errors}"
        )
        return False

    print(
        f"Pre-flight PASSED: Received {result.audio_responses} audio responses, "
        f"avg drift {result.avg_drift:.1f}s, "
        f"output {result.output_duration_sec:.1f}s "
        f"({result.tts_expansion_ratio:.3f}x), "
        f"tail lag {result.tail_lag_sec:.1f}s"
    )
    return True


async def run_batch(files: list[str], backend_url: str, output_dir: str) -> bool:
    """Run tests on a list of audio files sequentially."""
    total = len(files)
    all_captures_passed = total > 0
    captures_written = 0
    for i, fpath in enumerate(files, 1):
        print(f"\n=== Test {i}/{total}: {Path(fpath).name} ===")

        if not Path(fpath).exists():
            print(f"FAILED: File not found: {fpath}")
            all_captures_passed = False
            continue

        try:
            result = await run_test(fpath, backend_url)
        except Exception as e:
            print(f"\nERROR: {e}")
            all_captures_passed = False
            continue

        # Generate outputs
        stem = Path(fpath).stem
        parent = Path(output_dir)
        parent.mkdir(parents=True, exist_ok=True)
        plot_path = str(parent / f"{stem}_latency.png")
        csv_path = str(parent / f"{stem}_results.csv")
        summary_path = str(parent / f"{stem}_summary.json")

        try:
            generate_plot(result, plot_path)
            generate_csv(result, csv_path)
            generate_summary(result, summary_path)
            captures_written += 1
        except Exception as exc:
            print(f"Artifact generation FAILED: {exc}")
            all_captures_passed = False
            continue

        capture_errors = validate_capture_result(result)
        if capture_errors:
            all_captures_passed = False
            print("Capture validation FAILED:")
            for error in capture_errors:
                print(f"  - {error}")

        print(
            f"Summary: avg_drift={result.avg_drift:.1f}s, "
            f"max_drift={result.max_drift:.1f}s, "
            f"final_drift={result.final_drift:.1f}s, "
            f"output={result.output_duration_sec:.1f}s, "
            f"expansion={result.tts_expansion_ratio:.3f}x, "
            f"tail_lag={result.tail_lag_sec:.1f}s, "
            f"first_audio={result.first_audio_latency_sec:.1f}s, "
            f"duration_excess={result.duration_excess_sec:.1f}s, "
            f"playback_tail={result.playback_tail_sec:.1f}s, "
            f"post_input_responses={result.post_input_responses}"
        )
    return all_captures_passed and captures_written == total


def main():
    parser = argparse.ArgumentParser(
        description="Batch latency test for real-time audio translation"
    )
    parser.add_argument(
        "--preflight", action="store_true",
        help="Run pre-flight validation only (preflight.wav)",
    )
    parser.add_argument(
        "--file", type=str,
        help="Test a single audio file instead of the full batch",
    )
    parser.add_argument(
        "--backend", type=str, default="http://localhost:8000",
        help="Backend URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--output-dir", type=str, default="test_results_nemotron",
        help="Directory for new CSV and plot outputs",
    )
    args = parser.parse_args()

    if not check_backend(args.backend):
        sys.exit(1)

    if args.preflight:
        ok = asyncio.run(run_preflight(args.backend))
        sys.exit(0 if ok else 1)

    if args.file:
        ok = asyncio.run(run_batch([args.file], args.backend, args.output_dir))
    else:
        ok = asyncio.run(run_batch(LONG_FORM_FILES, args.backend, args.output_dir))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
