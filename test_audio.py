#!/usr/bin/env python3
"""Simple audio test to diagnose mic/speaker issues."""

import sounddevice as sd
import numpy as np

RATE = 16000
DURATION = 3

print("Audio Devices:")
print("=" * 50)
print(sd.query_devices())
print("=" * 50)
print()
print(f"Default input device:  {sd.default.device[0]}")
print(f"Default output device: {sd.default.device[1]}")
print()

# Test 1: Play a tone
print("Test 1: Playing a 440Hz tone for 1 second...")
t = np.linspace(0, 1, RATE)
tone = (np.sin(2 * np.pi * 440 * t) * 0.3 * 32767).astype(np.int16)
sd.play(tone, RATE)
sd.wait()
print("Did you hear a beep? (If not, check speaker/output device)")
print()

# Test 2: Record and playback
input("Test 2: Press Enter to record 3 seconds, then playback...")
print(f"Recording {DURATION} seconds... Speak now!")
recording = sd.rec(int(DURATION * RATE), samplerate=RATE, channels=1, dtype='int16')
sd.wait()
print("Recording complete.")

# Check if we got audio
rms = np.sqrt(np.mean(recording.astype(np.float32) ** 2))
print(f"Audio RMS level: {rms:.1f} (should be > 100 if you spoke)")

print("Playing back recording...")
sd.play(recording, RATE)
sd.wait()
print("Done.")
