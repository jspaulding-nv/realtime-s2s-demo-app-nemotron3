"""WebSocket session management for translation streams."""

import asyncio
import copy
import os
import time
from concurrent.futures import wait
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from config import SUPPORTED_LANGUAGES, staged_pipeline_config
from riva_client import riva_client, AudioChunkIterator
from audio_processor import calculate_rms
from staged_models import StagedOutputEventKind
from timing_logger import timing_logger


VERBOSE_CHUNKS = os.getenv("RIVA_VERBOSE_CHUNKS", "0") == "1"


def create_staged_pipeline(target_language: str):
    """Build one isolated direct-ASR/NMT/TTS pipeline for a WebSocket stream.

    Imports stay lazy so the default monolithic path does not construct direct
    model clients or their executors.  A fresh set of clients is required for
    every stream because cancellation can close their retained gRPC channels.
    """
    if target_language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported target language: {target_language}")

    from direct_asr_client import DirectASRClient
    from direct_nmt_client import DirectNMTClient
    from direct_tts_client import DirectTTSClient
    from staged_pipeline import StagedPipelineSession

    return StagedPipelineSession(
        asr_client=DirectASRClient(),
        nmt_client=DirectNMTClient(
            rpc_timeout_s=staged_pipeline_config.nmt_rpc_timeout_s,
        ),
        tts_client=DirectTTSClient(
            max_audio_duration_s=staged_pipeline_config.tts_max_segment_audio_s,
        ),
        target_language=target_language,
        config=staged_pipeline_config,
        owns_clients=True,
    )


class SessionStatus(str, Enum):
    """Session status states."""
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    LISTENING = "listening"
    PROCESSING = "processing"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class TranslationSession:
    """Manages a single translation session."""
    websocket: WebSocket
    status: SessionStatus = SessionStatus.CONNECTED
    target_language: str = "es-US"
    chunk_iterator: Optional[AudioChunkIterator] = None
    pipeline_mode: str = "monolithic"
    staged_pipeline_factory: Optional[Callable[[str], Any]] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False
    _awaiting_completion: bool = False
    _staged_pipeline: Any = None
    _staged_output_task: Optional[asyncio.Task] = None
    _staged_cleanup_pipeline: Any = None
    _staged_cleanup_task: Optional[asyncio.Task] = None
    _staged_generation: int = 0
    _staged_terminal_generation: int = -1
    _staged_audio_sequence_ids_sent: list[int] = field(default_factory=list)
    _staged_websocket_send_events: list[dict] = field(default_factory=list)
    _last_staged_summary: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.pipeline_mode not in {"monolithic", "staged"}:
            raise ValueError("pipeline_mode must be either 'monolithic' or 'staged'")

    @property
    def uses_staged_pipeline(self) -> bool:
        return self.pipeline_mode == "staged"

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
        async with self._send_lock:
            await self._send_json_unlocked({
                "type": "status",
                "status": status.value,
                "message": message
            })

    async def send_error(self, message: str) -> None:
        """Send error message to client."""
        self.status = SessionStatus.ERROR
        async with self._send_lock:
            await self._send_json_unlocked({
                "type": "error",
                "message": message
            })

    async def send_audio(self, audio_data: bytes) -> bool:
        """Send translated audio to client."""
        async with self._send_lock:
            return await self._send_audio_unlocked(audio_data)

    async def send_level(self, rms: float) -> None:
        """Send audio level to client."""
        async with self._send_lock:
            await self._send_json_unlocked({
                "type": "level",
                "rms": rms
            })

    async def send_pong(self) -> None:
        """Send a ping response through the same serialized writer."""
        if self.uses_staged_pipeline:
            await self._send_staged_json(
                self._staged_generation,
                {"type": "pong"},
            )
            return
        async with self._send_lock:
            await self._send_json_unlocked({"type": "pong"})

    async def _send_json_unlocked(self, payload: dict) -> bool:
        """Write JSON while the caller owns ``_send_lock``."""
        if not self._is_websocket_open():
            return False
        try:
            await self.websocket.send_json(payload)
            return True
        except Exception:
            return False

    async def _send_audio_unlocked(self, audio_data: bytes) -> bool:
        """Write PCM while the caller owns ``_send_lock``."""
        if not self._is_websocket_open():
            return False
        try:
            if VERBOSE_CHUNKS:
                print(f"[WS] Sending {len(audio_data)} bytes of audio to client")
            await self.websocket.send_bytes(audio_data)
            return True
        except Exception as exc:
            print(f"[WS] Failed to send audio: {exc}")
            return False

    async def start_stream(self, target_language: str) -> None:
        """Start a new translation stream."""
        staged_start = None
        async with self._lock:
            print(f"[WS] start_stream called: target={target_language}, current_status={self.status}")
            if self._closed:
                print("[WS] Ignoring start_stream for a closed session")
                return
            self._awaiting_completion = False

            # Stop any existing stream first
            if self.chunk_iterator:
                print("[WS] Stopping existing stream before starting new one")
                self._awaiting_completion = False
                self.chunk_iterator.stop()
                self.chunk_iterator = None

            if (
                self._staged_pipeline is not None
                or self._staged_output_task is not None
            ):
                print("[WS] Stopping existing staged stream before starting new one")
                self._awaiting_completion = False
                await self._shutdown_staged_pipeline()

            self.target_language = target_language

            if self.uses_staged_pipeline:
                staged_start = await self._start_staged_pipeline(target_language)
            else:
                await self.send_status(
                    SessionStatus.LISTENING,
                    f"Translating to {target_language}",
                )

                # Capture the event loop for thread-safe callbacks
                loop = asyncio.get_running_loop()

                # Create callback to send audio back through WebSocket
                # These run in a background thread, so use run_coroutine_threadsafe
                pending_audio_sends = []

                def on_audio(audio_bytes: bytes):
                    if not self._closed:
                        timing_logger.log_audio_from_riva(len(audio_bytes))

                        async def _send_and_log():
                            if not await self.send_audio(audio_bytes):
                                raise RuntimeError(
                                    "translated audio could not be sent to the client"
                                )
                            timing_logger.log_audio_sent_to_client(len(audio_bytes))

                        pending_audio_sends.append(
                            asyncio.run_coroutine_threadsafe(_send_and_log(), loop)
                        )

                def on_error(error_msg: str):
                    if not self._closed:
                        asyncio.run_coroutine_threadsafe(self.send_error(error_msg), loop)

                def on_complete():
                    if self._closed or not self._awaiting_completion:
                        return

                    done, not_done = wait(pending_audio_sends, timeout=60)
                    if not_done:
                        on_error(
                            "Timed out sending final translated audio to the client"
                        )
                        return
                    failed = []
                    for future in done:
                        try:
                            exception = future.exception()
                        except BaseException as exc:
                            exception = exc
                        if exception is not None:
                            failed.append(exception)
                    if failed:
                        on_error(f"Failed to send final translated audio: {failed[0]}")
                        return

                    async def _mark_completed():
                        if self._closed or not self._awaiting_completion:
                            return
                        self._awaiting_completion = False
                        await self.send_status(
                            SessionStatus.COMPLETED,
                            "Riva translated-audio stream complete",
                        )

                    asyncio.run_coroutine_threadsafe(_mark_completed(), loop)

                try:
                    self.chunk_iterator = await riva_client.translate_stream(
                        target_language=target_language,
                        on_audio=on_audio,
                        on_error=on_error,
                        on_complete=on_complete,
                    )
                except Exception as e:
                    await self.send_error(f"Failed to start stream: {str(e)}")

        if staged_start is None:
            return
        pipeline, generation, start_error = staged_start
        if start_error:
            await self._send_staged_error(
                generation,
                f"Failed to start stream: {start_error}",
            )
            return

        listening_sent = await self._send_staged_json(
            generation,
            {
                "type": "status",
                "status": SessionStatus.LISTENING.value,
                "message": f"Translating to {target_language}",
            },
            status=SessionStatus.LISTENING,
        )
        if not listening_sent:
            return
        async with self._lock:
            if (
                not self._closed
                and generation == self._staged_generation
                and self._staged_pipeline is pipeline
                and self._staged_terminal_generation != generation
            ):
                self._staged_output_task = asyncio.create_task(
                    self._relay_staged_outputs(pipeline, generation),
                    name="staged-websocket-output",
                )

    async def _start_staged_pipeline(
        self, target_language: str
    ) -> tuple[Any, int, str]:
        """Start direct clients while the caller owns the lifecycle lock."""
        factory = self.staged_pipeline_factory or create_staged_pipeline
        pipeline = None
        self._staged_generation += 1
        generation = self._staged_generation
        self._staged_audio_sequence_ids_sent = []
        self._staged_websocket_send_events = []
        try:
            pipeline = factory(target_language)
            self._staged_pipeline = pipeline
            await pipeline.start()
            return pipeline, generation, ""
        except Exception as exc:
            if pipeline is not None:
                try:
                    await self._await_staged_pipeline_cleanup(pipeline)
                except Exception as cleanup_exc:
                    print(f"[WS] Failed to clean up staged stream: {cleanup_exc}")
                self._retain_staged_summary(pipeline)
            if self._staged_pipeline is pipeline:
                self._staged_pipeline = None
            return pipeline, generation, str(exc) or type(exc).__name__

    async def _relay_staged_outputs(self, pipeline: Any, generation: int) -> None:
        """Forward staged output in FIFO order and map its terminal contract."""
        pipeline_closed = False
        try:
            while True:
                output = await pipeline.next_output()
                if generation != self._staged_generation:
                    return
                if output.kind is StagedOutputEventKind.AUDIO:
                    audio = output.segment.audio
                    timing_logger.log_audio_from_riva(len(audio))
                    sent = await self._send_staged_audio(
                        generation,
                        audio,
                        output.segment.sequence_id,
                    )
                    if sent is None:
                        return
                    if not sent:
                        await self._send_staged_error(
                            generation,
                            "translated audio could not be sent to the client",
                        )
                        return
                    continue

                if output.kind is StagedOutputEventKind.ERROR:
                    await self._send_staged_error(
                        generation,
                        f"Staged {output.stage} failed: {output.error}",
                    )
                    return

                if output.kind is not StagedOutputEventKind.COMPLETE:
                    raise RuntimeError(
                        f"unknown staged output event kind: {output.kind}"
                    )

                cleanup_error, pipeline_closed = (
                    await self._close_completed_staged_pipeline(pipeline)
                )
                if cleanup_error:
                    await self._send_staged_error(
                        generation,
                        f"Staged cleanup failed: {cleanup_error}",
                    )
                    return
                await self._send_staged_completed(generation)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                await self._send_staged_error(
                    generation,
                    f"Staged output failed: {exc}",
                )
        finally:
            if not pipeline_closed:
                try:
                    await self._await_staged_pipeline_cleanup(pipeline)
                except Exception as exc:
                    print(f"[WS] Failed to close staged stream: {exc}")
                self._retain_staged_summary(pipeline)
            if self._staged_pipeline is pipeline:
                self._staged_pipeline = None
            if self._staged_output_task is asyncio.current_task():
                self._staged_output_task = None

    async def _close_completed_staged_pipeline(
        self, pipeline: Any
    ) -> tuple[str, bool]:
        """Close a successful pipeline and surface cleanup failure as terminal."""
        try:
            await self._await_staged_pipeline_cleanup(pipeline)
        except Exception as exc:
            self._retain_staged_summary(pipeline)
            return str(exc) or type(exc).__name__, False

        snapshot = self._retain_staged_summary(pipeline)
        if snapshot is None:
            return "pipeline summary unavailable after cleanup", True
        cleanup_errors = snapshot.get("cleanup_errors", [])
        if cleanup_errors:
            first = cleanup_errors[0]
            if isinstance(first, dict):
                return (
                    str(first.get("error") or first.get("code") or first),
                    True,
                )
            return str(first), True

        outcome = snapshot.get("outcome")
        if outcome not in {None, "complete"}:
            return f"pipeline outcome was {outcome}", True
        incomplete = snapshot.get("incomplete_sequence_ids", [])
        if incomplete:
            return f"incomplete sequence IDs: {incomplete}", True
        completed = snapshot.get("completed_sequence_ids")
        if completed is not None and list(completed) != self._staged_audio_sequence_ids_sent:
            return (
                (
                    "WebSocket-sent sequence IDs did not match dequeued pipeline output: "
                    f"sent={self._staged_audio_sequence_ids_sent}, "
                    f"dequeued={list(completed)}"
                ),
                True,
            )
        return "", True

    def _ensure_staged_cleanup_task(self, pipeline: Any) -> asyncio.Task:
        """Return one independently owned cleanup task for ``pipeline``.

        Relay cancellation must never cancel the cleanup operation itself. A
        dedicated task also lets explicit stop/replacement await the exact same
        close rather than racing a second ``aclose`` call.
        """
        if (
            self._staged_cleanup_pipeline is pipeline
            and self._staged_cleanup_task is not None
        ):
            return self._staged_cleanup_task
        self._staged_cleanup_pipeline = pipeline
        self._staged_cleanup_task = asyncio.create_task(
            pipeline.aclose(),
            name="staged-pipeline-cleanup",
        )
        return self._staged_cleanup_task

    async def _await_staged_pipeline_cleanup(self, pipeline: Any) -> None:
        """Await deterministic cleanup without donating caller cancellation."""
        cleanup = self._ensure_staged_cleanup_task(pipeline)
        await asyncio.shield(cleanup)

    async def _send_staged_audio(
        self,
        generation: int,
        audio: bytes,
        sequence_id: int,
    ) -> Optional[bool]:
        """Send current-generation PCM before any terminal message.

        The writer lock is acquired before the short lifecycle check. This
        preserves PCM/terminal order without holding the lifecycle lock across
        socket backpressure. ``None`` means a newer generation or a terminal
        owner already won; ``False`` means the socket write failed.
        """
        async with self._send_lock:
            async with self._lock:
                if (
                    generation != self._staged_generation
                    or self._staged_terminal_generation == generation
                    or self._closed
                ):
                    return None
            if not await self._send_audio_unlocked(audio):
                return False
            timing_logger.log_audio_sent_to_client(len(audio))
            self._staged_audio_sequence_ids_sent.append(sequence_id)
            self._staged_websocket_send_events.append(
                {
                    "sequence_id": sequence_id,
                    "sent_monotonic_ms": time.monotonic_ns() / 1_000_000,
                    "audio_bytes": len(audio),
                }
            )
            return True

    async def _send_staged_json(
        self,
        generation: int,
        payload: dict,
        *,
        status: Optional[SessionStatus] = None,
        terminal: bool = False,
        require_awaiting_completion: bool = False,
    ) -> bool:
        """Serialize a generation-aware JSON write without lock-held I/O."""
        async with self._send_lock:
            async with self._lock:
                if (
                    generation != self._staged_generation
                    or self._closed
                    or self._staged_terminal_generation == generation
                ):
                    return False
                if require_awaiting_completion and not self._awaiting_completion:
                    return False
                if terminal:
                    self._staged_terminal_generation = generation
                    self._awaiting_completion = False
                if status is not None:
                    self.status = status
            # Eligibility/terminal ownership, rather than socket health, is the
            # return contract. Cleanup must still run after a disconnected peer.
            await self._send_json_unlocked(payload)
            return True

    async def _send_staged_terminal(
        self,
        generation: int,
        status: SessionStatus,
        message: str,
    ) -> bool:
        """Claim and emit exactly one terminal outcome for one generation."""
        if status is SessionStatus.ERROR:
            payload = {"type": "error", "message": message}
        else:
            payload = {
                "type": "status",
                "status": status.value,
                "message": message,
            }
        return await self._send_staged_json(
            generation,
            payload,
            status=status,
            terminal=True,
        )

    async def _send_staged_completed(self, generation: int) -> bool:
        return await self._send_staged_json(
            generation,
            {
                "type": "status",
                "status": SessionStatus.COMPLETED.value,
                "message": "Riva translated-audio stream complete",
            },
            status=SessionStatus.COMPLETED,
            terminal=True,
            require_awaiting_completion=True,
        )

    async def _send_staged_error(self, generation: int, message: str) -> bool:
        """Emit at most the current generation's error under lifecycle order."""
        return await self._send_staged_terminal(
            generation,
            SessionStatus.ERROR,
            message,
        )

    async def send_runtime_error(self, message: str) -> None:
        """Map an endpoint exception without duplicating a staged terminal."""
        if self.uses_staged_pipeline:
            await self._send_staged_error(self._staged_generation, message)
            return
        await self.send_error(message)

    async def send_protocol_error(self, message: str) -> None:
        """Terminate the active staged generation for an invalid control frame."""
        if not self.uses_staged_pipeline:
            await self.send_error(message)
            return
        generation = self._staged_generation
        claimed = await self._send_staged_error(generation, message)
        if not claimed:
            return
        async with self._lock:
            if generation == self._staged_generation:
                await self._shutdown_staged_pipeline(preserve_terminal=True)

    async def _resolve_staged_input_failure(
        self,
        pipeline: Any,
        generation: int,
        exc: Exception,
        operation: str,
    ) -> None:
        """Let a failed pipeline relay its structured terminal before fallback.

        A worker records ``failure``/``FAILED`` just before its ERROR reaches
        the output queue.  ``add_audio`` or ``finish_input`` can observe that
        intermediate state and raise.  Awaiting the existing relay here keeps
        the endpoint loop alive, preserves queued PCM ordering, and prevents
        its generic exception handler from cancelling the structured error.
        """
        if generation != self._staged_generation or self._closed:
            return

        failure = getattr(pipeline, "failure", None)
        state = getattr(pipeline, "state", None)
        state_value = getattr(state, "value", state)
        relay = self._staged_output_task
        structured_terminal_expected = (
            failure is not None
            or state_value in {"failed", "complete", "closed"}
        )

        if (
            structured_terminal_expected
            and relay is not None
            and relay is not asyncio.current_task()
            and not relay.done()
        ):
            try:
                await asyncio.shield(relay)
            except asyncio.CancelledError:
                # Propagate cancellation of this request task, but not
                # cancellation of a stale relay during stream replacement.
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                # The relay normally translates its own exceptions into a
                # terminal error.  The latch below is the final safety net.
                pass

        if (
            generation != self._staged_generation
            or self._closed
            or self._staged_terminal_generation == generation
        ):
            return

        sent = await self._send_staged_error(
            generation,
            f"Staged {operation} failed: {exc}",
        )
        if not sent:
            return

        # No pipeline terminal was available, so abort this failed generation.
        # The terminal latch prevents relay PCM or another terminal afterward.
        async with self._lock:
            if generation == self._staged_generation:
                await self._shutdown_staged_pipeline(preserve_terminal=True)

    def _retain_staged_summary(self, pipeline: Any) -> Optional[dict]:
        summary = getattr(pipeline, "summary", None)
        if not callable(summary):
            return None
        try:
            try:
                snapshot = summary(include_events=True)
            except TypeError:
                snapshot = summary()
            snapshot = copy.deepcopy(snapshot)
        except Exception as exc:
            print(f"[WS] Failed to capture staged telemetry: {exc}")
            return None
        snapshot["websocket_sent_sequence_ids"] = list(
            self._staged_audio_sequence_ids_sent
        )
        snapshot["websocket_send_events"] = copy.deepcopy(
            self._staged_websocket_send_events
        )
        self._last_staged_summary = snapshot
        return snapshot

    def staged_telemetry_snapshot(self) -> Optional[dict]:
        """Return active-or-last staged telemetry without exposing mutable state."""
        if self._staged_pipeline is not None:
            snapshot = self._retain_staged_summary(self._staged_pipeline)
        else:
            snapshot = self._last_staged_summary
        return copy.deepcopy(snapshot) if snapshot is not None else None

    def clear_staged_telemetry(self) -> bool:
        """Clear retained evidence unless a staged stream is still active."""
        if self._staged_pipeline is not None or self._staged_output_task is not None:
            return False
        self._last_staged_summary = None
        self._staged_audio_sequence_ids_sent = []
        self._staged_websocket_send_events = []
        return True

    async def _shutdown_staged_pipeline(
        self, *, preserve_terminal: bool = False
    ) -> None:
        prior_generation = self._staged_generation
        terminal_was_claimed = (
            self._staged_terminal_generation == prior_generation
        )
        self._staged_generation += 1
        if preserve_terminal and terminal_was_claimed:
            self._staged_terminal_generation = self._staged_generation
        pipeline = self._staged_pipeline
        output_task = self._staged_output_task
        self._staged_pipeline = None
        self._staged_output_task = None

        current_task = asyncio.current_task()
        if output_task is not None and output_task is not current_task:
            output_task.cancel()
        if pipeline is not None:
            try:
                await self._await_staged_pipeline_cleanup(pipeline)
            except Exception as exc:
                print(f"[WS] Failed to close staged stream: {exc}")
            self._retain_staged_summary(pipeline)
        if output_task is not None and output_task is not current_task:
            await asyncio.gather(output_task, return_exceptions=True)

    async def stop_stream(self) -> None:
        """Stop the current translation stream and notify client."""
        staged_stop_generation = None
        send_monolithic_stopped = False
        async with self._lock:
            print(f"[WS] stop_stream called, current_status={self.status}")
            if self._closed:
                return
            self._awaiting_completion = False
            if self.chunk_iterator:
                self.chunk_iterator.stop()
                self.chunk_iterator = None
            if self.uses_staged_pipeline:
                if (
                    self._staged_pipeline is not None
                    or self._staged_output_task is not None
                ):
                    await self._shutdown_staged_pipeline()
                else:
                    # A control-level stop still owns one fresh terminal
                    # generation, even when no model stream is active.
                    self._staged_generation += 1
                staged_stop_generation = self._staged_generation
            else:
                send_monolithic_stopped = True

        if staged_stop_generation is not None:
            await self._send_staged_terminal(
                staged_stop_generation,
                SessionStatus.STOPPED,
                "Stream stopped",
            )
        elif send_monolithic_stopped:
            await self.send_status(SessionStatus.STOPPED, "Stream stopped")

    async def finish_input(self) -> None:
        """Close the Riva request stream while keeping the WebSocket open.

        Riva can then flush the final ASR/NMT/TTS responses back to the client,
        which is required to measure true tail lag for file-based tests.
        """
        staged_failure = None
        staged_processing_generation = None
        monolithic_processing = False
        async with self._lock:
            print(f"[WS] finish_input called, current_status={self.status}")
            if self._awaiting_completion or self.status is not SessionStatus.LISTENING:
                return
            if self.uses_staged_pipeline:
                pipeline = self._staged_pipeline
                generation = self._staged_generation
                if pipeline is None:
                    return
                try:
                    pipeline.finish_input()
                except Exception as exc:
                    staged_failure = (pipeline, generation, exc)
                else:
                    self._awaiting_completion = True
                    staged_processing_generation = generation
            elif self.chunk_iterator:
                self._awaiting_completion = True
                self.chunk_iterator.stop()
                monolithic_processing = True

        if staged_failure is not None:
            pipeline, generation, exc = staged_failure
            await self._resolve_staged_input_failure(
                pipeline,
                generation,
                exc,
                "input finalization",
            )
        elif staged_processing_generation is not None:
            await self._send_staged_json(
                staged_processing_generation,
                {
                    "type": "status",
                    "status": SessionStatus.PROCESSING.value,
                    "message": "Input complete; draining translated audio",
                },
                status=SessionStatus.PROCESSING,
            )
        elif monolithic_processing:
            await self.send_status(
                SessionStatus.PROCESSING,
                "Input complete; draining translated audio",
            )

    def close(self) -> None:
        """Best-effort legacy close; manager cleanup uses awaited ``aclose``."""
        self._closed = True
        self._awaiting_completion = False
        if self.chunk_iterator:
            self.chunk_iterator.stop()
            self.chunk_iterator = None
        pipeline = self._staged_pipeline
        output_task = self._staged_output_task
        if output_task is not None:
            output_task.cancel()
        if pipeline is not None or output_task is not None:
            self._staged_generation += 1
        if pipeline is not None:
            try:
                self._ensure_staged_cleanup_task(pipeline)
            except RuntimeError:
                pass
        self._staged_pipeline = None
        self._staged_output_task = None

    async def aclose(self) -> None:
        """Close all per-session resources before the manager forgets them."""
        async with self._lock:
            self._closed = True
            self._awaiting_completion = False
            if self.chunk_iterator:
                self.chunk_iterator.stop()
                self.chunk_iterator = None
            await self._shutdown_staged_pipeline()

    async def process_audio(self, audio_bytes: bytes) -> None:
        """Process incoming audio chunk from client."""
        if self.uses_staged_pipeline:
            await self._process_staged_audio(audio_bytes)
            return

        input_ready = (
            self.chunk_iterator is not None
        )
        if self.status != SessionStatus.LISTENING or not input_ready:
            print(f"[WS] Ignoring audio: status={self.status}, has_input={input_ready}")
            return

        # Audio is already Int16 from the browser (converted in AudioWorklet)
        # Just pass it through to Riva
        if VERBOSE_CHUNKS:
            print(f"[WS] Received {len(audio_bytes)} bytes (Int16) from client")

        # Timing instrumentation
        chunk_idx = timing_logger.log_audio_received(len(audio_bytes))

        # Calculate RMS for visualization
        rms = calculate_rms(audio_bytes, dtype="int16")

        # Send to Riva (already Int16 format)
        timing_logger.log_audio_to_riva(chunk_idx, len(audio_bytes))
        self.chunk_iterator.add_chunk(audio_bytes)

        # Send RMS level back to client for visualization
        await self.send_level(rms)

    async def _process_staged_audio(self, audio_bytes: bytes) -> None:
        """Submit staged input without allowing worker-failure races to escape."""
        staged_failure = None
        level_update = None
        async with self._lock:
            pipeline = self._staged_pipeline
            generation = self._staged_generation
            input_ready = pipeline is not None
            if self.status is not SessionStatus.LISTENING or not input_ready:
                print(
                    f"[WS] Ignoring audio: status={self.status}, "
                    f"has_input={input_ready}"
                )
                return

            if VERBOSE_CHUNKS:
                print(
                    f"[WS] Received {len(audio_bytes)} bytes "
                    "(Int16) from client"
                )

            chunk_idx = timing_logger.log_audio_received(len(audio_bytes))
            rms = calculate_rms(audio_bytes, dtype="int16")
            timing_logger.log_audio_to_riva(chunk_idx, len(audio_bytes))
            try:
                pipeline.add_audio(audio_bytes)
            except Exception as exc:
                staged_failure = (pipeline, generation, exc)
            else:
                level_update = (generation, rms)

        if staged_failure is not None:
            pipeline, generation, exc = staged_failure
            await self._resolve_staged_input_failure(
                pipeline,
                generation,
                exc,
                "audio input",
            )
        elif level_update is not None:
            generation, rms = level_update
            await self._send_staged_json(
                generation,
                {"type": "level", "rms": rms},
            )


class SessionManager:
    """Manages active translation sessions (single user mode)."""

    def __init__(
        self,
        *,
        pipeline_mode: Optional[str] = None,
        staged_pipeline_factory: Optional[Callable[[str], Any]] = None,
    ):
        if pipeline_mode is not None and pipeline_mode not in {"monolithic", "staged"}:
            raise ValueError("pipeline_mode must be either 'monolithic' or 'staged'")
        self._active_session: Optional[TranslationSession] = None
        self._lock = asyncio.Lock()
        self._pipeline_mode = pipeline_mode
        self._staged_pipeline_factory = staged_pipeline_factory
        self._last_staged_summary: Optional[dict] = None

    async def create_session(self, websocket: WebSocket) -> Optional[TranslationSession]:
        """Create a new session, closing any existing one."""
        async with self._lock:
            # Close existing session if any (don't try to send messages)
            if self._active_session:
                previous = self._active_session
                await previous.aclose()
                snapshot = previous.staged_telemetry_snapshot()
                if snapshot is not None:
                    self._last_staged_summary = snapshot
                self._active_session = None

            pipeline_mode = self._pipeline_mode or staged_pipeline_config.pipeline_mode

            # The staged path owns three direct model clients per stream.  The
            # monolithic connection is therefore neither needed nor consulted.
            if pipeline_mode == "monolithic":
                if not riva_client.is_connected():
                    if not riva_client.connect():
                        return None

            session = TranslationSession(
                websocket=websocket,
                pipeline_mode=pipeline_mode,
                staged_pipeline_factory=self._staged_pipeline_factory,
            )
            self._active_session = session
            return session

    async def remove_session(self, session: TranslationSession) -> None:
        """Remove a session."""
        async with self._lock:
            if self._active_session == session:
                await session.aclose()
                snapshot = session.staged_telemetry_snapshot()
                if snapshot is not None:
                    self._last_staged_summary = snapshot
                self._active_session = None

    def get_active_session(self) -> Optional[TranslationSession]:
        """Get the currently active session."""
        return self._active_session

    def get_staged_telemetry(self) -> Optional[dict]:
        """Return retained staged telemetry for diagnostics/export."""
        if self._active_session is not None:
            snapshot = self._active_session.staged_telemetry_snapshot()
            if snapshot is not None:
                return snapshot
        return copy.deepcopy(self._last_staged_summary)

    def clear_staged_telemetry(self) -> bool:
        """Start a clean evidence window, rejecting an in-flight staged run."""
        if (
            self._active_session is not None
            and not self._active_session.clear_staged_telemetry()
        ):
            return False
        self._last_staged_summary = None
        return True


# Global session manager
session_manager = SessionManager()
