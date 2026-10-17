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

DEFAULT_MODEL: str = "claude-haiku-5-5"
# Число ПОВТОРНЫХ попыток, если модель вернула невалидный JSON.
LLM_JSON_RETRIES: int = 2
# Таймаут HTTP-запросов при scraping и загрузке фото, секунды.
HTTP_TIMEOUT: float = 15.0

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


def _require_env(name: str) -> str:
    """Вернуть значение обязательной переменной окружения или поднять ConfigError."""
    value = os.getenv(name, "").strip()
    if not value or value.startswith("your_"):
        raise ConfigError(
            f"Переменная окружения {name} не задана. "
            f"Скопируйте .env.example в .env и заполните {name}."
        )
    return value


def load_settings() -> Settings:
    """Собрать настройки из переменных окружения."""
    return Settings(
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        anthropic_api_key=_require_env("ANTHROPIC_API_KEY"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


def ensure_directories() -> None:
    """Создать папки для JSON, PDF и логов, если их ещё нет."""
    for directory in (JSON_DIR, REPORTS_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
