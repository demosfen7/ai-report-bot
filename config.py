"""Конфигурация приложения: пути проекта и переменные окружения из `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR: Path = Path(__file__).resolve().parent
TEMPLATES_DIR: Path = BASE_DIR / "templates"
JSON_DIR: Path = BASE_DIR / "data" / "json"
REPORTS_DIR: Path = BASE_DIR / "reports"
LOGS_DIR: Path = BASE_DIR / "logs"
ACCESS_FILE: Path = BASE_DIR / "data" / "access" / "users.json"

DEFAULT_MODEL: str = "claude-haiku-5-5"
# Число ПОВТОРНЫХ попыток, если модель вернула невалидный JSON.
LLM_JSON_RETRIES: int = 2
# Таймаут HTTP-запросов при scraping и загрузке фото, секунды.
HTTP_TIMEOUT: float = 15.0
# Таймаут загрузки страницы в headless-браузере (для сайтов на JavaScript), секунды.
BROWSER_TIMEOUT: float = 30.0
# Сколько последних реплик остаётся в истории после сворачивания старых в резюме.
SUMMARY_KEEP_MESSAGES: int = 6
# Сколько аккаунтов Telegram можно привязать к одному токену доступа.
MAX_USERS_PER_TOKEN: int = 1
# Сколько апдейтов обрабатывается параллельно и сколько может ждать у одного пользователя.
MAX_CONCURRENT_UPDATES: int = 8
MAX_PENDING_PER_USER: int = 3
# Неудачных попыток ввести токен в час — дальше бот молчит.
AUTH_ATTEMPTS_PER_HOUR: int = 5
# Как часто запускается фоновая уборка, секунды.
CLEANUP_INTERVAL: float = 3600.0

load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Обязательная переменная окружения не задана."""


@dataclass(frozen=True)
class Settings:
    """Настройки, прочитанные из окружения."""

    telegram_bot_token: str
    anthropic_api_key: str
    anthropic_model: str = DEFAULT_MODEL
    log_level: str = "INFO"
    browser_rendering: bool = True
    short_memory_messages: int = 10
    short_memory_summary_after: int = 20
    access_tokens: str = ""
    pdf_per_hour: int = 10
    chat_per_hour: int = 30
    file_retention_days: int = 7
    memory_idle_hours: int = 6


def _require_env(name: str) -> str:
    """Вернуть значение обязательной переменной окружения или поднять ConfigError."""
    value = os.getenv(name, "").strip()
    if not value or value.startswith("your_") or ":your_" in value:
        raise ConfigError(
            f"Переменная окружения {name} не задана. "
            f"Скопируйте .env.example в .env и заполните {name}."
        )
    return value


def _get_int(name: str, default: int, minimum: int) -> int:
    """Прочитать целое число из окружения с проверкой нижней границы."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом, сейчас: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} должен быть не меньше {minimum}, сейчас: {value}")
    return value


def _get_bool(name: str, default: bool) -> bool:
    """Прочитать флаг из окружения: true/false, 1/0, yes/no."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} должен быть true или false, сейчас: {raw!r}")


def load_settings() -> Settings:
    """Собрать настройки из переменных окружения."""
    summary_after = _get_int("SHORT_MEMORY_SUMMARY_AFTER", 20, minimum=0)
    if 0 < summary_after <= SUMMARY_KEEP_MESSAGES:
        raise ConfigError(
            f"SHORT_MEMORY_SUMMARY_AFTER должен быть больше {SUMMARY_KEEP_MESSAGES} "
            "(или 0, чтобы выключить резюмирование)"
        )
    return Settings(
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        anthropic_api_key=_require_env("ANTHROPIC_API_KEY"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        browser_rendering=_get_bool("BROWSER_RENDERING", True),
        short_memory_messages=_get_int("SHORT_MEMORY_MESSAGES", 10, minimum=2),
        short_memory_summary_after=summary_after,
        access_tokens=_require_env("ACCESS_TOKENS"),
        pdf_per_hour=_get_int("PDF_PER_HOUR", 10, minimum=1),
        chat_per_hour=_get_int("CHAT_PER_HOUR", 30, minimum=1),
        file_retention_days=_get_int("FILE_RETENTION_DAYS", 7, minimum=1),
        memory_idle_hours=_get_int("MEMORY_IDLE_HOURS", 6, minimum=1),
    )


def ensure_directories() -> None:
    """Создать папки для JSON, PDF и логов, если их ещё нет."""
    for directory in (JSON_DIR, REPORTS_DIR, LOGS_DIR, ACCESS_FILE.parent):
        directory.mkdir(parents=True, exist_ok=True)
