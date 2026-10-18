"""Pydantic-модели ответов LLM, разбор JSON и сохранение результатов на диск."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

MAX_FEATURES = 12

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-–—•*·▪✓✔]+|\d+[.)])\s*")
_CURRENCY_ALIASES: dict[str, str] = {
    "€": "EUR",
    "ЕВРО": "EUR",
    "$": "USD",
    "ДОЛЛ": "USD",
    "₽": "RUB",
    "РУБ": "RUB",
    "Р": "RUB",
    "£": "GBP",
    "₴": "UAH",
    "ГРН": "UAH",
    "₸": "KZT",
    "ТГ": "KZT",
}


class LLMResponseError(ValueError):
    """Ответ модели не является валидным JSON ожидаемой структуры."""


# --------------------------------------------------------------------------- #
# Нормализация значений
# --------------------------------------------------------------------------- #


def clean_text_list(value: Any, limit: int | None = None) -> Any:
    """Привести строку или список к списку непустых строк без маркеров списка.

    Строка делится по переводам строк и точкам с запятой. Нестроковые элементы
    оставляются как есть, чтобы их отклонил Pydantic.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = re.split(r"[\n;]+", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return value

    cleaned: list[Any] = []
    for item in items:
        if isinstance(item, str):
            item = _BULLET_RE.sub("", item).strip()
            if not item:
                continue
        cleaned.append(item)
    return cleaned[:limit] if limit else cleaned


def parse_price(value: Any) -> float | None:
    """Разобрать цену из числа или строки вида «1 299,99 €» / «1,299.99».

    Возвращает None, если цена не указана или в строке нет цифр.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("цена не может быть логическим значением")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError("цена должна быть числом или строкой")

    digits = re.sub(r"[^\d,.]", "", value)
    if not re.search(r"\d", digits):
        return None
    digits = digits.strip(".,")

    if "," in digits and "." in digits:
        # Последний встретившийся разделитель — десятичный.
        if digits.rfind(",") > digits.rfind("."):
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
    elif "," in digits or "." in digits:
        sep = "," if "," in digits else "."
        head, _, tail = digits.rpartition(sep)
        if digits.count(sep) == 1 and len(tail) in (1, 2):
            digits = f"{head}.{tail}"  # десятичная часть: 12,99
        else:
            digits = digits.replace(sep, "")  # разделитель тысяч: 1.299 / 1,299,000
    return float(digits)


def normalize_currency(value: Any) -> str | None:
    """Привести валюту к коду ISO 4217 (символы и русские сокращения тоже понимает)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("валюта должна быть строкой")
    code = value.strip().upper().rstrip(".")
    if not code:
        return None
    for alias, iso in _CURRENCY_ALIASES.items():
        if code == alias or (len(alias) > 1 and code.startswith(alias)):
            return iso
    return code[:10]


def normalize_url(value: Any) -> str | None:
    """Вернуть URL, если это http(s)-ссылка, иначе None."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    return url if re.match(r"^https?://\S+$", url, re.IGNORECASE) else None


# --------------------------------------------------------------------------- #
# Модели
# --------------------------------------------------------------------------- #


class _LLMModel(BaseModel):
    """Базовая модель: обрезает пробелы, лишние поля игнорирует."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


class ClientReport(_LLMModel):
    """Структурированный отчёт по диалогу с клиентом."""

    client_name: str = Field(min_length=1, max_length=200)
    topic: str = Field(min_length=1, max_length=300)
    main_request: str = Field(min_length=1, max_length=4000)
    mood: str = Field(min_length=1, max_length=300)
    next_steps: list[str]

    @field_validator("next_steps", mode="before")
    @classmethod
    def _split_steps(cls, value: Any) -> Any:
        """Принять шаги и строкой, и списком."""
        return clean_text_list(value)


class ProductCard(_LLMModel):
    """Структурированные данные карточки товара."""

    product_name: str = Field(min_length=1, max_length=300)
    short_description: str = Field(max_length=3000)
    price: float | None = Field(ge=0)
    currency: str | None
    key_features: list[str]
    image_url: str | None
    source_url: str | None

    @field_validator("short_description", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        """Отсутствующее описание хранить пустой строкой."""
        return "" if value is None else value

    @field_validator("price", mode="before")
    @classmethod
    def _parse_price(cls, value: Any) -> float | None:
        """Принять цену числом или строкой."""
        return parse_price(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> str | None:
        """Привести валюту к коду ISO."""
        return normalize_currency(value)

    @field_validator("key_features", mode="before")
    @classmethod
    def _clean_features(cls, value: Any) -> Any:
        """Очистить список характеристик и ограничить его длину."""
        return clean_text_list(value, limit=MAX_FEATURES)

    @field_validator("image_url", "source_url", mode="before")
    @classmethod
    def _normalize_urls(cls, value: Any) -> str | None:
        """Оставить только корректные http(s)-ссылки."""
        return normalize_url(value)


# --------------------------------------------------------------------------- #
# Разбор ответа LLM
# --------------------------------------------------------------------------- #


def extract_json_text(raw: str) -> str:
    """Достать JSON-объект из ответа модели, даже если он обёрнут в ``` или текст."""
    text = raw.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return text


def extract_rejection(raw: str) -> str | None:
    """Код отказа, если модель вернула `{"error": "..."}` вместо данных, иначе None."""
    try:
        payload = json.loads(extract_json_text(raw))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and set(payload) == {"error"} and isinstance(payload["error"], str):
        return payload["error"]
    return None


def _format_validation_error(exc: ValidationError) -> str:
    """Сжать ошибки Pydantic в одну строку для лога и уточняющего промпта."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<объект>"
        parts.append(f"{location}: {error['msg']}")
    return "Ошибки валидации: " + "; ".join(parts)


def parse_llm_json(raw: str, model: type[ModelT]) -> ModelT:
    """Разобрать ответ модели и провалидировать его Pydantic-моделью.

    Raises:
        LLMResponseError: ответ не JSON-объект или не проходит валидацию.
    """
    text = extract_json_text(raw)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"Ответ не является JSON: {exc.msg} (позиция {exc.pos})") from exc
    if not isinstance(payload, dict):
        raise LLMResponseError("Ожидался JSON-объект, получен другой тип данных")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise LLMResponseError(_format_validation_error(exc)) from exc


# --------------------------------------------------------------------------- #
# Сохранение файлов
# --------------------------------------------------------------------------- #


def build_output_path(directory: Path, prefix: str, suffix: str) -> Path:
    """Сформировать уникальный путь вида `<prefix>_20261018_143000_ab12cd34<suffix>`."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}{suffix}"


def save_model_json(data: BaseModel, directory: Path, prefix: str) -> Path:
    """Сохранить модель в JSON-файл с уникальным именем и вернуть путь к нему."""
    path = build_output_path(directory, prefix, ".json")
    path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
    logger.info("JSON сохранён: %s", path)
    return path
