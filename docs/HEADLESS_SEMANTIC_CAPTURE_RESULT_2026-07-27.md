# Headless scheduled semantic capture — July 27, 2026

## Outcome

**Evidence capture PASS; semantic-delay result NOT YET EVALUATED.**

The tracked 60-second fixture completed through staged Nemotron 3 ASR, Riva
NMT, and Magpie TTS. The run produced a valid private source/translated PCM
bundle and exact schedule ledger for independent bilingual review. This is an
operational and evidence-integrity pass only. No reviewed source/target
landmarks exist yet, so it is not a five-second or ten-second semantic-delay
PASS or FAIL.

## Runtime

| Component | Verified selection |
| --- | --- |
| ASR | Nemotron ASR Streaming `1.2.0` |
| ASR endpointing | 800 ms EOU; word offsets enabled |
| NMT | Riva Translate 1.6B `1.5.2`, `en-US` to `es-US` |
| TTS | Magpie multilingual `1.7.0`, Spanish voice |
| Pipeline | staged telemetry schema 3 |
| TTS publication | incremental 500 ms frames |
| Listener policy | no drop; 5-second target, 8-second urgent, 10-second limit |
| Playback rates | 1.00x / 1.05x / 1.10x |
| Input pacing | 300 ms PCM chunks at source-end boundaries |

All three pinned NIMs were healthy before and after the run. The FastAPI log
contained no runtime error, exception, or OOM signature.

## Capture integrity

| Evidence | Result |
| --- | ---: |
| Source duration / chunks | 60.000 s / 200 |
| Source PCM | 960,000 mono PCM16 samples at 16 kHz |
| Complete translated parents | 23 |
| Validated translated frames | 113 |
| Translated PCM | 806,951 mono PCM16 samples at 16 kHz |
| Translated duration / source ratio | 50.434 s / 0.841x |
| Parents with complete ASR source ranges | 23 of 23 |
| End-only fallback parents | 0 |
| Input and terminal completion | PASS |
| Staged transport/integrity reconciliation | PASS |
| Independent ledger/WAV/hash validation | PASS |
| Canonical listener-schedule replay | PASS |

No translated frame was lost, duplicated, reordered, or mapped to the wrong
parent. The capture directory was verified as mode `0700`, each private
artifact as mode `0600`, and every artifact as ignored by Git. Raw audio,
ledger hashes, private paths, and reviewer records are intentionally omitted
from this tracked result.

## Mechanical listener result

| Metric | Fresh capture |
| --- | ---: |
| First translated audio receipt | 5.1 s |
| Service tail after input | 1.4 s |
| Scheduled listener tail | 6.979 s |
| Time-weighted queue p95 | 4.095 s |
| Peak queue | 5.563 s |
| Time above 10 seconds | 0.000 s |
| Accelerated translated audio | 1.808% |
| Urgent-rate translated audio | 0.000% |
| Queue gate | PASS |

The listener queue remained bounded during this one-minute control and met the
registered mechanical target. That does not establish when a corresponding
translated semantic landmark would be heard.

## Source-parent schedule envelope

| Source boundary to scheduled parent audio | p50 | p95 | maximum |
| --- | ---: | ---: | ---: |
| First frame | 3.814 s | 6.056 s | 6.185 s |
| Final frame | 6.458 s | 7.624 s | 8.505 s |
| Parent scheduled duration | 2.090 s | 4.368 s | 4.690 s |

These envelopes are useful diagnostics, but they remain parent-level bounds.
They are not the delay from an English joke or short reaction to its
corresponding Spanish word, and scheduled digital time is not physical
audibility.

## Comparison with the preceding Magpie control

| Metric | Preceding control | Private capture | Difference |
| --- | ---: | ---: | ---: |
| Queue p95 | 4.181 s | 4.095 s | -0.086 s |
| Peak queue | 5.460 s | 5.563 s | +0.104 s |
| Scheduled listener tail | 6.756 s | 6.979 s | +0.223 s |
| First-parent-frame p95 | 6.043 s | 6.056 s | +0.013 s |
| Final-parent-frame p95 | 8.171 s | 7.624 s | -0.547 s |

Both one-minute runs preserved every frame, remained below the ten-second
queue ceiling, and produced similar queue and parent-envelope behavior. Two
runs do not establish a long-form bound.

## Review packet status

The offline five-file reviewer packet is prepared for this frozen capture. Its
assignment binds the exact ledger and source/translated PCM, and every reviewer
receives the same files. The local review page verifies those bindings before
exporting an anonymous structured observation; it is reviewer tooling only and
does not make a browser part of the deployed S2S path.

No human observation, canonical marker sidecar, or five-second or ten-second
analysis exists yet. Packet readiness does not change the semantic verdict:
the result remains **not evaluated**. The current whole-stream interface is
qualified for this 60-second control; long-form review requires a separately
validated zoomed-window workflow.

## Required review before a semantic result

At least two independent bilingual reviewers must mark the same anonymous
source and translated semantic landmarks in the frozen review WAVs. Use at
least three unambiguous events spread across the beginning, middle, and end,
including a short reaction or punchline-like event when available. Do not
invent marker positions from ASR ranges, frame boundaries, queue telemetry, or
the parent envelopes above.

After the exact marker sidecar is complete, run the analyzer separately at:

- five seconds for the working objective; and
- ten seconds for the soft live-audience ceiling.

Only those reviewed, hash-bound results can answer the scheduled
source-to-translated semantic-delay question. A physical audience-reaction
claim still requires a same-clock acoustic or digital-loopback experiment.

## Verification

The implementation and evidence contracts passed the maintained repository
suite: 1,069 tests passed and one expected environment-dependent test was
skipped. The backend suite separately passed 462 tests with one expected
environment-dependent skip. See the
[headless semantic-delay runbook](HEADLESS_SEMANTIC_DELAY_GATE.md).
