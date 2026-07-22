# Real-Time Speech-to-Speech Translation with Nemotron 3

A web-based real-time speech translation application using NVIDIA Riva services. Captures English audio from your microphone, translates it, and plays back synthesized speech in the target language.

This repository preserves [Jonathan Gough's original demo](https://github.com/jgough-essextec/realtime-s2s-demo-app) and adds the Pellera/NVIDIA evaluation configuration for English-to-Spanish long-form speech. It uses Nemotron 3 streaming ASR in place of Parakeet CTC, pins all three NIM releases, measures listener backlog for sermon-length tests, and includes an experimental adaptive playback controller that preserves every translated audio chunk while trying to keep the browser queue near 5-10 seconds.

GitHub permits only one fork of a source repository per owner. Because `jspaulding-nv/realtime-s2s-demo-app` already occupies that fork slot, this clean evaluation repository retains Jonathan's full Git history as a standalone repository and records his project as the upstream source.

## What Changed

- Nemotron ASR Streaming `1.2.0` with the English `batch_size=32` profile
- Riva Translate 1.6B `1.5.2` and Magpie multilingual TTS `1.7.0`
- NVIDIA Riva Python client `2.24.0`
- Automatic ASR punctuation and an 800 ms final end-of-utterance window
- Environment-based Riva configuration instead of a hardcoded server address
- An `end_input` control message so the test harness can drain final translated audio
- First-audio latency, output/input duration ratio, service tail, and simulated listener playback-tail metrics
- Exact browser playback-queue telemetry with adaptive 1.00x, 1.05x, and 1.10x scheduling
- Queue-aware test completion: file input ends independently, then Riva output and browser playback drain
- A dashboard switch for fixed 1.00x control runs versus adaptive runs, recorded in the CSV
- Pinned, single-GPU Docker Compose deployment for ASR, NMT, and TTS

The current monolithic Riva S2S endpoint does not expose separate ASR, NMT, and TTS stage queues. Explicit punctuation-boundary splitting and bounded NMT/TTS parallelism are therefore future client-orchestration work, not claims made by this version.

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
- Queue-based audio playback with an experimental bounded-latency catch-up policy
- Audience backlog metrics and CSV telemetry in the latency dashboard
- Configurable target languages (based on Riva server capabilities)

## Project Structure

```
realtime-s2s-demo-app/
├── docker-compose.yaml     # Pinned Nemotron ASR, NMT, and TTS services
├── .env.example            # Compose and application configuration template
├── NEMOTRON_TEST_RESULTS.md
├── docs/                   # Playback, metrics, experiment, and staged-pipeline guides
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
│   │   ├── types/
│   │   │   └── messages.ts           # TypeScript types
│   │   └── utils/
│   │       └── playbackPolicy.ts      # Adaptive queue policy and summaries
│   ├── package.json
│   └── vite.config.ts
│
├── realtime_s2s.py          # Original CLI-based translation script
├── start.sh                 # Script to start both servers
└── README.md
```

## Prerequisites

- Python 3.9+
- Node.js 20.19+ or 22.12+
- Docker with NVIDIA Container Toolkit and a visible CDI GPU device
- An NGC Personal Key with access to the NGC Catalog
- An NVIDIA GPU with enough memory for all three selected profiles

The selected profiles allocate approximately 26.4 GB of GPU memory in total: 6 GB for ASR, 9.5 GB for NMT, and 10.87 GB for TTS. They fit comfortably on the tested 96 GB RTX PRO 6000 Blackwell Server Edition and should fit a 48 GB RTX 6000 Ada, though peak usage should be checked during an end-to-end stream.

## Quick Start

### 1. Configure NGC and the application

```bash
cp .env.example .env
mkdir -p .cache/nim
chmod 777 .cache/nim
```

Add an active NGC Personal Key to `.env`. Docker Compose reads that file automatically, but `docker login` runs in the shell, so export the values before authenticating:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin
```

Never commit `.env`. If login returns `unauthorized`, confirm the key is active, unexpired, and includes the NGC Catalog service.

### 2. Start the pinned Riva services

```bash
nvidia-ctk cdi list
docker compose pull
docker compose up -d
docker compose ps
```

Initial model downloads and TensorRT engine generation can take 30 minutes or more. The NMT container starts after ASR and TTS report healthy.

Verify readiness:

```bash
curl --fail http://localhost:9002/v1/health/ready  # ASR
curl --fail http://localhost:9001/v1/health/ready  # NMT
curl --fail http://localhost:9003/v1/health/ready  # TTS
nvidia-smi
```

The application connects to the NMT/S2S gRPC endpoint at `localhost:50051`. ASR and TTS are also exposed at `localhost:50052` and `localhost:50053` for direct tests.

### 3. Start the web application

```bash
./start.sh
```

This will:
- Install backend dependencies if needed
- Install frontend dependencies if needed
- Start the backend on http://localhost:8000
- Start the frontend on http://localhost:5173

### 4. Open the Web UI

Navigate to http://localhost:5173 in your browser.

### 5. Use the Application

1. Click the microphone button to start
2. Speak English into your microphone
3. Hear the translated speech through your speakers
4. Click the button again to stop

**Important:** Use headphones to prevent audio feedback!

## Manual Setup

### Backend

```bash
set -a
source .env
set +a

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

Copy `.env.example` to `.env` and set these values when the defaults do not match your deployment:

```dotenv
RIVA_URI=localhost:50051
RIVA_NMT_MODEL=megatronnmt_any_any_1b
RIVA_SOURCE_LANGUAGE=en-US
RIVA_EOU_MS=800
RIVA_VERBOSE_CHUNKS=0
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
{"type": "end_input"}
{"type": "stop_stream"}
{"type": "ping"}
```

`end_input` closes the request-audio iterator while leaving the WebSocket open so final translated audio can drain. `stop_stream` ends the session after that drain.
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

## Long-Form Latency Test

With the Riva services and backend running, execute a one-minute preflight before a sermon file:

```bash
python batch_latency_test.py --preflight
python batch_latency_test.py \
  --file test_audio/200108_SpiritandPresenceofGod.mp3 \
  --output-dir test_results_nemotron
```

Generated event CSVs and plots stay ignored because they are large. Compact summaries from the July 8, 2026 runs are versioned under `docs/results/nemotron3/`; interpretation and comparison with Jonathan's earlier runs are in `NEMOTRON_TEST_RESULTS.md`.

For a live audience, the remaining listener-visible delay matters more than server flush time. Spanish synthesized audio was still longer than the source in these runs, so this branch experiments with a 5-second catch-up target, an 8-second urgent threshold, and a 10-second soft ceiling. It schedules output at 1.00x, 1.05x, or 1.10x and never drops speech.

The 10-second value is an audience-experience objective, not a guaranteed hard cap. If translated audio is generated faster than 1.10x playback can consume it, the queue can still exceed that value. Browser queue depth also excludes the upstream time spent waiting for ASR finalization, translation, and the first TTS audio; therefore it does not by itself equal the delay between an English joke and its Spanish rendering.

A deterministic replay of the three saved Nemotron arrival traces reduced the combined fixed-rate listener tail by 82.2%, from 472.151 seconds to 84.001 seconds. It did not satisfy the queue objective: simulated peaks remained 23.8-53.1 seconds. These are offline policy projections, not new live Riva/browser runs or listening-quality results. Reproduce them when the ignored raw CSVs are present:

```bash
python analyze_playback_policy.py --input-dir test_results_nemotron
```

The compact replay report is [versioned with the Nemotron results](docs/results/nemotron3/playback_policy_analysis.md).

Detailed guides:

- [Adaptive playback controller](docs/ADAPTIVE_PLAYBACK.md)
- [Audience-latency metric definitions](docs/AUDIENCE_LATENCY_METRICS.md)
- [Bounded-playback experiment plan](docs/BOUNDED_PLAYBACK_EXPERIMENT.md)
- [Staged ASR -> NMT -> TTS design](docs/STAGED_PIPELINE_DESIGN.md)
- [Implementation and verification log](docs/IMPLEMENTATION_LOG.md)

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

### Stop the Riva services

```bash
docker compose down
```

The model cache remains under `.cache/nim`. Avoid `docker compose down --rmi all` unless removing the local images is intentional.

## Technology Stack

- **Frontend:** React, TypeScript, Vite, Tailwind CSS
- **Backend:** FastAPI, Python, WebSockets
- **Audio:** Web Audio API, AudioWorklet
- **Translation:** NVIDIA Riva (ASR + NMT + TTS)
