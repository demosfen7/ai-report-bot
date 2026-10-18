"""Сквозной тест шлюза доступа и лимитов: настоящие апдейты PTB, вызовы Bot API подменены."""

from __future__ import annotations

from pathlib import Path

import pytest
from bot_harness import OWNER, TEACHER, make_app, message_update, run_updates

import bot


@pytest.fixture
def app_and_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return make_app(tmp_path, monkeypatch)


def test_unauthorized_user_gets_prompt(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    run_updates(application, message_update("/report"), message_update("привет", update_id=2))
    assert recorder.texts() == [bot.AUTH_PROMPT, bot.AUTH_PROMPT]


def test_deep_link_activates_and_deletes_token_message(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    run_updates(application, message_update(f"/start {OWNER}"), message_update("Что в отчёте?", update_id=2))
    assert "deleteMessage" in recorder.endpoints()
    assert recorder.texts()[0].startswith("✅ Доступ открыт")
    assert recorder.texts()[1] == "Ответ модели"


def test_wrong_token_and_taken_token(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    run_updates(
        application,
        message_update("wrong-token-CCCCCCCCCCCCCCCCCC"),
        message_update(TEACHER, update_id=2),
        message_update(TEACHER, user_id=2, update_id=3),
    )
    texts = recorder.texts()
    assert texts[0].startswith("Токен не подошёл")
    assert texts[1].startswith("✅ Доступ открыт")
    assert texts[2].startswith("Этот токен уже активирован")


def test_group_chat_is_left(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    run_updates(application, message_update("/report", chat_type="group"))
    assert "leaveChat" in recorder.endpoints()
    assert recorder.texts() == []


def test_chat_limit(app_and_recorder) -> None:
    application, recorder = app_and_recorder
    run_updates(application, message_update(OWNER), message_update("вопрос 1", update_id=2), message_update("вопрос 2", update_id=3))
    texts = recorder.texts()
    assert texts[1] == "Ответ модели"
    assert texts[2].startswith("Лимит: не больше 1 сообщений в чате в час")
