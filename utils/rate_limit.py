"""Ограничение частоты запросов: скользящее окно на каждого пользователя и вид действия."""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    """Считает действия пользователя в скользящем окне.

    Пример: `RateLimiter({"pdf": (10, 3600)})` — не больше 10 PDF в час.
    """

    def __init__(self, limits: dict[str, tuple[int, float]]) -> None:
        """Задать лимиты: вид действия → (максимум, окно в секундах)."""
        self._limits = limits
        self._hits: dict[tuple[int, str], deque[float]] = {}

    def hit(self, user_id: int, kind: str, now: float | None = None) -> float | None:
        """Зарегистрировать действие.

        Returns:
            None, если действие разрешено и учтено; иначе сколько секунд подождать.
        """
        limit, window = self._limits[kind]
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault((user_id, kind), deque())
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1.0, hits[0] + window - now)
        hits.append(now)
        return None

    def limit_of(self, kind: str) -> int:
        """Максимум действий в окне для вида действия."""
        return self._limits[kind][0]

    def prune(self, now: float | None = None) -> None:
        """Удалить устаревшие отметки, чтобы словарь не рос бесконечно."""
        now = time.monotonic() if now is None else now
        for key in list(self._hits):
            window = self._limits[key[1]][1]
            hits = self._hits[key]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if not hits:
                del self._hits[key]


def format_wait(seconds: float) -> str:
    """«через 5 мин» / «через 40 сек»."""
    if seconds >= 60:
        return f"через {int(seconds // 60) + 1} мин"
    return f"через {int(seconds) + 1} сек"
