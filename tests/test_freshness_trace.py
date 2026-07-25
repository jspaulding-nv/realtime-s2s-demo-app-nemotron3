import copy
import csv
import json
from dataclasses import asdict

import pytest

from freshness_trace import load_parent_freshness_trace


CSV_HEADER = [
    "source",
    "stage",
    "timestamp_ms",
    "chunk_index",
    "source_position_sec",
    "audio_bytes",
]
METADATA_CSV_HEADER = CSV_HEADER + [
    "protocol_version",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "source_start_ms",
    "source_end_ms",
    "source_end_to_receipt_ms",
]


def _parent(parent_id, frame_count, audio_bytes):
    return {
        "parent_sequence_id": parent_id,
        "audio_frame_count": frame_count,
        "audio_bytes": audio_bytes,
        "retry_count": 0,
        "atomic_fallback_applied": False,
    }


def _write_valid_capture(tmp_path):
    csv_path = tmp_path / "neutral_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(
            [
                ["client", "chunk_sent", "0.00", "0", "0.0", "9600"],
                ["client", "audio_received", "1000.00", "0", "0.0", "3200"],
                ["backend", "audio_sent_to_client", "1000.01", "-1", "0.0", "3200"],
                ["client", "audio_received", "1050.00", "1", "0.0", "100"],
                ["client", "audio_received", "1200.00", "2", "0.0", "1600"],
                ["client", "input_ended", "2000.00", "1", "2.0", "0"],
            ]
        )

    parents = [_parent(0, 2, 3300), _parent(1, 1, 1600)]
    keys = [
        {"parent_sequence_id": 0, "audio_frame_id": 0},
        {"parent_sequence_id": 0, "audio_frame_id": 1},
        {"parent_sequence_id": 1, "audio_frame_id": 0},
    ]
    frame_bytes = [3200, 100, 1600]
    send_events = [
        {
            "sequence_id": key["parent_sequence_id"],
            **key,
            "audio_bytes": audio_bytes,
            "sent_monotonic_ms": 50_000.0 + index,
        }
        for index, (key, audio_bytes) in enumerate(zip(keys, frame_bytes))
    ]
    summary = {
        "pipeline_mode": "staged",
        "input_end_timestamp_ms": 2000.004,
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "backend_url": "PRIVATE-MARKER",
        "backend_config": {
            "sampleRate": 16_000,
            "channels": 1,
            "modelConfig": {"asr": {"endpoint": "PRIVATE-MARKER"}},
            "stagedConfig": {
                "telemetrySchemaVersion": 3,
                "ttsIncrementalPublishEnabled": True,
            },
        },
        "websocket_receive_events": [
            {
                "order": 0,
                "timestamp_ms": 0.0,
                "frame_type": "control",
                "message_type": "status",
                "status": "connected",
            },
            {
                "order": 1,
                "timestamp_ms": 1000.004,
                "frame_type": "pcm",
                "audio_bytes": 3200,
            },
            {
                "order": 2,
                "timestamp_ms": 1050.004,
                "frame_type": "pcm",
                "audio_bytes": 100,
            },
            {
                "order": 3,
                "timestamp_ms": 1200.004,
                "frame_type": "pcm",
                "audio_bytes": 1600,
            },
            {
                "order": 4,
                "timestamp_ms": 2001.0,
                "frame_type": "control",
                "message_type": "status",
                "status": "completed",
            },
        ],
        "staged_pipeline": {
            "session_id": "PRIVATE-MARKER",
            "telemetry_schema_version": 3,
            "tts_incremental_publish_enabled": True,
            "state": "closed",
            "outcome": "complete",
            "failure": None,
            "cleanup_errors": [],
            "incomplete_sequence_ids": [],
            "completed_sequence_ids": [0, 1],
            "websocket_sent_sequence_ids": [0, 1],
            "audio_frames_produced": 3,
            "audio_segments_produced": 2,
            "completed_parent_summaries": copy.deepcopy(parents),
            "produced_parent_summaries": copy.deepcopy(parents),
            "websocket_completed_parent_summaries": copy.deepcopy(parents),
            "websocket_send_events": send_events,
            "published_audio_frame_keys": copy.deepcopy(keys),
            "dequeued_audio_frame_keys": copy.deepcopy(keys),
            "websocket_sent_audio_frame_keys": copy.deepcopy(keys),
            "published_audio_frame_bytes": list(frame_bytes),
            "dequeued_audio_frame_bytes": list(frame_bytes),
            "websocket_sent_audio_frame_bytes": list(frame_bytes),
        },
    }
    summary_path = tmp_path / "neutral_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    return csv_path, summary_path, summary


def _upgrade_capture_to_protocol_v1(csv_path, summary_path, summary):
    generation = 7
    input_sample_zero_ms = 100.0
    summary["backend_config"]["audioMetadataProtocolVersions"] = [1]
    frame_metadata = [
        {
            "protocolVersion": 1,
            "streamGeneration": generation,
            "parentSequenceId": 0,
            "audioFrameId": 0,
            "audioBytes": 3200,
            "sampleRateHz": 16_000,
            "channels": 1,
            "bytesPerSample": 2,
            "sourceStartMs": None,
            "sourceEndMs": 500.0,
        },
        {
            "protocolVersion": 1,
            "streamGeneration": generation,
            "parentSequenceId": 0,
            "audioFrameId": 1,
            "audioBytes": 100,
            "sampleRateHz": 16_000,
            "channels": 1,
            "bytesPerSample": 2,
            "sourceStartMs": None,
            "sourceEndMs": 500.0,
        },
        {
            "protocolVersion": 1,
            "streamGeneration": generation,
            "parentSequenceId": 1,
            "audioFrameId": 0,
            "audioBytes": 1600,
            "sampleRateHz": 16_000,
            "channels": 1,
            "bytesPerSample": 2,
            "sourceStartMs": 500.0,
            "sourceEndMs": 900.0,
        },
    ]
    arrival_ms = [1000.004, 1050.004, 1200.004]
    source_delays_ms = [
        timestamp_ms - input_sample_zero_ms - metadata["sourceEndMs"]
        for timestamp_ms, metadata in zip(arrival_ms, frame_metadata)
    ]

    receive_events = [
        {
            "order": 0,
            "timestamp_ms": 0.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "connected",
        }
    ]
    order = 1
    for index, (timestamp_ms, metadata) in enumerate(
        zip(arrival_ms, frame_metadata)
    ):
        receive_events.append(
            {
                "order": order,
                "timestamp_ms": timestamp_ms - 0.001,
                "frame_type": "control",
                "audio_bytes": 0,
                "message_type": "audio_frame",
                "status": None,
                **metadata,
            }
        )
        order += 1
        receive_events.append(
            {
                "order": order,
                "timestamp_ms": timestamp_ms,
                "frame_type": "pcm",
                "audio_bytes": metadata["audioBytes"],
                **metadata,
                "sourceEndToReceiptMs": source_delays_ms[index],
            }
        )
        order += 1
        if index in (1, 2):
            parent_id = metadata["parentSequenceId"]
            parent_frames = [
                frame
                for frame in frame_metadata
                if frame["parentSequenceId"] == parent_id
            ]
            receive_events.append(
                {
                    "order": order,
                    "timestamp_ms": timestamp_ms + 0.001,
                    "frame_type": "control",
                    "audio_bytes": 0,
                    "message_type": "audio_parent_complete",
                    "status": None,
                    "protocolVersion": 1,
                    "streamGeneration": generation,
                    "parentSequenceId": parent_id,
                    "audioFrameCount": len(parent_frames),
                    "audioBytes": sum(
                        frame["audioBytes"] for frame in parent_frames
                    ),
                    "sourceStartMs": metadata["sourceStartMs"],
                    "sourceEndMs": metadata["sourceEndMs"],
                }
            )
            order += 1
    receive_events.append(
        {
            "order": order,
            "timestamp_ms": 2001.0,
            "frame_type": "control",
            "audio_bytes": 0,
            "message_type": "status",
            "status": "completed",
        }
    )
    summary["websocket_receive_events"] = receive_events
    summary["audio_metadata_observation"] = {
        "protocol_version": 1,
        "stream_generation": generation,
        "input_sample_zero_timestamp_ms": input_sample_zero_ms,
        "paired_frames": 3,
        "completed_parents": 2,
        "source_end_to_receipt": {
            "availability": (
                "available_audio_processed_end_offset_not_semantic_boundary"
            ),
            "sample_count": 3,
            "p50_ms": 400.004,
            "p95_ms": 450.004,
            "max_ms": 450.004,
            "clock": "client_monotonic",
            "source_offset_origin": "input_pcm_sample_zero",
            "semantic_boundary_proven": False,
            "actual_audibility_proven": False,
        },
        "playback_behavior_changed": False,
        "contains_transcript_or_translation_text": False,
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    receive_index = 0
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METADATA_CSV_HEADER)
        writer.writeheader()
        for row in rows:
            if row["source"] == "client" and row["stage"] == "audio_received":
                metadata = frame_metadata[receive_index]
                row.update(
                    {
                        "protocol_version": "1",
                        "stream_generation": str(generation),
                        "parent_sequence_id": str(
                            metadata["parentSequenceId"]
                        ),
                        "audio_frame_id": str(metadata["audioFrameId"]),
                        "source_start_ms": (
                            ""
                            if metadata["sourceStartMs"] is None
                            else f"{metadata['sourceStartMs']:.3f}"
                        ),
                        "source_end_ms": f"{metadata['sourceEndMs']:.3f}",
                        "source_end_to_receipt_ms": (
                            f"{source_delays_ms[receive_index]:.3f}"
                        ),
                    }
                )
                receive_index += 1
            writer.writerow(row)
    return frame_metadata


def test_load_parent_freshness_trace_joins_validated_parent_frames(tmp_path):
    csv_path, summary_path, _ = _write_valid_capture(tmp_path)

    trace = load_parent_freshness_trace(csv_path, summary_path)

    assert trace.trace_csv == "schema3_client_events.csv"
    assert trace.summary_json == "schema3_capture_summary.json"
    assert len(trace.trace_sha256) == 64
    assert len(trace.summary_sha256) == 64
    assert trace.input_end_seconds == pytest.approx(2.000004)
    assert (trace.sample_rate_hz, trace.channels, trace.bytes_per_sample) == (
        16_000,
        1,
        2,
    )
    assert [
        (
            frame.source_index,
            frame.parent_sequence_id,
            frame.audio_frame_id,
            frame.parent_frame_count,
            frame.audio_bytes,
        )
        for frame in trace.frames
    ] == [
        (0, 0, 0, 2, 3200),
        (1, 0, 1, 2, 100),
        (2, 1, 0, 1, 1600),
    ]
    assert trace.frames[0].arrival_seconds == pytest.approx(1.0)
    assert trace.frames[0].duration_seconds == pytest.approx(0.1)
    assert trace.input_sample_zero_timestamp_ms is None
    assert trace.audio_metadata_protocol_version is None
    assert all(frame.source_end_ms is None for frame in trace.frames)


def test_protocol_v1_replays_wire_and_reconciles_staged_and_csv_evidence(
    tmp_path,
):
    csv_path, summary_path, summary = _write_valid_capture(tmp_path)
    _upgrade_capture_to_protocol_v1(csv_path, summary_path, summary)

    trace = load_parent_freshness_trace(csv_path, summary_path)

    assert trace.audio_metadata_protocol_version == 1
    assert trace.audio_metadata_stream_generation == 7
    assert trace.input_sample_zero_timestamp_ms == 100.0
    assert trace.source_end_to_receipt_availability == (
        "available_audio_processed_end_offset_not_semantic_boundary"
    )
    assert trace.frames[0].arrival_seconds == pytest.approx(1.000004)
    assert [
        (frame.source_start_ms, frame.source_end_ms)
        for frame in trace.frames
    ] == [(None, 500.0), (None, 500.0), (500.0, 900.0)]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda summary: summary["websocket_receive_events"].pop(1),
            "without an audio_frame",
        ),
        (
            lambda summary: summary["websocket_receive_events"][2].update(
                parentSequenceId=1
            ),
            "preceding audio_frame",
        ),
        (
            lambda summary: summary["websocket_receive_events"][5].update(
                audioBytes=3299
            ),
            "byte count does not reconcile",
        ),
        (
            lambda summary: summary["audio_metadata_observation"][
                "source_end_to_receipt"
            ].update(p50_ms=999.0),
            "p50_ms does not reconcile",
        ),
        (
            lambda summary: summary["audio_metadata_observation"].update(
                paired_frames=2
            ),
            "paired-frame count does not reconcile",
        ),
    ],
)
def test_protocol_v1_loader_rejects_inconsistent_wire_or_summary_evidence(
    tmp_path,
    mutator,
    message,
):
    csv_path, summary_path, summary = _write_valid_capture(tmp_path)
    _upgrade_capture_to_protocol_v1(csv_path, summary_path, summary)
    mutator(summary)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_parent_freshness_trace(csv_path, summary_path)


def test_protocol_v1_loader_rejects_csv_metadata_disagreement(tmp_path):
    csv_path, summary_path, summary = _write_valid_capture(tmp_path)
    _upgrade_capture_to_protocol_v1(csv_path, summary_path, summary)
    rows = list(csv.reader(csv_path.open(encoding="utf-8")))
    parent_id_column = rows[0].index("parent_sequence_id")
    rows[2][parent_id_column] = "1"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)

    with pytest.raises(ValueError, match="CSV audio metadata identity"):
        load_parent_freshness_trace(csv_path, summary_path)


def test_protocol_v1_loader_requires_advertised_backend_capability(tmp_path):
    csv_path, summary_path, summary = _write_valid_capture(tmp_path)
    _upgrade_capture_to_protocol_v1(csv_path, summary_path, summary)
    summary["backend_config"]["audioMetadataProtocolVersions"] = []
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match="did not advertise"):
        load_parent_freshness_trace(csv_path, summary_path)


def test_protocol_v1_loader_requires_extended_csv_metadata_columns(tmp_path):
    csv_path, summary_path, summary = _write_valid_capture(tmp_path)
    _upgrade_capture_to_protocol_v1(csv_path, summary_path, summary)
    rows = list(csv.reader(csv_path.open(encoding="utf-8")))
    source_end_column = rows[0].index("source_end_ms")
    for row in rows:
        row.pop(source_end_column)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)

    with pytest.raises(ValueError, match="missing required columns"):
        load_parent_freshness_trace(csv_path, summary_path)


def test_default_summary_name_is_resolved_adjacent_to_csv(tmp_path):
    csv_path, summary_path, _ = _write_valid_capture(tmp_path)
    expected = csv_path.with_name("neutral_summary.json")
    assert summary_path == expected

    trace = load_parent_freshness_trace(csv_path)

    assert len(trace.frames) == 3


def test_public_trace_does_not_copy_private_summary_fields_or_absolute_paths(
    tmp_path,
):
    csv_path, summary_path, _ = _write_valid_capture(tmp_path)
    identifying_csv = tmp_path / "Person_Customer_Sermon_results.csv"
    identifying_summary = tmp_path / "Person_Customer_Sermon_summary.json"
    csv_path.rename(identifying_csv)
    summary_path.rename(identifying_summary)

    serialized = json.dumps(
        asdict(
            load_parent_freshness_trace(
                identifying_csv,
                identifying_summary,
            )
        )
    )

    assert "PRIVATE-MARKER" not in serialized
    assert "Person_Customer_Sermon" not in serialized
    assert str(tmp_path) not in serialized


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda summary: summary["staged_integrity"].update(passed=False),
            "integrity",
        ),
        (
            lambda summary: summary["staged_pipeline"].update(
                telemetry_schema_version=2
            ),
            "schema 3",
        ),
        (
            lambda summary: summary["staged_pipeline"][
                "websocket_sent_audio_frame_keys"
            ][0].update(audio_frame_id=99),
            "disagrees",
        ),
        (
            lambda summary: summary["staged_pipeline"][
                "completed_parent_summaries"
            ][0].update(audio_bytes=999),
            "summary layers disagree",
        ),
        (
            lambda summary: summary["websocket_receive_events"][1].update(
                audio_bytes=1
            ),
            "PCM bytes",
        ),
        (
            lambda summary: summary.update(input_end_timestamp_ms=2100.0),
            "input-end timestamps disagree",
        ),
        (
            lambda summary: summary["staged_pipeline"].update(
                incomplete_sequence_ids=[1]
            ),
            "incomplete",
        ),
    ],
)
def test_loader_rejects_inconsistent_schema3_evidence(
    tmp_path,
    mutator,
    message,
):
    csv_path, summary_path, summary = _write_valid_capture(tmp_path)
    mutator(summary)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_parent_freshness_trace(csv_path, summary_path)


def test_loader_rejects_csv_index_byte_and_timestamp_mismatches(tmp_path):
    csv_path, summary_path, _ = _write_valid_capture(tmp_path)
    rows = list(csv.reader(csv_path.open(encoding="utf-8")))

    rows[2][3] = "2"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    with pytest.raises(ValueError, match="indexes must be contiguous"):
        load_parent_freshness_trace(csv_path, summary_path)

    rows[2][3] = "0"
    rows[2][5] = "3199"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    with pytest.raises(ValueError, match="PCM bytes"):
        load_parent_freshness_trace(csv_path, summary_path)

    rows[2][5] = "3200"
    rows[2][2] = "1000.02"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    with pytest.raises(ValueError, match="timestamp 0 disagree"):
        load_parent_freshness_trace(csv_path, summary_path)


def test_loader_requires_one_completed_terminal_after_input_and_final_pcm(
    tmp_path,
):
    csv_path, summary_path, summary = _write_valid_capture(tmp_path)
    completed = summary["websocket_receive_events"][-1]

    summary["websocket_receive_events"].pop()
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one completed"):
        load_parent_freshness_trace(csv_path, summary_path)

    summary["websocket_receive_events"].append(completed)
    summary["websocket_receive_events"].append(
        {
            **completed,
            "order": 5,
            "timestamp_ms": 2002.0,
        }
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one completed"):
        load_parent_freshness_trace(csv_path, summary_path)

    summary["websocket_receive_events"].pop()
    summary["websocket_receive_events"][-1]["timestamp_ms"] = 1999.0
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="before input ended"):
        load_parent_freshness_trace(csv_path, summary_path)

    summary["websocket_receive_events"][-1]["timestamp_ms"] = 2001.0
    terminal = summary["websocket_receive_events"].pop()
    terminal["order"] = 3
    final_pcm = summary["websocket_receive_events"][-1]
    final_pcm["order"] = 4
    summary["websocket_receive_events"].insert(3, terminal)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="PCM was received after"):
        load_parent_freshness_trace(csv_path, summary_path)
