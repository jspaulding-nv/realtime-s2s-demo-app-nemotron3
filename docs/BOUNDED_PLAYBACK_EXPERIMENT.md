# Bounded playback experiment

## Objective

Determine whether Nemotron 3 ASR plus adaptive Spanish playback can keep the
listener backlog in an acceptable 5-10 second operating range over complete
sermons without unacceptable speech-quality loss.

The experiment must also measure semantic English-to-Spanish delay at marked
phrases. Queue depth alone cannot answer the live-audience joke question.

The 10-second threshold is a soft/SLA ceiling. No condition in this experiment
authorizes dropping translated speech.

## Fixed test configuration

Use the repository's pinned images for every comparable run:

| Stage | Image | Host endpoint |
|---|---|---|
| ASR | `nvcr.io/nim/nvidia/nemotron-asr-streaming:1.2.0` | `localhost:50052` |
| NMT/S2S | `nvcr.io/nim/nvidia/riva-translate-1_6b:1.5.2` | `localhost:50051` |
| TTS | `nvcr.io/nim/nvidia/magpie-tts-multilingual:1.7.0` | `localhost:50053` |

Also hold these settings constant:

- Nemotron English streaming profile, `batch_size=32`;
- English `en-US` to Spanish `es-US`;
- `Magpie-Multilingual.ES-US.Isabela`;
- 16 kHz mono Int16 PCM;
- 300 ms input chunks at real-time pace;
- automatic ASR punctuation enabled; and
- final EOU window of 800 ms.

Do not infer that a lower EOU is better. Riva's latest analysis found that 800
ms with punctuation handling outperformed a 300 ms configuration without
punctuation handling for this workload.

Record the exact Git commit, image references or digests, GPU model, driver,
free/used GPU memory, CPU load, and network path with every run.

## Prepare a new VM

```bash
git clone https://github.com/jspaulding-nv/realtime-s2s-demo-app-nemotron3.git
cd realtime-s2s-demo-app-nemotron3

cp .env.example .env
mkdir -p .cache/nim
chmod 777 .cache/nim
```

Add an active NGC Personal Key to `.env`. Compose reads `.env` automatically,
but `docker login` does not, so export the file into the current shell before
authenticating:

```bash
set -a
source .env
set +a

printf '%s' "$NGC_API_KEY" | \
  docker login nvcr.io --username '$oauthtoken' --password-stdin
```

If login returns `unauthorized`, replace or reactivate the NGC Personal Key.
Do not paste the key into logs, Markdown, shell history, or Git.

Start and validate the pinned stack:

```bash
nvidia-ctk cdi list
docker compose config --images
docker compose pull
docker compose up -d
docker compose ps

curl --fail http://localhost:9002/v1/health/ready  # ASR
curl --fail http://localhost:9001/v1/health/ready  # NMT
curl --fail http://localhost:9003/v1/health/ready  # TTS
nvidia-smi
```

The selected profiles use approximately 26.4 GB of GPU memory in aggregate in
the observed setup. They fit the 96 GB RTX PRO 6000 Blackwell Server Edition;
a 48 GB RTX 6000 Ada should have nominal capacity, but capture peak memory
during a real stream before declaring that configuration supported.

Start the application after the services are healthy:

```bash
set -a
source .env
set +a
./start.sh
```

Open `http://localhost:5173/#/test` for the browser test dashboard.

## Pre-run software validation

From the repository root, run the backend suite in its configured Python
environment:

```bash
python -m pytest backend/tests tests -q
```

Use Node.js 22 LTS for the frontend:

```bash
cd frontend
npm ci
npm test
npm run build
npm run lint
cd ..
```

At the documentation snapshot, 53 combined Python backend/analysis tests and
81 frontend tests passed, and the frontend production build and lint both
passed.

## Offline replay expectation

Before a new live run, the saved July 8 arrival traces were replayed through a
deterministic implementation of the browser policy:

| Saved trace | Simulated adaptive tail | Queue p95 | Peak queue | Playback time over 10 s |
|---|---:|---:|---:|---:|
| Spirit | 27.183 s | 38.31 s | 46.54 s | 80.0% |
| Blessed | 40.206 s | 44.95 s | 53.11 s | 77.3% |
| Beholding | 16.613 s | 19.48 s | 23.81 s | 45.6% |

Aggregate tail was 82.2% lower than fixed 1.00x replay, with no chunks
dropped. This is not a new live Riva or browser run. It predicts two things the
formal experiment must test: 1.10x can sharply reduce the final tail, but it is
unlikely to hold the listener queue below the 10-second soft ceiling without
upstream improvements. It also predicts long exposure to accelerated audio,
so the listening-quality evaluation is essential.

Reproduce the offline analysis when the ignored event CSVs are available:

```bash
python analyze_playback_policy.py \
  --input-dir test_results_nemotron \
  --json-output docs/results/nemotron3/playback_policy_analysis.json \
  --markdown-output docs/results/nemotron3/playback_policy_analysis.md
```

Run its focused regression tests with:

```bash
python -m pytest \
  tests/test_playback_simulation.py \
  tests/test_analyze_playback_policy.py -q
```

The generated report is
[`results/nemotron3/playback_policy_analysis.md`](results/nemotron3/playback_policy_analysis.md).

## Experimental matrix

Run at least these two conditions:

| Condition | Browser policy | Purpose |
|---|---|---|
| Control | Fixed 1.00x | Reproduce listener backlog without catch-up |
| Adaptive | 1.00x / 1.05x / 1.10x at 5/8 seconds | Measure backlog reduction and quality tradeoff |

`useAudioPlayback` defaults to fixed mode, while translated output in the live
panel opts into adaptive mode. The test dashboard exposes an **Adaptive
Spanish playback** checkbox before a run starts. Leave it selected for the
adaptive condition and clear it for the fixed 1.00x control. The CSV's
`playback_session_started` event records the selection, so both conditions can
use the same commit.

Run all three sermon files:

```text
test_audio/200108_SpiritandPresenceofGod.mp3
test_audio/Blessed_Self-Forgetfulness.mp3
test_audio/gospel_in_life_tk_1-john-part-2-mp3_Beholding_the_Love_of_God.mp3
```

Use at least three repeats per file and condition. Randomize or alternate the
condition order to reduce warm-cache and thermal bias. Before the first
long-form run, validate the service path with:

```bash
python batch_latency_test.py --preflight
```

The batch CLI is useful for service timing and historical fixed-rate playback
tails. The adaptive controller executes in the browser, so use the test
dashboard and its CSV export for the live adaptive condition unless an offline
simulator has been separately validated against browser scheduling.

## Run procedure

For each browser-dashboard run:

1. Confirm all three readiness endpoints and save `docker compose ps` and
   `nvidia-smi` output.
2. Record commit, condition, file, repeat number, UTC start time, and whether
   output monitoring is muted.
3. Select or clear **Adaptive Spanish playback**, load one sermon in `/#/test`,
   and start the test. Muting does not stop browser scheduling, so leave output
   muted for timing-only runs and use headphones when judging quality.
4. Let the complete file run at real-time pace. Do not manually stop during
   the drain phase.
5. Verify natural completion sends `end_input` and that the dashboard waits
   for both network quiet and an empty listener queue.
6. Export the CSV and save backend/container logs with the same run ID.
7. Note audible artifacts, pitch shift, discontinuities, skipped content,
   WebSocket reconnects, container restarts, and GPU memory peaks.
8. Confirm the CSV contains `playback_chunk_scheduled` and
   `playback_queue_sample` events plus the expected
   `adaptive_playback_enabled` value before accepting it.

Use a run ID such as:

```text
20260722_spirit_adaptive_repeat01_<git-short-sha>
```

Keep large CSV, audio, and plot artifacts outside Git or in an approved
artifact store. Commit compact, sanitized summaries only.

## Required measurements

For every run, report:

- first translated audio latency;
- service flush tail;
- output/input duration ratio and duration excess;
- listener playback tail;
- browser queue p50, p95, and maximum;
- seconds and percent over 5 seconds and over 10 seconds;
- number of over-limit excursions;
- time and audio duration scheduled at each playback rate;
- maximum continuous interval at 1.10x;
- container/runtime failures and reconnects; and
- GPU memory and utilization peaks.

For marked phrases or jokes, also report synchronized English-to-Spanish
semantic delay. Follow the procedure in
[Audience latency metrics](AUDIENCE_LATENCY_METRICS.md).

## Candidate acceptance gates

Agree final gates with Pellera before treating the experiment as a product
decision. A reasonable starting point is:

- browser queue p95 at or below 10 seconds for every sermon;
- no sustained upward queue trend in the final third of a sermon;
- time over 10 seconds below 1% of the observed run;
- no dropped or reordered speech;
- no container restart or WebSocket failure;
- marked-phrase delay acceptable for a live room; and
- native Spanish listeners accept intelligibility and naturalness at the
  selected speeds for long-form listening.

Treat failure to satisfy the 10-second queue gate as an SLA breach, not a
request to discard audio. If 1.10x cannot consume output as fast as it is
produced, proceed to server-side prosody or pitch-preserving time-scale work
and the staged backend design.

## Next-run checklist

- [ ] Active NGC key loaded with `source .env`; no secret in Git or logs
- [ ] Exact pinned images shown by `docker compose config --images`
- [ ] ASR, NMT, and TTS readiness probes pass
- [ ] GPU model, driver, memory, and utilization recorded
- [ ] Git commit and control/adaptive flag recorded
- [ ] Backend tests, frontend tests, build, and lint pass
- [ ] 800 ms EOU and automatic punctuation confirmed
- [ ] One-minute preflight produces translated audio
- [ ] Three sermons run under both control and adaptive conditions
- [ ] At least three repeats per file and condition
- [ ] Natural `end_input` and complete browser drain observed
- [ ] CSV contains playback schedule and queue sample fields
- [ ] Marked phrase/joke delay measured on a synchronized clock
- [ ] Native-listener quality notes captured at 1.05x and 1.10x
- [ ] Compact summary explicitly distinguishes historical and new runs

## Graceful shutdown

```bash
docker compose down
```

This preserves `.cache/nim`. Do not use `docker compose down --rmi all` unless
removing the downloaded model images is intentional.
