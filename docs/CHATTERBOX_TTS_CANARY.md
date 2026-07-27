# Chatterbox TTS Multilingual NIM Canary

## Purpose

This is a default-off comparison arm for determining whether Chatterbox TTS
Multilingual can reduce Spanish synthesized-audio duration enough to improve
listener backlog without unacceptable latency or quality regressions.

It does **not** replace Magpie in the active speech-to-speech path. The existing
NMT container still calls `tts:50053`, which is the pinned Magpie service. The
Chatterbox container has no dependency edge from NMT and is reachable only on
the alternate host ports documented below.

Chatterbox is worth measuring because NVIDIA NIM exposes a per-request
`exaggeration_factor`. It is not a direct playback-rate control:

- NVIDIA documents it as an emotional-prosody control with a range of
  `0.25`-`2.0` and a default of `0.5`.
- The upstream Chatterbox project says larger exaggeration values *tend* to
  make speech faster. That is an empirical tendency, not a duration guarantee.
- Upstream also exposes `cfg_weight`, but Chatterbox NIM `1.0.0` does not
  document that key as supported. It is excluded from the formal first canary
  and tested only by the separate default-off compatibility/effect probe.

Official references:

- [NVIDIA Speech NIM 26.05 release notes](https://docs.nvidia.com/nim/speech/26.05.0/about/release-notes.html)
- [NVIDIA TTS NIM support matrix](https://docs.nvidia.com/nim/speech/26.05.0/reference/support-matrix/tts.html#chatterbox-tts-multilingual)
- [NVIDIA TTS emotion-exaggeration customization](https://docs.nvidia.com/nim/speech/26.05.0/tts/customization.html#emotion-exaggeration)
- [NVIDIA TTS performance reference](https://docs.nvidia.com/nim/speech/26.05.0/reference/performances/tts/performance.html)
- [Upstream Chatterbox tuning notes](https://github.com/resemble-ai/chatterbox#original-chatterbox-tips)

## Pinned boundary

| Component | Pin or endpoint |
| --- | --- |
| Container | `nvcr.io/nim/nvidia/chatterbox-tts-multilingual:1.0.0@sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6` |
| Pulled repository digest on the first test VM | `sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6` |
| Profile | `name=chatterbox-tts-multilingual` |
| Host HTTP | `localhost:9004` |
| Host gRPC | `localhost:50054` |
| Canary client | `nvidia-riva-client==2.26.0` |
| Initial TTS locale | Runtime-discovered Spanish locale; expected `es-ES` |
| Initial built-in voice | Runtime-discovered voice; expected `Chatterbox-Multilingual.es-ES.Male` |
| Formal initial factors | `0.5`, `0.7`, `1.0`, `1.5` |

The NMT target remains `es-US`. If Chatterbox exposes only `es-ES`, later
integration must map the validated `es-US` translated text to an independent
`es-ES` TTS request. Do not change the NMT or target-text-validation locale to
make the TTS call work.

## GPU and disk feasibility

NVIDIA documents 52.5 GB of GPU memory and 6.4 GB of CPU memory for the single
Chatterbox profile. The tested RTX PRO 6000 Blackwell Server Edition has
97,887 MiB of GPU memory and is listed in the supported Blackwell RTX 60xx
family. Before Chatterbox startup, the existing ASR, NMT, and Magpie services
used 32,300 MiB, leaving 64,950 MiB free. A simple mixed-unit projection was
therefore roughly 85,000 MiB used. The final immutable-image run observed
85,274 MiB used and 11,977 MiB free with all four services healthy.

That is enough for a controlled single-stream canary, but the margin is not
large enough to assume safe production concurrency. Record live peak memory,
container restarts, and OOM events. The documented 52.5 GB profile does not fit
on a 48 GB RTX 6000 Ada by itself.

The pinned image occupies about 29.1 GB after extraction on the first VM. Check
host disk before the pull and while the model cache is populated.

## Safe setup

Compose reads `.env` automatically. Docker login does not, so export it first:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin
```

Never run an unredacted `docker compose config` while a real key is in `.env`;
it expands the key into terminal or automation logs. These validation forms do
not print service environment values:

```bash
docker compose --profile chatterbox-canary config -q
docker compose --profile chatterbox-canary config --services
docker compose --profile chatterbox-canary config --images
```

Pull the exact digest-qualified release:

```bash
docker pull \
  nvcr.io/nim/nvidia/chatterbox-tts-multilingual:1.0.0@sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6

docker image inspect \
  nvcr.io/nim/nvidia/chatterbox-tts-multilingual:1.0.0@sha256:fec12c0c6d0af511d66999daa3a824d176fb3bbc11a86ff071330726c4952cc6 \
  --format '{{json .RepoDigests}}'
```

Install the newer client into an isolated target directory. This leaves the
main application on its proven `2.24.0` client:

```bash
python3 -m pip install \
  --disable-pip-version-check \
  --target .python-packages-chatterbox \
  -r requirements-chatterbox-canary.txt

PYTHONPATH=.python-packages-chatterbox python3 -c \
  'import importlib.metadata; print(importlib.metadata.version("nvidia-riva-client"))'
```

The result must be `2.26.0`. That release adds the
`custom_configuration` protobuf map and changes streaming TTS to the
bidirectional client API. The repository's `2.24.0` client cannot send the
Chatterbox factor.

## Start and verify the isolated service

To reuse a cache outside this checkout, override `LOCAL_NIM_CACHE` only for the
Compose command:

```bash
LOCAL_NIM_CACHE=/absolute/path/to/shared/nim-cache \
  docker compose --profile chatterbox-canary up -d chatterbox-tts
```

Otherwise the default is this checkout's `.cache/nim`.

First startup can take 30 minutes or more while the RMIR is downloaded,
materialized, and loaded. Verify the isolated container and retain the current
service checks:

```bash
docker ps --filter name=s2s-eval-chatterbox-tts
docker inspect --format \
  '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}' \
  s2s-eval-chatterbox-tts
curl --fail http://localhost:9004/v1/health/ready
nvidia-smi

curl --fail --silent \
  http://localhost:9004/v1/audio/list_voices | python3 -m json.tool
```

Treat the live voice inventory as authoritative. NVIDIA's current pages are
inconsistent: the support matrix shows `es-ES`, while one deployment example
shows `es-US`.

## Run the repeated sweep

The canary uses a short neutral Spanish fixture unless `--text` or
`--text-file` is supplied:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/.python-packages-chatterbox" \
  python3 -S chatterbox_tts_canary.py
```

Explicit form:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/.python-packages-chatterbox" \
  python3 -S chatterbox_tts_canary.py \
    --grpc-uri localhost:50054 \
    --http-base-url http://localhost:9004 \
    --tts-locale es-ES \
    --repeats-per-factor 3 \
    --rpc-timeout-seconds 60 \
    --max-audio-duration-seconds 60 \
    --exaggeration-factors 0.5 0.7 1.0 1.5
```

The runner requires exactly `nvidia-riva-client==2.26.0` and verifies that
`riva.client` resolves beneath `.python-packages-chatterbox`. It performs one
warm-up whose status—but not timing/audio metrics or WAV—is retained, then
rotates the starting factor across three measured repeats. Compose launches
the default image by immutable digest. The JSON still labels image/profile
metadata as declared rather than runtime-attested, so verify the live container
separately with `docker inspect` and `docker image inspect`.

The ignored timestamped artifact directory contains:

- one WAV per successful factor and repeat, normally 12 files;
- `chatterbox-tts-canary.json` with time to first audio, full RPC wall time,
  synthesized duration, real-time factor, nonempty inter-chunk cadence,
  continuity margin through the last audio arrival, underrun risk, and the
  separate terminal RPC-completion tail;
- a neutral fixture ID/version plus input character and UTF-8 byte counts,
  never the text, its path, or a candidate-matchable text fingerprint;
- safe error types/statuses without service messages.

Each request has an active cancellation deadline and an incremental PCM-size
limit. The runner creates a new symlink-safe artifact tree at mode `0700`;
WAV and JSON files are mode `0600`, published atomically, and never
overwritten. A pre-existing artifact directory is rejected.

The WAV files contain synthesized speech. Review them before any sharing and
do not force-add the artifact directory to Git.

## Run the matched Magpie control

Use the same neutral text with the repository-local `2.24.0` client and the
existing Magpie service:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/.python-packages:$PWD" \
  python3 -S magpie_tts_control.py
```

The control performs one discarded warm-up and three measured repetitions.
It applies the same private-artifact, deadline, output-bound, cadence, and
continuity rules. Do not put both Riva client releases in one interpreter.

## Experimental `cfg_weight` probe

The Chatterbox container is pinned at `1.0.0`. The Speech NIM customization
documentation for release `26.05.0` documents `exaggeration_factor`, but not
`cfg_weight`. The standalone probe therefore distinguishes three questions:

1. Does the pinned NIM accept the key under healthy no-key controls?
2. If accepted, does a balanced repeated matrix demonstrate a stable duration
   effect?
3. If an effect is demonstrated, does any cell satisfy the predeclared
   duration, first-audio, RTF, and continuity screen?

Run the bracketed contract smoke first:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/.python-packages-chatterbox:$PWD" \
  python3 -S chatterbox_cfg_weight_probe.py
```

The runner discards one no-key warm-up, then sends:

```text
no cfg_weight -> 0.3 -> 0.5 -> 0.7 -> no cfg_weight
```

The controls omit the map key entirely; they do not send a null or empty
value. `accepted` proves only that all weighted requests returned valid PCM
between healthy controls. It does not prove the model applied the value.
Deterministic `INVALID_ARGUMENT` or `UNIMPLEMENTED` responses under healthy
controls are classified as `rejected`; mixed failures are `inconclusive`.

Only after an accepted smoke, run the balanced effect matrix:

```bash
PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/.python-packages-chatterbox:$PWD" \
  python3 -S chatterbox_cfg_weight_probe.py \
    --balanced-matrix \
    --repeats-per-cell 5
```

This rotates 40 measured requests across exaggeration factors `0.5` and `0.7`
and `cfg_weight` omitted, `0.3`, `0.5`, and `0.7`. A stable effect requires the
`0.3`-versus-`0.7` direction to agree in at least four of five repeats at both
exaggeration levels, with the same direction and at least a 5% or 0.25-second
median difference. Acceptance without that evidence is reported as
`effect_not_demonstrated`, never as proof that the key was ignored.
The smoke exits zero only for transport acceptance. Balanced mode exits zero
only when it demonstrates both an effect and a realtime candidate; exit `3`
means the balanced run completed under accepted transport but did not meet
that promotion gate. Consumers should retain and inspect the JSON report.

The ignored artifact directory uses the same private `0700`/`0600`, atomic
publication, safe-error, deadline, PCM-limit, and no-text/no-path rules as the
first canary. WAV files still require native review before sharing.

The completed July 27 run accepted the field but did not demonstrate a stable
effect or realtime candidate. See the
[`cfg_weight` result](CHATTERBOX_CFG_WEIGHT_RESULT_2026-07-27.md).

## First-gate interpretation

This is a duration, responsiveness, and quality comparison—not an automatic
latency improvement.

Runtime gate:

- all 12 measured Chatterbox requests and all three Magpie controls succeed;
- container remains healthy with zero restarts and no OOM;
- observed memory leaves operating headroom;
- streaming returns real audio chunks before RPC completion;
- continuity is evaluated independently of the final RPC trailer.

Duration gate:

- compare factor aggregates with `0.5`, not individual stochastic syntheses;
- compare Chatterbox with Magpie using identical translated text.
- promote to multiple fixed texts only if the repeated short sweep shows a
  useful duration signal.

Quality gate:

- native Spanish reviewers check pronunciation, intelligibility, unnatural
  speed changes, mixed-language output, hallucinated words, clipped endings,
  and boundary artifacts;
- specifically review Castilian-Spanish pronunciation, which NVIDIA lists as
  a known limitation;
- a shorter file is not a pass if meaning or naturalness degrades.

Audience gate:

- Chatterbox's published first-audio numbers are several hundred milliseconds,
  slower than Magpie's published numbers;
- shorter synthesized duration could reduce accumulated queue growth, but it
  does not remove ASR endpointing, NMT, or parent-burst delay;
- the live design still needs a bounded playback queue, initially targeting
  approximately 5-10 seconds, plus an explicit overload policy;
- validate synchronized phrase or punchline timing, not only file-tail lag.

## Promotion order

If the isolated sweep shows a useful duration signal:

1. Run the matched multi-text mechanical gate.
2. Create and complete the blinded native-Spanish review only if the
   mechanical gate passes.
3. Upgrade the main Riva client to `2.26.0` only after a Magpie regression
   test.
4. Add a separate TTS request locale/voice and validated
   `custom_configuration` to `DirectTTSClient`; keep NMT output at `es-US`.
5. Run the one-minute staged preflight.
6. Promote to the five-minute stress sample.
7. Run all three long-form samples only after runtime, duration, and native
   listener-quality gates pass.

The completed multi-text run stopped at step 1 because one Chatterbox
long-clause trial required 1.505 seconds of startup buffering, above the
predeclared 1.25-second cap. No reviewer bundle was created, and integration
steps remain blocked.

`cfg_weight` remains outside the integration contract. The pinned NIM accepted
the tested values, but the balanced run did not demonstrate a stable effect.
Do not depend on it unless NVIDIA documents the Speech NIM contract and a
future controlled response test proves a useful, repeatable result.

See the
[July 26 live Chatterbox result](CHATTERBOX_TTS_RESULT_2026-07-26.md) for the
first repeated comparison and the resulting promotion decision.

## Stop only the comparison arm

```bash
docker compose --profile chatterbox-canary stop chatterbox-tts
docker compose --profile chatterbox-canary rm -f chatterbox-tts
```

Compose grants newly created Chatterbox containers five minutes for a graceful
stop. A July 27 multi-text run showed that two minutes was still insufficient:
Docker stopped the container with exit `137`, while reporting
`OOMKilled=false`. Recreate any container that predates the five-minute setting
before relying on that timeout. These commands leave the separate ASR, NMT,
and Magpie deployment unchanged.
