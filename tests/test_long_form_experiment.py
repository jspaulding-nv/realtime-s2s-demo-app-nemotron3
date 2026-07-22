import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import run_long_form_experiment as experiment


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


def write_valid_csv(path: Path, *, sent=10, received_bytes=(32_000, 32_000)):
    rows = ["source,stage,timestamp_ms,chunk_index,source_position_sec,audio_bytes"]
    rows.extend(
        f"client,chunk_sent,{index * 300},{index},{index * 0.3},9600"
        for index in range(sent)
    )
    rows.extend(
        f"client,audio_received,{4000 + index * 10},{index},0,{audio_bytes}"
        for index, audio_bytes in enumerate(received_bytes)
    )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


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


def test_candidate_sla_miss_is_reported_without_operational_failure():
    analysis = {
        "traces": [
            {
                "adaptive": {
                    "arrival_queue_p95_seconds": 12.0,
                    "time_weighted_queue_p95_seconds": 12.0,
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
