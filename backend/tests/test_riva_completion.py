from unittest.mock import MagicMock

import pytest

from riva_client import RivaS2SClient


class InlineExecutor:
    def submit(self, function):
        function()


class CompletingNmtClient:
    def streaming_s2s_response_generator(self, *, audio_chunks, streaming_config):
        del streaming_config
        audio_chunks.stop()
        list(audio_chunks)
        return iter(())


class FailingNmtClient:
    def streaming_s2s_response_generator(self, *, audio_chunks, streaming_config):
        del audio_chunks, streaming_config
        raise RuntimeError("final flush failed")


class EndpointThenDrainNmtClient:
    def __init__(self):
        self.calls = 0

    def streaming_s2s_response_generator(self, *, audio_chunks, streaming_config):
        del streaming_config
        self.calls += 1
        if self.calls == 1:
            audio_chunks.add_chunk(b"final queued input")
            audio_chunks.stop()
            return iter(())
        list(audio_chunks)
        return iter(())


@pytest.mark.asyncio
async def test_translation_thread_always_invokes_completion_callback():
    client = RivaS2SClient()
    client._executor = InlineExecutor()
    client._connected = True
    client._nmt_client = CompletingNmtClient()
    client.create_s2s_config = MagicMock(return_value=object())
    on_complete = MagicMock()

    await client.translate_stream(
        target_language="es-US",
        on_audio=MagicMock(),
        on_error=MagicMock(),
        on_complete=on_complete,
    )

    on_complete.assert_called_once_with()


@pytest.mark.asyncio
async def test_translation_failure_reports_error_without_completion():
    client = RivaS2SClient()
    client._executor = InlineExecutor()
    client._connected = True
    client._nmt_client = FailingNmtClient()
    client.create_s2s_config = MagicMock(return_value=object())
    on_error = MagicMock()
    on_complete = MagicMock()

    await client.translate_stream(
        target_language="es-US",
        on_audio=MagicMock(),
        on_error=on_error,
        on_complete=on_complete,
    )

    on_error.assert_called_once()
    assert "final flush failed" in on_error.call_args.args[0]
    on_complete.assert_not_called()


@pytest.mark.asyncio
async def test_endpoint_restart_drains_queued_input_before_completion():
    nmt_client = EndpointThenDrainNmtClient()
    client = RivaS2SClient()
    client._executor = InlineExecutor()
    client._connected = True
    client._nmt_client = nmt_client
    client.create_s2s_config = MagicMock(return_value=object())
    on_error = MagicMock()
    on_complete = MagicMock()

    iterator = await client.translate_stream(
        target_language="es-US",
        on_audio=MagicMock(),
        on_error=on_error,
        on_complete=on_complete,
    )

    assert nmt_client.calls == 2
    assert iterator._input_exhausted is True
    assert iterator._queue.empty()
    on_complete.assert_called_once_with()
    on_error.assert_not_called()
