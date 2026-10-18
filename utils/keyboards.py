"""Кнопки бота: постоянное меню под полем ввода и inline-кнопки под сообщениями."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import filters

BTN_REPORT = "📄 Отчёт по диалогу"
BTN_PRODUCT = "🛍 Карточка товара"
BTN_TEST = "🧪 Тестовый диалог"
BTN_HELP = "❓ Помощь"
BTN_CLEAR = "🧹 Очистить память"
BTN_CANCEL = "✖️ Отмена"
MENU_BUTTONS = (BTN_REPORT, BTN_PRODUCT, BTN_TEST, BTN_HELP, BTN_CLEAR, BTN_CANCEL)

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_REPORT, BTN_PRODUCT], [BTN_TEST, BTN_HELP], [BTN_CLEAR, BTN_CANCEL]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Вопрос по отчёту или кнопка меню",
)

# callback_data inline-кнопок
CB_SKIP = "skip"
CB_REPORT_TEST = "report_test"  # report_test:<номер диалога>
CB_LETTER = "letter"
CB_NEW_REPORT = "new_report"
CB_NEW_PRODUCT = "new_product"


def menu_button(text: str) -> filters.BaseFilter:
    """Фильтр «нажата кнопка меню с этим текстом»."""
    return filters.Text([text])


# Текст, который ввёл пользователь, а не кнопка меню и не команда.
TEXT_INPUT = filters.TEXT & ~filters.COMMAND & ~filters.Text(list(MENU_BUTTONS))


def skip_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Пропустить» для необязательных шагов ручного ввода."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data=CB_SKIP)]])


def test_dialog_keyboard(index: int) -> InlineKeyboardMarkup:
    """Кнопка под тестовым диалогом: сразу сделать по нему отчёт."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📄 Сделать отчёт из этого диалога", callback_data=f"{CB_REPORT_TEST}:{index}")]]
    )


def after_report_keyboard() -> InlineKeyboardMarkup:
    """Кнопки под готовым отчётом."""
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✉️ Письмо клиенту", callback_data=CB_LETTER),
            InlineKeyboardButton("📄 Новый отчёт", callback_data=CB_NEW_REPORT),
        ]]
    )


def after_product_keyboard() -> InlineKeyboardMarkup:
    """Кнопка под готовой карточкой товара."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛍 Ещё одна карточка", callback_data=CB_NEW_PRODUCT)]]
    )
