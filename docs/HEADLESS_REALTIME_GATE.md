# Browser-independent real-time S2S gate

## Purpose

Chrome, Vite, and Web Audio are not deployment requirements for this
speech-to-speech pipeline. They remain useful for the interactive demonstration
and for one optional rendered-digital diagnostic, but the primary repeatable
service gate is headless:

```text
source audio
  -> Python WebSocket client, paced in real time
  -> FastAPI /ws/translate
  -> staged Nemotron 3 ASR -> Riva NMT -> Magpie TTS
  -> validated metadata-header + PCM-frame stream
  -> deterministic no-drop listener-schedule analysis
```

This path tests the same English PCM input, translated Spanish PCM output, and
terminal drain contract that a native, mobile, browser, or embedded listener
client would use. It does not require a microphone, sound card, display server,
Chrome, Node.js, or the Vite frontend.

## What this gate establishes

The headless run can establish:

- pinned ASR, NMT, and TTS services were ready and reachable;
- Nemotron 3 streaming ASR received English audio with automatic punctuation
  and an 800 ms end-of-utterance setting;
- the staged server preserved ordering while overlapping NMT and TTS through
  bounded internal queues;
- source input was sent no earlier than each chunk's absolute source-end
  boundary, rather than as a file-upload burst;
- every protocol-v1 metadata header paired with exactly one binary PCM frame;
- parent/frame counts, byte totals, generation identity, and the terminal drain
  reconciled;
- every translated audio chunk was retained by the listener scheduling model;
  and
- the fixed 1.00x and adaptive 1.00x/1.05x/1.10x schedules can be compared on
  the same live Riva arrival trace.

The scheduler uses a 5-second target, an 8-second urgent threshold, and a
10-second audience-experience limit. These are policy thresholds, not a
destructive buffer cap. The 10-second limit is reported as an SLA breach; the
policy never drops speech to force the queue below it. If translated audio
arrives faster than 1.10x playback can consume it, the queue can exceed 10
seconds and the audience gate should miss.

The mechanical pass criteria are a time-weighted queue p95 at or below five
seconds, a peak queue at or below ten seconds, and zero dropped, reordered, or
duplicated frames. The current harness validates received digital PCM framing
and analyzes its timing and duration metadata. It does not prove that a DAC,
amplifier, speaker, or listener rendered the samples at the modeled time.

## Runtime used by the gate

The checked-in Compose file pins this single-GPU configuration:

| Stage | Image | Release |
|---|---|---:|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming` | `1.2.0` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b` | `1.5.2` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual` | `1.7.0` |

The selected profiles reserve approximately 26.4 GB in total. They were tested
together on a 96 GB RTX PRO 6000 Blackwell Server Edition and should fit a
48 GB RTX 6000 Ada. A new GPU or profile combination still needs an
end-to-end `nvidia-smi` check because peak use, engine-build workspace, and
other processes are not represented by the profile sum.

## One-time setup

From the repository root:

```bash
cp .env.example .env
mkdir -p .cache/nim
chmod 777 .cache/nim

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt -r backend/requirements.txt
```

Add an active NGC Personal Key to `.env`. Compose reads `.env` itself, but
`docker login` and a manually launched backend need the variables exported in
the shell:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin
```

Do not commit `.env`. Verify the bundled test fixtures before a comparison
run:

```bash
(cd test_audio && sha256sum --check SHA256SUMS)
```

## Configure the staged observation path

Use these values in `.env` for the protocol-v1 headless gate:

```dotenv
S2S_PIPELINE_MODE=staged
RIVA_EOU_MS=800
RIVA_ASR_WORD_TIMES=1
STAGED_TTS_INCREMENTAL_PUBLISH=1
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
```

`STAGED_TTS_INCREMENTAL_PUBLISH=1` selects staged telemetry schema 3. Together
with staged mode, it allows the backend to advertise audio metadata protocol
v1. Word offsets allow source start/end attribution when Nemotron supplies
them; without those offsets, a source-end value may be an
`audio_processed` fallback and is explicitly non-semantic.

Keep post-NMT TTS subsegmentation disabled. The matched canaries found that the
tested subsegment policies increased generated audio and listener backlog.
The other staged queue and timeout values can remain at their checked-in
defaults unless the run is explicitly testing a different configuration.

## Start and verify the services

Start the pinned NIMs:

```bash
set -a
source .env
set +a

nvidia-ctk cdi list
docker compose pull
docker compose up -d
docker compose ps
```

The first model download and TensorRT engine build may take 30 minutes or
longer. Do not start the long-form run until all three readiness probes pass:

```bash
curl --fail http://localhost:9002/v1/health/ready  # ASR
curl --fail http://localhost:9001/v1/health/ready  # NMT
curl --fail http://localhost:9003/v1/health/ready  # TTS
nvidia-smi
```

Start only FastAPI in another terminal; the frontend is unnecessary:

```bash
cd /path/to/realtime-s2s-demo-app
source .venv/bin/activate
set -a
source .env
set +a
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

Before labeling a result, inspect the active process configuration:

```bash
curl --fail --silent http://localhost:8000/ | python3 -m json.tool
curl --fail --silent http://localhost:8000/api/config | python3 -m json.tool
```

The configuration must show:

- `pipelineMode` equal to `staged`;
- `audioMetadataProtocolVersions` containing `1`;
- `stagedConfig.telemetrySchemaVersion` equal to `3`;
- ASR end-of-utterance equal to 800 ms;
- ASR word-time offsets enabled; and
- the expected pinned image references, profiles, endpoints, languages, model,
  and voice.

Restart FastAPI after changing `.env`; configuration is read when the backend
process imports the application.

## Run all three samples

First inspect the resolved plan without contacting the backend or creating
artifacts:

```bash
python3 run_long_form_experiment.py \
  --audio-metadata-protocol-v1 \
  --dry-run
```

Then run the one-minute preflight followed by all three long-form samples:

```bash
python3 run_long_form_experiment.py --audio-metadata-protocol-v1
```

The files are streamed sequentially at real-time pace:

- `test_audio/long-form-01.mp3`
- `test_audio/long-form-02.mp3`
- `test_audio/long-form-03.mp3`

One repeat contains about 103.7 minutes of source audio, plus the one-minute
preflight and each capture's output-drain time. The backend allows one active
session, so the run is intentionally sequential.

If the VM stops after a promoted checkpoint, resume the exact clean commit and
unchanged inputs:

```bash
python3 run_long_form_experiment.py \
  --resume-dir experiment_results/<run-id>
```

The manifest freezes the protocol choice and backend model configuration.
Resume revalidates the Git commit, input SHA-256 values, artifact hashes, event
counts, and received-byte totals before skipping a completed capture.

The harness does not start or stop Compose or FastAPI. It checks the services
already running and leaves them in their original state.

## Results and metrics

Raw artifacts are written under the ignored `experiment_results/` directory:

```text
experiment_results/<run-id>/
├── manifest.json
├── playback_policy_analysis.json
├── playback_policy_analysis.md
├── preflight/
└── repeat-01/
    ├── <sample>_results.csv
    ├── <sample>_summary.json
    └── <sample>_latency.png
```

Each per-sample summary embeds a fixed-schema `headless_playback` report with
the canonical scheduler cross-check, queue gate, source-boundary distributions,
parent playback envelope, accumulated frontier drift, privacy declaration, and
claim boundaries. The cross-sample analysis repeats the fixed/adaptive
source-frontier comparison from the saved CSV using analysis schema version 2.

Keep raw traces private. They can contain identifying metadata even though the
protocol itself contains no transcript or translation text.

Interpret the outputs in separate evidence categories:

| Category | Representative metrics | What it means |
|---|---|---|
| Service readiness | health endpoints, GPU state, runtime failures | The selected NIMs were available for the run |
| Transport integrity | headers/frames, generations, parent counts, bytes, order, terminal `completed` | No translated PCM was lost, duplicated, reordered, or accepted after terminal completion |
| Pipeline responsiveness | first-audio latency, NMT/TTS processing and queue residence, service tail, terminal tail | Where server-side waiting occurred |
| Audio production | translated/source duration ratio | Whether synthesized media intrinsically grows faster than source media |
| Listener schedule | time-weighted queue p50/p95, peak queue, time above 10 seconds, fixed/adaptive listener tail, rate occupancy | How a no-drop digital listener schedule would accumulate and drain backlog |
| Source freshness | protocol-v1 source offsets and source-end-to-receipt timing | How old an attributed source boundary was when translated PCM reached the client |

The policy objective is to keep the queue near five seconds, move into urgent
catch-up at eight seconds, and avoid exceeding ten seconds. A useful candidate
must also preserve every frame and pass all ordering, byte, terminal, and
cleanup checks. Treat a queue-objective miss as an experiment result, not as a
reason to discard the trace or silently drop speech.

Keep these clocks and tails distinct:

- first translated PCM is not the same as first scheduled playback;
- server/service tail is not listener playback tail;
- terminal completion is not queue drain;
- translated/source duration ratio is not a latency percentile; and
- listener queue depth does not include ASR finalization, NMT, or the delay
  before a parent's first TTS frame arrives.

### Attribute queue-growth windows

After a complete schema-3 run, align all three retained CSV/summary pairs with
the server stages and deterministic listener schedule:

```bash
PYTHONPATH=backend:.python-packages:. \
python3 analyze_stage_burst_attribution.py \
  experiment_results/<run-id>/repeat-01/*_results.csv \
  --window-seconds 30 \
  --top-window-count 5 \
  --json-output experiment_results/<run-id>/stage_burst_attribution.json \
  --markdown-output experiment_results/<run-id>/stage_burst_attribution.md
```

The analyzer fails closed on incompatible or inconsistent evidence and writes
only neutral numeric aggregates. It reports aligned fixed-grid burst windows
and separately labeled complete rolling-window associations. Keep the
generated reports with the ignored raw run; commit only a reviewed sanitized
summary.

See [Stage-burst attribution](STAGE_BURST_ATTRIBUTION.md) for the method and
[the 2026-07-26 formal result](STAGE_BURST_ATTRIBUTION_RESULT_2026-07-26.md)
for the current interpretation.

### Attribute the TTS publisher handoff

When stage/burst analysis finds a material frame-ready-to-output-enqueue tail,
enable the default-off publisher-handoff diagnostic. It preserves the same
schema-3 PCM and queue policy while separating prior-frame serialization,
worker-to-event-loop dispatch, capacity acquisition, queue residence, and
WebSocket send timing. Start with its one-minute fail-closed preflight before
running the promoted five-minute Sample 02 diagnostic.

See [TTS publisher-handoff diagnostic](PUBLISHER_HANDOFF_DIAGNOSTIC.md) for the
exact feature contract, commands, privacy boundary, artifacts, and decision
rule.

## What can be claimed

After a complete run, it is reasonable to claim that the pinned staged
Nemotron 3/NMT/TTS path:

- processed the three fixtures in real time through a browser-independent
  WebSocket client;
- preserved or failed the protocol and PCM continuity checks recorded in the
  report;
- produced the measured first-audio, tail, duration-ratio, and queue results;
  and
- would schedule the captured PCM according to the registered no-drop
  5/8/10-second policy.

Do not describe the deterministic schedule as executed loudspeaker playback or
claim that the queue was hard-capped at 10 seconds. Do not infer translation
quality, intelligibility, voice quality, or acceptable accelerated prosody
from timing telemetry.

## The joke-delay question

The practical concern is whether a Spanish listener hears a translated
punchline long after the English-speaking audience laughs. Queue depth alone
cannot answer that question. The end-to-end delay includes at least:

```text
English phrase boundary
  + ASR finalization
  + NMT
  + first relevant TTS audio
  + listener queue wait
  + output-device rendering
```

Protocol v1 and ASR word timing support a conservative
source-boundary-to-receipt or source-boundary-to-scheduled-playback proxy. That
proxy is useful for detecting accumulated freshness drift, but it does not
prove the exact English punchline and the corresponding Spanish landmark were
semantically aligned. A precise joke-delay claim requires consent-cleared
source markers, review of the corresponding translated-audio landmark, and—if
physical audibility matters—a common-clock rendered or loopback capture.

Chrome is one way to perform the optional rendered-digital cross-check; it is
not the only possible deployed client and it is not required for the primary
headless service gate. See
[Semantic source-event latency gate](SEMANTIC_EVENT_LATENCY_GATE.md) for the
stricter marker-bound evidence method.

## Optional browser diagnostic

The React/Vite UI remains useful as a demonstration and the Web Audio
common-clock preflight remains useful for validating that particular rendering
implementation. Run it only when the question concerns browser scheduling or
browser-rendered PCM. Its Chrome-specific outcome must not block evaluation of
the browser-independent server and WebSocket path.

See
[Rendered-digital common-clock preflight](RENDERED_DIGITAL_COMMON_CLOCK_PREFLIGHT.md)
for that optional experiment.

## Graceful shutdown

Stop the FastAPI process with `Ctrl+C` or the service manager that owns it.
When the NIMs are no longer needed:

```bash
docker compose down
```

This removes the containers and network without deleting the shared model
cache.
