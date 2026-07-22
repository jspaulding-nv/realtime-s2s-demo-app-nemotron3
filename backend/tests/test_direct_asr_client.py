import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from direct_asr_client import (
    DirectASRClient,
    DirectASRStreamClosed,
    iter_transcript_results,
)
from staged_models import ASRStreamEvent, ASRStreamEventKind, ASRTranscript, AsrFinal


def response(*results):
    return SimpleNamespace(results=list(results))


def result(
    text,
    *,
    is_final=False,
    stability=0.5,
    confidence=0.75,
    audio_processed=1.25,
    words=(),
    languages=(),
):
    alternative = SimpleNamespace(
        transcript=text,
        confidence=confidence,
        words=list(words),
        language_code=list(languages),
    )
    return SimpleNamespace(
        alternatives=[alternative],
        is_final=is_final,
        stability=stability,
        audio_processed=audio_processed,
    )


def test_response_parser_routes_all_results_and_skips_empty_entries():
    no_alternatives = SimpleNamespace(alternatives=[])
    responses = [
        response(
            result(" interim ", languages=["en-US"]),
            no_alternatives,
            result("Final.", is_final=True, confidence=0.9),
            result("   ", is_final=True),
        )
    ]
    ticks = iter([100.0, 200.0])

    events = list(iter_transcript_results(responses, clock_ms=lambda: next(ticks)))

    assert [item.text for item in events] == ["interim", "Final."]
    assert [item.is_final for item in events] == [False, True]
    assert events[0].detected_languages == ("en-US",)
    assert events[1].confidence == pytest.approx(0.9)
    assert events[1].source_end_ms == pytest.approx(1_250)


def test_response_parser_prefers_word_timing_envelope():
    words = [
        SimpleNamespace(start_time=250, end_time=400),
        SimpleNamespace(start_time=450, end_time=900),
    ]

    event = next(
        iter_transcript_results(
            [response(result("Timed.", is_final=True, words=words))],
            clock_ms=lambda: 1_000,
        )
    )

    assert event.source_start_ms == 250
    assert event.source_end_ms == 900


def test_shared_asr_config_uses_rnnt_800ms_and_punctuation_fields():
    with patch("asr_config.riva_asr_pb2.EndpointingConfig") as endpoint_cls, patch(
        "asr_config.riva_asr_pb2.RecognitionConfig"
    ) as recognition_cls, patch(
        "asr_config.riva_asr_pb2.StreamingRecognitionConfig"
    ) as streaming_cls:
        from asr_config import create_streaming_asr_config

        create_streaming_asr_config(
            sample_rate_hz=16_000,
            channels=1,
            language_code="en-US",
            endpointing_history_ms=800,
            enable_word_time_offsets=True,
        )

    endpoint_cls.assert_called_once_with(
        start_history=300,
        start_threshold=0.2,
        stop_history=800,
        stop_threshold=0.98,
    )
    endpoint_kwargs = endpoint_cls.call_args.kwargs
    assert "stop_history_eou" not in endpoint_kwargs
    recognition_kwargs = recognition_cls.call_args.kwargs
    assert recognition_kwargs["enable_automatic_punctuation"] is True
    assert recognition_kwargs["enable_word_time_offsets"] is True
    assert recognition_kwargs["language_code"] == "en-US"
    streaming_cls.assert_called_once_with(
        config=recognition_cls.return_value,
        interim_results=True,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate_hz": 0},
        {"channels": 0},
        {"endpointing_history_ms": 0},
    ],
)
def test_shared_asr_config_rejects_nonpositive_values(kwargs):
    from asr_config import create_streaming_asr_config

    with pytest.raises(ValueError, match="must be positive"):
        create_streaming_asr_config(**kwargs)


class CompletingService:
    def streaming_response_generator(self, *, audio_chunks, streaming_config):
        del streaming_config
        list(audio_chunks)
        return iter(
            [
                response(result("Working", is_final=False)),
                response(result("Complete.", is_final=True)),
            ]
        )


class EarlyEndingService:
    def streaming_response_generator(self, *, audio_chunks, streaming_config):
        del audio_chunks, streaming_config
        return iter(())


class FailingService:
    def streaming_response_generator(self, *, audio_chunks, streaming_config):
        del audio_chunks, streaming_config
        raise RuntimeError("ASR unavailable")


class TwoFinalService:
    def streaming_response_generator(self, *, audio_chunks, streaming_config):
        del streaming_config
        list(audio_chunks)
        return iter(
            [
                response(result("First.", is_final=True)),
                response(result("Second.", is_final=True)),
            ]
        )


class FatalService:
    def streaming_response_generator(self, *, audio_chunks, streaming_config):
        del audio_chunks, streaming_config
        raise SystemExit("fatal worker failure")


def configured_client(service):
    client = DirectASRClient()
    channel = MagicMock()
    client._connected = True
    client._auth = SimpleNamespace(channel=channel)
    client._service = service
    client.create_config = MagicMock(return_value=object())
    return client, channel


async def next_event(stream):
    return await stream.next_event(timeout_s=1)


@pytest.mark.asyncio
async def test_bounded_stream_routes_interims_finals_and_completion_in_order():
    client, channel = configured_client(CompletingService())
    stream = await client.open_stream(event_queue_maxsize=2)

    stream.add_chunk(b"pcm")
    stream.finish_input()
    events = [await next_event(stream) for _ in range(3)]

    assert [event.kind for event in events] == [
        ASRStreamEventKind.INTERIM,
        ASRStreamEventKind.FINAL,
        ASRStreamEventKind.COMPLETE,
    ]
    assert events[0].transcript.text == "Working"
    assert isinstance(events[1].final, AsrFinal)
    assert events[1].final.final_id == 0
    assert events[1].final.text == "Complete."
    assert stream.audio_input._input_exhausted is True
    await client.aclose()
    channel.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_early_server_end_is_error_not_false_completion():
    client, _ = configured_client(EarlyEndingService())
    stream = await client.open_stream()

    event = await next_event(stream)

    assert event.kind is ASRStreamEventKind.ERROR
    assert "before all queued input" in event.error
    assert stream.audio_input._stopped is True
    await client.aclose()


@pytest.mark.asyncio
async def test_service_failure_reports_error_without_completion():
    client, _ = configured_client(FailingService())
    stream = await client.open_stream()

    event = await next_event(stream)

    assert event.kind is ASRStreamEventKind.ERROR
    assert "ASR unavailable" in event.error
    assert stream.audio_input._stopped is True
    await client.aclose()


@pytest.mark.asyncio
async def test_full_event_queue_backpressures_worker_and_preserves_order():
    client, _ = configured_client(TwoFinalService())
    stream = await client.open_stream(event_queue_maxsize=1)
    stream.finish_input()

    first = await next_event(stream)
    await asyncio.sleep(0.05)
    assert stream.worker_done is False
    second = await next_event(stream)
    complete = await next_event(stream)

    assert first.final.final_id == 0
    assert second.final.final_id == 1
    assert complete.kind is ASRStreamEventKind.COMPLETE
    await client.aclose()


@pytest.mark.asyncio
async def test_close_unblocks_worker_waiting_on_full_event_queue():
    client, _ = configured_client(TwoFinalService())
    stream = await client.open_stream(event_queue_maxsize=1)
    stream.finish_input()

    for _ in range(100):
        if stream.events.full():
            break
        await asyncio.sleep(0.01)
    assert stream.events.full()

    await stream.aclose(timeout_s=1)

    assert stream.worker_done is True
    await client.aclose()


@pytest.mark.asyncio
async def test_waiter_observes_manual_stream_close():
    client, _ = configured_client(CompletingService())
    stream = await client.open_stream()
    waiter = asyncio.create_task(stream.next_event())

    await asyncio.sleep(0.02)
    await stream.aclose(timeout_s=1)

    with pytest.raises(DirectASRStreamClosed, match="closed"):
        await asyncio.wait_for(waiter, timeout=1)
    await client.aclose()


@pytest.mark.asyncio
async def test_unexpected_worker_failure_is_not_silently_discarded():
    client, _ = configured_client(FatalService())
    stream = await client.open_stream()

    with pytest.raises(RuntimeError, match="failed without a terminal") as failure:
        await stream.next_event(timeout_s=1)

    assert isinstance(failure.value.__cause__, SystemExit)
    await client.aclose()


@pytest.mark.asyncio
async def test_second_stream_is_rejected_while_first_is_active():
    client, _ = configured_client(CompletingService())
    stream = await client.open_stream()

    with pytest.raises(RuntimeError, match="already active"):
        await client.open_stream()

    await stream.aclose(timeout_s=1)
    await client.aclose()


@pytest.mark.asyncio
async def test_connect_is_idempotent_and_preserves_active_channel():
    client, channel = configured_client(CompletingService())
    original_auth = client._auth
    original_service = client._service
    stream = await client.open_stream()

    assert client.connect() is True
    assert client._auth is original_auth
    assert client._service is original_service

    await stream.aclose(timeout_s=1)
    await client.aclose()
    channel.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_new_stream_is_rejected_while_client_close_is_in_progress():
    client, _ = configured_client(CompletingService())
    stream = await client.open_stream()
    close_entered = asyncio.Event()
    allow_close = asyncio.Event()
    original_close = stream.aclose

    async def delayed_close(timeout_s=10.0):
        close_entered.set()
        await allow_close.wait()
        await original_close(timeout_s=timeout_s)

    stream.aclose = delayed_close
    close_task = asyncio.create_task(client.aclose(timeout_s=1))
    await close_entered.wait()

    with pytest.raises(RuntimeError, match="closing"):
        await client.open_stream()
    assert client.connect() is False

    allow_close.set()
    await close_task


@pytest.mark.asyncio
async def test_cancelled_client_close_still_aborts_and_clears_resources():
    client, channel = configured_client(CompletingService())
    stream = await client.open_stream()
    close_entered = asyncio.Event()
    never_finish = asyncio.Event()

    async def delayed_close(timeout_s=10.0):
        del timeout_s
        close_entered.set()
        await never_finish.wait()

    stream.aclose = delayed_close
    close_task = asyncio.create_task(client.aclose(timeout_s=1))
    await close_entered.wait()
    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task
    for _ in range(100):
        if stream.worker_done:
            break
        await asyncio.sleep(0.01)

    assert stream.worker_done is True
    assert client._closing is False
    assert client.is_connected() is False
    assert client._service is None
    assert client._auth is None
    channel.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_async_client_close_stops_stream_and_closes_channel_once():
    client, channel = configured_client(CompletingService())
    stream = await client.open_stream()

    await client.aclose(timeout_s=1)

    assert stream.worker_done is True
    assert client.is_connected() is False
    channel.close.assert_called_once_with()


def test_sync_disconnect_rejects_active_worker():
    async def exercise():
        client, _ = configured_client(CompletingService())
        stream = await client.open_stream()
        with pytest.raises(RuntimeError, match="await client.aclose"):
            client.disconnect()
        await stream.aclose(timeout_s=1)
        await client.aclose()

    asyncio.run(exercise())


def test_stream_event_payload_contract_rejects_ambiguous_events():
    interim = ASRTranscript(
        text="working",
        is_final=False,
        received_monotonic_ms=1,
    )
    event = ASRStreamEvent(
        kind=ASRStreamEventKind.INTERIM,
        transcript=interim,
    )

    assert event.transcript is interim
    with pytest.raises(ValueError, match="complete events"):
        ASRStreamEvent(
            kind=ASRStreamEventKind.COMPLETE,
            transcript=interim,
        )


@pytest.mark.asyncio
async def test_disconnected_client_fails_before_starting_worker():
    client = DirectASRClient()

    with pytest.raises(RuntimeError, match="not connected"):
        await client.open_stream()

    client.disconnect()


def test_sync_config_failure_releases_exclusive_stream_flag():
    client, _ = configured_client(CompletingService())
    client.create_config = MagicMock(side_effect=RuntimeError("bad config"))

    with pytest.raises(RuntimeError, match="bad config"):
        next(client.iter_transcripts([]))

    assert client._sync_stream_active is False
    client.disconnect()
