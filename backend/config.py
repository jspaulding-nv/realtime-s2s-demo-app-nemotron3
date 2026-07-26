"""Configuration settings for the speech-to-speech translation backend."""

import math
import os
from dataclasses import dataclass
from typing import Dict


@dataclass
class AudioConfig:
    """Audio configuration matching the existing realtime_s2s.py."""
    sample_rate: int = 16000
    chunk_size: int = 4800  # ~300ms at 16kHz
    channels: int = 1
    bytes_per_sample: int = 2  # int16


@dataclass
class RivaConfig:
    """Riva service configuration."""
    uri: str = os.getenv("RIVA_URI", "localhost:50051")
    asr_uri: str = os.getenv("RIVA_ASR_URI", "localhost:50052")
    tts_uri: str = os.getenv("RIVA_TTS_URI", "localhost:50053")
    model: str = os.getenv("RIVA_NMT_MODEL", "megatronnmt_any_any_1b")
    source_language: str = os.getenv("RIVA_SOURCE_LANGUAGE", "en-US")
    target_language: str = os.getenv("RIVA_TARGET_LANGUAGE", "es-US")
    endpointing_history_ms: int = int(os.getenv("RIVA_EOU_MS", "800"))
    asr_word_time_offsets: bool = os.getenv("RIVA_ASR_WORD_TIMES", "0") == "1"
    asr_image: str = os.getenv(
        "ASR_IMAGE", "nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0"
    )
    nmt_image: str = os.getenv(
        "NMT_IMAGE", "nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2"
    )
    tts_image: str = os.getenv(
        "TTS_IMAGE", "nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0"
    )
    asr_image_digest: str = os.getenv("ASR_IMAGE_DIGEST", "")
    nmt_image_digest: str = os.getenv("NMT_IMAGE_DIGEST", "")
    tts_image_digest: str = os.getenv("TTS_IMAGE_DIGEST", "")
    asr_profile: str = os.getenv(
        "ASR_NIM_TAGS_SELECTOR",
        "name=nemotron-asr-streaming,type=en-US,batch_size=32",
    )
    # The proven NMT Compose launch did not set NIM_TAGS_SELECTOR. This optional
    # label is provenance-only for externally configured deployments; it is
    # intentionally not presented as a Compose-applied selector.
    nmt_profile: str = os.getenv("NMT_PROFILE", "")
    tts_profile: str = os.getenv(
        "TTS_NIM_TAGS_SELECTOR",
        "name=magpie-tts-multilingual,batch_size=8",
    )


@dataclass
class StagedPipelineConfig:
    """Experimental staged-pipeline controls.

    The browser remains on the proven monolithic route unless ``pipeline_mode``
    is explicitly set to ``staged``. Queue limits are deliberately small: they
    bound memory and expose overload; they are not a promise that a live audio
    source can be backpressured.
    """

    pipeline_mode: str = os.getenv("S2S_PIPELINE_MODE", "monolithic")
    segment_max_chars: int = int(os.getenv("STAGED_SEGMENT_MAX_CHARS", "240"))
    segment_max_age_ms: int = int(os.getenv("STAGED_SEGMENT_MAX_AGE_MS", "2000"))
    asr_event_queue_maxsize: int = int(
        os.getenv("STAGED_ASR_EVENT_QUEUE_MAXSIZE", "32")
    )
    nmt_queue_maxsize: int = int(os.getenv("STAGED_NMT_QUEUE_MAXSIZE", "4"))
    tts_queue_maxsize: int = int(os.getenv("STAGED_TTS_QUEUE_MAXSIZE", "4"))
    output_queue_maxsize: int = int(
        os.getenv("STAGED_OUTPUT_QUEUE_MAXSIZE", "4")
    )
    nmt_rpc_timeout_s: float = float(
        os.getenv("STAGED_NMT_RPC_TIMEOUT_SECONDS", "15")
    )
    tts_rpc_timeout_s: float = float(
        os.getenv("STAGED_TTS_RPC_TIMEOUT_SECONDS", "60")
    )
    tts_max_segment_audio_s: float = float(
        os.getenv("STAGED_TTS_MAX_SEGMENT_AUDIO_SECONDS", "60")
    )
    tts_max_retries: int = int(os.getenv("STAGED_TTS_MAX_RETRIES", "1"))
    # Default-off diagnostic: retain response timing/byte counts only. It
    # never publishes partial PCM and therefore does not change retry safety.
    tts_response_chunk_telemetry_enabled: bool = (
        os.getenv("STAGED_TTS_RESPONSE_CHUNK_TELEMETRY", "0") == "1"
    )
    # Zero preserves the existing one-parent/one-TTS-call path. Positive
    # values opt into post-NMT target-text subsegmentation.
    tts_subsegment_max_chars: int = int(
        os.getenv("STAGED_TTS_SUBSEGMENT_MAX_CHARS", "0")
    )
    tts_subsegment_min_chars: int = int(
        os.getenv("STAGED_TTS_SUBSEGMENT_MIN_CHARS", "12")
    )
    close_timeout_s: float = float(
        os.getenv("STAGED_CLOSE_TIMEOUT_SECONDS", "10")
    )
    # Default-off schema-v3 experiment: forward bounded, frame-aligned PCM
    # while one TTS response iterator is still active. These fields remain at
    # the end to preserve the legacy positional constructor order.
    tts_incremental_publish_enabled: bool = (
        os.getenv("STAGED_TTS_INCREMENTAL_PUBLISH", "0") == "1"
    )
    tts_incremental_frame_ms: int = int(
        os.getenv("STAGED_TTS_INCREMENTAL_FRAME_MS", "500")
    )
    # Tiny validated targets retain atomic retry safety even under schema 3,
    # then publish the successful private PCM through normal output frames.
    # Zero disables this narrowly bounded fallback.
    tts_incremental_atomic_fallback_max_chars: int = int(
        os.getenv(
            "STAGED_TTS_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS",
            "4",
        )
    )

    def __post_init__(self) -> None:
        if not isinstance(self.pipeline_mode, str):
            raise ValueError("S2S_PIPELINE_MODE must be text")
        self.pipeline_mode = self.pipeline_mode.strip().lower()
        if self.pipeline_mode not in {"monolithic", "staged"}:
            raise ValueError(
                "S2S_PIPELINE_MODE must be either 'monolithic' or 'staged'"
            )
        for name in (
            "segment_max_chars",
            "segment_max_age_ms",
            "asr_event_queue_maxsize",
            "nmt_queue_maxsize",
            "tts_queue_maxsize",
            "output_queue_maxsize",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            not isinstance(self.tts_max_retries, int)
            or isinstance(self.tts_max_retries, bool)
            or self.tts_max_retries not in {0, 1}
        ):
            raise ValueError("tts_max_retries must be zero or one")
        if not isinstance(self.tts_response_chunk_telemetry_enabled, bool):
            raise ValueError(
                "tts_response_chunk_telemetry_enabled must be a boolean"
            )
        if not isinstance(self.tts_incremental_publish_enabled, bool):
            raise ValueError(
                "tts_incremental_publish_enabled must be a boolean"
            )
        if (
            not isinstance(self.tts_incremental_frame_ms, int)
            or isinstance(self.tts_incremental_frame_ms, bool)
            or self.tts_incremental_frame_ms <= 0
        ):
            raise ValueError(
                "tts_incremental_frame_ms must be a positive integer"
            )
        if (
            not isinstance(
                self.tts_incremental_atomic_fallback_max_chars,
                int,
            )
            or isinstance(
                self.tts_incremental_atomic_fallback_max_chars,
                bool,
            )
            or self.tts_incremental_atomic_fallback_max_chars < 0
        ):
            raise ValueError(
                "tts_incremental_atomic_fallback_max_chars must be a "
                "non-negative integer"
            )
        if (
            not isinstance(self.tts_subsegment_max_chars, int)
            or isinstance(self.tts_subsegment_max_chars, bool)
            or self.tts_subsegment_max_chars < 0
        ):
            raise ValueError(
                "tts_subsegment_max_chars must be a non-negative integer"
            )
        if (
            not isinstance(self.tts_subsegment_min_chars, int)
            or isinstance(self.tts_subsegment_min_chars, bool)
            or self.tts_subsegment_min_chars <= 0
        ):
            raise ValueError(
                "tts_subsegment_min_chars must be a positive integer"
            )
        if (
            self.tts_incremental_publish_enabled
            and self.tts_subsegment_max_chars > 0
        ):
            raise ValueError(
                "incremental TTS publication and TTS subsegmentation are "
                "mutually exclusive"
            )
        for name in (
            "nmt_rpc_timeout_s",
            "tts_rpc_timeout_s",
            "tts_max_segment_audio_s",
            "close_timeout_s",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")

    @property
    def telemetry_schema_version(self) -> int:
        if self.tts_incremental_publish_enabled:
            return 3
        if self.tts_subsegment_max_chars > 0:
            return 2
        return 1

    @property
    def tts_publisher_handoff_telemetry_enabled(self) -> bool:
        """Whether schema-3 publisher handoff timing is available."""
        return (
            self.tts_incremental_publish_enabled
            and self.tts_response_chunk_telemetry_enabled
        )


# Supported target languages with their TTS voice names
# Note: Only languages with voices installed on the Riva server will work
SUPPORTED_LANGUAGES: Dict[str, dict] = {
    "es-US": {
        "name": "Spanish (US)",
        "voice": "Magpie-Multilingual.ES-US.Isabela",
        "available": True,
    },
    # Add more languages here when voices are installed on the Riva server
    # Example format:
    # "fr-FR": {
    #     "name": "French",
    #     "voice": "Voice-Name-Here",
    #     "available": True,
    # },
}

# Default configuration instances
audio_config = AudioConfig()
riva_config = RivaConfig()
staged_pipeline_config = StagedPipelineConfig()
