"""Configuration settings for the speech-to-speech translation backend."""

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
    """Experimental text segmentation settings for the staged path."""

    segment_max_chars: int = int(os.getenv("STAGED_SEGMENT_MAX_CHARS", "240"))
    segment_max_age_ms: int = int(os.getenv("STAGED_SEGMENT_MAX_AGE_MS", "2000"))


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
