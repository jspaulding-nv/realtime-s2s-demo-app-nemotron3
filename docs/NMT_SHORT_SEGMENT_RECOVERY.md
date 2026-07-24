# Narrow NMT recovery for short punctuated segments

## Scope

This document defines one fail-closed recovery for a deterministic boundary
case in the pinned Riva Translate 1.6B `1.5.2` service. It is not a general NMT
retry policy and does not relax target-text validation.

The staged pipeline observed an isolated source segment with this shape:

```text
1-32 ASCII letters + one terminal ".", "?", or "!"
```

The NMT response declared the requested `es-US` language but contained a
different writing system and unsupported punctuation. The existing target
validator stopped the segment before TTS, as intended.

## Privacy-safe diagnosis

The failure was reproduced with a controlled replay that recorded request
shape and validation outcome without retaining source or translated words:

| Replay condition | Result |
|---|---:|
| Exact isolated short-token shape | invalid wrong-script output, 5/5 |
| Same token with terminal punctuation removed | valid Latin-script output, 5/5 |
| Same token with preceding context | valid Latin-script output, 5/5 |
| Same token with following context | valid Latin-script output, 5/5 |
| Same token with both neighboring contexts | valid Latin-script output, 5/5 |

These results identify a deterministic short single-token plus punctuation
edge case in the pinned NMT release. They do not implicate ASR, TTS, GPU
capacity, worker concurrency, or a transient gRPC failure. Repeating the
unchanged request would reproduce the unsafe result; changing sentence context
also works but would complicate ordering and add latency.

## Recovery contract

The direct NMT adapter normally issues exactly one request. It may issue one
additional request only when all of the following are true:

1. The requested target language is exactly `es-US`.
2. After trimming surrounding whitespace, the original source consists of
   exactly 1-32 characters in `[A-Za-z]`.
3. That token is followed by exactly one terminal character in `[.?!]`.
4. The first NMT RPC returns the expected cardinality and response-language
   metadata.
5. The first translated text raises `TargetTextValidationError`.

For that one case, the adapter removes only the terminal punctuation from the
NMT request text and issues exactly one more RPC. It retains the original
`TextSegment`, sequence ID, ASR-final provenance, source timing, and emission
reason. The transformed request is an adapter-local recovery input, not a new
segment and not a change to the transcript record.

The second response must independently pass:

- exact-one translation cardinality;
- exact `es-US` response-language metadata; and
- the complete target-text validator.

Only a validated result enters the TTS queue. The defensive validation at the
TTS boundary remains in place.

## Explicit non-retry cases

There is no retry for:

- gRPC errors or deadlines;
- zero or multiple translations;
- missing or mismatched response-language metadata;
- targets other than exact `es-US`;
- blank input;
- a source with zero or more than 32 ASCII letters;
- digits, spaces, apostrophes, hyphens, non-ASCII letters, multiple tokens, or
  more than one terminal character;
- source text without one of the three allowed terminal characters;
- successful first-pass validation; or
- any failure of the second request or its validation.

The adapter never retries an unchanged NMT request. It never strips unsupported
characters from translated text, relabels response metadata, or forwards an
invalid result to TTS. A second failure terminates the session through the
existing first-failure path.

## Telemetry and integrity

A successfully recovered `TranslatedSegment` records `retry_count=1`; the
normal path records `retry_count=0`. The staged `nmt/completed` event carries
the same `retry_count`, and the session summary exposes `nmt_retry_count` as
the sum of completed NMT-event retries.

The batch integrity validator requires every completed NMT event to report
either zero or one retry and requires the summary total to equal the event
sum. It therefore detects missing, duplicated, or inconsistent recovery
telemetry. If the second attempt fails, its typed `nmt/error` event carries the
original sequence/provenance and `retry_count=1` without carrying text. It has
no completed NMT event and is not included in the successful-recovery summary
total.

## Finalized export and failed-capture evidence

Staged shutdown can yield while workers and channels close. The backend now
retains an immediately exportable staged snapshot before asynchronous cleanup,
then replaces it with the finalized `closed` snapshot. The batch client polls
for that closed snapshot for a bounded close-settling interval. If the interval
expires, it returns the latest object so normal integrity validation can report
the incomplete state instead of losing the failure evidence.

The long-form harness promotes only a fully validated CSV/summary/plot set.
When a capture fails after any of those generated artifacts exist, it retains
only this allowlist under the ignored run directory:

- event CSV, renamed `events.csv`;
- result summary, renamed `summary.json`; and
- latency plot, renamed `latency.png`.

The failure directory is private to the owner, retained files use owner-only
permissions, and the manifest records relative paths and SHA-256 hashes under
`failure_artifacts`. The harness does not copy source audio, generated audio,
temporary credentials, container logs, or arbitrary staging files into that
record. These local artifacts can still contain detailed timing metadata and
must be sanitized before external sharing.

## Verification

Run the focused deterministic checks first:

```bash
PYTHONPATH=backend \
python -m pytest \
  backend/tests/test_direct_nmt_client.py \
  backend/tests/test_staged_pipeline.py \
  backend/tests/test_staged_websocket.py \
  tests/test_batch_latency_test.py \
  tests/test_long_form_experiment.py
```

The checks must cover:

- one eligible failure followed by one valid recovery;
- original segment identity and provenance after recovery;
- no retry for every ineligible or non-validation failure class;
- fail-closed behavior when the second response is invalid;
- `retry_count` and `nmt_retry_count` consistency;
- an export request during asynchronous cleanup and after final close; and
- allowlisted, neutral-name, hashed failed artifacts with no source-audio
  retention.

The recovery implementation was verified on 2026-07-23 with:

- `376 passed, 1 skipped` across `backend/tests` and `tests`;
- all 88 frontend tests, frontend lint, and the production build;
- ready HTTP health responses from the pinned NMT, ASR, and TTS services; and
- one privacy-safe live replay through the direct adapter using the exact
  protected failure segment. The replay reported `retry_count=1`, preserved
  sequence 77, returned exact `es-US`, and passed target validation without
  printing either source or translated text.

The subsequent full Sample 02 recovery canary also passed. It reached
`closed` / `complete` with 805 emitted, produced, and completed segments;
contiguous IDs 0–804; no incomplete IDs, stage failure, cleanup error,
connection loss, or timeout; and three successful recoveries at sequence IDs
77, 92, and 449. Its aggregate, transcript-free record is
[Sample 02 post-recovery staged canary](STAGED_SAMPLE_02_RECOVERY_CANARY.md).

That standalone canary closes the targeted recovery gate, but it is not a
resumable three-sample checkpoint. For the next formal run:

1. Relaunch the staged FastAPI backend after any host restart and verify `/`
   and `/api/config`.
2. Run the one-minute staged WebSocket preflight.
3. Start a new clean, provenance-frozen one-repeat run across
   `long-form-01.mp3`, `long-form-02.mp3`, and `long-form-03.mp3`.
4. Retain the ignored runtime directory, review any allowlisted failure record,
   and publish only a compact sanitized result.
5. After operational completion, continue the separate browser queue,
   marked-phrase latency, and listener-quality evaluation. This recovery
   prevents one invalid short NMT result from reaching TTS; it does not by
   itself bound long-form audience playback delay.
