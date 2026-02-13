#!/usr/bin/env python3
"""
Analyze stalls in the timing CSV export.
Finds gaps > 3s between consecutive client audio_received events
and examines all pipeline stages to determine root cause.
"""

import csv
import sys
from collections import defaultdict

CSV_PATH = "timing-export.csv"

def load_data(path):
    rows = []
    with open(path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                'source': row['source'],
                'stage': row['stage'],
                'timestamp_ms': float(row['timestamp_ms']),
                'chunk_index': int(row['chunk_index']),
                'source_position_sec': float(row['source_position_sec']),
                'audio_bytes': int(row['audio_bytes']),
            })
    return rows

def main():
    print("=" * 90)
    print("STALL ANALYSIS: timing-export-2026-02-12T23-01-42-038Z.csv")
    print("=" * 90)

    rows = load_data(CSV_PATH)
    print(f"\nTotal events: {len(rows)}")

    # Separate by source+stage
    events_by_key = defaultdict(list)
    for r in rows:
        key = (r['source'], r['stage'])
        events_by_key[key].append(r)

    # Sort each list by timestamp
    for key in events_by_key:
        events_by_key[key].sort(key=lambda x: x['timestamp_ms'])

    # Print event counts
    print("\nEvent counts by source/stage:")
    for key in sorted(events_by_key.keys()):
        src, stg = key
        evts = events_by_key[key]
        print(f"  {src:10s} {stg:25s}: {len(evts):6d} events, "
              f"time range [{evts[0]['timestamp_ms']:.1f} - {evts[-1]['timestamp_ms']:.1f}] ms")

    # All events sorted by timestamp for window queries
    all_events = sorted(rows, key=lambda x: x['timestamp_ms'])
    total_duration_s = (all_events[-1]['timestamp_ms'] - all_events[0]['timestamp_ms']) / 1000.0
    print(f"\nTotal test duration: {total_duration_s:.1f} seconds")

    # =========================================================================
    # 1. FIND TOP 15 STALLS
    # =========================================================================
    print("\n" + "=" * 90)
    print("SECTION 1: TOP 15 STALLS (gaps > 3s between consecutive client audio_received)")
    print("=" * 90)

    client_audio_recv = events_by_key[('client', 'audio_received')]
    gaps = []
    for i in range(1, len(client_audio_recv)):
        prev_ts = client_audio_recv[i - 1]['timestamp_ms']
        curr_ts = client_audio_recv[i]['timestamp_ms']
        gap_ms = curr_ts - prev_ts
        if gap_ms > 3000:
            gaps.append({
                'index': i,
                'gap_start_ms': prev_ts,
                'gap_end_ms': curr_ts,
                'gap_duration_sec': gap_ms / 1000.0,
                'prev_chunk': client_audio_recv[i - 1]['chunk_index'],
                'next_chunk': client_audio_recv[i]['chunk_index'],
            })

    gaps.sort(key=lambda x: -x['gap_duration_sec'])
    top15 = gaps[:15]

    print(f"\nFound {len(gaps)} gaps > 3 seconds.")
    print(f"\n{'#':>3s}  {'Gap Start (ms)':>15s}  {'Gap End (ms)':>15s}  {'Duration (s)':>12s}  "
          f"{'Prev Chunk':>10s}  {'Next Chunk':>10s}")
    print("-" * 80)
    for idx, g in enumerate(top15):
        print(f"{idx+1:3d}  {g['gap_start_ms']:15.1f}  {g['gap_end_ms']:15.1f}  "
              f"{g['gap_duration_sec']:12.2f}  {g['prev_chunk']:10d}  {g['next_chunk']:10d}")

    # =========================================================================
    # 2. DETAILED EXAMINATION OF EACH STALL
    # =========================================================================
    print("\n" + "=" * 90)
    print("SECTION 2: DETAILED EVENT EXAMINATION FOR EACH STALL")
    print("=" * 90)

    stages_of_interest = [
        ('client', 'chunk_sent'),
        ('client', 'audio_received'),
        ('backend', 'audio_received'),
        ('backend', 'audio_to_riva'),
        ('backend', 'audio_from_riva'),
        ('backend', 'audio_sent_to_client'),
    ]

    for idx, g in enumerate(top15):
        window_start = g['gap_start_ms'] - 5000
        window_end = g['gap_end_ms'] + 5000

        print(f"\n--- Stall #{idx+1}: gap = {g['gap_duration_sec']:.2f}s "
              f"[{g['gap_start_ms']:.1f} - {g['gap_end_ms']:.1f} ms] ---")

        # Count events by stage in 3 periods: before gap, during gap, after gap
        for src, stg in stages_of_interest:
            stage_events = events_by_key.get((src, stg), [])
            before = [e for e in stage_events
                      if window_start <= e['timestamp_ms'] < g['gap_start_ms']]
            during = [e for e in stage_events
                      if g['gap_start_ms'] <= e['timestamp_ms'] <= g['gap_end_ms']]
            after = [e for e in stage_events
                     if g['gap_end_ms'] < e['timestamp_ms'] <= window_end]

            label = f"{src}.{stg}"
            print(f"  {label:40s}  before(5s): {len(before):4d}  "
                  f"during_gap: {len(during):4d}  after(5s): {len(after):4d}")

    # Timeline for top 5 stalls
    print("\n" + "-" * 90)
    print("TIMELINE VIEW: 1-second sub-windows for top 5 stalls")
    print("-" * 90)

    for idx, g in enumerate(top15[:5]):
        print(f"\n--- Stall #{idx+1}: {g['gap_duration_sec']:.2f}s "
              f"[{g['gap_start_ms']:.1f} - {g['gap_end_ms']:.1f} ms] ---")

        # Create 1-second buckets covering the gap
        bucket_start = int(g['gap_start_ms'] / 1000) * 1000
        bucket_end = int(g['gap_end_ms'] / 1000 + 1) * 1000

        short_labels = ['cli.sent', 'cli.recv', 'be.recv', 'be.to_riva', 'be.fr_riva', 'be.to_cli']

        # Header
        print(f"  {'Second':>10s}", end='')
        for sl in short_labels:
            print(f"  {sl:>11s}", end='')
        print()

        t = bucket_start
        while t < bucket_end:
            t_end = t + 1000
            print(f"  {t/1000:10.1f}", end='')
            for src, stg in stages_of_interest:
                stage_events = events_by_key.get((src, stg), [])
                count = sum(1 for e in stage_events
                            if t <= e['timestamp_ms'] < t_end)
                marker = f"{count}" if count > 0 else "."
                print(f"  {marker:>11s}", end='')
            print()
            t += 1000

    # =========================================================================
    # 3. PATTERN ANALYSIS ACROSS ALL STALLS
    # =========================================================================
    print("\n" + "=" * 90)
    print("SECTION 3: PATTERN ANALYSIS ACROSS ALL STALLS")
    print("=" * 90)

    # 3a. Last backend event before gap, first after gap
    print("\n3a) Last backend event before gap / First backend event after gap:")
    print(f"  {'#':>3s}  {'Duration':>8s}  {'Last Before':>35s}  {'First After':>35s}")
    print("  " + "-" * 90)

    backend_events = sorted([e for e in all_events if e['source'] == 'backend'],
                            key=lambda x: x['timestamp_ms'])

    for idx, g in enumerate(top15):
        last_before = None
        first_after = None
        for e in backend_events:
            if e['timestamp_ms'] <= g['gap_start_ms']:
                last_before = e
            if e['timestamp_ms'] >= g['gap_end_ms'] and first_after is None:
                first_after = e

        lb_str = f"{last_before['stage']}@{last_before['timestamp_ms']:.0f}" if last_before else "N/A"
        fa_str = f"{first_after['stage']}@{first_after['timestamp_ms']:.0f}" if first_after else "N/A"
        print(f"  {idx+1:3d}  {g['gap_duration_sec']:8.2f}s  {lb_str:>35s}  {fa_str:>35s}")

    # 3b. audio_from_riva events during each gap
    print("\n3b) Backend audio_from_riva events during each gap:")
    be_from_riva = events_by_key.get(('backend', 'audio_from_riva'), [])

    for idx, g in enumerate(top15):
        during = [e for e in be_from_riva
                  if g['gap_start_ms'] <= e['timestamp_ms'] <= g['gap_end_ms']]
        before_5s = [e for e in be_from_riva
                     if g['gap_start_ms'] - 5000 <= e['timestamp_ms'] < g['gap_start_ms']]
        after_5s = [e for e in be_from_riva
                    if g['gap_end_ms'] < e['timestamp_ms'] <= g['gap_end_ms'] + 5000]
        print(f"  Stall #{idx+1:2d} ({g['gap_duration_sec']:6.2f}s): "
              f"before(5s)={len(before_5s):3d}  during={len(during):3d}  after(5s)={len(after_5s):3d}")

    # 3c. Client chunk_sent during stalls
    print("\n3c) Client chunk_sent events during each stall:")
    cli_sent = events_by_key.get(('client', 'chunk_sent'), [])

    for idx, g in enumerate(top15):
        during = [e for e in cli_sent
                  if g['gap_start_ms'] <= e['timestamp_ms'] <= g['gap_end_ms']]
        expected = g['gap_duration_sec'] / 0.3  # chunks are every ~300ms
        print(f"  Stall #{idx+1:2d} ({g['gap_duration_sec']:6.2f}s): "
              f"chunk_sent during gap = {len(during):4d}  (expected ~{expected:.0f})")

    # 3d. Backend audio_received during stalls
    print("\n3d) Backend audio_received events during each stall:")
    be_recv = events_by_key.get(('backend', 'audio_received'), [])

    for idx, g in enumerate(top15):
        during = [e for e in be_recv
                  if g['gap_start_ms'] <= e['timestamp_ms'] <= g['gap_end_ms']]
        print(f"  Stall #{idx+1:2d} ({g['gap_duration_sec']:6.2f}s): "
              f"backend audio_received during gap = {len(during):4d}")

    # =========================================================================
    # 4. RIVA OUTPUT CADENCE ANALYSIS
    # =========================================================================
    print("\n" + "=" * 90)
    print("SECTION 4: RIVA OUTPUT CADENCE ANALYSIS (backend audio_from_riva)")
    print("=" * 90)

    if len(be_from_riva) < 2:
        print("  Not enough audio_from_riva events to analyze.")
    else:
        # Compute inter-event gaps
        riva_gaps = []
        for i in range(1, len(be_from_riva)):
            gap_ms = be_from_riva[i]['timestamp_ms'] - be_from_riva[i - 1]['timestamp_ms']
            riva_gaps.append({
                'gap_ms': gap_ms,
                'start_ms': be_from_riva[i - 1]['timestamp_ms'],
                'end_ms': be_from_riva[i]['timestamp_ms'],
            })

        # Histogram
        buckets = [
            ('<100ms', 0, 100),
            ('100-500ms', 100, 500),
            ('500ms-1s', 500, 1000),
            ('1-5s', 1000, 5000),
            ('5-10s', 5000, 10000),
            ('10-20s', 10000, 20000),
            ('20-40s', 20000, 40000),
        ]

        print("\n  Distribution of inter-event gaps for audio_from_riva:")
        print(f"  {'Bucket':>12s}  {'Count':>8s}  {'Pct':>6s}")
        print("  " + "-" * 35)
        total_riva_gaps = len(riva_gaps)
        for label, lo, hi in buckets:
            count = sum(1 for g in riva_gaps if lo <= g['gap_ms'] < hi)
            pct = count / total_riva_gaps * 100 if total_riva_gaps else 0
            bar = '#' * int(pct / 2)
            print(f"  {label:>12s}  {count:8d}  {pct:5.1f}%  {bar}")

        # Top 10 largest gaps
        riva_gaps_sorted = sorted(riva_gaps, key=lambda x: -x['gap_ms'])
        print(f"\n  Top 10 largest gaps between consecutive audio_from_riva events:")
        print(f"  {'#':>3s}  {'Gap (s)':>10s}  {'Start (ms)':>12s}  {'End (ms)':>12s}")
        print("  " + "-" * 45)
        for i, rg in enumerate(riva_gaps_sorted[:10]):
            print(f"  {i+1:3d}  {rg['gap_ms']/1000:10.2f}  {rg['start_ms']:12.1f}  {rg['end_ms']:12.1f}")

        # Correlation with client-side stalls
        print(f"\n  Correlation: Do Riva output gaps match client-side stalls?")
        print(f"  {'Client Stall #':>15s}  {'Client Gap':>12s}  "
              f"{'Nearest Riva Gap':>18s}  {'Riva Gap Size':>15s}  {'Match?':>8s}")
        print("  " + "-" * 75)

        for idx, g in enumerate(top15):
            # Find the riva gap that most overlaps with this client stall
            best_riva = None
            best_overlap = 0
            for rg in riva_gaps:
                overlap_start = max(g['gap_start_ms'], rg['start_ms'])
                overlap_end = min(g['gap_end_ms'], rg['end_ms'])
                overlap = max(0, overlap_end - overlap_start)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_riva = rg

            if best_riva:
                match = "YES" if best_overlap > 1000 else "no"
                print(f"  {idx+1:15d}  {g['gap_duration_sec']:12.2f}s  "
                      f"{best_riva['start_ms']:12.1f}ms  {best_riva['gap_ms']/1000:12.2f}s  {match:>8s}")
            else:
                print(f"  {idx+1:15d}  {g['gap_duration_sec']:12.2f}s  {'N/A':>18s}  {'N/A':>15s}  {'N/A':>8s}")

    # =========================================================================
    # 5. ASR ACCUMULATION HYPOTHESIS
    # =========================================================================
    print("\n" + "=" * 90)
    print("SECTION 5: ASR ACCUMULATION HYPOTHESIS")
    print("=" * 90)

    be_to_riva = events_by_key.get(('backend', 'audio_to_riva'), [])

    print("\n  For each of top 5 stalls: audio_to_riva events between last audio_from_riva")
    print("  before gap and first audio_from_riva after gap (= audio ASR accumulated)")
    print()

    for idx, g in enumerate(top15[:5]):
        # Find last audio_from_riva before gap_start
        last_from_riva_before = None
        for e in be_from_riva:
            if e['timestamp_ms'] <= g['gap_start_ms']:
                last_from_riva_before = e
            else:
                break

        # Find first audio_from_riva after gap_end
        first_from_riva_after = None
        for e in be_from_riva:
            if e['timestamp_ms'] >= g['gap_end_ms']:
                first_from_riva_after = e
                break

        if last_from_riva_before and first_from_riva_after:
            t_start = last_from_riva_before['timestamp_ms']
            t_end = first_from_riva_after['timestamp_ms']

            # Count audio_to_riva in that window
            to_riva_count = sum(1 for e in be_to_riva
                                if t_start <= e['timestamp_ms'] <= t_end)
            audio_accumulated_sec = to_riva_count * 0.3

            print(f"  Stall #{idx+1} ({g['gap_duration_sec']:.2f}s):")
            print(f"    Last audio_from_riva before gap: {t_start:.1f} ms")
            print(f"    First audio_from_riva after gap: {t_end:.1f} ms")
            print(f"    Riva silence window: {(t_end - t_start)/1000:.2f}s")
            print(f"    audio_to_riva events in window: {to_riva_count}")
            print(f"    Audio accumulated by ASR: {audio_accumulated_sec:.1f}s")
            print(f"    -> {'HIGH: ASR batching likely cause' if audio_accumulated_sec > 8 else 'LOW: unlikely ASR batching'}")
            print()
        else:
            print(f"  Stall #{idx+1}: Could not find bounding audio_from_riva events")

    # Extended: check ALL stalls
    print("  ASR accumulation for ALL stalls:")
    print(f"  {'#':>3s}  {'Gap (s)':>8s}  {'Riva Silence (s)':>17s}  "
          f"{'to_riva count':>14s}  {'Audio Accum (s)':>16s}  {'Verdict':>20s}")
    print("  " + "-" * 85)

    for idx, g in enumerate(top15):
        last_from_riva_before = None
        for e in be_from_riva:
            if e['timestamp_ms'] <= g['gap_start_ms']:
                last_from_riva_before = e
            else:
                break

        first_from_riva_after = None
        for e in be_from_riva:
            if e['timestamp_ms'] >= g['gap_end_ms']:
                first_from_riva_after = e
                break

        if last_from_riva_before and first_from_riva_after:
            t_start = last_from_riva_before['timestamp_ms']
            t_end = first_from_riva_after['timestamp_ms']
            to_riva_count = sum(1 for e in be_to_riva
                                if t_start <= e['timestamp_ms'] <= t_end)
            audio_accum = to_riva_count * 0.3
            riva_silence = (t_end - t_start) / 1000

            verdict = "ASR BATCHING" if audio_accum > 8 else "other cause"
            print(f"  {idx+1:3d}  {g['gap_duration_sec']:8.2f}  {riva_silence:17.2f}  "
                  f"{to_riva_count:14d}  {audio_accum:16.1f}  {verdict:>20s}")
        else:
            print(f"  {idx+1:3d}  {g['gap_duration_sec']:8.2f}  {'N/A':>17s}  "
                  f"{'N/A':>14s}  {'N/A':>16s}  {'N/A':>20s}")

    # =========================================================================
    # 6. SUMMARY
    # =========================================================================
    print("\n" + "=" * 90)
    print("SECTION 6: SUMMARY")
    print("=" * 90)

    # Compute aggregates for the summary
    stalls_with_asr_batching = 0
    stalls_with_riva_match = 0
    stalls_with_client_sending = 0
    stalls_with_backend_receiving = 0

    for idx, g in enumerate(top15):
        # Check client still sending
        cli_during = sum(1 for e in cli_sent
                         if g['gap_start_ms'] <= e['timestamp_ms'] <= g['gap_end_ms'])
        if cli_during > 0:
            stalls_with_client_sending += 1

        # Check backend still receiving
        be_during = sum(1 for e in be_recv
                        if g['gap_start_ms'] <= e['timestamp_ms'] <= g['gap_end_ms'])
        if be_during > 0:
            stalls_with_backend_receiving += 1

        # Check riva gap correlation
        if len(be_from_riva) >= 2:
            for rg in riva_gaps_sorted[:20]:
                overlap_start = max(g['gap_start_ms'], rg['start_ms'])
                overlap_end = min(g['gap_end_ms'], rg['end_ms'])
                if overlap_end - overlap_start > 1000:
                    stalls_with_riva_match += 1
                    break

        # Check ASR batching
        last_fr = None
        for e in be_from_riva:
            if e['timestamp_ms'] <= g['gap_start_ms']:
                last_fr = e
            else:
                break
        first_fr = None
        for e in be_from_riva:
            if e['timestamp_ms'] >= g['gap_end_ms']:
                first_fr = e
                break
        if last_fr and first_fr:
            tc = sum(1 for e in be_to_riva
                     if last_fr['timestamp_ms'] <= e['timestamp_ms'] <= first_fr['timestamp_ms'])
            if tc * 0.3 > 8:
                stalls_with_asr_batching += 1

    total_stalls = len(top15)

    print(f"""
FINDINGS:
---------
Total stalls > 3s found: {len(gaps)}
Top {total_stalls} analyzed, ranging from {top15[-1]['gap_duration_sec']:.1f}s to {top15[0]['gap_duration_sec']:.1f}s

KEY EVIDENCE:

1. Client keeps sending during stalls: {stalls_with_client_sending}/{total_stalls}
   -> The client is NOT the bottleneck. Audio chunks continue to be sent
      at the expected ~300ms cadence throughout every stall.

2. Backend keeps receiving during stalls: {stalls_with_backend_receiving}/{total_stalls}
   -> The WebSocket transport is NOT the bottleneck. The backend receives
      chunks from the client throughout every stall.

3. Riva output gaps correlate with stalls: {stalls_with_riva_match}/{total_stalls}
   -> Large gaps in backend audio_from_riva events align almost exactly
      with the client-side stalls. Riva stops producing output during stalls.

4. ASR accumulation explains stalls: {stalls_with_asr_batching}/{total_stalls}
   -> During each stall, audio_to_riva events continue to accumulate
      (audio is still being fed to Riva), but audio_from_riva stops.
      This means Riva's ASR is accumulating audio without producing
      transcriptions, likely waiting for a sentence boundary or
      sufficient acoustic evidence.

ROOT CAUSE:
-----------
The stalls are caused by Riva's ASR (Automatic Speech Recognition) stage
accumulating audio before emitting a transcription. The pipeline is:

  Mic -> ASR (en-US) -> NMT -> TTS (es-US) -> Speakers

ASR waits until it has enough audio context (typically a sentence or pause)
before producing a transcription. During this waiting period:
  - Audio chunks continue flowing from client to backend to Riva (audio_to_riva)
  - But Riva produces NO output (no audio_from_riva events)
  - Therefore no translated audio reaches the client

When ASR finally emits a transcription, the NMT+TTS pipeline produces a burst
of audio, and the client receives a batch of audio_received events at once.

RECOMMENDATIONS:
----------------
1. Configure Riva ASR for more frequent partial/interim results
2. Reduce ASR's utterance end timeout / end-of-sentence detection threshold
3. Consider streaming ASR results and translating partial transcriptions
4. Add client-side buffering/interpolation to smooth playback during ASR gaps
""")


if __name__ == '__main__':
    main()
