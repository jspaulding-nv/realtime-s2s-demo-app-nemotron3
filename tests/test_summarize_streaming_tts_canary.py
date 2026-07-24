from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from summarize_streaming_tts_canary import (
    build_canary_summary,
    render_markdown,
)


DIGESTS = {
    "asr": "sha256:" + "a" * 64,
    "nmt": "sha256:" + "b" * 64,
    "tts": "sha256:" + "c" * 64,
}


def _event(
    stage: str,
    event: str,
    monotonic_ms: float,
    *,
    sequence_id: int | None = None,
    asr_final_id: int | None = None,
    text_chars: int = 0,
    source_start_ms: float | None = None,
    source_end_ms: float | None = None,
    audio_bytes: int = 0,
) -> dict:
    return {
        "stage": stage,
        "event": event,
        "monotonic_ms": monotonic_ms,
        "sequence_id": sequence_id,
        "asr_final_id": asr_final_id,
        "contributing_final_ids": (
            [0] if stage in {"segmenter", "nmt", "tts"} else []
        ),
        "emission_reason": (
            "punctuation" if stage in {"segmenter", "nmt", "tts"} else None
        ),
        "source_start_ms": source_start_ms,
        "source_end_ms": source_end_ms,
        "text_chars": text_chars,
        "audio_bytes": audio_bytes,
    }


def _backend_config(*, streaming: bool) -> dict:
    staged = {
        "telemetrySchemaVersion": 3 if streaming else 1,
        "segmentMaxChars": 240,
        "segmentMaxAgeMs": 2000,
        "asrEventQueueMaxSize": 32,
        "nmtQueueMaxSize": 4,
        "ttsQueueMaxSize": 4,
        "outputQueueMaxSize": 4,
        "nmtRpcTimeoutSeconds": 15.0,
        "ttsRpcTimeoutSeconds": 60.0,
        "ttsMaxSegmentAudioSeconds": 60.0,
        "ttsMaxRetries": 1,
        "ttsResponseChunkTelemetryEnabled": True,
        "ttsSubsegmentMaxChars": 0,
        "ttsSubsegmentMinChars": 12,
        "closeTimeoutSeconds": 10.0,
    }
    if streaming:
        staged.update(
            {
                "ttsIncrementalPublishEnabled": True,
                "ttsIncrementalFrameMs": 100,
            }
        )
    return {
        "pipelineMode": "staged",
        "sampleRate": 16000,
        "chunkSize": 4800,
        "channels": 1,
        "modelConfig": {
            service: {
                "image": f"nvcr.io/nim/nvidia/{service}:1.2.3",
                "imageDigest": DIGESTS[service],
                "endpoint": f"localhost:{50051 + index}",
            }
            for index, service in enumerate(("asr", "nmt", "tts"))
        },
        "stagedConfig": staged,
    }


def _playback(*, streaming: bool) -> dict:
    chunks = 2 if streaming else 1
    benefit = 1.0 if streaming else 0.0

    def mode(*, adaptive: bool) -> dict:
        return {
            "chunks_scheduled": chunks,
            "chunks_dropped": 0,
            "listener_tail_seconds": (
                2.0 - benefit - (0.25 if adaptive else 0.0)
            ),
            "time_weighted_queue_p50_seconds": 1.0 - benefit * 0.25,
            "time_weighted_queue_p95_seconds": 2.0 - benefit * 0.5,
            "peak_queue_depth_seconds": 2.5 - benefit * 0.5,
            "percent_playback_window_above_limit": 0.0,
            "urgent_source_percent": 0.0,
        }

    return {
        "schema_version": 1,
        "policy": {
            "target_queue_seconds": 5.0,
            "urgent_queue_seconds": 8.0,
            "limit_queue_seconds": 10.0,
            "catch_up_release_seconds": 4.0,
            "urgent_release_seconds": 7.0,
            "normal_rate": 1.0,
            "catch_up_rate": 1.05,
            "urgent_rate": 1.1,
        },
        "traces": [
            {
                "trace_csv": "shared-prefix_results.csv",
                "translated_audio_seconds": 1.0,
                "fixed_1x": mode(adaptive=False),
                "adaptive": mode(adaptive=True),
            }
        ],
    }


def _summary(prefix: Path, *, streaming: bool) -> dict:
    first_ms = 2420.0 if streaming else 2500.0
    complete_ms = 2750.0 if streaming else 2800.0
    websocket_events = (
        [
            {
                "sequence_id": 0,
                "parent_sequence_id": 0,
                "audio_frame_id": 0,
                "sent_monotonic_ms": 2430.0,
                "audio_bytes": 16000,
            },
            {
                "sequence_id": 0,
                "parent_sequence_id": 0,
                "audio_frame_id": 1,
                "sent_monotonic_ms": 2760.0,
                "audio_bytes": 16000,
            },
        ]
        if streaming
        else [
            {
                "sequence_id": 0,
                "sent_monotonic_ms": 2810.0,
                "audio_bytes": 32000,
            }
        ]
    )
    events = [
        _event("pipeline", "started", 1000.0),
        _event(
            "asr",
            "final",
            2000.0,
            asr_final_id=0,
            text_chars=10,
            source_start_ms=0.0,
            source_end_ms=800.0,
        ),
        _event(
            "segmenter",
            "emitted",
            2100.0,
            sequence_id=0,
            text_chars=10,
            source_start_ms=0.0,
            source_end_ms=800.0,
        ),
        _event(
            "nmt",
            "completed",
            2200.0,
            sequence_id=0,
            text_chars=12,
            source_start_ms=0.0,
            source_end_ms=800.0,
        ),
        _event(
            "tts",
            "started",
            2300.0,
            sequence_id=0,
            source_start_ms=0.0,
            source_end_ms=800.0,
        ),
        _event(
            "tts",
            "first_audio",
            first_ms,
            sequence_id=0,
            source_start_ms=0.0,
            source_end_ms=800.0,
        ),
        _event(
            "tts",
            "completed",
            complete_ms,
            sequence_id=0,
            source_start_ms=0.0,
            source_end_ms=800.0,
            audio_bytes=32000,
        ),
    ]
    staged = {
        "telemetry_schema_version": 3 if streaming else 1,
        "tts_subsegmentation_enabled": False,
        "tts_response_chunk_telemetry_enabled": True,
        "state": "closed",
        "outcome": "complete",
        "failure": None,
        "cleanup_errors": [],
        "segments_emitted": 1,
        "completed_sequence_ids": [0],
        "incomplete_sequence_ids": [],
        "events": events,
        "websocket_send_events": websocket_events,
        "tts_response_chunk_telemetry": {
            "schema_version": 1,
            "segments_observed": 1,
            "response_chunk_count": 2,
            "chunks": [
                {
                    "parent_sequence_id": 0,
                    "subsequence_id": 0,
                    "subsequence_count": 1,
                    "response_index": 0,
                    "response_count": 2,
                    "audio_bytes": 16000,
                    "cumulative_audio_bytes": 16000,
                    "since_request_start_ms": first_ms - 2300.0,
                },
                {
                    "parent_sequence_id": 0,
                    "subsequence_id": 0,
                    "subsequence_count": 1,
                    "response_index": 1,
                    "response_count": 2,
                    "audio_bytes": 16000,
                    "cumulative_audio_bytes": 32000,
                    "since_request_start_ms": complete_ms - 2300.0,
                },
            ],
        },
    }
    if streaming:
        staged.update(
            {
                "tts_incremental_publish_enabled": True,
                "tts_incremental_frame_ms": 100,
                "audio_frames_produced": 2,
            }
        )
    return {
        "audio_path": str(prefix.resolve()),
        "backend_config": _backend_config(streaming=streaming),
        "pipeline_mode": "staged",
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "staged_pipeline": staged,
        "input_duration_sec": 10.0,
        "chunks_sent": 34,
        "audio_responses": 2 if streaming else 1,
        "total_received_bytes": 32000,
        "output_duration_sec": 1.0,
        "first_audio_latency_sec": 1.43 if streaming else 1.81,
        "tail_lag_sec": 0.25 if streaming else 0.50,
        "playback_tail_sec": 1.0 if streaming else 2.0,
        "input_completed": True,
        "connection_lost": False,
        "drain_timed_out": False,
        "translation_completed": True,
        "server_error": "",
    }


def _write_fixture(tmp_path: Path) -> tuple[Path, dict[str, dict]]:
    prefix = tmp_path / "shared-prefix.wav"
    prefix.write_bytes(b"shared synthetic prefix")
    digest = hashlib.sha256(prefix.read_bytes()).hexdigest()
    (tmp_path / "run_info.txt").write_text(
        f"prefix_sha256={digest}\n",
        encoding="utf-8",
    )
    values = {}
    for arm_name, streaming in (("atomic", False), ("streaming", True)):
        arm_dir = tmp_path / arm_name
        arm_dir.mkdir()
        summary = _summary(prefix, streaming=streaming)
        playback = _playback(streaming=streaming)
        (arm_dir / "shared-prefix_summary.json").write_text(
            json.dumps(summary),
            encoding="utf-8",
        )
        (arm_dir / "playback_policy_analysis.json").write_text(
            json.dumps(playback),
            encoding="utf-8",
        )
        values[arm_name] = {
            "summary": summary,
            "playback": playback,
        }
    return tmp_path, values


def test_builds_privacy_safe_matched_comparison(tmp_path: Path) -> None:
    input_dir, _ = _write_fixture(tmp_path)

    result = build_canary_summary(input_dir)

    assert result["matched_design"]["passed"] is True
    assert result["privacy"] == {
        "contains_transcript_text": False,
        "contains_audio": False,
        "contains_input_paths_or_filenames": False,
        "contains_endpoints": False,
        "contains_session_ids": False,
    }
    assert result["comparison"]["first_websocket_publication_p95"][
        "reduction"
    ] == pytest.approx(0.38)
    assert result["comparison"]["first_response_withheld_p95"][
        "reduction"
    ] == pytest.approx(0.30)
    assert result["comparison"]["output_audio_bytes"][
        "streaming_minus_atomic"
    ] == 0
    assert result["arms"][1]["audio_messages"] == 2
    assert result["audio_output_comparability"]["materially_different"] is False
    assert result["cross_arm_playback_conclusion"]["confounded"] is False
    assert result["primary_incremental_evidence"][
        "first_websocket_lead_over_full_response_seconds"
    ]["p95"] == pytest.approx(0.32)

    rendered = json.dumps(result) + render_markdown(result)
    assert "localhost" not in rendered
    assert str(input_dir) not in rendered
    assert "shared-prefix.wav" not in rendered


def test_rejects_mismatched_asr_structure(tmp_path: Path) -> None:
    input_dir, values = _write_fixture(tmp_path)
    summary = copy.deepcopy(values["streaming"]["summary"])
    summary["staged_pipeline"]["events"][1]["text_chars"] = 11
    (input_dir / "streaming" / "shared-prefix_summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="matched upstream evidence differs"):
        build_canary_summary(input_dir)


def test_rejects_inconsistent_incremental_api_flag(tmp_path: Path) -> None:
    input_dir, values = _write_fixture(tmp_path)
    summary = copy.deepcopy(values["streaming"]["summary"])
    del summary["backend_config"]["stagedConfig"][
        "ttsIncrementalPublishEnabled"
    ]
    (input_dir / "streaming" / "shared-prefix_summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="incremental-publication flag"):
        build_canary_summary(input_dir)


def test_rejects_changed_shared_prefix_after_capture(tmp_path: Path) -> None:
    input_dir, _ = _write_fixture(tmp_path)
    (input_dir / "shared-prefix.wav").write_bytes(b"changed")

    with pytest.raises(ValueError, match="digest does not match"):
        build_canary_summary(input_dir)


def test_material_audio_difference_marks_playback_inconclusive(
    tmp_path: Path,
) -> None:
    input_dir, values = _write_fixture(tmp_path)
    summary = copy.deepcopy(values["streaming"]["summary"])
    playback = copy.deepcopy(values["streaming"]["playback"])
    summary["total_received_bytes"] = 33600
    summary["output_duration_sec"] = 1.05
    completed = summary["staged_pipeline"]["events"][-1]
    completed["audio_bytes"] = 33600
    chunks = summary["staged_pipeline"]["tts_response_chunk_telemetry"][
        "chunks"
    ]
    chunks[0]["audio_bytes"] = 16800
    chunks[0]["cumulative_audio_bytes"] = 16800
    chunks[1]["audio_bytes"] = 16800
    chunks[1]["cumulative_audio_bytes"] = 33600
    for event in summary["staged_pipeline"]["websocket_send_events"]:
        event["audio_bytes"] = 16800
    playback["traces"][0]["translated_audio_seconds"] = 1.05
    (input_dir / "streaming" / "shared-prefix_summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )
    (
        input_dir / "streaming" / "playback_policy_analysis.json"
    ).write_text(json.dumps(playback), encoding="utf-8")

    result = build_canary_summary(input_dir)

    assert result["audio_output_comparability"]["materially_different"] is True
    conclusion = result["cross_arm_playback_conclusion"]
    assert conclusion["confounded"] is True
    assert conclusion["status"].startswith("inconclusive_")
    assert result["primary_incremental_evidence"][
        "classification"
    ] == "within_arm_direct_measurement"
