"""Fail-closed validation for text crossing the NMT-to-TTS boundary."""

from __future__ import annotations

import unicodedata
from typing import Iterable, Tuple


# Explicit punctuation known to be representable by the pinned Spanish Magpie
# profile. Do not admit punctuation solely by Unicode category: U+3002 was one
# of the tokenizer-missing characters in the failure that motivated this gate.
_ES_US_MAGPIE_SAFE_PUNCTUATION = frozenset(
    "!\"#%&'()*,-./:;?@[\\]_{}"
    "¡¿«»“”‘’…–—"
)


class TargetTextValidationError(ValueError):
    """Raised when translated text is unsafe for the requested TTS language.

    Diagnostics intentionally contain only metadata and Unicode identifiers;
    the translated text itself is never included in the exception message.
    """

    def __init__(
        self,
        *,
        sequence_id: int,
        language: str,
        reason: str,
        unsupported_code_points: Iterable[str] = (),
        unsupported_scripts: Iterable[str] = (),
    ) -> None:
        self.sequence_id = sequence_id
        self.language = language
        self.reason = reason
        self.unsupported_code_points = tuple(unsupported_code_points)
        self.unsupported_scripts = tuple(unsupported_scripts)

        diagnostics = [
            f"target text validation failed for sequence {sequence_id}",
            f"language={language!r}",
            f"reason={reason}",
        ]
        if self.unsupported_scripts:
            diagnostics.append(
                "unsupported_scripts=" + ",".join(self.unsupported_scripts)
            )
        if self.unsupported_code_points:
            diagnostics.append(
                "unsupported_code_points="
                + ",".join(self.unsupported_code_points)
            )
        super().__init__("; ".join(diagnostics))


def validate_target_text(
    text: str,
    *,
    language: str,
    sequence_id: int,
) -> str:
    """Return NFC-normalized target text or raise a safe, typed error.

    The currently deployed staged pipeline has one target policy: ``es-US``.
    It admits Latin-script letters, Unicode decimal digits, an explicit
    Spanish Magpie-safe punctuation allowlist, non-control whitespace, and
    combining marks attached to Latin letters. Other punctuation (including
    CJK U+3002), scripts, symbols, and control/format characters fail closed.
    """
    if not isinstance(text, str):
        raise TargetTextValidationError(
            sequence_id=sequence_id,
            language=language,
            reason="non_text_value",
        )
    if language != "es-US":
        raise TargetTextValidationError(
            sequence_id=sequence_id,
            language=language,
            reason="unsupported_language_policy",
        )

    normalized = unicodedata.normalize("NFC", text)
    unsupported_code_points = []
    unsupported_scripts = []
    has_letter_or_digit = False
    latin_base_active = False

    for character in normalized:
        category = unicodedata.category(character)
        name = unicodedata.name(character, "UNNAMED")

        if category.startswith("L"):
            if "LATIN" in name or ord(character) in {0x00AA, 0x00BA}:
                has_letter_or_digit = True
                latin_base_active = True
            else:
                unsupported_code_points.append(_code_point(character))
                unsupported_scripts.append(_script_name(name))
                latin_base_active = False
            continue

        if category == "Nd":
            has_letter_or_digit = True
            latin_base_active = False
            continue

        if category.startswith("M"):
            if not latin_base_active:
                unsupported_code_points.append(_code_point(character))
                unsupported_scripts.append("NonLatinOrDetachedMark")
            continue

        latin_base_active = False
        if category.startswith("P"):
            if character in _ES_US_MAGPIE_SAFE_PUNCTUATION:
                continue
            unsupported_code_points.append(_code_point(character))
            unsupported_scripts.append("UnsupportedPunctuation")
            continue
        if character.isspace() and not category.startswith("C"):
            continue

        # This includes controls/formats as well as symbols, private-use,
        # surrogate, and unassigned code points. The policy is fail closed.
        unsupported_code_points.append(_code_point(character))
        if category in {"Cc", "Cf"}:
            unsupported_scripts.append("ControlOrFormat")
        elif category.startswith("C"):
            unsupported_scripts.append("UnsupportedUnicodeOther")
        else:
            unsupported_scripts.append("UnsupportedSymbol")

    if unsupported_code_points:
        raise TargetTextValidationError(
            sequence_id=sequence_id,
            language=language,
            reason="unsupported_characters",
            unsupported_code_points=_unique(unsupported_code_points),
            unsupported_scripts=_unique(unsupported_scripts),
        )
    if not has_letter_or_digit:
        raise TargetTextValidationError(
            sequence_id=sequence_id,
            language=language,
            reason="missing_letter_or_digit",
        )

    return normalized.strip()


def _code_point(character: str) -> str:
    return f"U+{ord(character):04X}"


def _script_name(unicode_name: str) -> str:
    for marker, label in (
        ("CYRILLIC", "Cyrillic"),
        ("GREEK", "Greek"),
        ("HEBREW", "Hebrew"),
        ("ARABIC", "Arabic"),
        ("HIRAGANA", "Hiragana"),
        ("KATAKANA", "Katakana"),
        ("HANGUL", "Hangul"),
        ("CJK", "Han"),
        ("IDEOGRAPH", "Han"),
        ("DEVANAGARI", "Devanagari"),
        ("THAI", "Thai"),
    ):
        if marker in unicode_name:
            return label
    return "NonLatin"


def _unique(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(values))
