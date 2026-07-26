import asyncio
import csv
import json

import pytest

from batch_latency_test import (
    INPUT_PACING_DEADLINE_BASIS,
    INPUT_PACING_MODE,
    INPUT_SAMPLE_ZERO_CLOCK,
    TestResult as BatchTestResult,
    TimingEvent,
    _canonical_capture_time,
    _relative_pacing_margin_ms,
    _send_pcm_chunks_at_end_boundaries,
    compute_playback_metrics,
    fetch_backend_export,
    fetch_backend_config,
    generate_csv,
    generate_summary,
    resolve_pipeline_mode,
    run_batch,
    run_test,
    validate_input_pacing_evidence,
    validate_staged_pipeline_integrity,
)


def test_relative_pacing_margin_matches_persisted_large_clock_coordinates():
    """Large monotonic origins must not change arithmetic during replay."""

    client_clock_origin = 9_876_543.0
    sample_zero = client_clock_origin + 0.123456789
    send_timestamp = sample_zero + 0.3
    sample_zero_timestamp_ms = (
        sample_zero - client_clock_origin
    ) * 1000
    persisted_ledger_margin_ms = (
        (send_timestamp - client_clock_origin) * 1000
        - sample_zero_timestamp_ms
        - 300.0
    )

    observed = _relative_pacing_margin_ms(
        send_timestamp=send_timestamp,
        client_clock_origin=client_clock_origin,
        sample_zero_timestamp_ms=sample_zero_timestamp_ms,
        chunk_index=0,
        chunk_duration_ms=300.0,
    )

    assert observed == persisted_ledger_margin_ms
    assert observed == 7.450580596923828e-07


def test_pacing_validator_allows_large_clock_numeric_early_noise():
    """An on-deadline send can round a few nanoseconds early."""

    client_clock_origin = 84_597_763.30097976
    sample_zero = client_clock_origin + 0.757954403758049
    send_timestamp = sample_zero + 0.3
    sample_zero_timestamp_ms = (
        sample_zero - client_clock_origin
    ) * 1000
    margin_ms = _relative_pacing_margin_ms(
        send_timestamp=send_timestamp,
        client_clock_origin=client_clock_origin,
        sample_zero_timestamp_ms=sample_zero_timestamp_ms,
        chunk_index=0,
        chunk_duration_ms=300.0,
    )
    result = BatchTestResult(
        audio_path="test.wav",
        duration_sec=0.3,
        chunks_sent=1,
        input_sample_zero_timestamp_ms=sample_zero_timestamp_ms,
        input_pacing={
            "mode": INPUT_PACING_MODE,
            "chunk_duration_ms": 300.0,
            "source_sample_zero_clock": INPUT_SAMPLE_ZERO_CLOCK,
            "deadline_basis": INPUT_PACING_DEADLINE_BASIS,
            "source_sample_zero_timestamp_ms": sample_zero_timestamp_ms,
            "observed_chunk_count": 1,
            "min_emission_minus_deadline_ms": margin_ms,
            "max_emission_minus_deadline_ms": margin_ms,
        },
        client_events=[
            TimingEvent(
                "client",
                "chunk_sent",
                (send_timestamp - client_clock_origin) * 1000,
                0,
                0.0,
                9_600,
            )
        ],
    )

    assert -1e-4 <= margin_ms < -1e-6
    assert validate_input_pacing_evidence(
        result,
        require_chunk_events=True,
        timing_tolerance_ms=1e-9,
        numeric_early_tolerance_ms=1e-4,
    ) == []


def test_canonical_capture_time_uses_persisted_scheduler_coordinate():
    """Live and replay scheduling must use the same floating-point value."""

    raw_arrival_seconds = 4_036.609045744068
    timestamp_ms, scheduler_seconds = _canonical_capture_time(
        raw_arrival_seconds,
        0.0,
    )

    assert scheduler_seconds == timestamp_ms / 1000.0
    assert scheduler_seconds != raw_arrival_seconds


def test_generate_csv_preserves_playback_boundary_timestamp(tmp_path):
    """Sub-hundredth-ms arrivals must survive artifact round trips exactly."""

    boundary_timestamp_ms = 0.0049
    result = BatchTestResult(
        audio_path="test.wav",
        duration_sec=5.0,
        client_events=[
            TimingEvent(
                "client",
                "audio_received",
                boundary_timestamp_ms,
                1,
                5.0,
                3_200,
                protocol_version=1,
                stream_generation=1,
                parent_sequence_id=0,
                audio_frame_id=1,
                source_start_ms=0.0,
                source_end_ms=5_000.0,
                source_end_to_receipt_ms=-4_999.9951,
            )
        ],
    )
    csv_path = tmp_path / "results.csv"

    generate_csv(result, str(csv_path))

    with csv_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert float(row["timestamp_ms"]) == boundary_timestamp_ms


def staged_config():
    return {
        "pipelineMode": "staged",
        "stagedConfig": {
            "segmentMaxChars": 240,
            "segmentMaxAgeMs": 2000,
            "asrEventQueueMaxSize": 32,
            "nmtQueueMaxSize": 4,
            "ttsQueueMaxSize": 4,
            "outputQueueMaxSize": 4,
            "nmtRpcTimeoutSeconds": 15,
            "ttsRpcTimeoutSeconds": 60,
            "ttsMaxSegmentAudioSeconds": 60,
            "ttsMaxRetries": 1,
            "closeTimeoutSeconds": 10,
        },
    }


def successful_staged_export():
    completed = [0, 1]
    events = []
    for sequence_id in completed:
        events.extend(
            [
                {
                    "stage": "segmenter",
                    "event": "emitted",
                    "sequence_id": sequence_id,
                },
                {
                    "stage": "nmt",
                    "event": "completed",
                    "sequence_id": sequence_id,
                    "retry_count": 0,
                },
                {
                    "stage": "tts",
                    "event": "completed",
                    "sequence_id": sequence_id,
                    "retry_count": 0,
                },
                {
                    "stage": "output",
                    "event": "dequeued",
                    "sequence_id": sequence_id,
                    "queue_depth": 0,
                    "queue_capacity": 4,
                },
            ]
        )
    return {
        "session_id": "session-1",
        "state": "closed",
        "outcome": "complete",
        "segments_emitted": 2,
        "audio_segments_produced": 2,
        "nmt_retry_count": 0,
        "tts_retry_count": 0,
        "completed_sequence_ids": completed,
        "incomplete_sequence_ids": [],
        "failure": None,
        "cleanup_errors": [],
        "max_queue_depths": {"nmt": 2, "tts": 1, "output": 2},
        "blocked_put_counts": {"nmt": 0, "tts": 0, "output": 0},
        "events": events,
        "websocket_sent_sequence_ids": completed,
        "websocket_send_events": [
            {
                "sequence_id": sequence_id,
                "sent_monotonic_ms": 1000.0 + sequence_id,
                "audio_bytes": 3200,
            }
            for sequence_id in completed
        ],
    }


def successful_websocket_receive_events():
    return [
        {
            "order": 0,
            "timestamp_ms": 1.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "connected",
            "audio_bytes": 0,
        },
        {
            "order": 1,
            "timestamp_ms": 2.0,
            "frame_type": "pcm",
            "audio_bytes": 3200,
        },
        {
            "order": 2,
            "timestamp_ms": 2.5,
            "frame_type": "pcm",
            "audio_bytes": 3200,
        },
        {
            "order": 3,
            "timestamp_ms": 3.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "completed",
            "audio_bytes": 0,
        },
    ]


def staged_config_v2(max_chars=40):
    config = staged_config()
    config["stagedConfig"].update(
        {
            "telemetrySchemaVersion": 2,
            "ttsSubsegmentMaxChars": max_chars,
            "ttsSubsegmentMinChars": 12,
        }
    )
    return config


def successful_staged_export_v2():
    parent_specs = {
        0: {
            "parent_text_chars": 39,
            "children": [
                (0, 2, 20, 3200),
                (1, 2, 18, 4800),
            ],
        },
        1: {
            "parent_text_chars": 30,
            "children": [(0, 1, 30, 4000)],
        },
    }
    events = []
    keys = []
    audio_bytes = []
    for parent_sequence_id, spec in parent_specs.items():
        events.extend(
            [
                {
                    "stage": "segmenter",
                    "event": "emitted",
                    "sequence_id": parent_sequence_id,
                },
                {
                    "stage": "nmt",
                    "event": "completed",
                    "sequence_id": parent_sequence_id,
                    "text_chars": spec["parent_text_chars"],
                    "retry_count": 0,
                },
            ]
        )
        for subsequence_id, subsequence_count, text_chars, pcm_bytes in spec[
            "children"
        ]:
            identity = {
                "sequence_id": parent_sequence_id,
                "parent_sequence_id": parent_sequence_id,
                "subsequence_id": subsequence_id,
                "subsequence_count": subsequence_count,
            }
            key = {
                name: identity[name]
                for name in (
                    "parent_sequence_id",
                    "subsequence_id",
                    "subsequence_count",
                )
            }
            keys.append(key)
            audio_bytes.append(pcm_bytes)
            events.extend(
                [
                    {
                        **identity,
                        "stage": "target_splitter",
                        "event": "emitted",
                        "text_chars": text_chars,
                        "parent_text_chars": spec["parent_text_chars"],
                    },
                    {
                        **identity,
                        "stage": "tts",
                        "event": "enqueued",
                        "text_chars": text_chars,
                    },
                    {
                        **identity,
                        "stage": "tts",
                        "event": "started",
                        "text_chars": text_chars,
                        "parent_text_chars": spec["parent_text_chars"],
                    },
                    {
                        **identity,
                        "stage": "tts",
                        "event": "first_audio",
                    },
                    {
                        **identity,
                        "stage": "tts",
                        "event": "completed",
                        "audio_bytes": pcm_bytes,
                        "retry_count": 0,
                    },
                    {
                        **identity,
                        "stage": "output",
                        "event": "enqueued",
                        "audio_bytes": pcm_bytes,
                    },
                    {
                        **identity,
                        "stage": "output",
                        "event": "dequeued",
                        "audio_bytes": pcm_bytes,
                    },
                ]
            )

    # StagedPipelineSession.next_output() uses output/dequeued for both AUDIO
    # children and the sole identity-free COMPLETE terminal.
    events.append(
        {
            "stage": "output",
            "event": "dequeued",
            "sequence_id": None,
            "subsequence_id": None,
            "subsequence_count": None,
            "queue_depth": 0,
            "queue_capacity": 5,
            "text_chars": 0,
            "audio_bytes": 0,
            "audio_duration_ms": 0.0,
        }
    )

    completed = list(parent_specs)
    return {
        "telemetry_schema_version": 2,
        "tts_subsegmentation_enabled": True,
        "tts_subsegment_max_chars": 40,
        "tts_subsegment_min_chars": 12,
        "session_id": "session-v2",
        "state": "closed",
        "outcome": "complete",
        "segments_emitted": len(completed),
        "tts_subsegments_planned": len(keys),
        "tts_subsegments_produced": len(keys),
        "audio_segments_produced": len(keys),
        "nmt_retry_count": 0,
        "tts_retry_count": 0,
        "completed_sequence_ids": completed,
        "incomplete_sequence_ids": [],
        "planned_subsegment_keys": [dict(key) for key in keys],
        "synthesized_subsegment_keys": [dict(key) for key in keys],
        "completed_subsegment_keys": [dict(key) for key in keys],
        "incomplete_subsegment_keys": [],
        "failure": None,
        "cleanup_errors": [],
        "max_queue_depths": {"nmt": 2, "tts": 4, "output": 2},
        "blocked_put_counts": {"nmt": 0, "tts": 0, "output": 0},
        "events": events,
        "websocket_sent_sequence_ids": completed,
        "websocket_sent_subsegment_keys": [dict(key) for key in keys],
        "websocket_send_events": [
            {
                "sequence_id": key["parent_sequence_id"],
                **key,
                "sent_monotonic_ms": 1000.0 + index,
                "audio_bytes": pcm_bytes,
            }
            for index, (key, pcm_bytes) in enumerate(zip(keys, audio_bytes))
        ],
    }


def successful_websocket_receive_events_v2():
    return [
        {
            "order": 0,
            "timestamp_ms": 1.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "connected",
            "audio_bytes": 0,
        },
        *[
            {
                "order": index,
                "timestamp_ms": 1.0 + index,
                "frame_type": "pcm",
                "audio_bytes": audio_bytes,
            }
            for index, audio_bytes in enumerate((3200, 4800, 4000), start=1)
        ],
        {
            "order": 4,
            "timestamp_ms": 5.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "completed",
            "audio_bytes": 0,
        },
    ]


def staged_config_v3():
    config = staged_config()
    config["stagedConfig"].update(
        {
            "telemetrySchemaVersion": 3,
            "ttsIncrementalPublishEnabled": True,
            "ttsIncrementalFrameMs": 100,
            "ttsSubsegmentMaxChars": 0,
        }
    )
    return config


def successful_staged_export_v3():
    parent_specs = {
        0: {
            "retry_count": 0,
            "frame_bytes": [3200, 1600],
        },
        1: {
            "retry_count": 1,
            "frame_bytes": [2400],
        },
    }
    events = []
    frame_keys = []
    frame_bytes = []
    parent_summaries = []
    websocket_send_events = []
    for parent_sequence_id, spec in parent_specs.items():
        retry_count = spec["retry_count"]
        events.extend(
            [
                {
                    "stage": "segmenter",
                    "event": "emitted",
                    "sequence_id": parent_sequence_id,
                },
                {
                    "stage": "nmt",
                    "event": "completed",
                    "sequence_id": parent_sequence_id,
                    "retry_count": 0,
                },
            ]
        )
        for audio_frame_id, pcm_bytes in enumerate(spec["frame_bytes"]):
            identity = {
                "sequence_id": parent_sequence_id,
                "parent_sequence_id": parent_sequence_id,
                "audio_frame_id": audio_frame_id,
            }
            key = {
                "parent_sequence_id": parent_sequence_id,
                "audio_frame_id": audio_frame_id,
            }
            frame_keys.append(key)
            frame_bytes.append(pcm_bytes)
            events.extend(
                [
                    {
                        **identity,
                        "stage": "tts",
                        "event": "frame_received",
                        "audio_bytes": pcm_bytes,
                        "retry_count": retry_count,
                    },
                    {
                        **identity,
                        "stage": "output",
                        "event": "frame_enqueued",
                        "audio_bytes": pcm_bytes,
                        "queue_depth": 1,
                        "queue_capacity": 4,
                    },
                    {
                        **identity,
                        "stage": "output",
                        "event": "frame_dequeued",
                        "audio_bytes": pcm_bytes,
                        "queue_depth": 0,
                        "queue_capacity": 4,
                    },
                ]
            )
            websocket_send_events.append(
                {
                    **identity,
                    "sent_monotonic_ms": 1000.0 + len(frame_keys),
                    "audio_bytes": pcm_bytes,
                }
            )

        summary = {
            "parent_sequence_id": parent_sequence_id,
            "audio_frame_count": len(spec["frame_bytes"]),
            "audio_bytes": sum(spec["frame_bytes"]),
            "retry_count": retry_count,
        }
        parent_summaries.append(summary)
        events.extend(
            [
                {
                    "stage": "tts",
                    "event": "completed",
                    "sequence_id": parent_sequence_id,
                    **summary,
                },
                {
                    "stage": "output",
                    "event": "parent_complete_enqueued",
                    "sequence_id": parent_sequence_id,
                    **summary,
                    "queue_depth": 1,
                    "queue_capacity": 4,
                },
                {
                    "stage": "output",
                    "event": "parent_complete_dequeued",
                    "sequence_id": parent_sequence_id,
                    **summary,
                    "queue_depth": 0,
                    "queue_capacity": 4,
                },
            ]
        )

    completed = list(parent_specs)
    return {
        "telemetry_schema_version": 3,
        "tts_incremental_publish_enabled": True,
        "tts_incremental_frame_ms": 100,
        "tts_incremental_frame_bytes": 3200,
        "tts_subsegmentation_enabled": False,
        "session_id": "session-v3",
        "state": "closed",
        "outcome": "complete",
        "segments_emitted": len(completed),
        "audio_segments_produced": len(completed),
        "audio_frames_produced": len(frame_keys),
        "nmt_retry_count": 0,
        "tts_retry_count": sum(
            spec["retry_count"] for spec in parent_specs.values()
        ),
        "completed_sequence_ids": completed,
        "incomplete_sequence_ids": [],
        "published_audio_frame_keys": [dict(key) for key in frame_keys],
        "dequeued_audio_frame_keys": [dict(key) for key in frame_keys],
        "websocket_sent_audio_frame_keys": [
            dict(key) for key in frame_keys
        ],
        "published_audio_frame_bytes": list(frame_bytes),
        "dequeued_audio_frame_bytes": list(frame_bytes),
        "websocket_sent_audio_frame_bytes": list(frame_bytes),
        "produced_parent_summaries": [
            dict(summary) for summary in parent_summaries
        ],
        "completed_parent_summaries": [
            dict(summary) for summary in parent_summaries
        ],
        "websocket_completed_parent_summaries": [
            dict(summary) for summary in parent_summaries
        ],
        "failure": None,
        "cleanup_errors": [],
        "max_queue_depths": {"nmt": 2, "tts": 1, "output": 1},
        "blocked_put_counts": {"nmt": 0, "tts": 0, "output": 0},
        "events": events,
        "websocket_sent_sequence_ids": completed,
        "websocket_send_events": websocket_send_events,
    }


def enable_publisher_handoff_v3(export, config):
    config["stagedConfig"].update(
        {
            "ttsPublisherHandoffTelemetryEnabled": True,
            "ttsResponseChunkTelemetryEnabled": True,
        }
    )
    export.update(
        {
            "tts_publisher_handoff_telemetry_enabled": True,
            "tts_response_chunk_telemetry_enabled": True,
        }
    )
    canonical_keys = [
        (item["parent_sequence_id"], item["audio_frame_id"])
        for item in export["published_audio_frame_keys"]
    ]
    retry_by_parent = {
        item["parent_sequence_id"]: item["retry_count"]
        for item in export["produced_parent_summaries"]
    }
    fallback_by_parent = {
        item["parent_sequence_id"]: item.get(
            "atomic_fallback_applied",
            False,
        )
        for item in export["produced_parent_summaries"]
    }
    direct_ready_by_key = {
        key: 1_000.0 + index * 100.0
        for index, key in enumerate(canonical_keys)
    }
    ready_by_key = dict(direct_ready_by_key)
    for parent, fallback_applied in fallback_by_parent.items():
        if not fallback_applied:
            continue
        parent_ready = min(
            direct_ready_by_key[key]
            for key in canonical_keys
            if key[0] == parent
        )
        for key in canonical_keys:
            if key[0] == parent:
                ready_by_key[key] = parent_ready

    for event in export["events"]:
        event_name = (event.get("stage"), event.get("event"))
        if event_name not in {
            ("tts", "frame_received"),
            ("output", "frame_enqueued"),
            ("output", "frame_dequeued"),
        }:
            continue
        key = (event["parent_sequence_id"], event["audio_frame_id"])
        ready_ms = ready_by_key[key]
        request_offset = key[1] * 10.0
        requested_ms = ready_ms + 1.0 + request_offset
        event["retry_count"] = retry_by_parent[key[0]]
        event.update(
            {
                "session_id": export["session_id"],
                "error_code": "",
                "emission_reason": "punctuation",
                "asr_final_id": None,
                "contributing_final_ids": [key[0]],
                "source_start_ms": key[0] * 1_000.0,
                "source_end_ms": key[0] * 1_000.0 + 500.0,
            }
        )
        if event_name == ("tts", "frame_received"):
            event["monotonic_ms"] = ready_ms
        elif event_name == ("output", "frame_enqueued"):
            event.update(
                {
                    "publish_requested_monotonic_ms": requested_ms,
                    "event_loop_callback_started_monotonic_ms": (
                        requested_ms + 1.0
                    ),
                    "output_capacity_acquired_monotonic_ms": (
                        requested_ms + 3.0
                    ),
                    "monotonic_ms": requested_ms + 4.0,
                    "blocked_put_ms": 0.0,
                }
            )
        else:
            event["monotonic_ms"] = requested_ms + 5.0
    for event in export["websocket_send_events"]:
        key = (event["parent_sequence_id"], event["audio_frame_id"])
        requested_ms = ready_by_key[key] + 1.0 + key[1] * 10.0
        event["send_started_monotonic_ms"] = requested_ms + 6.0
        event["sent_monotonic_ms"] = requested_ms + 7.0

    chunks = []
    bytes_per_ms = (
        export["tts_incremental_frame_bytes"]
        / config["stagedConfig"]["ttsIncrementalFrameMs"]
    )
    response_times_by_parent = {}
    for parent in sorted(retry_by_parent):
        parent_frames = [
            (key, audio_bytes)
            for key, audio_bytes in zip(
                canonical_keys,
                export["published_audio_frame_bytes"],
            )
            if key[0] == parent
        ]
        if fallback_by_parent[parent]:
            completion_ms = ready_by_key[parent_frames[0][0]]
            response_times = [
                completion_ms - (len(parent_frames) - index) * 10.0
                for index in range(len(parent_frames))
            ]
        else:
            response_times = [
                ready_by_key[key] for key, _audio_bytes in parent_frames
            ]
        response_times_by_parent[parent] = response_times
        request_started_ms = response_times[0] - 100.0
        cumulative_bytes = 0
        cumulative_duration_ms = 0.0
        previous_received_ms = request_started_ms
        for response_index, (
            (_key, audio_bytes),
            received_ms,
        ) in enumerate(zip(parent_frames, response_times)):
            audio_duration_ms = audio_bytes / bytes_per_ms
            cumulative_bytes += audio_bytes
            cumulative_duration_ms += audio_duration_ms
            chunks.append(
                {
                    "parent_sequence_id": parent,
                    "subsequence_id": 0,
                    "subsequence_count": 1,
                    "response_index": response_index,
                    "response_count": len(parent_frames),
                    "audio_bytes": audio_bytes,
                    "cumulative_audio_bytes": cumulative_bytes,
                    "audio_duration_ms": audio_duration_ms,
                    "cumulative_audio_duration_ms": cumulative_duration_ms,
                    "received_monotonic_ms": received_ms,
                    "since_request_start_ms": (
                        received_ms - request_started_ms
                    ),
                    "since_previous_response_ms": (
                        received_ms - previous_received_ms
                    ),
                    "retry_count": retry_by_parent[parent],
                }
            )
            previous_received_ms = received_ms

    lifecycle_events = []
    for parent in sorted(retry_by_parent):
        response_times = response_times_by_parent[parent]
        completion_ms = (
            ready_by_key[
                next(key for key in canonical_keys if key[0] == parent)
            ]
            if fallback_by_parent[parent]
            else response_times[-1] + 50.0
        )
        lifecycle_events.append(
            {
                "session_id": export["session_id"],
                "stage": "tts",
                "event": "first_audio",
                "sequence_id": parent,
                "monotonic_ms": response_times[0],
            }
        )
        completed = next(
            event
            for event in export["events"]
            if (event.get("stage"), event.get("event"))
            == ("tts", "completed")
            and event.get("sequence_id") == parent
        )
        completed["monotonic_ms"] = completion_ms
    export["events"].extend(lifecycle_events)
    export["tts_response_chunk_telemetry"] = {
        "schema_version": 1,
        "segments_observed": len(retry_by_parent),
        "response_chunk_count": len(chunks),
        "chunks": chunks,
    }
    return export, config


def enable_atomic_fallback_v3(
    export,
    config,
    *,
    threshold=4,
    fallback_parent_ids=(1,),
    text_chars=None,
):
    text_chars = text_chars or {0: 10, 1: 3}
    fallback_parent_ids = list(fallback_parent_ids)
    config["stagedConfig"][
        "ttsIncrementalAtomicFallbackMaxChars"
    ] = threshold
    export.update(
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
    fallback_set = set(fallback_parent_ids)
    for field in (
        "produced_parent_summaries",
        "completed_parent_summaries",
        "websocket_completed_parent_summaries",
    ):
        for summary in export[field]:
            summary["atomic_fallback_applied"] = (
                summary["parent_sequence_id"] in fallback_set
            )
    completion_times = {0: 1000.0, 1: 1002.0}
    for event in export["events"]:
        parent = event.get("sequence_id")
        event_type = (event.get("stage"), event.get("event"))
        if event_type in {
            ("tts", "completed"),
            ("output", "parent_complete_enqueued"),
            ("output", "parent_complete_dequeued"),
        }:
            event["atomic_fallback_applied"] = parent in fallback_set
        if event_type == ("tts", "completed"):
            event["monotonic_ms"] = completion_times[parent]
        if (
            parent in fallback_set
            and event_type
            in {
                ("tts", "frame_received"),
                ("output", "frame_enqueued"),
                ("output", "frame_dequeued"),
            }
        ):
            event["monotonic_ms"] = (
                completion_times[parent]
                + 0.1
                + event["audio_frame_id"] * 0.1
            )
    export["events"].extend(
        {
            "stage": "tts",
            "event": "started",
            "sequence_id": parent,
            "text_chars": chars,
        }
        for parent, chars in sorted(text_chars.items())
    )
    return completion_times


def successful_websocket_receive_events_v3():
    return [
        {
            "order": 0,
            "timestamp_ms": 1.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "connected",
            "audio_bytes": 0,
        },
        *[
            {
                "order": index,
                "timestamp_ms": 1.0 + index,
                "frame_type": "pcm",
                "audio_bytes": audio_bytes,
            }
            for index, audio_bytes in enumerate((3200, 1600, 2400), start=1)
        ],
        {
            "order": 4,
            "timestamp_ms": 5.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "completed",
            "audio_bytes": 0,
        },
    ]


def test_end_boundary_pacer_uses_absolute_deadlines_and_stable_sample_zero():
    clock_value = 25.0
    deadlines = []
    sent = []
    anchors = []
    successful_sends = []

    def clock():
        return clock_value

    async def exercise():
        nonlocal clock_value
        stream_abort = asyncio.Event()

        async def wait_until(deadline, observed_abort):
            nonlocal clock_value
            assert observed_abort is stream_abort
            assert not observed_abort.is_set()
            deadlines.append(deadline)
            clock_value = deadline
            return True

        async def send_chunk(idx, chunk, send_timestamp):
            sent.append((idx, chunk, send_timestamp))
            return True

        return await _send_pcm_chunks_at_end_boundaries(
            b"abcdefgh",
            stream_abort,
            send_chunk,
            on_sample_zero=anchors.append,
            on_successful_send=lambda idx, emitted, deadline: (
                successful_sends.append((idx, emitted, deadline))
            ),
            clock=clock,
            wait_until=wait_until,
            chunk_bytes=4,
            chunk_duration=0.3,
        )

    sample_zero, chunk_count = asyncio.run(exercise())

    assert sample_zero == 25.0
    assert anchors == [25.0]
    assert chunk_count == 2
    assert deadlines == pytest.approx([25.3, 25.6])
    assert [item[0] for item in sent] == [0, 1]
    assert [item[1] for item in sent] == [b"abcd", b"efgh"]
    assert [item[2] for item in sent] == pytest.approx([25.3, 25.6])
    assert successful_sends == pytest.approx(
        [(0, 25.3, 25.3), (1, 25.6, 25.6)]
    )
    # Returning at the second send timestamp proves there is no trailing
    # third interval after the final source chunk.
    assert clock_value == pytest.approx(25.6)

    client_clock_origin = 24.5
    input_sample_zero_ms = (sample_zero - client_clock_origin) * 1000
    receipt_clock = 25.725
    source_end_to_receipt_ms = (
        (receipt_clock - client_clock_origin) * 1000
        - input_sample_zero_ms
        - 600.0
    )
    assert source_end_to_receipt_ms == pytest.approx(125.0)


def test_end_boundary_pacer_aborts_during_pre_first_wait():
    clock_value = 10.0
    deadlines = []
    sent = []

    def clock():
        return clock_value

    async def exercise():
        nonlocal clock_value
        stream_abort = asyncio.Event()

        async def wait_until(deadline, observed_abort):
            nonlocal clock_value
            deadlines.append(deadline)
            clock_value = 10.1
            observed_abort.set()
            return False

        async def send_chunk(idx, chunk, send_timestamp):
            sent.append((idx, chunk, send_timestamp))
            return True

        return await _send_pcm_chunks_at_end_boundaries(
            b"abcd",
            stream_abort,
            send_chunk,
            clock=clock,
            wait_until=wait_until,
            chunk_bytes=4,
            chunk_duration=0.3,
        )

    sample_zero, chunk_count = asyncio.run(exercise())

    assert sample_zero == 10.0
    assert deadlines == pytest.approx([10.3])
    assert sent == []
    assert chunk_count == 0
    assert clock_value == 10.1


def test_end_boundary_pacer_records_only_successful_sends():
    clock_value = 5.0
    successful_sends = []

    def clock():
        return clock_value

    async def exercise():
        nonlocal clock_value
        stream_abort = asyncio.Event()

        async def wait_until(deadline, _observed_abort):
            nonlocal clock_value
            clock_value = deadline
            return True

        async def send_chunk(idx, _chunk, _send_timestamp):
            return idx == 0

        return await _send_pcm_chunks_at_end_boundaries(
            b"abcdefgh",
            stream_abort,
            send_chunk,
            on_successful_send=lambda idx, emitted, deadline: (
                successful_sends.append((idx, emitted, deadline))
            ),
            clock=clock,
            wait_until=wait_until,
            chunk_bytes=4,
            chunk_duration=0.3,
        )

    sample_zero, chunk_count = asyncio.run(exercise())

    assert sample_zero == 5.0
    assert chunk_count == 1
    assert successful_sends == pytest.approx([(0, 5.3, 5.3)])


def test_generate_summary_records_capture_integrity(tmp_path):
    config = staged_config()
    staged_pipeline = successful_staged_export()
    result = BatchTestResult(
        audio_path="test_audio/example.mp3",
        duration_sec=60.0,
        backend_url="http://localhost:8000",
        backend_config_url="http://localhost:8000/api/config",
        backend_config=config,
        pipeline_mode="staged",
        pipeline_mode_source="api_config",
        staged_pipeline=staged_pipeline,
        staged_integrity_errors=[],
        websocket_receive_events=successful_websocket_receive_events(),
        chunks_sent=200,
        audio_responses=12,
        input_completed=True,
        connection_lost=False,
        drain_timed_out=False,
        drain_duration_sec=13.25,
        input_end_timestamp_ms=60_001.5,
        terminal_arrival_timestamp_ms=60_501.5,
        terminal_arrival_lag_sec=0.5,
        translation_completed=True,
        server_error="",
        input_pacing={
            "mode": INPUT_PACING_MODE,
            "chunk_duration_ms": 300.0,
            "source_sample_zero_clock": INPUT_SAMPLE_ZERO_CLOCK,
            "deadline_basis": INPUT_PACING_DEADLINE_BASIS,
            "source_sample_zero_timestamp_ms": 1.5,
            "observed_chunk_count": 200,
            "min_emission_minus_deadline_ms": 0.01,
            "max_emission_minus_deadline_ms": 2.5,
        },
    )
    output = tmp_path / "example_summary.json"

    generate_summary(result, str(output))

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["backend_url"] == "http://localhost:8000"
    assert summary["backend_config_url"].endswith("/api/config")
    assert summary["backend_config"] == config
    assert summary["pipeline_mode"] == "staged"
    assert summary["pipeline_mode_source"] == "api_config"
    assert summary["staged_pipeline"] == staged_pipeline
    assert summary["staged_integrity"] == {
        "applicable": True,
        "passed": True,
        "errors": [],
    }
    assert summary["websocket_receive_events"] == successful_websocket_receive_events()
    assert summary["input_completed"] is True
    assert summary["connection_lost"] is False
    assert summary["drain_timed_out"] is False
    assert summary["drain_duration_sec"] == 13.25
    assert summary["input_end_timestamp_ms"] == 60_001.5
    assert summary["terminal_arrival_timestamp_ms"] == 60_501.5
    assert summary["terminal_arrival_lag_sec"] == 0.5
    assert summary["translation_completed"] is True
    assert summary["server_error"] == ""
    assert summary["input_pacing"] == {
        "mode": "chunk_end_boundary_v1",
        "chunk_duration_ms": 300.0,
        "source_sample_zero_clock": "client_monotonic",
        "deadline_basis": (
            "source_sample_zero_plus_one_based_chunk_duration"
        ),
        "source_sample_zero_timestamp_ms": 1.5,
        "observed_chunk_count": 200,
        "min_emission_minus_deadline_ms": 0.01,
        "max_emission_minus_deadline_ms": 2.5,
    }


def test_generate_summary_keeps_monolithic_runs_backward_compatible(tmp_path):
    result = BatchTestResult(
        audio_path="test_audio/example.mp3",
        duration_sec=60.0,
        backend_config={"sampleRate": 16000},
    )
    output = tmp_path / "monolithic_summary.json"

    generate_summary(result, str(output))

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["pipeline_mode"] == "monolithic"
    assert summary["pipeline_mode_source"] == "legacy_default"
    assert summary["staged_pipeline"] is None
    assert summary["staged_integrity"] == {
        "applicable": False,
        "passed": None,
        "errors": [],
    }
    assert "input_pacing" not in summary


def test_resolve_pipeline_mode_records_api_or_legacy_provenance():
    assert resolve_pipeline_mode({"pipelineMode": "staged"}) == (
        "staged",
        "api_config",
    )
    assert resolve_pipeline_mode({"sampleRate": 16000}) == (
        "monolithic",
        "legacy_default",
    )


def test_fetch_backend_config_captures_server_snapshot_and_source(monkeypatch):
    config = staged_config()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return config

    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr("batch_latency_test.requests.get", fake_get)

    captured, mode, source, url = fetch_backend_config(
        "http://localhost:8000/"
    )

    assert captured is config
    assert mode == "staged"
    assert source == "api_config"
    assert url == "http://localhost:8000/api/config"
    assert calls == [(url, 10)]


@pytest.mark.parametrize("mode", ["unknown", "STAGED", 1, None, True])
def test_resolve_pipeline_mode_rejects_invalid_values(mode):
    with pytest.raises(ValueError, match="pipelineMode"):
        resolve_pipeline_mode({"pipelineMode": mode})


def test_staged_integrity_accepts_complete_ordered_bounded_export():
    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert errors == []


def test_staged_integrity_accepts_schema_v2_composite_child_lifecycle():
    errors = validate_staged_pipeline_integrity(
        successful_staged_export_v2(),
        staged_config_v2(),
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert errors == []


def test_staged_integrity_accepts_schema_v3_incremental_frame_lifecycle():
    errors = validate_staged_pipeline_integrity(
        successful_staged_export_v3(),
        staged_config_v3(),
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert errors == []


def test_staged_integrity_v3_accepts_publisher_handoff_telemetry():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert errors == []


def test_staged_integrity_v3_rejects_enabled_handoff_without_config_marker():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    del config["stagedConfig"]["ttsPublisherHandoffTelemetryEnabled"]

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "ttsPublisherHandoffTelemetryEnabled=true" in error
        for error in errors
    )


def test_staged_integrity_v3_rejects_incorrect_computed_config_capability():
    export = successful_staged_export_v3()
    config = staged_config_v3()
    config["stagedConfig"].update(
        {
            "ttsResponseChunkTelemetryEnabled": True,
            "ttsPublisherHandoffTelemetryEnabled": False,
        }
    )

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "must equal ttsIncrementalPublishEnabled" in error for error in errors
    )


def test_staged_integrity_v3_rejects_explicit_false_handoff_summary_marker():
    export = successful_staged_export_v3()
    export["tts_publisher_handoff_telemetry_enabled"] = False

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v3(),
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "legacy captures must omit the marker" in error for error in errors
    )


def test_staged_integrity_v3_rejects_enabled_handoff_without_response_sidecar():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    del export["tts_response_chunk_telemetry"]

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "requires tts_response_chunk_telemetry" in error for error in errors
    )


@pytest.mark.parametrize(
    "field",
    [
        "publish_requested_monotonic_ms",
        "event_loop_callback_started_monotonic_ms",
        "output_capacity_acquired_monotonic_ms",
    ],
)
def test_staged_integrity_v3_requires_every_enqueue_boundary(field):
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    enqueued = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("output", "frame_enqueued")
    )
    del enqueued[field]

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(field in error for error in errors)


def test_staged_integrity_v3_requires_websocket_send_started_boundary():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    del export["websocket_send_events"][0]["send_started_monotonic_ms"]

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("send_started_monotonic_ms" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("error_code", "free-form-error", "empty success code"),
        ("emission_reason", "customer-label", "fixed supported value"),
        ("source_start_ms", "not-numeric", "non-negative, finite"),
        (
            "contributing_final_ids",
            [0, "external-id"],
            "non-negative integer IDs",
        ),
        ("session_id", "", "nonempty and match"),
    ],
)
def test_staged_integrity_v3_rejects_frame_value_smuggling(
    field,
    value,
    message,
):
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    row = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("output", "frame_enqueued")
    )
    row[field] = value

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(message in error for error in errors)


@pytest.mark.parametrize(
    ("layer", "field", "value"),
    [
        ("enqueued", "publish_requested_monotonic_ms", 999.0),
        (
            "enqueued",
            "event_loop_callback_started_monotonic_ms",
            1_000.5,
        ),
        (
            "enqueued",
            "output_capacity_acquired_monotonic_ms",
            1_001.5,
        ),
        ("enqueued", "monotonic_ms", 1_003.0),
        ("dequeued", "monotonic_ms", 1_004.5),
        ("websocket", "send_started_monotonic_ms", 1_005.5),
        ("websocket", "sent_monotonic_ms", 1_006.5),
    ],
)
def test_staged_integrity_v3_rejects_handoff_timestamp_inversion(
    layer,
    field,
    value,
):
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    if layer == "websocket":
        row = export["websocket_send_events"][0]
    else:
        row = next(
            event
            for event in export["events"]
            if (event.get("stage"), event.get("event"))
            == ("output", f"frame_{layer}")
        )
    row[field] = value

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("timestamps are inverted" in error for error in errors)


def test_staged_integrity_v3_rejects_global_boundary_column_regression():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    first_send = export["websocket_send_events"][0]
    first_send["send_started_monotonic_ms"] = 1_150.0
    first_send["sent_monotonic_ms"] = 1_151.0

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "send_started timestamps must be globally nondecreasing" in error
        for error in errors
    )


@pytest.mark.parametrize(
    ("layer", "field", "value"),
    [
        ("tts", "audio_bytes", 1_234),
        ("output", "retry_count", 1),
        ("websocket", "audio_frame_id", 99),
    ],
)
def test_staged_integrity_v3_rejects_handoff_layer_mismatch(
    layer,
    field,
    value,
):
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    if layer == "websocket":
        row = export["websocket_send_events"][0]
    else:
        row = next(
            event
            for event in export["events"]
            if event.get("stage") == layer
            and event.get("event")
            in {"frame_received", "frame_enqueued"}
        )
    row[field] = value

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "canonical frame" in error or "retry coverage" in error
        for error in errors
    )


def test_staged_integrity_v3_rejects_duplicate_handoff_frame_row():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    duplicate = next(
        dict(event)
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("tts", "frame_received")
    )
    export["events"].append(duplicate)

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("exactly one canonical frame row" in error for error in errors)


def test_staged_integrity_v3_rejects_response_first_audio_mismatch():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    first_audio = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("tts", "first_audio")
        and event.get("sequence_id") == 0
    )
    first_audio["monotonic_ms"] += 1.0

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("tts/first_audio does not match" in error for error in errors)


def test_staged_integrity_v3_rejects_response_after_tts_completion():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    completed = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("tts", "completed")
        and event.get("sequence_id") == 0
    )
    completed["monotonic_ms"] = 1_099.0

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("final response chunk follows" in error for error in errors)


def test_staged_integrity_v3_rejects_direct_frame_response_boundary_mismatch():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    received = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("tts", "frame_received")
        and event.get("parent_sequence_id") == 0
        and event.get("audio_frame_id") == 1
    )
    received["monotonic_ms"] += 0.5

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "readiness does not match cumulative TTS response bytes" in error
        for error in errors
    )


def test_staged_integrity_v3_accepts_atomic_fallback_handoff_boundary():
    export = successful_staged_export_v3()
    config = staged_config_v3()
    enable_atomic_fallback_v3(export, config)
    enable_publisher_handoff_v3(export, config)

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert errors == []


def test_staged_integrity_v3_rejects_atomic_fallback_ready_before_completion():
    export = successful_staged_export_v3()
    config = staged_config_v3()
    enable_atomic_fallback_v3(export, config)
    enable_publisher_handoff_v3(export, config)
    fallback_received = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("tts", "frame_received")
        and event.get("parent_sequence_id") == 1
    )
    fallback_received["monotonic_ms"] -= 1.0

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("must match tts/completed" in error for error in errors)


def test_staged_integrity_v3_does_not_treat_unblocked_capacity_gap_as_wait():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    enqueued = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("output", "frame_enqueued")
        and event.get("parent_sequence_id") == 1
    )
    dequeued = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("output", "frame_dequeued")
        and event.get("parent_sequence_id") == 1
    )
    websocket = next(
        event
        for event in export["websocket_send_events"]
        if event.get("parent_sequence_id") == 1
    )
    enqueued.update(
        {
            "output_capacity_acquired_monotonic_ms": 1_240.0,
            "monotonic_ms": 1_241.0,
            "blocked_put_ms": 0.0,
        }
    )
    dequeued["monotonic_ms"] = 1_242.0
    websocket.update(
        {
            "send_started_monotonic_ms": 1_243.0,
            "sent_monotonic_ms": 1_244.0,
        }
    )

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert errors == []


def test_staged_integrity_v3_reconciles_handoff_blocked_wait_with_tolerance():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    enqueued = next(
        event
        for event in export["events"]
        if (event.get("stage"), event.get("event"))
        == ("output", "frame_enqueued")
    )
    enqueued["blocked_put_ms"] = 12.0
    accepted = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )
    assert accepted == []

    enqueued["blocked_put_ms"] = 12.001
    rejected = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )
    assert any("blocked_put_ms does not reconcile" in error for error in rejected)


@pytest.mark.parametrize("location", ["event", "websocket", "response_chunk"])
def test_staged_integrity_v3_rejects_private_handoff_payload_fields(location):
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    if location == "event":
        row = next(
            event
            for event in export["events"]
            if (event.get("stage"), event.get("event"))
            == ("tts", "frame_received")
        )
        row["audio"] = "raw-payload"
    elif location == "websocket":
        export["websocket_send_events"][0]["transcript"] = "private-text"
    else:
        export["tts_response_chunk_telemetry"]["chunks"][0][
            "endpoint"
        ] = "private-endpoint"

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "private or unsupported payload fields" in error for error in errors
    )


def test_staged_integrity_v3_rejects_handoff_fields_without_active_marker():
    export, config = enable_publisher_handoff_v3(
        successful_staged_export_v3(),
        staged_config_v3(),
    )
    export["tts_publisher_handoff_telemetry_enabled"] = False
    config["stagedConfig"]["ttsPublisherHandoffTelemetryEnabled"] = False

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("timing fields require" in error for error in errors)


def test_staged_integrity_v3_accepts_mixed_atomic_fallback():
    export = successful_staged_export_v3()
    config = staged_config_v3()
    enable_atomic_fallback_v3(export, config)

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert errors == []


def test_staged_integrity_v3_legacy_omission_means_no_fallback():
    export = successful_staged_export_v3()
    config = staged_config_v3()

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert errors == []
    assert not any("fallback" in error for error in errors)


def test_staged_integrity_v3_rejects_fallback_threshold_mismatch():
    export = successful_staged_export_v3()
    config = staged_config_v3()
    enable_atomic_fallback_v3(export, config)
    config["stagedConfig"][
        "ttsIncrementalAtomicFallbackMaxChars"
    ] = 5

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any("fallback_max_chars must match" in error for error in errors)


def test_staged_integrity_v3_rejects_fallback_policy_mismatch():
    export = successful_staged_export_v3()
    config = staged_config_v3()
    enable_atomic_fallback_v3(
        export,
        config,
        text_chars={0: 10, 1: 5},
    )

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "must exactly match the configured tts/started text_chars threshold"
        in error
        for error in errors
    )


def test_staged_integrity_v3_rejects_fallback_publish_before_completion():
    export = successful_staged_export_v3()
    config = staged_config_v3()
    completion_times = enable_atomic_fallback_v3(export, config)
    fallback_send = next(
        event
        for event in export["websocket_send_events"]
        if event["parent_sequence_id"] == 1
    )
    fallback_send["sent_monotonic_ms"] = completion_times[1] - 0.1

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "WebSocket frame was published before TTS completion" in error
        for error in errors
    )


def test_staged_integrity_rejects_identity_free_positive_audio_dequeue():
    export = successful_staged_export_v2()
    terminal_dequeue = export["events"][-1]
    terminal_dequeue["audio_bytes"] = 3200

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v2(),
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert any(
        "parent_sequence_id is invalid" in error for error in errors
    )


def test_staged_integrity_treats_missing_schema_version_as_legacy_v1():
    export = successful_staged_export()
    config = staged_config()

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events(),
        2.75,
    )

    assert errors == []


def test_staged_integrity_rejects_schema_config_disagreement():
    config = staged_config_v2()
    config["stagedConfig"]["telemetrySchemaVersion"] = 1

    errors = validate_staged_pipeline_integrity(
        successful_staged_export_v2(),
        config,
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert any("schema version must match" in error for error in errors)


def test_staged_integrity_rejects_unknown_schema_version():
    export = successful_staged_export()
    export["telemetry_schema_version"] = 4
    config = staged_config()
    config["stagedConfig"]["telemetrySchemaVersion"] = 4

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events(),
        2.75,
    )

    assert sum("must be 1, 2, or 3" in error for error in errors) == 2


def test_staged_integrity_v3_rejects_frame_key_layer_mismatch():
    export = successful_staged_export_v3()
    export["dequeued_audio_frame_keys"][1]["audio_frame_id"] = 7

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v3(),
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "dequeued_audio_frame_keys must exactly match "
        "published_audio_frame_keys" in error
        for error in errors
    )


def test_staged_integrity_v3_rejects_frame_byte_layer_mismatch():
    export = successful_staged_export_v3()
    export["websocket_sent_audio_frame_bytes"][2] += 2

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v3(),
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "websocket_sent_audio_frame_bytes must exactly match "
        "published_audio_frame_bytes" in error
        for error in errors
    )


def test_staged_integrity_v3_rejects_noncontiguous_frame_ids():
    export = successful_staged_export_v3()
    for field in (
        "published_audio_frame_keys",
        "dequeued_audio_frame_keys",
        "websocket_sent_audio_frame_keys",
    ):
        export[field][1]["audio_frame_id"] = 2
    export["websocket_send_events"][1]["audio_frame_id"] = 2
    for event in export["events"]:
        if (
            event.get("parent_sequence_id") == 0
            and event.get("audio_frame_id") == 1
        ):
            event["audio_frame_id"] = 2

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v3(),
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "published audio frame keys must be contiguous within every parent"
        in error
        for error in errors
    )


def test_staged_integrity_v3_rejects_parent_summary_layer_mismatch():
    export = successful_staged_export_v3()
    export["completed_parent_summaries"][0]["audio_bytes"] += 2

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v3(),
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "completed_parent_summaries must exactly match "
        "produced_parent_summaries" in error
        for error in errors
    )


def test_staged_integrity_v3_rejects_retry_count_above_one():
    export = successful_staged_export_v3()
    export["produced_parent_summaries"][1]["retry_count"] = 2

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v3(),
        successful_websocket_receive_events_v3(),
        4.5,
    )

    assert any(
        "produced_parent_summaries[1].retry_count must be zero or one"
        in error
        for error in errors
    )


def test_staged_integrity_requires_explicit_v2_websocket_composite_key():
    export = successful_staged_export_v2()
    del export["websocket_send_events"][0]["subsequence_count"]

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v2(),
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert any(
        "websocket_send_events[0].subsequence_count is invalid" in error
        for error in errors
    )


def test_staged_integrity_rejects_gapped_or_reordered_composite_lifecycle():
    export = successful_staged_export_v2()
    output_indices = [
        index
        for index, event in enumerate(export["events"])
        if event["stage"] == "output" and event["event"] == "dequeued"
    ]
    first, second = output_indices[:2]
    export["events"][first], export["events"][second] = (
        export["events"][second],
        export["events"][first],
    )

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v2(),
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert any(
        "output/dequeued composite order" in error
        for error in errors
    )


def test_staged_integrity_rejects_child_over_configured_cap():
    export = successful_staged_export_v2()
    for event in export["events"]:
        if (
            event["stage"] in {"target_splitter", "tts"}
            and event["event"] in {"emitted", "enqueued", "started"}
            and event.get("parent_sequence_id") == 0
            and event.get("subsequence_id") == 0
        ):
            event["text_chars"] = 41

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v2(max_chars=40),
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert any("text_chars exceeds configured" in error for error in errors)


def test_staged_integrity_rejects_parent_character_provenance_mismatch():
    export = successful_staged_export_v2()
    started = next(
        event
        for event in export["events"]
        if event["stage"] == "tts" and event["event"] == "started"
    )
    started["parent_text_chars"] += 1

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config_v2(),
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert any(
        "parent_text_chars must match" in error
        for error in errors
    )


def test_staged_integrity_rejects_equal_total_reordered_pcm_frame_sizes():
    received = successful_websocket_receive_events_v2()
    received[1]["audio_bytes"], received[2]["audio_bytes"] = (
        received[2]["audio_bytes"],
        received[1]["audio_bytes"],
    )
    assert sum(
        event["audio_bytes"]
        for event in received
        if event["frame_type"] == "pcm"
    ) == 12_000

    errors = validate_staged_pipeline_integrity(
        successful_staged_export_v2(),
        staged_config_v2(),
        received,
        4.5,
    )

    assert any("frame-by-frame" in error for error in errors)


def test_staged_integrity_rejects_schema_v2_with_disabled_cap():
    config = staged_config_v2(max_chars=40)
    config["stagedConfig"]["ttsSubsegmentMaxChars"] = 0

    errors = validate_staged_pipeline_integrity(
        successful_staged_export_v2(),
        config,
        successful_websocket_receive_events_v2(),
        4.5,
    )

    assert any(
        "ttsSubsegmentMaxChars must be a positive integer" in error
        for error in errors
    )


def test_staged_integrity_accepts_and_reconciles_one_shot_nmt_recovery():
    export = successful_staged_export()
    nmt_completed = [
        event
        for event in export["events"]
        if event["stage"] == "nmt" and event["event"] == "completed"
    ]
    nmt_completed[1]["retry_count"] = 1
    export["nmt_retry_count"] = 1

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert errors == []


def test_staged_integrity_accepts_and_reconciles_one_shot_tts_recovery():
    export = successful_staged_export()
    tts_completed = [
        event
        for event in export["events"]
        if event["stage"] == "tts" and event["event"] == "completed"
    ]
    tts_completed[1]["retry_count"] = 1
    export["tts_retry_count"] = 1

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert errors == []


def test_staged_integrity_rejects_mismatched_tts_retry_summary():
    export = successful_staged_export()
    export["tts_retry_count"] = 1

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert any(
        "tts_retry_count must equal the sum" in error
        for error in errors
    )


def test_staged_integrity_rejects_tts_retry_when_configured_off():
    export = successful_staged_export()
    tts_completed = [
        event
        for event in export["events"]
        if event["stage"] == "tts" and event["event"] == "completed"
    ]
    tts_completed[0]["retry_count"] = 1
    export["tts_retry_count"] = 1
    config = staged_config()
    config["stagedConfig"]["ttsMaxRetries"] = 0

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events(),
        2.75,
    )

    assert any("TTS retry telemetry is incompatible" in error for error in errors)


@pytest.mark.parametrize("event_retry", [-1, 2, True, None])
def test_staged_integrity_rejects_invalid_nmt_recovery_telemetry(event_retry):
    export = successful_staged_export()
    nmt_event = next(
        event
        for event in export["events"]
        if event["stage"] == "nmt" and event["event"] == "completed"
    )
    nmt_event["retry_count"] = event_retry

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert any("retry_count must be zero or one" in error for error in errors)


def test_staged_integrity_rejects_unclosed_or_mismatched_retry_summary():
    export = successful_staged_export()
    export["state"] = "failed"
    export["nmt_retry_count"] = 1

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert any("state must be 'closed'" in error for error in errors)
    assert any("sum of nmt/completed event retries" in error for error in errors)


def test_fetch_backend_export_waits_for_closed_staged_snapshot(monkeypatch):
    snapshots = [
        {"events": [], "stagedPipeline": None},
        {
            "events": [{"stage": "nmt"}],
            "stagedPipeline": {"state": "failed", "outcome": "failed"},
        },
        {
            "events": [{"stage": "pipeline"}],
            "stagedPipeline": {"state": "closed", "outcome": "failed"},
        },
    ]
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return Response(snapshots.pop(0))

    monkeypatch.setattr("batch_latency_test.requests.get", fake_get)
    monkeypatch.setattr("batch_latency_test.EXPORT_POLL_INTERVAL_SECONDS", 0)

    exported = asyncio.run(
        fetch_backend_export(
            "http://backend/",
            pipeline_mode="staged",
            backend_config=staged_config(),
        )
    )

    assert exported["stagedPipeline"]["state"] == "closed"
    assert exported["events"] == [{"stage": "pipeline"}]
    assert calls == [("http://backend/api/test/export", 30)] * 3


def test_fetch_backend_export_timeout_returns_latest_failure_snapshot(monkeypatch):
    failed = {
        "events": [{"stage": "nmt", "event": "error"}],
        "stagedPipeline": {"state": "failed", "outcome": "failed"},
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return failed

    class Clock:
        def __init__(self):
            self.ticks = iter((0.0, 31.0))

        def monotonic(self):
            return next(self.ticks)

    monkeypatch.setattr(
        "batch_latency_test.requests.get",
        lambda _url, timeout: Response(),
    )
    monkeypatch.setattr("batch_latency_test.time", Clock())

    exported = asyncio.run(
        fetch_backend_export(
            "http://backend",
            pipeline_mode="staged",
            backend_config={"pipelineMode": "staged", "stagedConfig": {}},
        )
    )

    assert exported is failed


def test_staged_integrity_reports_lifecycle_order_and_queue_failures():
    export = successful_staged_export()
    export.update(
        {
            "outcome": "cleanup_failed",
            "failure": {"stage": "cleanup", "error": "disconnect failed"},
            "cleanup_errors": [
                {"stage": "tts", "error": "disconnect failed"}
            ],
            "completed_sequence_ids": [0, 2],
            "incomplete_sequence_ids": [1],
            "websocket_sent_sequence_ids": [0],
            "max_queue_depths": {"nmt": 5, "tts": 1, "output": 2},
        }
    )
    export["events"][0]["queue_depth"] = 5
    export["events"][0]["queue_capacity"] = 4

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert any("outcome" in error for error in errors)
    assert any("failure must be null" in error for error in errors)
    assert any("cleanup_errors must be empty" in error for error in errors)
    assert any("incomplete sequence IDs remain" in error for error in errors)
    assert any("contiguous and ordered" in error for error in errors)
    assert any("websocket_sent_sequence_ids" in error for error in errors)
    assert any("queue depth 5 exceeds capacity 4" in error for error in errors)
    assert any("max_queue_depths.nmt=5" in error for error in errors)


def test_staged_integrity_requires_export_object_and_configured_queue_bounds():
    assert validate_staged_pipeline_integrity(
        None,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    ) == [
        "/api/test/export.stagedPipeline must be a JSON object"
    ]

    export = successful_staged_export()
    config = staged_config()
    del config["stagedConfig"]["outputQueueMaxSize"]

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events(),
        2.75,
    )

    assert errors == [
        "/api/config.stagedConfig.outputQueueMaxSize must be a positive integer"
    ]


def test_staged_integrity_requires_exactly_one_completed_terminal():
    events = successful_websocket_receive_events()
    events.append(
        {
            "order": 4,
            "timestamp_ms": 4.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "completed",
            "audio_bytes": 0,
        }
    )

    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        events,
        2.75,
    )

    assert "exactly one completed WebSocket terminal is required (got 2)" in errors


def test_staged_integrity_rejects_pcm_after_completed_terminal():
    events = successful_websocket_receive_events()
    events.append(
        {
            "order": 4,
            "timestamp_ms": 4.0,
            "frame_type": "pcm",
            "audio_bytes": 3200,
        }
    )

    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        events,
        2.75,
    )

    assert "PCM was received after the completed WebSocket terminal" in errors


def test_staged_integrity_rejects_send_receive_count_and_byte_mismatch():
    missing_frame = successful_websocket_receive_events()
    del missing_frame[2]
    for order, event in enumerate(missing_frame):
        event["order"] = order

    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        missing_frame,
        2.75,
    )
    assert any("receive count" in error for error in errors)

    wrong_bytes = successful_websocket_receive_events()
    wrong_bytes[2]["audio_bytes"] = 1600
    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        wrong_bytes,
        2.75,
    )
    assert any("received bytes" in error for error in errors)


def test_staged_integrity_rejects_completed_before_input_end():
    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        successful_websocket_receive_events(),
        3.5,
    )
    assert "completed WebSocket terminal arrived before end_input" in errors


def test_playback_tail_uses_end_input_not_final_chunk_start():
    result = BatchTestResult(
        audio_path="test_audio/example.wav",
        duration_sec=0.3,
        input_end_timestamp_ms=300.0,
        client_events=[
            TimingEvent("client", "chunk_sent", 0.0, 0, 0.0, 9600),
            TimingEvent("client", "input_ended", 300.0, 1, 0.3, 0),
            TimingEvent("client", "audio_received", 600.0, 0, 0.1, 3200),
        ],
    )

    compute_playback_metrics(result)

    assert result.first_audio_latency_sec == pytest.approx(0.6)
    assert result.playback_tail_sec == pytest.approx(0.4)


def test_playback_tail_legacy_fallback_adds_exact_chunk_duration():
    result = BatchTestResult(
        audio_path="test_audio/example.wav",
        duration_sec=0.1,
        client_events=[
            TimingEvent("client", "chunk_sent", 0.0, 0, 0.0, 3200),
            TimingEvent("client", "audio_received", 600.0, 0, 0.1, 3200),
        ],
    )

    compute_playback_metrics(result)

    assert result.playback_tail_sec == pytest.approx(0.6)


def test_playback_tail_end_boundary_fallback_uses_send_timestamp_directly():
    result = BatchTestResult(
        audio_path="test_audio/example.wav",
        duration_sec=0.1,
        audio_metadata_protocol_version=1,
        input_pacing={
            "mode": INPUT_PACING_MODE,
            "chunk_duration_ms": 300.0,
            "source_sample_zero_clock": INPUT_SAMPLE_ZERO_CLOCK,
            "deadline_basis": INPUT_PACING_DEADLINE_BASIS,
            "source_sample_zero_timestamp_ms": 0.0,
            "observed_chunk_count": 1,
            "min_emission_minus_deadline_ms": 0.0,
            "max_emission_minus_deadline_ms": 0.0,
        },
        client_events=[
            TimingEvent("client", "chunk_sent", 300.0, 0, 0.0, 3200),
            TimingEvent("client", "audio_received", 600.0, 0, 0.1, 3200),
        ],
    )

    compute_playback_metrics(result)

    assert result.playback_tail_sec == pytest.approx(0.4)


def test_playback_tail_partial_unnegotiated_pacing_uses_legacy_fallback():
    result = BatchTestResult(
        audio_path="test_audio/example.wav",
        duration_sec=0.1,
        input_pacing={"mode": INPUT_PACING_MODE},
        client_events=[
            TimingEvent("client", "chunk_sent", 300.0, 0, 0.0, 3200),
            TimingEvent("client", "audio_received", 600.0, 0, 0.1, 3200),
        ],
    )

    compute_playback_metrics(result)

    assert result.playback_tail_sec == pytest.approx(0.3)


def test_run_batch_fails_when_requested_file_is_missing(tmp_path):
    ok = asyncio.run(
        run_batch(
            [str(tmp_path / "missing.wav")],
            "http://localhost:8000",
            str(tmp_path / "results"),
        )
    )

    assert ok is False


def test_run_batch_fails_when_run_test_raises(monkeypatch, tmp_path):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")

    async def fail_run_test(_audio_path, _backend_url):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr("batch_latency_test.run_test", fail_run_test)

    ok = asyncio.run(
        run_batch(
            [str(audio)],
            "http://localhost:8000",
            str(tmp_path / "results"),
        )
    )

    assert ok is False


def test_backend_error_stops_streaming_early_and_batch_writes_failed_capture(
    monkeypatch,
    tmp_path,
):
    total_chunks = 20
    error_after_chunks = 3

    class FakePcm:
        def __len__(self):
            return total_chunks * 4800

        def tobytes(self):
            return b"\0" * (total_chunks * 9600)

    class FakeResponse:
        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeWebSocket:
        def __init__(self):
            self.recv_count = 0
            self.binary_sends = 0
            self.control_sends = []
            self.error_ready = asyncio.Event()

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps({"type": "status", "status": "connected"})
            if self.recv_count == 2:
                return json.dumps({"type": "status", "status": "listening"})
            if self.recv_count == 3:
                await self.error_ready.wait()
                return json.dumps(
                    {"type": "error", "message": "synthetic backend failure"}
                )
            await asyncio.Future()

        async def send(self, payload):
            if isinstance(payload, bytes):
                self.binary_sends += 1
                if self.binary_sends == error_after_chunks:
                    self.error_ready.set()
                return
            self.control_sends.append(json.loads(payload))

    class FakeConnection:
        def __init__(self, websocket):
            self.websocket = websocket
            self.exited = False

        async def __aenter__(self):
            return self.websocket

        async def __aexit__(self, *_args):
            self.exited = True

    fake_ws = FakeWebSocket()
    fake_connection = FakeConnection(fake_ws)
    post_calls = []
    get_calls = []

    monkeypatch.setattr("batch_latency_test.decode_audio", lambda _path: FakePcm())
    monkeypatch.setattr(
        "batch_latency_test.fetch_backend_config",
        lambda url: (
            {"pipelineMode": "monolithic"},
            "monolithic",
            "api_config",
            f"{url}/api/config",
        ),
    )
    monkeypatch.setattr(
        "batch_latency_test.websockets.connect",
        lambda *_args, **_kwargs: fake_connection,
    )
    monkeypatch.setattr("batch_latency_test.CHUNK_DURATION", 0.01)
    monkeypatch.setattr(
        "batch_latency_test.requests.post",
        lambda url, timeout: post_calls.append((url, timeout)) or FakeResponse(),
    )
    monkeypatch.setattr(
        "batch_latency_test.requests.get",
        lambda url, timeout: get_calls.append((url, timeout))
        or FakeResponse({"events": []}),
    )

    result = asyncio.run(run_test("synthetic.wav", "http://backend"))

    assert result.chunks_sent == error_after_chunks
    assert result.chunks_sent < total_chunks
    assert result.input_completed is False
    assert result.server_error == "synthetic backend failure"
    assert result.translation_completed is False
    assert result.drain_timed_out is False
    assert fake_connection.exited is True
    assert [message["type"] for message in fake_ws.control_sends] == [
        "start_stream",
        "stop_stream",
    ]
    assert [event["order"] for event in result.websocket_receive_events] == [
        0,
        1,
        2,
    ]
    terminal_event = result.websocket_receive_events[-1]
    assert terminal_event["frame_type"] == "control"
    assert terminal_event["message_type"] == "error"
    assert terminal_event["message"] == "synthetic backend failure"
    assert not any(
        event.get("status") == "completed"
        for event in result.websocket_receive_events
    )
    assert post_calls == [
        ("http://backend/api/test/start", 10),
        ("http://backend/api/test/stop", 10),
    ]
    assert get_calls == [("http://backend/api/test/export", 30)]

    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")

    async def return_failed_capture(_audio_path, _backend_url):
        return result

    generated = []
    monkeypatch.setattr("batch_latency_test.run_test", return_failed_capture)
    monkeypatch.setattr(
        "batch_latency_test.generate_plot",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_csv",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_summary",
        lambda _result, path: generated.append(path),
    )

    ok = asyncio.run(
        run_batch(
            [str(audio)],
            "http://backend",
            str(tmp_path / "results"),
        )
    )

    assert ok is False
    assert len(generated) == 3


def test_completed_before_end_input_aborts_sender_and_is_not_success(monkeypatch):
    total_chunks = 20

    class FakePcm:
        def __len__(self):
            return total_chunks * 4800

        def tobytes(self):
            return b"\0" * (total_chunks * 9600)

    class FakeResponse:
        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeWebSocket:
        def __init__(self):
            self.recv_count = 0
            self.binary_sends = 0
            self.control_sends = []
            self.completed_ready = asyncio.Event()

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps({"type": "status", "status": "connected"})
            if self.recv_count == 2:
                return json.dumps({"type": "status", "status": "listening"})
            if self.recv_count == 3:
                await self.completed_ready.wait()
                return json.dumps({"type": "status", "status": "completed"})
            await asyncio.Future()

        async def send(self, payload):
            if isinstance(payload, bytes):
                self.binary_sends += 1
                if self.binary_sends == 3:
                    self.completed_ready.set()
            else:
                self.control_sends.append(json.loads(payload))

    class FakeConnection:
        def __init__(self, websocket):
            self.websocket = websocket

        async def __aenter__(self):
            return self.websocket

        async def __aexit__(self, *_args):
            return None

    fake_ws = FakeWebSocket()
    monkeypatch.setattr("batch_latency_test.decode_audio", lambda _path: FakePcm())
    monkeypatch.setattr(
        "batch_latency_test.fetch_backend_config",
        lambda url: (
            {"pipelineMode": "monolithic"},
            "monolithic",
            "api_config",
            f"{url}/api/config",
        ),
    )
    monkeypatch.setattr(
        "batch_latency_test.websockets.connect",
        lambda *_args, **_kwargs: FakeConnection(fake_ws),
    )
    monkeypatch.setattr("batch_latency_test.CHUNK_DURATION", 0.01)
    monkeypatch.setattr(
        "batch_latency_test.requests.post",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    monkeypatch.setattr(
        "batch_latency_test.requests.get",
        lambda *_args, **_kwargs: FakeResponse({"events": []}),
    )

    result = asyncio.run(run_test("synthetic.wav", "http://backend"))

    assert result.chunks_sent == 3
    assert result.input_completed is False
    assert result.translation_completed is False
    assert result.server_error == "backend completed before client end_input"
    assert result.terminal_arrival_timestamp_ms > 0
    assert [message["type"] for message in fake_ws.control_sends] == [
        "start_stream",
        "stop_stream",
    ]


def test_run_batch_writes_but_rejects_partial_capture(monkeypatch, tmp_path):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    result = BatchTestResult(
        audio_path=str(audio),
        duration_sec=60.0,
        chunks_sent=200,
        audio_responses=1,
        total_received_bytes=3200,
        input_completed=False,
        translation_completed=True,
    )

    async def partial_run_test(_audio_path, _backend_url):
        return result

    generated = []
    monkeypatch.setattr("batch_latency_test.run_test", partial_run_test)
    monkeypatch.setattr(
        "batch_latency_test.generate_plot",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_csv",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_summary",
        lambda _result, path: generated.append(path),
    )

    ok = asyncio.run(
        run_batch(
            [str(audio)],
            "http://localhost:8000",
            str(tmp_path / "results"),
        )
    )

    assert ok is False
    assert len(generated) == 3
