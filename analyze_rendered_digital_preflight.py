#!/usr/bin/env python3
"""Validate a rendered-digital common-clock preflight evidence bundle.

The browser export is deliberately private, transcript-free mechanical
evidence.  This validator proves the integrity of the stereo PCM capture,
reconciles the source and protocol ledgers, and replays the browser playback
policy on the captured AudioContext clock.  It does not claim that a physical
device emitted sound or that a translation was semantically correct.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from playback_simulation import AudioChunk, PlaybackPolicy, simulate_playback


CAPTURE_SCHEMA = "rendered-digital-common-clock-preflight/v1"
BLOCK_LEDGER_SCHEMA = "rendered-digital-block-ledger/v1"
REPORT_SCHEMA = "rendered-digital-common-clock-preflight-report/v1"
SAMPLE_RATE_HZ = 16_000
CHANNEL_COUNT = 2
BITS_PER_SAMPLE = 16
BYTES_PER_MONO_SAMPLE = 2
BYTES_PER_FRAME = 4
SOURCE_FRAME_COUNT = 960_000
SOURCE_CHUNK_FRAMES = 4_800
SOURCE_CHUNK_COUNT = 200
PREFLIGHT_FILE_SHA256 = (
    "0c2cb04d9774f60472b55355f587da2148a053f3a55c05ff36c7dfc23be5c257"
)
PREFLIGHT_PCM_SHA256 = (
    "81720f2e23e5b85df4eb2be0bbd486b6591d0b1ed98580118e9e2e1e466bd51c"
)
APPROVED_ASR_IMAGE_DIGEST = (
    "sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850"
)
APPROVED_NMT_IMAGE_DIGEST = (
    "sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb"
)
APPROVED_TTS_IMAGE_DIGEST = (
    "sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d"
)
APPROVED_WORKLET_MODULE_SHA256 = (
    "8baf6193f097acc3c2663ca91299a19f68b1e7c17deaa586db339a073a7d0a5d"
)
QUEUE_P95_OBJECTIVE_SECONDS = 5.0
QUEUE_PEAK_LIMIT_SECONDS = 10.0
CSV_FLOAT_TOLERANCE_SECONDS = 0.000_002
CSV_CLIENT_TOLERANCE_MS = 0.01
RENDER_QUANTUM_FRAMES = 128
INPUT_CLOCK_RATE_MIN_RATIO = 0.99
INPUT_CLOCK_RATE_MAX_RATIO = 1.01
INPUT_CLOCK_INTERIOR_RATE_TOLERANCE = 0.01
INPUT_CLOCK_RECEIPT_LAG_LIMIT_MS = 100.0
CLIENT_CLOCK_ORIGIN_TOLERANCE_MS = 0.02
MAX_JSON_BYTES = 1_000_000
MAX_LEDGER_BYTES = 2_000_000
MAX_TIMING_BYTES = 64_000_000
MAX_WAV_BYTES = 16_000_000

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
UUID_V4_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
UTC_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"
)

MANIFEST_KEYS = {
    "schema",
    "capture_id",
    "created_at_utc",
    "evidence_status",
    "run_terminal",
    "claims",
    "clock",
    "channels",
    "source_reference",
    "translated_output",
    "translated_transport",
    "artifacts",
    "runtime",
    "playback_policy",
    "queue_gate",
}
RUN_TERMINAL_KEYS = {"server_status", "dashboard_phase"}
CLAIM_KEYS = {
    "common_sample_clock_recorded",
    "source_pcm_binding_recorded",
    "rendered_graph_output_observed",
    "protocol_pcm_no_loss_verified",
    "queue_bound_verified",
    "semantic_latency_status",
    "physical_dac_output_proven",
    "acoustic_audibility_proven",
    "translation_quality_proven",
    "audience_reaction_alignment_proven",
}
CLOCK_KEYS = {
    "basis",
    "sample_rate_hz",
    "channel_count",
    "capture_start_context_frame",
    "capture_end_context_frame_exclusive",
    "capture_frame_count",
    "block_count",
    "context_state_violation_count",
    "visibility_violation_count",
}
CHANNEL_KEYS = {"index", "role", "tap"}
SOURCE_REFERENCE_KEYS = {
    "input_file_sha256",
    "input_pcm_sha256",
    "input_pcm_frame_count",
    "source_start_context_frame",
    "source_end_context_frame_exclusive",
    "capture_frame_start",
    "capture_frame_end_exclusive",
    "active_slice_pcm_sha256",
    "outside_active_nonzero_sample_count",
    "chunk_frames",
    "chunk_count",
}
TRANSLATED_OUTPUT_KEYS = {
    "received_frame_count",
    "scheduled_frame_count",
    "completed_parent_count",
    "nonzero_sample_count",
    "nonzero_outside_scheduled_sample_count",
    "last_scheduled_end_context_frame_exclusive",
}
TRANSLATED_TRANSPORT_KEYS = {
    "received_frame_count",
    "scheduled_frame_count",
    "received_pcm_byte_count",
    "scheduled_pcm_byte_count",
    "received_ordered_pcm_sha256",
    "scheduled_ordered_pcm_sha256",
    "received_frame_ledger_sha256",
    "scheduled_frame_ledger_sha256",
}
ARTIFACT_KEYS = {
    "wav_sha256",
    "wav_byte_count",
    "interleaved_pcm_sha256",
    "interleaved_pcm_sample_count",
    "source_channel_pcm_sha256",
    "translated_channel_pcm_sha256",
    "timing_csv_sha256",
    "timing_csv_byte_count",
    "block_ledger_csv_sha256",
    "block_ledger_csv_byte_count",
}
RUNTIME_KEYS = {
    "repository_commit",
    "repository_dirty",
    "pipeline_mode",
    "audio_metadata_protocol_version",
    "telemetry_schema_version",
    "punctuation_segmentation",
    "staged_execution",
    "browser_capture",
    "asr",
    "nmt",
    "tts",
    "normalized_config_sha256",
}
PUNCTUATION_SEGMENTATION_KEYS = {"max_chars", "max_age_ms"}
STAGED_EXECUTION_KEYS = {
    "asr_event_queue_max_size",
    "nmt_queue_max_size",
    "tts_queue_max_size",
    "output_queue_max_size",
    "nmt_rpc_timeout_seconds",
    "tts_rpc_timeout_seconds",
    "tts_max_segment_audio_seconds",
    "tts_max_retries",
    "tts_response_chunk_telemetry_enabled",
    "tts_subsegment_max_chars",
    "tts_subsegment_min_chars",
    "incremental_atomic_fallback_max_chars",
    "close_timeout_seconds",
}
BROWSER_CAPTURE_KEYS = {
    "frontend_repository_commit",
    "frontend_repository_dirty",
    "worklet_module_sha256",
}
ASR_KEYS = {
    "image_digest",
    "profile_id",
    "eou_ms",
    "word_time_offsets",
}
NMT_KEYS = {"image_digest", "model_id", "language_pair_id"}
TTS_KEYS = {
    "image_digest",
    "profile_id",
    "voice_id",
    "incremental_publish_enabled",
    "incremental_frame_ms",
}
PLAYBACK_POLICY_KEYS = {
    "adaptive_playback_enabled",
    "loss_policy",
    "target_queue_seconds",
    "urgent_queue_seconds",
    "limit_queue_seconds",
    "catch_up_release_seconds",
    "urgent_release_seconds",
    "normal_rate",
    "catch_up_rate",
    "urgent_rate",
}
QUEUE_GATE_KEYS = {
    "metric",
    "p95_objective_seconds",
    "peak_limit_seconds",
    "result",
}

TIMING_COLUMNS = [
    "source",
    "stage",
    "timestamp_ms",
    "chunk_index",
    "source_position_sec",
    "audio_bytes",
    "media_duration_sec",
    "scheduled_duration_sec",
    "playback_wait_sec",
    "queue_depth_sec",
    "playback_rate",
    "playback_mode",
    "terminal_status",
    "adaptive_playback_enabled",
    "audio_metadata_protocol_version",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "audio_frame_count",
    "source_start_ms",
    "source_end_ms",
    "source_timing_basis",
    "binary_receipt_client_ms",
    "parent_complete_received_client_ms",
    "input_sample_zero_client_ms",
    "input_chunk_emitted_client_ms",
    "input_source_sample_start",
    "input_source_sample_end_exclusive",
    "input_sample_rate_hz",
    "input_pcm_sha256",
    "input_pcm_sample_count",
    "input_ledger_valid",
    "input_source_boundary_context_frame",
    "input_source_boundary_delivered_after_context_frame",
    "input_source_boundary_received_context_frame_before",
    "input_source_boundary_received_context_frame_after",
    "input_source_boundary_received_client_ms",
    "input_chunk_emitted_context_frame",
    "source_end_boundary_client_ms",
    "source_end_to_binary_receipt_ms",
    "source_end_to_parent_complete_ms",
    "schedule_performance_client_ms",
    "audio_context_time_at_schedule_sec",
    "scheduled_start_context_sec",
    "scheduled_end_context_sec",
    "scheduled_start_context_frame_floor",
    "scheduled_end_context_frame_exclusive",
    "projected_scheduled_start_client_ms",
    "source_end_to_projected_scheduled_start_ms",
    "playback_clock_session_id",
    "clock_sample_sequence",
    "clock_sample_reason",
    "clock_sample_performance_client_ms",
    "clock_sample_performance_before_client_ms",
    "clock_sample_performance_after_client_ms",
    "clock_sample_context_sec",
    "clock_sample_output_context_sec",
    "clock_sample_output_performance_client_ms",
    "clock_sample_basis",
    "clock_sample_queue_end_context_sec",
]
BLOCK_LEDGER_COLUMNS = [
    "schema",
    "sequence",
    "start_context_frame",
    "capture_frame_start",
    "frame_count",
    "interleaved_pcm_sha256",
]
BACKEND_STAGES = {
    "audio_received",
    "audio_to_riva",
    "audio_from_riva",
    "audio_sent_to_client",
}
BASE_TIMING_COLUMNS = {
    "source",
    "stage",
    "timestamp_ms",
    "chunk_index",
    "source_position_sec",
    "audio_bytes",
}
SOURCE_RANGE_COLUMNS = {
    "source_start_ms",
    "source_end_ms",
    "source_timing_basis",
}
SOURCE_LEDGER_LINK_COLUMNS = {
    "input_sample_zero_client_ms",
    "input_ledger_valid",
    "source_end_boundary_client_ms",
}
CLIENT_STAGE_REQUIRED_COLUMNS = {
    "playback_session_started": (
        BASE_TIMING_COLUMNS | {"adaptive_playback_enabled"}
    ),
    "chunk_sent": (
        BASE_TIMING_COLUMNS
        | {
            "input_sample_zero_client_ms",
            "input_chunk_emitted_client_ms",
            "input_source_sample_start",
            "input_source_sample_end_exclusive",
            "input_sample_rate_hz",
            "input_pcm_sha256",
            "input_pcm_sample_count",
            "input_ledger_valid",
            "input_source_boundary_context_frame",
            "input_source_boundary_delivered_after_context_frame",
            "input_source_boundary_received_context_frame_before",
            "input_source_boundary_received_context_frame_after",
            "input_source_boundary_received_client_ms",
            "input_chunk_emitted_context_frame",
        }
    ),
    "audio_received": (
        BASE_TIMING_COLUMNS
        | SOURCE_RANGE_COLUMNS
        | {
            "audio_metadata_protocol_version",
            "stream_generation",
            "parent_sequence_id",
            "audio_frame_id",
            "binary_receipt_client_ms",
        }
    ),
    "audio_parent_complete": (
        BASE_TIMING_COLUMNS
        | SOURCE_RANGE_COLUMNS
        | {
            "audio_metadata_protocol_version",
            "stream_generation",
            "parent_sequence_id",
            "audio_frame_count",
            "parent_complete_received_client_ms",
        }
    ),
    "playback_chunk_scheduled": (
        BASE_TIMING_COLUMNS
        | SOURCE_RANGE_COLUMNS
        | {
            "media_duration_sec",
            "scheduled_duration_sec",
            "playback_wait_sec",
            "queue_depth_sec",
            "playback_rate",
            "playback_mode",
            "audio_metadata_protocol_version",
            "stream_generation",
            "parent_sequence_id",
            "audio_frame_id",
            "binary_receipt_client_ms",
            "schedule_performance_client_ms",
            "audio_context_time_at_schedule_sec",
            "scheduled_start_context_sec",
            "scheduled_end_context_sec",
            "scheduled_start_context_frame_floor",
            "scheduled_end_context_frame_exclusive",
            "projected_scheduled_start_client_ms",
            "playback_clock_session_id",
        }
    ),
    "playback_clock_sample": (
        BASE_TIMING_COLUMNS
        | {
            "playback_clock_session_id",
            "clock_sample_sequence",
            "clock_sample_reason",
            "clock_sample_performance_client_ms",
            "clock_sample_performance_before_client_ms",
            "clock_sample_performance_after_client_ms",
            "clock_sample_context_sec",
            "clock_sample_basis",
            "clock_sample_queue_end_context_sec",
        }
    ),
    "playback_queue_sample": (
        BASE_TIMING_COLUMNS
        | {"queue_depth_sec", "playback_rate", "playback_mode"}
    ),
    "server_terminal": BASE_TIMING_COLUMNS | {"terminal_status"},
    "input_ended": BASE_TIMING_COLUMNS,
}
CLIENT_STAGE_OPTIONAL_COLUMNS = {
    "playback_session_started": set(),
    "chunk_sent": set(),
    "audio_received": (
        SOURCE_LEDGER_LINK_COLUMNS | {"source_end_to_binary_receipt_ms"}
    ),
    "audio_parent_complete": (
        SOURCE_LEDGER_LINK_COLUMNS | {"source_end_to_parent_complete_ms"}
    ),
    "playback_chunk_scheduled": (
        SOURCE_LEDGER_LINK_COLUMNS
        | {
            "source_end_to_binary_receipt_ms",
            "source_end_to_projected_scheduled_start_ms",
        }
    ),
    "playback_clock_sample": {
        "clock_sample_output_context_sec",
        "clock_sample_output_performance_client_ms",
    },
    "playback_queue_sample": set(),
    "server_terminal": set(),
    "input_ended": set(),
}
INTEGER_TIMING_COLUMNS = {
    "chunk_index",
    "audio_bytes",
    "audio_metadata_protocol_version",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "audio_frame_count",
    "input_source_sample_start",
    "input_source_sample_end_exclusive",
    "input_sample_rate_hz",
    "input_pcm_sample_count",
    "input_source_boundary_context_frame",
    "input_source_boundary_delivered_after_context_frame",
    "input_source_boundary_received_context_frame_before",
    "input_source_boundary_received_context_frame_after",
    "input_chunk_emitted_context_frame",
    "scheduled_start_context_frame_floor",
    "scheduled_end_context_frame_exclusive",
    "playback_clock_session_id",
    "clock_sample_sequence",
}
FLOAT_TIMING_COLUMNS = {
    "timestamp_ms",
    "source_position_sec",
    "media_duration_sec",
    "scheduled_duration_sec",
    "playback_wait_sec",
    "queue_depth_sec",
    "playback_rate",
    "binary_receipt_client_ms",
    "parent_complete_received_client_ms",
    "input_sample_zero_client_ms",
    "input_chunk_emitted_client_ms",
    "input_source_boundary_received_client_ms",
    "source_end_boundary_client_ms",
    "source_end_to_binary_receipt_ms",
    "source_end_to_parent_complete_ms",
    "schedule_performance_client_ms",
    "audio_context_time_at_schedule_sec",
    "scheduled_start_context_sec",
    "scheduled_end_context_sec",
    "projected_scheduled_start_client_ms",
    "source_end_to_projected_scheduled_start_ms",
    "clock_sample_performance_client_ms",
    "clock_sample_performance_before_client_ms",
    "clock_sample_performance_after_client_ms",
    "clock_sample_context_sec",
    "clock_sample_output_context_sec",
    "clock_sample_output_performance_client_ms",
    "clock_sample_queue_end_context_sec",
}


class PreflightValidationError(ValueError):
    """Evidence is malformed, contradictory, corrupt, or ambiguous."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WavEvidence:
    frame_count: int
    pcm: bytes
    source_pcm: bytes
    translated_pcm: bytes
    translated_nonzero_sample_count: int


@dataclass(frozen=True)
class InputClockEvidence:
    maximum_worklet_delivery_lag_frames: int
    maximum_main_receipt_lag_frames: int
    maximum_chunk_emission_lag_frames: int
    expected_boundary_span_ms: float
    observed_boundary_span_ms: float
    wall_to_source_ratio: float
    maximum_elapsed_drift_ms: float


@dataclass(frozen=True)
class TimingEvidence:
    input_chunk_count: int
    received_frame_count: int
    scheduled_frame_count: int
    completed_parent_count: int
    received_audio_bytes: int
    scheduled_audio_bytes: int
    protocol_pcm_no_loss: bool
    time_weighted_queue_p95_seconds: float
    peak_queue_depth_seconds: float
    queue_bound: bool
    chunks_dropped: int
    input_clock: InputClockEvidence


def _invalid(code: str, message: str) -> PreflightValidationError:
    return PreflightValidationError(code, message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact_keys(value: Any, expected: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid("schema", f"{context} must be an object")
    if set(value) != expected:
        raise _invalid("schema", f"{context} does not use the exact key set")
    return value


def _plain_int(
    value: Any,
    context: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid("schema", f"{context} must be an integer")
    if minimum is not None and value < minimum:
        raise _invalid("schema", f"{context} is below its minimum")
    return value


def _finite_number(
    value: Any,
    context: str,
    *,
    minimum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid("schema", f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise _invalid("schema", f"{context} must be finite")
    if minimum is not None and result < minimum:
        raise _invalid("schema", f"{context} is below its minimum")
    return result


def _literal(value: Any, expected: Any, context: str) -> None:
    if value != expected or type(value) is not type(expected):
        raise _invalid("schema", f"{context} has an unsupported value")


def _digest(value: Any, context: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise _invalid("schema", f"{context} must be a lowercase SHA-256")
    return value


def _image_digest(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or IMAGE_DIGEST_PATTERN.fullmatch(value) is None
    ):
        raise _invalid("schema", f"{context} must be a pinned image digest")
    return value


def _close(
    actual: float,
    expected: float,
    context: str,
    *,
    tolerance: float = CSV_FLOAT_TOLERANCE_SECONDS,
) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance):
        raise _invalid("timing_replay", f"{context} does not replay exactly")


def _load_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _invalid("io", f"{label} could not be read") from exc
    if not data or len(data) > limit:
        raise _invalid("size", f"{label} has an invalid byte count")
    return data


def _load_json_object(data: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _invalid("json", "manifest contains a duplicate key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise _invalid("json", f"manifest contains non-standard number {value}")

    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except PreflightValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid("json", "manifest is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise _invalid("schema", "manifest must be an object")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_runtime(runtime_value: Any) -> None:
    runtime = _exact_keys(runtime_value, RUNTIME_KEYS, "runtime")
    commit = runtime["repository_commit"]
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise _invalid(
            "runtime",
            "runtime.repository_commit must bind a clean Git commit",
        )
    _literal(
        runtime["repository_dirty"],
        False,
        "runtime.repository_dirty",
    )
    _literal(runtime["pipeline_mode"], "staged", "runtime.pipeline_mode")
    _literal(
        runtime["audio_metadata_protocol_version"],
        1,
        "runtime.audio_metadata_protocol_version",
    )
    _plain_int(
        runtime["telemetry_schema_version"],
        "runtime.telemetry_schema_version",
        minimum=3,
    )
    _literal(
        runtime["telemetry_schema_version"],
        3,
        "runtime.telemetry_schema_version",
    )
    punctuation = _exact_keys(
        runtime["punctuation_segmentation"],
        PUNCTUATION_SEGMENTATION_KEYS,
        "runtime.punctuation_segmentation",
    )
    _literal(
        punctuation["max_chars"],
        240,
        "runtime.punctuation_segmentation.max_chars",
    )
    _literal(
        punctuation["max_age_ms"],
        2000,
        "runtime.punctuation_segmentation.max_age_ms",
    )
    staged_execution = _exact_keys(
        runtime["staged_execution"],
        STAGED_EXECUTION_KEYS,
        "runtime.staged_execution",
    )
    expected_staged_execution = {
        "asr_event_queue_max_size": 32,
        "nmt_queue_max_size": 4,
        "tts_queue_max_size": 4,
        "output_queue_max_size": 4,
        "nmt_rpc_timeout_seconds": 15,
        "tts_rpc_timeout_seconds": 60,
        "tts_max_segment_audio_seconds": 60,
        "tts_max_retries": 1,
        "tts_response_chunk_telemetry_enabled": False,
        "tts_subsegment_max_chars": 0,
        "tts_subsegment_min_chars": 12,
        "incremental_atomic_fallback_max_chars": 4,
        "close_timeout_seconds": 10,
    }
    for key, expected in expected_staged_execution.items():
        _literal(
            staged_execution[key],
            expected,
            f"runtime.staged_execution.{key}",
        )
    browser_capture = _exact_keys(
        runtime["browser_capture"],
        BROWSER_CAPTURE_KEYS,
        "runtime.browser_capture",
    )
    _literal(
        browser_capture["frontend_repository_commit"],
        commit,
        "runtime.browser_capture.frontend_repository_commit",
    )
    _literal(
        browser_capture["frontend_repository_dirty"],
        False,
        "runtime.browser_capture.frontend_repository_dirty",
    )
    _literal(
        _digest(
            browser_capture["worklet_module_sha256"],
            "runtime.browser_capture.worklet_module_sha256",
        ),
        APPROVED_WORKLET_MODULE_SHA256,
        "runtime.browser_capture.worklet_module_sha256",
    )

    asr = _exact_keys(runtime["asr"], ASR_KEYS, "runtime.asr")
    _literal(
        _image_digest(asr["image_digest"], "runtime.asr.image_digest"),
        APPROVED_ASR_IMAGE_DIGEST,
        "runtime.asr.image_digest",
    )
    _literal(
        asr["profile_id"],
        "nemotron-asr-streaming_en-US_batch32",
        "runtime.asr.profile_id",
    )
    _literal(asr["eou_ms"], 800, "runtime.asr.eou_ms")
    _literal(
        asr["word_time_offsets"],
        True,
        "runtime.asr.word_time_offsets",
    )

    nmt = _exact_keys(runtime["nmt"], NMT_KEYS, "runtime.nmt")
    _literal(
        _image_digest(nmt["image_digest"], "runtime.nmt.image_digest"),
        APPROVED_NMT_IMAGE_DIGEST,
        "runtime.nmt.image_digest",
    )
    _literal(
        nmt["model_id"],
        "megatronnmt_any_any_1b",
        "runtime.nmt.model_id",
    )
    _literal(
        nmt["language_pair_id"],
        "en-US_to_es-US",
        "runtime.nmt.language_pair_id",
    )

    tts = _exact_keys(runtime["tts"], TTS_KEYS, "runtime.tts")
    _literal(
        _image_digest(tts["image_digest"], "runtime.tts.image_digest"),
        APPROVED_TTS_IMAGE_DIGEST,
        "runtime.tts.image_digest",
    )
    _literal(
        tts["profile_id"],
        "magpie-tts-multilingual_batch8",
        "runtime.tts.profile_id",
    )
    _literal(
        tts["voice_id"],
        "Magpie-Multilingual.ES-US.Isabela",
        "runtime.tts.voice_id",
    )
    _literal(
        tts["incremental_publish_enabled"],
        True,
        "runtime.tts.incremental_publish_enabled",
    )
    _literal(
        tts["incremental_frame_ms"],
        500,
        "runtime.tts.incremental_frame_ms",
    )

    normalized = {key: value for key, value in runtime.items() if key != (
        "normalized_config_sha256"
    )}
    if _digest(
        runtime["normalized_config_sha256"],
        "runtime.normalized_config_sha256",
    ) != _sha256(_canonical_json(normalized)):
        raise _invalid("runtime_hash", "normalized runtime hash does not match")


def _validate_local_worklet_module() -> None:
    module_path = (
        Path(__file__).resolve().parent
        / "frontend"
        / "public"
        / "rendered-digital-recorder.worklet.js"
    )
    try:
        module_digest = _sha256(module_path.read_bytes())
    except OSError as exc:
        raise _invalid(
            "runtime",
            "checked-in recorder worklet module is unavailable",
        ) from exc
    if module_digest != APPROVED_WORKLET_MODULE_SHA256:
        raise _invalid(
            "runtime",
            "checked-in recorder worklet module is not the approved version",
        )


def _validate_manifest(manifest: dict[str, Any]) -> None:
    _exact_keys(manifest, MANIFEST_KEYS, "manifest")
    _literal(manifest["schema"], CAPTURE_SCHEMA, "manifest.schema")
    if (
        not isinstance(manifest["capture_id"], str)
        or UUID_V4_PATTERN.fullmatch(manifest["capture_id"]) is None
    ):
        raise _invalid("schema", "manifest.capture_id must be a UUIDv4")
    if (
        not isinstance(manifest["created_at_utc"], str)
        or UTC_TIMESTAMP_PATTERN.fullmatch(manifest["created_at_utc"]) is None
    ):
        raise _invalid(
            "schema",
            "manifest.created_at_utc must be a millisecond UTC timestamp",
        )
    try:
        datetime.strptime(
            manifest["created_at_utc"],
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
    except ValueError as exc:
        raise _invalid(
            "schema",
            "manifest.created_at_utc is not a real UTC timestamp",
        ) from exc
    _literal(
        manifest["evidence_status"],
        "unverified_browser_export",
        "manifest.evidence_status",
    )
    run_terminal = _exact_keys(
        manifest["run_terminal"],
        RUN_TERMINAL_KEYS,
        "run_terminal",
    )
    expected_terminal = {
        "server_status": "completed",
        "dashboard_phase": "completed",
    }
    if run_terminal != expected_terminal:
        raise _invalid(
            "terminal",
            "manifest does not bind a completed server/dashboard run",
        )

    claims = _exact_keys(manifest["claims"], CLAIM_KEYS, "claims")
    expected_claims = {
        "common_sample_clock_recorded": True,
        "source_pcm_binding_recorded": True,
        "rendered_graph_output_observed": True,
        "protocol_pcm_no_loss_verified": False,
        "queue_bound_verified": False,
        "semantic_latency_status": "not_evaluated",
        "physical_dac_output_proven": False,
        "acoustic_audibility_proven": False,
        "translation_quality_proven": False,
        "audience_reaction_alignment_proven": False,
    }
    if claims != expected_claims:
        raise _invalid("claim_boundary", "manifest claim boundary is invalid")

    clock = _exact_keys(manifest["clock"], CLOCK_KEYS, "clock")
    _literal(
        clock["basis"],
        "single_audio_context_render_quantum",
        "clock.basis",
    )
    _literal(clock["sample_rate_hz"], SAMPLE_RATE_HZ, "clock.sample_rate_hz")
    _literal(clock["channel_count"], CHANNEL_COUNT, "clock.channel_count")
    capture_start = _plain_int(
        clock["capture_start_context_frame"],
        "clock.capture_start_context_frame",
        minimum=0,
    )
    capture_end = _plain_int(
        clock["capture_end_context_frame_exclusive"],
        "clock.capture_end_context_frame_exclusive",
        minimum=1,
    )
    capture_frames = _plain_int(
        clock["capture_frame_count"],
        "clock.capture_frame_count",
        minimum=1,
    )
    if capture_end - capture_start != capture_frames:
        raise _invalid("clock", "capture clock interval is inconsistent")
    _plain_int(clock["block_count"], "clock.block_count", minimum=1)
    _literal(
        clock["context_state_violation_count"],
        0,
        "clock.context_state_violation_count",
    )
    _literal(
        clock["visibility_violation_count"],
        0,
        "clock.visibility_violation_count",
    )

    channels = manifest["channels"]
    if not isinstance(channels, list) or len(channels) != CHANNEL_COUNT:
        raise _invalid("schema", "channels must contain exactly two entries")
    expected_channels = [
        {
            "index": 0,
            "role": "source_reference",
            "tap": "post_playback_rate_pre_monitor_mute",
        },
        {
            "index": 1,
            "role": "translated_output",
            "tap": "post_queue_post_playback_rate_pre_monitor_mute",
        },
    ]
    for index, channel in enumerate(channels):
        _exact_keys(channel, CHANNEL_KEYS, f"channels[{index}]")
    if channels != expected_channels:
        raise _invalid("schema", "channel order or tap semantics are invalid")

    source = _exact_keys(
        manifest["source_reference"],
        SOURCE_REFERENCE_KEYS,
        "source_reference",
    )
    _literal(
        _digest(
            source["input_file_sha256"],
            "source_reference.input_file_sha256",
        ),
        PREFLIGHT_FILE_SHA256,
        "source_reference.input_file_sha256",
    )
    _literal(
        _digest(
            source["input_pcm_sha256"],
            "source_reference.input_pcm_sha256",
        ),
        PREFLIGHT_PCM_SHA256,
        "source_reference.input_pcm_sha256",
    )
    _literal(
        source["input_pcm_frame_count"],
        SOURCE_FRAME_COUNT,
        "source_reference.input_pcm_frame_count",
    )
    source_start = _plain_int(
        source["source_start_context_frame"],
        "source_reference.source_start_context_frame",
        minimum=0,
    )
    source_end = _plain_int(
        source["source_end_context_frame_exclusive"],
        "source_reference.source_end_context_frame_exclusive",
        minimum=1,
    )
    if source_end - source_start != SOURCE_FRAME_COUNT:
        raise _invalid("source_binding", "source clock interval is not 60 seconds")
    source_capture_start = _plain_int(
        source["capture_frame_start"],
        "source_reference.capture_frame_start",
        minimum=0,
    )
    source_capture_end = _plain_int(
        source["capture_frame_end_exclusive"],
        "source_reference.capture_frame_end_exclusive",
        minimum=1,
    )
    if (
        source_capture_start != source_start - capture_start
        or source_capture_end != source_capture_start + SOURCE_FRAME_COUNT
        or source_capture_end > capture_frames
    ):
        raise _invalid("source_binding", "source capture interval is inconsistent")
    _literal(
        _digest(
            source["active_slice_pcm_sha256"],
            "source_reference.active_slice_pcm_sha256",
        ),
        PREFLIGHT_PCM_SHA256,
        "source_reference.active_slice_pcm_sha256",
    )
    _literal(
        source["outside_active_nonzero_sample_count"],
        0,
        "source_reference.outside_active_nonzero_sample_count",
    )
    _literal(
        source["chunk_frames"],
        SOURCE_CHUNK_FRAMES,
        "source_reference.chunk_frames",
    )
    _literal(
        source["chunk_count"],
        SOURCE_CHUNK_COUNT,
        "source_reference.chunk_count",
    )

    translated = _exact_keys(
        manifest["translated_output"],
        TRANSLATED_OUTPUT_KEYS,
        "translated_output",
    )
    _plain_int(
        translated["received_frame_count"],
        "translated_output.received_frame_count",
        minimum=1,
    )
    _plain_int(
        translated["scheduled_frame_count"],
        "translated_output.scheduled_frame_count",
        minimum=1,
    )
    _plain_int(
        translated["completed_parent_count"],
        "translated_output.completed_parent_count",
        minimum=0,
    )
    _plain_int(
        translated["nonzero_sample_count"],
        "translated_output.nonzero_sample_count",
        minimum=1,
    )
    _literal(
        translated["nonzero_outside_scheduled_sample_count"],
        0,
        "translated_output.nonzero_outside_scheduled_sample_count",
    )
    translated_end = _plain_int(
        translated["last_scheduled_end_context_frame_exclusive"],
        "translated_output.last_scheduled_end_context_frame_exclusive",
        minimum=1,
    )
    if translated_end > capture_end:
        raise _invalid("clock", "translated schedule extends beyond capture")

    transport = _exact_keys(
        manifest["translated_transport"],
        TRANSLATED_TRANSPORT_KEYS,
        "translated_transport",
    )
    for key in (
        "received_frame_count",
        "scheduled_frame_count",
        "received_pcm_byte_count",
        "scheduled_pcm_byte_count",
    ):
        _plain_int(transport[key], f"translated_transport.{key}", minimum=1)
    for key in (
        "received_ordered_pcm_sha256",
        "scheduled_ordered_pcm_sha256",
        "received_frame_ledger_sha256",
        "scheduled_frame_ledger_sha256",
    ):
        _digest(transport[key], f"translated_transport.{key}")

    artifacts = _exact_keys(manifest["artifacts"], ARTIFACT_KEYS, "artifacts")
    for key in (
        "wav_sha256",
        "interleaved_pcm_sha256",
        "source_channel_pcm_sha256",
        "translated_channel_pcm_sha256",
        "timing_csv_sha256",
        "block_ledger_csv_sha256",
    ):
        _digest(artifacts[key], f"artifacts.{key}")
    for key in (
        "wav_byte_count",
        "interleaved_pcm_sample_count",
        "timing_csv_byte_count",
        "block_ledger_csv_byte_count",
    ):
        _plain_int(artifacts[key], f"artifacts.{key}", minimum=1)

    _validate_runtime(manifest["runtime"])

    policy = _exact_keys(
        manifest["playback_policy"],
        PLAYBACK_POLICY_KEYS,
        "playback_policy",
    )
    _literal(
        policy["adaptive_playback_enabled"],
        True,
        "playback_policy.adaptive_playback_enabled",
    )
    _literal(policy["loss_policy"], "no_drop", "playback_policy.loss_policy")
    expected_policy = {
        "target_queue_seconds": 5,
        "urgent_queue_seconds": 8,
        "limit_queue_seconds": 10,
        "catch_up_release_seconds": 4,
        "urgent_release_seconds": 7,
        "normal_rate": 1,
        "catch_up_rate": 1.05,
        "urgent_rate": 1.1,
    }
    for key, expected in expected_policy.items():
        if _finite_number(policy[key], f"playback_policy.{key}") != expected:
            raise _invalid("policy", f"playback_policy.{key} is not v1")

    queue_gate = _exact_keys(
        manifest["queue_gate"],
        QUEUE_GATE_KEYS,
        "queue_gate",
    )
    _literal(
        queue_gate["metric"],
        "exact_piecewise_linear_audio_context_schedule",
        "queue_gate.metric",
    )
    if _finite_number(
        queue_gate["p95_objective_seconds"],
        "queue_gate.p95_objective_seconds",
    ) != QUEUE_P95_OBJECTIVE_SECONDS:
        raise _invalid("policy", "queue p95 objective is not v1")
    if _finite_number(
        queue_gate["peak_limit_seconds"],
        "queue_gate.peak_limit_seconds",
    ) != QUEUE_PEAK_LIMIT_SECONDS:
        raise _invalid("policy", "queue peak limit is not v1")
    _literal(
        queue_gate["result"],
        "pending_offline_validation",
        "queue_gate.result",
    )


def _validate_artifact_bindings(
    manifest: dict[str, Any],
    wav_bytes: bytes,
    block_bytes: bytes,
    timing_bytes: bytes,
) -> None:
    artifacts = manifest["artifacts"]
    bindings = (
        ("wav", wav_bytes, "wav_sha256", "wav_byte_count"),
        (
            "block ledger",
            block_bytes,
            "block_ledger_csv_sha256",
            "block_ledger_csv_byte_count",
        ),
        (
            "timing CSV",
            timing_bytes,
            "timing_csv_sha256",
            "timing_csv_byte_count",
        ),
    )
    for label, data, digest_key, count_key in bindings:
        if artifacts[count_key] != len(data):
            raise _invalid("artifact_hash", f"{label} byte count does not match")
        if artifacts[digest_key] != _sha256(data):
            raise _invalid("artifact_hash", f"{label} SHA-256 does not match")


def _parse_wav(wav_bytes: bytes, manifest: dict[str, Any]) -> WavEvidence:
    if len(wav_bytes) < 44:
        raise _invalid("wav", "WAV is shorter than the canonical header")
    if (
        wav_bytes[0:4] != b"RIFF"
        or wav_bytes[8:12] != b"WAVE"
        or wav_bytes[12:16] != b"fmt "
        or wav_bytes[36:40] != b"data"
    ):
        raise _invalid("wav", "WAV is not canonical RIFF/fmt/data")
    (
        riff_size,
        fmt_size,
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        data_size,
    ) = (
        struct.unpack_from("<I", wav_bytes, 4)[0],
        struct.unpack_from("<I", wav_bytes, 16)[0],
        struct.unpack_from("<H", wav_bytes, 20)[0],
        struct.unpack_from("<H", wav_bytes, 22)[0],
        struct.unpack_from("<I", wav_bytes, 24)[0],
        struct.unpack_from("<I", wav_bytes, 28)[0],
        struct.unpack_from("<H", wav_bytes, 32)[0],
        struct.unpack_from("<H", wav_bytes, 34)[0],
        struct.unpack_from("<I", wav_bytes, 40)[0],
    )
    expected = (
        len(wav_bytes) - 8,
        16,
        1,
        CHANNEL_COUNT,
        SAMPLE_RATE_HZ,
        SAMPLE_RATE_HZ * BYTES_PER_FRAME,
        BYTES_PER_FRAME,
        BITS_PER_SAMPLE,
        len(wav_bytes) - 44,
    )
    actual = (
        riff_size,
        fmt_size,
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        data_size,
    )
    if actual != expected or data_size % BYTES_PER_FRAME != 0:
        raise _invalid("wav", "WAV format or exact length is invalid")

    pcm = wav_bytes[44:]
    frame_count = len(pcm) // BYTES_PER_FRAME
    if frame_count != manifest["clock"]["capture_frame_count"]:
        raise _invalid("wav", "WAV frame count does not match the clock")
    if (
        manifest["artifacts"]["interleaved_pcm_sample_count"]
        != frame_count * CHANNEL_COUNT
    ):
        raise _invalid("wav", "interleaved sample count does not match")
    if manifest["artifacts"]["interleaved_pcm_sha256"] != _sha256(pcm):
        raise _invalid("artifact_hash", "interleaved PCM SHA-256 does not match")

    source_pcm = bytearray(frame_count * BYTES_PER_MONO_SAMPLE)
    translated_pcm = bytearray(frame_count * BYTES_PER_MONO_SAMPLE)
    translated_nonzero = 0
    for frame in range(frame_count):
        interleaved_offset = frame * BYTES_PER_FRAME
        mono_offset = frame * BYTES_PER_MONO_SAMPLE
        source_pcm[mono_offset : mono_offset + 2] = pcm[
            interleaved_offset : interleaved_offset + 2
        ]
        translated_sample = pcm[
            interleaved_offset + 2 : interleaved_offset + 4
        ]
        translated_pcm[mono_offset : mono_offset + 2] = translated_sample
        if translated_sample != b"\x00\x00":
            translated_nonzero += 1
    source_bytes = bytes(source_pcm)
    translated_bytes = bytes(translated_pcm)
    artifacts = manifest["artifacts"]
    if artifacts["source_channel_pcm_sha256"] != _sha256(source_bytes):
        raise _invalid("artifact_hash", "source-channel SHA-256 does not match")
    if artifacts["translated_channel_pcm_sha256"] != _sha256(
        translated_bytes
    ):
        raise _invalid(
            "artifact_hash",
            "translated-channel SHA-256 does not match",
        )
    if (
        manifest["translated_output"]["nonzero_sample_count"]
        != translated_nonzero
    ):
        raise _invalid(
            "translated_capture",
            "translated nonzero sample count does not match",
        )
    if translated_nonzero == 0:
        raise _invalid("translated_capture", "translated channel is silent")

    source = manifest["source_reference"]
    active_start = source["capture_frame_start"] * BYTES_PER_MONO_SAMPLE
    active_end = source["capture_frame_end_exclusive"] * BYTES_PER_MONO_SAMPLE
    active = source_bytes[active_start:active_end]
    active_hash = _sha256(active)
    if (
        len(active) != SOURCE_FRAME_COUNT * BYTES_PER_MONO_SAMPLE
        or active_hash != source["input_pcm_sha256"]
        or active_hash != source["active_slice_pcm_sha256"]
    ):
        raise _invalid(
            "source_binding",
            "active source slice does not match the input PCM",
        )
    outside = source_bytes[:active_start] + source_bytes[active_end:]
    if any(outside):
        raise _invalid(
            "source_binding",
            "source channel is nonzero outside its active interval",
        )

    return WavEvidence(
        frame_count=frame_count,
        pcm=pcm,
        source_pcm=source_bytes,
        translated_pcm=translated_bytes,
        translated_nonzero_sample_count=translated_nonzero,
    )


def _parse_csv_rows(
    data: bytes,
    expected_header: list[str],
    label: str,
) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid("csv", f"{label} is not UTF-8") from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        rows = list(reader)
    except csv.Error as exc:
        raise _invalid("csv", f"{label} is malformed") from exc
    if not rows or rows[0] != expected_header:
        raise _invalid("csv", f"{label} header is not the exact v1 schema")
    if len(rows) == 1:
        raise _invalid("csv", f"{label} has no evidence rows")
    result: list[dict[str, str]] = []
    for line_number, values in enumerate(rows[1:], start=2):
        if len(values) != len(expected_header):
            raise _invalid("csv", f"{label} row {line_number} has wrong width")
        row = dict(zip(expected_header, values))
        row["_line"] = str(line_number)
        result.append(row)
    return result


def _csv_int(
    row: dict[str, str],
    key: str,
    context: str,
    *,
    minimum: int | None = None,
) -> int:
    raw = row.get(key, "")
    if re.fullmatch(r"-?[0-9]+", raw) is None:
        raise _invalid("timing", f"{context} has invalid {key}")
    value = int(raw)
    if minimum is not None and value < minimum:
        raise _invalid("timing", f"{context} has invalid {key}")
    return value


def _csv_float(
    row: dict[str, str],
    key: str,
    context: str,
    *,
    minimum: float | None = None,
) -> float:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError) as exc:
        raise _invalid("timing", f"{context} has invalid {key}") from exc
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise _invalid("timing", f"{context} has invalid {key}")
    return value


def _validate_timing_row_schemas(rows: list[dict[str, str]]) -> None:
    """Reject unused cells and type every nonempty timing value."""

    for row in rows:
        context = f"timing row {row['_line']}"
        source = row["source"]
        stage = row["stage"]
        nonempty = {
            key
            for key, value in row.items()
            if key != "_line" and value != ""
        }
        if source == "backend":
            if stage not in BACKEND_STAGES:
                raise _invalid(
                    "timing",
                    f"{context} has an unsupported backend stage",
                )
            required = BASE_TIMING_COLUMNS
            allowed = BASE_TIMING_COLUMNS
        elif source == "client":
            if stage not in CLIENT_STAGE_REQUIRED_COLUMNS:
                raise _invalid(
                    "timing",
                    f"{context} has an unsupported client stage",
                )
            required = CLIENT_STAGE_REQUIRED_COLUMNS[stage]
            allowed = required | CLIENT_STAGE_OPTIONAL_COLUMNS[stage]
        else:
            raise _invalid("timing", f"{context} has unsupported source")
        if not required.issubset(nonempty):
            raise _invalid(
                "timing",
                f"{context} is missing required stage fields",
            )
        if not nonempty.issubset(allowed):
            raise _invalid(
                "free_text",
                f"{context} contains data in an unused column",
            )

        for key in nonempty & INTEGER_TIMING_COLUMNS:
            raw = row[key]
            if re.fullmatch(r"-?[0-9]+", raw) is None:
                raise _invalid("timing", f"{context} has invalid {key}")
            value = int(raw)
            minimum = -1 if key == "chunk_index" else 0
            if value < minimum:
                raise _invalid("timing", f"{context} has invalid {key}")
        for key in nonempty & FLOAT_TIMING_COLUMNS:
            try:
                value = float(row[key])
            except ValueError as exc:
                raise _invalid(
                    "timing",
                    f"{context} has invalid {key}",
                ) from exc
            if not math.isfinite(value):
                raise _invalid("timing", f"{context} has invalid {key}")

        if row["timestamp_ms"] == "" or float(row["timestamp_ms"]) < 0:
            raise _invalid("timing", f"{context} has invalid timestamp_ms")
        if (
            row["source_position_sec"] == ""
            or float(row["source_position_sec"]) < 0
        ):
            raise _invalid(
                "timing",
                f"{context} has invalid source_position_sec",
            )
        if row["audio_bytes"] == "" or int(row["audio_bytes"]) < 0:
            raise _invalid("timing", f"{context} has invalid audio_bytes")

        if "input_pcm_sha256" in nonempty:
            if SHA256_PATTERN.fullmatch(row["input_pcm_sha256"]) is None:
                raise _invalid("timing", f"{context} has invalid PCM SHA-256")
        for key in ("adaptive_playback_enabled", "input_ledger_valid"):
            if key in nonempty and row[key] not in {"true", "false"}:
                raise _invalid("timing", f"{context} has invalid {key}")
        if (
            "playback_mode" in nonempty
            and row["playback_mode"]
            not in {"normal", "catch-up", "urgent", "over-limit"}
        ):
            raise _invalid("timing", f"{context} has invalid playback_mode")
        if (
            "terminal_status" in nonempty
            and row["terminal_status"] not in {"completed", "error"}
        ):
            raise _invalid("timing", f"{context} has invalid terminal_status")
        if (
            "source_timing_basis" in nonempty
            and row["source_timing_basis"]
            not in {
                "attributed_range",
                "audio_processed/nonsemantic",
                "partial_range",
                "unavailable",
            }
        ):
            raise _invalid(
                "timing",
                f"{context} has invalid source_timing_basis",
            )
        for key in ("source_start_ms", "source_end_ms"):
            if key not in nonempty or row[key] == "null":
                continue
            try:
                value = float(row[key])
            except ValueError as exc:
                raise _invalid(
                    "free_text",
                    f"{context} has invalid {key}",
                ) from exc
            if not math.isfinite(value) or value < 0:
                raise _invalid("timing", f"{context} has invalid {key}")
        if (
            "clock_sample_reason" in nonempty
            and row["clock_sample_reason"]
            not in {
                "session_started",
                "queue_started",
                "interval",
                "queue_drained",
                "session_stopped",
            }
        ):
            raise _invalid(
                "timing",
                f"{context} has invalid clock_sample_reason",
            )
        if "clock_sample_basis" in nonempty:
            basis = row["clock_sample_basis"]
            if basis not in {
                "get_output_timestamp",
                "current_time_bracket",
            }:
                raise _invalid(
                    "timing",
                    f"{context} has invalid clock_sample_basis",
                )
            output_fields_present = {
                "clock_sample_output_context_sec",
                "clock_sample_output_performance_client_ms",
            }.issubset(nonempty)
            if output_fields_present != (basis == "get_output_timestamp"):
                raise _invalid(
                    "timing",
                    f"{context} has inconsistent output-clock fields",
                )


def _parse_block_ledger(
    block_bytes: bytes,
    manifest: dict[str, Any],
    wav: WavEvidence,
) -> None:
    rows = _parse_csv_rows(
        block_bytes,
        BLOCK_LEDGER_COLUMNS,
        "block ledger",
    )
    clock = manifest["clock"]
    if len(rows) != clock["block_count"]:
        raise _invalid("block_ledger", "block count does not match manifest")
    expected_sequence = 0
    expected_context_frame = clock["capture_start_context_frame"]
    expected_capture_frame = 0
    pcm_offset = 0
    for row in rows:
        context = f"block ledger row {row['_line']}"
        if row["schema"] != BLOCK_LEDGER_SCHEMA:
            raise _invalid("block_ledger", f"{context} has wrong schema")
        sequence = _csv_int(row, "sequence", context, minimum=0)
        start_context_frame = _csv_int(
            row,
            "start_context_frame",
            context,
            minimum=0,
        )
        capture_frame_start = _csv_int(
            row,
            "capture_frame_start",
            context,
            minimum=0,
        )
        frame_count = _csv_int(row, "frame_count", context, minimum=1)
        digest = row["interleaved_pcm_sha256"]
        if SHA256_PATTERN.fullmatch(digest) is None:
            raise _invalid("block_ledger", f"{context} has invalid SHA-256")
        if (
            sequence != expected_sequence
            or start_context_frame != expected_context_frame
            or capture_frame_start != expected_capture_frame
        ):
            raise _invalid(
                "block_ledger",
                f"{context} contains a gap, overlap, or reorder",
            )
        block_bytes_count = frame_count * BYTES_PER_FRAME
        block_pcm = wav.pcm[pcm_offset : pcm_offset + block_bytes_count]
        if (
            len(block_pcm) != block_bytes_count
            or _sha256(block_pcm) != digest
        ):
            raise _invalid(
                "block_ledger",
                f"{context} PCM SHA-256 does not match",
            )
        expected_sequence += 1
        expected_context_frame += frame_count
        expected_capture_frame += frame_count
        pcm_offset += block_bytes_count
    if (
        expected_context_frame
        != clock["capture_end_context_frame_exclusive"]
        or expected_capture_frame != clock["capture_frame_count"]
        or pcm_offset != len(wav.pcm)
    ):
        raise _invalid("block_ledger", "blocks do not tile the capture")


def _validate_input_ledger(
    rows: list[dict[str, str]],
    manifest: dict[str, Any],
) -> InputClockEvidence:
    input_rows = [
        row
        for row in rows
        if row["source"] == "client" and row["stage"] == "chunk_sent"
    ]
    if len(input_rows) != SOURCE_CHUNK_COUNT:
        raise _invalid("input_ledger", "input ledger must contain 200 chunks")
    source = manifest["source_reference"]
    previous_received_ms = -math.inf
    first_boundary: int | None = None
    first_received_ms: float | None = None
    last_boundary: int | None = None
    last_received_ms: float | None = None
    first_client_clock_origin_ms: float | None = None
    maximum_elapsed_drift_ms = 0.0
    maximum_worklet_delivery_lag_frames = 0
    maximum_main_receipt_lag_frames = 0
    maximum_chunk_emission_lag_frames = 0
    for index, row in enumerate(input_rows):
        context = f"input chunk {index}"
        start = index * SOURCE_CHUNK_FRAMES
        end = start + SOURCE_CHUNK_FRAMES
        if (
            _csv_int(row, "chunk_index", context, minimum=0) != index
            or _csv_int(row, "audio_bytes", context, minimum=1)
            != SOURCE_CHUNK_FRAMES * BYTES_PER_MONO_SAMPLE
            or _csv_int(
                row,
                "input_source_sample_start",
                context,
                minimum=0,
            )
            != start
            or _csv_int(
                row,
                "input_source_sample_end_exclusive",
                context,
                minimum=1,
            )
            != end
            or _csv_int(row, "input_sample_rate_hz", context, minimum=1)
            != SAMPLE_RATE_HZ
            or _csv_int(row, "input_pcm_sample_count", context, minimum=1)
            != SOURCE_FRAME_COUNT
            or row["input_pcm_sha256"] != source["input_pcm_sha256"]
            or row["input_ledger_valid"] != "true"
        ):
            raise _invalid("input_ledger", f"{context} is inconsistent")
        expected_boundary = source["source_start_context_frame"] + end
        boundary = _csv_int(
            row,
            "input_source_boundary_context_frame",
            context,
            minimum=0,
        )
        delivered = _csv_int(
            row,
            "input_source_boundary_delivered_after_context_frame",
            context,
            minimum=0,
        )
        receipt_before = _csv_int(
            row,
            "input_source_boundary_received_context_frame_before",
            context,
            minimum=0,
        )
        receipt_after = _csv_int(
            row,
            "input_source_boundary_received_context_frame_after",
            context,
            minimum=0,
        )
        emitted_context_frame = _csv_int(
            row,
            "input_chunk_emitted_context_frame",
            context,
            minimum=0,
        )
        if (
            boundary != expected_boundary
            or delivered < boundary
            or delivered - boundary >= RENDER_QUANTUM_FRAMES
            or receipt_before < delivered
            or receipt_after < receipt_before
            or emitted_context_frame < receipt_after
            or receipt_after - boundary > 1_600
            or emitted_context_frame - boundary > 1_600
        ):
            raise _invalid(
                "input_ledger",
                f"{context} lacks a bounded causal AudioContext chain",
            )
        maximum_worklet_delivery_lag_frames = max(
            maximum_worklet_delivery_lag_frames,
            delivered - boundary,
        )
        maximum_main_receipt_lag_frames = max(
            maximum_main_receipt_lag_frames,
            receipt_after - boundary,
        )
        maximum_chunk_emission_lag_frames = max(
            maximum_chunk_emission_lag_frames,
            emitted_context_frame - boundary,
        )
        received_ms = _csv_float(
            row,
            "input_source_boundary_received_client_ms",
            context,
            minimum=0,
        )
        emitted_ms = _csv_float(
            row,
            "input_chunk_emitted_client_ms",
            context,
            minimum=0,
        )
        timestamp_ms = _csv_float(
            row,
            "timestamp_ms",
            context,
            minimum=0,
        )
        if (
            received_ms < previous_received_ms
            or emitted_ms + CSV_CLIENT_TOLERANCE_MS < received_ms
        ):
            raise _invalid("input_ledger", f"{context} is reordered")
        client_clock_origin_ms = emitted_ms - timestamp_ms
        if first_client_clock_origin_ms is None:
            first_client_clock_origin_ms = client_clock_origin_ms
        elif (
            abs(client_clock_origin_ms - first_client_clock_origin_ms)
            > CLIENT_CLOCK_ORIGIN_TOLERANCE_MS
        ):
            raise _invalid(
                "input_ledger",
                f"{context} contradicts the client monotonic clock origin",
            )
        if first_boundary is None or first_received_ms is None:
            first_boundary = boundary
            first_received_ms = received_ms
        expected_elapsed_ms = (
            (boundary - first_boundary) / SAMPLE_RATE_HZ * 1000.0
        )
        observed_elapsed_ms = received_ms - first_received_ms
        maximum_elapsed_drift_ms = max(
            maximum_elapsed_drift_ms,
            abs(observed_elapsed_ms - expected_elapsed_ms),
        )
        allowed_elapsed_drift_ms = (
            INPUT_CLOCK_RECEIPT_LAG_LIMIT_MS
            + expected_elapsed_ms * INPUT_CLOCK_INTERIOR_RATE_TOLERANCE
        )
        if (
            abs(observed_elapsed_ms - expected_elapsed_ms)
            > allowed_elapsed_drift_ms + CSV_CLIENT_TOLERANCE_MS
        ):
            raise _invalid(
                "input_pacing",
                f"{context} is outside the registered cumulative "
                "wall-clock pacing envelope",
            )
        last_boundary = boundary
        last_received_ms = received_ms
        previous_received_ms = received_ms
    if (
        first_boundary is None
        or first_received_ms is None
        or last_boundary is None
        or last_received_ms is None
        or last_boundary <= first_boundary
    ):
        raise _invalid("input_pacing", "source boundary span is unavailable")
    expected_boundary_span_ms = (
        (last_boundary - first_boundary) / SAMPLE_RATE_HZ * 1000.0
    )
    observed_boundary_span_ms = last_received_ms - first_received_ms
    minimum_span_ms = (
        expected_boundary_span_ms * INPUT_CLOCK_RATE_MIN_RATIO
    )
    maximum_span_ms = (
        expected_boundary_span_ms * INPUT_CLOCK_RATE_MAX_RATIO
    )
    wall_to_source_ratio = (
        observed_boundary_span_ms / expected_boundary_span_ms
    )
    if (
        observed_boundary_span_ms + CSV_CLIENT_TOLERANCE_MS
        < minimum_span_ms
        or observed_boundary_span_ms - CSV_CLIENT_TOLERANCE_MS
        > maximum_span_ms
    ):
        raise _invalid(
            "input_pacing",
            "source boundary wall-clock pacing is outside the registered "
            f"one-percent envelope (client/source ratio="
            f"{wall_to_source_ratio:.6f})",
        )
    return InputClockEvidence(
        maximum_worklet_delivery_lag_frames=(
            maximum_worklet_delivery_lag_frames
        ),
        maximum_main_receipt_lag_frames=maximum_main_receipt_lag_frames,
        maximum_chunk_emission_lag_frames=(
            maximum_chunk_emission_lag_frames
        ),
        expected_boundary_span_ms=round(expected_boundary_span_ms, 6),
        observed_boundary_span_ms=round(observed_boundary_span_ms, 6),
        wall_to_source_ratio=round(wall_to_source_ratio, 9),
        maximum_elapsed_drift_ms=round(maximum_elapsed_drift_ms, 6),
    )


def _identity(row: dict[str, str], context: str) -> tuple[int, int, int]:
    if _csv_int(
        row,
        "audio_metadata_protocol_version",
        context,
        minimum=1,
    ) != 1:
        raise _invalid("protocol", f"{context} is not protocol v1")
    return (
        _csv_int(row, "stream_generation", context, minimum=0),
        _csv_int(row, "parent_sequence_id", context, minimum=0),
        _csv_int(row, "audio_frame_id", context, minimum=0),
    )


def _policy_from_manifest(manifest: dict[str, Any]) -> PlaybackPolicy:
    value = manifest["playback_policy"]
    return PlaybackPolicy(
        target_queue_seconds=float(value["target_queue_seconds"]),
        urgent_queue_seconds=float(value["urgent_queue_seconds"]),
        limit_queue_seconds=float(value["limit_queue_seconds"]),
        catch_up_release_seconds=float(value["catch_up_release_seconds"]),
        urgent_release_seconds=float(value["urgent_release_seconds"]),
        normal_rate=float(value["normal_rate"]),
        catch_up_rate=float(value["catch_up_rate"]),
        urgent_rate=float(value["urgent_rate"]),
    )


def _transport_ledger_hash(rows: list[dict[str, str]]) -> str:
    entries = []
    for sequence, row in enumerate(rows):
        entries.append(
            {
                "sequence": sequence,
                "stream_generation": int(row["stream_generation"]),
                "parent_sequence_id": int(row["parent_sequence_id"]),
                "audio_frame_id": int(row["audio_frame_id"]),
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "channels": 1,
                "bytes_per_sample": BYTES_PER_MONO_SAMPLE,
                "audio_bytes": int(row["audio_bytes"]),
            }
        )
    return _sha256(_canonical_json(entries))


def _validate_timing(
    timing_bytes: bytes,
    manifest: dict[str, Any],
    wav: WavEvidence,
) -> TimingEvidence:
    rows = _parse_csv_rows(timing_bytes, TIMING_COLUMNS, "timing CSV")
    _validate_timing_row_schemas(rows)

    input_clock = _validate_input_ledger(rows, manifest)
    session_rows = [
        row
        for row in rows
        if row["source"] == "client"
        and row["stage"] == "playback_session_started"
    ]
    if len(session_rows) != 1:
        raise _invalid("timing", "one playback session declaration is required")
    if session_rows[0]["adaptive_playback_enabled"] != "true":
        raise _invalid("policy", "timing session is not adaptive")
    terminal_indices = [
        index
        for index, row in enumerate(rows)
        if row["source"] == "client" and row["stage"] == "server_terminal"
    ]
    if len(terminal_indices) != 1:
        raise _invalid(
            "terminal",
            "timing CSV must contain one server terminal",
        )
    terminal_index = terminal_indices[0]
    if rows[terminal_index]["terminal_status"] != "completed":
        raise _invalid(
            "terminal",
            "timing server terminal is not completed",
        )
    if any(
        row["source"] == "client"
        and row["stage"]
        in {
            "chunk_sent",
            "audio_received",
            "audio_parent_complete",
            "playback_chunk_scheduled",
        }
        for row in rows[terminal_index + 1 :]
    ):
        raise _invalid(
            "terminal",
            "translated audio appears after server completion",
        )
    input_ended_indices = [
        index
        for index, row in enumerate(rows)
        if row["source"] == "client" and row["stage"] == "input_ended"
    ]
    chunk_indices = [
        index
        for index, row in enumerate(rows)
        if row["source"] == "client" and row["stage"] == "chunk_sent"
    ]
    if (
        len(input_ended_indices) != 1
        or len(chunk_indices) != SOURCE_CHUNK_COUNT
        or input_ended_indices[0] <= chunk_indices[-1]
        or input_ended_indices[0] >= terminal_index
    ):
        raise _invalid(
            "terminal",
            "input completion does not follow all source chunks",
        )
    input_ended_row = rows[input_ended_indices[0]]
    if (
        input_ended_row["chunk_index"] != "-1"
        or input_ended_row["audio_bytes"] != "0"
        or not math.isclose(
            float(input_ended_row["source_position_sec"]),
            SOURCE_FRAME_COUNT / SAMPLE_RATE_HZ,
            rel_tol=0.0,
            abs_tol=0.001,
        )
    ):
        raise _invalid("input_ledger", "input completion row is inconsistent")

    received_rows = [
        row
        for row in rows
        if row["source"] == "client" and row["stage"] == "audio_received"
    ]
    schedule_rows = [
        row
        for row in rows
        if row["source"] == "client"
        and row["stage"] == "playback_chunk_scheduled"
    ]
    complete_rows = [
        row
        for row in rows
        if row["source"] == "client"
        and row["stage"] == "audio_parent_complete"
    ]
    translated = manifest["translated_output"]
    if (
        len(received_rows) != translated["received_frame_count"]
        or len(schedule_rows) != translated["scheduled_frame_count"]
        or len(complete_rows) != translated["completed_parent_count"]
    ):
        raise _invalid("timing", "translated row counts do not match manifest")
    if not received_rows or not schedule_rows:
        raise _invalid("timing", "translated timing rows are empty")

    received: dict[tuple[int, int, int], dict[str, str]] = {}
    schedules: dict[tuple[int, int, int], dict[str, str]] = {}
    for index, row in enumerate(received_rows):
        context = f"audio receipt {index}"
        key = _identity(row, context)
        if key in received:
            raise _invalid("protocol", "audio receipt identity is duplicated")
        if _csv_int(row, "chunk_index", context, minimum=0) != index:
            raise _invalid("protocol", f"{context} index is fragmented")
        audio_bytes = _csv_int(row, "audio_bytes", context, minimum=1)
        if audio_bytes % BYTES_PER_MONO_SAMPLE != 0:
            raise _invalid("protocol", f"{context} is not PCM16")
        _csv_float(row, "binary_receipt_client_ms", context, minimum=0)
        received[key] = row

    playback_clock_session_id: int | None = None
    chunks: list[AudioChunk] = []
    captured_schedule: list[dict[str, Any]] = []
    for index, row in enumerate(schedule_rows):
        context = f"playback schedule {index}"
        key = _identity(row, context)
        if key in schedules:
            raise _invalid("protocol", "playback schedule identity is duplicated")
        schedule_bytes = _csv_int(row, "audio_bytes", context, minimum=1)
        if schedule_bytes % BYTES_PER_MONO_SAMPLE != 0:
            raise _invalid("protocol", f"{context} is not PCM16")
        session_id = _csv_int(
            row,
            "playback_clock_session_id",
            context,
            minimum=1,
        )
        if playback_clock_session_id is None:
            playback_clock_session_id = session_id
        elif session_id != playback_clock_session_id:
            raise _invalid("clock", "playback clock session changed")
        arrival = _csv_float(
            row,
            "audio_context_time_at_schedule_sec",
            context,
            minimum=0,
        )
        media_duration = _csv_float(
            row,
            "media_duration_sec",
            context,
            minimum=0,
        )
        if media_duration <= 0:
            raise _invalid("timing", f"{context} has empty media")
        expected_media_duration = (
            schedule_bytes
            / BYTES_PER_MONO_SAMPLE
            / SAMPLE_RATE_HZ
        )
        _close(media_duration, expected_media_duration, f"{context} media")
        chunks.append(
            AudioChunk(
                arrival_seconds=arrival,
                duration_seconds=expected_media_duration,
                audio_bytes=schedule_bytes,
                source_index=index,
            )
        )
        captured_schedule.append(
            {
                "start": _csv_float(
                    row,
                    "scheduled_start_context_sec",
                    context,
                    minimum=0,
                ),
                "end": _csv_float(
                    row,
                    "scheduled_end_context_sec",
                    context,
                    minimum=0,
                ),
                "start_frame": _csv_int(
                    row,
                    "scheduled_start_context_frame_floor",
                    context,
                    minimum=0,
                ),
                "end_frame": _csv_int(
                    row,
                    "scheduled_end_context_frame_exclusive",
                    context,
                    minimum=1,
                ),
                "duration": _csv_float(
                    row,
                    "scheduled_duration_sec",
                    context,
                    minimum=0,
                ),
                "wait": _csv_float(
                    row,
                    "playback_wait_sec",
                    context,
                    minimum=0,
                ),
                "queue": _csv_float(
                    row,
                    "queue_depth_sec",
                    context,
                    minimum=0,
                ),
                "rate": _csv_float(
                    row,
                    "playback_rate",
                    context,
                    minimum=0,
                ),
                "mode": row["playback_mode"],
            }
        )
        schedules[key] = row

    try:
        simulation = simulate_playback(
            chunks,
            input_end_seconds=(
                manifest["source_reference"][
                    "source_end_context_frame_exclusive"
                ]
                / SAMPLE_RATE_HZ
            ),
            adaptive=True,
            policy=_policy_from_manifest(manifest),
        )
    except ValueError as exc:
        raise _invalid(
            "timing_replay",
            "captured schedule cannot be replayed",
        ) from exc
    for index, (replayed, captured) in enumerate(
        zip(simulation.schedule, captured_schedule)
    ):
        context = f"playback schedule {index}"
        _close(captured["start"], replayed.start_seconds, f"{context} start")
        _close(captured["end"], replayed.end_seconds, f"{context} end")
        _close(
            captured["duration"],
            replayed.end_seconds - replayed.start_seconds,
            f"{context} duration",
        )
        _close(
            captured["wait"],
            replayed.wait_before_playback_seconds,
            f"{context} wait",
        )
        _close(
            captured["queue"],
            replayed.queue_depth_seconds,
            f"{context} queue",
        )
        _close(
            captured["rate"],
            replayed.playback_rate,
            f"{context} rate",
        )
        if captured["mode"] != replayed.playback_mode:
            raise _invalid("timing_replay", f"{context} mode does not replay")
        replayed_start_frame = math.floor(
            replayed.start_seconds * SAMPLE_RATE_HZ
            + 2.220446049250313e-16
        )
        replayed_end_frame = math.ceil(
            replayed.end_seconds * SAMPLE_RATE_HZ
            - 2.220446049250313e-16
        )
        if (
            captured["start_frame"] != replayed_start_frame
            or captured["end_frame"] != replayed_end_frame
        ):
            raise _invalid(
                "timing_replay",
                f"{context} integer frame interval does not replay",
            )

    final_end_frame = captured_schedule[-1]["end_frame"]
    if (
        translated["last_scheduled_end_context_frame_exclusive"]
        != final_end_frame
    ):
        raise _invalid("clock", "translated endpoint does not match schedule")
    clock = manifest["clock"]
    if (
        simulation.schedule[0].start_seconds * SAMPLE_RATE_HZ
        < clock["capture_start_context_frame"] - 1
        or final_end_frame > clock["capture_end_context_frame_exclusive"]
    ):
        raise _invalid("clock", "translated schedule is outside the capture")

    scheduled_intervals: list[list[int]] = []
    capture_start_frame = manifest["clock"]["capture_start_context_frame"]
    capture_end_frame = manifest["clock"][
        "capture_end_context_frame_exclusive"
    ]
    for index, captured in enumerate(captured_schedule):
        start_frame = captured["start_frame"]
        end_frame = captured["end_frame"]
        if (
            start_frame < capture_start_frame
            or end_frame > capture_end_frame
            or end_frame <= start_frame
            or (
                index > 0
                and start_frame < captured_schedule[index - 1]["start_frame"]
            )
        ):
            raise _invalid(
                "translated_capture",
                "translated schedule interval is invalid",
            )
        if scheduled_intervals and start_frame <= scheduled_intervals[-1][1]:
            scheduled_intervals[-1][1] = max(
                scheduled_intervals[-1][1],
                end_frame,
            )
        else:
            scheduled_intervals.append([start_frame, end_frame])
    interval_index = 0
    nonzero_outside_schedule = 0
    for capture_frame in range(wav.frame_count):
        absolute_frame = capture_start_frame + capture_frame
        while (
            interval_index < len(scheduled_intervals)
            and absolute_frame >= scheduled_intervals[interval_index][1]
        ):
            interval_index += 1
        inside = (
            interval_index < len(scheduled_intervals)
            and absolute_frame >= scheduled_intervals[interval_index][0]
        )
        translated_offset = capture_frame * BYTES_PER_MONO_SAMPLE
        if (
            not inside
            and wav.translated_pcm[
                translated_offset : translated_offset + 2
            ]
            != b"\x00\x00"
        ):
            nonzero_outside_schedule += 1
    if (
        nonzero_outside_schedule
        != manifest["translated_output"][
            "nonzero_outside_scheduled_sample_count"
        ]
        or nonzero_outside_schedule != 0
    ):
        raise _invalid(
            "translated_capture",
            "translated audio exists outside scheduled intervals",
        )

    complete: dict[tuple[int, int], dict[str, str]] = {}
    for index, row in enumerate(complete_rows):
        context = f"parent completion {index}"
        if _csv_int(
            row,
            "audio_metadata_protocol_version",
            context,
            minimum=1,
        ) != 1:
            raise _invalid("protocol", f"{context} is not protocol v1")
        key = (
            _csv_int(row, "stream_generation", context, minimum=0),
            _csv_int(row, "parent_sequence_id", context, minimum=0),
        )
        if key in complete:
            raise _invalid("protocol", "parent completion is duplicated")
        _csv_int(row, "audio_frame_count", context, minimum=1)
        _csv_int(row, "audio_bytes", context, minimum=1)
        complete[key] = row

    generations = {
        generation
        for generation, _parent, _frame in received
    }
    generations.update(
        generation
        for generation, _parent, _frame in schedules
    )
    generations.update(generation for generation, _parent in complete)
    if len(generations) != 1 or next(iter(generations), 0) <= 0:
        raise _invalid(
            "protocol",
            "exactly one positive stream generation is required",
        )

    def parent_order(
        frame_rows: list[dict[str, str]],
    ) -> list[int]:
        order: list[int] = []
        previous: int | None = None
        for frame_row in frame_rows:
            parent = int(frame_row["parent_sequence_id"])
            if parent != previous:
                order.append(parent)
                previous = parent
        return order

    expected_parent_order = list(range(len(complete_rows)))
    if (
        parent_order(received_rows) != expected_parent_order
        or parent_order(schedule_rows) != expected_parent_order
        or [int(row["parent_sequence_id"]) for row in complete_rows]
        != expected_parent_order
    ):
        raise _invalid(
            "protocol",
            "parent sequence IDs are not contiguous and in order",
        )
    for parent_key, completion in complete.items():
        related_receipts = [
            row
            for key, row in received.items()
            if key[:2] == parent_key
        ]
        related_schedules = [
            row
            for key, row in schedules.items()
            if key[:2] == parent_key
        ]
        completion_line = int(completion["_line"])
        if (
            not related_receipts
            or not related_schedules
            or completion_line
            <= max(
                int(row["_line"])
                for row in related_receipts + related_schedules
            )
            or completion_line >= int(rows[terminal_index]["_line"])
        ):
            raise _invalid(
                "protocol",
                "parent completion is not after its frames and before terminal",
            )
    for parent_sequence_id in range(len(complete_rows) - 1):
        current_completion = complete[
            (next(iter(generations)), parent_sequence_id)
        ]
        next_receipt_lines = [
            int(row["_line"])
            for key, row in received.items()
            if key[1] == parent_sequence_id + 1
        ]
        next_schedule_lines = [
            int(row["_line"])
            for key, row in schedules.items()
            if key[1] == parent_sequence_id + 1
        ]
        if (
            not next_receipt_lines
            or not next_schedule_lines
            or int(current_completion["_line"]) >= min(next_receipt_lines)
            or int(current_completion["_line"]) >= min(next_schedule_lines)
        ):
            raise _invalid(
                "protocol",
                "parent completion does not causally precede the next parent",
            )

    protocol_pcm_no_loss = set(received) == set(schedules)
    if protocol_pcm_no_loss:
        for key in received:
            receipt = received[key]
            schedule = schedules[key]
            if (
                receipt["audio_bytes"] != schedule["audio_bytes"]
                or receipt["chunk_index"] != schedule["chunk_index"]
                or int(receipt["_line"]) >= int(schedule["_line"])
                or not math.isclose(
                    float(receipt["binary_receipt_client_ms"]),
                    float(schedule["binary_receipt_client_ms"]),
                    rel_tol=0.0,
                    abs_tol=CSV_CLIENT_TOLERANCE_MS,
                )
                or float(schedule["schedule_performance_client_ms"])
                + CSV_CLIENT_TOLERANCE_MS
                < float(receipt["binary_receipt_client_ms"])
            ):
                protocol_pcm_no_loss = False
                break
    received_parents: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for (generation, parent, frame), row in received.items():
        received_parents.setdefault((generation, parent), []).append(
            (frame, int(row["audio_bytes"]))
        )
    if set(received_parents) != set(complete):
        protocol_pcm_no_loss = False
    for parent_key, frames in received_parents.items():
        completion = complete.get(parent_key)
        if completion is None:
            continue
        ordered = sorted(frames)
        expected_count = int(completion["audio_frame_count"])
        expected_bytes = int(completion["audio_bytes"])
        if (
            [frame for frame, _ in ordered] != list(range(expected_count))
            or sum(audio_bytes for _, audio_bytes in ordered) != expected_bytes
        ):
            protocol_pcm_no_loss = False

    received_audio_bytes = sum(
        int(row["audio_bytes"]) for row in received_rows
    )
    scheduled_audio_bytes = sum(
        int(row["audio_bytes"]) for row in schedule_rows
    )
    transport = manifest["translated_transport"]
    received_ledger_hash = _transport_ledger_hash(received_rows)
    scheduled_ledger_hash = _transport_ledger_hash(schedule_rows)
    if (
        transport["received_frame_count"] != len(received_rows)
        or transport["scheduled_frame_count"] != len(schedule_rows)
        or transport["received_pcm_byte_count"] != received_audio_bytes
        or transport["scheduled_pcm_byte_count"] != scheduled_audio_bytes
        or transport["received_frame_ledger_sha256"]
        != received_ledger_hash
        or transport["scheduled_frame_ledger_sha256"]
        != scheduled_ledger_hash
    ):
        raise _invalid(
            "transport_binding",
            "translated transport aggregate does not match timing",
        )
    transport_no_loss = (
        transport["received_frame_count"]
        == transport["scheduled_frame_count"]
        and transport["received_pcm_byte_count"]
        == transport["scheduled_pcm_byte_count"]
        and transport["received_ordered_pcm_sha256"]
        == transport["scheduled_ordered_pcm_sha256"]
        and transport["received_frame_ledger_sha256"]
        == transport["scheduled_frame_ledger_sha256"]
    )
    protocol_pcm_no_loss = protocol_pcm_no_loss and transport_no_loss
    if received_audio_bytes != scheduled_audio_bytes:
        protocol_pcm_no_loss = False

    summary = simulation.summary
    queue_bound = (
        summary.time_weighted_queue_p95_seconds
        <= QUEUE_P95_OBJECTIVE_SECONDS
        and summary.peak_queue_depth_seconds <= QUEUE_PEAK_LIMIT_SECONDS
    )
    return TimingEvidence(
        input_chunk_count=SOURCE_CHUNK_COUNT,
        received_frame_count=len(received_rows),
        scheduled_frame_count=len(schedule_rows),
        completed_parent_count=len(complete_rows),
        received_audio_bytes=received_audio_bytes,
        scheduled_audio_bytes=scheduled_audio_bytes,
        protocol_pcm_no_loss=protocol_pcm_no_loss,
        time_weighted_queue_p95_seconds=(
            summary.time_weighted_queue_p95_seconds
        ),
        peak_queue_depth_seconds=summary.peak_queue_depth_seconds,
        queue_bound=queue_bound,
        chunks_dropped=summary.chunks_dropped,
        input_clock=input_clock,
    )


def _base_report(status: str) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "mechanical_gate": {
            "capture_integrity_verified": False,
            "protocol_pcm_no_loss_verified": None,
            "queue_bound_verified": None,
            "passed": False,
        },
        "queue": {
            "metric": "exact_piecewise_linear_audio_context_schedule",
            "p95_objective_seconds": QUEUE_P95_OBJECTIVE_SECONDS,
            "peak_limit_seconds": QUEUE_PEAK_LIMIT_SECONDS,
            "time_weighted_p95_seconds": None,
            "peak_seconds": None,
        },
        "input_common_clock_lag": {
            "worklet_delivery_limit_frames_exclusive": RENDER_QUANTUM_FRAMES,
            "main_thread_limit_frames_inclusive": 1_600,
            "maximum_worklet_delivery_lag_frames": None,
            "maximum_main_receipt_lag_frames": None,
            "maximum_chunk_emission_lag_frames": None,
            "maximum_worklet_delivery_lag_ms": None,
            "maximum_main_receipt_lag_ms": None,
            "maximum_chunk_emission_lag_ms": None,
        },
        "input_common_clock_rate": {
            "minimum_ratio_inclusive": INPUT_CLOCK_RATE_MIN_RATIO,
            "maximum_ratio_inclusive": INPUT_CLOCK_RATE_MAX_RATIO,
            "expected_boundary_span_ms": None,
            "observed_boundary_span_ms": None,
            "wall_to_source_ratio": None,
            "maximum_elapsed_drift_ms": None,
            "interior_rate_tolerance": (
                INPUT_CLOCK_INTERIOR_RATE_TOLERANCE
            ),
            "receipt_lag_allowance_ms": (
                INPUT_CLOCK_RECEIPT_LAG_LIMIT_MS
            ),
        },
        "evidence": None,
        "claim_boundary": {
            "semantic_latency_status": "not_evaluated",
            "physical_dac_output_proven": False,
            "acoustic_audibility_proven": False,
            "translation_quality_proven": False,
            "audience_reaction_alignment_proven": False,
        },
        "errors": [],
    }


def validate_bundle(
    manifest_path: Path,
    wav_path: Path,
    blocks_path: Path,
    timing_path: Path,
) -> tuple[dict[str, Any], int]:
    """Validate one exact four-file bundle and return report plus exit code."""

    manifest_bytes = _load_bounded(
        manifest_path,
        MAX_JSON_BYTES,
        "manifest",
    )
    wav_bytes = _load_bounded(wav_path, MAX_WAV_BYTES, "WAV")
    block_bytes = _load_bounded(
        blocks_path,
        MAX_LEDGER_BYTES,
        "block ledger",
    )
    timing_bytes = _load_bounded(
        timing_path,
        MAX_TIMING_BYTES,
        "timing CSV",
    )
    manifest = _load_json_object(manifest_bytes)
    _validate_local_worklet_module()
    _validate_manifest(manifest)
    _validate_artifact_bindings(
        manifest,
        wav_bytes,
        block_bytes,
        timing_bytes,
    )
    wav = _parse_wav(wav_bytes, manifest)
    _parse_block_ledger(block_bytes, manifest, wav)
    timing = _validate_timing(timing_bytes, manifest, wav)

    mechanical_pass = timing.protocol_pcm_no_loss and timing.queue_bound
    status = "PASS" if mechanical_pass else "FAIL"
    report = _base_report(status)
    report["mechanical_gate"] = {
        "capture_integrity_verified": True,
        "protocol_pcm_no_loss_verified": timing.protocol_pcm_no_loss,
        "queue_bound_verified": timing.queue_bound,
        "passed": mechanical_pass,
    }
    report["queue"]["time_weighted_p95_seconds"] = (
        timing.time_weighted_queue_p95_seconds
    )
    report["queue"]["peak_seconds"] = timing.peak_queue_depth_seconds
    input_clock = timing.input_clock
    report["input_common_clock_lag"].update(
        {
            "maximum_worklet_delivery_lag_frames": (
                input_clock.maximum_worklet_delivery_lag_frames
            ),
            "maximum_main_receipt_lag_frames": (
                input_clock.maximum_main_receipt_lag_frames
            ),
            "maximum_chunk_emission_lag_frames": (
                input_clock.maximum_chunk_emission_lag_frames
            ),
            "maximum_worklet_delivery_lag_ms": (
                input_clock.maximum_worklet_delivery_lag_frames
                / SAMPLE_RATE_HZ
                * 1000.0
            ),
            "maximum_main_receipt_lag_ms": (
                input_clock.maximum_main_receipt_lag_frames
                / SAMPLE_RATE_HZ
                * 1000.0
            ),
            "maximum_chunk_emission_lag_ms": (
                input_clock.maximum_chunk_emission_lag_frames
                / SAMPLE_RATE_HZ
                * 1000.0
            ),
        }
    )
    report["input_common_clock_rate"].update(
        {
            "expected_boundary_span_ms": (
                input_clock.expected_boundary_span_ms
            ),
            "observed_boundary_span_ms": (
                input_clock.observed_boundary_span_ms
            ),
            "wall_to_source_ratio": input_clock.wall_to_source_ratio,
            "maximum_elapsed_drift_ms": (
                input_clock.maximum_elapsed_drift_ms
            ),
        }
    )
    report["evidence"] = {
        "capture_id": manifest["capture_id"],
        "manifest_sha256": _sha256(manifest_bytes),
        "wav_sha256": _sha256(wav_bytes),
        "block_ledger_csv_sha256": _sha256(block_bytes),
        "timing_csv_sha256": _sha256(timing_bytes),
        "capture_frame_count": wav.frame_count,
        "source_input_pcm_sha256": manifest["source_reference"][
            "input_pcm_sha256"
        ],
        "source_input_frame_count": SOURCE_FRAME_COUNT,
        "input_chunk_count": timing.input_chunk_count,
        "received_frame_count": timing.received_frame_count,
        "scheduled_frame_count": timing.scheduled_frame_count,
        "completed_parent_count": timing.completed_parent_count,
        "received_audio_bytes": timing.received_audio_bytes,
        "scheduled_audio_bytes": timing.scheduled_audio_bytes,
        "translated_nonzero_sample_count": (
            wav.translated_nonzero_sample_count
        ),
        "chunks_dropped": timing.chunks_dropped,
    }
    return report, 0 if mechanical_pass else 1


def _invalid_report(error: PreflightValidationError) -> dict[str, Any]:
    report = _base_report("INVALID")
    report["errors"] = [{"code": error.code, "message": str(error)}]
    return report


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a private rendered-digital common-clock preflight bundle"
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--blocks", type=Path, required=True)
    parser.add_argument("--timing-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = (args.manifest, args.wav, args.blocks, args.timing_csv)
    try:
        output_resolved = args.output.resolve()
        if any(path.resolve() == output_resolved for path in inputs):
            raise _invalid("io", "output must not replace an evidence input")
        report, exit_code = validate_bundle(*inputs)
    except PreflightValidationError as exc:
        report = _invalid_report(exc)
        exit_code = 2
    except Exception:
        report = _invalid_report(
            _invalid("internal", "validator encountered an unexpected error")
        )
        exit_code = 2
    _atomic_write_json(args.output, report)
    print(f"{report['status']}: {args.output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
