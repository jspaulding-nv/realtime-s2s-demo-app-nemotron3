import pytest

from target_text_validation import (
    TargetTextValidationError,
    validate_target_text,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("¡Hola, congregación!", "¡Hola, congregación!"),
        ("  ¿Cantaron 123 veces?  ", "¿Cantaron 123 veces?"),
        ("La congregacio\u0301n.", "La congregación."),
        ("Cantó 1.ª vez.", "Cantó 1.ª vez."),
        ("123...", "123..."),
    ],
)
def test_es_us_policy_allows_spanish_text_digits_punctuation_and_spaces(
    text, expected
):
    assert (
        validate_target_text(text, language="es-US", sequence_id=4)
        == expected
    )


@pytest.mark.parametrize(
    "text,reason",
    [
        ("... ¿?!", "missing_letter_or_digit"),
        ("Hola\nMundo", "unsupported_characters"),
        ("Hola\u200dMundo", "unsupported_characters"),
        ("Hola 😀", "unsupported_characters"),
        ("Hola。", "unsupported_characters"),
        ("aБ", "unsupported_characters"),
    ],
)
def test_es_us_policy_fails_closed_without_echoing_target_text(text, reason):
    with pytest.raises(TargetTextValidationError) as failure:
        validate_target_text(text, language="es-US", sequence_id=9)

    assert failure.value.reason == reason
    assert failure.value.sequence_id == 9
    assert text not in str(failure.value)


def test_es_us_policy_identifies_cjk_full_stop_without_echoing_text():
    with pytest.raises(TargetTextValidationError) as failure:
        validate_target_text("Hola。", language="es-US", sequence_id=12)

    assert failure.value.unsupported_code_points == ("U+3002",)
    assert failure.value.unsupported_scripts == ("UnsupportedPunctuation",)
    assert "Hola" not in str(failure.value)


def test_missing_language_policy_fails_closed():
    with pytest.raises(
        TargetTextValidationError, match="unsupported_language_policy"
    ):
        validate_target_text("Bonjour.", language="fr-FR", sequence_id=1)
