"""Timing event collection and pub/sub for latency measurement."""

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TimingEvent:
    """A single timing measurement event."""
    stage: str
    timestamp: float
    chunk_index: int
    source_position_sec: float
    audio_bytes_len: int
    wall_clock: float


class TimingLogger:
    """Thread-safe timing event logger with pub/sub for real-time streaming.

    Called from both the Riva ThreadPoolExecutor thread and the asyncio event loop,
    so all mutations are protected by a threading.Lock.
    """

    # chunk_size / sample_rate = 4800 / 16000
    SECONDS_PER_CHUNK = 0.3

    def __init__(self, maxlen: int = 100_000):
        self._events: deque[TimingEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._sub_lock = threading.Lock()
        self._is_test_active = False
        self._test_start_time: Optional[float] = None
        self._chunk_counter = 0

    @property
    def is_test_active(self) -> bool:
        return self._is_test_active

    def start_test(self) -> None:
        """Begin a new test session, clearing previous data."""
        with self._lock:
            self._events.clear()
            self._chunk_counter = 0
            self._test_start_time = time.time()
            self._is_test_active = True

    def stop_test(self) -> None:
        """End the current test session."""
        with self._lock:
            self._is_test_active = False

    def _record(self, stage: str, chunk_index: int, source_position_sec: float,
                audio_bytes_len: int) -> None:
        """Record an event (must be called with _lock held or when safe)."""
        now = time.time()
        wall_clock = now - self._test_start_time if self._test_start_time else 0.0
        event = TimingEvent(
            stage=stage,
            timestamp=now,
            chunk_index=chunk_index,
            source_position_sec=source_position_sec,
            audio_bytes_len=audio_bytes_len,
            wall_clock=wall_clock,
        )
        self._events.append(event)
        self._notify(event)

    def _notify(self, event: TimingEvent) -> None:
        """Push event to all subscribers. Drops if queue is full."""
        with self._sub_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # slow subscriber -- drop event

    def log_audio_received(self, audio_bytes_len: int) -> int:
        """Log an incoming audio chunk from the client.

        Returns the chunk index assigned to this chunk.
        """
        with self._lock:
            if not self._is_test_active:
                return -1
            idx = self._chunk_counter
            self._chunk_counter += 1
            source_pos = idx * self.SECONDS_PER_CHUNK
            self._record("audio_received", idx, source_pos, audio_bytes_len)
            return idx

    def log_audio_to_riva(self, chunk_index: int, audio_bytes_len: int) -> None:
        """Log audio being forwarded to Riva."""
        with self._lock:
            if not self._is_test_active:
                return
            source_pos = chunk_index * self.SECONDS_PER_CHUNK
            self._record("audio_to_riva", chunk_index, source_pos, audio_bytes_len)

    def log_audio_from_riva(self, audio_bytes_len: int) -> None:
        """Log audio received from Riva (called from Riva thread)."""
        with self._lock:
            if not self._is_test_active:
                return
            self._record("audio_from_riva", -1, 0.0, audio_bytes_len)

    def log_audio_sent_to_client(self, audio_bytes_len: int) -> None:
        """Log audio sent to the client via WebSocket."""
        with self._lock:
            if not self._is_test_active:
                return
            self._record("audio_sent_to_client", -1, 0.0, audio_bytes_len)

    def subscribe(self, maxsize: int = 10_000) -> asyncio.Queue:
        """Subscribe to real-time events. Returns an asyncio.Queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber."""
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def get_all_events(self) -> list[dict]:
        """Return all recorded events as dicts (does not clear)."""
        with self._lock:
            return [
                {
                    "stage": e.stage,
                    "timestamp": e.timestamp,
                    "chunk_index": e.chunk_index,
                    "source_position_sec": e.source_position_sec,
                    "audio_bytes_len": e.audio_bytes_len,
                    "wall_clock": e.wall_clock,
                }
                for e in self._events
            ]


# Global instance
timing_logger = TimingLogger()
