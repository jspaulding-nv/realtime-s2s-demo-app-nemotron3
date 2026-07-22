import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import run_three_sermon_experiment as experiment


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
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


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


def test_build_manifest_orders_three_sermons_per_repeat(tmp_path):
    names = [
        "200108_SpiritandPresenceofGod.mp3",
        "Blessed_Self-Forgetfulness.mp3",
        "gospel_in_life_tk_1-john-part-2-mp3_Beholding_the_Love_of_God.mp3",
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

    assert [(entry["repeat"], entry["sermon"]) for entry in manifest["runs"]] == [
        (1, "spirit"),
        (1, "blessed"),
        (1, "beholding"),
        (2, "spirit"),
        (2, "blessed"),
        (2, "beholding"),
    ]
    assert manifest["provenance"]["fixed_and_adaptive_use_identical_arrival_trace"]
    assert manifest["provenance"]["browser_web_audio_executed"] is False


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


def test_resume_provenance_rejects_changed_sermon(monkeypatch, tmp_path):
    sermon = tmp_path / "200108_SpiritandPresenceofGod.mp3"
    sermon.write_bytes(b"original audio")
    manifest = experiment.build_manifest(
        run_id="unit",
        run_dir=tmp_path / "run",
        backend_url="http://localhost:8000",
        repeats=1,
        skip_preflight=True,
        files=[sermon],
    )
    manifest["git"] = {"commit": "abc123", "dirty": False}
    monkeypatch.setattr(
        experiment,
        "git_metadata",
        lambda: {"commit": "abc123", "short_commit": "abc123", "dirty": False},
    )
    experiment.validate_resume_provenance(manifest)

    sermon.write_bytes(b"modified audio")
    try:
        experiment.validate_resume_provenance(manifest)
    except experiment.ExperimentError as exc:
        assert "differs" in str(exc)
    else:
        raise AssertionError("changed sermon provenance should fail")


def test_run_id_is_timestamped_and_commit_scoped():
    now = datetime(2026, 7, 22, 12, 34, 56, tzinfo=timezone.utc)
    assert experiment.make_run_id(now, "d46451d") == "20260722T123456Z_d46451d"


def test_dry_run_never_checks_backend_or_writes_artifacts(monkeypatch, tmp_path):
    def unexpected_backend_check(_url):
        raise AssertionError("dry run contacted the backend")

    monkeypatch.setattr(experiment, "check_backend_ready", unexpected_backend_check)
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
        "200108_SpiritandPresenceofGod.mp3",
        "Blessed_Self-Forgetfulness.mp3",
        "gospel_in_life_tk_1-john-part-2-mp3_Beholding_the_Love_of_God.mp3",
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
        lambda _url: {"status": "ok", "riva_connected": True},
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
        "200108_SpiritandPresenceofGod",
        "Blessed_Self-Forgetfulness",
        "gospel_in_life_tk_1-john-part-2-mp3_Beholding_the_Love_of_God",
    ]
    saved = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert all(entry["status"] == "completed" for entry in saved["runs"])
    assert all(entry["artifact_sha256"] for entry in saved["runs"])
    assert saved["analysis"]["all_candidate_gates_pass"] is False
