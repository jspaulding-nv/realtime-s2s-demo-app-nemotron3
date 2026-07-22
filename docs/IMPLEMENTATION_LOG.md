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
- Completed one long-form run for each of @jgough-essextec's three sample files
  without a WebSocket drop or gRPC failure.

Pinned pipeline:

```text
Nemotron ASR Streaming 1.2.0
  -> Riva Translate 1.6B 1.5.2
  -> Magpie multilingual TTS 1.7.0
```

Historical results:

| Sample | First audio | Service flush tail | Output/input | Fixed 1.00x playback tail |
|---|---:|---:|---:|---:|
| Sample 01 | 16.1 s | 0.0 s | 1.056x | 172.638 s |
| Sample 02 | 2.3 s | 0.4 s | 1.081x | 228.772 s |
| Sample 03 | 4.7 s | 0.8 s | 1.017x | 70.394 s |

The fixed tails shown here were later corrected to the exact end of the final
PCM source chunk. The compact July 8 summaries retain their original
last-chunk-start values as historical capture records; the generated playback
report labels those values as compatibility evidence.

The short service flush tail did not eliminate the listener tail. Spanish
media was longer than the source, and response timing left substantial audio
queued for fixed-rate playback. Sanitized aggregate findings are retained in
`NEMOTRON_TEST_RESULTS.md`; raw captures are intentionally excluded.

Benefit observed from Nemotron 3:

- It uses the Riva team's recommended high-quality English streaming ASR for
  this English-input use case.
- Sample 03 showed a substantial listener-tail improvement and Sample 01 a
  moderate improvement relative to reconstructed prior runs.
- Sample 02 remained effectively unchanged, so the ASR change alone did not
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

No new live Riva sample run using the adaptive browser controller had been
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
| Sample 01 | 27.051 s | 38.08 s | 46.54 s | 80.0% |
| Sample 02 | 40.195 s | 44.71 s | 53.11 s | 77.3% |
| Sample 03 | 16.408 s | 19.10 s | 23.81 s | 45.6% |

The simulated aggregate tail reduction was 82.3%. This is promising but does
not meet the queue goal: 1.10x still left all three traces above the 10-second
soft ceiling for substantial periods. The output also predicts that most
translated media would be accelerated, so a live browser run and native
listener review remain mandatory.

Generated replay artifacts remain under ignored local result directories. They
are deterministic simulations from saved arrival traces, not modified live
Riva results.

Completed experiment automation:

- Added `run_long_form_experiment.py` to health-check an already running
  deployment, execute the one-minute preflight, and stream Sample 01, Sample 02, and
  Sample 03 sequentially.
- Added repeat and interrupted-run recovery controls with `--repeats` and
  `--resume-dir`, plus `--dry-run` for reviewing the resolved plan without
  contacting the backend.
- Pinned the runner dependencies to `websockets==15.0.1`,
  `matplotlib==3.10.9`, and `imageio-ffmpeg==0.6.0` in the root requirements.
- Added timestamped, ignored `experiment_results/` runs that keep raw traces,
  per-sample summaries and plots, matched playback-policy analysis, and run
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
  same Git commit, with each sample unchanged by size and SHA-256.
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
│   ├── preflight_results.csv
│   ├── preflight_summary.json
│   └── preflight_latency.png
└── repeat-01/
    ├── <sample-stem>_results.csv
    ├── <sample-stem>_summary.json
    └── <sample-stem>_latency.png
```

Each `repeat-NN` directory contains that artifact set for all three samples.
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
(roughly 1 hour 45 minutes) of source audio for the three samples. A new run
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
  trace for Sample 01, Sample 02, and Sample 03.
- Verified terminal completion, summary/CSV consistency, manifest artifact
  hashes, empty staging state, and final service readiness.
- Exercised strict resume after one Sample 02 TTS failure. The harness
  retained verified work, discarded incomplete staging output, and reran only
  the failed and pending work.

Measured fixed 1.00x versus adaptive playback:

| Sample | Fixed tail | Adaptive tail | Reduction | Adaptive p95 | Time above 10 s |
|---|---:|---:|---:|---:|---:|
| Sample 01 | 142.433 s | 36.656 s | 74.3% | 35.823 s | 69.377% |
| Sample 02 | 204.144 s | 30.509 s | 85.1% | 37.460 s | 78.620% |
| Sample 03 | 41.806 s | 17.797 s | 57.4% | 18.622 s | 41.569% |

Aggregate tail fell 78.1%, from 388.383 to 84.962 seconds, with no translated
chunks dropped. Every sample trace nevertheless missed the overall candidate
gate set. The data does not support treating the 10-second value as a bounded
audience experience at the current 1.10x maximum rate.

The first Sample 02 attempt exposed a separate robustness issue. Magpie's logs
showed that the text reaching TTS contained Chinese `阿门。` for a final
"Amen." fragment despite the Spanish target; the NMT logs did not expose the
translated text directly. The Magpie ensemble failed while mapping it. Direct
TTS, concurrency, and 30-second end-of-file S2S isolation probes later
succeeded, ruling out a simple permanent inability to synthesize the text but
not isolating the cause. Target-language/non-empty validation before TTS is
still required in the staged design.

The detailed procedure, metrics, failure diagnosis, evidence boundaries,
artifact hashes, and next recommendations are in
[Three-sample acceptance run](ACCEPTANCE_RUN_2026-07-22.md).

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

## 2026-07-22: bounded staged NMT and TTS pipeline

Completed on the stacked `agent/staged-nmt-tts-pipeline` branch:

- Added direct one-segment NMT with an explicit unary deadline, exact-one and
  nonempty-response validation, and an exact `es-US` language check.
- Added direct Magpie TTS with Isabela voice selection, mono Int16 validation,
  active-call cancellation, and atomic whole-segment PCM publication.
- Confirmed live that blank NMT requests can hallucinate fluent output and
  enforced a pre-RPC blank-input rejection.
- Added one NMT worker and one TTS worker on separate executors so stages
  overlap while output remains in source order without a reorder buffer.
- Added bounded NMT, TTS, and output queues, exact natural sentinel drain,
  first-failure ownership, deterministic cancellation, and model deadlines.
- Added queue-depth, blocked-put, queue-residence, processing, first-audio,
  provenance, and PCM-duration telemetry.
- Added an opt-in end-to-end WAV smoke that emits raw PCM and a JSON report.

Validation:

```text
Direct NMT tests:                    24 passed
Direct TTS tests:                    29 passed
Staged orchestrator tests:           23 passed
Full backend regression suite:      176 passed
Full Python regression suite:       225 passed

Live 60-second staged preflight:
  terminal outcome:                 complete
  first translated audio:           5.109 s
  post-input tail drain:             1.307 s
  translated audio segments:        23
  max NMT / TTS / output depth:      2 / 2 / 2
  blocked queue puts:                0
  NMT average processing:          319.84 ms
  TTS average first audio:         145.98 ms
  TTS average full completion:     493.69 ms
```

That milestone left the active browser route monolithic. The subsequent
default-off WebSocket integration is documented in
[Feature-flagged staged WebSocket integration](STAGED_WEBSOCKET_INTEGRATION.md).
The one-minute WebSocket gate later passed. The Sample 03 canary also passed
after the content-safety hardening documented below; new staged Sample 01,
Sample 02, and Sample 03 matrix runs remain pending.

## 2026-07-22: feature-flagged staged WebSocket path

Completed on top of `agent/staged-nmt-tts-pipeline`:

- wired the bounded direct pipeline into `/ws/translate` only when
  `S2S_PIPELINE_MODE=staged`; the default remains `monolithic`;
- created fresh session-owned ASR, NMT, and TTS clients for every staged
  stream so cancellation cannot poison a later stream;
- preserved the existing control/status/error/binary PCM protocol;
- emitted `listening` only after all staged clients and workers started;
- serialized PCM, status, level, and pong WebSocket writes;
- retained FIFO sequence IDs and successful WebSocket send timestamps;
- validated cleanup, outcome, incomplete sequences, and sent/dequeued parity
  before emitting `completed`;
- made duplicate `end_input` idempotent and session replacement await cleanup;
- exported full staged events and summaries from `/api/test/export`;
- exposed active mode and all staged limits through `/api/config`;
- taught the batch and resumable sample harnesses to save and enforce staged
  integrity evidence while keeping audience SLA misses as measurements;
- made the browser dashboard require server completion, network quiet, and an
  empty Web Audio queue for natural success; and
- exposed current/peak browser queue, playback rate/mode, and limit breaches
  in the live translation panel.

Regression validation after integration:

```text
Python:          275 passed
Frontend:         88 passed
Frontend lint:    passed
Frontend build:   passed (existing bundle-size warning only)
Python compile:   passed
git diff check:   passed
```

Live terminal-aware `/ws/translate` preflight with `test_audio/test-1min.wav`:

```text
mode:                            staged (reported by /api/config)
input:                           60.000 s / 200 chunks, complete
first translated client audio:    5.087 s
translated output:               50.295 s / 1,609,442 bytes
output/input whole-prefix ratio:   0.838x
last-audio tail after input:       1.316 s
completed-terminal arrival:       1.374 s
harness drain observation:         2.254 s (poll/settle included)
fixed-rate playback tail:          6.996 s
segments emitted/sent:            23 / 23 (IDs 0-22)
max NMT/TTS/output depths:         2 / 2 / 1
blocked queue puts:                0 / 0 / 0
integrity result:                  passed
```

The run had no failure, cleanup error, incomplete sequence, disconnect,
timeout, container restart, or GPU OOM. All three NIMs remained healthy with
zero restarts. Detailed timings, limitations, reproduction commands, and the
compact evidence record are in
[Feature-flagged staged WebSocket integration](STAGED_WEBSOCKET_INTEGRATION.md).

This one-minute pass does not establish the live-audience SLA. A complete
Sample 03 operational canary subsequently passed after the hardening described
below. New staged runs of all three samples remain pending. Browser queue
p95/peak/time-over-10-seconds and a synchronized joke/marked-phrase delay also
remain separate audience evidence.

## 2026-07-22: staged content hardening and full Sample 03 canary

The full-sample promotion gate exposed two defects that the one-minute
preflight did not reach:

- Attempt 1 stopped approximately 69.1 seconds into Sample 03 when a producer
  capture timestamp arrived behind a newer asyncio age-poll observation and
  triggered `monotonic time cannot move backwards`. The staged consumer now
  supplies a nondecreasing observation timeline to the segmenter while retaining
  the original capture times and Nemotron source-word offsets as evidence.
- Attempt 2 reached 807.9 seconds before Magpie failed on an isolated English
  hesitation final, `uh.`. A targeted ASR replay reproduced that exact fragment;
  Riva Translate 1.5.2 translated it to Chinese `呃。` while reporting the
  requested Spanish target. Direct probes found the same wrong-script class for
  standalone `Okay.` (`好吧。`) and `Amen.` (`阿门。`). This tied the earlier
  Sample 02 `阿门。` observation to a reproducible short-fragment NMT content
  class rather than a permanent TTS or GPU failure.

The mitigation is deliberately layered:

- suppress only exact standalone hesitation fillers (`uh`, `um`, `er`, `erm`,
  and `hmm`, ignoring case and surrounding punctuation) before assigning a
  segment ID, with privacy-safe `filler_discarded` telemetry;
- validate every Spanish-target NMT result immediately after NMT and again at
  the TTS boundary, rejecting empty, wrong-script, mixed-script, control,
  format, symbol, or detached-mark content before it can reach Magpie; and
- use narrow deterministic Spanish overrides for standalone `OK`/`Okay` and
  `Amen` fragments. Meaningful short utterances remain eligible for normal
  translation, and unsafe content is not blindly retried through TTS.

An exact 790-815 second Sample 03-region smoke then completed successfully. It
emitted and synthesized 10 ordered segments, discarded the isolated filler,
passed staged integrity, and reported no pipeline error or incomplete ID.

Attempt 3 completed the entire 1,888.1045-second Sample 03 source through the
feature-flagged staged WebSocket path:

| Measurement | Result |
|---|---:|
| Ordered segment IDs | 646 |
| Exact fillers discarded | 6 |
| Translated output/input duration | 1.01627x |
| First translated audio | 5.113 s |
| Last-audio arrival tail | 0.850 s |
| Completed-terminal arrival after input | 1.744 s |
| Harness drain observation (poll/settle included) | 2.255 s |
| Fixed 1.00x playback tail | 64.038 s |
| Maximum NMT / TTS / output queue depth | 4 / 4 / 1 |
| Blocked NMT / TTS / output puts | 15 / 0 / 0 |

All 646 IDs were emitted, dequeued, and sent in order. The run passed the
terminal and staged-integrity checks with no server error, cleanup error,
incomplete sequence, disconnect, timeout, container restart, or GPU OOM. The
15 blocked NMT puts demonstrate that bounded backpressure was exercised rather
than bypassed.

The post-run playback analyzer also exposed and fixed a 300 ms boundary error:
new CSVs contain an explicit `client/input_ended` event, but the loader still
used the start timestamp of the last input chunk. It now prefers the explicit
boundary; older traces use the exact end of their final PCM chunk, while old
start-boundary summaries are accepted only as annotated compatibility
evidence. Regression tests cover both rules. The Sample 03 replay matches its
explicit-boundary 64.038-second fixed tail within 7 microseconds. The adaptive
replay reaches a 14.246-second tail (77.75% lower) but still peaks at 28.052
seconds of queued media.

The final release audit then hardened cases not exercised by the live canary:

- reject a `completed` control before client `end_input`;
- require exact successful-server-send/client-receive PCM frame and byte
  parity for staged captures;
- freeze full declared ASR/NMT/TTS model configuration for new runs and
  reject incompatible resume checkpoints;
- record exact completed-terminal arrival separately from harness
  polling/settle duration;
- make staged cleanup singleton and cancellation-safe, prevent a displaced
  session from restarting, and avoid lifecycle locks across socket writes;
- route unknown controls through the staged terminal latch; and
- reject non-Magpie-safe target punctuation, including CJK full stop, without
  logging translated text.

The retained attempt-3 trace satisfies the new terminal order and byte-parity
checks, but its old summary lacks `modelConfig` and the new top-level timing
fields. It is therefore historical/non-resumable evidence. The final edge
hardening is unit/integration-tested and still needs a fresh GPU preflight
before the remaining long-form matrix.

Post-hardening validation snapshot:

```text
Focused backend hardening:                125 passed
Focused harness hardening:                 59 passed
Full backend suite:                       264 passed
Full Python backend + analysis + harness: 344 passed
Frontend tests:                            88 passed (Node.js 22)
Frontend lint:                             passed
Frontend build:                            passed (existing bundle-size warning only)
```

This is an operational canary pass, not an audience-latency acceptance. Its
64.038-second fixed-rate listener tail still illustrates the delayed-joke risk.
An actual browser/Web Audio run, synchronized English-to-Spanish phrase timing,
native Spanish quality review, and a new full staged Sample 01/Sample 02/Sample 03
matrix remain required.

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
samples:

```bash
pip install -r requirements.txt
git status --short  # must be empty for a resumable live run
python run_long_form_experiment.py --dry-run
python run_long_form_experiment.py
```

The defaults are:

- `--backend http://localhost:8000`;
- `--output-root experiment_results`; and
- `--repeats 1`.

`--run-id` gives a new run a deterministic directory name. `--skip-preflight`
is available only when an operator intentionally accepts the loss of that
service-path check; it cannot change a resumed run.

Collect three live traces per sample or continue an interrupted experiment:

```bash
python run_long_form_experiment.py --repeats 3
python run_long_form_experiment.py \
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
- [x] Run one automated live trace for all three samples
- [x] Verify each completed trace produces the matched fixed/adaptive comparison
- [x] Verify terminal completion, PCM-send drain, staged promotion, and hashes
- [x] Verify resume provenance and backend lock behavior after a live failure
- [ ] Run three repeats per sample after the staged design improves the queue
- [ ] Cross-check replay scheduling with an actual browser/Web Audio run
- [ ] Capture synchronized phrase/punchline delay, not only queue depth
- [ ] Review 1.05x and 1.10x quality with native Spanish listeners
- [x] Implement and unit-test punctuation splitting before staged live tests
- [x] Add bounded NMT/TTS queues and ordered drain behavior
- [x] Integrate the staged pipeline into `/ws/translate` behind a default-off flag
- [x] Pass the terminal-aware one-minute staged WebSocket preflight
- [x] Pass one complete staged Sample 03 operational canary
- [ ] Run the full staged Sample 01, Sample 02, and Sample 03 matrix
- [x] Keep unapproved private/internal container references out of external
  documentation
