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
    endpointing_history_ms: int = int(os.getenv("RIVA_EOU_MS", "800"))
    asr_word_time_offsets: bool = os.getenv("RIVA_ASR_WORD_TIMES", "0") == "1"


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
    close_timeout_s: float = float(
        os.getenv("STAGED_CLOSE_TIMEOUT_SECONDS", "10")
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
