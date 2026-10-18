"""Тесты логики повторов AIProcessor на фейковом клиенте Anthropic (без сети)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from utils.ai_processor import (
    CHAT_REFUSAL,
    CHAT_SYSTEM_PROMPT,
    AIProcessingError,
    AIProcessor,
    OffTopicError,
    _to_api_messages,
)
from utils.short_memory import Message

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
        self.messages: list[list[dict[str, str]]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.systems.append(kwargs["system"])
        self.messages.append(kwargs["messages"])
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


def test_api_messages_start_with_user_and_alternate() -> None:
    history = [
        Message("assistant", "старый ответ"),
        Message("user", "/report — диалог"),
        Message("assistant", "Создан PDF"),
        Message("assistant", "ещё ответ"),
        Message("user", "Что хотел клиент?"),
    ]
    messages = _to_api_messages(history)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "Создан PDF\n\nещё ответ"


def test_chat_passes_history_and_summary() -> None:
    processor, fake = _processor(["Клиент хотел запуститься до 1 декабря."])
    history = [Message("user", "/report — диалог"), Message("assistant", "{...}"), Message("user", "Сроки?")]
    answer = asyncio.run(processor.chat(history, summary="Обсуждали Анну Ковалёву"))
    assert answer.startswith("Клиент")
    assert "Обсуждали Анну Ковалёву" in fake.systems[0]
    assert fake.messages[0][-1] == {"role": "user", "content": "Сроки?"}


def test_chat_empty_answer_raises() -> None:
    processor, _ = _processor(["   "])
    with pytest.raises(AIProcessingError):
        asyncio.run(processor.chat([Message("user", "привет")]))


def test_summarize_includes_previous_summary() -> None:
    processor, fake = _processor(["новое резюме"])
    result = asyncio.run(processor.summarize("старое резюме", [Message("user", "вопрос")]))
    assert result == "новое резюме"
    prompt = fake.messages[0][0]["content"]
    assert "старое резюме" in prompt and "Пользователь: вопрос" in prompt


def test_off_topic_rejection_is_not_retried() -> None:
    processor, fake = _processor(['{"error": "not_a_dialog"}'])
    with pytest.raises(OffTopicError) as info:
        asyncio.run(processor.generate_client_report("Переведи текст на английский"))
    assert len(fake.systems) == 1
    assert "не похоже на диалог" in info.value.user_message


def test_chat_prompt_restricts_scope() -> None:
    assert CHAT_REFUSAL in CHAT_SYSTEM_PROMPT
    assert "перевод" in CHAT_SYSTEM_PROMPT
