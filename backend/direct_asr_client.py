"""Direct Nemotron streaming ASR adapter for the staged pipeline foundation."""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from collections import Counter
from concurrent.futures import (
    CancelledError as FutureCancelledError,
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from typing import Callable, Iterable, Iterator, Optional

import riva.client

from asr_config import create_streaming_asr_config
from config import riva_config
from riva_client import AudioChunkIterator
from staged_models import (
    ASR_BOUNDARY_NUMERIC_CLASSES,
    ASR_BOUNDARY_PRESENCE_ABSENT,
    ASR_BOUNDARY_PRESENCE_PRESENT,
    ASR_BOUNDARY_PRESENCE_UNOBSERVABLE,
    ASR_BOUNDARY_PRESENCES,
    ASR_TIMING_BASIS_AUDIO_PROCESSED_END_ONLY,
    ASR_TIMING_BASIS_INCOMPLETE_WORD_OFFSETS,
    ASR_TIMING_BASIS_UNAVAILABLE,
    ASR_TIMING_BASIS_WORD_OFFSETS,
    ASR_WORD_TIMING_SHAPES,
    ASRBoundaryNumericClassCounts,
    ASRBoundaryObservation,
    ASRBoundaryPresenceCounts,
    ASRStreamEvent,
    ASRStreamEventKind,
    ASRTranscript,
    ASRWordTimingEntry,
    ASRWordTimingEnvelope,
    ASRWordTimingShapeCounts,
    ASRWordTimingShapeDiagnostics,
    AsrFinal,
)


class _StreamCancelled(Exception):
    """Internal signal used to stop a worker without emitting an error."""


class DirectASRStreamClosed(RuntimeError):
    """Raised when a canceled or completed stream has no more queued events."""


class DirectASRStream:
    """One direct-ASR session with bounded, event-loop-owned output."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        event_queue_maxsize: int,
        abort_rpc: Callable[[], None],
    ) -> None:
        if event_queue_maxsize <= 0:
            raise ValueError("event_queue_maxsize must be positive")
        self.audio_input = AudioChunkIterator()
        self.events: asyncio.Queue[ASRStreamEvent] = asyncio.Queue(
            maxsize=event_queue_maxsize
        )
        self._loop = loop
        self._abort_rpc = abort_rpc
        self._cancel_requested = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending_put: Optional[Future] = None
        self._worker_future: Optional[Future] = None
        self._worker_exception: Optional[BaseException] = None
        self._close_lock = asyncio.Lock()

    @property
    def worker_done(self) -> bool:
        return self._worker_future is not None and self._worker_future.done()

    def add_chunk(self, chunk: bytes) -> None:
        self.audio_input.add_chunk(chunk)

    def finish_input(self) -> None:
        self.audio_input.stop()

    async def next_event(self, timeout_s: Optional[float] = None) -> ASRStreamEvent:
        """Return the next FIFO event with a cross-thread wakeup heartbeat."""
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        deadline = None if timeout_s is None else self._loop.time() + timeout_s
        while True:
            if deadline is not None and self._loop.time() >= deadline:
                raise asyncio.TimeoutError()
            try:
                return self.events.get_nowait()
            except asyncio.QueueEmpty:
                if self.worker_done:
                    worker_exception = self._worker_exception
                    if worker_exception is None and self._worker_future is not None:
                        try:
                            worker_exception = self._worker_future.exception()
                        except FutureCancelledError as exc:
                            worker_exception = exc
                    if worker_exception is not None:
                        raise RuntimeError(
                            "direct ASR worker failed without a terminal event"
                        ) from worker_exception
                    raise DirectASRStreamClosed(
                        "direct ASR stream closed with no remaining events"
                    )
                await asyncio.sleep(0.01)

    def _attach_worker(self, worker_future: Future) -> None:
        self._worker_future = worker_future

    def _mark_worker_done(self, worker_future: Future) -> None:
        try:
            self._worker_exception = worker_future.exception()
        except FutureCancelledError as exc:
            self._worker_exception = exc

    def _put_from_worker(self, event: ASRStreamEvent) -> None:
        """Put on the asyncio queue and block the worker when it is full."""
        if self._cancel_requested.is_set():
            raise _StreamCancelled()
        pending = asyncio.run_coroutine_threadsafe(self.events.put(event), self._loop)
        with self._pending_lock:
            self._pending_put = pending
            if self._cancel_requested.is_set():
                pending.cancel()

        try:
            while True:
                try:
                    pending.result(timeout=0.25)
                    return
                except FutureTimeoutError:
                    if self._cancel_requested.is_set():
                        pending.cancel()
                        raise _StreamCancelled()
                except FutureCancelledError as exc:
                    raise _StreamCancelled() from exc
        finally:
            with self._pending_lock:
                if self._pending_put is pending:
                    self._pending_put = None

    def _request_cancel(self) -> None:
        self._cancel_requested.set()
        self.audio_input.stop()
        with self._pending_lock:
            if self._pending_put is not None:
                self._pending_put.cancel()

    async def aclose(self, timeout_s: float = 10.0) -> None:
        """Cancel input/backpressure and wait for the blocking worker to exit."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        async with self._close_lock:
            self._request_cancel()
            worker = self._worker_future
            if worker is None:
                return
            try:
                await _await_worker(worker, timeout_s)
                return
            except asyncio.TimeoutError:
                # Closing the captured channel aborts a gRPC iterator that did
                # not respond to the input sentinel.
                self._abort_rpc()
                self._request_cancel()
            try:
                await _await_worker(worker, timeout_s)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("direct ASR worker did not stop after RPC abort") from exc


class DirectASRClient:
    """Connect directly to the ASR NIM without changing the S2S WebSocket path."""

    def __init__(
        self,
        uri: Optional[str] = None,
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        self.uri = uri or riva_config.asr_uri
        self._owns_executor = executor is None
        self._executor = executor or _new_executor()
        self._auth = None
        self._service = None
        self._connected = False
        self._channel_closed = False
        self._active_stream: Optional[DirectASRStream] = None
        self._sync_stream_active = False
        self._closing = False
        self._lifecycle_lock = threading.RLock()

    def connect(self) -> bool:
        new_auth = None
        with self._lifecycle_lock:
            if self._closing:
                print(
                    "[Direct ASR] Cannot connect while client close is in "
                    "progress",
                    file=sys.stderr,
                )
                return False
            if self._connected:
                return True
            if (
                self._sync_stream_active
                or (
                    self._active_stream is not None
                    and not self._active_stream.worker_done
                )
            ):
                print(
                    "[Direct ASR] Cannot connect while an ASR stream is active",
                    file=sys.stderr,
                )
                return False
            if self._owns_executor and self._executor is None:
                self._executor = _new_executor()
        try:
            new_auth = riva.client.Auth(uri=self.uri)
            new_service = riva.client.ASRService(new_auth)
            with self._lifecycle_lock:
                if self._closing:
                    raise RuntimeError("client close started while connecting")
                self._auth = new_auth
                self._service = new_service
                self._connected = True
                self._channel_closed = False
            return True
        except Exception as exc:
            print(
                "[Direct ASR] Connection failed "
                f"({type(exc).__name__})",
                file=sys.stderr,
            )
            channel = getattr(new_auth, "channel", None)
            if channel is not None:
                channel.close()
            return False

    def disconnect(self) -> None:
        """Close an idle client; active streams require ``await aclose()``."""
        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("direct ASR client close is already in progress")
            if self._sync_stream_active:
                raise RuntimeError("synchronous ASR iteration is active")
            if self._active_stream is not None and not self._active_stream.worker_done:
                raise RuntimeError("active ASR stream; use 'await client.aclose()'")
        self._close_idle_resources()

    async def aclose(self, timeout_s: float = 10.0) -> None:
        """Gracefully stop an active stream, channel, and owned executor."""
        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("direct ASR client close is already in progress")
            self._closing = True
            active_stream = self._active_stream
        try:
            if active_stream is not None and not active_stream.worker_done:
                await active_stream.aclose(timeout_s=timeout_s)

            self._abort_channel()
            with self._lifecycle_lock:
                self._connected = False
                self._service = None
                self._auth = None
                executor = self._executor if self._owns_executor else None
                if self._owns_executor:
                    self._executor = None
                self._active_stream = None
            if executor is not None:
                # The active worker has already completed, so this cannot block
                # on ASR I/O and avoids another cross-thread wakeup dependency.
                executor.shutdown(wait=True, cancel_futures=True)
        except BaseException:
            if active_stream is not None and not active_stream.worker_done:
                active_stream._request_cancel()
            self._abort_channel()
            with self._lifecycle_lock:
                executor = self._executor if self._owns_executor else None
                if self._owns_executor:
                    self._executor = None
                self._connected = False
                self._service = None
                self._auth = None
                self._active_stream = None
                self._closing = False
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            with self._lifecycle_lock:
                self._closing = False

    def is_connected(self) -> bool:
        return self._connected

    def create_config(self):
        return create_streaming_asr_config(
            enable_word_time_offsets=riva_config.asr_word_time_offsets,
        )

    def iter_transcripts(
        self,
        audio_chunks: Iterable[bytes],
    ) -> Iterator[ASRTranscript]:
        """Synchronously yield transcripts for the exclusive smoke-test path."""
        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("Direct ASR client is closing")
            if not self._connected or self._service is None:
                raise RuntimeError("Direct ASR client is not connected")
            if self._sync_stream_active or (
                self._active_stream is not None
                and not self._active_stream.worker_done
            ):
                raise RuntimeError("a direct ASR stream is already active")
            self._sync_stream_active = True
            service = self._service
        try:
            config = self.create_config()
            responses = service.streaming_response_generator(
                audio_chunks=audio_chunks,
                streaming_config=config,
            )
            yield from iter_transcript_results(responses)
        finally:
            with self._lifecycle_lock:
                self._sync_stream_active = False

    async def open_stream(self, event_queue_maxsize: int = 32) -> DirectASRStream:
        """Open one worker whose ordered output uses a bounded asyncio queue."""
        loop = asyncio.get_running_loop()
        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("Direct ASR client is closing")
            if not self._connected or self._service is None:
                raise RuntimeError("Direct ASR client is not connected")
            if self._sync_stream_active:
                raise RuntimeError("synchronous ASR iteration is active")
            if self._active_stream is not None and not self._active_stream.worker_done:
                raise RuntimeError("a direct ASR stream is already active")
            if self._executor is None:
                raise RuntimeError("Direct ASR executor is closed")
            service = self._service
            executor = self._executor
            config = self.create_config()
            stream = DirectASRStream(
                loop=loop,
                event_queue_maxsize=event_queue_maxsize,
                abort_rpc=self._abort_channel,
            )
            self._active_stream = stream
            try:
                worker = executor.submit(
                    self._run_stream,
                    service,
                    config,
                    stream,
                )
            except Exception:
                self._active_stream = None
                raise
            stream._attach_worker(worker)
            worker.add_done_callback(
                lambda completed: self._release_stream(stream, completed)
            )
        return stream

    @staticmethod
    def _run_stream(service, config, stream: DirectASRStream) -> None:
        final_id = 0
        try:
            responses = service.streaming_response_generator(
                audio_chunks=stream.audio_input,
                streaming_config=config,
            )
            for transcript in iter_transcript_results(responses):
                if transcript.is_final:
                    event = ASRStreamEvent(
                        kind=ASRStreamEventKind.FINAL,
                        final=AsrFinal.from_transcript(final_id, transcript),
                    )
                    final_id += 1
                else:
                    event = ASRStreamEvent(
                        kind=ASRStreamEventKind.INTERIM,
                        transcript=transcript,
                    )
                stream._put_from_worker(event)

            if not stream.audio_input._input_exhausted:
                raise RuntimeError(
                    "Direct ASR stream ended before all queued input was consumed"
                )
            stream._put_from_worker(
                ASRStreamEvent(kind=ASRStreamEventKind.COMPLETE)
            )
        except _StreamCancelled:
            return
        except Exception as exc:
            stream.audio_input.stop()
            try:
                stream._put_from_worker(
                    ASRStreamEvent(
                        kind=ASRStreamEventKind.ERROR,
                        error=f"Direct ASR stream failed: {exc}",
                    )
                )
            except _StreamCancelled:
                return

    def _release_stream(self, stream: DirectASRStream, completed: Future) -> None:
        stream._mark_worker_done(completed)
        with self._lifecycle_lock:
            if self._active_stream is stream:
                self._active_stream = None

    def _abort_channel(self) -> None:
        with self._lifecycle_lock:
            if self._channel_closed:
                return
            auth = self._auth
            self._channel_closed = True
            self._connected = False
        channel = getattr(auth, "channel", None)
        if channel is not None:
            channel.close()

    def _close_idle_resources(self) -> None:
        self._abort_channel()
        with self._lifecycle_lock:
            self._connected = False
            self._service = None
            self._auth = None
            executor = self._executor if self._owns_executor else None
            if self._owns_executor:
                self._executor = None
            self._active_stream = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


def iter_transcript_results(
    responses: Iterable[object],
    *,
    clock_ms: Optional[Callable[[], float]] = None,
) -> Iterator[ASRTranscript]:
    """Convert Riva response protobufs into downstream-safe typed records."""
    resolved_clock = clock_ms or (lambda: time.monotonic_ns() / 1_000_000)
    for response in responses:
        for result in getattr(response, "results", ()):
            alternatives = getattr(result, "alternatives", ())
            if not alternatives:
                continue
            alternative = alternatives[0]
            text = str(getattr(alternative, "transcript", "")).strip()
            if not text:
                continue

            words = tuple(getattr(alternative, "words", ()) or ())
            is_final = bool(getattr(result, "is_final", False))
            word_timing_shape_diagnostics = (
                _classify_word_timing_shapes(words)
                if is_final
                else None
            )
            audio_processed_s = float(getattr(result, "audio_processed", 0.0) or 0.0)
            source_start_ms = None
            source_end_ms = None
            first_word_start_ms = None
            last_word_end_ms = None
            timing_basis = ASR_TIMING_BASIS_UNAVAILABLE
            if words:
                first_word_start_ms = _optional_nonnegative_finite(
                    getattr(words[0], "start_time", None)
                )
                last_word_end_ms = _optional_nonnegative_finite(
                    getattr(words[-1], "end_time", None)
                )
                if (
                    first_word_start_ms is not None
                    and last_word_end_ms is not None
                    and last_word_end_ms > first_word_start_ms
                ):
                    source_start_ms = first_word_start_ms
                    source_end_ms = last_word_end_ms
                    timing_basis = ASR_TIMING_BASIS_WORD_OFFSETS
                else:
                    # Do not synthesize a start from audio_processed or a
                    # neighboring hypothesis. A partial envelope remains
                    # explicitly unusable for semantic source attribution.
                    if (
                        first_word_start_ms is not None
                        and last_word_end_ms is not None
                    ):
                        last_word_end_ms = None
                    source_end_ms = last_word_end_ms
                    if source_end_ms is None and audio_processed_s > 0:
                        source_end_ms = audio_processed_s * 1_000
                    timing_basis = ASR_TIMING_BASIS_INCOMPLETE_WORD_OFFSETS
            elif audio_processed_s > 0:
                source_end_ms = audio_processed_s * 1_000
                timing_basis = ASR_TIMING_BASIS_AUDIO_PROCESSED_END_ONLY

            languages = tuple(
                str(value)
                for value in (getattr(alternative, "language_code", ()) or ())
                if value
            )
            yield ASRTranscript(
                text=text,
                is_final=is_final,
                received_monotonic_ms=resolved_clock(),
                audio_processed_s=audio_processed_s,
                stability=float(getattr(result, "stability", 0.0) or 0.0),
                confidence=float(getattr(alternative, "confidence", 0.0) or 0.0),
                source_start_ms=source_start_ms,
                source_end_ms=source_end_ms,
                detected_languages=languages,
                word_count=len(words),
                first_word_start_ms=first_word_start_ms,
                last_word_end_ms=last_word_end_ms,
                timing_basis=timing_basis,
                word_timing_shape_diagnostics=(
                    word_timing_shape_diagnostics
                ),
            )


def _classify_word_timing_shapes(
    words: tuple[object, ...],
) -> ASRWordTimingShapeDiagnostics:
    entries = []
    entry_shapes: Counter[str] = Counter()
    start_numeric_classes: Counter[str] = Counter()
    end_numeric_classes: Counter[str] = Counter()
    start_presences: Counter[str] = Counter()
    end_presences: Counter[str] = Counter()
    for word_index, word in enumerate(words):
        start = _observe_word_boundary(word, "start_time")
        end = _observe_word_boundary(word, "end_time")
        numeric_relation, shape = _classify_word_timing_shape(start, end)
        entry = ASRWordTimingEntry(
            word_index=word_index,
            start=start,
            end=end,
            numeric_relation=numeric_relation,
            shape=shape,
        )
        entries.append(entry)
        entry_shapes[shape] += 1
        start_numeric_classes[start.numeric_class] += 1
        end_numeric_classes[end.numeric_class] += 1
        start_presences[start.presence] += 1
        end_presences[end.presence] += 1

    envelope = None
    if entries:
        envelope_start = entries[0].start
        envelope_end = entries[-1].end
        envelope_relation, envelope_shape = _classify_word_timing_shape(
            envelope_start,
            envelope_end,
        )
        envelope = ASRWordTimingEnvelope(
            start_word_index=0,
            end_word_index=len(entries) - 1,
            start=envelope_start,
            end=envelope_end,
            numeric_relation=envelope_relation,
            shape=envelope_shape,
            usable=envelope_shape == "valid",
        )

    return ASRWordTimingShapeDiagnostics(
        word_entry_count=len(entries),
        no_word_entries=not entries,
        envelope=envelope,
        entry_shape_counts=ASRWordTimingShapeCounts(
            **{
                shape: entry_shapes[shape]
                for shape in ASR_WORD_TIMING_SHAPES
            }
        ),
        start_numeric_class_counts=ASRBoundaryNumericClassCounts(
            **{
                numeric_class: start_numeric_classes[numeric_class]
                for numeric_class in ASR_BOUNDARY_NUMERIC_CLASSES
            }
        ),
        end_numeric_class_counts=ASRBoundaryNumericClassCounts(
            **{
                numeric_class: end_numeric_classes[numeric_class]
                for numeric_class in ASR_BOUNDARY_NUMERIC_CLASSES
            }
        ),
        start_presence_counts=ASRBoundaryPresenceCounts(
            **{
                presence: start_presences[presence]
                for presence in ASR_BOUNDARY_PRESENCES
            }
        ),
        end_presence_counts=ASRBoundaryPresenceCounts(
            **{
                presence: end_presences[presence]
                for presence in ASR_BOUNDARY_PRESENCES
            }
        ),
        anomalies=tuple(entry for entry in entries if entry.shape != "valid"),
    )


def _observe_word_boundary(
    word: object,
    field_name: str,
) -> ASRBoundaryObservation:
    presence = _word_boundary_presence(word, field_name)
    if presence == ASR_BOUNDARY_PRESENCE_ABSENT:
        return ASRBoundaryObservation(
            presence=presence,
            numeric_class="not_available",
        )
    sentinel = object()
    try:
        value = getattr(word, field_name, sentinel)
    except Exception:
        value = sentinel
    if value is sentinel or value is None:
        numeric_class = (
            "not_available"
            if value is sentinel
            else "unparseable"
        )
        return ASRBoundaryObservation(
            presence=presence,
            numeric_class=numeric_class,
        )
    if isinstance(value, bool):
        return ASRBoundaryObservation(
            presence=presence,
            numeric_class="unparseable",
        )
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return ASRBoundaryObservation(
            presence=presence,
            numeric_class="unparseable",
        )
    if not math.isfinite(converted):
        return ASRBoundaryObservation(
            presence=presence,
            numeric_class="nonfinite",
        )
    if converted < 0:
        numeric_class = "negative"
    elif converted == 0:
        numeric_class = "zero"
    else:
        numeric_class = "positive"
    return ASRBoundaryObservation(
        presence=presence,
        numeric_class=numeric_class,
        finite_value_ms=converted,
    )


def _word_boundary_presence(word: object, field_name: str) -> str:
    descriptor = getattr(word, "DESCRIPTOR", None)
    fields_by_name = getattr(descriptor, "fields_by_name", {})
    field = (
        fields_by_name.get(field_name)
        if hasattr(fields_by_name, "get")
        else None
    )
    if field is not None:
        if getattr(field, "has_presence", False) is False:
            return ASR_BOUNDARY_PRESENCE_UNOBSERVABLE
        has_field = getattr(word, "HasField", None)
        if callable(has_field):
            try:
                return (
                    ASR_BOUNDARY_PRESENCE_PRESENT
                    if has_field(field_name)
                    else ASR_BOUNDARY_PRESENCE_ABSENT
                )
            except (TypeError, ValueError):
                return ASR_BOUNDARY_PRESENCE_UNOBSERVABLE
    try:
        value = getattr(word, field_name)
    except (AttributeError, TypeError, ValueError):
        return ASR_BOUNDARY_PRESENCE_ABSENT
    return (
        ASR_BOUNDARY_PRESENCE_ABSENT
        if value is None
        else ASR_BOUNDARY_PRESENCE_PRESENT
    )


def _classify_word_timing_shape(
    start: ASRBoundaryObservation,
    end: ASRBoundaryObservation,
) -> tuple[str, str]:
    numeric_classes = {start.numeric_class, end.numeric_class}
    if "not_available" in numeric_classes:
        return "not_comparable", "absent_boundary"
    if "unparseable" in numeric_classes:
        return "not_comparable", "unparseable_boundary"
    if "nonfinite" in numeric_classes:
        return "not_comparable", "nonfinite_boundary"

    start_value = start.finite_value_ms
    end_value = end.finite_value_ms
    if start_value is None or end_value is None:
        return "not_comparable", "unparseable_boundary"
    if end_value > start_value:
        relation = "end_after_start"
    elif end_value == start_value:
        relation = "equal"
    else:
        relation = "end_before_start"
    if "negative" in numeric_classes:
        return relation, "negative_boundary"
    if relation == "equal":
        return relation, "zero_length"
    if relation == "end_before_start":
        return relation, "reversed"
    return relation, "valid"


def _optional_nonnegative_finite(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(converted) or converted < 0:
        return None
    return converted


def _new_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="direct-asr")


async def _await_worker(worker: Future, timeout_s: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not worker.done():
        if loop.time() >= deadline:
            raise asyncio.TimeoutError()
        await asyncio.sleep(0.01)
    worker.result()


# Separate global instance for future staged orchestration. It is intentionally
# not wired into the current monolithic WebSocket session yet.
direct_asr_client = DirectASRClient()
