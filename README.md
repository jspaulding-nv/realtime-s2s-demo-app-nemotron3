# Real-Time Speech-to-Speech Translation Web Demo

A web-based real-time speech translation application using NVIDIA Riva services. Captures English audio from your microphone, translates it, and plays back synthesized speech in the target language.

## Architecture

```
┌─────────────────────┐     WebSocket      ┌─────────────────────┐     gRPC      ┌─────────────────┐
│   React Frontend    │◄──────────────────►│   FastAPI Backend   │◄────────────►│   NVIDIA Riva   │
│  (Vite + Tailwind)  │  Binary + JSON     │   (WebSocket API)   │  Streaming   │   Services      │
└─────────────────────┘                    └─────────────────────┘              └─────────────────┘
```

**Audio Flow:** Browser Mic → Int16 PCM → WebSocket → Riva S2S Pipeline → Translated Audio → WebSocket → Browser Playback

## Features

- Real-time speech-to-speech translation
- Web Audio API microphone capture at 16kHz
- WebSocket streaming for low-latency communication
- Audio level visualization
- Queue-based audio playback to prevent gaps
- Configurable target languages (based on Riva server capabilities)

## Project Structure

```
s2s-streaming/
├── backend/
│   ├── main.py              # FastAPI app + WebSocket endpoint
│   ├── config.py            # Settings (Riva URI, audio params, languages)
│   ├── riva_client.py       # Riva S2S wrapper
│   ├── websocket_handler.py # Session management
│   ├── audio_processor.py   # Audio format utilities
│   └── requirements.txt
│
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── TranslationPanel.tsx  # Main UI container
│   │   │   ├── ControlButton.tsx     # Start/Stop mic button
│   │   │   ├── LanguageSelector.tsx  # Target language dropdown
│   │   │   ├── StatusIndicator.tsx   # Connection status
│   │   │   └── AudioVisualizer.tsx   # RMS-based level bars
│   │   ├── hooks/
│   │   │   ├── useWebSocket.ts       # WebSocket connection
│   │   │   ├── useAudioCapture.ts    # Mic capture via Web Audio API
│   │   │   └── useAudioPlayback.ts   # Translated audio playback
│   │   └── types/
│   │       └── messages.ts           # TypeScript types
│   ├── package.json
│   └── vite.config.ts
│
├── realtime_s2s.py          # Original CLI-based translation script
├── start.sh                 # Script to start both servers
└── README.md
```

## Prerequisites

- Python 3.9+
- Node.js 18+
- NVIDIA Riva server running with:
  - ASR (Automatic Speech Recognition) for English
  - NMT (Neural Machine Translation) model
  - TTS (Text-to-Speech) voices for target languages

## Quick Start

### 1. Start Both Servers

```bash
./start.sh
```

This will:
- Install backend dependencies if needed
- Install frontend dependencies if needed
- Start the backend on http://localhost:8000
- Start the frontend on http://localhost:5173

### 2. Open the Web UI

Navigate to http://localhost:5173 in your browser.

### 3. Use the Application

1. Click the microphone button to start
2. Speak English into your microphone
3. Hear the translated speech through your speakers
4. Click the button again to stop

**Important:** Use headphones to prevent audio feedback!

## Manual Setup

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Configuration

### Riva Server

Edit `backend/config.py` to configure your Riva server:

```python
@dataclass
class RivaConfig:
    uri: str = "riva-host:50051"  # Your Riva server address
    model: str = "megatronnmt_any_any_1b"
    source_language: str = "en-US"
```

### Adding Languages

To add more target languages, you need:
1. The TTS voice installed on your Riva server
2. Add the language to `backend/config.py`:

```python
SUPPORTED_LANGUAGES: Dict[str, dict] = {
    "es-US": {
        "name": "Spanish (US)",
        "voice": "Magpie-Multilingual.ES-US.Isabela",
        "available": True,
    },
    # Add more languages here:
    "fr-FR": {
        "name": "French",
        "voice": "Your-French-Voice-Name",
        "available": True,
    },
}
```

### Audio Configuration

The audio settings in `backend/config.py` should match your Riva server:

```python
@dataclass
class AudioConfig:
    sample_rate: int = 16000      # 16kHz
    chunk_size: int = 4800        # ~300ms chunks
    channels: int = 1             # Mono
    bytes_per_sample: int = 2     # Int16
```

## API Endpoints

### REST

- `GET /` - Health check
- `GET /api/languages` - List available target languages
- `GET /api/config` - Get audio configuration

### WebSocket

- `WS /ws/translate` - Real-time translation stream

**WebSocket Messages:**

Client → Server:
```json
{"type": "start_stream", "targetLanguage": "es-US"}
{"type": "stop_stream"}
{"type": "ping"}
```
Plus binary audio frames (Int16 PCM)

Server → Client:
```json
{"type": "status", "status": "listening", "message": "..."}
{"type": "error", "message": "..."}
{"type": "level", "rms": 0.5}
{"type": "pong"}
```
Plus binary audio frames (Int16 PCM translated audio)

## Original CLI Script

The original command-line translation script is still available:

```bash
source venv/bin/activate

# Real-time translation
python realtime_s2s.py

# Test microphone (record and playback)
python realtime_s2s.py --test 5

# Test translation pipeline
python realtime_s2s.py --translate 5
```

## Troubleshooting

### No audio output
- Check browser console for errors
- Verify Riva server is running and accessible
- Check backend logs for `[Riva] Response N: got X bytes of audio`

### WebSocket connection errors
- Ensure backend is running on port 8000
- Check that Vite proxy is configured correctly

### Translation not working for a language
- The TTS voice for that language may not be installed on your Riva server
- Check `backend/config.py` for correct voice names

## Technology Stack

- **Frontend:** React, TypeScript, Vite, Tailwind CSS
- **Backend:** FastAPI, Python, WebSockets
- **Audio:** Web Audio API, AudioWorklet
- **Translation:** NVIDIA Riva (ASR + NMT + TTS)
