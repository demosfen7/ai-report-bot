"""Тестовые диалоги для кнопки «🧪 Тестовый диалог» (файлы `examples/example_dialog*.txt`)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TestDialog:
    """Пример диалога с клиентом: заголовок и текст."""

    title: str
    text: str


def load_test_dialogs(directory: Path) -> list[TestDialog]:
    """Прочитать примеры из папки; первая строка `# …` — заголовок, она в текст не входит."""
    dialogs: list[TestDialog] = []
    for path in sorted(directory.glob("example_dialog*.txt")):
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        title = path.stem
        if lines and lines[0].startswith("#"):
            title = lines.pop(0).lstrip("#").strip()
        text = "\n".join(lines).strip()
        if text:
            dialogs.append(TestDialog(title, text))
    return dialogs
