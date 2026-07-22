# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Real-time speech-to-speech translation application using NVIDIA Riva services. Captures English audio from microphone, translates to Spanish, and plays back synthesized speech.

## Commands

```bash
# Activate virtual environment
source venv/bin/activate

# Run real-time translation
python realtime_s2s.py

# Test microphone (record and playback)
python realtime_s2s.py --test          # 10 second test
python realtime_s2s.py --test 5        # custom duration

# Test translation pipeline
python realtime_s2s.py --translate     # 5 second test
python realtime_s2s.py --translate 10  # custom duration

# Diagnostic tools
python test_audio.py                   # Test audio devices
python test_mic_stream.py              # Test microphone stream class
```

## Architecture

### Translation Pipeline
```
Microphone → Riva ASR (en-US) → NMT → TTS (es-US) → Speakers
```

All Riva services are accessed through the gRPC endpoint configured by
`RIVA_URI` (default: `localhost:50051`).

### Key Classes (realtime_s2s.py)

**MicrophoneStream**: Iterator yielding audio chunks from sounddevice callback
- Context manager for stream lifecycle
- 4800 samples/chunk (300ms at 16kHz), mono, int16

**AudioPlayer**: Continuous audio output with queue-based buffering
- Non-blocking playback via threading
- Prevents audio underruns during translation latency

### Audio Configuration
- Sample rate: 16,000 Hz
- Chunk size: 4800 samples
- Format: mono int16
- Translation model: `megatronnmt_any_any_1b`
- TTS voice: `Magpie-Multilingual.ES-US.Isabela`

## Ubuntu Setup

```bash
# Install PortAudio (required for sounddevice)
sudo apt-get install -y portaudio19-dev python3-dev

# Create and activate venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Notes

- Use headphones to prevent audio feedback loop
- Riva services must be running (Docker containers)
- RMS level bars display in terminal during recording
