import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

import batch_latency_test as batch


def _audio_frame(*, audio_bytes: int = 3200) -> dict:
    return {
        "type": "audio_frame",
        "protocolVersion": 1,
        "streamGeneration": 1,
        "parentSequenceId": 0,
        "audioFrameId": 0,
        "audioBytes": audio_bytes,
        "sampleRateHz": 16000,
        "channels": 1,
        "bytesPerSample": 2,
        "sourceStartMs": 0.0,
        "sourceEndMs": 10.0,
    }


def _parent_complete(*, audio_bytes: int = 3200) -> dict:
    return {
        "type": "audio_parent_complete",
        "protocolVersion": 1,
        "streamGeneration": 1,
        "parentSequenceId": 0,
        "audioFrameCount": 1,
        "audioBytes": audio_bytes,
        "sourceStartMs": 0.0,
        "sourceEndMs": 10.0,
    }


class _FakePcm:
    def __len__(self) -> int:
        return 4800

    def tobytes(self) -> bytes:
        return b"\0" * 9600


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeWebSocket:
    def __init__(self, *, wrong_binary_size: bool = False):
        self.responses = [
            json.dumps({"type": "status", "status": "connected"}),
            json.dumps({"type": "status", "status": "listening"}),
            json.dumps(_audio_frame()),
            b"\x21" * (1600 if wrong_binary_size else 3200),
            json.dumps(_parent_complete()),
        ]
        self.end_input = asyncio.Event()
        self.terminal_sent = False

    async def recv(self):
        if self.responses:
            return self.responses.pop(0)
        await self.end_input.wait()
        if not self.terminal_sent:
            self.terminal_sent = True
            return json.dumps({"type": "status", "status": "completed"})
        await asyncio.Future()

    async def send(self, payload) -> None:
        if isinstance(payload, bytes):
            return
        message = json.loads(payload)
        if message["type"] == "end_input":
            self.end_input.set()


class _FakeConnection:
    def __init__(self, websocket: _FakeWebSocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *_args):
        return None


class _RecordingPrivateCapture:
    """Test double for the deliberately narrow private-capture protocol."""

    def __init__(self, *_args, **_kwargs):
        self.source_bindings = []
        self.source_anchors = []
        self.source_chunks = []
        self.frames = []
        self.parents = []
        self.seals = []
        self.write_paths = []
        self.abort_count = 0

    def bind_source_pcm(
        self,
        pcm_bytes,
        *,
        sample_rate_hz,
        channels,
        bytes_per_sample,
    ) -> None:
        self.source_bindings.append(
            {
                "pcm_bytes": pcm_bytes,
                "sample_rate_hz": sample_rate_hz,
                "channels": channels,
                "bytes_per_sample": bytes_per_sample,
            }
        )

    def record_source_anchor(self, sample_zero_seconds) -> None:
        self.source_anchors.append(sample_zero_seconds)

    def record_source_chunk(self, **values) -> None:
        self.source_chunks.append(values)

    def accept_frame(self, *, metadata, pcm, schedule) -> None:
        self.frames.append(
            {
                "metadata": metadata,
                "pcm": pcm,
                "schedule": schedule,
            }
        )

    def complete_parent(self, metadata, *, received_seconds) -> None:
        self.parents.append(
            {
                "metadata": metadata,
                "received_seconds": received_seconds,
            }
        )

    def seal(
        self,
        *,
        input_end_seconds,
        terminal_completed,
        headless_report,
    ) -> None:
        self.seals.append(
            {
                "input_end_seconds": input_end_seconds,
                "terminal_completed": terminal_completed,
                "headless_report": headless_report,
            }
        )

    def write_new(self, path: Path) -> None:
        destination = Path(path)
        self.write_paths.append(destination)
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for filename in (
            "source-review.wav",
            "translated-review.wav",
            "schedule-ledger.json",
        ):
            artifact = destination / filename
            artifact.write_bytes(b"private-test-artifact")
            artifact.chmod(0o600)

    def abort(self) -> None:
        self.abort_count += 1


def _install_live_run_fakes(
    monkeypatch,
    websocket: _FakeWebSocket,
) -> None:
    config = {
        "pipelineMode": "staged",
        "sampleRate": 16000,
        "channels": 1,
        "audioMetadataProtocolVersions": [1],
        "modelConfig": {"nmt": {"targetLanguage": "es-US"}},
        "stagedConfig": {
            "telemetrySchemaVersion": 3,
            "ttsIncrementalPublishEnabled": True,
        },
    }
    monkeypatch.setattr(batch, "decode_audio", lambda _path: _FakePcm())
    monkeypatch.setattr(
        batch,
        "fetch_backend_config",
        lambda url: (
            config,
            "staged",
            "api_config",
            f"{url}/api/config",
        ),
    )
    monkeypatch.setattr(
        batch.websockets,
        "connect",
        lambda *_args, **_kwargs: _FakeConnection(websocket),
    )
    monkeypatch.setattr(batch, "CHUNK_DURATION", 0.001)
    monkeypatch.setattr(batch, "TERMINAL_SETTLE_SECONDS", 0)
    monkeypatch.setattr(
        batch.requests,
        "post",
        lambda *_args, **_kwargs: _FakeResponse(),
    )

    async def fake_export(*_args, **_kwargs):
        return {
            "events": [],
            "stagedPipeline": {
                "websocket_send_events": [
                    {
                        "parent_sequence_id": 0,
                        "audio_frame_id": 0,
                        "audio_bytes": 3200,
                    }
                ],
                "websocket_completed_parent_summaries": [
                    {
                        "parent_sequence_id": 0,
                        "audio_frame_count": 1,
                        "audio_bytes": 3200,
                    }
                ],
            },
        }

    monkeypatch.setattr(batch, "fetch_backend_export", fake_export)
    monkeypatch.setattr(
        batch,
        "validate_staged_pipeline_integrity",
        lambda *_args, **_kwargs: [],
    )


def _minimal_result(audio: Path) -> batch.TestResult:
    return batch.TestResult(
        audio_path=str(audio),
        duration_sec=1.0,
        chunks_sent=1,
        audio_responses=1,
        total_received_bytes=3200,
        input_completed=True,
        translation_completed=True,
    )


def _disable_regular_artifact_writes(monkeypatch) -> None:
    monkeypatch.setattr(batch, "generate_plot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(batch, "generate_csv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        batch,
        "generate_summary",
        lambda *_args, **_kwargs: None,
    )


def test_run_test_private_capture_requires_protocol_v1(monkeypatch):
    capture = _RecordingPrivateCapture()
    monkeypatch.setattr(
        batch,
        "PrivatePcmScheduleCapture",
        _RecordingPrivateCapture,
    )

    with pytest.raises(ValueError, match=r"(?i)protocol.*1"):
        asyncio.run(
            batch.run_test(
                "synthetic.wav",
                "http://backend",
                private_semantic_capture=capture,
            )
        )

    assert capture.source_bindings == []
    assert capture.frames == []
    assert capture.write_paths == []


def test_run_test_forwards_pcm_only_after_protocol_validation_and_keeps_generic_sink_metadata_only(
    monkeypatch,
):
    websocket = _FakeWebSocket()
    _install_live_run_fakes(monkeypatch, websocket)
    capture = _RecordingPrivateCapture()
    generic_frames = []
    monkeypatch.setattr(
        batch,
        "PrivatePcmScheduleCapture",
        _RecordingPrivateCapture,
    )

    result = asyncio.run(
        batch.run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
            audio_frame_sink=generic_frames.append,
            private_semantic_capture=capture,
        )
    )

    assert result.translation_completed is True
    assert capture.abort_count == 0
    assert capture.source_bindings == [
        {
            "pcm_bytes": b"\0" * 9600,
            "sample_rate_hz": 16000,
            "channels": 1,
            "bytes_per_sample": 2,
        }
    ]
    assert len(capture.source_anchors) == 1
    assert len(capture.source_chunks) == 1
    assert capture.source_chunks[0]["chunk_index"] == 0
    assert capture.source_chunks[0]["sample_start"] == 0
    assert capture.source_chunks[0]["sample_end_exclusive"] == 4800
    assert capture.source_chunks[0]["audio_bytes"] == 9600

    assert len(capture.frames) == 1
    private_frame = capture.frames[0]
    assert private_frame["pcm"] == b"\x21" * 3200
    assert private_frame["metadata"] == _audio_frame()
    assert private_frame["schedule"].parent_sequence_id == 0
    assert private_frame["schedule"].audio_frame_id == 0
    assert private_frame["schedule"].start_seconds >= 0
    assert private_frame["schedule"].end_seconds > (
        private_frame["schedule"].start_seconds
    )

    assert len(generic_frames) == 1
    generic_frame = generic_frames[0]
    assert isinstance(generic_frame, batch.ValidatedAudioFrame)
    assert generic_frame.audio_bytes == 3200
    assert not hasattr(generic_frame, "pcm")
    assert not hasattr(generic_frame, "metadata")

    assert len(capture.parents) == 1
    assert capture.parents[0]["metadata"] == _parent_complete()
    assert capture.parents[0]["received_seconds"] >= 0
    assert len(capture.seals) == 1
    assert capture.seals[0]["terminal_completed"] is True
    assert capture.seals[0]["headless_report"] == (
        result.headless_playback_report
    )


def test_run_test_seals_real_private_capture_and_publishes_bound_artifacts(
    monkeypatch,
    tmp_path,
):
    websocket = _FakeWebSocket()
    _install_live_run_fakes(monkeypatch, websocket)
    # Preserve the real 4,800-sample / 300 ms source cadence required by the
    # private ledger while retaining the fake terminal's zero settle time.
    monkeypatch.setattr(batch, "CHUNK_DURATION", 0.3)
    capture = batch.PrivatePcmScheduleCapture()

    result = asyncio.run(
        batch.run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
            private_semantic_capture=capture,
        )
    )
    artifacts = capture.write_new(tmp_path / "private-evidence")
    ledger = json.loads(
        artifacts.ledger_json.read_text(encoding="utf-8")
    )

    assert result.translation_completed is True
    assert ledger["capture"]["terminal_completed"] is True
    assert ledger["capture"]["source_chunk_count"] == 1
    assert ledger["capture"]["frame_count"] == 1
    assert ledger["capture"]["parent_count"] == 1
    assert ledger["source_pcm"]["audio_bytes"] == 9600
    assert ledger["translated_pcm"]["audio_bytes"] == 3200
    assert artifacts.directory.stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in (
            artifacts.source_wav,
            artifacts.translated_wav,
            artifacts.ledger_json,
        )
    )


def test_invalid_binary_is_never_forwarded_and_private_state_is_aborted(
    monkeypatch,
):
    websocket = _FakeWebSocket(wrong_binary_size=True)
    _install_live_run_fakes(monkeypatch, websocket)
    capture = _RecordingPrivateCapture()
    monkeypatch.setattr(
        batch,
        "PrivatePcmScheduleCapture",
        _RecordingPrivateCapture,
    )

    result = asyncio.run(
        batch.run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
            private_semantic_capture=capture,
        )
    )

    assert result.translation_completed is False
    assert result.server_error.startswith(
        "audio metadata protocol violation:"
    )
    assert capture.frames == []
    assert capture.parents == []
    assert capture.seals == []
    assert capture.abort_count == 1
    assert capture.write_paths == []


def test_run_batch_default_off_does_not_construct_or_publish_private_capture(
    monkeypatch,
    tmp_path,
):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    private_path = tmp_path / "must-not-exist"
    observed_kwargs = []

    class UnexpectedPrivateCapture:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("default run constructed a private PCM capture")

    async def fake_run_test(_audio_path, _backend_url, **kwargs):
        observed_kwargs.append(kwargs)
        return _minimal_result(audio)

    monkeypatch.setattr(
        batch,
        "PrivatePcmScheduleCapture",
        UnexpectedPrivateCapture,
        raising=False,
    )
    monkeypatch.setattr(batch, "run_test", fake_run_test)
    monkeypatch.setattr(batch, "validate_capture_result", lambda _result: [])
    _disable_regular_artifact_writes(monkeypatch)

    ok = asyncio.run(
        batch.run_batch(
            [str(audio)],
            "http://backend",
            str(tmp_path / "regular-results"),
            audio_metadata_protocol_version=1,
        )
    )

    assert ok is True
    assert len(observed_kwargs) == 1
    assert observed_kwargs[0].get("private_semantic_capture") is None
    assert not private_path.exists()


def test_run_batch_private_capture_requires_protocol_v1(
    tmp_path,
):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    private_path = tmp_path / "private"

    with pytest.raises(ValueError, match=r"(?i)protocol.*1"):
        asyncio.run(
            batch.run_batch(
                [str(audio)],
                "http://backend",
                str(tmp_path / "regular-results"),
                private_semantic_capture_dir=private_path,
            )
        )

    assert not private_path.exists()


def test_run_batch_rejects_private_capture_outside_ignored_root(
    tmp_path,
):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    private_path = tmp_path / "private"

    with pytest.raises(
        ValueError,
        match=r"(?i)child of.*experiment_results",
    ):
        asyncio.run(
            batch.run_batch(
                [str(audio)],
                "http://backend",
                str(tmp_path / "regular-results"),
                audio_metadata_protocol_version=1,
                private_semantic_capture_dir=private_path,
            )
        )

    assert not private_path.exists()


@pytest.mark.parametrize(
    ("arguments", "message_pattern"),
    [
        (
            [
                "--file",
                "input.wav",
                "--private-semantic-capture-dir",
                "private",
            ],
            r"(?i)private-semantic-capture-dir.*protocol",
        ),
        (
            [
                "--audio-metadata-protocol-v1",
                "--private-semantic-capture-dir",
                "private",
            ],
            r"(?i)private-semantic-capture-dir.*file",
        ),
        (
            [
                "--preflight",
                "--audio-metadata-protocol-v1",
                "--private-semantic-capture-dir",
                "private",
            ],
            r"(?i)private-semantic-capture-dir.*file",
        ),
    ],
)
def test_private_capture_cli_is_protocol_v1_single_file_only(
    monkeypatch,
    capsys,
    arguments,
    message_pattern,
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["batch_latency_test.py", *arguments],
    )
    monkeypatch.setattr(
        batch,
        "check_backend",
        lambda _url: pytest.fail(
            "invalid private CLI reached backend readiness"
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        batch.main()

    assert exc_info.value.code == 2
    assert re.search(message_pattern, capsys.readouterr().err)


def test_private_capture_cli_passes_path_to_single_file_batch(
    monkeypatch,
    tmp_path,
):
    audio = tmp_path / "input.wav"
    private_path = (
        Path(batch.__file__).resolve().parent
        / "experiment_results"
        / f"private-cli-{tmp_path.name}"
    )
    assert not private_path.exists()
    observed = {}

    async def fake_run_batch(
        files,
        backend_url,
        output_dir,
        **kwargs,
    ):
        observed.update(
            {
                "files": files,
                "backend_url": backend_url,
                "output_dir": output_dir,
                "kwargs": kwargs,
            }
        )
        return True

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "batch_latency_test.py",
            "--file",
            str(audio),
            "--audio-metadata-protocol-v1",
            "--private-semantic-capture-dir",
            str(private_path),
        ],
    )
    monkeypatch.setattr(batch, "check_backend", lambda _url: True)
    monkeypatch.setattr(batch, "run_batch", fake_run_batch)

    with pytest.raises(SystemExit) as exc_info:
        batch.main()

    assert exc_info.value.code == 0
    assert observed["files"] == [str(audio)]
    assert observed["kwargs"]["audio_metadata_protocol_version"] == 1
    assert observed["kwargs"]["private_semantic_capture_dir"] == private_path


def test_invalid_batch_capture_aborts_without_private_publication(
    monkeypatch,
    tmp_path,
):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    private_path = tmp_path / "private"
    captures = []

    class Capture(_RecordingPrivateCapture):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            captures.append(self)

    async def fake_run_test(
        _audio_path,
        _backend_url,
        **kwargs,
    ):
        assert kwargs["private_semantic_capture"] is captures[0]
        return _minimal_result(audio)

    monkeypatch.setattr(
        batch,
        "PrivatePcmScheduleCapture",
        Capture,
        raising=False,
    )
    monkeypatch.setattr(
        batch,
        "_resolve_private_semantic_capture_dir",
        lambda path: path,
    )
    monkeypatch.setattr(batch, "run_test", fake_run_test)
    monkeypatch.setattr(
        batch,
        "validate_capture_result",
        lambda _result: ["synthetic invalid capture"],
    )
    _disable_regular_artifact_writes(monkeypatch)

    ok = asyncio.run(
        batch.run_batch(
            [str(audio)],
            "http://backend",
            str(tmp_path / "regular-results"),
            audio_metadata_protocol_version=1,
            private_semantic_capture_dir=private_path,
        )
    )

    assert ok is False
    assert len(captures) == 1
    assert captures[0].abort_count == 1
    assert captures[0].write_paths == []
    assert not (private_path / "source-review.wav").exists()
    assert not (private_path / "translated-review.wav").exists()
    assert not (private_path / "schedule-ledger.json").exists()


def test_run_test_failure_aborts_without_private_publication(
    monkeypatch,
    tmp_path,
):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    private_path = tmp_path / "private"
    captures = []

    class Capture(_RecordingPrivateCapture):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            captures.append(self)

    async def fail_run_test(
        _audio_path,
        _backend_url,
        **kwargs,
    ):
        assert kwargs["private_semantic_capture"] is captures[0]
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr(
        batch,
        "PrivatePcmScheduleCapture",
        Capture,
        raising=False,
    )
    monkeypatch.setattr(
        batch,
        "_resolve_private_semantic_capture_dir",
        lambda path: path,
    )
    monkeypatch.setattr(batch, "run_test", fail_run_test)
    _disable_regular_artifact_writes(monkeypatch)

    ok = asyncio.run(
        batch.run_batch(
            [str(audio)],
            "http://backend",
            str(tmp_path / "regular-results"),
            audio_metadata_protocol_version=1,
            private_semantic_capture_dir=private_path,
        )
    )

    assert ok is False
    assert len(captures) == 1
    assert captures[0].abort_count == 1
    assert captures[0].write_paths == []
    assert not private_path.exists()


def test_clean_batch_capture_publishes_only_after_validation(
    monkeypatch,
    tmp_path,
):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    private_path = tmp_path / "private"
    captures = []
    ordering = []

    class Capture(_RecordingPrivateCapture):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            captures.append(self)

        def write_new(self, path: Path) -> None:
            ordering.append("publish")
            super().write_new(path)

    async def fake_run_test(
        _audio_path,
        _backend_url,
        **kwargs,
    ):
        assert kwargs["private_semantic_capture"] is captures[0]
        return _minimal_result(audio)

    def validate(_result):
        ordering.append("validate")
        return []

    monkeypatch.setattr(
        batch,
        "PrivatePcmScheduleCapture",
        Capture,
        raising=False,
    )
    monkeypatch.setattr(
        batch,
        "_resolve_private_semantic_capture_dir",
        lambda path: path,
    )
    monkeypatch.setattr(batch, "run_test", fake_run_test)
    monkeypatch.setattr(batch, "validate_capture_result", validate)
    _disable_regular_artifact_writes(monkeypatch)

    ok = asyncio.run(
        batch.run_batch(
            [str(audio)],
            "http://backend",
            str(tmp_path / "regular-results"),
            audio_metadata_protocol_version=1,
            private_semantic_capture_dir=private_path,
        )
    )

    assert ok is True
    assert ordering == ["validate", "publish"]
    assert captures[0].write_paths == [private_path]
    assert private_path.stat().st_mode & 0o777 == 0o700
    assert sorted(path.name for path in private_path.iterdir()) == [
        "schedule-ledger.json",
        "source-review.wav",
        "translated-review.wav",
    ]
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in private_path.iterdir()
    )
