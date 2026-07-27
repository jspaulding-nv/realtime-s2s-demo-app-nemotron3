import hashlib
import json
import os
import stat
import wave
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import analyze_scheduled_semantic_delay as analyzer
from headless_playback_scheduler import HeadlessPlaybackScheduler
from playback_simulation import (
    DEFAULT_PLAYBACK_POLICY,
    AudioChunk,
    simulate_playback,
)
from private_pcm_schedule_ledger import PrivatePcmScheduleCapture


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_wav(path: Path, pcm: bytes, *, sample_rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)


def write_json(path: Path, value: dict[str, Any]) -> bytes:
    payload = (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    path.write_bytes(payload)
    return payload


def build_bundle(
    tmp_path: Path,
    *,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_pcm = (
        b"\x01\x00" * 8_000
        + b"\x02\x00" * 8_000
    )
    translated_frame_pcm = (
        b"\x03\x00" * 16_000,
        b"\x04\x00" * 16_000,
    )
    translated_pcm = b"".join(translated_frame_pcm)
    source_wav = tmp_path / analyzer.SOURCE_REVIEW_WAV_FILENAME
    translated_wav = (
        tmp_path / analyzer.TRANSLATED_REVIEW_WAV_FILENAME
    )
    write_wav(source_wav, source_pcm)
    write_wav(translated_wav, translated_pcm)
    source_wav_bytes = source_wav.read_bytes()
    translated_wav_bytes = translated_wav.read_bytes()

    arrivals = (1.25, 2.0)
    replay = simulate_playback(
        tuple(
            AudioChunk(
                arrival_seconds=arrival,
                duration_seconds=1.0,
                audio_bytes=32_000,
                source_index=index,
            )
            for index, arrival in enumerate(arrivals)
        ),
        input_end_seconds=1.0,
        adaptive=True,
        policy=DEFAULT_PLAYBACK_POLICY,
    )
    frames = []
    parents = []
    for index, scheduled in enumerate(replay.schedule):
        sample_start = index * 16_000
        sample_end = sample_start + 16_000
        source_start = index * 500.0
        source_end = source_start + 500.0
        frames.append(
            {
                "source_index": index,
                "stream_generation": 1,
                "parent_sequence_id": index,
                "audio_frame_id": 0,
                "audio_bytes": 32_000,
                "sample_count": 16_000,
                "translated_sample_start": sample_start,
                "translated_sample_end_exclusive": sample_end,
                "pcm_sha256": sha256(translated_frame_pcm[index]),
                "source_start_ms": source_start,
                "source_end_ms": source_end,
                "arrival_seconds": arrivals[index],
                "scheduled_start_seconds": scheduled.start_seconds,
                "scheduled_end_seconds": scheduled.end_seconds,
                "playback_rate": scheduled.playback_rate,
                "playback_mode": scheduled.playback_mode,
            }
        )
        parents.append(
            {
                "stream_generation": 1,
                "parent_sequence_id": index,
                "audio_frame_count": 1,
                "audio_bytes": 32_000,
                "translated_sample_start": sample_start,
                "translated_sample_end_exclusive": sample_end,
                "source_start_ms": source_start,
                "source_end_ms": source_end,
                "completion_received_seconds": arrivals[index] + 0.1,
            }
        )

    ledger = {
        "schema_version": 1,
        "report_type": "private_pcm_schedule_ledger",
        "capture": {
            "audio_metadata_protocol_version": 1,
            "stream_generation": 1,
            "terminal_completed": True,
            "input_pacing_mode": "chunk_end_boundary_v1",
            "clock": "client_monotonic_from_capture_start",
            "input_sample_zero_seconds": 0.0,
            "input_end_seconds": 1.0,
            "source_chunk_count": 2,
            "parent_count": 2,
            "frame_count": 2,
            "canonical_replay_verified": True,
        },
        "source_pcm": {
            "sample_rate_hz": 16_000,
            "channels": 1,
            "bytes_per_sample": 2,
            "sample_count": 16_000,
            "audio_bytes": len(source_pcm),
            "pcm_sha256": sha256(source_pcm),
            "review_wav_sha256": sha256(source_wav_bytes),
        },
        "translated_pcm": {
            "sample_rate_hz": 16_000,
            "channels": 1,
            "bytes_per_sample": 2,
            "sample_count": 32_000,
            "audio_bytes": len(translated_pcm),
            "pcm_sha256": sha256(translated_pcm),
            "review_wav_sha256": sha256(translated_wav_bytes),
        },
        "policy": asdict(DEFAULT_PLAYBACK_POLICY),
        "source_chunks": [
            {
                "chunk_index": 0,
                "sample_start": 0,
                "sample_end_exclusive": 8_000,
                "audio_bytes": 16_000,
                "deadline_seconds": 0.5,
                "emitted_seconds": 0.5,
            },
            {
                "chunk_index": 1,
                "sample_start": 8_000,
                "sample_end_exclusive": 16_000,
                "audio_bytes": 16_000,
                "deadline_seconds": 1.0,
                "emitted_seconds": 1.0,
            },
        ],
        "frames": frames,
        "parents": parents,
        "privacy": {
            "contains_private_audio_in_companion_wavs": True,
            "ledger_contains_pcm": False,
            "contains_transcript_or_translation_text": False,
            "contains_file_path_or_uri": False,
            "contains_wall_clock_timestamp": False,
            "review_required_before_sharing": True,
        },
    }
    ledger_path = tmp_path / analyzer.SCHEDULE_LEDGER_FILENAME
    ledger_bytes = write_json(ledger_path, ledger)
    marker_event = event or {
        "event_id": "event-001",
        "source_sample_index": 8_000,
        "translated_sample_index": 8_000,
        "source_independent_reviewer_count": 2,
        "translated_independent_reviewer_count": 2,
    }
    markers = {
        "schema_version": 1,
        "schedule_ledger_sha256": sha256(ledger_bytes),
        "source_pcm_sha256": sha256(source_pcm),
        "source_pcm_sample_count": 16_000,
        "translated_pcm_sha256": sha256(translated_pcm),
        "translated_pcm_sample_count": 32_000,
        "events": [marker_event],
    }
    markers_path = tmp_path / analyzer.REVIEWER_MARKERS_FILENAME
    write_json(markers_path, markers)
    return {
        "source_wav": source_wav,
        "translated_wav": translated_wav,
        "ledger_path": ledger_path,
        "markers_path": markers_path,
        "ledger": ledger,
        "markers": markers,
    }


def rewrite_ledger(
    bundle: dict[str, Any],
    mutator: Callable[[dict[str, Any]], None],
    *,
    rebind: bool = True,
) -> None:
    mutator(bundle["ledger"])
    payload = write_json(bundle["ledger_path"], bundle["ledger"])
    if rebind:
        bundle["markers"]["schedule_ledger_sha256"] = sha256(payload)
        write_json(bundle["markers_path"], bundle["markers"])


def rewrite_markers(
    bundle: dict[str, Any],
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    mutator(bundle["markers"])
    write_json(bundle["markers_path"], bundle["markers"])


def analyze(
    bundle: dict[str, Any],
    *,
    maximum: float = 1.3,
    minimum_reviewers: int = 2,
) -> dict[str, Any]:
    return analyzer.analyze_scheduled_semantic_delay(
        bundle["ledger_path"],
        bundle["source_wav"],
        bundle["translated_wav"],
        bundle["markers_path"],
        maximum,
        minimum_reviewers=minimum_reviewers,
    )


def cli_args(bundle: dict[str, Any], maximum: float) -> list[str]:
    return [
        "--schedule-ledger",
        str(bundle["ledger_path"]),
        "--source-wav",
        str(bundle["source_wav"]),
        "--translated-wav",
        str(bundle["translated_wav"]),
        "--markers-json",
        str(bundle["markers_path"]),
        "--max-latency-seconds",
        str(maximum),
    ]


def test_pass_maps_target_sample_and_reports_one_sample_bounds(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)

    result = analyze(bundle, maximum=1.3)

    assert result["summary"]["event_count"] == 1
    assert result["summary"]["status_counts"] == {
        "pass": 1,
        "inconclusive": 0,
        "fail": 0,
    }
    assert result["summary"]["overall_status"] == "pass"
    distribution = result["summary"][
        "latency_bound_distribution_seconds"
    ]
    for statistic in ("minimum", "median", "maximum"):
        assert distribution["lower_bound_seconds"][statistic] == (
            pytest.approx(1.2499375)
        )
        assert distribution["upper_bound_seconds"][statistic] == (
            pytest.approx(1.2500625)
        )
    event = result["events"][0]
    assert event["source_offset_ms"] == 500.0
    assert event["attributed_source_range_ms"] == {
        "start": 0.0,
        "end": 500.0,
        "endpoint_convention": "closed",
    }
    assert event["mapped_frame"] == {
        "stream_generation": 1,
        "parent_sequence_id": 0,
        "audio_frame_id": 0,
        "translated_frame_sample_offset": 8_000,
    }
    assert event["source_sample_interval_seconds"] == {
        "lower_inclusive": 0.5,
        "upper_exclusive": 0.5000625,
    }
    target_interval = event[
        "scheduled_target_sample_interval_seconds"
    ]
    assert target_interval["lower_inclusive"] == 1.75
    assert target_interval["upper_exclusive"] == pytest.approx(
        1.7500625
    )
    assert target_interval["playback_rate"] == 1.0
    assert target_interval["playback_mode"] == "normal"
    bounds = event["scheduled_semantic_delay_bounds_seconds"]
    assert bounds["lower"] == pytest.approx(1.2499375)
    assert bounds["upper"] == pytest.approx(1.2500625)
    assert bounds["bound_width"] == pytest.approx(0.000125)
    assert (
        result["evidence"][
            "canonical_adaptive_schedule_replay_verified"
        ]
        is True
    )
    assert result["claim_scope"] == {
        "reviewed_source_semantic_landmark": True,
        "reviewed_translated_semantic_landmark": True,
        "scheduled_digital_semantic_delay": True,
        "dac_or_acoustic_audibility_proven": False,
        "room_reaction_synchronization_proven": False,
        "translation_quality_proven": False,
    }
    serialized = json.dumps(result)
    assert str(tmp_path) not in serialized
    assert "Texto" not in serialized


def test_event_rejects_target_parent_without_source_range(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)

    def remove_source_range(ledger: dict[str, Any]) -> None:
        for collection in ("frames", "parents"):
            ledger[collection][0]["source_start_ms"] = None
            ledger[collection][0]["source_end_ms"] = None

    rewrite_ledger(bundle, remove_source_range)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="lacks a complete attributed source range",
    ):
        analyze(bundle)


def test_event_rejects_end_only_source_range(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)

    def make_end_only(ledger: dict[str, Any]) -> None:
        for collection in ("frames", "parents"):
            ledger[collection][0]["source_start_ms"] = None

    rewrite_ledger(bundle, make_end_only)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="lacks a complete attributed source range",
    ):
        analyze(bundle)


def test_end_only_range_is_allowed_for_unreviewed_parent(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)

    def make_unreviewed_parent_end_only(ledger: dict[str, Any]) -> None:
        ledger["frames"][1]["source_start_ms"] = None
        ledger["parents"][1]["source_start_ms"] = None

    rewrite_ledger(bundle, make_unreviewed_parent_end_only)

    assert analyze(bundle)["summary"]["overall_status"] == "pass"


def test_event_rejects_source_marker_outside_target_parent_range(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)

    def shorten_source_range(ledger: dict[str, Any]) -> None:
        for collection in ("frames", "parents"):
            ledger[collection][0]["source_end_ms"] = 499.999

    rewrite_ledger(bundle, shorten_source_range)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="outside its target parent's attributed source range",
    ):
        analyze(bundle)


def test_target_source_range_allows_one_sample_duration_tolerance(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)

    def extend_to_tolerance(ledger: dict[str, Any]) -> None:
        for collection in ("frames", "parents"):
            ledger[collection][0]["source_end_ms"] = 1_000.0625

    rewrite_ledger(bundle, extend_to_tolerance)

    assert analyze(bundle)["summary"]["overall_status"] == "pass"


def test_target_source_range_rejects_more_than_one_sample_past_pcm(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)

    def extend_past_tolerance(ledger: dict[str, Any]) -> None:
        for collection in ("frames", "parents"):
            ledger[collection][0]["source_end_ms"] = 1_000.062501

    rewrite_ledger(bundle, extend_past_tolerance)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="exceeds the source PCM duration",
    ):
        analyze(bundle)


@pytest.mark.parametrize(
    ("maximum", "expected"),
    [
        (1.3, "pass"),
        (1.2, "fail"),
        (1.25, "inconclusive"),
    ],
)
def test_gate_uses_complete_conservative_bound(
    tmp_path: Path,
    maximum: float,
    expected: str,
) -> None:
    bundle = build_bundle(tmp_path)

    result = analyze(bundle, maximum=maximum)

    assert result["events"][0]["status"] == expected
    assert result["summary"]["overall_status"] == expected


def test_report_preserves_precision_supporting_gate_status(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    maximum = 1.2499374998

    result = analyze(bundle, maximum=maximum)

    lower = result["events"][0][
        "scheduled_semantic_delay_bounds_seconds"
    ]["lower"]
    assert result["summary"]["overall_status"] == "fail"
    assert result["maximum_latency_seconds"] == maximum
    assert lower > result["maximum_latency_seconds"]


def test_target_sample_maps_across_frame_boundary(tmp_path: Path) -> None:
    bundle = build_bundle(
        tmp_path,
        event={
            "event_id": "event-002",
            "source_sample_index": 12_000,
            "translated_sample_index": 24_000,
            "source_independent_reviewer_count": 2,
            "translated_independent_reviewer_count": 3,
        },
    )

    result = analyze(bundle, maximum=2.1)

    event = result["events"][0]
    assert event["mapped_frame"]["parent_sequence_id"] == 1
    assert event["mapped_frame"]["translated_frame_sample_offset"] == 8_000
    assert event["scheduled_target_sample_interval_seconds"][
        "lower_inclusive"
    ] == 2.75
    assert event["status"] == "pass"


def test_multiple_events_fail_dominates_inconclusive_and_pass(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    bundle["markers"]["events"] = [
        {
            "event_id": "event-001",
            "source_sample_index": 8_000,
            "translated_sample_index": 8_000,
            "source_independent_reviewer_count": 2,
            "translated_independent_reviewer_count": 2,
        },
        {
            "event_id": "event-002",
            "source_sample_index": 12_000,
            "translated_sample_index": 24_000,
            "source_independent_reviewer_count": 2,
            "translated_independent_reviewer_count": 2,
        },
    ]
    write_json(bundle["markers_path"], bundle["markers"])

    result = analyze(bundle, maximum=1.25)

    assert [event["status"] for event in result["events"]] == [
        "inconclusive",
        "fail",
    ]
    assert result["summary"]["overall_status"] == "fail"


@pytest.mark.parametrize(
    ("target", "field"),
    [
        ("ledger", "unexpected"),
        ("capture", "unexpected"),
        ("source_pcm", "unexpected"),
        ("frame", "unexpected"),
        ("parent", "unexpected"),
        ("privacy", "unexpected"),
    ],
)
def test_ledger_rejects_unknown_fields(
    tmp_path: Path,
    target: str,
    field: str,
) -> None:
    bundle = build_bundle(tmp_path)

    def mutate(ledger: dict[str, Any]) -> None:
        if target == "ledger":
            ledger[field] = 1
        elif target == "capture":
            ledger["capture"][field] = 1
        elif target == "source_pcm":
            ledger["source_pcm"][field] = 1
        elif target == "frame":
            ledger["frames"][0][field] = 1
        elif target == "parent":
            ledger["parents"][0][field] = 1
        else:
            ledger["privacy"][field] = 1

    rewrite_ledger(bundle, mutate)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="exact schema",
    ):
        analyze(bundle)


def test_sidecar_rejects_unknown_event_field(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    rewrite_markers(
        bundle,
        lambda markers: markers["events"][0].update({"note": "private"}),
    )

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="exact schema",
    ):
        analyze(bundle)


def test_duplicate_ledger_json_key_is_invalid(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    payload = bundle["ledger_path"].read_text(encoding="utf-8")
    payload = payload.replace(
        '"schema_version": 1,',
        '"schema_version": 1, "schema_version": 1,',
        1,
    )
    bundle["ledger_path"].write_text(payload, encoding="utf-8")

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="duplicate object key",
    ):
        analyze(bundle)


def test_duplicate_sidecar_json_key_is_invalid(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    payload = bundle["markers_path"].read_text(encoding="utf-8")
    payload = payload.replace(
        '"schema_version": 1,',
        '"schema_version": 1, "schema_version": 1,',
        1,
    )
    bundle["markers_path"].write_text(payload, encoding="utf-8")

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="duplicate object key",
    ):
        analyze(bundle)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_ledger_numbers_are_invalid(
    tmp_path: Path,
    value: str,
) -> None:
    bundle = build_bundle(tmp_path)
    payload = bundle["ledger_path"].read_text(encoding="utf-8")
    payload = payload.replace(
        '"arrival_seconds": 1.25',
        f'"arrival_seconds": {value}',
        1,
    )
    bundle["ledger_path"].write_text(payload, encoding="utf-8")

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="non-standard number",
    ):
        analyze(bundle)


def test_overflowing_integer_in_float_field_is_invalid(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)

    def overflow_arrival(ledger: dict[str, Any]) -> None:
        ledger["frames"][0]["arrival_seconds"] = 10**1_000

    rewrite_ledger(bundle, overflow_arrival)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="arrival_seconds must be finite",
    ):
        analyze(bundle)


def test_cli_overflowing_derived_cadence_returns_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = build_bundle(tmp_path)

    def overflow_cadence(ledger: dict[str, Any]) -> None:
        ledger["capture"]["input_end_seconds"] = 1e308
        for chunk in ledger["source_chunks"]:
            chunk["deadline_seconds"] = 1e308
            chunk["emitted_seconds"] = 1e308

    rewrite_ledger(bundle, overflow_cadence)

    assert analyzer.main(cli_args(bundle, 1.3)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Invalid semantic-delay evidence:")
    assert str(tmp_path) not in captured.err


def test_sidecar_must_bind_exact_ledger_bytes(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    bundle["ledger_path"].write_bytes(
        bundle["ledger_path"].read_bytes() + b" "
    )

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="does not bind",
    ):
        analyze(bundle)


def test_source_wav_full_file_hash_is_validated(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    bundle["source_wav"].write_bytes(
        bundle["source_wav"].read_bytes() + b"trailing"
    )

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="WAV hash",
    ):
        analyze(bundle)


def test_source_pcm_hash_is_validated_after_wav_binding(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    replacement_pcm = b"\x05\x00" * 16_000
    write_wav(bundle["source_wav"], replacement_pcm)
    new_wav_hash = sha256(bundle["source_wav"].read_bytes())

    def mutate(ledger: dict[str, Any]) -> None:
        ledger["source_pcm"]["review_wav_sha256"] = new_wav_hash

    rewrite_ledger(bundle, mutate)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="PCM hash",
    ):
        analyze(bundle)


def test_per_frame_pcm_hash_is_validated(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)

    def mutate(ledger: dict[str, Any]) -> None:
        ledger["frames"][0]["pcm_sha256"] = "0" * 64

    rewrite_ledger(bundle, mutate)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="translated PCM",
    ):
        analyze(bundle)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("scheduled_start_seconds", 1.251),
        ("scheduled_end_seconds", 2.251),
        ("playback_rate", 1.05),
        ("playback_mode", "catch-up"),
    ],
)
def test_saved_schedule_must_match_canonical_adaptive_replay(
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    bundle = build_bundle(tmp_path)

    def mutate(ledger: dict[str, Any]) -> None:
        ledger["frames"][0][field] = replacement

    rewrite_ledger(bundle, mutate)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="inconsistent|canonical replay",
    ):
        analyze(bundle)


def test_saved_schedule_tolerances_cannot_hide_duration_mismatch(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    bundle["ledger"]["frames"][0]["scheduled_start_seconds"] -= 0.75e-9
    bundle["ledger"]["frames"][0]["scheduled_end_seconds"] += 0.75e-9
    rewrite_ledger(bundle, lambda _ledger: None)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="scheduled_end_seconds is inconsistent",
    ):
        analyze(bundle)


def test_saved_schedule_tolerances_cannot_hide_frame_overlap(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    for field in ("scheduled_start_seconds", "scheduled_end_seconds"):
        bundle["ledger"]["frames"][0][field] += 0.75e-9
        bundle["ledger"]["frames"][1][field] -= 0.75e-9
    rewrite_ledger(bundle, lambda _ledger: None)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="scheduled playback order is inconsistent",
    ):
        analyze(bundle)


@pytest.mark.parametrize("replacement", [4.0, 5.0000000001])
def test_policy_must_equal_registered_default(
    tmp_path: Path,
    replacement: float,
) -> None:
    bundle = build_bundle(tmp_path)

    def mutate(ledger: dict[str, Any]) -> None:
        ledger["policy"]["target_queue_seconds"] = replacement

    rewrite_ledger(bundle, mutate)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="policy.target_queue_seconds",
    ):
        analyze(bundle)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda ledger: ledger["source_chunks"][1].update(
            {"sample_start": 7_999}
        ),
        lambda ledger: ledger["source_chunks"][0].update(
            {"emitted_seconds": 0.49}
        ),
        lambda ledger: ledger["capture"].update(
            {"source_chunk_count": 3}
        ),
        lambda ledger: (
            ledger["source_chunks"][0].update(
                {
                    "sample_end_exclusive": 7_999,
                    "audio_bytes": 15_998,
                }
            ),
            ledger["source_chunks"][1].update(
                {
                    "sample_start": 7_999,
                    "audio_bytes": 16_002,
                }
            ),
        ),
    ],
)
def test_source_chunk_ledger_is_fail_closed(
    tmp_path: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    bundle = build_bundle(tmp_path)
    rewrite_ledger(bundle, mutator)

    with pytest.raises(analyzer.ScheduledSemanticDelayError):
        analyze(bundle)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda ledger: ledger["frames"][1].update({"source_index": 0}),
        lambda ledger: ledger["frames"][1].update(
            {"translated_sample_start": 15_999}
        ),
        lambda ledger: ledger["parents"][1].update(
            {"audio_frame_count": 2}
        ),
        lambda ledger: ledger["parents"][0].update(
            {"stream_generation": 2}
        ),
        lambda ledger: ledger["parents"][0].update(
            {"completion_received_seconds": 1.0}
        ),
    ],
)
def test_frame_and_parent_reconciliation_is_fail_closed(
    tmp_path: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    bundle = build_bundle(tmp_path)
    rewrite_ledger(bundle, mutator)

    with pytest.raises(analyzer.ScheduledSemanticDelayError):
        analyze(bundle)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda markers: markers.update(
            {"source_pcm_sha256": "0" * 64}
        ),
        lambda markers: markers.update(
            {"source_pcm_sample_count": 15_999}
        ),
        lambda markers: markers.update(
            {"translated_pcm_sha256": "0" * 64}
        ),
        lambda markers: markers.update(
            {"translated_pcm_sample_count": 31_999}
        ),
    ],
)
def test_sidecar_pcm_bindings_are_exact(
    tmp_path: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    bundle = build_bundle(tmp_path)
    rewrite_markers(bundle, mutator)

    with pytest.raises(analyzer.ScheduledSemanticDelayError):
        analyze(bundle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sample_index", 16_000),
        ("translated_sample_index", 32_000),
        ("source_independent_reviewer_count", 1),
        ("translated_independent_reviewer_count", 1),
    ],
)
def test_reviewed_events_require_in_range_samples_and_two_reviewers(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    bundle = build_bundle(tmp_path)
    rewrite_markers(
        bundle,
        lambda markers: markers["events"][0].update({field: value}),
    )

    with pytest.raises(analyzer.ScheduledSemanticDelayError):
        analyze(bundle)


@pytest.mark.parametrize(
    ("source_index", "translated_index", "message"),
    [
        (8_000, 9_000, "source samples"),
        (9_000, 8_000, "translated samples"),
    ],
)
def test_reviewed_event_samples_must_be_unique(
    tmp_path: Path,
    source_index: int,
    translated_index: int,
    message: str,
) -> None:
    bundle = build_bundle(tmp_path)
    bundle["markers"]["events"].append(
        {
            "event_id": "event-002",
            "source_sample_index": source_index,
            "translated_sample_index": translated_index,
            "source_independent_reviewer_count": 2,
            "translated_independent_reviewer_count": 2,
        }
    )
    write_json(bundle["markers_path"], bundle["markers"])

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match=message,
    ):
        analyze(bundle)


def test_reviewed_event_ids_must_be_unique(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    bundle["markers"]["events"].append(
        {
            "event_id": "event-001",
            "source_sample_index": 9_000,
            "translated_sample_index": 9_000,
            "source_independent_reviewer_count": 2,
            "translated_independent_reviewer_count": 2,
        }
    )
    write_json(bundle["markers_path"], bundle["markers"])

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="event IDs must be unique",
    ):
        analyze(bundle)


def test_target_interval_cannot_precede_source_interval(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(
        tmp_path,
        event={
            "event_id": "event-001",
            "source_sample_index": 8_000,
            "translated_sample_index": 0,
            "source_independent_reviewer_count": 2,
            "translated_independent_reviewer_count": 2,
        },
    )
    bundle["ledger"]["capture"]["input_sample_zero_seconds"] = 2.0
    bundle["ledger"]["capture"]["input_end_seconds"] = 3.0
    bundle["ledger"]["source_chunks"][0]["deadline_seconds"] = 2.5
    bundle["ledger"]["source_chunks"][0]["emitted_seconds"] = 2.5
    bundle["ledger"]["source_chunks"][1]["deadline_seconds"] = 3.0
    bundle["ledger"]["source_chunks"][1]["emitted_seconds"] = 3.0
    ledger_bytes = write_json(bundle["ledger_path"], bundle["ledger"])
    bundle["markers"]["schedule_ledger_sha256"] = sha256(ledger_bytes)
    write_json(bundle["markers_path"], bundle["markers"])

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="precedes its source",
    ):
        analyze(bundle)


def test_target_interval_ending_at_source_start_is_invalid(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(
        tmp_path,
        event={
            "event_id": "event-001",
            "source_sample_index": 1,
            "translated_sample_index": 0,
            "source_independent_reviewer_count": 2,
            "translated_independent_reviewer_count": 2,
        },
    )
    bundle["ledger"]["capture"]["input_sample_zero_seconds"] = 1.25
    bundle["ledger"]["capture"]["input_end_seconds"] = 2.25
    bundle["ledger"]["source_chunks"][0]["deadline_seconds"] = 1.75
    bundle["ledger"]["source_chunks"][0]["emitted_seconds"] = 1.75
    bundle["ledger"]["source_chunks"][1]["deadline_seconds"] = 2.25
    bundle["ledger"]["source_chunks"][1]["emitted_seconds"] = 2.25
    ledger_bytes = write_json(bundle["ledger_path"], bundle["ledger"])
    bundle["markers"]["schedule_ledger_sha256"] = sha256(ledger_bytes)
    write_json(bundle["markers_path"], bundle["markers"])

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="precedes its source",
    ):
        analyze(bundle)


def test_large_clock_anchor_cannot_collapse_sample_intervals(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    shift = 1e13
    capture = bundle["ledger"]["capture"]
    capture["input_sample_zero_seconds"] += shift
    capture["input_end_seconds"] += shift
    for chunk in bundle["ledger"]["source_chunks"]:
        chunk["deadline_seconds"] += shift
        chunk["emitted_seconds"] += shift
    for frame in bundle["ledger"]["frames"]:
        frame["arrival_seconds"] += shift
        frame["scheduled_start_seconds"] += shift
        frame["scheduled_end_seconds"] += shift
    for parent in bundle["ledger"]["parents"]:
        parent["completion_received_seconds"] += shift
    rewrite_ledger(bundle, lambda _ledger: None)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="not a representable non-empty interval",
    ):
        analyze(bundle)


@pytest.mark.parametrize(
    ("maximum", "expected_exit"),
    [
        (1.3, 0),
        (1.2, 1),
        (1.25, 3),
    ],
)
def test_cli_exit_codes_follow_gate_status(
    tmp_path: Path,
    maximum: float,
    expected_exit: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = build_bundle(tmp_path)

    exit_code = analyzer.main(cli_args(bundle, maximum))

    assert exit_code == expected_exit
    output = json.loads(capsys.readouterr().out)
    assert (
        output["summary"]["overall_status"]
        == {0: "pass", 1: "fail", 3: "inconclusive"}[expected_exit]
    )


def test_cli_invalid_evidence_returns_two_without_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = build_bundle(tmp_path)
    bundle["markers"]["schedule_ledger_sha256"] = "0" * 64
    write_json(bundle["markers_path"], bundle["markers"])

    exit_code = analyzer.main(cli_args(bundle, 1.3))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith("Invalid semantic-delay evidence:")
    assert str(tmp_path) not in captured.err


def test_cli_deeply_nested_json_returns_invalid_evidence_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = build_bundle(tmp_path)
    bundle["ledger_path"].write_text(
        "[" * 2_000 + "0" + "]" * 2_000,
        encoding="utf-8",
    )

    exit_code = analyzer.main(cli_args(bundle, 1.3))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith("Invalid semantic-delay evidence:")
    assert str(tmp_path) not in captured.err


def test_cli_writes_private_atomic_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = build_bundle(tmp_path)
    report = tmp_path / "analysis" / "report.json"
    args = cli_args(bundle, 1.3) + ["--json-output", str(report)]

    exit_code = analyzer.main(args)

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert json.loads(report.read_text(encoding="utf-8"))[
        "summary"
    ]["overall_status"] == "pass"
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert not list(report.parent.glob(".*.tmp"))


def test_documented_cli_aliases_write_private_markdown(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    report = tmp_path / "analysis" / "report.md"
    args = [
        "--ledger",
        str(bundle["ledger_path"]),
        "--source-wav",
        str(bundle["source_wav"]),
        "--translated-wav",
        str(bundle["translated_wav"]),
        "--markers",
        str(bundle["markers_path"]),
        "--max-latency-seconds",
        "1.3",
        "--markdown-output",
        str(report),
    ]

    assert analyzer.main(args) == 0
    markdown = report.read_text(encoding="utf-8")
    assert "Overall result: **PASS**" in markdown
    assert "event-001" in markdown
    assert "physical audibility" in markdown
    assert "closed start/end containment" in markdown
    assert "half-open one-sample timing intervals" in markdown
    assert stat.S_IMODE(report.stat().st_mode) == 0o600


def test_output_paths_cannot_collide_with_each_other_or_inputs(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    shared = tmp_path / "shared"
    base = cli_args(bundle, 1.3)

    with pytest.raises(SystemExit) as same_output:
        analyzer.parse_cli_args(
            base
            + [
                "--json-output",
                str(shared),
                "--markdown-output",
                str(shared),
            ]
        )
    assert same_output.value.code == 2

    with pytest.raises(SystemExit) as overwrite_input:
        analyzer.parse_cli_args(
            base + ["--markdown-output", str(bundle["source_wav"])]
        )
    assert overwrite_input.value.code == 2


def test_non_pcm_or_wrong_rate_wav_is_invalid(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    replacement_pcm = b"\x01\x00" * 16_000
    write_wav(bundle["source_wav"], replacement_pcm, sample_rate=8_000)
    new_hash = sha256(bundle["source_wav"].read_bytes())

    def mutate(ledger: dict[str, Any]) -> None:
        ledger["source_pcm"]["review_wav_sha256"] = new_hash

    rewrite_ledger(bundle, mutate)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="PCM contract",
    ):
        analyze(bundle)


def test_symlink_evidence_is_rejected(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    link = tmp_path / "ledger-link.json"
    link.symlink_to(bundle["ledger_path"])

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="missing or unsafe",
    ):
        analyzer.analyze_scheduled_semantic_delay(
            link,
            bundle["source_wav"],
            bundle["translated_wav"],
            bundle["markers_path"],
            1.3,
        )


def test_symlink_loop_path_fails_with_cli_exit_two(
    tmp_path: Path,
) -> None:
    bundle = build_bundle(tmp_path)
    loop = tmp_path / "loop"
    loop.symlink_to("loop")
    args = cli_args(bundle, 1.3)
    ledger_index = args.index("--schedule-ledger") + 1
    args[ledger_index] = str(loop)

    with pytest.raises(SystemExit) as error:
        analyzer.parse_cli_args(args)

    assert error.value.code == 2


def test_maximum_and_reviewer_arguments_are_strict(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)

    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="max_latency_seconds",
    ):
        analyze(bundle, maximum=float("nan"))
    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="minimum_reviewers",
    ):
        analyzer.analyze_scheduled_semantic_delay(
            bundle["ledger_path"],
            bundle["source_wav"],
            bundle["translated_wav"],
            bundle["markers_path"],
            1.3,
            minimum_reviewers=True,
        )
    with pytest.raises(
        analyzer.ScheduledSemanticDelayError,
        match="greater than or equal to 2",
    ):
        analyze(bundle, minimum_reviewers=1)
    with pytest.raises(SystemExit) as cli_error:
        analyzer.parse_cli_args(
            cli_args(bundle, 1.3) + ["--minimum-reviewers", "1"]
        )
    assert cli_error.value.code == 2


def test_analyzer_consumes_writer_artifacts_at_declared_sample_rate(
    tmp_path: Path,
) -> None:
    source_sample_rate = 1_000
    translated_sample_rate = 2_000
    sample_zero = 0.5
    input_end = 2.6
    source_pcm = b"\x11\x00" * 1_500
    translated_pcm = b"\x22\x00" * 1_200

    capture = PrivatePcmScheduleCapture()
    capture.bind_source_pcm(
        source_pcm,
        sample_rate_hz=source_sample_rate,
        channels=1,
        bytes_per_sample=2,
    )
    capture.record_source_anchor(sample_zero)
    capture.record_source_chunk(
        chunk_index=0,
        sample_start=0,
        sample_end_exclusive=1_000,
        audio_bytes=2_000,
        deadline_seconds=1.5,
        emitted_seconds=1.5,
    )
    # The final partial chunk retains the fixed one-second pacing deadline.
    capture.record_source_chunk(
        chunk_index=1,
        sample_start=1_000,
        sample_end_exclusive=1_500,
        audio_bytes=1_000,
        deadline_seconds=2.5,
        emitted_seconds=2.5,
    )

    frame = SimpleNamespace(
        arrival_seconds=2.0,
        audio_bytes=len(translated_pcm),
        protocol_version=1,
        stream_generation=7,
        parent_sequence_id=0,
        audio_frame_id=0,
        sample_rate_hz=translated_sample_rate,
        channels=1,
        bytes_per_sample=2,
        source_start_ms=0.0,
        source_end_ms=1_000.0,
    )
    scheduler = HeadlessPlaybackScheduler()
    schedule = scheduler.accept(frame)
    capture.accept_frame(
        metadata={
            "type": "audio_frame",
            "protocolVersion": 1,
            "streamGeneration": 7,
            "parentSequenceId": 0,
            "audioFrameId": 0,
            "audioBytes": len(translated_pcm),
            "sampleRateHz": translated_sample_rate,
            "channels": 1,
            "bytesPerSample": 2,
            "sourceStartMs": 0.0,
            "sourceEndMs": 1_000.0,
        },
        pcm=translated_pcm,
        schedule=schedule,
    )
    capture.complete_parent(
        {
            "type": "audio_parent_complete",
            "protocolVersion": 1,
            "streamGeneration": 7,
            "parentSequenceId": 0,
            "audioFrameCount": 1,
            "audioBytes": len(translated_pcm),
            "sourceStartMs": 0.0,
            "sourceEndMs": 1_000.0,
        },
        received_seconds=2.1,
    )
    headless_report = scheduler.finalize(
        input_end_seconds=input_end,
        input_sample_zero_seconds=sample_zero,
    )
    ledger = capture.seal(
        input_end_seconds=input_end,
        terminal_completed=True,
        headless_report=headless_report,
    )
    artifacts = capture.write_new(tmp_path / "writer-evidence")
    markers_path = artifacts.directory / analyzer.REVIEWER_MARKERS_FILENAME
    write_json(
        markers_path,
        {
            "schema_version": 1,
            "schedule_ledger_sha256": artifacts.ledger_sha256,
            "source_pcm_sha256": ledger["source_pcm"]["pcm_sha256"],
            "source_pcm_sample_count": ledger["source_pcm"]["sample_count"],
            "translated_pcm_sha256": (
                ledger["translated_pcm"]["pcm_sha256"]
            ),
            "translated_pcm_sample_count": (
                ledger["translated_pcm"]["sample_count"]
            ),
            "events": [
                {
                    "event_id": "event-001",
                    "source_sample_index": 500,
                    "translated_sample_index": 600,
                    "source_independent_reviewer_count": 2,
                    "translated_independent_reviewer_count": 2,
                }
            ],
        },
    )
    markers_path.chmod(0o600)

    result = analyzer.analyze_scheduled_semantic_delay(
        artifacts.ledger_json,
        artifacts.source_wav,
        artifacts.translated_wav,
        markers_path,
        1.4,
    )

    assert result["summary"]["overall_status"] == "pass"
    assert (
        result["evidence"]["source_sample_rate_hz"]
        == source_sample_rate
    )
    assert (
        result["evidence"]["translated_sample_rate_hz"]
        == translated_sample_rate
    )
    assert result["events"][0]["attributed_source_range_ms"] == {
        "start": 0.0,
        "end": 1_000.0,
        "endpoint_convention": "closed",
    }
