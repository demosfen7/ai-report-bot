"""Тесты доступа по токенам."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.access import AccessManager, AccessToken, ActivationResult, parse_access_tokens

OWNER = "owner-token-AAAAAAAAAAAAAAAA"
TEACHER = "teacher-token-BBBBBBBBBBBBBBB"


def _manager(path: Path, owner: str = OWNER) -> AccessManager:
    return AccessManager([AccessToken("owner", owner), AccessToken("teacher", TEACHER)], path)


def test_parse_access_tokens() -> None:
    tokens = parse_access_tokens(f"owner:{OWNER}, teacher:{TEACHER}")
    assert [t.label for t in tokens] == ["owner", "teacher"]
    assert tokens[1].token == TEACHER


@pytest.mark.parametrize(
    "raw",
    [
        "просто-токен-без-метки-1234567",
        "owner:short",
        f"owner:{OWNER},owner:{TEACHER}",
        f"owner:{OWNER},teacher:{OWNER}",
        "owner:токен-с-кириллицей-1234567",
    ],
)
def test_parse_access_tokens_rejects_bad_input(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_access_tokens(raw)


def test_unknown_user_is_not_authorized(tmp_path: Path) -> None:
    assert not _manager(tmp_path / "users.json").is_authorized(1)


def test_activation_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    manager = _manager(path)
    assert manager.activate(1, f"  {OWNER}\n", "viktor") is ActivationResult.ACTIVATED
    assert manager.is_authorized(1)
    assert manager.label_for(1) == "owner"
    assert manager.activate(1, OWNER) is ActivationResult.ALREADY_ACTIVE

    # после перезапуска привязка сохраняется, а сам токен в файле не хранится
    assert _manager(path).is_authorized(1)
    assert OWNER not in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["1"]["label"] == "owner"


def test_invalid_token(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "users.json")
    assert manager.activate(1, "wrong-token-CCCCCCCCCCCCCCC") is ActivationResult.INVALID
    assert not manager.is_authorized(1)


def test_token_binds_to_single_account(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "users.json")
    assert manager.activate(1, OWNER) is ActivationResult.ACTIVATED
    assert manager.activate(2, OWNER) is ActivationResult.TAKEN
    assert not manager.is_authorized(2)
    assert manager.activate(2, TEACHER) is ActivationResult.ACTIVATED


def test_rotating_token_revokes_access(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    _manager(path).activate(1, OWNER)

    rotated = _manager(path, owner="owner-token-NEWNEWNEWNEWNEW")
    assert not rotated.is_authorized(1)
    # новый токен свободен, его можно активировать заново
    assert rotated.activate(2, "owner-token-NEWNEWNEWNEWNEW") is ActivationResult.ACTIVATED


def test_corrupted_storage_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    path.write_text("{не json", encoding="utf-8")
    assert not _manager(path).is_authorized(1)
