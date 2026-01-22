"""Audio format conversion utilities."""

import numpy as np
from config import audio_config


def float32_to_int16(float_audio: bytes) -> bytes:
    """
    Convert Float32 PCM audio (from browser) to Int16 PCM (for Riva).

    Browser Web Audio API outputs Float32 samples in range [-1.0, 1.0].
    Riva expects Int16 samples in range [-32768, 32767].
    """
    float_array = np.frombuffer(float_audio, dtype=np.float32)
    # Scale and clip to int16 range
    int_array = np.clip(float_array * 32767, -32768, 32767).astype(np.int16)
    return int_array.tobytes()


def int16_to_float32(int_audio: bytes) -> bytes:
    """
    Convert Int16 PCM audio (from Riva) to Float32 PCM (for browser).

    Riva outputs Int16 samples in range [-32768, 32767].
    Browser Web Audio API expects Float32 samples in range [-1.0, 1.0].
    """
    int_array = np.frombuffer(int_audio, dtype=np.int16)
    float_array = (int_array.astype(np.float32) / 32767.0)
    return float_array.tobytes()


def calculate_rms(audio_bytes: bytes, dtype: str = "int16") -> float:
    """
    Calculate RMS (root mean square) level of audio.

    Returns a value between 0.0 and 1.0.
    """
    if dtype == "int16":
        audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        max_val = 32767.0
    else:  # float32
        audio_array = np.frombuffer(audio_bytes, dtype=np.float32)
        max_val = 1.0

    if len(audio_array) == 0:
        return 0.0

    rms = np.sqrt(np.mean(audio_array ** 2))
    normalized = min(rms / max_val, 1.0)
    return normalized


def validate_audio_chunk(audio_bytes: bytes) -> bool:
    """Validate that audio chunk has expected size."""
    expected_bytes = audio_config.chunk_size * audio_config.bytes_per_sample
    return len(audio_bytes) == expected_bytes
