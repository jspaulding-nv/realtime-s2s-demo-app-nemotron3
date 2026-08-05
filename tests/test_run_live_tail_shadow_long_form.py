from pathlib import Path

import run_live_tail_shadow_long_form as batch


def test_new_manifest_registers_all_sources(monkeypatch):
    monkeypatch.setattr(batch, "git_commit", lambda: "a" * 40)
    monkeypatch.setattr(batch, "git_dirty", lambda: False)

    manifest = batch.new_manifest([1, 2, 3])

    assert manifest["schema"] == batch.SCHEMA
    assert manifest["incrementalFrameMs"] == 100
    assert manifest["observationOnly"] is True
    assert manifest["liveAudioChanged"] is False
    assert [item["sample"] for item in manifest["samples"]] == [1, 2, 3]
    assert [item["sourceSha256"] for item in manifest["samples"]] == [
        batch.LONG_FORM_SOURCES[sample][1] for sample in (1, 2, 3)
    ]


def test_child_command_pins_complete_sample_and_100_ms_profile():
    command = batch.build_child_command(
        sample=2,
        output_dir=Path("/private/sample-02-attempt-01"),
        timeout_seconds=3600,
        npm=Path("/tools/node/bin/npm"),
        chrome=Path("/usr/bin/google-chrome"),
    )

    assert command[0] == batch.sys.executable
    assert command[1].endswith("run_live_tail_shadow_preflight.py")
    assert command[command.index("--long-form-sample") + 1] == "2"
    assert command[command.index("--incremental-frame-ms") + 1] == "100"
    assert command[command.index("--timeout-seconds") + 1] == "3600"
    assert command[command.index("--npm") + 1] == "/tools/node/bin/npm"
    assert command[command.index("--chrome") + 1] == "/usr/bin/google-chrome"


def test_next_attempt_directory_never_overwrites_prior_attempts(tmp_path):
    record = {"sample": 3, "attempts": [{"status": "failed"}]}

    assert batch.next_attempt_directory(tmp_path, record) == (
        tmp_path / "sample-03-attempt-02"
    )
