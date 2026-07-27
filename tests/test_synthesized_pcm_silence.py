import copy
import math

import numpy as np
import pytest

from synthesized_pcm_silence import (
    PRIMARY_THRESHOLD_DBFS,
    SYNTHESIZED_PCM_SILENCE_SCHEMA_VERSION,
    SynthesizedPcmSilenceError,
    StreamingPcmSilenceDiagnostic,
    _window_is_low_energy,
    validate_synthesized_pcm_silence_observation,
)


SAMPLE_RATE_HZ = 16_000
WINDOW_SAMPLES = 320


def pcm(samples) -> bytes:
    return np.asarray(samples, dtype="<i2").tobytes()


def constant(sample_count: int, amplitude: int) -> bytes:
    return pcm(np.full(sample_count, amplitude, dtype=np.int16))


def frame(
    payload: bytes,
    *,
    parent: int = 0,
    frame_id: int = 0,
    generation: int = 1,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    channels: int = 1,
    bytes_per_sample: int = 2,
    source_start_ms: float | None = 100.0,
    source_end_ms: float | None = 250.0,
    audio_bytes: int | None = None,
) -> dict:
    return {
        "type": "audio_frame",
        "protocolVersion": 1,
        "streamGeneration": generation,
        "parentSequenceId": parent,
        "audioFrameId": frame_id,
        "audioBytes": len(payload) if audio_bytes is None else audio_bytes,
        "sampleRateHz": sample_rate_hz,
        "channels": channels,
        "bytesPerSample": bytes_per_sample,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


def completion(
    *,
    parent: int = 0,
    frame_count: int = 1,
    audio_bytes: int,
    generation: int = 1,
    source_start_ms: float | None = 100.0,
    source_end_ms: float | None = 250.0,
) -> dict:
    return {
        "type": "audio_parent_complete",
        "protocolVersion": 1,
        "streamGeneration": generation,
        "parentSequenceId": parent,
        "audioFrameCount": frame_count,
        "audioBytes": audio_bytes,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


def observe_one(payload: bytes) -> dict:
    diagnostic = StreamingPcmSilenceDiagnostic()
    diagnostic.accept_frame(frame(payload), payload)
    diagnostic.complete_parent(completion(audio_bytes=len(payload)))
    return diagnostic.finalize()


def threshold_row(
    observation: dict,
    *,
    parent: int = 0,
    threshold: float = PRIMARY_THRESHOLD_DBFS,
) -> dict:
    return next(
        row
        for row in observation["parent_threshold_rows"]
        if row["parent_sequence_id"] == parent
        and row["threshold_dbfs"] == threshold
    )


def test_windows_continue_across_transport_frame_boundaries():
    samples = np.concatenate(
        (
            np.zeros(WINDOW_SAMPLES, dtype=np.int16),
            np.full(WINDOW_SAMPLES, 2_000, dtype=np.int16),
        )
    )
    first = pcm(samples[:137])
    second = pcm(samples[137:])
    diagnostic = StreamingPcmSilenceDiagnostic()

    diagnostic.accept_frame(frame(first), first)
    diagnostic.accept_frame(frame(second, frame_id=1), second)
    diagnostic.complete_parent(
        completion(frame_count=2, audio_bytes=len(first) + len(second))
    )
    result = diagnostic.finalize()
    row = threshold_row(result)

    assert row["full_window_count"] == 2
    assert row["partial_window_count"] == 0
    assert row["leading_low_energy_sample_count"] == WINDOW_SAMPLES
    assert row["active_sample_count"] == WINDOW_SAMPLES
    assert row["internal_low_energy_sample_count"] == 0
    assert row["trailing_low_energy_sample_count"] == 0


def test_leading_internal_and_trailing_low_energy_partition_exactly():
    payload = b"".join(
        (
            constant(WINDOW_SAMPLES, 0),
            constant(WINDOW_SAMPLES, 2_000),
            constant(WINDOW_SAMPLES * 2, 0),
            constant(WINDOW_SAMPLES, 2_000),
            constant(100, 0),
        )
    )

    row = threshold_row(observe_one(payload))

    assert row["sample_count"] == WINDOW_SAMPLES * 5 + 100
    assert row["active_sample_count"] == WINDOW_SAMPLES * 2
    assert row["low_energy_sample_count"] == WINDOW_SAMPLES * 3 + 100
    assert row["leading_low_energy_sample_count"] == WINDOW_SAMPLES
    assert row["internal_low_energy_sample_count"] == WINDOW_SAMPLES * 2
    assert row["trailing_low_energy_sample_count"] == 100
    assert row["internal_low_energy_run_count"] == 1
    assert (
        row["longest_internal_low_energy_run_samples"]
        == WINDOW_SAMPLES * 2
    )
    assert row["duration_ms"] == 106.25
    assert row["trailing_low_energy_ms"] == 6.25


def test_partial_window_is_classified_at_parent_completion():
    payload = b"".join(
        (
            constant(WINDOW_SAMPLES, 2_000),
            constant(17, 0),
        )
    )

    result = observe_one(payload)
    row = threshold_row(result)

    assert row["full_window_count"] == 1
    assert row["partial_window_count"] == 1
    assert row["active_sample_count"] == WINDOW_SAMPLES
    assert row["trailing_low_energy_sample_count"] == 17
    assert result["totals"]["sample_count"] == WINDOW_SAMPLES + 17
    assert result["totals"]["duration_ms"] == 21.0625


def test_all_low_parent_is_canonicalized_as_leading_only():
    payload = constant(WINDOW_SAMPLES * 2 + 7, 0)

    result = observe_one(payload)
    for row in result["parent_threshold_rows"]:
        assert row["all_low_energy"] is True
        assert row["active_sample_count"] == 0
        assert row["low_energy_sample_count"] == WINDOW_SAMPLES * 2 + 7
        assert (
            row["leading_low_energy_sample_count"]
            == WINDOW_SAMPLES * 2 + 7
        )
        assert row["internal_low_energy_sample_count"] == 0
        assert row["trailing_low_energy_sample_count"] == 0
        assert row["internal_low_energy_run_count"] == 0
        assert row["longest_internal_low_energy_run_samples"] == 0


def test_exact_rms_threshold_is_classified_as_low_energy():
    sum_squares = WINDOW_SAMPLES * (16_384**2)
    exact_dbfs = 10.0 * math.log10(
        (sum_squares / WINDOW_SAMPLES) / (32_768**2)
    )

    assert _window_is_low_energy(
        sum_squares,
        WINDOW_SAMPLES,
        exact_dbfs,
    )
    assert not _window_is_low_energy(
        sum_squares,
        WINDOW_SAMPLES,
        exact_dbfs - 1e-9,
    )


def test_negative_full_scale_does_not_overflow_during_squaring():
    result = observe_one(constant(WINDOW_SAMPLES, -32_768))
    row = threshold_row(result)

    assert row["active_sample_count"] == WINDOW_SAMPLES
    assert row["low_energy_sample_count"] == 0
    assert row["all_low_energy"] is False


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sample_rate_hz": 8_000}, "16000 Hz"),
        ({"channels": 2}, "16000 Hz"),
        ({"bytes_per_sample": 1}, "16000 Hz"),
    ],
)
def test_rejects_unsupported_pcm_format(changes, message):
    payload = constant(WINDOW_SAMPLES, 0)
    metadata = frame(payload, **changes)
    diagnostic = StreamingPcmSilenceDiagnostic()

    with pytest.raises(SynthesizedPcmSilenceError, match=message):
        diagnostic.accept_frame(metadata, payload)


def test_rejects_payload_alignment_and_header_byte_mismatch():
    diagnostic = StreamingPcmSilenceDiagnostic()
    with pytest.raises(SynthesizedPcmSilenceError, match="align"):
        diagnostic.accept_frame(frame(b"\0\0\0"), b"\0\0\0")

    payload = b"\0\0"
    with pytest.raises(SynthesizedPcmSilenceError, match="byte count"):
        diagnostic.accept_frame(
            frame(payload, audio_bytes=4),
            payload,
        )


def test_rejects_non_bytes_pcm_and_unknown_metadata_fields():
    payload = constant(4, 0)
    diagnostic = StreamingPcmSilenceDiagnostic()
    with pytest.raises(SynthesizedPcmSilenceError, match="must be bytes"):
        diagnostic.accept_frame(frame(payload), bytearray(payload))

    metadata = frame(payload)
    metadata["text"] = "must not enter telemetry"
    with pytest.raises(SynthesizedPcmSilenceError, match="unexpected text"):
        diagnostic.accept_frame(metadata, payload)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (lambda payload: frame(payload, parent=1), "ordered from zero"),
        (lambda payload: frame(payload, frame_id=1), "must be zero"),
    ],
)
def test_rejects_invalid_initial_order(metadata, message):
    payload = constant(4, 0)
    diagnostic = StreamingPcmSilenceDiagnostic()

    with pytest.raises(SynthesizedPcmSilenceError, match=message):
        diagnostic.accept_frame(metadata(payload), payload)


def test_rejects_frame_gaps_parent_interleaving_and_generation_changes():
    payload = constant(4, 0)
    diagnostic = StreamingPcmSilenceDiagnostic()
    diagnostic.accept_frame(frame(payload), payload)

    with pytest.raises(SynthesizedPcmSilenceError, match="contiguous"):
        diagnostic.accept_frame(frame(payload, frame_id=2), payload)
    with pytest.raises(SynthesizedPcmSilenceError, match="new parent"):
        diagnostic.accept_frame(
            frame(payload, parent=1, source_start_ms=100.0),
            payload,
        )
    with pytest.raises(SynthesizedPcmSilenceError, match="changed"):
        diagnostic.accept_frame(
            frame(payload, frame_id=1, generation=2),
            payload,
        )


def test_rejects_source_range_change_within_parent():
    payload = constant(4, 0)
    diagnostic = StreamingPcmSilenceDiagnostic()
    diagnostic.accept_frame(frame(payload), payload)

    with pytest.raises(SynthesizedPcmSilenceError, match="source range"):
        diagnostic.accept_frame(
            frame(
                payload,
                frame_id=1,
                source_start_ms=101.0,
            ),
            payload,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"parent": 1}, "wrong parent"),
        ({"frame_count": 2}, "frame count"),
        ({"audio_bytes": 10}, "byte count"),
        ({"generation": 2}, "changed"),
        ({"source_end_ms": 251.0}, "source range"),
    ],
)
def test_completion_must_reconcile(changes, message):
    payload = constant(4, 0)
    diagnostic = StreamingPcmSilenceDiagnostic()
    diagnostic.accept_frame(frame(payload), payload)
    values = {"audio_bytes": len(payload), **changes}

    with pytest.raises(SynthesizedPcmSilenceError, match=message):
        diagnostic.complete_parent(completion(**values))


def test_parents_and_threshold_totals_are_contiguous_and_reconciled():
    first = constant(WINDOW_SAMPLES, 0)
    second = constant(WINDOW_SAMPLES, 2_000)
    diagnostic = StreamingPcmSilenceDiagnostic()
    diagnostic.accept_frame(frame(first), first)
    diagnostic.complete_parent(completion(audio_bytes=len(first)))
    diagnostic.accept_frame(
        frame(
            second,
            parent=1,
            source_start_ms=300.0,
            source_end_ms=500.0,
        ),
        second,
    )
    diagnostic.complete_parent(
        completion(
            parent=1,
            audio_bytes=len(second),
            source_start_ms=300.0,
            source_end_ms=500.0,
        )
    )

    result = diagnostic.finalize()
    primary = next(
        row
        for row in result["threshold_totals"]
        if row["threshold_dbfs"] == PRIMARY_THRESHOLD_DBFS
    )

    assert result["totals"] == {
        "stream_generation": 1,
        "parent_count": 2,
        "frame_count": 2,
        "audio_bytes": len(first) + len(second),
        "sample_count": WINDOW_SAMPLES * 2,
        "duration_ms": 40.0,
        "full_window_count": 2,
        "partial_window_count": 0,
    }
    assert primary["parent_count"] == 2
    assert primary["all_low_energy_parent_count"] == 1
    assert primary["active_sample_count"] == WINDOW_SAMPLES
    assert primary["low_energy_sample_count"] == WINDOW_SAMPLES


def test_finalize_fails_closed_for_empty_active_duplicate_and_post_terminal():
    empty = StreamingPcmSilenceDiagnostic()
    with pytest.raises(SynthesizedPcmSilenceError, match="empty"):
        empty.finalize()

    payload = constant(4, 0)
    active = StreamingPcmSilenceDiagnostic()
    active.accept_frame(frame(payload), payload)
    with pytest.raises(SynthesizedPcmSilenceError, match="active parent"):
        active.finalize()
    active.complete_parent(completion(audio_bytes=len(payload)))
    result = active.finalize()
    assert result["schema_version"] == SYNTHESIZED_PCM_SILENCE_SCHEMA_VERSION

    with pytest.raises(SynthesizedPcmSilenceError, match="already finalized"):
        active.finalize()
    with pytest.raises(SynthesizedPcmSilenceError, match="already finalized"):
        active.accept_frame(frame(payload, parent=1), payload)
    with pytest.raises(SynthesizedPcmSilenceError, match="already finalized"):
        active.complete_parent(
            completion(parent=1, audio_bytes=len(payload))
        )


def test_completion_without_parent_and_duplicate_completion_are_rejected():
    payload = constant(4, 0)
    diagnostic = StreamingPcmSilenceDiagnostic()
    marker = completion(audio_bytes=len(payload))
    with pytest.raises(SynthesizedPcmSilenceError, match="no active parent"):
        diagnostic.complete_parent(marker)

    diagnostic.accept_frame(frame(payload), payload)
    diagnostic.complete_parent(marker)
    with pytest.raises(SynthesizedPcmSilenceError, match="no active parent"):
        diagnostic.complete_parent(marker)


def test_observation_validator_returns_a_deep_normalized_copy():
    result = observe_one(constant(WINDOW_SAMPLES, 0))

    normalized = validate_synthesized_pcm_silence_observation(result)

    assert normalized == result
    assert normalized is not result
    assert normalized["method"] is not result["method"]
    assert (
        normalized["parent_threshold_rows"]
        is not result["parent_threshold_rows"]
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update({"pcm": "forbidden"}),
            "unexpected pcm",
        ),
        (
            lambda value: value["privacy"].update(
                {"contains_transcript_text": True}
            ),
            "contains_transcript_text",
        ),
        (
            lambda value: value["parent_threshold_rows"][1].update(
                {"low_energy_sample_count": 1}
            ),
            "partition",
        ),
        (
            lambda value: value["threshold_totals"][1].update(
                {"parent_count": 2}
            ),
            "parent_count",
        ),
    ],
)
def test_observation_validator_rejects_schema_or_reconciliation_tampering(
    mutate,
    message,
):
    result = observe_one(constant(WINDOW_SAMPLES, 0))
    tampered = copy.deepcopy(result)
    mutate(tampered)

    with pytest.raises(SynthesizedPcmSilenceError, match=message):
        validate_synthesized_pcm_silence_observation(tampered)


def _assert_no_retained_pcm_like(value, seen=None):
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)
    assert not isinstance(
        value,
        (bytes, bytearray, memoryview, np.ndarray),
    )
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_retained_pcm_like(key, seen)
            _assert_no_retained_pcm_like(item, seen)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _assert_no_retained_pcm_like(item, seen)
    elif hasattr(value, "__dict__"):
        _assert_no_retained_pcm_like(vars(value), seen)


def test_diagnostic_retains_only_scalars_and_numeric_rows_not_pcm():
    payload = constant(137, 1_234)
    diagnostic = StreamingPcmSilenceDiagnostic()
    diagnostic.accept_frame(frame(payload), payload)

    _assert_no_retained_pcm_like(diagnostic)
    assert diagnostic._window_sample_count == 137
    assert type(diagnostic._window_sum_squares) is int

    diagnostic.complete_parent(completion(audio_bytes=len(payload)))
    result = diagnostic.finalize()
    _assert_no_retained_pcm_like(diagnostic)
    _assert_no_retained_pcm_like(result)
    assert result["privacy"] == {
        "contains_audio": False,
        "contains_transcript_text": False,
        "contains_translation_text": False,
        "contains_input_paths_or_filenames": False,
        "contains_endpoints": False,
        "contains_session_ids": False,
        "contains_source_timing": False,
        "numeric_telemetry_only": True,
    }
