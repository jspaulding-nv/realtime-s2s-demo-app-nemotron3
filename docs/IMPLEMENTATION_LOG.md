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

Live terminal-aware `/ws/translate` preflight with the local `preflight.wav`:

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

## 2026-07-23: narrow NMT recovery and failure evidence

A new staged Sample 02 run failed closed when Riva Translate 1.6B `1.5.2`
declared `es-US` but returned unsupported wrong-script content for one isolated
short source segment. The target validator prevented that text from reaching
TTS. Privacy-safe controlled replay isolated a deterministic request-shape
boundary:

| Replay condition | Validation result |
|---|---:|
| Exact isolated ASCII-token plus punctuation shape | failed, 5/5 |
| Same token without terminal punctuation | passed, 5/5 |
| Same token with preceding context | passed, 5/5 |
| Same token with following context | passed, 5/5 |
| Same token with both neighboring contexts | passed, 5/5 |

This evidence ruled out a transient RPC, TTS, concurrency, or GPU-capacity
failure. Repeating the unchanged request was therefore rejected as a recovery
strategy.

The direct NMT adapter now owns one narrowly defined alternate request:

- exact target `es-US`;
- source text, after trimming, of 1-32 ASCII letters followed by exactly one
  `.`, `?`, or `!`;
- first RPC cardinality and response language are valid, but translated text
  raises `TargetTextValidationError`; and
- one second request removes only the terminal punctuation.

The returned object keeps the original segment, sequence ID, ASR-final
provenance, source timing, and emission reason. The second NMT response is
fully revalidated before TTS. RPC, cardinality, response-language, ineligible
source, and second-attempt failures receive no extra attempt. Neither NMT nor
TTS ever repeats an unchanged payload.

Recovery is observable without recording transcript text. A completed NMT
event carries `retry_count` zero or one, and the staged summary's
`nmt_retry_count` must equal the sum across those events. If the alternate
request also fails, a typed NMT error event retains the original sequence and
provenance with `retry_count=1`; no translated text enters telemetry or TTS.

Failure evidence handling was hardened at the same boundary:

- staged shutdown retains an exportable snapshot before asynchronous cleanup
  and a finalized snapshot afterward;
- the batch client polls for the finalized `closed` snapshot for a bounded
  close-settling interval;
- staged integrity requires the closed state and consistent retry totals; and
- a failed long-form capture retains only its generated event CSV, summary, and
  latency plot under neutral names in an ignored owner-private directory, with
  paths and SHA-256 hashes recorded under `failure_artifacts`.

Source audio, generated audio, arbitrary temporary files, logs, and credentials
are not copied into the failed-capture record. The complete behavior,
verification gates, and clean rerun order are documented in
[Narrow NMT recovery for short punctuated segments](NMT_SHORT_SEGMENT_RECOVERY.md).
At that point, the next gates were a targeted Sample 02 pass followed by a
fresh preflight and clean three-sample matrix.

Local verification passed `376` Python tests with one skipped integration test,
all `88` frontend tests, frontend lint, and the production build. All three
pinned service HTTP readiness probes returned ready. A privacy-safe live call
through the updated direct adapter used the exact protected failure segment and
reported one retry, preserved sequence 77, returned exact `es-US`, and passed
target validation without printing source or translated text.

## 2026-07-23: full Sample 02 post-recovery canary

Commit `55b59bd` completed one standalone 2,427.011-second Sample 02 run through
the hardened staged WebSocket path. The artifact reached `closed` / `complete`
and passed staged integrity with:

- 805 emitted, NMT-completed, TTS-completed, produced, WebSocket-sent, and
  client-received audio segments;
- contiguous unique sequence IDs 0–804 and exact PCM count-and-byte parity;
- no incomplete IDs, pipeline failure, cleanup error, connection loss, drain
  timeout, server error, or PCM after terminal completion;
- three validated NMT recoveries at sequence IDs 77, 92, and 449;
- maximum NMT/TTS/output queue depths of 4/4/1; and
- 17/5/0 blocked puts, demonstrating bounded backpressure without drops.

First audio arrived after 4.807 seconds. Last translated audio arrived 0.308
seconds after source input ended, and the completed terminal arrived after
1.455 seconds. Operationally, the recovery and drain paths passed.

The audience gate did not pass. Translated PCM totaled 2,641.342 seconds,
1.08831x the 2,427.011-second input, and fixed 1.00x arrival replay ended
239.156 seconds late. This makes 1.10x a more plausible catch-up candidate than
1.05x for this sample, but executed browser playback, marked-phrase timing, and
native-Spanish quality review remain required.

This targeted canary is not part of a resumable experiment manifest and must
not be treated as a completed matrix checkpoint. The compact, transcript-free
record is
[Sample 02 post-recovery staged canary](STAGED_SAMPLE_02_RECOVERY_CANARY.md).

## 2026-07-24: VM restart readiness observation

The completed canary artifacts survived a later VM lease expiry and shutdown.
After the VM returned, all three Compose-managed NIM containers restarted at
approximately 00:03:24 UTC and reported `healthy`; their `unless-stopped`
policy behaved as configured. The separately launched FastAPI process did not
restart and its staged endpoint was not listening.

The next formal experiment must therefore relaunch FastAPI, verify `/` and
`/api/config`, and pass a new 60-second preflight before starting a clean
three-sample matrix. Healthy model containers alone are not sufficient
readiness after a host restart.

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

## July 24, 2026: intermittent short-target TTS recovery

The first post-reboot provenance-frozen matrix passed preflight and then
failed during Sample 01 at source position 39.3 seconds. Sequence 7 had a
three-character source and validated two-character `es-US` target. NMT
completed normally with no retry, but Magpie returned gRPC `UNKNOWN` with an
internal Triton zero-token tensor mismatch.

Retained evidence ruled out queue pressure, transport loss, GPU exhaustion,
container restart, OOM, and a persistent service failure. The same source
hash and sequence shape had succeeded earlier, and a direct known-good TTS
call passed after the failure.

A new privacy-safe `diagnose_short_segment.py` replayed the first 39.3 seconds
through the production-style asynchronous ASR bridge and age-polling segmenter.
It retains no text, path, filename, or endpoint hostname and uses only
structural counts plus a per-run keyed HMAC whose key is discarded. The replay
recreated sequence 7 exactly: three source characters and a validated
two-character target. With client retry disabled, four of five exact TTS calls
passed and one reproduced gRPC `UNKNOWN`. The adjacent-context translation
passed five of five calls. With retry enabled, all 20 isolated calls passed and
two recorded `client_retry_count=1`, directly exercising successful recovery
against the live pinned service.

The direct TTS adapter now permits one retry only for gRPC `UNKNOWN`, controlled
by `STAGED_TTS_MAX_RETRIES` (`0` or `1`, default `1` for the staged path).
Both attempts remain inside one orchestrator deadline and use separate private
PCM buffers. Validation, cancellation, timeout, resource, format, and local
safety failures are never retried. Successful and exhausted recovery paths
retain sequence attribution and privacy-safe retry telemetry. Batch integrity
reconciles `tts_retry_count` against completed TTS events.

True post-NMT coalescing was intentionally deferred. It requires grouped
synthesis units and grouped sequence accounting. The five-call context result
is promising but does not establish a general classifier, and broadly holding
short utterances would add avoidable audience delay. See
[Atomic TTS recovery for an intermittent short-segment failure](TTS_SHORT_SEGMENT_RECOVERY.md).

The failed `700aeec` matrix remains an immutable baseline. Because the harness
correctly rejects cross-commit resume, the recovery must be evaluated in a
fresh formal matrix after commit, backend restart, and preflight.

## July 24, 2026: clean staged recovery matrix

Recovery commit `636f4784797372a8b6092255d0438951f9300c0b` passed the
one-minute preflight and completed one new real-time trace for each neutral
long-form sample. The repository was clean, the manifest froze the pinned
ASR/NMT/TTS digests, and all promoted artifact hashes validated.

The three captures produced 2,027/2,027 ordered PCM segments with exact
server-send/client-receive count and byte parity. Three guarded NMT retries
recovered during Sample 02. No TTS retry was needed, and there was no incomplete
sequence, connection loss, timeout, pipeline failure, or cleanup error. A
separate immediate post-run check, outside the hashed manifest, found all
containers healthy with zero restarts or OOM events.

The run is an operational pass and an audience-latency miss. Matched no-drop
adaptive replay reduced the sum of fixed listener tails from 526.441 seconds to
117.516 seconds, or 77.677%. Per-sample adaptive queue p95 remained 61.414,
38.158, and 23.210 seconds. The controller already played 78.650-94.811% of
translated media at 1.10x, so threshold-only tuning is unlikely to establish the
5-10 second objective.

Retain the ignored raw artifacts separately. The tracked, transcript-free
result and next-experiment decision are in
[Staged recovery three-sample matrix](STAGED_RECOVERY_MATRIX_2026-07-24.md).

The offline analyzer now accepts repeatable `--constant-rate` and
`--media-duration-scale` options. Its optional capacity report preserves every
chunk and records tail, time-weighted queue p50/p95, peak, time above 10
seconds, captured chunk-duration quantiles, and 30/60/300-second wall-clock
media-arrival p95. The default analyzer output remains byte-compatible when
the new flags are absent. Constant rates through 1.15x did not reach queue p95
at or below 10 seconds on any of the three captured traces.

The post-sweep full Python suite passed 413 tests with one optional local-trace
test skipped. Independent review matched the rolling-rate implementation
against a brute-force calculation over 10,000 randomized cases.

## July 24, 2026: privacy-safe TTS duration capacity model

The completed matrix retained character counts on TTS-start telemetry and PCM
duration on matching TTS-completed events. A new deterministic
`analyze_tts_duration.py` joins those fields by sequence ID only after
validating clean staged outcome, contiguous and paired sequences, parent
counts, WebSocket parent IDs, and retry totals. Its output excludes transcript,
audio, paths, filenames, endpoints, and session IDs.

The 2,027 structural records fit
`audio_seconds = 0.488769 + 0.056789 * translated_characters`, with
`R² = 0.853396`. Leave-one-sample-out residuals yielded a 44-character
4-second-p95 limit and a 46-character 8-second observed-max-residual limit.
Requiring aggregate, per-sample, and cross-sample constraints to pass within
the observed character range on a five-character grid selected 40 characters.

Call amplification prevents treating that cap as automatically beneficial.
Ideal packing would increase calls by at least 71.8%, 58.3%, and 34.0% for
40-, 45-, and 60-character caps. The aggregate intercept counterfactual
corresponds to 10.7%, 8.7%, and 5.1% extra captured output. These are risk
estimates, not live split results. A default-off composite child sequence
contract and matched unsplit/40/45/60 five-minute canary are required before
another full matrix. See
[Post-NMT TTS subsegment capacity model](TTS_SUBSEGMENT_CAPACITY_MODEL.md).
The post-model Python backend, analysis, and harness suite passed 431 tests
with one optional local-trace test skipped; the focused analyzer suite passed
18 tests.

## July 24, 2026: default-off post-NMT TTS subsegmentation

The feature-flagged staged path now keeps one complete NMT parent and can split
only its validated target text before TTS. Zero disables splitting; positive
caps use punctuation, whitespace, and bounded hard-fallback rules with exact
normalized reconstruction tests.

Every enabled TTS child carries a stable
`(parent_sequence_id, subsequence_id, subsequence_count)` identity. The bounded
queues, single TTS worker, output relay, atomic retry, error attribution,
WebSocket sends, and telemetry preserve that identity. Raw run evidence remains
privacy-sensitive because it can include session IDs, timings, paths, and
endpoints, so it stays under ignored result directories. The pipeline marks a
parent complete only after its final child is dequeued; the WebSocket layer
separately proves that every completed child was successfully sent before the
capture is accepted as end-to-end complete.

Enabled captures use telemetry schema v2 with distinct planned, synthesized,
dequeued, and WebSocket-sent child lists. The batch gate checks every lifecycle
stream and compares client/server PCM sizes frame by frame. Disabled captures
remain schema v1, and older captures with no explicit version retain legacy
`(sequence, 0, 1)` semantics. The duration analyzer preserves the published v1
digest and generated files byte for byte while adding composite v2 pairing.

`run_tts_subsegment_canary.sh` creates one shared five-minute prefix and runs
the disabled, 40-, 45-, and 60-character policies sequentially. It manages
only its own FastAPI process and leaves the pinned NIMs untouched.
Implementation details and gates are in
[Default-off post-NMT TTS subsegmentation](TTS_SUBSEGMENT_IMPLEMENTATION.md).

A non-formal 60-second-per-arm live probe passed every integrity, provenance,
terminal, and cleanup gate. Splitting reduced child-duration p95 by 64.5–71.0%
but increased total generated audio and worsened adaptive queue p95 and
listener tail in every enabled arm. The 60-character policy was least harmful
but still did not beat the disabled control. First audio remained about
15.5–15.7 seconds. See
[Post-NMT TTS subsegmentation: 60-second live probe](TTS_SUBSEGMENT_60S_PROBE_2026-07-24.md).
The final local Python suite passed 514 tests with one optional local-trace
test skipped.

The formal five-minute matrix then ran from clean commit `4367931`. All four
arms used the same 1,000 input chunks and 74 upstream NMT parents, passed
immutable image provenance and matched-design checks, completed without a
retry or dropped chunk, and left all three pinned NIMs healthy. The disabled
control had a 19.339-second adaptive queue p95 and 30.510-second adaptive tail.
Every split cap was worse: cap 60, the least harmful, increased adaptive queue
p95 by 12.0% and tail by 8.1%. Splitting also left first audio effectively
unchanged at about 15.5 seconds. The feature therefore stays disabled. See
[Post-NMT TTS subsegmentation: five-minute matched canary](TTS_SUBSEGMENT_5MIN_CANARY_2026-07-24.md).

## July 24, 2026: default-off incremental TTS publication

The staged pipeline can now publish frame-aligned Magpie PCM while one TTS RPC
is still active. The feature is disabled unless
`STAGED_TTS_INCREMENTAL_PUBLISH=1`; the framing default is 100 ms. Incremental
publication and post-NMT TTS subsegmentation are mutually exclusive for the
first experiment, so schema-v3 identity is exactly
`(parent_sequence_id, audio_frame_id)`.

The blocking adapter privately reframes variable Riva responses, commits a
frame only after bounded output-queue insertion is acknowledged, and returns
an authoritative parent completion with frame and byte totals. A genuine
gRPC `UNKNOWN` can retry once only before the first committed frame. After a
committed prefix, any failure sends that prefix once and then one terminal
error without replay. Abort wins an acquire/abort race until queue insertion
has linearized the commit, preventing a reserved terminal from overtaking
uncommitted PCM.

The pipeline, WebSocket relay, batch gate, smoke tool, and latency analyzer
now reconcile production, dequeue, server-send, and client-receive frame
identities and bytes. The schema-v3 analyzer measures the first-frame lead
against the same parent's TTS completion, avoiding a causal comparison across
stochastic synthesis runs. `run_streaming_tts_canary.sh` automates a
provenance-checked atomic/schema-v3 pair, and its comparator marks cross-arm
queue and tail differences inconclusive whenever generated audio differs
materially.

The complete Python backend and analysis suite passed 575 tests with one
optional local-trace test skipped. Python compilation, shell syntax, and
whitespace checks passed. Frontend tests were not rerun on this VM because its
Node.js 12 runtime is below the repository's declared Node.js 20.19 minimum;
the schema-v3 wire contract remains ordinary ordered binary PCM and required
no frontend source change.

The first clean schema-v3 canary from `9505679` then reproduced the known
two-character Magpie `UNKNOWN` after two 100 ms frames had committed. The
adapter correctly refused to retry or replay that prefix and failed closed,
but the run demonstrated that the diagnosed tiny-target shape needs atomic
retry safety even when normal parents publish incrementally.

Commit `57c7ffa` added a default four-character atomic fallback inside schema
3. Tiny validated targets keep PCM private through iterator completion and the
one allowed `UNKNOWN` retry, then reframe the successful attempt through the
same bounded frame publisher. Longer targets retain true incremental delivery.
Fallback identity now reconciles across adapter completion, pipeline events,
output barriers, WebSocket completion, batch validation, latency analysis, and
the matched canary comparator. Direct incremental-benefit metrics exclude
fallback parents; audience-facing metrics retain them. Older schema-v3
artifacts normalize omitted fallback fields to zero/empty.

The post-fix suite passed 605 tests with one optional local-trace test skipped.
The clean 60-second rerun completed all 16 parents in both arms. Schema 3
selected parent 7 as its only atomic fallback and completed all 448 PCM frames
without retry or failure. Across the 15 true-incremental parents, first PCM
reached the WebSocket 35.5 ms p50 / 48.6 ms p95 after the first TTS response
and led full-response completion by 347.5 ms p50 / 1.054 s p95.

The atomic and schema-3 arms generated 52.199 and 43.886 seconds of speech,
respectively, a 15.93% workload difference. Queue and tail deltas therefore
remain confounded. First audio also remained about 15.1-15.5 seconds. This is
an operational/direct-publication pass, not an audience-delay pass. See
[Incremental TTS publication: 60-second formal canary](STREAMING_TTS_60S_CANARY_2026-07-24.md).

The next clean five-minute matched canary ran from `29cdf4e`. Both arms
completed the same 1,000 input chunks and 74 parent structure without a model
retry, incomplete parent, terminal failure, or cleanup error. Schema 3
delivered 2,782 frames and again classified the two-character parent 7 as its
only atomic fallback.

Across 73 true-incremental parents, first PCM followed the first TTS response
by 35.5 ms p50 / 46.7 ms p95 and led full TTS completion by 348.1 ms p50 /
1.659 s p95. The direct publication benefit therefore persisted over five
minutes. Cross-arm playback remained confounded because generated duration
differed by 7.43%.

The schema-3 arm independently missed the live-audience bound: the no-drop
adaptive queue had a 17.077-second time-weighted p95, 23.412-second peak, and
27.120-second listener tail. It spent 44.286 seconds above the nominal
10-second limit even though 55.20% of source audio was accelerated and 30.69%
played at 1.10x. First audio remained 15.196 seconds.

The unchanged no-drop policy is therefore not promoted directly to another
three-fixture matrix. The next experiment is a deterministic hard
freshness-cap simulation over the completed schema-3 arrival trace, reporting
the explicit fidelity cost of whole-parent eviction at 5/8/10-second caps. See
[Incremental TTS publication: five-minute matched canary](STREAMING_TTS_5MIN_CANARY_2026-07-24.md).

## July 24, 2026: schema-3 whole-parent freshness-cap simulation

The offline follow-on now joins client PCM arrivals to schema-3
`(parent_sequence_id, audio_frame_id)` evidence only after parent-summary,
frame-key, byte, order, timestamp, completion, and input-boundary layers
reconcile. Public analyzer output contains only fixed role labels, hashes,
numeric identities, counts, bytes, durations, and timing.

The causal simulator applies the existing 1.00x/1.05x/1.10x decision before
any loss. It can evict only a complete parent whose frames are all
not-yet-audible, then compacts retained future frames while preserving their
already-selected rates. It compares minimum oldest-first eviction with a
jump-to-latest-complete policy and reports every residual breach rather than
claiming an unachieved hard cap. Existing no-drop simulation output is
unchanged.

Primary results use a 100 ms cancellation guard so audio beginning effectively
“now” is protected. The analyzer also replays 0, 50, 100, and 250 ms guards for
the 10-second oldest-first candidate.

The validated five-minute trace had 2,782 frames, 74 parents, and 274.369
seconds of translated audio. No-drop queue p95/peak/tail were
17.077/23.412/27.120 seconds. The most useful candidate was 10-second
oldest-first: queue p95 fell to 8.352 seconds, peak to 14.059 seconds, and tail
to 12.672 seconds. It retained 85.77% of generated audio, meaning it skipped 8
parents and 39.056 seconds of speech. It still spent 4.180 seconds above the
cap.

The 10-second oldest-first headline was identical at 0/50/100 ms. At 250 ms it
retained 86.83% instead of 85.77%, while queue p95 was 8.354 seconds and time
above cap was 4.203 seconds. The candidate conclusion is therefore stable
across the tested practical margins, though the selected parent IDs change.

None of the six scenarios achieved its configured limit. Unaccelerated parent
source-PCM duration was 11.331 seconds at p95 and 14.257 seconds maximum, but
those media durations are not themselves rate-adjusted queue depth. The
definitive failure evidence is the recorded residual breach after all eligible
whole-parent evictions: incomplete, already-audible, or protected audio still
exceeded each selected cap.

Loss remains disabled. The next safe gate is versioned, opt-in parent/frame
wire metadata plus observation-only browser telemetry, followed by an opt-in
short-lookahead scheduler. See
[Schema-3 whole-parent freshness-cap simulation](SCHEMA3_FRESHNESS_CAP_SIMULATION_2026-07-24.md).

## July 25, 2026: observation-only parent/frame metadata

The staged schema-3 incremental-TTS path now advertises audio metadata protocol
version 1 and accepts an explicit per-stream opt-in. Under the existing
serialized WebSocket send lock, every translated PCM frame is preceded by an
exact numeric `audio_frame` header, and every parent ends with an
`audio_parent_complete` marker that reconciles frame and byte totals. Legacy
clients continue to receive anonymous binary PCM. The main translation screen
remains legacy; only the test dashboard and CLI evidence clients opt in.

Backend, Python, and browser receivers fail closed on missing, extra,
misordered, mismatched, cross-generation, incomplete, or post-terminal
evidence. Protocol fields exclude text and wall-clock identity. Raw capture
artifacts remain private because they also contain operational paths and
endpoints.

The real-time file harness anchors input PCM sample zero and records a
contiguous source-sample ledger. It can therefore report source-end to binary
receipt in one client-monotonic clock. The browser additionally records
Web Audio scheduling coordinates and a projected scheduled start without
changing rate, order, buffering, or playback. End-only `audio_processed`
offsets are labeled non-semantic; scheduled start is not claimed as physical
audibility. A synchronized phrase marker and output loopback are still needed
to measure the audience's English-joke to Spanish-audio delay directly.

The long-form runner persists protocol choice in immutable manifest provenance,
propagates it through preflight and all three samples, validates saved wire and
summary evidence on resume, and rejects legacy-to-v1 upgrades. The freshness
analyzer prefers explicit version-1 wire identity while retaining strict
backward compatibility for older positional schema-3 captures. Its 10-second
oldest-first parent policy remains a counterfactual: no live audio is dropped.

Before live capture, the complete backend suite passed 416 tests, the root
Python suite passed 317 tests with one optional local-artifact test skipped
(733 passing Python tests combined), and the frontend passed 118 tests plus
TypeScript build and lint. Protocol,
privacy, clock, rollout, and reproduction details are in
[Audio metadata observation protocol v1](AUDIO_METADATA_OBSERVATION_V1.md).

The first clean live candidate completed both 60-second model arms, but the
formal comparator rejected a legacy-control artifact that contained a
client sample-zero marker despite not negotiating v1. Commit `062f28a` scoped
the marker to negotiated streams and added a regression test.

The corrected formal canary then passed all matched-design, provenance, wire,
parent, byte, terminal, drain, and cleanup gates. The schema-3 arm reconciled
521 frames and 16 parents. Source end to client receipt was 2.656 seconds p50 /
3.915 seconds p95, while source end to deterministic scheduled start was
8.264 seconds p50 / 12.860 seconds p95. These source ranges are not proven
semantic boundaries, and scheduled start is not actual audibility.

The 10-second oldest-first shadow policy would have retained 87.3% of
translated audio and skipped one parent, yet it still peaked at 11.317 seconds.
Loss remains disabled. See
[Audio metadata protocol v1: 60-second formal canary](AUDIO_METADATA_60S_CANARY_2026-07-25.md).

## July 25, 2026: semantic source-event parent-envelope gate

The first semantic-event gate is now implemented without changing audio
metadata protocol v1. A private exact-schema sidecar binds anonymous
`event-NNN` markers at exact source PCM samples to both the SHA-256 of one Test
Dashboard CSV and the SHA-256 plus padded sample count of the exact Int16 wire
image. The browser constructs, hashes, and streams from that same immutable
PCM buffer. At least two independent reviewers are required by default. The
public analyzer output contains only hashes, anonymous IDs, numeric
identity/timing, counts, and explicit claim booleans; it excludes transcript,
translation, filenames, paths, free text, and ignored CSV columns.

The browser and CLI real-time file senders now anchor source sample zero before
their first wait and release each complete PCM chunk at its absolute source-end
deadline. The first two default chunks therefore transmit at 300 and 600 ms,
not at 0 and approximately 600 ms. Timer lateness remains measured, the final
chunk completes without an extra interval, and stop/restart invalidates stale
timers. Protocol-v1 summaries record the privacy-safe monotonic sample-zero
anchor, fixed absolute-deadline formula, successful chunk count, and measured
minimum/maximum emission-minus-deadline margins. Live validation recomputes
those margins from the client event ledger; resumed long-form validation
replays the serialized CSV against the summary and rejects missing, malformed,
early, or inconsistent evidence. The playback-tail fallback now treats a
proven chunk-end send timestamp as that chunk's end while preserving the
historical add-duration rule for legacy or provenance-free traces. Older
start-boundary-paced captures are not formal gate evidence.

`analyze_semantic_event_latency.py` independently reconciles the capture hash,
marker schema, reviewer count, contiguous input ledger, chunk-end pacing,
exact PCM digest/count, protocol version/generation, attributed source ranges,
parent/frame identity, bytes, receipt/completion timing, AudioContext
projection, 16-kHz mono Int16 duration, global wire order, and non-overlapping
projected playback chronology. It allows different source markers within one
ASR final while assigning their shared conservative parent envelope a
deterministic anonymous group ID. Marker counts and unique candidate-group
counts are reported separately so repeated markers are not presented as
independent trials. Duplicate source samples, incomplete evidence, end-only
offsets, backward-moving ranges, unsupported overlaps, ambiguous marker
attribution, or impossible timelines fail the complete analysis. Ordered
same-end suffix overlaps are accepted as a Nemotron/punctuation provenance
shape, but a marker inside more than one distinct range still rejects the
complete gate.

For an explicitly supplied SLA, the result is PASS only when the complete
clock-linkage-adjusted candidate parent envelope finishes by the limit, FAIL
when even its conservative projected first-frame start lower bound is too
late, and INCONCLUSIVE when the adjusted envelope straddles the limit. This
does not identify the target-language landmark or prove physical audibility. A
target-sample annotation, common-clock digital loopback, and ultimately a
two-channel physical recording remain higher evidence tiers. CLI exit codes
distinguish PASS (`0`), FAIL (`1`), invalid arguments/evidence or report-output
I/O failure (`2`), and INCONCLUSIVE (`3`) for automation. Multi-report output
is staged before installation and rolled back as a set on expected
installation failures.

Review found and closed an initial false-PASS path that trusted projected
schedule fields without independently reconciling their source-boundary and
AudioContext clocks. Later review also closed missing source-PCM binding,
self-declared-only CLI pacing provenance, automation exit-status, partial-tail
fallback, and repeated-parent-counting ambiguities. The final validation passed
425 root Python tests with one optional test skipped, 418 backend tests, and
150 frontend tests. Frontend lint/build, Python compilation, shell syntax,
Compose configuration, and whitespace checks also passed. No live semantic
marker result is claimed yet. See
[Semantic source-event latency gate](SEMANTIC_EVENT_LATENCY_GATE.md).

The first live Test Dashboard preflight then exposed 170 false projected
overlaps, up to 16.0 ms, even though all 496 AudioContext frame intervals were
non-overlapping. The cause was a fresh `performance.now()` to quantized
`AudioContext.currentTime` projection for every frame. Browser projection now
advances by `max(schedulePerformance, previousProjectedEnd)`, while the
analyzer independently verifies that recurrence and the corresponding
AudioContext recurrence across parent boundaries. A matched rerun passed every
mechanical invariant: 200 input chunks, 23 translated parents, 520 output
frames, 0.0-55.1 ms input pacing lateness, 1.642-8.254 second projected-start
source-end lag, 1.742-8.312 second projected-end source-end lag, and a
6.563-second projected tail after input ended. It has no human marker sidecar,
so it is not a semantic PASS/FAIL result. See
[Semantic gate browser preflight](SEMANTIC_EVENT_GATE_PREFLIGHT_2026-07-25.md).

Final review initially added a 25 ms per-frame playback-wait linkage limit and
a 50 ms capture-wide client/AudioContext offset-span limit. The matched
60-second browser capture remained valid at a 17.8 ms maximum residual and
28.3 ms span. The first long-form browser run correctly tested those registered
limits unchanged. It completed 645 translated parents and 19,544 output
frames, but is **INVALID evidence**, not a semantic PASS or FAIL. Three ASR
finals lacked a word-derived source start, affecting three parents and 51
frames. Separately, one discrete browser clock-offset step produced a 254.4 ms
maximum linkage residual and a 286.9 ms capture span. No marker sidecar or
two-person semantic review was performed. See
[Semantic event gate long-form diagnostic](SEMANTIC_EVENT_GATE_LONG_FORM_DIAGNOSTIC_2026-07-25.md).

The vNext evidence model replaces those provisional fixed residual/span
thresholds. The browser now records a continuous clock trace at a nominal
200 ms cadence while audio is queued, plus session, queue-start, queue-drain,
and stop boundaries. Every queued interval must have exactly one unique,
ordered start/drain pair and no performance-clock or AudioContext sample gap
above 500 ms. Every observation retains the `performance.now()` before/after
bracket around `currentTime`. An initialized `AudioContext.getOutputTimestamp()`
pair is added only when both components are nonfuture and no more than 500 ms
old; stale, future, zero, failed, or unavailable output timestamps downgrade to
the bracket basis.

The analyzer combines every sample bound with every per-frame schedule offset,
plus a monotonic cross-corner rectangle between every adjacent sample inside a
queued interval. Its lower corner is the previous `performanceBefore` minus
the next `currentTime`; its upper corner is the next `performanceAfter` minus
the previous `currentTime`. These broad rectangles conservatively contain a
transient offset excursion between timer callbacks. The analyzer forms one
capture-wide observed client/AudioContext offset interval and applies a
pre-registered 32 ms guard to each side. Semantic decisions map the candidate
frames' scheduled AudioContext start/end coordinates through that guarded
interval. A discrete clock step can only widen the decision bounds; it cannot
improve a PASS. The historical projected client-clock recurrence and the raw
residual/span remain validation/diagnostic evidence, but the formal decision
no longer relies on a fixed 25/50 ms rejection model.

ASR attribution is now a separate prerequisite. Before another full gate, run
the exact long-form padded PCM twice against the pinned direct ASR service:

```bash
python3 asr_final_attribution_gate.py \
  --file test_audio/long-form-03-30min.wav \
  --uri 127.0.0.1:50052 \
  --docker-container <local-asr-container> \
  --runs 2 \
  --json-output \
    experiment_results/semantic-event-gate/asr-final-attribution.json
```

The runner uses the same absolute chunk-end pacing as the browser gate. It
passes only when both complete real-time runs share one exact padded PCM
SHA-256/sample count, produce nonempty finals, and report zero finals missing
word offsets. Each run also records maximum/mean chunk-release lateness and
must remain within the preregistered 250 ms maximum, preventing a suspended VM
from qualifying an overdue burst as real-time delivery. The replacement
browser capture must use that same padded PCM identity. Any end-only final
still blocks semantic analysis; neighboring ranges are never used to invent a
start. Qualification artifacts remain ignored and transcript-free.

Adversarial review also removed Web Audio decoding as an input-identity
confound for the registered file. Both the browser and qualification runner now
pass through the exact little-endian 16 kHz mono PCM16 RIFF `data` bytes and
zero-pad only the final 4,800-sample wire chunk. The next run is registered to
30,211,200 padded samples and SHA-256
`9ad08fe1e83e714c48dde4f971606431302fae9d3079293b73ab8ff99d1d8143`.
The historical Web Audio-derived digest
`b3e622c467fb4be80622df5c2fea708cf08ca49a000cf46933bdadd0e1b9e11b`
belongs only to the invalid diagnostic and cannot be reused.

Both paths now enforce the same strict RIFF passthrough contract: exact
declared RIFF length, one valid `fmt ` chunk, one `data` chunk, PCM format 1,
mono, 16-bit, 16 kHz, consistent byte rate/block alignment, and aligned data.
The qualification runner durably writes a fresh `passed=false`, zero-run
attempt checkpoint before preflight, then checkpoints the attested pre-run
state and every completed run. An interruption or lease loss cannot expose an
older passing artifact as current evidence.

The qualification JSON intentionally omits arbitrary filenames, endpoint
strings, transcripts, translations, image tags, model profiles, and the local
container name. The runner requires a literal `127.0.0.1` endpoint and proves
the compatible Docker host-port binding before streaming. It verifies
healthy/running state, the 50052 container-port binding, configured pinned
image, local image ID, immutable repository digest, and exactly one registered
Nemotron English streaming selector. The privacy-safe report retains its
registered selector hash. It repeats the same attestation after every complete
replay and invalidates the qualification if the attested identity changes. A
valid report therefore requires
`asr.runtime_attestation.verified=true`; declaration alone cannot substitute
for Docker inspection.

### Two-run qualification outcome (2026-07-25)

The registered two-run qualification completed against the reviewed merged
branch. Runtime attestation, strict PCM identity, full input consumption, and
real-time pacing all passed. Both runs produced the same 437 nonempty finals,
but the same 13 final IDs lacked complete usable word envelopes: 10 had
incomplete word timing and 3 had only an `audio_processed` end horizon.
Top-level `passed=false` is therefore the correct result, and the formal
semantic browser gate remains blocked.

The transcript-free ignored report has SHA-256
`ee4661f59a2d1839c36ac10bec2d8aa60de962582571a82ae381394391fa9f6e`.
Detailed sanitized findings and the service-team escalation package are in
`docs/ASR_FINAL_ATTRIBUTION_GATE_2026-07-25.md`.

### Privacy-safe word-timing-shape diagnostic

A separate one-run diagnostic now captures why an ASR final's raw word timing
cannot form a positive-duration first-start/last-end envelope. It classifies every word
entry and the result envelope by boundary presence, numeric class, numeric
relation, and mutually exclusive timing shape. Invalid entries retain only
their zero-based index and timing observations; transcript, token, and
translation content remain excluded.

For the installed proto3 word timing scalars, zero has unobservable field
presence after decoding. The diagnostic therefore records zero as an observed
numeric class with `presence=unobservable`; it does not claim that zero means
missing or unset.

The runner performs one exact-PCM, real-time replay with runtime attestation
before and after capture. It writes a failed current-attempt checkpoint before
long-running work so an interruption cannot expose stale completed evidence.
The capture also binds the source WAV before, while opening it for streaming,
and after the replay. Runtime continuity includes a one-way fingerprint of the
container ID, start time, and restart count, so a same-image restart or
replacement cannot compare equal.
Its schema explicitly marks the output diagnostic-only and ineligible for
qualification, and forbids formal `gate` or `passed` fields. The unchanged
formal acceptance criterion still requires two complete runs and zero
incomplete word envelopes. Its evidence implementation now also binds the
computed padded PCM, registered English/800 ms client configuration,
container-instance continuity, and concurrent output ownership.

The registered replay completed in 1,888.578 seconds of wall time with exact
input/PCM binding, valid pacing, and stable runtime identity. It reproduced the
same 13 incomplete final IDs as both formal runs. Ten envelopes collapsed to
equal positive start/end timestamps and three had no word entries. Across all
5,263 word entries, 4,676 were zero-duration and 587 had positive duration;
there were no absent, unparseable, nonfinite, negative, numerically zero, or
reversed supplied boundaries.

This is consistent with point-like alignment semantics for many word entries,
but that interpretation requires service-contract confirmation. The unchanged
formal gate should not be repeated until a supported timing policy or service
remediation is known. The transcript-free result and escalation questions are
in
[ASR Word-Timing-Shape Diagnostic Result — 2026-07-25](ASR_WORD_TIMING_SHAPE_RESULT_2026-07-25.md).

## 2026-07-25: rendered-digital common-clock preflight

Implemented and exercised by fail-closed live attempts, but **not yet
accepted**:

- Added a default-off Test Dashboard mode that owns one 16 kHz
  `AudioContext` for the exact source reference, translated playback, worklet
  capture, source chunk pacing, and queue schedule.
- Routed the exact source after 1.00x playback-rate processing and before
  monitor mute to stereo channel 0.
- Routed translated output after queue scheduling and adaptive playback-rate
  processing, but before monitor mute, to stereo channel 1.
- Replaced independent timer pacing in the formal mode with 200 direct
  worklet-observed source boundaries for the 960,000-frame padded PCM fixture.
  Each source chunk is 4,800 frames.
- Bound the tracked source WAV SHA-256
  `0c2cb04d9774f60472b55355f587da2148a053f3a55c05ff36c7dfc23be5c257`
  and padded PCM SHA-256
  `81720f2e23e5b85df4eb2be0bbd486b6591d0b1ed98580118e9e2e1e466bd51c`.
- Added a private four-file browser export: stereo PCM16 WAV, exact timing
  CSV, per-render-block ledger CSV, and hash-binding manifest.
- Added one exact `input_ended` timing row after all 200 `chunk_sent` rows and
  before the server terminal. It binds the `-1` sentinel index, zero bytes, and
  the 60.0-second source endpoint.
- Added integer schedule columns for each translated frame. The manifest and
  validator reconcile their floor/ceiling relationship to the AudioContext
  schedule and use those exact intervals to validate channel-1 WAV occupancy.
- Reconciled received and scheduled translated transport in browser memory and
  bound their ordered aggregate PCM SHA-256 values, byte/frame counts, and
  canonical frame-ledger hashes.
- Required a normal dashboard completion, one server `completed` terminal,
  no post-terminal translated receipt/scheduling, and an exactly drained
  playback queue before formal export.
- Bound a clean Git commit, staged protocol v1/schema-3 mode, ASR EOU 800 ms
  with word times, punctuation segmentation at 240 characters/2,000 ms,
  incremental TTS publication at 100 ms, and the registered image digests.
  The gate also pins ASR/NMT/TTS/output queue capacities at 32/4/4/4, NMT/TTS
  RPC deadlines at 15/60 seconds, TTS maximum segment audio at 60 seconds,
  one retry, response-chunk telemetry off, TTS subsegmentation at 0/12,
  four-character atomic fallback, and a 10-second close deadline:

  ```text
  ASR sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850
  NMT sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb
  TTS sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d
  ```

- Required the Vite frontend and FastAPI backend process-start provenance to
  be clean and to name the same commit. A stale process cannot borrow the
  current checkout's identity.
- Replaced formal use of the mutable Vite development server with an immutable
  `npm run build` plus production preview. Backend provenance is discovered
  directly from Git and cannot be overridden by environment declarations.
- Bracketed each worklet boundary's main-thread receipt on the shared
  AudioContext and sampled the clock again only after successful WebSocket
  handoff. Receipt and handoff are limited to 1,600 frames (100 ms) after the
  source boundary.
- Registered one strict 60-column timing CSV schema. The browser emits that
  fixed header; the validator rejects missing, extra, duplicate, or reordered
  fields.
- Sized the worklet capture for 420 seconds, covering the 60-second source,
  the dashboard's full 300-second drain limit, and teardown margin.
- Fetched the recorder worklet without cache reuse, hashed the exact bytes
  loaded into the `AudioContext`, and bound that digest for comparison with
  the worklet tracked by the registered commit. The registered module SHA-256
  is
  `8baf6193f097acc3c2663ca91299a19f68b1e7c17deaa586db339a073a7d0a5d`.
- Added a fail-closed offline validator that reconciles the source, worklet
  block, protocol frame/parent, terminal, artifact-hash, runtime, and captured
  playback-schedule evidence.
- Registered a no-drop adaptive queue gate with time-weighted p95 at or below
  5 seconds and peak at or below 10 seconds. The validator reports `PASS` only
  when both queue bounds and protocol PCM continuity pass, `FAIL` for valid
  evidence that misses either mechanical gate, and `INVALID` for unusable or
  contradictory evidence.
- Added ignore rules for the four raw browser artifact patterns and documented
  that all source/translated PCM and associated run evidence must remain
  private.
- Added `run_rendered_digital_preflight.py`, a fail-closed Chrome DevTools
  runner that verifies the exact fixture, Riva readiness, clean Git state,
  backend pins, immutable served frontend, exact four-file download set, and
  offline report while cleaning up only processes it starts.
- Added read-only Docker attestation by exact HTTP/gRPC port pair. The runner
  requires distinct running/healthy containers, exact port mappings, approved
  local image `RepoDigest`s, backend-declaration agreement, and unchanged
  identities before/after capture. Its private `docker-attestation.json`
  contains no environment, names, or secrets and is finalized with the clean
  commit plus browser manifest SHA-256.
- Completed the automated implementation checks: frontend build/lint and 202
  tests passed; backend/analysis/runner suites passed 1,014 tests with one
  skipped environment-specific case.
- The first clean-commit runner invocation failed closed before capture because
  FastAPI serialized registered timeout values as JSON numbers decoded to
  Python floats (for example, `15.0`) while the runner fixture required the
  integer type. The runner now accepts only equal integer/float representations
  for the four float-backed timeout/duration fields and still rejects booleans,
  strings, non-finite values, or numeric drift. Regression tests cover the
  actual API shape; no browser/model capture occurred during the failed attempt.

## 2026-07-26: arm-scoped recorder epoch and wall-clock pacing

The first runner invocation to reach live browser capture failed closed before
the first source chunk. The Riva WebSocket reached `listening`, then the
dashboard performed an orderly stop. A privacy-safe diagnostic classified the
browser failure as the recorder code `noncontiguous_render_quantum`; no
dashboard text, transcript, or audio content was written to automation logs.

An isolated headless-Chrome AudioWorklet probe reproduced one deterministic
startup-only discontinuity: the first 128-frame render quantum was followed
by a 256-frame jump, after which the clock remained contiguous. The same jump
occurred with the default output, the host ALSA null sink, disabled audio
output, and a non-muted silent pull path. This established that the rejected
frames belonged to Chrome worklet warm-up before source scheduling, not to
ASR, NMT, TTS, WebSocket transport, or live evidence.

The recorder lifecycle now separates processor readiness from evidence
capture:

- pre-arm worklet quanta emit no PCM and carry no continuity claim;
- `arm_source_clock` starts the formal epoch on the next render quantum;
- that first captured quantum must be at or before the scheduled source start;
- all post-arm render quanta must remain exactly contiguous;
- the worklet emits explicit zeros through an active destination path while
  recording its two inputs internally; and
- a post-arm gap, late capture start, invalid arm, or frame-limit breach stops
  the processor and fails the run.

The offline validator adds an independent clock-rate defense. The first and
last source boundary receipts cover exactly 955,200 frames (59.7 seconds).
Their monotonic client-time span must be within the inclusive 0.99x–1.01x
envelope. Out-of-envelope evidence is `INVALID`, not a queue `FAIL`, because
it cannot support real-time latency conclusions. Every intermediate boundary
must also remain within 100 ms plus 1% of elapsed source time, and each
chunk's relative timestamp and absolute `performance.now()` handoff must
preserve one client-clock origin. This prevents a sink from running fast for
part of the test and slow later merely to recover the correct final span.

Focused validation completed:

- the direct worklet harness excludes distinctive pre-arm PCM, accepts the
  first safe post-arm quantum, and rejects post-arm gaps and late capture
  starts;
- the React hook resolves on readiness, waits for the arm-scoped capture
  start, and independently rejects an epoch after source playback;
- validator fixtures accept 0.99x and 1.01x, reject 0.98x and 1.02x, and
  remain invariant to absolute `performance.now()` origin; and
- a browser-native probe using the actual edited worklet completed with zero
  recorder errors, ten source ticks, capture start before source start, and a
  measured AudioContext/wall-time rate of approximately 1.0046x.

Repository-wide validation also passed: frontend lint and production build,
all 208 frontend tests, the `python -m pytest -q tests` suite (570 passed,
one environment-specific skip), Python bytecode compilation,
`git diff --check`, and the sanitized-text scan. The only scan matches were
the Docker GPU resource key and deliberately fake `.invalid` test addresses.

The claim boundary remains intentionally narrow. This gate measures
rendered-digital graph output on one sample clock; it does not prove physical
DAC output, acoustic audibility, translation quality, semantic phrase delay,
or audience-reaction alignment.

Pending:

- Merge the reviewed recorder/pacing fix and restart the application from that
  clean commit.
- Run the exact 60-second browser preflight in a secure localhost context.
- Preserve the private four-file bundle, run
  `analyze_rendered_digital_preflight.py`, and review the first real
  `PASS`/`FAIL`/`INVALID` report.

See
[Rendered-digital common-clock 60-second preflight](RENDERED_DIGITAL_COMMON_CLOCK_PREFLIGHT.md).

## 2026-07-26: 500 ms registered TTS publication frame

The rendered-digital formal profile now requires 500 ms incremental TTS
publication frames. This supersedes the profile's original 100 ms setting
without rewriting the earlier 100 ms canary results. Queue capacities, queue
pass/fail bounds, RPC and drain timeouts, input audio, and recorder continuity
rules are unchanged.

The change follows a transcript-free synthetic Chrome graph-load diagnostic.
A 30-second translated burst reproduced `noncontiguous_render_quantum`: the
observed `currentFrame` was one 128-frame render quantum behind the expected
frame. The initial exploratory sweep was not provenance-bound and is
superseded by the hardened comparison below.

The 2026-07-26 rerun verified the registered worklet SHA-256, recorded the
sanitized Chrome version and browser-binary SHA-256, awaited all translated
node scheduling, validated the exact node count, 16 kHz AudioContext, source
ticks, captured PCM blocks, lifecycle messages, and clock rate, and failed
closed on any incomplete repeat. On Chrome `149.0.7827.155`, five repeats at
each candidate duration produced 0/5 failures at 100 ms, 1/5 at 250 ms, 2/5
at 300 ms, and 1/5 at 500 ms. This mixed result does not prove that larger
publication frames prevent the discontinuity. A separate 65-second 500 ms run
scheduled all 60 expected translated sources, emitted all 200 source ticks,
recorded zero capture errors, and measured an AudioContext/wall-clock rate of
1.000351.

Historical Magpie cadence telemetry measured a 139 ms median response duration
and a 19 ms median inter-response interval. The 500 ms publisher may hold
several responses before publication, but neither its browser-stability
benefit nor its incremental latency is established by the synthetic sweep.
The formal live run must decide the mechanical queue outcome, and later
semantic review must measure listener-relevant phrase delay.

## 2026-07-26: 500 ms formal run failed on the worklet frame clock

The formal run from clean commit
`3ebe28c494892b3b22904fa592238e3250d01924` failed fast after 166 source
chunks with `noncontiguous_render_quantum`: expected frame `803712`, observed
frame `803584`, delta `-128`. The runner correctly exported no evidence bundle
and no report. Pre-capture Docker attestation
`66910766b66445b4d75ea83e06e9340c1bf95c48c8cd87e087a9374492ebca98`
bound the run. A separate post-run status check found all three pinned
services healthy with zero restarts. The result shows that 500 ms publication
did not eliminate the discontinuity.

A generated-PCM shadow diagnostic then reproduced the event in one of five
65-second Chrome `149.0.7827.155` repeats. The exact recorder and shadow
worklet both reported expected frame `458752` and observed frame `458624`. The
worklet frame label repeated for one 128-frame callback, jumped 256 frames on
the next callback, and then advanced normally. Across the complete four-before
plus event plus sixteen-after trace, every decoded generated-source marker
advanced exactly 128 frames. This classifies the reproduced event as a
repeated clock label with immediate catch-up and a contiguous generated
source-marker stream at the shadow input. It does not establish translated-mix
or physical-output continuity. Four later repeats had no event.

Chromium lock contention remains a source-backed hypothesis rather than a
confirmed root cause. In the pinned Chrome source, the real-time destination
[advances its frame counter before the worklet-global
update](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/realtime_audio_destination_handler.cc#273);
the
[worklet update uses the graph lock through a non-blocking
`TryLock`](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/base_audio_context.cc#977);
and both
[`AudioNode.connect()`](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/audio_node.cc#150)
and
[scheduled-source start handling](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.155/third_party/blink/renderer/modules/webaudio/base_audio_context.cc#778)
use that lock. A skipped global update is therefore consistent with the trace,
but the trace does not observe the lock itself.

The next focused trial should preserve PCM and normalize only one exact
zero-step/next-quantum-catch-up pair, expose an integer-only event counter, and
fail on a second event or any other clock shape. PCM, lifecycle, wall-clock
pacing, queue, and audience-latency gates remain unchanged. See
[Chrome render-clock shadow diagnostic
result](RENDER_CLOCK_SHADOW_RESULT_2026-07-26.md).

## 2026-07-26: browser-independent scheduled-playback gate

The primary deployment-oriented gate no longer depends on Chrome, Vite, Web
Audio, a display server, a microphone, or an output sound device. The existing
Python WebSocket client already supplied absolute source-end pacing, staged
Riva transport, terminal-aware drain behavior, and three-sample automation.
The new integration sends each protocol-v1 PCM frame to the headless scheduler
only after strict metadata-header/binary pairing succeeds.

The scheduler accepts no PCM payload, transcript, translation, path, URI, or
wall-clock timestamp. It schedules every validated frame immediately with the
registered 5/8/10-second, 1.00x/1.05x/1.10x no-drop policy and independently
replays the completed trace through `simulate_playback`. A mismatch fails the
capture. Per-sample summaries now include an aggregate-only report with:

- exact time-weighted queue percentiles, peak, threshold exposure, and rate
  occupancy;
- a mechanical pass criterion of queue p95 at or below five seconds, peak at
  or below ten seconds, and zero drop/reorder/duplicate counts;
- source-end to first-arrival and scheduled-parent-envelope timing;
- first-versus-final-quartile source-frontier drift when at least four parents
  have bounded ASR source ranges; and
- explicit flags that scheduled digital playback is not DAC/acoustic
  audibility or an exact joke/punchline landmark measurement.

The saved-CSV analyzer now emits schema 2 and repeats the fixed/adaptive
source-frontier projection. Legacy traces remain supported, and older canary
summarizers accept both schema 1 and schema 2. The formal three-sample command
remains:

```bash
python3 run_long_form_experiment.py --audio-metadata-protocol-v1
```

After the artifact-integrity and large-clock timing review, the intended
automated Python suites passed with 1,123 tests and one environment-specific
skip. Root-level manual microphone scripts were excluded because this headless
VM has no PortAudio device/library.

A live 60-second staged preflight then passed against the three healthy pinned
services. It sent all 200 source chunks at real-time pace, received and
scheduled 112 translated frames, drained through the single completed
terminal, produced 49.7 seconds of translated PCM (0.828x of the whole input),
and measured a 1.4-second service tail. The new report validator found no loss,
reorder, duplication, or canonical-replay mismatch.

This preflight proves the browser-independent service and scheduling path is
operational. It does not prove the 5-second-p95/10-second-peak objective on the
long-form samples. Retained protocol-v1 canaries still miss that queue
objective, so another three-sample live matrix and later reviewed semantic
landmarks remain necessary.

See [Browser-independent real-time S2S gate](HEADLESS_REALTIME_GATE.md).

## 2026-07-26: privacy-safe stage and burst attribution

Added `analyze_stage_burst_attribution.py` to join complete schema-3
CSV/summary evidence with the validated ASR, segmenter, NMT, TTS, output,
WebSocket, protocol-v1 client-arrival, and deterministic no-drop playback
contracts. The analyzer:

- accepts only staged, closed, complete schema-3 captures with incremental TTS
  publication enabled and TTS request subsegmentation disabled;
- validates parent/frame coverage, stage order, queue residence, processing
  envelopes, retry bounds, frame relay order, and server/client cadence without
  mixing clock domains;
- evaluates aligned fixed-grid 30-second windows, including the final capture
  tail, and separately labels complete one-second-stride rolling windows;
- uses a fixed numeric metric whitelist and never serializes text lengths,
  transcript or translation text, paths, filenames, endpoints, session
  identifiers, raw events, PCM, or wall-clock time; and
- rejects output paths that could overwrite an input CSV, its inferred summary,
  or the other generated report.

The retained formal three-sample evidence reconciled 2,020 parents and 14,209
client PCM frames. Source-boundary-to-first-client-frame p95 was 5.773, 5.536,
and 4.765 seconds, while source-boundary-to-scheduled-start p95 was 74.302,
35.561, and 24.090 seconds. The largest aligned 30-second bins delivered
72.958, 56.053, and 49.412 seconds of translated media and grew the adaptive
listener queue by 36.325, 21.109, and 25.842 seconds.

Aligned-window translated-media volume had descriptive Spearman associations
of 0.849, 0.934, and 0.793 with queue change. Parent audio duration had
associations of 0.999, 0.995, and 0.997 with its immediate queue change,
whereas source-boundary-to-first-frame timing had much smaller associations of
0.158, 0.145, and 0.105. Output queue residence and WebSocket relay remained in
the millisecond range. This supports translated-media bursts as the immediate
queue-growth mechanism, but it does not independently identify a causal model
stage.

The analysis also exposed rare TTS-frame-received to output-enqueue gaps:
2/9/2 frames exceeded 100 ms, 1/7/0 exceeded one second, and the per-sample
maxima were 5.648, 7.054, and 0.930 seconds. The next diagnostic is explicit
privacy-safe TTS-worker-to-publisher handoff and response-chunk timing, followed
by a one-minute preflight and the shortest five-minute unsplit canary. Earlier
40-, 45-, and 60-character request splitting remains rejected because it
reduced individual burst size but increased total generated duration and
listener backlog.

See [Stage-burst attribution](STAGE_BURST_ATTRIBUTION.md) and
[formal attribution result](STAGE_BURST_ATTRIBUTION_RESULT_2026-07-26.md).

## 2026-07-26: default-off TTS publisher-handoff diagnostic

Added a default-off, privacy-safe diagnostic for the rare interval between a
Magpie response making enough PCM available and that frame reaching the
listener WebSocket. It activates only when schema-3 incremental publication
and the existing response-chunk sidecar are both enabled. The API exposes the
computed capability, and the staged summary emits an active-only marker.

Each successfully committed frame now carries an all-or-none monotonic chain:
publication request, event-loop callback start, output-capacity acquisition,
queue commit, dequeue, WebSocket send start, and successful send completion.
The implementation preserves the legacy meaning of `blocked_put_ms`: it is
positive only when the output queue was observed full. Queue-commit telemetry
is prevalidated, timestamped immediately before `put_nowait`, and emitted after
commit so an external telemetry-sink failure cannot cause committed PCM to be
retried.

The batch gate and streaming-latency analyzer fail closed on missing,
duplicated, reordered, inconsistent, or value-smuggled frame evidence. They
reconcile response cumulative bytes to the frame-ready timestamp, parent
totals, retry counts, lifecycle times, and WebSocket completion. Direct
handoff distributions exclude deliberate atomic-fallback frames. For
multi-frame response bursts, the analyzer separates propagated
prior-frame-commit wait from worker/adapter work after publication can proceed,
preventing earlier output backpressure from being charged repeatedly as a new
TTS delay.

The canary runner now has a one-arm `handoff` mode. It requires an ignored
output root, defaults that mode to the registered 500 ms frame profile, and
generates both handoff-component and stage/burst reports. The promoted sequence
is a one-minute Sample 01 integrity preflight followed by a five-minute Sample
02 diagnostic, with all three samples available as a fixed-profile sequential
extension.

Implementation validation passed the complete automated Python gate with 1,193
tests and one environment-specific skip, including the 463-test backend suite,
102-test batch module, and 37-test streaming-latency analyzer module. All 211
frontend tests, frontend lint, and the production build passed with the
bundled Node.js 22 runtime.

See [TTS publisher-handoff diagnostic](PUBLISHER_HANDOFF_DIAGNOSTIC.md).

The subsequent live gate completed on the same clean commit. The 60-second
Sample 01 preflight reconciled 116 frames; the promoted 300-second Sample 02
run reconciled 650. Neither run had a frame-ready-to-enqueue interval over
100 ms, and the five-minute maximum was 8.690 ms. No NMT, TTS, or output
admission blocked.

The five-minute no-drop listener schedule still failed its audience objective:
queue p95 was 12.104 seconds, peak queue was 21.991 seconds, and 46.361 seconds
were spent above ten seconds. Its strongest aligned 30-second window delivered
41.657 seconds of translated media and grew the queue by 10.092 seconds. This
rejects publisher-handoff optimization as the next intervention for the
observed window and promotes generated-duration/burst mitigation plus semantic
landmark measurement.

See [TTS publisher-handoff live result](PUBLISHER_HANDOFF_RESULT_2026-07-26.md).

## Handoff checklist

- [x] Frontend lint passed on the adaptive working branch
- [x] Commit and push the adaptive branch with the initial experiment documents
- [x] Run one automated live trace for all three samples
- [x] Verify each completed trace produces the matched fixed/adaptive comparison
- [x] Verify terminal completion, PCM-send drain, staged promotion, and hashes
- [x] Verify resume provenance and backend lock behavior after a live failure
- [ ] Run three repeats per sample after the staged design improves the queue
- [x] Cross-check replay scheduling with an actual browser/Web Audio run
- [ ] Capture synchronized phrase/punchline delay, not only queue depth
- [ ] Review 1.05x and 1.10x quality with native Spanish listeners
- [x] Implement and unit-test punctuation splitting before staged live tests
- [x] Add bounded NMT/TTS queues and ordered drain behavior
- [x] Integrate the staged pipeline into `/ws/translate` behind a default-off flag
- [x] Pass the terminal-aware one-minute staged WebSocket preflight
- [x] Pass one complete staged Sample 03 operational canary
- [x] Pass one complete post-recovery staged Sample 02 canary
- [x] Run the full staged Sample 01, Sample 02, and Sample 03 matrix
- [x] Sweep no-drop playback capacity on the completed matched traces
- [x] Simulate 5/8/10-second whole-parent freshness/loss tradeoffs on schema 3
- [x] Add opt-in parent/frame wire metadata and observation-only browser telemetry
- [x] Pass a 60-second matched live canary with protocol-v1 evidence
- [x] Implement chunk-end-paced semantic source-event parent-envelope analysis
- [x] Implement fail-closed runtime attestation for the ASR container's
  endpoint binding, health, pinned image/digest, and registered profile
- [x] Execute two real-time ASR final-attribution qualification runs on the
  exact long-form padded PCM
- [ ] Pass two real-time ASR final-attribution qualification runs on the exact
  long-form padded PCM (blocked by 13 deterministic incomplete finals per run)
- [x] Run and review one privacy-safe ASR word-timing-shape diagnostic; escalate
  its deterministic point-like/no-word response shapes before repeating the
  formal two-run qualification
- [ ] Capture a formal two-reviewer semantic source-event gate run
- [ ] Pass a five-minute matched live canary with protocol-v1 evidence
- [ ] Run protocol v1 across all three long-form samples
- [x] Implement and live-preflight the browser-independent protocol-v1
  scheduled-playback gate
- [x] Attribute all three formal schema-3 traces across model stages,
  publication, transport, and listener burst windows
- [x] Instrument and validate the TTS-worker-to-publisher handoff
- [x] Run the unsplit one-minute publisher-handoff preflight and five-minute
  Sample 02 diagnostic
- [ ] Repeat publisher-handoff telemetry over complete long-form samples before
  claiming the historical late-sample anomaly is eliminated
- [x] Fit and document a privacy-safe post-NMT TTS character/duration model
- [x] Implement default-off composite-key post-NMT TTS subsegmentation
- [x] Run matched unsplit/40/45/60 short and five-minute real-time canaries
- [x] Keep unapproved private/internal container references out of external
  documentation
