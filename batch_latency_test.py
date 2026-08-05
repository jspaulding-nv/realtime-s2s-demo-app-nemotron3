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
from typing import Any, Awaitable, Callable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
import websockets

from audio_metadata_protocol import (
    AUDIO_METADATA_PROTOCOL_VERSION,
    AudioMetadataProtocolError,
    AudioMetadataTracker,
)
from headless_playback_scheduler import (
    HeadlessPlaybackScheduler,
    validate_headless_playback_report,
)
from private_pcm_schedule_ledger import PrivatePcmScheduleCapture
from synthesized_pcm_silence import (
    StreamingPcmSilenceDiagnostic,
    SynthesizedPcmSilenceError,
    validate_synthesized_pcm_silence_observation,
)

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
INPUT_PACING_MODE = "chunk_end_boundary_v1"
INPUT_SAMPLE_ZERO_CLOCK = "client_monotonic"
INPUT_PACING_DEADLINE_BASIS = (
    "source_sample_zero_plus_one_based_chunk_duration"
)
INPUT_PACING_FIELDS = frozenset(
    {
        "mode",
        "chunk_duration_ms",
        "source_sample_zero_clock",
        "deadline_basis",
        "source_sample_zero_timestamp_ms",
        "observed_chunk_count",
        "min_emission_minus_deadline_ms",
        "max_emission_minus_deadline_ms",
    }
)
SYNTHESIZED_PCM_SCAN_P95_LIMIT_MS = 5.0
SYNTHESIZED_PCM_SCAN_MAX_LIMIT_MS = 25.0
SYNTHESIZED_PCM_SCAN_MEASUREMENT_POSITION = (
    "after_arrival_and_playback_scheduling_before_next_receive"
)
SYNTHESIZED_PCM_SCAN_CLOCK = "client_monotonic_duration"
SYNTHESIZED_PCM_SCAN_PRIVACY = {
    "aggregate_only": True,
    "contains_per_frame_timings": False,
    "contains_wall_clock_timestamps": False,
}
SYNTHESIZED_PCM_SCAN_FIELDS = frozenset(
    {
        "measurement_position",
        "clock",
        "frame_count",
        "total_ms",
        "mean_ms",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "p95_limit_ms",
        "max_limit_ms",
        "gate_passed",
        "privacy",
    }
)

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
    protocol_version: int | None = None
    stream_generation: int | None = None
    parent_sequence_id: int | None = None
    audio_frame_id: int | None = None
    source_start_ms: float | None = None
    source_end_ms: float | None = None
    source_end_to_receipt_ms: float | None = None


@dataclass
class DriftSample:
    elapsed_sec: float
    drift_sec: float


@dataclass(frozen=True)
class ValidatedAudioFrame:
    """Privacy-safe observation of one protocol-validated PCM frame.

    The sink receives timing, identity, PCM-format, and source-attribution
    metadata only. It deliberately does not receive the PCM payload, transcript,
    translation, filename, URL, or a wall-clock timestamp.
    """

    arrival_seconds: float
    audio_bytes: int
    protocol_version: int
    stream_generation: int
    parent_sequence_id: int
    audio_frame_id: int
    sample_rate_hz: int
    channels: int
    bytes_per_sample: int
    source_start_ms: float | None
    source_end_ms: float | None


# The callback executes inline in the sole WebSocket receive loop. It must be
# an O(1), nonblocking observer: enqueue work with ``put_nowait`` if downstream
# processing could perform I/O, wait on a device, or otherwise block.
ValidatedAudioFrameSink = Callable[[ValidatedAudioFrame], None]


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
    audio_metadata_protocol_version: int | None = None
    audio_metadata_stream_generation: int | None = None
    input_sample_zero_timestamp_ms: float | None = None
    input_pacing: dict[str, Any] | None = None
    audio_metadata_paired_frames: int = 0
    audio_metadata_completed_parents: int = 0
    source_end_to_receipt_samples_ms: list[float] = field(default_factory=list)
    source_end_to_receipt_p50_ms: float | None = None
    source_end_to_receipt_p95_ms: float | None = None
    source_end_to_receipt_max_ms: float | None = None
    source_end_to_receipt_availability: str = "protocol_not_negotiated"
    headless_playback_report: dict[str, Any] | None = None
    synthesized_pcm_silence_requested: bool = False
    synthesized_pcm_silence: dict[str, Any] | None = None
    synthesized_pcm_silence_processing: dict[str, Any] | None = None
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
_PUBLISHER_HANDOFF_FIELDS = (
    "publish_requested_monotonic_ms",
    "event_loop_callback_started_monotonic_ms",
    "output_capacity_acquired_monotonic_ms",
)
_PUBLISHER_HANDOFF_BLOCKED_TOLERANCE_MS = 10.0
_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS = 1e-3
_VALID_EMISSION_REASONS = frozenset(
    {"punctuation", "length", "age", "final_flush"}
)
_PIPELINE_EVENT_SAFE_FIELDS = frozenset(
    {
        "session_id",
        "stage",
        "event",
        "monotonic_ms",
        "sequence_id",
        "parent_sequence_id",
        "subsequence_id",
        "subsequence_count",
        "asr_final_id",
        "contributing_final_ids",
        "emission_reason",
        "source_start_ms",
        "source_end_ms",
        "queue_depth",
        "queue_capacity",
        "queue_residence_ms",
        "processing_duration_ms",
        "blocked_put_ms",
        "text_chars",
        "parent_text_chars",
        "audio_bytes",
        "audio_duration_ms",
        "retry_count",
        "error_code",
        "audio_frame_id",
        "audio_frame_count",
        "atomic_fallback_applied",
        *_PUBLISHER_HANDOFF_FIELDS,
    }
)
_WEBSOCKET_FRAME_SAFE_FIELDS = frozenset(
    {
        "sequence_id",
        "parent_sequence_id",
        "audio_frame_id",
        "audio_bytes",
        "send_started_monotonic_ms",
        "sent_monotonic_ms",
    }
)
_TTS_RESPONSE_SIDECAR_SAFE_FIELDS = frozenset(
    {
        "schema_version",
        "segments_observed",
        "response_chunk_count",
        "chunks",
    }
)
_TTS_RESPONSE_CHUNK_SAFE_FIELDS = frozenset(
    {
        "parent_sequence_id",
        "subsequence_id",
        "subsequence_count",
        "response_index",
        "response_count",
        "audio_bytes",
        "cumulative_audio_bytes",
        "audio_duration_ms",
        "cumulative_audio_duration_ms",
        "received_monotonic_ms",
        "since_request_start_ms",
        "since_previous_response_ms",
        "retry_count",
    }
)
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


def _nonnegative_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _handoff_timestamp(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
) -> float | None:
    if not _nonnegative_finite_number(value):
        errors.append(f"{field_name} must be non-negative and finite")
        return None
    return float(value)


def _reject_unsafe_telemetry_fields(
    value: dict[str, Any],
    *,
    allowed_fields: frozenset[str],
    field_name: str,
    errors: list[str],
) -> None:
    unexpected = sorted(set(value) - allowed_fields)
    if unexpected:
        errors.append(
            f"{field_name} contains private or unsupported payload fields: "
            + ", ".join(unexpected)
        )


def _publisher_frame_metadata(
    event: dict[str, Any],
    *,
    summary_session_id: Any,
    field_name: str,
    errors: list[str],
) -> tuple[Any, ...] | None:
    """Validate privacy-safe values retained on one successful frame row."""
    valid = True
    session_id = event.get("session_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or session_id != summary_session_id
    ):
        errors.append(
            f"{field_name}.session_id must be nonempty and match "
            "the staged summary"
        )
        valid = False

    error_code = event.get("error_code", "")
    if not isinstance(error_code, str) or error_code:
        errors.append(
            f"{field_name}.error_code must be the empty success code"
        )
        valid = False

    emission_reason = event.get("emission_reason")
    if (
        emission_reason is not None
        and emission_reason not in _VALID_EMISSION_REASONS
    ):
        errors.append(
            f"{field_name}.emission_reason is not a fixed supported value"
        )
        valid = False

    asr_final_id = event.get("asr_final_id")
    if asr_final_id is not None and (
        not isinstance(asr_final_id, int)
        or isinstance(asr_final_id, bool)
        or asr_final_id < 0
    ):
        errors.append(
            f"{field_name}.asr_final_id must be a non-negative integer or null"
        )
        valid = False

    contributing_final_ids = event.get("contributing_final_ids", ())
    if not isinstance(contributing_final_ids, (list, tuple)) or any(
        not isinstance(final_id, int)
        or isinstance(final_id, bool)
        or final_id < 0
        for final_id in contributing_final_ids
    ):
        errors.append(
            f"{field_name}.contributing_final_ids must contain only "
            "non-negative integer IDs"
        )
        valid = False
        normalized_final_ids: tuple[int, ...] = ()
    else:
        normalized_final_ids = tuple(contributing_final_ids)
        if len(set(normalized_final_ids)) != len(normalized_final_ids):
            errors.append(
                f"{field_name}.contributing_final_ids must not contain "
                "duplicates"
            )
            valid = False

    source_values: list[float | None] = []
    for source_field in ("source_start_ms", "source_end_ms"):
        value = event.get(source_field)
        if value is None:
            source_values.append(None)
        elif not _nonnegative_finite_number(value):
            errors.append(
                f"{field_name}.{source_field} must be non-negative, finite, "
                "or null"
            )
            source_values.append(None)
            valid = False
        else:
            source_values.append(float(value))
    if (
        source_values[0] is not None
        and source_values[1] is not None
        and source_values[1] < source_values[0]
    ):
        errors.append(
            f"{field_name}.source_end_ms cannot precede source_start_ms"
        )
        valid = False

    if not valid:
        return None
    return (
        session_id,
        error_code,
        emission_reason,
        asr_final_id,
        normalized_final_ids,
        *source_values,
    )


def _validate_v3_response_chunk_sidecar(
    staged_pipeline: dict[str, Any],
    expected_frames: list[tuple[tuple[int, int], int, int]],
    *,
    frame_bytes_per_ms: float | None,
    errors: list[str],
) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
    """Validate the required privacy-safe response-chunk diagnostic."""
    sidecar = staged_pipeline.get("tts_response_chunk_telemetry")
    if not isinstance(sidecar, dict):
        errors.append(
            "enabled TTS response-chunk telemetry requires "
            "tts_response_chunk_telemetry"
        )
        return {}
    _reject_unsafe_telemetry_fields(
        sidecar,
        allowed_fields=_TTS_RESPONSE_SIDECAR_SAFE_FIELDS,
        field_name="tts_response_chunk_telemetry",
        errors=errors,
    )
    if sidecar.get("schema_version") != 1:
        errors.append("tts_response_chunk_telemetry.schema_version must be 1")

    chunks = sidecar.get("chunks")
    if not isinstance(chunks, list):
        errors.append("tts_response_chunk_telemetry.chunks must be a list")
        return {}
    response_chunk_count = sidecar.get("response_chunk_count")
    if (
        not isinstance(response_chunk_count, int)
        or isinstance(response_chunk_count, bool)
        or response_chunk_count < 0
    ):
        errors.append(
            "tts_response_chunk_telemetry.response_chunk_count must be "
            "a non-negative integer"
        )
    elif response_chunk_count != len(chunks):
        errors.append(
            "tts_response_chunk_telemetry.response_chunk_count must match "
            "the chunk row count"
        )

    expected_parent_bytes: dict[int, int] = {}
    expected_parent_retries: dict[int, int] = {}
    for (parent_id, _frame_id), audio_bytes, retry_count in expected_frames:
        expected_parent_bytes[parent_id] = (
            expected_parent_bytes.get(parent_id, 0) + audio_bytes
        )
        previous_retry = expected_parent_retries.setdefault(
            parent_id,
            retry_count,
        )
        if previous_retry != retry_count:
            errors.append(
                f"frame retry_count changed within parent {parent_id}"
            )

    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for index, chunk in enumerate(chunks):
        field_name = f"tts_response_chunk_telemetry.chunks[{index}]"
        if not isinstance(chunk, dict):
            errors.append(f"{field_name} must be an object")
            continue
        _reject_unsafe_telemetry_fields(
            chunk,
            allowed_fields=_TTS_RESPONSE_CHUNK_SAFE_FIELDS,
            field_name=field_name,
            errors=errors,
        )
        parent = chunk.get("parent_sequence_id")
        subsequence_id = chunk.get("subsequence_id")
        subsequence_count = chunk.get("subsequence_count")
        if (
            not isinstance(parent, int)
            or isinstance(parent, bool)
            or parent < 0
        ):
            errors.append(f"{field_name}.parent_sequence_id is invalid")
            continue
        if (
            subsequence_id != 0
            or isinstance(subsequence_id, bool)
            or subsequence_count != 1
            or isinstance(subsequence_count, bool)
        ):
            errors.append(
                f"{field_name} must use the schema-v3 request identity "
                "subsequence_id=0, subsequence_count=1"
            )
            continue

        parsed: dict[str, Any] = {}
        for integer_field, positive in (
            ("response_index", False),
            ("response_count", True),
            ("audio_bytes", True),
            ("cumulative_audio_bytes", True),
        ):
            value = chunk.get(integer_field)
            valid = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and (value > 0 if positive else value >= 0)
            )
            if not valid:
                qualifier = "positive" if positive else "non-negative"
                errors.append(
                    f"{field_name}.{integer_field} must be a {qualifier} integer"
                )
            else:
                parsed[integer_field] = value
        retry_count = chunk.get("retry_count")
        if (
            not isinstance(retry_count, int)
            or isinstance(retry_count, bool)
            or retry_count not in {0, 1}
        ):
            errors.append(
                f"{field_name}.retry_count must be zero or one"
            )
        else:
            parsed["retry_count"] = retry_count
        for timing_field in (
            "audio_duration_ms",
            "cumulative_audio_duration_ms",
            "received_monotonic_ms",
            "since_request_start_ms",
            "since_previous_response_ms",
        ):
            timing = _handoff_timestamp(
                chunk.get(timing_field),
                field_name=f"{field_name}.{timing_field}",
                errors=errors,
            )
            if timing is not None:
                parsed[timing_field] = timing
        if len(parsed) == 10:
            grouped.setdefault((parent, 0, 1), []).append(parsed)

    expected_keys = {
        (parent_id, 0, 1) for parent_id in expected_parent_bytes
    }
    if set(grouped) != expected_keys:
        errors.append(
            "TTS response-chunk identities must exactly cover canonical "
            "frame parents"
        )
    segments_observed = sidecar.get("segments_observed")
    if (
        not isinstance(segments_observed, int)
        or isinstance(segments_observed, bool)
        or segments_observed < 0
    ):
        errors.append(
            "tts_response_chunk_telemetry.segments_observed must be "
            "a non-negative integer"
        )
    elif segments_observed != len(grouped):
        errors.append(
            "tts_response_chunk_telemetry.segments_observed must match "
            "the observed request count"
        )

    for (parent_id, _subsequence_id, _subsequence_count), rows in grouped.items():
        declared_count = rows[0]["response_count"]
        if (
            declared_count != len(rows)
            or any(row["response_count"] != declared_count for row in rows)
        ):
            errors.append(
                f"parent {parent_id} TTS response_count must match its chunk rows"
            )
            continue
        if [row["response_index"] for row in rows] != list(
            range(declared_count)
        ):
            errors.append(
                f"parent {parent_id} TTS response indices must be contiguous "
                "from zero"
            )

        cumulative_bytes = 0
        cumulative_duration_ms = 0.0
        request_started_ms = (
            rows[0]["received_monotonic_ms"]
            - rows[0]["since_request_start_ms"]
        )
        if request_started_ms < 0:
            errors.append(
                f"parent {parent_id} TTS response request start is negative"
            )
        previous_received_ms = request_started_ms
        for row in rows:
            cumulative_bytes += row["audio_bytes"]
            cumulative_duration_ms += row["audio_duration_ms"]
            if row["cumulative_audio_bytes"] != cumulative_bytes:
                errors.append(
                    f"parent {parent_id} TTS response cumulative bytes "
                    "do not reconcile"
                )
            if not math.isclose(
                row["cumulative_audio_duration_ms"],
                cumulative_duration_ms,
                rel_tol=0.0,
                abs_tol=_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS,
            ):
                errors.append(
                    f"parent {parent_id} TTS response cumulative duration "
                    "does not reconcile"
                )
            received_ms = row["received_monotonic_ms"]
            if received_ms < previous_received_ms:
                errors.append(
                    f"parent {parent_id} TTS response timestamps are inverted"
                )
            if not math.isclose(
                row["since_previous_response_ms"],
                received_ms - previous_received_ms,
                rel_tol=0.0,
                abs_tol=_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS,
            ):
                errors.append(
                    f"parent {parent_id} TTS response inter-arrival timing "
                    "does not reconcile"
                )
            if not math.isclose(
                row["since_request_start_ms"],
                received_ms - request_started_ms,
                rel_tol=0.0,
                abs_tol=_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS,
            ):
                errors.append(
                    f"parent {parent_id} TTS response request timing "
                    "does not reconcile"
                )
            if (
                frame_bytes_per_ms is not None
                and not math.isclose(
                    row["audio_duration_ms"],
                    row["audio_bytes"] / frame_bytes_per_ms,
                    rel_tol=0.0,
                    abs_tol=_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS,
                )
            ):
                errors.append(
                    f"parent {parent_id} TTS response duration does not "
                    "reconcile with audio bytes"
                )
            if row["retry_count"] != expected_parent_retries.get(parent_id):
                errors.append(
                    f"parent {parent_id} TTS response retry attribution "
                    "does not match canonical frames"
                )
            previous_received_ms = received_ms
        if cumulative_bytes != expected_parent_bytes.get(parent_id):
            errors.append(
                f"parent {parent_id} TTS response bytes do not match "
                "canonical frames"
            )
    return grouped


def _validate_v3_response_frame_boundaries(
    staged_pipeline: dict[str, Any],
    response_groups: dict[
        tuple[int, int, int],
        list[dict[str, Any]],
    ],
    canonical_keys: list[tuple[int, int]] | None,
    canonical_bytes: list[int] | None,
    received_rows: list[dict[str, Any]],
    errors: list[str],
) -> None:
    """Reconcile response chunks with TTS lifecycle and frame readiness."""
    if canonical_keys is None or canonical_bytes is None:
        return
    parent_summaries = staged_pipeline.get("produced_parent_summaries")
    if not isinstance(parent_summaries, list):
        return
    parent_metadata: dict[int, tuple[int, int, bool]] = {}
    for item in parent_summaries:
        if not isinstance(item, dict):
            continue
        parent_id = item.get("parent_sequence_id")
        audio_bytes = item.get("audio_bytes")
        retry_count = item.get("retry_count")
        fallback_applied = item.get("atomic_fallback_applied", False)
        if (
            isinstance(parent_id, int)
            and not isinstance(parent_id, bool)
            and parent_id >= 0
            and _positive_int(audio_bytes)
            and isinstance(retry_count, int)
            and not isinstance(retry_count, bool)
            and retry_count in {0, 1}
            and isinstance(fallback_applied, bool)
        ):
            parent_metadata[parent_id] = (
                audio_bytes,
                retry_count,
                fallback_applied,
            )

    lifecycle: dict[str, dict[int, dict[str, Any]]] = {
        "first_audio": {},
        "completed": {},
    }
    events = staged_pipeline.get("events")
    if not isinstance(events, list):
        return
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("stage") != "tts":
            continue
        event_name = event.get("event")
        if event_name not in lifecycle:
            continue
        parent_id = event.get("sequence_id")
        if (
            not isinstance(parent_id, int)
            or isinstance(parent_id, bool)
            or parent_id < 0
        ):
            errors.append(
                f"events[{index}] TTS lifecycle sequence_id is invalid"
            )
            continue
        if parent_id in lifecycle[event_name]:
            errors.append(
                f"parent {parent_id} must have exactly one "
                f"tts/{event_name} lifecycle row"
            )
            continue
        monotonic_ms = _handoff_timestamp(
            event.get("monotonic_ms"),
            field_name=f"events[{index}].monotonic_ms",
            errors=errors,
        )
        lifecycle[event_name][parent_id] = {
            "monotonic_ms": monotonic_ms,
            "audio_bytes": event.get("audio_bytes"),
            "retry_count": event.get("retry_count"),
        }

    expected_parents = set(parent_metadata)
    for event_name, rows in lifecycle.items():
        if set(rows) != expected_parents:
            errors.append(
                f"tts/{event_name} lifecycle rows must exactly cover "
                "canonical frame parents"
            )

    received_by_key = {
        row.get("key"): row for row in received_rows if row.get("key") is not None
    }
    parent_cumulative_bytes: dict[int, int] = {}
    for key, frame_audio_bytes in zip(canonical_keys, canonical_bytes):
        parent_id, _frame_id = key
        metadata = parent_metadata.get(parent_id)
        response_rows = response_groups.get((parent_id, 0, 1))
        received = received_by_key.get(key)
        if metadata is None or not response_rows or received is None:
            continue
        expected_audio_bytes, expected_retry, fallback_applied = metadata
        first_audio = lifecycle["first_audio"].get(parent_id)
        completed = lifecycle["completed"].get(parent_id)
        if first_audio is None or completed is None:
            continue
        first_audio_ms = first_audio["monotonic_ms"]
        completed_ms = completed["monotonic_ms"]
        if (
            first_audio_ms is not None
            and not math.isclose(
                first_audio_ms,
                response_rows[0]["received_monotonic_ms"],
                rel_tol=0.0,
                abs_tol=_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS,
            )
        ):
            errors.append(
                f"parent {parent_id} tts/first_audio does not match "
                "the first response chunk"
            )
        if (
            completed_ms is not None
            and response_rows[-1]["received_monotonic_ms"]
            > completed_ms + _PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS
        ):
            errors.append(
                f"parent {parent_id} final response chunk follows "
                "tts/completed"
            )
        if (
            completed["audio_bytes"] != expected_audio_bytes
            or completed["retry_count"] != expected_retry
        ):
            errors.append(
                f"parent {parent_id} tts/completed bytes or retry "
                "do not match its canonical completion"
            )

        ready_ms = received.get("monotonic_ms")
        if ready_ms is None or completed_ms is None:
            continue
        if fallback_applied:
            if not math.isclose(
                ready_ms,
                completed_ms,
                rel_tol=0.0,
                abs_tol=_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS,
            ):
                errors.append(
                    f"atomic fallback parent {parent_id} frame-ready timing "
                    "must match tts/completed"
                )
            continue

        cumulative = (
            parent_cumulative_bytes.get(parent_id, 0) + frame_audio_bytes
        )
        parent_cumulative_bytes[parent_id] = cumulative
        response_boundary = next(
            (
                row
                for row in response_rows
                if row["cumulative_audio_bytes"] >= cumulative
            ),
            None,
        )
        if response_boundary is None or not math.isclose(
            ready_ms,
            response_boundary["received_monotonic_ms"],
            rel_tol=0.0,
            abs_tol=_PUBLISHER_HANDOFF_TIMING_TOLERANCE_MS,
        ):
            errors.append(
                f"direct frame {key} readiness does not match cumulative "
                "TTS response bytes"
            )


def _validate_v3_publisher_handoff_telemetry(
    staged_pipeline: dict[str, Any],
    staged_config: dict[str, Any] | None,
    canonical_keys: list[tuple[int, int]] | None,
    canonical_bytes: list[int] | None,
    errors: list[str],
) -> None:
    """Fail closed for the optional privacy-safe publisher-handoff trace."""
    marker_name = "tts_publisher_handoff_telemetry_enabled"
    marker_present = marker_name in staged_pipeline
    marker = staged_pipeline.get(marker_name, _MISSING)
    if marker_present and not isinstance(marker, bool):
        errors.append(
            "tts_publisher_handoff_telemetry_enabled must be a boolean"
        )
    elif marker is False:
        errors.append(
            "tts_publisher_handoff_telemetry_enabled=false is invalid; "
            "legacy captures must omit the marker"
        )

    config_marker: Any = _MISSING
    config_marker_valid = True
    config_marker_present = False
    if staged_config is not None:
        config_marker_name = "ttsPublisherHandoffTelemetryEnabled"
        config_marker_present = config_marker_name in staged_config
        config_marker = staged_config.get(
            config_marker_name,
            _MISSING,
        )
        if config_marker_present and not isinstance(config_marker, bool):
            errors.append(
                "/api/config.stagedConfig."
                "ttsPublisherHandoffTelemetryEnabled must be a boolean"
            )
            config_marker_valid = False
        if config_marker_present and config_marker_valid:
            computed_capability = (
                staged_config.get("ttsIncrementalPublishEnabled") is True
                and staged_config.get("ttsResponseChunkTelemetryEnabled")
                is True
            )
            if config_marker is not computed_capability:
                errors.append(
                    "/api/config.stagedConfig."
                    "ttsPublisherHandoffTelemetryEnabled must equal "
                    "ttsIncrementalPublishEnabled && "
                    "ttsResponseChunkTelemetryEnabled"
                )

    events = staged_pipeline.get("events")
    websocket_events = staged_pipeline.get("websocket_send_events")
    has_boundary_fields = (
        isinstance(events, list)
        and any(
            isinstance(event, dict)
            and any(field in event for field in _PUBLISHER_HANDOFF_FIELDS)
            for event in events
        )
    )
    has_send_started = (
        isinstance(websocket_events, list)
        and any(
            isinstance(event, dict)
            and "send_started_monotonic_ms" in event
            for event in websocket_events
        )
    )
    if marker is not True:
        if config_marker is True:
            errors.append(
                "enabled publisher-handoff config is missing the active "
                "summary marker"
            )
        if has_boundary_fields or has_send_started:
            errors.append(
                "publisher-handoff timing fields require "
                "tts_publisher_handoff_telemetry_enabled=true"
            )
        return
    if not config_marker_present or config_marker is not True:
        errors.append(
            "tts_publisher_handoff_telemetry_enabled=true requires "
            "/api/config.stagedConfig."
            "ttsPublisherHandoffTelemetryEnabled=true"
        )
    if staged_pipeline.get("tts_response_chunk_telemetry_enabled") is not True:
        errors.append(
            "publisher-handoff telemetry requires "
            "tts_response_chunk_telemetry_enabled=true"
        )
    if (
        staged_config is None
        or staged_config.get("ttsResponseChunkTelemetryEnabled") is not True
    ):
        errors.append(
            "publisher-handoff telemetry requires "
            "/api/config.stagedConfig."
            "ttsResponseChunkTelemetryEnabled=true"
        )

    if not isinstance(events, list) or not isinstance(websocket_events, list):
        return
    event_names = (
        ("tts", "frame_received"),
        ("output", "frame_enqueued"),
        ("output", "frame_dequeued"),
    )
    frame_rows: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = {event_name: [] for event_name in event_names}
    summary_session_id = staged_pipeline.get("session_id")
    if not isinstance(summary_session_id, str) or not summary_session_id:
        errors.append(
            "publisher-handoff telemetry requires a nonempty summary session_id"
        )
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        event_name = (event.get("stage"), event.get("event"))
        if (
            event_name != ("output", "frame_enqueued")
            and any(field in event for field in _PUBLISHER_HANDOFF_FIELDS)
        ):
            errors.append(
                f"events[{index}] publisher-handoff fields require "
                "output/frame_enqueued"
            )
        if event_name not in frame_rows:
            continue
        _reject_unsafe_telemetry_fields(
            event,
            allowed_fields=_PIPELINE_EVENT_SAFE_FIELDS,
            field_name=f"events[{index}]",
            errors=errors,
        )
        key = _audio_frame_key(
            event,
            field_name=f"events[{index}]",
            errors=errors,
            sequence_alias=event.get("sequence_id"),
        )
        audio_bytes = event.get("audio_bytes")
        retry_count = event.get("retry_count")
        monotonic_ms = _handoff_timestamp(
            event.get("monotonic_ms"),
            field_name=f"events[{index}].monotonic_ms",
            errors=errors,
        )
        if not _positive_int(audio_bytes):
            errors.append(f"events[{index}].audio_bytes is invalid")
        if (
            not isinstance(retry_count, int)
            or isinstance(retry_count, bool)
            or retry_count not in {0, 1}
        ):
            errors.append(
                f"events[{index}].retry_count must be zero or one"
            )
        normalized: dict[str, Any] = {
            "key": key,
            "audio_bytes": audio_bytes,
            "retry_count": retry_count,
            "monotonic_ms": monotonic_ms,
            "metadata": _publisher_frame_metadata(
                event,
                summary_session_id=summary_session_id,
                field_name=f"events[{index}]",
                errors=errors,
            ),
        }
        if event_name == ("output", "frame_enqueued"):
            for timing_field in _PUBLISHER_HANDOFF_FIELDS:
                normalized[timing_field] = _handoff_timestamp(
                    event.get(timing_field),
                    field_name=f"events[{index}].{timing_field}",
                    errors=errors,
                )
            blocked_put_ms = _handoff_timestamp(
                event.get("blocked_put_ms"),
                field_name=f"events[{index}].blocked_put_ms",
                errors=errors,
            )
            normalized["blocked_put_ms"] = blocked_put_ms
        frame_rows[event_name].append(normalized)

    expected_pairs = (
        list(zip(canonical_keys, canonical_bytes))
        if canonical_keys is not None and canonical_bytes is not None
        else None
    )
    layer_records: dict[
        tuple[str, str],
        list[tuple[tuple[int, int] | None, Any, Any]],
    ] = {
        event_name: [
            (row["key"], row["audio_bytes"], row["retry_count"])
            for row in rows
        ]
        for event_name, rows in frame_rows.items()
    }
    if expected_pairs is not None:
        for (stage, event_name), rows in layer_records.items():
            if [(key, audio_bytes) for key, audio_bytes, _retry in rows] != (
                expected_pairs
            ):
                errors.append(
                    f"{stage}/{event_name} must contain exactly one canonical "
                    "frame row in published order"
                )
        retries = [
            retry_count
            for _key, _audio_bytes, retry_count in layer_records[
                ("tts", "frame_received")
            ]
        ]
        for (stage, event_name), rows in layer_records.items():
            if [retry for _key, _audio_bytes, retry in rows] != retries:
                errors.append(
                    f"{stage}/{event_name} retry coverage must exactly match "
                    "tts/frame_received"
                )
        canonical_metadata = [
            row["metadata"]
            for row in frame_rows[("tts", "frame_received")]
        ]
        for (stage, event_name), rows in frame_rows.items():
            if [row["metadata"] for row in rows] != canonical_metadata:
                errors.append(
                    f"{stage}/{event_name} source and session metadata must "
                    "exactly match tts/frame_received"
                )
        raw_parent_summaries = staged_pipeline.get(
            "produced_parent_summaries"
        )
        parent_retry = {
            item.get("parent_sequence_id"): item.get("retry_count")
            for item in (
                raw_parent_summaries
                if isinstance(raw_parent_summaries, list)
                else []
            )
            if isinstance(item, dict)
        }
        for key, _audio_bytes, retry_count in layer_records[
            ("tts", "frame_received")
        ]:
            if key is not None and retry_count != parent_retry.get(key[0]):
                errors.append(
                    f"frame {key} retry_count must match its parent completion"
                )

    websocket_rows: list[dict[str, Any]] = []
    for index, event in enumerate(websocket_events):
        if not isinstance(event, dict):
            continue
        _reject_unsafe_telemetry_fields(
            event,
            allowed_fields=_WEBSOCKET_FRAME_SAFE_FIELDS,
            field_name=f"websocket_send_events[{index}]",
            errors=errors,
        )
        key = _audio_frame_key(
            event,
            field_name=f"websocket_send_events[{index}]",
            errors=errors,
            sequence_alias=event.get("sequence_id"),
        )
        audio_bytes = event.get("audio_bytes")
        if not _positive_int(audio_bytes):
            errors.append(
                f"websocket_send_events[{index}].audio_bytes is invalid"
            )
        websocket_rows.append(
            {
                "key": key,
                "audio_bytes": audio_bytes,
                "send_started_monotonic_ms": _handoff_timestamp(
                    event.get("send_started_monotonic_ms"),
                    field_name=(
                        f"websocket_send_events[{index}]."
                        "send_started_monotonic_ms"
                    ),
                    errors=errors,
                ),
                "sent_monotonic_ms": _handoff_timestamp(
                    event.get("sent_monotonic_ms"),
                    field_name=(
                        f"websocket_send_events[{index}].sent_monotonic_ms"
                    ),
                    errors=errors,
                ),
            }
        )
    if (
        expected_pairs is not None
        and [
            (row["key"], row["audio_bytes"]) for row in websocket_rows
        ]
        != expected_pairs
    ):
        errors.append(
            "websocket_send_events must contain exactly one canonical "
            "frame row in published order"
        )

    layer_lengths_match = (
        canonical_keys is not None
        and all(
            len(rows) == len(canonical_keys)
            for rows in (*frame_rows.values(), websocket_rows)
        )
    )
    if layer_lengths_match:
        boundary_columns = {
            name: []
            for name in (
                "frame_received",
                "publish_requested",
                "callback_started",
                "capacity_acquired",
                "frame_enqueued",
                "frame_dequeued",
                "send_started",
                "sent",
            )
        }
        for index, key in enumerate(canonical_keys):
            received = frame_rows[("tts", "frame_received")][index]
            enqueued = frame_rows[("output", "frame_enqueued")][index]
            dequeued = frame_rows[("output", "frame_dequeued")][index]
            websocket = websocket_rows[index]
            ordered = (
                received["monotonic_ms"],
                enqueued["publish_requested_monotonic_ms"],
                enqueued["event_loop_callback_started_monotonic_ms"],
                enqueued["output_capacity_acquired_monotonic_ms"],
                enqueued["monotonic_ms"],
                dequeued["monotonic_ms"],
                websocket["send_started_monotonic_ms"],
                websocket["sent_monotonic_ms"],
            )
            for boundary_name, value in zip(boundary_columns, ordered):
                boundary_columns[boundary_name].append(value)
            if all(value is not None for value in ordered) and any(
                later < earlier
                for earlier, later in zip(ordered, ordered[1:])
            ):
                errors.append(
                    f"publisher-handoff timestamps are inverted for frame {key}"
                )
            callback_ms = enqueued[
                "event_loop_callback_started_monotonic_ms"
            ]
            capacity_ms = enqueued[
                "output_capacity_acquired_monotonic_ms"
            ]
            blocked_put_ms = enqueued["blocked_put_ms"]
            if (
                callback_ms is not None
                and capacity_ms is not None
                and blocked_put_ms is not None
                and blocked_put_ms > 0
                and not math.isclose(
                    blocked_put_ms,
                    capacity_ms - callback_ms,
                    rel_tol=0.0,
                    abs_tol=_PUBLISHER_HANDOFF_BLOCKED_TOLERANCE_MS,
                )
            ):
                errors.append(
                    f"output/frame_enqueued blocked_put_ms does not reconcile "
                    f"with capacity wait for frame {key}"
                )
        for boundary_name, values in boundary_columns.items():
            if all(value is not None for value in values) and any(
                later < earlier
                for earlier, later in zip(values, values[1:])
            ):
                errors.append(
                    f"publisher-handoff {boundary_name} timestamps must be "
                    "globally nondecreasing in canonical frame order"
                )

    expected_frames: list[tuple[tuple[int, int], int, int]] = []
    if expected_pairs is not None:
        tts_rows = layer_records[("tts", "frame_received")]
        if len(tts_rows) == len(expected_pairs):
            expected_frames = [
                (key, audio_bytes, retry_count)
                for key, audio_bytes, retry_count in tts_rows
                if key is not None
                and _positive_int(audio_bytes)
                and isinstance(retry_count, int)
                and not isinstance(retry_count, bool)
                and retry_count in {0, 1}
            ]
    frame_bytes_per_ms: float | None = None
    frame_bytes = staged_pipeline.get("tts_incremental_frame_bytes")
    frame_ms = (
        staged_config.get("ttsIncrementalFrameMs")
        if staged_config is not None
        else None
    )
    if _positive_int(frame_bytes) and _positive_int(frame_ms):
        frame_bytes_per_ms = frame_bytes / frame_ms
    response_groups = _validate_v3_response_chunk_sidecar(
        staged_pipeline,
        expected_frames,
        frame_bytes_per_ms=frame_bytes_per_ms,
        errors=errors,
    )
    _validate_v3_response_frame_boundaries(
        staged_pipeline,
        response_groups,
        canonical_keys,
        canonical_bytes,
        frame_rows[("tts", "frame_received")],
        errors,
    )


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
    _validate_v3_publisher_handoff_telemetry(
        staged_pipeline,
        staged_config,
        canonical_keys,
        canonical_bytes,
        errors,
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


def _canonical_capture_time(
    monotonic_timestamp: float,
    client_clock_origin: float,
) -> tuple[float, float]:
    """Return one persisted millisecond value and its scheduler coordinate."""

    timestamp_ms = (
        monotonic_timestamp - client_clock_origin
    ) * 1000
    return timestamp_ms, timestamp_ms / 1000.0


def _relative_pacing_margin_ms(
    *,
    send_timestamp: float,
    client_clock_origin: float,
    sample_zero_timestamp_ms: float,
    chunk_index: int,
    chunk_duration_ms: float,
) -> float:
    """Compute a pacing margin in the exact coordinate system we persist."""

    return (
        (send_timestamp - client_clock_origin) * 1000
        - sample_zero_timestamp_ms
        - (chunk_index + 1) * chunk_duration_ms
    )


def validate_input_pacing_evidence(
    result: TestResult,
    *,
    require_chunk_events: bool,
    timing_tolerance_ms: float = 1e-6,
    numeric_early_tolerance_ms: float = 1e-4,
) -> list[str]:
    """Validate measured end-boundary pacing provenance.

    Protocol-v1 capture objects retain the complete client event ledger, so
    their recorded extrema are recomputed here. A saved summary contains the
    same fixed clock identity, source-sample-zero anchor, count, and measured
    extrema but not the full ledger; artifact validation separately replays
    the CSV and calls this validator with ``require_chunk_events=True``.
    """

    errors: list[str] = []
    pacing = result.input_pacing
    if not isinstance(pacing, dict):
        return ["audio metadata capture requires input pacing provenance"]
    if set(pacing) != INPUT_PACING_FIELDS:
        errors.append(
            "audio metadata input pacing provenance fields are invalid"
        )
    if pacing.get("mode") != INPUT_PACING_MODE:
        errors.append(
            "audio metadata input pacing mode must be "
            f"{INPUT_PACING_MODE!r}"
        )
    pacing_duration_ms = pacing.get("chunk_duration_ms")
    duration_valid = (
        isinstance(pacing_duration_ms, (int, float))
        and not isinstance(pacing_duration_ms, bool)
        and math.isfinite(pacing_duration_ms)
        and math.isclose(
            float(pacing_duration_ms),
            CHUNK_DURATION * 1000,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )
    if not duration_valid:
        errors.append(
            "audio metadata input pacing chunk duration is invalid"
        )
    if (
        pacing.get("source_sample_zero_clock")
        != INPUT_SAMPLE_ZERO_CLOCK
    ):
        errors.append(
            "audio metadata input sample-zero clock must be "
            f"{INPUT_SAMPLE_ZERO_CLOCK!r}"
        )
    if pacing.get("deadline_basis") != INPUT_PACING_DEADLINE_BASIS:
        errors.append(
            "audio metadata input pacing deadline basis is invalid"
        )

    anchor_ms = pacing.get("source_sample_zero_timestamp_ms")
    anchor_valid = (
        isinstance(anchor_ms, (int, float))
        and not isinstance(anchor_ms, bool)
        and math.isfinite(anchor_ms)
        and anchor_ms >= 0
    )
    if not anchor_valid:
        errors.append(
            "audio metadata input pacing source-sample-zero timestamp "
            "is invalid"
        )
    elif (
        isinstance(result.input_sample_zero_timestamp_ms, (int, float))
        and not isinstance(result.input_sample_zero_timestamp_ms, bool)
        and math.isfinite(result.input_sample_zero_timestamp_ms)
        and not math.isclose(
            float(anchor_ms),
            float(result.input_sample_zero_timestamp_ms),
            rel_tol=0.0,
            abs_tol=1e-6,
        )
    ):
        errors.append(
            "audio metadata input pacing source-sample-zero timestamp "
            "does not match the observation"
        )

    observed_count = pacing.get("observed_chunk_count")
    count_valid = (
        isinstance(observed_count, int)
        and not isinstance(observed_count, bool)
        and observed_count > 0
    )
    if not count_valid:
        errors.append(
            "audio metadata input pacing observed chunk count is invalid"
        )
    elif (
        not isinstance(result.chunks_sent, int)
        or isinstance(result.chunks_sent, bool)
        or observed_count != result.chunks_sent
    ):
        errors.append(
            "audio metadata input pacing observed chunk count does not "
            "match chunks_sent"
        )

    minimum_margin = pacing.get("min_emission_minus_deadline_ms")
    maximum_margin = pacing.get("max_emission_minus_deadline_ms")
    margins_valid = all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in (minimum_margin, maximum_margin)
    )
    if not margins_valid:
        errors.append(
            "audio metadata input pacing emission margins are invalid"
        )
    elif minimum_margin > maximum_margin:
        errors.append(
            "audio metadata input pacing emission margin range is invalid"
        )
    elif minimum_margin < -numeric_early_tolerance_ms:
        errors.append(
            "audio metadata input pacing contains an early chunk emission"
        )

    if not require_chunk_events:
        return errors

    chunk_events = [
        event
        for event in result.client_events
        if isinstance(event, TimingEvent)
        and event.source == "client"
        and event.stage == "chunk_sent"
    ]
    if not count_valid or len(chunk_events) != observed_count:
        errors.append(
            "audio metadata input pacing event count does not match "
            "observed_chunk_count"
        )
        return errors
    if not duration_valid or not anchor_valid:
        return errors

    calculated_margins: list[float] = []
    expected_indices = list(range(len(chunk_events)))
    observed_indices = [event.chunk_index for event in chunk_events]
    if observed_indices != expected_indices:
        errors.append(
            "audio metadata input pacing chunk indices are not contiguous"
        )
    for expected_index, event in enumerate(chunk_events):
        if (
            not isinstance(event.timestamp_ms, (int, float))
            or isinstance(event.timestamp_ms, bool)
            or not math.isfinite(event.timestamp_ms)
        ):
            errors.append(
                "audio metadata input pacing chunk timestamp is invalid"
            )
            continue
        expected_source_position = (
            expected_index * float(pacing_duration_ms) / 1000
        )
        if (
            not isinstance(event.source_position_sec, (int, float))
            or isinstance(event.source_position_sec, bool)
            or not math.isfinite(event.source_position_sec)
            or not math.isclose(
                float(event.source_position_sec),
                expected_source_position,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            errors.append(
                "audio metadata input pacing source position is invalid"
            )
        deadline_ms = (
            float(anchor_ms)
            + (expected_index + 1) * float(pacing_duration_ms)
        )
        calculated_margins.append(float(event.timestamp_ms) - deadline_ms)

    if len(calculated_margins) != len(chunk_events):
        return errors
    calculated_minimum = min(calculated_margins)
    calculated_maximum = max(calculated_margins)
    if calculated_minimum < -numeric_early_tolerance_ms:
        errors.append(
            "audio metadata input pacing event ledger contains an early "
            "chunk emission"
        )
    if margins_valid and (
        not math.isclose(
            float(minimum_margin),
            calculated_minimum,
            rel_tol=0.0,
            abs_tol=timing_tolerance_ms,
        )
        or not math.isclose(
            float(maximum_margin),
            calculated_maximum,
            rel_tol=0.0,
            abs_tol=timing_tolerance_ms,
        )
    ):
        errors.append(
            "audio metadata input pacing emission margins do not match "
            "the client event ledger"
        )
    return errors


def validate_audio_metadata_observation(
    result: TestResult,
    *,
    require_pacing_chunk_events: bool = True,
) -> list[str]:
    """Replay captured wire evidence and fail closed on v1 inconsistencies."""

    if result.audio_metadata_protocol_version is None:
        if any(
            isinstance(event, dict)
            and event.get("message_type")
            in {"audio_frame", "audio_parent_complete"}
            for event in result.websocket_receive_events
        ):
            return [
                "audio metadata was captured without protocol negotiation"
            ]
        return []
    if (
        result.audio_metadata_protocol_version
        != AUDIO_METADATA_PROTOCOL_VERSION
    ):
        return ["audio metadata protocol version must equal 1"]

    errors = validate_input_pacing_evidence(
        result,
        require_chunk_events=require_pacing_chunk_events,
    )

    tracker = AudioMetadataTracker(
        enabled=True,
        protocol_version=result.audio_metadata_protocol_version,
    )
    observed_orders: list[int] = []
    completed_terminals = 0
    frame_fields = (
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
    completion_fields = (
        "protocolVersion",
        "streamGeneration",
        "parentSequenceId",
        "audioFrameCount",
        "audioBytes",
        "sourceStartMs",
        "sourceEndMs",
    )

    for index, event in enumerate(result.websocket_receive_events):
        if not isinstance(event, dict):
            errors.append(
                f"websocket_receive_events[{index}] must be an object"
            )
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
        else:
            observed_orders.append(order)
        try:
            if event.get("frame_type") == "pcm":
                paired = tracker.accept_binary_size(event.get("audio_bytes"))
                if paired is None:
                    raise AudioMetadataProtocolError(
                        "negotiated PCM did not pair with metadata"
                    )
                captured = {
                    field_name: event.get(field_name)
                    for field_name in frame_fields
                }
                if captured != {
                    field_name: paired[field_name]
                    for field_name in frame_fields
                }:
                    raise AudioMetadataProtocolError(
                        "PCM metadata does not match its preceding header"
                    )
                if (
                    paired["sourceEndMs"] is not None
                    and result.input_sample_zero_timestamp_ms is not None
                ):
                    expected_delay = (
                        float(event["timestamp_ms"])
                        - result.input_sample_zero_timestamp_ms
                        - paired["sourceEndMs"]
                    )
                    observed_delay = event.get(
                        "sourceEndToReceiptMs"
                    )
                    if (
                        not isinstance(observed_delay, (int, float))
                        or isinstance(observed_delay, bool)
                        or not math.isfinite(observed_delay)
                        or not math.isclose(
                            float(observed_delay),
                            expected_delay,
                            rel_tol=0.0,
                            abs_tol=1e-6,
                        )
                    ):
                        raise AudioMetadataProtocolError(
                            "sourceEndToReceiptMs is inconsistent"
                        )
                continue

            message_type = event.get("message_type")
            if message_type == "audio_frame":
                payload = {
                    "type": "audio_frame",
                    **{
                        field_name: event.get(field_name)
                        for field_name in frame_fields
                    },
                }
                tracker.accept_control(payload)
            elif message_type == "audio_parent_complete":
                payload = {
                    "type": "audio_parent_complete",
                    **{
                        field_name: event.get(field_name)
                        for field_name in completion_fields
                    },
                }
                tracker.accept_control(payload)
            else:
                tracker.accept_control({"type": message_type})
                if (
                    message_type == "status"
                    and event.get("status") == "completed"
                ):
                    tracker.assert_terminal_ready()
                    completed_terminals += 1
        except (AudioMetadataProtocolError, KeyError, TypeError, ValueError) as exc:
            errors.append(
                f"websocket_receive_events[{index}] metadata invalid: {exc}"
            )

    if observed_orders != list(range(len(observed_orders))):
        errors.append(
            "audio metadata receive order must be contiguous from zero"
        )
    if completed_terminals != 1:
        errors.append(
            "audio metadata capture requires exactly one completed terminal"
        )
    if tracker.pending_frame is not None:
        errors.append("audio metadata capture has a dangling frame header")
    if tracker.active_parent_sequence_id is not None:
        errors.append("audio metadata capture has an incomplete parent")
    if result.audio_metadata_stream_generation != tracker.stream_generation:
        errors.append(
            "audio metadata stream generation summary does not reconcile"
        )
    if result.audio_metadata_paired_frames != len(tracker.paired_frames):
        errors.append("audio metadata paired-frame count does not reconcile")
    if result.audio_metadata_completed_parents != len(
        tracker.completed_parents
    ):
        errors.append(
            "audio metadata completed-parent count does not reconcile"
        )
    if result.audio_metadata_paired_frames != result.audio_responses:
        errors.append(
            "every translated PCM response must have one paired frame header"
        )
    configured_sample_rate = result.backend_config.get("sampleRate")
    configured_channels = result.backend_config.get("channels")
    if any(
        frame["sampleRateHz"] != configured_sample_rate
        or frame["channels"] != configured_channels
        for frame in tracker.paired_frames
    ):
        errors.append(
            "wire PCM format does not match /api/config audio format"
        )
    staged = result.staged_pipeline
    if not isinstance(staged, dict):
        errors.append(
            "audio metadata capture requires staged pipeline evidence"
        )
    else:
        send_events = staged.get("websocket_send_events")
        if not isinstance(send_events, list):
            errors.append(
                "audio metadata capture requires WebSocket send evidence"
            )
        else:
            expected_frames = [
                {
                    "parentSequenceId": event.get(
                        "parent_sequence_id"
                    ),
                    "audioFrameId": event.get("audio_frame_id"),
                    "audioBytes": event.get("audio_bytes"),
                }
                for event in send_events
                if isinstance(event, dict)
            ]
            observed_frames = [
                {
                    "parentSequenceId": frame["parentSequenceId"],
                    "audioFrameId": frame["audioFrameId"],
                    "audioBytes": frame["audioBytes"],
                }
                for frame in tracker.paired_frames
            ]
            if expected_frames != observed_frames:
                errors.append(
                    "wire frame metadata does not reconcile with server "
                    "WebSocket send evidence"
                )
        parent_summaries = staged.get(
            "websocket_completed_parent_summaries"
        )
        if not isinstance(parent_summaries, list):
            errors.append(
                "audio metadata capture requires server parent-completion "
                "evidence"
            )
        else:
            expected_parents = [
                {
                    "parentSequenceId": parent.get(
                        "parent_sequence_id"
                    ),
                    "audioFrameCount": parent.get("audio_frame_count"),
                    "audioBytes": parent.get("audio_bytes"),
                }
                for parent in parent_summaries
                if isinstance(parent, dict)
            ]
            observed_parents = [
                {
                    "parentSequenceId": parent["parentSequenceId"],
                    "audioFrameCount": parent["audioFrameCount"],
                    "audioBytes": parent["audioBytes"],
                }
                for parent in tracker.completed_parents
            ]
            if expected_parents != observed_parents:
                errors.append(
                    "wire parent completions do not reconcile with server "
                    "completion evidence"
                )
    if (
        not isinstance(result.input_sample_zero_timestamp_ms, (int, float))
        or isinstance(result.input_sample_zero_timestamp_ms, bool)
        or not math.isfinite(result.input_sample_zero_timestamp_ms)
        or result.input_sample_zero_timestamp_ms < 0
    ):
        errors.append(
            "audio metadata capture requires an input sample-zero marker"
        )
    return errors


def validate_synthesized_pcm_silence_capture(
    result: TestResult,
) -> list[str]:
    """Reconcile an optional low-energy PCM diagnostic with wire evidence."""

    observation = result.synthesized_pcm_silence
    processing = result.synthesized_pcm_silence_processing
    if observation is None:
        errors = []
        if result.synthesized_pcm_silence_requested:
            errors.append(
                "requested synthesized PCM silence diagnostic is missing"
            )
        if processing is not None:
            errors.append(
                "synthesized PCM silence processing evidence is present "
                "without an observation"
            )
        return errors

    errors: list[str] = []
    if not result.synthesized_pcm_silence_requested:
        errors.append(
            "synthesized PCM silence observation was present without being "
            "requested"
        )
    if (
        result.audio_metadata_protocol_version
        != AUDIO_METADATA_PROTOCOL_VERSION
    ):
        errors.append(
            "synthesized PCM silence diagnostic requires audio metadata "
            "protocol version 1"
        )
    staged_config = result.backend_config.get("stagedConfig")
    if (
        result.pipeline_mode != "staged"
        or not isinstance(staged_config, dict)
        or staged_config.get("telemetrySchemaVersion") != 3
        or staged_config.get("ttsIncrementalPublishEnabled") is not True
    ):
        errors.append(
            "synthesized PCM silence diagnostic requires the staged "
            "schema-3 incremental-publication path"
        )

    try:
        normalized = validate_synthesized_pcm_silence_observation(
            observation
        )
    except (SynthesizedPcmSilenceError, TypeError, ValueError) as exc:
        errors.append(
            "synthesized PCM silence observation is invalid: "
            f"{exc}"
        )
        return errors

    totals = normalized["totals"]
    if processing is None:
        errors.append(
            "synthesized PCM silence processing evidence is missing"
        )
    else:
        try:
            validated_processing = (
                validate_synthesized_pcm_silence_processing(
                    processing,
                    expected_frame_count=totals["frame_count"],
                )
            )
        except (TypeError, ValueError) as exc:
            errors.append(
                "synthesized PCM silence processing evidence is invalid: "
                f"{exc}"
            )
        else:
            if validated_processing["gate_passed"] is not True:
                errors.append(
                    "synthesized PCM silence processing overhead gate did "
                    "not pass"
                )
    parent_rows = normalized["parent_threshold_rows"]
    thresholds = normalized["method"]["thresholds_dbfs"]
    primary_threshold = normalized["method"]["primary_threshold_dbfs"]
    primary_rows = [
        row
        for row in parent_rows
        if row["threshold_dbfs"] == primary_threshold
    ]

    if totals["parent_count"] != result.audio_metadata_completed_parents:
        errors.append(
            "synthesized PCM silence parent count does not match protocol "
            "completion evidence"
        )
    if totals["frame_count"] != result.audio_metadata_paired_frames:
        errors.append(
            "synthesized PCM silence frame count does not match paired "
            "protocol frames"
        )
    if totals["frame_count"] != result.audio_responses:
        errors.append(
            "synthesized PCM silence frame count does not match translated "
            "responses"
        )
    if totals["audio_bytes"] != result.total_received_bytes:
        errors.append(
            "synthesized PCM silence byte count does not match translated "
            "audio"
        )
    expected_stream_generation = result.audio_metadata_stream_generation
    if totals["stream_generation"] != expected_stream_generation:
        errors.append(
            "synthesized PCM silence stream generation does not match "
            "protocol evidence"
        )

    frame_rows: dict[int, list[dict[str, Any]]] = {}
    completion_rows: list[dict[str, Any]] = []
    for event in result.websocket_receive_events:
        if not isinstance(event, dict):
            continue
        if event.get("frame_type") == "pcm":
            parent_id = event.get("parentSequenceId")
            if isinstance(parent_id, int) and not isinstance(parent_id, bool):
                frame_rows.setdefault(parent_id, []).append(event)
        elif event.get("message_type") == "audio_parent_complete":
            completion_rows.append(event)

    expected_parent_ids = list(range(len(primary_rows)))
    if [row["parent_sequence_id"] for row in primary_rows] != (
        expected_parent_ids
    ):
        errors.append(
            "synthesized PCM silence parent IDs are not contiguous from zero"
        )
    if len(completion_rows) != len(primary_rows):
        errors.append(
            "synthesized PCM silence parents do not match wire completion "
            "count"
        )

    for parent_index, parent in enumerate(primary_rows):
        parent_id = parent["parent_sequence_id"]
        observed_frames = frame_rows.get(parent_id, [])
        observed_audio_bytes = 0
        frame_bytes_valid = True
        for frame_index, event in enumerate(observed_frames):
            audio_bytes = event.get("audio_bytes")
            if (
                not isinstance(audio_bytes, int)
                or isinstance(audio_bytes, bool)
                or audio_bytes <= 0
            ):
                errors.append(
                    "synthesized PCM silence paired wire frame "
                    f"{parent_id}:{frame_index} has invalid audio bytes"
                )
                frame_bytes_valid = False
            else:
                observed_audio_bytes += audio_bytes
        if (
            parent["frame_count"] != len(observed_frames)
            or (
                frame_bytes_valid
                and parent["audio_bytes"] != observed_audio_bytes
            )
        ):
            errors.append(
                f"synthesized PCM silence parent {parent_id} does not "
                "reconcile with paired wire frames"
            )
        if parent_index < len(completion_rows):
            completion = completion_rows[parent_index]
            if (
                completion.get("parentSequenceId") != parent_id
                or completion.get("audioFrameCount")
                != parent["frame_count"]
                or completion.get("audioBytes") != parent["audio_bytes"]
            ):
                errors.append(
                    f"synthesized PCM silence parent {parent_id} does not "
                    "reconcile with its wire completion"
                )

    # Every threshold must carry the exact same parent identity and transport
    # totals. The core validator checks this internally; this explicit replay
    # also ties each sensitivity row to the independent wire ledger.
    for threshold in thresholds:
        threshold_rows = [
            row
            for row in parent_rows
            if row["threshold_dbfs"] == threshold
        ]
        if len(threshold_rows) != len(primary_rows):
            errors.append(
                "synthesized PCM silence threshold rows do not cover every "
                "completed parent"
            )
            continue
        for primary, candidate in zip(
            primary_rows,
            threshold_rows,
            strict=True,
        ):
            for field_name in (
                "stream_generation",
                "parent_sequence_id",
                "frame_count",
                "audio_bytes",
                "sample_count",
                "duration_ms",
                "full_window_count",
                "partial_window_count",
            ):
                if candidate[field_name] != primary[field_name]:
                    errors.append(
                        "synthesized PCM silence threshold rows disagree on "
                        f"parent transport field {field_name}"
                    )
                    break

    return errors


def validate_capture_result(
    result: TestResult,
    *,
    require_pacing_chunk_events: bool = True,
) -> list[str]:
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
    errors.extend(
        validate_audio_metadata_observation(
            result,
            require_pacing_chunk_events=require_pacing_chunk_events,
        )
    )
    if result.audio_metadata_protocol_version is None:
        if result.headless_playback_report is not None:
            errors.append(
                "headless playback report requires audio metadata "
                "protocol version 1"
            )
    elif not isinstance(result.headless_playback_report, dict):
        errors.append(
            "audio metadata capture requires a headless playback report"
        )
    else:
        report = result.headless_playback_report
        try:
            validate_headless_playback_report(
                report,
                expected_frames=result.audio_responses,
                expected_parents=(
                    result.audio_metadata_completed_parents
                ),
                expected_stream_generation=(
                    result.audio_metadata_stream_generation
                ),
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"headless playback report is invalid: {exc}")
        if report.get("schema_version") != 1:
            errors.append("headless playback report schema is invalid")
        if (
            report.get("report_type")
            != "headless_scheduled_digital_playback"
        ):
            errors.append("headless playback report type is invalid")
        capture = report.get("capture")
        if not isinstance(capture, dict):
            errors.append("headless playback capture summary is missing")
        else:
            if capture.get("frames_received") != result.audio_responses:
                errors.append(
                    "headless playback received-frame count does not "
                    "match translated responses"
                )
            if capture.get("frames_scheduled") != result.audio_responses:
                errors.append(
                    "headless playback scheduled-frame count does not "
                    "match translated responses"
                )
            if (
                capture.get("complete_parents")
                != result.audio_metadata_completed_parents
            ):
                errors.append(
                    "headless playback parent count does not match "
                    "protocol completion evidence"
                )
            if capture.get("canonical_replay_verified") is not True:
                errors.append(
                    "headless playback canonical replay was not verified"
                )
        queue_gate = report.get("queue_gate")
        if not isinstance(queue_gate, dict):
            errors.append("headless playback queue gate is missing")
        elif (
            queue_gate.get("frames_dropped") != 0
            or queue_gate.get("frames_reordered") != 0
            or queue_gate.get("frames_duplicated") != 0
            or queue_gate.get("all_frames_preserved_once_in_order")
            is not True
        ):
            errors.append(
                "headless playback did not preserve every frame once "
                "and in order"
            )
        privacy = report.get("privacy")
        if not isinstance(privacy, dict) or any(
            privacy.get(field_name) is not expected
            for field_name, expected in {
                "aggregate_only": True,
                "contains_pcm": False,
                "contains_transcript_or_translation_text": False,
                "contains_file_path_or_uri": False,
                "contains_wall_clock_timestamp": False,
            }.items()
        ):
            errors.append("headless playback privacy declaration is invalid")
    errors.extend(validate_synthesized_pcm_silence_capture(result))
    errors.extend(result.staged_integrity_errors)
    return errors


def compute_playback_metrics(result: TestResult) -> None:
    """Compute arrival-replay first-audio latency and listener-visible tail.

    Prefer the observed ``end_input`` send timestamp. In
    ``chunk_end_boundary_v1`` captures each ``chunk_sent`` timestamp already
    identifies that chunk's source-end boundary, so the fallback uses it
    directly. Legacy or unproven traces retain the historical behavior of
    adding each chunk's exact PCM duration to its send time.
    """
    playback_end_sec = 0.0
    estimated_input_end_sec = 0.0
    explicit_input_end_sec = (
        result.input_end_timestamp_ms / 1000
        if result.input_end_timestamp_ms > 0
        else None
    )
    result.first_audio_latency_sec = 0.0
    chunks_end_at_send = (
        result.audio_metadata_protocol_version
        == AUDIO_METADATA_PROTOCOL_VERSION
        and isinstance(result.input_pacing, dict)
        and set(result.input_pacing) == INPUT_PACING_FIELDS
        and result.input_pacing.get("mode") == INPUT_PACING_MODE
    )

    for event in sorted(result.client_events, key=lambda event: event.timestamp_ms):
        event_time_sec = event.timestamp_ms / 1000
        if event.stage == "chunk_sent":
            chunk_duration_sec = (
                0.0
                if chunks_end_at_send
                else event.audio_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            )
            estimated_input_end_sec = max(
                estimated_input_end_sec,
                event_time_sec + chunk_duration_sec,
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


def nearest_rank(values: list[float], quantile: float) -> float | None:
    """Return the nearest-rank quantile used by capture summaries."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def build_synthesized_pcm_silence_processing(
    durations_ms: list[float],
) -> dict[str, Any]:
    """Build aggregate-only evidence for inline diagnostic scan overhead."""

    if not durations_ms or any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
        for value in durations_ms
    ):
        raise ValueError(
            "synthesized PCM silence processing durations must be finite "
            "non-negative numbers"
        )
    numeric = [float(value) for value in durations_ms]
    frame_count = len(numeric)
    total_ms = round(sum(numeric), 6)
    p50_ms = round(float(nearest_rank(numeric, 0.50)), 6)
    p95_ms = round(float(nearest_rank(numeric, 0.95)), 6)
    max_ms = round(max(numeric), 6)
    mean_ms = round(total_ms / frame_count, 6)
    gate_passed = (
        p95_ms <= SYNTHESIZED_PCM_SCAN_P95_LIMIT_MS
        and max_ms <= SYNTHESIZED_PCM_SCAN_MAX_LIMIT_MS
    )
    return {
        "measurement_position": (
            SYNTHESIZED_PCM_SCAN_MEASUREMENT_POSITION
        ),
        "clock": SYNTHESIZED_PCM_SCAN_CLOCK,
        "frame_count": frame_count,
        "total_ms": total_ms,
        "mean_ms": mean_ms,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "max_ms": max_ms,
        "p95_limit_ms": SYNTHESIZED_PCM_SCAN_P95_LIMIT_MS,
        "max_limit_ms": SYNTHESIZED_PCM_SCAN_MAX_LIMIT_MS,
        "gate_passed": gate_passed,
        "privacy": dict(SYNTHESIZED_PCM_SCAN_PRIVACY),
    }


def validate_synthesized_pcm_silence_processing(
    value: Any,
    *,
    expected_frame_count: int | None = None,
) -> dict[str, Any]:
    """Validate aggregate inline-scan timing without accepting extra fields."""

    if not isinstance(value, dict) or set(value) != (
        SYNTHESIZED_PCM_SCAN_FIELDS
    ):
        raise ValueError(
            "synthesized PCM silence processing fields are invalid"
        )
    if (
        value.get("measurement_position")
        != SYNTHESIZED_PCM_SCAN_MEASUREMENT_POSITION
        or value.get("clock") != SYNTHESIZED_PCM_SCAN_CLOCK
    ):
        raise ValueError(
            "synthesized PCM silence processing measurement provenance is "
            "invalid"
        )
    frame_count = value.get("frame_count")
    if (
        not isinstance(frame_count, int)
        or isinstance(frame_count, bool)
        or frame_count <= 0
    ):
        raise ValueError(
            "synthesized PCM silence processing frame_count is invalid"
        )
    if (
        expected_frame_count is not None
        and frame_count != expected_frame_count
    ):
        raise ValueError(
            "synthesized PCM silence processing frame_count does not "
            "reconcile"
        )

    numeric_fields = (
        "total_ms",
        "mean_ms",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "p95_limit_ms",
        "max_limit_ms",
    )
    numeric: dict[str, float] = {}
    for field_name in numeric_fields:
        field_value = value.get(field_name)
        if (
            not isinstance(field_value, (int, float))
            or isinstance(field_value, bool)
            or not math.isfinite(field_value)
            or field_value < 0
        ):
            raise ValueError(
                "synthesized PCM silence processing "
                f"{field_name} is invalid"
            )
        numeric[field_name] = float(field_value)
    if (
        numeric["p95_limit_ms"] != SYNTHESIZED_PCM_SCAN_P95_LIMIT_MS
        or numeric["max_limit_ms"] != SYNTHESIZED_PCM_SCAN_MAX_LIMIT_MS
    ):
        raise ValueError(
            "synthesized PCM silence processing limits are invalid"
        )
    if not (
        numeric["p50_ms"]
        <= numeric["p95_ms"]
        <= numeric["max_ms"]
        <= numeric["total_ms"] + 1e-6
    ):
        raise ValueError(
            "synthesized PCM silence processing distributions are invalid"
        )
    if not math.isclose(
        numeric["mean_ms"],
        numeric["total_ms"] / frame_count,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError(
            "synthesized PCM silence processing mean does not reconcile"
        )
    expected_gate = (
        numeric["p95_ms"] <= SYNTHESIZED_PCM_SCAN_P95_LIMIT_MS
        and numeric["max_ms"] <= SYNTHESIZED_PCM_SCAN_MAX_LIMIT_MS
    )
    if (
        not isinstance(value.get("gate_passed"), bool)
        or value["gate_passed"] is not expected_gate
    ):
        raise ValueError(
            "synthesized PCM silence processing gate is invalid"
        )
    if value.get("privacy") != SYNTHESIZED_PCM_SCAN_PRIVACY:
        raise ValueError(
            "synthesized PCM silence processing privacy declaration is "
            "invalid"
        )

    return {
        "measurement_position": (
            SYNTHESIZED_PCM_SCAN_MEASUREMENT_POSITION
        ),
        "clock": SYNTHESIZED_PCM_SCAN_CLOCK,
        "frame_count": frame_count,
        **numeric,
        "gate_passed": expected_gate,
        "privacy": dict(SYNTHESIZED_PCM_SCAN_PRIVACY),
    }


async def _wait_until_or_abort(
    deadline: float,
    stream_abort: asyncio.Event,
    *,
    clock: Callable[[], float],
) -> bool:
    """Wait for one absolute deadline, returning false if the stream aborts."""

    while not stream_abort.is_set():
        remaining = deadline - clock()
        if remaining <= 0:
            return True
        try:
            await asyncio.wait_for(
                stream_abort.wait(),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            continue
        return False
    return False


async def _send_pcm_chunks_at_end_boundaries(
    pcm_bytes: bytes,
    stream_abort: asyncio.Event,
    send_chunk: Callable[[int, bytes, float], Awaitable[bool]],
    *,
    on_sample_zero: Callable[[float], None] | None = None,
    on_successful_send: (
        Callable[[int, float, float], None] | None
    ) = None,
    clock: Callable[[], float] | None = None,
    wait_until: (
        Callable[[float, asyncio.Event], Awaitable[bool]] | None
    ) = None,
    chunk_bytes: int | None = None,
    chunk_duration: float | None = None,
) -> tuple[float, int]:
    """Send PCM no earlier than each chunk's absolute source-end boundary."""

    resolved_clock = clock or time.monotonic
    resolved_chunk_bytes = CHUNK_BYTES if chunk_bytes is None else chunk_bytes
    resolved_chunk_duration = (
        CHUNK_DURATION if chunk_duration is None else chunk_duration
    )
    if resolved_chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    if resolved_chunk_duration <= 0:
        raise ValueError("chunk_duration must be positive")

    sample_zero = resolved_clock()
    if on_sample_zero is not None:
        on_sample_zero(sample_zero)

    chunks_sent = 0
    for idx, offset in enumerate(
        range(0, len(pcm_bytes), resolved_chunk_bytes)
    ):
        deadline = sample_zero + (idx + 1) * resolved_chunk_duration
        if wait_until is None:
            deadline_reached = await _wait_until_or_abort(
                deadline,
                stream_abort,
                clock=resolved_clock,
            )
        else:
            deadline_reached = await wait_until(deadline, stream_abort)
        if not deadline_reached or stream_abort.is_set():
            break

        send_timestamp = resolved_clock()
        if send_timestamp < deadline:
            raise RuntimeError(
                "input pacing attempted to send before the source-end "
                "boundary"
            )
        chunk = pcm_bytes[offset : offset + resolved_chunk_bytes]
        if not await send_chunk(idx, chunk, send_timestamp):
            break
        if on_successful_send is not None:
            on_successful_send(idx, send_timestamp, deadline)
        chunks_sent += 1

    return sample_zero, chunks_sent


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------
async def _run_test_impl(
    audio_path: str,
    backend_url: str,
    *,
    audio_metadata_protocol_version: int | None = None,
    audio_frame_sink: ValidatedAudioFrameSink | None = None,
    measure_synthesized_pcm_silence: bool = False,
    private_semantic_capture: PrivatePcmScheduleCapture | None = None,
) -> TestResult:
    """Run a single latency test against one audio file.

    ``audio_frame_sink`` is an optional nonblocking observation hook invoked
    after protocol-v1 header/binary validation. It executes inline so receipt
    timestamps remain causally ordered; blocking work would perturb later
    arrival measurements and must be queued by the callback instead.

    ``measure_synthesized_pcm_silence`` enables a first-party, aggregate-only
    low-energy PCM diagnostic. Raw samples are inspected only after protocol
    validation and are never exposed through ``audio_frame_sink`` or retained.

    ``private_semantic_capture`` is an explicit, default-off evidence sink.
    It retains the exact sent and validated translated PCM in memory until a
    clean capture is sealed; the batch runner alone publishes it to a fresh
    owner-private ignored directory.
    """

    if (
        audio_metadata_protocol_version is not None
        and (
            not isinstance(audio_metadata_protocol_version, int)
            or isinstance(audio_metadata_protocol_version, bool)
            or audio_metadata_protocol_version
            != AUDIO_METADATA_PROTOCOL_VERSION
        )
    ):
        raise ValueError(
            "audio_metadata_protocol_version must be omitted or equal 1"
        )
    if (
        audio_frame_sink is not None
        and audio_metadata_protocol_version is None
    ):
        raise ValueError(
            "audio_frame_sink requires audio metadata protocol version 1"
        )
    if not isinstance(measure_synthesized_pcm_silence, bool):
        raise TypeError(
            "measure_synthesized_pcm_silence must be a boolean"
        )
    if (
        measure_synthesized_pcm_silence
        and audio_metadata_protocol_version is None
    ):
        raise ValueError(
            "synthesized PCM silence diagnostic requires audio metadata "
            "protocol version 1"
        )
    if (
        private_semantic_capture is not None
        and not isinstance(
            private_semantic_capture,
            PrivatePcmScheduleCapture,
        )
    ):
        raise TypeError(
            "private_semantic_capture must be a "
            "PrivatePcmScheduleCapture"
        )
    if (
        private_semantic_capture is not None
        and audio_metadata_protocol_version is None
    ):
        raise ValueError(
            "private semantic capture requires audio metadata protocol "
            "version 1"
        )
    if (
        private_semantic_capture is not None
        and measure_synthesized_pcm_silence
    ):
        raise ValueError(
            "private semantic capture cannot be combined with the "
            "synthesized PCM silence diagnostic"
        )
    headless_scheduler = (
        HeadlessPlaybackScheduler()
        if audio_metadata_protocol_version is not None
        else None
    )

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
        audio_metadata_protocol_version=audio_metadata_protocol_version,
        synthesized_pcm_silence_requested=(
            measure_synthesized_pcm_silence
        ),
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
    if audio_metadata_protocol_version is not None:
        supported_metadata_versions = result.backend_config.get(
            "audioMetadataProtocolVersions"
        )
        if supported_metadata_versions != [
            audio_metadata_protocol_version
        ]:
            raise RuntimeError(
                "/api/config does not advertise audio metadata "
                f"protocol version {audio_metadata_protocol_version}"
            )
    if measure_synthesized_pcm_silence:
        staged_config = result.backend_config.get("stagedConfig")
        if (
            result.pipeline_mode != "staged"
            or not isinstance(staged_config, dict)
            or staged_config.get("telemetrySchemaVersion") != 3
            or staged_config.get("ttsIncrementalPublishEnabled") is not True
        ):
            raise RuntimeError(
                "synthesized PCM silence diagnostic requires the staged "
                "schema-3 incremental-publication path"
            )
    if private_semantic_capture is not None:
        staged_config = result.backend_config.get("stagedConfig")
        if (
            result.pipeline_mode != "staged"
            or not isinstance(staged_config, dict)
            or staged_config.get("telemetrySchemaVersion") != 3
            or staged_config.get("ttsIncrementalPublishEnabled") is not True
        ):
            raise RuntimeError(
                "private semantic capture requires the staged schema-3 "
                "incremental-publication path"
            )
    silence_diagnostic = (
        StreamingPcmSilenceDiagnostic()
        if measure_synthesized_pcm_silence
        else None
    )
    silence_processing_durations_ms: list[float] = []
    pcm_bytes = pcm.tobytes()
    if private_semantic_capture is not None:
        private_semantic_capture.bind_source_pcm(
            pcm_bytes,
            sample_rate_hz=SAMPLE_RATE,
            channels=1,
            bytes_per_sample=BYTES_PER_SAMPLE,
        )
    total_chunks = (len(pcm_bytes) + CHUNK_BYTES - 1) // CHUNK_BYTES

    # -- Start backend timing session ---------------------------------------
    print("Starting test session...", flush=True)
    resp = requests.post(f"{backend_url}/api/test/start", timeout=10)
    resp.raise_for_status()

    # -- Connect WebSocket --------------------------------------------------
    print(f"Connecting to {ws_url}...", flush=True)
    test_start_time = time.monotonic()
    client_clock_origin = time.monotonic()
    receive_order = 0
    metadata_tracker = AudioMetadataTracker(
        enabled=audio_metadata_protocol_version is not None,
        protocol_version=audio_metadata_protocol_version,
    )

    def record_receive(
        *,
        frame_type: str,
        received_at: float,
        timestamp_ms: float | None = None,
        control: dict[str, Any] | None = None,
        audio_bytes: int = 0,
        metadata: dict[str, Any] | None = None,
        source_end_to_receipt_ms: float | None = None,
    ) -> dict[str, Any]:
        nonlocal receive_order
        event: dict[str, Any] = {
            "order": receive_order,
            "timestamp_ms": (
                timestamp_ms
                if timestamp_ms is not None
                else (received_at - client_clock_origin) * 1000
            ),
            "frame_type": frame_type,
            "audio_bytes": audio_bytes,
        }
        if control is not None:
            event["message_type"] = control.get("type")
            event["status"] = control.get("status")
            if control.get("type") == "error":
                event["message"] = control.get("message")
        if metadata is not None:
            for field_name in (
                "protocolVersion",
                "streamGeneration",
                "parentSequenceId",
                "audioFrameId",
                "audioFrameCount",
                "audioBytes",
                "sampleRateHz",
                "channels",
                "bytesPerSample",
                "sourceStartMs",
                "sourceEndMs",
            ):
                if field_name in metadata:
                    event[field_name] = metadata[field_name]
        if source_end_to_receipt_ms is not None:
            event["sourceEndToReceiptMs"] = source_end_to_receipt_ms
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
        metadata_tracker.accept_control(msg)
        record_receive(
            frame_type="control",
            received_at=time.monotonic(),
            control=msg,
        )
        if msg.get("status") != "connected":
            raise RuntimeError(f"Unexpected initial message: {msg}")

        # Send start_stream
        start_stream_message = {
            "type": "start_stream",
            "targetLanguage": TARGET_LANGUAGE,
        }
        if audio_metadata_protocol_version is not None:
            start_stream_message["audioMetadataProtocolVersion"] = (
                audio_metadata_protocol_version
            )
        await ws.send(json.dumps(start_stream_message))

        # Wait for "listening"
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        metadata_tracker.accept_control(msg)
        record_receive(
            frame_type="control",
            received_at=time.monotonic(),
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
        pacing_margins_ms: list[float] = []

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

            def record_sample_zero(sample_zero: float) -> None:
                if audio_metadata_protocol_version is None:
                    return
                result.input_sample_zero_timestamp_ms = (
                    sample_zero - client_clock_origin
                ) * 1000
                result.input_pacing = {
                    "mode": INPUT_PACING_MODE,
                    "chunk_duration_ms": CHUNK_DURATION * 1000,
                    "source_sample_zero_clock": (
                        INPUT_SAMPLE_ZERO_CLOCK
                    ),
                    "deadline_basis": INPUT_PACING_DEADLINE_BASIS,
                    "source_sample_zero_timestamp_ms": (
                        result.input_sample_zero_timestamp_ms
                    ),
                    "observed_chunk_count": 0,
                    "min_emission_minus_deadline_ms": None,
                    "max_emission_minus_deadline_ms": None,
                }
                if private_semantic_capture is not None:
                    private_semantic_capture.record_source_anchor(
                        sample_zero - client_clock_origin
                    )

            def record_successful_send(
                idx: int,
                send_timestamp: float,
                _deadline: float,
            ) -> None:
                if audio_metadata_protocol_version is None:
                    return
                if not isinstance(result.input_pacing, dict):
                    raise RuntimeError(
                        "input pacing sample-zero anchor was not recorded"
                    )
                if result.input_sample_zero_timestamp_ms is None:
                    raise RuntimeError(
                        "input pacing sample-zero marker was not recorded"
                    )
                pacing_margins_ms.append(
                    _relative_pacing_margin_ms(
                        send_timestamp=send_timestamp,
                        client_clock_origin=client_clock_origin,
                        sample_zero_timestamp_ms=(
                            result.input_sample_zero_timestamp_ms
                        ),
                        chunk_index=idx,
                        chunk_duration_ms=CHUNK_DURATION * 1000,
                    )
                )
                result.input_pacing.update(
                    {
                        "observed_chunk_count": idx + 1,
                        "min_emission_minus_deadline_ms": min(
                            pacing_margins_ms
                        ),
                        "max_emission_minus_deadline_ms": max(
                            pacing_margins_ms
                        ),
                    }
                )
                if private_semantic_capture is not None:
                    byte_start = idx * CHUNK_BYTES
                    byte_end = min(
                        byte_start + CHUNK_BYTES,
                        len(pcm_bytes),
                    )
                    private_semantic_capture.record_source_chunk(
                        chunk_index=idx,
                        sample_start=byte_start // BYTES_PER_SAMPLE,
                        sample_end_exclusive=(
                            byte_end // BYTES_PER_SAMPLE
                        ),
                        audio_bytes=byte_end - byte_start,
                        deadline_seconds=(
                            _deadline - client_clock_origin
                        ),
                        emitted_seconds=(
                            send_timestamp - client_clock_origin
                        ),
                    )

            async def send_chunk(
                idx: int,
                chunk: bytes,
                send_ts: float,
            ) -> bool:
                nonlocal chunks_sent, last_print_time, connection_lost
                try:
                    await ws.send(chunk)
                except websockets.exceptions.ConnectionClosed:
                    connection_lost = True
                    stream_abort.set()
                    terminal_received.set()
                    print(f"\nConnection lost at chunk {idx} "
                          f"({idx * CHUNK_DURATION:.1f}s)")
                    return False

                result.client_events.append(TimingEvent(
                    source="client",
                    stage="chunk_sent",
                    timestamp_ms=(send_ts - client_clock_origin) * 1000,
                    chunk_index=idx,
                    source_position_sec=idx * CHUNK_DURATION,
                    audio_bytes=len(chunk),
                ))

                chunks_sent = idx + 1

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
                return True

            await _send_pcm_chunks_at_end_boundaries(
                pcm_bytes,
                stream_abort,
                send_chunk,
                on_sample_zero=record_sample_zero,
                on_successful_send=record_successful_send,
            )

        # -- Receive task ---------------------------------------------------
        async def receive_audio():
            nonlocal audio_responses, total_recv_bytes, connection_lost, last_audio_time, server_error
            recv_idx = 0
            try:
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        recv_ts = time.monotonic()
                        (
                            arrival_timestamp_ms,
                            arrival_seconds,
                        ) = _canonical_capture_time(
                            recv_ts,
                            client_clock_origin,
                        )
                        try:
                            paired_metadata = (
                                metadata_tracker.accept_binary(raw)
                            )
                        except AudioMetadataProtocolError as exc:
                            record_receive(
                                frame_type="pcm",
                                received_at=recv_ts,
                                timestamp_ms=arrival_timestamp_ms,
                                audio_bytes=len(raw),
                            )
                            server_error = (
                                "audio metadata protocol violation: "
                                f"{exc}"
                            )
                            stream_abort.set()
                            terminal_received.set()
                            return
                        source_end_to_receipt_ms = None
                        if (
                            paired_metadata is not None
                            and paired_metadata["sourceEndMs"] is not None
                            and result.input_sample_zero_timestamp_ms
                            is not None
                        ):
                            source_end_to_receipt_ms = (
                                arrival_timestamp_ms
                                - result.input_sample_zero_timestamp_ms
                                - paired_metadata["sourceEndMs"]
                            )
                            result.source_end_to_receipt_samples_ms.append(
                                source_end_to_receipt_ms
                            )
                        record_receive(
                            frame_type="pcm",
                            received_at=recv_ts,
                            timestamp_ms=arrival_timestamp_ms,
                            audio_bytes=len(raw),
                            metadata=paired_metadata,
                            source_end_to_receipt_ms=(
                                source_end_to_receipt_ms
                            ),
                        )
                        audio_responses += 1
                        total_recv_bytes += len(raw)
                        last_audio_time = time.monotonic()
                        result.client_events.append(TimingEvent(
                            source="client",
                            stage="audio_received",
                            timestamp_ms=arrival_timestamp_ms,
                            chunk_index=recv_idx,
                            source_position_sec=total_recv_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE),
                            audio_bytes=len(raw),
                            protocol_version=(
                                paired_metadata["protocolVersion"]
                                if paired_metadata is not None
                                else None
                            ),
                            stream_generation=(
                                paired_metadata["streamGeneration"]
                                if paired_metadata is not None
                                else None
                            ),
                            parent_sequence_id=(
                                paired_metadata["parentSequenceId"]
                                if paired_metadata is not None
                                else None
                            ),
                            audio_frame_id=(
                                paired_metadata["audioFrameId"]
                                if paired_metadata is not None
                                else None
                            ),
                            source_start_ms=(
                                paired_metadata["sourceStartMs"]
                                if paired_metadata is not None
                                else None
                            ),
                            source_end_ms=(
                                paired_metadata["sourceEndMs"]
                                if paired_metadata is not None
                                else None
                            ),
                            source_end_to_receipt_ms=(
                                source_end_to_receipt_ms
                            ),
                        ))
                        if paired_metadata is not None:
                            validated_frame = ValidatedAudioFrame(
                                arrival_seconds=arrival_seconds,
                                audio_bytes=len(raw),
                                protocol_version=paired_metadata[
                                    "protocolVersion"
                                ],
                                stream_generation=paired_metadata[
                                    "streamGeneration"
                                ],
                                parent_sequence_id=paired_metadata[
                                    "parentSequenceId"
                                ],
                                audio_frame_id=paired_metadata[
                                    "audioFrameId"
                                ],
                                sample_rate_hz=paired_metadata[
                                    "sampleRateHz"
                                ],
                                channels=paired_metadata["channels"],
                                bytes_per_sample=paired_metadata[
                                    "bytesPerSample"
                                ],
                                source_start_ms=paired_metadata[
                                    "sourceStartMs"
                                ],
                                source_end_ms=paired_metadata[
                                    "sourceEndMs"
                                ],
                            )
                            schedule_decision = None
                            try:
                                if headless_scheduler is not None:
                                    schedule_decision = (
                                        headless_scheduler.accept(
                                            validated_frame
                                        )
                                    )
                            except Exception as exc:
                                server_error = (
                                    "headless playback scheduler failed: "
                                    f"{type(exc).__name__}"
                                )
                                stream_abort.set()
                                terminal_received.set()
                                return
                            if private_semantic_capture is not None:
                                try:
                                    if schedule_decision is None:
                                        raise RuntimeError(
                                            "missing schedule decision"
                                        )
                                    private_semantic_capture.accept_frame(
                                        metadata=paired_metadata,
                                        pcm=raw,
                                        schedule=schedule_decision,
                                    )
                                except Exception as exc:
                                    private_semantic_capture.abort()
                                    server_error = (
                                        "private semantic capture failed: "
                                        f"{type(exc).__name__}"
                                    )
                                    stream_abort.set()
                                    terminal_received.set()
                                    return
                            try:
                                if audio_frame_sink is not None:
                                    audio_frame_sink(validated_frame)
                            except Exception as exc:
                                server_error = (
                                    "validated audio frame sink failed: "
                                    f"{type(exc).__name__}"
                                )
                                stream_abort.set()
                                terminal_received.set()
                                return
                            if silence_diagnostic is not None:
                                scan_started_ns = time.perf_counter_ns()
                                try:
                                    silence_diagnostic.accept_frame(
                                        paired_metadata,
                                        raw,
                                    )
                                    silence_processing_durations_ms.append(
                                        (
                                            time.perf_counter_ns()
                                            - scan_started_ns
                                        )
                                        / 1_000_000
                                    )
                                except Exception as exc:
                                    server_error = (
                                        "synthesized PCM silence diagnostic "
                                        f"failed: {type(exc).__name__}"
                                    )
                                    stream_abort.set()
                                    terminal_received.set()
                                    return
                        recv_idx += 1
                    elif isinstance(raw, str):
                        try:
                            control = json.loads(raw)
                        except json.JSONDecodeError:
                            try:
                                metadata_tracker.accept_control({})
                            except AudioMetadataProtocolError as exc:
                                server_error = (
                                    "audio metadata protocol violation: "
                                    f"{exc}"
                                )
                                stream_abort.set()
                                terminal_received.set()
                                return
                            record_receive(
                                frame_type="text",
                                received_at=time.monotonic(),
                            )
                            continue
                        control_received_at = time.monotonic()
                        try:
                            normalized_metadata = (
                                metadata_tracker.accept_control(control)
                            )
                        except AudioMetadataProtocolError as exc:
                            record_receive(
                                frame_type="control",
                                received_at=control_received_at,
                                control=control,
                            )
                            server_error = (
                                "audio metadata protocol violation: "
                                f"{exc}"
                            )
                            stream_abort.set()
                            terminal_received.set()
                            return
                        receive_event = record_receive(
                            frame_type="control",
                            received_at=control_received_at,
                            control=control,
                            metadata=normalized_metadata,
                        )
                        if (
                            normalized_metadata is not None
                            and normalized_metadata["type"]
                            == "audio_parent_complete"
                        ):
                            if private_semantic_capture is not None:
                                try:
                                    private_semantic_capture.complete_parent(
                                        metadata=normalized_metadata,
                                        received_seconds=(
                                            float(
                                                receive_event[
                                                    "timestamp_ms"
                                                ]
                                            )
                                            / 1000.0
                                        ),
                                    )
                                except Exception as exc:
                                    private_semantic_capture.abort()
                                    server_error = (
                                        "private semantic capture failed: "
                                        f"{type(exc).__name__}"
                                    )
                                    stream_abort.set()
                                    terminal_received.set()
                                    return
                            if silence_diagnostic is not None:
                                try:
                                    silence_diagnostic.complete_parent(
                                        normalized_metadata
                                    )
                                except Exception as exc:
                                    server_error = (
                                        "synthesized PCM silence diagnostic "
                                        f"failed: {type(exc).__name__}"
                                    )
                                    stream_abort.set()
                                    terminal_received.set()
                                    return
                            result.audio_metadata_completed_parents = len(
                                metadata_tracker.completed_parents
                            )
                        if (
                            control.get("type") == "status"
                            and control.get("status") == "completed"
                        ):
                            try:
                                metadata_tracker.assert_terminal_ready()
                                if silence_diagnostic is not None:
                                    finalized_silence = (
                                        silence_diagnostic.finalize()
                                    )
                                    validated_silence = (
                                        validate_synthesized_pcm_silence_observation(
                                            finalized_silence
                                        )
                                    )
                                    processing = (
                                        build_synthesized_pcm_silence_processing(
                                            silence_processing_durations_ms
                                        )
                                    )
                                    validated_processing = (
                                        validate_synthesized_pcm_silence_processing(
                                            processing,
                                            expected_frame_count=(
                                                validated_silence["totals"][
                                                    "frame_count"
                                                ]
                                            ),
                                        )
                                    )
                                    result.synthesized_pcm_silence = (
                                        validated_silence
                                    )
                                    result.synthesized_pcm_silence_processing = (
                                        validated_processing
                                    )
                            except AudioMetadataProtocolError as exc:
                                server_error = (
                                    "audio metadata protocol violation: "
                                    f"{exc}"
                                )
                                stream_abort.set()
                                terminal_received.set()
                                return
                            except Exception as exc:
                                server_error = (
                                    "synthesized PCM silence diagnostic "
                                    f"failed: {type(exc).__name__}"
                                )
                                stream_abort.set()
                                terminal_received.set()
                                return
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
            input_end_epoch = time.monotonic()
            result.input_end_timestamp_ms = (
                input_end_epoch - client_clock_origin
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

    if audio_metadata_protocol_version is not None:
        result.audio_metadata_stream_generation = (
            metadata_tracker.stream_generation
        )
        result.audio_metadata_paired_frames = len(
            metadata_tracker.paired_frames
        )
        result.audio_metadata_completed_parents = len(
            metadata_tracker.completed_parents
        )
        if result.input_sample_zero_timestamp_ms is None:
            result.source_end_to_receipt_availability = (
                "unavailable_missing_input_sample_zero"
            )
        elif not result.source_end_to_receipt_samples_ms:
            result.source_end_to_receipt_availability = (
                "unavailable_missing_source_end_offsets"
            )
        elif any(
            frame["sourceStartMs"] is None
            for frame in metadata_tracker.paired_frames
            if frame["sourceEndMs"] is not None
        ):
            result.source_end_to_receipt_availability = (
                "available_audio_processed_end_offset_not_semantic_boundary"
            )
        else:
            result.source_end_to_receipt_availability = (
                "available_asr_source_range_end_offset"
            )
        result.source_end_to_receipt_p50_ms = nearest_rank(
            result.source_end_to_receipt_samples_ms,
            0.50,
        )
        result.source_end_to_receipt_p95_ms = nearest_rank(
            result.source_end_to_receipt_samples_ms,
            0.95,
        )
        result.source_end_to_receipt_max_ms = max(
            result.source_end_to_receipt_samples_ms,
            default=None,
        )
        if (
            result.translation_completed
            and not result.server_error
            and headless_scheduler is not None
            and result.input_sample_zero_timestamp_ms is not None
        ):
            result.headless_playback_report = headless_scheduler.finalize(
                input_end_seconds=(
                    result.input_end_timestamp_ms / 1000.0
                ),
                input_sample_zero_seconds=(
                    result.input_sample_zero_timestamp_ms / 1000.0
                ),
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

    if private_semantic_capture is not None:
        capture_errors = validate_capture_result(result)
        if capture_errors:
            private_semantic_capture.abort()
        else:
            try:
                private_semantic_capture.seal(
                    input_end_seconds=(
                        result.input_end_timestamp_ms / 1000.0
                    ),
                    terminal_completed=result.translation_completed,
                    headless_report=result.headless_playback_report,
                )
            except Exception as exc:
                private_semantic_capture.abort()
                raise RuntimeError(
                    "private semantic capture finalization failed: "
                    f"{type(exc).__name__}"
                ) from exc

    return result


async def run_test(
    audio_path: str,
    backend_url: str,
    *,
    audio_metadata_protocol_version: int | None = None,
    audio_frame_sink: ValidatedAudioFrameSink | None = None,
    measure_synthesized_pcm_silence: bool = False,
    private_semantic_capture: PrivatePcmScheduleCapture | None = None,
) -> TestResult:
    """Run one capture and discard retained private PCM on every exception."""

    try:
        return await _run_test_impl(
            audio_path,
            backend_url,
            audio_metadata_protocol_version=(
                audio_metadata_protocol_version
            ),
            audio_frame_sink=audio_frame_sink,
            measure_synthesized_pcm_silence=(
                measure_synthesized_pcm_silence
            ),
            private_semantic_capture=private_semantic_capture,
        )
    except BaseException:
        if private_semantic_capture is not None:
            try:
                private_semantic_capture.abort()
            except Exception:
                pass
        raise


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
    """Write combined client + backend timing events to CSV.

    Float fields use Python's round-trip representation. Protocol-v1 artifact
    replay must see the exact arrival/source values used by the live scheduler;
    fixed decimal rounding could cross a 5/8/10-second policy boundary and
    produce a different playback mode during resume validation.
    """
    all_events = result.client_events + result.backend_events
    all_events.sort(key=lambda e: e.timestamp_ms)

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "source", "stage", "timestamp_ms",
            "chunk_index", "source_position_sec", "audio_bytes",
            "protocol_version", "stream_generation",
            "parent_sequence_id", "audio_frame_id",
            "source_start_ms", "source_end_ms",
            "source_end_to_receipt_ms",
        ])
        for ev in all_events:
            writer.writerow([
                ev.source, ev.stage, repr(float(ev.timestamp_ms)),
                ev.chunk_index, repr(float(ev.source_position_sec)),
                ev.audio_bytes,
                (
                    ev.protocol_version
                    if ev.protocol_version is not None
                    else ""
                ),
                (
                    ev.stream_generation
                    if ev.stream_generation is not None
                    else ""
                ),
                (
                    ev.parent_sequence_id
                    if ev.parent_sequence_id is not None
                    else ""
                ),
                (
                    ev.audio_frame_id
                    if ev.audio_frame_id is not None
                    else ""
                ),
                (
                    repr(float(ev.source_start_ms))
                    if ev.source_start_ms is not None
                    else ""
                ),
                (
                    repr(float(ev.source_end_ms))
                    if ev.source_end_ms is not None
                    else ""
                ),
                (
                    repr(float(ev.source_end_to_receipt_ms))
                    if ev.source_end_to_receipt_ms is not None
                    else ""
                ),
            ])
    print(f"Saved: {output_path}")


def generate_summary(result: TestResult, output_path: str):
    """Write metrics, provenance, and staged evidence for one audio file."""
    synthesized_pcm_silence = result.synthesized_pcm_silence
    synthesized_pcm_silence_processing = (
        result.synthesized_pcm_silence_processing
    )
    if result.synthesized_pcm_silence_requested != (
        synthesized_pcm_silence is not None
    ) or result.synthesized_pcm_silence_requested != (
        synthesized_pcm_silence_processing is not None
    ):
        raise ValueError(
            "synthesized PCM silence diagnostic request/result state is "
            "incomplete"
        )
    if synthesized_pcm_silence is not None:
        synthesized_pcm_silence = (
            validate_synthesized_pcm_silence_observation(
                synthesized_pcm_silence
            )
        )
        synthesized_pcm_silence_processing = (
            validate_synthesized_pcm_silence_processing(
                synthesized_pcm_silence_processing,
                expected_frame_count=(
                    synthesized_pcm_silence["totals"]["frame_count"]
                ),
            )
        )
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
        "audio_metadata_observation": {
            "protocol_version": result.audio_metadata_protocol_version,
            "stream_generation": result.audio_metadata_stream_generation,
            # A client-monotonic offset, never a wall-clock timestamp.
            "input_sample_zero_timestamp_ms": (
                result.input_sample_zero_timestamp_ms
            ),
            "paired_frames": result.audio_metadata_paired_frames,
            "completed_parents": result.audio_metadata_completed_parents,
            "source_end_to_receipt": {
                "availability": (
                    result.source_end_to_receipt_availability
                ),
                "sample_count": len(
                    result.source_end_to_receipt_samples_ms
                ),
                "p50_ms": result.source_end_to_receipt_p50_ms,
                "p95_ms": result.source_end_to_receipt_p95_ms,
                "max_ms": result.source_end_to_receipt_max_ms,
                "clock": "client_monotonic",
                "source_offset_origin": "input_pcm_sample_zero",
                "semantic_boundary_proven": False,
                "actual_audibility_proven": False,
            },
            "playback_behavior_changed": False,
            "contains_transcript_or_translation_text": False,
        },
        "headless_playback": result.headless_playback_report,
        # Optional aggregate-only measurement of windowed low-energy PCM.
        # Raw samples, per-window energies, text, paths, and wall clocks are
        # never retained by the diagnostic.
        "synthesized_pcm_silence_requested": (
            result.synthesized_pcm_silence_requested
        ),
        "synthesized_pcm_silence": synthesized_pcm_silence,
        "synthesized_pcm_silence_processing": (
            synthesized_pcm_silence_processing
        ),
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
    if result.input_pacing is not None:
        summary["input_pacing"] = result.input_pacing
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


async def run_preflight(
    backend_url: str,
    *,
    audio_metadata_protocol_version: int | None = None,
    measure_synthesized_pcm_silence: bool = False,
) -> bool:
    """Run pre-flight validation with the local neutral fixture."""
    print("\n=== Pre-flight Validation ===")
    if not Path(PREFLIGHT_FILE).exists():
        print(f"ERROR: Pre-flight file not found: {PREFLIGHT_FILE}")
        return False

    try:
        if (
            audio_metadata_protocol_version is None
            and not measure_synthesized_pcm_silence
        ):
            result = await run_test(PREFLIGHT_FILE, backend_url)
        else:
            result = await run_test(
                PREFLIGHT_FILE,
                backend_url,
                audio_metadata_protocol_version=(
                    audio_metadata_protocol_version
                ),
                measure_synthesized_pcm_silence=(
                    measure_synthesized_pcm_silence
                ),
            )
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


async def run_batch(
    files: list[str],
    backend_url: str,
    output_dir: str,
    *,
    audio_metadata_protocol_version: int | None = None,
    measure_synthesized_pcm_silence: bool = False,
    private_semantic_capture_dir: Path | None = None,
) -> bool:
    """Run tests on a list of audio files sequentially."""
    total = len(files)
    if private_semantic_capture_dir is not None:
        if not isinstance(private_semantic_capture_dir, Path):
            raise TypeError(
                "private_semantic_capture_dir must be a pathlib.Path"
            )
        if total != 1:
            raise ValueError(
                "private semantic capture requires exactly one input file"
            )
        if (
            audio_metadata_protocol_version
            != AUDIO_METADATA_PROTOCOL_VERSION
        ):
            raise ValueError(
                "private semantic capture requires audio metadata "
                "protocol version 1"
            )
        if measure_synthesized_pcm_silence:
            raise ValueError(
                "private semantic capture cannot be combined with the "
                "synthesized PCM silence diagnostic"
            )
        private_semantic_capture_dir = (
            _resolve_private_semantic_capture_dir(
                private_semantic_capture_dir
            )
        )
    all_captures_passed = total > 0
    captures_written = 0
    for i, fpath in enumerate(files, 1):
        print(f"\n=== Test {i}/{total}: {Path(fpath).name} ===")

        if not Path(fpath).exists():
            print(f"FAILED: File not found: {fpath}")
            all_captures_passed = False
            continue

        private_capture = (
            PrivatePcmScheduleCapture()
            if private_semantic_capture_dir is not None
            else None
        )
        try:
            if (
                audio_metadata_protocol_version is None
                and not measure_synthesized_pcm_silence
                and private_capture is None
            ):
                result = await run_test(fpath, backend_url)
            else:
                result = await run_test(
                    fpath,
                    backend_url,
                    audio_metadata_protocol_version=(
                        audio_metadata_protocol_version
                    ),
                    measure_synthesized_pcm_silence=(
                        measure_synthesized_pcm_silence
                    ),
                    private_semantic_capture=private_capture,
                )
        except Exception as e:
            if private_capture is not None:
                try:
                    private_capture.abort()
                except Exception:
                    pass
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
            if private_capture is not None:
                try:
                    private_capture.abort()
                except Exception:
                    pass
            print(f"Artifact generation FAILED: {exc}")
            all_captures_passed = False
            continue

        capture_errors = validate_capture_result(result)
        if capture_errors:
            if private_capture is not None:
                try:
                    private_capture.abort()
                except Exception:
                    pass
            all_captures_passed = False
            print("Capture validation FAILED:")
            for error in capture_errors:
                print(f"  - {error}")
        elif private_capture is not None:
            try:
                private_capture.write_new(
                    private_semantic_capture_dir
                )
                print(
                    "Private semantic evidence written to the requested "
                    "owner-private directory."
                )
            except Exception as exc:
                try:
                    private_capture.abort()
                except Exception:
                    pass
                print(
                    "Private semantic artifact publication FAILED: "
                    f"{type(exc).__name__}"
                )
                all_captures_passed = False

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


def _resolve_private_semantic_capture_dir(requested: Path) -> Path:
    """Resolve a fresh private path beneath this checkout's ignored root."""

    if not isinstance(requested, Path):
        raise TypeError("private capture directory must be a pathlib.Path")
    repository_root = Path(__file__).resolve().parent
    ignored_root = (repository_root / "experiment_results").resolve(
        strict=False
    )
    candidate = requested.expanduser()
    if not candidate.is_absolute():
        candidate = repository_root / candidate
    if candidate.is_symlink():
        raise ValueError(
            "private capture directory must not be a symbolic link"
        )
    candidate = candidate.resolve(strict=False)
    try:
        relative = candidate.relative_to(ignored_root)
    except ValueError as exc:
        raise ValueError(
            "private capture directory must be a child of the ignored "
            "experiment_results directory"
        ) from exc
    if not relative.parts:
        raise ValueError(
            "private capture directory must be a fresh child of "
            "experiment_results"
        )
    if candidate.exists() or candidate.is_symlink():
        raise ValueError(
            "private capture directory already exists; choose a fresh "
            "attempt directory"
        )
    return candidate


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
    parser.add_argument(
        "--audio-metadata-protocol-v1",
        action="store_true",
        help=(
            "Negotiate observation-only parent/frame metadata protocol v1; "
            "does not change or drop translated audio"
        ),
    )
    parser.add_argument(
        "--measure-synthesized-pcm-silence",
        action="store_true",
        help=(
            "measure aggregate-only 20 ms low-energy PCM at -60/-50/-40 "
            "dBFS; requires --audio-metadata-protocol-v1 and staged schema 3"
        ),
    )
    parser.add_argument(
        "--private-semantic-capture-dir",
        type=Path,
        help=(
            "explicitly retain source/translated PCM and a schedule ledger "
            "in one fresh child of ignored experiment_results; requires "
            "--file and --audio-metadata-protocol-v1"
        ),
    )
    args = parser.parse_args()
    if (
        args.measure_synthesized_pcm_silence
        and not args.audio_metadata_protocol_v1
    ):
        parser.error(
            "--measure-synthesized-pcm-silence requires "
            "--audio-metadata-protocol-v1"
        )
    if args.private_semantic_capture_dir is not None:
        if not args.audio_metadata_protocol_v1:
            parser.error(
                "--private-semantic-capture-dir requires "
                "--audio-metadata-protocol-v1"
            )
        if args.preflight or not args.file:
            parser.error(
                "--private-semantic-capture-dir requires exactly one "
                "--file input and cannot be used with --preflight"
            )
        if args.measure_synthesized_pcm_silence:
            parser.error(
                "--private-semantic-capture-dir cannot be combined with "
                "--measure-synthesized-pcm-silence"
            )
        try:
            args.private_semantic_capture_dir = (
                _resolve_private_semantic_capture_dir(
                    args.private_semantic_capture_dir
                )
            )
        except (TypeError, ValueError) as exc:
            parser.error(str(exc))
    metadata_version = (
        AUDIO_METADATA_PROTOCOL_VERSION
        if args.audio_metadata_protocol_v1
        else None
    )

    if not check_backend(args.backend):
        sys.exit(1)

    if args.preflight:
        ok = asyncio.run(
            run_preflight(
                args.backend,
                audio_metadata_protocol_version=metadata_version,
                measure_synthesized_pcm_silence=(
                    args.measure_synthesized_pcm_silence
                ),
            )
        )
        sys.exit(0 if ok else 1)

    if args.file:
        ok = asyncio.run(
            run_batch(
                [args.file],
                args.backend,
                args.output_dir,
                audio_metadata_protocol_version=metadata_version,
                measure_synthesized_pcm_silence=(
                    args.measure_synthesized_pcm_silence
                ),
                private_semantic_capture_dir=(
                    args.private_semantic_capture_dir
                ),
            ),
        )
    else:
        ok = asyncio.run(
            run_batch(
                LONG_FORM_FILES,
                args.backend,
                args.output_dir,
                audio_metadata_protocol_version=metadata_version,
                measure_synthesized_pcm_silence=(
                    args.measure_synthesized_pcm_silence
                ),
            ),
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
