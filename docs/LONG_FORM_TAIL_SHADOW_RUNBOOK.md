# Complete three-sample live tail-shadow runbook

## Purpose

This gate extends the selected 100 ms, observation-only tail-freshness shadow
from five minutes to the three complete registered long-form fixtures. It does
not cancel, trim, replace, or otherwise change audible Spanish output.

The three source files contain approximately 103.7 minutes of audio in total:

| Sample | Source duration |
|---|---:|
| 01 | 31:48 |
| 02 | 40:27 |
| 03 | 31:28 |

Allow additional time for frontend builds, application startup, TTS drain, and
independent validation. Samples run sequentially because the backend timing
contract permits one active translation session.

## Fixed experiment contract

Every sample uses:

- the tracked source identity registered in `test_audio/SHA256SUMS`;
- conversion to 16 kHz, mono, signed 16-bit PCM inside the private evidence
  directory;
- Nemotron streaming ASR with 800 ms EOU, automatic punctuation, and word
  offsets;
- the staged ASR, NMT, and TTS queues;
- schema-3 TTS incremental publication in 100 ms frames;
- adaptive, no-drop playback at 1.00x, 1.05x, and 1.10x;
- an observation-only 10-second projected scheduled-audio cap with a 100 ms
  guard; and
- rendered-digital PCM capture disabled.

The shadow may project trailing-suffix cancellation, but the browser continues
to schedule and play the complete translated output.

## Readiness

Use a clean committed checkout. Confirm that the pinned Nemotron ASR, Riva
Translate NMT, and Magpie TTS services are healthy. Use Node.js 22.12.0 or a
compatible Node.js 22 release for the production frontend build.

Inspect the batch plan without contacting Riva:

```bash
python3 run_live_tail_shadow_long_form.py \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm \
  --dry-run
```

## Run

From the repository root:

```bash
python3 run_live_tail_shadow_long_form.py \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

The runner creates an ignored owner-private directory named like:

```text
experiment_results/live-tail-shadow-long-form-100ms-<UTC>-<commit>/
```

Its `manifest.json` is checkpointed before and after every child process. Each
sample uses an immutable attempt directory. A failed attempt is retained and a
resume creates the next numbered attempt rather than overwriting evidence.

## Resume

After an interruption, use the exact directory printed by the runner:

```bash
python3 run_live_tail_shadow_long_form.py \
  --resume-dir experiment_results/<batch-directory> \
  --npm /path/to/node-v22.12.0-linux-x64/bin/npm
```

Resume requires the original Git commit and the exact registered source
hashes. Completed samples are skipped. The failing or interrupted sample gets
a new attempt directory.

## Per-sample acceptance

Accept a mechanical capture only when:

- the application reaches natural completion;
- the independent replay matches every browser shadow decision;
- the projected queue never exceeds 10 seconds after applying the shadow;
- every projected loss is a complete parent or one continuous trailing parent
  suffix;
- the single-tail invariant passes;
- no Riva container restarts and no WebSocket or terminal pipeline error occurs;
  and
- generated duration, retention, projected removal, affected parents, and the
  longest omitted suffix are reported.

These gates establish mechanical overload behavior only. They do not show that
an omitted suffix is semantically safe, that a translated joke arrives within
10 seconds, or that accelerated Spanish is acceptable to a native listener.

## Evidence and publication

Keep the complete manifest, generated PCM fixture, timing CSV, shadow JSON,
logs, and analyzer reports outside Git. Commit only a compact sanitized result
that contains aggregate numeric measurements, model versions, source fixture
IDs, and explicit claim boundaries.
