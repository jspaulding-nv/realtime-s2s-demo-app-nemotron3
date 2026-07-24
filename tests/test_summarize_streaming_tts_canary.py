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
    parent_summary = {
        "parent_sequence_id": 0,
        "audio_frame_count": 2 if streaming else 1,
        "audio_bytes": 32000,
        "retry_count": 0,
    }
    if streaming:
        events.extend(
            [
                {
                    **events[-1],
                    "stage": "output",
                    "event": "parent_complete_enqueued",
                    "monotonic_ms": complete_ms + 10.0,
                },
                {
                    **events[-1],
                    "stage": "output",
                    "event": "parent_complete_dequeued",
                    "monotonic_ms": complete_ms + 20.0,
                },
            ]
        )
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
                "produced_parent_summaries": [dict(parent_summary)],
                "completed_parent_summaries": [dict(parent_summary)],
                "websocket_completed_parent_summaries": [
                    dict(parent_summary)
                ],
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


def _add_second_parent(input_dir: Path, values: dict[str, dict]) -> None:
    for arm_name, streaming in (("atomic", False), ("streaming", True)):
        summary = copy.deepcopy(values[arm_name]["summary"])
        playback = copy.deepcopy(values[arm_name]["playback"])
        staged = summary["staged_pipeline"]
        first_ms = 3420.0 if streaming else 3500.0
        complete_ms = 3750.0 if streaming else 3800.0
        staged["events"].extend(
            [
                _event(
                    "asr",
                    "final",
                    3000.0,
                    asr_final_id=1,
                    text_chars=9,
                    source_start_ms=900.0,
                    source_end_ms=1600.0,
                ),
                _event(
                    "segmenter",
                    "emitted",
                    3100.0,
                    sequence_id=1,
                    text_chars=9,
                    source_start_ms=900.0,
                    source_end_ms=1600.0,
                ),
                _event(
                    "nmt",
                    "completed",
                    3200.0,
                    sequence_id=1,
                    text_chars=14,
                    source_start_ms=900.0,
                    source_end_ms=1600.0,
                ),
                _event(
                    "tts",
                    "started",
                    3300.0,
                    sequence_id=1,
                    text_chars=14,
                    source_start_ms=900.0,
                    source_end_ms=1600.0,
                ),
                _event(
                    "tts",
                    "first_audio",
                    first_ms,
                    sequence_id=1,
                    source_start_ms=900.0,
                    source_end_ms=1600.0,
                ),
                _event(
                    "tts",
                    "completed",
                    complete_ms,
                    sequence_id=1,
                    source_start_ms=900.0,
                    source_end_ms=1600.0,
                    audio_bytes=32000,
                ),
            ]
        )
        if streaming:
            second_parent = {
                "parent_sequence_id": 1,
                "audio_frame_count": 2,
                "audio_bytes": 32000,
                "retry_count": 0,
            }
            staged["events"].extend(
                [
                    {
                        **staged["events"][-1],
                        "stage": "output",
                        "event": "parent_complete_enqueued",
                        "monotonic_ms": complete_ms + 10.0,
                    },
                    {
                        **staged["events"][-1],
                        "stage": "output",
                        "event": "parent_complete_dequeued",
                        "monotonic_ms": complete_ms + 20.0,
                    },
                ]
            )
            for field in (
                "produced_parent_summaries",
                "completed_parent_summaries",
                "websocket_completed_parent_summaries",
            ):
                staged[field].append(dict(second_parent))
            staged["websocket_send_events"].extend(
                [
                    {
                        "sequence_id": 1,
                        "parent_sequence_id": 1,
                        "audio_frame_id": 0,
                        "sent_monotonic_ms": 3430.0,
                        "audio_bytes": 16000,
                    },
                    {
                        "sequence_id": 1,
                        "parent_sequence_id": 1,
                        "audio_frame_id": 1,
                        "sent_monotonic_ms": 3760.0,
                        "audio_bytes": 16000,
                    },
                ]
            )
            staged["audio_frames_produced"] = 4
        else:
            staged["websocket_send_events"].append(
                {
                    "sequence_id": 1,
                    "sent_monotonic_ms": 3810.0,
                    "audio_bytes": 32000,
                }
            )
        sidecar = staged["tts_response_chunk_telemetry"]
        sidecar["segments_observed"] = 2
        sidecar["response_chunk_count"] = 4
        sidecar["chunks"].extend(
            [
                {
                    "parent_sequence_id": 1,
                    "subsequence_id": 0,
                    "subsequence_count": 1,
                    "response_index": 0,
                    "response_count": 2,
                    "audio_bytes": 16000,
                    "cumulative_audio_bytes": 16000,
                    "since_request_start_ms": first_ms - 3300.0,
                },
                {
                    "parent_sequence_id": 1,
                    "subsequence_id": 0,
                    "subsequence_count": 1,
                    "response_index": 1,
                    "response_count": 2,
                    "audio_bytes": 16000,
                    "cumulative_audio_bytes": 32000,
                    "since_request_start_ms": complete_ms - 3300.0,
                },
            ]
        )
        staged["segments_emitted"] = 2
        staged["completed_sequence_ids"] = [0, 1]
        summary["audio_responses"] = 4 if streaming else 2
        summary["total_received_bytes"] = 64000
        summary["output_duration_sec"] = 2.0
        playback["traces"][0]["translated_audio_seconds"] = 2.0
        for mode in ("fixed_1x", "adaptive"):
            playback["traces"][0][mode]["chunks_scheduled"] = (
                4 if streaming else 2
            )
        arm_dir = input_dir / arm_name
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


def _apply_streaming_fallback(
    input_dir: Path,
    values: dict[str, dict],
    *,
    fallback_parent_ids: tuple[int, ...],
    threshold: int = 4,
    text_chars: dict[int, int] | None = None,
) -> dict:
    summary = copy.deepcopy(values["streaming"]["summary"])
    staged = summary["staged_pipeline"]
    text_chars = text_chars or {0: 3, 1: 10}
    fallback_set = set(fallback_parent_ids)
    summary["backend_config"]["stagedConfig"][
        "ttsIncrementalAtomicFallbackMaxChars"
    ] = threshold
    staged.update(
        {
            "tts_incremental_atomic_fallback_max_chars": threshold,
            "tts_incremental_atomic_fallback_parent_count": len(
                fallback_parent_ids
            ),
            "tts_incremental_atomic_fallback_parent_sequence_ids": list(
                fallback_parent_ids
            ),
        }
    )
    for field in (
        "produced_parent_summaries",
        "completed_parent_summaries",
        "websocket_completed_parent_summaries",
    ):
        for parent in staged[field]:
            parent["atomic_fallback_applied"] = (
                parent["parent_sequence_id"] in fallback_set
            )
    completion_times = {}
    for event in staged["events"]:
        parent_id = event.get("sequence_id")
        event_type = (event["stage"], event["event"])
        if event_type == ("tts", "started"):
            event["text_chars"] = text_chars[parent_id]
        if event_type in {
            ("tts", "completed"),
            ("output", "parent_complete_enqueued"),
            ("output", "parent_complete_dequeued"),
        }:
            event["atomic_fallback_applied"] = parent_id in fallback_set
        if event_type == ("tts", "completed"):
            completion_times[parent_id] = event["monotonic_ms"]
    next_send_times = {0: [2760.0, 2770.0], 1: [3760.0, 3770.0]}
    send_index = {0: 0, 1: 0}
    for event in staged["websocket_send_events"]:
        parent_id = event["parent_sequence_id"]
        if parent_id not in fallback_set:
            continue
        event["sent_monotonic_ms"] = next_send_times[parent_id][
            send_index[parent_id]
        ]
        send_index[parent_id] += 1
    path = input_dir / "streaming" / "shared-prefix_summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    run_info_path = input_dir / "run_info.txt"
    run_info = run_info_path.read_text(encoding="utf-8")
    run_info_path.write_text(
        run_info
        + f"incremental_atomic_fallback_max_chars={threshold}\n",
        encoding="utf-8",
    )
    values["streaming"]["summary"] = summary
    return completion_times


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
    assert result["arms"][1][
        "incremental_atomic_fallback_parent_count"
    ] == 0
    assert result["primary_incremental_evidence"]["available"] is True
    assert result["audio_output_comparability"]["materially_different"] is False
    assert result["cross_arm_playback_conclusion"]["confounded"] is False
    assert result["primary_incremental_evidence"][
        "first_websocket_lead_over_full_response_seconds"
    ]["p95"] == pytest.approx(0.32)

    rendered = json.dumps(result) + render_markdown(result)
    assert "localhost" not in rendered
    assert str(input_dir) not in rendered
    assert "shared-prefix.wav" not in rendered


def test_mixed_fallback_excludes_fallback_from_primary_direct_metrics(
    tmp_path: Path,
) -> None:
    input_dir, values = _write_fixture(tmp_path)
    _add_second_parent(input_dir, values)
    _apply_streaming_fallback(
        input_dir,
        values,
        fallback_parent_ids=(0,),
    )

    result = build_canary_summary(input_dir)
    primary = result["primary_incremental_evidence"]

    assert result["arms"][1][
        "incremental_atomic_fallback_parent_count"
    ] == 1
    assert primary["available"] is True
    assert primary["included_direct_incremental_parent_count"] == 1
    assert primary["excluded_atomic_fallback_parent_count"] == 1
    assert primary[
        "first_response_to_first_websocket_seconds"
    ]["p95"] == pytest.approx(0.01)
    assert primary[
        "first_websocket_lead_over_full_response_seconds"
    ]["p95"] == pytest.approx(0.32)


def test_all_fallback_returns_unavailable_primary_metrics(tmp_path: Path) -> None:
    input_dir, values = _write_fixture(tmp_path)
    _add_second_parent(input_dir, values)
    _apply_streaming_fallback(
        input_dir,
        values,
        fallback_parent_ids=(0, 1),
        threshold=20,
        text_chars={0: 3, 1: 10},
    )

    result = build_canary_summary(input_dir)
    primary = result["primary_incremental_evidence"]

    assert primary["available"] is False
    assert (
        primary["unavailable_reason"]
        == "all_schema_v3_parents_used_atomic_fallback"
    )
    assert primary["included_direct_incremental_parent_count"] == 0
    assert primary["excluded_atomic_fallback_parent_count"] == 2
    assert primary["first_response_to_first_websocket_seconds"] is None
    assert result["comparison"]["first_response_withheld_p95"][
        "reduction"
    ] is None
    assert "benefit is unavailable" in render_markdown(result)


def test_rejects_fallback_threshold_mismatch(tmp_path: Path) -> None:
    input_dir, values = _write_fixture(tmp_path)
    _add_second_parent(input_dir, values)
    _apply_streaming_fallback(
        input_dir,
        values,
        fallback_parent_ids=(0,),
    )
    summary_path = input_dir / "streaming" / "shared-prefix_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["backend_config"]["stagedConfig"][
        "ttsIncrementalAtomicFallbackMaxChars"
    ] = 5
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match="threshold does not match"):
        build_canary_summary(input_dir)


def test_rejects_fallback_text_policy_mismatch(tmp_path: Path) -> None:
    input_dir, values = _write_fixture(tmp_path)
    _add_second_parent(input_dir, values)
    _apply_streaming_fallback(
        input_dir,
        values,
        fallback_parent_ids=(0,),
        text_chars={0: 5, 1: 10},
    )

    with pytest.raises(ValueError, match="configured text threshold"):
        build_canary_summary(input_dir)


def test_rejects_fallback_publish_before_completion(tmp_path: Path) -> None:
    input_dir, values = _write_fixture(tmp_path)
    _add_second_parent(input_dir, values)
    completion_times = _apply_streaming_fallback(
        input_dir,
        values,
        fallback_parent_ids=(0,),
    )
    summary_path = input_dir / "streaming" / "shared-prefix_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    first_fallback_send = next(
        event
        for event in summary["staged_pipeline"]["websocket_send_events"]
        if event["parent_sequence_id"] == 0
    )
    first_fallback_send["sent_monotonic_ms"] = completion_times[0] - 1
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match="published before TTS completion"):
        build_canary_summary(input_dir)


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
    completed = next(
        event
        for event in summary["staged_pipeline"]["events"]
        if (event["stage"], event["event"]) == ("tts", "completed")
    )
    completed["audio_bytes"] = 33600
    for field in (
        "produced_parent_summaries",
        "completed_parent_summaries",
        "websocket_completed_parent_summaries",
    ):
        summary["staged_pipeline"][field][0]["audio_bytes"] = 33600
    for event in summary["staged_pipeline"]["events"]:
        if event["event"].startswith("parent_complete_"):
            event["audio_bytes"] = 33600
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
