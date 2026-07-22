"""Riva S2S client wrapper adapted from realtime_s2s.py."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from typing import Optional, Callable

import riva.client
import riva.client.proto.riva_nmt_pb2 as riva_nmt_pb2

from asr_config import create_streaming_asr_config
from config import audio_config, riva_config, SUPPORTED_LANGUAGES


VERBOSE_CHUNKS = os.getenv("RIVA_VERBOSE_CHUNKS", "0") == "1"


class AudioChunkIterator:
    """Iterator that yields audio chunks from a queue."""

    def __init__(self):
        self._queue: Queue = Queue()
        self._stopped = False
        self._input_exhausted = False
        self._chunk_count = 0

    def add_chunk(self, chunk: bytes) -> None:
        """Add an audio chunk to be processed."""
        if not self._stopped:
            self._chunk_count += 1
            if VERBOSE_CHUNKS:
                print(f"[Riva] Audio chunk {self._chunk_count} added, {len(chunk)} bytes")
            self._queue.put(chunk)

    def stop(self) -> None:
        """Signal the iterator to stop."""
        if self._stopped:
            return
        print("[Riva] Iterator stopped")
        self._stopped = True
        self._queue.put(None)  # Sentinel to unblock iteration

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        # Block until we get a chunk or are stopped
        while True:
            try:
                chunk = self._queue.get(timeout=0.5)
                if chunk is None:
                    print("[Riva] Got stop sentinel")
                    self._input_exhausted = True
                    raise StopIteration
                return chunk
            except Empty:
                # stop() always queues a sentinel. Waiting for that sentinel,
                # rather than merely observing _stopped, proves that every
                # previously queued audio chunk was consumed.
                continue


class RivaS2SClient:
    """Wrapper for Riva Speech-to-Speech translation."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._nmt_client: Optional[riva.client.NeuralMachineTranslationClient] = None
        self._auth: Optional[riva.client.Auth] = None
        self._connected = False

    def connect(self) -> bool:
        """Connect to Riva services."""
        try:
            print(f"[Riva] Connecting to {riva_config.uri}...")
            self._auth = riva.client.Auth(uri=riva_config.uri)
            self._nmt_client = riva.client.NeuralMachineTranslationClient(self._auth)
            self._connected = True
            print("[Riva] Connected successfully")
            return True
        except Exception as e:
            print(f"[Riva] Failed to connect: {e}")
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from Riva services."""
        self._connected = False
        self._nmt_client = None
        self._auth = None

    def is_connected(self) -> bool:
        """Check if connected to Riva."""
        return self._connected

    def create_s2s_config(self, target_language: str) -> riva.client.StreamingTranslateSpeechToSpeechConfig:
        """Create S2S configuration for the specified target language."""
        # Get voice for target language
        lang_config = SUPPORTED_LANGUAGES.get(target_language, SUPPORTED_LANGUAGES["es-US"])
        voice_name = lang_config["voice"]

        print(f"[Riva] Creating config: {riva_config.source_language} -> {target_language}, voice: {voice_name}")

        # Shared with the staged direct-ASR adapter so both paths use the same
        # Nemotron RNNT endpointing and punctuation request.
        asr_config = create_streaming_asr_config()

        # NMT config for translation
        translation_config = riva_nmt_pb2.TranslationConfig(
            source_language_code=riva_config.source_language,
            target_language_code=target_language,
            model_name=riva_config.model
        )

        # TTS config for speech synthesis
        tts_config = riva.client.SynthesizeSpeechConfig(
            language_code=target_language,
            encoding=riva.client.AudioEncoding.LINEAR_PCM,
            sample_rate_hz=audio_config.sample_rate,
            voice_name=voice_name,
        )

        return riva.client.StreamingTranslateSpeechToSpeechConfig(
            asr_config=asr_config,
            translation_config=translation_config,
            tts_config=tts_config,
        )

    async def translate_stream(
        self,
        target_language: str,
        on_audio: Callable[[bytes], None],
        on_error: Callable[[str], None],
        on_complete: Callable[[], None],
    ) -> AudioChunkIterator:
        """
        Start a translation stream.

        Returns an AudioChunkIterator that accepts audio chunks.
        Translated audio is sent via the on_audio callback.
        """
        if not self._connected or not self._nmt_client:
            on_error("Not connected to Riva")
            raise RuntimeError("Not connected to Riva")

        chunk_iterator = AudioChunkIterator()

        def run_translation():
            """Run the blocking Riva translation in a thread with auto-restart."""
            print("[Riva] Starting translation thread")
            total_responses = 0
            restart_count = 0
            completed_successfully = False

            try:
                while True:
                    try:
                        # Create fresh config for each stream session
                        config = self.create_s2s_config(target_language)
                        responses = self._nmt_client.streaming_s2s_response_generator(
                            audio_chunks=chunk_iterator,
                            streaming_config=config,
                        )

                        for response in responses:
                            total_responses += 1
                            if response.speech and response.speech.audio:
                                audio_len = len(response.speech.audio)
                                if VERBOSE_CHUNKS:
                                    print(f"[Riva] Response {total_responses}: got {audio_len} bytes of audio")
                                on_audio(response.speech.audio)
                            elif VERBOSE_CHUNKS:
                                print(f"[Riva] Response {total_responses}: no audio")

                        # for-loop ended normally = ASR endpointing closed the stream
                        if not chunk_iterator._input_exhausted:
                            restart_count += 1
                            print(
                                "[Riva] ASR endpointing before input exhaustion "
                                f"— restarting stream (#{restart_count})"
                            )
                            continue
                        print(f"[Riva] Stream stopped normally, {total_responses} total responses")
                        completed_successfully = True
                        break

                    except Exception as e:
                        print(f"[Riva] Translation failed: {e}")
                        on_error(f"Riva translation stream failed: {e}")
                        break
            finally:
                print(f"[Riva] Translation thread exiting. "
                      f"{total_responses} responses, {restart_count} restarts")
                if completed_successfully:
                    try:
                        on_complete()
                    except Exception as exc:
                        print(f"[Riva] Completion callback failed: {exc}")

        # Run translation in background thread
        self._executor.submit(run_translation)

        return chunk_iterator


# Global client instance
riva_client = RivaS2SClient()
