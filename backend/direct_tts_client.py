"""Direct Magpie streaming-TTS adapter for the staged speech pipeline."""

from __future__ import annotations

import threading
import time
import math
from typing import Any, Callable, Dict, Optional

import riva.client

from config import SUPPORTED_LANGUAGES, audio_config, riva_config
from staged_models import SynthesizedSegment, TranslatedSegment
from target_text_validation import validate_target_text


class DirectTTSError(RuntimeError):
    """Raised when a direct TTS request cannot produce a complete segment."""


class DirectTTSCancelled(DirectTTSError):
    """Raised when client shutdown cancels an in-flight synthesis request."""


def _default_clock_ms() -> float:
    return time.monotonic() * 1000.0


class DirectTTSClient:
    """Synthesize one translated segment at a time through the TTS NIM.

    Magpie returns audio incrementally, but this adapter retains every chunk
    until the RPC completes.  The caller therefore observes either one whole
    :class:`SynthesizedSegment` or an exception, never partial audio that a
    later attempt could duplicate.

    The blocking API is intended to be owned by one staged-pipeline worker.
    Calls from multiple workers are rejected rather than reordered.
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        *,
        sample_rate_hz: Optional[int] = None,
        channels: Optional[int] = None,
        bytes_per_sample: Optional[int] = None,
        max_response_chunk_bytes: int = 256 * 1024,
        max_audio_duration_s: float = 60.0,
        language_configs: Optional[Dict[str, dict]] = None,
        clock_ms: Optional[Callable[[], float]] = None,
    ) -> None:
        self.uri = _required_text(
            "uri", riva_config.tts_uri if uri is None else uri
        )
        self.sample_rate_hz = (
            audio_config.sample_rate if sample_rate_hz is None else sample_rate_hz
        )
        self.channels = audio_config.channels if channels is None else channels
        self.bytes_per_sample = (
            audio_config.bytes_per_sample
            if bytes_per_sample is None
            else bytes_per_sample
        )
        if (
            not isinstance(self.sample_rate_hz, int)
            or isinstance(self.sample_rate_hz, bool)
            or self.sample_rate_hz <= 0
        ):
            raise ValueError("sample_rate_hz must be a positive integer")
        # The public Riva request has no channel-count or bit-depth fields.
        # LINEAR_PCM responses from this tested Magpie profile are mono Int16.
        if not isinstance(self.channels, int) or isinstance(self.channels, bool):
            raise ValueError("channels must be an integer")
        if self.channels != 1:
            raise ValueError("direct Magpie TTS currently requires mono output")
        if not isinstance(self.bytes_per_sample, int) or isinstance(
            self.bytes_per_sample, bool
        ):
            raise ValueError("bytes_per_sample must be an integer")
        if self.bytes_per_sample != 2:
            raise ValueError("direct Magpie TTS currently requires 16-bit PCM")
        if (
            not isinstance(max_response_chunk_bytes, int)
            or isinstance(max_response_chunk_bytes, bool)
            or max_response_chunk_bytes <= 0
        ):
            raise ValueError("max_response_chunk_bytes must be a positive integer")
        if (
            not isinstance(max_audio_duration_s, (int, float))
            or isinstance(max_audio_duration_s, bool)
            or not math.isfinite(max_audio_duration_s)
            or max_audio_duration_s <= 0
        ):
            raise ValueError("max_audio_duration_s must be a positive finite number")
        self.max_response_chunk_bytes = max_response_chunk_bytes
        self.max_audio_duration_s = float(max_audio_duration_s)
        self.max_audio_bytes = int(
            self.sample_rate_hz
            * self.channels
            * self.bytes_per_sample
            * self.max_audio_duration_s
        )

        self._language_configs = (
            SUPPORTED_LANGUAGES if language_configs is None else language_configs
        )
        self._clock_ms = clock_ms or _default_clock_ms
        self._auth = None
        self._service = None
        self._connected = False
        self._closing = False
        self._synthesis_active = False
        self._active_call = None
        self._lifecycle_lock = threading.RLock()

    def connect(self) -> bool:
        """Create the direct TTS channel; repeated connected calls are safe."""
        with self._lifecycle_lock:
            if self._closing:
                print("[Direct TTS] Cannot connect while client close is in progress")
                return False
            if self._connected:
                return True
            if self._synthesis_active:
                print("[Direct TTS] Cannot connect while synthesis is active")
                return False

            new_auth = None
            try:
                new_auth = riva.client.Auth(uri=self.uri)
                new_service = riva.client.SpeechSynthesisService(new_auth)
                self._auth = new_auth
                self._service = new_service
                self._connected = True
                return True
            except Exception as exc:
                print(f"[Direct TTS] Failed to connect to {self.uri}: {exc}")
                channel = getattr(new_auth, "channel", None)
                if channel is not None:
                    channel.close()
                return False

    def is_connected(self) -> bool:
        with self._lifecycle_lock:
            return self._connected

    def disconnect(self) -> None:
        """Cancel an active RPC, if any, and close the channel exactly once."""
        with self._lifecycle_lock:
            if self._closing:
                return
            self._closing = True
            active_call = self._active_call
            auth = self._auth
            self._connected = False
            self._service = None
            self._auth = None

        try:
            _cancel_call(active_call)
            channel = getattr(auth, "channel", None)
            if channel is not None:
                channel.close()
        finally:
            with self._lifecycle_lock:
                self._closing = False

    def synthesize(self, translation: TranslatedSegment) -> SynthesizedSegment:
        """Compatibility shorthand for :meth:`synthesize_segment`."""
        return self.synthesize_segment(translation)

    def synthesize_segment(
        self, translation: TranslatedSegment
    ) -> SynthesizedSegment:
        """Return one atomically collected PCM segment for ``translation``."""
        if not isinstance(translation, TranslatedSegment):
            raise TypeError("translation must be a TranslatedSegment")
        text = validate_target_text(
            translation.text,
            language=translation.language,
            sequence_id=translation.sequence_id,
        )
        voice_name = self._voice_for_language(translation.language)

        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("Direct TTS client is closing")
            if not self._connected or self._service is None:
                raise RuntimeError("Direct TTS client is not connected")
            if self._synthesis_active:
                raise RuntimeError("a direct TTS synthesis is already active")
            self._synthesis_active = True
            service = self._service

        call = None
        started_ms = None
        chunks = []
        total_audio_bytes = 0
        first_audio_ms = None
        try:
            started_ms = self._clock_ms()
            call = service.synthesize_online(
                text=text,
                voice_name=voice_name,
                language_code=translation.language,
                encoding=riva.client.AudioEncoding.LINEAR_PCM,
                sample_rate_hz=self.sample_rate_hz,
            )
            with self._lifecycle_lock:
                if not self._connected:
                    _cancel_call(call)
                    raise DirectTTSCancelled(
                        "Direct TTS client disconnected before synthesis started"
                    )
                self._active_call = call

            frame_bytes = self.channels * self.bytes_per_sample
            for response in call:
                with self._lifecycle_lock:
                    if not self._connected:
                        raise DirectTTSCancelled(
                            "Direct TTS client disconnected during synthesis"
                        )
                audio = bytes(getattr(response, "audio", b""))
                if not audio:
                    continue
                if len(audio) > self.max_response_chunk_bytes:
                    raise DirectTTSError(
                        "Direct TTS response chunk exceeded the configured limit: "
                        f"{len(audio)} > {self.max_response_chunk_bytes} bytes"
                    )
                if len(audio) % frame_bytes:
                    raise DirectTTSError(
                        "Direct TTS returned a partial PCM frame "
                        f"({len(audio)} bytes for {frame_bytes}-byte frames)"
                    )
                total_audio_bytes += len(audio)
                if total_audio_bytes > self.max_audio_bytes:
                    raise DirectTTSError(
                        "Direct TTS segment exceeded the configured audio limit: "
                        f"{total_audio_bytes} > {self.max_audio_bytes} bytes"
                    )
                if first_audio_ms is None:
                    first_audio_ms = self._clock_ms()
                chunks.append(audio)

            with self._lifecycle_lock:
                if not self._connected:
                    raise DirectTTSCancelled(
                        "Direct TTS client disconnected before synthesis completed"
                    )
            if not chunks or first_audio_ms is None:
                raise DirectTTSError("Direct TTS returned no audio")
            completed_ms = self._clock_ms()
            return SynthesizedSegment(
                translation=translation,
                audio=b"".join(chunks),
                sample_rate_hz=self.sample_rate_hz,
                channels=self.channels,
                bytes_per_sample=self.bytes_per_sample,
                started_monotonic_ms=started_ms,
                first_audio_monotonic_ms=first_audio_ms,
                completed_monotonic_ms=completed_ms,
            )
        except DirectTTSError:
            _cancel_call(call)
            raise
        except Exception as exc:
            _cancel_call(call)
            with self._lifecycle_lock:
                disconnected = not self._connected
            if disconnected:
                raise DirectTTSCancelled(
                    "Direct TTS synthesis was cancelled by client shutdown"
                ) from exc
            raise DirectTTSError(
                "Direct TTS synthesis failed for segment "
                f"{translation.segment.sequence_id}: {exc}"
            ) from exc
        finally:
            with self._lifecycle_lock:
                if self._active_call is call:
                    self._active_call = None
                self._synthesis_active = False

    def _voice_for_language(self, language: str) -> str:
        if not language or not language.strip():
            raise ValueError("TTS language must contain non-whitespace content")
        language_config = self._language_configs.get(language)
        if not language_config or not language_config.get("available", False):
            raise ValueError(f"unsupported TTS language: {language}")
        voice_name = language_config.get("voice", "")
        if not isinstance(voice_name, str) or not voice_name.strip():
            raise ValueError(f"no TTS voice configured for language: {language}")
        return voice_name.strip()


def _cancel_call(call: Any) -> None:
    cancel = getattr(call, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            # Channel close below remains the final abort mechanism.
            pass


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must contain non-whitespace text")
    return value.strip()


# Separate global instance for the feature-flagged staged orchestrator. It is
# not used by the existing monolithic S2S path.
direct_tts_client = DirectTTSClient()
