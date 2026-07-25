# Real-Time Speech-to-Speech Translation with Nemotron 3

A web-based real-time speech translation application using NVIDIA Riva services. Captures English audio from your microphone, translates it, and plays back synthesized speech in the target language.

This repository preserves [@jgough-essextec's original demo](https://github.com/jgough-essextec/realtime-s2s-demo-app) and adds the Riva evaluation configuration for English-to-Spanish long-form speech. It uses Nemotron 3 streaming ASR in place of Parakeet CTC, pins all three NIM releases, measures listener backlog for sample-length tests, and includes an experimental adaptive playback controller that preserves every translated audio chunk while trying to keep the browser queue near 5-10 seconds.

GitHub permits only one fork of a source repository per owner. Because `jspaulding-nv/realtime-s2s-demo-app` already occupies that fork slot, this clean evaluation repository retains the sanitized upstream history as a standalone repository and records that project as the upstream source.

The five upstream-published source recordings are bundled byte-for-byte under
neutral filenames so a fresh clone can reproduce the evaluation. Generated
audio and raw runtime captures remain excluded from Git.
See the [sanitization policy](docs/SANITIZATION.md) and
[audio-fixture instructions](test_audio/README.md) before running evaluations.

## What Changed

- Nemotron ASR Streaming `1.2.0` with the English `batch_size=32` profile
- Riva Translate 1.6B `1.5.2` and Magpie multilingual TTS `1.7.0`
- NVIDIA Riva Python client `2.24.0`
- Automatic ASR punctuation and an 800 ms final end-of-utterance window
- Environment-based Riva configuration instead of a hardcoded server address
- An `end_input` control message so the test harness can drain final translated audio
- A terminal `completed` status only after all source input, Riva output, and pending PCM sends finish
- First-audio latency, output/input duration ratio, service tail, and simulated listener playback-tail metrics
- Exact browser playback-queue telemetry with adaptive 1.00x, 1.05x, and 1.10x scheduling
- Queue-aware test completion: file input ends independently, then Riva output and browser playback drain
- A dashboard switch for fixed 1.00x control runs versus adaptive runs, recorded in the CSV
- A resumable one-command harness for sequential matched-policy runs across all three samples
- Optional no-drop constant-rate/media-duration sweeps and wall-clock burst diagnostics over saved arrival traces
- A fail-closed schema-v3 trace joiner and lossy whole-parent freshness simulator at 5-, 8-, and 10-second queue caps
- A privacy-safe TTS duration analyzer that sizes post-NMT subsegment experiments from character counts and PCM duration
- A default-off atomic TTS response-cadence diagnostic and source-boundary latency analyzer
- Default-off schema-v3 incremental TTS publication with 100 ms PCM framing,
  bounded backpressure, pre-commit-only retry, and a tiny-target atomic
  reliability fallback
- A matched atomic-versus-incremental canary with same-audio publication timing and explicit stochastic-output confounding checks
- Opt-in audio metadata protocol v1 for strict parent/frame observation,
  source-offset freshness, and observation-only shadow-policy replay
- Direct Nemotron ASR, Riva NMT, and Magpie TTS adapters with strict validation
- A bounded ordered staged orchestrator that overlaps NMT and TTS, drains exactly, and records per-stage telemetry
- Default-off staged `/ws/translate` integration with ordered PCM sends and retained sequence telemetry
- Browser acceptance that requires server completion as well as an empty Web Audio queue
- Standalone hesitation-filler suppression before sequence allocation, narrow known-short-utterance overrides, fail-closed target-script validation, and one guarded punctuation-normalized NMT recovery before TTS
- One atomic Magpie retry only for a server-side gRPC `UNKNOWN`, with privacy-safe retry telemetry and no partial-audio publication
- A repeatable one-minute direct ASR -> NMT -> TTS preflight tool
- A privacy-safe short-segment replay tool that retains structural metadata and per-run keyed equality fingerprints, never text
- Pinned, single-GPU Docker Compose deployment for ASR, NMT, and TTS

The safe default browser path still uses the monolithic Riva S2S operation.
Set `S2S_PIPELINE_MODE=staged` and restart FastAPI to route the same WebSocket
protocol through the direct bounded pipeline. The staged path has completed a
clean, preflight-gated real-time matrix over all three long-form samples:
2,027/2,027 ordered segments, three recovered NMT retries, no TTS retries, no
missing work, and no runtime failure. A separate immediate post-run check found
all three pinned NIMs healthy on the 96 GB RTX PRO 6000. This is an operational
pass, not an audience-latency pass. The no-drop 1.00x/1.05x/1.10x simulation
reduced summed listener tail by 77.7%, but per-sample queue p95 remained
23-61 seconds. Actual browser queue, marked-phrase delay, and native-listener
speed/quality gates remain open before the staged route should be treated as
the preferred live path.

The first provenance-frozen matrix passed its preflight, then stopped on
Sample 01 when Magpie returned an internal zero-token tensor error for a very
short, validated target. ASR, NMT, queues, GPU capacity, and service health
were ruled out. A privacy-safe production-style replay reproduced the exact
3-to-2-character shape and the same TTS failure on one of five raw calls.
With the bounded retry enabled, all 20 subsequent calls completed and two
reported a successful retry. The subsequent clean three-sample matrix completed
without a TTS fault or retry. That matrix validates long-form compatibility
with the recovery enabled; the targeted probe, not the matrix, is the evidence
that directly exercised successful TTS recovery.

Transcript-free structural telemetry from the clean matrix produced 2,027
character-count/PCM-duration pairs. A leave-one-sample-out model places the
4-second-p95 and 8-second observed-max-residual limits at 44 and 46 translated
characters. The strict five-character grid therefore starts at 40 characters,
but this is not yet a production setting: the same fit predicts substantial
call amplification and possible fixed-duration overhead. The next live canary
must compare unsplit, 40-, 45-, and 60-character post-NMT TTS policies while
measuring both burst size and total output expansion.

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
├── test_audio/              # Bundled source fixtures under neutral filenames
├── docs/                   # Playback, metrics, experiment, and staged-pipeline guides
│   └── SANITIZATION.md     # Public-data and evidence policy
├── backend/
│   ├── main.py              # FastAPI app + WebSocket endpoint
│   ├── config.py            # Settings (Riva URI, audio params, languages)
│   ├── riva_client.py       # Riva S2S wrapper
│   ├── direct_asr_client.py # Direct staged Nemotron adapter
│   ├── direct_nmt_client.py # Validated one-segment NMT adapter
│   ├── direct_tts_client.py # Atomic/incremental Magpie PCM adapter
│   ├── punctuation_segmenter.py # Ordered final-text segmentation
│   ├── staged_pipeline.py   # Bounded ordered stage orchestration
│   ├── staged_models.py     # Staged records and telemetry contract
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
├── direct_asr_smoke.py      # Opt-in direct Nemotron compatibility smoke
├── direct_asr_bridge_smoke.py # Opt-in bounded DirectASRStream smoke
├── staged_pipeline_smoke.py # Opt-in direct ASR -> NMT -> TTS preflight
├── diagnose_short_segment.py # Privacy-safe isolated/context replay
├── analyze_tts_duration.py # Transcript-free TTS duration/capacity model
├── analyze_streaming_latency.py # Source-boundary and TTS response-cadence analysis
├── analyze_freshness_cap.py # Parent-aware lossy queue counterfactual
├── freshness_trace.py      # Fail-closed schema-v3 evidence join
├── playback_simulation.py  # No-drop and whole-parent queue simulators
├── run_streaming_tts_canary.sh # Matched atomic/schema-v3 live canary
├── summarize_streaming_tts_canary.py # Privacy-safe matched comparison
├── run_long_form_experiment.py # Resumable long-form matched-trace harness
├── start.sh                 # Script to start both servers
└── README.md
```

## Prerequisites

- Python 3.10+
- Node.js 20.19+ or 22.12+
- Docker with NVIDIA Container Toolkit and a visible CDI GPU device
- An NGC Personal Key with access to the NGC Catalog
- An NVIDIA GPU with enough memory for all three selected profiles

The selected profiles allocate approximately 26.4 GB of GPU memory in total: 6 GB for ASR, 9.5 GB for NMT, and 10.87 GB for TTS. They fit comfortably on the tested 96 GB RTX PRO 6000 Blackwell Server Edition and should fit a 48 GB RTX 6000 Ada, though peak usage should be checked during an end-to-end stream.

## Quick Start

### 1. Configure NGC and the application

```bash
cp .env.example .env
mkdir -p .cache/nim test_audio
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

The repository includes the five evaluation fixtures documented in
`test_audio/README.md`. Verify their exact bytes before a comparison run:

```bash
(cd test_audio && sha256sum --check SHA256SUMS)
```

To use different consent-cleared inputs, set `S2S_TEST_AUDIO_DIR` to a private
directory containing the same neutral filenames. Additional recordings,
generated audio, and raw result directories remain ignored and must not be
force-added.

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

After a host reboot, Compose's `unless-stopped` policy should restart the three
NIM containers, but readiness must still be verified with the commands above.
The FastAPI backend is not part of this Compose project and does not restart
unless the operator manages it separately. Relaunch it, verify `/` and
`/api/config`, and rerun the one-minute preflight before any long-form capture.
This behavior was observed after the July 24 VM restart; see the
[Sample 02 recovery canary](docs/STAGED_SAMPLE_02_RECOVERY_CANARY.md).

Optional: validate all three direct services and the bounded staged drain with
a real-time one-minute WAV before starting the browser application:

```bash
python3 staged_pipeline_smoke.py \
  --file "${S2S_TEST_AUDIO_DIR:-test_audio}/preflight.wav" \
  --duration-seconds 60
```

This writes ignored raw PCM and JSON telemetry under `test_results_staged/`.
It exercises the direct pipeline without the WebSocket or browser.

To opt into that staged pipeline through `/ws/translate`, set the following in
`.env` and restart the backend:

```dotenv
S2S_PIPELINE_MODE=staged
```

Confirm the selected mode before recording a result:

```bash
curl --fail --silent http://localhost:8000/api/config | python3 -m json.tool
```

See [the staged WebSocket integration guide](docs/STAGED_WEBSOCKET_INTEGRATION.md)
for the terminal-aware one-minute gate, retained telemetry, rollback, and
full-sample promotion order.

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

### Tests

```bash
python3 -m pip install -r requirements-dev.txt
PYTHONPATH=backend:. python3 -m pytest -p no:cacheprovider -q \
  backend/tests tests
```

## Configuration

### Riva Server

Copy `.env.example` to `.env` and set these values when the defaults do not match your deployment:

```dotenv
RIVA_URI=localhost:50051
RIVA_ASR_URI=localhost:50052
RIVA_TTS_URI=localhost:50053
RIVA_NMT_MODEL=megatronnmt_any_any_1b
RIVA_SOURCE_LANGUAGE=en-US
RIVA_EOU_MS=800
RIVA_ASR_WORD_TIMES=0
RIVA_VERBOSE_CHUNKS=0
STAGED_SEGMENT_MAX_CHARS=240
STAGED_SEGMENT_MAX_AGE_MS=2000
STAGED_ASR_EVENT_QUEUE_MAXSIZE=32
STAGED_NMT_QUEUE_MAXSIZE=4
STAGED_TTS_QUEUE_MAXSIZE=4
STAGED_OUTPUT_QUEUE_MAXSIZE=4
STAGED_NMT_RPC_TIMEOUT_SECONDS=15
STAGED_TTS_RPC_TIMEOUT_SECONDS=60
STAGED_TTS_MAX_SEGMENT_AUDIO_SECONDS=60
STAGED_TTS_MAX_RETRIES=1
STAGED_TTS_RESPONSE_CHUNK_TELEMETRY=0
STAGED_TTS_INCREMENTAL_PUBLISH=0
STAGED_TTS_INCREMENTAL_FRAME_MS=100
STAGED_TTS_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS=4
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
STAGED_TTS_SUBSEGMENT_MIN_CHARS=12
STAGED_CLOSE_TIMEOUT_SECONDS=10
S2S_PIPELINE_MODE=monolithic
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
- `GET /api/config` - Get audio configuration, active pipeline mode, staged limits, declared ASR/NMT/TTS model provenance, and supported audio-metadata protocol versions
- `GET /api/test/export` - Get timing events plus retained staged pipeline evidence

### WebSocket

- `WS /ws/translate` - Real-time translation stream

**WebSocket Messages:**

Client → Server:
```json
{"type": "start_stream", "targetLanguage": "es-US"}
{"type": "start_stream", "targetLanguage": "es-US", "audioMetadataProtocolVersion": 1}
{"type": "end_input"}
{"type": "stop_stream"}
{"type": "ping"}
```

`end_input` closes the request-audio iterator while leaving the WebSocket open
so final translated audio can drain. The server sends `completed` only after
the request iterator consumes its stop sentinel, the Riva response generator
exhausts successfully, and every pending PCM WebSocket send finishes. Consuming
the sentinel proves all queued source chunks were read. If ASR endpointing ends
a generator sooner, the client restarts it to drain the remaining input.
Generator/final-flush errors or audio-send failures emit `error` instead;
`stop_stream` then ends the session.

In staged mode, the same terminal contract additionally requires all ASR, NMT,
and TTS workers to close cleanly, no emitted sequence to remain incomplete, and
dequeued sequence IDs to match successful WebSocket sends exactly.
Plus binary audio frames (Int16 PCM)

Server → Client:
```json
{"type": "status", "status": "listening", "message": "..."}
{"type": "status", "status": "completed", "message": "Riva translated-audio stream complete"}
{"type": "error", "message": "..."}
{"type": "level", "rms": 0.5}
{"type": "pong"}
```
Plus binary audio frames (Int16 PCM translated audio)

The second `start_stream` form opts into observation-only protocol v1. It is
available only for the staged schema-3 incremental-TTS path. In that mode, an
exact `audio_frame` JSON header immediately precedes every binary PCM frame,
and `audio_parent_complete` reconciles each parent before the next one begins.
Omitting the field preserves the legacy wire format. The metadata contains
only numeric identity, PCM format, and nullable source-media offsets; it does
not contain transcript or translation text. See
[Audio metadata observation protocol v1](docs/AUDIO_METADATA_OBSERVATION_V1.md).

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

Install the root test dependencies once:

```bash
pip install -r requirements.txt
```

The root requirements pin the harness-specific `websockets==15.0.1`,
`matplotlib==3.10.9`, and `imageio-ffmpeg==0.6.0` dependencies.

With the pinned Riva services and FastAPI backend already running, the
three-sample experiment can then be launched with one command:

```bash
python run_long_form_experiment.py
```

To make the all-three run negotiate observation-only parent/frame metadata,
use the schema-3 incremental backend and add:

```bash
python run_long_form_experiment.py --audio-metadata-protocol-v1
```

The harness performs its health checks and one-minute preflight, then streams
Sample 01, Sample 02, and Sample 03 sequentially at real-time pace. Use a dry run to
validate the plan without calling the backend, request repeated live traces, or
resume an interrupted experiment directory:

```bash
python run_long_form_experiment.py --dry-run
python run_long_form_experiment.py --repeats 3
python run_long_form_experiment.py --resume-dir experiment_results/<run-id>
```

`--dry-run` prints the resolved run directory, backend, preflight setting,
repeat count, files, and execution order without contacting the backend or
creating a run directory. A new live run is stored under the ignored
`experiment_results/` root:

```text
experiment_results/YYYYMMDDTHHMMSSZ_<git-short-sha>/
├── manifest.json
├── playback_policy_analysis.json
├── playback_policy_analysis.md
├── preflight/
│   ├── preflight_results.csv
│   ├── preflight_summary.json
│   └── preflight_latency.png
└── repeat-01/
    ├── <sample-stem>_results.csv
    ├── <sample-stem>_summary.json
    └── <sample-stem>_latency.png
```

Each `repeat-NN` directory contains that three-file set for each of the three
sample stems. The manifest is also the resumable status/checkpoint record.

`--resume-dir` must name an existing compatible harness run. Resume requires
the same clean Git commit and unchanged sample files, verified by size and
SHA-256. It also revalidates each artifact's manifest hash and its CSV event
counts and received-byte total against the summary before skipping a completed
capture. Backend and repeat settings are recovered from the manifest; explicit
values must match. `--output-root`, `--run-id`, and `--skip-preflight` control
new runs, and `--skip-preflight` cannot alter a resumed run.

The manifest also freezes the audio-metadata protocol choice. A protocol-v1
run resumes in v1 automatically when the flag is omitted; supplying the flag
is accepted only when that manifest already records v1. A legacy run cannot be
upgraded during resume.

New runs also freeze the complete `/api/config.modelConfig` snapshot: ASR,
NMT, and TTS endpoints, image references, optional digests and profiles, model
and voice, source/target languages, ASR EOU, and word-time setting. Every
promoted capture must match it. These values are supplied by the backend's
environment; the harness does not inspect the Docker daemon. For immutable
provenance, use digest-qualified image references, populate and independently
verify the digest fields, and keep the resulting manifest. Older manifests
without `modelConfig` are historical evidence and intentionally cannot resume
under the stricter schema.

Commit the intended code and start from a clean worktree if the run may need to
be resumed; a run whose original manifest records a dirty worktree is
intentionally not resumable.

Captures are written under `.staging` and promoted only after their CSV,
summary, and plot validate; their SHA-256 hashes are then stored in the
manifest. If a capture fails after those generated files exist, the harness
retains only the CSV, summary, and plot under an ignored owner-private
`failures` directory, gives them neutral filenames, and records their relative
paths and hashes under `failure_artifacts`. It does not copy source or generated
audio into that record.

The CLI waits for the backend's terminal `completed` status rather than
treating five seconds of silence as success. Failure to receive that status
within the 300-second drain maximum fails the capture. A Riva
generator/final-flush error or any pending PCM WebSocket send failure also
emits `error` and invalidates the capture. In staged mode, the client waits
through a bounded export-settling interval for the finalized `closed` pipeline
snapshot. Staged validation also rejects a completion before `end_input`, PCM
after completion, an unclosed staged snapshot, inconsistent NMT retry totals,
or any frame-count/byte mismatch between successful server sends and client
receives. The summary records completed-terminal arrival lag separately from
the longer harness polling/settle observation. A backend-keyed local file lock
prevents two harness processes on the same machine from using the
single-session backend concurrently.

For a direct observation-only schema-3 capture, add
`--audio-metadata-protocol-v1` to `batch_latency_test.py`. The harness first
requires `/api/config.audioMetadataProtocolVersions` to advertise version 1,
then fails closed on any header/binary, generation, parent, frame, byte, or
terminal mismatch. Its source-end latency uses a client-monotonic input
sample-zero marker and is explicitly labeled non-semantic when ASR supplies
only the `audio_processed` fallback offset.

One repeat represents one live Riva pass through each sample and contains
about 103.7 minutes (roughly 1 hour 45 minutes) of source audio. A new
experiment also adds a single one-minute preflight, and every capture adds its
tail drain time. Runs are intentionally sequential: the backend has one active
S2S session and one global timing logger.

The harness does not start or stop Docker Compose or the FastAPI backend; it
checks the services that are already running and leaves them in their original
state. Queue-SLA misses are recorded in the reports but do not make an
otherwise complete experiment an operational failure.

Each live translated-audio arrival trace is replayed through both the fixed
1.00x and adaptive 1.00x/1.05x/1.10x policies. This is a matched comparison:
both policies see identical audio bytes and arrival timing, so playback policy
is the only difference and a second Riva inference run is unnecessary. The
result is still a deterministic Python simulation of browser scheduling. It is
not an actual browser/Web Audio run, a native-Spanish-listener quality result,
or a measurement of semantic delay from an English joke to its Spanish
punchline. Those validations remain separate manual or browser-instrumented
experiments.

Candidate queue p95 is the exact time-weighted p95 over the simulated playback
window, not a percentile sampled only at chunk arrivals. The reports also show
exact time above 10 seconds and the longest continuous interval scheduled at
1.10x.

Generated event CSVs, plots, logs, and derived audio stay ignored because they
can contain identifying metadata in addition to being large. The five bundled
source fixtures are the explicit exception described in
`test_audio/README.md`. Sanitized aggregate interpretation and comparison with
@jgough-essextec's earlier runs are in `NEMOTRON_TEST_RESULTS.md`.

For a live audience, the remaining listener-visible delay matters more than server flush time. Spanish synthesized audio was still longer than the source in these runs, so this branch experiments with a 5-second catch-up target, an 8-second urgent threshold, and a 10-second soft ceiling. It schedules output at 1.00x, 1.05x, or 1.10x and never drops speech.

The 10-second value is an audience-experience objective, not a guaranteed hard cap. If translated audio is generated faster than 1.10x playback can consume it, the queue can still exceed that value. Browser queue depth also excludes the upstream time spent waiting for ASR finalization, translation, and the first TTS audio; therefore it does not by itself equal the delay between an English joke and its Spanish rendering.

A deterministic replay of the three saved Nemotron arrival traces reduced the combined fixed-rate listener tail by 82.3%, from 471.803 seconds to 83.654 seconds. It did not satisfy the queue objective: simulated peaks remained 23.8-53.1 seconds. These are offline policy projections, not new live Riva/browser runs or listening-quality results. Reproduce them when the ignored raw CSVs are present:

```bash
python analyze_playback_policy.py --input-dir test_results_nemotron
```

Sanitized aggregate replay findings are retained in
[`NEMOTRON_TEST_RESULTS.md`](NEMOTRON_TEST_RESULTS.md); raw reports remain in
ignored local output directories.

When the ignored five-minute evidence is present (it is not part of a fresh
clone), the schema-v3 canary can also be replayed through explicit lossy
whole-parent policies:

```bash
python3 analyze_freshness_cap.py \
  --results-csv \
    experiment_results/streaming-tts-canary-20260724T232158Z-29cdf4e/streaming/shared-prefix_results.csv \
  --summary-json \
    experiment_results/streaming-tts-canary-20260724T232158Z-29cdf4e/streaming/shared-prefix_summary.json
```

For a protocol-v1 capture, the loader replays the explicit
`audio_frame`/PCM/`audio_parent_complete` wire order and reconciles it with
server and CSV evidence. Older schema-v3 captures retain the positional join,
but only after every parent, frame, byte, receive-order, timestamp, completion,
and input-boundary invariant passes. The simulation is deliberately lossy and
offline; it does not change browser playback. Primary results use a 100 ms
cancellation guard and include 0/50/100/250 ms sensitivity. On the saved
five-minute trace, the 10-second oldest-first policy retained 85.77% of
translated audio, reduced queue p95 from 17.077 to 8.352 seconds, and reduced
listener tail from 27.120 to 12.672 seconds. It still peaked at 14.059 seconds
because an incomplete or audible parent cannot be removed whole. See the
[whole-parent freshness-cap report](docs/SCHEMA3_FRESHNESS_CAP_SIMULATION_2026-07-24.md).

The completed staged matrix can also size a post-NMT TTS subsegment experiment
without copying translated text:

```bash
python3 analyze_tts_duration.py \
  --input-dir \
    experiment_results/post-tts-recovery-matrix-20260724T054450Z-636f478/repeat-01
```

The model selects a strict 40-character starting cap, while identifying 45 and
60 characters as near-boundary and lower-call-amplification candidates. Those
are offline sizing results, not evidence that splitting improves live delay;
see the measured tradeoffs and required composite sequence contract in
[Post-NMT TTS subsegment capacity model](docs/TTS_SUBSEGMENT_CAPACITY_MODEL.md).
The default-off implementation and automated matched canary procedure are in
[Post-NMT TTS subsegment implementation](docs/TTS_SUBSEGMENT_IMPLEMENTATION.md).

To measure Magpie's response cadence without changing listener output, run one
unsplit real-time arm from a clean commit:

```bash
CANARY_TTS_RESPONSE_CHUNK_TELEMETRY=1 \
CANARY_CAPS=0 \
CANARY_DURATION_SECONDS=300 \
./run_tts_subsegment_canary.sh
```

The runner verifies the pinned model containers, retains only numeric
response-cadence data in the staged sidecar, and writes a privacy-safe
`streaming_latency_analysis.md`. The diagnostic remains atomic: it does not
forward partial PCM. The formal five-minute gate found 2,123 incremental PCM
responses across 74/74 multi-response requests. Current atomic buffering held
the first available PCM for another 0.430 seconds at p50 and 1.633 seconds at
p95. The default-off schema-v3 experiment now publishes frame-aligned PCM
without waiting for full TTS completion. This improves local responsiveness
but does not by itself bound the listener queue. See the
[atomic TTS response-chunk diagnostic](docs/TTS_RESPONSE_CHUNK_DIAGNOSTIC.md)
and [five-minute canary](docs/TTS_RESPONSE_CHUNK_5MIN_CANARY_2026-07-24.md).

To compare the atomic path with schema-v3 incremental publication on one
SHA-verified source prefix, run from a clean commit:

```bash
CANARY_DURATION_SECONDS=60 ./run_streaming_tts_canary.sh
```

The runner leaves the Riva containers unchanged, starts one local FastAPI
process per arm, requires response-cadence telemetry, and keeps post-NMT TTS
splitting disabled. It verifies pinned running-image digests, readiness,
backend flags, source structure, parent order, frame/byte integrity, and
terminal completion. Raw evidence stays under the ignored
`experiment_results/` directory.

Magpie can synthesize different audio durations across otherwise matched
calls. The comparator therefore does not require cross-arm byte equality and
does not automatically attribute queue or listener-tail differences to
publication mode. Its primary causal measurement is within the schema-v3 arm:
how much earlier each parent's first frame was sent than that same parent's
TTS completion. Validated targets of four characters or fewer use a narrow
atomic fallback so the known intermittent Magpie `UNKNOWN` shape can still
retry before any PCM is published; those parents remain in audience metrics
but are excluded from the direct incremental-lead distribution. See the
[incremental publication design](docs/STREAMING_TTS_PUBLICATION_DESIGN.md).

Detailed guides:

- [Adaptive playback controller](docs/ADAPTIVE_PLAYBACK.md)
- [Audience-latency metric definitions](docs/AUDIENCE_LATENCY_METRICS.md)
- [Observation-only parent/frame metadata protocol v1](docs/AUDIO_METADATA_OBSERVATION_V1.md)
- [Protocol-v1 60-second formal canary](docs/AUDIO_METADATA_60S_CANARY_2026-07-25.md)
- [Bounded-playback experiment plan](docs/BOUNDED_PLAYBACK_EXPERIMENT.md)
- [July 22 three-sample acceptance results](docs/ACCEPTANCE_RUN_2026-07-22.md)
- [Staged pipeline foundation and live smoke](docs/STAGED_PIPELINE_FOUNDATION.md)
- [Bounded staged NMT/TTS pipeline, preflight, and full canary](docs/STAGED_NMT_TTS_PIPELINE.md)
- [Feature-flagged staged WebSocket integration](docs/STAGED_WEBSOCKET_INTEGRATION.md)
- [Narrow NMT short-segment recovery and failed-capture evidence](docs/NMT_SHORT_SEGMENT_RECOVERY.md)
- [Atomic TTS recovery and privacy-safe short-segment replay](docs/TTS_SHORT_SEGMENT_RECOVERY.md)
- [July 24 staged recovery three-sample matrix](docs/STAGED_RECOVERY_MATRIX_2026-07-24.md)
- [Post-NMT TTS subsegment capacity model](docs/TTS_SUBSEGMENT_CAPACITY_MODEL.md)
- [Default-off post-NMT TTS subsegmentation and matched canary](docs/TTS_SUBSEGMENT_IMPLEMENTATION.md)
- [Post-NMT TTS subsegmentation 60-second live probe](docs/TTS_SUBSEGMENT_60S_PROBE_2026-07-24.md)
- [Post-NMT TTS subsegmentation five-minute matched canary](docs/TTS_SUBSEGMENT_5MIN_CANARY_2026-07-24.md)
- [Atomic TTS response-chunk diagnostic](docs/TTS_RESPONSE_CHUNK_DIAGNOSTIC.md)
- [Atomic TTS response-cadence five-minute canary](docs/TTS_RESPONSE_CHUNK_5MIN_CANARY_2026-07-24.md)
- [Default-off incremental TTS publication design](docs/STREAMING_TTS_PUBLICATION_DESIGN.md)
- [Incremental TTS publication 60-second formal canary](docs/STREAMING_TTS_60S_CANARY_2026-07-24.md)
- [Incremental TTS publication five-minute matched canary](docs/STREAMING_TTS_5MIN_CANARY_2026-07-24.md)
- [Schema-3 whole-parent freshness-cap simulation](docs/SCHEMA3_FRESHNESS_CAP_SIMULATION_2026-07-24.md)
- [Sample 02 post-recovery staged canary](docs/STAGED_SAMPLE_02_RECOVERY_CANARY.md)
- [Sample 03 full-sample staged canary](docs/LONG_FORM_03_STAGED_CANARY.md)
- [July 22 partner-facing experiment update](docs/S2S_PARTNER_UPDATE_2026-07-22.md)
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
