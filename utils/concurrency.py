"""Параллельная обработка апдейтов Telegram с очередью на каждого пользователя."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from telegram import Update
from telegram.ext import BaseUpdateProcessor

logger = logging.getLogger(__name__)


class PerUserUpdateProcessor(BaseUpdateProcessor):
    """Разные пользователи обрабатываются параллельно, сообщения одного — строго по очереди.

    Очередь нужна, чтобы два быстрых сообщения одного человека не сломали его
    сценарий (ConversationHandler хранит состояние на пользователя).

    PTB держит слот параллелизма, пока апдейт ждёт своей очереди. Поэтому, если у
    пользователя уже `max_pending_per_user` апдейтов в работе и ожидании, новые
    отбрасываются: иначе один человек мог бы занять все слоты и остановить бота
    для остальных.
    """

    def __init__(self, max_concurrent_updates: int, max_pending_per_user: int = 3) -> None:
        """Задать общий лимит параллельных апдейтов и лимит очереди на пользователя."""
        super().__init__(max_concurrent_updates)
        self._max_pending = max_pending_per_user
        self._locks: dict[int, asyncio.Lock] = {}
        self._pending: dict[int, int] = {}

    @staticmethod
    def _key(update: object) -> int | None:
        """Ключ очереди: id пользователя, а если его нет — id чата."""
        if isinstance(update, Update):
            if update.effective_user:
                return update.effective_user.id
            if update.effective_chat:
                return update.effective_chat.id
        return None

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        """Выполнить апдейт в очереди его пользователя."""
        key = self._key(update)
        if key is None:
            await coroutine
            return
        if self._pending.get(key, 0) >= self._max_pending:
            logger.warning("Очередь пользователя %s переполнена, апдейт отброшен", key)
            if hasattr(coroutine, "close"):
                coroutine.close()  # иначе Python предупредит о неожиданном coroutine
            return

        self._pending[key] = self._pending.get(key, 0) + 1
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                await coroutine
        finally:
            self._pending[key] -= 1
            if self._pending[key] == 0:
                del self._pending[key]
                self._locks.pop(key, None)

    async def initialize(self) -> None:
        """Ресурсов для инициализации нет."""

    async def shutdown(self) -> None:
        """Ресурсов для освобождения нет."""
