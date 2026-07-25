import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import analyze_semantic_event_latency as semantic_latency_module
from analyze_semantic_event_latency import (
    SemanticEventLatencyError,
    analyze_semantic_event_latency,
    main,
    parse_cli_args,
    render_semantic_event_latency_markdown,
)


CSV_COLUMNS = [
    "source",
    "stage",
    "chunk_index",
    "audio_bytes",
    "media_duration_sec",
    "scheduled_duration_sec",
    "playback_rate",
    "audio_metadata_protocol_version",
    "stream_generation",
    "parent_sequence_id",
    "audio_frame_id",
    "audio_frame_count",
    "source_start_ms",
    "source_end_ms",
    "source_timing_basis",
    "binary_receipt_client_ms",
    "parent_complete_received_client_ms",
    "input_sample_zero_client_ms",
    "input_chunk_emitted_client_ms",
    "input_source_sample_start",
    "input_source_sample_end_exclusive",
    "input_sample_rate_hz",
    "input_pcm_sha256",
    "input_pcm_sample_count",
    "input_ledger_valid",
    "source_end_boundary_client_ms",
    "source_end_to_binary_receipt_ms",
    "source_end_to_parent_complete_ms",
    "schedule_performance_client_ms",
    "audio_context_time_at_schedule_sec",
    "scheduled_start_context_sec",
    "scheduled_end_context_sec",
    "projected_scheduled_start_client_ms",
    "source_end_to_projected_scheduled_start_ms",
]
SOURCE_PCM_SHA256 = "ab" * 32
SOURCE_PCM_SAMPLE_COUNT = 2000


def _row(**values):
    row = {column: "" for column in CSV_COLUMNS}
    row.update({key: str(value) for key, value in values.items()})
    return row


def _protocol_row(
    stage,
    *,
    parent,
    frame="",
    frame_count="",
    audio_bytes,
    source_start,
    source_end,
    receipt="",
    complete_receipt="",
    schedule_time="",
    projected_start="",
    duration="",
    audio_context_time=1.0,
    scheduled_start_context="",
):
    source_boundary = 10000 + float(source_end)
    has_receipt = receipt != ""
    has_completion = complete_receipt != ""
    has_schedule = schedule_time != ""
    playback_rate = 1.0
    media_duration = float(duration) if has_schedule else ""
    if has_schedule and scheduled_start_context == "":
        scheduled_start_context = (
            float(audio_context_time)
            + (float(projected_start) - float(schedule_time)) / 1000.0
        )
    elif not has_schedule:
        scheduled_start_context = ""
    scheduled_end_context = (
        scheduled_start_context + float(duration)
        if has_schedule
        else ""
    )
    return _row(
        source="client",
        stage=stage,
        chunk_index=-1 if stage == "audio_parent_complete" else 0,
        audio_bytes=audio_bytes,
        media_duration_sec=media_duration,
        scheduled_duration_sec=duration,
        playback_rate=playback_rate if has_schedule else "",
        audio_metadata_protocol_version=1,
        stream_generation=7,
        parent_sequence_id=parent,
        audio_frame_id=frame,
        audio_frame_count=frame_count,
        source_start_ms=source_start,
        source_end_ms=source_end,
        source_timing_basis="attributed_range",
        binary_receipt_client_ms=receipt,
        parent_complete_received_client_ms=complete_receipt,
        input_sample_zero_client_ms=10000,
        input_ledger_valid="true",
        source_end_boundary_client_ms=source_boundary,
        source_end_to_binary_receipt_ms=(
            float(receipt) - source_boundary if has_receipt else ""
        ),
        source_end_to_parent_complete_ms=(
            float(complete_receipt) - source_boundary
            if has_completion
            else ""
        ),
        schedule_performance_client_ms=schedule_time,
        audio_context_time_at_schedule_sec=(
            audio_context_time if has_schedule else ""
        ),
        scheduled_start_context_sec=scheduled_start_context,
        scheduled_end_context_sec=scheduled_end_context,
        projected_scheduled_start_client_ms=projected_start,
        source_end_to_projected_scheduled_start_ms=(
            float(projected_start) - source_boundary
            if has_schedule
            else ""
        ),
    )


def _base_rows():
    rows = [
        _row(
            source="client",
            stage="chunk_sent",
            chunk_index=0,
            audio_bytes=2000,
            input_sample_zero_client_ms="10000.000",
            input_chunk_emitted_client_ms="11000.000",
            input_source_sample_start=0,
            input_source_sample_end_exclusive=1000,
            input_sample_rate_hz=1000,
            input_pcm_sha256=SOURCE_PCM_SHA256,
            input_pcm_sample_count=SOURCE_PCM_SAMPLE_COUNT,
            input_ledger_valid="true",
        ),
        _row(
            source="client",
            stage="chunk_sent",
            chunk_index=1,
            audio_bytes=2000,
            input_sample_zero_client_ms="10000.000",
            input_chunk_emitted_client_ms="12000.000",
            input_source_sample_start=1000,
            input_source_sample_end_exclusive=2000,
            input_sample_rate_hz=1000,
            input_pcm_sha256=SOURCE_PCM_SHA256,
            input_pcm_sample_count=SOURCE_PCM_SAMPLE_COUNT,
            input_ledger_valid="true",
        ),
    ]
    for (
        frame,
        receipt,
        schedule_time,
        projected_start,
        audio_context_time,
        scheduled_start_context,
    ) in (
        (0, 12000, 13000, 13000, 1.0, 1.0),
        (1, 13010, 13020, 13100, 1.02, 1.1),
    ):
        rows.append(
            _protocol_row(
                "audio_received",
                parent=0,
                frame=frame,
                audio_bytes=3200,
                source_start=0,
                source_end=900,
                receipt=receipt,
            )
        )
        rows.append(
            _protocol_row(
                "playback_chunk_scheduled",
                parent=0,
                frame=frame,
                audio_bytes=3200,
                source_start=0,
                source_end=900,
                receipt=receipt,
                schedule_time=schedule_time,
                projected_start=projected_start,
                duration="0.100000",
                audio_context_time=audio_context_time,
                scheduled_start_context=scheduled_start_context,
            )
        )
    rows.append(
        _protocol_row(
            "audio_parent_complete",
            parent=0,
            frame_count=2,
            audio_bytes=6400,
            source_start=0,
            source_end=900,
            complete_receipt=13120,
        )
    )
    rows.extend(
        [
            _protocol_row(
                "audio_received",
                parent=1,
                frame=0,
                audio_bytes=6400,
                source_start=1000,
                source_end=1900,
                receipt=14000,
            ),
            _protocol_row(
                "playback_chunk_scheduled",
                parent=1,
                frame=0,
                audio_bytes=6400,
                source_start=1000,
                source_end=1900,
                receipt=14000,
                schedule_time=15000,
                projected_start=15000,
                duration="0.200000",
                audio_context_time=3.0,
                scheduled_start_context=3.0,
            ),
            _protocol_row(
                "audio_parent_complete",
                parent=1,
                frame_count=1,
                audio_bytes=6400,
                source_start=1000,
                source_end=1900,
                complete_receipt=15100,
            ),
        ]
    )
    return rows


def _markers(*items):
    if not items:
        items = (("event-001", 800, 2),)
    return [
        {
            "event_id": event_id,
            "source_sample_index": sample_index,
            "independent_reviewer_count": reviewers,
        }
        for event_id, sample_index, reviewers in items
    ]


def _write_case(tmp_path, *, rows=None, markers=None):
    csv_path = tmp_path / "capture.csv"
    marker_path = tmp_path / "markers.json"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(_base_rows() if rows is None else rows)
    capture_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    document = {
        "schema_version": 1,
        "capture_csv_sha256": capture_hash,
        "source_sample_rate_hz": 1000,
        "source_pcm_sha256": SOURCE_PCM_SHA256,
        "source_pcm_sample_count": SOURCE_PCM_SAMPLE_COUNT,
        "markers": _markers() if markers is None else markers,
    }
    marker_path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )
    return csv_path, marker_path


def _set_parent_source_range(rows, parent, source_start, source_end):
    source_boundary = 10000 + float(source_end)
    for row in rows:
        if row["parent_sequence_id"] != str(parent):
            continue
        row["source_start_ms"] = str(source_start)
        row["source_end_ms"] = str(source_end)
        row["source_end_boundary_client_ms"] = str(source_boundary)
        if row["binary_receipt_client_ms"]:
            row["source_end_to_binary_receipt_ms"] = str(
                float(row["binary_receipt_client_ms"]) - source_boundary
            )
        if row["parent_complete_received_client_ms"]:
            row["source_end_to_parent_complete_ms"] = str(
                float(row["parent_complete_received_client_ms"])
                - source_boundary
            )
        if row["projected_scheduled_start_client_ms"]:
            row["source_end_to_projected_scheduled_start_ms"] = str(
                float(row["projected_scheduled_start_client_ms"])
                - source_boundary
            )


def _rewrite_markers(marker_path, mutate):
    document = json.loads(marker_path.read_text(encoding="utf-8"))
    mutate(document)
    marker_path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )


def test_valid_capture_calculates_conservative_bounds_and_claim_scope(
    tmp_path,
):
    csv_path, marker_path = _write_case(tmp_path)

    analysis = analyze_semantic_event_latency(
        csv_path,
        marker_path,
        3,
    )

    assert analysis["schema_version"] == 1
    assert analysis["evidence"]["capture_csv_sha256"] == (
        hashlib.sha256(csv_path.read_bytes()).hexdigest()
    )
    assert analysis["evidence"]["stream_generation"] == 7
    assert analysis["evidence"]["input_ledger_chunk_count"] == 2
    assert analysis["evidence"]["input_pacing_mode"] == (
        "chunk_end_boundary_v1"
    )
    assert analysis["evidence"]["source_pcm_sha256"] == SOURCE_PCM_SHA256
    assert analysis["evidence"]["source_pcm_sample_count"] == 2000
    assert analysis["evidence"]["source_pcm_binding_verified"] is True
    assert analysis["evidence"]["playback_clock_link_tolerance_ms"] == 25
    assert (
        analysis["evidence"]["playback_clock_offset_span_limit_ms"] == 50
    )
    assert (
        analysis["evidence"][
            "playback_clock_maximum_absolute_link_residual_ms"
        ]
        == 0
    )
    assert analysis["evidence"]["playback_clock_offset_span_ms"] == 0
    assert analysis["claim_scope"] == {
        "semantic_source_marker_reviewed": True,
        "source_pcm_binding_verified": True,
        "target_landmark_proven": False,
        "actual_audibility_proven": False,
    }
    assert analysis["summary"] == {
        "marker_count": 1,
        "unique_candidate_group_count": 1,
        "candidate_groups": [
            {
                "candidate_group_id": "group-001",
                "marker_count": 1,
            }
        ],
        "status_counts": {
            "pass": 1,
            "inconclusive": 0,
            "fail": 0,
        },
        "overall_status": "pass",
    }

    event = analysis["events"][0]
    assert event["event_id"] == "event-001"
    assert event["candidate_group_id"] == "group-001"
    assert event["semantic_source_marker_reviewed"] is True
    assert event["source_pcm_binding_verified"] is True
    assert event["source_event_client_ms"] == 10800
    assert event["candidate_parent_sequence_ids"] == [0]
    assert event["candidate_frame_count"] == 2
    assert event["first_candidate_frame_receipt_client_ms"] == 12000
    assert event["last_candidate_frame_receipt_client_ms"] == 13010
    assert event["parent_complete_receipt_bound_client_ms"] == 13120
    assert event["projected_first_frame_start_client_ms"] == 13000
    assert event["projected_final_frame_end_client_ms"] == 13200
    assert (
        event["conservative_projected_first_frame_start_client_ms"]
        == 12975
    )
    assert (
        event["conservative_projected_final_frame_end_client_ms"]
        == 13225
    )
    assert event["latency_bounds_ms"] == {
        "first_candidate_frame_receipt": 1200,
        "last_candidate_frame_receipt": 2210,
        "parent_complete_receipt": 2320,
        "conservative_projected_first_frame_start": 2175,
        "conservative_projected_final_frame_end": 2425,
    }


@pytest.mark.parametrize(
    ("maximum_latency", "expected"),
    [
        (2.5, "pass"),
        (2.3, "inconclusive"),
        (2.1, "fail"),
    ],
)
def test_event_status_uses_end_then_start_bounds(
    tmp_path,
    maximum_latency,
    expected,
):
    csv_path, marker_path = _write_case(tmp_path)

    analysis = analyze_semantic_event_latency(
        csv_path,
        marker_path,
        maximum_latency,
    )

    assert analysis["events"][0]["status"] == expected
    assert analysis["summary"]["overall_status"] == expected


@pytest.mark.parametrize(
    ("maximum_latency", "expected", "counts"),
    [
        (3.8, "pass", (2, 0, 0)),
        (3.6, "inconclusive", (1, 1, 0)),
        (3.0, "fail", (1, 0, 1)),
    ],
)
def test_aggregate_precedence_is_fail_then_inconclusive_then_pass(
    tmp_path,
    maximum_latency,
    expected,
    counts,
):
    csv_path, marker_path = _write_case(
        tmp_path,
        markers=_markers(
            ("event-001", 800, 2),
            ("event-002", 1500, 3),
        ),
    )

    analysis = analyze_semantic_event_latency(
        csv_path,
        marker_path,
        maximum_latency,
    )

    assert analysis["summary"]["overall_status"] == expected
    status_counts = analysis["summary"]["status_counts"]
    assert (
        status_counts["pass"],
        status_counts["inconclusive"],
        status_counts["fail"],
    ) == counts


def test_identical_source_range_siblings_use_union_conservatively(tmp_path):
    rows = _base_rows()
    for row in rows:
        if row["parent_sequence_id"] == "1":
            row["source_start_ms"] = "0"
            row["source_end_ms"] = "900"
            row["source_end_boundary_client_ms"] = "10900"
            if row["binary_receipt_client_ms"]:
                row["source_end_to_binary_receipt_ms"] = str(
                    float(row["binary_receipt_client_ms"]) - 10900
                )
            if row["parent_complete_received_client_ms"]:
                row["source_end_to_parent_complete_ms"] = str(
                    float(row["parent_complete_received_client_ms"]) - 10900
                )
            if row["projected_scheduled_start_client_ms"]:
                row["source_end_to_projected_scheduled_start_ms"] = str(
                    float(row["projected_scheduled_start_client_ms"])
                    - 10900
                )
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    analysis = analyze_semantic_event_latency(
        csv_path,
        marker_path,
        5,
    )

    event = analysis["events"][0]
    assert event["candidate_parent_sequence_ids"] == [0, 1]
    assert event["candidate_frame_count"] == 3
    assert event["first_candidate_frame_receipt_client_ms"] == 12000
    assert event["last_candidate_frame_receipt_client_ms"] == 14000
    assert event["parent_complete_receipt_bound_client_ms"] == 15100
    assert event["projected_first_frame_start_client_ms"] == 13000
    assert event["projected_final_frame_end_client_ms"] == 15200


@pytest.mark.parametrize("include_second_suffix", [False, True])
def test_same_end_suffix_pair_or_chain_allows_outer_only_marker(
    tmp_path,
    include_second_suffix,
):
    rows = _base_rows()
    _set_parent_source_range(rows, 1, 700, 900)
    if include_second_suffix:
        rows.extend(
            [
                _protocol_row(
                    "audio_received",
                    parent=2,
                    frame=0,
                    audio_bytes=3200,
                    source_start=800,
                    source_end=900,
                    receipt=15500,
                ),
                _protocol_row(
                    "playback_chunk_scheduled",
                    parent=2,
                    frame=0,
                    audio_bytes=3200,
                    source_start=800,
                    source_end=900,
                    receipt=15500,
                    schedule_time=16000,
                    projected_start=16000,
                    duration="0.100000",
                    audio_context_time=4.0,
                    scheduled_start_context=4.0,
                ),
                _protocol_row(
                    "audio_parent_complete",
                    parent=2,
                    frame_count=1,
                    audio_bytes=3200,
                    source_start=800,
                    source_end=900,
                    complete_receipt=16100,
                ),
            ]
        )
    csv_path, marker_path = _write_case(
        tmp_path,
        rows=rows,
        markers=_markers(("event-001", 100, 2)),
    )

    analysis = analyze_semantic_event_latency(csv_path, marker_path, 5)

    assert analysis["events"][0]["candidate_parent_sequence_ids"] == [0]


def test_marker_in_same_end_suffix_overlap_fails_closed(tmp_path):
    rows = _base_rows()
    _set_parent_source_range(rows, 1, 700, 900)
    csv_path, marker_path = _write_case(
        tmp_path,
        rows=rows,
        markers=_markers(("event-001", 800, 2)),
    )

    with pytest.raises(
        SemanticEventLatencyError,
        match="resolves to distinct source ranges",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 5)


@pytest.mark.parametrize(
    ("source_start", "source_end", "message"),
    [
        (700, 1200, "overlapping distinct source ranges"),
        (700, 900.001, "overlapping distinct source ranges"),
        (700, 800, "source ranges move backward"),
        (0, 1200, "overlapping distinct source ranges"),
    ],
    ids=[
        "crossing",
        "nonidentical-end",
        "different-end-nesting",
        "later-range-contains-earlier",
    ],
)
def test_unsupported_distinct_source_range_overlaps_fail_closed(
    tmp_path,
    source_start,
    source_end,
    message,
):
    rows = _base_rows()
    _set_parent_source_range(rows, 1, source_start, source_end)
    csv_path, marker_path = _write_case(
        tmp_path,
        rows=rows,
        markers=_markers(("event-001", 100, 2)),
    )

    with pytest.raises(SemanticEventLatencyError, match=message):
        analyze_semantic_event_latency(csv_path, marker_path, 5)


def test_marker_on_inclusive_touching_range_boundary_is_ambiguous(tmp_path):
    rows = _base_rows()
    _set_parent_source_range(rows, 1, 900, 1900)
    csv_path, marker_path = _write_case(
        tmp_path,
        rows=rows,
        markers=_markers(("event-001", 900, 2)),
    )

    with pytest.raises(
        SemanticEventLatencyError,
        match="resolves to distinct source ranges",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 5)


def test_hash_mismatch_fails_closed(tmp_path):
    csv_path, marker_path = _write_case(tmp_path)
    csv_path.write_bytes(csv_path.read_bytes() + b"\n")

    with pytest.raises(
        SemanticEventLatencyError,
        match="does not match the CSV bytes",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_marker_sidecar_rejects_non_utf8_encoding(tmp_path):
    csv_path, marker_path = _write_case(tmp_path)
    document = marker_path.read_text(encoding="utf-8")
    marker_path.write_bytes(document.encode("utf-16"))

    with pytest.raises(
        SemanticEventLatencyError,
        match="not valid UTF-8 JSON",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_reviewed_pcm_a_cannot_be_applied_to_capture_pcm_b(tmp_path):
    csv_path, marker_path = _write_case(tmp_path)
    _rewrite_markers(
        marker_path,
        lambda document: document.update(
            {"source_pcm_sha256": "cd" * 32}
        ),
    )

    with pytest.raises(
        SemanticEventLatencyError,
        match="input PCM digest does not match the sidecar",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_output_cannot_precede_its_attributed_source_boundary(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["parent_sequence_id"] == "0"
            and row["audio_frame_id"] == "0"
            and row["stage"] in {
                "audio_received",
                "playback_chunk_scheduled",
            }
        ):
            row["binary_receipt_client_ms"] = "10800"
            row["source_end_to_binary_receipt_ms"] = "-100"
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(
        SemanticEventLatencyError,
        match="before its source-end boundary",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_first_projected_start_must_equal_its_schedule_clock(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["stage"] == "playback_chunk_scheduled"
            and row["parent_sequence_id"] == "0"
            and row["audio_frame_id"] == "0"
        ):
            row["projected_scheduled_start_client_ms"] = "13020"
            row["source_end_to_projected_scheduled_start_ms"] = "2120"
            break
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(
        SemanticEventLatencyError,
        match="global projected start recurrence",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_global_projection_accepts_quantized_clocks_and_true_gap(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["stage"] == "playback_chunk_scheduled"
            and row["parent_sequence_id"] == "0"
            and row["audio_frame_id"] == "1"
        ):
            row["scheduled_start_context_sec"] = "1.100009"
            row["scheduled_end_context_sec"] = "1.200009"
            row["projected_scheduled_start_client_ms"] = "13100.009"
            row["source_end_to_projected_scheduled_start_ms"] = "2200.009"
            break
    csv_path, marker_path = _write_case(
        tmp_path,
        rows=rows,
        markers=_markers(
            ("event-001", 800, 2),
            ("event-002", 1500, 2),
        ),
    )

    analysis = analyze_semantic_event_latency(csv_path, marker_path, 5)

    assert analysis["summary"]["overall_status"] == "pass"
    assert analysis["events"][0][
        "projected_final_frame_end_client_ms"
    ] == 13200.009
    assert analysis["events"][1][
        "projected_first_frame_start_client_ms"
    ] == 15000


def test_client_audio_context_wait_divergence_fails_closed(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["stage"] == "playback_chunk_scheduled"
            and row["parent_sequence_id"] == "0"
            and row["audio_frame_id"] == "1"
        ):
            # Both ordered recurrences still pass independently, but the
            # context wait is now 30 ms longer than the projected wait.
            row["audio_context_time_at_schedule_sec"] = "0.99"
            break
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(
        SemanticEventLatencyError,
        match="client/AudioContext playback-wait linkage",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_capture_wide_clock_offset_divergence_fails_closed(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["stage"] == "playback_chunk_scheduled"
            and row["parent_sequence_id"] == "1"
        ):
            # Both domains independently show an underrun, so their wait
            # residual is zero. The 100 ms change in their session offset must
            # still invalidate the capture.
            row["audio_context_time_at_schedule_sec"] = "3.1"
            row["scheduled_start_context_sec"] = "3.1"
            row["scheduled_end_context_sec"] = "3.3"
            break
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(
        SemanticEventLatencyError,
        match="clock offset span exceeds",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


@pytest.mark.parametrize(
    ("maximum_latency", "expected"),
    [
        (2.41, "inconclusive"),
        (2.18, "inconclusive"),
        (2.17, "fail"),
    ],
)
def test_clock_link_allowance_is_applied_to_decision_bounds(
    tmp_path,
    maximum_latency,
    expected,
):
    csv_path, marker_path = _write_case(tmp_path)

    analysis = analyze_semantic_event_latency(
        csv_path,
        marker_path,
        maximum_latency,
    )

    assert analysis["summary"]["overall_status"] == expected


def test_old_event_local_projection_reanchoring_fails_closed(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["stage"] == "playback_chunk_scheduled"
            and row["parent_sequence_id"] == "0"
            and row["audio_frame_id"] == "1"
        ):
            # This satisfies the old event-local affine formula:
            # 13020 + (1.1 - 1.01) * 1000 = 13110. It is nevertheless
            # impossible in the global queue, whose prior projected end is
            # 13100.
            row["audio_context_time_at_schedule_sec"] = "1.01"
            row["projected_scheduled_start_client_ms"] = "13110"
            row["source_end_to_projected_scheduled_start_ms"] = "2210"
            break
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(
        SemanticEventLatencyError,
        match="global projected start recurrence",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_broken_context_recurrence_fails_closed(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["stage"] == "playback_chunk_scheduled"
            and row["parent_sequence_id"] == "0"
            and row["audio_frame_id"] == "1"
        ):
            # Both the old affine projection and the new projected recurrence
            # reconcile, but this inserts an unaccounted 10 ms context gap.
            row["schedule_performance_client_ms"] = "13010"
            row["scheduled_start_context_sec"] = "1.11"
            row["scheduled_end_context_sec"] = "1.21"
            break
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(
        SemanticEventLatencyError,
        match="global scheduled context start recurrence",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_context_recurrence_continues_across_parent_boundary(tmp_path):
    rows = _base_rows()
    for row in rows:
        if (
            row["stage"] == "playback_chunk_scheduled"
            and row["parent_sequence_id"] == "1"
        ):
            row["audio_context_time_at_schedule_sec"] = "1.9"
            row["scheduled_start_context_sec"] = "2.0"
            row["scheduled_end_context_sec"] = "2.2"
            break
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(
        SemanticEventLatencyError,
        match="global scheduled context start recurrence",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("duplicate_receive", "duplicate audio_received"),
        ("source_range_mismatch", "source range mismatch"),
        ("schedule_before_receipt", "scheduled before its receipt"),
        ("completion_before_frame", "completed before its final frame"),
        ("nonmonotonic_receipt", "non-monotonic frame receipts"),
        ("wire_row_order", "wire order"),
        ("cross_parent_clock", "wire order"),
        ("projected_overlap", "global projected start recurrence"),
        ("media_byte_duration", "media duration/output PCM byte count"),
    ],
)
def test_additional_protocol_chronology_corruption_fails_closed(
    tmp_path,
    corruption,
    message,
):
    rows = _base_rows()
    if corruption == "duplicate_receive":
        rows.append(dict(rows[2]))
    elif corruption == "source_range_mismatch":
        for row in rows:
            if row["stage"] == "playback_chunk_scheduled":
                row["source_start_ms"] = "1"
                break
    elif corruption == "schedule_before_receipt":
        for row in rows:
            if (
                row["stage"] == "playback_chunk_scheduled"
                and row["parent_sequence_id"] == "0"
                and row["audio_frame_id"] == "0"
            ):
                row["schedule_performance_client_ms"] = "11999"
                row["scheduled_start_context_sec"] = "2.001"
                row["scheduled_end_context_sec"] = "2.101"
                break
    elif corruption == "completion_before_frame":
        for row in rows:
            if (
                row["stage"] == "audio_parent_complete"
                and row["parent_sequence_id"] == "0"
            ):
                row["parent_complete_received_client_ms"] = "12050"
                row["source_end_to_parent_complete_ms"] = "1150"
                break
    elif corruption == "nonmonotonic_receipt":
        for row in rows:
            if (
                row["parent_sequence_id"] == "0"
                and row["audio_frame_id"] == "1"
                and row["stage"]
                in {"audio_received", "playback_chunk_scheduled"}
            ):
                row["binary_receipt_client_ms"] = "11950"
                row["source_end_to_binary_receipt_ms"] = "1050"
    elif corruption == "wire_row_order":
        rows[2], rows[3] = rows[3], rows[2]
    elif corruption == "cross_parent_clock":
        for row in rows:
            if (
                row["parent_sequence_id"] == "1"
                and row["stage"]
                in {"audio_received", "playback_chunk_scheduled"}
            ):
                row["binary_receipt_client_ms"] = "12150"
                row["source_end_to_binary_receipt_ms"] = "250"
    elif corruption == "projected_overlap":
        for row in rows:
            if row["parent_sequence_id"] != "1":
                continue
            if row["stage"] in {
                "audio_received",
                "playback_chunk_scheduled",
            }:
                row["binary_receipt_client_ms"] = "13000"
                row["source_end_to_binary_receipt_ms"] = "1100"
            if row["stage"] == "playback_chunk_scheduled":
                row["schedule_performance_client_ms"] = "13100"
                row["projected_scheduled_start_client_ms"] = "13150"
                row["source_end_to_projected_scheduled_start_ms"] = "1250"
                row["audio_context_time_at_schedule_sec"] = "2"
                row["scheduled_start_context_sec"] = "2"
                row["scheduled_end_context_sec"] = "2.2"
    elif corruption == "media_byte_duration":
        for row in rows:
            if row["stage"] == "playback_chunk_scheduled":
                row["media_duration_sec"] = "0.050000"
                break
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(SemanticEventLatencyError, match=message):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda document: document.update({"extra": "forbidden"}),
            "exact schema",
        ),
        (
            lambda document: document["markers"][0].update(
                {"extra": "forbidden"}
            ),
            "exact schema",
        ),
        (
            lambda document: document["markers"][0].update(
                {"event_id": "joke-001"}
            ),
            "event-NNN",
        ),
        (
            lambda document: document["markers"][0].update(
                {"source_sample_index": True}
            ),
            "must be an integer",
        ),
        (
            lambda document: document["markers"][0].update(
                {"independent_reviewer_count": 1}
            ),
            "fewer than 2",
        ),
        (
            lambda document: document.update({"schema_version": 2}),
            "schema_version must be 1",
        ),
        (
            lambda document: document.update(
                {"capture_csv_sha256": "A" * 64}
            ),
            "lowercase hexadecimal",
        ),
        (
            lambda document: document.update(
                {"source_pcm_sha256": "A" * 64}
            ),
            "source_pcm_sha256.*lowercase hexadecimal",
        ),
        (
            lambda document: document.update(
                {"source_pcm_sample_count": 0}
            ),
            "source_pcm_sample_count must be at least 1",
        ),
    ],
)
def test_marker_sidecar_schema_and_review_provenance_fail_closed(
    tmp_path,
    mutation,
    message,
):
    csv_path, marker_path = _write_case(tmp_path)
    _rewrite_markers(marker_path, mutation)

    with pytest.raises(SemanticEventLatencyError, match=message):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_duplicate_marker_json_key_fails_without_echoing_key(tmp_path):
    csv_path, marker_path = _write_case(tmp_path)
    text = marker_path.read_text(encoding="utf-8")
    marker_path.write_text(
        text.replace(
            '"schema_version": 1,',
            '"schema_version": 1,\n  "schema_version": 1,',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        SemanticEventLatencyError,
        match="duplicate object key",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_duplicate_csv_header_fails_closed(tmp_path):
    csv_path, marker_path = _write_case(tmp_path)
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    csv_path.write_text(
        "\n".join(
            [f"{lines[0]},source"]
            + [f"{line},client" for line in lines[1:]]
        )
        + "\n",
        encoding="utf-8",
    )
    _rewrite_markers(
        marker_path,
        lambda document: document.update(
            {
                "capture_csv_sha256": hashlib.sha256(
                    csv_path.read_bytes()
                ).hexdigest()
            }
        ),
    )

    with pytest.raises(
        SemanticEventLatencyError,
        match="duplicate column names",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("ledger_false", "marks the input ledger invalid"),
        ("ledger_gap", "duplicated or fragmented"),
        ("chunk_gap", "duplicated or fragmented"),
        ("sample_rate", "does not match the sidecar"),
        ("byte_count", "audio byte count"),
        ("anchor", "inconsistent sample-zero"),
        ("emission_order", "not monotonic"),
        ("early_emission", "chunk-end boundary pacing"),
        ("pcm_digest", "digest does not match the sidecar"),
        ("pcm_sample_count", "sample count does not match the sidecar"),
        ("final_sample", "final sample does not match"),
    ],
)
def test_input_ledger_corruption_fails_closed(
    tmp_path,
    corruption,
    message,
):
    rows = _base_rows()
    second = rows[1]
    if corruption == "ledger_false":
        second["input_ledger_valid"] = "false"
    elif corruption == "ledger_gap":
        second["input_source_sample_start"] = "1100"
        second["audio_bytes"] = "1800"
    elif corruption == "chunk_gap":
        second["chunk_index"] = "2"
    elif corruption == "sample_rate":
        second["input_sample_rate_hz"] = "16000"
    elif corruption == "byte_count":
        second["audio_bytes"] = "1998"
    elif corruption == "anchor":
        second["input_sample_zero_client_ms"] = "10001.000"
    elif corruption == "emission_order":
        second["input_chunk_emitted_client_ms"] = "10999.000"
    elif corruption == "early_emission":
        second["input_chunk_emitted_client_ms"] = "11998.999"
    elif corruption == "pcm_digest":
        second["input_pcm_sha256"] = "cd" * 32
    elif corruption == "pcm_sample_count":
        second["input_pcm_sample_count"] = "2001"
    elif corruption == "final_sample":
        second["input_source_sample_end_exclusive"] = "1900"
        second["audio_bytes"] = "1800"
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(SemanticEventLatencyError, match=message):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_marker_beyond_sent_ledger_fails_closed(tmp_path):
    csv_path, marker_path = _write_case(
        tmp_path,
        markers=_markers(("event-001", 2000, 2)),
    )

    with pytest.raises(SemanticEventLatencyError, match="beyond"):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_duplicate_source_sample_index_fails_closed(tmp_path):
    csv_path, marker_path = _write_case(
        tmp_path,
        markers=_markers(
            ("event-001", 800, 2),
            ("event-002", 800, 2),
        ),
    )

    with pytest.raises(
        SemanticEventLatencyError,
        match="duplicate source_sample_index",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_distinct_markers_may_share_one_conservative_parent_group(tmp_path):
    csv_path, marker_path = _write_case(
        tmp_path,
        markers=_markers(
            ("event-001", 100, 2),
            ("event-002", 800, 2),
        ),
    )

    analysis = analyze_semantic_event_latency(csv_path, marker_path, 4)

    assert [event["event_id"] for event in analysis["events"]] == [
        "event-001",
        "event-002",
    ]
    assert all(
        event["candidate_parent_sequence_ids"] == [0]
        for event in analysis["events"]
    )
    assert all(
        event["projected_final_frame_end_client_ms"] == 13200
        for event in analysis["events"]
    )
    assert {
        event["candidate_group_id"] for event in analysis["events"]
    } == {"group-001"}
    assert analysis["summary"]["marker_count"] == 2
    assert analysis["summary"]["unique_candidate_group_count"] == 1
    assert analysis["summary"]["candidate_groups"] == [
        {"candidate_group_id": "group-001", "marker_count": 2}
    ]


def test_events_are_sorted_by_source_sample_not_manifest_order(tmp_path):
    csv_path, marker_path = _write_case(
        tmp_path,
        markers=_markers(
            ("event-002", 1500, 2),
            ("event-001", 800, 2),
        ),
    )

    analysis = analyze_semantic_event_latency(
        csv_path,
        marker_path,
        4,
    )

    assert [event["event_id"] for event in analysis["events"]] == [
        "event-001",
        "event-002",
    ]
    assert [event["candidate_group_id"] for event in analysis["events"]] == [
        "group-001",
        "group-002",
    ]
    assert analysis["summary"]["candidate_groups"] == [
        {"candidate_group_id": "group-001", "marker_count": 1},
        {"candidate_group_id": "group-002", "marker_count": 1},
    ]


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("protocol", "protocol version 2"),
        ("generation", "stream-generation change"),
        ("basis", "lacks an attributed source range"),
        ("missing_range", "invalid or missing source_end_ms"),
        ("overlap", "overlapping distinct source ranges"),
        ("backward", "source ranges move backward"),
    ],
)
def test_protocol_and_source_range_corruption_fails_closed(
    tmp_path,
    corruption,
    message,
):
    rows = _base_rows()
    if corruption == "protocol":
        for row in rows:
            if row["stage"] == "audio_received":
                row["audio_metadata_protocol_version"] = "2"
                break
    elif corruption == "generation":
        for row in rows:
            if (
                row["stage"] == "playback_chunk_scheduled"
                and row["parent_sequence_id"] == "1"
            ):
                row["stream_generation"] = "8"
                break
    elif corruption == "basis":
        for row in rows:
            if row["stage"] == "audio_parent_complete":
                row["source_timing_basis"] = "partial_range"
                break
    elif corruption == "missing_range":
        for row in rows:
            if row["stage"] == "audio_received":
                row["source_end_ms"] = ""
                break
    elif corruption == "overlap":
        for row in rows:
            if row["parent_sequence_id"] == "1":
                row["source_start_ms"] = "800"
    elif corruption == "backward":
        for row in rows:
            parent_id = row["parent_sequence_id"]
            if parent_id not in {"0", "1"}:
                continue
            source_start, source_end = (
                ("1000", "1900")
                if parent_id == "0"
                else ("0", "900")
            )
            source_boundary = 10000 + float(source_end)
            row["source_start_ms"] = source_start
            row["source_end_ms"] = source_end
            row["source_end_boundary_client_ms"] = str(source_boundary)
            if row["binary_receipt_client_ms"]:
                row["source_end_to_binary_receipt_ms"] = str(
                    float(row["binary_receipt_client_ms"])
                    - source_boundary
                )
            if row["parent_complete_received_client_ms"]:
                row["source_end_to_parent_complete_ms"] = str(
                    float(row["parent_complete_received_client_ms"])
                    - source_boundary
                )
            if row["projected_scheduled_start_client_ms"]:
                row["source_end_to_projected_scheduled_start_ms"] = str(
                    float(row["projected_scheduled_start_client_ms"])
                    - source_boundary
                )
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(SemanticEventLatencyError, match=message):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("missing_receive", "frame keys do not match"),
        ("missing_schedule", "frame keys do not match"),
        ("missing_complete", "parent keys do not match"),
        ("receipt_mismatch", "receipt timestamp mismatch"),
        ("byte_mismatch", "audio bytes mismatch"),
        ("complete_count", "frame count does not reconcile"),
        ("complete_bytes", "audio bytes do not reconcile"),
        ("frame_gap", "fragmented frame IDs"),
    ],
)
def test_receive_schedule_and_parent_completion_must_reconcile(
    tmp_path,
    corruption,
    message,
):
    rows = _base_rows()
    if corruption == "missing_receive":
        rows = [
            row
            for row in rows
            if not (
                row["stage"] == "audio_received"
                and row["parent_sequence_id"] == "0"
                and row["audio_frame_id"] == "1"
            )
        ]
    elif corruption == "missing_schedule":
        rows = [
            row
            for row in rows
            if not (
                row["stage"] == "playback_chunk_scheduled"
                and row["parent_sequence_id"] == "0"
                and row["audio_frame_id"] == "1"
            )
        ]
    elif corruption == "missing_complete":
        rows = [
            row
            for row in rows
            if not (
                row["stage"] == "audio_parent_complete"
                and row["parent_sequence_id"] == "1"
            )
        ]
    elif corruption == "receipt_mismatch":
        for row in rows:
            if (
                row["stage"] == "playback_chunk_scheduled"
                and row["parent_sequence_id"] == "0"
                ):
                    row["binary_receipt_client_ms"] = "12001"
                    row["source_end_to_binary_receipt_ms"] = "1101"
                    break
    elif corruption == "byte_mismatch":
        for row in rows:
            if (
                row["stage"] == "audio_received"
                and row["parent_sequence_id"] == "0"
            ):
                row["audio_bytes"] = "3199"
                break
    elif corruption == "complete_count":
        for row in rows:
            if (
                row["stage"] == "audio_parent_complete"
                and row["parent_sequence_id"] == "0"
            ):
                row["audio_frame_count"] = "3"
                break
    elif corruption == "complete_bytes":
        for row in rows:
            if (
                row["stage"] == "audio_parent_complete"
                and row["parent_sequence_id"] == "0"
            ):
                row["audio_bytes"] = "199"
                break
    elif corruption == "frame_gap":
        for row in rows:
            if (
                row["parent_sequence_id"] == "0"
                and row["audio_frame_id"] == "1"
            ):
                row["audio_frame_id"] = "2"
    csv_path, marker_path = _write_case(tmp_path, rows=rows)

    with pytest.raises(SemanticEventLatencyError, match=message):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_unresolved_marker_fails_closed(tmp_path):
    csv_path, marker_path = _write_case(
        tmp_path,
        markers=_markers(("event-001", 950, 2)),
    )

    with pytest.raises(SemanticEventLatencyError, match="cannot be resolved"):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_touching_distinct_ranges_are_ambiguous_at_the_boundary(tmp_path):
    rows = _base_rows()
    for row in rows:
        if row["parent_sequence_id"] == "1":
            row["source_start_ms"] = "900"
    csv_path, marker_path = _write_case(
        tmp_path,
        rows=rows,
        markers=_markers(("event-001", 900, 2)),
    )

    with pytest.raises(
        SemanticEventLatencyError,
        match="resolves to distinct source ranges",
    ):
        analyze_semantic_event_latency(csv_path, marker_path, 3)


def test_markdown_and_json_are_privacy_safe_and_cli_writes_both(
    tmp_path,
    capsys,
):
    csv_path, marker_path = _write_case(tmp_path)
    csv_lines = csv_path.read_text(encoding="utf-8").splitlines()
    csv_path.write_text(
        "\n".join(
            [f"{csv_lines[0]},private_note"]
            + [
                f"{line},PII_SENTINEL_PERSON_OR_CUSTOMER"
                for line in csv_lines[1:]
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _rewrite_markers(
        marker_path,
        lambda document: document.update(
            {
                "capture_csv_sha256": hashlib.sha256(
                    csv_path.read_bytes()
                ).hexdigest()
            }
        ),
    )
    json_output = tmp_path / "analysis.json"
    markdown_output = tmp_path / "analysis.md"

    exit_code = main(
        [
            "--results-csv",
            str(csv_path),
            "--markers-json",
            str(marker_path),
            "--max-latency-seconds",
            "3",
            "--output-json",
            str(json_output),
            "--output-markdown",
            str(markdown_output),
        ]
    )
    assert exit_code == 0
    console = capsys.readouterr().out
    assert console == "status=PASS exit_code=0 outputs=json,markdown\n"
    assert str(tmp_path) not in console

    analysis = json.loads(json_output.read_text(encoding="utf-8"))
    markdown = markdown_output.read_text(encoding="utf-8")
    serialized = json.dumps(analysis)
    assert str(tmp_path) not in serialized
    assert "capture.csv" not in serialized
    assert "markers.json" not in serialized
    assert "transcript" not in serialized.lower()
    assert "PII_SENTINEL" not in serialized
    assert "Semantic source marker reviewed: true" in markdown
    assert "Source PCM binding verified: true" in markdown
    assert "Target-language landmark proven: false" in markdown
    assert "Actual physical audibility proven: false" in markdown
    assert str(tmp_path) not in markdown
    assert "PII_SENTINEL" not in markdown
    assert "event-001" in markdown
    assert "group-001" in markdown
    assert "800 / 800.000 ms" in markdown


def test_cli_staging_failure_exits_two_without_partial_reports(
    tmp_path,
    capsys,
):
    csv_path, marker_path = _write_case(tmp_path)
    json_output = tmp_path / "analysis.json"
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("keep", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--results-csv",
                str(csv_path),
                "--markers-json",
                str(marker_path),
                "--max-latency-seconds",
                "2.1",
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(blocked_parent / "analysis.md"),
            ]
        )

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not write complete report output set" in captured.err
    assert not json_output.exists()
    assert blocked_parent.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.bak"))


@pytest.mark.parametrize("preexisting_outputs", [False, True])
def test_cli_install_failure_rolls_back_complete_report_set(
    monkeypatch,
    tmp_path,
    capsys,
    preexisting_outputs,
):
    csv_path, marker_path = _write_case(tmp_path)
    json_output = tmp_path / "analysis.json"
    markdown_output = tmp_path / "analysis.md"
    if preexisting_outputs:
        json_output.write_text("old-json\n", encoding="utf-8")
        markdown_output.write_text("old-markdown\n", encoding="utf-8")

    real_replace = semantic_latency_module.os.replace
    failure_injected = False

    def fail_markdown_install_once(source, destination):
        nonlocal failure_injected
        if (
            not failure_injected
            and Path(source).suffix == ".tmp"
            and Path(destination) == markdown_output
        ):
            failure_injected = True
            raise OSError("synthetic report install failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        semantic_latency_module.os,
        "replace",
        fail_markdown_install_once,
    )

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--results-csv",
                str(csv_path),
                "--markers-json",
                str(marker_path),
                "--max-latency-seconds",
                "2.1",
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(markdown_output),
            ]
        )

    assert failure_injected is True
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not write complete report output set" in captured.err
    if preexisting_outputs:
        assert json_output.read_text(encoding="utf-8") == "old-json\n"
        assert (
            markdown_output.read_text(encoding="utf-8")
            == "old-markdown\n"
        )
    else:
        assert not json_output.exists()
        assert not markdown_output.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.bak"))


@pytest.mark.parametrize(
    ("maximum_latency", "expected_status", "expected_exit"),
    [
        ("2.5", "pass", 0),
        ("2.3", "inconclusive", 3),
        ("2.1", "fail", 1),
    ],
)
def test_cli_returns_distinct_gate_exit_codes_and_json(
    tmp_path,
    capsys,
    maximum_latency,
    expected_status,
    expected_exit,
):
    csv_path, marker_path = _write_case(tmp_path)

    exit_code = main(
        [
            "--results-csv",
            str(csv_path),
            "--markers-json",
            str(marker_path),
            "--max-latency-seconds",
            maximum_latency,
        ]
    )

    assert exit_code == expected_exit
    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["overall_status"] == expected_status


def test_script_entrypoint_propagates_inconclusive_exit_code(tmp_path):
    csv_path, marker_path = _write_case(tmp_path)
    script = Path(__file__).parents[1] / "analyze_semantic_event_latency.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--results-csv",
            str(csv_path),
            "--markers-json",
            str(marker_path),
            "--max-latency-seconds",
            "2.3",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 3
    assert json.loads(completed.stdout)["summary"]["overall_status"] == (
        "inconclusive"
    )


def test_cli_defaults_to_two_reviewers_and_rejects_output_collision(
    capsys,
):
    args = parse_cli_args(
        [
            "--results-csv",
            "capture.csv",
            "--markers-json",
            "markers.json",
            "--max-latency-seconds",
            "5",
        ]
    )
    assert args.minimum_reviewers == 2

    with pytest.raises(SystemExit) as exc_info:
        parse_cli_args(
            [
                "--results-csv",
                "capture.csv",
                "--markers-json",
                "markers.json",
                "--max-latency-seconds",
                "5",
                "--json-output",
                "same.out",
                "--markdown-output",
                "same.out",
            ]
        )
    assert exc_info.value.code == 2
    assert "must be different" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc_info:
        parse_cli_args(
            [
                "--results-csv",
                "capture.csv",
                "--markers-json",
                "markers.json",
                "--max-latency-seconds",
                "5",
                "--json-output",
                "capture.csv",
            ]
        )
    assert exc_info.value.code == 2
    assert "must not overwrite input evidence" in capsys.readouterr().err


@pytest.mark.parametrize("invalid", ["0", "-1", "nan", "inf"])
def test_cli_rejects_invalid_latency_threshold(capsys, invalid):
    with pytest.raises(SystemExit) as exc_info:
        parse_cli_args(
            [
                "--results-csv",
                "capture.csv",
                "--markers-json",
                "markers.json",
                "--max-latency-seconds",
                invalid,
            ]
        )
    assert exc_info.value.code == 2
    assert "finite and positive" in capsys.readouterr().err
