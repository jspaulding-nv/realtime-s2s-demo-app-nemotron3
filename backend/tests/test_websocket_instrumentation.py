"""Unit tests for timing instrumentation in websocket_handler."""

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
