# Rendered-digital common-clock 60-second preflight

## Status and claim boundary

This guide describes the default-off browser preflight that records the source
reference and rendered translated output against one Web Audio sample clock.
The implementation and offline validator are present in this repository, but
no live preflight result has yet been accepted from this implementation.

This is a **rendered-digital mechanical gate**. It can verify what entered the
browser audio graph, what the graph scheduled, protocol continuity, and the
translated playback queue on one `AudioContext` clock. It does **not** prove:

- that a DAC or physical output device emitted the samples;
- that the audio was acoustically audible in a room or headset;
- translation meaning or quality;
- the delay of a particular translated phrase; or
- alignment with an audience reaction.

Those claims require later semantic review and, for physical audibility, a
separate synchronized hardware or acoustic capture.

## Privacy and repository policy

The capture contains raw source and translated PCM. It is private test
evidence even though the tracked input fixture is public.

- Capture is **off by default** and must be explicitly enabled for each run.
- Keep the browser tab visible and the machine awake until the queue drains.
- Store all four exported files in an access-controlled location.
- Do not attach raw artifacts to a public issue or pull request.
- Do not force-add an artifact to Git.
- The repository ignores these generated filename patterns:

  ```text
  rendered-digital-preflight-*.stereo.wav
  rendered-digital-preflight-*.manifest.json
  rendered-digital-preflight-*.timing.csv
  rendered-digital-preflight-*.blocks.csv
  ```

The validator report is transcript-free, but it still binds a private run by
hash. Treat it according to the evidence-handling policy for the test.
The runner's `docker-attestation.json` is also transcript- and
environment-free, but contains container/image identities and the browser
manifest hash; retain it with the private run.

## What is recorded

One browser-created `AudioContext` must run at exactly 16,000 Hz. An
`AudioWorklet` receives two inputs and writes one interleaved stereo PCM16
stream:

```text
exact padded source PCM ── playbackRate 1.00x ──┐
                                                ├─ input 0 ── stereo channel 0
                                                │
translated PCM ── browser queue ── playbackRate ┤
                                                └─ input 1 ── stereo channel 1
```

The precise channel contract is:

| WAV channel | Role | Tap |
|---|---|---|
| 0 | Source reference | After playback-rate processing and before monitor mute |
| 1 | Translated output | After queue scheduling and playback-rate processing, before monitor mute |

Monitor mute therefore does not remove evidence from either channel. Channel 0
must reproduce the exact padded source PCM within its declared active interval
and must contain only zero samples outside that interval.

Before export, the browser also reconciles every translated protocol frame
from receipt to scheduling. The manifest binds ordered PCM byte counts and
SHA-256 values plus canonical received/scheduled frame-ledger hashes. The
offline validator ties those counts and ledger hashes to the timing CSV and
requires the received and scheduled aggregate identities to match. Channel 1
must contain no nonzero sample outside the declared translated schedule. The
schedule CSV records both floating-point AudioContext seconds and the integer
`scheduled_start_context_frame_floor` and
`scheduled_end_context_frame_exclusive` columns. The validator replays and
reconciles those integer frame intervals, then uses them—not a new
floating-point conversion—to test the channel-1 WAV interval.

This distinction is intentional: receipt-to-schedule protocol PCM continuity
is exact, while the captured channel proves that the browser graph rendered
translated audio only inside its scheduled intervals. The gate does not claim
a sample-exact reconstruction of every scheduled PCM frame after Web Audio
playback-rate processing. A `PASS` therefore remains a queue/transport result,
not proof that every physical or acoustic output sample was emitted.

The worklet also observes source chunk boundaries on the same render clock.
Those observations release the corresponding input chunks to the WebSocket.
This replaces an independent JavaScript timer for the formal run. Each
4,800-frame boundary may be reported only after it is reached and less than
one 128-frame render quantum late. The browser brackets main-thread receipt
with two AudioContext-frame samples, then takes another frame sample only
after `sendAudio()` confirms WebSocket handoff. Receipt and handoff may be no
more than 1,600 frames (100 ms) after the source boundary. After all 200
`chunk_sent` rows, the timing CSV must contain exactly one `input_ended` row
with chunk index `-1`, zero audio bytes, and source position 60.0 seconds. It
must occur before the server terminal row.

The capture continues beyond the 60-second source interval until:

1. the server emits its single terminal `completed` status;
2. no translated PCM is received or scheduled after that terminal;
3. the browser playback queue reaches exactly zero; and
4. the worklet acknowledges its final captured block.

The dashboard permits at most 300 seconds of drain after input completion.
The worklet capture allocation is capped at 420 seconds total, covering the
60-second source, the full drain limit, and teardown margin. Exceeding either
limit fails the run; it is not a latency `FAIL` result.

The timing artifact has one exact, registered 60-column CSV schema shared by
the browser serializer and offline validator. Unknown, missing, duplicate, or
reordered columns make a bundle `INVALID`.

## Exact source fixture

The formal gate accepts only `test_audio/preflight.wav` with these identities:

| Property | Required value |
|---|---|
| Source WAV SHA-256 | `0c2cb04d9774f60472b55355f587da2148a053f3a55c05ff36c7dfc23be5c257` |
| Decoded and padded mono PCM16 SHA-256 | `81720f2e23e5b85df4eb2be0bbd486b6591d0b1ed98580118e9e2e1e466bd51c` |
| Sample rate | 16,000 Hz |
| Padded frame count | 960,000 |
| Chunk size | 4,800 frames |
| Chunk count | 200 |
| Padded duration | 60 seconds |

Verify the tracked WAV before starting:

```bash
(cd test_audio && sha256sum --check SHA256SUMS)
```

The manifest builder and validator independently require the exact WAV hash,
padded PCM hash, frame count, and 200-row source ledger. A file that merely
decodes to approximately 60 seconds is not accepted.

## Registered runtime

The formal bundle binds a clean 40-character Git commit and the following
runtime:

| Component | Required setting |
|---|---|
| Pipeline | Staged direct ASR -> NMT -> TTS |
| Audio metadata protocol | Version 1 |
| Telemetry | Schema 3 |
| Punctuation segmentation | 240 maximum characters, 2,000 ms maximum age |
| Queue capacities | ASR events 32; NMT 4; TTS 4; output 4 |
| RPC deadlines | NMT 15 seconds; TTS 60 seconds |
| TTS safety limits | 60-second maximum segment audio; one retry |
| TTS response-chunk telemetry | Disabled |
| TTS target subsegmentation | Disabled (`0` maximum, `12` packing preference) |
| Incremental atomic fallback | 4 characters |
| Pipeline close deadline | 10 seconds |
| ASR image digest | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| ASR profile | `nemotron-asr-streaming_en-US_batch32` |
| ASR final EOU | 800 ms |
| ASR word time offsets | Enabled |
| NMT image digest | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| NMT model/language pair | `megatronnmt_any_any_1b`, `en-US_to_es-US` |
| TTS image digest | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |
| TTS profile/voice | `magpie-tts-multilingual_batch8`, `Magpie-Multilingual.ES-US.Isabela` |
| TTS publication | Incremental |
| TTS frame duration | 100 ms |
| Post-NMT TTS splitting | Disabled |
| Application provenance | Frontend and backend processes started clean from the same commit |
| Recorder worklet SHA-256 | `0a0206154739d0731f200629d8b2ae341e9c3176336936ef33fbfd40dc52d189` |

The image release tags in this repository are Nemotron ASR Streaming `1.2.0`,
Riva Translate 1.6B `1.5.2`, and Magpie multilingual TTS `1.7.0`. The
registered gate additionally requires the immutable digests above; tags alone
are not sufficient evidence.

Use digest-qualified image references and matching provenance fields in
`.env`:

```dotenv
ASR_IMAGE=nvcr.io/nim/nvidia/nemotron-asr-streaming@sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850
NMT_IMAGE=nvcr.io/nim/nvidia/riva-translate-1_6b@sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb
TTS_IMAGE=nvcr.io/nim/nvidia/magpie-tts-multilingual@sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d

ASR_IMAGE_DIGEST=sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850
NMT_IMAGE_DIGEST=sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb
TTS_IMAGE_DIGEST=sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d

ASR_NIM_TAGS_SELECTOR=name=nemotron-asr-streaming,type=en-US,batch_size=32
TTS_NIM_TAGS_SELECTOR=name=magpie-tts-multilingual,batch_size=8
RIVA_NMT_MODEL=megatronnmt_any_any_1b
RIVA_SOURCE_LANGUAGE=en-US
RIVA_TARGET_LANGUAGE=es-US
S2S_PIPELINE_MODE=staged
RIVA_EOU_MS=800
RIVA_ASR_WORD_TIMES=1
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
STAGED_TTS_INCREMENTAL_PUBLISH=1
STAGED_TTS_INCREMENTAL_FRAME_MS=100
STAGED_TTS_INCREMENTAL_ATOMIC_FALLBACK_MAX_CHARS=4
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
STAGED_TTS_SUBSEGMENT_MIN_CHARS=12
STAGED_CLOSE_TIMEOUT_SECONDS=10
```

Do not copy a digest into `.env` without independently confirming that the
deployed image resolves to it. `docker compose config --images` must show the
three digest-qualified references. Registry digest visibility can also be
checked locally with:

```bash
set -a
source .env
set +a

docker compose config --images
docker image inspect "$ASR_IMAGE" --format '{{json .RepoDigests}}'
docker image inspect "$NMT_IMAGE" --format '{{json .RepoDigests}}'
docker image inspect "$TTS_IMAGE" --format '{{json .RepoDigests}}'
```

The browser manifest records the backend's declared immutable digests. The
offline validator validates those declarations and their normalized hash; it
does not query Docker by itself. The automated runner closes that boundary
with read-only `docker container ls`, `docker container inspect`, and
`docker image inspect` calls. It discovers exactly one distinct,
running/healthy container for each registered HTTP/gRPC host-port pair,
requires the exact internal port mappings, resolves each container image ID to
the approved NGC `RepoDigest`, compares those digests with the backend
declarations, and requires identical identities before and after capture. It
never starts, stops, pulls, or modifies a container. The resulting private
`docker-attestation.json` is finalized with the clean repository commit and
browser manifest SHA-256.

## Start and attest the services

Run from the repository root. First require a committed, clean tree:

```bash
git rev-parse HEAD
test -z "$(git status --porcelain --untracked-files=normal)"
```

The second command must exit successfully without output. Commit intended code
changes before the run, then start both the frontend and backend from that
clean checkout. FastAPI snapshots its repository provenance when the
application process imports, while Vite embeds its provenance when the
production bundle is built and preview serves that immutable bundle. The
manifest builder requires both snapshots to be clean and to name the same
commit. A checkout or edit after either build/process starts requires
rebuilding the frontend and restarting both processes before capture.

The capture fetches the recorder worklet with `cache: no-store`, hashes those
exact bytes, and loads the same bytes into the `AudioContext`. The manifest
binds that SHA-256, and offline validation requires it to match
`frontend/public/rendered-digital-recorder.worklet.js` from the checked-in
validation source and the registered digest above. Record the local hash with:

```bash
sha256sum frontend/public/rendered-digital-recorder.worklet.js
```

If this Docker installation is not already authenticated to NGC, export the
ignored `.env` into the shell and log in before pulling:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin
```

Start or verify the pinned Riva services:

```bash
docker compose pull
docker compose up -d
docker compose ps

curl --fail http://localhost:9002/v1/health/ready
curl --fail http://localhost:9001/v1/health/ready
curl --fail http://localhost:9003/v1/health/ready
nvidia-smi
```

For a formal run, do not use the mutable Vite development server or
`./start.sh`. Build immutable assets, start FastAPI without reload, and serve
the production bundle:

```bash
cd frontend
npm run build
npm run preview -- --host 127.0.0.1 --port 5173 --strictPort
```

In a separate shell from the repository root:

```bash
python3 -m uvicorn main:app \
  --app-dir backend \
  --host 127.0.0.1 \
  --port 8000
```

In another shell, inspect the effective backend configuration:

```bash
curl --fail --silent http://localhost:8000/api/config | python3 -m json.tool
```

Before recording, confirm that the response reports:

- `pipelineMode` equal to `staged`;
- `audioMetadataProtocolVersions` containing `1`;
- `repositoryProvenance.dirty` equal to `false`;
- `repositoryProvenance.commit` equal to `git rev-parse HEAD`;
- `stagedConfig.telemetrySchemaVersion` equal to `3`;
- `stagedConfig.segmentMaxChars` equal to `240`;
- `stagedConfig.segmentMaxAgeMs` equal to `2000`;
- `stagedConfig.asrEventQueueMaxSize` equal to `32`;
- `stagedConfig.nmtQueueMaxSize`, `ttsQueueMaxSize`, and
  `outputQueueMaxSize` each equal to `4`;
- `stagedConfig.nmtRpcTimeoutSeconds` equal to `15`;
- `stagedConfig.ttsRpcTimeoutSeconds` and
  `ttsMaxSegmentAudioSeconds` each equal to `60`;
- `stagedConfig.ttsMaxRetries` equal to `1`;
- `stagedConfig.ttsResponseChunkTelemetryEnabled` equal to `false`;
- `stagedConfig.ttsSubsegmentMaxChars` equal to `0` and
  `ttsSubsegmentMinChars` equal to `12`;
- `stagedConfig.ttsIncrementalAtomicFallbackMaxChars` equal to `4`;
- `stagedConfig.closeTimeoutSeconds` equal to `10`;
- `stagedConfig.ttsIncrementalPublishEnabled` equal to `true`;
- `stagedConfig.ttsIncrementalFrameMs` equal to `100`;
- ASR EOU equal to `800` with word times enabled; and
- all three required image digests.

If any value is absent or differs, stop. Do not edit the manifest afterward.

## Secure browser requirement

`AudioWorklet` and the Web Audio capture must run in a secure browser context.
For development, `http://localhost` is treated as potentially trustworthy.
An unencrypted URL using a VM IP address is not equivalent and the capture
must fail closed.

When the services run on a remote VM, create a local SSH tunnel:

```bash
ssh -N -L 5173:127.0.0.1:5173 user@vm
```

Then use the local browser at:

```text
http://localhost:5173/#/test
```

The Vite server proxies application API and WebSocket traffic to the backend
on the VM, so the frontend tunnel is sufficient for the normal repository
configuration. If a deployment changes that proxy arrangement, tunnel the
configured backend port as well. HTTPS with a valid certificate is the other
supported option.

The browser file picker reads the local workstation, not the remote VM. Select
`test_audio/preflight.wav` from a matching local clone, or copy the tracked
fixture to the workstation and verify it there:

```bash
scp user@vm:/path/to/repository/test_audio/preflight.wav ./preflight.wav
printf '%s  %s\n' \
  '0c2cb04d9774f60472b55355f587da2148a053f3a55c05ff36c7dfc23be5c257' \
  './preflight.wav' | sha256sum --check
```

## Capture the four-file bundle

The recommended path is the fail-closed headless-browser runner. It verifies a
clean, unchanged checkout; checks the exact fixture and registered backend
configuration; verifies the three already-running Riva readiness endpoints;
attests the actual containers and immutable image digests serving those
endpoints; builds and fingerprints an immutable production frontend; starts
only the FastAPI/frontend/Chrome processes it owns; exports exactly four
browser artifacts; runs the offline validator; and preserves private logs and
artifacts on either success or failure.

Run from a clean repository root after the pinned Riva containers are ready:

```bash
python3 run_rendered_digital_preflight.py
```

The default private output directory is under
`~/private-s2s-evidence/`. Use `--output-dir` only with a new directory outside
the repository. The runner reads `.env` without executing it. It never starts,
stops, or modifies the Riva containers. Use `--no-start-services` only when
ports 8000 and 5173 already serve the intended clean production deployment.

For a supervised manual run using the same production service requirements:

1. Open `http://localhost:5173/#/test`.
2. Select the locally available, hash-verified `preflight.wav`.
3. Leave **Adaptive Spanish playback** enabled. The registered policy is
   lossless and uses 1.00x, 1.05x, and 1.10x scheduling.
4. Explicitly enable **60-second rendered-digital common-clock preflight**.
5. Click **Start Test** once.
6. Keep the tab visible and the `AudioContext` running. Do not switch tabs,
   suspend the machine, or click **Stop Test**.
7. Wait for server completion and the translated browser queue to drain. This
   can take longer than the 60-second input.
8. When the dashboard reaches `completed`, click **Export Evidence**.
9. Allow multiple downloads if the browser asks. One export action must
   produce four files with the same timestamp base.

The bundle is:

```text
rendered-digital-preflight-<timestamp>.manifest.json
rendered-digital-preflight-<timestamp>.stereo.wav
rendered-digital-preflight-<timestamp>.blocks.csv
rendered-digital-preflight-<timestamp>.timing.csv
```

Those are exactly the four browser-generated artifacts. The automated private
run directory additionally contains `docker-attestation.json`, the validator
report, and process logs. They are operational evidence, not extra browser
downloads; preserve them with the bundle.

Move the complete set to a private directory outside the repository. A manual
stop, hidden-tab event, suspended `AudioContext`, missing server terminal,
post-terminal audio, or incomplete queue drain cannot produce valid evidence.

## Offline validation

Set `BASE` to the common path and filename prefix, without a suffix:

```bash
BASE="$HOME/private-s2s-evidence/rendered-digital-preflight-<timestamp>"

python3 analyze_rendered_digital_preflight.py \
  --manifest "$BASE.manifest.json" \
  --wav "$BASE.stereo.wav" \
  --blocks "$BASE.blocks.csv" \
  --timing-csv "$BASE.timing.csv" \
  --output "$BASE.report.json"
```

The validator atomically writes the report and prints its status. Its process
exit code is:

| Status | Exit code | Meaning |
|---|---:|---|
| `PASS` | 0 | The bundle is structurally valid, capture integrity and protocol PCM continuity pass, and queue p95/peak are within the registered bounds. |
| `FAIL` | 1 | The bundle is valid evidence, but protocol PCM continuity or the registered queue bound fails. |
| `INVALID` | 2 | The bundle is malformed, incomplete, contradictory, corrupt, unbound, from the wrong fixture/runtime, or cannot be read safely. It is neither a latency pass nor a latency fail. |

`PASS` is only a mechanical rendered-digital result. It does not upgrade any
semantic, translation-quality, DAC, acoustic-audibility, or
audience-reaction claim.

Running the offline validator directly proves only the registered runtime
declarations embedded in the four-file bundle. Treat a result as bound to the
actual Nemotron/NMT/TTS images only when the runner's post-capture
`docker-attestation.json` is present, matches the browser manifest hash, and
records the approved `RepoDigest`s.

The validator fails closed on, among other conditions:

- a dirty or unbound repository state;
- a runtime digest or registered configuration mismatch;
- a source WAV, padded PCM, frame-count, or chunk-ledger mismatch;
- missing, duplicated, reordered, early, or more-than-one-quantum-late source
  boundary observations, or a main-thread receipt/WebSocket handoff more than
  100 ms after its source boundary;
- a missing, duplicated, early, or inconsistent `input_ended` row instead of
  one row after all 200 chunks and before the server terminal;
- a gap, overlap, reorder, or digest mismatch in the worklet block ledger;
- a fetched recorder-worklet hash that differs from the checked-in module;
- any WAV, channel, manifest, timing CSV, or ledger hash mismatch;
- a visibility or `AudioContext` state violation;
- missing or duplicate protocol frame/parent identities;
- received/scheduled frame, byte, ordered-PCM-hash, or canonical-ledger
  disagreement;
- schedule-second/integer-frame disagreement or nonzero translated capture
  outside the integer scheduled-frame intervals;
- a missing or non-`completed` server/dashboard terminal;
- translated receipt or scheduling after server completion; and
- any captured playback schedule that does not exactly replay under the
  registered policy.

## Queue decision

The validator reconstructs the lossless adaptive playback schedule from
captured AudioContext arrival times and media durations. It independently
checks every captured start, end, duration, wait, queue depth, rate, and mode.
The formal queue metric is the exact piecewise-linear AudioContext schedule,
not one-second UI samples.

The registered live-audience starting objective is:

```text
time-weighted queue p95 <= 5.0 seconds
peak queue depth         <= 10.0 seconds
chunks dropped           = 0
```

Both queue limits and protocol PCM continuity are required for a mechanical
`PASS`. A structurally valid run above either queue limit is `FAIL`; that is
useful evidence that the current no-drop policy has not bounded the translated
browser playback backlog tightly enough.

## Developer verification

The validator has a dedicated test module:

```bash
PYTHONPATH=backend:. python3 -m pytest -p no:cacheprovider -q \
  tests/test_analyze_rendered_digital_preflight.py
```

The frontend capture, artifact, manifest, source-clock, playback, timing, and
dashboard behavior is covered by the frontend suite:

```bash
cd frontend
npm run lint
npm run build
npm test -- --run
```

These checks validate implementation behavior; they do not substitute for a
clean, live, four-artifact browser capture and offline report.
