"""WebSocket session management for translation streams."""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from riva_client import riva_client, AudioChunkIterator
from audio_processor import float32_to_int16, calculate_rms


class SessionStatus(str, Enum):
    """Session status states."""
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    LISTENING = "listening"
    PROCESSING = "processing"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class TranslationSession:
    """Manages a single translation session."""
    websocket: WebSocket
    status: SessionStatus = SessionStatus.CONNECTED
    target_language: str = "es-US"
    chunk_iterator: Optional[AudioChunkIterator] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False

    def _is_websocket_open(self) -> bool:
        """Check if WebSocket is still open."""
        return (
            not self._closed
            and self.websocket.client_state == WebSocketState.CONNECTED
            and self.websocket.application_state == WebSocketState.CONNECTED
        )

    async def send_status(self, status: SessionStatus, message: str = "") -> None:
        """Send status update to client."""
        self.status = status
        if self._is_websocket_open():
            try:
                await self.websocket.send_json({
                    "type": "status",
                    "status": status.value,
                    "message": message
                })
            except Exception:
                pass  # WebSocket already closed

    async def send_error(self, message: str) -> None:
        """Send error message to client."""
        self.status = SessionStatus.ERROR
        if self._is_websocket_open():
            try:
                await self.websocket.send_json({
                    "type": "error",
                    "message": message
                })
            except Exception:
                pass  # WebSocket already closed

    async def send_audio(self, audio_data: bytes) -> None:
        """Send translated audio to client."""
        if self._is_websocket_open():
            try:
                print(f"[WS] Sending {len(audio_data)} bytes of audio to client")
                await self.websocket.send_bytes(audio_data)
            except Exception as e:
                print(f"[WS] Failed to send audio: {e}")

    async def send_level(self, rms: float) -> None:
        """Send audio level to client."""
        if self._is_websocket_open():
            try:
                await self.websocket.send_json({
                    "type": "level",
                    "rms": rms
                })
            except Exception:
                pass  # WebSocket already closed

    async def start_stream(self, target_language: str) -> None:
        """Start a new translation stream."""
        async with self._lock:
            print(f"[WS] start_stream called: target={target_language}, current_status={self.status}")

            # Stop any existing stream first
            if self.chunk_iterator:
                print("[WS] Stopping existing stream before starting new one")
                self.chunk_iterator.stop()
                self.chunk_iterator = None

            self.target_language = target_language
            await self.send_status(SessionStatus.LISTENING, f"Translating to {target_language}")

            # Capture the event loop for thread-safe callbacks
            loop = asyncio.get_running_loop()

            # Create callback to send audio back through WebSocket
            # These run in a background thread, so use run_coroutine_threadsafe
            def on_audio(audio_bytes: bytes):
                if not self._closed:
                    asyncio.run_coroutine_threadsafe(self.send_audio(audio_bytes), loop)

            def on_error(error_msg: str):
                if not self._closed:
                    asyncio.run_coroutine_threadsafe(self.send_error(error_msg), loop)

            try:
                self.chunk_iterator = await riva_client.translate_stream(
                    target_language=target_language,
                    on_audio=on_audio,
                    on_error=on_error,
                )
            except Exception as e:
                await self.send_error(f"Failed to start stream: {str(e)}")

    async def stop_stream(self) -> None:
        """Stop the current translation stream and notify client."""
        async with self._lock:
            print(f"[WS] stop_stream called, current_status={self.status}")
            if self.chunk_iterator:
                self.chunk_iterator.stop()
                self.chunk_iterator = None
            await self.send_status(SessionStatus.STOPPED, "Stream stopped")

    def close(self) -> None:
        """Close the session without sending messages (for cleanup)."""
        self._closed = True
        if self.chunk_iterator:
            self.chunk_iterator.stop()
            self.chunk_iterator = None

    async def process_audio(self, audio_bytes: bytes) -> None:
        """Process incoming audio chunk from client."""
        if self.status != SessionStatus.LISTENING or not self.chunk_iterator:
            print(f"[WS] Ignoring audio: status={self.status}, has_iterator={self.chunk_iterator is not None}")
            return

        # Audio is already Int16 from the browser (converted in AudioWorklet)
        # Just pass it through to Riva
        print(f"[WS] Received {len(audio_bytes)} bytes (Int16) from client")

        # Calculate RMS for visualization
        rms = calculate_rms(audio_bytes, dtype="int16")

        # Send to Riva (already Int16 format)
        self.chunk_iterator.add_chunk(audio_bytes)

        # Send RMS level back to client for visualization
        await self.send_level(rms)


class SessionManager:
    """Manages active translation sessions (single user mode)."""

    def __init__(self):
        self._active_session: Optional[TranslationSession] = None
        self._lock = asyncio.Lock()

    async def create_session(self, websocket: WebSocket) -> Optional[TranslationSession]:
        """Create a new session, closing any existing one."""
        async with self._lock:
            # Close existing session if any (don't try to send messages)
            if self._active_session:
                self._active_session.close()
                self._active_session = None

            # Ensure Riva is connected
            if not riva_client.is_connected():
                if not riva_client.connect():
                    return None

            session = TranslationSession(websocket=websocket)
            self._active_session = session
            return session

    async def remove_session(self, session: TranslationSession) -> None:
        """Remove a session."""
        async with self._lock:
            if self._active_session == session:
                session.close()
                self._active_session = None

    def get_active_session(self) -> Optional[TranslationSession]:
        """Get the currently active session."""
        return self._active_session


# Global session manager
session_manager = SessionManager()
