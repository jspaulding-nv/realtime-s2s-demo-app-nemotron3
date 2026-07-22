"""Unit tests for timing instrumentation in websocket_handler."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

from websocket_handler import TranslationSession, SessionStatus


@pytest.fixture
def mock_websocket():
    ws = AsyncMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    return ws


@pytest.fixture
def session(mock_websocket):
    s = TranslationSession(websocket=mock_websocket)
    s.status = SessionStatus.LISTENING
    s.chunk_iterator = MagicMock()
    return s


@patch("websocket_handler.timing_logger")
@patch("websocket_handler.calculate_rms", return_value=0.5)
@pytest.mark.asyncio
async def test_process_audio_logs_receive_event(mock_rms, mock_tl, session):
    mock_tl.log_audio_received.return_value = 0
    audio = b"\x00" * 9600
    await session.process_audio(audio)
    mock_tl.log_audio_received.assert_called_once_with(9600)


@patch("websocket_handler.timing_logger")
@patch("websocket_handler.calculate_rms", return_value=0.5)
@pytest.mark.asyncio
async def test_process_audio_logs_riva_event(mock_rms, mock_tl, session):
    mock_tl.log_audio_received.return_value = 7
    audio = b"\x00" * 9600
    await session.process_audio(audio)
    mock_tl.log_audio_to_riva.assert_called_once_with(7, 9600)


@patch("websocket_handler.timing_logger")
@patch("websocket_handler.calculate_rms", return_value=0.5)
@pytest.mark.asyncio
async def test_process_audio_still_passes_to_iterator(mock_rms, mock_tl, session):
    mock_tl.log_audio_received.return_value = 0
    audio = b"\x00" * 9600
    await session.process_audio(audio)
    session.chunk_iterator.add_chunk.assert_called_once_with(audio)


@patch("websocket_handler.timing_logger")
@patch("websocket_handler.calculate_rms", return_value=0.5)
@pytest.mark.asyncio
async def test_no_logging_when_not_listening(mock_rms, mock_tl, session):
    session.status = SessionStatus.CONNECTED
    session.chunk_iterator = None
    audio = b"\x00" * 9600
    await session.process_audio(audio)
    mock_tl.log_audio_received.assert_not_called()
    mock_tl.log_audio_to_riva.assert_not_called()


@pytest.mark.asyncio
async def test_finish_input_stops_input_and_keeps_websocket_open(session):
    iterator = session.chunk_iterator

    await session.finish_input()

    iterator.stop.assert_called_once_with()
    assert session.chunk_iterator is iterator
    assert session.status == SessionStatus.PROCESSING
    session.websocket.send_json.assert_awaited_once_with({
        "type": "status",
        "status": "processing",
        "message": "Input complete; draining translated audio",
    })


@pytest.mark.asyncio
async def test_riva_thread_completion_emits_terminal_status(mock_websocket):
    iterator = MagicMock()
    captured_callback = None

    async def fake_translate_stream(**kwargs):
        nonlocal captured_callback
        captured_callback = kwargs["on_complete"]
        return iterator

    session = TranslationSession(websocket=mock_websocket)
    with patch(
        "websocket_handler.riva_client.translate_stream",
        side_effect=fake_translate_stream,
    ):
        await session.start_stream("es-US")
        await session.finish_input()
        captured_callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert session.status == SessionStatus.COMPLETED
    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "status",
        "status": "completed",
        "message": "Riva translated-audio stream complete",
    }


@pytest.mark.asyncio
async def test_completion_waits_for_pending_audio_send(mock_websocket):
    iterator = MagicMock()
    audio_send_started = asyncio.Event()
    release_audio_send = asyncio.Event()
    callbacks = {}

    async def delayed_send_bytes(_audio):
        audio_send_started.set()
        await release_audio_send.wait()

    async def fake_translate_stream(**kwargs):
        callbacks.update(kwargs)
        return iterator

    mock_websocket.send_bytes.side_effect = delayed_send_bytes
    session = TranslationSession(websocket=mock_websocket)
    with patch(
        "websocket_handler.riva_client.translate_stream",
        side_effect=fake_translate_stream,
    ):
        await session.start_stream("es-US")
        await session.finish_input()
        callbacks["on_audio"](b"translated pcm")
        await asyncio.wait_for(audio_send_started.wait(), timeout=2)
        completion = threading.Thread(target=callbacks["on_complete"])
        completion.start()
        await asyncio.sleep(0)

        assert session.status == SessionStatus.PROCESSING
        assert completion.is_alive()

        release_audio_send.set()
        for _ in range(200):
            if not completion.is_alive():
                break
            await asyncio.sleep(0.01)
        completion.join(timeout=0)
        assert not completion.is_alive()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert session.status == SessionStatus.COMPLETED
    mock_websocket.send_bytes.assert_awaited_once_with(b"translated pcm")
