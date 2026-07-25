import copy
import json
import struct
import wave

import asr_word_timing_shape_diagnostic as diagnostic
import pytest


ATTESTATION = {
    "schema_version": 2,
    "verified": True,
    "health": "healthy",
    "host_port": 50052,
    "container_port": 50052,
    "local_image_id": "sha256:" + "ab" * 32,
    "repository_digest": "sha256:" + "cd" * 32,
    "profile_selector_sha256": "ef" * 32,
    "container_instance_sha256": "12" * 32,
}
ATTEMPT_ID = "12" * 16
ATTEMPT_STARTED_AT_UTC = "2026-07-25T00:00:00+00:00"


@pytest.fixture(autouse=True)
def registered_asr_config(monkeypatch):
    monkeypatch.setattr(
        diagnostic.riva_config,
        "source_language",
        diagnostic.REGISTERED_SOURCE_LANGUAGE,
    )
    monkeypatch.setattr(
        diagnostic.riva_config,
        "endpointing_history_ms",
        diagnostic.REGISTERED_EOU_MS,
    )
    monkeypatch.setattr(
        diagnostic.riva_config,
        "asr_word_time_offsets",
        True,
    )
    monkeypatch.setattr(
        diagnostic.riva_config,
        "asr_image_digest",
        ATTESTATION["repository_digest"],
    )


def write_pcm_wave(path, samples=(1, 2, 3, 4)):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def timing_diagnostics(*, shape="reversed"):
    valid = 1 if shape == "valid" else 0
    reversed_count = 1 if shape == "reversed" else 0
    anomaly = (
        []
        if shape == "valid"
        else [
            {
                "word_index": 0,
                "start": {
                    "presence": "unobservable",
                    "numeric_class": "positive",
                    "finite_value_ms": 100,
                },
                "end": {
                    "presence": "unobservable",
                    "numeric_class": "zero",
                    "finite_value_ms": 0,
                },
                "numeric_relation": "end_before_start",
                "shape": shape,
            }
        ]
    )
    return {
        "word_entry_count": 1,
        "no_word_entries": False,
        "envelope": {
            "start_word_index": 0,
            "end_word_index": 0,
            "start": {
                "presence": "unobservable",
                "numeric_class": "positive",
                "finite_value_ms": 100,
            },
            "end": {
                "presence": "unobservable",
                "numeric_class": (
                    "positive" if shape == "valid" else "zero"
                ),
                "finite_value_ms": 200 if shape == "valid" else 0,
            },
            "numeric_relation": (
                "end_after_start"
                if shape == "valid"
                else "end_before_start"
            ),
            "shape": shape,
            "usable": shape == "valid",
        },
        "counts": {
            "entry_shape": {
                "valid": valid,
                "absent_boundary": 0,
                "unparseable_boundary": 0,
                "nonfinite_boundary": 0,
                "negative_boundary": 0,
                "zero_length": 0,
                "reversed": reversed_count,
            },
            "start_numeric_class": {
                "not_available": 0,
                "unparseable": 0,
                "nonfinite": 0,
                "negative": 0,
                "zero": 0,
                "positive": 1,
            },
            "end_numeric_class": {
                "not_available": 0,
                "unparseable": 0,
                "nonfinite": 0,
                "negative": 0,
                "zero": reversed_count,
                "positive": valid,
            },
            "start_presence": {
                "absent": 0,
                "present": 0,
                "unobservable": 1,
            },
            "end_presence": {
                "absent": 0,
                "present": 0,
                "unobservable": 1,
            },
        },
        "anomalies": anomaly,
    }


def successful_run(
    *,
    input_completed=True,
    realtime_pacing=True,
    source_wav_sha256=None,
):
    return {
        "run_number": 1,
        "started_at_utc": "2026-07-25T00:01:00+00:00",
        "wall_seconds": 10.1,
        "audio_seconds_sent": 10.0,
        "source_sample_count": 159_999,
        "source_wav_sha256": source_wav_sha256,
        "padded_pcm_sample_count": 160_000,
        "padded_pcm_sha256": "ef" * 32,
        "input_completed": input_completed,
        "realtime_pacing": realtime_pacing,
        "pacing_basis": "absolute_chunk_end_deadlines_v1",
        "chunk_release_count": 100,
        "maximum_chunk_release_lateness_ms": (
            1.0 if realtime_pacing else 300.0
        ),
        "mean_chunk_release_lateness_ms": 0.1,
        "interim_count": 5,
        "attribution": {
            "schema_version": 2,
            "nonempty_final_count": 1,
            "final_with_word_offsets_count": 0,
            "final_missing_word_offsets_count": 1,
            "missing_word_offset_final_ids": [0],
            "timing_basis_counts": {
                "audio_processed_end_only": 0,
                "incomplete_word_offsets": 1,
                "unavailable": 0,
                "word_offsets": 0,
            },
            "all_nonempty_finals_have_word_offsets": False,
            "word_timing_shape_diagnostic_final_count": 1,
            "word_timing_shape_diagnostic_unavailable_final_count": 0,
            "word_timing_shape_diagnostic_unavailable_final_ids": [],
            "no_word_entries_final_count": 0,
            "no_word_entries_final_ids": [],
            "envelope_shape_counts": {
                "no_word_entries": 0,
                "valid": 0,
                "absent_boundary": 0,
                "unparseable_boundary": 0,
                "nonfinite_boundary": 0,
                "negative_boundary": 0,
                "zero_length": 0,
                "reversed": 1,
            },
            "envelope_shape_final_ids": {
                "no_word_entries": [],
                "valid": [],
                "absent_boundary": [],
                "unparseable_boundary": [],
                "nonfinite_boundary": [],
                "negative_boundary": [],
                "zero_length": [],
                "reversed": [0],
            },
            "word_entry_shape_counts": {
                "valid": 0,
                "absent_boundary": 0,
                "unparseable_boundary": 0,
                "nonfinite_boundary": 0,
                "negative_boundary": 0,
                "zero_length": 0,
                "reversed": 1,
            },
            "start_numeric_class_counts": {
                "not_available": 0,
                "unparseable": 0,
                "nonfinite": 0,
                "negative": 0,
                "zero": 0,
                "positive": 1,
            },
            "end_numeric_class_counts": {
                "not_available": 0,
                "unparseable": 0,
                "nonfinite": 0,
                "negative": 0,
                "zero": 1,
                "positive": 0,
            },
            "start_presence_counts": {
                "absent": 0,
                "present": 0,
                "unobservable": 1,
            },
            "end_presence_counts": {
                "absent": 0,
                "present": 0,
                "unobservable": 1,
            },
            "anomalous_word_entry_count": 1,
            "finals": [
                {
                    "final_id": 0,
                    "text_chars": 12,
                    "word_count": 1,
                    "audio_processed_s": 10.0,
                    "first_word_start_ms": 100.0,
                    "last_word_end_ms": None,
                    "source_start_ms": None,
                    "source_end_ms": 10_000.0,
                    "timing_basis": "incomplete_word_offsets",
                    "received_monotonic_ms": 1_000.0,
                    "word_timing_shape_diagnostics": timing_diagnostics(),
                }
            ],
        },
        # A failed formal-attribution result is expected for this anomaly and
        # must not affect or leak into the diagnostic outcome.
        "passed": False,
    }


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


def bind_run_to_audio(audio, run):
    binding = diagnostic._compute_exact_wav_pcm_binding(audio)
    bound = copy.deepcopy(run)
    bound["source_wav_sha256"] = binding["wav_sha256"]
    bound["source_sample_count"] = binding["source_sample_count"]
    bound["padded_pcm_sample_count"] = binding["padded_pcm_sample_count"]
    bound["padded_pcm_sha256"] = binding["padded_pcm_sha256"]
    return bound


def build(audio, run=None, before=ATTESTATION, after=ATTESTATION):
    input_sha256 = diagnostic._sha256(audio)
    if run is not None:
        run = bind_run_to_audio(audio, run)
    return diagnostic.build_report(
        audio_path=audio,
        uri="127.0.0.1:50052",
        run=run,
        runtime_attestation_before=before,
        runtime_attestation_after=after,
        input_wav_sha256_before=input_sha256,
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )


def test_parser_has_exactly_one_run_and_rejects_runs_option():
    args = diagnostic.build_parser().parse_args(
        ["--docker-container", "private-local-name"]
    )

    assert not hasattr(args, "runs")
    with pytest.raises(SystemExit):
        diagnostic.build_parser().parse_args(
            [
                "--docker-container",
                "private-local-name",
                "--runs",
                "2",
            ]
        )


def test_anomalous_offsets_can_complete_without_qualification_fields(
    tmp_path,
):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)

    report = build(audio, successful_run())
    encoded = json.dumps(report, allow_nan=False)

    assert report["artifact_kind"] == (
        "asr_word_timing_shape_diagnostic"
    )
    assert report["diagnostic_only"] is True
    assert report["qualification_eligible"] is False
    assert report["qualification_status"] == "not_evaluated"
    assert report["completion_status"] == "complete"
    assert report["requested_run_count"] == 1
    assert report["completed_run_count"] == 1
    assert report["capture_complete"] is True
    assert report["runs"][0]["capture_complete"] is True
    assert {"passed", "gate"}.isdisjoint(all_keys(report))
    assert "transcript" not in encoded
    assert "private-local-name" not in encoded
    assert "127.0.0.1:50052" not in encoded


def test_main_invalidates_stale_report_and_survives_interruption(
    tmp_path,
    monkeypatch,
):
    audio = tmp_path / "input.wav"
    output = tmp_path / "diagnostic.json"
    write_pcm_wave(audio)
    output.write_text(
        '{"passed": true, "stale": "secret transcript"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostic.riva_config,
        "asr_word_time_offsets",
        True,
    )
    monkeypatch.setattr(
        diagnostic,
        "attest_local_asr_runtime",
        lambda **_kwargs: copy.deepcopy(ATTESTATION),
    )

    def interrupt(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(diagnostic, "run_once", interrupt)

    with pytest.raises(KeyboardInterrupt):
        diagnostic.main(
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
    assert report["completion_status"] == "failed"
    assert report["completed_run_count"] == 0
    assert report["capture_complete"] is False
    assert {"passed", "gate"}.isdisjoint(all_keys(report))
    assert "stale" not in report


def test_main_records_specific_early_validation_failure(tmp_path):
    missing_audio = tmp_path / "missing.wav"
    output = tmp_path / "diagnostic.json"
    output.write_text(
        '{"completion_status": "complete", "stale": true}\n',
        encoding="utf-8",
    )

    exit_code = diagnostic.main(
        [
            "--file",
            str(missing_audio),
            "--docker-container",
            "private-local-name",
            "--json-output",
            str(output),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert report["completion_status"] == "failed"
    assert report["failure_reason"] == "input_not_found"
    assert "stale" not in report


def test_argparse_failure_invalidates_stale_diagnostic(tmp_path):
    audio = tmp_path / "input.wav"
    output = tmp_path / "diagnostic.json"
    write_pcm_wave(audio)
    output.write_text(
        '{"completion_status": "complete", "stale": true}\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        diagnostic.main(
            [
                "--file",
                str(audio),
                "--docker-container",
                "private-local-name",
                "--progress-seconds",
                "not-a-number",
                "--json-output",
                str(output),
            ]
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["completion_status"] == "failed"
    assert report["failure_reason"] == "initializing_or_interrupted"
    assert "stale" not in report


def test_main_completes_even_when_formal_attribution_would_fail(
    tmp_path,
    monkeypatch,
    capsys,
):
    audio = tmp_path / "input.wav"
    output = tmp_path / "diagnostic.json"
    write_pcm_wave(audio)
    monkeypatch.setattr(
        diagnostic.riva_config,
        "asr_word_time_offsets",
        True,
    )
    monkeypatch.setattr(
        diagnostic,
        "attest_local_asr_runtime",
        lambda **_kwargs: copy.deepcopy(ATTESTATION),
    )
    monkeypatch.setattr(
        diagnostic,
        "run_once",
        lambda **_kwargs: bind_run_to_audio(audio, successful_run()),
    )

    exit_code = diagnostic.main(
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
    captured = capsys.readouterr()
    assert exit_code == 0
    assert report["completion_status"] == "complete"
    assert "DIAGNOSTIC COMPLETE" in captured.err
    assert "NOT QUALIFICATION EVIDENCE" in captured.err


@pytest.mark.parametrize(
    ("input_completed", "realtime_pacing"),
    [(False, True), (True, False)],
)
def test_incomplete_input_or_bad_pacing_fails_completion(
    tmp_path,
    input_completed,
    realtime_pacing,
):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)

    report = build(
        audio,
        successful_run(
            input_completed=input_completed,
            realtime_pacing=realtime_pacing,
        ),
    )

    assert report["completion_status"] == "failed"
    assert report["capture_complete"] is False
    assert report["runs"][0]["capture_complete"] is False
    assert report["failure_reason"] == "capture_integrity_failed"


def test_runtime_identity_change_fails_completion(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    changed = copy.deepcopy(ATTESTATION)
    changed["local_image_id"] = "sha256:" + "01" * 32

    report = build(
        audio,
        successful_run(),
        before=ATTESTATION,
        after=changed,
    )

    assert report["completion_status"] == "failed"
    assert report["capture_complete"] is False
    assert report["completed_run_count"] == 1
    assert report["failure_reason"] == "runtime_identity_changed"
    assert (
        report["asr"]["runtime_attestation"]["identity_stable"] is False
    )


def test_container_instance_change_fails_runtime_continuity(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    changed = copy.deepcopy(ATTESTATION)
    changed["container_instance_sha256"] = "01" * 32

    report = build(
        audio,
        successful_run(),
        before=ATTESTATION,
        after=changed,
    )

    assert report["completion_status"] == "failed"
    assert report["failure_reason"] == "runtime_identity_changed"
    assert (
        report["asr"]["runtime_attestation"]["identity_stable"] is False
    )


def test_capture_rejects_changed_source_wav_identity(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)

    report = diagnostic.build_report(
        audio_path=audio,
        uri="127.0.0.1:50052",
        run=successful_run(source_wav_sha256="01" * 32),
        runtime_attestation_before=ATTESTATION,
        runtime_attestation_after=ATTESTATION,
        input_wav_sha256_before=diagnostic._sha256(audio),
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )

    assert report["completion_status"] == "failed"
    assert report["failure_reason"] == "input_identity_changed"
    assert report["input"]["exact_source_wav_binding_verified"] is False


def test_capture_rejects_mismatched_padded_pcm_binding(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    run = bind_run_to_audio(audio, successful_run())
    run["padded_pcm_sha256"] = "01" * 32

    report = diagnostic.build_report(
        audio_path=audio,
        uri="127.0.0.1:50052",
        run=run,
        runtime_attestation_before=ATTESTATION,
        runtime_attestation_after=ATTESTATION,
        input_wav_sha256_before=diagnostic._sha256(audio),
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )

    assert report["completion_status"] == "failed"
    assert report["failure_reason"] == "padded_pcm_binding_mismatch"
    assert report["input"]["exact_padded_pcm_binding_verified"] is False


def test_capture_requires_registered_language_and_eou(tmp_path, monkeypatch):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    monkeypatch.setattr(
        diagnostic.riva_config,
        "endpointing_history_ms",
        300,
    )

    report = build(audio, successful_run())

    assert report["completion_status"] == "failed"
    assert report["failure_reason"] == "asr_configuration_mismatch"
    assert report["asr"]["registered_configuration_verified"] is False


def test_report_rejects_incomplete_verified_runtime_attestation(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    incomplete = {
        "schema_version": 2,
        "verified": True,
        "container_instance_sha256": "12" * 32,
    }

    with pytest.raises(ValueError, match="runtime_attestation"):
        build(
            audio,
            successful_run(),
            before=incomplete,
            after=incomplete,
        )


def test_report_rejects_contradictory_nested_and_aggregate_shapes(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    contradictory_entry = successful_run()
    anomaly = contradictory_entry["attribution"]["finals"][0][
        "word_timing_shape_diagnostics"
    ]["anomalies"][0]
    anomaly["numeric_relation"] = "equal"
    anomaly["shape"] = "zero_length"

    with pytest.raises(ValueError, match="contradicts"):
        build(audio, contradictory_entry)

    contradictory_aggregate = successful_run()
    attribution = contradictory_aggregate["attribution"]
    attribution["envelope_shape_counts"]["reversed"] = 0
    attribution["envelope_shape_counts"]["valid"] = 1
    attribution["envelope_shape_final_ids"]["reversed"] = []
    attribution["envelope_shape_final_ids"]["valid"] = [0]

    with pytest.raises(ValueError, match="contradict"):
        build(audio, contradictory_aggregate)


def test_report_rejects_content_in_run_timestamp(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    run = successful_run()
    run["started_at_utc"] = "private spoken content"

    with pytest.raises(ValueError, match="UTC timestamp"):
        build(audio, run)


def test_report_rejects_unknown_pii_content_and_nonfinite_values(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    unknown_top_level = successful_run()
    unknown_top_level["attribution"]["customer_name"] = "private customer"
    unknown_nested = successful_run()
    unknown_nested["attribution"]["finals"][0][
        "word_timing_shape_diagnostics"
    ]["partner_name"] = "private partner"
    secret_in_allowed_scalar = successful_run()
    secret_in_allowed_scalar["attribution"]["finals"][0]["final_id"] = (
        "private spoken content"
    )
    with_nan = successful_run()
    with_nan["attribution"]["finals"][0][
        "word_timing_shape_diagnostics"
    ]["envelope"]["start"]["finite_value_ms"] = float("nan")

    with pytest.raises(ValueError, match="schema"):
        build(audio, unknown_top_level)
    with pytest.raises(ValueError, match="schema"):
        build(audio, unknown_nested)
    with pytest.raises(ValueError, match="non-negative integer"):
        build(audio, secret_in_allowed_scalar)
    with pytest.raises(ValueError):
        build(audio, with_nan)


def test_outer_validator_rejects_unknown_report_and_run_content(tmp_path):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    report = build(audio, successful_run())

    unknown_outer = copy.deepcopy(report)
    unknown_outer["customer_name"] = "private customer"
    unknown_run = copy.deepcopy(report)
    unknown_run["runs"][0]["transcript"] = "private spoken content"
    forged_initial = diagnostic._initial_report(
        attempt_id=ATTEMPT_ID,
        attempt_started_at_utc=ATTEMPT_STARTED_AT_UTC,
    )
    forged_initial["completed_run_count"] = 1
    forged_initial["runs"] = [{"transcript": "private spoken content"}]

    with pytest.raises(ValueError, match="outer schema"):
        diagnostic._validate_report(unknown_outer)
    with pytest.raises(ValueError, match="run"):
        diagnostic._validate_report(unknown_run)
    with pytest.raises(ValueError, match="checkpoint schema"):
        diagnostic._validate_report(forged_initial)


def test_malformed_declared_digest_is_not_retained(tmp_path, monkeypatch):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    monkeypatch.setattr(
        diagnostic.riva_config,
        "asr_image_digest",
        "private.person@example.invalid",
    )

    report = build(audio, successful_run())
    encoded = json.dumps(report, allow_nan=False)

    assert report["completion_status"] == "failed"
    assert report["failure_reason"] == "runtime_image_binding_mismatch"
    assert report["asr"]["declared_image_digest"] is None
    assert "private.person" not in encoded

    forged = copy.deepcopy(report)
    forged["asr"]["declared_image_digest"] = (
        "private.person@example.invalid"
    )
    with pytest.raises(ValueError, match="declared_image_digest"):
        diagnostic._validate_report(forged)


def test_output_collision_never_overwrites_input_or_formal_artifact(
    tmp_path,
):
    audio = tmp_path / "input.wav"
    write_pcm_wave(audio)
    original_audio = audio.read_bytes()

    same_path_exit = diagnostic.main(
        [
            "--file",
            str(audio),
            "--docker-container",
            "private-local-name",
            "--json-output",
            str(audio),
        ]
    )

    formal = tmp_path / "formal.json"
    formal_content = (
        '{"gate": "asr_final_word_attribution", "passed": true}\n'
    )
    formal.write_text(formal_content, encoding="utf-8")
    formal_exit = diagnostic.main(
        [
            "--file",
            str(audio),
            "--docker-container",
            "private-local-name",
            "--json-output",
            str(formal),
        ]
    )

    assert same_path_exit == 2
    assert formal_exit == 2
    assert audio.read_bytes() == original_audio
    assert formal.read_text(encoding="utf-8") == formal_content
