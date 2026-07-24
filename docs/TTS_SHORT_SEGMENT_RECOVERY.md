# Atomic TTS recovery for an intermittent short-segment failure

## Scope

This note records the failure that stopped the first post-reboot formal matrix,
the privacy-safe replay, and the narrowly bounded recovery added afterward.
It does not contain source transcripts, translated text, customer names, or
generated audio.

The failed baseline remains:

```text
experiment_results/post-reboot-matrix-20260724T022814Z-700aeec
```

That run passed its one-minute preflight, then stopped during Sample 01 at
source position 39.3 seconds. Samples 02 and 03 were not attempted.

## Diagnosis

Sequence 7 was emitted from a punctuation boundary spanning source time
35.44-37.36 seconds:

- source length: 3 characters;
- validated `es-US` target length: 2 characters;
- NMT duration: 114 ms;
- NMT retry count: zero;
- TTS queue residence: 293 ms; and
- TTS result: gRPC `UNKNOWN` from an internal Magpie/Triton tensor-shape
  failure with a zero-length encoder dimension.

The failure was not an ASR, NMT, queue, transport, GPU-capacity, or container
health problem:

- no bounded queue put blocked;
- the TTS container did not restart and was not OOM-killed;
- substantial GPU memory remained free;
- a known-good TTS request succeeded immediately afterward; and
- an earlier run of the same source hash and sequence provenance successfully
  synthesized the same 3-to-2-character shape.

The missing completion terminal, incomplete sequence ID, and early input abort
were consequences of the fail-closed TTS error, not independent causes.

## Privacy-safe replay

`diagnose_short_segment.py` keeps ASR and NMT text in memory only. Its JSON
contains source provenance, character/category counts, model outcomes, and a
keyed HMAC for equality comparisons inside one report. The random HMAC key is
discarded and never exported. A plain SHA-256 is deliberately not used because
a two-character value is trivial to enumerate.

The production-style replay uses the bounded asynchronous ASR bridge, interim
and timeout age checks, and the same punctuation segmenter as the staged
backend. It sent the first 39.3 seconds of Sample 01 at real-time pace:

```bash
PYTHONPATH=.python-packages:backend:. python diagnose_short_segment.py \
  --file test_audio/long-form-01.mp3 \
  --duration-seconds 39.3 \
  --sequence-id 7 \
  --repetitions 5 \
  --client-max-retries 0 \
  --json-output experiment_results/short-segment-diagnostic/report.json
```

The corrected replay reproduced the original boundary exactly: sequence 7,
ASR final 4, source time 35.44-37.36 seconds, three source characters, and a
validated two-character target consisting structurally of one Latin letter and
one punctuation character. Repeating that exact in-memory target with client
retry disabled produced four successful calls and one gRPC `UNKNOWN`. No raw
text was retained.

For comparison, the tool combined that source with the immediately following
segment, retranslated it, and issued five raw TTS calls. All five completed.
This small result is encouraging but is not a production quality or statistical
gate.

A second run enabled the new retry policy and issued 20 exact isolated calls:

```bash
PYTHONPATH=.python-packages:backend:. python diagnose_short_segment.py \
  --file test_audio/long-form-01.mp3 \
  --duration-seconds 39.3 \
  --sequence-id 7 \
  --repetitions 20 \
  --client-max-retries 1 \
  --isolated-only \
  --json-output experiment_results/short-segment-diagnostic/retry-report.json
```

All 20 calls completed. Two reported `client_retry_count=1`, proving on the
live pinned Magpie service that the first `UNKNOWN` was discarded and the
second atomic attempt succeeded. These are targeted diagnostic calls, not
independent ASR/NMT samples or a replacement for the formal matrix.

## Recovery contract

The staged direct TTS adapter now permits at most one retry, controlled by:

```dotenv
STAGED_TTS_MAX_RETRIES=1
```

The retry is deliberately narrower than a general transient-error loop:

1. Target text must already have passed both `es-US` validation gates.
2. The first attempt must fail with gRPC `UNKNOWN`.
3. The first attempt's PCM buffer is cancelled and discarded.
4. The client must still be connected and not closing.
5. The identical request is issued once more.
6. Both attempts share the original staged TTS deadline.
7. Only one complete successful PCM segment can reach the output queue.
8. A second failure is terminal and retains the sequence ID and retry count.

There is no retry for validation, unsupported language/voice, cancellation,
deadline, resource exhaustion, invalid arguments, empty audio, partial PCM
frames, response-size limits, or segment-size limits.

Successful recovery records `retry_count=1` on `tts/completed`. The session
summary's `tts_retry_count` must equal the sum of completed TTS retry counts.
An exhausted recovery records `retry_count=1` on `tts/error`, while the
successful-recovery total remains unchanged.

## Why coalescing is not enabled yet

True post-NMT coalescing changes the current one-emitted-segment to one-PCM-send
integrity contract. A correct implementation needs grouped synthesis units,
grouped sequence IDs, flattened completion accounting, and corresponding
WebSocket and batch-integrity changes. Reusing one sequence ID for two
translations would lose provenance.

The replay established one failing structural shape and the adjacent-context
variant passed five calls, but that is not enough to define a general
coalescing classifier. Broadly holding short utterances would add audience
delay to valid phrases such as short answers. Coalescing therefore remains a
follow-up if the fresh matrix exhausts the retry or a larger probe establishes
a bounded policy. The atomic retry directly addresses the observed
intermittent failure without changing segmentation or normal-path latency.

## Verification and formal-run rule

Focused tests cover:

- first-attempt private PCM followed by `UNKNOWN`, then successful retry;
- two `UNKNOWN` failures and privacy-safe terminal telemetry;
- non-retryable gRPC and local safety failures staying single-attempt;
- exactly one ordered pipeline output after recovery;
- exhausted-retry sequence attribution; and
- batch reconciliation of `tts_retry_count`.

The failed `700aeec` matrix cannot be resumed with this code because the
experiment harness correctly requires the original clean Git commit. Preserve
it as the baseline. After committing the recovery, restart FastAPI with the
same pinned NIM digests, pass preflight, and start a new three-sample matrix.
