# Private 60-second stage-quality isolation

## Purpose

Use this diagnostic after a bilingual reviewer reports missing meaning or poor
Spanish intelligibility. It replays the exact reviewed source PCM through the
current staged Nemotron ASR -> Riva NMT -> Magpie TTS path and privately retains
the stage text needed to distinguish among three failure classes:

1. source audio does not match the English ASR text: ASR or segmentation;
2. English text is faithful but Spanish text loses meaning: NMT or insufficient
   segment context; or
3. both texts are faithful but generated Spanish is difficult to understand:
   TTS.

The bilingual ratings describe the original frozen translated WAV. This run is
a fresh replay of the identical source PCM, so it establishes whether the
failure reproduces; it is not post-hoc proof of what the original model calls
returned.

## Privacy boundary

`--private-stage-trace` is off by default. When enabled, the staged smoke JSON
contains English transcript text, Spanish translation text, input paths,
service endpoints, and audio hashes. Run with `umask 077`, write only to a new
child of ignored `experiment_results/`, and do not commit or share these files
externally. The analyzer creates a `0700` directory and `0600` files.

## Run

Start from the repository root with the pinned services already healthy. Set
the frozen packet and returned review paths for the local VM:

```bash
set -a
source .env
set +a

PACKET="experiment_results/semantic-delay-60s-attempt-01"
REVIEW="/path/to/anonymous-quick-quality-review.json"
RUN="experiment_results/private-stage-quality-attempt-01"

test ! -e "$RUN"
install -d -m 700 "$RUN"
umask 077

STAGED_TTS_INCREMENTAL_PUBLISH=1 \
python3 staged_pipeline_smoke.py \
  --file "$PACKET/source-review.wav" \
  --duration-seconds 0 \
  --terminal-grace-seconds 180 \
  --private-stage-trace \
  --pcm-output "$RUN/translated-fresh.pcm" \
  --json-output "$RUN/private-stage-trace.json"

python3 analyze_private_stage_quality.py \
  --source-wav "$PACKET/source-review.wav" \
  --translated-pcm "$RUN/translated-fresh.pcm" \
  --stage-report "$RUN/private-stage-trace.json" \
  --review "$REVIEW" \
  --output-dir "$RUN/window-analysis"
```

The analyzer fails if the review, source PCM, stage report, translated PCM, or
per-parent byte totals do not match. For each review window it creates:

- the exact fixed English review-window excerpt;
- a second English excerpt expanded to the complete source envelope of every
  selected TTS parent;
- a fresh Spanish WAV made from every complete TTS parent that overlaps that
  source window; and
- private JSON/Markdown listing the English ASR text, Spanish NMT text, TTS
  timing, and the original reviewer ratings.

The translated diagnostic excerpt intentionally uses complete parent audio.
Use it with the parent-envelope English WAV for a content-aligned comparison.
The fixed-window English WAV is retained to expose any boundary mismatch in
the original review design. Neither comparison asks a listener to find or
pause at a semantic boundary.

## Interpretation

Start with the worst-rated window. Listen to its source WAV while reading the
ASR lines. If those match, read the Spanish NMT lines for meaning. Only after
both text stages pass should the fresh Spanish WAV be used to assess Magpie.

If the fresh run is correct but the frozen reviewed audio is not, repeat once
before changing a model. A mismatch across identical-source runs points to
model/runtime nondeterminism or a capture/alignment defect rather than a stable
translation error.

## Minimum-context punctuation canary

If the private evidence shows that automatic punctuation divided one ASR final
into tiny fragments before NMT, repeat the same run with this default-off
source-side control:

```bash
STAGED_SEGMENT_PUNCTUATION_MIN_CHARS=20 \
STAGED_TTS_INCREMENTAL_PUBLISH=1 \
python3 staged_pipeline_smoke.py ...
```

The control skips a short punctuation boundary only when additional text is
already buffered behind it. A genuinely standalone short utterance such as
"No." is still emitted immediately, so the control does not impose a blanket
minimum wait on short audience-relevant speech. Compare the same source
windows, parent count, NMT meaning, generated duration, and tail drain against
the zero-value control before considering promotion.
