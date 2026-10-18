"""Доступ по токенам-приглашениям.

Каждый токен из `ACCESS_TOKENS` привязывается к первому пользователю Telegram,
который его отправил. Привязки хранятся в JSON-файле и переживают перезапуск.
В файле лежит не сам токен, а его SHA-256: если поменять токен в настройках,
старая привязка перестаёт действовать и токен снова свободен.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ActivationResult(Enum):
    """Результат попытки активировать токен."""

    ACTIVATED = "activated"
    ALREADY_ACTIVE = "already_active"
    INVALID = "invalid"
    TAKEN = "taken"


@dataclass(frozen=True)
class AccessToken:
    """Токен доступа с меткой владельца (например, `owner` или `teacher`)."""

    label: str
    token: str


def parse_access_tokens(raw: str) -> list[AccessToken]:
    """Разобрать строку вида `owner:XXXX,teacher:YYYY`.

    Raises:
        ValueError: формат нарушен, токен слишком короткий или метки повторяются.
    """
    tokens: list[AccessToken] = []
    for chunk in filter(None, (part.strip() for part in raw.split(","))):
        label, sep, token = chunk.partition(":")
        label, token = label.strip(), token.strip()
        if not sep or not LABEL_RE.match(label):
            raise ValueError(f"ожидается формат метка:токен, получено {label!r}")
        if not TOKEN_RE.match(token):
            raise ValueError(
                f"токен для {label!r} должен состоять из 16–64 символов A-Z, a-z, 0-9, _ или -"
            )
        tokens.append(AccessToken(label, token))
    labels = [t.label for t in tokens]
    if len(set(labels)) != len(labels):
        raise ValueError("метки токенов должны быть уникальными")
    if len({t.token for t in tokens}) != len(tokens):
        raise ValueError("токены должны быть разными")
    return tokens


def _hash(token: str) -> str:
    """SHA-256 токена — в файл привязок пишем только его."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccessManager:
    """Проверяет доступ пользователей и активирует токены."""

    def __init__(self, tokens: list[AccessToken], storage_path: Path, max_users_per_token: int = 1) -> None:
        """Загрузить привязки из файла.

        Args:
            tokens: действующие токены.
            storage_path: JSON-файл с привязками `user_id → токен`.
            max_users_per_token: сколько аккаунтов можно привязать к одному токену.
        """
        self._tokens = tokens
        self._hash_by_label = {t.label: _hash(t.token) for t in tokens}
        self._path = storage_path
        self._max_users = max_users_per_token
        self._users: dict[str, dict[str, str]] = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        """Прочитать привязки; битый или отсутствующий файл — пустой словарь."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Доступ: не удалось прочитать %s, начинаю с пустого списка", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        """Атомарно записать привязки (через временный файл)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def _valid_entry(self, entry: dict[str, str] | None) -> bool:
        """Привязка действует, только если её токен не меняли."""
        return bool(entry) and entry.get("token_hash") == self._hash_by_label.get(entry.get("label", ""))

    def is_authorized(self, user_id: int) -> bool:
        """Есть ли у пользователя действующий доступ."""
        return self._valid_entry(self._users.get(str(user_id)))

    def label_for(self, user_id: int) -> str | None:
        """Метка токена пользователя (для логов)."""
        entry = self._users.get(str(user_id))
        return entry["label"] if self._valid_entry(entry) else None

    def _users_with_label(self, label: str) -> int:
        """Сколько аккаунтов уже привязано к токену."""
        return sum(1 for e in self._users.values() if e.get("label") == label and self._valid_entry(e))

    def activate(self, user_id: int, candidate: str, username: str | None = None) -> ActivationResult:
        """Попробовать активировать токен для пользователя."""
        if self.is_authorized(user_id):
            return ActivationResult.ALREADY_ACTIVE
        candidate = candidate.strip()
        matched: AccessToken | None = None
        for token in self._tokens:
            # сравниваем со всеми токенами за постоянное время
            if hmac.compare_digest(token.token.encode(), candidate.encode()):
                matched = token
        if matched is None:
            return ActivationResult.INVALID
        if self._users_with_label(matched.label) >= self._max_users:
            logger.warning("Доступ: токен %s уже привязан, попытка от %s", matched.label, user_id)
            return ActivationResult.TAKEN
        self._users[str(user_id)] = {
            "label": matched.label,
            "token_hash": self._hash_by_label[matched.label],
            "username": username or "",
            "activated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save()
        logger.info("Доступ: токен %s активирован пользователем %s", matched.label, user_id)
        return ActivationResult.ACTIVATED
