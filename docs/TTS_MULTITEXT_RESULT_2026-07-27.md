# TTS Multi-Text Gate Result — 2026-07-27

## Decision

Chatterbox TTS Multilingual `1.0.0` is **not promoted** to native-listener
review or staged speech-to-speech integration.

Two independent six-text runs confirmed a useful duration signal: Chatterbox
was approximately 16% shorter overall than Magpie and was at least 5% shorter
on five of six fixtures. Both runs nevertheless failed their predeclared
stream-continuity buffer cap. No blind-review bundle was created.

Keep Magpie as the active TTS backend. Do not move the continuity threshold
again based on these results. Chatterbox should advance only if its streaming
cadence improves or the product explicitly accepts a larger fixed TTS startup
buffer and validates the resulting source-to-listener delay end to end.

## Fixed boundary

- Corpus: `neutral-spanish-multitext-tts`, version 1
- Fixtures: six sanitized neutral Spanish texts
- Categories: micro, short, medium, punctuation-heavy, expressive/punchline
  like, and numbers/long clause
- Measured requests per run: 60
  - six fixtures;
  - two TTS arms; and
  - five measured repeats
- Discarded warm-ups per run: 12
- Chatterbox factor: documented default `exaggeration_factor=0.5`
- `cfg_weight`: omitted
- Playback scaling and audio post-processing: none
- Magpie: pinned `1.7.0`, `es-US`, client `2.24.0`
- Chatterbox: digest-qualified `1.0.0`, `es-ES`, client `2.26.0`

The systems use different voices, genders, and Spanish locales. Duration and
runtime results compare deployable system candidates; they do not isolate
model architecture.

## Protocol-version history

### Version 1

The first run required the two preselected listening repeats to fit a
1.00-second startup buffer.

- all 60 measured requests succeeded;
- overall median fixture-duration change: **-16.04%**;
- fixtures at least 5% shorter: **5/6**;
- Chatterbox median first audio: **0.902 seconds**; and
- selected maximum continuity deficit: **1.009 seconds**.

The run failed the 1.00-second cap by 0.009 seconds. It remains a failed run.
It was not reclassified after inspection.

### Version 2 confirmation

The confirmatory protocol predeclared a 1.25-second cap and made the coverage
stricter by applying it to all 30 measured Chatterbox trials. It required a
fresh synthesis run.

- all 60 measured requests succeeded;
- overall median fixture-duration change: **-16.60%**;
- fixtures at least 5% shorter: **5/6**;
- Chatterbox median first audio: **0.908 seconds**; and
- maximum continuity deficit across all trials: **1.505 seconds**.

One numbers/long-clause trial required approximately 1.505 seconds of startup
buffering. The run therefore failed the independent 1.25-second confirmation.
The preselected listening repeats reached 1.017 seconds, but the protocol
correctly considered all measured trials.

## Version-2 fixture result

Values below are medians of five measured trials. Duration change is
Chatterbox relative to Magpie.

| Fixture | Category | Magpie duration | Chatterbox duration | Change | Chatterbox first audio | Chatterbox maximum continuity deficit |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 001 | Micro response | 0.882 s | 1.090 s | +23.54% | 0.905 s | 0.490 s |
| 002 | Short statement | 2.043 s | 1.890 s | -7.50% | 0.882 s | 0.628 s |
| 003 | Medium neutral | 5.016 s | 4.210 s | -16.06% | 0.929 s | 0.895 s |
| 004 | Punctuation | 4.690 s | 3.570 s | -23.89% | 0.945 s | 0.863 s |
| 005 | Expressive/punchline-like | 3.158 s | 2.450 s | -22.42% | 0.880 s | 0.804 s |
| 006 | Numbers/long clause | 5.805 s | 4.810 s | -17.14% | 0.903 s | 1.505 s |

The micro fixture is important: Chatterbox was 23.5% longer and reached first
audio at 0.905 seconds, versus 0.129 seconds for Magpie. Chatterbox therefore
does not provide a universal advantage for short reactions.

The expressive fixture was 22.4% shorter, but Chatterbox's first audio was
0.880 seconds versus 0.131 seconds for Magpie. Covering its observed stream
gaps also adds buffering after first audio. This trades lower accumulated
duration for a larger fixed phrase-start delay.

## Live-audience interpretation

The duration reduction could help prevent listener backlog from growing as a
long talk continues. It does not solve the synchronized-joke problem by
itself.

For the observed long-clause outlier, Chatterbox needed approximately:

```text
0.93 seconds to first audio
+ 1.51 seconds of continuity protection
= about 2.44 seconds before safe TTS playback
```

ASR endpointing, NMT, network publication, and the intentional audience queue
would add to that delay. A 5-10-second bounded audience queue can absorb the
TTS deficit, but doing so does not keep translated listeners synchronized with
immediate room laughter. The later end-to-end gate must measure source-event
to audible-target timing, not only generated duration or file-tail lag.

Magpie remains the lower-fixed-latency choice in this direct comparison: its
first audio was approximately 0.13-0.14 seconds and it did not show the same
immediate-playback continuity deficit.

## Runtime attestation

Before and after each run:

- ASR, NMT, Magpie, and Chatterbox were healthy;
- readiness endpoints returned HTTP 200;
- restart counts remained zero;
- Docker reported no OOM;
- the pinned Chatterbox image ID was
  `sha256:24793d068997f43cc0865453ba0fa1c83179f8c6c97cf003c429a86771569bdc`;
  and
- the Chatterbox repository digest was
  `sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6`.

The version-2 post-run GPU state was 85,280 MiB used and 11,971 MiB free on the
97,887 MiB GPU.

Private ignored evidence:

| Run | Report SHA-256 |
| --- | --- |
| Protocol v1 | `dadccf1065107b5d96a917e5fa55bbc78b8a10e841a609298271833f51ad07e8` |
| Protocol v2 | `7303248ebf880c066dbddf1d98f1f2b627c7c02122eef1bfeb8415eef56c65cd` |

Each run directory also contains a mode-`0600` runtime attestation bound to
the corresponding report hash. Raw WAVs, request bindings, and reports remain
under ignored `experiment_results/`.

## Shutdown result

The recreated Chatterbox container correctly inherited a 120-second stop
timeout, but it still did not exit within that window. Docker stopped it with
exit `137` and reported `OOMKilled=false`. ASR, NMT, and Magpie remained
healthy, and GPU use returned to 32,300 MiB with 64,950 MiB free.

The Compose definition now grants five minutes to newly recreated Chatterbox
containers. That longer shutdown window is not yet live-verified.

## Completed next gate

The active Magpie path subsequently passed a fresh one-minute headless
speech-to-speech control with:

- Nemotron 3 ASR;
- 800 ms EOU plus punctuation splitting;
- parallel NMT/TTS scheduling;
- the bounded 5-10-second listener queue and explicit overload policy; and
- unchanged Magpie synthesis for the first control.

The queue p95 was 4.181 seconds, peak was 5.460 seconds, time above 10 seconds
was zero, and every translated frame was preserved. See the
[Magpie headless S2S control](MAGPIE_HEADLESS_CONTROL_RESULT_2026-07-27.md).

The next gate is a browser-independent, hash-bound private source/translated
PCM capture with bilingual source/target landmark review. That is required to
measure scheduled-digital punchline delay directly; queue depth alone is not
an audible-target measurement.
