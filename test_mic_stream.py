#!/usr/bin/env python3
"""Test the MicrophoneStream class directly."""

import numpy as np
import sounddevice as sd
from queue import Queue, Empty

CHUNK = 4800
RATE = 16000
CHANNELS = 1
DURATION = 5

print(f"Testing MicrophoneStream class")
print(f"Recording {DURATION} seconds...")
print("-" * 50)

# Simpler approach - just use the queue directly
audio_queue = Queue()
recorded_chunks = []

def callback(indata, frames, time, status):
    if status:
        print(f"Status: {status}")
    audio_queue.put(indata.copy())

# Start recording
stream = sd.InputStream(
    samplerate=RATE,
    channels=CHANNELS,
    dtype='int16',
    blocksize=CHUNK,
    callback=callback
)

stream.start()

chunks_needed = int((DURATION * RATE) / CHUNK)
print(f"Need {chunks_needed} chunks")

for i in range(chunks_needed):
    try:
        chunk = audio_queue.get(timeout=1.0)
        recorded_chunks.append(chunk)

        # Show level
        rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))
        level = int(min(rms / 1000, 1.0) * 20)
        bar = "|" * level + " " * (20 - level)
        print(f"\rChunk {i+1}/{chunks_needed} [{bar}] rms={rms:.0f}", end="", flush=True)
    except Empty:
        print(f"\nTimeout waiting for chunk {i+1}")
        break

stream.stop()
stream.close()

print()
print("-" * 50)
print(f"Collected {len(recorded_chunks)} chunks")

if recorded_chunks:
    # Combine chunks
    all_audio = np.concatenate(recorded_chunks)
    print(f"Total samples: {len(all_audio)}")
    print(f"Shape: {all_audio.shape}")
    print(f"Dtype: {all_audio.dtype}")

    # Flatten if needed
    if len(all_audio.shape) > 1:
        all_audio = all_audio.flatten()
        print(f"Flattened to: {all_audio.shape}")

    print()
    print("Playing back...")
    sd.play(all_audio, RATE)
    sd.wait()
    print("Done.")
else:
    print("No chunks recorded!")
