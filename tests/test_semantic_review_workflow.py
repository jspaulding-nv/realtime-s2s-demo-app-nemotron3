import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import semantic_review_workflow as workflow


def _load_capture_support():
    path = Path(__file__).with_name(
        "test_analyze_scheduled_semantic_delay.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_semantic_review_capture_support",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CAPTURE_SUPPORT = _load_capture_support()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def capture():
    root = Path(workflow.__file__).resolve().parent
    directory = (
        root
        / "experiment_results"
        / f"semantic-review-test-{uuid.uuid4().hex}"
    )
    directory.mkdir(mode=0o700)
    bundle = CAPTURE_SUPPORT.build_bundle(directory)
    bundle["markers_path"].unlink()
    for name in (
        workflow.LEDGER_FILENAME,
        workflow.SOURCE_WAV_FILENAME,
        workflow.TRANSLATED_WAV_FILENAME,
    ):
        (directory / name).chmod(0o600)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def _core_hashes(capture: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((capture / name).read_bytes()).hexdigest()
        for name in (
            workflow.LEDGER_FILENAME,
            workflow.SOURCE_WAV_FILENAME,
            workflow.TRANSLATED_WAV_FILENAME,
        )
    }


def _prepare(capture: Path) -> dict:
    result = workflow.prepare_review(
        capture,
        default_three_windows=True,
    )
    return result.document


def _observation(
    capture: Path,
    *,
    session: str,
    delta: int = 0,
    accepted: bool = True,
    source_confidence: int = 4,
    translated_confidence: int = 4,
) -> dict:
    assignment_bytes = (
        capture / workflow.ASSIGNMENT_FILENAME
    ).read_bytes()
    assignment = json.loads(assignment_bytes)
    values = (
        (3_000 + delta, 4_000 + delta),
        (8_000 + delta, 18_000 + delta),
        (13_000 + delta, 24_000 + delta),
    )
    return {
        "schema_version": 1,
        "observation_type": workflow.OBSERVATION_TYPE,
        "assignment_sha256": hashlib.sha256(
            assignment_bytes
        ).hexdigest(),
        "schedule_ledger_sha256": assignment[
            "schedule_ledger_sha256"
        ],
        "source_pcm_sha256": assignment["source_pcm_sha256"],
        "source_pcm_sample_count": assignment[
            "source_pcm_sample_count"
        ],
        "translated_pcm_sha256": assignment[
            "translated_pcm_sha256"
        ],
        "translated_pcm_sample_count": assignment[
            "translated_pcm_sample_count"
        ],
        "reviewer_session_id": session,
        "independence_attestation": True,
        "events": [
            {
                "event_id": f"event-{index:03d}",
                "source_sample_index": source,
                "translated_sample_index": translated,
                "semantic_equivalence": (
                    "accepted" if accepted else "uncertain"
                ),
                "source_boundary_confidence": source_confidence,
                "translated_boundary_confidence": (
                    translated_confidence
                ),
                "translation_quality_rating": 4,
                "intelligibility_rating": 5,
                "naturalness_rating": 4,
                "issue_flags": [],
            }
            for index, (source, translated) in enumerate(values, 1)
        ],
        "privacy": dict(workflow.OBSERVATION_PRIVACY),
    }


def _write_private_json(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _two_reviews(capture: Path, **second_options) -> tuple[Path, Path]:
    first = capture / "first-review.json"
    second = capture / "second-review.json"
    _write_private_json(
        first,
        _observation(
            capture,
            session="12345678-1234-4234-9234-123456789abc",
        ),
    )
    _write_private_json(
        second,
        _observation(
            capture,
            session="abcdefab-cdef-4abc-8def-abcdefabcdef",
            delta=10,
            **second_options,
        ),
    )
    return first, second


CANONICALS = (
    "event-001:3005:4005",
    "event-002:8005:18005",
    "event-003:13005:24005",
)

FOUR_EVENT_CANONICALS = (
    "event-001:1605:4005",
    "event-002:4805:10005",
    "event-003:8005:18005",
    "event-004:11205:26005",
)


def _four_event_reviews(
    capture: Path,
    *,
    fourth_accepted: bool = True,
) -> tuple[Path, Path]:
    workflow.prepare_review(
        capture,
        event_windows=(
            "event-001:0.05:0.15",
            "event-002:0.25:0.35",
            "event-003:0.45:0.55",
            "event-004:0.65:0.75",
        ),
    )
    sample_pairs = (
        (1_600, 4_000),
        (4_800, 10_000),
        (8_000, 18_000),
        (11_200, 26_000),
    )
    reviews = []
    for filename, session, delta in (
        (
            "first-review.json",
            "12345678-1234-4234-9234-123456789abc",
            0,
        ),
        (
            "second-review.json",
            "abcdefab-cdef-4abc-8def-abcdefabcdef",
            10,
        ),
    ):
        document = _observation(
            capture,
            session=session,
            delta=delta,
        )
        template = document["events"][0]
        document["events"] = [
            {
                **template,
                "event_id": f"event-{index:03d}",
                "source_sample_index": source + delta,
                "translated_sample_index": translated + delta,
                "semantic_equivalence": (
                    "accepted"
                    if index < 4 or fourth_accepted
                    else "uncertain"
                ),
            }
            for index, (source, translated) in enumerate(sample_pairs, 1)
        ]
        path = capture / filename
        _write_private_json(path, document)
        reviews.append(path)
    return reviews[0], reviews[1]


def _reconcile(capture: Path) -> tuple[Path, Path]:
    first, second = _two_reviews(capture)
    workflow.reconcile_reviews(
        capture,
        (first, second),
        CANONICALS,
    )
    return first, second


def test_prepare_exact_schema_private_modes_and_unchanged_core(capture):
    before = _core_hashes(capture)
    assignment = _prepare(capture)

    assert set(assignment) == set(workflow.ASSIGNMENT_KEYS)
    assert assignment["assignment_type"] == workflow.ASSIGNMENT_TYPE
    assert assignment["landmark_rule"] == workflow.LANDMARK_RULE
    assert assignment["source_sample_rate_hz"] == 16_000
    assert assignment["translated_sample_rate_hz"] == 16_000
    assert assignment["privacy"] == workflow.ASSIGNMENT_PRIVACY
    assert assignment["events"] == [
        {
            "event_id": "event-001",
            "source_window_start_sample": 1_600,
            "source_window_end_sample_exclusive": 4_800,
        },
        {
            "event_id": "event-002",
            "source_window_start_sample": 6_400,
            "source_window_end_sample_exclusive": 9_600,
        },
        {
            "event_id": "event-003",
            "source_window_start_sample": 11_200,
            "source_window_end_sample_exclusive": 14_400,
        },
    ]
    assert _mode(capture) == 0o700
    assert _mode(capture / workflow.ASSIGNMENT_FILENAME) == 0o600
    assert _mode(capture / workflow.ASSISTANT_FILENAME) == 0o600
    assert (
        capture / workflow.ASSISTANT_FILENAME
    ).read_bytes() == Path(workflow.__file__).with_name(
        workflow.ASSISTANT_SOURCE_FILENAME
    ).read_bytes()
    assert _core_hashes(capture) == before


def test_prepare_custom_windows_convert_exact_seconds(capture):
    assignment = workflow.prepare_review(
        capture,
        event_windows=(
            "event-003:0.1:0.2",
            "event-007:0.3:0.4",
            "event-010:0.5:0.6",
        ),
    ).document

    assert assignment["events"] == [
        {
            "event_id": "event-003",
            "source_window_start_sample": 1_600,
            "source_window_end_sample_exclusive": 3_200,
        },
        {
            "event_id": "event-007",
            "source_window_start_sample": 4_800,
            "source_window_end_sample_exclusive": 6_400,
        },
        {
            "event_id": "event-010",
            "source_window_start_sample": 8_000,
            "source_window_end_sample_exclusive": 9_600,
        },
    ]


@pytest.mark.parametrize(
    "windows",
    [
        ("event-001:0.1:0.2", "event-002:0.3:0.4"),
        (
            "event-001:0.1:0.4",
            "event-002:0.3:0.5",
            "event-003:0.6:0.7",
        ),
        (
            "event-002:0.1:0.2",
            "event-001:0.3:0.4",
            "event-003:0.5:0.6",
        ),
        (
            "event-001:NaN:0.2",
            "event-002:0.3:0.4",
            "event-003:0.5:0.6",
        ),
        (
            "event-001:0.0000625000000000000000000000000000000000000000000000000001:0.2",
            "event-002:0.3:0.4",
            "event-003:0.5:0.6",
        ),
    ],
)
def test_prepare_rejects_invalid_event_windows(capture, windows):
    with pytest.raises(workflow.SemanticReviewWorkflowError):
        workflow.prepare_review(capture, event_windows=windows)
    assert not (capture / workflow.ASSIGNMENT_FILENAME).exists()
    assert not (capture / workflow.ASSISTANT_FILENAME).exists()


def test_prepare_requires_git_ignored_capture(monkeypatch, capture):
    monkeypatch.setattr(
        workflow.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="ignored by Git",
    ):
        workflow.prepare_review(
            capture,
            default_three_windows=True,
        )


def test_prepare_rejects_unsafe_mode_and_no_clobber(capture):
    source = capture / workflow.SOURCE_WAV_FILENAME
    source.chmod(0o644)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="0600",
    ):
        workflow.prepare_review(
            capture,
            default_three_windows=True,
        )
    source.chmod(0o600)
    existing = capture / workflow.ASSIGNMENT_FILENAME
    existing.write_text("do not replace", encoding="utf-8")
    existing.chmod(0o600)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="overwrite",
    ):
        workflow.prepare_review(
            capture,
            default_three_windows=True,
        )
    assert existing.read_text(encoding="utf-8") == "do not replace"
    assert not (capture / workflow.ASSISTANT_FILENAME).exists()


@pytest.mark.parametrize("failing_write", (1, 2))
def test_exclusive_output_group_rolls_back_partial_current_file(
    monkeypatch,
    capture,
    failing_write,
):
    bundle = workflow.validate_capture_bundle(capture)
    real_write = workflow.os.write
    calls = 0

    def injected_write(descriptor, payload):
        nonlocal calls
        calls += 1
        if calls == failing_write:
            raise OSError("injected write failure")
        return real_write(descriptor, payload)

    monkeypatch.setattr(workflow.os, "write", injected_write)
    with pytest.raises(OSError, match="injected write failure"):
        workflow._publish_exclusive(
            bundle,
            {
                "first-private-output.json": b"first\n",
                "second-private-output.json": b"second\n",
            },
        )
    assert not (capture / "first-private-output.json").exists()
    assert not (capture / "second-private-output.json").exists()


def test_compare_is_anonymous_and_reports_deltas_and_ratings(capture):
    _prepare(capture)
    first, second = _two_reviews(capture)

    comparison = workflow.compare_reviews(capture, (first, second))
    serialized = json.dumps(comparison, sort_keys=True)

    assert comparison["independent_reviewer_count"] == 2
    assert comparison["events"][0]["marker_deltas"] == [
        {
            "left_review": "review-1",
            "right_review": "review-2",
            "source_sample_delta_right_minus_left": 10,
            "translated_sample_delta_right_minus_left": 10,
        }
    ]
    assert comparison["events"][0]["reviews"][0][
        "translation_quality_rating"
    ] == 4
    assert str(capture) not in serialized
    assert "12345678-1234-4234-9234-123456789abc" not in serialized
    assert "abcdefab-cdef-4abc-8def-abcdefabcdef" not in serialized


def test_compare_rejects_review_drift_before_return(
    monkeypatch,
    capture,
):
    _prepare(capture)
    first, second = _two_reviews(capture)
    real_load = workflow._load_observations
    calls = 0

    def drifting_load(*args, **kwargs):
        nonlocal calls
        observations = real_load(*args, **kwargs)
        calls += 1
        if calls == 1:
            first.write_bytes(first.read_bytes() + b"\n")
            first.chmod(0o600)
        return observations

    monkeypatch.setattr(workflow, "_load_observations", drifting_load)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="changed during comparison",
    ):
        workflow.compare_reviews(capture, (first, second))


def test_compare_rejects_duplicate_session_bool_and_duplicate_key(capture):
    _prepare(capture)
    first, second = _two_reviews(capture)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="distinct independent",
    ):
        workflow.compare_reviews(capture, (first, first))

    document = json.loads(second.read_text(encoding="utf-8"))
    document["events"][0]["translation_quality_rating"] = True
    _write_private_json(second, document)
    with pytest.raises(workflow.SemanticReviewWorkflowError):
        workflow.compare_reviews(capture, (first, second))

    document["events"][0]["translation_quality_rating"] = 4
    payload = json.dumps(document, sort_keys=True)
    payload = payload.replace(
        '"schema_version": 1',
        '"schema_version": 1, "schema_version": 1',
        1,
    )
    second.write_text(payload, encoding="utf-8")
    second.chmod(0o600)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="duplicate object key",
    ):
        workflow.compare_reviews(capture, (first, second))


@pytest.mark.parametrize(
    ("semantic_equivalence", "translated_confidence"),
    (
        ("accepted", 1),
        ("uncertain", 1),
        ("rejected", 2),
    ),
)
def test_compare_rejects_invalid_omission_cross_field_contract(
    capture,
    semantic_equivalence,
    translated_confidence,
):
    _prepare(capture)
    first, second = _two_reviews(capture)
    document = json.loads(first.read_text(encoding="utf-8"))
    event = document["events"][0]
    event["semantic_equivalence"] = semantic_equivalence
    event["translated_boundary_confidence"] = translated_confidence
    event["issue_flags"] = ["omission"]
    _write_private_json(first, document)

    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="omission must be rejected with translated boundary confidence 1",
    ):
        workflow.compare_reviews(capture, (first, second))


def test_omission_diagnostic_targets_may_duplicate_or_reverse_but_are_ineligible(
    capture,
):
    _prepare(capture)
    first, second = _two_reviews(capture)
    diagnostic_targets = (
        (24_000, 24_000, 4_000),
        (23_990, 23_990, 3_990),
    )
    for path, targets in zip((first, second), diagnostic_targets):
        document = json.loads(path.read_text(encoding="utf-8"))
        for event, translated in zip(document["events"], targets):
            event["translated_sample_index"] = translated
            event["semantic_equivalence"] = "rejected"
            event["translated_boundary_confidence"] = 1
            event["issue_flags"] = ["omission"]
        _write_private_json(path, document)

    comparison = workflow.compare_reviews(capture, (first, second))
    assert [
        event["reviews"][0]["translated_sample_index"]
        for event in comparison["events"]
    ] == [24_000, 24_000, 4_000]

    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="lacks two accepted high-confidence reviewers",
    ):
        workflow.reconcile_reviews(
            capture,
            (first, second),
            CANONICALS,
        )


def test_compare_rejects_same_session_or_payload_across_distinct_files(
    capture,
):
    _prepare(capture)
    first, second = _two_reviews(capture)
    first_document = json.loads(first.read_text(encoding="utf-8"))
    second_document = json.loads(second.read_text(encoding="utf-8"))
    second_document["reviewer_session_id"] = first_document[
        "reviewer_session_id"
    ]
    _write_private_json(second, second_document)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="distinct independent sessions",
    ):
        workflow.compare_reviews(capture, (first, second))

    second.write_bytes(first.read_bytes())
    second.chmod(0o600)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="distinct independent sessions",
    ):
        workflow.compare_reviews(capture, (first, second))


def test_cli_comparison_and_reconciliation_do_not_print_paths_or_uuids(
    capsys,
    capture,
):
    _prepare(capture)
    first, second = _two_reviews(capture)
    common = [
        "--capture-dir",
        str(capture),
        "--review",
        first.name,
        "--review",
        second.name,
    ]
    assert workflow.main(["compare", *common]) == 0
    compared = capsys.readouterr()
    assert str(capture) not in compared.out + compared.err
    assert "12345678-1234-4234-9234-123456789abc" not in (
        compared.out + compared.err
    )
    assert "abcdefab-cdef-4abc-8def-abcdefabcdef" not in (
        compared.out + compared.err
    )

    canonical_args = [
        item
        for canonical in CANONICALS
        for item in ("--canonical", canonical)
    ]
    assert workflow.main(
        ["reconcile", *common, *canonical_args]
    ) == 0
    reconciled = capsys.readouterr()
    assert str(capture) not in reconciled.out + reconciled.err
    assert "12345678-1234-4234-9234-123456789abc" not in (
        reconciled.out + reconciled.err
    )
    assert "abcdefab-cdef-4abc-8def-abcdefabcdef" not in (
        reconciled.out + reconciled.err
    )


def test_reconcile_writes_exact_markers_and_private_aggregate(capture):
    before = _core_hashes(capture)
    _prepare(capture)
    first, second = _two_reviews(capture)

    markers, feedback = workflow.reconcile_reviews(
        capture,
        (first, second),
        CANONICALS,
    )

    assert set(markers) == set(workflow.MARKER_DOCUMENT_KEYS)
    assert all(
        set(event) == set(workflow.MARKER_EVENT_KEYS)
        for event in markers["events"]
    )
    assert markers["events"][0] == {
        "event_id": "event-001",
        "source_sample_index": 3_005,
        "translated_sample_index": 4_005,
        "source_independent_reviewer_count": 2,
        "translated_independent_reviewer_count": 2,
    }
    serialized = json.dumps(feedback, sort_keys=True)
    assert feedback["independent_reviewer_count"] == 2
    assert feedback["privacy"] == workflow.OBSERVATION_PRIVACY
    assert "reviewer_session_id" not in serialized
    assert "12345678-1234-4234-9234-123456789abc" not in serialized
    assert str(capture) not in serialized
    assert _mode(capture / workflow.REVIEWER_MARKERS_FILENAME) == 0o600
    assert _mode(capture / workflow.REVIEWER_FEEDBACK_FILENAME) == 0o600
    assert _core_hashes(capture) == before


def test_reconcile_rejects_omitted_unresolved_assigned_event(capture):
    first, second = _four_event_reviews(
        capture,
        fourth_accepted=False,
    )

    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="explicit canonical is required for every assigned event",
    ):
        workflow.reconcile_reviews(
            capture,
            (first, second),
            FOUR_EVENT_CANONICALS[:-1],
        )

    assert not (capture / workflow.REVIEWER_MARKERS_FILENAME).exists()
    assert not (capture / workflow.REVIEWER_FEEDBACK_FILENAME).exists()


def test_reconcile_accepts_canonical_for_every_assigned_event(capture):
    first, second = _four_event_reviews(capture)

    markers, feedback = workflow.reconcile_reviews(
        capture,
        (first, second),
        FOUR_EVENT_CANONICALS,
    )

    expected_ids = [f"event-{index:03d}" for index in range(1, 5)]
    assert [event["event_id"] for event in markers["events"]] == expected_ids
    assert [event["event_id"] for event in feedback["events"]] == expected_ids


def test_reconcile_counts_source_and_translated_eligibility_independently(
    capture,
):
    _prepare(capture)
    reviews = []
    configurations = (
        (
            "12345678-1234-4234-9234-123456789abc",
            0,
            4,
            2,
        ),
        (
            "abcdefab-cdef-4abc-8def-abcdefabcdef",
            10,
            4,
            4,
        ),
        (
            "fedcbafe-dcba-4fed-9cba-fedcbafedcba",
            20,
            2,
            4,
        ),
    )
    for index, (
        session,
        delta,
        source_confidence,
        translated_confidence,
    ) in enumerate(configurations, 1):
        path = capture / f"review-{index}.json"
        _write_private_json(
            path,
            _observation(
                capture,
                session=session,
                delta=delta,
                source_confidence=source_confidence,
                translated_confidence=translated_confidence,
            ),
        )
        reviews.append(path)

    markers, feedback = workflow.reconcile_reviews(
        capture,
        reviews,
        (
            "event-001:3005:4015",
            "event-002:8005:18015",
            "event-003:13005:24015",
        ),
    )
    assert all(
        event["source_independent_reviewer_count"] == 2
        and event["translated_independent_reviewer_count"] == 2
        for event in markers["events"]
    )
    assert feedback["events"][0]["eligible_source_sample_range"] == {
        "minimum": 3_000,
        "maximum": 3_010,
    }
    assert feedback["events"][0][
        "eligible_translated_sample_range"
    ] == {
        "minimum": 4_010,
        "maximum": 4_020,
    }


@pytest.mark.parametrize(
    ("second_options", "canonicals"),
    [
        (
            {"source_confidence": 2},
            CANONICALS,
        ),
        (
            {},
            (
                "event-001:3011:4005",
                "event-002:8005:18005",
                "event-003:13005:24005",
            ),
        ),
    ],
)
def test_reconcile_rejects_insufficient_support_or_outside_range(
    capture,
    second_options,
    canonicals,
):
    _prepare(capture)
    first, second = _two_reviews(capture, **second_options)
    with pytest.raises(workflow.SemanticReviewWorkflowError):
        workflow.reconcile_reviews(
            capture,
            (first, second),
            canonicals,
        )
    assert not (capture / workflow.REVIEWER_MARKERS_FILENAME).exists()
    assert not (capture / workflow.REVIEWER_FEEDBACK_FILENAME).exists()


def test_reconcile_rejects_reversed_translated_canonical_events(capture):
    _prepare(capture)
    first, second = _two_reviews(capture)
    for offset, path in enumerate((first, second)):
        document = json.loads(path.read_text(encoding="utf-8"))
        for event, translated in zip(
            document["events"],
            (24_000 + offset * 10, 18_000 + offset * 10, 4_000 + offset * 10),
        ):
            event["translated_sample_index"] = translated
        _write_private_json(path, document)

    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="ordered",
    ):
        workflow.reconcile_reviews(
            capture,
            (first, second),
            (
                "event-001:3005:24005",
                "event-002:8005:18005",
                "event-003:13005:4005",
            ),
        )
    assert not (capture / workflow.REVIEWER_MARKERS_FILENAME).exists()
    assert not (capture / workflow.REVIEWER_FEEDBACK_FILENAME).exists()


def test_feedback_rejects_impossible_eligibility_intersection(capture):
    _prepare(capture)
    _reconcile(capture)
    feedback_path = capture / workflow.REVIEWER_FEEDBACK_FILENAME
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    feedback["independent_reviewer_count"] = 3
    for event in feedback["events"]:
        event["semantic_equivalence_counts"] = {
            "accepted": 3,
            "uncertain": 0,
            "rejected": 0,
        }
        for key, score in (
            ("source_boundary_confidence_counts", "4"),
            ("translated_boundary_confidence_counts", "4"),
            ("translation_quality_rating_counts", "4"),
            ("intelligibility_rating_counts", "5"),
            ("naturalness_rating_counts", "4"),
        ):
            event[key] = {
                str(value): 3 if str(value) == score else 0
                for value in range(1, 6)
            }
    _write_private_json(feedback_path, feedback)

    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="possible accepted high-confidence intersection",
    ):
        workflow.analyze_review(capture)


def test_feedback_reviewer_count_is_bounded(capture):
    _prepare(capture)
    _reconcile(capture)
    feedback_path = capture / workflow.REVIEWER_FEEDBACK_FILENAME
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    feedback["independent_reviewer_count"] = 101
    _write_private_json(feedback_path, feedback)

    with pytest.raises(workflow.SemanticReviewWorkflowError):
        workflow.analyze_review(capture)


@pytest.mark.parametrize("mutation", ("extra-key", "nan", "bool"))
def test_feedback_schema_and_numeric_mutations_fail_before_reports(
    capture,
    mutation,
):
    _prepare(capture)
    _reconcile(capture)
    feedback_path = capture / workflow.REVIEWER_FEEDBACK_FILENAME
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    if mutation == "extra-key":
        feedback["unexpected"] = False
    elif mutation == "nan":
        feedback["independent_reviewer_count"] = float("nan")
    else:
        feedback["events"][0]["semantic_equivalence_counts"][
            "accepted"
        ] = True
    _write_private_json(feedback_path, feedback)

    with pytest.raises(workflow.SemanticReviewWorkflowError):
        workflow.analyze_review(capture)
    for name in (
        workflow.FIVE_SECOND_JSON_FILENAME,
        workflow.FIVE_SECOND_MARKDOWN_FILENAME,
        workflow.TEN_SECOND_JSON_FILENAME,
        workflow.TEN_SECOND_MARKDOWN_FILENAME,
    ):
        assert not (capture / name).exists()


def test_analyze_runs_both_gates_and_publishes_private_reports(capture):
    before = _core_hashes(capture)
    _prepare(capture)
    _reconcile(capture)

    outcome, code = workflow.analyze_review(capture)

    assert code == 0
    assert outcome["statuses"] == {
        "5_seconds": "pass",
        "10_seconds": "pass",
    }
    for name in (
        workflow.FIVE_SECOND_JSON_FILENAME,
        workflow.FIVE_SECOND_MARKDOWN_FILENAME,
        workflow.TEN_SECOND_JSON_FILENAME,
        workflow.TEN_SECOND_MARKDOWN_FILENAME,
    ):
        assert (capture / name).is_file()
        assert _mode(capture / name) == 0o600
    assert json.loads(
        (capture / workflow.FIVE_SECOND_JSON_FILENAME).read_text(
            encoding="utf-8"
        )
    )["maximum_latency_seconds"] == 5.0
    assert json.loads(
        (capture / workflow.TEN_SECOND_JSON_FILENAME).read_text(
            encoding="utf-8"
        )
    )["maximum_latency_seconds"] == 10.0
    assert _core_hashes(capture) == before


def test_analyze_returns_fail_as_worst_gate(monkeypatch, capture):
    _prepare(capture)
    _reconcile(capture)
    marker_payload = (
        capture / workflow.REVIEWER_MARKERS_FILENAME
    ).read_bytes()
    ledger_payload = (capture / workflow.LEDGER_FILENAME).read_bytes()
    statuses = iter(("inconclusive", "fail"))

    def fake_analyze(*_args, **_kwargs):
        return {
            "evidence": {
                "schedule_ledger_sha256": hashlib.sha256(
                    ledger_payload
                ).hexdigest(),
                "reviewer_markers_sha256": hashlib.sha256(
                    marker_payload
                ).hexdigest(),
            },
            "summary": {"overall_status": next(statuses)},
        }

    monkeypatch.setattr(
        workflow,
        "analyze_scheduled_semantic_delay",
        fake_analyze,
    )
    monkeypatch.setattr(
        workflow,
        "render_scheduled_semantic_delay_markdown",
        lambda result: result["summary"]["overall_status"] + "\n",
    )
    _outcome, code = workflow.analyze_review(capture)
    assert code == 1


def test_analyze_returns_inconclusive_when_no_gate_fails(
    monkeypatch,
    capture,
):
    _prepare(capture)
    _reconcile(capture)
    marker_payload = (
        capture / workflow.REVIEWER_MARKERS_FILENAME
    ).read_bytes()
    ledger_payload = (capture / workflow.LEDGER_FILENAME).read_bytes()
    statuses = iter(("inconclusive", "pass"))

    def fake_analyze(*_args, **_kwargs):
        return {
            "evidence": {
                "schedule_ledger_sha256": hashlib.sha256(
                    ledger_payload
                ).hexdigest(),
                "reviewer_markers_sha256": hashlib.sha256(
                    marker_payload
                ).hexdigest(),
            },
            "summary": {"overall_status": next(statuses)},
        }

    monkeypatch.setattr(
        workflow,
        "analyze_scheduled_semantic_delay",
        fake_analyze,
    )
    monkeypatch.setattr(
        workflow,
        "render_scheduled_semantic_delay_markdown",
        lambda result: result["summary"]["overall_status"] + "\n",
    )
    outcome, code = workflow.analyze_review(capture)
    assert outcome["statuses"] == {
        "5_seconds": "inconclusive",
        "10_seconds": "pass",
    }
    assert code == 3


def test_analyze_rejects_unknown_status_before_publishing(
    monkeypatch,
    capture,
):
    _prepare(capture)
    _reconcile(capture)
    marker_payload = (
        capture / workflow.REVIEWER_MARKERS_FILENAME
    ).read_bytes()
    ledger_payload = (capture / workflow.LEDGER_FILENAME).read_bytes()

    def fake_analyze(*_args, **_kwargs):
        return {
            "evidence": {
                "schedule_ledger_sha256": hashlib.sha256(
                    ledger_payload
                ).hexdigest(),
                "reviewer_markers_sha256": hashlib.sha256(
                    marker_payload
                ).hexdigest(),
            },
            "summary": {"overall_status": "unknown"},
        }

    monkeypatch.setattr(
        workflow,
        "analyze_scheduled_semantic_delay",
        fake_analyze,
    )
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="invalid gate status",
    ):
        workflow.analyze_review(capture)
    for name in (
        workflow.FIVE_SECOND_JSON_FILENAME,
        workflow.FIVE_SECOND_MARKDOWN_FILENAME,
        workflow.TEN_SECOND_JSON_FILENAME,
        workflow.TEN_SECOND_MARKDOWN_FILENAME,
    ):
        assert not (capture / name).exists()


def test_rejects_assignment_schema_drift_and_symlink_review(capture):
    _prepare(capture)
    assignment_path = capture / workflow.ASSIGNMENT_FILENAME
    assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
    assignment["unexpected"] = False
    _write_private_json(assignment_path, assignment)
    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="exact schema",
    ):
        workflow.compare_reviews(capture, ("one.json", "two.json"))

    assignment.pop("unexpected")
    _write_private_json(assignment_path, assignment)
    target = capture / "outside-review.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    link = capture / "review-link.json"
    link.symlink_to(target.name)
    with pytest.raises(workflow.SemanticReviewWorkflowError):
        workflow.compare_reviews(capture, (link, target))


def test_compare_rejects_hardlinked_review_artifact(capture):
    _prepare(capture)
    first, second = _two_reviews(capture)
    linked = capture / "linked-review.json"
    os.link(first, linked)

    with pytest.raises(
        workflow.SemanticReviewWorkflowError,
        match="single-link",
    ):
        workflow.compare_reviews(capture, (first, second))


def test_outside_review_path_rejection_does_not_leak_file_descriptors(
    capture,
):
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("file-descriptor inventory is unavailable")
    before = len(tuple(descriptor_root.iterdir()))
    outside = capture.parent / "outside-private-review.json"
    for _ in range(25):
        with pytest.raises(
            workflow.SemanticReviewWorkflowError,
            match="inside the private capture",
        ):
            workflow._read_capture_file(
                capture,
                outside,
                maximum_bytes=workflow.MAX_JSON_BYTES,
            )
    after = len(tuple(descriptor_root.iterdir()))
    assert after <= before + 1
