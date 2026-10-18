"""AI Report Bot — Telegram-бот: PDF-отчёт по диалогу и PDF-карточка товара.

Запуск: `python bot.py` (настройки — в `.env`, см. `.env.example`).
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import sys
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from telegram import BotCommand, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
from utils.access import TOKEN_RE, AccessManager, ActivationResult, parse_access_tokens
from utils.ai_processor import AIProcessingError, AIProcessor, OffTopicError
from utils.concurrency import PerUserUpdateProcessor
from utils.examples import TestDialog, load_test_dialogs
from utils.keyboards import (
    BTN_CANCEL,
    BTN_CLEAR,
    BTN_HELP,
    BTN_PRODUCT,
    BTN_REPORT,
    BTN_TEST,
    CB_LETTER,
    CB_NEW_PRODUCT,
    CB_NEW_REPORT,
    CB_REPORT_TEST,
    CB_SKIP,
    MAIN_MENU,
    TEXT_INPUT,
    after_product_keyboard,
    after_report_keyboard,
    menu_button,
    skip_keyboard,
    test_dialog_keyboard,
)
from utils.maintenance import delete_old_files
from utils.pdf_generator import PDFGenerationError, PDFGenerator, format_price
from utils.scraper import (
    ScrapingError,
    ScrapedProduct,
    download_image_as_data_uri,
    ensure_public_host,
    image_bytes_to_data_uri,
    is_browser_available,
    normalize_input_url,
    scrape_product_static,
    scrape_with_browser_fallback,
    should_try_browser,
)
from utils.rate_limit import RateLimiter, format_wait
from utils.short_memory import ShortMemory
from utils.validators import ClientReport, ProductCard, clean_text_list, normalize_url, save_model_json

ORIGIN_REQUEST = {
    "page": "/product — карточка товара по ссылке {url}",
    "manual": "/product — карточка товара, данные введены вручную",
    "screenshot": "/product — карточка товара по скриншоту страницы {url}",
}

logger = logging.getLogger("ai_report_bot")

# Состояния ConversationHandler
(
    REPORT_WAIT_TEXT,
    PRODUCT_WAIT_URL,
    MANUAL_NAME,
    MANUAL_DESCRIPTION,
    MANUAL_PRICE,
    MANUAL_FEATURES,
    MANUAL_IMAGE,
) = range(7)

MIN_DIALOG_CHARS = 50
MAX_DIALOG_CHARS = 100_000
MAX_TXT_FILE_BYTES = 1_000_000
# Anthropic принимает картинки до 5 МБ.
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
SCREENSHOT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
SCREENSHOT = filters.PHOTO | filters.Document.IMAGE
SKIP_WORDS = {"пропустить", "пропуск", "skip", "-", "—", "нет"}
# Сколько символов исходного диалога запоминать для вопросов после /report.
MEMORY_DIALOG_CHARS = 8000
MAX_CHAT_MESSAGE_CHARS = 4000
TELEGRAM_TEXT_LIMIT = 4096

LETTER_REQUEST = (
    "Напиши короткое деловое письмо клиенту по итогам консультации из последнего отчёта: "
    "поблагодари за разговор, кратко зафиксируй запрос клиента и договорённости, "
    "перечисли следующие шаги со сроками."
)
DIALOG_EXAMPLES_DIR = config.BASE_DIR / "examples"

HELP_TEXT = (
    "<b>Кнопки меню</b>\n"
    f"{BTN_REPORT} — PDF-отчёт по диалогу с клиентом\n"
    f"{BTN_PRODUCT} — PDF-карточка товара по ссылке\n"
    f"{BTN_TEST} — пример диалога, чтобы попробовать отчёт\n"
    f"{BTN_CLEAR} — забыть нашу переписку\n"
    f"{BTN_CANCEL} — прервать текущий сценарий\n\n"
    "То же самое доступно командами /report, /product, /clear_chat, /cancel, /help.\n\n"
    "<b>Память</b>\n"
    "После отчёта или карточки можно задать вопрос обычным сообщением: "
    "«что клиент хотел по срокам?», «напиши клиенту письмо по итогам». "
    "Я помню последние сообщения и созданные документы; старая часть переписки "
    "сворачивается в краткое резюме. Память очищается кнопкой "
    f"{BTN_CLEAR} или при перезапуске бота.\n\n"
    "Я работаю только с отчётами и карточками товаров — посторонние задачи "
    "(переводы, справки, тексты на свободную тему) не выполняю."
)
START_TEXT = (
    "Привет! Я <b>AI Report Bot</b> — превращаю разговоры с клиентами и страницы товаров "
    "в аккуратные PDF.\n\n"
    f"{BTN_REPORT} — пришлите расшифровку разговора с клиентом, и я соберу отчёт: "
    "клиент, тема, запрос, настроение, следующие шаги.\n"
    f"{BTN_PRODUCT} — пришлите ссылку на товар, и я сделаю карточку. "
    "Если сайт не отдаёт данные, заполним её вручную.\n"
    f"{BTN_TEST} — готовый пример диалога, чтобы попробовать.\n\n"
    "После этого можно задавать вопросы по документам обычным сообщением.\n\n"
    "Кнопки меню — под полем ввода."
)
AUTH_PROMPT = (
    "🔒 Бот доступен по приглашению.\n\n"
    "Отправьте токен доступа сообщением или откройте ссылку-приглашение, которую вам прислали."
)
GENERIC_ERROR_TEXT = (
    "Произошла непредвиденная ошибка. Попробуйте ещё раз — кнопки меню под полем ввода."
)


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _ai(context: ContextTypes.DEFAULT_TYPE) -> AIProcessor:
    """AI-процессор из общего хранилища приложения."""
    return context.application.bot_data["ai"]


def _pdf(context: ContextTypes.DEFAULT_TYPE) -> PDFGenerator:
    """PDF-генератор из общего хранилища приложения."""
    return context.application.bot_data["pdf"]


def _memory(context: ContextTypes.DEFAULT_TYPE) -> ShortMemory:
    """Кратковременная память диалогов из общего хранилища приложения."""
    return context.application.bot_data["memory"]


def _access(context: ContextTypes.DEFAULT_TYPE) -> AccessManager:
    """Менеджер доступа по токенам."""
    return context.application.bot_data["access"]


def _limiter(context: ContextTypes.DEFAULT_TYPE) -> RateLimiter:
    """Лимиты частоты запросов."""
    return context.application.bot_data["limiter"]


async def _check_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> bool:
    """Учесть платное действие; если лимит исчерпан — сообщить пользователю и вернуть False."""
    limiter = _limiter(context)
    wait = limiter.hit(update.effective_user.id, kind)
    if wait is None:
        return True
    what = "PDF-документов" if kind == "pdf" else "сообщений в чате"
    logger.warning("Лимит %s исчерпан пользователем %s", kind, update.effective_user.id)
    await update.effective_message.reply_text(
        f"Лимит: не больше {limiter.limit_of(kind)} {what} в час. Попробуйте {format_wait(wait)}."
    )
    return False


async def _maybe_summarize(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Свернуть старую часть истории в резюме. Ошибки не пробрасываются: ответ уже отправлен."""
    memory = _memory(context)
    if not memory.needs_summary(user_id):
        return
    batch = memory.messages_to_summarize(user_id)
    try:
        summary = await _ai(context).summarize(memory.get_summary(user_id), batch)
    except Exception:
        logger.warning("Память: не удалось сделать резюме, попробую позже", exc_info=True)
        return
    memory.apply_summary(user_id, summary, len(batch))
    logger.info("Память: %d реплик пользователя %s свёрнуты в резюме", len(batch), user_id)


async def _remember_document(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, request: str, result: str
) -> None:
    """Записать в память созданный документ, чтобы потом обсуждать его в чате."""
    memory = _memory(context)
    memory.add_user_message(user_id, request)
    memory.add_assistant_message(user_id, result)
    await _maybe_summarize(context, user_id)


def _split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Разбить длинный ответ на части не длиннее лимита Telegram, по абзацам."""
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def _ack(update: Update) -> None:
    """Ответить на нажатие inline-кнопки, чтобы у неё пропали «часики»."""
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except TelegramError as exc:
            logger.debug("Не удалось ответить на callback: %s", exc)


async def _input_text(update: Update) -> str:
    """Текст ответа пользователя; нажатие «⏭ Пропустить» превращается в «пропустить»."""
    query = update.callback_query
    if query:
        await _ack(update)
        try:
            await query.edit_message_reply_markup(None)  # кнопку больше не нажать повторно
        except TelegramError as exc:
            logger.debug("Не удалось убрать кнопку: %s", exc)
        return "пропустить"
    return (update.effective_message.text or "").strip()


def _test_dialogs(context: ContextTypes.DEFAULT_TYPE) -> list[TestDialog]:
    """Тестовые диалоги, загруженные при старте."""
    return context.application.bot_data["test_dialogs"]


def _is_skip(text: str) -> bool:
    """Пользователь хочет пропустить необязательный шаг."""
    return text.strip().lower().strip(".!") in SKIP_WORDS


def _short(text: str, limit: int) -> str:
    """Обрезать строку до `limit` символов с многоточием."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _set_status(status: Message | None, text: str) -> None:
    """Обновить статусное сообщение, не падая, если Telegram отказал."""
    if status is None:
        return
    try:
        await status.edit_text(text)
    except TelegramError as exc:
        logger.debug("Не удалось обновить статус: %s", exc)


async def _delete_quietly(message: Message | None) -> None:
    """Удалить сообщение, игнорируя ошибки Telegram."""
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError as exc:
        logger.debug("Не удалось удалить сообщение: %s", exc)


async def _send_pdf(
    message: Message, path: Path, caption: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Отправить PDF-файл в чат."""
    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    with path.open("rb") as file:
        await message.chat.send_document(
            document=file,
            filename=path.name,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    logger.info("PDF отправлен в чат %s: %s", message.chat_id, path.name)


def _report_summary(report: ClientReport) -> str:
    """Краткая сводка отчёта для Telegram (HTML)."""
    e = html.escape
    lines = [
        "<b>Краткая сводка</b>",
        f"<b>Клиент:</b> {e(report.client_name)}",
        f"<b>Тема:</b> {e(report.topic)}",
        f"<b>Основной запрос:</b> {e(_short(report.main_request, 700))}",
        f"<b>Настроение:</b> {e(report.mood)}",
    ]
    if report.next_steps:
        lines.append("<b>Следующие шаги:</b>")
        lines += [f"{i}. {e(_short(step, 200))}" for i, step in enumerate(report.next_steps[:10], 1)]
    else:
        lines.append("<b>Следующие шаги:</b> не зафиксированы")
    lines += [
        "",
        f"<b>Истинный запрос:</b> {e(_short(report.true_need, 600))}",
        f"<b>Рекомендация:</b> {e(_short(report.recommendation, 600))}",
    ]
    return _short("\n".join(lines), TELEGRAM_TEXT_LIMIT)


def _product_caption(card: ProductCard, image_missing_note: bool) -> str:
    """Подпись к PDF-карточке (лимит подписи Telegram — 1024 символа)."""
    e = html.escape
    lines = [
        "<b>Готово! Вот ваша карточка товара в PDF.</b>",
        "",
        f"<b>{e(_short(card.product_name, 200))}</b>",
        f"Цена: {e(format_price(card.price, card.currency))}",
    ]
    if card.key_features:
        lines.append(f"Характеристик: {len(card.key_features)}")
    if image_missing_note:
        lines.append("\nФото загрузить не удалось — карточка создана без изображения.")
    return _short("\n".join(lines), 1024)


async def _read_dialog_text(message: Message) -> str | None:
    """Получить текст диалога из сообщения или .txt-файла."""
    if message.text:
        return message.text
    document = message.document
    if document is None:
        return None
    if document.file_size and document.file_size > MAX_TXT_FILE_BYTES:
        await message.reply_text("Файл слишком большой. Пришлите .txt до 1 МБ.")
        return None
    telegram_file = await document.get_file()
    raw = bytes(await telegram_file.download_as_bytearray())
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    await message.reply_text("Не удалось прочитать файл. Сохраните его в кодировке UTF-8.")
    return None


# --------------------------------------------------------------------------- #
# Доступ
# --------------------------------------------------------------------------- #


async def access_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропустить дальше только личные чаты пользователей с действующим токеном.

    Работает в группе хендлеров -1, то есть до всех остальных. Неавторизованный
    пользователь может только активировать токен: сообщением или ссылкой
    `t.me/<бот>?start=<токен>`.
    """
    if not isinstance(update, Update):
        raise ApplicationHandlerStop
    user, chat, message = update.effective_user, update.effective_chat, update.effective_message
    if user is None or chat is None or user.is_bot:
        raise ApplicationHandlerStop

    if chat.type != ChatType.PRIVATE:
        logger.warning("Доступ: апдейт из чата %s (%s) — бот работает только в личке, выхожу", chat.id, chat.type)
        try:
            await chat.leave()
        except TelegramError as exc:
            logger.debug("Не удалось выйти из чата %s: %s", chat.id, exc)
        raise ApplicationHandlerStop

    access = _access(context)
    if access.is_authorized(user.id):
        return
    if message is None:
        raise ApplicationHandlerStop

    candidate = (message.text or "").strip()
    if candidate.startswith("/start"):
        candidate = candidate.removeprefix("/start").strip()
    elif candidate.startswith("/"):
        candidate = ""
    if not TOKEN_RE.match(candidate):
        # обычный текст или команда — не попытка ввести токен
        await message.reply_text(AUTH_PROMPT)
        raise ApplicationHandlerStop

    if _limiter(context).hit(user.id, "auth") is not None:
        logger.warning("Доступ: слишком много попыток ввести токен от %s", user.id)
        raise ApplicationHandlerStop

    result = access.activate(user.id, candidate, user.username)
    await _delete_quietly(message)  # токен не должен оставаться в истории чата
    if result is ActivationResult.ACTIVATED:
        await message.chat.send_message(
            "✅ Доступ открыт.\n\n" + START_TEXT, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU
        )
    elif result is ActivationResult.TAKEN:
        await message.chat.send_message(
            "Этот токен уже активирован другим аккаунтом. Попросите владельца бота выдать новый."
        )
    else:
        logger.info("Доступ: неверный токен от %s", user.id)
        await message.chat.send_message("Токен не подошёл. Проверьте его и отправьте ещё раз.")
    raise ApplicationHandlerStop


# --------------------------------------------------------------------------- #
# Общие команды
# --------------------------------------------------------------------------- #


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/start — приветствие; заодно сбрасывает текущий сценарий."""
    logger.info("/start от пользователя %s", update.effective_user.id if update.effective_user else "?")
    context.user_data.clear()
    await update.effective_message.reply_text(START_TEXT, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — список команд. Текущий сценарий не прерывает."""
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel — отмена текущего сценария."""
    logger.info("Сценарий отменён пользователем %s", update.effective_user.id if update.effective_user else "?")
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Действие отменено. Выберите, что сделать, кнопками меню.", reply_markup=MAIN_MENU
    )
    return ConversationHandler.END


async def unexpected_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Неподходящий тип сообщения внутри сценария (стикер, голосовое и т.п.)."""
    await update.effective_message.reply_text(
        f"Я жду текстовое сообщение. Чтобы выйти из сценария, нажмите «{BTN_CANCEL}»."
    )


async def outside_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нетекстовое сообщение вне сценария — подсказать команды."""
    await update.effective_message.reply_text(
        "Выберите действие кнопками меню или напишите вопрос по документам текстом.",
        reply_markup=MAIN_MENU,
    )


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободный чат вне сценариев: ответ Claude с учётом памяти о переписке."""
    text = (update.effective_message.text or "").strip()[:MAX_CHAT_MESSAGE_CHARS]
    if text:
        await _chat_reply(update, context, text)


async def letter_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «✉️ Письмо клиенту» под отчётом: письмо по последнему отчёту из памяти."""
    await _ack(update)
    await _chat_reply(update, context, LETTER_REQUEST)
    return ConversationHandler.END


async def _chat_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Ответ Claude на реплику пользователя с учётом памяти."""
    message = update.effective_message
    user_id = update.effective_user.id
    logger.info("Чат: сообщение от %s (%d символов)", user_id, len(text))

    if not await _check_limit(update, context, "chat"):
        return
    memory = _memory(context)
    memory.add_user_message(user_id, text)
    await message.chat.send_action(ChatAction.TYPING)
    try:
        answer = await _ai(context).chat(memory.get_history(user_id), memory.get_summary(user_id))
    except AIProcessingError as exc:
        memory.discard_last_user_message(user_id)
        logger.error("Чат: ошибка LLM: %s", exc.detail or exc.user_message)
        await message.reply_text(exc.user_message)
        return

    memory.add_assistant_message(user_id, answer)
    for part in _split_text(answer):
        await message.reply_text(part)
    await _maybe_summarize(context, user_id)


async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear_chat — очистить историю и резюме этого пользователя. Сценарий не прерывает."""
    _memory(context).clear(update.effective_user.id)
    logger.info("Память пользователя %s очищена", update.effective_user.id)
    await update.effective_message.reply_text("🧹 Память диалога очищена.")


async def send_test_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🧪 Тестовый диалог»: прислать пример, который можно скопировать.

    Текущий сценарий не прерывает: если бот ждёт диалог для отчёта, пример
    можно сразу скопировать и отправить.
    """
    dialogs = _test_dialogs(context)
    if not dialogs:
        await update.effective_message.reply_text("Тестовые диалоги не найдены.")
        return
    last = context.user_data.get("last_test_dialog")
    choices = [i for i in range(len(dialogs)) if i != last] or [0]
    index = random.choice(choices)
    context.user_data["last_test_dialog"] = index
    dialog = dialogs[index]
    logger.info("Тестовый диалог №%d для пользователя %s", index, update.effective_user.id)
    await update.effective_message.reply_text(
        f"🧪 <b>Тестовый диалог: {html.escape(dialog.title)}</b>\n"
        f"Скопируйте текст ниже, нажмите «{BTN_REPORT}» и вставьте его — "
        "или сразу нажмите кнопку под сообщением.\n\n"
        f"<pre>{html.escape(dialog.text)}</pre>",
        parse_mode=ParseMode.HTML,
        reply_markup=test_dialog_keyboard(index),
    )


async def report_from_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «📄 Сделать отчёт из этого диалога» под тестовым диалогом."""
    await _ack(update)
    dialogs = _test_dialogs(context)
    try:
        index = int(update.callback_query.data.split(":", 1)[1])
        dialog = dialogs[index].text
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Этот пример больше недоступен. Нажмите «🧪 Тестовый диалог» ещё раз.")
        return ConversationHandler.END
    context.user_data.clear()
    await _run_report(update, context, dialog)
    return ConversationHandler.END


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажата inline-кнопка из уже завершённого сценария."""
    if update.callback_query:
        try:
            await update.callback_query.answer("Эта кнопка уже неактуальна", show_alert=False)
        except TelegramError as exc:
            logger.debug("Не удалось ответить на callback: %s", exc)


# --------------------------------------------------------------------------- #
# Сценарий /report
# --------------------------------------------------------------------------- #


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/report — запросить текст диалога."""
    await _ack(update)
    context.user_data.clear()
    logger.info("/report: старт сценария (чат %s)", update.effective_chat.id)
    await update.effective_message.reply_text(
        "Отправьте текст диалога с клиентом, и я подготовлю PDF-отчёт.\n\n"
        "Можно сообщением или .txt-файлом. Нет диалога под рукой — нажмите "
        f"«{BTN_TEST}».",
        reply_markup=MAIN_MENU,
    )
    return REPORT_WAIT_TEXT


async def report_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получить диалог, вызвать Claude, сгенерировать и отправить PDF."""
    message = update.effective_message
    dialog = await _read_dialog_text(message)
    if dialog is None:
        return REPORT_WAIT_TEXT
    dialog = dialog.strip()
    logger.info("/report: получен диалог, %d символов (чат %s)", len(dialog), message.chat_id)

    if len(dialog) < MIN_DIALOG_CHARS:
        await message.reply_text("Текст слишком короткий для анализа. Пришлите полный диалог с клиентом.")
        return REPORT_WAIT_TEXT
    if len(dialog) > MAX_DIALOG_CHARS:
        await message.reply_text(
            f"Текст слишком длинный ({len(dialog):,} символов). Максимум — {MAX_DIALOG_CHARS:,}.".replace(",", " ")
        )
        return REPORT_WAIT_TEXT
    context.user_data.clear()
    await _run_report(update, context, dialog)
    return ConversationHandler.END


async def _run_report(update: Update, context: ContextTypes.DEFAULT_TYPE, dialog: str) -> None:
    """Пайплайн отчёта: лимит → Claude → JSON → PDF → отправка → память."""
    message = update.effective_message
    if not await _check_limit(update, context, "pdf"):
        return
    status = await message.chat.send_message("Анализирую диалог…")
    try:
        await message.chat.send_action(ChatAction.TYPING)
        report = await _ai(context).generate_client_report(dialog)
        await asyncio.to_thread(save_model_json, report, config.JSON_DIR, "report")
        await _set_status(status, "Формирую PDF…")
        pdf_path = await asyncio.to_thread(_pdf(context).generate_report_pdf, report)
        await _send_pdf(message, pdf_path, "<b>Готово! Вот ваш отчёт в PDF.</b>")
        await message.chat.send_message(
            _report_summary(report), parse_mode=ParseMode.HTML, reply_markup=after_report_keyboard()
        )
        await _delete_quietly(status)
        excerpt = dialog[:MEMORY_DIALOG_CHARS]
        if len(dialog) > MEMORY_DIALOG_CHARS:
            excerpt += "\n[…диалог обрезан]"
        await _remember_document(
            context,
            update.effective_user.id,
            f"/report — транскрибация диалога с клиентом для отчёта:\n{excerpt}",
            "Создан PDF «Отчёт по клиентской консультации». Данные отчёта (JSON):\n"
            + report.model_dump_json(),
        )
    except OffTopicError as exc:
        await _set_status(status, exc.user_message)
    except AIProcessingError as exc:
        logger.error("/report: ошибка LLM: %s", exc.detail or exc.user_message)
        await _set_status(status, f"{exc.user_message}\n\nПопробовать снова: «{BTN_REPORT}».")
    except PDFGenerationError:
        await _set_status(status, f"Не удалось создать PDF. Попробуйте ещё раз: «{BTN_REPORT}».")
    except Exception:
        logger.exception("/report: непредвиденная ошибка")
        await _set_status(status, GENERIC_ERROR_TEXT)


# --------------------------------------------------------------------------- #
# Сценарий /product
# --------------------------------------------------------------------------- #


async def product_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/product — запросить ссылку на товар."""
    await _ack(update)
    context.user_data.clear()
    logger.info("/product: старт сценария (чат %s)", update.effective_chat.id)
    await update.effective_message.reply_text(
        "Отправьте ссылку на товар, и я создам карточку в PDF.\n\n"
        "Если магазин защищён от автоматического чтения (Ozon, Wildberries и т.п.), "
        "пришлите сразу скриншот страницы товара — я распознаю его сам.",
        reply_markup=MAIN_MENU
    )
    return PRODUCT_WAIT_URL


async def product_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получить URL, попытаться выполнить scraping; при неудаче — ручной режим."""
    message = update.effective_message
    url: str | None = None
    try:
        url = normalize_input_url(message.text or "")
        await asyncio.to_thread(ensure_public_host, url)
    except ScrapingError as exc:
        logger.info("/product: ссылка не принята (%s): %s", exc.reason, exc)
        if exc.reason == "invalid_url":
            await message.reply_text("Это не похоже на ссылку на товар. Отправьте URL вида https://shop.example/item")
            return PRODUCT_WAIT_URL
        return await _start_manual_mode(message, context, url=url, reason=exc.hint)

    logger.info("/product: получен URL %s", url)
    if not await _check_limit(update, context, "pdf"):
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data["pdf_counted"] = True
    status = await message.reply_text("Загружаю страницу товара…")
    try:
        scraped: ScrapedProduct = await _scrape(url, status, context)
    except ScrapingError as exc:
        logger.warning("/product: scraping не удался (%s): %s", exc.reason, exc)
        await _delete_quietly(status)
        return await _start_manual_mode(message, context, url=url, reason=exc.hint)
    except Exception:
        logger.exception("/product: непредвиденная ошибка scraping")
        await _delete_quietly(status)
        return await _start_manual_mode(message, context, url=url, reason="ошибка при чтении страницы")

    await _set_status(status, "Данные получены, формирую карточку…")
    raw_data = scraped.to_dict()
    raw_data.pop("image_url", None)  # фото и источник берём из scraping, а не из ответа модели
    await _build_and_send_product(
        message, context, status,
        user_id=update.effective_user.id,
        raw_data=raw_data,
        image_url=scraped.image_url,
        image_bytes=None,
        source_url=scraped.source_url,
        origin="page",
    )
    return ConversationHandler.END


async def _scrape(url: str, status: Message, context: ContextTypes.DEFAULT_TYPE) -> ScrapedProduct:
    """Обычный HTTP-запрос, а если он не дал данных — страница в headless-браузере."""
    try:
        return await asyncio.to_thread(scrape_product_static, url, config.HTTP_TIMEOUT)
    except ScrapingError as exc:
        if not (context.application.bot_data["browser_enabled"] and should_try_browser(exc)):
            raise
        logger.info("/product: HTTP не дал данных (%s), открываю страницу в браузере", exc.reason)
        await _set_status(status, "Страница собирается JavaScript-ом — открываю её в браузере…")
        return await asyncio.to_thread(
            scrape_with_browser_fallback, url, exc, config.BROWSER_TIMEOUT
        )


async def _start_manual_mode(
    message: Message, context: ContextTypes.DEFAULT_TYPE, url: str | None, reason: str
) -> int:
    """Сообщить о неудаче scraping и начать пошаговый ручной ввод."""
    context.user_data["manual"] = {"source_url": url}
    logger.info("/product: переход в ручной режим (причина: %s)", reason)
    await message.reply_text(
        f"Не удалось автоматически получить данные с сайта: {reason}.\n\n"
        "Есть два способа сделать карточку:\n"
        "📸 <b>Пришлите скриншот страницы товара</b> — я сам распознаю название, цену "
        "и характеристики.\n"
        "✍️ Или <b>введите название товара</b> — заполним карточку по шагам.\n\n"
        f"Передумали — нажмите «{BTN_CANCEL}».",
        parse_mode=ParseMode.HTML,
    )
    return MANUAL_NAME


async def product_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Скриншот вместо ссылки: сразу распознать товар."""
    return await _product_from_screenshot(update, context, retry_state=PRODUCT_WAIT_URL)


async def manual_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Скриншот в ручном режиме, когда сайт не отдал страницу."""
    return await _product_from_screenshot(update, context, retry_state=MANUAL_NAME)


async def _read_screenshot(message: Message) -> tuple[bytes, str] | None:
    """Скачать скриншот из сообщения (фото или картинка файлом)."""
    if message.photo:
        telegram_file = await message.photo[-1].get_file()
        return bytes(await telegram_file.download_as_bytearray()), "image/jpeg"
    document = message.document
    if document is None or document.mime_type not in SCREENSHOT_TYPES:
        await message.reply_text("Пришлите скриншот как фото или картинку в формате JPG/PNG.")
        return None
    if document.file_size and document.file_size > MAX_SCREENSHOT_BYTES:
        await message.reply_text("Файл больше 5 МБ. Пришлите скриншот как фото — Telegram его сожмёт.")
        return None
    telegram_file = await document.get_file()
    return bytes(await telegram_file.download_as_bytearray()), document.mime_type


async def _product_from_screenshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE, retry_state: int
) -> int:
    """Распознать карточку по скриншоту и перейти к шагу с фото товара."""
    message = update.effective_message
    # Первая попытка в сценарии входит в лимит PDF, повторные — в лимит сообщений.
    kind = "chat" if context.user_data.get("pdf_counted") else "pdf"
    if not await _check_limit(update, context, kind):
        return retry_state
    context.user_data["pdf_counted"] = True

    screenshot = await _read_screenshot(message)
    if screenshot is None:
        return retry_state
    image, media_type = screenshot
    manual: dict[str, Any] = context.user_data.setdefault("manual", {"source_url": None})
    logger.info("/product: скриншот получен (%d КБ)", len(image) // 1024)

    status = await message.chat.send_message("📸 Распознаю скриншот…")
    await message.chat.send_action(ChatAction.TYPING)
    try:
        card = await _ai(context).generate_product_card_from_screenshot(image, media_type)
    except OffTopicError:
        await _set_status(
            status,
            "Не вижу на скриншоте страницу товара. Пришлите скриншот, где видны название и цена, "
            "или введите название товара текстом.",
        )
        return retry_state
    except AIProcessingError as exc:
        logger.error("/product: ошибка распознавания скриншота: %s", exc.detail or exc.user_message)
        await _set_status(status, f"{exc.user_message}\n\nПопробовать снова: «{BTN_PRODUCT}».")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["card"] = card
    features = f"\nХарактеристик: {len(card.key_features)}" if card.key_features else ""
    await _set_status(
        status,
        f"Распознал: {card.product_name}\nЦена: {format_price(card.price, card.currency)}{features}",
    )
    await message.chat.send_message(
        "Пришлите фото товара для карточки — например, вырежьте его со скриншота. "
        "Или пропустите: карточка будет без фото.",
        reply_markup=skip_keyboard(),
    )
    logger.info("/product: скриншот распознан: %s", card.product_name[:80])
    context.user_data["origin"] = "screenshot"
    manual.setdefault("title", card.product_name)
    return MANUAL_IMAGE


async def manual_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: название товара (обязательно)."""
    text = await _input_text(update)
    if not text or _is_skip(text):
        await update.effective_message.reply_text("Название обязательно. Введите название товара:")
        return MANUAL_NAME
    context.user_data["manual"]["title"] = text[:300]
    await update.effective_message.reply_text("Введите краткое описание товара:", reply_markup=skip_keyboard())
    return MANUAL_DESCRIPTION


async def manual_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: краткое описание."""
    text = await _input_text(update)
    context.user_data["manual"]["description"] = None if _is_skip(text) else text[:3000]
    await update.effective_message.reply_text(
        "Введите цену и валюту, например: 1299 EUR:", reply_markup=skip_keyboard()
    )
    return MANUAL_PRICE


async def manual_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: цена и валюта одной строкой — нормализует Claude."""
    text = await _input_text(update)
    context.user_data["manual"]["price_and_currency"] = None if _is_skip(text) else text[:100]
    await update.effective_message.reply_text(
        "Введите ключевые характеристики — каждую с новой строки или через «;».\n"
        "Например:\nОбъём 1,8 л\nМощность 1450 Вт",
        reply_markup=skip_keyboard(),
    )
    return MANUAL_FEATURES


async def manual_features(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: список характеристик."""
    text = await _input_text(update)
    context.user_data["manual"]["features"] = [] if _is_skip(text) else clean_text_list(text, limit=20)
    await update.effective_message.reply_text(
        "Отправьте прямую ссылку на фото товара или напишите “пропустить”.\n"
        "Можно также просто прислать фото в чат.",
        reply_markup=skip_keyboard(),
    )
    return MANUAL_IMAGE


async def manual_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: ссылка на фото, фото из чата или пропуск; затем генерация PDF."""
    message = update.effective_message
    manual: dict[str, Any] = context.user_data["manual"]
    image_url: str | None = None
    image_bytes: bytes | None = None

    if update.callback_query:
        await _input_text(update)  # «⏭ Пропустить»: карточка без фото
    elif message.photo:
        telegram_file = await message.photo[-1].get_file()
        image_bytes = bytes(await telegram_file.download_as_bytearray())
        logger.info("/product: фото получено из чата (%d КБ)", len(image_bytes) // 1024)
    else:
        text = (message.text or "").strip()
        if not _is_skip(text):
            image_url = normalize_url(text)
            if image_url is None:
                await message.reply_text(
                    "Это не похоже на ссылку. Пришлите ссылку вида https://… на фото или напишите “пропустить”."
                )
                return MANUAL_IMAGE

    ready_card: ProductCard | None = context.user_data.get("card")
    status = await message.chat.send_message(
        "Формирую карточку…" if ready_card else "Нормализую данные и формирую карточку…"
    )
    await _build_and_send_product(
        message, context, status,
        user_id=update.effective_user.id,
        raw_data={key: value for key, value in manual.items() if key != "source_url"},
        image_url=image_url,
        image_bytes=image_bytes,
        source_url=manual.get("source_url"),
        origin=context.user_data.get("origin", "manual"),
        ready_card=ready_card,
    )
    return ConversationHandler.END


async def _build_and_send_product(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    status: Message,
    *,
    user_id: int,
    raw_data: dict[str, Any],
    image_url: str | None,
    image_bytes: bytes | None,
    source_url: str | None,
    origin: str,
    ready_card: ProductCard | None = None,
) -> None:
    """Общий финал /product: Claude → JSON → фото → PDF → отправка.

    Если карточка уже распознана (по скриншоту), повторно модель не вызывается.
    """
    try:
        await message.chat.send_action(ChatAction.TYPING)
        card = ready_card or await _ai(context).generate_product_card(raw_data)
        # Ссылки берём из исходных данных, а не из ответа модели: так она не сможет их исказить.
        card = card.model_copy(update={"image_url": image_url, "source_url": source_url})
        await asyncio.to_thread(save_model_json, card, config.JSON_DIR, "product")

        image_src: str | None = None
        if image_bytes:
            image_src = await asyncio.to_thread(image_bytes_to_data_uri, image_bytes, "image/jpeg")
        elif image_url:
            await _set_status(status, "Загружаю фото товара…")
            image_src = await asyncio.to_thread(download_image_as_data_uri, image_url, config.HTTP_TIMEOUT)
        image_failed = bool(image_url or image_bytes) and image_src is None

        await _set_status(status, "Формирую PDF…")
        pdf_path = await asyncio.to_thread(_pdf(context).generate_product_pdf, card, image_src, origin)
        await _send_pdf(message, pdf_path, _product_caption(card, image_failed), after_product_keyboard())
        await _delete_quietly(status)
        request = ORIGIN_REQUEST[origin].format(url=source_url or "").strip()
        await _remember_document(
            context,
            user_id,
            request,
            "Создан PDF «Карточка товара». Данные карточки (JSON):\n" + card.model_dump_json(),
        )
    except OffTopicError as exc:
        await _set_status(status, exc.user_message)
    except AIProcessingError as exc:
        logger.error("/product: ошибка LLM: %s", exc.detail or exc.user_message)
        await _set_status(status, f"{exc.user_message}\n\nПопробовать снова: «{BTN_PRODUCT}».")
    except PDFGenerationError:
        await _set_status(status, f"Не удалось создать PDF. Попробуйте ещё раз: «{BTN_PRODUCT}».")
    except Exception:
        logger.exception("/product: непредвиденная ошибка")
        await _set_status(status, GENERIC_ERROR_TEXT)
    finally:
        context.user_data.clear()


# --------------------------------------------------------------------------- #
# Сборка приложения
# --------------------------------------------------------------------------- #


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Последний рубеж: залогировать ошибку и, если возможно, ответить пользователю."""
    logger.error("Необработанная ошибка при обработке апдейта", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(GENERIC_ERROR_TEXT)
        except TelegramError:
            logger.warning("Не удалось отправить сообщение об ошибке пользователю")


async def _cleanup_loop(application: Application) -> None:
    """Раз в час: удалить старые PDF и JSON, забыть неактивных пользователей, почистить лимиты."""
    retention_days = application.bot_data["file_retention_days"]
    idle_seconds = application.bot_data["memory_idle_hours"] * 3600
    while True:
        try:
            await asyncio.to_thread(delete_old_files, (config.REPORTS_DIR, config.JSON_DIR), retention_days)
            forgotten = application.bot_data["memory"].prune_idle(idle_seconds)
            if forgotten:
                logger.info("Уборка: забыта история %d неактивных пользователей", forgotten)
            application.bot_data["limiter"].prune()
        except Exception:
            logger.exception("Уборка: ошибка")
        await asyncio.sleep(config.CLEANUP_INTERVAL)


async def _post_init(application: Application) -> None:
    """Зарегистрировать меню команд в Telegram и запустить фоновую уборку."""
    application.bot_data["cleanup_task"] = asyncio.create_task(_cleanup_loop(application))
    await application.bot.set_my_commands(
        [
            BotCommand("report", "PDF-отчёт по диалогу с клиентом"),
            BotCommand("product", "PDF-карточка товара по ссылке"),
            BotCommand("cancel", "Отменить текущий сценарий"),
            BotCommand("clear_chat", "Очистить память диалога"),
            BotCommand("help", "Список команд"),
        ]
    )
    me = await application.bot.get_me()
    logger.info("Бот @%s запущен и ждёт сообщений", me.username)


async def _post_shutdown(application: Application) -> None:
    """Остановить уборку и закрыть соединения клиента Anthropic."""
    task = application.bot_data.get("cleanup_task")
    if task:
        task.cancel()
    await application.bot_data["ai"].close()


def _skip_button(callback: Any) -> CallbackQueryHandler:
    """Хендлер inline-кнопки «⏭ Пропустить» для шага ручного ввода."""
    return CallbackQueryHandler(callback, pattern=f"^{CB_SKIP}$")


def build_application(settings: config.Settings) -> Application:
    """Собрать Telegram-приложение со всеми хендлерами.

    Raises:
        config.ConfigError: ACCESS_TOKENS заданы в неверном формате.
    """
    try:
        tokens = parse_access_tokens(settings.access_tokens)
    except ValueError as exc:
        raise config.ConfigError(f"ACCESS_TOKENS: {exc}") from exc
    if not tokens:
        raise config.ConfigError("ACCESS_TOKENS: нужен хотя бы один токен")

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(
            PerUserUpdateProcessor(config.MAX_CONCURRENT_UPDATES, config.MAX_PENDING_PER_USER)
        )
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["ai"] = AIProcessor(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        json_retries=config.LLM_JSON_RETRIES,
    )
    application.bot_data["pdf"] = PDFGenerator(config.TEMPLATES_DIR, config.REPORTS_DIR)
    application.bot_data["access"] = AccessManager(tokens, config.ACCESS_FILE, config.MAX_USERS_PER_TOKEN)
    application.bot_data["limiter"] = RateLimiter(
        {
            "pdf": (settings.pdf_per_hour, 3600),
            "chat": (settings.chat_per_hour, 3600),
            "auth": (config.AUTH_ATTEMPTS_PER_HOUR, 3600),
        }
    )
    application.bot_data["file_retention_days"] = settings.file_retention_days
    application.bot_data["memory_idle_hours"] = settings.memory_idle_hours
    logger.info("Доступ: токенов %d (%s)", len(tokens), ", ".join(t.label for t in tokens))
    application.bot_data["memory"] = ShortMemory(
        settings.short_memory_messages,
        settings.short_memory_summary_after,
        config.SUMMARY_KEEP_MESSAGES,
    )
    browser_enabled = settings.browser_rendering and is_browser_available()
    application.bot_data["browser_enabled"] = browser_enabled
    if settings.browser_rendering and not browser_enabled:
        logger.warning(
            "BROWSER_RENDERING=true, но Playwright не установлен: сайты на JavaScript уйдут в ручной режим"
        )
    logger.info("Рендеринг страниц в браузере: %s", "включён" if browser_enabled else "выключен")
    application.bot_data["test_dialogs"] = load_test_dialogs(DIALOG_EXAMPLES_DIR)
    logger.info("Тестовых диалогов: %d", len(application.bot_data["test_dialogs"]))

    # Кнопки и команды, которые работают и внутри сценария, и вне его.
    common_handlers = [
        CommandHandler("cancel", cancel),
        MessageHandler(menu_button(BTN_CANCEL), cancel),
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        MessageHandler(menu_button(BTN_HELP), help_command),
        CommandHandler("clear_chat", clear_chat),
        MessageHandler(menu_button(BTN_CLEAR), clear_chat),
        MessageHandler(menu_button(BTN_TEST), send_test_dialog),
        CallbackQueryHandler(report_from_test, pattern=f"^{CB_REPORT_TEST}:"),
        CallbackQueryHandler(letter_button, pattern=f"^{CB_LETTER}$"),
    ]
    # PTB предупреждает, что при per_message=False inline-кнопки не привязаны к сообщению.
    # Нам так и нужно: состояние сценария ведётся по пользователю.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)
        conversation = ConversationHandler(
            entry_points=[
                CommandHandler("report", report_start),
                MessageHandler(menu_button(BTN_REPORT), report_start),
                CallbackQueryHandler(report_start, pattern=f"^{CB_NEW_REPORT}$"),
                CommandHandler("product", product_start),
                MessageHandler(menu_button(BTN_PRODUCT), product_start),
                CallbackQueryHandler(product_start, pattern=f"^{CB_NEW_PRODUCT}$"),
            ],
            states={
                REPORT_WAIT_TEXT: [
                    MessageHandler(TEXT_INPUT | filters.Document.FileExtension("txt"), report_receive_text),
                ],
                PRODUCT_WAIT_URL: [
                    MessageHandler(TEXT_INPUT, product_receive_url),
                    MessageHandler(SCREENSHOT, product_screenshot),
                ],
                MANUAL_NAME: [
                    MessageHandler(TEXT_INPUT, manual_name),
                    MessageHandler(SCREENSHOT, manual_screenshot),
                ],
                MANUAL_DESCRIPTION: [MessageHandler(TEXT_INPUT, manual_description), _skip_button(manual_description)],
                MANUAL_PRICE: [MessageHandler(TEXT_INPUT, manual_price), _skip_button(manual_price)],
                MANUAL_FEATURES: [MessageHandler(TEXT_INPUT, manual_features), _skip_button(manual_features)],
                MANUAL_IMAGE: [
                    MessageHandler(TEXT_INPUT | filters.PHOTO, manual_image),
                    _skip_button(manual_image),
                ],
            },
            fallbacks=[
                *common_handlers,
                MessageHandler(~filters.COMMAND, unexpected_input),
            ],
            allow_reentry=True,
            name="ai_report_bot",
        )

    application.add_handler(TypeHandler(Update, access_gate), group=-1)
    application.add_handler(conversation)
    for handler in common_handlers:
        application.add_handler(handler)
    application.add_handler(MessageHandler(TEXT_INPUT, chat_message))
    application.add_handler(CallbackQueryHandler(stale_button))
    application.add_handler(MessageHandler(filters.ALL, outside_conversation))
    application.add_error_handler(error_handler)
    return application


def setup_logging(level: str) -> None:
    """Логи в консоль и в `logs/bot.log` с ротацией."""
    # Консоль Windows по умолчанию в cp1252/cp866 — без этого кириллица в логах ломается.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        config.LOGS_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[console, file_handler], force=True)
    # httpx на уровне INFO пишет URL запросов к Telegram вместе с токеном бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # WeasyPrint и fontTools на INFO пишут каждый шаг вёрстки.
    logging.getLogger("weasyprint").setLevel(logging.WARNING)
    logging.getLogger("fontTools").setLevel(logging.WARNING)


def main() -> None:
    """Точка входа: прочитать настройки и запустить long polling."""
    try:
        settings = config.load_settings()
    except config.ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(1)

    config.ensure_directories()
    setup_logging(settings.log_level)
    logger.info("Запуск AI Report Bot, модель %s", settings.anthropic_model)
    try:
        application = build_application(settings)
    except config.ConfigError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        sys.exit(1)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
