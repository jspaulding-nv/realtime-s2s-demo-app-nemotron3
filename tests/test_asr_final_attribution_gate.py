import hashlib
import json
import struct
import wave

import asr_final_attribution_gate as gate_module
import pytest
from asr_final_attribution_gate import (
    RealtimeWaveChunks,
    attest_local_asr_runtime,
    build_parser,
    build_report,
)

RUNTIME_ATTESTATION = {
    "schema_version": 1,
    "verified": True,
    "health": "healthy",
    "host_port": 50052,
    "container_port": 50052,
    "local_image_id": "sha256:" + "cd" * 32,
    "repository_digest": "sha256:" + "ef" * 32,
}
ATTEMPT_ID = "12" * 16
ATTEMPT_STARTED_AT_UTC = "2026-07-25T00:00:00+00:00"


def write_pcm_wave(path, samples=(1, 2, 3, 4, 5)):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def riff_chunk(chunk_id, payload):
    return (
        chunk_id
        + struct.pack("<I", len(payload))
        + payload
        + (b"\x00" if len(payload) & 1 else b"")
    )


def successful_run(run_number):
    return {
        "run_number": run_number,
        "input_completed": True,
        "realtime_pacing": True,
        "passed": True,
        "padded_pcm_sha256": "ab" * 32,
        "padded_pcm_sample_count": 30211200,
        "attribution": {
            "nonempty_final_count": 2,
            "final_missing_word_offsets_count": 0,
            "finals": [
                {
                    "final_id": 0,
                    "text_chars": 20,
                    "word_count": 4,
                    "audio_processed_s": 1.25,
                    "first_word_start_ms": 100,
                    "last_word_end_ms": 900,
                    "timing_basis": "word_offsets",
                }
            ],
        },
    }


def test_parser_defaults_to_two_realtime_long_form_runs():
    args = build_parser().parse_args(
        ["--docker-container", "local-asr"]
    )

    assert args.runs == 2
    assert args.file.name == "long-form-03-30min.wav"
    assert not hasattr(args, "fast")


def test_report_requires_two_independently_successful_runs(tmp_path):
    audio = tmp_path / "long-form.wav"
    audio.write_bytes(b"pcm")

    one_run = build_report(
        audio_path=audio,
        uri="localhost:50052",
        runs=[successful_run(1)],
        runtime_attestation=RUNTIME_ATTESTATION,
        requested_run_count=2,
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )
    two_runs = build_report(
        audio_path=audio,
        uri="localhost:50052",
        runs=[successful_run(1), successful_run(2)],
        runtime_attestation=RUNTIME_ATTESTATION,
        requested_run_count=2,
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )

    assert one_run["passed"] is False
    assert two_runs["passed"] is True
    assert two_runs["requirements"]["missing_word_offsets_allowed_per_run"] == 0
    assert two_runs["input"]["exact_padded_pcm_binding_verified"] is True

    unattested = build_report(
        audio_path=audio,
        uri="localhost:50052",
        runs=[successful_run(1), successful_run(2)],
        runtime_attestation={"verified": False},
        requested_run_count=2,
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )
    assert unattested["passed"] is False

    incomplete_requested_set = build_report(
        audio_path=audio,
        uri="localhost:50052",
        runs=[successful_run(1), successful_run(2)],
        runtime_attestation=RUNTIME_ATTESTATION,
        requested_run_count=3,
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )
    assert incomplete_requested_set["passed"] is False
    assert (
        incomplete_requested_set["requirements"]["requested_realtime_runs"]
        == 3
    )


def test_report_contains_no_transcript_text(tmp_path):
    audio = tmp_path / "long-form.wav"
    audio.write_bytes(b"pcm")

    report = build_report(
        audio_path=audio,
        uri="localhost:50052",
        runs=[successful_run(1), successful_run(2)],
        runtime_attestation=RUNTIME_ATTESTATION,
        requested_run_count=2,
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )
    encoded = json.dumps(report)

    assert "transcript" not in encoded
    assert '"text"' not in encoded
    assert "localhost:50052" not in encoded
    assert '"file_name"' not in encoded
    assert '"image"' not in encoded
    assert '"profile"' not in encoded
    assert report["asr"]["runtime_attestation"]["verified"] is True


def test_realtime_chunks_pad_and_release_at_chunk_end(
    tmp_path,
    monkeypatch,
):
    audio = tmp_path / "short.wav"
    samples = (1, 2, 3, 4, 5)
    write_pcm_wave(audio, samples)

    monkeypatch.setattr(gate_module.audio_config, "chunk_size", 4)
    now = [100.0]
    sleeps = []

    def fake_monotonic():
        return now[0]

    def fake_sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(gate_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gate_module.time, "sleep", fake_sleep)

    chunks = RealtimeWaveChunks(
        audio,
        run_number=1,
        progress_interval_s=60,
    )
    emitted = list(chunks)
    wire_pcm = b"".join(emitted)

    assert [len(chunk) for chunk in emitted] == [8, 8]
    assert sleeps == pytest.approx([0.00025, 0.00025])
    assert chunks.source_frames == 5
    assert chunks.expected_frames == 8
    assert chunks.frames_sent == 8
    assert chunks.chunk_release_count == 2
    assert chunks.maximum_release_lateness_ms == pytest.approx(0)
    assert chunks.mean_release_lateness_ms == pytest.approx(0)
    assert chunks.realtime_pacing_within_limit is True
    assert wire_pcm == struct.pack("<5h", *samples) + b"\x00" * 6
    assert chunks.padded_pcm_sha256 == hashlib.sha256(wire_pcm).hexdigest()


def test_realtime_chunks_record_and_reject_excessive_release_lateness(
    tmp_path,
    monkeypatch,
):
    audio = tmp_path / "late.wav"
    write_pcm_wave(audio)
    monkeypatch.setattr(gate_module.audio_config, "chunk_size", 4)
    now = [100.0]

    monkeypatch.setattr(gate_module.time, "monotonic", lambda: now[0])

    def oversleep(delay):
        now[0] += delay + 0.3

    monkeypatch.setattr(gate_module.time, "sleep", oversleep)
    chunks = RealtimeWaveChunks(
        audio,
        run_number=1,
        progress_interval_s=60,
    )

    list(chunks)

    assert chunks.maximum_release_lateness_ms > 299
    assert chunks.realtime_pacing_within_limit is False


def test_realtime_chunks_reject_nonexact_riff_and_duplicate_data(tmp_path):
    audio = tmp_path / "ambiguous.wav"
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    body = (
        b"WAVE"
        + riff_chunk(b"fmt ", fmt)
        + riff_chunk(b"data", b"\x01\x00")
        + riff_chunk(b"data", b"\x02\x00")
    )
    audio.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)

    with pytest.raises(ValueError, match="one data chunk"):
        list(
            RealtimeWaveChunks(
                audio,
                run_number=1,
                progress_interval_s=60,
            )
        )

    valid = tmp_path / "trailing.wav"
    write_pcm_wave(valid)
    valid.write_bytes(valid.read_bytes() + b"\x00")
    with pytest.raises(ValueError, match="RIFF length"):
        list(
            RealtimeWaveChunks(
                valid,
                run_number=1,
                progress_interval_s=60,
            )
        )


def test_registered_long_form_pcm_binding_is_stable():
    audio = gate_module.ROOT / "test_audio" / "long-form-03-30min.wav"
    pcm = gate_module._extract_exact_mono_pcm16_wav(
        audio.read_bytes(),
        target_sample_rate=16000,
    )
    source_samples = len(pcm) // 2
    chunk_size = gate_module.audio_config.chunk_size
    padded_samples = (
        (source_samples + chunk_size - 1) // chunk_size
    ) * chunk_size
    digest = hashlib.sha256()
    digest.update(pcm)
    digest.update(b"\x00" * ((padded_samples - source_samples) * 2))

    assert source_samples == 30_209_672
    assert padded_samples == 30_211_200
    assert digest.hexdigest() == (
        "9ad08fe1e83e714c48dde4f971606431"
        "302fae9d3079293b73ab8ff99d1d8143"
    )


@pytest.mark.parametrize(
    ("host_ip", "should_verify"),
    [
        ("127.0.0.1", True),
        ("0.0.0.0", True),
        ("192.0.2.10", False),
    ],
)
def test_runtime_attestation_verifies_healthy_bound_image(
    monkeypatch,
    host_ip,
    should_verify,
):
    expected_digest = "sha256:" + "ef" * 32
    image_id = "sha256:" + "cd" * 32
    monkeypatch.setattr(
        gate_module.riva_config,
        "asr_image",
        "registry.example/asr:1.2.0",
    )
    monkeypatch.setattr(
        gate_module.riva_config,
        "asr_image_digest",
        expected_digest,
    )
    monkeypatch.setattr(
        gate_module.riva_config,
        "asr_profile",
        gate_module.REGISTERED_ASR_PROFILE,
    )

    class Result:
        def __init__(self, document):
            self.stdout = json.dumps([document])

    def fake_run(arguments, **_kwargs):
        if arguments[:2] == ["docker", "inspect"]:
            return Result(
                {
                    "Config": {
                        "Image": "registry.example/asr:1.2.0",
                        "Env": [
                            "UNRELATED=value",
                            (
                                "NIM_TAGS_SELECTOR="
                                "name=nemotron-asr-streaming,type=en-US,"
                                "batch_size=32"
                            ),
                        ],
                    },
                    "Image": image_id,
                    "State": {
                        "Running": True,
                        "Health": {"Status": "healthy"},
                    },
                    "NetworkSettings": {
                        "Ports": {
                            "50052/tcp": [
                                {
                                    "HostIp": host_ip,
                                    "HostPort": "50052",
                                }
                            ]
                        }
                    },
                }
            )
        return Result(
            {
                "Id": image_id,
                "RepoDigests": [
                    f"registry.example/asr@{expected_digest}",
                ],
            }
        )

    monkeypatch.setattr(gate_module.subprocess, "run", fake_run)

    if not should_verify:
        with pytest.raises(RuntimeError, match="declared healthy image"):
            attest_local_asr_runtime(
                container_name="private-local-name",
                uri="127.0.0.1:50052",
            )
        return

    attestation = attest_local_asr_runtime(
        container_name="private-local-name",
        uri="127.0.0.1:50052",
    )

    assert attestation["verified"] is True
    assert attestation["repository_digest"] == expected_digest
    assert attestation["local_image_id"] == image_id
    assert (
        attestation["profile_selector_sha256"]
        == gate_module.REGISTERED_ASR_PROFILE_SHA256
    )
    assert "private-local-name" not in json.dumps(attestation)


def test_runtime_attestation_rejects_ambiguous_localhost():
    with pytest.raises(RuntimeError, match="literal 127.0.0.1"):
        attest_local_asr_runtime(
            container_name="private-local-name",
            uri="localhost:50052",
        )


@pytest.mark.parametrize(
    "profile_entries",
    [
        [],
        ["NIM_TAGS_SELECTOR=name=other-profile"],
        [
            (
                "NIM_TAGS_SELECTOR="
                "name=nemotron-asr-streaming,type=en-US,batch_size=32"
            ),
            (
                "NIM_TAGS_SELECTOR="
                "name=nemotron-asr-streaming,type=en-US,batch_size=32"
            ),
        ],
    ],
)
def test_runtime_attestation_rejects_unbound_profile(
    monkeypatch,
    profile_entries,
):
    expected_digest = "sha256:" + "ef" * 32
    image_id = "sha256:" + "cd" * 32
    image_name = "registry.example/asr:1.2.0"
    monkeypatch.setattr(gate_module.riva_config, "asr_image", image_name)
    monkeypatch.setattr(
        gate_module.riva_config,
        "asr_image_digest",
        expected_digest,
    )
    monkeypatch.setattr(
        gate_module.riva_config,
        "asr_profile",
        gate_module.REGISTERED_ASR_PROFILE,
    )

    class Result:
        def __init__(self, document):
            self.stdout = json.dumps([document])

    def fake_run(arguments, **_kwargs):
        if arguments[:2] == ["docker", "inspect"]:
            return Result(
                {
                    "Config": {
                        "Image": image_name,
                        "Env": profile_entries,
                    },
                    "Image": image_id,
                    "State": {
                        "Running": True,
                        "Health": {"Status": "healthy"},
                    },
                    "NetworkSettings": {
                        "Ports": {
                            "50052/tcp": [
                                {
                                    "HostIp": "127.0.0.1",
                                    "HostPort": "50052",
                                }
                            ],
                        }
                    },
                }
            )
        return Result(
            {
                "Id": image_id,
                "RepoDigests": [f"registry.example/asr@{expected_digest}"],
            }
        )

    monkeypatch.setattr(gate_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="declared healthy image"):
        attest_local_asr_runtime(
            container_name="private-local-name",
            uri="127.0.0.1:50052",
        )


def test_main_invalidates_stale_report_before_attestation(
    tmp_path,
    monkeypatch,
):
    audio = tmp_path / "input.wav"
    output = tmp_path / "qualification.json"
    write_pcm_wave(audio)
    output.write_text('{"passed": true, "stale": true}\n', encoding="utf-8")
    monkeypatch.setattr(
        gate_module.riva_config,
        "asr_word_time_offsets",
        True,
    )

    def reject_attestation(**_kwargs):
        raise RuntimeError("not attested")

    monkeypatch.setattr(
        gate_module,
        "attest_local_asr_runtime",
        reject_attestation,
    )

    exit_code = gate_module.main(
        [
            "--file",
            str(audio),
            "--uri",
            "127.0.0.1:50052",
            "--docker-container",
            "private-local-name",
            "--json-output",
            str(output),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert report["passed"] is False
    assert report["runs"] == []
    assert report["attempt_id"] != ATTEMPT_ID
    assert "stale" not in report


def test_main_leaves_current_zero_run_checkpoint_on_interruption(
    tmp_path,
    monkeypatch,
):
    audio = tmp_path / "input.wav"
    output = tmp_path / "qualification.json"
    write_pcm_wave(audio)
    output.write_text('{"passed": true, "stale": true}\n', encoding="utf-8")
    monkeypatch.setattr(
        gate_module.riva_config,
        "asr_word_time_offsets",
        True,
    )
    monkeypatch.setattr(
        gate_module,
        "attest_local_asr_runtime",
        lambda **_kwargs: RUNTIME_ATTESTATION,
    )

    def interrupt_run(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(gate_module, "run_once", interrupt_run)

    with pytest.raises(KeyboardInterrupt):
        gate_module.main(
            [
                "--file",
                str(audio),
                "--uri",
                "127.0.0.1:50052",
                "--docker-container",
                "private-local-name",
                "--json-output",
                str(output),
            ]
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["runs"] == []
    assert report["asr"]["runtime_attestation"]["verified"] is True
    assert "stale" not in report


def test_main_never_overwrites_input_with_report(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    original = audio.read_bytes()

    exit_code = gate_module.main(
        [
            "--file",
            str(audio),
            "--docker-container",
            "private-local-name",
            "--json-output",
            str(audio),
        ]
    )

    assert exit_code == 2
    assert audio.read_bytes() == original
