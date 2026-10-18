"""Сквозной тест шлюза доступа и лимитов: настоящие апдейты PTB, вызовы Bot API подменены."""

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
        if endpoint == "sendMessage":
            return {
                "message_id": len(self.calls) + 1000,
                "date": 0,
                "chat": {"id": data["chat_id"], "type": "private"},
                "from": BOT_USER,
                "text": data["text"],
            }
        return True

    def texts(self) -> list[str]:
        return [data["text"] for endpoint, data in self.calls if endpoint == "sendMessage"]

    def endpoints(self) -> list[str]:
        return [endpoint for endpoint, _ in self.calls]


def _update(text: str, user_id: int = 1, chat_type: str = "private", update_id: int = 1) -> dict[str, Any]:
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


@pytest.fixture
def app_and_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def _run(application: Any, *updates: dict[str, Any]) -> None:
    async def scenario() -> None:
        await application.initialize()
        for raw in updates:
            await application.process_update(Update.de_json(raw, application.bot))
        await application.shutdown()

    asyncio.run(scenario())


def test_unauthorized_user_gets_prompt(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    _run(application, _update("/report"), _update("привет", update_id=2))
    assert recorder.texts() == [bot.AUTH_PROMPT, bot.AUTH_PROMPT]


def test_deep_link_activates_and_deletes_token_message(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    _run(application, _update(f"/start {OWNER}"), _update("Что в отчёте?", update_id=2))
    assert "deleteMessage" in recorder.endpoints()
    assert recorder.texts()[0].startswith("✅ Доступ открыт")
    assert recorder.texts()[1] == "Ответ модели"


def test_wrong_token_and_taken_token(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    _run(
        application,
        _update("wrong-token-CCCCCCCCCCCCCCCCCC"),
        _update(TEACHER, update_id=2),
        _update(TEACHER, user_id=2, update_id=3),
    )
    texts = recorder.texts()
    assert texts[0].startswith("Токен не подошёл")
    assert texts[1].startswith("✅ Доступ открыт")
    assert texts[2].startswith("Этот токен уже активирован")


def test_group_chat_is_left(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    _run(application, _update("/report", chat_type="group"))
    assert "leaveChat" in recorder.endpoints()
    assert recorder.texts() == []


def test_chat_limit(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    _run(application, _update(OWNER), _update("вопрос 1", update_id=2), _update("вопрос 2", update_id=3))
    texts = recorder.texts()
    assert texts[1] == "Ответ модели"
    assert texts[2].startswith("Лимит: не больше 1 сообщений в чате в час")
