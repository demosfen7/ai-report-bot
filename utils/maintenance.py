"""Фоновая уборка: старые PDF и JSON на диске."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


def delete_old_files(
    directories: Iterable[Path], max_age_days: float, suffixes: tuple[str, ...] = (".pdf", ".json")
) -> int:
    """Удалить файлы с нужными расширениями старше `max_age_days` дней.

    Служебные файлы (например, `.gitkeep`) не трогает. Возвращает число удалённых файлов.
    """
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and path.suffix in suffixes and path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    logger.warning("Уборка: не удалось удалить %s", path, exc_info=True)
    if removed:
        logger.info("Уборка: удалено файлов старше %s дн.: %d", max_age_days, removed)
    return removed
