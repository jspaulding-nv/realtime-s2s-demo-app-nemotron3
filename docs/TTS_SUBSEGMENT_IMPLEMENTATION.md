# Default-off post-NMT TTS subsegmentation

## Purpose

The completed long-form matrix showed that translated-audio delivery bursts can
be large even when ASR, NMT, TTS, and the bounded queues all complete
correctly. A privacy-safe duration model selected 40 translated characters as
the strict first experimental cap, with 45 and 60 characters retained as
comparison policies.

This implementation makes that experiment possible without reducing NMT
context. NMT still receives and returns one complete parent segment. Only the
validated target-language text is split before TTS.

The feature is off by default. It is an experiment, not a production latency
claim.

## Configuration

```dotenv
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
STAGED_TTS_SUBSEGMENT_MIN_CHARS=12
```

`STAGED_TTS_SUBSEGMENT_MAX_CHARS=0` preserves the prior one-parent,
one-TTS-call path. A positive maximum enables splitting. The minimum is only a
packing preference: a tiny adjacent clause is merged when the merged payload
fits the maximum, but a valid child may remain shorter.

The intended matched policies are:

| Policy | Maximum translated characters |
|---|---:|
| Control | 0, disabled |
| Strict | 40 |
| Near-boundary | 45 |
| Lower call amplification | 60 |

The backend must be restarted after changing either value. `/api/config`
reports the resolved values and the active telemetry schema.

## Split contract

The splitter applies NFC normalization, collapses Unicode whitespace runs to
one ASCII space, and trims the parent. It then prefers the latest feasible
boundary before the cap in this order:

1. sentence-ending punctuation;
2. comma, semicolon, colon, or dash;
3. whitespace; and
4. a hard Unicode-code-point boundary.

Punctuation and closing marks stay with the preceding content. Empty and
punctuation-only children are rejected. A hard split never begins a child with
a combining mark. Each child carries separator metadata used by tests to prove
that ordered reconstruction exactly matches the normalized parent, including a
rare hard split inside a long token.

Target-language validation remains fail-closed. Each child is defensively
validated again immediately before TTS.

## Identity, ordering, and atomicity

Every TTS child has a stable identity:

```text
(parent_sequence_id, subsequence_id, subsequence_count)
```

`subsequence_id` is zero-based. One NMT worker constructs the complete child
tuple before its first queue wait, then enqueues the children in order. One TTS
worker processes that FIFO stream. A following parent cannot overtake the
current parent's final child.

The existing private-buffer TTS retry applies independently to each child.
PCM from a failed attempt cannot escape. Atomicity is deliberately per child,
not per parent: if child 0 has already been sent and child 1 later fails, child
0 is not withdrawn or duplicated. The listener receives the successful prefix
followed by one error terminal, and the parent remains incomplete.

The pipeline queues one successful PCM frame per child and queues the sole
completion terminal only after all child frames. A parent enters
`completed_sequence_ids` only when its final child is dequeued. The WebSocket
uses the same final-child rule for `websocket_sent_sequence_ids`.

## Telemetry schemas

The disabled control emits schema v1. Missing schema fields on older captures
are also interpreted as v1 and normalized internally to `(sequence, 0, 1)`.
An enabled cap emits schema v2.

Schema v2 separates parent and child accounting:

| Field | Meaning |
|---|---|
| `segments_emitted` | Source/NMT parent count |
| `completed_sequence_ids` | Fully dequeued parent IDs |
| `tts_subsegments_planned` | Children constructed before queue waits |
| `tts_subsegments_produced` | Successful TTS child completions |
| `audio_segments_produced` | Child PCM frames accepted by the output queue |
| `planned_subsegment_keys` | Ordered constructed child identities |
| `synthesized_subsegment_keys` | Ordered successful TTS identities |
| `completed_subsegment_keys` | Ordered dequeued child identities |
| `incomplete_subsegment_keys` | Planned children not dequeued |
| `websocket_sent_subsegment_keys` | Ordered successfully sent child identities |

Every child-bearing TTS, output, and WebSocket-send event records the composite
identity. TTS-start telemetry also records the parent's translated character
count without retaining text.

The batch integrity gate rejects unknown or mismatched schemas; missing,
duplicate, gapped, or reordered children; changed child counts inside a
parent; cap violations; lifecycle disagreement; retry-count disagreement;
parent completion before a final child; and PCM size disagreement. Because
browser binary frames do not carry IDs, received frames are matched
positionally to server sends and every frame size must agree in order.

## Local verification

Install the test dependencies in the active virtual environment:

```bash
python3 -m pip install -r requirements-dev.txt
```

Run the focused contract tests:

```bash
PYTHONPATH=backend:. python3 -m pytest -p no:cacheprovider -q \
  backend/tests/test_target_text_splitter.py \
  backend/tests/test_staged_pipeline.py \
  backend/tests/test_staged_websocket.py \
  backend/tests/test_direct_tts_client.py \
  tests/test_batch_latency_test.py \
  tests/test_analyze_tts_duration.py
```

Then run the complete Python suite:

```bash
PYTHONPATH=backend:. python3 -m pytest -p no:cacheprovider -q \
  backend/tests tests
```

Generated audio, event traces, backend logs, and raw summaries remain under
ignored result directories.

## Matched live canary

Verify the already-running pinned NIMs first:

```bash
curl --fail http://127.0.0.1:9002/v1/health/ready
curl --fail http://127.0.0.1:9001/v1/health/ready
curl --fail http://127.0.0.1:9003/v1/health/ready
docker compose ps
nvidia-smi
```

Commit the implementation before an evidence run, then execute:

```bash
./run_tts_subsegment_canary.sh
```

The runner:

- sources `.env` without printing secrets;
- requires a clean worktree and an in-repository `CANARY_OUTPUT_ROOT` that Git
  confirms is ignored for every formal evidence run;
- resolves the output root canonically, rejects repository escapes (including
  escapes through existing symlinks), and atomically creates a new final run
  directory instead of reusing or mixing evidence;
- finds the unique running container publishing each configured ASR, NMT, and
  TTS HTTP port without relying on a Compose project or container name;
- verifies each running container uses the configured pinned image tag,
  resolves its unique immutable `RepoDigest`, and fails if a supplied
  `*_IMAGE_DIGEST` does not match;
- exports the inspected `sha256:<64-hex>` digests to FastAPI and verifies the
  backend reports the same images and digests before each capture;
- creates one shared five-minute WAV prefix from `long-form-01.mp3`;
- checks its hash once and reuses those exact bytes for every arm;
- runs the disabled, 40, 45, and 60 policies sequentially;
- starts and gracefully stops only its own FastAPI process;
- never starts or stops the Riva NIM containers;
- verifies the backend-reported cap before each capture;
- uses real-time input pacing and the terminal-aware WebSocket harness;
- runs the duration and playback-policy analyzers for every arm;
- emits a cross-arm, privacy-safe JSON and Markdown comparison; and
- writes privacy-sensitive raw evidence only under a Git-ignored,
  in-repository output root (`experiment_results/` by default).

The comparison does not select a winner by default. Automatic selection
requires a complete, explicit set of promotion thresholds, and every candidate
still requires native-language review.

Useful overrides:

```bash
CANARY_SOURCE=test_audio/long-form-02.mp3 \
CANARY_DURATION_SECONDS=300 \
CANARY_CAPS="0 40 45 60" \
CANARY_BACKEND_PORT=8100 \
ASR_HTTP_PORT=9002 \
NMT_HTTP_PORT=9001 \
TTS_HTTP_PORT=9003 \
./run_tts_subsegment_canary.sh
```

The formal runner refuses a dirty worktree. `ALLOW_DIRTY_CANARY=1` is available
only for a disposable probe and is recorded as `non-formal-probe`; it may
bypass the clean-worktree and Git-ignore gates but not the model-provenance
checks. All output roots, including probe roots, must still resolve inside the
repository. The runner performs read-only Docker inspection and never manages
the NIM containers.

## Decision gate

Reject an arm immediately if integrity, terminal, cleanup, PCM parity, or model
completion fails. For every surviving arm compare:

- actual child-duration p50, p95, and maximum;
- TTS call count and retry count;
- output/input duration ratio;
- first audio and service tail;
- fixed and adaptive listener queue p50, p95, peak, and tail;
- time above the 10-second soft ceiling; and
- playback-rate exposure.

Splitting should advance only if smaller delivery bursts do not materially
increase total synthesized duration or listener backlog. A browser/Web Audio
cross-check, synchronized source-event to translated-audible markers, and
native-language quality review remain separate requirements before any
production recommendation.

## Rollback

Set:

```dotenv
STAGED_TTS_SUBSEGMENT_MAX_CHARS=0
```

and restart FastAPI. This restores one TTS call and one PCM frame per NMT
parent without changing the pinned ASR, NMT, or TTS services.
