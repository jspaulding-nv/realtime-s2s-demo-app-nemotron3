import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import chatterbox_cfg_weight_probe as probe
import chatterbox_tts_canary as canary


PRIVATE_TEXT = "Texto privado para private.example.test."
PRIVATE_PATH = "/private/customer/person/transcript.txt"
PCM_CHUNK = b"\x01\x00" * 6_615


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
    def __init__(self, responses):
        self.responses = iter(responses)
        self.cancel_count = 0

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.responses)

    def cancel(self):
        self.cancel_count += 1
        return True


class RecordingService:
    def __init__(self, response_factory):
        self.response_factory = response_factory
        self.calls = []

    def synthesize_online(self, **kwargs):
        self.calls.append(kwargs)
        responses = self.response_factory(kwargs)
        return responses if isinstance(responses, FakeCall) else FakeCall(
            responses
        )


class FakeTimer:
    def __init__(self, timeout, callback):
        self.timeout = timeout
        self.callback = callback
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass

    def join(self):
        pass


def timer_factory(timeout, callback):
    return FakeTimer(timeout, callback)


def riva_module():
    return SimpleNamespace(
        __file__=str(
            Path(canary.__file__).resolve().parent
            / ".python-packages-chatterbox"
            / "riva"
            / "client"
            / "__init__.py"
        ),
        AudioEncoding=SimpleNamespace(LINEAR_PCM="LINEAR_PCM"),
    )


def fixed_timestamps():
    values = iter(
        [
            datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 27, 12, 1, tzinfo=timezone.utc),
        ]
    )
    return lambda: next(values)


@pytest.mark.parametrize(
    ("cfg_weight", "expected"),
    [
        (None, {"exaggeration_factor": "0.5"}),
        (0.3, {"exaggeration_factor": "0.5", "cfg_weight": "0.3"}),
        (0.5, {"exaggeration_factor": "0.5", "cfg_weight": "0.5"}),
        (0.7, {"exaggeration_factor": "0.5", "cfg_weight": "0.7"}),
    ],
)
def test_canary_omits_cfg_weight_or_sends_exact_decimal_string(
    tmp_path, cfg_weight, expected
):
    service = RecordingService(lambda _kwargs: [Response(PCM_CHUNK)])

    result = canary.synthesize_factor(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        exaggeration_factor=0.5,
        cfg_weight=cfg_weight,
        output_path=tmp_path / "audio.wav",
        rpc_timeout_seconds=60.0,
        max_audio_duration_seconds=60.0,
        clock=StepClock(),
        timer_factory=timer_factory,
    )

    assert service.calls[0]["custom_configuration"] == expected
    assert "cfg_weight" not in result


@pytest.mark.parametrize(
    "cfg_weight", [float("nan"), float("inf"), True, "0.5"]
)
def test_canary_rejects_nonfinite_or_nonnumeric_cfg_weight(
    tmp_path, cfg_weight
):
    service = RecordingService(lambda _kwargs: [Response(PCM_CHUNK)])

    with pytest.raises(ValueError, match="cfg_weight must be finite"):
        canary.synthesize_factor(
            service,
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-ES",
            voice=canary.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            exaggeration_factor=0.5,
            cfg_weight=cfg_weight,
            output_path=tmp_path / "audio.wav",
            rpc_timeout_seconds=60.0,
            max_audio_duration_seconds=60.0,
            clock=StepClock(),
            timer_factory=timer_factory,
        )

    assert service.calls == []


def test_default_plan_is_exact_bracketed_smoke_order():
    plan = probe.build_probe_plan(balanced_matrix=False)

    assert [
        (request.exaggeration_factor, request.cfg_weight, request.phase)
        for request in plan
    ] == [
        (0.5, None, "control_before"),
        (0.5, 0.3, "treatment"),
        (0.5, 0.5, "treatment"),
        (0.5, 0.7, "treatment"),
        (0.5, None, "control_after"),
    ]


def test_balanced_plan_has_every_cell_each_repeat_and_rotates_order():
    plan = probe.build_probe_plan(
        balanced_matrix=True,
        repeats_per_cell=5,
    )

    assert len(plan) == 2 * 4 * 5
    assert {
        (request.exaggeration_factor, request.cfg_weight)
        for request in plan
    } == {
        (0.5, None),
        (0.5, 0.3),
        (0.5, 0.5),
        (0.5, 0.7),
        (0.7, None),
        (0.7, 0.3),
        (0.7, 0.5),
        (0.7, 0.7),
    }
    for repeat in range(1, 6):
        cells = [
            (request.exaggeration_factor, request.cfg_weight)
            for request in plan
            if request.repeat_index == repeat
        ]
        assert len(cells) == 8
        assert len(set(cells)) == 8
    assert plan[0].cfg_weight is None
    assert plan[8].cfg_weight == 0.3


def result(
    *,
    status="succeeded",
    cfg_weight=None,
    grpc_status="",
    factor=0.5,
    repeat=1,
    duration=10.0,
    ttfa=0.5,
    rtf=1.0,
    underrun=False,
    underrun_risk=None,
):
    item = {
        "status": status,
        "exaggeration_factor": factor,
        "repeat_index": repeat,
        "cfg_weight_setting": (
            "omitted" if cfg_weight is None else "provided"
        ),
    }
    if cfg_weight is not None:
        item["cfg_weight"] = cfg_weight
    if status == "succeeded":
        item.update(
            {
                "audio_duration_seconds": duration,
                "ttfa_seconds": ttfa,
                "real_time_factor": rtf,
                "underrun_risk_detected": underrun,
                "underrun_risk_seconds": (
                    float(underrun)
                    if underrun_risk is None
                    else underrun_risk
                ),
            }
        )
    else:
        item["error"] = {
            "category": "rpc_or_runtime_error",
            "grpc_status": grpc_status,
        }
    return item


def test_compatibility_distinguishes_accept_reject_and_inconclusive():
    accepted = [
        result(cfg_weight=None),
        result(cfg_weight=0.3),
        result(cfg_weight=0.7),
        result(cfg_weight=None),
    ]
    assert probe.classify_compatibility(accepted)["classification"] == (
        "accepted"
    )
    assert (
        probe.classify_compatibility(accepted)[
            "acceptance_establishes_effect"
        ]
        is False
    )

    rejected = [
        result(cfg_weight=None),
        result(
            status="failed",
            cfg_weight=0.3,
            grpc_status="INVALID_ARGUMENT",
        ),
        result(
            status="failed",
            cfg_weight=0.7,
            grpc_status="UNIMPLEMENTED",
        ),
        result(cfg_weight=None),
    ]
    assert probe.classify_compatibility(rejected)["classification"] == (
        "rejected"
    )

    transient = rejected.copy()
    transient[1] = result(
        status="failed",
        cfg_weight=0.3,
        grpc_status="UNAVAILABLE",
    )
    assert probe.classify_compatibility(transient)["classification"] == (
        "inconclusive"
    )

    unhealthy_control = rejected.copy()
    unhealthy_control[0] = result(
        status="failed",
        cfg_weight=None,
        grpc_status="UNAVAILABLE",
    )
    assert (
        probe.classify_compatibility(unhealthy_control)["classification"]
        == "inconclusive"
    )


def balanced_results(
    *,
    extreme_duration=9.0,
    inconsistent_repeat=None,
    extreme_ttfa=0.6,
    extreme_rtf=0.9,
    extreme_underrun=False,
):
    items = []
    for factor in (0.5, 0.7):
        for repeat in range(1, 6):
            for cfg_weight in (None, 0.3, 0.5, 0.7):
                duration = {
                    None: 10.0,
                    0.3: 10.0,
                    0.5: 9.8,
                    0.7: extreme_duration,
                }[cfg_weight]
                if cfg_weight == 0.7 and repeat == inconsistent_repeat:
                    duration = 10.2
                items.append(
                    result(
                        cfg_weight=cfg_weight,
                        factor=factor,
                        repeat=repeat,
                        duration=duration,
                        ttfa=(
                            extreme_ttfa
                            if cfg_weight == 0.7
                            else 0.5
                        ),
                        rtf=(
                            extreme_rtf
                            if cfg_weight == 0.7
                            else 1.0
                        ),
                        underrun=(
                            extreme_underrun
                            if cfg_weight == 0.7
                            else False
                        ),
                    )
                )
    return items


def test_effect_requires_four_of_five_at_both_factors_and_threshold():
    items = balanced_results(inconsistent_repeat=5)
    compatibility = probe.classify_compatibility(items)

    effect = probe.classify_effect(
        items,
        compatibility=compatibility,
        balanced_matrix=True,
        repeats_per_cell=5,
    )

    assert effect["classification"] == "effect_detected"
    assert effect["consistent_direction"] == "cfg_0_7_shorter"
    assert [
        evidence["directional_agreement_count"]
        for evidence in effect["factor_evidence"]
    ] == [4, 4]
    assert all(
        evidence["duration_threshold_met"]
        for evidence in effect["factor_evidence"]
    )


def test_acceptance_does_not_become_effect_without_measured_threshold():
    items = balanced_results(extreme_duration=9.9)
    compatibility = probe.classify_compatibility(items)

    effect = probe.classify_effect(
        items,
        compatibility=compatibility,
        balanced_matrix=True,
        repeats_per_cell=5,
    )

    assert compatibility["classification"] == "accepted"
    assert effect["classification"] == "effect_not_demonstrated"
    assert effect["acceptance_alone_is_not_effect"] is True
    assert "ignored" not in json.dumps(effect).lower()


def test_effect_is_inconclusive_with_under_five_repeats():
    items = balanced_results()[:]

    effect = probe.classify_effect(
        items,
        compatibility={"classification": "accepted"},
        balanced_matrix=True,
        repeats_per_cell=4,
    )

    assert effect["classification"] == "inconclusive"
    assert effect["reason"] == "at_least_five_repeats_required"


def test_realtime_candidate_requires_duration_ttfa_rtf_and_underrun():
    items = balanced_results(inconsistent_repeat=5)
    effect = {"classification": "effect_detected"}

    realtime = probe.classify_realtime_candidates(
        items,
        effect=effect,
        balanced_matrix=True,
        repeats_per_cell=5,
    )

    assert realtime["classification"] == "candidate_found"
    assert realtime["candidates"] == [
        {"exaggeration_factor": 0.5, "cfg_weight": 0.7},
        {"exaggeration_factor": 0.7, "cfg_weight": 0.7},
    ]

    worsened = balanced_results(
        inconsistent_repeat=5,
        extreme_ttfa=0.76,
        extreme_rtf=1.01,
        extreme_underrun=True,
    )
    blocked = probe.classify_realtime_candidates(
        worsened,
        effect=effect,
        balanced_matrix=True,
        repeats_per_cell=5,
    )

    assert blocked["classification"] == "candidate_not_demonstrated"
    extreme_comparisons = [
        item
        for item in blocked["comparisons"]
        if item["cfg_weight"] == 0.7
    ]
    assert all(
        item["checks"]
        == {
            "duration_reduction_met": True,
            "ttfa_not_worse_beyond_limit": False,
            "real_time_factor_not_worse": False,
            "underrun_count_not_worse": False,
            "median_underrun_risk_not_worse": False,
        }
        for item in extreme_comparisons
    )


def test_smoke_report_is_private_atomic_ordered_and_permissioned(tmp_path):
    class PrivateFailure(RuntimeError):
        def code(self):
            return SimpleNamespace(name="UNAVAILABLE")

    def responses(kwargs):
        if kwargs["custom_configuration"].get("cfg_weight") == "0.5":
            raise PrivateFailure(
                f"{PRIVATE_TEXT} {PRIVATE_PATH} private-host.internal"
            )
        return [Response(PCM_CHUNK)]

    service = RecordingService(responses)
    artifact_dir = tmp_path / "private-parent" / "smoke"
    report, report_path = probe.run_probe(
        service,
        riva_client_module=riva_module(),
        text=PRIVATE_TEXT,
        tts_locale="es-ES",
        voice=canary.DEFAULT_VOICE,
        sample_rate_hz=22_050,
        artifact_dir=artifact_dir,
        grpc_uri="private-host.internal:50054",
        client_version="2.26.0",
        clock=StepClock(),
        timer_factory=timer_factory,
        now=fixed_timestamps(),
        quiet=True,
    )

    assert service.calls[0]["custom_configuration"] == {
        "exaggeration_factor": "0.5"
    }
    assert [
        call["custom_configuration"]
        for call in service.calls[1:]
    ] == [
        {"exaggeration_factor": "0.5"},
        {"exaggeration_factor": "0.5", "cfg_weight": "0.3"},
        {"exaggeration_factor": "0.5", "cfg_weight": "0.5"},
        {"exaggeration_factor": "0.5", "cfg_weight": "0.7"},
        {"exaggeration_factor": "0.5"},
    ]
    assert report["compatibility"]["classification"] == "inconclusive"
    assert report["warmup"] == {
        "status": "succeeded",
        "exaggeration_factor": 0.5,
        "cfg_weight_setting": "omitted",
        "audio_artifact_written": False,
    }
    assert report["effect"]["classification"] == "inconclusive"
    assert report["results"][2]["error"] == {
        "category": "rpc_or_runtime_error",
        "grpc_status": "UNAVAILABLE",
    }
    assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert len(list(artifact_dir.glob("*.wav"))) == 4
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in artifact_dir.glob("*.wav")
    )
    assert not list(artifact_dir.glob(".tmp-*"))
    serialized = report_path.read_text(encoding="utf-8")
    assert json.loads(serialized) == report
    for forbidden in (
        PRIVATE_TEXT,
        PRIVATE_PATH,
        "private-host.internal",
        "PrivateFailure",
    ):
        assert forbidden not in serialized
    assert "declared_container_image" not in serialized
    assert report["privacy"]["contains_audio_fingerprint"] is False
    assert report["privacy"]["contains_text_fingerprint"] is False
    assert report["privacy"]["contains_service_hostname"] is False


@pytest.mark.parametrize(
    "client_version",
    ["2.25.0", "2.26", "v2.26.0", "2.26.0rc1", "2.26.0+local"],
)
def test_probe_enforces_exact_final_client_before_artifacts(
    tmp_path, client_version
):
    artifact_dir = tmp_path / "must-not-exist"

    with pytest.raises(RuntimeError, match="==2.26.0"):
        probe.run_probe(
            RecordingService(lambda _kwargs: [Response(PCM_CHUNK)]),
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-ES",
            voice=canary.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            artifact_dir=artifact_dir,
            grpc_uri="localhost:50054",
            client_version=client_version,
            timer_factory=timer_factory,
            quiet=True,
        )

    assert not artifact_dir.exists()


def test_probe_enforces_pinned_provenance_before_artifacts(tmp_path):
    artifact_dir = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="pinned semantic-version"):
        probe.run_probe(
            RecordingService(lambda _kwargs: [Response(PCM_CHUNK)]),
            riva_client_module=riva_module(),
            text=PRIVATE_TEXT,
            tts_locale="es-ES",
            voice=canary.DEFAULT_VOICE,
            sample_rate_hz=22_050,
            artifact_dir=artifact_dir,
            grpc_uri="localhost:50054",
            client_version="2.26.0",
            container_image=(
                "nvcr.io/nim/nvidia/"
                "chatterbox-tts-multilingual:latest"
            ),
            timer_factory=timer_factory,
            quiet=True,
        )

    assert not artifact_dir.exists()


class FakeChannel:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeAuth:
    def __init__(self, uri):
        self.uri = uri
        self.channel = FakeChannel()


def fake_cli_module(response_factory):
    service = RecordingService(response_factory)
    module = riva_module()
    module.Auth = FakeAuth
    module.SpeechSynthesisService = lambda _auth: service
    return module, service


def test_cli_returns_zero_for_accepted_smoke(
    tmp_path, monkeypatch, capsys
):
    module, service = fake_cli_module(
        lambda _kwargs: [Response(PCM_CHUNK)]
    )
    monkeypatch.setattr(
        probe, "_load_client", lambda: ("2.26.0", module)
    )

    exit_code = probe.main(
        [
            "--artifact-dir",
            str(tmp_path / "accepted"),
            "--quiet",
        ]
    )

    assert exit_code == 0
    assert len(service.calls) == 6
    assert capsys.readouterr().err == ""


def test_cli_returns_distinct_nonzero_when_balanced_gate_not_met(
    tmp_path, monkeypatch, capsys
):
    module, service = fake_cli_module(
        lambda _kwargs: [Response(PCM_CHUNK)]
    )
    monkeypatch.setattr(
        probe, "_load_client", lambda: ("2.26.0", module)
    )
    artifact_dir = tmp_path / "balanced-no-effect"

    exit_code = probe.main(
        [
            "--artifact-dir",
            str(artifact_dir),
            "--balanced-matrix",
            "--repeats-per-cell",
            "5",
            "--quiet",
        ]
    )

    report = json.loads(
        (artifact_dir / probe.REPORT_FILENAME).read_text(encoding="utf-8")
    )
    assert exit_code == probe.BALANCED_GATE_NOT_MET_EXIT_CODE
    assert len(service.calls) == 41
    assert report["compatibility"]["classification"] == "accepted"
    assert report["effect"]["classification"] == "effect_not_demonstrated"
    assert (
        report["realtime_candidate"]["classification"]
        == "not_evaluated"
    )
    assert capsys.readouterr().err == ""


def test_cli_returns_one_for_deterministic_rejection(
    tmp_path, monkeypatch
):
    class Rejected(RuntimeError):
        def code(self):
            return SimpleNamespace(name="INVALID_ARGUMENT")

    def responses(kwargs):
        if "cfg_weight" in kwargs["custom_configuration"]:
            raise Rejected(PRIVATE_TEXT)
        return [Response(PCM_CHUNK)]

    module, _service = fake_cli_module(responses)
    monkeypatch.setattr(
        probe, "_load_client", lambda: ("2.26.0", module)
    )

    assert (
        probe.main(
            [
                "--artifact-dir",
                str(tmp_path / "rejected"),
                "--quiet",
            ]
        )
        == 1
    )


def test_failed_discarded_warmup_forces_inconclusive_and_nonzero(
    tmp_path, monkeypatch
):
    def responses(_kwargs):
        if len(service.calls) == 1:
            raise RuntimeError(PRIVATE_TEXT)
        return [Response(PCM_CHUNK)]

    service = RecordingService(responses)
    module = riva_module()
    module.Auth = FakeAuth
    module.SpeechSynthesisService = lambda _auth: service
    monkeypatch.setattr(
        probe, "_load_client", lambda: ("2.26.0", module)
    )
    artifact_dir = tmp_path / "failed-warmup"

    exit_code = probe.main(
        [
            "--artifact-dir",
            str(artifact_dir),
            "--quiet",
        ]
    )

    report = json.loads(
        (artifact_dir / probe.REPORT_FILENAME).read_text(encoding="utf-8")
    )
    assert exit_code == 1
    assert len(service.calls) == 6
    assert report["warmup"] == {
        "status": "failed",
        "exaggeration_factor": 0.5,
        "cfg_weight_setting": "omitted",
        "audio_artifact_written": False,
    }
    assert report["compatibility"]["classification"] == "inconclusive"
    assert report["compatibility"]["warmup_healthy"] is False
    assert report["summary"]["succeeded"] == 5
    assert report["summary"]["warmup_status"] == "failed"
    assert PRIVATE_TEXT not in json.dumps(report)


def test_cli_returns_two_for_setup_failure_without_leaking_details(
    tmp_path, capsys
):
    exit_code = probe.main(
        [
            "--artifact-dir",
            str(tmp_path / "unused"),
            "--repeats-per-cell",
            "0",
            "--text",
            PRIVATE_TEXT,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == "cfg_weight probe setup failed.\n"
    assert PRIVATE_TEXT not in captured.err
    assert not (tmp_path / "unused").exists()
