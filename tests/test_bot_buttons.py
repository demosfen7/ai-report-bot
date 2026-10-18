"""Сквозные тесты кнопок меню, тестового диалога и ручного ввода с «Пропустить»."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bot_harness import OWNER, callback_update, make_app, message_update, run_updates

import bot
from utils.ai_processor import OffTopicError
from utils.keyboards import BTN_CLEAR, BTN_PRODUCT, BTN_REPORT, BTN_TEST, CB_SKIP, MAIN_MENU
from utils.scraper import ScrapingError
from utils.validators import ClientReport, ProductCard

REPORT = ClientReport(
    client_name="Анна Ковалёва",
    topic="Редизайн магазина",
    main_request="Ускорить сайт",
    mood="Заинтересованная",
    next_steps=["Отправить КП"],
)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bot.config, "JSON_DIR", tmp_path / "json")
    application, recorder = make_app(tmp_path, monkeypatch)
    application.bot_data["pdf"]._output_dir = tmp_path / "reports"
    return application, recorder


def _activate() -> dict[str, Any]:
    return message_update(OWNER, update_id=1)


def _keyboard_callbacks(data: dict[str, Any]) -> list[str]:
    markup = json.loads(data["reply_markup"]) if isinstance(data["reply_markup"], str) else data["reply_markup"]
    if not isinstance(markup, dict):
        markup = markup.to_dict()
    return [button["callback_data"] for row in markup["inline_keyboard"] for button in row]


def test_activation_shows_main_menu(app) -> None:
    application, recorder = app
    run_updates(application, _activate())
    markup = recorder.sent("sendMessage")[0]["reply_markup"]
    assert markup == MAIN_MENU


def test_test_dialog_is_copyable_and_has_report_button(app) -> None:
    application, recorder = app
    run_updates(application, _activate(), message_update(BTN_TEST, update_id=2))
    data = recorder.sent("sendMessage")[-1]
    assert "<pre>" in data["text"] and "Менеджер:" in data["text"]
    assert len(data["text"]) <= 4096
    assert _keyboard_callbacks(data)[0].startswith("report_test:")


def test_report_from_test_dialog_button(app, monkeypatch: pytest.MonkeyPatch) -> None:
    application, recorder = app
    received: list[str] = []

    async def fake_report(dialog: str) -> ClientReport:
        received.append(dialog)
        return REPORT

    monkeypatch.setattr(application.bot_data["ai"], "generate_client_report", fake_report)
    run_updates(application, _activate(), callback_update("report_test:0", update_id=2))

    assert received == [application.bot_data["test_dialogs"][0].text]
    assert len(recorder.sent("sendDocument")) == 1
    summary = recorder.sent("sendMessage")[-1]
    assert "Анна Ковалёва" in summary["text"]
    assert _keyboard_callbacks(summary) == ["letter", "new_report"]
    assert "answerCallbackQuery" in recorder.endpoints()


def test_menu_button_text_is_not_sent_to_chat_model(app, monkeypatch: pytest.MonkeyPatch) -> None:
    application, recorder = app
    asked: list[Any] = []

    async def fake_chat(history: Any, summary: str = "") -> str:
        asked.append(history)
        return "ответ"

    monkeypatch.setattr(application.bot_data["ai"], "chat", fake_chat)
    run_updates(application, _activate(), message_update(BTN_CLEAR, update_id=2))
    assert asked == []
    assert recorder.texts()[-1] == "🧹 Память диалога очищена."


def test_not_a_dialog_is_rejected(app, monkeypatch: pytest.MonkeyPatch) -> None:
    application, recorder = app

    async def refuse(dialog: str) -> ClientReport:
        raise OffTopicError("Это не похоже на диалог с клиентом", detail="not_a_dialog")

    monkeypatch.setattr(application.bot_data["ai"], "generate_client_report", refuse)
    run_updates(
        application,
        _activate(),
        message_update(BTN_REPORT, update_id=2),
        message_update("Переведи на английский: " + "текст статьи о погоде. " * 5, update_id=3),
    )
    assert recorder.sent("sendDocument") == []
    assert recorder.sent("editMessageText")[-1]["text"].startswith("Это не похоже на диалог")


def test_manual_product_with_skip_buttons(app, monkeypatch: pytest.MonkeyPatch) -> None:
    application, recorder = app
    raw_inputs: list[dict[str, Any]] = []

    def blocked(url: str, timeout: float) -> None:
        raise ScrapingError("http_error", "HTTP 404")

    async def fake_card(raw: dict[str, Any]) -> ProductCard:
        raw_inputs.append(raw)
        return ProductCard(
            product_name=raw["title"], short_description="", price=None, currency=None,
            key_features=[], image_url=None, source_url=None,
        )

    monkeypatch.setattr(bot, "ensure_public_host", lambda url: None)
    monkeypatch.setattr(bot, "scrape_product_static", blocked)
    monkeypatch.setattr(application.bot_data["ai"], "generate_product_card", fake_card)
    run_updates(
        application,
        _activate(),
        message_update(BTN_PRODUCT, update_id=2),
        message_update("https://shop.example/item/1", update_id=3),
        message_update("Чайник Bosch", update_id=4),
        callback_update(CB_SKIP, update_id=5),
        callback_update(CB_SKIP, update_id=6),
        callback_update(CB_SKIP, update_id=7),
        callback_update(CB_SKIP, update_id=8),
    )
    assert raw_inputs == [{"title": "Чайник Bosch", "description": None, "price_and_currency": None, "features": []}]
    assert len(recorder.sent("sendDocument")) == 1
    assert _keyboard_callbacks(recorder.sent("sendDocument")[0]) == ["new_product"]
