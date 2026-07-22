from dataclasses import replace

import pytest

from punctuation_segmenter import PunctuationSegmenter
from staged_models import AsrFinal, EmissionReason, PipelineEvent


def final(final_id, text, at=0, start=None, end=None):
    return AsrFinal(
        final_id=final_id,
        text=text,
        received_monotonic_ms=at,
        source_start_ms=start,
        source_end_ms=end,
    )


@pytest.mark.parametrize("terminal", [".", "?", "!", "。", "？", "！"])
def test_basic_terminal_boundaries_are_retained(terminal):
    segmenter = PunctuationSegmenter()

    emitted = segmenter.push_final(final(0, f"A complete thought{terminal}"))

    assert [item.text for item in emitted] == [f"A complete thought{terminal}"]
    assert emitted[0].reason is EmissionReason.PUNCTUATION


def test_multiple_sentences_get_independent_ordered_segment_ids():
    segmenter = PunctuationSegmenter()

    emitted = segmenter.push_final(final(7, "One. Two? Three!", at=100))

    assert [item.sequence_id for item in emitted] == [0, 1, 2]
    assert [item.text for item in emitted] == ["One.", "Two?", "Three!"]
    assert all(item.contributing_final_ids == (7,) for item in emitted)


def test_sentence_can_span_multiple_asr_finals():
    segmenter = PunctuationSegmenter()

    assert segmenter.push_final(final(0, "This spans", at=100, start=0, end=500)) == []
    emitted = segmenter.push_final(
        final(1, "finals.", at=250, start=500, end=1_000)
    )

    assert [item.text for item in emitted] == ["This spans finals."]
    assert emitted[0].contributing_final_ids == (0, 1)
    assert emitted[0].buffered_since_monotonic_ms == 100
    assert emitted[0].source_start_ms == 0
    assert emitted[0].source_end_ms == 1_000


def test_closing_straight_quote_gets_separator_across_finals():
    segmenter = PunctuationSegmenter()

    assert segmenter.push_final(final(0, 'He said "Hello"')) == []
    emitted = segmenter.push_final(final(1, "Then left."))

    assert [item.text for item in emitted] == ['He said "Hello" Then left.']


def test_opening_straight_quote_does_not_get_separator_across_finals():
    segmenter = PunctuationSegmenter()

    assert segmenter.push_final(final(0, 'He said "')) == []
    emitted = segmenter.push_final(final(1, 'Hello."'))

    assert [item.text for item in emitted] == ['He said "Hello."']


def test_leading_opening_straight_quote_gets_separator_across_finals():
    segmenter = PunctuationSegmenter()

    assert segmenter.push_final(final(0, "He said")) == []
    emitted = segmenter.push_final(final(1, '"Hello."'))

    assert [item.text for item in emitted] == ['He said "Hello."']


def test_leading_closing_straight_quote_joins_across_finals():
    segmenter = PunctuationSegmenter()

    assert segmenter.push_final(final(0, 'He said "Hello')) == []
    emitted = segmenter.push_final(final(1, '" Then left.'))

    assert [item.text for item in emitted] == ['He said "Hello" Then left.']


def test_leading_terminal_punctuation_joins_without_synthetic_space():
    segmenter = PunctuationSegmenter()
    segmenter.push_final(final(0, "This spans", at=0))

    emitted = segmenter.push_final(final(1, ". Next!", at=100))

    assert [item.text for item in emitted] == ["This spans.", "Next!"]


def test_unpunctuated_residual_flushes_exactly_once():
    segmenter = PunctuationSegmenter()
    segmenter.push_final(final(0, "Unfinished thought", at=10))

    emitted = segmenter.flush(20)

    assert len(emitted) == 1
    assert emitted[0].text == "Unfinished thought"
    assert emitted[0].reason is EmissionReason.FINAL_FLUSH
    assert segmenter.flush(30) == []
    assert segmenter.closed is True
    with pytest.raises(RuntimeError, match="after segmenter flush"):
        segmenter.push_final(final(1, "Too late.", at=40))


def test_empty_finals_are_noops_and_do_not_reset_age():
    segmenter = PunctuationSegmenter(max_age_ms=2_000)
    segmenter.push_final(final(0, "Still waiting", at=100))

    assert segmenter.push_final(final(1, "   ", at=2_099)) == []
    emitted = segmenter.emit_due(2_100)

    assert [item.text for item in emitted] == ["Still waiting"]
    assert emitted[0].reason is EmissionReason.AGE
    assert emitted[0].buffered_since_monotonic_ms == 100


@pytest.mark.parametrize(
    "text",
    [
        "The value is 3.14 and stable.",
        "Release v1.2 remains supported.",
        "Visit example.com tomorrow.",
        "Dr. Smith spoke clearly.",
        "The U.S. policy changed.",
        "Use e.g. a short illustration.",
    ],
)
def test_protected_periods_do_not_split_early(text):
    segmenter = PunctuationSegmenter()

    emitted = segmenter.push_final(final(0, text))

    assert [item.text for item in emitted] == [text]


def test_custom_abbreviation_is_honored():
    segmenter = PunctuationSegmenter(abbreviations=["Gen"])

    emitted = segmenter.push_final(final(0, "Gen. Smith arrived."))

    assert [item.text for item in emitted] == ["Gen. Smith arrived."]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Really?! Yes.", ["Really?!", "Yes."]),
        ("Wait... Continue.", ["Wait...", "Continue."]),
        ("Pause… Continue.", ["Pause…", "Continue."]),
        ("He said “Go!” Then left.", ["He said “Go!”", "Then left."]),
        ("¿Ready? ¡Go!", ["¿Ready?", "¡Go!"]),
    ],
)
def test_punctuation_runs_unicode_and_closers(text, expected):
    segmenter = PunctuationSegmenter()

    emitted = segmenter.push_final(final(0, text))

    assert [item.text for item in emitted] == expected


@pytest.mark.parametrize(
    "parts",
    [
        ("Really", "!Next sentence."),
        ("Stop!Go.",),
    ],
)
def test_question_and_exclamation_split_without_following_space(parts):
    segmenter = PunctuationSegmenter()
    emitted = []

    for final_id, text in enumerate(parts):
        emitted.extend(segmenter.push_final(final(final_id, text, at=final_id)))

    assert [item.text for item in emitted] == [
        "Really!" if len(parts) == 2 else "Stop!",
        "Next sentence." if len(parts) == 2 else "Go.",
    ]


def test_punctuation_at_exact_limit_wins_over_length():
    segmenter = PunctuationSegmenter(max_chars=10)

    emitted = segmenter.push_final(final(0, "123456789."))

    assert len(emitted) == 1
    assert emitted[0].reason is EmissionReason.PUNCTUATION


def test_length_cut_prefers_complete_word_and_preserves_residual():
    segmenter = PunctuationSegmenter(max_chars=10)

    emitted = segmenter.push_final(final(0, "alpha beta gamma"))

    assert [item.text for item in emitted] == ["alpha beta"]
    assert emitted[0].reason is EmissionReason.LENGTH
    assert segmenter.residual_text == "gamma"


def test_long_token_is_hard_cut_without_character_loss():
    text = "abcdefghijklmnopqrstuv"
    segmenter = PunctuationSegmenter(max_chars=5)

    emitted = segmenter.push_final(final(0, text))
    emitted.extend(segmenter.flush(1))

    assert "".join(item.text for item in emitted) == text
    assert [item.reason for item in emitted[:-1]] == [EmissionReason.LENGTH] * 4
    assert emitted[-1].reason is EmissionReason.FINAL_FLUSH


def test_length_precedes_punctuation_beyond_limit():
    segmenter = PunctuationSegmenter(max_chars=10)

    emitted = segmenter.push_final(final(0, "alpha beta sentence."))

    assert [item.text for item in emitted] == ["alpha beta", "sentence."]
    assert [item.reason for item in emitted] == [
        EmissionReason.LENGTH,
        EmissionReason.PUNCTUATION,
    ]


def test_age_fallback_fires_at_threshold_without_new_final():
    segmenter = PunctuationSegmenter(max_age_ms=2_000)
    segmenter.push_final(final(0, "No punctuation", at=500))

    assert segmenter.emit_due(2_499) == []
    emitted = segmenter.emit_due(2_500)

    assert len(emitted) == 1
    assert emitted[0].reason is EmissionReason.AGE


def test_punctuation_arriving_at_age_deadline_wins():
    segmenter = PunctuationSegmenter(max_age_ms=2_000)
    segmenter.push_final(final(0, "Deadline", at=0))

    emitted = segmenter.push_final(final(1, ".", at=2_000))

    assert [item.reason for item in emitted] == [EmissionReason.PUNCTUATION]
    assert [item.text for item in emitted] == ["Deadline."]


def test_equivalent_chunking_has_same_text_when_fallbacks_do_not_fire():
    single = PunctuationSegmenter()
    split = PunctuationSegmenter()

    single_output = single.push_final(final(0, "One sentence. Another one!"))
    split_output = []
    split_output.extend(split.push_final(final(0, "One", at=0)))
    split_output.extend(split.push_final(final(1, "sentence.", at=1)))
    split_output.extend(split.push_final(final(2, "Another one!", at=2)))

    assert [item.text for item in single_output] == [item.text for item in split_output]


def test_ids_time_and_source_ranges_are_validated():
    segmenter = PunctuationSegmenter()
    segmenter.push_final(final(2, "Buffered", at=100))

    with pytest.raises(ValueError, match="strictly increasing"):
        segmenter.push_final(final(2, "Duplicate", at=101))
    with pytest.raises(ValueError, match="backwards"):
        segmenter.emit_due(99)
    with pytest.raises(ValueError, match="cannot precede"):
        final(3, "Bad", at=102, start=10, end=5)


def test_inconsistent_partial_source_ranges_degrade_to_unknown():
    segmenter = PunctuationSegmenter()
    segmenter.push_final(final(0, "Earlier", at=0, end=100))

    emitted = segmenter.push_final(final(1, "later.", at=1, start=200))

    assert len(emitted) == 1
    assert emitted[0].source_start_ms is None
    assert emitted[0].source_end_ms is None


@pytest.mark.parametrize(
    ("pieces", "expected"),
    [
        (
            [(None, 100), (200, 300)],
            (None, 300),
        ),
        (
            [(0, 100), (200, None)],
            (0, None),
        ),
    ],
)
def test_partial_source_endpoints_are_not_fabricated(pieces, expected):
    segmenter = PunctuationSegmenter()
    segmenter.push_final(
        final(0, "Earlier", at=0, start=pieces[0][0], end=pieces[0][1])
    )

    emitted = segmenter.push_final(
        final(1, "later.", at=1, start=pieces[1][0], end=pieces[1][1])
    )

    assert (emitted[0].source_start_ms, emitted[0].source_end_ms) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_times_are_rejected(value):
    with pytest.raises(ValueError, match="finite"):
        final(0, "Invalid", at=value)
    with pytest.raises(ValueError, match="finite"):
        PunctuationSegmenter(max_age_ms=value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": 0},
        {"max_chars": 2.5},
        {"max_chars": float("nan")},
        {"max_chars": float("inf")},
        {"max_chars": True},
        {"max_age_ms": 0},
        {"abbreviations": [" "]},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        PunctuationSegmenter(**kwargs)


def test_pipeline_event_contract_serializes_without_proto_dependencies():
    event = PipelineEvent(
        session_id="session-1",
        sequence_id=3,
        asr_final_id=7,
        contributing_final_ids=(7, 8),
        emission_reason=EmissionReason.PUNCTUATION,
        stage="segmenter",
        event="segment_emitted",
        monotonic_ms=42.5,
        queue_depth=1,
        text_chars=18,
    )

    assert event.to_dict()["sequence_id"] == 3
    assert event.to_dict()["asr_final_id"] == 7
    assert event.to_dict()["contributing_final_ids"] == (7, 8)
    assert event.to_dict()["emission_reason"] == "punctuation"
    assert replace(event, event="nmt_enqueued").event == "nmt_enqueued"

    with pytest.raises(ValueError, match="EmissionReason"):
        replace(event, emission_reason="punctuation")


def test_embedded_alphanumeric_period_policy_is_conservative():
    segmenter = PunctuationSegmenter()

    emitted = segmenter.push_final(final(0, "Sentence.Next one."))

    assert [item.text for item in emitted] == ["Sentence.Next one."]
