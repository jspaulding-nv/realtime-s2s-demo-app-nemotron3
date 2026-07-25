from __future__ import annotations

from unittest.mock import patch

from repository_provenance import get_repository_provenance


def test_repository_provenance_uses_local_git_without_paths():

    with patch(
        "repository_provenance._git",
        side_effect=["b" * 40, " M frontend/src/example.ts"],
    ):
        result = get_repository_provenance()

    assert result == {"commit": "b" * 40, "dirty": True}
    assert set(result) == {"commit", "dirty"}


def test_repository_provenance_fails_closed_when_git_is_unavailable():
    with patch("repository_provenance._git", return_value=None):
        assert get_repository_provenance() == {
            "commit": None,
            "dirty": None,
        }
