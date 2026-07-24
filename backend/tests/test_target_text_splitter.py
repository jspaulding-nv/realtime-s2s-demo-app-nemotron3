import unicodedata

import pytest

from target_text_splitter import (
    TargetTextChunk,
    TargetTextSplitError,
    normalize_target_text,
    reconstruct_target_text,
    split_target_text,
)


def texts(chunks):
    return [chunk.text for chunk in chunks]


def assert_split_contract(source, chunks, max_chars):
    assert chunks
    assert reconstruct_target_text(chunks) == normalize_target_text(source)
    assert chunks[0].separator_before == ""
    assert all(chunk.text == chunk.text.strip() for chunk in chunks)
    assert all(0 < len(chunk.text) <= max_chars for chunk in chunks)
    assert all(any(character.isalnum() for character in chunk.text) for chunk in chunks)


@pytest.mark.parametrize("max_chars", [0, -1, True, 2.5, "40"])
def test_max_chars_must_be_a_positive_integer(max_chars):
    with pytest.raises(ValueError, match="max_chars"):
        split_target_text("Texto válido.", max_chars=max_chars)


@pytest.mark.parametrize("min_chars", [0, -1, True, 2.5, "12"])
def test_min_chars_must_be_a_positive_integer(min_chars):
    with pytest.raises(ValueError, match="min_chars"):
        split_target_text("Texto válido.", max_chars=40, min_chars=min_chars)


def test_non_string_input_is_rejected():
    with pytest.raises(TypeError, match="string"):
        split_target_text(None, max_chars=40)


@pytest.mark.parametrize("source", ["", " \n\t ", "...", "¿?!", "— ; ,"])
def test_empty_and_punctuation_only_parents_are_rejected(source):
    with pytest.raises(TargetTextSplitError):
        split_target_text(source, max_chars=40)


def test_nfc_and_unicode_whitespace_normalization_is_lossless():
    source = " \t¿Co\u0301mo?\n\nMuy\u2003  bien. "

    chunks = split_target_text(source, max_chars=8)

    assert normalize_target_text(source) == "¿Cómo? Muy bien."
    assert texts(chunks) == ["¿Cómo?", "Muy", "bien."]
    assert [chunk.separator_before for chunk in chunks] == ["", " ", " "]
    assert reconstruct_target_text(chunks) == "¿Cómo? Muy bien."
    assert unicodedata.is_normalized("NFC", reconstruct_target_text(chunks))


def test_sentence_boundary_is_preferred_over_later_soft_boundary():
    source = "Primera. segunda, tercera parte considerable."

    chunks = split_target_text(source, max_chars=20, min_chars=1)

    assert texts(chunks)[0] == "Primera."
    assert texts(chunks)[1] == "segunda,"
    assert_split_contract(source, chunks, 20)


def test_soft_boundary_is_preferred_over_later_whitespace():
    source = "uno dos, tres cuatro cinco seis"

    chunks = split_target_text(source, max_chars=18, min_chars=1)

    assert texts(chunks)[0] == "uno dos,"
    assert_split_contract(source, chunks, 18)


def test_whitespace_boundary_is_preferred_over_hard_boundary():
    source = "abcdefgh ijklmnopqrstuv"

    chunks = split_target_text(source, max_chars=12, min_chars=1)

    assert texts(chunks)[0] == "abcdefgh"
    assert chunks[1].separator_before == " "
    assert_split_contract(source, chunks, 12)


def test_spanish_inverted_punctuation_stays_with_its_clause():
    source = "¿Cómo estás? ¡Muy bien! Seguimos ahora."

    chunks = split_target_text(source, max_chars=16, min_chars=1)

    assert texts(chunks) == ["¿Cómo estás?", "¡Muy bien!", "Seguimos ahora."]
    assert_split_contract(source, chunks, 16)


def test_terminal_run_and_closer_are_retained_with_previous_chunk():
    source = "Ella preguntó: «¿Vamos ahora?!» Después continuó."

    chunks = split_target_text(source, max_chars=32, min_chars=1)

    assert texts(chunks)[0] == "Ella preguntó: «¿Vamos ahora?!»"
    assert not any(chunk.text.startswith(("?", "!", "»")) for chunk in chunks[1:])
    assert_split_contract(source, chunks, 32)


def test_long_unicode_token_uses_hard_boundaries_without_losing_content():
    source = "Electroencefalografísticamente"

    chunks = split_target_text(source, max_chars=8, min_chars=1)

    assert len(chunks) > 1
    assert all(chunk.separator_before == "" for chunk in chunks)
    assert "".join(texts(chunks)) == source
    assert_split_contract(source, chunks, 8)


def test_hard_boundary_never_starts_a_chunk_with_a_combining_mark():
    source = "abcdefg\u0338hijklmnop"

    chunks = split_target_text(source, max_chars=7, min_chars=1)

    assert all(
        not unicodedata.category(chunk.text[0]).startswith("M")
        for chunk in chunks
    )
    assert_split_contract(source, chunks, 7)


def test_long_token_keeps_terminal_punctuation_and_closer_with_content():
    source = "Superextraordinariamente!”"

    chunks = split_target_text(source, max_chars=8, min_chars=1)

    assert chunks[-1].text.endswith("!”")
    assert any(character.isalpha() for character in chunks[-1].text)
    assert not any(chunk.text.startswith(("!", "”")) for chunk in chunks[1:])
    assert_split_contract(source, chunks, 8)


def test_tiny_adjacent_clause_is_merged_when_cap_allows():
    source = (
        "Sí. una cláusula breve, "
        "seguida por una explicación deliberadamente extensa"
    )

    chunks = split_target_text(source, max_chars=24, min_chars=12)

    assert texts(chunks)[0] == "Sí. una cláusula breve,"
    assert_split_contract(source, chunks, 24)


def test_tiny_clause_remains_separate_when_no_adjacent_merge_fits():
    source = "Sí. extraordinariamentelargo otrobloqueextenso"

    chunks = split_target_text(source, max_chars=22, min_chars=12)

    assert texts(chunks)[0] == "Sí."
    assert_split_contract(source, chunks, 22)


def test_all_chunks_are_substantive_around_repeated_punctuation():
    source = "Una afirmación muy larga... ¿De verdad?! Sí."

    chunks = split_target_text(source, max_chars=19, min_chars=1)

    assert all(any(character.isalnum() for character in chunk.text) for chunk in chunks)
    assert_split_contract(source, chunks, 19)


def test_same_parent_and_options_always_produce_identical_chunks():
    source = "¿Primero? Segundo, tercero y cuarto; finalmente terminamos."

    first = split_target_text(source, max_chars=18)
    second = split_target_text(source, max_chars=18)

    assert first == second


def test_chunk_record_rejects_empty_unstripped_or_punctuation_only_payloads():
    for invalid in ("", " texto", "texto ", "?!"):
        with pytest.raises(ValueError):
            TargetTextChunk(invalid)
