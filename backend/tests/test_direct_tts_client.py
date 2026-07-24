import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import grpc
import pytest
import riva.client

from direct_tts_client import (
    DirectTTSClient,
    DirectTTSCancelled,
    DirectTTSPartialStreamError,
    DirectTTSRetryError,
    DirectTTSError,
)
from staged_models import (
    EmissionReason,
    SynthesizedAudioFrame,
    SynthesizedSegment,
    SynthesizedStreamCompletion,
    TextSegment,
    TranslatedSegment,
)
from target_text_validation import TargetTextValidationError


def translated_segment(text="La congregación se rió.", language="es-US"):
    segment = TextSegment(
        sequence_id=7,
        text="The congregation laughed.",
        reason=EmissionReason.PUNCTUATION,
        emitted_monotonic_ms=20,
        buffered_since_monotonic_ms=10,
        source_start_ms=100,
        source_end_ms=900,
        contributing_final_ids=(2, 3),
    )
    return TranslatedSegment(
        segment=segment,
        text=text,
        language=language,
        started_monotonic_ms=21,
        completed_monotonic_ms=22,
    )


class Response:
    def __init__(self, audio):
        self.audio = audio


class RecordingService:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def synthesize_online(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.responses)


class FailingService:
    def synthesize_online(self, **kwargs):
        del kwargs
        raise RuntimeError("TTS unavailable")


class GrpcStatusError(grpc.RpcError):
    def __init__(self, status):
        self.status = status
        super().__init__(status.name)

    def code(self):
        return self.status


class GrpcLookalikeError(RuntimeError):
    def code(self):
        return grpc.StatusCode.UNKNOWN


class FailingAfterAudio:
    def __init__(self, status):
        self.status = status
        self.cancelled = False

    def __iter__(self):
        yield Response(b"\x01\x02")
        raise GrpcStatusError(self.status)

    def cancel(self):
        self.cancelled = True


class FailingAfterResponses:
    def __init__(self, responses, status):
        self.responses = list(responses)
        self.status = status
        self.cancelled = False

    def __iter__(self):
        yield from self.responses
        raise GrpcStatusError(self.status)

    def cancel(self):
        self.cancelled = True


class AttemptService:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def synthesize_online(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingCall:
    def __init__(self):
        self.iterating = threading.Event()
        self.cancelled = threading.Event()
        self.cancel_count = 0

    def __iter__(self):
        self.iterating.set()
        self.cancelled.wait(timeout=2)
        raise RuntimeError("cancelled")
        yield  # pragma: no cover - make this method a generator

    def cancel(self):
        self.cancel_count += 1
        self.cancelled.set()


class BlockingService:
    def __init__(self, call):
        self.call = call

    def synthesize_online(self, **kwargs):
        del kwargs
        return self.call


def configured_client(service, *, ticks=(100, 125, 200)):
    clock = MagicMock(side_effect=ticks)
    client = DirectTTSClient(clock_ms=clock)
    channel = MagicMock()
    client._connected = True
    client._auth = SimpleNamespace(channel=channel)
    client._service = service
    return client, channel


def test_synthesis_collects_complete_pcm_atomically_and_preserves_provenance():
    service = RecordingService([Response(b"\x01\x02"), Response(b"\x03\x04")])
    client, channel = configured_client(service)
    translation = translated_segment()

    result = client.synthesize_segment(translation)

    assert isinstance(result, SynthesizedSegment)
    assert result.translation is translation
    assert result.audio == b"\x01\x02\x03\x04"
    assert result.sample_rate_hz == 16_000
    assert result.channels == 1
    assert result.bytes_per_sample == 2
    assert result.audio_duration_ms == pytest.approx(0.125)
    assert result.started_monotonic_ms == 100
    assert result.first_audio_monotonic_ms == 125
    assert result.completed_monotonic_ms == 200
    assert result.response_chunks == ()
    assert "response_chunks" not in result.to_dict()
    assert service.calls == [
        {
            "text": "La congregación se rió.",
            "voice_name": "Magpie-Multilingual.ES-US.Isabela",
            "language_code": "es-US",
            "encoding": riva.client.AudioEncoding.LINEAR_PCM,
            "sample_rate_hz": 16_000,
        }
    ]
    client.disconnect()
    channel.close.assert_called_once_with()


def test_opt_in_records_privacy_safe_response_chunk_metrics():
    service = RecordingService(
        [Response(b"\x01\x02"), Response(b"\x03\x04\x05\x06")]
    )
    clock = MagicMock(side_effect=(100, 125, 150, 200))
    client = DirectTTSClient(
        capture_response_chunk_metrics=True,
        clock_ms=clock,
    )
    client._connected = True
    client._service = service

    result = client.synthesize_segment(translated_segment())

    assert [
        (
            chunk.response_index,
            chunk.audio_bytes,
            chunk.cumulative_audio_bytes,
            chunk.received_monotonic_ms,
            chunk.retry_count,
        )
        for chunk in result.response_chunks
    ] == [
        (0, 2, 2, 125, 0),
        (1, 4, 6, 150, 0),
    ]
    assert result.first_audio_monotonic_ms == 125
    payload = result.to_dict()
    assert payload["response_chunks"] == [
        {
            "response_index": 0,
            "audio_bytes": 2,
            "cumulative_audio_bytes": 2,
            "received_monotonic_ms": 125,
            "retry_count": 0,
        },
        {
            "response_index": 1,
            "audio_bytes": 4,
            "cumulative_audio_bytes": 6,
            "received_monotonic_ms": 150,
            "retry_count": 0,
        },
    ]
    assert "audio" not in payload
    assert "text" not in str(payload["response_chunks"])


def test_incremental_reframes_variable_responses_and_preserves_exact_pcm():
    responses = [
        Response(b"\x01\x02" * 500),
        Response(b"\x03\x04" * 1_300),
        Response(b"\x05\x06" * 1_500),
    ]
    source_audio = b"".join(response.audio for response in responses)
    service = RecordingService(responses)
    clock = MagicMock(side_effect=(100, 110, 120, 130, 140))
    client = DirectTTSClient(
        capture_response_chunk_metrics=True,
        clock_ms=clock,
    )
    client._connected = True
    client._service = service
    frames = []

    completion = client.synthesize_incremental(
        translated_segment(),
        frames.append,
    )

    assert isinstance(completion, SynthesizedStreamCompletion)
    assert all(isinstance(frame, SynthesizedAudioFrame) for frame in frames)
    assert [len(frame.audio) for frame in frames] == [3_200, 3_200, 200]
    assert [frame.audio_frame_id for frame in frames] == [0, 1, 2]
    assert [frame.received_monotonic_ms for frame in frames] == [120, 130, 130]
    assert [frame.retry_count for frame in frames] == [0, 0, 0]
    assert b"".join(frame.audio for frame in frames) == source_audio
    assert completion.audio_frame_count == 3
    assert completion.audio_bytes == len(source_audio) == 6_600
    assert completion.started_monotonic_ms == 100
    assert completion.first_audio_monotonic_ms == 110
    assert completion.completed_monotonic_ms == 140
    assert completion.atomic_fallback_applied is False
    assert [
        (
            chunk.response_index,
            chunk.audio_bytes,
            chunk.cumulative_audio_bytes,
            chunk.received_monotonic_ms,
        )
        for chunk in completion.response_chunks
    ] == [
        (0, 1_000, 1_000, 110),
        (1, 2_600, 3_600, 120),
        (2, 3_000, 6_600, 130),
    ]


def test_incremental_short_target_retries_privately_then_frames_only_success():
    failed_audio = b"\x01\x02" * 3_200
    successful_audio = b"\x03\x04" * 3_300
    first = FailingAfterResponses(
        [Response(failed_audio)],
        grpc.StatusCode.UNKNOWN,
    )
    service = AttemptService(
        [first, iter([Response(successful_audio)])]
    )
    client = DirectTTSClient(
        max_retries=1,
        capture_response_chunk_metrics=True,
        incremental_atomic_fallback_max_chars=4,
        clock_ms=MagicMock(
            side_effect=(100, 110, 120, 130, 140)
        ),
    )
    client._connected = True
    client._service = service
    frames = []

    completion = client.synthesize_incremental(
        translated_segment(text="A."),
        frames.append,
    )

    assert len(service.calls) == 2
    assert first.cancelled is True
    assert [len(frame.audio) for frame in frames] == [3_200, 3_200, 200]
    assert [frame.audio_frame_id for frame in frames] == [0, 1, 2]
    assert [frame.retry_count for frame in frames] == [1, 1, 1]
    assert [frame.received_monotonic_ms for frame in frames] == [
        140,
        140,
        140,
    ]
    assert b"".join(frame.audio for frame in frames) == successful_audio
    assert failed_audio not in b"".join(frame.audio for frame in frames)
    assert completion.audio_frame_count == 3
    assert completion.audio_bytes == len(successful_audio)
    assert completion.started_monotonic_ms == 100
    assert completion.first_audio_monotonic_ms == 130
    assert completion.completed_monotonic_ms == 140
    assert completion.retry_count == 1
    assert completion.atomic_fallback_applied is True
    assert [
        (
            chunk.audio_bytes,
            chunk.cumulative_audio_bytes,
            chunk.received_monotonic_ms,
            chunk.retry_count,
        )
        for chunk in completion.response_chunks
    ] == [(6_600, 6_600, 130, 1)]


def test_incremental_atomic_fallback_uses_normalized_trimmed_target_length():
    service = RecordingService([Response(b"\x01\x02" * 1_600)])
    client = DirectTTSClient(
        incremental_atomic_fallback_max_chars=3,
        clock_ms=MagicMock(side_effect=(100, 110, 120)),
    )
    client._connected = True
    client._service = service
    frames = []

    completion = client.synthesize_incremental(
        translated_segment(text="  Si\u0301.  "),
        frames.append,
    )

    assert service.calls[0]["text"] == "Sí."
    assert completion.atomic_fallback_applied is True
    assert [frame.received_monotonic_ms for frame in frames] == [120]


def test_incremental_target_above_atomic_fallback_limit_remains_live():
    first = FailingAfterResponses(
        [Response(b"\x01\x02" * 1_600)],
        grpc.StatusCode.UNKNOWN,
    )
    service = AttemptService(
        [first, iter([Response(b"\x03\x04" * 1_600)])]
    )
    client = DirectTTSClient(
        max_retries=1,
        incremental_atomic_fallback_max_chars=4,
        clock_ms=MagicMock(side_effect=(100, 110)),
    )
    client._connected = True
    client._service = service
    frames = []

    with pytest.raises(DirectTTSPartialStreamError):
        client.synthesize_incremental(
            translated_segment(text="Hola."),
            frames.append,
        )

    assert len(service.calls) == 1
    assert [frame.audio_frame_id for frame in frames] == [0]


def test_zero_atomic_fallback_limit_leaves_short_target_incremental():
    first = FailingAfterResponses(
        [Response(b"\x01\x02" * 1_600)],
        grpc.StatusCode.UNKNOWN,
    )
    service = AttemptService(
        [first, iter([Response(b"\x03\x04" * 1_600)])]
    )
    client = DirectTTSClient(
        max_retries=1,
        incremental_atomic_fallback_max_chars=0,
        clock_ms=MagicMock(side_effect=(100, 110)),
    )
    client._connected = True
    client._service = service
    frames = []

    with pytest.raises(DirectTTSPartialStreamError):
        client.synthesize_incremental(
            translated_segment(text="A."),
            frames.append,
        )

    assert len(service.calls) == 1
    assert [frame.audio_frame_id for frame in frames] == [0]


def test_atomic_fallback_publisher_failure_after_ack_never_retries_or_replays():
    service = AttemptService(
        [
            iter([Response(b"\x01\x02" * 3_200)]),
            iter([Response(b"\x03\x04" * 1_600)]),
        ]
    )
    client = DirectTTSClient(
        max_retries=1,
        incremental_atomic_fallback_max_chars=4,
        clock_ms=MagicMock(side_effect=(100, 110, 120)),
    )
    client._connected = True
    client._service = service
    committed = []

    def publish(frame):
        if frame.audio_frame_id == 1:
            raise RuntimeError("publisher stopped")
        committed.append(frame)

    with pytest.raises(DirectTTSPartialStreamError) as failure:
        client.synthesize_incremental(
            translated_segment(text="A."),
            publish,
        )

    assert len(service.calls) == 1
    assert [frame.audio_frame_id for frame in committed] == [0]
    assert failure.value.committed_frame_count == 1
    assert failure.value.committed_audio_bytes == 3_200
    assert failure.value.retry_count == 0


def test_incremental_exact_frame_multiple_does_not_publish_empty_tail():
    service = RecordingService([Response(b"\x01\x02" * 3_200)])
    client, _ = configured_client(service, ticks=(100, 125, 200))
    frames = []

    completion = client.synthesize_incremental(
        translated_segment(),
        frames.append,
    )

    assert [len(frame.audio) for frame in frames] == [3_200, 3_200]
    assert completion.audio_frame_count == 2
    assert completion.audio_bytes == 6_400


def test_incremental_unknown_before_commit_discards_private_carry_and_retries():
    first = FailingAfterResponses(
        [Response(b"\x01\x02" * 500)],
        grpc.StatusCode.UNKNOWN,
    )
    second_audio = b"\x03\x04" * 1_600
    service = AttemptService([first, iter([Response(second_audio)])])
    clock = MagicMock(side_effect=(100, 110, 120, 130, 140))
    client = DirectTTSClient(
        max_retries=1,
        capture_response_chunk_metrics=True,
        clock_ms=clock,
    )
    client._connected = True
    client._service = service
    frames = []

    completion = client.synthesize_incremental(
        translated_segment(),
        frames.append,
    )

    assert len(service.calls) == 2
    assert first.cancelled is True
    assert [frame.audio for frame in frames] == [second_audio]
    assert [frame.audio_frame_id for frame in frames] == [0]
    assert [frame.retry_count for frame in frames] == [1]
    assert completion.retry_count == 1
    assert completion.started_monotonic_ms == 100
    assert completion.first_audio_monotonic_ms == 130
    assert completion.completed_monotonic_ms == 140
    assert [
        (chunk.audio_bytes, chunk.cumulative_audio_bytes, chunk.retry_count)
        for chunk in completion.response_chunks
    ] == [(3_200, 3_200, 1)]


def test_incremental_unknown_after_commit_never_retries_or_replays_prefix():
    first = FailingAfterResponses(
        [Response(b"\x01\x02" * 1_600)],
        grpc.StatusCode.UNKNOWN,
    )
    service = AttemptService(
        [first, iter([Response(b"\x03\x04" * 1_600)])]
    )
    client = DirectTTSClient(
        max_retries=1,
        clock_ms=MagicMock(side_effect=(100, 110)),
    )
    client._connected = True
    client._service = service
    frames = []

    with pytest.raises(DirectTTSPartialStreamError) as failure:
        client.synthesize_incremental(
            translated_segment(text="Texto privado."),
            frames.append,
        )

    assert len(service.calls) == 1
    assert first.cancelled is True
    assert len(frames) == 1
    assert frames[0].audio_frame_id == 0
    assert failure.value.committed_frame_count == 1
    assert failure.value.committed_audio_bytes == 3_200
    assert failure.value.retry_count == 0
    assert failure.value.status_code == "UNKNOWN"
    assert "Texto privado" not in str(failure.value)
    assert client._synthesis_active is False
    assert client._active_call is None


def test_incremental_publisher_rejection_is_local_and_never_retried():
    service = AttemptService(
        [
            iter([Response(b"\x01\x02" * 1_600)]),
            iter([Response(b"\x03\x04" * 1_600)]),
        ]
    )
    client = DirectTTSClient(
        max_retries=1,
        clock_ms=MagicMock(side_effect=(100, 110)),
    )
    client._connected = True
    client._service = service

    def reject(_frame):
        raise GrpcStatusError(grpc.StatusCode.UNKNOWN)

    with pytest.raises(DirectTTSError, match="publisher failed") as failure:
        client.synthesize_incremental(translated_segment(), reject)

    assert not isinstance(failure.value, DirectTTSPartialStreamError)
    assert len(service.calls) == 1
    assert client._synthesis_active is False
    assert client._active_call is None


def test_incremental_limit_failure_after_commit_reports_exact_prefix():
    service = RecordingService(
        [
            Response(b"\x01\x02" * 1_600),
            Response(b"\x03\x04"),
        ]
    )
    client = DirectTTSClient(
        max_audio_duration_s=0.1,
        max_retries=1,
        clock_ms=MagicMock(side_effect=(100, 110)),
    )
    client._connected = True
    client._service = service
    frames = []

    with pytest.raises(DirectTTSPartialStreamError) as failure:
        client.synthesize_incremental(
            translated_segment(),
            frames.append,
        )

    assert [len(frame.audio) for frame in frames] == [3_200]
    assert failure.value.committed_frame_count == 1
    assert failure.value.committed_audio_bytes == 3_200
    assert len(service.calls) == 1


def test_incremental_rejects_nonintegral_frame_duration_before_rpc():
    service = RecordingService([Response(b"\x00\x00")])
    client = DirectTTSClient(
        sample_rate_hz=1,
        incremental_frame_ms=100,
    )
    client._connected = True
    client._service = service

    with pytest.raises(ValueError, match="whole number of PCM samples"):
        client.synthesize_incremental(translated_segment(), lambda frame: None)

    assert service.calls == []


def test_incremental_no_audio_and_partial_pcm_fail_before_publication():
    for responses, message in (
        ([Response(b""), SimpleNamespace()], "no audio"),
        ([Response(b"\x00")], "partial PCM frame"),
    ):
        service = RecordingService(responses)
        client = DirectTTSClient(clock_ms=lambda: 100)
        client._connected = True
        client._service = service
        frames = []

        with pytest.raises(DirectTTSError, match=message):
            client.synthesize_incremental(
                translated_segment(),
                frames.append,
            )

        assert frames == []
        assert len(service.calls) == 1


def test_incremental_exhausted_precommit_retry_is_privacy_safe():
    service = AttemptService(
        [
            GrpcStatusError(grpc.StatusCode.UNKNOWN),
            GrpcStatusError(grpc.StatusCode.UNKNOWN),
        ]
    )
    client = DirectTTSClient(
        max_retries=1,
        clock_ms=MagicMock(side_effect=(100, 110)),
    )
    client._connected = True
    client._service = service
    frames = []

    with pytest.raises(DirectTTSRetryError) as failure:
        client.synthesize_incremental(
            translated_segment(text="Texto privado."),
            frames.append,
        )

    assert frames == []
    assert len(service.calls) == 2
    assert failure.value.retry_count == 1
    assert failure.value.initial_status_code == "UNKNOWN"
    assert failure.value.retry_status_code == "UNKNOWN"
    assert "Texto privado" not in str(failure.value)


def test_incremental_rejects_subsegmented_translation_before_rpc():
    service = RecordingService([Response(b"\x00\x00")])
    client = DirectTTSClient()
    client._connected = True
    client._service = service
    translation = replace(
        translated_segment(),
        subsequence_id=0,
        subsequence_count=2,
    )

    with pytest.raises(ValueError, match="unsplit"):
        client.synthesize_incremental(translation, lambda frame: None)

    assert service.calls == []


def test_empty_responses_do_not_set_first_audio_time():
    service = RecordingService([Response(b""), Response(b"\x00\x00")])
    client, _ = configured_client(service)

    result = client.synthesize(translated_segment())

    assert result.first_audio_monotonic_ms == 125


def test_target_text_is_normalized_to_nfc_before_magpie_call():
    service = RecordingService([Response(b"\x00\x00")])
    client, _ = configured_client(service)

    client.synthesize(translated_segment(text="La congregacio\u0301n canto\u0301."))

    assert service.calls[0]["text"] == "La congregación cantó."


@pytest.mark.parametrize(
    "unsafe_text,expected_diagnostic",
    [
        ("好吧。", "Han"),
        ("Привет.", "Cyrillic"),
        ("Hola а todos.", "Cyrillic"),
        ("Hola\u2060.", "U+2060"),
        ("¿?!", "missing_letter_or_digit"),
    ],
)
def test_unsafe_target_text_never_calls_magpie(
    unsafe_text, expected_diagnostic
):
    service = RecordingService([Response(b"\x00\x00")])
    client, _ = configured_client(service)

    with pytest.raises(TargetTextValidationError) as failure:
        client.synthesize(translated_segment(text=unsafe_text))

    assert failure.value.sequence_id == 7
    assert expected_diagnostic in str(failure.value)
    assert unsafe_text not in str(failure.value)
    assert service.calls == []


def test_no_audio_is_an_explicit_failure_and_never_publishes_a_result():
    service = RecordingService([Response(b""), SimpleNamespace()])
    client, _ = configured_client(service, ticks=(100,))
    client.max_retries = 1

    with pytest.raises(DirectTTSError, match="no audio"):
        client.synthesize(translated_segment())

    assert len(service.calls) == 1
    assert client._synthesis_active is False


def test_partial_pcm_frame_is_rejected():
    service = RecordingService([Response(b"\x00")])
    client, _ = configured_client(service, ticks=(100,))
    client.max_retries = 1

    with pytest.raises(DirectTTSError, match="partial PCM frame"):
        client.synthesize(translated_segment())

    assert len(service.calls) == 1


def test_oversized_response_chunk_is_rejected_before_buffering():
    service = RecordingService([Response(b"\x00\x00\x00\x00")])
    client = DirectTTSClient(
        max_response_chunk_bytes=2,
        max_retries=1,
        clock_ms=lambda: 100,
    )
    client._connected = True
    client._service = service

    with pytest.raises(DirectTTSError, match="response chunk exceeded"):
        client.synthesize(translated_segment())

    assert len(service.calls) == 1


def test_total_segment_audio_limit_is_enforced_across_chunks():
    service = RecordingService([Response(b"\x00\x00"), Response(b"\x00\x00")])
    client = DirectTTSClient(
        sample_rate_hz=1,
        max_audio_duration_s=1,
        max_retries=1,
        clock_ms=lambda: 100,
    )
    client._connected = True
    client._service = service

    with pytest.raises(DirectTTSError, match="segment exceeded"):
        client.synthesize(translated_segment())

    assert len(service.calls) == 1


def test_service_failure_is_wrapped_with_segment_identity_and_releases_state():
    client, _ = configured_client(FailingService(), ticks=(100,))

    with pytest.raises(DirectTTSError, match="segment 7") as failure:
        client.synthesize(translated_segment())

    assert isinstance(failure.value.__cause__, RuntimeError)
    assert "TTS unavailable" in str(failure.value.__cause__)
    assert client._synthesis_active is False
    assert client._active_call is None


def test_unknown_after_private_pcm_retries_once_and_publishes_only_second_attempt():
    first = FailingAfterAudio(grpc.StatusCode.UNKNOWN)
    second = iter([Response(b"\x03\x04"), Response(b"\x05\x06")])
    service = AttemptService([first, second])
    client, _ = configured_client(
        service,
        ticks=(100, 110, 120, 130, 140),
    )
    client.max_retries = 1
    translation = translated_segment()

    result = client.synthesize(translation)

    assert result.audio == b"\x03\x04\x05\x06"
    assert result.retry_count == 1
    assert result.started_monotonic_ms == 100
    assert result.first_audio_monotonic_ms == 130
    assert result.completed_monotonic_ms == 140
    assert first.cancelled is True
    assert len(service.calls) == 2
    assert service.calls[0] == service.calls[1]
    assert client._synthesis_active is False
    assert client._active_call is None


def test_response_metrics_discard_failed_attempt_pcm_before_atomic_retry():
    first = FailingAfterAudio(grpc.StatusCode.UNKNOWN)
    second = iter([Response(b"\x03\x04"), Response(b"\x05\x06")])
    service = AttemptService([first, second])
    clock = MagicMock(side_effect=(100, 110, 120, 130, 140, 150))
    client = DirectTTSClient(
        max_retries=1,
        capture_response_chunk_metrics=True,
        clock_ms=clock,
    )
    client._connected = True
    client._service = service

    result = client.synthesize(translated_segment())

    assert result.audio == b"\x03\x04\x05\x06"
    assert result.retry_count == 1
    assert result.started_monotonic_ms == 100
    assert result.first_audio_monotonic_ms == 130
    assert result.completed_monotonic_ms == 150
    assert [
        (
            chunk.response_index,
            chunk.audio_bytes,
            chunk.cumulative_audio_bytes,
            chunk.received_monotonic_ms,
            chunk.retry_count,
        )
        for chunk in result.response_chunks
    ] == [
        (0, 2, 2, 130, 1),
        (1, 2, 4, 140, 1),
    ]
    assert first.cancelled is True
    assert len(service.calls) == 2


def test_two_unknown_failures_raise_privacy_safe_retry_error_once():
    service = AttemptService(
        [
            GrpcStatusError(grpc.StatusCode.UNKNOWN),
            GrpcStatusError(grpc.StatusCode.UNKNOWN),
        ]
    )
    client, _ = configured_client(service, ticks=(100, 110))
    client.max_retries = 1

    with pytest.raises(DirectTTSRetryError) as failure:
        client.synthesize(translated_segment(text="Texto privado."))

    assert failure.value.sequence_id == 7
    assert failure.value.retry_count == 1
    assert failure.value.initial_status_code == "UNKNOWN"
    assert failure.value.retry_status_code == "UNKNOWN"
    assert "Texto privado" not in str(failure.value)
    assert len(service.calls) == 2
    assert client._synthesis_active is False
    assert client._active_call is None


def test_disconnect_during_second_attempt_preserves_retry_attribution():
    second = BlockingCall()
    service = AttemptService(
        [
            GrpcStatusError(grpc.StatusCode.UNKNOWN),
            second,
        ]
    )
    client, channel = configured_client(service, ticks=(100, 110))
    client.max_retries = 1
    failures = []

    worker = threading.Thread(
        target=lambda: _capture_failure(
            failures,
            lambda: client.synthesize(translated_segment()),
        )
    )
    worker.start()
    assert second.iterating.wait(timeout=1)

    client.disconnect()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    assert len(failures) == 1
    assert isinstance(failures[0], DirectTTSCancelled)
    assert failures[0].sequence_id == 7
    assert failures[0].retry_count == 1
    assert len(service.calls) == 2
    channel.close.assert_called_once_with()


def test_nonretryable_grpc_status_is_attempted_once():
    service = AttemptService(
        [GrpcStatusError(grpc.StatusCode.INVALID_ARGUMENT)]
    )
    client, _ = configured_client(service, ticks=(100,))
    client.max_retries = 1

    with pytest.raises(DirectTTSError):
        client.synthesize(translated_segment())

    assert len(service.calls) == 1


def test_non_grpc_unknown_lookalike_is_attempted_once():
    service = AttemptService([GrpcLookalikeError("not an RPC error")])
    client, _ = configured_client(service, ticks=(100,))
    client.max_retries = 1

    with pytest.raises(DirectTTSError):
        client.synthesize(translated_segment())

    assert len(service.calls) == 1


def test_disconnected_client_fails_before_calling_service():
    service = RecordingService([Response(b"\x00\x00")])
    client = DirectTTSClient()
    client._service = service

    with pytest.raises(RuntimeError, match="not connected"):
        client.synthesize(translated_segment())

    assert service.calls == []


@pytest.mark.parametrize(
    "language_configs, message",
    [
        ({}, "unsupported TTS language"),
        ({"es-US": {"available": True, "voice": ""}}, "no TTS voice"),
        (
            {"es-US": {"available": False, "voice": "unused"}},
            "unsupported TTS language",
        ),
    ],
)
def test_language_and_voice_are_validated_before_rpc(language_configs, message):
    service = RecordingService([Response(b"\x00\x00")])
    client = DirectTTSClient(language_configs=language_configs)
    client._connected = True
    client._service = service

    with pytest.raises(ValueError, match=message):
        client.synthesize(translated_segment())

    assert service.calls == []


def test_concurrent_synthesis_is_rejected_and_disconnect_cancels_active_call():
    call = BlockingCall()
    client, channel = configured_client(BlockingService(call), ticks=(100,))
    errors = []

    def run():
        try:
            client.synthesize(translated_segment())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert call.iterating.wait(timeout=1)

    with pytest.raises(RuntimeError, match="already active"):
        client.synthesize(translated_segment())

    client.disconnect()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    assert len(errors) == 1
    assert isinstance(errors[0], DirectTTSCancelled)
    assert call.cancel_count >= 1
    assert client.is_connected() is False
    channel.close.assert_called_once_with()


def test_connect_is_idempotent_and_disconnect_is_idempotent():
    client = DirectTTSClient()
    auth = SimpleNamespace(channel=MagicMock())
    service = object()

    with patch("direct_tts_client.riva.client.Auth", return_value=auth) as auth_cls, patch(
        "direct_tts_client.riva.client.SpeechSynthesisService",
        return_value=service,
    ) as service_cls:
        assert client.connect() is True
        assert client.connect() is True

    auth_cls.assert_called_once_with(uri="localhost:50053")
    service_cls.assert_called_once_with(auth)
    client.disconnect()
    client.disconnect()
    auth.channel.close.assert_called_once_with()


def test_failed_client_construction_closes_new_channel():
    client = DirectTTSClient(uri="tts.test:50053")
    auth = SimpleNamespace(channel=MagicMock())

    with patch("direct_tts_client.riva.client.Auth", return_value=auth), patch(
        "direct_tts_client.riva.client.SpeechSynthesisService",
        side_effect=RuntimeError("bad stub"),
    ):
        assert client.connect() is False

    auth.channel.close.assert_called_once_with()
    assert client.is_connected() is False


def test_clock_failure_releases_exclusive_synthesis_state():
    service = RecordingService([Response(b"\x00\x00")])
    client, _ = configured_client(service)
    client._clock_ms = MagicMock(side_effect=RuntimeError("clock failed"))

    with pytest.raises(DirectTTSError, match="segment 7"):
        client.synthesize(translated_segment())

    assert client._synthesis_active is False
    assert client._active_call is None


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"sample_rate_hz": 0}, "sample_rate_hz"),
        ({"sample_rate_hz": True}, "sample_rate_hz"),
        ({"channels": 2}, "mono"),
        ({"channels": True}, "channels"),
        ({"bytes_per_sample": 4}, "16-bit"),
        ({"bytes_per_sample": True}, "bytes_per_sample"),
    ],
)
def test_audio_format_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        DirectTTSClient(**kwargs)


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"max_response_chunk_bytes": 0}, "max_response_chunk_bytes"),
        ({"max_response_chunk_bytes": True}, "max_response_chunk_bytes"),
        ({"max_audio_duration_s": 0}, "max_audio_duration_s"),
        ({"max_audio_duration_s": float("inf")}, "max_audio_duration_s"),
        ({"max_audio_duration_s": True}, "max_audio_duration_s"),
        ({"max_retries": -1}, "max_retries"),
        ({"max_retries": 2}, "max_retries"),
        ({"max_retries": True}, "max_retries"),
        (
            {"capture_response_chunk_metrics": 1},
            "capture_response_chunk_metrics",
        ),
        ({"incremental_frame_ms": 0}, "incremental_frame_ms"),
        ({"incremental_frame_ms": True}, "incremental_frame_ms"),
        (
            {"incremental_atomic_fallback_max_chars": -1},
            "incremental_atomic_fallback_max_chars",
        ),
        (
            {"incremental_atomic_fallback_max_chars": True},
            "incremental_atomic_fallback_max_chars",
        ),
    ],
)
def test_audio_bound_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        DirectTTSClient(**kwargs)


@pytest.mark.parametrize("uri", ["", "   ", 42])
def test_explicit_invalid_uri_is_rejected(uri):
    with pytest.raises(ValueError, match="uri"):
        DirectTTSClient(uri=uri)


def _capture_failure(destination, operation):
    try:
        operation()
    except BaseException as exc:
        destination.append(exc)
