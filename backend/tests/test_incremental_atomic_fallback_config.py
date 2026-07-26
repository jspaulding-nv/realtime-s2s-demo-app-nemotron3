import pytest

from config import StagedPipelineConfig


def test_incremental_publication_defaults_to_500_ms():
    assert StagedPipelineConfig().tts_incremental_frame_ms == 500


def test_incremental_atomic_fallback_defaults_to_four_and_zero_disables():
    assert StagedPipelineConfig().tts_incremental_atomic_fallback_max_chars == 4
    assert (
        StagedPipelineConfig(
            tts_incremental_atomic_fallback_max_chars=0
        ).tts_incremental_atomic_fallback_max_chars
        == 0
    )


@pytest.mark.parametrize("value", (-1, True, 1.5, "4"))
def test_incremental_atomic_fallback_requires_nonnegative_integer(value):
    with pytest.raises(
        ValueError,
        match="tts_incremental_atomic_fallback_max_chars",
    ):
        StagedPipelineConfig(
            tts_incremental_atomic_fallback_max_chars=value
        )
