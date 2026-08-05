import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import private_pcm_schedule_ledger as ledger_module
from headless_playback_scheduler import HeadlessPlaybackScheduler
from private_pcm_schedule_ledger import (
    CAPTURE_KEYS,
    FRAME_KEYS,
    LEDGER_KEYS,
    PARENT_KEYS,
    PCM_KEYS,
    PRIVACY,
    SOURCE_CHUNK_KEYS,
    PrivatePcmScheduleCapture,
    PrivatePcmScheduleLedgerError,
    validate_private_pcm_schedule_ledger,
)


SOURCE_RATE = 1_000
TRANSLATED_RATE = 1_000
SAMPLE_ZERO = 0.5
INPUT_END = 2.6


def _frame_metadata(
    *,
    parent,
    frame,
    pcm,
    source_start,
    source_end,
    generation=7,
):
    return {
        "type": "audio_frame",
        "protocolVersion": 1,
        "streamGeneration": generation,
        "parentSequenceId": parent,
        "audioFrameId": frame,
        "audioBytes": len(pcm),
        "sampleRateHz": TRANSLATED_RATE,
        "channels": 1,
        "bytesPerSample": 2,
        "sourceStartMs": source_start,
        "sourceEndMs": source_end,
    }


def _completion_metadata(
    *,
    parent,
    frame_count,
    audio_bytes,
    source_start,
    source_end,
    generation=7,
):
    return {
        "type": "audio_parent_complete",
        "protocolVersion": 1,
        "streamGeneration": generation,
        "parentSequenceId": parent,
        "audioFrameCount": frame_count,
        "audioBytes": audio_bytes,
        "sourceStartMs": source_start,
        "sourceEndMs": source_end,
    }


def _scheduler_frame(
    *,
    parent,
    frame,
    arrival,
    pcm,
    source_start,
    source_end,
):
    return SimpleNamespace(
        arrival_seconds=arrival,
        audio_bytes=len(pcm),
        protocol_version=1,
        stream_generation=7,
        parent_sequence_id=parent,
        audio_frame_id=frame,
        sample_rate_hz=TRANSLATED_RATE,
        channels=1,
        bytes_per_sample=2,
        source_start_ms=source_start,
        source_end_ms=source_end,
    )


def _new_bound_capture(**limits):
    capture = PrivatePcmScheduleCapture(**limits)
    # Include recognizable bytes to verify they never appear in JSON.
    source_pcm = (b"PRIVATE-TEXT" * 334)[:4_000]
    if len(source_pcm) % 2:
        source_pcm += b"\x00"
    source_pcm = source_pcm.ljust(4_000, b"\x00")
    capture.bind_source_pcm(
        source_pcm,
        sample_rate_hz=SOURCE_RATE,
        channels=1,
        bytes_per_sample=2,
    )
    capture.record_source_anchor(SAMPLE_ZERO)
    capture.record_source_chunk(
        chunk_index=0,
        sample_start=0,
        sample_end_exclusive=1_000,
        audio_bytes=2_000,
        deadline_seconds=1.5,
        emitted_seconds=1.5,
    )
    capture.record_source_chunk(
        chunk_index=1,
        sample_start=1_000,
        sample_end_exclusive=2_000,
        audio_bytes=2_000,
        deadline_seconds=2.5,
        emitted_seconds=2.5,
    )
    return capture, source_pcm


def _complete_capture(capture_bundle=None):
    if capture_bundle is None:
        capture, source_pcm = _new_bound_capture()
    else:
        capture, source_pcm = capture_bundle
    scheduler = HeadlessPlaybackScheduler()
    translated_parts = []

    parent_zero_bytes = 0
    for frame_id, arrival, sample_value in (
        (0, 2.0, 1),
        (1, 2.05, 2),
    ):
        pcm = sample_value.to_bytes(2, "little") * 200
        translated_parts.append(pcm)
        parent_zero_bytes += len(pcm)
        frame = _scheduler_frame(
            parent=0,
            frame=frame_id,
            arrival=arrival,
            pcm=pcm,
            source_start=0.0,
            source_end=900.0,
        )
        decision = scheduler.accept(frame)
        capture.accept_frame(
            metadata=_frame_metadata(
                parent=0,
                frame=frame_id,
                pcm=pcm,
                source_start=0.0,
                source_end=900.0,
            ),
            pcm=pcm,
            schedule=decision,
        )
    capture.complete_parent(
        _completion_metadata(
            parent=0,
            frame_count=2,
            audio_bytes=parent_zero_bytes,
            source_start=0.0,
            source_end=900.0,
        ),
        received_seconds=2.06,
    )

    pcm = (3).to_bytes(2, "little") * 200
    translated_parts.append(pcm)
    frame = _scheduler_frame(
        parent=1,
        frame=0,
        arrival=2.1,
        pcm=pcm,
        source_start=1_000.0,
        source_end=1_900.0,
    )
    decision = scheduler.accept(frame)
    capture.accept_frame(
        metadata=_frame_metadata(
            parent=1,
            frame=0,
            pcm=pcm,
            source_start=1_000.0,
            source_end=1_900.0,
        ),
        pcm=pcm,
        schedule=decision,
    )
    capture.complete_parent(
        _completion_metadata(
            parent=1,
            frame_count=1,
            audio_bytes=len(pcm),
            source_start=1_000.0,
            source_end=1_900.0,
        ),
        received_seconds=2.11,
    )
    report = scheduler.finalize(
        input_end_seconds=INPUT_END,
        input_sample_zero_seconds=SAMPLE_ZERO,
    )
    return capture, source_pcm, b"".join(translated_parts), report


def _sealed_capture():
    capture, source_pcm, translated_pcm, report = _complete_capture()
    ledger = capture.seal(
        input_end_seconds=INPUT_END,
        terminal_completed=True,
        headless_report=report,
    )
    return capture, source_pcm, translated_pcm, report, ledger


def test_seal_and_write_private_artifacts_with_exact_schema_and_modes(
    tmp_path,
):
    capture, source_pcm, translated_pcm, _report, ledger = (
        _sealed_capture()
    )

    assert set(ledger) == set(LEDGER_KEYS)
    assert set(ledger["capture"]) == set(CAPTURE_KEYS)
    assert set(ledger["source_pcm"]) == set(PCM_KEYS)
    assert set(ledger["translated_pcm"]) == set(PCM_KEYS)
    assert all(
        set(item) == set(SOURCE_CHUNK_KEYS)
        for item in ledger["source_chunks"]
    )
    assert all(set(item) == set(FRAME_KEYS) for item in ledger["frames"])
    assert all(
        set(item) == set(PARENT_KEYS) for item in ledger["parents"]
    )
    assert ledger["privacy"] == PRIVACY
    assert ledger["capture"] == {
        "audio_metadata_protocol_version": 1,
        "stream_generation": 7,
        "terminal_completed": True,
        "input_pacing_mode": "chunk_end_boundary_v1",
        "clock": "client_monotonic_from_capture_start",
        "input_sample_zero_seconds": SAMPLE_ZERO,
        "input_end_seconds": INPUT_END,
        "source_chunk_count": 2,
        "parent_count": 2,
        "frame_count": 3,
        "canonical_replay_verified": True,
    }
    assert ledger["source_pcm"]["pcm_sha256"] == hashlib.sha256(
        source_pcm
    ).hexdigest()
    assert ledger["translated_pcm"]["pcm_sha256"] == hashlib.sha256(
        translated_pcm
    ).hexdigest()
    assert [frame["translated_sample_start"] for frame in ledger["frames"]] == [
        0,
        200,
        400,
    ]
    assert [
        frame["translated_sample_end_exclusive"]
        for frame in ledger["frames"]
    ] == [200, 400, 600]
    serialized = json.dumps(ledger)
    assert "PRIVATE-TEXT" not in serialized
    assert ".wav" not in serialized

    artifacts = capture.write_new(tmp_path / "private-evidence")

    assert artifacts.directory.stat().st_mode & 0o777 == 0o700
    assert artifacts.source_wav.stat().st_mode & 0o777 == 0o600
    assert artifacts.translated_wav.stat().st_mode & 0o777 == 0o600
    assert artifacts.ledger_json.stat().st_mode & 0o777 == 0o600
    assert artifacts.ledger_sha256 == hashlib.sha256(
        artifacts.ledger_json.read_bytes()
    ).hexdigest()
    assert artifacts.ledger_json.read_bytes().endswith(b"\n")
    assert (
        json.loads(artifacts.ledger_json.read_text(encoding="utf-8"))
        == ledger
    )
    assert hashlib.sha256(artifacts.source_wav.read_bytes()).hexdigest() == (
        ledger["source_pcm"]["review_wav_sha256"]
    )
    assert hashlib.sha256(
        artifacts.translated_wav.read_bytes()
    ).hexdigest() == ledger["translated_pcm"]["review_wav_sha256"]
    assert (
        validate_private_pcm_schedule_ledger(
            ledger,
            source_wav_bytes=artifacts.source_wav.read_bytes(),
            translated_wav_bytes=artifacts.translated_wav.read_bytes(),
        )
        == ledger
    )


def test_accept_frame_rejects_schedule_identity_before_retaining_pcm():
    capture, _source_pcm = _new_bound_capture()
    scheduler = HeadlessPlaybackScheduler()
    pcm = b"\x01\x00" * 200
    frame = _scheduler_frame(
        parent=0,
        frame=0,
        arrival=2.0,
        pcm=pcm,
        source_start=0.0,
        source_end=900.0,
    )
    decision = scheduler.accept(frame)

    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="schedule identity",
    ):
        capture.accept_frame(
            metadata=_frame_metadata(
                parent=0,
                frame=0,
                pcm=pcm,
                source_start=0.0,
                source_end=900.0,
            ),
            pcm=pcm,
            schedule=replace(decision, audio_frame_id=1),
        )

    capture.accept_frame(
        metadata=_frame_metadata(
            parent=0,
            frame=0,
            pcm=pcm,
            source_start=0.0,
            source_end=900.0,
        ),
        pcm=pcm,
        schedule=decision,
    )


def test_private_byte_and_count_limits_fail_closed():
    with pytest.raises(PrivatePcmScheduleLedgerError, match="source PCM"):
        capture = PrivatePcmScheduleCapture(max_source_pcm_bytes=2)
        capture.bind_source_pcm(
            b"\x00\x00" * 2,
            sample_rate_hz=1_000,
            channels=1,
            bytes_per_sample=2,
        )

    capture, _source_pcm = _new_bound_capture(
        max_translated_pcm_bytes=399,
    )
    scheduler = HeadlessPlaybackScheduler()
    pcm = b"\x01\x00" * 200
    decision = scheduler.accept(
        _scheduler_frame(
            parent=0,
            frame=0,
            arrival=2.0,
            pcm=pcm,
            source_start=0.0,
            source_end=900.0,
        )
    )
    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="translated PCM exceeds",
    ):
        capture.accept_frame(
            metadata=_frame_metadata(
                parent=0,
                frame=0,
                pcm=pcm,
                source_start=0.0,
                source_end=900.0,
            ),
            pcm=pcm,
            schedule=decision,
        )


def test_source_pacing_rejects_early_or_noncontiguous_chunks():
    capture = PrivatePcmScheduleCapture()
    capture.bind_source_pcm(
        b"\x00\x00" * 2_000,
        sample_rate_hz=1_000,
        channels=1,
        bytes_per_sample=2,
    )
    capture.record_source_anchor(SAMPLE_ZERO)

    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="early",
    ):
        capture.record_source_chunk(
            chunk_index=0,
            sample_start=0,
            sample_end_exclusive=1_000,
            audio_bytes=2_000,
            deadline_seconds=1.5,
            emitted_seconds=1.49,
        )

    capture.record_source_chunk(
        chunk_index=0,
        sample_start=0,
        sample_end_exclusive=1_000,
        audio_bytes=2_000,
        deadline_seconds=1.5,
        emitted_seconds=1.5,
    )
    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="bytes do not match",
    ):
        capture.record_source_chunk(
            chunk_index=1,
            sample_start=1_000,
            sample_end_exclusive=2_000,
            audio_bytes=1_998,
            deadline_seconds=2.5,
            emitted_seconds=2.5,
        )
    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="contiguous partition",
    ):
        capture.record_source_chunk(
            chunk_index=1,
            sample_start=999,
            sample_end_exclusive=2_000,
            audio_bytes=2_002,
            deadline_seconds=2.5,
            emitted_seconds=2.5,
        )


def test_source_pacing_accepts_partial_final_chunk_at_nominal_deadline():
    capture = PrivatePcmScheduleCapture()
    source_pcm = b"\x00\x00" * 1_500
    capture.bind_source_pcm(
        source_pcm,
        sample_rate_hz=SOURCE_RATE,
        channels=1,
        bytes_per_sample=2,
    )
    capture.record_source_anchor(SAMPLE_ZERO)
    capture.record_source_chunk(
        chunk_index=0,
        sample_start=0,
        sample_end_exclusive=1_000,
        audio_bytes=2_000,
        deadline_seconds=1.5,
        emitted_seconds=1.5,
    )
    capture.record_source_chunk(
        chunk_index=1,
        sample_start=1_000,
        sample_end_exclusive=1_500,
        audio_bytes=1_000,
        deadline_seconds=2.5,
        emitted_seconds=2.5,
    )

    completed, _source, _translated, _report = _complete_capture(
        (capture, source_pcm)
    )
    ledger = completed.seal(
        input_end_seconds=INPUT_END,
        terminal_completed=True,
        headless_report=_report,
    )

    assert ledger["source_chunks"][-1] == {
        "chunk_index": 1,
        "sample_start": 1_000,
        "sample_end_exclusive": 1_500,
        "audio_bytes": 1_000,
        "deadline_seconds": 2.5,
        "emitted_seconds": 2.5,
    }


def test_parent_completion_and_terminal_are_required():
    capture, _source_pcm = _new_bound_capture()
    scheduler = HeadlessPlaybackScheduler()
    pcm = b"\x01\x00" * 200
    decision = scheduler.accept(
        _scheduler_frame(
            parent=0,
            frame=0,
            arrival=2.0,
            pcm=pcm,
            source_start=0.0,
            source_end=900.0,
        )
    )
    capture.accept_frame(
        metadata=_frame_metadata(
            parent=0,
            frame=0,
            pcm=pcm,
            source_start=0.0,
            source_end=900.0,
        ),
        pcm=pcm,
        schedule=decision,
    )
    report = scheduler.finalize(
        input_end_seconds=INPUT_END,
        input_sample_zero_seconds=SAMPLE_ZERO,
    )

    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="completed terminal",
    ):
        capture.seal(
            input_end_seconds=INPUT_END,
            terminal_completed=False,
            headless_report=report,
        )
    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="parent evidence is incomplete",
    ):
        capture.seal(
            input_end_seconds=INPUT_END,
            terminal_completed=True,
            headless_report=report,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unexpected": 1}),
        lambda value: value["privacy"].update(
            {"contains_transcript_or_translation_text": True}
        ),
        lambda value: value["policy"].update({"urgent_rate": 1.2}),
        lambda value: value["frames"][0].update(
            {"scheduled_start_seconds": 2.01}
        ),
        lambda value: value["frames"][0].update({"pcm_sha256": "0" * 64}),
        lambda value: value["parents"][0].update({"audio_bytes": 2}),
        lambda value: value["source_chunks"][0].update(
            {"sample_end_exclusive": 999}
        ),
    ],
)
def test_validator_rejects_schema_hash_schedule_and_reconciliation_mutations(
    mutate,
):
    capture, _source, _translated, _report, ledger = _sealed_capture()
    changed = copy.deepcopy(ledger)
    mutate(changed)
    assert capture._source_wav_bytes is not None
    assert capture._translated_wav_bytes is not None

    with pytest.raises(PrivatePcmScheduleLedgerError):
        validate_private_pcm_schedule_ledger(
            changed,
            source_wav_bytes=capture._source_wav_bytes,
            translated_wav_bytes=capture._translated_wav_bytes,
        )


def test_validator_rejects_mutated_wav_bytes():
    capture, _source, _translated, _report, ledger = _sealed_capture()
    artifacts_dir = None
    # Accessing sealed bytes through installation keeps the public test on the
    # same contract an analyzer will use.
    assert capture._source_wav_bytes is not None
    assert capture._translated_wav_bytes is not None
    changed = bytearray(capture._translated_wav_bytes)
    changed[-1] ^= 0xFF

    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="WAV hash",
    ):
        validate_private_pcm_schedule_ledger(
            ledger,
            source_wav_bytes=capture._source_wav_bytes,
            translated_wav_bytes=bytes(changed),
        )
    assert artifacts_dir is None


def test_write_refuses_existing_directory_and_symlink(tmp_path):
    capture, *_rest = _sealed_capture()
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="refusing"):
        capture.write_new(existing)

    capture, *_rest = _sealed_capture()
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "link"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(
        PrivatePcmScheduleLedgerError,
        match="symbolic link",
    ):
        capture.write_new(symlink)


def test_failed_install_removes_partial_artifact_set(
    monkeypatch,
    tmp_path,
):
    capture, *_rest = _sealed_capture()
    real_write = ledger_module._write_private_bytes_atomic
    calls = 0

    def fail_second(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated private write failure")
        real_write(path, payload)

    monkeypatch.setattr(
        ledger_module,
        "_write_private_bytes_atomic",
        fail_second,
    )
    destination = tmp_path / "failed"
    with pytest.raises(OSError, match="simulated"):
        capture.write_new(destination)

    assert not destination.exists()


def test_failed_install_after_link_removes_destination(
    monkeypatch,
    tmp_path,
):
    capture, *_rest = _sealed_capture()
    real_chmod = ledger_module.os.chmod

    def fail_private_destination_chmod(path, mode):
        if path.name == ledger_module.SOURCE_WAV_FILENAME:
            raise OSError("simulated post-link failure")
        real_chmod(path, mode)

    monkeypatch.setattr(
        ledger_module.os,
        "chmod",
        fail_private_destination_chmod,
    )
    destination = tmp_path / "post-link-failed"

    with pytest.raises(OSError, match="post-link"):
        capture.write_new(destination)

    assert not destination.exists()


def test_abort_discards_state_and_prevents_artifacts(tmp_path):
    capture, _source_pcm = _new_bound_capture()

    capture.abort()

    with pytest.raises(PrivatePcmScheduleLedgerError, match="aborted"):
        capture.record_source_anchor(1.0)
    with pytest.raises(PrivatePcmScheduleLedgerError, match="aborted"):
        capture.write_new(tmp_path / "private")
