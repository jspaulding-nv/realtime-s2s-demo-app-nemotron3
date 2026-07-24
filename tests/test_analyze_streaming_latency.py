import json

import pytest

from analyze_streaming_latency import (
    analyze_paths,
    load_summary_latency,
    main,
    render_markdown,
    validate_output_destinations,
)


PRIVATE_MARKER = "PRIVATE_STREAMING_LATENCY_MARKER"


def _event(
    stage,
    event,
    monotonic_ms,
    *,
    source_start_ms=None,
    source_end_ms=None,
    sequence_id=None,
    asr_final_id=None,
    contributing_final_ids=None,
    audio_bytes=0,
    composite=None,
    parent_sequence_id=None,
    audio_frame_id=None,
    audio_frame_count=None,
    retry_count=0,
):
    payload = {
        "stage": stage,
        "event": event,
        "monotonic_ms": monotonic_ms,
        "source_start_ms": source_start_ms,
        "source_end_ms": source_end_ms,
        "sequence_id": sequence_id,
        "asr_final_id": asr_final_id,
        "contributing_final_ids": contributing_final_ids or [],
        "audio_bytes": audio_bytes,
        "retry_count": retry_count,
        "private": PRIVATE_MARKER,
    }
    if composite is not None:
        parent, subsequence, count = composite
        payload.update(
            {
                "sequence_id": parent,
                "parent_sequence_id": parent,
                "subsequence_id": subsequence,
                "subsequence_count": count,
            }
        )
    if parent_sequence_id is not None:
        payload["parent_sequence_id"] = parent_sequence_id
    if audio_frame_id is not None:
        payload["audio_frame_id"] = audio_frame_id
    if audio_frame_count is not None:
        payload["audio_frame_count"] = audio_frame_count
    return payload


def write_summary(path, *, schema_version=1):
    events = [
        _event("pipeline", "started", 1_000),
        _event(
            "asr",
            "final",
            1_700,
            source_start_ms=0,
            source_end_ms=500,
            asr_final_id=0,
        ),
        _event(
            "segmenter",
            "emitted",
            1_750,
            source_start_ms=0,
            source_end_ms=500,
            sequence_id=0,
            contributing_final_ids=[0],
        ),
    ]
    websocket_events = []
    if schema_version == 1:
        events.extend(
            [
                _event(
                    "tts",
                    "first_audio",
                    2_000,
                    source_start_ms=0,
                    source_end_ms=500,
                    sequence_id=0,
                ),
                _event(
                    "tts",
                    "completed",
                    2_300,
                    source_start_ms=0,
                    source_end_ms=500,
                    sequence_id=0,
                    audio_bytes=32_000,
                ),
                _event(
                    "asr",
                    "final",
                    2_800,
                    source_start_ms=600,
                    source_end_ms=1_500,
                    asr_final_id=1,
                ),
                _event(
                    "segmenter",
                    "emitted",
                    2_900,
                    source_start_ms=600,
                    source_end_ms=1_500,
                    sequence_id=1,
                    contributing_final_ids=[1],
                ),
                _event(
                    "tts",
                    "first_audio",
                    3_100,
                    source_start_ms=600,
                    source_end_ms=1_500,
                    sequence_id=1,
                ),
                _event(
                    "tts",
                    "completed",
                    3_500,
                    source_start_ms=600,
                    source_end_ms=1_500,
                    sequence_id=1,
                    audio_bytes=64_000,
                ),
            ]
        )
        websocket_events = [
            {
                "sequence_id": 0,
                "sent_monotonic_ms": 2_350,
                "audio_bytes": 32_000,
                "private": PRIVATE_MARKER,
            },
            {
                "sequence_id": 1,
                "sent_monotonic_ms": 3_550,
                "audio_bytes": 64_000,
                "private": PRIVATE_MARKER,
            },
        ]
        parent_count = 2
        request_count = 2
    else:
        for subsequence, (first, full, sent, audio_bytes) in enumerate(
            [
                (2_000, 2_200, 2_210, 16_000),
                (2_250, 2_500, 2_520, 24_000),
            ]
        ):
            composite = (0, subsequence, 2)
            events.extend(
                [
                    _event(
                        "tts",
                        "first_audio",
                        first,
                        source_start_ms=0,
                        source_end_ms=500,
                        composite=composite,
                    ),
                    _event(
                        "tts",
                        "completed",
                        full,
                        source_start_ms=0,
                        source_end_ms=500,
                        audio_bytes=audio_bytes,
                        composite=composite,
                    ),
                ]
            )
            websocket_events.append(
                {
                    "sequence_id": 0,
                    "parent_sequence_id": 0,
                    "subsequence_id": subsequence,
                    "subsequence_count": 2,
                    "sent_monotonic_ms": sent,
                    "audio_bytes": audio_bytes,
                    "private": PRIVATE_MARKER,
                }
            )
        parent_count = 1
        request_count = 2

    staged_pipeline = {
        "telemetry_schema_version": schema_version,
        "state": "closed",
        "outcome": "complete",
        "failure": None,
        "cleanup_errors": [],
        "incomplete_sequence_ids": [],
        "segments_emitted": parent_count,
        "audio_segments_produced": request_count,
        "completed_sequence_ids": list(range(parent_count)),
        "websocket_sent_sequence_ids": list(range(parent_count)),
        "events": events,
        "websocket_send_events": websocket_events,
        "session_id": PRIVATE_MARKER,
    }
    if schema_version == 2:
        staged_pipeline["tts_subsegments_produced"] = request_count

    payload = {
        "audio_path": PRIVATE_MARKER,
        "backend_url": PRIVATE_MARKER,
        "backend_config": {
            "sampleRate": 16_000,
            "chunkSize": 4_800,
            "channels": 1,
            "pipelineMode": "staged",
            "endpoint": PRIVATE_MARKER,
        },
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "staged_pipeline": staged_pipeline,
        "private": PRIVATE_MARKER,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_schema_v3_summary(path):
    events = [
        _event("pipeline", "started", 1_000),
        _event(
            "asr",
            "final",
            1_700,
            source_start_ms=0,
            source_end_ms=500,
            asr_final_id=0,
        ),
        _event(
            "segmenter",
            "emitted",
            1_750,
            source_start_ms=0,
            source_end_ms=500,
            sequence_id=0,
            contributing_final_ids=[0],
        ),
        _event(
            "tts",
            "started",
            1_900,
            source_start_ms=0,
            source_end_ms=500,
            sequence_id=0,
        ),
        _event(
            "tts",
            "first_audio",
            2_000,
            source_start_ms=0,
            source_end_ms=500,
            sequence_id=0,
            parent_sequence_id=0,
            audio_frame_count=2,
        ),
    ]
    frame_specs = [
        (0, 0, 3_200, 2_050, 2_060, 2_070, 2_080, 0),
        (0, 1, 1_600, 2_150, 2_160, 2_170, 2_230, 0),
    ]
    for (
        parent,
        frame_id,
        audio_bytes,
        received,
        enqueued,
        dequeued,
        _sent,
        retry_count,
    ) in frame_specs:
        for stage, event_name, timestamp in (
            ("tts", "frame_received", received),
            ("output", "frame_enqueued", enqueued),
            ("output", "frame_dequeued", dequeued),
        ):
            events.append(
                _event(
                    stage,
                    event_name,
                    timestamp,
                    source_start_ms=0,
                    source_end_ms=500,
                    sequence_id=parent,
                    parent_sequence_id=parent,
                    audio_frame_id=frame_id,
                    audio_bytes=audio_bytes,
                    retry_count=retry_count,
                )
            )
    events.extend(
        [
            _event(
                "tts",
                "completed",
                2_300,
                source_start_ms=0,
                source_end_ms=500,
                sequence_id=0,
                parent_sequence_id=0,
                audio_frame_count=2,
                audio_bytes=4_800,
            ),
            _event(
                "output",
                "parent_complete_enqueued",
                2_310,
                source_start_ms=0,
                source_end_ms=500,
                sequence_id=0,
                parent_sequence_id=0,
                audio_frame_count=2,
                audio_bytes=4_800,
            ),
            _event(
                "output",
                "parent_complete_dequeued",
                2_320,
                source_start_ms=0,
                source_end_ms=500,
                sequence_id=0,
                parent_sequence_id=0,
                audio_frame_count=2,
                audio_bytes=4_800,
            ),
            _event(
                "asr",
                "final",
                2_800,
                source_start_ms=600,
                source_end_ms=1_500,
                asr_final_id=1,
            ),
            _event(
                "segmenter",
                "emitted",
                2_900,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
                contributing_final_ids=[1],
            ),
            _event(
                "tts",
                "started",
                3_000,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
            ),
            _event(
                "tts",
                "first_audio",
                3_100,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
                parent_sequence_id=1,
                audio_frame_count=1,
                retry_count=1,
            ),
        ]
    )
    frame_specs.append((1, 0, 2_400, 3_200, 3_210, 3_220, 3_250, 1))
    for stage, event_name, timestamp in (
        ("tts", "frame_received", 3_200),
        ("output", "frame_enqueued", 3_210),
        ("output", "frame_dequeued", 3_220),
    ):
        events.append(
            _event(
                stage,
                event_name,
                timestamp,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
                parent_sequence_id=1,
                audio_frame_id=0,
                audio_bytes=2_400,
                retry_count=1,
            )
        )
    events.extend(
        [
            _event(
                "tts",
                "completed",
                3_500,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
                parent_sequence_id=1,
                audio_frame_count=1,
                audio_bytes=2_400,
                retry_count=1,
            ),
            _event(
                "output",
                "parent_complete_enqueued",
                3_510,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
                parent_sequence_id=1,
                audio_frame_count=1,
                audio_bytes=2_400,
                retry_count=1,
            ),
            _event(
                "output",
                "parent_complete_dequeued",
                3_520,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
                parent_sequence_id=1,
                audio_frame_count=1,
                audio_bytes=2_400,
                retry_count=1,
            ),
        ]
    )
    frame_keys = [
        {"parent_sequence_id": 0, "audio_frame_id": 0},
        {"parent_sequence_id": 0, "audio_frame_id": 1},
        {"parent_sequence_id": 1, "audio_frame_id": 0},
    ]
    frame_bytes = [3_200, 1_600, 2_400]
    parent_summaries = [
        {
            "parent_sequence_id": 0,
            "audio_frame_count": 2,
            "audio_bytes": 4_800,
            "retry_count": 0,
        },
        {
            "parent_sequence_id": 1,
            "audio_frame_count": 1,
            "audio_bytes": 2_400,
            "retry_count": 1,
        },
    ]
    websocket_events = [
        {
            "sequence_id": parent,
            "parent_sequence_id": parent,
            "audio_frame_id": frame_id,
            "sent_monotonic_ms": sent,
            "audio_bytes": audio_bytes,
            "private": PRIVATE_MARKER,
        }
        for (
            parent,
            frame_id,
            audio_bytes,
            _received,
            _enqueued,
            _dequeued,
            sent,
            _retry_count,
        ) in frame_specs
    ]
    staged_pipeline = {
        "telemetry_schema_version": 3,
        "tts_incremental_publish_enabled": True,
        "tts_incremental_frame_ms": 100,
        "tts_incremental_frame_bytes": 3_200,
        "tts_subsegmentation_enabled": False,
        "state": "closed",
        "outcome": "complete",
        "failure": None,
        "cleanup_errors": [],
        "incomplete_sequence_ids": [],
        "segments_emitted": 2,
        "audio_segments_produced": 2,
        "audio_frames_produced": 3,
        "completed_sequence_ids": [0, 1],
        "websocket_sent_sequence_ids": [0, 1],
        "published_audio_frame_keys": frame_keys,
        "dequeued_audio_frame_keys": [dict(item) for item in frame_keys],
        "websocket_sent_audio_frame_keys": [
            dict(item) for item in frame_keys
        ],
        "published_audio_frame_bytes": frame_bytes,
        "dequeued_audio_frame_bytes": list(frame_bytes),
        "websocket_sent_audio_frame_bytes": list(frame_bytes),
        "produced_parent_summaries": parent_summaries,
        "completed_parent_summaries": [
            dict(item) for item in parent_summaries
        ],
        "websocket_completed_parent_summaries": [
            dict(item) for item in parent_summaries
        ],
        "events": events,
        "websocket_send_events": websocket_events,
        "session_id": PRIVATE_MARKER,
    }
    payload = {
        "audio_path": PRIVATE_MARKER,
        "backend_url": PRIVATE_MARKER,
        "backend_config": {
            "sampleRate": 16_000,
            "chunkSize": 4_800,
            "channels": 1,
            "pipelineMode": "staged",
            "endpoint": PRIVATE_MARKER,
        },
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "staged_pipeline": staged_pipeline,
        "private": PRIVATE_MARKER,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def add_response_chunk_sidecar(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    staged = payload["staged_pipeline"]
    staged["tts_response_chunk_telemetry_enabled"] = True
    staged["events"].extend(
        [
            _event(
                "tts",
                "started",
                1_900,
                source_start_ms=0,
                source_end_ms=500,
                sequence_id=0,
            ),
            _event(
                "tts",
                "started",
                3_000,
                source_start_ms=600,
                source_end_ms=1_500,
                sequence_id=1,
            ),
        ]
    )
    chunks = [
        {
            "parent_sequence_id": 0,
            "subsequence_id": 0,
            "subsequence_count": 1,
            "response_index": 0,
            "response_count": 2,
            "audio_bytes": 16_000,
            "cumulative_audio_bytes": 16_000,
            "audio_duration_ms": 500,
            "cumulative_audio_duration_ms": 500,
            "received_monotonic_ms": 2_000,
            "since_request_start_ms": 100,
            "since_previous_response_ms": 100,
            "retry_count": 0,
        },
        {
            "parent_sequence_id": 0,
            "subsequence_id": 0,
            "subsequence_count": 1,
            "response_index": 1,
            "response_count": 2,
            "audio_bytes": 16_000,
            "cumulative_audio_bytes": 32_000,
            "audio_duration_ms": 500,
            "cumulative_audio_duration_ms": 1_000,
            "received_monotonic_ms": 2_200,
            "since_request_start_ms": 300,
            "since_previous_response_ms": 200,
            "retry_count": 0,
        },
        {
            "parent_sequence_id": 1,
            "subsequence_id": 0,
            "subsequence_count": 1,
            "response_index": 0,
            "response_count": 1,
            "audio_bytes": 64_000,
            "cumulative_audio_bytes": 64_000,
            "audio_duration_ms": 2_000,
            "cumulative_audio_duration_ms": 2_000,
            "received_monotonic_ms": 3_100,
            "since_request_start_ms": 100,
            "since_previous_response_ms": 100,
            "retry_count": 0,
        },
    ]
    staged["tts_response_chunk_telemetry"] = {
        "schema_version": 1,
        "segments_observed": 2,
        "response_chunk_count": len(chunks),
        "chunks": chunks,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def add_schema_v3_response_chunk_sidecar(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    staged = payload["staged_pipeline"]
    staged["tts_response_chunk_telemetry_enabled"] = True
    chunks = [
        {
            "parent_sequence_id": 0,
            "subsequence_id": 0,
            "subsequence_count": 1,
            "response_index": 0,
            "response_count": 2,
            "audio_bytes": 3_200,
            "cumulative_audio_bytes": 3_200,
            "audio_duration_ms": 100,
            "cumulative_audio_duration_ms": 100,
            "received_monotonic_ms": 2_000,
            "since_request_start_ms": 100,
            "since_previous_response_ms": 100,
            "retry_count": 0,
        },
        {
            "parent_sequence_id": 0,
            "subsequence_id": 0,
            "subsequence_count": 1,
            "response_index": 1,
            "response_count": 2,
            "audio_bytes": 1_600,
            "cumulative_audio_bytes": 4_800,
            "audio_duration_ms": 50,
            "cumulative_audio_duration_ms": 150,
            "received_monotonic_ms": 2_200,
            "since_request_start_ms": 300,
            "since_previous_response_ms": 200,
            "retry_count": 0,
        },
        {
            "parent_sequence_id": 1,
            "subsequence_id": 0,
            "subsequence_count": 1,
            "response_index": 0,
            "response_count": 1,
            "audio_bytes": 2_400,
            "cumulative_audio_bytes": 2_400,
            "audio_duration_ms": 75,
            "cumulative_audio_duration_ms": 75,
            "received_monotonic_ms": 3_100,
            "since_request_start_ms": 100,
            "since_previous_response_ms": 100,
            "retry_count": 1,
        },
    ]
    staged["tts_response_chunk_telemetry"] = {
        "schema_version": 1,
        "segments_observed": 2,
        "response_chunk_count": len(chunks),
        "chunks": chunks,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_schema_v1_quantifies_boundaries_and_withheld_opportunity(tmp_path):
    path = tmp_path / "private-name_summary.json"
    write_summary(path)

    analysis = analyze_paths([path])
    request = analysis["aggregate"]["request_level"]
    boundary = request["source_boundary_to_event_seconds"]

    assert boundary["asr_final"] == {
        "observation_count": 2,
        "min": 0.2,
        "p50": 0.2,
        "p95": 0.3,
        "max": 0.3,
        "mean": 0.25,
        "cumulative": 0.5,
    }
    assert boundary["segment_emitted"]["p95"] == 0.4
    assert boundary["tts_first_response"]["p95"] == 0.6
    assert boundary["tts_full_response"]["p95"] == 1.0
    assert boundary["websocket_send"]["p95"] == 1.05
    assert request["tts_first_response_to_websocket_send_seconds"] == {
        "observation_count": 2,
        "min": 0.35,
        "p50": 0.35,
        "p95": 0.45,
        "max": 0.45,
        "mean": 0.4,
        "cumulative": 0.8,
    }
    assert analysis["samples"][0]["harness_frame_duration_ms"] == 300.0
    assert analysis["samples"][0]["initial_server_path"][
        "asr_final"
    ]["pipeline_elapsed_seconds"] == 0.7


def test_response_chunk_sidecar_quantifies_actual_cadence(tmp_path):
    path = tmp_path / "response_chunks_summary.json"
    write_summary(path)
    add_response_chunk_sidecar(path)

    analysis = analyze_paths([path])
    diagnostic = analysis["aggregate"]["tts_response_chunk_diagnostic"]

    assert diagnostic["available"] is True
    assert diagnostic["request_count"] == 2
    assert diagnostic["response_chunk_count"] == 3
    assert diagnostic["multi_response_request_percent"] == 50
    assert diagnostic["responses_per_request"]["p95"] == 2
    assert diagnostic[
        "first_response_to_rpc_complete_seconds"
    ]["p50"] == 0.3
    assert diagnostic[
        "first_response_to_rpc_complete_seconds"
    ]["p95"] == 0.4
    assert diagnostic[
        "first_response_to_last_response_seconds"
    ]["p50"] == 0
    assert diagnostic[
        "inter_response_arrival_seconds"
    ]["p50"] == 0.2
    markdown = render_markdown(analysis)
    assert "TTS response-chunk diagnostic" in markdown
    assert "50.0%" in markdown
    assert PRIVATE_MARKER not in markdown


def test_response_chunk_sidecar_rejects_noncontiguous_indices(tmp_path):
    path = tmp_path / "response_chunks_summary.json"
    write_summary(path)
    add_response_chunk_sidecar(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["tts_response_chunk_telemetry"]["chunks"][1][
        "response_index"
    ] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="indices must be contiguous"):
        analyze_paths([path])


def test_response_chunk_sidecar_rejects_duration_byte_mismatch(tmp_path):
    path = tmp_path / "response_chunks_summary.json"
    write_summary(path)
    add_response_chunk_sidecar(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["tts_response_chunk_telemetry"]["chunks"][0][
        "audio_duration_ms"
    ] = 501
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="duration does not reconcile"):
        analyze_paths([path])


def test_schema_v2_preserves_request_and_parent_units(tmp_path):
    path = tmp_path / "split_summary.json"
    write_summary(path, schema_version=2)

    sample = load_summary_latency(path, sample_index=1)
    analysis = analyze_paths([path])
    aggregate = analysis["aggregate"]

    assert [item.identity for item in sample.tts_first_responses] == [
        (0, 0, 2),
        (0, 1, 2),
    ]
    assert aggregate["counts"]["tts_requests"] == 2
    assert aggregate["counts"]["tts_parents"] == 1
    assert aggregate["request_level"]["source_boundary_to_event_seconds"][
        "tts_first_response"
    ]["p95"] == 0.75
    assert aggregate["parent_level"][
        "source_boundary_to_first_tts_response_seconds"
    ]["p50"] == 0.5
    assert aggregate["parent_level"][
        "source_boundary_to_parent_full_tts_response_seconds"
    ]["p50"] == 1.0
    assert aggregate["parent_level"][
        "source_boundary_to_parent_final_websocket_send_seconds"
    ]["p50"] == 1.02


def test_schema_v3_quantifies_incremental_first_publish_benefit(tmp_path):
    path = tmp_path / "incremental_summary.json"
    write_schema_v3_summary(path)

    sample = load_summary_latency(path, sample_index=1)
    analysis = analyze_paths([path])
    aggregate = analysis["aggregate"]
    incremental = aggregate["incremental_tts_publication"]

    assert sample.telemetry_schema_version == 3
    assert [item.identity for item in sample.tts_first_responses] == [
        (0, 0, 1),
        (1, 0, 1),
    ]
    assert [item.frame_count for item in sample.incremental_publications] == [
        2,
        1,
    ]
    assert aggregate["counts"]["tts_requests"] == 2
    assert aggregate["counts"]["websocket_audio_frames"] == 3
    assert aggregate["request_level"][
        "tts_first_response_to_websocket_send_seconds"
    ] is None
    assert incremental["available"] is True
    assert incremental["cross_arm_audio_duration_comparison"] is False
    assert "same generated PCM" in incremental["comparison_basis"]
    assert incremental["parent_count"] == 2
    assert incremental["audio_frame_count"] == 3
    assert incremental[
        "tts_first_response_to_first_websocket_send_seconds"
    ]["p50"] == 0.08
    assert incremental[
        "tts_first_response_to_first_websocket_send_seconds"
    ]["p95"] == 0.15
    assert incremental["atomic_withholding_equivalent_seconds"]["p50"] == 0.3
    assert incremental["atomic_withholding_equivalent_seconds"]["p95"] == 0.4
    assert incremental[
        "first_publish_lead_over_tts_completion_seconds"
    ]["p50"] == 0.22
    assert incremental[
        "first_publish_lead_over_tts_completion_seconds"
    ]["p95"] == 0.25
    assert aggregate["parent_level"][
        "source_boundary_to_first_websocket_send_seconds"
    ]["p50"] == 0.58
    assert aggregate["parent_level"][
        "source_boundary_to_parent_final_websocket_send_seconds"
    ]["p95"] == 0.75
    assert analysis["samples"][0]["initial_server_path"][
        "first_websocket_send"
    ]["pipeline_elapsed_seconds"] == 1.08

    markdown = render_markdown(analysis)
    assert "Incremental TTS publication (schema v3)" in markdown
    assert "Atomic withholding equivalent" in markdown
    assert PRIVATE_MARKER not in markdown


def test_schema_v3_preserves_response_chunk_diagnostic(tmp_path):
    path = tmp_path / "incremental_response_chunks_summary.json"
    write_schema_v3_summary(path)
    add_schema_v3_response_chunk_sidecar(path)

    analysis = analyze_paths([path])
    response = analysis["aggregate"]["tts_response_chunk_diagnostic"]
    incremental = analysis["aggregate"]["incremental_tts_publication"]

    assert response["available"] is True
    assert response["response_chunk_count"] == 3
    assert response["multi_response_request_percent"] == 50
    assert incremental["available"] is True
    assert incremental[
        "first_publish_lead_over_tts_completion_seconds"
    ]["p50"] == 0.22


def test_schema_v3_rejects_frame_byte_layer_mismatch(tmp_path):
    path = tmp_path / "incremental_summary.json"
    write_schema_v3_summary(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["websocket_sent_audio_frame_bytes"][1] += 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="frame byte layers"):
        analyze_paths([path])


def test_schema_v3_rejects_noncontiguous_parent_frame_identity(tmp_path):
    path = tmp_path / "incremental_summary.json"
    write_schema_v3_summary(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    staged = payload["staged_pipeline"]
    for field in (
        "published_audio_frame_keys",
        "dequeued_audio_frame_keys",
        "websocket_sent_audio_frame_keys",
    ):
        staged[field][1]["audio_frame_id"] = 2
    staged["websocket_send_events"][1]["audio_frame_id"] = 2
    for event in staged["events"]:
        if event.get("parent_sequence_id") == 0 and event.get(
            "audio_frame_id"
        ) == 1:
            event["audio_frame_id"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="not contiguous by parent"):
        analyze_paths([path])


def test_schema_v3_rejects_tts_parent_byte_mismatch(tmp_path):
    path = tmp_path / "incremental_summary.json"
    write_schema_v3_summary(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    completed = next(
        event
        for event in payload["staged_pipeline"]["events"]
        if event["stage"] == "tts"
        and event["event"] == "completed"
        and event["sequence_id"] == 0
    )
    completed["audio_bytes"] += 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="TTS completion and parent summary"):
        analyze_paths([path])


def test_multiple_inputs_use_neutral_labels_and_copy_no_private_data(tmp_path):
    first = tmp_path / "customer-a_summary.json"
    second = tmp_path / "customer-b_summary.json"
    write_summary(first)
    write_summary(second, schema_version=2)

    analysis = analyze_paths([first, second])
    rendered = json.dumps(analysis)
    markdown = render_markdown(analysis)

    assert analysis["telemetry_schema_versions"] == [1, 2]
    assert [sample["sample"] for sample in analysis["samples"]] == [
        "sample_01",
        "sample_02",
    ]
    assert analysis["aggregate"]["counts"]["tts_requests"] == 4
    assert PRIVATE_MARKER not in rendered
    assert PRIVATE_MARKER not in markdown
    assert "customer-a" not in rendered
    assert "customer-b" not in markdown
    assert all(value is False for key, value in analysis["privacy"].items() if key.startswith("contains_"))


def test_markdown_states_final_range_and_300ms_harness_caveats(tmp_path):
    path = tmp_path / "capture_summary.json"
    write_summary(path)

    markdown = render_markdown(analyze_paths([path]))

    assert "whole contributing ASR final" in markdown
    assert "300 ms PCM frames" in markdown
    assert "upper bound on delay recoverable" in markdown
    assert "microphone-to-ear" in markdown


def test_rejects_mismatched_websocket_identity(tmp_path):
    path = tmp_path / "capture_summary.json"
    write_summary(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["websocket_send_events"][1][
        "sequence_id"
    ] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="no matching TTS request"):
        analyze_paths([path])


def test_rejects_missing_source_boundary(tmp_path):
    path = tmp_path / "capture_summary.json"
    write_summary(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    final = next(
        event
        for event in payload["staged_pipeline"]["events"]
        if event["stage"] == "asr" and event["event"] == "final"
    )
    final["source_end_ms"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="source_end_ms must be finite"):
        analyze_paths([path])


def test_accepts_end_only_asr_source_timing(tmp_path):
    path = tmp_path / "capture_summary.json"
    write_summary(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    final = next(
        event
        for event in payload["staged_pipeline"]["events"]
        if event["stage"] == "asr" and event["event"] == "final"
    )
    final["source_start_ms"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    analysis = analyze_paths([path])

    assert analysis["aggregate"]["counts"]["asr_finals"] == 2


def test_rejects_incomplete_or_failed_capture(tmp_path):
    path = tmp_path / "capture_summary.json"
    write_summary(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["staged_pipeline"]["outcome"] = "error"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="outcome is not complete"):
        analyze_paths([path])


def test_output_destinations_cannot_alias_inputs_or_each_other(tmp_path):
    source = tmp_path / "source.json"
    json_output = tmp_path / "analysis.json"

    with pytest.raises(ValueError, match="overwrite an input"):
        validate_output_destinations(
            [source],
            json_output=source,
            markdown_output=None,
        )
    with pytest.raises(ValueError, match="different files"):
        validate_output_destinations(
            [source],
            json_output=json_output,
            markdown_output=json_output,
        )


def test_cli_writes_optional_json_and_markdown_outputs(tmp_path, capsys):
    source = tmp_path / "capture_summary.json"
    json_output = tmp_path / "reports" / "latency.json"
    markdown_output = tmp_path / "reports" / "latency.md"
    write_summary(source)

    main(
        [
            str(source),
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )

    assert json.loads(json_output.read_text(encoding="utf-8"))[
        "schema_version"
    ] == 1
    assert markdown_output.read_text(encoding="utf-8").startswith(
        "# Streaming Latency Analysis"
    )
    assert "Wrote" in capsys.readouterr().out


def test_cli_without_outputs_prints_markdown(tmp_path, capsys):
    source = tmp_path / "capture_summary.json"
    write_summary(source)

    main([str(source)])

    output = capsys.readouterr().out
    assert output.startswith("# Streaming Latency Analysis")
    assert PRIVATE_MARKER not in output
