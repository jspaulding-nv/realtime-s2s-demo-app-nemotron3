"""Riva S2S client wrapper adapted from realtime_s2s.py."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from typing import Optional, Callable

import riva.client
import riva.client.proto.riva_asr_pb2 as riva_asr_pb2
import riva.client.proto.riva_nmt_pb2 as riva_nmt_pb2

from config import audio_config, riva_config, SUPPORTED_LANGUAGES


class AudioChunkIterator:
    """Iterator that yields audio chunks from a queue."""

    def __init__(self):
        self._queue: Queue = Queue()
        self._stopped = False
        self._chunk_count = 0

    def add_chunk(self, chunk: bytes) -> None:
        """Add an audio chunk to be processed."""
        if not self._stopped:
            self._chunk_count += 1
            print(f"[Riva] Audio chunk {self._chunk_count} added, {len(chunk)} bytes")
            self._queue.put(chunk)

    def stop(self) -> None:
        """Signal the iterator to stop."""
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
                    raise StopIteration
                return chunk
            except Empty:
                if self._stopped:
                    print("[Riva] Iterator stopped during wait")
                    raise StopIteration
                # Keep waiting for more chunks
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

        # ASR config for speech recognition
        asr_config = riva_asr_pb2.StreamingRecognitionConfig(
            config=riva_asr_pb2.RecognitionConfig(
                encoding=riva.client.AudioEncoding.LINEAR_PCM,
                sample_rate_hertz=audio_config.sample_rate,
                language_code=riva_config.source_language,
                max_alternatives=1,
                enable_automatic_punctuation=True,
                audio_channel_count=audio_config.channels,
            ),
            interim_results=True
        )

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
        s2s_config = self.create_s2s_config(target_language)

        def run_translation():
            """Run the blocking Riva translation in a thread."""
            print("[Riva] Starting translation thread")
            response_count = 0
            try:
                responses = self._nmt_client.streaming_s2s_response_generator(
                    audio_chunks=chunk_iterator,
                    streaming_config=s2s_config
                )

                for response in responses:
                    response_count += 1
                    if response.speech and response.speech.audio:
                        audio_len = len(response.speech.audio)
                        print(f"[Riva] Response {response_count}: got {audio_len} bytes of audio")
                        on_audio(response.speech.audio)
                    else:
                        print(f"[Riva] Response {response_count}: no audio")

                print(f"[Riva] Translation stream ended, {response_count} responses")

            except Exception as e:
                print(f"[Riva] Translation error: {e}")
                on_error(f"Translation error: {str(e)}")

        # Run translation in background thread
        self._executor.submit(run_translation)

        return chunk_iterator


# Global client instance
riva_client = RivaS2SClient()
