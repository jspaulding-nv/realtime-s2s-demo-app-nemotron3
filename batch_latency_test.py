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
TARGET_LANGUAGE = "es-US"

TEST_FILES = [
    "test_audio/200108_SpiritandPresenceofGod.mp3",
    "test_audio/Blessed_Self-Forgetfulness.mp3",
    "test_audio/gospel_in_life_tk_1-john-part-2-mp3_Beholding_the_Love_of_God.mp3",
]
PREFLIGHT_FILE = "test_audio/test-1min.wav"


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
            elif sum(received_pcm_bytes) != sum(websocket_event_audio_bytes):
                errors.append(
                    "WebSocket PCM received bytes must exactly match successful "
                    "send bytes "
                    f"({sum(received_pcm_bytes)} != "
                    f"{sum(websocket_event_audio_bytes)})"
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

    staged_config = backend_config.get("stagedConfig")
    if staged_config is not None and not isinstance(staged_config, dict):
        errors.append("/api/config.stagedConfig must be an object")
        staged_config = None

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
        export_resp = requests.get(f"{backend_url}/api/test/export", timeout=30)
        export_resp.raise_for_status()
        export_data = export_resp.json()
        if not isinstance(export_data, dict):
            raise ValueError("/api/test/export must return a JSON object")
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
    """Run pre-flight validation with test-1min.wav."""
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
        help="Run pre-flight validation only (test-1min.wav)",
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
        ok = asyncio.run(run_batch(TEST_FILES, args.backend, args.output_dir))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
