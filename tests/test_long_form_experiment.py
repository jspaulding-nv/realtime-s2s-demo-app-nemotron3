import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_long_form_experiment as experiment
from headless_playback_scheduler import HeadlessPlaybackScheduler


def model_config():
    return {
        "asr": {
            "endpoint": "localhost:50052",
            "image": "nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0",
            "imageDigest": "sha256:asr",
            "profile": "name=nemotron-asr-streaming,type=en-US,batch_size=32",
            "eouMs": 800,
            "wordTimeOffsets": False,
            "sourceLanguage": "en-US",
        },
        "nmt": {
            "endpoint": "localhost:50051",
            "image": "nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2",
            "imageDigest": "sha256:nmt",
            "profile": None,
            "model": "megatronnmt_any_any_1b",
            "sourceLanguage": "en-US",
            "targetLanguage": "es-US",
        },
        "tts": {
            "endpoint": "localhost:50053",
            "image": "nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0",
            "imageDigest": "sha256:tts",
            "profile": "name=magpie-tts-multilingual,batch_size=8",
            "targetLanguage": "es-US",
            "voice": "Magpie-Multilingual.ES-US.Isabela",
        },
    }


def write_valid_summary(path: Path, **overrides):
    payload = {
        "input_completed": True,
        "connection_lost": False,
        "drain_timed_out": False,
        "translation_completed": True,
        "server_error": "",
        "chunks_sent": 10,
        "audio_responses": 2,
        "total_received_bytes": 64_000,
        "pipeline_mode": "monolithic",
        "target_language": "es-US",
        "backend_config": {
            "pipelineMode": "monolithic",
            "modelConfig": model_config(),
        },
        "input_pacing": {
            "mode": "chunk_end_boundary_v1",
            "chunk_duration_ms": 300.0,
            "source_sample_zero_clock": "client_monotonic",
            "deadline_basis": (
                "source_sample_zero_plus_one_based_chunk_duration"
            ),
            "source_sample_zero_timestamp_ms": 0.0,
            "observed_chunk_count": 10,
            "min_emission_minus_deadline_ms": 0.0,
            "max_emission_minus_deadline_ms": 0.0,
        },
        "input_end_timestamp_ms": 3000.0,
        "terminal_arrival_timestamp_ms": 3500.0,
        "terminal_arrival_lag_sec": 0.5,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def valid_staged_summary_fields(*, passed=True, errors=None, queue_size=4):
    return {
        "pipeline_mode": "staged",
        "backend_config": {
            "pipelineMode": "staged",
            "stagedConfig": {"nmtQueueMaxSize": queue_size},
            "modelConfig": model_config(),
        },
        "target_language": "es-US",
        "staged_pipeline": {"outcome": "complete"},
        "websocket_receive_events": [
            {
                "order": 0,
                "frame_type": "control",
                "message_type": "status",
                "status": "completed",
            }
        ],
        "staged_integrity": {
            "applicable": True,
            "passed": passed,
            "errors": errors or [],
        },
    }


def valid_audio_metadata_summary_fields():
    frame_metadata = {
        "protocolVersion": 1,
        "streamGeneration": 1,
        "parentSequenceId": 0,
        "audioFrameId": 0,
        "audioBytes": 3_200,
        "sampleRateHz": 16_000,
        "channels": 1,
        "bytesPerSample": 2,
        "sourceStartMs": None,
        "sourceEndMs": 1_000.0,
    }
    backend_config = {
        "sampleRate": 16_000,
        "channels": 1,
        "pipelineMode": "staged",
        "audioMetadataProtocolVersions": [1],
        "stagedConfig": {
            "telemetrySchemaVersion": 3,
            "ttsIncrementalPublishEnabled": True,
        },
        "modelConfig": model_config(),
    }
    scheduler = HeadlessPlaybackScheduler()
    scheduler.accept(
        SimpleNamespace(
            arrival_seconds=1.5,
            audio_bytes=3_200,
            protocol_version=1,
            stream_generation=1,
            parent_sequence_id=0,
            audio_frame_id=0,
            sample_rate_hz=16_000,
            channels=1,
            bytes_per_sample=2,
            source_start_ms=None,
            source_end_ms=1_000.0,
        )
    )
    headless_report = scheduler.finalize(
        input_end_seconds=3.0,
        input_sample_zero_seconds=0.0,
    )
    return {
        "audio_path": "test_audio/long-form-01.mp3",
        "backend_url": "http://localhost:8000",
        "input_duration_sec": 60.0,
        "chunks_sent": 10,
        "audio_responses": 1,
        "total_received_bytes": 3_200,
        "pipeline_mode": "staged",
        "backend_config": backend_config,
        "target_language": "es-US",
        "staged_pipeline": {
            "outcome": "complete",
            "websocket_send_events": [
                {
                    "parent_sequence_id": 0,
                    "audio_frame_id": 0,
                    "audio_bytes": 3_200,
                }
            ],
            "websocket_completed_parent_summaries": [
                {
                    "parent_sequence_id": 0,
                    "audio_frame_count": 1,
                    "audio_bytes": 3_200,
                }
            ],
        },
        "websocket_receive_events": [
            {
                "order": 0,
                "frame_type": "control",
                "message_type": "status",
                "status": "listening",
            },
            {
                "order": 1,
                "frame_type": "control",
                "message_type": "audio_frame",
                **frame_metadata,
            },
            {
                "order": 2,
                "frame_type": "pcm",
                "message_type": "binary",
                "timestamp_ms": 1_500.0,
                "audio_bytes": 3_200,
                "sourceEndToReceiptMs": 500.0,
                **frame_metadata,
            },
            {
                "order": 3,
                "frame_type": "control",
                "message_type": "audio_parent_complete",
                "protocolVersion": 1,
                "streamGeneration": 1,
                "parentSequenceId": 0,
                "audioFrameCount": 1,
                "audioBytes": 3_200,
                "sourceStartMs": None,
                "sourceEndMs": 1_000.0,
            },
            {
                "order": 4,
                "frame_type": "control",
                "message_type": "status",
                "status": "completed",
            },
        ],
        "staged_integrity": {
            "applicable": True,
            "passed": True,
            "errors": [],
        },
        "audio_metadata_observation": {
            "protocol_version": 1,
            "stream_generation": 1,
            "input_sample_zero_timestamp_ms": 0.0,
            "paired_frames": 1,
            "completed_parents": 1,
            "source_end_to_receipt": {
                "availability": (
                    "available_audio_processed_end_offset_not_semantic_boundary"
                ),
                "sample_count": 1,
                "p50_ms": 500.0,
                "p95_ms": 500.0,
                "max_ms": 500.0,
                "clock": "client_monotonic",
                "source_offset_origin": "input_pcm_sample_zero",
                "semantic_boundary_proven": False,
                "actual_audibility_proven": False,
            },
            "playback_behavior_changed": False,
            "contains_transcript_or_translation_text": False,
        },
        "headless_playback": headless_report,
    }


def write_valid_csv(path: Path, *, sent=10, received_bytes=(32_000, 32_000)):
    rows = ["source,stage,timestamp_ms,chunk_index,source_position_sec,audio_bytes"]
    rows.extend(
        f"client,chunk_sent,{(index + 1) * 300},{index},{index * 0.3},9600"
        for index in range(sent)
    )
    rows.extend(
        f"client,audio_received,{4000 + index * 10},{index},0,{audio_bytes}"
        for index, audio_bytes in enumerate(received_bytes)
    )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def write_valid_protocol_v1_csv(path: Path):
    header = [
        "source",
        "stage",
        "timestamp_ms",
        "chunk_index",
        "source_position_sec",
        "audio_bytes",
        "protocol_version",
        "stream_generation",
        "parent_sequence_id",
        "audio_frame_id",
        "source_start_ms",
        "source_end_ms",
        "source_end_to_receipt_ms",
    ]
    rows = [
        [
            "client",
            "chunk_sent",
            str((index + 1) * 300),
            str(index),
            f"{index * 0.3:.3f}",
            "9600",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
        for index in range(10)
    ]
    rows.append(
        [
            "client",
            "audio_received",
            "1500.00",
            "0",
            "0.100",
            "3200",
            "1",
            "1",
            "0",
            "0",
            "",
            "1000.000",
            "500.000",
        ]
    )
    path.write_text(
        "\n".join(
            ",".join(row)
            for row in ([header] + rows)
        )
        + "\n",
        encoding="utf-8",
    )


def mutate_protocol_receive_row(path: Path, **updates):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)
    receive = next(
        row for row in rows if row["stage"] == "audio_received"
    )
    receive.update({key: str(value) for key, value in updates.items()})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_build_manifest_orders_three_samples_per_repeat(tmp_path):
    names = [
        "long-form-01.mp3",
        "long-form-02.mp3",
        "long-form-03.mp3",
    ]
    files = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"audio")
        files.append(path)

    manifest = experiment.build_manifest(
        run_id="unit",
        run_dir=tmp_path / "run",
        backend_url="http://localhost:8000",
        repeats=2,
        skip_preflight=False,
        files=files,
        include_hashes=False,
    )

    assert [(entry["repeat"], entry["sample"]) for entry in manifest["runs"]] == [
        (1, "sample_01"),
        (1, "sample_02"),
        (1, "sample_03"),
        (2, "sample_01"),
        (2, "sample_02"),
        (2, "sample_03"),
    ]
    assert manifest["provenance"]["fixed_and_adaptive_use_identical_arrival_trace"]
    assert manifest["provenance"]["browser_web_audio_executed"] is False
    assert manifest["pipeline_provenance"] is None


def test_build_manifest_persists_observation_protocol_and_plan(
    tmp_path,
    capsys,
):
    sample = tmp_path / "long-form-01.mp3"
    sample.write_bytes(b"audio")
    manifest = experiment.build_manifest(
        run_id="unit-v1",
        run_dir=tmp_path / "run",
        backend_url="http://localhost:8000",
        repeats=1,
        skip_preflight=False,
        files=[sample],
        include_hashes=False,
        audio_metadata_protocol_version=1,
    )

    assert manifest["audio_metadata_protocol_version"] == 1
    assert manifest["provenance"]["audio_metadata_protocol_version"] == 1
    assert manifest["provenance"]["audio_metadata_observation_only"] is True

    experiment.print_plan(tmp_path / "run", manifest)
    assert "Audio metadata: protocol v1 (observation-only)" in capsys.readouterr().out


def test_audio_metadata_cli_is_opt_in():
    assert (
        experiment.parse_args([]).audio_metadata_protocol_v1 is False
    )
    assert (
        experiment.parse_args(
            ["--audio-metadata-protocol-v1"]
        ).audio_metadata_protocol_v1
        is True
    )


def test_validate_summary_rejects_partial_and_invalid_captures(tmp_path):
    summary = tmp_path / "summary.json"
    write_valid_summary(summary)
    assert experiment.validate_summary(summary) == (True, "ok")

    write_valid_summary(summary, connection_lost=True)
    assert experiment.validate_summary(summary)[0] is False

    write_valid_summary(summary, audio_responses=0)
    assert experiment.validate_summary(summary)[0] is False

    write_valid_summary(summary, chunks_sent=None)
    assert experiment.validate_summary(summary) == (
        False,
        "capture counters are invalid",
    )

    write_valid_summary(
        summary,
        **valid_staged_summary_fields(
            passed=False,
            errors=["cleanup_errors must be empty"],
        ),
    )
    valid, reason = experiment.validate_summary(summary)
    assert valid is False
    assert "staged pipeline integrity failed" in reason

    staged_missing_integrity = valid_staged_summary_fields()
    del staged_missing_integrity["staged_integrity"]
    write_valid_summary(summary, **staged_missing_integrity)
    assert experiment.validate_summary(summary) == (
        False,
        "staged pipeline integrity result is missing",
    )

    staged_missing_raw = valid_staged_summary_fields()
    del staged_missing_raw["staged_pipeline"]
    write_valid_summary(summary, **staged_missing_raw)
    assert experiment.validate_summary(summary) == (
        False,
        "staged pipeline raw evidence is missing or invalid",
    )


def test_validate_summary_replays_audio_metadata_observation(tmp_path):
    summary = tmp_path / "summary.json"
    write_valid_summary(
        summary,
        **valid_audio_metadata_summary_fields(),
    )

    assert experiment.validate_summary(
        summary,
        expected_audio_metadata_protocol_version=1,
    ) == (True, "ok")

    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["audio_metadata_observation"]["paired_frames"] = 0
    summary.write_text(json.dumps(payload), encoding="utf-8")
    valid, reason = experiment.validate_summary(
        summary,
        expected_audio_metadata_protocol_version=1,
    )
    assert valid is False
    assert "paired-frame count does not reconcile" in reason


def test_v1_summary_requires_captured_capability_advertisement(tmp_path):
    summary = tmp_path / "summary.json"
    fields = valid_audio_metadata_summary_fields()
    fields["backend_config"]["audioMetadataProtocolVersions"] = []
    write_valid_summary(
        summary,
        **fields,
    )

    valid, reason = experiment.validate_summary(
        summary,
        expected_audio_metadata_protocol_version=1,
    )
    assert valid is False
    assert "does not advertise" in reason


def test_v1_summary_requires_input_pacing_provenance(tmp_path):
    summary = tmp_path / "summary.json"
    write_valid_summary(
        summary,
        **valid_audio_metadata_summary_fields(),
        input_pacing=None,
    )

    valid, reason = experiment.validate_summary(
        summary,
        expected_audio_metadata_protocol_version=1,
    )

    assert valid is False
    assert "requires input pacing provenance" in reason


@pytest.mark.parametrize(
    ("mutation", "reason_fragment"),
    [
        (
            lambda pacing: pacing.pop("deadline_basis"),
            "provenance fields are invalid",
        ),
        (
            lambda pacing: pacing.update(observed_chunk_count=9),
            "does not match chunks_sent",
        ),
        (
            lambda pacing: pacing.update(
                min_emission_minus_deadline_ms=-0.1
            ),
            "contains an early chunk emission",
        ),
        (
            lambda pacing: pacing.update(
                max_emission_minus_deadline_ms=float("inf")
            ),
            "emission margins are invalid",
        ),
        (
            lambda pacing: pacing.update(
                source_sample_zero_timestamp_ms=1.0
            ),
            "does not match the observation",
        ),
    ],
)
def test_v1_summary_rejects_malformed_or_inconsistent_pacing_evidence(
    tmp_path,
    mutation,
    reason_fragment,
):
    summary = tmp_path / "summary.json"
    fields = valid_audio_metadata_summary_fields()
    write_valid_summary(summary, **fields)
    payload = json.loads(summary.read_text(encoding="utf-8"))
    mutation(payload["input_pacing"])
    summary.write_text(json.dumps(payload), encoding="utf-8")

    valid, reason = experiment.validate_summary(
        summary,
        expected_audio_metadata_protocol_version=1,
    )

    assert valid is False
    assert reason_fragment in reason


@pytest.mark.parametrize(
    "mutate",
    [
        lambda observation: observation.update(stream_generation=1),
        lambda observation: observation.update(
            input_sample_zero_timestamp_ms=0.0
        ),
        lambda observation: observation.update(paired_frames=1),
        lambda observation: observation.update(completed_parents=1),
        lambda observation: observation["source_end_to_receipt"].update(
            availability="unavailable_missing_source_end_offsets"
        ),
        lambda observation: observation["source_end_to_receipt"].update(
            sample_count=1,
            p50_ms=0.0,
            p95_ms=0.0,
            max_ms=0.0,
        ),
    ],
)
def test_legacy_summary_rejects_mixed_metadata_evidence(mutate):
    observation = {
        "protocol_version": None,
        "stream_generation": None,
        "input_sample_zero_timestamp_ms": None,
        "paired_frames": 0,
        "completed_parents": 0,
        "source_end_to_receipt": {
            "availability": "protocol_not_negotiated",
            "sample_count": 0,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "clock": "client_monotonic",
            "source_offset_origin": "input_pcm_sample_zero",
            "semantic_boundary_proven": False,
            "actual_audibility_proven": False,
        },
        "playback_behavior_changed": False,
        "contains_transcript_or_translation_text": False,
    }
    mutate(observation)

    valid, reason = experiment._validate_saved_audio_metadata_observation(
        {
            "backend_config": {"audioMetadataProtocolVersions": []},
            "audio_metadata_observation": observation,
        },
        None,
    )

    assert valid is False
    assert "legacy audio metadata observation is inconsistent" in reason


def test_validate_result_rejects_staged_integrity_failure():
    result = experiment.TestResult(
        audio_path="test_audio/example.wav",
        duration_sec=60.0,
        input_completed=True,
        connection_lost=False,
        drain_timed_out=False,
        translation_completed=True,
        input_end_timestamp_ms=1000.0,
        terminal_arrival_timestamp_ms=1500.0,
        terminal_arrival_lag_sec=0.5,
        audio_responses=2,
        total_received_bytes=64_000,
        staged_integrity_errors=["websocket sequence parity failed"],
    )

    try:
        experiment.validate_result(result)
    except experiment.ExperimentError as exc:
        assert "staged pipeline integrity failed" in str(exc)
        assert "websocket sequence parity failed" in str(exc)
    else:
        raise AssertionError("staged integrity failure should invalidate capture")


def test_capture_artifact_validation_requires_csv_and_summary(tmp_path):
    entry = {
        "csv": "repeat-01/example_results.csv",
        "summary": "repeat-01/example_summary.json",
        "plot": "repeat-01/example_latency.png",
    }
    parent = tmp_path / "repeat-01"
    parent.mkdir()
    write_valid_summary(parent / "example_summary.json")

    valid, reason = experiment.capture_artifacts_valid(tmp_path, entry)
    assert valid is False
    assert "event CSV" in reason

    write_valid_csv(parent / "example_results.csv")
    (parent / "example_latency.png").write_bytes(b"plot")
    assert experiment.capture_artifacts_valid(tmp_path, entry) == (True, "ok")

    write_valid_summary(
        parent / "example_summary.json",
        **valid_staged_summary_fields(
            passed=False,
            errors=["max queue depth exceeded configured capacity"],
        ),
    )
    valid, reason = experiment.capture_artifacts_valid(tmp_path, entry)
    assert valid is False
    assert "staged pipeline integrity failed" in reason
    write_valid_summary(parent / "example_summary.json")

    (parent / "example_results.csv").write_text(
        "source,stage,timestamp_ms,chunk_index,source_position_sec,audio_bytes\n",
        encoding="utf-8",
    )
    valid, reason = experiment.capture_artifacts_valid(tmp_path, entry)
    assert valid is False
    assert "mismatch" in reason

    write_valid_csv(parent / "example_results.csv")
    entry["artifact_sha256"] = experiment._artifact_hashes(tmp_path, entry)
    (parent / "example_latency.png").write_bytes(b"changed plot")
    valid, reason = experiment.capture_artifacts_valid(tmp_path, entry)
    assert valid is False
    assert "hash differs" in reason


def test_v1_artifact_resume_replays_pacing_evidence_against_csv(tmp_path):
    csv_path = tmp_path / "capture_results.csv"
    summary_path = tmp_path / "capture_summary.json"
    plot_path = tmp_path / "capture_latency.png"
    write_valid_protocol_v1_csv(csv_path)
    write_valid_summary(
        summary_path,
        **valid_audio_metadata_summary_fields(),
    )
    plot_path.write_bytes(b"plot")

    assert experiment.validate_artifact_set(
        csv_path,
        summary_path,
        plot_path,
        expected_audio_metadata_protocol_version=1,
    ) == (True, "ok")

    rows = csv_path.read_text(encoding="utf-8").splitlines()
    rows[1] = rows[1].replace(",300,0,", ",299,0,")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    valid, reason = experiment.validate_artifact_set(
        csv_path,
        summary_path,
        plot_path,
        expected_audio_metadata_protocol_version=1,
    )
    assert valid is False
    assert "early chunk emission" in reason

    write_valid_protocol_v1_csv(csv_path)
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    payload["input_pacing"]["max_emission_minus_deadline_ms"] = 1.0
    summary_path.write_text(json.dumps(payload), encoding="utf-8")
    valid, reason = experiment.validate_artifact_set(
        csv_path,
        summary_path,
        plot_path,
        expected_audio_metadata_protocol_version=1,
    )
    assert valid is False
    assert "do not match the client event ledger" in reason


def test_v1_artifact_replays_saved_headless_report_against_csv(tmp_path):
    csv_path = tmp_path / "capture_results.csv"
    summary_path = tmp_path / "capture_summary.json"
    plot_path = tmp_path / "capture_latency.png"
    write_valid_protocol_v1_csv(csv_path)
    write_valid_summary(
        summary_path,
        **valid_audio_metadata_summary_fields(),
    )
    plot_path.write_bytes(b"plot")

    # This remains a well-formed report and a self-consistent CSV row, but the
    # saved report can no longer have been derived from the promoted CSV.
    mutate_protocol_receive_row(
        csv_path,
        timestamp_ms="1600.00",
        source_end_to_receipt_ms="600.000",
    )
    valid, reason = experiment.validate_artifact_set(
        csv_path,
        summary_path,
        plot_path,
        expected_audio_metadata_protocol_version=1,
    )

    assert valid is False
    assert "headless playback replay value mismatch" in reason


@pytest.mark.parametrize(
    ("field_name", "value", "reason_fragment"),
    [
        (
            "stream_generation",
            "2",
            "headless playback replay value mismatch",
        ),
        (
            "parent_sequence_id",
            "1",
            "parent_sequence_id must be contiguous",
        ),
        (
            "audio_frame_id",
            "1",
            "each parent must start",
        ),
    ],
)
def test_v1_artifact_replay_requires_generation_parent_and_frame_order(
    tmp_path,
    field_name,
    value,
    reason_fragment,
):
    csv_path = tmp_path / "capture_results.csv"
    summary_path = tmp_path / "capture_summary.json"
    plot_path = tmp_path / "capture_latency.png"
    write_valid_protocol_v1_csv(csv_path)
    write_valid_summary(
        summary_path,
        **valid_audio_metadata_summary_fields(),
    )
    plot_path.write_bytes(b"plot")
    mutate_protocol_receive_row(csv_path, **{field_name: value})

    valid, reason = experiment.validate_artifact_set(
        csv_path,
        summary_path,
        plot_path,
        expected_audio_metadata_protocol_version=1,
    )

    assert valid is False
    assert reason_fragment in reason


def test_v1_artifact_replay_rejects_source_start_without_end(tmp_path):
    csv_path = tmp_path / "capture_results.csv"
    summary_path = tmp_path / "capture_summary.json"
    plot_path = tmp_path / "capture_latency.png"
    write_valid_protocol_v1_csv(csv_path)
    write_valid_summary(
        summary_path,
        **valid_audio_metadata_summary_fields(),
    )
    plot_path.write_bytes(b"plot")
    mutate_protocol_receive_row(
        csv_path,
        source_start_ms="1.000",
        source_end_ms="",
        source_end_to_receipt_ms="",
    )

    valid, reason = experiment.validate_artifact_set(
        csv_path,
        summary_path,
        plot_path,
        expected_audio_metadata_protocol_version=1,
    )

    assert valid is False
    assert "source_start_ms requires source_end_ms" in reason


def test_v1_artifact_replay_rejects_timestamp_mutation(tmp_path):
    csv_path = tmp_path / "capture_results.csv"
    summary_path = tmp_path / "capture_summary.json"
    plot_path = tmp_path / "capture_latency.png"
    write_valid_protocol_v1_csv(csv_path)
    write_valid_summary(
        summary_path,
        **valid_audio_metadata_summary_fields(),
    )
    plot_path.write_bytes(b"plot")
    mutate_protocol_receive_row(
        csv_path,
        timestamp_ms="1500.005",
        source_end_to_receipt_ms="500.005",
    )

    valid, reason = experiment.validate_artifact_set(
        csv_path,
        summary_path,
        plot_path,
        expected_audio_metadata_protocol_version=1,
    )

    assert valid is False
    assert "headless playback replay value mismatch" in reason


def test_v1_summary_rejects_unknown_headless_report_payload_field(tmp_path):
    summary_path = tmp_path / "capture_summary.json"
    fields = valid_audio_metadata_summary_fields()
    fields["headless_playback"]["transcript"] = "must not be accepted"
    write_valid_summary(summary_path, **fields)

    valid, reason = experiment.validate_summary(
        summary_path,
        expected_audio_metadata_protocol_version=1,
    )

    assert valid is False
    assert "unknown transcript" in reason


def test_capture_one_propagates_v1_to_batch_runner(monkeypatch, tmp_path):
    calls = []
    result = object()

    async def fake_run_test(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(experiment, "run_test", fake_run_test)
    monkeypatch.setattr(experiment, "generate_plot", lambda *_args: None)
    monkeypatch.setattr(experiment, "generate_csv", lambda *_args: None)
    monkeypatch.setattr(experiment, "generate_summary", lambda *_args: None)
    monkeypatch.setattr(experiment, "validate_result", lambda _result: None)
    audio = tmp_path / "preflight.wav"
    audio.write_bytes(b"audio")

    asyncio.run(
        experiment.capture_one(
            audio,
            "http://localhost:8000",
            tmp_path / "v1",
            audio_metadata_protocol_version=1,
        )
    )
    asyncio.run(
        experiment.capture_one(
            audio,
            "http://localhost:8000",
            tmp_path / "legacy",
        )
    )

    assert calls == [
        (
            (str(audio), "http://localhost:8000"),
            {"audio_metadata_protocol_version": 1},
        ),
        ((str(audio), "http://localhost:8000"), {}),
    ]


def test_failed_capture_retains_only_allowlisted_diagnostics(
    monkeypatch,
    tmp_path,
):
    audio = tmp_path / "long-form-02.mp3"
    audio.write_bytes(b"source audio remains outside staging")
    run_dir = tmp_path / "run"
    entry = {
        "repeat": 1,
        "sample": "sample_02",
        "csv": "repeat-01/long-form-02_results.csv",
        "summary": "repeat-01/long-form-02_summary.json",
        "plot": "repeat-01/long-form-02_latency.png",
        "failure_artifacts": [],
    }

    async def failed_capture(audio_path, _backend_url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{audio_path.stem}_results.csv").write_text(
            "source,stage,timestamp_ms,chunk_index,source_position_sec,audio_bytes\n",
            encoding="utf-8",
        )
        (output_dir / f"{audio_path.stem}_summary.json").write_text(
            '{"server_error":"safe typed failure"}\n',
            encoding="utf-8",
        )
        (output_dir / f"{audio_path.stem}_latency.png").write_bytes(b"plot")
        (output_dir / "unallowlisted.raw").write_bytes(b"must not survive")
        raise experiment.ExperimentError("safe typed failure")

    monkeypatch.setattr(experiment, "capture_one", failed_capture)

    with pytest.raises(experiment.ExperimentError, match="safe typed failure"):
        asyncio.run(
            experiment.capture_and_promote(
                audio,
                "http://localhost:8000",
                run_dir,
                entry,
                expected_pipeline={},
            )
        )

    assert not (run_dir / entry["csv"]).exists()
    assert not (run_dir / entry["summary"]).exists()
    assert not (run_dir / entry["plot"]).exists()
    assert list((run_dir / ".staging").iterdir()) == []
    assert not list(run_dir.rglob("*.raw"))

    assert len(entry["failure_artifacts"]) == 1
    retained = entry["failure_artifacts"][0]
    assert set(retained["artifacts"]) == {"csv", "summary", "plot"}
    assert set(retained["sha256"]) == {"csv", "summary", "plot"}
    for key, relative_path in retained["artifacts"].items():
        path = run_dir / relative_path
        assert path.is_file()
        assert retained["sha256"][key] == experiment.sha256_file(path)
        assert path.stat().st_mode & 0o777 == 0o600
    assert (
        run_dir / "failures" / "repeat-01-sample_02" / "attempt-01"
    ).stat().st_mode & 0o777 == 0o700
    assert (run_dir / "failures").stat().st_mode & 0o777 == 0o700
    assert (
        run_dir / "failures" / "repeat-01-sample_02"
    ).stat().st_mode & 0o777 == 0o700


def test_candidate_sla_miss_is_reported_without_operational_failure():
    analysis = {
        "traces": [
            {
                    "adaptive": {
                        "arrival_queue_p95_seconds": 12.0,
                        "time_weighted_queue_p95_seconds": 12.0,
                        "peak_queue_depth_seconds": 14.0,
                        "percent_playback_window_above_limit": 4.0,
                        "chunks_dropped": 0,
                    }
            }
        ]
    }

    experiment.add_candidate_gate_results(analysis)

    assert analysis["traces"][0]["candidate_acceptance"]["pass"] is False
    assert analysis["candidate_acceptance"]["all_traces_pass"] is False
    assert "not an operational runner failure" in analysis["candidate_acceptance"]["note"]


def test_resume_settings_must_match_original_matrix():
    manifest = {
        "backend_url": "http://localhost:8000",
        "requested_repeats": 3,
    }

    assert experiment._merge_resume_settings(
        manifest, backend_url=None, repeats=None
    ) == ("http://localhost:8000", 3)

    try:
        experiment._merge_resume_settings(
            manifest, backend_url=None, repeats=1
        )
    except experiment.ExperimentError as exc:
        assert "repeat mismatch" in str(exc)
    else:
        raise AssertionError("repeat mismatch should fail")


def test_resume_preserves_protocol_and_cannot_upgrade_legacy_manifest():
    legacy_manifest = {
        "backend_url": "http://localhost:8000",
        "requested_repeats": 1,
        "provenance": {},
    }
    assert experiment.manifest_audio_metadata_protocol_version(
        legacy_manifest
    ) is None
    assert experiment._merge_resume_settings(
        legacy_manifest,
        backend_url=None,
        repeats=None,
    ) == ("http://localhost:8000", 1)
    with pytest.raises(experiment.ExperimentError, match="protocol mismatch"):
        experiment._merge_resume_settings(
            legacy_manifest,
            backend_url=None,
            repeats=None,
            audio_metadata_protocol_v1=True,
        )

    v1_manifest = {
        "backend_url": "http://localhost:8000",
        "requested_repeats": 1,
        "audio_metadata_protocol_version": 1,
        "provenance": {
            "audio_metadata_protocol_version": 1,
        },
    }
    assert experiment._merge_resume_settings(
        v1_manifest,
        backend_url=None,
        repeats=None,
    ) == ("http://localhost:8000", 1)
    assert experiment._merge_resume_settings(
        v1_manifest,
        backend_url=None,
        repeats=None,
        audio_metadata_protocol_v1=True,
    ) == ("http://localhost:8000", 1)

    v1_manifest["provenance"]["audio_metadata_protocol_version"] = None
    with pytest.raises(experiment.ExperimentError, match="inconsistent"):
        experiment.manifest_audio_metadata_protocol_version(v1_manifest)


def test_pipeline_provenance_is_frozen_and_rejects_mode_or_config_changes():
    manifest = {"pipeline_provenance": None}
    readiness = {
        "pipeline_mode": "staged",
        "config": {
            "pipelineMode": "staged",
            "stagedConfig": {"nmtQueueMaxSize": 4, "ttsQueueMaxSize": 4},
            "modelConfig": model_config(),
        },
    }

    frozen = experiment.freeze_or_validate_pipeline_provenance(
        manifest,
        readiness,
    )

    assert frozen == {
        "pipeline_mode": "staged",
        "stagedConfig": {"nmtQueueMaxSize": 4, "ttsQueueMaxSize": 4},
        "modelConfig": model_config(),
    }
    # Capability advertisement was added after legacy manifests existed; it
    # must not invalidate their frozen model/pipeline provenance.
    readiness["config"]["audioMetadataProtocolVersions"] = []
    assert experiment.freeze_or_validate_pipeline_provenance(
        manifest,
        readiness,
    ) == frozen
    readiness["config"]["stagedConfig"]["nmtQueueMaxSize"] = 99
    assert manifest["pipeline_provenance"]["stagedConfig"]["nmtQueueMaxSize"] == 4

    with pytest.raises(experiment.ExperimentError, match="differs from the frozen"):
        experiment.freeze_or_validate_pipeline_provenance(
            manifest,
            {
                "pipeline_mode": "monolithic",
                "config": {
                    "pipelineMode": "monolithic",
                    "modelConfig": model_config(),
                },
            },
        )
    with pytest.raises(experiment.ExperimentError, match="differs from the frozen"):
        experiment.freeze_or_validate_pipeline_provenance(
            manifest,
            readiness,
        )
    readiness["config"]["stagedConfig"]["nmtQueueMaxSize"] = 4
    readiness["config"]["modelConfig"]["asr"]["image"] = (
        "nvcr.io/nim/nvidia/parakeet-1-1b-ctc-en-us:1.0.0"
    )
    with pytest.raises(experiment.ExperimentError, match="differs from the frozen"):
        experiment.freeze_or_validate_pipeline_provenance(manifest, readiness)


def test_resume_manifest_without_frozen_pipeline_is_rejected():
    manifest = {
        "pipeline_provenance": None,
        "backend_readiness": {"pipeline_mode": "staged"},
    }

    with pytest.raises(experiment.ExperimentError, match="no frozen pipeline"):
        experiment.freeze_or_validate_pipeline_provenance(
            manifest,
            {
                "pipeline_mode": "staged",
                "config": {
                    "pipelineMode": "staged",
                    "stagedConfig": {"nmtQueueMaxSize": 4},
                    "modelConfig": model_config(),
                },
            },
        )


def test_summary_pipeline_must_match_frozen_manifest(tmp_path):
    summary = tmp_path / "summary.json"
    write_valid_summary(summary, **valid_staged_summary_fields(queue_size=8))

    valid, reason = experiment.validate_summary(
        summary,
        expected_pipeline={
            "pipeline_mode": "staged",
            "stagedConfig": {"nmtQueueMaxSize": 4},
            "modelConfig": model_config(),
        },
    )

    assert valid is False
    assert "differs from manifest" in reason


def test_summary_rejects_pipeline_mode_config_disagreement(tmp_path):
    summary = tmp_path / "summary.json"
    write_valid_summary(
        summary,
        pipeline_mode="monolithic",
        backend_config={"pipelineMode": "staged", "stagedConfig": {}},
    )

    valid, reason = experiment.validate_summary(summary)

    assert valid is False
    assert "mode/config mismatch" in reason


def test_backend_lock_rejects_concurrent_local_runner():
    backend = "http://localhost:18000"
    with experiment.experiment_lock(backend):
        try:
            with experiment.experiment_lock(backend):
                pass
        except experiment.ExperimentError as exc:
            assert "already using" in str(exc)
        else:
            raise AssertionError("a concurrent runner should not acquire the lock")


def test_resume_provenance_rejects_changed_sample(monkeypatch, tmp_path):
    sample = tmp_path / "long-form-01.mp3"
    sample.write_bytes(b"original audio")
    manifest = experiment.build_manifest(
        run_id="unit",
        run_dir=tmp_path / "run",
        backend_url="http://localhost:8000",
        repeats=1,
        skip_preflight=True,
        files=[sample],
    )
    manifest["git"] = {"commit": "abc123", "dirty": False}
    monkeypatch.setattr(
        experiment,
        "git_metadata",
        lambda: {"commit": "abc123", "short_commit": "abc123", "dirty": False},
    )
    experiment.validate_resume_provenance(manifest)

    sample.write_bytes(b"modified audio")
    try:
        experiment.validate_resume_provenance(manifest)
    except experiment.ExperimentError as exc:
        assert "differs" in str(exc)
    else:
        raise AssertionError("changed sample provenance should fail")


def test_run_id_is_timestamped_and_commit_scoped():
    now = datetime(2026, 7, 22, 12, 34, 56, tzinfo=timezone.utc)
    assert experiment.make_run_id(now, "d46451d") == "20260722T123456Z_d46451d"


class ReadinessResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_staged_backend_readiness_allows_idle_monolithic_client(monkeypatch):
    responses = {
        "http://localhost:8000/": {
            "status": "ok",
            "pipeline_mode": "staged",
            "riva_connected": False,
        },
        "http://localhost:8000/api/config": {
            "pipelineMode": "staged",
            "stagedConfig": {"nmtQueueMaxSize": 4},
            "modelConfig": model_config(),
        },
    }
    monkeypatch.setattr(
        experiment.requests,
        "get",
        lambda url, timeout: ReadinessResponse(responses[url]),
    )

    readiness = experiment.check_backend_ready("http://localhost:8000")

    assert readiness["pipeline_mode"] == "staged"
    assert readiness["riva_connected"] is False
    assert readiness["config"] == responses["http://localhost:8000/api/config"]


def test_metadata_readiness_requires_advertised_v1(monkeypatch):
    config = {
        "pipelineMode": "staged",
        "audioMetadataProtocolVersions": [],
        "stagedConfig": {
            "telemetrySchemaVersion": 3,
            "ttsIncrementalPublishEnabled": True,
        },
        "modelConfig": model_config(),
    }
    responses = {
        "http://localhost:8000/": {
            "status": "ok",
            "pipeline_mode": "staged",
            "riva_connected": False,
        },
        "http://localhost:8000/api/config": config,
    }
    monkeypatch.setattr(
        experiment.requests,
        "get",
        lambda url, timeout: ReadinessResponse(responses[url]),
    )

    with pytest.raises(experiment.ExperimentError, match="does not advertise"):
        experiment.check_backend_ready(
            "http://localhost:8000",
            audio_metadata_protocol_version=1,
        )

    config["audioMetadataProtocolVersions"] = [1]
    readiness = experiment.check_backend_ready(
        "http://localhost:8000",
        audio_metadata_protocol_version=1,
    )
    assert readiness["config"]["audioMetadataProtocolVersions"] == [1]


def test_monolithic_backend_readiness_still_requires_connection(monkeypatch):
    monkeypatch.setattr(
        experiment.requests,
        "get",
        lambda url, timeout: ReadinessResponse(
            {
                "status": "ok",
                "pipeline_mode": "monolithic",
                "riva_connected": False,
            }
        ),
    )

    with pytest.raises(experiment.ExperimentError, match="not connected to Riva"):
        experiment.check_backend_ready("http://localhost:8000")


def test_staged_backend_readiness_rejects_config_mode_mismatch(monkeypatch):
    responses = {
        "http://localhost:8000/": {
            "status": "ok",
            "pipeline_mode": "staged",
            "riva_connected": False,
        },
        "http://localhost:8000/api/config": {"pipelineMode": "monolithic"},
    }
    monkeypatch.setattr(
        experiment.requests,
        "get",
        lambda url, timeout: ReadinessResponse(responses[url]),
    )

    with pytest.raises(experiment.ExperimentError, match="mode mismatch"):
        experiment.check_backend_ready("http://localhost:8000")


def test_dry_run_never_checks_backend_or_writes_artifacts(monkeypatch, tmp_path):
    def unexpected_backend_check(_url):
        raise AssertionError("dry run contacted the backend")

    monkeypatch.setattr(experiment, "check_backend_ready", unexpected_backend_check)
    local_audio = tmp_path / "audio"
    local_audio.mkdir()
    files = []
    for index in range(1, 4):
        path = local_audio / f"long-form-{index:02d}.mp3"
        path.write_bytes(b"test audio")
        files.append(str(path))
    monkeypatch.setattr(experiment, "LONG_FORM_FILES", files)
    output_root = tmp_path / "experiments"

    exit_code = experiment.main(
        [
            "--dry-run",
            "--run-id",
            "unit-dry-run",
            "--output-root",
            str(output_root),
        ]
    )

    assert exit_code == 0
    assert not output_root.exists()


def test_execute_experiment_checkpoints_sequential_captures(monkeypatch, tmp_path):
    names = [
        "long-form-01.mp3",
        "long-form-02.mp3",
        "long-form-03.mp3",
    ]
    files = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"audio")
        files.append(path)
    run_dir = tmp_path / "run"
    manifest = experiment.build_manifest(
        run_id="unit",
        run_dir=run_dir,
        backend_url="http://localhost:8000",
        repeats=1,
        skip_preflight=True,
        files=files,
        include_hashes=False,
    )
    capture_order = []

    async def fake_capture(audio_path, _backend_url, output_dir):
        capture_order.append(audio_path.stem)
        output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = output_dir / f"{audio_path.stem}_results.csv"
        summary_path = output_dir / f"{audio_path.stem}_summary.json"
        plot_path = output_dir / f"{audio_path.stem}_latency.png"
        write_valid_csv(csv_path)
        write_valid_summary(summary_path)
        plot_path.write_bytes(b"plot")
        return {
            "csv": str(csv_path),
            "summary": str(summary_path),
            "plot": str(plot_path),
        }

    monkeypatch.setattr(
        experiment,
        "check_backend_ready",
        lambda _url: {
            "status": "ok",
            "riva_connected": True,
            "pipeline_mode": "monolithic",
            "config": {
                "pipelineMode": "monolithic",
                "modelConfig": model_config(),
            },
        },
    )
    monkeypatch.setattr(experiment, "capture_one", fake_capture)
    monkeypatch.setattr(
        experiment,
        "write_analysis",
        lambda _run_dir, _manifest: {
            "candidate_acceptance": {"all_traces_pass": False}
        },
    )

    exit_code = asyncio.run(experiment.execute_experiment(run_dir, manifest))

    assert exit_code == 0
    assert capture_order == [
        "long-form-01",
        "long-form-02",
        "long-form-03",
    ]
    saved = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert all(entry["status"] == "completed" for entry in saved["runs"])
    assert all(entry["artifact_sha256"] for entry in saved["runs"])
    assert saved["analysis"]["all_candidate_gates_pass"] is False
    assert saved["pipeline_provenance"] == {
        "pipeline_mode": "monolithic",
        "stagedConfig": None,
        "modelConfig": model_config(),
    }


def test_v1_execute_propagates_to_preflight_and_all_three_captures(
    monkeypatch,
    tmp_path,
):
    files = []
    for index in range(1, 4):
        path = tmp_path / f"long-form-{index:02d}.mp3"
        path.write_bytes(b"audio")
        files.append(path)
    run_dir = tmp_path / "run"
    manifest = experiment.build_manifest(
        run_id="unit-v1",
        run_dir=run_dir,
        backend_url="http://localhost:8000",
        repeats=1,
        skip_preflight=False,
        files=files,
        include_hashes=False,
        audio_metadata_protocol_version=1,
    )
    observed = []

    def fake_ready(_url, *, audio_metadata_protocol_version):
        assert audio_metadata_protocol_version == 1
        return {
            "status": "ok",
            "riva_connected": False,
            "pipeline_mode": "staged",
            "config": {
                "pipelineMode": "staged",
                "audioMetadataProtocolVersions": [1],
                "stagedConfig": {
                    "telemetrySchemaVersion": 3,
                    "ttsIncrementalPublishEnabled": True,
                },
                "modelConfig": model_config(),
            },
        }

    async def fake_capture(
        audio_path,
        _backend_url,
        _run_dir,
        entry,
        _expected_pipeline,
        *,
        audio_metadata_protocol_version,
    ):
        observed.append(
            (
                entry.get("sample", "preflight"),
                Path(audio_path).name,
                audio_metadata_protocol_version,
            )
        )
        entry["_captured"] = True

    monkeypatch.setattr(experiment, "check_backend_ready", fake_ready)
    monkeypatch.setattr(experiment, "capture_and_promote", fake_capture)
    monkeypatch.setattr(
        experiment,
        "capture_artifacts_valid",
        lambda _run_dir, entry, **_kwargs: (
            (True, "ok")
            if entry.get("_captured")
            else (False, "missing")
        ),
    )
    monkeypatch.setattr(
        experiment,
        "write_analysis",
        lambda _run_dir, _manifest: {
            "candidate_acceptance": {"all_traces_pass": False}
        },
    )

    assert asyncio.run(
        experiment.execute_experiment(run_dir, manifest)
    ) == 0
    assert observed == [
        ("preflight", "preflight.wav", 1),
        ("sample_01", "long-form-01.mp3", 1),
        ("sample_02", "long-form-02.mp3", 1),
        ("sample_03", "long-form-03.mp3", 1),
    ]
