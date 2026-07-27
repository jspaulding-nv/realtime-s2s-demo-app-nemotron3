from __future__ import annotations

import hashlib
import io
import json
import os
import random
import stat
import wave
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import run_blinded_tts_comparison as gate
import tts_multitext_corpus as corpus


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _fake_wav(frame_count: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(gate.SAMPLE_RATE_HZ)
        audio.writeframes(b"\x00\x00" * frame_count)
    return output.getvalue()


def _make_repository(root: Path) -> None:
    (root / "magpie_tts_control.py").write_text("", encoding="utf-8")
    (root / "chatterbox_tts_canary.py").write_text("", encoding="utf-8")
    (root / ".python-packages").mkdir()
    (root / ".python-packages-chatterbox").mkdir()


def _fixture_number(text: str) -> int:
    for index, item in enumerate(corpus.ITEMS, start=1):
        if item.text == text:
            return index
    raise AssertionError("unexpected fixture text")


class FakeArmRunner:
    def __init__(
        self,
        *,
        chatterbox_ratio: float = 0.85,
        chatterbox_ttfa: float = 0.90,
        chatterbox_underrun: float = 0.70,
    ) -> None:
        self.chatterbox_ratio = chatterbox_ratio
        self.chatterbox_ttfa = chatterbox_ttfa
        self.chatterbox_underrun = chatterbox_underrun
        self.calls: list[
            tuple[list[str], dict[str, str], Path, float]
        ] = []

    def __call__(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> int:
        command = list(command)
        self.calls.append(
            (
                command,
                dict(environment),
                cwd,
                timeout_seconds,
            )
        )
        text_path = Path(command[command.index("--text-file") + 1])
        artifact_dir = Path(
            command[command.index("--artifact-dir") + 1]
        )
        text = text_path.read_text(encoding="utf-8").strip()
        fixture_number = _fixture_number(text)
        artifact_dir.mkdir(mode=0o700)

        is_magpie = command[2].endswith("magpie_tts_control.py")
        magpie_frames = 2_205 + fixture_number * 441
        if is_magpie:
            arm = "magpie"
            frame_count = magpie_frames
            ttfa = 0.15
            underrun = 0.0
            report_name = "magpie-tts-control.json"
        else:
            arm = "chatterbox"
            frame_count = round(
                magpie_frames * self.chatterbox_ratio
            )
            ttfa = self.chatterbox_ttfa
            underrun = self.chatterbox_underrun
            report_name = "chatterbox-tts-canary.json"
        duration = frame_count / gate.SAMPLE_RATE_HZ

        results: list[dict[str, Any]] = []
        for repeat_index in range(1, gate.DEFAULT_REPEATS + 1):
            result = {
                "status": "succeeded",
                "repeat_index": repeat_index,
                "audio_duration_seconds": duration,
                "ttfa_seconds": ttfa,
                "wall_time_seconds": duration * 0.8,
                "real_time_factor": 0.8,
                "underrun_risk_seconds": underrun,
            }
            if arm == "chatterbox":
                result.update(
                    {
                        "factor_index": 1,
                        "execution_index": repeat_index,
                        "exaggeration_factor": 0.5,
                    }
                )
                wav_name = (
                    "chatterbox-factor-01-0p5-"
                    f"repeat-{repeat_index:02d}.wav"
                )
            else:
                wav_name = (
                    f"magpie-control-repeat-{repeat_index:03d}.wav"
                )
            results.append(result)
            (artifact_dir / wav_name).write_bytes(
                _fake_wav(frame_count)
            )
            os.chmod(artifact_dir / wav_name, 0o600)

        if arm == "magpie":
            report = {
                "schema_version": 1,
                "diagnostic": "magpie_tts_matched_streaming_control",
                "client": {
                    "version": gate.MAGPIE_CLIENT_VERSION,
                },
                "declared_unverified_model_provenance": {
                    "container_image": gate.MAGPIE_IMAGE,
                    "nim_profile": gate.MAGPIE_PROFILE,
                    "image_digest": gate.MAGPIE_DIGEST,
                },
                "service": {
                    "model": "magpie-tts-multilingual",
                },
            }
            request = {
                "tts_locale": gate.MAGPIE_LOCALE,
                "voice": gate.MAGPIE_VOICE,
                "sample_rate_hz": gate.SAMPLE_RATE_HZ,
                "channels": 1,
                "sample_width_bytes": 2,
                "encoding": "LINEAR_PCM",
                "measured_repeat_count": gate.DEFAULT_REPEATS,
                "warmup_request_count": 1,
            }
        else:
            report = {
                "schema_version": 2,
                "diagnostic": "chatterbox_tts_exaggeration_canary",
                "client": {
                    "reported_version": (
                        gate.CHATTERBOX_CLIENT_VERSION
                    ),
                    "required_version": (
                        gate.CHATTERBOX_CLIENT_VERSION
                    ),
                },
                "declared_model_provenance": {
                    "declared_container_image": gate.CHATTERBOX_IMAGE,
                    "declared_nim_profile": gate.CHATTERBOX_PROFILE,
                    "declared_image_digest": gate.CHATTERBOX_DIGEST,
                },
                "service": {
                    "requested_model": (
                        "chatterbox-tts-multilingual"
                    ),
                },
                "warmup": {
                    "status": "succeeded",
                    "exaggeration_factor": 0.5,
                },
            }
            request = {
                "tts_locale": gate.CHATTERBOX_LOCALE,
                "voice": gate.CHATTERBOX_VOICE,
                "sample_rate_hz": gate.SAMPLE_RATE_HZ,
                "channels": 1,
                "sample_width_bytes": 2,
                "encoding": "LINEAR_PCM",
                "repeats_per_factor": gate.DEFAULT_REPEATS,
                "warmup_request_count": 1,
                "exaggeration_factors": [0.5],
            }
        request["text"] = {
            "comparison_status": "custom_unmatched",
            "character_count": len(text),
            "utf8_byte_count": len(text.encode("utf-8")),
        }
        report.update(
            {
                "request": request,
                "summary": {
                    "requested": gate.DEFAULT_REPEATS,
                    "succeeded": gate.DEFAULT_REPEATS,
                    "failed": 0,
                },
                "results": results,
            }
        )
        (artifact_dir / report_name).write_text(
            json.dumps(report),
            encoding="utf-8",
        )
        os.chmod(artifact_dir / report_name, 0o600)
        return 0


def test_corpus_is_versioned_unique_and_text_free_identity() -> None:
    items = corpus.validate_corpus()
    identity = corpus.corpus_identity()

    assert len(items) == 6
    assert identity["corpus_id"] == "neutral-spanish-multitext-tts"
    assert identity["corpus_version"] == 1
    assert identity["fixture_count"] == 6
    serialized = json.dumps(identity, ensure_ascii=False)
    for item in items:
        assert item.text not in serialized
        assert item.identity()["character_count"] == len(item.text)


@pytest.mark.parametrize("reviewer_count", [1, 9, True, 2.5])
def test_reviewer_count_is_bounded(reviewer_count: object) -> None:
    with pytest.raises(ValueError):
        gate.validate_options(
            reviewer_count=reviewer_count,  # type: ignore[arg-type]
            child_timeout_seconds=30,
        )


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), True],
)
def test_child_timeout_must_be_positive_finite(timeout: object) -> None:
    with pytest.raises(ValueError):
        gate.validate_options(
            reviewer_count=2,
            child_timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_child_environment_is_minimal_and_arm_specific(
    tmp_path: Path,
) -> None:
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "GH_TOKEN": "secret-github-token",
        "NGC_API_KEY": "secret-ngc-key",
        "UNRELATED": "private",
    }

    magpie = gate._minimal_child_environment(
        tmp_path,
        "magpie",
        source,
    )
    chatterbox = gate._minimal_child_environment(
        tmp_path,
        "chatterbox",
        source,
    )

    assert magpie["PATH"] == "/usr/bin"
    assert magpie["HOME"] == "/home/test"
    assert magpie["PYTHONNOUSERSITE"] == "1"
    assert str(tmp_path / ".python-packages") in magpie["PYTHONPATH"]
    assert (
        str(tmp_path / ".python-packages-chatterbox")
        in chatterbox["PYTHONPATH"]
    )
    for environment in (magpie, chatterbox):
        assert "GH_TOKEN" not in environment
        assert "NGC_API_KEY" not in environment
        assert "UNRELATED" not in environment


def test_arm_commands_use_text_files_and_separate_runners(
    tmp_path: Path,
) -> None:
    text_file = tmp_path / "input.txt"
    artifact = tmp_path / "artifact"

    magpie = gate._arm_command(
        python_executable="/usr/bin/python3",
        repository_root=tmp_path,
        arm="magpie",
        text_file=text_file,
        artifact_dir=artifact,
    )
    chatterbox = gate._arm_command(
        python_executable="/usr/bin/python3",
        repository_root=tmp_path,
        arm="chatterbox",
        text_file=text_file,
        artifact_dir=artifact,
    )

    assert magpie[:2] == ["/usr/bin/python3", "-S"]
    assert chatterbox[:2] == ["/usr/bin/python3", "-S"]
    assert magpie[2].endswith("magpie_tts_control.py")
    assert chatterbox[2].endswith("chatterbox_tts_canary.py")
    assert "--text-file" in magpie
    assert "--text-file" in chatterbox
    assert corpus.ITEMS[0].text not in magpie
    assert corpus.ITEMS[0].text not in chatterbox
    assert magpie[magpie.index("--repeats") + 1] == "5"
    assert (
        chatterbox[
            chatterbox.index("--repeats-per-factor") + 1
        ]
        == "5"
    )
    assert (
        chatterbox[
            chatterbox.index("--exaggeration-factors") + 1
        ]
        == "0.5"
    )
    assert "cfg_weight" not in " ".join(chatterbox)


def test_formal_artifacts_are_restricted_to_ignored_results(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    resolved = gate._resolve_artifact_directory(
        Path("experiment_results/formal-run"),
        repository_root=repository,
        allow_outside_results=False,
    )
    assert resolved == (
        repository / "experiment_results" / "formal-run"
    )

    with pytest.raises(ValueError, match="experiment_results"):
        gate._resolve_artifact_directory(
            tmp_path / "outside",
            repository_root=repository,
            allow_outside_results=False,
        )


def test_full_gate_builds_private_balanced_blind_packages(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    artifact = tmp_path / "artifacts"
    runner = FakeArmRunner()

    report, report_path = gate.run_gate(
        artifact_dir=artifact,
        repository_root=repository,
        python_executable="/usr/bin/python3",
        reviewer_count=3,
        child_timeout_seconds=30,
        allow_artifact_outside_results=True,
        process_runner=runner,
        source_environment={
            "PATH": "/usr/bin",
            "GH_TOKEN": "must-not-propagate",
            "NGC_API_KEY": "must-not-propagate",
        },
        randomizer=random.Random(7),
    )

    assert report_path == artifact / gate.REPORT_FILENAME
    assert report["mechanical_gate"]["passed"] is True
    assert (
        report["mechanical_gate"]["status"]
        == "client_metrics_passed_pending_runtime_attestation"
    )
    assert (
        report["mechanical_gate"]["observed"][
            "shorter_fixture_count"
        ]
        == 6
    )
    assert report["review_package"] == {
        "reviewer_bundle_count": 3,
        "minimum_completed_reviewer_count": 2,
        "preferred_completed_reviewer_count": 3,
        "pair_count_per_reviewer": 12,
        "clip_count_per_reviewer": 24,
        "source_clip_count": 24,
        "selected_repeat_indexes": [2, 4],
        "blinding_key_sha256": report["review_package"][
            "blinding_key_sha256"
        ],
        "quality_status": "pending_native_listener_review",
        "phase_one_status": (
            "created_withheld_pending_runtime_attestation"
        ),
        "phase_two_status": "withheld_pending_phase_one_lock",
    }

    assert len(runner.calls) == 12
    expected_arm_order = [
        "magpie",
        "chatterbox",
        "chatterbox",
        "magpie",
    ] * 3
    observed_arm_order = [
        (
            "magpie"
            if call[0][2].endswith("magpie_tts_control.py")
            else "chatterbox"
        )
        for call in runner.calls
    ]
    assert observed_arm_order == expected_arm_order
    for command, environment, cwd, timeout in runner.calls:
        assert cwd == repository
        assert timeout == 30
        assert command[1] == "-S"
        assert "GH_TOKEN" not in environment
        assert "NGC_API_KEY" not in environment
        for item in corpus.ITEMS:
            assert item.text not in command

    assert _mode(artifact) == 0o700
    assert _mode(artifact / "raw") == 0o700
    assert _mode(artifact / "private") == 0o700
    assert _mode(artifact / "review") == 0o700
    assert _mode(report_path) == 0o600
    assert not list((artifact / "private").glob(".input-*.txt"))

    key_path = artifact / "private" / gate.BLINDING_KEY_FILENAME
    key_bytes = key_path.read_bytes()
    assert _mode(key_path) == 0o600
    assert hashlib.sha256(key_bytes).hexdigest() == report[
        "review_package"
    ]["blinding_key_sha256"]
    key = json.loads(key_bytes)
    serialized_key = key_bytes.decode("utf-8")
    for item in corpus.ITEMS:
        assert item.text not in serialized_key
    for reviewer in key["reviewers"]:
        pairs = reviewer["pairs"]
        assert len(pairs) == 12
        assert (
            sum(
                pair["clip_a"]["model"] == "chatterbox"
                for pair in pairs
            )
            == 6
        )
        assert {
            pair["source_repeat_index"] for pair in pairs
        } == {2, 4}
        for item in corpus.ITEMS:
            fixture_pairs = [
                pair
                for pair in pairs
                if pair["fixture_id"] == item.fixture_id
            ]
            assert len(fixture_pairs) == 2
            assert {
                pair["clip_a"]["model"] for pair in fixture_pairs
            } == {"magpie", "chatterbox"}

    for reviewer_index in range(1, 4):
        reviewer_dir = (
            artifact / "review" / f"reviewer-{reviewer_index:02d}"
        )
        clips = sorted((reviewer_dir / "clips").glob("*.wav"))
        assert len(clips) == 24
        assert all(_mode(path) == 0o600 for path in clips)
        pass_one = (
            reviewer_dir / "pass-1-audio-only.csv"
        ).read_text(encoding="utf-8")
        phase_two = (
            artifact
            / "private"
            / "phase-2-reference-templates"
            / f"reviewer-{reviewer_index:02d}"
            / "pass-2-reference-visible.csv"
        ).read_text(encoding="utf-8")
        assert not (
            reviewer_dir / "pass-2-reference-visible.csv"
        ).exists()
        assert "reference_text" not in pass_one.splitlines()[0]
        assert "reference_text" in phase_two.splitlines()[0]
        assert "comment" not in pass_one.lower()
        assert "comment" not in phase_two.lower()
        assert "name" not in pass_one.splitlines()[0].lower()
        assert "email" not in phase_two.splitlines()[0].lower()
        assert _mode(
            reviewer_dir / "pass-1-audio-only.csv"
        ) == 0o600

    public_json = report_path.read_text(encoding="utf-8")
    for item in corpus.ITEMS:
        assert item.text not in public_json
    assert str(tmp_path) not in public_json
    assert '"contains_model_to_clip_mapping": false' in public_json


def test_wrong_child_provenance_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    base_runner = FakeArmRunner()

    def wrong_provenance_runner(
        command: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> int:
        return_code = base_runner(
            command,
            environment,
            cwd,
            timeout_seconds,
        )
        if command[2].endswith("chatterbox_tts_canary.py"):
            artifact_dir = Path(
                command[command.index("--artifact-dir") + 1]
            )
            report_path = (
                artifact_dir / "chatterbox-tts-canary.json"
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["request"]["voice"] = "wrong-voice"
            report_path.write_text(json.dumps(report), encoding="utf-8")
        return return_code

    with pytest.raises(RuntimeError, match="pinned TTS contract"):
        gate.run_gate(
            artifact_dir=tmp_path / "artifacts",
            repository_root=repository,
            reviewer_count=2,
            allow_artifact_outside_results=True,
            process_runner=wrong_provenance_runner,
            source_environment={"PATH": "/usr/bin"},
            randomizer=random.Random(1),
        )


def test_malformed_child_wav_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    base_runner = FakeArmRunner()

    def malformed_wav_runner(
        command: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> int:
        return_code = base_runner(
            command,
            environment,
            cwd,
            timeout_seconds,
        )
        artifact_dir = Path(
            command[command.index("--artifact-dir") + 1]
        )
        if command[2].endswith("magpie_tts_control.py"):
            wav_path = artifact_dir / "magpie-control-repeat-001.wav"
        else:
            wav_path = (
                artifact_dir
                / "chatterbox-factor-01-0p5-repeat-01.wav"
            )
        wav_path.write_bytes(b"RIFF" + b"\x00" * 40)
        return return_code

    with pytest.raises(RuntimeError, match="valid WAV"):
        gate.run_gate(
            artifact_dir=tmp_path / "artifacts",
            repository_root=repository,
            reviewer_count=2,
            allow_artifact_outside_results=True,
            process_runner=malformed_wav_runner,
            source_environment={"PATH": "/usr/bin"},
            randomizer=random.Random(1),
        )


def test_mechanical_gate_rejects_small_duration_advantage(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    runner = FakeArmRunner(chatterbox_ratio=0.97)

    report, _ = gate.run_gate(
        artifact_dir=tmp_path / "artifacts",
        repository_root=repository,
        reviewer_count=2,
        allow_artifact_outside_results=True,
        process_runner=runner,
        source_environment={"PATH": "/usr/bin"},
        randomizer=random.Random(3),
    )

    mechanical = report["mechanical_gate"]
    assert mechanical["passed"] is False
    assert mechanical["status"] == "mechanical_gate_not_met"
    assert (
        mechanical["conditions"][
            "overall_duration_ratio_at_most_0_92"
        ]
        is False
    )
    assert (
        mechanical["conditions"][
            "at_least_four_fixtures_five_percent_shorter"
        ]
        is False
    )
    assert (
        report["review_package"]["quality_status"]
        == "not_evaluated_mechanical_gate_failed"
    )
    assert not (tmp_path / "artifacts" / "review").exists()
    assert not (
        tmp_path
        / "artifacts"
        / "private"
        / gate.BLINDING_KEY_FILENAME
    ).exists()


def test_mechanical_gate_checks_ttfa_and_selected_buffer(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    runner = FakeArmRunner(
        chatterbox_ttfa=1.30,
        chatterbox_underrun=1.26,
    )

    report, _ = gate.run_gate(
        artifact_dir=tmp_path / "artifacts",
        repository_root=repository,
        reviewer_count=2,
        allow_artifact_outside_results=True,
        process_runner=runner,
        source_environment={"PATH": "/usr/bin"},
        randomizer=random.Random(5),
    )

    conditions = report["mechanical_gate"]["conditions"]
    assert (
        conditions[
            "median_chatterbox_ttfa_at_most_1_25_seconds"
        ]
        is False
    )
    assert (
        conditions[
            "all_chatterbox_trials_fit_1_25_second_startup_buffer"
        ]
        is False
    )


def test_artifact_directory_cannot_be_reused(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    artifact = tmp_path / "artifacts"
    artifact.mkdir()

    with pytest.raises(FileExistsError):
        gate.run_gate(
            artifact_dir=artifact,
            repository_root=repository,
            reviewer_count=2,
            allow_artifact_outside_results=True,
            process_runner=FakeArmRunner(),
            source_environment={"PATH": "/usr/bin"},
            randomizer=random.Random(1),
        )


def test_artifact_directory_cannot_be_a_symlink(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    target = tmp_path / "target"
    target.mkdir()
    artifact = tmp_path / "artifact-link"
    artifact.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError):
        gate.run_gate(
            artifact_dir=artifact,
            repository_root=repository,
            reviewer_count=2,
            allow_artifact_outside_results=True,
            process_runner=FakeArmRunner(),
            source_environment={"PATH": "/usr/bin"},
            randomizer=random.Random(1),
        )


def test_nonzero_child_exit_is_not_copied_into_a_raw_error(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)

    def failed_runner(
        command: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> int:
        del command, environment, cwd, timeout_seconds
        return 17

    with pytest.raises(RuntimeError, match="subprocess failed"):
        gate.run_gate(
            artifact_dir=tmp_path / "artifacts",
            repository_root=repository,
            reviewer_count=2,
            allow_artifact_outside_results=True,
            process_runner=failed_runner,
            source_environment={
                "PATH": "/usr/bin",
                "SECRET": "do-not-copy",
            },
            randomizer=random.Random(1),
        )
