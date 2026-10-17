"""AI Report Bot — Telegram-бот: PDF-отчёт по диалогу и PDF-карточка товара.

Запуск: `python bot.py` (настройки — в `.env`, см. `.env.example`).
"""

from __future__ import annotations

import asyncio
import html
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from utils.ai_processor import AIProcessingError, AIProcessor
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
from utils.short_memory import ShortMemory
from utils.validators import ClientReport, ProductCard, clean_text_list, normalize_url, save_model_json

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
SKIP_WORDS = {"пропустить", "пропуск", "skip", "-", "—", "нет"}
# Сколько символов исходного диалога запоминать для вопросов после /report.
MEMORY_DIALOG_CHARS = 8000
MAX_CHAT_MESSAGE_CHARS = 4000
TELEGRAM_TEXT_LIMIT = 4096

TEXT_ONLY = filters.TEXT & ~filters.COMMAND

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/report — PDF-отчёт по диалогу с клиентом\n"
    "/product — PDF-карточка товара по ссылке\n"
    "/cancel — отменить текущий сценарий\n"
    "/clear_chat — очистить память диалога\n"
    "/help — список команд\n\n"
    "<b>Память</b>\n"
    "Вне команд со мной можно просто переписываться. Я помню последние сообщения "
    "и созданные отчёты и карточки: спросите, например, «что клиент хотел по срокам?» "
    "или «напиши клиенту письмо по итогам». Более старая часть переписки сворачивается "
    "в краткое резюме. Память хранится в оперативной памяти и очищается при "
    "перезапуске бота или командой /clear_chat."
)
START_TEXT = (
    "Привет! Я <b>AI Report Bot</b>.\n\n"
    "• <b>/report</b> — пришлите транскрибацию разговора с клиентом, "
    "и я соберу PDF-отчёт: клиент, тема, запрос, настроение, следующие шаги.\n"
    "• <b>/product</b> — пришлите ссылку на товар, и я сделаю PDF-карточку. "
    "Если сайт не отдаёт данные, заполним карточку вручную.\n"
    "• Просто напишите сообщение — я помню нашу переписку и созданные документы "
    "и отвечу на вопросы по ним.\n\n"
    "/help — список команд."
)
GENERIC_ERROR_TEXT = (
    "Произошла непредвиденная ошибка. Попробуйте ещё раз: /report или /product."
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


async def _send_pdf(message: Message, path: Path, caption: str) -> None:
    """Отправить PDF-файл в чат."""
    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    with path.open("rb") as file:
        await message.reply_document(
            document=file,
            filename=path.name,
            caption=caption,
            parse_mode=ParseMode.HTML,
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
    return "\n".join(lines)


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
# Общие команды
# --------------------------------------------------------------------------- #


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/start — приветствие; заодно сбрасывает текущий сценарий."""
    logger.info("/start от пользователя %s", update.effective_user.id if update.effective_user else "?")
    context.user_data.clear()
    await update.effective_message.reply_text(START_TEXT, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — список команд. Текущий сценарий не прерывает."""
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel — отмена текущего сценария."""
    logger.info("Сценарий отменён пользователем %s", update.effective_user.id if update.effective_user else "?")
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Действие отменено. Выберите режим: /report или /product."
    )
    return ConversationHandler.END


async def unexpected_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Неподходящий тип сообщения внутри сценария (стикер, голосовое и т.п.)."""
    await update.effective_message.reply_text(
        "Я жду текстовое сообщение. Чтобы выйти из сценария, отправьте /cancel."
    )


async def outside_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нетекстовое сообщение вне сценария — подсказать команды."""
    await update.effective_message.reply_text(
        "Выберите режим: /report — отчёт по диалогу, /product — карточка товара. "
        "Или просто напишите вопрос текстом. /help — все команды."
    )


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободный чат вне сценариев: ответ Claude с учётом памяти о переписке."""
    message = update.effective_message
    user_id = update.effective_user.id
    text = (message.text or "").strip()[:MAX_CHAT_MESSAGE_CHARS]
    if not text:
        return
    logger.info("Чат: сообщение от %s (%d символов)", user_id, len(text))

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
    await update.effective_message.reply_text("Память диалога очищена.")


# --------------------------------------------------------------------------- #
# Сценарий /report
# --------------------------------------------------------------------------- #


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/report — запросить текст диалога."""
    context.user_data.clear()
    logger.info("/report: старт сценария (чат %s)", update.effective_chat.id)
    await update.effective_message.reply_text(
        "Отправьте текст диалога с клиентом, и я подготовлю PDF-отчёт.\n\n"
        "Можно сообщением или .txt-файлом. Для отмены — /cancel."
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

    status = await message.reply_text("Анализирую диалог…")
    try:
        await message.chat.send_action(ChatAction.TYPING)
        report = await _ai(context).generate_client_report(dialog)
        await asyncio.to_thread(save_model_json, report, config.JSON_DIR, "report")
        await _set_status(status, "Формирую PDF…")
        pdf_path = await asyncio.to_thread(_pdf(context).generate_report_pdf, report)
        await _send_pdf(message, pdf_path, "<b>Готово! Вот ваш отчёт в PDF.</b>")
        await message.reply_text(_report_summary(report), parse_mode=ParseMode.HTML)
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
    except AIProcessingError as exc:
        logger.error("/report: ошибка LLM: %s", exc.detail or exc.user_message)
        await _set_status(status, f"{exc.user_message}\n\nПопробовать снова: /report")
    except PDFGenerationError:
        await _set_status(status, "Не удалось создать PDF. Попробуйте ещё раз: /report")
    except Exception:
        logger.exception("/report: непредвиденная ошибка")
        await _set_status(status, GENERIC_ERROR_TEXT)
    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Сценарий /product
# --------------------------------------------------------------------------- #


async def product_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/product — запросить ссылку на товар."""
    context.user_data.clear()
    logger.info("/product: старт сценария (чат %s)", update.effective_chat.id)
    await update.effective_message.reply_text(
        "Отправьте ссылку на товар, и я создам карточку в PDF.\n\nДля отмены — /cancel."
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
        manual_entry=False,
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
        "Не удалось автоматически получить данные с сайта. Давайте заполним карточку вручную.\n"
        f"Причина: {reason}.\n\n"
        "Для отмены — /cancel."
    )
    await message.reply_text("Введите название товара:")
    return MANUAL_NAME


async def manual_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: название товара (обязательно)."""
    text = (update.effective_message.text or "").strip()
    if not text or _is_skip(text):
        await update.effective_message.reply_text("Название обязательно. Введите название товара:")
        return MANUAL_NAME
    context.user_data["manual"]["title"] = text[:300]
    await update.effective_message.reply_text("Введите краткое описание товара (или напишите «пропустить»):")
    return MANUAL_DESCRIPTION


async def manual_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: краткое описание."""
    text = (update.effective_message.text or "").strip()
    context.user_data["manual"]["description"] = None if _is_skip(text) else text[:3000]
    await update.effective_message.reply_text(
        "Введите цену и валюту, например: 1299 EUR:\n(или напишите «пропустить»)"
    )
    return MANUAL_PRICE


async def manual_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: цена и валюта одной строкой — нормализует Claude."""
    text = (update.effective_message.text or "").strip()
    context.user_data["manual"]["price_and_currency"] = None if _is_skip(text) else text[:100]
    await update.effective_message.reply_text(
        "Введите ключевые характеристики — каждую с новой строки или через «;».\n"
        "Например:\nОбъём 1,8 л\nМощность 1450 Вт\n\n(или напишите «пропустить»)"
    )
    return MANUAL_FEATURES


async def manual_features(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: список характеристик."""
    text = (update.effective_message.text or "").strip()
    context.user_data["manual"]["features"] = [] if _is_skip(text) else clean_text_list(text, limit=20)
    await update.effective_message.reply_text(
        "Отправьте прямую ссылку на фото товара или напишите “пропустить”.\n"
        "Можно также просто прислать фото в чат."
    )
    return MANUAL_IMAGE


async def manual_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ручной режим: ссылка на фото, фото из чата или пропуск; затем генерация PDF."""
    message = update.effective_message
    manual: dict[str, Any] = context.user_data["manual"]
    image_url: str | None = None
    image_bytes: bytes | None = None

    if message.photo:
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

    status = await message.reply_text("Нормализую данные и формирую карточку…")
    await _build_and_send_product(
        message, context, status,
        user_id=update.effective_user.id,
        raw_data={key: value for key, value in manual.items() if key != "source_url"},
        image_url=image_url,
        image_bytes=image_bytes,
        source_url=manual.get("source_url"),
        manual_entry=True,
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
    manual_entry: bool,
) -> None:
    """Общий финал /product: Claude → JSON → фото → PDF → отправка."""
    try:
        await message.chat.send_action(ChatAction.TYPING)
        card = await _ai(context).generate_product_card(raw_data)
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
        pdf_path = await asyncio.to_thread(
            _pdf(context).generate_product_pdf, card, image_src, manual_entry
        )
        await _send_pdf(message, pdf_path, _product_caption(card, image_failed))
        await _delete_quietly(status)
        request = (
            "/product — карточка товара, данные введены вручную"
            if manual_entry
            else f"/product — карточка товара по ссылке {source_url}"
        )
        await _remember_document(
            context,
            user_id,
            request,
            "Создан PDF «Карточка товара». Данные карточки (JSON):\n" + card.model_dump_json(),
        )
    except AIProcessingError as exc:
        logger.error("/product: ошибка LLM: %s", exc.detail or exc.user_message)
        await _set_status(status, f"{exc.user_message}\n\nПопробовать снова: /product")
    except PDFGenerationError:
        await _set_status(status, "Не удалось создать PDF. Попробуйте ещё раз: /product")
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


async def _post_init(application: Application) -> None:
    """Зарегистрировать меню команд в Telegram."""
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
    """Закрыть соединения клиента Anthropic."""
    await application.bot_data["ai"].close()


def build_application(settings: config.Settings) -> Application:
    """Собрать Telegram-приложение со всеми хендлерами."""
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
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

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("report", report_start),
            CommandHandler("product", product_start),
        ],
        states={
            REPORT_WAIT_TEXT: [
                MessageHandler(TEXT_ONLY | filters.Document.FileExtension("txt"), report_receive_text),
            ],
            PRODUCT_WAIT_URL: [MessageHandler(TEXT_ONLY, product_receive_url)],
            MANUAL_NAME: [MessageHandler(TEXT_ONLY, manual_name)],
            MANUAL_DESCRIPTION: [MessageHandler(TEXT_ONLY, manual_description)],
            MANUAL_PRICE: [MessageHandler(TEXT_ONLY, manual_price)],
            MANUAL_FEATURES: [MessageHandler(TEXT_ONLY, manual_features)],
            MANUAL_IMAGE: [MessageHandler(TEXT_ONLY | filters.PHOTO, manual_image)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("clear_chat", clear_chat),
            MessageHandler(~filters.COMMAND, unexpected_input),
        ],
        allow_reentry=True,
        name="ai_report_bot",
    )

    application.add_handler(conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("clear_chat", clear_chat))
    application.add_handler(MessageHandler(TEXT_ONLY, chat_message))
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
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
