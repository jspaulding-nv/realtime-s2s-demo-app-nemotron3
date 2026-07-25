import pytest

from audio_metadata_protocol import (
    AUDIO_METADATA_PROTOCOL_VERSION,
    AudioMetadataProtocolError,
    AudioMetadataTracker,
    validate_audio_frame,
    validate_audio_parent_complete,
)


def frame(
    *,
    parent=0,
    frame_id=0,
    audio_bytes=3200,
    generation=1,
    sample_rate_hz=16000,
    channels=1,
    bytes_per_sample=2,
    source_start_ms=100.0,
    source_end_ms=250.0,
):
    return {
        "type": "audio_frame",
        "protocolVersion": AUDIO_METADATA_PROTOCOL_VERSION,
        "streamGeneration": generation,
        "parentSequenceId": parent,
        "audioFrameId": frame_id,
        "audioBytes": audio_bytes,
        "sampleRateHz": sample_rate_hz,
        "channels": channels,
        "bytesPerSample": bytes_per_sample,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


def parent_complete(
    *,
    parent=0,
    frame_count=1,
    audio_bytes=3200,
    generation=1,
    source_start_ms=100.0,
    source_end_ms=250.0,
):
    return {
        "type": "audio_parent_complete",
        "protocolVersion": AUDIO_METADATA_PROTOCOL_VERSION,
        "streamGeneration": generation,
        "parentSequenceId": parent,
        "audioFrameCount": frame_count,
        "audioBytes": audio_bytes,
        "sourceStartMs": source_start_ms,
        "sourceEndMs": source_end_ms,
    }


def test_tracker_reconciles_frames_and_parent_completion():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)

    first = tracker.accept_control(frame(audio_bytes=3200))
    assert tracker.accept_binary(b"\0" * 3200) == first
    second = tracker.accept_control(frame(frame_id=1, audio_bytes=1600))
    assert tracker.accept_binary(b"\0" * 1600) == second
    completion = tracker.accept_control(
        parent_complete(frame_count=2, audio_bytes=4800)
    )
    tracker.assert_terminal_ready()

    assert tracker.stream_generation == 1
    assert tracker.paired_frames == [first, second]
    assert tracker.completed_parents == [completion]
    assert tracker.next_parent_sequence_id == 1


def test_legacy_tracker_accepts_binary_and_rejects_unnegotiated_metadata():
    tracker = AudioMetadataTracker(enabled=False)

    assert tracker.accept_binary(b"\0\0") is None
    assert tracker.accept_control({"type": "pong"}) is None
    with pytest.raises(
        AudioMetadataProtocolError,
        match="without negotiating",
    ):
        tracker.accept_control(frame())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.update({"text": "must not be captured"}),
            "unexpected text",
        ),
        (
            lambda payload: payload.update({"protocolVersion": True}),
            "protocolVersion",
        ),
        (
            lambda payload: payload.update({"sourceEndMs": 99.0}),
            "cannot precede",
        ),
        (
            lambda payload: payload.update({"audioBytes": 3}),
            "align",
        ),
    ],
)
def test_frame_validator_fails_closed(mutate, message):
    payload = frame()
    mutate(payload)

    with pytest.raises(AudioMetadataProtocolError, match=message):
        validate_audio_frame(payload)


def test_parent_validator_rejects_unknown_fields():
    payload = parent_complete()
    payload["endpoint"] = "private.example"

    with pytest.raises(AudioMetadataProtocolError, match="unexpected endpoint"):
        validate_audio_parent_complete(payload)


def test_tracker_requires_immediate_header_binary_pairing():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    tracker.accept_control(frame())

    with pytest.raises(AudioMetadataProtocolError, match="immediately"):
        tracker.accept_control({"type": "pong"})


def test_tracker_rejects_binary_without_header_and_wrong_byte_count():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    with pytest.raises(AudioMetadataProtocolError, match="without"):
        tracker.accept_binary(b"\0\0")

    tracker.accept_control(frame(audio_bytes=3200))
    with pytest.raises(AudioMetadataProtocolError, match="byte count"):
        tracker.accept_binary(b"\0" * 1600)


def test_tracker_rejects_parent_interleaving_and_bad_completion_totals():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    tracker.accept_control(frame())
    tracker.accept_binary(b"\0" * 3200)

    with pytest.raises(AudioMetadataProtocolError, match="new parent"):
        tracker.accept_control(frame(parent=1))

    with pytest.raises(AudioMetadataProtocolError, match="frame count"):
        tracker.accept_control(parent_complete(frame_count=2))


def test_tracker_rejects_generation_changes_and_incomplete_terminal():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    tracker.accept_control(frame())
    tracker.accept_binary(b"\0" * 3200)

    with pytest.raises(AudioMetadataProtocolError, match="changed"):
        tracker.accept_control(parent_complete(generation=2))
    with pytest.raises(AudioMetadataProtocolError, match="before"):
        tracker.assert_terminal_ready()


def test_tracker_accepts_null_source_offsets_but_reconciles_them():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    tracker.accept_control(
        frame(source_start_ms=None, source_end_ms=None)
    )
    tracker.accept_binary(b"\0" * 3200)
    tracker.accept_control(
        parent_complete(source_start_ms=None, source_end_ms=None)
    )

    tracker.assert_terminal_ready()


def test_tracker_accepts_independently_nullable_source_start():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    tracker.accept_control(
        frame(source_start_ms=None, source_end_ms=300.0)
    )
    tracker.accept_binary(b"\0" * 3200)
    tracker.accept_control(
        parent_complete(source_start_ms=None, source_end_ms=300.0)
    )

    tracker.assert_terminal_ready()


def test_tracker_rejects_pcm_format_change_between_parents():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    tracker.accept_control(frame())
    tracker.accept_binary(b"\0" * 3200)
    tracker.accept_control(parent_complete())

    with pytest.raises(
        AudioMetadataProtocolError,
        match="stable within a stream",
    ):
        tracker.accept_control(
            frame(parent=1, sample_rate_hz=22_050)
        )


def test_tracker_rejects_duplicate_terminal_and_post_terminal_pcm():
    tracker = AudioMetadataTracker(enabled=True, protocol_version=1)
    tracker.accept_control(frame())
    tracker.accept_binary(b"\0" * 3200)
    tracker.accept_control(parent_complete())
    tracker.assert_terminal_ready()

    with pytest.raises(AudioMetadataProtocolError, match="duplicate"):
        tracker.assert_terminal_ready()
    with pytest.raises(AudioMetadataProtocolError, match="after"):
        tracker.accept_binary(b"\0\0")
