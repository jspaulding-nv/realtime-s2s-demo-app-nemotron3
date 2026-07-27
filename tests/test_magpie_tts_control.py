import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import magpie_tts_control as control
import tts_comparison_fixture as fixture


PRIVATE_TEXT = "Texto privado de una persona identificable."
PCM_CHUNK = b"\x01\x00" * 6_615  # 0.3 s at 22.05 kHz.


class SequenceClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class StepClock:
    def __init__(self, step=0.1):
        self.value = 0.0
        self.step = step

    def __call__(self):
        value = self.value
        self.value += self.step
        return value


class Response:
    def __init__(self, audio):
        self.audio = audio


class FakeCall:
    def __init__(self, responses, *, on_first_next=None):
        self.responses = iter(responses)
        self.on_first_next = on_first_next
        self.cancel_count = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.on_first_next is not None:
            callback = self.on_first_next
            self.on_first_next = None
            callback()
        if self.cancel_count:
            raise RuntimeError(PRIVATE_TEXT)
        return next(self.responses)

    def cancel(self):
        self.cancel_count += 1
        return True


class RecordingService:
    def __init__(self, factory):
        self.factory = factory
        self.calls = []
        self.returned_calls = []

    def synthesize_online(self, **kwargs):
        self.calls.append(kwargs)
        call = FakeCall(self.factory(kwargs))
        self.returned_calls.append(call)
        return call


class FakeTimer:
    def __init__(self, timeout, callback):
        self.timeout = timeout
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        self.joined = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def join(self):
        self.joined = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class TimerFactory:
    def __init__(self):
        self.timers = []

    def __call__(self, timeout, callback):
        timer = FakeTimer(timeout, callback)
        self.timers.append(timer)
        return timer


class CancelRaceTimer(FakeTimer):
    """Simulate a callback already queued when the main thread cancels."""

    def cancel(self):
        self.callback()
        super().cancel()


class CancelRaceTimerFactory:
    def __init__(self):
        self.timer = None

    def __call__(self, timeout, callback):
        self.timer = CancelRaceTimer(timeout, callback)
        return self.timer


def riva_module():
    return SimpleNamespace(
        __file__=str(
            Path(control.__file__).resolve().parent
            / ".python-packages"
            / "riva"
            / "client"
            / "__init__.py"
        ),
        AudioEncoding=SimpleNamespace(LINEAR_PCM="LINEAR_PCM")
    )


def standard_responses(_kwargs):
    return [Response(PCM_CHUNK), Response(b""), Response(PCM_CHUNK)]


def fixed_timestamps():
    values = iter(
        [
            datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 26, 20, 1, tzinfo=timezone.utc),
        ]
    )
    return lambda: next(values)


def test_defaults_are_matched_and_pinned(monkeypatch):
    for name in ("TTS_IMAGE", "TTS_NIM_TAGS_SELECTOR", "TTS_IMAGE_DIGEST"):
        monkeypatch.delenv(name, raising=False)

    args = control.build_parser().parse_args([])

    assert control.DEFAULT_TEXT == (
        "La traducción en tiempo real debe mantener un ritmo claro y constante."
    )
    assert args.grpc_uri == "localhost:50053"
    assert args.tts_locale == "es-US"
    assert control.DEFAULT_VOICE == "Magpie-Multilingual.ES-US.Isabela"
    assert args.sample_rate_hz == 22_050
    assert args.repeats == 3
    assert args.rpc_timeout_seconds == 60.0
    assert args.max_audio_duration_seconds == 60.0
    assert control.DEFAULT_CONTAINER_IMAGE.endswith(":1.7.0")
    assert not hasattr(args, "voice")
    assert not hasattr(args, "container_image")
    assert not hasattr(args, "nim_profile")
    assert fixture.identify_text(control.DEFAULT_TEXT) == {
        "fixture_id": "neutral-spanish-streaming-tts",
        "fixture_version": 1,
        "comparison_status": "matched",
        "character_count": len(control.DEFAULT_TEXT),
        "utf8_byte_count": len(control.DEFAULT_TEXT.encode("utf-8")),
    }
    assert fixture.identify_text(
        control.DEFAULT_TEXT,
        custom_override=True,
    )["comparison_status"] == "custom_unmatched"


@pytest.mark.parametrize(
    "version",
    ["2.23.9", "2.24.0rc1", "2.24.0+local", "2.24.1", "2.26.0"],
)
def test_client_gate_is_exact_pep440_2240(version):
    with pytest.raises(RuntimeError, match="==2.24.0"):
        control.require_exact_client_version(version)

    control.require_exact_client_version("2.24.0")


def test_client_origin_must_resolve_under_dot_python_packages(tmp_path):
    expected = tmp_path / ".python-packages" / "riva" / "client"
    expected.mkdir(parents=True)
    module = expected / "__init__.py"
    module.touch()

    assert control.require_local_client_origin(
        str(module), repository_root=tmp_path
    ) == module.resolve()

    outside = tmp_path / "elsewhere.py"
    outside.touch()
    with pytest.raises(RuntimeError, match=r"\.python-packages"):
        control.require_local_client_origin(
            str(outside), repository_root=tmp_path
        )


def test_declared_unverified_provenance_is_strict_and_safe():
    provenance = control.validate_declared_provenance(
        container_image=control.DEFAULT_CONTAINER_IMAGE,
        nim_profile=control.DEFAULT_NIM_PROFILE,
        image_digest=control.DEFAULT_IMAGE_DIGEST,
    )
    assert provenance["verification_status"] == "declared_unverified"
    assert provenance["image_tag"] == "1.7.0"

    with pytest.raises(ValueError):
        control.validate_declared_provenance(
            container_image="nvcr.io/nim/nvidia/magpie-tts-multilingual:1.8.0",
            nim_profile=control.DEFAULT_NIM_PROFILE,
            image_digest=control.DEFAULT_IMAGE_DIGEST,
        )
    with pytest.raises(ValueError):
        control.validate_declared_provenance(
            container_image=control.DEFAULT_CONTAINER_IMAGE,
            nim_profile="name=magpie-tts-multilingual,batch_size=4",
            image_digest=control.DEFAULT_IMAGE_DIGEST,
        )
    with pytest.raises(ValueError):
        control.validate_declared_provenance(
            container_image=control.DEFAULT_CONTAINER_IMAGE,
            nim_profile=control.DEFAULT_NIM_PROFILE,
            image_digest="sha256:not-a-digest",
        )


def test_formal_control_rejects_nondefault_voice():
    with pytest.raises(ValueError, match="public default voice"):
        control.validate_request(
            tts_locale="es-US",
            voice="Magpie-Multilingual.ES-US.Other",
            sample_rate_hz=22_050,
            repeats=3,
            rpc_timeout_seconds=60.0,
            max_audio_duration_seconds=60.0,
        )


def test_stream_metrics_ignore_empty_responses_and_separate_terminal_tail():
    service = RecordingService(standard_responses)
    timers = TimerFactory()
    clock = SequenceClock([0.0, 1.0, 1.2, 1.7, 1.8])

    result = control.synthesize_once(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-US",
        voice=control.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        output_path=None,
        rpc_timeout_seconds=60.0,
        max_audio_duration_seconds=60.0,
        clock=clock,
        timer_factory=timers,
    )

    assert service.calls == [
        {
            "text": PRIVATE_TEXT,
            "voice_name": control.DEFAULT_VOICE,
            "language_code": "es-US",
            "encoding": "LINEAR_PCM",
            "sample_rate_hz": 22_050,
        }
    ]
    assert "custom_configuration" not in service.calls[0]
    assert result["ttfa_seconds"] == pytest.approx(1.0)
    assert result["audio_duration_seconds"] == pytest.approx(0.6)
    assert result["wall_time_seconds"] == pytest.approx(1.8)
    assert result["audio_chunk_count"] == 2
    assert result["empty_response_count"] == 1
    assert result["inter_audio_chunk_gap_count"] == 1
    assert result["inter_audio_chunk_gap_p50_seconds"] == pytest.approx(0.7)
    assert result["post_first_audio_delivery_seconds"] == pytest.approx(0.7)
    assert result["produced_audio_margin_seconds"] == pytest.approx(-0.1)
    assert result["minimum_produced_audio_margin_seconds"] == pytest.approx(-0.4)
    assert result["underrun_risk_seconds"] == pytest.approx(0.4)
    assert result["rpc_completion_tail_seconds"] == pytest.approx(0.1)
    assert timers.timers[0].cancelled is True
    assert timers.timers[0].joined is True


def test_incremental_audio_bound_cancels_live_call():
    service = RecordingService(lambda _kwargs: [Response(PCM_CHUNK)])

    with pytest.raises(control.AudioLimitExceeded):
        control.synthesize_once(
            service,
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-US",
            voice=control.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            output_path=None,
            rpc_timeout_seconds=60.0,
            max_audio_duration_seconds=0.2,
            clock=StepClock(),
            timer_factory=TimerFactory(),
        )

    assert service.returned_calls[0].cancel_count == 1


def test_deadline_watchdog_cancels_live_call():
    timers = TimerFactory()

    class DeadlineService:
        def __init__(self):
            self.call = None

        def synthesize_online(self, **kwargs):
            del kwargs
            self.call = FakeCall(
                [Response(PCM_CHUNK)],
                on_first_next=lambda: timers.timers[0].fire(),
            )
            return self.call

    service = DeadlineService()
    with pytest.raises(control.RPCDeadlineExceeded):
        control.synthesize_once(
            service,
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-US",
            voice=control.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            output_path=None,
            rpc_timeout_seconds=60.0,
            max_audio_duration_seconds=60.0,
            clock=StepClock(),
            timer_factory=timers,
        )

    assert service.call.cancel_count >= 1
    assert timers.timers[0].joined is True


def test_watchdog_cancel_race_cannot_timeout_a_completed_rpc():
    service = RecordingService(lambda _kwargs: [Response(PCM_CHUNK)])
    timers = CancelRaceTimerFactory()

    result = control.synthesize_once(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-US",
        voice=control.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        output_path=None,
        rpc_timeout_seconds=60.0,
        max_audio_duration_seconds=60.0,
        clock=StepClock(),
        timer_factory=timers,
    )

    assert result["status"] == "succeeded"
    assert service.returned_calls[0].cancel_count == 0
    assert timers.timer.cancelled is True
    assert timers.timer.joined is True


def test_private_directory_creates_fresh_tree_and_rejects_symlinks(tmp_path):
    artifact_dir = tmp_path / "fresh" / "experiment_results" / "run"
    normalized = control._new_private_directory(artifact_dir)

    assert normalized == artifact_dir.resolve()
    assert artifact_dir.is_dir()
    assert stat.S_IMODE(
        (tmp_path / "fresh").stat().st_mode
    ) == 0o700
    assert stat.S_IMODE(
        (tmp_path / "fresh" / "experiment_results").stat().st_mode
    ) == 0o700
    assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700

    with pytest.raises(FileExistsError):
        control._new_private_directory(artifact_dir)

    target = tmp_path / "real-target"
    target.mkdir()
    symlink = tmp_path / "artifact-symlink"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        control._new_private_directory(symlink)


def test_private_directory_rejects_symlink_parent(tmp_path):
    target = tmp_path / "real-parent"
    target.mkdir()
    symlink_parent = tmp_path / "linked-parent"
    symlink_parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        control._new_private_directory(symlink_parent / "run")

    assert not (target / "run").exists()


def test_run_has_discarded_warmup_private_atomic_artifacts_and_safe_json(
    tmp_path,
):
    service = RecordingService(standard_responses)
    timers = TimerFactory()
    artifact_dir = tmp_path / "private-control"

    report, report_path = control.run_control(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        artifact_dir=artifact_dir,
        grpc_uri="private-hostname.example:50053",
        clock=StepClock(),
        timer_factory=timers,
        now=fixed_timestamps(),
        quiet=True,
    )

    assert len(service.calls) == 4
    assert report["request"]["warmup_request_count"] == 1
    assert report["request"]["text"] == {
        "fixture_id": fixture.FIXTURE_ID,
        "fixture_version": fixture.FIXTURE_VERSION,
        "comparison_status": "custom_unmatched",
        "character_count": len(PRIVATE_TEXT),
        "utf8_byte_count": len(PRIVATE_TEXT.encode("utf-8")),
    }
    assert "warmup" not in report
    assert report["summary"]["succeeded"] == 3
    assert report["aggregates"]["ttfa_seconds"] == {
        "median": pytest.approx(0.1),
        "min": pytest.approx(0.1),
        "max": pytest.approx(0.1),
    }
    assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700
    artifacts = sorted(artifact_dir.iterdir())
    assert len(artifacts) == 4
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in artifacts)
    assert not any(path.name.startswith(".tmp-") for path in artifacts)

    serialized = report_path.read_text(encoding="utf-8")
    assert PRIVATE_TEXT not in serialized
    assert str(tmp_path) not in serialized
    assert "private-hostname.example" not in serialized
    assert ".wav" not in serialized
    assert report["privacy"]["contains_text_fingerprint"] is False

    with pytest.raises(FileExistsError):
        control.run_control(
            service,
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            artifact_dir=artifact_dir,
            grpc_uri="localhost:50053",
            clock=StepClock(),
            timer_factory=TimerFactory(),
            now=fixed_timestamps(),
            quiet=True,
        )


def test_run_control_verifies_client_origin_before_artifact_creation(tmp_path):
    external_module = SimpleNamespace(
        __file__="/tmp/external/riva/client/__init__.py",
        AudioEncoding=SimpleNamespace(LINEAR_PCM="LINEAR_PCM"),
    )
    artifact_dir = tmp_path / "must-not-exist"

    with pytest.raises(RuntimeError, match=r"\.python-packages"):
        control.run_control(
            RecordingService(standard_responses),
            riva_client_module=external_module,
            text=control.DEFAULT_TEXT,
            artifact_dir=artifact_dir,
            grpc_uri="localhost:50053",
            quiet=True,
        )

    assert not artifact_dir.exists()


def test_run_control_uses_normalized_artifact_directory(tmp_path):
    requested = tmp_path / "unused" / ".." / "normalized-control"
    report, report_path = control.run_control(
        RecordingService(standard_responses),
        riva_client_module=riva_module(),
        text=control.DEFAULT_TEXT,
        artifact_dir=requested,
        grpc_uri="localhost:50053",
        clock=StepClock(),
        timer_factory=TimerFactory(),
        now=fixed_timestamps(),
        quiet=True,
    )

    assert report_path.parent == (tmp_path / "normalized-control").resolve()
    assert report["request"]["text"]["comparison_status"] == "matched"


def test_error_reduction_omits_private_message_and_type_name():
    class CustomerNamedFailure(RuntimeError):
        pass

    reduced = control.safe_error(CustomerNamedFailure(PRIVATE_TEXT))
    serialized = json.dumps(reduced)

    assert PRIVATE_TEXT not in serialized
    assert "CustomerNamedFailure" not in serialized
    assert reduced == {
        "category": "rpc_or_runtime_error",
        "grpc_status": "",
    }
