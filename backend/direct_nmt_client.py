"""Direct text-to-text NMT adapter for the staged speech pipeline."""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Callable, Optional

import riva.client
import riva.client.proto.riva_nmt_pb2 as riva_nmt_pb2

from config import riva_config
from staged_models import TextSegment, TranslatedSegment
from target_text_validation import validate_target_text


class DirectNMTResponseError(RuntimeError):
    """Raised when NMT returns a response unsafe to pass to TTS."""


class DirectNMTClient:
    """Blocking unary NMT client intended for one staged-pipeline worker.

    ``translate_segment`` performs blocking gRPC I/O and must therefore run in
    a worker thread rather than on the asyncio event loop. Retry policy belongs
    to the orchestrator; this adapter issues exactly one RPC per call.
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        model: Optional[str] = None,
        source_language: Optional[str] = None,
        rpc_timeout_s: float = 15.0,
        clock_ms: Optional[Callable[[], float]] = None,
    ) -> None:
        self.uri = _required_text(
            "uri", riva_config.uri if uri is None else uri
        )
        self.model = _required_text(
            "model", riva_config.model if model is None else model
        )
        self.source_language = _required_text(
            "source_language",
            riva_config.source_language if source_language is None else source_language,
        )
        if (
            isinstance(rpc_timeout_s, bool)
            or not isinstance(rpc_timeout_s, (int, float))
            or not math.isfinite(rpc_timeout_s)
            or rpc_timeout_s <= 0
        ):
            raise ValueError("rpc_timeout_s must be a positive finite number")
        self.rpc_timeout_s = float(rpc_timeout_s)
        self._clock_ms = clock_ms or (lambda: time.monotonic_ns() / 1_000_000)
        self._auth = None
        self._client = None
        self._connected = False
        self._lifecycle_lock = threading.RLock()

    def connect(self) -> bool:
        """Create the Riva channel once; repeated successful calls are no-ops."""
        with self._lifecycle_lock:
            if self._connected:
                return True

            new_auth = None
            try:
                new_auth = riva.client.Auth(uri=self.uri)
                new_client = riva.client.NeuralMachineTranslationClient(new_auth)
            except Exception as exc:
                print(f"[Direct NMT] Failed to connect to {self.uri}: {exc}")
                channel = getattr(new_auth, "channel", None)
                if channel is not None:
                    channel.close()
                return False

            self._auth = new_auth
            self._client = new_client
            self._connected = True
            return True

    def disconnect(self) -> None:
        """Idempotently detach and close the owned Riva channel."""
        with self._lifecycle_lock:
            auth = self._auth
            self._auth = None
            self._client = None
            self._connected = False

        channel = getattr(auth, "channel", None)
        if channel is not None:
            channel.close()

    def is_connected(self) -> bool:
        with self._lifecycle_lock:
            return self._connected

    def translate_segment(
        self,
        segment: TextSegment,
        target_language: str,
    ) -> TranslatedSegment:
        """Translate exactly one ordered segment and validate it for TTS."""
        if not isinstance(segment, TextSegment):
            raise ValueError("segment must be a TextSegment")
        if not segment.text.strip():
            # The pinned service can hallucinate output for blank input, so
            # this guard must remain before request construction/RPC dispatch.
            raise ValueError("segment text must contain non-whitespace content")
        resolved_target = _required_text("target_language", target_language)

        with self._lifecycle_lock:
            if not self._connected or self._auth is None or self._client is None:
                raise RuntimeError("Direct NMT client is not connected")
            auth = self._auth
            client = self._client

        override = _standalone_es_us_override(segment.text, resolved_target)
        if override is not None:
            started_ms = self._clock_ms()
            translated_text = validate_target_text(
                override,
                language=resolved_target,
                sequence_id=segment.sequence_id,
            )
            completed_ms = self._clock_ms()
            return TranslatedSegment(
                segment=segment,
                text=translated_text,
                language=resolved_target,
                started_monotonic_ms=started_ms,
                completed_monotonic_ms=completed_ms,
                source_override_applied=True,
            )

        started_ms = self._clock_ms()
        request = riva_nmt_pb2.TranslateTextRequest(
            texts=[segment.text],
            model=self.model,
            source_language=self.source_language,
            target_language=resolved_target,
        )
        response = client.stub.TranslateText(
            request,
            metadata=auth.get_auth_metadata(),
            timeout=self.rpc_timeout_s,
        )
        completed_ms = self._clock_ms()

        translations = tuple(getattr(response, "translations", ()) or ())
        if len(translations) != 1:
            raise DirectNMTResponseError(
                "NMT response must contain exactly one translation; "
                f"received {len(translations)}"
            )
        translation = translations[0]
        translated_text = str(getattr(translation, "text", ""))
        response_language = str(getattr(translation, "language", "")).strip()
        if response_language != resolved_target:
            raise DirectNMTResponseError(
                "NMT response language mismatch: "
                f"expected {resolved_target!r}, received {response_language!r}"
            )
        translated_text = validate_target_text(
            translated_text,
            language=response_language,
            sequence_id=segment.sequence_id,
        )

        return TranslatedSegment(
            segment=segment,
            text=translated_text,
            language=response_language,
            started_monotonic_ms=started_ms,
            completed_monotonic_ms=completed_ms,
        )


_STANDALONE_ES_US_OVERRIDE = re.compile(
    r"^(?P<term>ok(?:ay)?|amen)(?P<terminal>[.!?]?)$",
    re.IGNORECASE,
)


def _standalone_es_us_override(text: str, target_language: str) -> Optional[str]:
    """Translate only known standalone short utterances without an NMT RPC."""
    if target_language != "es-US":
        return None
    candidate = text.strip()
    opening_quote = ""
    closing_quote = ""
    quote_pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»"}
    if candidate[:1] in quote_pairs:
        opening_quote = candidate[0]
        closing_quote = quote_pairs[opening_quote]
        if not candidate.endswith(closing_quote):
            return None
        candidate = candidate[1:-1]

    match = _STANDALONE_ES_US_OVERRIDE.fullmatch(candidate)
    if match is None:
        return None

    translated = (
        "De acuerdo"
        if match.group("term").casefold() in {"ok", "okay"}
        else "Amén"
    )
    terminal = match.group("terminal") or "."
    if terminal == "?":
        result = f"¿{translated}?"
    elif terminal == "!":
        result = f"¡{translated}!"
    else:
        result = f"{translated}."
    return f"{opening_quote}{result}{closing_quote}"


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must contain non-whitespace text")
    return value.strip()


# Separate global instance for the feature-flagged staged orchestrator. It is
# not used by the existing monolithic S2S path.
direct_nmt_client = DirectNMTClient()
