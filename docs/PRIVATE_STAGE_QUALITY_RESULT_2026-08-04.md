# Private stage-quality result — 2026-08-04

## Decision

Do not attribute the single negative bilingual quality observation to one model
and do not promote the minimum-context segmentation control yet. The fresh
replay exposed two independent effects:

1. automatic punctuation produced several very small source fragments before
   NMT; and
2. the worst-rated review pair compared a fixed 12-second English window with
   complete Spanish parents representing a wider 15.44-second source envelope.

The second effect makes the original `meaning = no` answer confounded. The
reviewer heard valid translated content corresponding to 2.96 seconds before
the English window and 0.48 seconds after it. This observation cannot cleanly
distinguish NMT meaning loss from excerpt-boundary mismatch.

All transcript, translation, audio, reviewer, path, session, and hash evidence
remains under ignored owner-private result directories. This tracked result is
aggregate-only.

## Runtime and method

Both matched runs used:

- the identical 60.0-second source PCM reviewed previously;
- Nemotron streaming ASR 1.2.0;
- Riva Translate 1.5.2;
- Magpie multilingual TTS 1.7.0;
- 800 ms EOU and automatic punctuation;
- bounded ASR/NMT/TTS queues;
- schema-3 incremental 500 ms TTS publication;
- real-time source pacing; and
- no TTS subsegmentation or playback acceleration.

The control retained the existing punctuation behavior. The canary set
`STAGED_SEGMENT_PUNCTUATION_MIN_CHARS=20`. This coalesces a short punctuation
boundary only when more text is already buffered behind it. It does not delay a
standalone short utterance.

## Results

| Metric | Control | Minimum-context canary | Change |
|---|---:|---:|---:|
| Clean completion | yes | yes | unchanged |
| Source seconds sent | 60.000 | 60.000 | unchanged |
| Total translation parents | 23 | 20 | -13.0% |
| Worst-window parents | 10 | 7 | -30.0% |
| Generated Spanish, whole run | 51.920 s | 51.038 s | -1.7% |
| Generated Spanish, worst window | 17.601 s | 17.973 s | +2.1% |
| Tail drain | 1.739 s | 1.540 s | -0.199 s |
| NMT retries | 0 | 0 | unchanged |
| TTS retries | 0 | 0 | unchanged |

The canary successfully reduced fragmentation, but it did not reduce the
worst window's synthesized duration. Its larger text units are easier to
evaluate for NMT context, yet aggregate timing alone cannot prove improved
meaning or naturalness.

Nemotron remained operationally strong in both runs: it completed the exact
real-time-paced input, produced 18 non-empty finals, and provided a usable word
offset envelope for every final. The issue observed here is how automatic
punctuation is converted into NMT units, not a Nemotron container failure.

## Review-alignment finding

The original frozen review pair boundaries were:

| Pair | Fixed English window | Complete-parent source envelope | Boundary mismatch |
|---|---:|---:|---:|
| 1 | 6.00–18.00 s | 8.24–17.52 s | parent content is inside window |
| 2 | 24.00–36.00 s | 21.04–36.48 s | +2.96 s prefix, +0.48 s suffix |
| 3 | 42.00–54.00 s | 42.32–55.28 s | +1.28 s suffix |

Pair 2 had both the largest boundary mismatch and the only `meaning = no`
answer. That correlation is not proof, but it is sufficient to invalidate a
model-level conclusion from this one observation.

## Next gate

1. Use complete-parent English envelopes whenever complete-parent Spanish
   audio is presented. The private analyzer now emits both the old fixed source
   clip and a content-aligned parent-envelope source clip automatically.
2. Ask the available bilingual reviewer for one short, parent-aligned A/B on
   the worst window: control versus the 20-character canary. No pause-at-a-mark
   task is required.
3. Promote the control only if the reviewer prefers it for meaning and
   intelligibility without a material latency regression.
4. If neither version is acceptable, keep Nemotron and investigate a source
   segmentation/context policy before changing TTS. If Spanish text is correct
   but its audio is still hard to understand, return to the Magpie versus
   Chatterbox TTS quality gate.
5. After quality passes, run a five-minute real-time canary and enforce the
   audience-facing bounded playback queue target of 5–10 seconds. A short
   60-second tail drain does not prove long-form bounded latency.

## Reproduction

See [PRIVATE_STAGE_QUALITY_ISOLATION.md](PRIVATE_STAGE_QUALITY_ISOLATION.md).
The diagnostic is default-off and its private outputs must remain ignored.
