#!/usr/bin/env python3
"""
Real-Time Speech-to-Speech Translation with NVIDIA Riva

Captures audio from your microphone, streams it through the NVIDIA Riva
ASR → NMT → TTS pipeline, and plays the translated audio through speakers.

Language pair: English (en-US) → Spanish (es-US)

Prerequisites:
- Docker containers must be running: docker compose up -d
- Use headphones to prevent audio feedback loop

Usage:
    python realtime_s2s.py           # Run real-time translation
    python realtime_s2s.py --test    # Test mic: record 10s and playback
    python realtime_s2s.py --test 5  # Test mic: record 5s and playback

Press Ctrl+C to stop.
"""

import os
import sys
import threading
import numpy as np
import sounddevice as sd
from queue import Queue
import riva.client
import riva.client.proto.riva_asr_pb2 as riva_asr_pb2
import riva.client.proto.riva_nmt_pb2 as riva_nmt_pb2

# Audio parameters
CHUNK = 4800      # samples per chunk (~300ms at 16kHz)
RATE = 16000      # sample rate in Hz
CHANNELS = 1      # mono audio

# Language settings
MODEL = 'megatronnmt_any_any_1b'
SOURCE_LANGUAGE = 'en-US'
TARGET_LANGUAGE = 'es-US'

# Riva service endpoint
NMT_URI = os.environ.get('RIVA_URI', 'localhost:50051')


class MicrophoneStream:
    """Opens a microphone stream as an iterator yielding audio chunks."""

    def __init__(self, rate, chunk_size):
        self.rate = rate
        self.chunk_size = chunk_size
        self._audio_queue = Queue()
        self._stream = None
        self._closed = False
        self._chunk_count = 0

    def __enter__(self):
        self._stream = sd.InputStream(
            samplerate=self.rate,
            channels=CHANNELS,
            dtype='int16',
            blocksize=self.chunk_size,
            callback=self._audio_callback
        )
        self._stream.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._closed = True
        if self._stream:
            self._stream.stop()
            self._stream.close()

    def _audio_callback(self, indata, frames, time, status):
        """Called by sounddevice for each audio block."""
        if status:
            print(f"Audio status: {status}")

        self._chunk_count += 1

        # Calculate audio level (RMS)
        audio_array = indata.flatten().astype(np.float32)
        rms = np.sqrt(np.mean(audio_array ** 2))
        level = int(min(rms / 1000, 1.0) * 20)  # Scale to 0-20 bars
        bar = "|" * level + " " * (20 - level)

        # Print mic input indicator (overwrite same line)
        print(f"\rMic [{bar}] chunk {self._chunk_count}", end="", flush=True)

        # Convert to bytes for Riva (must copy - indata buffer is reused)
        self._audio_queue.put(indata.copy().tobytes())

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        # Block until audio is available
        chunk = self._audio_queue.get()
        if chunk is None:
            raise StopIteration
        return chunk

    def stop(self):
        """Signal the stream to stop."""
        self._closed = True
        self._audio_queue.put(None)


def create_s2s_config():
    """Create the combined S2S configuration."""
    # ASR config for speech recognition
    asr_config = riva_asr_pb2.StreamingRecognitionConfig(
        config=riva_asr_pb2.RecognitionConfig(
            encoding=riva.client.AudioEncoding.LINEAR_PCM,
            sample_rate_hertz=RATE,
            language_code=SOURCE_LANGUAGE,
            max_alternatives=1,
            enable_automatic_punctuation=True,
            audio_channel_count=CHANNELS,
        ),
        interim_results=True
    )

    # NMT config for translation
    translation_config = riva_nmt_pb2.TranslationConfig(
        source_language_code=SOURCE_LANGUAGE,
        target_language_code=TARGET_LANGUAGE,
        model_name=MODEL
    )

    # TTS config for speech synthesis
    tts_config = riva.client.SynthesizeSpeechConfig(
        language_code=TARGET_LANGUAGE,
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hz=RATE,
        voice_name='Magpie-Multilingual.ES-US.Isabela',
    )

    # Combined S2S config
    return riva.client.StreamingTranslateSpeechToSpeechConfig(
        asr_config=asr_config,
        translation_config=translation_config,
        tts_config=tts_config,
    )


class AudioPlayer:
    """Continuous audio output stream that plays from a queue."""

    def __init__(self, rate):
        self.rate = rate
        self.queue = Queue()
        self.buffer = np.array([], dtype=np.int16)
        self.stream = None
        self.chunk_count = 0
        self.lock = threading.Lock()

    def start(self):
        self.stream = sd.OutputStream(
            samplerate=self.rate,
            channels=1,
            dtype='int16',
            callback=self._callback,
            blocksize=1024,
        )
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def _callback(self, outdata, frames, time, status):
        """Called by sounddevice to fill output buffer."""
        with self.lock:
            # If we have queued chunks, add them to buffer
            while not self.queue.empty():
                try:
                    chunk = self.queue.get_nowait()
                    self.buffer = np.concatenate([self.buffer, chunk])
                except:
                    break

            # Fill output from buffer
            if len(self.buffer) >= frames:
                outdata[:, 0] = self.buffer[:frames]
                self.buffer = self.buffer[frames:]
            else:
                # Not enough data - play what we have, fill rest with silence
                outdata[:len(self.buffer), 0] = self.buffer
                outdata[len(self.buffer):, 0] = 0
                self.buffer = np.array([], dtype=np.int16)

    def play(self, audio_data):
        """Queue audio data for playback."""
        self.chunk_count += 1
        self.queue.put(audio_data)


def run_realtime_translation():
    """Captures mic audio, translates, and plays through speakers in real-time."""

    # Connect to NMT service
    print(f"Connecting to Riva NMT service at {NMT_URI}...")
    nmt_auth = riva.client.Auth(uri=NMT_URI)
    nmt_client = riva.client.NeuralMachineTranslationClient(nmt_auth)

    # Create configuration
    s2s_config = create_s2s_config()

    # Create continuous audio player
    player = AudioPlayer(RATE)
    player.start()

    print()
    print("=" * 50)
    print("Real-Time Speech-to-Speech Translation")
    print("=" * 50)
    print(f"Source: {SOURCE_LANGUAGE}")
    print(f"Target: {TARGET_LANGUAGE}")
    print(f"Model:  {MODEL}")
    print("=" * 50)
    print()
    print("Speak English into your microphone.")
    print("Spanish translation will play through speakers.")
    print("Press Ctrl+C to stop.")
    print()
    print("-" * 50)

    try:
        with MicrophoneStream(RATE, CHUNK) as mic_stream:
            # Create the streaming S2S response generator
            responses = nmt_client.streaming_s2s_response_generator(
                audio_chunks=mic_stream,
                streaming_config=s2s_config
            )

            for response in responses:
                # Check if we have audio data
                if response.speech and response.speech.audio:
                    audio_data = np.frombuffer(response.speech.audio, dtype=np.int16)

                    if len(audio_data) > 0:
                        print(f"\n>> Playing chunk {player.chunk_count + 1} ({len(audio_data)} samples)")
                        player.play(audio_data)

    except KeyboardInterrupt:
        print()
        print("-" * 50)
        print("Stopped by user.")
    finally:
        # Wait a moment for remaining audio to play
        import time
        time.sleep(0.5)
        player.stop()

    print(f"Total chunks played: {player.chunk_count}")


def test_microphone(duration=10):
    """Record audio for specified duration and play it back."""
    print(f"Recording {duration} seconds of audio...")
    print("Speak into your microphone.")
    print("-" * 50)

    recorded_chunks = []

    with MicrophoneStream(RATE, CHUNK) as mic_stream:
        # Calculate how many chunks we need for the duration
        chunks_needed = int((duration * RATE) / CHUNK)

        for i, chunk in enumerate(mic_stream):
            recorded_chunks.append(chunk)
            if i >= chunks_needed - 1:
                break

    print()
    print("-" * 50)
    print(f"Recorded {len(recorded_chunks)} chunks")

    # Debug: check first chunk
    if recorded_chunks:
        print(f"First chunk type: {type(recorded_chunks[0])}")
        print(f"First chunk size: {len(recorded_chunks[0])} bytes")

    # Combine all chunks
    all_audio = b''.join(recorded_chunks)
    audio_array = np.frombuffer(all_audio, dtype=np.int16)

    print(f"Total bytes: {len(all_audio)}")
    print(f"Total samples: {len(audio_array)}")
    print(f"Duration: {len(audio_array) / RATE:.2f} seconds")
    print(f"Array dtype: {audio_array.dtype}")
    print(f"Array min/max: {audio_array.min()} / {audio_array.max()}")
    print()
    print("Playing back recorded audio...")
    print("-" * 50)

    # Ensure array is contiguous
    audio_array = np.ascontiguousarray(audio_array)
    sd.play(audio_array, RATE)
    sd.wait()

    print("Playback complete.")


def test_translation(duration=5):
    """Record audio, translate it, and play back Spanish."""
    print(f"Recording {duration} seconds of English audio...")
    print("Speak English into your microphone.")
    print("-" * 50)

    recorded_chunks = []

    with MicrophoneStream(RATE, CHUNK) as mic_stream:
        chunks_needed = int((duration * RATE) / CHUNK)
        for i, chunk in enumerate(mic_stream):
            recorded_chunks.append(chunk)
            if i >= chunks_needed - 1:
                break

    print()
    print("-" * 50)
    print(f"Recorded {len(recorded_chunks)} chunks")

    # Connect to Riva
    print(f"Connecting to Riva at {NMT_URI}...")
    nmt_auth = riva.client.Auth(uri=NMT_URI)
    nmt_client = riva.client.NeuralMachineTranslationClient(nmt_auth)

    # Create config
    s2s_config = create_s2s_config()

    # Create an iterator from our recorded chunks
    def chunk_iterator():
        for chunk in recorded_chunks:
            yield chunk

    print("Sending to Riva for translation...")
    print("-" * 50)

    # Get responses
    responses = nmt_client.streaming_s2s_response_generator(
        audio_chunks=chunk_iterator(),
        streaming_config=s2s_config
    )

    # Collect translated audio
    translated_audio = []
    for i, response in enumerate(responses):
        print(f"Response {i}: speech={response.speech is not None}, ", end="")
        if response.speech:
            print(f"audio_len={len(response.speech.audio)}")
            if response.speech.audio:
                audio_data = np.frombuffer(response.speech.audio, dtype=np.int16)
                translated_audio.append(audio_data)
        else:
            print("no speech")

    print("-" * 50)
    print(f"Received {len(translated_audio)} audio chunks from Riva")

    if translated_audio:
        all_spanish = np.concatenate(translated_audio)
        print(f"Total Spanish samples: {len(all_spanish)}")
        print(f"Duration: {len(all_spanish) / RATE:.2f} seconds")
        print()
        print("Playing Spanish translation...")
        sd.play(all_spanish, RATE)
        sd.wait()
        print("Done.")
    else:
        print("No translated audio received!")


def main():
    if len(sys.argv) > 1:
        duration = 5
        if len(sys.argv) > 2:
            try:
                duration = int(sys.argv[2])
            except ValueError:
                pass

        if sys.argv[1] == "--test":
            test_microphone(duration)
        elif sys.argv[1] == "--translate":
            test_translation(duration)
        else:
            print("Usage:")
            print("  python realtime_s2s.py              # Real-time translation")
            print("  python realtime_s2s.py --test [N]   # Test mic (record N sec, playback)")
            print("  python realtime_s2s.py --translate [N]  # Test translation (record N sec)")
    else:
        try:
            run_realtime_translation()
        except Exception as e:
            print(f"Error: {e}")
            raise


if __name__ == "__main__":
    main()
