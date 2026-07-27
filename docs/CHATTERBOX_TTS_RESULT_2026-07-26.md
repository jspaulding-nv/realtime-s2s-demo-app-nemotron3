# Chatterbox TTS Canary Result — 2026-07-26

## Decision

Chatterbox TTS Multilingual `1.0.0` passed the isolated runtime gate and
produced materially shorter Spanish audio than the current Magpie control for
the fixed test sentence. It did not pass an immediate-playback continuity
gate, and the effect of `exaggeration_factor` was not monotonic.

The result is promising enough for a small multi-text and native-listener
quality gate. It is not yet sufficient to replace Magpie in the live
speech-to-speech path or to start all long-form samples.

## Tested boundary

- GPU: NVIDIA RTX PRO 6000 Blackwell Server Edition, 97,887 MiB total
- Chatterbox image:
  `chatterbox-tts-multilingual:1.0.0@sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6`
- Chatterbox declared repository digest:
  `sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6`
- Chatterbox profile: `name=chatterbox-tts-multilingual`
- Chatterbox client: exactly `nvidia-riva-client==2.26.0`
- Chatterbox locale/voice: `es-ES` /
  `Chatterbox-Multilingual.es-ES.Male`
- Magpie image: `magpie-tts-multilingual:1.7.0`
- Magpie declared repository digest:
  `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d`
- Magpie profile: `name=magpie-tts-multilingual,batch_size=8`
- Magpie client: exactly `nvidia-riva-client==2.24.0`
- Magpie locale/voice: `es-US` /
  `Magpie-Multilingual.ES-US.Isabela`

The report labels image metadata as declared and unverified. Before the run,
separate Docker inspection confirmed the expected image tags and repository
digests. No Docker socket or container environment is exposed to either
benchmark script.

Both arms recorded the same neutral fixture ID, version, character count, and
UTF-8 byte count under the shared canonical metric schema. Each excluded one
warm-up from measured aggregates and WAV capture. Chatterbox retained only its
warm-up status. Chatterbox ran three repeats for each factor; Magpie ran three
repeats. Generated WAV and raw JSON artifacts are ignored by Git and protected
with directory mode `0700` and file mode `0600`.

This is a system-level candidate comparison, not a controlled voice study.
Chatterbox exposes one built-in male `es-ES` voice, while the current Magpie
path uses a female `es-US` voice. The output-duration difference can still
predict queue pressure, but it cannot be attributed solely to model
architecture or exaggeration.

## Resource result

The existing ASR, NMT, and Magpie services used 32,300 MiB before Chatterbox.
With all four services healthy after the formal run, the GPU used 85,274 MiB
and had 11,977 MiB
free. This proves single-stream canary coexistence on this 96 GB-class GPU,
but the remaining margin is too small to infer safe production concurrency.

NVIDIA documents 52.5 GB of GPU memory for Chatterbox alone. It therefore
does not fit on a 48 GB RTX 6000 Ada. Replacing Magpie instead of running both
TTS services would leave more practical headroom.

## Matched latency and duration result

All values are medians of three measured warm trials from the final
digest-qualified run. Duration change is relative to Magpie's 4.923-second
median.

| TTS arm | Audio duration | Change vs. Magpie | First audio | RPC wall | RTF | Minimum continuity margin | Immediate underruns |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Magpie | 4.923 s | control | 0.145 s | 0.799 s | 0.164 | +0.118 s | 0/3 |
| Chatterbox 0.5 | 4.250 s | -13.7% | 0.904 s | 4.941 s | 1.224 | -0.751 s | 3/3 |
| Chatterbox 0.7 | 4.130 s | -16.1% | 0.900 s | 4.800 s | 1.162 | -0.521 s | 3/3 |
| Chatterbox 1.0 | 4.410 s | -10.4% | 0.900 s | 5.016 s | 1.208 | -0.673 s | 3/3 |
| Chatterbox 1.5 | 4.370 s | -11.2% | 0.930 s | 5.230 s | 1.264 | -0.894 s | 3/3 |

The Chatterbox duration result directly targets accumulated listener-queue
growth: this sentence was approximately 0.51–0.79 seconds shorter than the
Magpie result. The cost was approximately 0.75–0.78 seconds more time to first
audio and much slower generation.

Chatterbox's median nonempty-chunk p95 gaps were approximately 0.86–0.94
seconds. Starting playback at its first tiny audio chunk would exhaust the
available audio before later chunks arrived in every trial. A roughly
one-second TTS-side startup buffer would cover this short-fixture result, but
the end-to-end live policy should retain the previously proposed bounded
5–10-second audience queue and an explicit overload policy.

RTF here is full RPC wall time divided by generated duration. It is not by
itself a playback-continuity verdict because playback overlaps streaming.
Continuity margin is evaluated only through the final nonempty audio arrival;
the terminal RPC trailer is reported separately.

## Exaggeration-factor interpretation

The upstream Chatterbox project says higher exaggeration tends to speed up
speech. This NIM run did not show a monotonic relationship: `0.7` had the
shortest median, `1.0` was the longest, and `1.5` remained longer than the
default `0.5`. Earlier pilot sweeps also changed the ordering. Three trials of
one short sentence are not enough to rank the factors. Treat
`exaggeration_factor` as an emotion/prosody control with a possible duration
side effect, not as deterministic playback rate.

The upstream repository also exposes `cfg_weight`, but NVIDIA's Chatterbox NIM
`1.0.0` documentation lists only `exaggeration_factor` as a supported custom
configuration key. `cfg_weight` was not used in the formal result and must not
become an integration dependency without a documented NIM contract.

The separate July 27 follow-up proved that the pinned NIM accepted explicit
`cfg_weight` values `0.3`, `0.5`, and `0.7`, but its balanced 40-request matrix
did not demonstrate a stable duration effect or realtime promotion candidate.
See
[`CHATTERBOX_CFG_WEIGHT_RESULT_2026-07-27.md`](CHATTERBOX_CFG_WEIGHT_RESULT_2026-07-27.md).

## Quality and audience risk

Audio quality has not passed native Spanish review. Reviewers must check
pronunciation, intelligibility, unnatural speed changes, mixed-language
output, hallucinated words, and clipped or damaged phrase boundaries. NVIDIA
states that English quality is strongest and lists those categories as known
non-English risks.

For a live audience, Chatterbox trades a larger fixed phrase-start delay for
shorter generated phrases. The shorter duration could reduce delay growth as a
talk continues, while the slower first audio could make a synchronized joke
or audience reaction feel later. The correct gate therefore measures both:

- synchronized phrase or punchline delay from source event to audible target
  event; and
- queue depth and tail delay over time.

## Recommended next gate

1. Have native Spanish listeners compare the retained Magpie and Chatterbox
   WAVs without seeing model labels.
2. Repeat the direct comparison on a small fixed corpus containing short,
   medium, punctuation-heavy, and expressive sentences.
3. If quality passes and the duration advantage persists, add Chatterbox behind
   a default-off TTS backend selector using client `2.26.0`.
4. Run the existing one-minute staged speech-to-speech preflight with an
   approximately one-second TTS startup buffer inside the overall bounded
   5–10-second audience queue.
5. Promote to one five-minute sample before any full long-form matrix.

After capture, the default-off Chatterbox container was stopped gracefully.
The ASR, NMT, and Magpie containers remained healthy with zero restarts, and
GPU use returned to 32,300 MiB.

Official context:

- [NVIDIA Speech NIM 26.05 release notes](https://docs.nvidia.com/nim/speech/26.05.0/about/release-notes.html)
- [NVIDIA Chatterbox support matrix](https://docs.nvidia.com/nim/speech/26.05.0/reference/support-matrix/tts.html#chatterbox-tts-multilingual)
- [NVIDIA emotion-exaggeration customization](https://docs.nvidia.com/nim/speech/26.05.0/tts/customization.html#emotion-exaggeration)
- [Upstream Chatterbox tuning notes](https://github.com/resemble-ai/chatterbox#original-chatterbox-tips)
