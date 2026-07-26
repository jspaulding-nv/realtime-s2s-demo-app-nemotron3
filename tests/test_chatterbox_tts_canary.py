import json
import stat
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import chatterbox_tts_canary as canary
import tts_comparison_fixture as fixture


PRIVATE_TEXT = "Texto privado de una persona identificable."
PCM_CHUNK = b"\x01\x00" * 6_615  # 0.3 s at 22.05 kHz.
PROVENANCE_ENV = (
    "CHATTERBOX_TTS_IMAGE",
    "CHATTERBOX_TTS_NIM_TAGS_SELECTOR",
    "CHATTERBOX_TTS_IMAGE_DIGEST",
)


class StepClock:
    def __init__(self, *, step=0.1):
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
    def __init__(self, response_factory):
        self.response_factory = response_factory
        self.calls = []
        self.returned_calls = []

    def synthesize_online(self, **kwargs):
        self.calls.append(kwargs)
        responses = self.response_factory(kwargs)
        call = (
            responses
            if isinstance(responses, FakeCall)
            else FakeCall(responses)
        )
        self.returned_calls.append(call)
        return call


class FakeTimer:
    def __init__(self, timeout, callback):
        self.timeout = timeout
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.joined = False

    def start(self):
        self.started = True

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
    """Simulate a callback already queued when main calls cancel()."""

    def cancel(self):
        self.callback()
        super().cancel()


class CancelRaceTimerFactory:
    def __init__(self):
        self.timer = None

    def __call__(self, timeout, callback):
        self.timer = CancelRaceTimer(timeout, callback)
        return self.timer


class FakeHTTPResponse:
    def __init__(self, payload, *, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback

    def read(self, limit):
        return self.payload[:limit]


def riva_module():
    return SimpleNamespace(
        __file__=str(
            Path(canary.__file__).resolve().parent
            / ".python-packages-chatterbox"
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


def test_parser_uses_pinned_endpoints_factors_limits_and_provenance(
    monkeypatch,
):
    for name in PROVENANCE_ENV:
        monkeypatch.delenv(name, raising=False)

    args = canary.build_parser().parse_args([])

    assert args.grpc_uri == "localhost:50054"
    assert args.http_base_url == "http://localhost:9004"
    assert args.tts_locale == "es-ES"
    assert args.sample_rate_hz == 22_050
    assert args.exaggeration_factors == [0.5, 0.7, 1.0, 1.5]
    assert args.rpc_timeout_seconds == 60.0
    assert args.max_audio_duration_seconds == 60.0
    assert args.repeats_per_factor == 3
    assert args.container_image == canary.DEFAULT_CONTAINER_IMAGE
    assert args.nim_profile == canary.DEFAULT_NIM_PROFILE
    assert args.image_digest == canary.DEFAULT_IMAGE_DIGEST
    assert canary.DEFAULT_TEXT == fixture.DEFAULT_TEXT
    assert fixture.identify_text(canary.DEFAULT_TEXT) == {
        "fixture_id": "neutral-spanish-streaming-tts",
        "fixture_version": 1,
        "comparison_status": "matched",
        "character_count": len(canary.DEFAULT_TEXT),
        "utf8_byte_count": len(canary.DEFAULT_TEXT.encode("utf-8")),
    }
    assert fixture.identify_text(
        canary.DEFAULT_TEXT,
        custom_override=True,
    )["comparison_status"] == "custom_unmatched"


def test_parser_accepts_strict_provenance_from_environment(monkeypatch):
    digest = "sha256:" + "a" * 64
    monkeypatch.setenv(
        "CHATTERBOX_TTS_IMAGE",
        (
            "nvcr.io/nim/nvidia/chatterbox-tts-multilingual:"
            f"1.1.0@{digest}"
        ),
    )
    monkeypatch.setenv(
        "CHATTERBOX_TTS_NIM_TAGS_SELECTOR",
        "name=chatterbox-tts-multilingual,batch_size=8",
    )
    monkeypatch.setenv("CHATTERBOX_TTS_IMAGE_DIGEST", digest)

    args = canary.build_parser().parse_args([])
    provenance = canary.validate_declared_model_provenance(
        container_image=args.container_image,
        nim_profile=args.nim_profile,
        image_digest=args.image_digest,
    )

    assert provenance["verification_status"] == "declared_unverified"
    assert provenance["runtime_attested"] is False
    assert provenance["declared_release"] == "1.1.0"
    assert provenance["declared_image_digest"] == digest
    assert provenance["declared_nim_profile"].endswith("batch_size=8")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("container_image", "nvcr.io/nim/nvidia/chatterbox-tts-multilingual:latest"),
        ("container_image", "private.registry/chatterbox:1.0.0"),
        (
            "container_image",
            "nvcr.io/nim/nvidia/chatterbox-tts-multilingual:1.0.0",
        ),
        ("nim_profile", "name=private-customer"),
        ("nim_profile", "name=chatterbox-tts-multilingual,owner=private"),
        ("image_digest", "sha256:not-a-digest"),
    ],
)
def test_provenance_rejects_unpinned_or_unsafe_values(field, value):
    values = {
        "container_image": canary.DEFAULT_CONTAINER_IMAGE,
        "nim_profile": canary.DEFAULT_NIM_PROFILE,
        "image_digest": canary.DEFAULT_IMAGE_DIGEST,
    }
    values[field] = value

    with pytest.raises(ValueError):
        canary.validate_declared_model_provenance(**values)


def test_provenance_rejects_mismatched_embedded_and_separate_digests():
    with pytest.raises(ValueError, match="must match"):
        canary.validate_declared_model_provenance(
            container_image=(
                "nvcr.io/nim/nvidia/chatterbox-tts-multilingual:"
                f"1.0.0@{'sha256:' + 'a' * 64}"
            ),
            nim_profile=canary.DEFAULT_NIM_PROFILE,
            image_digest="sha256:" + "b" * 64,
        )


def test_compose_and_env_use_the_exact_immutable_default_image():
    repository_root = Path(canary.__file__).resolve().parent
    compose = (repository_root / "docker-compose.yaml").read_text(
        encoding="utf-8"
    )
    env_example = (repository_root / ".env.example").read_text(
        encoding="utf-8"
    )

    assert (
        "image: ${CHATTERBOX_TTS_IMAGE:-"
        f"{canary.DEFAULT_CONTAINER_IMAGE}"
        "}"
    ) in compose
    assert (
        f"CHATTERBOX_TTS_IMAGE={canary.DEFAULT_CONTAINER_IMAGE}"
        in env_example
    )
    assert (
        f"CHATTERBOX_TTS_IMAGE_DIGEST={canary.DEFAULT_IMAGE_DIGEST}"
        in env_example
    )


@pytest.mark.parametrize(
    "version",
    ["2.26.0", "2.26", "2.26.0.0", "v2.26.0", "0!2.26.0"],
)
def test_client_version_gate_accepts_pep440_equivalent_final_release(version):
    canary.require_supported_riva_client(version)


@pytest.mark.parametrize(
    "version",
    [
        "2.24.0",
        "2.27.0",
        "2.26.0rc1",
        "2.26.0.dev1",
        "2.26.0.post1",
        "2.26.0+local",
        "1!2.26.0",
    ],
)
def test_client_version_gate_rejects_nonexact_or_variant_release(version):
    with pytest.raises(RuntimeError, match="==2.26.0"):
        canary.require_supported_riva_client(version)


@pytest.mark.parametrize("factor", [0.24, 2.01, float("nan"), float("inf")])
def test_rejects_unsupported_exaggeration_factor(factor):
    with pytest.raises(ValueError, match=r"\[0.25, 2.0\]"):
        canary.validate_exaggeration_factors([factor])


def test_inventory_fetch_extracts_only_documented_chatterbox_voices():
    payload = {
        "voices": [
            {
                "voice_name": "Chatterbox-Multilingual.es-ES.Male",
                "language_code": "es-ES",
            },
            {
                "voice": "Magpie-Multilingual.ES-US.Isabela",
                "locale": "es-US",
            },
            {
                "voice": "Chatterbox-Multilingual.en-US.Male",
                "locale": "en-US",
                "private_note": PRIVATE_TEXT,
            },
            {
                "voice_name": "Chatterbox-Multilingual.es-ES.Female",
                "language_code": "es-ES",
            },
            {
                "voice_name": "Chatterbox-Multilingual.xx-XX.Male",
                "language_code": "xx-XX",
            },
            {
                "voice_name": "Chatterbox-Multilingual.es-ES.Male.Custom",
                "language_code": "es-ES",
            },
        ],
        "private_text": PRIVATE_TEXT,
    }
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeHTTPResponse(payload)

    result = canary.fetch_voice_inventory(
        "http://localhost:9004",
        timeout_seconds=3.0,
        opener=opener,
    )

    assert calls == [
        ("http://localhost:9004/v1/audio/list_voices", 3.0)
    ]
    assert result["matched_chatterbox_voice_count"] == 2
    assert result["voices"][1] == {
        "locale": "es-ES",
        "voice": "Chatterbox-Multilingual.es-ES.Male",
    }
    assert PRIVATE_TEXT not in json.dumps(result)


def test_inventory_failure_records_fixed_category_without_private_details():
    class PrivateInventoryFailure(RuntimeError):
        pass

    def opener(request, timeout):
        del request, timeout
        raise PrivateInventoryFailure(PRIVATE_TEXT)

    result = canary.fetch_voice_inventory(
        "http://localhost:9004",
        timeout_seconds=3.0,
        opener=opener,
    )

    assert result["error"] == {
        "category": "rpc_or_runtime_error",
        "grpc_status": "",
    }
    assert "PrivateInventoryFailure" not in json.dumps(result)
    assert PRIVATE_TEXT not in json.dumps(result)


def test_voice_resolution_prefers_inventory_and_preserves_es_es_fallback():
    inventory = {
        "voices": [
            {
                "locale": "fr-FR",
                "voice": "Chatterbox-Multilingual.fr-FR.Male",
            }
        ]
    }

    assert (
        canary.resolve_voice(
            tts_locale="fr-FR",
            explicit_voice=None,
            inventory=inventory,
        )
        == "Chatterbox-Multilingual.fr-FR.Male"
    )
    assert (
        canary.resolve_voice(
            tts_locale="es-ES",
            explicit_voice=None,
            inventory={"voices": []},
        )
        == "Chatterbox-Multilingual.es-ES.Male"
    )


@pytest.mark.parametrize(
    ("locale", "voice"),
    [
        ("xx-XX", "Chatterbox-Multilingual.xx-XX.Male"),
        ("es-ES", "Chatterbox-Multilingual.es-ES.Female"),
        ("es-ES", "Chatterbox-Multilingual.en-US.Male"),
        ("es-ES", "Chatterbox-Multilingual.es-ES.Male.Custom"),
    ],
)
def test_voice_resolution_rejects_undocumented_locale_or_voice(locale, voice):
    with pytest.raises(ValueError):
        canary.resolve_voice(
            tts_locale=locale,
            explicit_voice=voice,
            inventory={"voices": []},
        )


def test_inventory_sanitizer_revalidates_voices_and_error_category():
    sanitized = canary.sanitize_inventory_for_report(
        {
            "attempted": True,
            "succeeded": False,
            "voices": [
                {
                    "locale": "es-ES",
                    "voice": canary.DEFAULT_VOICE,
                },
                {
                    "locale": "xx-XX",
                    "voice": "Chatterbox-Multilingual.xx-XX.Male",
                },
            ],
            "error": {
                "category": "PrivateCustomerFailure",
                "grpc_status": "PRIVATE",
                "message": PRIVATE_TEXT,
            },
        }
    )

    assert sanitized["voices"] == [
        {"locale": "es-ES", "voice": canary.DEFAULT_VOICE}
    ]
    assert sanitized["error"] == {
        "category": "rpc_or_runtime_error",
        "grpc_status": "",
    }
    assert PRIVATE_TEXT not in json.dumps(sanitized)


def test_safe_error_uses_only_fixed_categories_and_status_allowlist():
    class CustomerNamedFailure(RuntimeError):
        def code(self):
            return SimpleNamespace(name="UNAVAILABLE")

    reduced = canary.safe_error(CustomerNamedFailure(PRIVATE_TEXT))

    assert reduced == {
        "category": "rpc_or_runtime_error",
        "grpc_status": "UNAVAILABLE",
    }
    serialized = json.dumps(reduced)
    assert "CustomerNamedFailure" not in serialized
    assert PRIVATE_TEXT not in serialized
    assert canary.safe_error(canary.RPCDeadlineExceeded())["category"] == (
        "rpc_deadline_exceeded"
    )
    assert canary.safe_error(canary.AudioLimitExceeded())["category"] == (
        "audio_limit_exceeded"
    )
    assert canary.safe_error(ValueError())["category"] == (
        "invalid_audio_response"
    )


def test_local_client_origin_requires_repository_vendor_tree(tmp_path):
    repository_root = tmp_path / "repository"
    local_module = (
        repository_root
        / ".python-packages-chatterbox"
        / "riva"
        / "client"
        / "__init__.py"
    )

    assert canary.require_local_client_origin(
        str(local_module),
        repository_root=repository_root,
    ) == local_module.resolve()
    with pytest.raises(RuntimeError, match=r"\.python-packages-chatterbox"):
        canary.require_local_client_origin(
            str(tmp_path / "system-site-packages" / "riva" / "__init__.py"),
            repository_root=repository_root,
        )
    with pytest.raises(RuntimeError, match="cannot verify"):
        canary.require_local_client_origin(
            None,
            repository_root=repository_root,
        )


def test_watchdog_cancels_the_live_rpc_at_deadline(tmp_path):
    timers = TimerFactory()

    def response_factory(_kwargs):
        return FakeCall(
            [Response(PCM_CHUNK)],
            on_first_next=lambda: timers.timers[-1].fire(),
        )

    service = RecordingService(response_factory)
    output = tmp_path / "deadline.wav"

    with pytest.raises(canary.RPCDeadlineExceeded):
        canary.synthesize_factor(
            service,
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-ES",
            voice=canary.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            exaggeration_factor=0.5,
            output_path=output,
            rpc_timeout_seconds=7.5,
            max_audio_duration_seconds=60.0,
            clock=StepClock(),
            timer_factory=timers,
        )

    assert service.returned_calls[0].cancel_count == 1
    assert timers.timers[0].timeout == 7.5
    assert timers.timers[0].started is True
    assert timers.timers[0].cancelled is True
    assert timers.timers[0].joined is True
    assert not output.exists()


def test_watchdog_cancel_race_cannot_mark_completed_rpc_as_timed_out(
    tmp_path,
):
    service = RecordingService(lambda _kwargs: [Response(PCM_CHUNK)])
    timers = CancelRaceTimerFactory()

    result = canary.synthesize_factor(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        exaggeration_factor=0.5,
        output_path=tmp_path / "completed.wav",
        rpc_timeout_seconds=60.0,
        max_audio_duration_seconds=60.0,
        clock=StepClock(),
        timer_factory=timers,
    )

    assert result["status"] == "succeeded"
    assert result["post_first_audio_delivery_seconds"] == 0
    assert result["rpc_completion_tail_seconds"] == pytest.approx(0.1)
    assert result["produced_audio_margin_seconds"] == pytest.approx(0.3)
    assert result["underrun_risk_detected"] is False
    assert service.returned_calls[0].cancel_count == 0
    assert timers.timer.cancelled is True
    assert timers.timer.joined is True


def test_incremental_audio_bound_cancels_before_buffering_or_writing(tmp_path):
    service = RecordingService(
        lambda _kwargs: [Response(b"\x01\x00" * 22_050)]
    )
    timers = TimerFactory()
    output = tmp_path / "too-long.wav"

    with pytest.raises(canary.AudioLimitExceeded):
        canary.synthesize_factor(
            service,
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-ES",
            voice=canary.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            exaggeration_factor=0.5,
            output_path=output,
            rpc_timeout_seconds=60.0,
            max_audio_duration_seconds=0.5,
            clock=StepClock(),
            timer_factory=timers,
        )

    assert service.returned_calls[0].cancel_count == 1
    assert timers.timers[0].cancelled is True
    assert timers.timers[0].joined is True
    assert not output.exists()


def test_continuity_metrics_detect_negative_produced_margin(tmp_path):
    service = RecordingService(
        lambda _kwargs: [
            Response(b"\x01\x00" * 2_205),
            Response(b"\x01\x00" * 2_205),
        ]
    )
    timers = TimerFactory()

    result = canary.synthesize_factor(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        exaggeration_factor=0.5,
        output_path=tmp_path / "risk.wav",
        rpc_timeout_seconds=60.0,
        max_audio_duration_seconds=60.0,
        clock=StepClock(step=0.2),
        timer_factory=timers,
    )

    assert result["audio_duration_seconds"] == pytest.approx(0.2)
    assert result["inter_audio_chunk_gap_p50_seconds"] == pytest.approx(0.2)
    assert result["post_first_audio_delivery_seconds"] == pytest.approx(0.2)
    assert result["rpc_completion_tail_seconds"] == pytest.approx(0.2)
    assert result["produced_audio_margin_seconds"] == pytest.approx(0)
    assert result["minimum_produced_audio_margin_seconds"] == pytest.approx(-0.1)
    assert result["underrun_risk_seconds"] == pytest.approx(0.1)
    assert result["underrun_risk_detected"] is True


def test_balanced_sweep_warmup_repeats_metrics_permissions_and_privacy(
    tmp_path,
):
    service = RecordingService(standard_responses)
    timers = TimerFactory()
    artifact_dir = tmp_path / "balanced-run"
    inventory = {
        "attempted": True,
        "succeeded": True,
        "matched_chatterbox_voice_count": 1,
        "voices": [
            {
                "locale": "es-ES",
                "voice": canary.DEFAULT_VOICE,
            }
        ],
    }

    report, report_path = canary.run_sweep(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        factors=(0.5, 0.7, 1.0, 1.5),
        artifact_dir=artifact_dir,
        inventory=inventory,
        grpc_uri="localhost:50054",
        http_base_url="http://localhost:9004",
        client_version="2.26.0",
        clock=StepClock(),
        timer_factory=timers,
        now=fixed_timestamps(),
        quiet=True,
    )

    assert len(service.calls) == 13  # One warm-up plus 4 factors * 3.
    assert [
        call["custom_configuration"]["exaggeration_factor"]
        for call in service.calls
    ] == [
        "0.5",
        "0.5",
        "0.7",
        "1",
        "1.5",
        "0.7",
        "1",
        "1.5",
        "0.5",
        "1",
        "1.5",
        "0.5",
        "0.7",
    ]
    assert all(call["language_code"] == "es-ES" for call in service.calls)
    assert all(call["voice_name"] == canary.DEFAULT_VOICE for call in service.calls)
    assert all(call["encoding"] == "LINEAR_PCM" for call in service.calls)
    assert all(
        timer.started and timer.cancelled and timer.joined
        for timer in timers.timers
    )
    assert all(call.cancel_count == 0 for call in service.returned_calls)

    assert report["schema_version"] == 2
    assert report["summary"] == {
        "requested": 12,
        "succeeded": 12,
        "failed": 0,
        "warmup_status": "succeeded",
    }
    assert report["warmup"] == {
        "status": "succeeded",
        "exaggeration_factor": 0.5,
        "audio_artifact_written": False,
    }
    assert not {
        "ttfa_seconds",
        "wall_time_seconds",
        "audio_duration_seconds",
        "real_time_factor",
        "wav_file",
    }.intersection(report["warmup"])
    assert len(report["results"]) == 12
    assert len({result["wav_file"] for result in report["results"]}) == 12
    assert [
        (result["factor_index"], result["repeat_index"])
        for result in report["results"]
    ] == [
        (1, 1),
        (2, 1),
        (3, 1),
        (4, 1),
        (2, 2),
        (3, 2),
        (4, 2),
        (1, 2),
        (3, 3),
        (4, 3),
        (1, 3),
        (2, 3),
    ]

    for result in report["results"]:
        assert result["ttfa_seconds"] == pytest.approx(0.1)
        assert result["wall_time_seconds"] == pytest.approx(0.4)
        assert result["audio_chunk_count"] == 2
        assert result["empty_response_count"] == 1
        assert result["audio_bytes"] == len(PCM_CHUNK) * 2
        assert result["audio_duration_seconds"] == pytest.approx(0.6)
        assert result["real_time_factor"] == pytest.approx(2 / 3)
        assert result["inter_audio_chunk_gap_count"] == 1
        assert result["inter_audio_chunk_gap_p50_seconds"] == pytest.approx(0.2)
        assert result["inter_audio_chunk_gap_p95_seconds"] == pytest.approx(0.2)
        assert result["inter_audio_chunk_gap_max_seconds"] == pytest.approx(0.2)
        assert result["post_first_audio_delivery_seconds"] == pytest.approx(0.2)
        assert result["rpc_completion_tail_seconds"] == pytest.approx(0.1)
        assert result["produced_audio_margin_seconds"] == pytest.approx(0.4)
        assert result["minimum_produced_audio_margin_seconds"] == pytest.approx(
            0.1
        )
        assert result["underrun_risk_seconds"] == 0
        assert result["underrun_risk_detected"] is False
        wav_path = artifact_dir / result["wav_file"]
        assert stat.S_IMODE(wav_path.stat().st_mode) == 0o600
        with wave.open(str(wav_path), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 22_050
            assert handle.getnframes() == 13_230

    assert len(report["factor_aggregates"]) == 4
    for aggregate in report["factor_aggregates"]:
        assert aggregate["requested_repeats"] == 3
        assert aggregate["succeeded"] == 3
        assert aggregate["failed"] == 0
        assert aggregate["underrun_risk_detected_count"] == 0
        assert aggregate["metrics"]["ttfa_seconds"] == pytest.approx(
            {"median": 0.1, "min": 0.1, "max": 0.1}
        )
        assert aggregate["metrics"]["audio_duration_seconds"] == pytest.approx(
            {"median": 0.6, "min": 0.6, "max": 0.6}
        )

    assert report["request"]["text"] == {
        "fixture_id": fixture.FIXTURE_ID,
        "fixture_version": fixture.FIXTURE_VERSION,
        "comparison_status": "custom_unmatched",
        "character_count": len(PRIVATE_TEXT),
        "utf8_byte_count": len(PRIVATE_TEXT.encode("utf-8")),
    }
    assert report["request"]["repeats_per_factor"] == 3
    assert report["request"]["rpc_timeout_seconds"] == 60.0
    assert report["request"]["max_audio_duration_seconds"] == 60.0
    assert report["client"] == {
        "package": "nvidia-riva-client",
        "reported_version": "2.26.0",
        "required_version": "2.26.0",
        "streaming_api": "bidirectional",
    }
    assert "model_provenance" not in report
    assert report["declared_model_provenance"] == {
        "verification_status": "declared_unverified",
        "runtime_attested": False,
        "declared_release": "1.0.0",
        "declared_container_image": canary.DEFAULT_CONTAINER_IMAGE,
        "declared_image_repository": (
            "nvcr.io/nim/nvidia/chatterbox-tts-multilingual"
        ),
        "declared_image_tag": "1.0.0",
        "declared_image_digest": canary.DEFAULT_IMAGE_DIGEST,
        "declared_nim_profile": canary.DEFAULT_NIM_PROFILE,
    }
    assert report_path.parent == artifact_dir.resolve()
    assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert not list(artifact_dir.glob(".tmp-*"))
    serialized = report_path.read_text(encoding="utf-8")
    assert json.loads(serialized) == report
    assert PRIVATE_TEXT not in serialized
    assert report["privacy"]["contains_input_text"] is False
    assert report["privacy"]["contains_text_fingerprint"] is False
    assert report["privacy"]["artifact_directory_mode"] == "0700"
    assert report["privacy"]["artifact_file_mode"] == "0600"


def test_sweep_continues_after_private_service_failure(tmp_path):
    class PrivateSynthesisFailure(RuntimeError):
        pass

    def response_factory(kwargs):
        factor = kwargs["custom_configuration"]["exaggeration_factor"]
        if factor == "0.7":
            raise PrivateSynthesisFailure(PRIVATE_TEXT)
        return [Response(PCM_CHUNK)]

    service = RecordingService(response_factory)
    timers = TimerFactory()
    artifact_dir = tmp_path / "failure-run"
    report, report_path = canary.run_sweep(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        factors=(0.5, 0.7, 1.0),
        repeats_per_factor=1,
        artifact_dir=artifact_dir,
        inventory=canary._disabled_inventory(),
        grpc_uri="localhost:50054",
        http_base_url="http://localhost:9004",
        client_version="2.26.0",
        clock=StepClock(),
        timer_factory=timers,
        now=fixed_timestamps(),
        quiet=True,
    )

    assert report["summary"] == {
        "requested": 3,
        "succeeded": 2,
        "failed": 1,
        "warmup_status": "succeeded",
    }
    assert report["results"][1]["error"] == {
        "category": "rpc_or_runtime_error",
        "grpc_status": "",
    }
    assert "PrivateSynthesisFailure" not in json.dumps(report)
    assert report["factor_aggregates"][1]["failed"] == 1
    assert PRIVATE_TEXT not in report_path.read_text(encoding="utf-8")
    failed_name = canary.wav_filename(
        factor_index=2,
        factor=0.7,
        repeat_index=1,
    )
    assert not (artifact_dir / failed_name).exists()


def test_report_preserves_exact_pep440_equivalent_reported_client_version(
    tmp_path,
):
    service = RecordingService(lambda _kwargs: [Response(PCM_CHUNK)])
    report, _ = canary.run_sweep(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        factors=(0.5,),
        repeats_per_factor=1,
        include_warmup=False,
        artifact_dir=tmp_path / "reported-version-run",
        inventory=canary._disabled_inventory(),
        grpc_uri="localhost:50054",
        http_base_url="http://localhost:9004",
        client_version="v2.26.0",
        clock=StepClock(),
        timer_factory=TimerFactory(),
        now=fixed_timestamps(),
        quiet=True,
    )

    assert report["client"]["reported_version"] == "v2.26.0"
    assert report["client"]["required_version"] == "2.26.0"


def test_sweep_refuses_to_reuse_preexisting_artifact_directory(tmp_path):
    existing = tmp_path / "existing-run"
    existing.mkdir()
    service = RecordingService(lambda _kwargs: [Response(b"\x00\x00")])

    with pytest.raises(FileExistsError, match="refusing to reuse"):
        canary.run_sweep(
            service,
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-ES",
            voice=canary.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            factors=(0.5,),
            repeats_per_factor=1,
            artifact_dir=existing,
            inventory=canary._disabled_inventory(),
            grpc_uri="localhost:50054",
            http_base_url="http://localhost:9004",
            client_version="2.26.0",
            timer_factory=TimerFactory(),
            quiet=True,
        )

    assert list(existing.iterdir()) == []
    assert service.calls == []


def test_private_directory_creates_fresh_tree_and_rejects_symlinks(tmp_path):
    artifact_dir = tmp_path / "fresh" / "experiment_results" / "run"
    normalized = canary._new_private_directory(artifact_dir)

    assert normalized == artifact_dir.resolve()
    assert artifact_dir.is_dir()
    assert stat.S_IMODE((tmp_path / "fresh").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (tmp_path / "fresh" / "experiment_results").stat().st_mode
    ) == 0o700
    assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700

    with pytest.raises(FileExistsError):
        canary._new_private_directory(artifact_dir)

    target = tmp_path / "real-target"
    target.mkdir()
    symlink = tmp_path / "artifact-symlink"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        canary._new_private_directory(symlink)


def test_private_directory_rejects_symlink_parent(tmp_path):
    target = tmp_path / "real-parent"
    target.mkdir()
    symlink_parent = tmp_path / "linked-parent"
    symlink_parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        canary._new_private_directory(symlink_parent / "run")

    assert not (target / "run").exists()


def test_sweep_verifies_client_origin_before_artifact_creation(tmp_path):
    external_module = SimpleNamespace(
        __file__="/tmp/external/riva/client/__init__.py",
        AudioEncoding=SimpleNamespace(LINEAR_PCM="LINEAR_PCM"),
    )
    artifact_dir = tmp_path / "must-not-exist"
    service = RecordingService(standard_responses)

    with pytest.raises(RuntimeError, match=r"\.python-packages-chatterbox"):
        canary.run_sweep(
            service,
            riva_client_module=external_module,
            text=canary.DEFAULT_TEXT,
            tts_locale="es-ES",
            voice=canary.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            factors=(0.5,),
            repeats_per_factor=1,
            artifact_dir=artifact_dir,
            inventory=canary._disabled_inventory(),
            grpc_uri="localhost:50054",
            http_base_url="http://localhost:9004",
            client_version="2.26.0",
            quiet=True,
        )

    assert not artifact_dir.exists()
    assert service.calls == []


def test_sweep_rejects_undocumented_voice_before_artifact_creation(tmp_path):
    artifact_dir = tmp_path / "must-not-exist"
    service = RecordingService(standard_responses)

    with pytest.raises(ValueError, match="documented built-in voice"):
        canary.run_sweep(
            service,
            riva_client_module=riva_module(),
            text=canary.DEFAULT_TEXT,
            tts_locale="es-ES",
            voice="Chatterbox-Multilingual.es-ES.Female",
            sample_rate_hz=22_050,
            factors=(0.5,),
            repeats_per_factor=1,
            artifact_dir=artifact_dir,
            inventory=canary._disabled_inventory(),
            grpc_uri="localhost:50054",
            http_base_url="http://localhost:9004",
            client_version="2.26.0",
            quiet=True,
        )

    assert not artifact_dir.exists()
    assert service.calls == []


def test_sweep_uses_normalized_artifact_directory(tmp_path):
    requested = tmp_path / "unused" / ".." / "normalized-canary"
    report, report_path = canary.run_sweep(
        RecordingService(standard_responses),
        riva_client_module=riva_module(),
        text=canary.DEFAULT_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        factors=(0.5,),
        repeats_per_factor=1,
        include_warmup=False,
        artifact_dir=requested,
        inventory=canary._disabled_inventory(),
        grpc_uri="localhost:50054",
        http_base_url="http://localhost:9004",
        client_version="2.26.0",
        clock=StepClock(),
        timer_factory=TimerFactory(),
        now=fixed_timestamps(),
        quiet=True,
    )

    assert report_path.parent == (tmp_path / "normalized-canary").resolve()
    assert report["request"]["text"]["comparison_status"] == "matched"
    assert not (tmp_path / "unused").exists()


def test_failed_warmup_records_only_status_factor_and_no_artifact(tmp_path):
    def response_factory(_kwargs):
        if len(service.calls) == 1:
            raise RuntimeError(PRIVATE_TEXT)
        return [Response(PCM_CHUNK)]

    service = RecordingService(response_factory)
    artifact_dir = tmp_path / "warmup-failure-run"
    report, report_path = canary.run_sweep(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        factors=(0.5,),
        repeats_per_factor=1,
        artifact_dir=artifact_dir,
        inventory=canary._disabled_inventory(),
        grpc_uri="localhost:50054",
        http_base_url="http://localhost:9004",
        client_version="2.26.0",
        clock=StepClock(),
        timer_factory=TimerFactory(),
        now=fixed_timestamps(),
        quiet=True,
    )

    assert report["warmup"] == {
        "status": "failed",
        "exaggeration_factor": 0.5,
        "audio_artifact_written": False,
    }
    assert report["summary"]["warmup_status"] == "failed"
    assert report["summary"]["succeeded"] == 1
    serialized = report_path.read_text(encoding="utf-8")
    assert PRIVATE_TEXT not in serialized
    assert "RuntimeError" not in serialized
