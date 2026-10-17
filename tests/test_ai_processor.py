"""Тесты логики повторов AIProcessor на фейковом клиенте Anthropic (без сети)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from utils.ai_processor import AIProcessingError, AIProcessor

VALID_REPORT = json.dumps(
    {
        "client_name": "Анна",
        "topic": "Сайт",
        "main_request": "Новый сайт",
        "mood": "Спокойное",
        "next_steps": ["Отправить КП"],
    },
    ensure_ascii=False,
)


class FakeMessages:
    """Отдаёт заранее заданные ответы и запоминает системные промпты."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.systems: list[str] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.systems.append(kwargs["system"])
        text = self.replies[len(self.systems) - 1]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )


def _processor(replies: list[str]) -> tuple[AIProcessor, FakeMessages]:
    messages = FakeMessages(replies)
    client = SimpleNamespace(messages=messages)
    return AIProcessor(api_key="test", model="claude-haiku-5-5", client=client), messages  # type: ignore[arg-type]


def test_valid_on_first_try() -> None:
    processor, messages = _processor([VALID_REPORT])
    report = asyncio.run(processor.generate_client_report("Менеджер: ... Клиент: ..."))
    assert report.client_name == "Анна"
    assert len(messages.systems) == 1


def test_retry_adds_clarifying_system_message() -> None:
    processor, messages = _processor(["Конечно! Вот отчёт...", VALID_REPORT])
    report = asyncio.run(processor.generate_client_report("диалог"))
    assert report.topic == "Сайт"
    assert len(messages.systems) == 2
    assert "ВНИМАНИЕ" not in messages.systems[0]
    assert "ВНИМАНИЕ" in messages.systems[1]


def test_gives_up_after_two_retries() -> None:
    processor, messages = _processor(["не json"] * 3)
    with pytest.raises(AIProcessingError) as info:
        asyncio.run(processor.generate_client_report("диалог"))
    assert len(messages.systems) == 3
    assert "неверном формате" in info.value.user_message
