from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from direct_nmt_client import (
    DirectNMTClient,
    DirectNMTRecoveryError,
    DirectNMTResponseError,
)
from staged_models import EmissionReason, TextSegment, TranslatedSegment
from target_text_validation import TargetTextValidationError


def make_segment(text="The congregation laughed at the joke."):
    return TextSegment(
        sequence_id=7,
        text=text,
        reason=EmissionReason.PUNCTUATION,
        emitted_monotonic_ms=1_500,
        buffered_since_monotonic_ms=1_000,
        source_start_ms=250,
        source_end_ms=1_400,
        contributing_final_ids=(3, 4),
    )


def translation(text="La congregación se rió de la broma.", language="es-US"):
    return SimpleNamespace(text=text, language=language)


def configured_client(*, response=None, clock_values=(2_000, 2_250)):
    ticks = iter(clock_values)
    client = DirectNMTClient(
        uri="nmt.test:50051",
        model="test-model",
        source_language="en-US",
        rpc_timeout_s=4.5,
        clock_ms=lambda: next(ticks),
    )
    auth = SimpleNamespace(
        channel=MagicMock(),
        get_auth_metadata=MagicMock(return_value=(("authorization", "test"),)),
    )
    rpc = MagicMock(
        return_value=response
        or SimpleNamespace(translations=[translation()])
    )
    client._auth = auth
    client._client = SimpleNamespace(stub=SimpleNamespace(TranslateText=rpc))
    client._connected = True
    return client, auth, rpc


def test_translate_segment_builds_single_request_and_preserves_provenance():
    client, auth, rpc = configured_client()
    segment = make_segment()
    request = object()

    with patch(
        "direct_nmt_client.riva_nmt_pb2.TranslateTextRequest",
        return_value=request,
    ) as request_cls:
        result = client.translate_segment(segment, " es-US ")

    request_cls.assert_called_once_with(
        texts=[segment.text],
        model="test-model",
        source_language="en-US",
        target_language="es-US",
    )
    rpc.assert_called_once_with(
        request,
        metadata=(("authorization", "test"),),
        timeout=4.5,
    )
    auth.get_auth_metadata.assert_called_once_with()
    assert isinstance(result, TranslatedSegment)
    assert result.segment is segment
    assert result.sequence_id == segment.sequence_id
    assert result.text == "La congregación se rió de la broma."
    assert result.language == "es-US"
    assert result.started_monotonic_ms == 2_000
    assert result.completed_monotonic_ms == 2_250
    assert result.source_override_applied is False
    assert result.retry_count == 0
    assert result.to_dict()["retry_count"] == 0


@pytest.mark.parametrize(
    ("source_text", "recovery_text"),
    [
        ("Peace.", "Peace"),
        ("Peace?", "Peace"),
        ("Peace!", "Peace"),
        ("  Peace.  ", "Peace"),
    ],
)
def test_short_ascii_punctuated_source_retries_once_without_terminal_punctuation(
    source_text,
    recovery_text,
):
    client, auth, rpc = configured_client()
    segment = make_segment(source_text)
    requests = (object(), object())
    rpc.side_effect = [
        SimpleNamespace(translations=[translation(text="好。")]),
        SimpleNamespace(translations=[translation(text="Paz.")]),
    ]

    with patch(
        "direct_nmt_client.riva_nmt_pb2.TranslateTextRequest",
        side_effect=requests,
    ) as request_cls:
        result = client.translate_segment(segment, "es-US")

    assert request_cls.call_args_list[0].kwargs == {
        "texts": [source_text],
        "model": "test-model",
        "source_language": "en-US",
        "target_language": "es-US",
    }
    assert request_cls.call_args_list[1].kwargs == {
        "texts": [recovery_text],
        "model": "test-model",
        "source_language": "en-US",
        "target_language": "es-US",
    }
    assert [call.args[0] for call in rpc.call_args_list] == list(requests)
    assert all(call.kwargs["timeout"] == 4.5 for call in rpc.call_args_list)
    assert auth.get_auth_metadata.call_count == 2
    assert result.segment is segment
    assert result.text == "Paz."
    assert result.language == "es-US"
    assert result.started_monotonic_ms == 2_000
    assert result.completed_monotonic_ms == 2_250
    assert result.retry_count == 1
    assert result.to_dict()["retry_count"] == 1


@pytest.mark.parametrize(
    "source_text",
    [
        "Peace",
        "Peace...",
        "Two words.",
        "A1.",
        "O'clock.",
        "well-known.",
        "Péace.",
        "Peace。",
        "“Peace.”",
        f"{'A' * 33}.",
    ],
)
def test_punctuation_recovery_rejects_ineligible_source_shapes(source_text):
    client, _, rpc = configured_client(
        response=SimpleNamespace(translations=[translation(text="好。")])
    )

    with pytest.raises(TargetTextValidationError):
        client.translate_segment(make_segment(source_text), "es-US")

    assert rpc.call_count == 1


def test_punctuation_recovery_fails_closed_after_one_invalid_retry():
    unsafe_outputs = ("好。", "Привет.")
    client, _, rpc = configured_client()
    rpc.side_effect = [
        SimpleNamespace(translations=[translation(text=text)])
        for text in unsafe_outputs
    ]

    segment = make_segment("Peace.")
    with pytest.raises(DirectNMTRecoveryError) as failure:
        client.translate_segment(segment, "es-US")

    assert rpc.call_count == 2
    assert failure.value.segment is segment
    assert failure.value.sequence_id == segment.sequence_id
    assert failure.value.retry_count == 1
    assert failure.value.initial_reason == "unsupported_characters"
    assert failure.value.retry_error_code == "TargetTextValidationError"
    assert failure.value.retry_reason == "unsupported_characters"
    assert all(text not in str(failure.value) for text in unsafe_outputs)


def test_punctuation_recovery_does_not_mask_invalid_retry_response_shape():
    client, _, rpc = configured_client()
    rpc.side_effect = [
        SimpleNamespace(translations=[translation(text="好。")]),
        SimpleNamespace(translations=[]),
    ]

    segment = make_segment("Peace.")
    with pytest.raises(DirectNMTRecoveryError) as failure:
        client.translate_segment(segment, "es-US")

    assert rpc.call_count == 2
    assert failure.value.segment is segment
    assert failure.value.retry_count == 1
    assert failure.value.retry_error_code == "DirectNMTResponseError"
    assert "exactly one" not in str(failure.value)


def test_punctuation_recovery_only_applies_to_es_us():
    client, _, rpc = configured_client(
        response=SimpleNamespace(
            translations=[translation(text="Paz.", language="es-ES")]
        )
    )

    with pytest.raises(
        TargetTextValidationError,
        match="unsupported_language_policy",
    ):
        client.translate_segment(make_segment("Peace."), "es-ES")

    assert rpc.call_count == 1


def test_blank_source_is_rejected_before_request_or_rpc():
    client, _, rpc = configured_client()
    segment = MagicMock(spec=TextSegment)
    segment.text = "  \t "

    with patch(
        "direct_nmt_client.riva_nmt_pb2.TranslateTextRequest"
    ) as request_cls, pytest.raises(ValueError, match="segment text"):
        client.translate_segment(segment, "es-US")

    request_cls.assert_not_called()
    rpc.assert_not_called()


@pytest.mark.parametrize("target_language", ["", "   ", None])
def test_blank_target_is_rejected_before_rpc(target_language):
    client, _, rpc = configured_client()

    with pytest.raises(ValueError, match="target_language"):
        client.translate_segment(make_segment(), target_language)

    rpc.assert_not_called()


@pytest.mark.parametrize("translations", [[], [translation(), translation()]])
def test_response_requires_exactly_one_translation(translations):
    client, _, rpc = configured_client(
        response=SimpleNamespace(translations=translations)
    )

    with pytest.raises(DirectNMTResponseError, match="exactly one"):
        client.translate_segment(make_segment("Peace."), "es-US")

    assert rpc.call_count == 1


@pytest.mark.parametrize("text", ["", "   "])
def test_response_rejects_blank_translated_text(text):
    client, _, _ = configured_client(
        response=SimpleNamespace(translations=[translation(text=text)])
    )

    with pytest.raises(
        TargetTextValidationError, match="missing_letter_or_digit"
    ):
        client.translate_segment(make_segment(), "es-US")


def test_response_is_normalized_to_nfc_before_becoming_a_segment():
    client, _, _ = configured_client(
        response=SimpleNamespace(
            translations=[translation(text="La congregacio\u0301n canto\u0301.")]
        )
    )

    result = client.translate_segment(make_segment(), "es-US")

    assert result.text == "La congregación cantó."


@pytest.mark.parametrize(
    "unsafe_text,expected_diagnostic",
    [
        ("好吧。", "Han"),
        ("Привет.", "Cyrillic"),
        ("Hola а todos.", "Cyrillic"),
        ("Hola\u2060.", "U+2060"),
        ("¿?!", "missing_letter_or_digit"),
    ],
)
def test_response_rejects_unsafe_target_text_with_safe_diagnostics(
    unsafe_text, expected_diagnostic
):
    client, _, _ = configured_client(
        response=SimpleNamespace(translations=[translation(text=unsafe_text)])
    )

    with pytest.raises(TargetTextValidationError) as failure:
        client.translate_segment(make_segment(), "es-US")

    assert failure.value.sequence_id == 7
    assert failure.value.language == "es-US"
    assert expected_diagnostic in str(failure.value)
    assert unsafe_text not in str(failure.value)


@pytest.mark.parametrize(
    "source_text,expected_text",
    [
        ("Okay.", "De acuerdo."),
        ("okay?", "¿De acuerdo?"),
        ("OK!", "¡De acuerdo!"),
        ("Amen.", "Amén."),
        ("AMEN?", "¿Amén?"),
        ("amen!", "¡Amén!"),
        ('  "Amen."  ', '"Amén."'),
        ("“Okay.”", "“De acuerdo.”"),
        ("‘amen!’", "‘¡Amén!’"),
    ],
)
def test_standalone_source_overrides_bypass_nmt_and_are_observable(
    source_text, expected_text
):
    client, auth, rpc = configured_client()

    result = client.translate_segment(make_segment(source_text), "es-US")

    assert result.text == expected_text
    assert result.language == "es-US"
    assert result.source_override_applied is True
    assert result.to_dict()["source_override_applied"] is True
    assert result.retry_count == 0
    rpc.assert_not_called()
    auth.get_auth_metadata.assert_not_called()


def test_source_override_is_narrow_and_sentence_context_still_calls_nmt():
    client, _, rpc = configured_client()

    result = client.translate_segment(
        make_segment("Okay everyone, please sit down."), "es-US"
    )

    assert result.source_override_applied is False
    rpc.assert_called_once()


@pytest.mark.parametrize("source_text", ['“Amen."', '"Okay.”', '“Amen. now”'])
def test_source_override_rejects_mismatched_or_non_standalone_quotes(source_text):
    client, _, rpc = configured_client()

    result = client.translate_segment(make_segment(source_text), "es-US")

    assert result.source_override_applied is False
    rpc.assert_called_once()


@pytest.mark.parametrize("language", ["", "es-ES", "en-US"])
def test_response_requires_exact_requested_language(language):
    client, _, rpc = configured_client(
        response=SimpleNamespace(translations=[translation(language=language)])
    )

    with pytest.raises(DirectNMTResponseError, match="language mismatch"):
        client.translate_segment(make_segment("Peace."), "es-US")

    assert rpc.call_count == 1


def test_rpc_failure_propagates_for_orchestrator_classification():
    client, _, rpc = configured_client()
    service_error = RuntimeError("NMT unavailable")
    rpc.side_effect = service_error

    with pytest.raises(RuntimeError, match="NMT unavailable") as caught:
        client.translate_segment(make_segment("Peace."), "es-US")

    assert caught.value is service_error
    assert rpc.call_count == 1


@pytest.mark.parametrize("retry_count", [-1, 2, True, 1.5])
def test_translated_segment_rejects_invalid_retry_count(retry_count):
    with pytest.raises(ValueError, match="retry_count"):
        TranslatedSegment(
            segment=make_segment(),
            text="Paz.",
            language="es-US",
            started_monotonic_ms=2_000,
            completed_monotonic_ms=2_250,
            retry_count=retry_count,
        )


def test_disconnected_client_rejects_translation():
    client = DirectNMTClient()

    with pytest.raises(RuntimeError, match="not connected"):
        client.translate_segment(make_segment(), "es-US")


def test_disconnected_client_also_rejects_source_override():
    client = DirectNMTClient()

    with pytest.raises(RuntimeError, match="not connected"):
        client.translate_segment(make_segment("Amen."), "es-US")


def test_connect_is_idempotent_and_disconnect_closes_channel_once():
    client = DirectNMTClient(uri="nmt.test:50051")
    auth = SimpleNamespace(channel=MagicMock())
    service = SimpleNamespace(stub=object())

    with patch("direct_nmt_client.riva.client.Auth", return_value=auth) as auth_cls, patch(
        "direct_nmt_client.riva.client.NeuralMachineTranslationClient",
        return_value=service,
    ) as service_cls:
        assert client.connect() is True
        assert client.connect() is True
        client.disconnect()
        client.disconnect()

    auth_cls.assert_called_once_with(uri="nmt.test:50051")
    service_cls.assert_called_once_with(auth)
    auth.channel.close.assert_called_once_with()
    assert client.is_connected() is False


def test_failed_client_construction_closes_new_channel():
    client = DirectNMTClient(uri="nmt.test:50051")
    auth = SimpleNamespace(channel=MagicMock())

    with patch("direct_nmt_client.riva.client.Auth", return_value=auth), patch(
        "direct_nmt_client.riva.client.NeuralMachineTranslationClient",
        side_effect=RuntimeError("bad stub"),
    ):
        assert client.connect() is False

    auth.channel.close.assert_called_once_with()
    assert client.is_connected() is False


@pytest.mark.parametrize("rpc_timeout_s", [0, -1, float("inf"), float("nan"), True])
def test_timeout_must_be_positive_and_finite(rpc_timeout_s):
    with pytest.raises(ValueError, match="rpc_timeout_s"):
        DirectNMTClient(rpc_timeout_s=rpc_timeout_s)


@pytest.mark.parametrize(
    "field,value",
    [
        ("uri", " "),
        ("model", ""),
        ("source_language", None),
    ],
)
def test_explicit_blank_configuration_is_rejected(field, value):
    kwargs = {field: value}
    # ``None`` means use the default for optional constructor fields, so use a
    # non-string sentinel to exercise required source-language validation.
    if field == "source_language" and value is None:
        kwargs[field] = 42
    with pytest.raises(ValueError, match=field):
        DirectNMTClient(**kwargs)
