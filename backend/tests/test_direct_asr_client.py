import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from direct_asr_client import (
    DirectASRClient,
    DirectASRStreamClosed,
    _classify_word_timing_shapes,
    iter_transcript_results,
)
from staged_models import (
    ASRBoundaryNumericClassCounts,
    ASRBoundaryObservation,
    ASRStreamEvent,
    ASRStreamEventKind,
    ASRTranscript,
    ASRWordTimingEnvelope,
    ASRWordTimingShapeCounts,
    AsrFinal,
)


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
    assert event.word_count == 2
    assert event.first_word_start_ms == 250
    assert event.last_word_end_ms == 900
    assert event.timing_basis == "word_offsets"
    diagnostics = event.word_timing_shape_diagnostics.to_dict()
    assert diagnostics["envelope"]["shape"] == "valid"
    assert diagnostics["counts"]["entry_shape"]["valid"] == 2
    assert diagnostics["anomalies"] == []


def test_response_parser_skips_shape_diagnostics_for_timed_interims():
    words = [
        SimpleNamespace(start_time=250, end_time=400),
        SimpleNamespace(start_time=450, end_time=900),
    ]

    event = next(
        iter_transcript_results(
            [response(result("Interim.", words=words))],
            clock_ms=lambda: 1_000,
        )
    )

    assert event.is_final is False
    assert event.source_start_ms == 250
    assert event.source_end_ms == 900
    assert event.first_word_start_ms == 250
    assert event.last_word_end_ms == 900
    assert event.timing_basis == "word_offsets"
    assert event.word_timing_shape_diagnostics is None


def test_response_parser_keeps_missing_word_start_fail_closed():
    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Untimed final.",
                        is_final=True,
                        audio_processed=12.5,
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    assert event.word_count == 0
    assert event.first_word_start_ms is None
    assert event.last_word_end_ms is None
    assert event.source_start_ms is None
    assert event.source_end_ms == 12_500
    assert event.timing_basis == "audio_processed_end_only"


def test_response_parser_marks_partial_word_envelope_without_inventing_start():
    words = [
        SimpleNamespace(end_time=400),
        SimpleNamespace(start_time=450, end_time=900),
    ]

    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Partially timed.",
                        is_final=True,
                        audio_processed=1.25,
                        words=words,
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    assert event.word_count == 2
    assert event.first_word_start_ms is None
    assert event.last_word_end_ms == 900
    assert event.source_start_ms is None
    assert event.source_end_ms == 900
    assert event.timing_basis == "incomplete_word_offsets"


def test_response_parser_rejects_default_zero_length_word_envelope():
    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Default scalar offsets.",
                        is_final=True,
                        audio_processed=1.25,
                        words=[
                            SimpleNamespace(start_time=0, end_time=0),
                        ],
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    assert event.word_count == 1
    assert event.first_word_start_ms == 0
    assert event.last_word_end_ms is None
    assert event.source_start_ms is None
    assert event.source_end_ms == 1_250
    assert event.timing_basis == "incomplete_word_offsets"
    diagnostics = event.word_timing_shape_diagnostics.to_dict()
    assert diagnostics["envelope"]["shape"] == "zero_length"
    assert diagnostics["anomalies"][0]["shape"] == "zero_length"


def proto3_word_info(*, start_time=0, end_time=0):
    descriptor = SimpleNamespace(
        fields_by_name={
            "start_time": SimpleNamespace(has_presence=False),
            "end_time": SimpleNamespace(has_presence=False),
        }
    )
    return SimpleNamespace(
        DESCRIPTOR=descriptor,
        start_time=start_time,
        end_time=end_time,
    )


def test_proto3_word_info_zero_scalars_have_unobservable_presence():
    word = proto3_word_info()

    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Default protobuf fields.",
                        is_final=True,
                        words=[word],
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    diagnostics = event.word_timing_shape_diagnostics.to_dict()
    envelope = diagnostics["envelope"]
    assert envelope["start"] == {
        "presence": "unobservable",
        "numeric_class": "zero",
        "finite_value_ms": 0,
    }
    assert envelope["end"] == {
        "presence": "unobservable",
        "numeric_class": "zero",
        "finite_value_ms": 0,
    }
    assert envelope["shape"] == "zero_length"


def test_proto3_word_info_positive_start_zero_end_is_raw_reversed():
    word = proto3_word_info(start_time=100)

    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Zero or unset end.",
                        is_final=True,
                        words=[word],
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    envelope = event.word_timing_shape_diagnostics.to_dict()["envelope"]
    assert envelope["start"]["presence"] == "unobservable"
    assert envelope["start"]["numeric_class"] == "positive"
    assert envelope["end"]["presence"] == "unobservable"
    assert envelope["end"]["numeric_class"] == "zero"
    assert envelope["numeric_relation"] == "end_before_start"
    assert envelope["shape"] == "reversed"


def test_installed_word_info_descriptor_and_zero_values_match_assumption():
    script = """
import json
try:
    import riva.client
    from direct_asr_client import _classify_word_timing_shapes
except ImportError:
    raise SystemExit(77)
word = riva.client.proto.riva_asr_pb2.WordInfo()
diagnostics = _classify_word_timing_shapes((word,)).to_dict()
print(json.dumps({
    "start_has_presence": (
        word.DESCRIPTOR.fields_by_name["start_time"].has_presence
    ),
    "end_has_presence": (
        word.DESCRIPTOR.fields_by_name["end_time"].has_presence
    ),
    "diagnostics": diagnostics,
}))
"""
    environment = dict(os.environ)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 77:
        pytest.skip("installed Riva client is unavailable")
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)

    assert observed["start_has_presence"] is False
    assert observed["end_has_presence"] is False
    envelope = observed["diagnostics"]["envelope"]
    assert envelope["start"]["presence"] == "unobservable"
    assert envelope["start"]["numeric_class"] == "zero"
    assert envelope["end"]["presence"] == "unobservable"
    assert envelope["end"]["numeric_class"] == "zero"


@pytest.mark.parametrize(
    ("word", "expected_shape", "start_class", "end_class"),
    [
        (
            SimpleNamespace(end_time=100),
            "absent_boundary",
            "not_available",
            "positive",
        ),
        (
            SimpleNamespace(start_time=0, end_time=100),
            "valid",
            "zero",
            "positive",
        ),
        (
            SimpleNamespace(start_time="not-a-number", end_time=100),
            "unparseable_boundary",
            "unparseable",
            "positive",
        ),
        (
            SimpleNamespace(start_time=float("nan"), end_time=100),
            "nonfinite_boundary",
            "nonfinite",
            "positive",
        ),
        (
            SimpleNamespace(start_time=0, end_time=float("inf")),
            "nonfinite_boundary",
            "zero",
            "nonfinite",
        ),
        (
            SimpleNamespace(start_time=-1, end_time=100),
            "negative_boundary",
            "negative",
            "positive",
        ),
        (
            SimpleNamespace(start_time=200, end_time=-1),
            "negative_boundary",
            "positive",
            "negative",
        ),
        (
            SimpleNamespace(start_time=200, end_time=200),
            "zero_length",
            "positive",
            "positive",
        ),
        (
            SimpleNamespace(start_time=200, end_time=100),
            "reversed",
            "positive",
            "positive",
        ),
    ],
)
def test_response_parser_classifies_raw_word_timing_shapes(
    word,
    expected_shape,
    start_class,
    end_class,
):
    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Timing shape.",
                        is_final=True,
                        words=[word],
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    diagnostics = event.word_timing_shape_diagnostics.to_dict()
    assert diagnostics["envelope"]["shape"] == expected_shape
    assert (
        diagnostics["envelope"]["start"]["numeric_class"]
        == start_class
    )
    assert diagnostics["envelope"]["end"]["numeric_class"] == end_class
    assert diagnostics["counts"]["entry_shape"][expected_shape] == 1
    assert sum(diagnostics["counts"]["entry_shape"].values()) == 1
    assert (
        sum(diagnostics["counts"]["start_numeric_class"].values())
        == 1
    )
    assert sum(diagnostics["counts"]["end_numeric_class"].values()) == 1
    assert len(diagnostics["anomalies"]) == (
        0 if expected_shape == "valid" else 1
    )


def test_response_parser_counts_invalid_interior_word_independently():
    words = [
        SimpleNamespace(start_time=0, end_time=100),
        SimpleNamespace(start_time=200, end_time=0),
        SimpleNamespace(start_time=300, end_time=400),
    ]

    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Interior anomaly.",
                        is_final=True,
                        words=words,
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    diagnostics = event.word_timing_shape_diagnostics.to_dict()
    assert diagnostics["envelope"]["shape"] == "valid"
    assert diagnostics["counts"]["entry_shape"]["valid"] == 2
    assert diagnostics["counts"]["entry_shape"]["reversed"] == 1
    assert [item["word_index"] for item in diagnostics["anomalies"]] == [1]
    assert event.timing_basis == "word_offsets"


def test_response_parser_records_no_word_entries_without_token_content():
    event = next(
        iter_transcript_results(
            [response(result("No words.", is_final=True, words=[]))],
            clock_ms=lambda: 1_000,
        )
    )

    diagnostics = event.word_timing_shape_diagnostics.to_dict()
    assert diagnostics["word_entry_count"] == 0
    assert diagnostics["no_word_entries"] is True
    assert diagnostics["envelope"] is None
    assert diagnostics["anomalies"] == []
    assert all(
        sum(group.values()) == 0
        for group in diagnostics["counts"].values()
    )


def test_asr_final_preserves_and_validates_shape_diagnostics():
    event = next(
        iter_transcript_results(
            [
                response(
                    result(
                        "Timed final.",
                        is_final=True,
                        words=[
                            SimpleNamespace(start_time=100, end_time=900),
                        ],
                    )
                )
            ],
            clock_ms=lambda: 1_000,
        )
    )

    final = AsrFinal.from_transcript(0, event)

    assert (
        final.word_timing_shape_diagnostics
        is event.word_timing_shape_diagnostics
    )
    with pytest.raises(ValueError, match="must match word_count"):
        AsrFinal(
            final_id=1,
            text="private transcript",
            received_monotonic_ms=1_000,
            word_count=2,
            first_word_start_ms=100,
            last_word_end_ms=900,
            timing_basis="word_offsets",
            word_timing_shape_diagnostics=(
                event.word_timing_shape_diagnostics
            ),
        )


def test_word_timing_counters_reject_boolean_values():
    with pytest.raises(ValueError, match="non-negative integers"):
        ASRWordTimingShapeCounts(valid=True)


def test_word_timing_typed_diagnostics_reject_bool_and_impossible_residuals():
    with pytest.raises(ValueError, match="must be numeric"):
        ASRBoundaryObservation(
            presence="present",
            numeric_class="positive",
            finite_value_ms=True,
        )
    start = ASRBoundaryObservation(
        presence="present",
        numeric_class="zero",
        finite_value_ms=0,
    )
    end = ASRBoundaryObservation(
        presence="present",
        numeric_class="positive",
        finite_value_ms=100,
    )
    with pytest.raises(ValueError, match="usable must be boolean"):
        ASRWordTimingEnvelope(
            start_word_index=0,
            end_word_index=0,
            start=start,
            end=end,
            numeric_relation="end_after_start",
            shape="valid",
            usable=1,
        )
    with pytest.raises(ValueError, match="no_word_entries must be boolean"):
        replace(
            _classify_word_timing_shapes(()),
            no_word_entries=0,
        )

    two_valid_words = _classify_word_timing_shapes(
        (
            SimpleNamespace(start_time=0, end_time=100),
            SimpleNamespace(start_time=200, end_time=300),
        )
    )
    with pytest.raises(ValueError, match="impossible boundary categories"):
        replace(
            two_valid_words,
            start_numeric_class_counts=ASRBoundaryNumericClassCounts(
                negative=1,
                positive=1,
            ),
        )


def test_incomplete_normalized_offsets_must_match_raw_envelope():
    diagnostics = _classify_word_timing_shapes(
        (SimpleNamespace(start_time=100, end_time=0),)
    )

    with pytest.raises(ValueError, match="match the raw envelope"):
        AsrFinal(
            final_id=0,
            text="private transcript",
            received_monotonic_ms=1_000,
            source_end_ms=1_000,
            audio_processed_s=1.0,
            word_count=1,
            first_word_start_ms=999,
            last_word_end_ms=None,
            timing_basis="incomplete_word_offsets",
            word_timing_shape_diagnostics=diagnostics,
        )


def test_response_parser_preserves_hypothesis_local_source_timing():
    responses = [
        response(result("interim", audio_processed=68.9600601196289)),
        response(
            result(
                "Final words.",
                is_final=True,
                audio_processed=69.0,
                words=[
                    SimpleNamespace(start_time=60_800, end_time=61_040),
                    SimpleNamespace(start_time=67_760, end_time=68_080),
                ],
            )
        ),
    ]

    interim, final_event = iter_transcript_results(
        responses, clock_ms=iter([180_109_747.9, 180_110_061.943]).__next__
    )

    # A final word envelope can end before an earlier interim's processed-audio
    # horizon. That is a normal hypothesis transition, not a stream reset.
    assert interim.source_end_ms == pytest.approx(68_960.0601196289)
    assert final_event.source_start_ms == 60_800
    assert final_event.source_end_ms == 68_080
    assert final_event.source_end_ms < interim.source_end_ms


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
