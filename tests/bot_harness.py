"""Общая обвязка для сквозных тестов бота: настоящие апдейты PTB, сетевой слой подменён."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from telegram import Update

import bot
import config

OWNER = "owner-token-AAAAAAAAAAAAAAAA"
TEACHER = "teacher-token-BBBBBBBBBBBBBBB"
BOT_USER = {"id": 999, "is_bot": True, "first_name": "AI Report Bot", "username": "ai_report_test_bot"}


class Recorder:
    """Подменяет сетевой слой бота: запоминает вызовы и отдаёт правдоподобные ответы."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, endpoint: str, data: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        self.calls.append((endpoint, data))
        if endpoint == "getMe":
            return BOT_USER
        if endpoint == "getFile":
            return {"file_id": "f", "file_unique_id": "u", "file_size": 10, "file_path": "photos/f.jpg"}
        if endpoint in ("sendMessage", "sendDocument", "editMessageText", "editMessageReplyMarkup"):
            return {
                "message_id": len(self.calls) + 1000,
                "date": 0,
                "chat": {"id": data.get("chat_id", 1), "type": "private"},
                "from": BOT_USER,
                "text": data.get("text", ""),
            }
        return True

    def texts(self) -> list[str]:
        return [data["text"] for endpoint, data in self.calls if endpoint == "sendMessage"]

    def sent(self, endpoint: str) -> list[dict[str, Any]]:
        return [data for name, data in self.calls if name == endpoint]

    def endpoints(self) -> list[str]:
        return [endpoint for endpoint, _ in self.calls]


def message_update(text: str, user_id: int = 1, chat_type: str = "private", update_id: int = 1) -> dict[str, Any]:
    chat_id = user_id if chat_type == "private" else -100
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
            "text": text,
            **({"entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}
               if text.startswith("/") else {}),
        },
    }


def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Recorder]:
    monkeypatch.setattr(config, "ACCESS_FILE", tmp_path / "users.json")
    settings = config.Settings(
        telegram_bot_token="123456:TEST_TOKEN_FOR_UNIT_TESTS_ONLY_xxxxx",
        anthropic_api_key="test",
        access_tokens=f"owner:{OWNER},teacher:{TEACHER}",
        chat_per_hour=1,
    )
    application = bot.build_application(settings)
    recorder = Recorder()

    async def fake_post(self: Any, endpoint: str, data: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Any:
        return await recorder(endpoint, data or {})

    # атрибуты экземпляра бота заморожены PTB, поэтому подменяем метод класса
    monkeypatch.setattr(type(application.bot), "_post", fake_post)

    async def fake_chat(history: Any, summary: str = "") -> str:
        return "Ответ модели"

    monkeypatch.setattr(application.bot_data["ai"], "chat", fake_chat)
    return application, recorder


def run_updates(application: Any, *updates: dict[str, Any]) -> None:
    async def scenario() -> None:
        await application.initialize()
        for raw in updates:
            await application.process_update(Update.de_json(raw, application.bot))
        await application.shutdown()

    asyncio.run(scenario())


def callback_update(data: str, user_id: int = 1, update_id: int = 1) -> dict[str, Any]:
    """Нажатие inline-кнопки под сообщением бота."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
            "chat_instance": "test",
            "data": data,
            "message": {
                "message_id": 500 + update_id,
                "date": 0,
                "chat": {"id": user_id, "type": "private"},
                "from": BOT_USER,
                "text": "сообщение бота",
            },
        },
    }


def photo_update(user_id: int = 1, update_id: int = 1) -> dict[str, Any]:
    """Пользователь прислал фото (скриншот)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
            "photo": [{"file_id": "f", "file_unique_id": "u", "width": 800, "height": 1600, "file_size": 10}],
        },
    }
