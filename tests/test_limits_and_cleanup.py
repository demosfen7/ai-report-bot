"""Тесты лимитов, очереди апдейтов на пользователя и фоновой уборки."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

from telegram import Update

from utils.concurrency import PerUserUpdateProcessor
from utils.maintenance import delete_old_files
from utils.rate_limit import RateLimiter, format_wait
from utils.short_memory import ShortMemory


def test_rate_limiter_sliding_window() -> None:
    limiter = RateLimiter({"pdf": (2, 3600)})
    assert limiter.hit(1, "pdf", now=0) is None
    assert limiter.hit(1, "pdf", now=10) is None
    assert limiter.hit(1, "pdf", now=20) == 3580  # первое действие «освободится» в 3600
    assert limiter.hit(2, "pdf", now=20) is None  # у другого пользователя свой счётчик
    assert limiter.hit(1, "pdf", now=3601) is None


def test_rate_limiter_prune() -> None:
    limiter = RateLimiter({"chat": (5, 60)})
    limiter.hit(1, "chat", now=0)
    limiter.prune(now=100)
    assert limiter._hits == {}


def test_format_wait() -> None:
    assert format_wait(30) == "через 31 сек"
    assert format_wait(600) == "через 11 мин"


def test_memory_forgets_idle_users() -> None:
    memory = ShortMemory(window=10, summary_after=0)
    memory.add_user_message(1, "старое")
    memory.add_user_message(2, "свежее")
    memory._last_seen[1] -= 7200
    assert memory.prune_idle(3600) == 1
    assert memory.get_history(1) == []
    assert len(memory.get_history(2)) == 1


def test_delete_old_files(tmp_path: Path) -> None:
    old_pdf, new_pdf, keep = tmp_path / "old.pdf", tmp_path / "new.pdf", tmp_path / ".gitkeep"
    for path in (old_pdf, new_pdf, keep):
        path.write_text("x")
    week_ago = time.time() - 8 * 86400
    os.utime(old_pdf, (week_ago, week_ago))
    os.utime(keep, (week_ago, week_ago))

    assert delete_old_files([tmp_path, tmp_path / "missing"], max_age_days=7) == 1
    assert not old_pdf.exists()
    assert new_pdf.exists() and keep.exists()


def _update(user_id: int) -> Update:
    """Update с нужным effective_user без обращения к Telegram."""
    update = Update(update_id=user_id)
    update._effective_user = SimpleNamespace(id=user_id)  # type: ignore[assignment]
    return update


def test_same_user_is_serialized_and_users_run_in_parallel() -> None:
    async def scenario() -> list[str]:
        processor = PerUserUpdateProcessor(max_concurrent_updates=8)
        log: list[str] = []

        async def job(name: str) -> None:
            log.append(f"{name}:start")
            await asyncio.sleep(0.05)
            log.append(f"{name}:end")

        await asyncio.gather(
            processor.process_update(_update(1), job("a1")),
            processor.process_update(_update(1), job("a2")),
            processor.process_update(_update(2), job("b1")),
        )
        return log

    log = asyncio.run(scenario())
    # сообщения пользователя 1 не перекрываются
    assert log.index("a1:end") < log.index("a2:start")
    # пользователь 2 не ждёт, пока пользователь 1 закончит
    assert log.index("b1:start") < log.index("a1:end")


def test_excess_updates_from_one_user_are_dropped() -> None:
    async def scenario() -> list[int]:
        processor = PerUserUpdateProcessor(max_concurrent_updates=8, max_pending_per_user=2)
        done: list[int] = []

        async def job(n: int) -> None:
            await asyncio.sleep(0.02)
            done.append(n)

        await asyncio.gather(*(processor.process_update(_update(1), job(n)) for n in range(5)))
        return done

    assert asyncio.run(scenario()) == [0, 1]
