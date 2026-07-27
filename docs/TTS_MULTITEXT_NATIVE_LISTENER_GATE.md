# TTS Multi-Text Native-Listener Gate

## Purpose

This gate determines whether the pinned Chatterbox candidate should advance
from a one-sentence timing signal to a default-off speech-to-speech preflight.
It compares Chatterbox with the current pinned Magpie baseline across a small,
versioned Spanish corpus and produces model-label-blind audio for native
Spanish review.

The direct-TTS comparison can evaluate synthesized duration, first-audio
latency, stream continuity, pronunciation, intelligibility, naturalness, and
live-listening acceptability. It cannot measure end-to-end source-to-listener
delay or prove that a punchline remains synchronized with a live room. Those
questions remain part of the later staged speech-to-speech gate.

## Locked boundary

- Magpie remains the active speech-to-speech TTS backend.
- Chatterbox remains an isolated, default-off comparison service.
- Magpie uses its pinned `1.7.0` image, `es-US` locale, current public voice,
  and repository-local `nvidia-riva-client==2.24.0`.
- Chatterbox uses its digest-qualified `1.0.0` image, `es-ES` locale, built-in
  public voice, and isolated `nvidia-riva-client==2.26.0`.
- Chatterbox uses the documented default `exaggeration_factor=0.5`.
- `cfg_weight` is omitted entirely.
- No playback-rate change, silence compression, normalization, or other audio
  post-processing is applied.
- Audio remains mono, 16-bit PCM at 22,050 Hz.

The two systems use different voices, genders, and Spanish locales. This is a
system-candidate comparison, not a controlled study that can attribute a
difference solely to model architecture.

## Fixed corpus and repetitions

The tracked corpus has six sanitized, neutral fixture IDs:

1. micro response;
2. short statement;
3. medium neutral statement;
4. punctuation-heavy question and exclamation;
5. expressive, punchline-like sentence; and
6. numbers expressed as words in a longer clause.

Each arm receives one discarded warm-up followed by five measured requests per
fixture. Execution order alternates by fixture to reduce simple arm-order
bias. Runtime reports retain fixture IDs, versions, and character/byte counts,
but not input text, input paths, or text fingerprints.

Two measured repetitions per arm and fixture are selected by the runner before
timing results are inspected. The selection must never choose the shortest or
best-sounding output. The resulting 24 source clips are relabeled for blind
review; raw model-labeled audio and the blinding key remain separate.

## Mechanical promotion gate

The runner may classify the client metrics as passed only when all of these
conditions hold:

- every measured request succeeds;
- Chatterbox's overall median of per-fixture duration ratios is no greater
  than `0.92` relative to Magpie;
- Chatterbox is at least 5% shorter on at least four of six fixtures;
- Chatterbox median time to first audio is no greater than 1.25 seconds; and
- every measured Chatterbox trial needs no more than 1.25 seconds of startup
  buffering to avoid the observed immediate-playback deficit.

Protocol version 1 tested a 1.00-second cap only on the preselected listening
repeats. Its first live run missed by 0.009 seconds while every measured trial
remained below 1.172 seconds. That run remains a failure. Protocol version 2
predeclares a 1.25-second cap and applies it to all 30 measured Chatterbox
trials, then requires a fresh run; it does not retroactively reclassify the
version-1 evidence.

Container health, restart count, OOM state, and GPU headroom are recorded
outside the client report before and after the run. Until those checks are
bound to the run, the report status remains
`client_metrics_passed_pending_runtime_attestation`. Passing both parts means
only that a native-listener review is worthwhile.

## Native-listener protocol

Use at least two independent native Spanish reviewers; three are preferred.
Use anonymous reviewer codes rather than names or email addresses. Review
unaltered WAVs at a fixed comfortable volume with headphones.

The review has two ordered passes:

1. **Audio-only:** score naturalness, pace, prosody, listening ease, and
   acceptability for a live audience without opening the reference sheet.
2. **Reference-visible:** score intelligibility, pronunciation, and text
   fidelity, then record only the predefined defect flags.

Do not enter names, organizations, customer context, or free-form comments in
the scoring files. A separately randomized pairwise preference is recorded
for each fixture. Model labels remain hidden until every reviewer has finished
both passes.

The candidate quality gate requires:

- no content omission, hallucination, or mixed-language defect independently
  confirmed by two reviewers;
- median intelligibility of at least 4/5 for every fixture;
- overall naturalness, pronunciation, and prosody no more than 0.5 points
  below Magpie;
- no fixture at least one full point worse than Magpie on those dimensions;
  and
- at least 80% of Chatterbox ratings marked acceptable for a live audience,
  with no fixture rejected by a reviewer majority.

Shorter audio is never allowed to override an intelligibility or content
failure.

## Privacy and artifact handling

Generated artifacts stay under ignored `experiment_results/`. Directories are
mode `0700`, and files are mode `0600`. The initial shareable reviewer tree
contains only opaque clip names, fixed instructions, and blank audio-only
scoring sheets. It does not contain reference text or the model key.

The private tree retains:

- raw model-labeled WAVs and client reports;
- the blind mapping and audio hashes;
- corpus-to-request reconciliation; and
- reference-visible phase-two templates until phase one is completed and
  hash-locked; and
- completed reviewer score files.

The initial reviewer tree contains only audio and the blank audio-only scoring
sheet. It deliberately does not contain reference text. Release each
reviewer's phase-two template only after validating and recording the SHA-256
of that reviewer's completed phase-one file.

WAV files contain synthesized speech and must be reviewed before any external
sharing. Commit only sanitized aggregate conclusions, never raw audio, blind
keys, or reviewer-level scores.

## Runtime sequence

Verify that the existing ASR, NMT, and Magpie services are healthy. Recreate
the isolated Chatterbox service so it inherits the current five-minute stop
grace period:

```bash
LOCAL_NIM_CACHE=/absolute/path/to/shared/nim-cache \
  docker compose --profile chatterbox-canary up -d \
    --force-recreate chatterbox-tts
```

Wait for both health interfaces and confirm GPU headroom:

```bash
curl --fail http://localhost:9004/v1/health/ready
docker inspect --format \
  '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' \
  s2s-eval-chatterbox-tts
nvidia-smi
```

Run the multi-text orchestrator from the repository root. It launches each arm
in a separate `python3 -S` process so the two pinned Riva client versions never
share an interpreter:

```bash
PYTHONNOUSERSITE=1 PYTHONPATH="$PWD" \
  python3 -S run_blinded_tts_comparison.py
```

Formal outputs are restricted to a fresh child of the repository's ignored
`experiment_results/` tree. A separate explicit override exists for an
approved private mount; do not use it for a tracked checkout path.

After recording post-run health and copying the reviewer-only tree, stop only
the comparison arm:

```bash
LOCAL_NIM_CACHE=/absolute/path/to/shared/nim-cache \
  docker compose --profile chatterbox-canary stop chatterbox-tts
```

ASR, NMT, and Magpie remain running.

## Promotion after a pass

If both the mechanical and native-listener gates pass:

1. add Chatterbox behind a default-off backend selector;
2. run a one-minute staged speech-to-speech preflight with the independently
   validated TTS startup buffer, no greater than 1.25 seconds, inside the
   overall bounded 5-10-second audience queue;
3. evaluate source-event-to-audible-target timing for short reactions and
   punchline-like phrases; and
4. promote to one five-minute stress sample before any three-file long-form
   matrix.
