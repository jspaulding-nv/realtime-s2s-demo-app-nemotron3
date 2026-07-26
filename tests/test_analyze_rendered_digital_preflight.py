import csv
import hashlib
import io
import json
import math
import shutil
import struct
import sys
from array import array
from pathlib import Path

import pytest

from analyze_rendered_digital_preflight import (
    BLOCK_LEDGER_COLUMNS,
    BLOCK_LEDGER_SCHEMA,
    CAPTURE_SCHEMA,
    REPORT_SCHEMA,
    TIMING_COLUMNS,
    main,
)
from playback_simulation import AudioChunk, DEFAULT_PLAYBACK_POLICY, simulate_playback


SAMPLE_RATE = 16_000
CAPTURE_START = 100_000
CAPTURE_FRAMES = 1_000_000
CAPTURE_END = CAPTURE_START + CAPTURE_FRAMES
SOURCE_CAPTURE_START = 4_000
SOURCE_FRAMES = 960_000
SOURCE_START = CAPTURE_START + SOURCE_CAPTURE_START
SOURCE_END = SOURCE_START + SOURCE_FRAMES
BLOCK_FRAMES = 8_000
PREFLIGHT_FILE_SHA256 = (
    "0c2cb04d9774f60472b55355f587da2148a053f3a55c05ff36c7dfc23be5c257"
)
PREFLIGHT_PCM_SHA256 = (
    "81720f2e23e5b85df4eb2be0bbd486b6591d0b1ed98580118e9e2e1e466bd51c"
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def little_endian_bytes(values):
    copy = array("h", values)
    if sys.byteorder != "little":
        copy.byteswap()
    return copy.tobytes()


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def encode_wav(pcm):
    header = bytearray(44)
    header[0:4] = b"RIFF"
    struct.pack_into("<I", header, 4, 36 + len(pcm))
    header[8:12] = b"WAVE"
    header[12:16] = b"fmt "
    struct.pack_into("<IHHIIHH", header, 16, 16, 1, 2, 16_000, 64_000, 4, 16)
    header[36:40] = b"data"
    struct.pack_into("<I", header, 40, len(pcm))
    return bytes(header) + pcm


def serialize_rows(columns, rows, *, final_newline):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(column, "") for column in columns])
    value = output.getvalue()
    if not final_newline:
        value = value.removesuffix("\n")
    return value.encode()


def parse_rows(data):
    values = list(csv.reader(io.StringIO(data.decode(), newline="")))
    header = values[0]
    return header, [dict(zip(header, row)) for row in values[1:]]


def transport_ledger(rows):
    return [
        {
            "sequence": sequence,
            "stream_generation": int(row["stream_generation"]),
            "parent_sequence_id": int(row["parent_sequence_id"]),
            "audio_frame_id": int(row["audio_frame_id"]),
            "sample_rate_hz": SAMPLE_RATE,
            "channels": 1,
            "bytes_per_sample": 2,
            "audio_bytes": int(row["audio_bytes"]),
        }
        for sequence, row in enumerate(rows)
    ]


def update_transport_manifest(manifest, rows):
    received = [
        row
        for row in rows
        if row["source"] == "client" and row["stage"] == "audio_received"
    ]
    scheduled = [
        row
        for row in rows
        if row["source"] == "client"
        and row["stage"] == "playback_chunk_scheduled"
    ]
    completed = [
        row
        for row in rows
        if row["source"] == "client"
        and row["stage"] == "audio_parent_complete"
    ]
    received_ledger = transport_ledger(received)
    scheduled_ledger = transport_ledger(scheduled)
    received_bytes = sum(int(row["audio_bytes"]) for row in received)
    scheduled_bytes = sum(int(row["audio_bytes"]) for row in scheduled)
    received_pcm_hash = sha256(
        b"received-pcm-v1" + canonical_json(received_ledger)
    )
    scheduled_pcm_hash = (
        received_pcm_hash
        if received_ledger == scheduled_ledger
        else sha256(b"scheduled-pcm-v1" + canonical_json(scheduled_ledger))
    )
    manifest["translated_output"]["received_frame_count"] = len(received)
    manifest["translated_output"]["scheduled_frame_count"] = len(scheduled)
    manifest["translated_output"]["completed_parent_count"] = len(completed)
    manifest["translated_transport"] = {
        "received_frame_count": len(received),
        "scheduled_frame_count": len(scheduled),
        "received_pcm_byte_count": received_bytes,
        "scheduled_pcm_byte_count": scheduled_bytes,
        "received_ordered_pcm_sha256": received_pcm_hash,
        "scheduled_ordered_pcm_sha256": scheduled_pcm_hash,
        "received_frame_ledger_sha256": sha256(
            canonical_json(received_ledger)
        ),
        "scheduled_frame_ledger_sha256": sha256(
            canonical_json(scheduled_ledger)
        ),
    }


def source_and_translated(pcm):
    source = bytearray(CAPTURE_FRAMES * 2)
    translated = bytearray(CAPTURE_FRAMES * 2)
    for frame in range(CAPTURE_FRAMES):
        offset = frame * 4
        mono = frame * 2
        source[mono : mono + 2] = pcm[offset : offset + 2]
        translated[mono : mono + 2] = pcm[offset + 2 : offset + 4]
    return bytes(source), bytes(translated)


def make_timing_rows(duration_seconds=0.5):
    rows = [
        {
            "source": "client",
            "stage": "playback_session_started",
            "timestamp_ms": "0.00",
            "chunk_index": "-1",
            "source_position_sec": "0.000",
            "audio_bytes": "0",
            "adaptive_playback_enabled": "true",
        }
    ]
    source_zero_ms = 1_000.0
    for index in range(200):
        start = index * 4_800
        end = start + 4_800
        boundary = SOURCE_START + end
        delivered = boundary + ((128 - boundary % 128) % 128)
        received_ms = source_zero_ms + end / SAMPLE_RATE * 1_000
        rows.append(
            {
                "source": "client",
                "stage": "chunk_sent",
                "timestamp_ms": f"{received_ms - source_zero_ms:.2f}",
                "chunk_index": str(index),
                "source_position_sec": f"{start / SAMPLE_RATE:.3f}",
                "audio_bytes": "9600",
                "input_sample_zero_client_ms": f"{source_zero_ms:.3f}",
                "input_chunk_emitted_client_ms": f"{received_ms:.3f}",
                "input_source_sample_start": str(start),
                "input_source_sample_end_exclusive": str(end),
                "input_sample_rate_hz": str(SAMPLE_RATE),
                "input_pcm_sha256": "",
                "input_pcm_sample_count": str(SOURCE_FRAMES),
                "input_ledger_valid": "true",
                "input_source_boundary_context_frame": str(boundary),
                "input_source_boundary_delivered_after_context_frame": str(
                    delivered
                ),
                "input_source_boundary_received_context_frame_before": str(
                    delivered
                ),
                "input_source_boundary_received_context_frame_after": str(
                    delivered
                ),
                "input_source_boundary_received_client_ms": (
                    f"{received_ms:.3f}"
                ),
                "input_chunk_emitted_context_frame": str(delivered),
            }
        )

    rows.append(
        {
            "source": "client",
            "stage": "input_ended",
            "timestamp_ms": "60000.00",
            "chunk_index": "-1",
            "source_position_sec": "60.000",
            "audio_bytes": "0",
        }
    )
    audio_bytes = round(duration_seconds * SAMPLE_RATE) * 2
    arrival_context_seconds = 7.0
    simulation = simulate_playback(
        [
            AudioChunk(
                arrival_seconds=arrival_context_seconds,
                duration_seconds=duration_seconds,
                audio_bytes=audio_bytes,
            )
        ],
        input_end_seconds=SOURCE_END / SAMPLE_RATE,
        adaptive=True,
        policy=DEFAULT_PLAYBACK_POLICY,
    )
    scheduled = simulation.schedule[0]
    identity = {
        "audio_metadata_protocol_version": "1",
        "stream_generation": "1",
        "parent_sequence_id": "0",
        "audio_frame_id": "0",
        "source_start_ms": "null",
        "source_end_ms": "null",
        "source_timing_basis": "unavailable",
    }
    rows.extend(
        [
            {
                "source": "client",
                "stage": "audio_received",
                "timestamp_ms": "1000.00",
                "chunk_index": "0",
                "source_position_sec": "0.000",
                "audio_bytes": str(audio_bytes),
                "binary_receipt_client_ms": "2000.000",
                **identity,
            },
            {
                "source": "client",
                "stage": "playback_chunk_scheduled",
                "timestamp_ms": "1001.00",
                "chunk_index": "0",
                "source_position_sec": "0.000",
                "audio_bytes": str(audio_bytes),
                "media_duration_sec": f"{duration_seconds:.6f}",
                "scheduled_duration_sec": (
                    f"{scheduled.end_seconds - scheduled.start_seconds:.6f}"
                ),
                "playback_wait_sec": (
                    f"{scheduled.wait_before_playback_seconds:.6f}"
                ),
                "queue_depth_sec": f"{scheduled.queue_depth_seconds:.6f}",
                "playback_rate": f"{scheduled.playback_rate:.2f}",
                "playback_mode": scheduled.playback_mode,
                "binary_receipt_client_ms": "2000.000",
                "schedule_performance_client_ms": "2001.000",
                "audio_context_time_at_schedule_sec": (
                    f"{arrival_context_seconds:.6f}"
                ),
                "scheduled_start_context_sec": (
                    f"{scheduled.start_seconds:.6f}"
                ),
                "scheduled_end_context_sec": f"{scheduled.end_seconds:.6f}",
                "scheduled_start_context_frame_floor": str(
                    math.floor(
                        scheduled.start_seconds * SAMPLE_RATE
                        + sys.float_info.epsilon
                    )
                ),
                "scheduled_end_context_frame_exclusive": str(
                    math.ceil(
                        scheduled.end_seconds * SAMPLE_RATE
                        - sys.float_info.epsilon
                    )
                ),
                "projected_scheduled_start_client_ms": "2001.000",
                "playback_clock_session_id": "1",
                **identity,
            },
            {
                "source": "client",
                "stage": "audio_parent_complete",
                "timestamp_ms": "1002.00",
                "chunk_index": "-1",
                "source_position_sec": "0.000",
                "audio_bytes": str(audio_bytes),
                "audio_metadata_protocol_version": "1",
                "stream_generation": "1",
                "parent_sequence_id": "0",
                "audio_frame_count": "1",
                "source_start_ms": "null",
                "source_end_ms": "null",
                "source_timing_basis": "unavailable",
                "parent_complete_received_client_ms": "2002.000",
            },
            {
                "source": "client",
                "stage": "server_terminal",
                "timestamp_ms": "1003.00",
                "chunk_index": "-1",
                "source_position_sec": "60.000",
                "audio_bytes": "0",
                "terminal_status": "completed",
            },
        ]
    )
    return rows, math.ceil(
        scheduled.end_seconds * SAMPLE_RATE - sys.float_info.epsilon
    )


def write_manifest(path, manifest):
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def bind_timing(bundle, manifest, rows):
    timing = serialize_rows(TIMING_COLUMNS, rows, final_newline=False)
    bundle["timing"].write_bytes(timing)
    manifest["artifacts"]["timing_csv_sha256"] = sha256(timing)
    manifest["artifacts"]["timing_csv_byte_count"] = len(timing)
    update_transport_manifest(manifest, rows)


def bind_blocks(bundle, manifest, rows):
    blocks = serialize_rows(BLOCK_LEDGER_COLUMNS, rows, final_newline=True)
    bundle["blocks"].write_bytes(blocks)
    manifest["artifacts"]["block_ledger_csv_sha256"] = sha256(blocks)
    manifest["artifacts"]["block_ledger_csv_byte_count"] = len(blocks)


def rebind_wav_and_blocks(bundle, manifest):
    wav = bundle["wav"].read_bytes()
    pcm = wav[44:]
    source, translated = source_and_translated(pcm)
    manifest["artifacts"].update(
        {
            "wav_sha256": sha256(wav),
            "wav_byte_count": len(wav),
            "interleaved_pcm_sha256": sha256(pcm),
            "interleaved_pcm_sample_count": len(pcm) // 2,
            "source_channel_pcm_sha256": sha256(source),
            "translated_channel_pcm_sha256": sha256(translated),
        }
    )
    manifest["translated_output"]["nonzero_sample_count"] = sum(
        translated[index : index + 2] != b"\x00\x00"
        for index in range(0, len(translated), 2)
    )
    block_rows = []
    for sequence, capture_start in enumerate(range(0, CAPTURE_FRAMES, 8_000)):
        frame_count = min(8_000, CAPTURE_FRAMES - capture_start)
        block_pcm = pcm[capture_start * 4 : (capture_start + frame_count) * 4]
        block_rows.append(
            {
                "schema": BLOCK_LEDGER_SCHEMA,
                "sequence": str(sequence),
                "start_context_frame": str(CAPTURE_START + capture_start),
                "capture_frame_start": str(capture_start),
                "frame_count": str(frame_count),
                "interleaved_pcm_sha256": sha256(block_pcm),
            }
        )
    bind_blocks(bundle, manifest, block_rows)


def make_bundle(directory):
    directory.mkdir(parents=True, exist_ok=True)
    bundle = {
        "manifest": directory / "capture.manifest.json",
        "wav": directory / "capture.stereo.wav",
        "blocks": directory / "capture.blocks.csv",
        "timing": directory / "capture.timing.csv",
        "output": directory / "report.json",
    }

    preflight_file = (
        Path(__file__).resolve().parents[1] / "test_audio" / "preflight.wav"
    )
    preflight_wav = preflight_file.read_bytes()
    assert sha256(preflight_wav) == PREFLIGHT_FILE_SHA256
    active_pcm = preflight_wav[248:]
    assert len(active_pcm) == SOURCE_FRAMES * 2
    assert sha256(active_pcm) == PREFLIGHT_PCM_SHA256
    source_active = array("h")
    source_active.frombytes(active_pcm)
    if sys.byteorder != "little":
        source_active.byteswap()
    interleaved = array("h", [0]) * (CAPTURE_FRAMES * 2)
    for index, sample in enumerate(source_active):
        interleaved[(SOURCE_CAPTURE_START + index) * 2] = sample
    for frame in range(12_000, 20_000):
        interleaved[frame * 2 + 1] = 1_234
    pcm = little_endian_bytes(interleaved)
    wav = encode_wav(pcm)
    bundle["wav"].write_bytes(wav)

    source_full, translated_full = source_and_translated(pcm)
    active_pcm = little_endian_bytes(source_active)
    timing_rows, translated_end = make_timing_rows()
    input_hash = sha256(active_pcm)
    for row in timing_rows:
        if row["stage"] == "chunk_sent":
            row["input_pcm_sha256"] = input_hash
    timing = serialize_rows(TIMING_COLUMNS, timing_rows, final_newline=False)
    bundle["timing"].write_bytes(timing)

    block_rows = []
    for sequence, capture_start in enumerate(range(0, CAPTURE_FRAMES, 8_000)):
        frame_count = min(8_000, CAPTURE_FRAMES - capture_start)
        block_pcm = pcm[capture_start * 4 : (capture_start + frame_count) * 4]
        block_rows.append(
            {
                "schema": BLOCK_LEDGER_SCHEMA,
                "sequence": str(sequence),
                "start_context_frame": str(CAPTURE_START + capture_start),
                "capture_frame_start": str(capture_start),
                "frame_count": str(frame_count),
                "interleaved_pcm_sha256": sha256(block_pcm),
            }
        )
    blocks = serialize_rows(BLOCK_LEDGER_COLUMNS, block_rows, final_newline=True)
    bundle["blocks"].write_bytes(blocks)

    runtime = {
        "repository_commit": "a" * 40,
        "repository_dirty": False,
        "pipeline_mode": "staged",
        "audio_metadata_protocol_version": 1,
        "telemetry_schema_version": 3,
        "punctuation_segmentation": {
            "max_chars": 240,
            "max_age_ms": 2000,
        },
        "staged_execution": {
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
        },
        "browser_capture": {
            "frontend_repository_commit": "a" * 40,
            "frontend_repository_dirty": False,
            "worklet_module_sha256": (
                "8baf6193f097acc3c2663ca91299a19f"
                "68b1e7c17deaa586db339a073a7d0a5d"
            ),
        },
        "asr": {
            "image_digest": (
                "sha256:"
                "0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850"
            ),
            "profile_id": "nemotron-asr-streaming_en-US_batch32",
            "eou_ms": 800,
            "word_time_offsets": True,
        },
        "nmt": {
            "image_digest": (
                "sha256:"
                "3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb"
            ),
            "model_id": "megatronnmt_any_any_1b",
            "language_pair_id": "en-US_to_es-US",
        },
        "tts": {
            "image_digest": (
                "sha256:"
                "6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d"
            ),
            "profile_id": "magpie-tts-multilingual_batch8",
            "voice_id": "Magpie-Multilingual.ES-US.Isabela",
            "incremental_publish_enabled": True,
            "incremental_frame_ms": 500,
        },
    }
    runtime_hash = sha256(canonical_json(runtime))
    manifest = {
        "schema": CAPTURE_SCHEMA,
        "capture_id": "123e4567-e89b-42d3-a456-426614174000",
        "created_at_utc": "2026-07-25T12:34:56.789Z",
        "evidence_status": "unverified_browser_export",
        "run_terminal": {
            "server_status": "completed",
            "dashboard_phase": "completed",
        },
        "claims": {
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
        },
        "clock": {
            "basis": "single_audio_context_render_quantum",
            "sample_rate_hz": SAMPLE_RATE,
            "channel_count": 2,
            "capture_start_context_frame": CAPTURE_START,
            "capture_end_context_frame_exclusive": CAPTURE_END,
            "capture_frame_count": CAPTURE_FRAMES,
            "block_count": len(block_rows),
            "context_state_violation_count": 0,
            "visibility_violation_count": 0,
        },
        "channels": [
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
        ],
        "source_reference": {
            "input_file_sha256": PREFLIGHT_FILE_SHA256,
            "input_pcm_sha256": input_hash,
            "input_pcm_frame_count": SOURCE_FRAMES,
            "source_start_context_frame": SOURCE_START,
            "source_end_context_frame_exclusive": SOURCE_END,
            "capture_frame_start": SOURCE_CAPTURE_START,
            "capture_frame_end_exclusive": (
                SOURCE_CAPTURE_START + SOURCE_FRAMES
            ),
            "active_slice_pcm_sha256": input_hash,
            "outside_active_nonzero_sample_count": 0,
            "chunk_frames": 4_800,
            "chunk_count": 200,
        },
        "translated_output": {
            "received_frame_count": 1,
            "scheduled_frame_count": 1,
            "completed_parent_count": 1,
            "nonzero_sample_count": 8_000,
            "nonzero_outside_scheduled_sample_count": 0,
            "last_scheduled_end_context_frame_exclusive": translated_end,
        },
        "translated_transport": {},
        "artifacts": {
            "wav_sha256": sha256(wav),
            "wav_byte_count": len(wav),
            "interleaved_pcm_sha256": sha256(pcm),
            "interleaved_pcm_sample_count": len(pcm) // 2,
            "source_channel_pcm_sha256": sha256(source_full),
            "translated_channel_pcm_sha256": sha256(translated_full),
            "timing_csv_sha256": sha256(timing),
            "timing_csv_byte_count": len(timing),
            "block_ledger_csv_sha256": sha256(blocks),
            "block_ledger_csv_byte_count": len(blocks),
        },
        "runtime": {
            **runtime,
            "normalized_config_sha256": runtime_hash,
        },
        "playback_policy": {
            "adaptive_playback_enabled": True,
            "loss_policy": "no_drop",
            "target_queue_seconds": 5,
            "urgent_queue_seconds": 8,
            "limit_queue_seconds": 10,
            "catch_up_release_seconds": 4,
            "urgent_release_seconds": 7,
            "normal_rate": 1,
            "catch_up_rate": 1.05,
            "urgent_rate": 1.1,
        },
        "queue_gate": {
            "metric": "exact_piecewise_linear_audio_context_schedule",
            "p95_objective_seconds": 5,
            "peak_limit_seconds": 10,
            "result": "pending_offline_validation",
        },
    }
    update_transport_manifest(manifest, timing_rows)
    write_manifest(bundle["manifest"], manifest)
    return bundle


@pytest.fixture(scope="module")
def base_bundle(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("rendered-digital-base"))


@pytest.fixture
def bundle(base_bundle, tmp_path):
    destination = tmp_path / "bundle"
    shutil.copytree(base_bundle["manifest"].parent, destination)
    return {
        key: destination / path.name
        for key, path in base_bundle.items()
    }


def invoke(bundle):
    exit_code = main(
        [
            "--manifest",
            str(bundle["manifest"]),
            "--wav",
            str(bundle["wav"]),
            "--blocks",
            str(bundle["blocks"]),
            "--timing-csv",
            str(bundle["timing"]),
            "--output",
            str(bundle["output"]),
        ]
    )
    return exit_code, json.loads(bundle["output"].read_text())


def test_happy_path_is_mechanical_pass_with_narrow_claims(bundle):
    exit_code, report = invoke(bundle)

    assert exit_code == 0
    assert report["schema"] == REPORT_SCHEMA
    assert report["status"] == "PASS"
    assert report["mechanical_gate"] == {
        "capture_integrity_verified": True,
        "passed": True,
        "protocol_pcm_no_loss_verified": True,
        "queue_bound_verified": True,
    }
    assert report["queue"]["time_weighted_p95_seconds"] < 5
    assert report["queue"]["peak_seconds"] < 10
    assert report["input_common_clock_lag"] == {
        "main_thread_limit_frames_inclusive": 1600,
        "maximum_chunk_emission_lag_frames": 64,
        "maximum_chunk_emission_lag_ms": 4.0,
        "maximum_main_receipt_lag_frames": 64,
        "maximum_main_receipt_lag_ms": 4.0,
        "maximum_worklet_delivery_lag_frames": 64,
        "maximum_worklet_delivery_lag_ms": 4.0,
        "worklet_delivery_limit_frames_exclusive": 128,
    }
    assert report["input_common_clock_rate"] == {
        "expected_boundary_span_ms": 59_700.0,
        "interior_rate_tolerance": 0.01,
        "maximum_elapsed_drift_ms": 0.0,
        "maximum_ratio_inclusive": 1.01,
        "minimum_ratio_inclusive": 0.99,
        "observed_boundary_span_ms": 59_700.0,
        "receipt_lag_allowance_ms": 100.0,
        "wall_to_source_ratio": 1.0,
    }
    assert report["claim_boundary"] == {
        "acoustic_audibility_proven": False,
        "audience_reaction_alignment_proven": False,
        "physical_dac_output_proven": False,
        "semantic_latency_status": "not_evaluated",
        "translation_quality_proven": False,
    }


def test_approved_worklet_digest_matches_checked_in_module():
    worklet = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "public"
        / "rendered-digital-recorder.worklet.js"
    )
    assert sha256(worklet.read_bytes()) == (
        "8baf6193f097acc3c2663ca91299a19f"
        "68b1e7c17deaa586db339a073a7d0a5d"
    )


def test_malformed_wav_is_invalid_even_when_file_hash_is_rebound(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    wav = bytearray(bundle["wav"].read_bytes())
    struct.pack_into("<H", wav, 34, 24)
    bundle["wav"].write_bytes(wav)
    manifest["artifacts"]["wav_sha256"] = sha256(wav)
    manifest["artifacts"]["wav_byte_count"] = len(wav)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["status"] == "INVALID"
    assert report["errors"][0]["code"] == "wav"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest.update({"operator_name": "Person Name"}),
        lambda manifest: manifest.update({"created_at_utc": "Person Name"}),
    ],
    ids=["unknown-key", "pii-like-free-text"],
)
def test_manifest_rejects_unknown_keys_and_free_text(bundle, mutation):
    manifest = json.loads(bundle["manifest"].read_text())
    mutation(manifest)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["status"] == "INVALID"
    assert report["errors"][0]["code"] in {"schema", "free_text"}


def test_unrebound_wav_hash_corruption_is_invalid(bundle):
    wav = bytearray(bundle["wav"].read_bytes())
    wav[-1] ^= 1
    bundle["wav"].write_bytes(wav)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "artifact_hash"


@pytest.mark.parametrize("mutation", ["gap", "overlap", "reorder"])
def test_block_ledger_rejects_gap_overlap_and_reorder(bundle, mutation):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["blocks"].read_bytes())
    if mutation == "gap":
        rows[1]["start_context_frame"] = str(
            int(rows[1]["start_context_frame"]) + 1
        )
    elif mutation == "overlap":
        rows[1]["capture_frame_start"] = str(
            int(rows[1]["capture_frame_start"]) - 1
        )
    else:
        rows[0], rows[1] = rows[1], rows[0]
    bind_blocks(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "block_ledger"


def test_source_active_slice_mismatch_is_invalid(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    wav = bytearray(bundle["wav"].read_bytes())
    active_offset = 44 + SOURCE_CAPTURE_START * 4
    original = struct.unpack_from("<h", wav, active_offset)[0]
    struct.pack_into("<h", wav, active_offset, original + 1)
    bundle["wav"].write_bytes(wav)
    rebind_wav_and_blocks(bundle, manifest)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "source_binding"


def test_source_nonzero_outside_active_interval_is_invalid(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    wav = bytearray(bundle["wav"].read_bytes())
    struct.pack_into("<h", wav, 44, 1)
    bundle["wav"].write_bytes(wav)
    rebind_wav_and_blocks(bundle, manifest)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "source_binding"


def test_timing_protocol_version_missing_is_invalid(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    receipt = next(row for row in rows if row["stage"] == "audio_received")
    receipt["audio_metadata_protocol_version"] = ""
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "timing"


def test_terminal_must_precede_no_later_receive_or_schedule(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    schedule = next(
        row for row in rows if row["stage"] == "playback_chunk_scheduled"
    )
    duplicate = dict(schedule)
    duplicate["audio_frame_id"] = "1"
    duplicate["chunk_index"] = "1"
    rows.append(duplicate)
    manifest["translated_output"]["scheduled_frame_count"] = 2
    manifest["translated_output"][
        "last_scheduled_end_context_frame_exclusive"
    ] = int(duplicate["scheduled_end_context_frame_exclusive"])
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "terminal"


def test_input_ended_is_required_after_all_source_chunks(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    rows = [row for row in rows if row["stage"] != "input_ended"]
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "terminal"


def test_integer_schedule_interval_must_replay_exactly(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    schedule = next(
        row for row in rows if row["stage"] == "playback_chunk_scheduled"
    )
    schedule["scheduled_start_context_frame_floor"] = str(
        int(schedule["scheduled_start_context_frame_floor"]) + 1
    )
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "timing_replay"


def test_stream_generation_must_be_one_positive_value(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    for row in rows:
        if row["stage"] in {
            "audio_received",
            "playback_chunk_scheduled",
            "audio_parent_complete",
        }:
            row["stream_generation"] = "0"
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "protocol"


def test_runtime_must_bind_clean_approved_configuration(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    manifest["runtime"]["repository_dirty"] = True
    runtime = {
        key: value
        for key, value in manifest["runtime"].items()
        if key != "normalized_config_sha256"
    }
    manifest["runtime"]["normalized_config_sha256"] = sha256(
        canonical_json(runtime)
    )
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["status"] == "INVALID"


def test_runtime_rejects_nonapproved_staged_execution_pin(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    manifest["runtime"]["staged_execution"]["nmt_queue_max_size"] = 5
    runtime = {
        key: value
        for key, value in manifest["runtime"].items()
        if key != "normalized_config_sha256"
    }
    manifest["runtime"]["normalized_config_sha256"] = sha256(
        canonical_json(runtime)
    )
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["status"] == "INVALID"


def test_transport_ledger_hash_corruption_is_invalid(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    manifest["translated_transport"]["received_frame_ledger_sha256"] = "f" * 64
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "transport_binding"


def test_transport_pcm_mismatch_is_no_loss_fail(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    manifest["translated_transport"]["scheduled_ordered_pcm_sha256"] = "f" * 64
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert report["mechanical_gate"]["protocol_pcm_no_loss_verified"] is False


@pytest.mark.parametrize("source", ["client", "backend"])
def test_unused_timing_columns_cannot_hide_free_text(bundle, source):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    if source == "client":
        session = next(
            row for row in rows if row["stage"] == "playback_session_started"
        )
        session["source_start_ms"] = "Person Name"
    else:
        rows.append(
            {
                column: ""
                for column in TIMING_COLUMNS
            }
        )
        rows[-1].update(
            {
                "source": "backend",
                "stage": "audio_received",
                "timestamp_ms": "1.00",
                "chunk_index": "0",
                "source_position_sec": "0.000",
                "audio_bytes": "9600",
                "playback_mode": "Person Name",
            }
        )
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "free_text"


def test_approximate_sample_zero_anchor_is_not_a_common_clock_gate(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    for index, row in enumerate(
        row for row in rows if row["stage"] == "chunk_sent"
    ):
        row["input_sample_zero_client_ms"] = f"{1000 + index / 10:.3f}"
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 0
    assert report["status"] == "PASS"


@pytest.mark.parametrize(
    ("ratio", "expected_exit"),
    [(0.98, 2), (0.99, 0), (1.01, 0), (1.02, 2)],
)
def test_input_boundary_wall_clock_pacing_gate(
    bundle,
    ratio,
    expected_exit,
):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    chunks = [row for row in rows if row["stage"] == "chunk_sent"]
    first_boundary = int(chunks[0]["input_source_boundary_context_frame"])
    first_received_ms = float(
        chunks[0]["input_source_boundary_received_client_ms"]
    )
    first_timestamp_ms = float(chunks[0]["timestamp_ms"])
    for row in chunks:
        boundary = int(row["input_source_boundary_context_frame"])
        source_elapsed_ms = (
            (boundary - first_boundary) / SAMPLE_RATE * 1000.0
        )
        received_ms = first_received_ms + source_elapsed_ms * ratio
        row["input_source_boundary_received_client_ms"] = (
            f"{received_ms:.3f}"
        )
        row["input_chunk_emitted_client_ms"] = f"{received_ms:.3f}"
        row["timestamp_ms"] = (
            f"{first_timestamp_ms + source_elapsed_ms * ratio:.2f}"
        )
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == expected_exit
    if expected_exit == 0:
        assert report["status"] == "PASS"
        assert report["input_common_clock_rate"][
            "wall_to_source_ratio"
        ] == pytest.approx(ratio)
    else:
        assert report["status"] == "INVALID"
        assert report["errors"][0]["code"] == "input_pacing"


def test_input_boundary_pacing_uses_elapsed_not_absolute_client_time(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    for row in rows:
        if row["stage"] != "chunk_sent":
            continue
        for column in (
            "input_source_boundary_received_client_ms",
            "input_chunk_emitted_client_ms",
        ):
            row[column] = f"{float(row[column]) + 1_000_000:.3f}"
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 0
    assert report["status"] == "PASS"
    assert report["input_common_clock_rate"][
        "observed_boundary_span_ms"
    ] == 59_700.0


def test_input_boundary_pacing_rejects_nonuniform_interior_clock(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    chunks = [row for row in rows if row["stage"] == "chunk_sent"]
    first_boundary = int(chunks[0]["input_source_boundary_context_frame"])
    first_received_ms = float(
        chunks[0]["input_source_boundary_received_client_ms"]
    )
    first_timestamp_ms = float(chunks[0]["timestamp_ms"])
    total_elapsed_ms = 59_700.0
    for row in chunks:
        boundary = int(row["input_source_boundary_context_frame"])
        source_elapsed_ms = (
            (boundary - first_boundary) / SAMPLE_RATE * 1000.0
        )
        distorted_elapsed_ms = (
            source_elapsed_ms
            + 5_000.0 * math.sin(
                math.pi * source_elapsed_ms / total_elapsed_ms
            )
        )
        received_ms = first_received_ms + distorted_elapsed_ms
        row["input_source_boundary_received_client_ms"] = (
            f"{received_ms:.3f}"
        )
        row["input_chunk_emitted_client_ms"] = f"{received_ms:.3f}"
        row["timestamp_ms"] = (
            f"{first_timestamp_ms + distorted_elapsed_ms:.2f}"
        )
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["status"] == "INVALID"
    assert report["errors"][0]["code"] == "input_pacing"


def test_input_ledger_rejects_client_clock_origin_contradiction(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    chunks = [row for row in rows if row["stage"] == "chunk_sent"]
    row = chunks[len(chunks) // 2]
    row["input_chunk_emitted_client_ms"] = (
        f"{float(row['input_chunk_emitted_client_ms']) + 1.0:.3f}"
    )
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["status"] == "INVALID"
    assert report["errors"][0]["code"] == "input_ledger"


@pytest.mark.parametrize(
    "mutation",
    [
        "worklet-delivery-limit",
        "receipt-before-delivery",
        "receipt-bracket-reversed",
        "main-receipt-limit",
        "chunk-emission-limit",
    ],
)
def test_input_context_chain_rejects_reorder_and_excess_lag(bundle, mutation):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    row = next(row for row in rows if row["stage"] == "chunk_sent")
    boundary = int(row["input_source_boundary_context_frame"])
    delivered = int(
        row["input_source_boundary_delivered_after_context_frame"]
    )
    if mutation == "worklet-delivery-limit":
        value = boundary + 128
        row["input_source_boundary_delivered_after_context_frame"] = str(value)
        row["input_source_boundary_received_context_frame_before"] = str(value)
        row["input_source_boundary_received_context_frame_after"] = str(value)
        row["input_chunk_emitted_context_frame"] = str(value)
    elif mutation == "receipt-before-delivery":
        row["input_source_boundary_received_context_frame_before"] = str(
            delivered - 1
        )
    elif mutation == "receipt-bracket-reversed":
        row["input_source_boundary_received_context_frame_before"] = str(
            delivered + 1
        )
        row["input_source_boundary_received_context_frame_after"] = str(
            delivered
        )
    elif mutation == "main-receipt-limit":
        value = boundary + 1601
        row["input_source_boundary_received_context_frame_before"] = str(value)
        row["input_source_boundary_received_context_frame_after"] = str(value)
        row["input_chunk_emitted_context_frame"] = str(value)
    else:
        row["input_chunk_emitted_context_frame"] = str(boundary + 1601)
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "input_ledger"


def test_translated_capture_rejects_nonzero_audio_outside_schedule(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    wav = bytearray(bundle["wav"].read_bytes())
    struct.pack_into("<h", wav, 46, 1)
    bundle["wav"].write_bytes(wav)
    rebind_wav_and_blocks(bundle, manifest)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "translated_capture"


def test_duplicate_protocol_identity_is_invalid(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    receipt = next(row for row in rows if row["stage"] == "audio_received")
    duplicate = dict(receipt)
    duplicate["chunk_index"] = "1"
    terminal_index = next(
        index for index, row in enumerate(rows) if row["stage"] == "server_terminal"
    )
    rows.insert(terminal_index, duplicate)
    manifest["translated_output"]["received_frame_count"] = 2
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "protocol"


def test_parent_completion_must_precede_next_parent_receipt_and_schedule(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    receipt_zero = next(
        row for row in rows if row["stage"] == "audio_received"
    )
    schedule_zero = next(
        row for row in rows if row["stage"] == "playback_chunk_scheduled"
    )
    completion_zero = next(
        row for row in rows if row["stage"] == "audio_parent_complete"
    )
    receipt_one = {
        **receipt_zero,
        "timestamp_ms": "1003.00",
        "chunk_index": "1",
        "parent_sequence_id": "1",
        "audio_frame_id": "0",
        "binary_receipt_client_ms": "2003.000",
    }
    replay = simulate_playback(
        [
            AudioChunk(
                arrival_seconds=7.0,
                duration_seconds=0.5,
                audio_bytes=16_000,
            ),
            AudioChunk(
                arrival_seconds=7.1,
                duration_seconds=0.5,
                audio_bytes=16_000,
            ),
        ],
        input_end_seconds=SOURCE_END / SAMPLE_RATE,
        adaptive=True,
        policy=DEFAULT_PLAYBACK_POLICY,
    )
    second = replay.schedule[1]
    schedule_one = {
        **schedule_zero,
        "timestamp_ms": "1004.00",
        "chunk_index": "1",
        "parent_sequence_id": "1",
        "audio_frame_id": "0",
        "binary_receipt_client_ms": "2003.000",
        "schedule_performance_client_ms": "2004.000",
        "audio_context_time_at_schedule_sec": "7.100000",
        "scheduled_start_context_sec": f"{second.start_seconds:.6f}",
        "scheduled_end_context_sec": f"{second.end_seconds:.6f}",
        "scheduled_start_context_frame_floor": str(
            math.floor(
                second.start_seconds * SAMPLE_RATE + sys.float_info.epsilon
            )
        ),
        "scheduled_end_context_frame_exclusive": str(
            math.ceil(
                second.end_seconds * SAMPLE_RATE - sys.float_info.epsilon
            )
        ),
        "scheduled_duration_sec": (
            f"{second.end_seconds - second.start_seconds:.6f}"
        ),
        "playback_wait_sec": (
            f"{second.wait_before_playback_seconds:.6f}"
        ),
        "queue_depth_sec": f"{second.queue_depth_seconds:.6f}",
        "playback_rate": f"{second.playback_rate:.2f}",
        "playback_mode": second.playback_mode,
        "projected_scheduled_start_client_ms": "2004.000",
    }
    completion_one = {
        **completion_zero,
        "timestamp_ms": "1006.00",
        "parent_sequence_id": "1",
        "parent_complete_received_client_ms": "2006.000",
    }
    completion_index = rows.index(completion_zero)
    rows[completion_index:completion_index] = [receipt_one, schedule_one]
    rows.insert(completion_index + 3, completion_one)
    manifest["translated_output"][
        "last_scheduled_end_context_frame_exclusive"
    ] = int(schedule_one["scheduled_end_context_frame_exclusive"])
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "protocol"
    assert "causally precede" in report["errors"][0]["message"]


def test_complete_unmatched_receive_is_no_loss_fail(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    receipt = next(row for row in rows if row["stage"] == "audio_received")
    second = dict(receipt)
    second["chunk_index"] = "1"
    second["audio_frame_id"] = "1"
    completion_index = next(
        index
        for index, row in enumerate(rows)
        if row["stage"] == "audio_parent_complete"
    )
    rows.insert(completion_index, second)
    completion = next(
        row for row in rows if row["stage"] == "audio_parent_complete"
    )
    completion["audio_frame_count"] = "2"
    completion["audio_bytes"] = str(int(completion["audio_bytes"]) * 2)
    manifest["translated_output"]["received_frame_count"] = 2
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert report["mechanical_gate"]["protocol_pcm_no_loss_verified"] is False
    assert report["mechanical_gate"]["queue_bound_verified"] is True


def test_captured_playback_policy_mismatch_is_invalid(bundle):
    manifest = json.loads(bundle["manifest"].read_text())
    _, rows = parse_rows(bundle["timing"].read_bytes())
    schedule = next(
        row for row in rows if row["stage"] == "playback_chunk_scheduled"
    )
    schedule["playback_rate"] = "1.05"
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 2
    assert report["errors"][0]["code"] == "timing_replay"


@pytest.mark.parametrize(
    ("duration_seconds", "expect_peak_fail"),
    [(9.0, False), (12.0, True)],
)
def test_queue_p95_and_peak_breaches_are_mechanical_fail(
    bundle,
    duration_seconds,
    expect_peak_fail,
):
    manifest = json.loads(bundle["manifest"].read_text())
    rows, translated_end = make_timing_rows(duration_seconds)
    input_hash = manifest["source_reference"]["input_pcm_sha256"]
    for row in rows:
        if row["stage"] == "chunk_sent":
            row["input_pcm_sha256"] = input_hash
    manifest["translated_output"][
        "last_scheduled_end_context_frame_exclusive"
    ] = translated_end
    bind_timing(bundle, manifest, rows)
    write_manifest(bundle["manifest"], manifest)

    exit_code, report = invoke(bundle)

    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert report["mechanical_gate"]["protocol_pcm_no_loss_verified"] is True
    assert report["mechanical_gate"]["queue_bound_verified"] is False
    assert report["queue"]["time_weighted_p95_seconds"] > 5
    assert (report["queue"]["peak_seconds"] > 10) is expect_peak_fail
