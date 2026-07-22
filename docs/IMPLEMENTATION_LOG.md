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

| Saved trace | Simulated adaptive tail | Arrival-sampled queue p95 | Peak queue | Playback time over 10 s |
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

Completed experiment automation:

- Added `run_three_sermon_experiment.py` to health-check an already running
  deployment, execute the one-minute preflight, and stream Spirit, Blessed, and
  Beholding sequentially.
- Added repeat and interrupted-run recovery controls with `--repeats` and
  `--resume-dir`, plus `--dry-run` for reviewing the resolved plan without
  contacting the backend.
- Pinned the runner dependencies to `websockets==15.0.1`,
  `matplotlib==3.10.9`, and `imageio-ffmpeg==0.6.0` in the root requirements.
- Added timestamped, ignored `experiment_results/` runs that keep raw traces,
  per-sermon summaries and plots, matched playback-policy analysis, and run
  metadata together.
- Made the experiment fail when requested artifacts or valid translated audio
  are missing instead of silently treating partial batch output as success.

Post-review hardening:

- The backend now emits terminal `completed` only after the Riva response
  generator exhausts successfully, the request iterator has consumed its stop
  sentinel, and all pending PCM WebSocket sends finish. Sentinel consumption
  proves every queued source chunk was read; early ASR endpointing restarts the
  generator until input is exhausted. Generator/final-flush errors or
  audio-send failures emit `error` and invalidate the capture. Five seconds of
  output silence is no longer success, and the 300-second maximum is a failure.
- Captures are generated under `.staging`, validated as a complete
  CSV/summary/plot set, promoted, hashed with SHA-256, and validated again.
  Validation reconciles CSV sent/receive counts and received-byte totals with
  the summary. Resume rechecks the stored artifact hashes.
- Resume now requires the original and current worktrees to be clean at the
  same Git commit, with each sermon unchanged by size and SHA-256.
- A backend-keyed local `flock` rejects concurrent harnesses on the same
  machine. It does not coordinate clients on separate machines.
- Candidate p95 is now the exact time-weighted queue p95 over the playback
  window. Reports also include exact time above 10 seconds and the longest
  continuous interval scheduled at 1.10x.

The output contract is:

```text
experiment_results/YYYYMMDDTHHMMSSZ_<git-short-sha>/
├── manifest.json
├── playback_policy_analysis.json
├── playback_policy_analysis.md
├── preflight/
│   ├── test-1min_results.csv
│   ├── test-1min_summary.json
│   └── test-1min_latency.png
└── repeat-01/
    ├── <sermon-stem>_results.csv
    ├── <sermon-stem>_summary.json
    └── <sermon-stem>_latency.png
```

Each `repeat-NN` directory contains that artifact set for all three sermons.
The manifest stores resumable per-capture status, so there is no separate
checkpoint file. `--dry-run` does not contact the backend or create the run
directory. `--resume-dir` requires a compatible existing manifest, validates
provenance and artifact integrity before skipping completed entries, and
retries incomplete or failed entries. It recovers backend and repeat settings
from the manifest; explicitly supplied values must match. Queue-SLA misses
remain reported experimental outcomes; they are not treated like transport,
preflight, zero-output, completion, or artifact-integrity failures.

One live Riva trace is sufficient for both fixed and adaptive playback analysis
because the playback policy is downstream of ASR, NMT, and TTS. Reusing the
same PCM arrival events makes the two policy results a matched comparison and
avoids a second inference pass whose service and network variation would
confound the result. The Python scheduler mirrors the browser policy, but this
automation is not a browser/Web Audio execution. It also does not evaluate
Spanish naturalness or intelligibility and cannot measure semantic delay from
an English joke or marked phrase to the corresponding Spanish audio.

Runs are sequential because the application exposes one active S2S session and
one global timing session. A repeat contains approximately 103.7 minutes
(roughly 1 hour 45 minutes) of source audio for the three sermons. A new run
adds a single one-minute preflight, and every capture adds translated-tail drain
time. The harness deliberately does not start or stop the Riva containers or
FastAPI backend.

Automation validation snapshot:

```text
Python backend + analysis + harness tests: 70 passed
Frontend tests:                           81 passed (Node.js 22)
Frontend lint:                            passed
Frontend build:                           passed (Vite chunk-size warning only)
Docker Compose configuration:             passed
Harness --help and no-write --dry-run:     passed
```

Known experimental limitation:

- Web Audio `playbackRate` changes pitch as well as tempo and may reveal chunk
  boundary artifacts. Native Spanish listener evaluation is required.
- The 10-second threshold cannot be a hard cap while speech is preserved if
  sustained translated media arrives faster than 1.10x consumption.
- Browser queue depth is exact playback backlog, but it is only one component
  of semantic English-to-Spanish delay.

## 2026-07-22: one-repeat live acceptance run

Completed:

- Started all three pinned NIMs on one NVIDIA RTX PRO 6000 Blackwell Server
  Edition and confirmed final usage of 32,217 MiB with 65,034 MiB free.
- Passed the one-minute preflight and captured one new real-time Riva arrival
  trace for Spirit, Blessed, and Beholding.
- Verified terminal completion, summary/CSV consistency, manifest artifact
  hashes, empty staging state, and final service readiness.
- Exercised strict resume after one Blessed TTS failure. The harness
  retained verified work, discarded incomplete staging output, and reran only
  the failed and pending work.

Measured fixed 1.00x versus adaptive playback:

| Sermon | Fixed tail | Adaptive tail | Reduction | Adaptive p95 | Time above 10 s |
|---|---:|---:|---:|---:|---:|
| Spirit | 142.565 s | 36.788 s | 74.2% | 35.823 s | 69.377% |
| Blessed | 204.155 s | 30.520 s | 85.1% | 37.460 s | 78.620% |
| Beholding | 42.011 s | 18.001 s | 57.2% | 18.622 s | 41.569% |

Aggregate tail fell 78.1%, from 388.731 to 85.310 seconds, with no translated
chunks dropped. Every sermon trace nevertheless missed the overall candidate
gate set. The data does not support treating the 10-second value as a bounded
audience experience at the current 1.10x maximum rate.

The first Blessed attempt exposed a separate robustness issue. Magpie's logs
showed that the text reaching TTS contained Chinese `阿门。` for a final
"Amen." fragment despite the Spanish target; the NMT logs did not expose the
translated text directly. The Magpie ensemble failed while mapping it. Direct
TTS, concurrency, and 30-second end-of-file S2S isolation probes later
succeeded, ruling out a simple permanent inability to synthesize the text but
not isolating the cause. Target-language/non-empty validation before TTS is
still required in the staged design.

The detailed procedure, metrics, failure diagnosis, evidence boundaries,
artifact hashes, and next recommendations are in
[Three-sermon acceptance run](ACCEPTANCE_RUN_2026-07-22.md).

## 2026-07-22: staged pipeline foundation

Completed on the stacked `agent/staged-s2s-pipeline` branch:

- Added a direct Nemotron streaming-ASR adapter on `localhost:50052` while
  leaving the active monolithic WebSocket path unchanged.
- Added a bounded ordered ASR event bridge whose blocking worker backpressures
  on a full asyncio queue, with tested terminal events, cancellation, active
  stream exclusion, channel close, and owned-executor cleanup.
- Factored the tested 16 kHz mono, automatic-punctuation, 800 ms RNNT EOU
  request into one builder shared with the existing S2S client.
- Added separate ASR-final and emitted-segment identities with provenance and
  a future stage-event telemetry contract.
- Added deterministic punctuation segmentation across finals, abbreviation
  and decimal protection, Unicode boundaries, exact-once final flush, and
  configurable 240-character/2,000 ms safety valves.
- Added strict direct-ASR completion: early server termination before the
  input sentinel is consumed reports an error rather than success.
- Added a standalone real-time WAV smoke command.

Validation:

```text
Focused staged foundation tests: 79 passed
Full Python regression suite:     149 passed
Live direct ASR smoke:             20.0 s audio, 59 interims,
                                   5 finals, 4 segments, input complete
Live bounded event bridge:         59 INTERIM, 5 FINAL, 1 COMPLETE,
                                   0 ERROR
```

The smoke used the pinned Nemotron ASR Streaming `1.2.0`, Riva client `2.24.0`,
automatic punctuation, and the 800 ms EOU configuration. Detailed contracts,
commands, limitations, and observed results are in
[Staged pipeline foundation](STAGED_PIPELINE_FOUNDATION.md).

## Remaining staged backend implementation

Not yet completed:

- Bounded NMT and TTS queues with pipeline overlap.
- Direct NMT/TTS application adapters and target-language validation.
- Ordered outbound audio and a reorder buffer if worker counts exceed one.
- Persisted per-stage queue residence and inference metrics.
- Feature-flagged staged WebSocket integration and deterministic stage drain.
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

Install the root experiment dependencies, inspect the plan, and run all three
sermons:

```bash
pip install -r requirements.txt
git status --short  # must be empty for a resumable live run
python run_three_sermon_experiment.py --dry-run
python run_three_sermon_experiment.py
```

The defaults are:

- `--backend http://localhost:8000`;
- `--output-root experiment_results`; and
- `--repeats 1`.

`--run-id` gives a new run a deterministic directory name. `--skip-preflight`
is available only when an operator intentionally accepts the loss of that
service-path check; it cannot change a resumed run.

Collect three live traces per sermon or continue an interrupted experiment:

```bash
python run_three_sermon_experiment.py --repeats 3
python run_three_sermon_experiment.py \
  --resume-dir experiment_results/<run-id>
```

The automated report calculates both fixed and adaptive playback from every
live trace. Separately use `http://localhost:5173/#/test` and export its CSV for
an actual Web Audio cross-check. Preserve the entire browser drain; do not stop
after network output goes quiet while the playback queue remains nonzero. A
native-listener review and synchronized joke/marked-phrase measurements are
also separate required activities.

## Next-run record template

Copy this block into the compact summary for each formal run:

```text
Run ID:
UTC start:
Git commit:
Measurement source: automated Python replay | browser Web Audio
Policy: fixed-1.00x | adaptive
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
Simulated time-weighted queue p50 / p95; peak:
Longest continuous 1.10x:
Actual browser queue p50 / p95 / max (browser runs only):
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
- [x] Commit and push the adaptive branch with the initial experiment documents
- [x] Run one automated live trace for all three sermons
- [x] Verify each completed trace produces the matched fixed/adaptive comparison
- [x] Verify terminal completion, PCM-send drain, staged promotion, and hashes
- [x] Verify resume provenance and backend lock behavior after a live failure
- [ ] Run three repeats per sermon after the staged design improves the queue
- [ ] Cross-check replay scheduling with an actual browser/Web Audio run
- [ ] Capture synchronized phrase/punchline delay, not only queue depth
- [ ] Review 1.05x and 1.10x quality with native Spanish listeners
- [x] Implement and unit-test punctuation splitting before staged live tests
- [ ] Add bounded NMT/TTS queues and ordered drain behavior
- [ ] Keep the private `nvidian/tegra-audio` image reference out of external
  documentation unless NVIDIA explicitly grants Pellera access
