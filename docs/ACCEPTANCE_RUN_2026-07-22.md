# Three-sample acceptance run: 2026-07-22

## Outcome

The pinned Nemotron 3 S2S stack completed one new live Riva capture for each
of @jgough-essextec's three sample files. Every final capture passed the
harness's completion and artifact-integrity checks. The adaptive playback
candidate retained every translated chunk and reduced aggregate listener tail
by 78.1%, from 388.383 seconds at fixed 1.00x playback to 84.962 seconds.

The proposed 5-10 second live-audience queue objective was **not met**. The
time-weighted adaptive queue p95 was 35.823 seconds for Sample 01, 37.460 seconds
for Sample 02, and 18.622 seconds for Sample 03. The queue remained above 10
seconds for 41.6-78.6% of each simulated playback window.

This means a live joke, audience reaction, or other time-sensitive moment can
still reach a Spanish listener tens of seconds after it reaches the English
audience. Queue depth is only one component of semantic English-to-Spanish
delay, so the exact joke-to-punchline delay was not measured and could be
larger.

Do not interpret the successful run as acceptance of the current latency
policy. The final capture matrix and recovery automation passed, the run also
exposed a service robustness failure, and the overall candidate
audience-latency result missed on every sample trace.

## Later staged-recovery postscript

This report remains the historical July 22 monolithic acceptance record. On
July 23, the final staged recovery snapshot separately completed one full
Sample 02 canary with 805 ordered segments and three validated NMT recoveries.
Its 0.308-second service tail coexisted with a 239.156-second fixed playback
tail, so it passed the operational gate without closing the audience gate.
That standalone result is documented in
[Sample 02 post-recovery staged canary](STAGED_SAMPLE_02_RECOVERY_CANARY.md);
the clean final-snapshot three-sample matrix remains pending.

## Run identity and provenance

| Field | Value |
|---|---|
| Run ID | `acceptance-01-6dfc03c` |
| UTC start | `2026-07-22T04:12:01.350079Z` |
| UTC completion | `2026-07-22T06:43:48.428823Z` |
| Git commit | `6dfc03c74d6a87c3b8402daa39f30473a50301bc` |
| Git state at capture | Clean |
| Repeats | One live trace per sample |
| Backend | `http://localhost:8000` |
| Result directory | `experiment_results/acceptance-01-6dfc03c` |

The result directory is intentionally ignored by Git because its event CSVs
and plots are large. Preserve the directory separately when moving to another
VM.

The capture source and replay method are important:

- Each sample was streamed to the live pinned Riva S2S stack at real-time
  pace.
- Fixed and adaptive results were calculated from the same newly captured
  translated-audio arrival trace.
- Playback was a deterministic Python scheduling simulation, not an actual
  browser/Web Audio execution.
- Native Spanish listening quality and marked-phrase or joke delay were not
  measured.

## Environment

| Component | Pinned image | Local image digest |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| NMT/S2S | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |

The NMT image has both `1.5` and `1.5.2` local tags pointing at the digest
above; Compose explicitly selected `1.5.2`.

GPU at the final readiness check:

```text
NVIDIA RTX PRO 6000 Blackwell Server Edition
Driver 595.84
97,887 MiB total; 32,217 MiB used; 65,034 MiB free
```

The three services therefore fit simultaneously with approximately 65 GB of
GPU memory still free after the run. This confirms ample capacity on this
96 GB RTX PRO 6000 configuration. GPU-memory capacity was not the deployment
constraint, although this memory snapshot alone does not prove compute
headroom during every inference burst.

The test used the English Nemotron streaming `batch_size=32` profile,
automatic ASR punctuation, an 800 ms final EOU window, English `en-US` to
Spanish `es-US`, the `Magpie-Multilingual.ES-US.Isabela` voice, and 16 kHz mono
Int16 input sent in 300 ms chunks.

## Procedure

The services were started with the repository's pinned Compose file and the
parent VM environment file:

```bash
docker compose --env-file ../.env up -d
docker compose --env-file ../.env ps

curl --fail http://localhost:9002/v1/health/ready
curl --fail http://localhost:9001/v1/health/ready
curl --fail http://localhost:9003/v1/health/ready
```

On this new clone, `.cache/nim` initially lacked write permission for the
non-root NIM processes. The cache permissions were corrected before model
generation. First startup then took more than 30 minutes while TensorRT engines
were built. Subsequent starts can reuse the cache.

The backend was started from the tested commit, then the experiment ran with:

```bash
PYTHONPATH=.python-packages:. \
MPLCONFIGDIR=/tmp/s2s-eval-matplotlib \
/usr/bin/python3 -u run_long_form_experiment.py \
  --run-id acceptance-01-6dfc03c
```

The one-minute preflight completed before the samples. Its first audio arrived
in 17.234 seconds, its translated output was 48.159 seconds for 60 seconds of
input, and its fixed-rate playback tail was 7.547 seconds.

## Capture results

`Harness terminal observation` is the time from the explicit end-of-input
signal until the legacy polling loop observed terminal completion. It can
include up to a polling interval; these historical summaries do not retain the
terminal's exact receive timestamp. `Fixed listener tail` replays the received
chunks at 1.00x and measures when queued translated playback ends relative to
source input. It is not the same as terminal-arrival lag.

| Sample | Input | Output | Output/input | Duration excess | First audio | Harness terminal observation | Fixed listener tail |
|---|---:|---:|---:|---:|---:|---:|---:|
| Sample 01 | 1,908.432 s | 1,982.439 s | 1.039x | 74.007 s | 16.897 s | 1.002 s | 142.433 s |
| Sample 02 | 2,427.011 s | 2,620.528 s | 1.080x | 193.517 s | 2.296 s | 1.002 s | 204.144 s |
| Sample 03 | 1,888.105 s | 1,877.486 s | 0.994x | 0.000 s | 4.577 s | 2.004 s | 41.806 s |

All three final summaries report:

```text
input_completed=true
connection_lost=false
drain_timed_out=false
translation_completed=true
server_error=""
```

Spanish output expansion remains a major source of backlog for Sample 01 and
especially Sample 02. Sample 03 is also informative: its translated output was
slightly shorter than the English source, yet fixed playback still ended 42
seconds late. Arrival burstiness and upstream stalls therefore contribute to
listener delay in addition to total TTS duration.

## Adaptive playback replay

The candidate controller used a 5-second target, 8-second urgent threshold,
10-second soft limit, 4/7-second release hysteresis, and 1.00x/1.05x/1.10x
rates. It never drops audio.

| Sample | Fixed tail | Adaptive tail | Tail reduction | Adaptive p95 | Adaptive peak | Playback time above 10 s |
|---|---:|---:|---:|---:|---:|---:|
| Sample 01 | 142.433 s | 36.656 s | 74.3% | 35.823 s | 41.683 s | 69.377% |
| Sample 02 | 204.144 s | 30.509 s | 85.1% | 37.460 s | 44.638 s | 78.620% |
| Sample 03 | 41.806 s | 17.797 s | 57.4% | 18.622 s | 26.930 s | 41.569% |

| Sample | Accelerated source audio | Source audio at 1.10x | Longest continuous 1.10x | Dropped chunks |
|---|---:|---:|---:|---:|
| Sample 01 | 90.7% | 81.922% | 440.633 s | 0 |
| Sample 02 | 95.6% | 87.767% | 521.650 s | 0 |
| Sample 03 | 79.2% | 60.876% | 115.763 s | 0 |

Aggregate fixed tail was 388.383 seconds and aggregate adaptive tail was
84.962 seconds, a reduction of 303.421 seconds or 78.124%.

These replay numbers were regenerated with the exact end of each final PCM
source chunk. The original capture summaries used the final chunk's start;
the analyzer recognizes those values only as annotated compatibility evidence.

Candidate gates were evaluated per trace:

| Gate | Sample 01 | Sample 02 | Sample 03 |
|---|---|---|---|
| Time-weighted queue p95 at or below 10 s | Fail | Fail | Fail |
| Playback time above 10 s below 1% | Fail | Fail | Fail |
| No translated chunks dropped | Pass | Pass | Pass |
| Overall | **Miss** | **Miss** | **Miss** |

The amount and continuity of 1.10x playback also make a native-listener
quality review mandatory before this rate can be recommended. The simulation
does not model pitch quality, perceived naturalness, or chunk-boundary
artifacts.

## Transient Sample 02 failure and recovery

The first Sample 02 attempt failed during its final drain at approximately
`2026-07-22T05:25:34Z`. Magpie's logs show that the text reaching TTS contained
the Chinese `阿门。` ("Amen.") for a short final segment despite the Spanish
target. The logs do not expose the translated NMT text directly. The Magpie
Spanish character mapper could not map those characters, then its ensemble
failed with:

```text
The expanded size of the tensor (6) must match the existing size (0) at
non-singleton dimension 1. Target sizes: [8, 6]. Tensor sizes: [8, 0]
```

The harness correctly marked Sample 02 failed, promoted no partial Sample 02
artifacts, stopped before Sample 03, and retained the verified preflight and
Sample 01 results. ASR, NMT, and TTS remained healthy with zero container
restarts; there was no GPU out-of-memory event.

Operator-run isolation checks found:

- direct Magpie TTS requests for both `阿门。` and `Amén.` succeeded;
- a controlled concurrent eight-request TTS probe succeeded; and
- a full S2S probe over the last 30 seconds of Sample 02 succeeded.

Those isolation probes were manual host diagnostics, not artifacts recorded
by the experiment manifest.

The successful probes rule out a simple permanent inability to synthesize
`阿门。`, but they do not isolate the cause. Possibilities include transient
request or language state, long-stream batching/state, or a cross-model
language/content mismatch. It is still a robustness defect: target-language
output should be validated before TTS, and the incident should be shared with
NVIDIA with the original log excerpt.

The run resumed safely with:

```bash
PYTHONPATH=.python-packages:. \
MPLCONFIGDIR=/tmp/s2s-eval-matplotlib \
/usr/bin/python3 -u run_long_form_experiment.py \
  --resume-dir experiment_results/acceptance-01-6dfc03c
```

Resume hash-validated and skipped preflight and Sample 01, reran Sample 02 from
scratch, then completed Sample 03 and aggregate analysis. This validates the
harness's staged promotion and strict resume behavior under a real long-form
failure.

## Post-acceptance retrospective: staged hardening and Sample 03 canary

This section records evidence obtained later on 2026-07-22. The incident
account above is intentionally unchanged because it accurately describes what
was known during the original monolithic acceptance run.

The staged WebSocket promotion work made the short-fragment failure class
reproducible. Its first full Sample 03 attempt stopped at approximately 69.1
seconds on `monotonic time cannot move backwards`: a producer capture timestamp
was older than an asyncio age-poll observation. The fix supplies a
nondecreasing consumer observation timeline to the punctuation segmenter while
preserving raw capture times and Nemotron source offsets.

A second attempt reached 807.9 seconds before an isolated ASR final, `uh.`,
caused Riva Translate 1.5.2 to return Chinese `呃。` under the requested Spanish
target. Direct probes reproduced the broader short-fragment class: `Okay.`
became `好吧。`, and `Amen.` became `阿门。`. This provides a concrete NMT
wrong-script explanation for the content reaching Magpie and connects it to
the earlier Sample 02 observation. It does not indicate a GPU-capacity failure
or a permanent inability of Magpie to synthesize ordinary Spanish.

The staged path now applies three safety layers: exact standalone hesitation
fillers are discarded before ID allocation with privacy-safe telemetry; NMT
output is validated for Spanish-compatible content immediately after
translation and defensively again before TTS; and narrowly scoped deterministic
Spanish overrides handle standalone `OK`/`Okay` and `Amen`. Invalid content is
not blindly retried through Magpie. A real-time smoke over the exact 790-815
second source region then passed with 10 ordered segments, one filler discard,
no incomplete IDs, and no pipeline error.

The third attempt completed the full 1,888.1045-second Sample 03 source. It
delivered all 646 ordered IDs, discarded six exact fillers, and passed staged
and terminal integrity with no runtime, cleanup, disconnect, timeout, restart,
or OOM error. Measured results were:

| Measurement | Staged Sample 03 attempt 3 |
|---|---:|
| Output/input duration | 1.01627x |
| First translated audio | 5.113 s |
| Last-audio arrival tail | 0.850 s |
| Completed-terminal arrival after input | 1.744 s |
| Harness drain observation (poll/settle included) | 2.255 s |
| Fixed 1.00x playback tail | 64.038 s |
| Maximum NMT / TTS / output queue depth | 4 / 4 / 1 |
| Blocked NMT / TTS / output puts | 15 / 0 / 0 |

This closes the single-sample staged operational canary, but it does not change
the original audience-latency decision. The long fixed-rate playback tail
still represents an awkward-delay risk for jokes and audience reactions. An
actual browser/Web Audio run, synchronized semantic-marker timing, native
Spanish quality review, and the full staged Sample 01/Sample 02/Sample 03 matrix
remain pending.

## Nemotron 3 assessment

Nemotron 3 remains the appropriate ASR choice for this experiment:

- it is the Riva team's recommended high-quality streaming model for English
  input;
- the pinned RNNT configuration completed all three long-form captures with
  automatic punctuation and the recommended 800 ms final EOU setting; and
- earlier repository results showed clear improvement on Sample 03 and a
  moderate improvement on Sample 01 relative to @jgough-essextec's reconstructed prior
  traces.

This run was not a same-day Parakeet-versus-Nemotron A/B test, so it cannot
attribute its exact numbers to the ASR swap alone. Sample 02 continues to show
that the ASR change does not solve sustained translated-audio expansion, NMT
output validity, TTS behavior, or queue growth by itself.

## Original decision and next work (historical)

The list below is retained as the decision made from the monolithic acceptance
evidence. The retrospective above records that the staged WebSocket path,
bounded queues, and target-content validation were subsequently implemented
and that the first full Sample 03 operational canary passed. The current open
work is the remaining staged sample matrix and audience-facing validation.

Running three more identical repetitions is not the highest-value next step:
one repeat already misses both queue gates by wide margins. Keep the complete
trace as the acceptance evidence and address the limiting architecture first.

Recommended sequence:

1. Implement the staged `ASR -> punctuation splitter -> NMT -> TTS` backend so
   NMT and TTS can overlap behind explicit bounded queues.
2. Preserve ordering, add per-stage queue-residence and inference timing, and
   make a queue overflow policy explicit. A 5-10 second queue cannot be a hard
   bound while every chunk is preserved if production remains faster than
   1.10x consumption.
3. Validate that NMT output is non-empty and consistent with the requested
   target language before TTS. Retry or apply a documented safe fallback for
   invalid final fragments such as the observed `阿门。` case.
4. Replay the captured traces at higher pitch-preserving time-stretch caps to
   estimate the rate required for recovery before asking listeners to evaluate
   it.
5. Run an actual browser/Web Audio cross-check and a native Spanish quality
   review at 1.05x, 1.10x, and any proposed higher rate.
6. Add synchronized semantic markers, including a joke or marked phrase, to
   measure end-to-end English event to Spanish playback delay.
7. After the staged path meets or approaches the queue objective, collect
   three repeats per sample to quantify variability and compare it with the
   monolithic path.

## Artifact verification

The harness manifest records SHA-256 values for every promoted CSV, JSON
summary, and plot. An independent post-run validation reported `valid=True`
for preflight, Sample 01, Sample 02, and Sample 03, and `.staging` was empty.

Top-level hashes at the end of the run:

```text
95f66d9ed84816498127847f8c08e851af650b9c50c855a9a7464ea0c9da6e50  manifest.json
2171cd0112ccec51ab585124d1173da58b6b0f0340e2f31db9cf24019ca4ab65  playback_policy_analysis.json
5ebb1d9bfa48217ebba3ef626f0378b8a98b9b42360aacaefe96fd79a339908b  playback_policy_analysis.md
```

Evidence boundaries:

- The manifest-backed evidence includes the final preflight and three sample
  captures, source and Git provenance, capture summaries, event CSVs, plots,
  playback-policy analysis, and their recorded hashes.
- The initial failed Sample 02 attempt was deliberately not promoted. Its runner
  failure and resume transcript remains in
  `/tmp/s2s-eval-acceptance-01-6dfc03c.log` on the test VM.
- The exact character-mapper and tensor messages, image digests, GPU snapshot,
  readiness checks, startup observations, and isolation-probe outcomes were
  collected from the live host and transcribed into this report. They are not
  part of the manifest-backed result directory.

The `/tmp` transcript and Docker container logs are vulnerable to VM or
container deletion. Copy them with the ignored result directory before
releasing the VM if raw diagnostic preservation is required.

Final checks found all three Compose services healthy, all three HTTP
readiness endpoints returning `ready`, and the backend reporting
`riva_connected=true`. The containers were intentionally left running after
the acceptance test.
