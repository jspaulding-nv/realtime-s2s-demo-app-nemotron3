"""Shared streaming ASR request construction for monolithic and staged paths."""

from typing import Optional

import riva.client
import riva.client.proto.riva_asr_pb2 as riva_asr_pb2

from config import audio_config, riva_config


def create_streaming_asr_config(
    *,
    sample_rate_hz: Optional[int] = None,
    channels: Optional[int] = None,
    language_code: Optional[str] = None,
    endpointing_history_ms: Optional[int] = None,
    interim_results: bool = True,
    enable_word_time_offsets: bool = False,
) -> riva_asr_pb2.StreamingRecognitionConfig:
    """Build the tested Nemotron RNNT streaming request.

    The Riva team's current starting point is an 800 ms final EOU window with
    automatic punctuation. Do not add the CTC-only ``stop_history_eou`` fields
    to this RNNT configuration.
    """

    resolved_rate = (
        audio_config.sample_rate if sample_rate_hz is None else sample_rate_hz
    )
    resolved_channels = audio_config.channels if channels is None else channels
    resolved_language = language_code or riva_config.source_language
    resolved_eou = (
        riva_config.endpointing_history_ms
        if endpointing_history_ms is None
        else endpointing_history_ms
    )

    if resolved_rate <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if resolved_channels <= 0:
        raise ValueError("channels must be positive")
    if resolved_eou <= 0:
        raise ValueError("endpointing_history_ms must be positive")

    endpointing_config = riva_asr_pb2.EndpointingConfig(
        start_history=300,
        start_threshold=0.2,
        stop_history=resolved_eou,
        stop_threshold=0.98,
    )
    recognition_config = riva_asr_pb2.RecognitionConfig(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hertz=resolved_rate,
        language_code=resolved_language,
        max_alternatives=1,
        enable_automatic_punctuation=True,
        enable_word_time_offsets=enable_word_time_offsets,
        audio_channel_count=resolved_channels,
        endpointing_config=endpointing_config,
    )
    return riva_asr_pb2.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=interim_results,
    )
