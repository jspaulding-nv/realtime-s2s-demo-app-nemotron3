# Implementation log

This log distinguishes completed repository work from proposed work and from
historical test evidence. Dates are UTC.

## 2026-07-08: pinned Nemotron baseline

Completed:

- Replaced Parakeet CTC with Nemotron ASR Streaming `1.2.0`, using the English
  `batch_size=32` profile.
- Pinned Riva Translate 1.6B to `1.5.2` and Magpie multilingual TTS to `1.7.0`.
- Configured one-GPU Compose networking with ASR on gRPC 50052, NMT/S2S on
  50051, and TTS on 50053.
- Enabled ASR automatic punctuation and used an 800 ms final EOU window.
- Added environment-based Riva connection and endpointing settings.
- Added explicit `end_input` handling so final translated output could drain
  before `stop_stream`.
- Corrected the batch harness to measure first audio, post-input output,
  service flush tail, duration expansion, and fixed-rate listener playback
  tail.
- Completed one long-form run for each of Jonathan Gough's three sermon files
  without a WebSocket drop or gRPC failure.

Pinned pipeline:

```text
Nemotron ASR Streaming 1.2.0
  -> Riva Translate 1.6B 1.5.2
  -> Magpie multilingual TTS 1.7.0
```

Historical results:

| Sermon | First audio | Service flush tail | Output/input | Fixed 1.00x playback tail |
|---|---:|---:|---:|---:|
| Spirit | 16.1 s | 0.0 s | 1.056x | 172.770 s |
| Blessed | 2.3 s | 0.4 s | 1.081x | 228.782 s |
| Beholding | 4.7 s | 0.8 s | 1.017x | 70.598 s |

The short service flush tail did not eliminate the listener tail. Spanish
media was longer than the source, and response timing left substantial audio
queued for fixed-rate playback. Detailed artifacts are in
`docs/results/nemotron3/` and `NEMOTRON_TEST_RESULTS.md`.

Benefit observed from Nemotron 3:

- It uses the Riva team's recommended high-quality English streaming ASR for
  this English-input use case.
- Beholding showed a substantial listener-tail improvement and Spirit a
  moderate improvement relative to reconstructed prior runs.
- Blessed remained effectively unchanged, so the ASR change alone did not
  solve audience delay across all files.

The percentages are based on one new run per file against three older runs and
must be repeated before being treated as stable.

## 2026-07-22: adaptive browser playback implementation

Completed in the working branch:

- Added a policy module with 5-second catch-up, 8-second urgent, and 10-second
  over-limit thresholds.
- Added 4-second and 7-second release thresholds for hysteresis.
- Added 1.00x, 1.05x, and 1.10x per-buffer playback rates.
- Kept speech lossless: no buffer is dropped when the queue exceeds 10
  seconds. The threshold is an SLA alarm and breach counter.
- Added current, peak, p50, and p95 browser queue telemetry plus time above 5
  and 10 seconds.
- Added scheduling and queue-sample fields to client CSV exports.
- Enabled adaptive translated output in the live translation panel and file
  test dashboard; kept English input monitoring at fixed 1.00x.
- Updated the test dashboard to require both network quiet and an empty
  browser playback queue before completing a natural drain.
- Added the typed `end_input` client message.
- Added policy, scheduling, telemetry, CSV, and dashboard test coverage.
- Added a pre-run adaptive/fixed control in the dashboard; its session event
  records that condition in every exported client CSV.
- Pinned the backend SDK to the tested `nvidia-riva-client==2.24.0` for
  reproducible service integration and the planned direct-stage work.

Validation snapshot:

```text
Python backend + analysis tests: 53 passed
Frontend tests:                  81 passed (Node.js 22)
Frontend build:                  passed (Vite chunk-size warning only)
Frontend lint:                   passed
```

No new live Riva sermon run using the adaptive browser controller had been
completed at this point. Do not label the July 8 fixed-rate tails as adaptive
results.

Completed offline validation:

- Added a pure Python playback simulator matching the browser thresholds,
  hysteresis, and rate decisions.
- Added a trace analyzer that reads the ignored July 8 client event CSVs,
  validates fixed-rate tails against the compact committed summaries, and
  writes JSON and Markdown results.
- Replayed every translated PCM chunk; no speech was dropped.

| Saved trace | Simulated adaptive tail | Queue p95 | Peak queue | Playback time over 10 s |
|---|---:|---:|---:|---:|
| Spirit | 27.183 s | 38.31 s | 46.54 s | 80.0% |
| Blessed | 40.206 s | 44.95 s | 53.11 s | 77.3% |
| Beholding | 16.613 s | 19.48 s | 23.81 s | 45.6% |

The simulated aggregate tail reduction was 82.2%. This is promising but does
not meet the queue goal: 1.10x still left all three traces above the 10-second
soft ceiling for substantial periods. The output also predicts that most
translated media would be accelerated, so a live browser run and native
listener review remain mandatory.

Generated replay artifacts are under `docs/results/nemotron3/`. They are
deterministic simulations from saved July 8 arrival traces, not modified live
Riva results.

Known experimental limitation:

- Web Audio `playbackRate` changes pitch as well as tempo and may reveal chunk
  boundary artifacts. Native Spanish listener evaluation is required.
- The 10-second threshold cannot be a hard cap while speech is preserved if
  sustained translated media arrives faster than 1.10x consumption.
- Browser queue depth is exact playback backlog, but it is only one component
  of semantic English-to-Spanish delay.

## Planned next implementation: staged backend

Not yet completed:

- Direct Nemotron streaming ASR consumption.
- Explicit splitting of ASR finals at punctuation boundaries.
- Bounded NMT and TTS queues with pipeline overlap.
- Ordered segment IDs and a reorder buffer if worker counts exceed one.
- Per-stage queue residence and inference metrics.
- A live comparison of monolithic versus staged paths.
- Server-side TTS prosody or pitch-preserving client time scaling.

The proposed architecture and lifecycle are in
[Staged pipeline design](STAGED_PIPELINE_DESIGN.md).

## Reproducible validation commands

Authenticate and start the pinned Riva services:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin

docker compose config --images
docker compose pull
docker compose up -d
docker compose ps

curl --fail http://localhost:9002/v1/health/ready
curl --fail http://localhost:9001/v1/health/ready
curl --fail http://localhost:9003/v1/health/ready
nvidia-smi
```

Run code validation:

```bash
python -m pytest backend/tests tests -q

cd frontend
npm ci
npm test
npm run build
npm run lint
cd ..
```

Run the service preflight and one historical-style long-form test:

```bash
python batch_latency_test.py --preflight
python batch_latency_test.py \
  --file test_audio/200108_SpiritandPresenceofGod.mp3 \
  --output-dir test_results_nemotron
```

Use `http://localhost:5173/#/test` and export its CSV for a live adaptive
browser run. Preserve the entire drain; do not stop after network output goes
quiet while the playback queue remains nonzero.

## Next-run record template

Copy this block into the compact summary for each formal run:

```text
Run ID:
UTC start:
Git commit:
Condition: fixed-1.00x | adaptive
Audio file:
Repeat number:
GPU / driver:
ASR image or digest:
NMT image or digest:
TTS image or digest:
EOU / punctuation:
First audio:
Service flush tail:
Output/input duration:
Browser queue p50 / p95 / max:
Seconds and percent >5 s:
Seconds and percent >10 s:
Rate exposure at 1.00x / 1.05x / 1.10x:
Listener playback tail:
Marked-phrase or punchline delay:
Quality observations:
Runtime failures:
Artifact locations:
```

## Handoff checklist

- [x] Frontend lint passed on the adaptive working branch
- [ ] Commit and push the adaptive branch with these documents
- [ ] Run fixed and adaptive browser controls on all three sermons
- [ ] Repeat every condition at least three times
- [ ] Capture synchronized phrase/punchline delay, not only queue depth
- [ ] Review 1.05x and 1.10x quality with native Spanish listeners
- [ ] Implement and unit-test punctuation splitting before staged live tests
- [ ] Add bounded NMT/TTS queues and ordered drain behavior
- [ ] Keep the private `nvidian/tegra-audio` image reference out of external
  documentation unless NVIDIA explicitly grants Pellera access
