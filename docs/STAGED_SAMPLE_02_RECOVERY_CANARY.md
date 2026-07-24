# Sample 02 post-recovery staged canary

## Purpose and status

On 2026-07-23, one full, real-time Sample 02 run completed through the direct
staged path. The ignored artifact directory identifies short commit
`55b59bd`, which resolves in the local Git history to
`55b59bd513014188cb7205b9dc9b44b077eca19d`:

```text
Nemotron ASR
  -> punctuation-aware segmenter
  -> bounded NMT queue
  -> Riva Translate
  -> bounded TTS queue
  -> Magpie TTS
  -> ordered WebSocket PCM
```

This was a targeted recovery canary after the diagnosed short punctuated
segment failure. It passed the operational and integrity gates, including all
three observed uses of the narrow NMT recovery. It was not launched as part of
the resumable three-sample experiment and is therefore not a matrix checkpoint.
A new provenance-frozen matrix must still start with its own preflight.

The ignored local evidence consists of the generated event CSV, summary JSON,
and latency plot. This document retains only aggregate, transcript-free
measurements suitable for the tracked repository.

The commit is inferred from that directory label and local Git history; the
summary JSON does not itself embed a Git SHA.

## Frozen model and pipeline configuration

The backend reported staged mode, English input, Spanish output, 800 ms ASR
EOU, and the following exact images. The shared Nemotron request builder
enables automatic punctuation:

| Stage | Image | Verified local digest |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |

The ASR used the English `batch_size=32` profile with word offsets disabled.
The TTS used the Spanish multilingual profile with batch size 8. Segment
limits were 240 characters or 2,000 ms. NMT, TTS, and output queues each had a
configured capacity of four.

The Compose file remains version-tag pinned for portability. The digests above
are the immutable identities recorded by `/api/config` for this run; a future
formal experiment should independently verify and freeze them in its manifest.

## Operational result

The canary artifact was finalized at 2026-07-23 23:07 UTC.

| Metric | Observed |
|---|---:|
| Input completed | yes |
| Translation completed | yes |
| Staged state / outcome | `closed` / `complete` |
| Integrity validation | passed, no errors |
| Source input duration | 2,427.010625 s |
| Translated PCM duration | 2,641.342063 s |
| Output/input duration ratio | 1.088310877x |
| Translated-duration excess | 214.331438 s |
| First translated audio | 4.807140 s |
| Last-audio arrival after input ended | 0.308061 s |
| Completed-terminal arrival after input ended | 1.455045 s |
| Harness drain observation | 2.255528 s |
| Fixed 1.00x arrival-replay listener tail | 239.156018 s |
| Emitted / produced / completed segments | 805 / 805 / 805 |
| Completed sequence IDs | contiguous 0–804 |
| WebSocket-sent / client-received PCM | 805 / 805 segments; 84,522,946 / 84,522,946 bytes |
| Incomplete IDs | none |
| Standalone fillers suppressed | 6 |
| NMT / TTS / output maximum queue depth | 4 / 4 / 1 |
| NMT / TTS / output blocked puts | 17 / 5 / 0 |
| Connection loss / timeout / server error | none |
| Pipeline failure / cleanup errors | none |

The bounded queues applied backpressure rather than dropping or reordering
work. The output queue stayed below capacity, and all 805 audio segments
reached ordered completion. Exactly one completed terminal followed the audio,
and no PCM arrived afterward.

## NMT recovery evidence

The guarded punctuation-normalized recovery completed exactly three times, for
sequence IDs 77, 92, and 449. Each successful NMT event reported
`retry_count=1`, and the session-level `nmt_retry_count` was 3. All three
results passed exact target-language and target-script validation before TTS.

No source or translated text is retained here. The result verifies the
recovery contract against a full sample; it does not broaden the eligibility
rule or permit retries for RPC, cardinality, language-metadata, ineligible
input, or second-attempt failures. See
[Narrow NMT recovery for short punctuated segments](NMT_SHORT_SEGMENT_RECOVERY.md).

## Audience-delay interpretation

The service path itself kept pace at the end: last audio arrived 0.308 seconds
after source input ended, and the terminal completion arrived after 1.455
seconds. The listener result was very different. Spanish PCM was 8.83% longer
than the English source, and fixed-rate playback ended 239.156 seconds after
source input.

This is the delayed-punchline risk in measurable form. A late-program joke can
reach the translation service promptly while still waiting behind several
minutes of already generated Spanish audio.

Aggregate duration alone also shows why 1.05x may be insufficient for this
sample:

| Playback rate | Effective translated duration | Difference from source duration |
|---|---:|---:|
| 1.00x | 2,641.342 s | +214.331 s |
| 1.05x | 2,515.564 s | +88.553 s |
| 1.10x | 2,401.220 s | -25.791 s |

The 1.10x row means only that aggregate media duration can catch up. Burst
timing, the initial delay, local expansion, queue policy, and speech quality
still require actual browser playback and native-listener validation. The
audience objective remains a queue near five seconds with ten seconds as a
soft ceiling.

The fixed tail is a gapless 1.00x replay of client arrival timing, not observed
browser-device playback. The output/input ratio includes silence in the whole
source and is not an utterance-aligned speech-only expansion measurement.
Queue depths above count work items, not seconds of waiting audio. This single
canary provides no repeat variance, semantic-alignment result, or translation
and prosody quality conclusion.

## VM restart observation

The canary artifacts were complete before the VM later stopped because its
lease expired. After the lease was extended and the VM restarted, all three
Compose-managed NIM containers restarted at approximately 2026-07-24 00:03:24
UTC and returned to `healthy`. Their configured `unless-stopped` restart
policy behaved as intended.

The separately launched FastAPI process did not return automatically; its
configured endpoint was not listening after the reboot. Operators must
relaunch FastAPI and recheck `/` and `/api/config` before another test. A host
reboot therefore does not justify resuming directly into a long-form capture.

## Next formal experiment

1. Confirm the worktree is clean at the intended commit.
2. Verify all three image tags and digests and confirm each NIM is healthy.
3. Relaunch FastAPI in staged mode and confirm the frozen `/api/config`.
4. Run the 60-second terminal-aware preflight.
5. Start a new one-repeat Sample 01, Sample 02, and Sample 03 matrix through
   `run_long_form_experiment.py`.
6. Compare fixed 1.00x with 1.05x and 1.10x playback, then cross-check the
   actual Web Audio queue and synchronized marked-phrase delay.
7. Obtain native-Spanish review before promoting accelerated playback or TTS
   prosody.

The standalone canary is useful evidence, but it must not be copied into or
treated as a completed capture in the new matrix manifest.
