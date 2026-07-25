# Semantic gate browser preflight — July 25, 2026

## Claim boundary

This is a privacy-safe mechanical preflight for the semantic source-event
latency gate. It used the tracked neutral `test_audio/preflight.wav` 60-second
fixture through the real Test Dashboard path with adaptive playback enabled.
It proves that the reviewed capture machinery can produce internally
consistent protocol-v1 evidence. Follow the
[dashboard capture procedure](SEMANTIC_EVENT_LATENCY_GATE.md#capture-and-marker-sidecar)
to reproduce the browser path.

It does not contain a two-reviewer semantic marker, identify a target-language
landmark, observe rendered device output, or establish an audience-latency
PASS/FAIL.

## Runtime

- GPU: NVIDIA RTX PRO 6000 Blackwell Server Edition
- Pipeline: direct staged ASR -> NMT -> TTS
- Input: 16 kHz mono Int16, 300 ms frames, absolute source-end pacing
- EOU: 800 ms
- ASR word timing: enabled
- TTS publication: schema-3 incremental, 100 ms frames
- Post-NMT TTS splitting: disabled
- Playback: adaptive no-drop 1.00x/1.05x/1.10x policy

Pinned model evidence:

| Stage | Image | Digest |
| --- | --- | --- |
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `sha256:0f01867023d93402fefab2859bdc363cf6f002e37083e5c0ca5d632df30e1850` |
| NMT | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `sha256:3789b08b72c8dfbb09d1144e2bfd1f13c95911c2f997c9e11d81afb5aa90c9fb` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `sha256:6eacebdc45b35199bf2782c1f0c27d102aef5361ae3ea874e27bf3b8f6d5333d` |

## Initial browser finding

The first completed dashboard CSV was correctly rejected by the new analyzer:
170 projected frame intervals appeared to overlap, with a maximum of 16.0 ms.
The underlying AudioContext evidence had zero overlaps, 489 exactly contiguous
boundaries, and six real idle gaps.

The defect was in the projection, not the Web Audio schedule. Each output frame
independently estimated:

```text
performance.now()
  + (scheduledStartContext - AudioContext.currentTime) * 1000
```

`AudioContext.currentTime` is render-quantized, so the independently sampled
clock offset moved by -16.0 to +15.1 ms between adjacent frames. Contiguous
audio consequently appeared to overlap or acquire a gap.

The fix retains the exact AudioContext schedule and projects an ordered cursor:

```text
projectedStart[i] =
  max(schedulePerformance[i], projectedEnd[i-1])
```

The analyzer independently verifies both that recurrence and:

```text
contextStart[i] =
  max(audioContextAtSchedule[i], contextEnd[i-1])
```

Both checks continue across parent boundaries and reset only with a new
playback session.

Review then added a cross-domain invariant so independently valid recurrences
cannot drift apart undetected. The analyzer limits the per-frame difference
between AudioContext wait and projected client-clock wait to 25 ms, limits the
capture-wide client/AudioContext offset span to 50 ms, and widens formal
semantic decision bounds by 25 ms in both directions.

## Source-range finding

The same trace contained two ordered same-end suffix envelopes: one 240 ms
overlap and one 480 ms overlap. All frame, receipt, completion, source-boundary,
and PCM evidence reconciled. This shape is explainable when punctuation
segmentation consumes earlier text plus part of a later Nemotron final while
the residual retains that final's narrower word-time envelope.

The analyzer now accepts only strictly advancing starts with exactly equal
ends. It still rejects crossing, backward, different-end, and all other
containing overlaps. A human marker inside two distinct ranges remains
ambiguous and rejects the complete gate.

## Matched post-fix result

The second completed Test Dashboard capture passed every mechanical validator:

| Metric | Result |
| --- | ---: |
| Capture CSV SHA-256 | `6e00dbdd760d89783fdb39928ee10c41ef6a1e146f62e4a715062e3eae49d860` |
| Input PCM SHA-256 | `7ddfecc4dc145aa678711a55b2b5371793386bc2c9ffe2eab61e174db4ed0541` |
| Input PCM samples / duration | 960,000 / 60.000 s |
| Input chunks | 200 |
| Input emission-minus-deadline | 0.0-55.1 ms |
| Stream generations | 1 |
| Translated parents | 23 |
| Output frames | 520 |
| Maximum absolute playback clock-link residual | 17.800 ms |
| Playback clock-offset span | 28.300 ms |
| Projected-start source-end lag | 1.642-8.254 s |
| Projected-end source-end lag | 1.742-8.312 s |
| Projected tail after input end | 6.563 s |

The CSV and raw browser material remain under the ignored
`experiment_results/` tree and must not be committed.

## Next gate

1. Run the selected long-form dashboard capture with the pre-registered 25 ms
   link-residual and 50 ms capture-span limits unchanged. The latter is
   provisional because it was calibrated on 60 seconds; rejection is an
   invalid-evidence result, not a semantic failure.
2. Freeze the accepted long-form dashboard CSV and its SHA-256.
3. Have at least two people independently mark the same semantic source event
   at a zero-based 16 kHz sample.
4. Reconcile one anonymous `event-NNN` marker without adding transcript, names,
   paths, or free text to the sidecar.
5. Run the analyzer twice against the same frozen CSV and sidecar: once with
   `--max-latency-seconds 5` for the objective and once with
   `--max-latency-seconds 10` for the soft ceiling.
6. If the parent envelope is still too late, evaluate the bounded 5-10 second
   listener queue and the documented 1.05x/1.10x quality tradeoff.

Only a later common-clock rendered or physical two-channel capture can prove
actual audibility.
