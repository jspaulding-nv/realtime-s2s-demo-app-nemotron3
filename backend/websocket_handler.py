"""WebSocket session management for translation streams."""

import asyncio
import copy
import math
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
AUDIO_METADATA_PROTOCOL_VERSION = 1
_AUDIO_METADATA_PROTOCOL_UNSET = object()


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
            max_retries=staged_pipeline_config.tts_max_retries,
            capture_response_chunk_metrics=(
                staged_pipeline_config.tts_response_chunk_telemetry_enabled
            ),
            incremental_frame_ms=(
                staged_pipeline_config.tts_incremental_frame_ms
            ),
            incremental_atomic_fallback_max_chars=(
                staged_pipeline_config.tts_incremental_atomic_fallback_max_chars
            ),
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
    _audio_metadata_protocol_version: Optional[int] = None
    _staged_audio_sequence_ids_sent: list[int] = field(default_factory=list)
    _staged_audio_subsegment_keys_sent: list[tuple[int, int, int]] = field(
        default_factory=list
    )
    _staged_audio_frame_keys_sent: list[tuple[int, int]] = field(
        default_factory=list
    )
    _staged_audio_frame_bytes_sent: list[int] = field(default_factory=list)
    _staged_parent_completions_sent: list[dict] = field(default_factory=list)
    _staged_pending_frame_parent: Optional[int] = None
    _staged_pending_frame_count: int = 0
    _staged_pending_frame_bytes: int = 0
    _staged_pending_frame_sample_rate_hz: Optional[int] = None
    _staged_pending_frame_channels: Optional[int] = None
    _staged_pending_frame_bytes_per_sample: Optional[int] = None
    _staged_pending_frame_source_start_ms: Optional[float] = None
    _staged_pending_frame_source_end_ms: Optional[float] = None
    _staged_stream_sample_rate_hz: Optional[int] = None
    _staged_stream_channels: Optional[int] = None
    _staged_stream_bytes_per_sample: Optional[int] = None
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

    async def start_stream(
        self,
        target_language: str,
        *,
        audio_metadata_protocol_version: Any = (
            _AUDIO_METADATA_PROTOCOL_UNSET
        ),
    ) -> None:
        """Start a new translation stream."""
        try:
            negotiated_metadata_version = (
                self._validate_audio_metadata_protocol_request(
                    audio_metadata_protocol_version
                )
            )
        except ValueError as exc:
            await self.send_protocol_error(str(exc))
            return

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
                staged_start = await self._start_staged_pipeline(
                    target_language,
                    negotiated_metadata_version,
                )
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
            if generation == self._staged_generation:
                self._audio_metadata_protocol_version = None
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

    def _validate_audio_metadata_protocol_request(
        self,
        requested_version: Any,
    ) -> Optional[int]:
        """Negotiate the opt-in observation metadata protocol fail-closed."""
        if requested_version is _AUDIO_METADATA_PROTOCOL_UNSET:
            return None
        if (
            not isinstance(requested_version, int)
            or isinstance(requested_version, bool)
            or requested_version != AUDIO_METADATA_PROTOCOL_VERSION
        ):
            raise ValueError(
                "audioMetadataProtocolVersion must be the integer 1"
            )
        if not self.uses_staged_pipeline:
            raise ValueError(
                "audio metadata protocol version 1 requires the staged "
                "schema-3 incremental TTS pipeline"
            )
        return requested_version

    async def _start_staged_pipeline(
        self,
        target_language: str,
        audio_metadata_protocol_version: Optional[int],
    ) -> tuple[Any, int, str]:
        """Start direct clients while the caller owns the lifecycle lock."""
        factory = self.staged_pipeline_factory or create_staged_pipeline
        pipeline = None
        self._staged_generation += 1
        generation = self._staged_generation
        self._audio_metadata_protocol_version = (
            audio_metadata_protocol_version
        )
        self._staged_audio_sequence_ids_sent = []
        self._staged_audio_subsegment_keys_sent = []
        self._staged_audio_frame_keys_sent = []
        self._staged_audio_frame_bytes_sent = []
        self._staged_parent_completions_sent = []
        self._staged_pending_frame_parent = None
        self._staged_pending_frame_count = 0
        self._staged_pending_frame_bytes = 0
        self._staged_pending_frame_sample_rate_hz = None
        self._staged_pending_frame_channels = None
        self._staged_pending_frame_bytes_per_sample = None
        self._staged_pending_frame_source_start_ms = None
        self._staged_pending_frame_source_end_ms = None
        self._staged_stream_sample_rate_hz = None
        self._staged_stream_channels = None
        self._staged_stream_bytes_per_sample = None
        self._staged_websocket_send_events = []
        try:
            pipeline = factory(target_language)
            if (
                audio_metadata_protocol_version
                == AUDIO_METADATA_PROTOCOL_VERSION
                and getattr(
                    getattr(pipeline, "config", None),
                    "tts_incremental_publish_enabled",
                    False,
                )
                is not True
            ):
                raise ValueError(
                    "audio metadata protocol version 1 requires the staged "
                    "schema-3 incremental TTS pipeline"
                )
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
                        getattr(output.segment, "subsequence_id", 0),
                        getattr(output.segment, "subsequence_count", 1),
                        include_composite=(
                            getattr(
                                getattr(pipeline, "config", None),
                                "tts_subsegment_max_chars",
                                0,
                            )
                            > 0
                        ),
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
                if output.kind is StagedOutputEventKind.AUDIO_FRAME:
                    frame = output.frame
                    audio = frame.audio
                    source_start_ms, source_end_ms = _source_range(frame)
                    timing_logger.log_audio_from_riva(len(audio))
                    sent = await self._send_staged_audio(
                        generation,
                        audio,
                        frame.parent_sequence_id,
                        audio_frame_id=frame.audio_frame_id,
                        include_frame=True,
                        sample_rate_hz=getattr(
                            frame,
                            "sample_rate_hz",
                            None,
                        ),
                        channels=getattr(frame, "channels", None),
                        bytes_per_sample=getattr(
                            frame,
                            "bytes_per_sample",
                            None,
                        ),
                        source_start_ms=source_start_ms,
                        source_end_ms=source_end_ms,
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
                if output.kind is StagedOutputEventKind.PARENT_COMPLETE:
                    completed = await self._complete_staged_parent(
                        generation,
                        output.completion,
                    )
                    if completed is None:
                        return
                    if not completed:
                        await self._send_staged_error(
                            generation,
                            "incremental TTS parent completion did not "
                            "reconcile WebSocket frames",
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
            if generation == self._staged_generation:
                self._audio_metadata_protocol_version = None

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
        schema_version = snapshot.get("telemetry_schema_version")
        if schema_version == 2:
            completed_subsegments = snapshot.get(
                "completed_subsegment_keys"
            )
            sent_subsegments = _subsegment_key_payloads(
                self._staged_audio_subsegment_keys_sent
            )
            if (
                completed_subsegments is None
                or list(completed_subsegments) != sent_subsegments
            ):
                return (
                    (
                        "WebSocket-sent subsegment keys did not match "
                        "dequeued pipeline output"
                    ),
                    True,
                )
        elif schema_version == 3:
            frame_key_fields = (
                "published_audio_frame_keys",
                "dequeued_audio_frame_keys",
                "websocket_sent_audio_frame_keys",
            )
            frame_byte_fields = (
                "published_audio_frame_bytes",
                "dequeued_audio_frame_bytes",
                "websocket_sent_audio_frame_bytes",
            )
            parent_fields = (
                "produced_parent_summaries",
                "completed_parent_summaries",
                "websocket_completed_parent_summaries",
            )
            for fields, label in (
                (frame_key_fields, "audio-frame identities"),
                (frame_byte_fields, "audio-frame byte counts"),
                (parent_fields, "parent completion summaries"),
            ):
                values = [snapshot.get(field) for field in fields]
                if (
                    any(not isinstance(value, list) for value in values)
                    or values[1:] != values[:-1]
                ):
                    return (
                        f"schema-3 {label} did not reconcile across pipeline "
                        "and WebSocket",
                        True,
                    )
            produced_parent_summaries = snapshot[
                "produced_parent_summaries"
            ]
            fallback_parent_ids = []
            for parent_summary in produced_parent_summaries:
                if (
                    not isinstance(parent_summary, dict)
                    or not isinstance(
                        parent_summary.get("atomic_fallback_applied"),
                        bool,
                    )
                ):
                    return (
                        "schema-3 atomic fallback attribution was invalid",
                        True,
                    )
                if parent_summary["atomic_fallback_applied"]:
                    fallback_parent_ids.append(
                        parent_summary.get("parent_sequence_id")
                    )
            fallback_count = snapshot.get(
                "tts_incremental_atomic_fallback_parent_count"
            )
            configured_threshold = snapshot.get(
                "tts_incremental_atomic_fallback_max_chars"
            )
            if (
                not isinstance(configured_threshold, int)
                or isinstance(configured_threshold, bool)
                or configured_threshold < 0
                or not isinstance(fallback_count, int)
                or isinstance(fallback_count, bool)
                or fallback_count != len(fallback_parent_ids)
                or snapshot.get(
                    "tts_incremental_atomic_fallback_parent_sequence_ids"
                )
                != fallback_parent_ids
                or (configured_threshold == 0 and fallback_parent_ids)
            ):
                return (
                    "schema-3 atomic fallback summary did not reconcile "
                    "across pipeline and WebSocket",
                    True,
                )
            if (
                self._staged_pending_frame_parent is not None
                or self._staged_pending_frame_count != 0
                or self._staged_pending_frame_bytes != 0
            ):
                return (
                    "schema-3 WebSocket retained an incomplete parent",
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
        subsequence_id: int = 0,
        subsequence_count: int = 1,
        *,
        include_composite: bool = False,
        audio_frame_id: Optional[int] = None,
        include_frame: bool = False,
        sample_rate_hz: Optional[int] = None,
        channels: Optional[int] = None,
        bytes_per_sample: Optional[int] = None,
        source_start_ms: Optional[float] = None,
        source_end_ms: Optional[float] = None,
    ) -> Optional[bool]:
        """Send current-generation PCM before any terminal message.

        The writer lock is acquired before the short lifecycle check. This
        preserves PCM/terminal order without holding the lifecycle lock across
        socket backpressure. ``None`` means a newer generation or a terminal
        owner already won; ``False`` means the socket write failed.
        """
        _validate_subsegment_key(
            sequence_id,
            subsequence_id,
            subsequence_count,
        )
        if include_frame:
            _validate_audio_frame_key(sequence_id, audio_frame_id)
            if include_composite:
                raise ValueError(
                    "audio frame and subsegment identities are mutually exclusive"
                )
        elif audio_frame_id is not None:
            raise ValueError(
                "audio_frame_id requires include_frame=true"
            )
        async with self._send_lock:
            async with self._lock:
                if (
                    generation != self._staged_generation
                    or self._staged_terminal_generation == generation
                    or self._closed
                ):
                    return None
            if include_frame:
                expected_parent = (
                    sequence_id
                    if self._staged_pending_frame_parent is None
                    else self._staged_pending_frame_parent
                )
                if (
                    sequence_id != expected_parent
                    or audio_frame_id != self._staged_pending_frame_count
                ):
                    return False
                if self._audio_metadata_protocol_version is not None:
                    try:
                        _validate_pcm_metadata(
                            sample_rate_hz,
                            channels,
                            bytes_per_sample,
                        )
                        _validate_source_range(
                            source_start_ms,
                            source_end_ms,
                        )
                    except ValueError:
                        return False
                    if len(audio) % (channels * bytes_per_sample):
                        return False
                    if (
                        self._staged_stream_sample_rate_hz is not None
                        and (
                            sample_rate_hz
                            != self._staged_stream_sample_rate_hz
                            or channels != self._staged_stream_channels
                            or bytes_per_sample
                            != self._staged_stream_bytes_per_sample
                        )
                    ):
                        return False
                    if self._staged_pending_frame_parent is not None and (
                        sample_rate_hz
                        != self._staged_pending_frame_sample_rate_hz
                        or channels != self._staged_pending_frame_channels
                        or bytes_per_sample
                        != self._staged_pending_frame_bytes_per_sample
                        or source_start_ms
                        != self._staged_pending_frame_source_start_ms
                        or source_end_ms
                        != self._staged_pending_frame_source_end_ms
                    ):
                        return False
                    header = {
                        "type": "audio_frame",
                        "protocolVersion": (
                            self._audio_metadata_protocol_version
                        ),
                        "streamGeneration": generation,
                        "parentSequenceId": sequence_id,
                        "audioFrameId": audio_frame_id,
                        "audioBytes": len(audio),
                        "sampleRateHz": sample_rate_hz,
                        "channels": channels,
                        "bytesPerSample": bytes_per_sample,
                        "sourceStartMs": source_start_ms,
                        "sourceEndMs": source_end_ms,
                    }
                    if not await self._send_json_unlocked(header):
                        return False
            if not await self._send_audio_unlocked(audio):
                return False
            timing_logger.log_audio_sent_to_client(len(audio))
            if include_frame:
                if self._staged_stream_sample_rate_hz is None:
                    self._staged_stream_sample_rate_hz = sample_rate_hz
                    self._staged_stream_channels = channels
                    self._staged_stream_bytes_per_sample = bytes_per_sample
                if self._staged_pending_frame_parent is None:
                    self._staged_pending_frame_parent = sequence_id
                    self._staged_pending_frame_sample_rate_hz = (
                        sample_rate_hz
                    )
                    self._staged_pending_frame_channels = channels
                    self._staged_pending_frame_bytes_per_sample = (
                        bytes_per_sample
                    )
                    self._staged_pending_frame_source_start_ms = (
                        source_start_ms
                    )
                    self._staged_pending_frame_source_end_ms = source_end_ms
                self._staged_audio_frame_keys_sent.append(
                    (sequence_id, audio_frame_id)
                )
                self._staged_audio_frame_bytes_sent.append(len(audio))
                self._staged_pending_frame_count += 1
                self._staged_pending_frame_bytes += len(audio)
            else:
                key = (sequence_id, subsequence_id, subsequence_count)
                self._staged_audio_subsegment_keys_sent.append(key)
                if subsequence_id == subsequence_count - 1:
                    self._staged_audio_sequence_ids_sent.append(sequence_id)
            event = {
                "sequence_id": sequence_id,
                "sent_monotonic_ms": time.monotonic_ns() / 1_000_000,
                "audio_bytes": len(audio),
            }
            if include_frame:
                event.update(
                    {
                        "parent_sequence_id": sequence_id,
                        "audio_frame_id": audio_frame_id,
                    }
                )
            elif include_composite:
                event.update(
                    {
                        "parent_sequence_id": sequence_id,
                        "subsequence_id": subsequence_id,
                        "subsequence_count": subsequence_count,
                    }
                )
            self._staged_websocket_send_events.append(event)
            return True

    async def _complete_staged_parent(
        self,
        generation: int,
        completion: Any,
    ) -> Optional[bool]:
        """Record an internal parent marker after all its frames were sent."""
        parent_sequence_id = getattr(
            completion,
            "parent_sequence_id",
            None,
        )
        audio_frame_count = getattr(completion, "audio_frame_count", None)
        audio_bytes = getattr(completion, "audio_bytes", None)
        retry_count = getattr(completion, "retry_count", None)
        atomic_fallback_applied = getattr(
            completion,
            "atomic_fallback_applied",
            None,
        )
        sample_rate_hz = getattr(completion, "sample_rate_hz", None)
        channels = getattr(completion, "channels", None)
        bytes_per_sample = getattr(
            completion,
            "bytes_per_sample",
            None,
        )
        source_start_ms, source_end_ms = _source_range(completion)
        if (
            not isinstance(parent_sequence_id, int)
            or isinstance(parent_sequence_id, bool)
            or parent_sequence_id < 0
            or not isinstance(audio_frame_count, int)
            or isinstance(audio_frame_count, bool)
            or audio_frame_count <= 0
            or not isinstance(audio_bytes, int)
            or isinstance(audio_bytes, bool)
            or audio_bytes <= 0
            or retry_count not in {0, 1}
            or not isinstance(atomic_fallback_applied, bool)
        ):
            return False
        async with self._send_lock:
            async with self._lock:
                if (
                    generation != self._staged_generation
                    or self._staged_terminal_generation == generation
                    or self._closed
                ):
                    return None
            if (
                self._staged_pending_frame_parent != parent_sequence_id
                or self._staged_pending_frame_count != audio_frame_count
                or self._staged_pending_frame_bytes != audio_bytes
            ):
                return False
            if self._audio_metadata_protocol_version is not None:
                try:
                    _validate_pcm_metadata(
                        sample_rate_hz,
                        channels,
                        bytes_per_sample,
                    )
                    _validate_source_range(
                        source_start_ms,
                        source_end_ms,
                    )
                except ValueError:
                    return False
                if audio_bytes % (channels * bytes_per_sample):
                    return False
                if (
                    sample_rate_hz
                    != self._staged_pending_frame_sample_rate_hz
                    or channels != self._staged_pending_frame_channels
                    or bytes_per_sample
                    != self._staged_pending_frame_bytes_per_sample
                    or source_start_ms
                    != self._staged_pending_frame_source_start_ms
                    or source_end_ms
                    != self._staged_pending_frame_source_end_ms
                ):
                    return False
                if not await self._send_json_unlocked(
                    {
                        "type": "audio_parent_complete",
                        "protocolVersion": (
                            self._audio_metadata_protocol_version
                        ),
                        "streamGeneration": generation,
                        "parentSequenceId": parent_sequence_id,
                        "audioFrameCount": audio_frame_count,
                        "audioBytes": audio_bytes,
                        "sourceStartMs": source_start_ms,
                        "sourceEndMs": source_end_ms,
                    }
                ):
                    return False
            summary = {
                "parent_sequence_id": parent_sequence_id,
                "audio_frame_count": audio_frame_count,
                "audio_bytes": audio_bytes,
                "retry_count": retry_count,
                "atomic_fallback_applied": atomic_fallback_applied,
            }
            self._staged_parent_completions_sent.append(summary)
            self._staged_audio_sequence_ids_sent.append(parent_sequence_id)
            self._staged_pending_frame_parent = None
            self._staged_pending_frame_count = 0
            self._staged_pending_frame_bytes = 0
            self._staged_pending_frame_sample_rate_hz = None
            self._staged_pending_frame_channels = None
            self._staged_pending_frame_bytes_per_sample = None
            self._staged_pending_frame_source_start_ms = None
            self._staged_pending_frame_source_end_ms = None
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
        schema_version = snapshot.get("telemetry_schema_version")
        if schema_version == 2:
            snapshot["websocket_sent_subsegment_keys"] = (
                _subsegment_key_payloads(
                    self._staged_audio_subsegment_keys_sent
                )
            )
        elif schema_version == 3:
            snapshot["websocket_sent_audio_frame_keys"] = (
                _audio_frame_key_payloads(
                    self._staged_audio_frame_keys_sent
                )
            )
            snapshot["websocket_sent_audio_frame_bytes"] = list(
                self._staged_audio_frame_bytes_sent
            )
            snapshot["websocket_completed_parent_summaries"] = [
                dict(item) for item in self._staged_parent_completions_sent
            ]
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
        cleanup_active = (
            self._staged_cleanup_task is not None
            and not self._staged_cleanup_task.done()
        )
        if (
            self._staged_pipeline is not None
            or self._staged_output_task is not None
            or cleanup_active
        ):
            return False
        self._last_staged_summary = None
        self._staged_audio_sequence_ids_sent = []
        self._staged_audio_subsegment_keys_sent = []
        self._staged_audio_frame_keys_sent = []
        self._staged_audio_frame_bytes_sent = []
        self._staged_parent_completions_sent = []
        self._staged_pending_frame_parent = None
        self._staged_pending_frame_count = 0
        self._staged_pending_frame_bytes = 0
        self._staged_pending_frame_sample_rate_hz = None
        self._staged_pending_frame_channels = None
        self._staged_pending_frame_bytes_per_sample = None
        self._staged_pending_frame_source_start_ms = None
        self._staged_pending_frame_source_end_ms = None
        self._staged_stream_sample_rate_hz = None
        self._staged_stream_channels = None
        self._staged_stream_bytes_per_sample = None
        self._audio_metadata_protocol_version = None
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
        self._audio_metadata_protocol_version = None
        self._staged_stream_sample_rate_hz = None
        self._staged_stream_channels = None
        self._staged_stream_bytes_per_sample = None
        pipeline = self._staged_pipeline
        output_task = self._staged_output_task
        if pipeline is not None:
            # Keep an immediately exportable failure snapshot while aclose()
            # yields. A second snapshot below records the finalized CLOSED
            # state and any cleanup errors.
            self._retain_staged_summary(pipeline)
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
                    self._audio_metadata_protocol_version = None
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
        self._audio_metadata_protocol_version = None
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
            self._retain_staged_summary(pipeline)
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


def _subsegment_key_payloads(
    keys: list[tuple[int, int, int]],
) -> list[dict]:
    return [
        {
            "parent_sequence_id": parent_sequence_id,
            "subsequence_id": subsequence_id,
            "subsequence_count": subsequence_count,
        }
        for parent_sequence_id, subsequence_id, subsequence_count in keys
    ]


def _audio_frame_key_payloads(
    keys: list[tuple[int, int]],
) -> list[dict]:
    return [
        {
            "parent_sequence_id": parent_sequence_id,
            "audio_frame_id": audio_frame_id,
        }
        for parent_sequence_id, audio_frame_id in keys
    ]


def _validate_subsegment_key(
    parent_sequence_id: int,
    subsequence_id: int,
    subsequence_count: int,
) -> None:
    for name, value in (
        ("parent_sequence_id", parent_sequence_id),
        ("subsequence_id", subsequence_id),
        ("subsequence_count", subsequence_count),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
    if parent_sequence_id < 0:
        raise ValueError("parent_sequence_id must be non-negative")
    if subsequence_count <= 0:
        raise ValueError("subsequence_count must be positive")
    if subsequence_id < 0 or subsequence_id >= subsequence_count:
        raise ValueError("subsequence_id is outside subsequence_count")


def _validate_audio_frame_key(
    parent_sequence_id: int,
    audio_frame_id: Optional[int],
) -> None:
    for name, value in (
        ("parent_sequence_id", parent_sequence_id),
        ("audio_frame_id", audio_frame_id),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative integer")


def _source_range(value: Any) -> tuple[Optional[float], Optional[float]]:
    """Return privacy-safe source timing from a synthesized payload."""
    translation = getattr(value, "translation", None)
    carrier = getattr(translation, "segment", None)
    if carrier is None:
        carrier = value
    return (
        getattr(carrier, "source_start_ms", None),
        getattr(carrier, "source_end_ms", None),
    )


def _validate_pcm_metadata(
    sample_rate_hz: Any,
    channels: Any,
    bytes_per_sample: Any,
) -> None:
    for name, value in (
        ("sample_rate_hz", sample_rate_hz),
        ("channels", channels),
        ("bytes_per_sample", bytes_per_sample),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive integer")


def _validate_source_range(
    source_start_ms: Any,
    source_end_ms: Any,
) -> None:
    for name, value in (
        ("source_start_ms", source_start_ms),
        ("source_end_ms", source_end_ms),
    ):
        if value is None:
            continue
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be non-negative and finite")
    if (
        source_start_ms is not None
        and source_end_ms is not None
        and source_end_ms < source_start_ms
    ):
        raise ValueError("source_end_ms cannot precede source_start_ms")


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
