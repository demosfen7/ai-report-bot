"""Тесты разбора и валидации JSON-ответов LLM."""

from __future__ import annotations

import json

import pytest

from utils.validators import (
    ClientReport,
    LLMResponseError,
    ProductCard,
    clean_text_list,
    extract_rejection,
    parse_llm_json,
    parse_price,
    save_model_json,
)

VALID_REPORT = {
    "client_name": "Анна Ковалёва",
    "topic": "Редизайн интернет-магазина",
    "main_request": "Ускорить сайт и добавить Apple Pay.",
    "mood": "Заинтересованная",
    "next_steps": ["Отправить КП до пятницы", "Созвон в понедельник"],
    "true_need": "Боится повторить неудачу с прошлой студией, которая сорвала сроки.",
    "true_need_signals": ["Прошлая студия сорвала нам сроки на два месяца"],
    "recommendation": "Показать план с контрольными точками и предложить штраф за срыв сроков.",
}

VALID_PRODUCT = {
    "product_name": "Кофемашина DeLonghi Magnifica S",
    "short_description": "Автоматическая кофемашина.",
    "price": 449.99,
    "currency": "EUR",
    "key_features": ["Давление 15 бар", "Резервуар 1,8 л"],
    "image_url": "https://example.com/photo.jpg",
    "source_url": "https://example.com/item",
}


def test_valid_report_json() -> None:
    report = parse_llm_json(json.dumps(VALID_REPORT, ensure_ascii=False), ClientReport)
    assert report.client_name == "Анна Ковалёва"
    assert report.next_steps == ["Отправить КП до пятницы", "Созвон в понедельник"]


def test_json_in_markdown_fence_is_accepted() -> None:
    raw = "```json\n" + json.dumps(VALID_REPORT, ensure_ascii=False) + "\n```"
    assert parse_llm_json(raw, ClientReport).topic == "Редизайн интернет-магазина"


def test_json_surrounded_by_text_is_accepted() -> None:
    raw = "Вот результат: " + json.dumps(VALID_REPORT, ensure_ascii=False) + " Готово."
    assert parse_llm_json(raw, ClientReport).mood == "Заинтересованная"


def test_not_json_raises() -> None:
    with pytest.raises(LLMResponseError, match="не является JSON"):
        parse_llm_json("Клиент Анна хочет новый сайт.", ClientReport)


def test_json_array_raises() -> None:
    with pytest.raises(LLMResponseError, match="JSON-объект"):
        parse_llm_json("[1, 2, 3]", ClientReport)


def test_missing_field_raises() -> None:
    data = {k: v for k, v in VALID_REPORT.items() if k != "topic"}
    with pytest.raises(LLMResponseError, match="topic"):
        parse_llm_json(json.dumps(data), ClientReport)


def test_empty_string_field_raises() -> None:
    data = VALID_REPORT | {"client_name": "   "}
    with pytest.raises(LLMResponseError, match="client_name"):
        parse_llm_json(json.dumps(data), ClientReport)


def test_next_steps_string_is_split_into_list() -> None:
    data = VALID_REPORT | {"next_steps": "1. Отправить КП\n2. Созвон\n\n"}
    assert ClientReport.model_validate(data).next_steps == ["Отправить КП", "Созвон"]


def test_valid_product_json() -> None:
    card = parse_llm_json(json.dumps(VALID_PRODUCT), ProductCard)
    assert card.price == 449.99
    assert card.currency == "EUR"
    assert card.key_features == ["Давление 15 бар", "Резервуар 1,8 л"]


def test_product_normalization() -> None:
    data = VALID_PRODUCT | {
        "price": "1 299,99 €",
        "currency": "€",
        "image_url": "",
        "source_url": "javascript:alert(1)",
        "short_description": None,
        "key_features": "- Пункт один\n- Пункт два",
    }
    card = ProductCard.model_validate(data)
    assert card.price == 1299.99
    assert card.currency == "EUR"
    assert card.image_url is None
    assert card.source_url is None
    assert card.short_description == ""
    assert card.key_features == ["Пункт один", "Пункт два"]


def test_product_without_price_is_valid() -> None:
    card = ProductCard.model_validate(VALID_PRODUCT | {"price": None, "currency": None})
    assert card.price is None


def test_negative_price_rejected() -> None:
    with pytest.raises(LLMResponseError, match="price"):
        parse_llm_json(json.dumps(VALID_PRODUCT | {"price": -5}), ProductCard)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1299", 1299.0),
        ("1 299 EUR", 1299.0),
        ("12,99", 12.99),
        ("1.299", 1299.0),
        ("1,299.50", 1299.5),
        ("1.299,50 €", 1299.5),
        ("2 499,00 ₽", 2499.0),
        (59, 59.0),
        ("договорная", None),
        (None, None),
    ],
)
def test_parse_price(raw: object, expected: float | None) -> None:
    assert parse_price(raw) == expected


def test_clean_text_list_limit() -> None:
    assert clean_text_list("a; b; c; d", limit=2) == ["a", "b"]


def test_save_model_json_creates_unique_files(tmp_path) -> None:
    report = ClientReport.model_validate(VALID_REPORT)
    first = save_model_json(report, tmp_path, "report")
    second = save_model_json(report, tmp_path, "report")
    assert first != second
    assert json.loads(first.read_text(encoding="utf-8"))["client_name"] == "Анна Ковалёва"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"error": "not_a_dialog"}', "not_a_dialog"),
        ('```json\n{"error": "not_a_product"}\n```', "not_a_product"),
        (json.dumps(VALID_REPORT, ensure_ascii=False), None),
        ('{"error": "x", "client_name": "Анна"}', None),
        ("не json", None),
    ],
)
def test_extract_rejection(raw: str, expected: str | None) -> None:
    assert extract_rejection(raw) == expected


def test_test_dialogs_fit_in_telegram_message() -> None:
    from pathlib import Path

    from utils.examples import load_test_dialogs

    dialogs = load_test_dialogs(Path(__file__).resolve().parent.parent / "examples")
    assert len(dialogs) >= 3
    for dialog in dialogs:
        assert dialog.title and not dialog.text.startswith("#")
        assert len(dialog.text) + 400 < 4096  # плюс заголовок и HTML-разметка сообщения


def test_true_need_fields_required_and_signals_limited() -> None:
    data = {k: v for k, v in VALID_REPORT.items() if k != "recommendation"}
    with pytest.raises(LLMResponseError, match="recommendation"):
        parse_llm_json(json.dumps(data), ClientReport)

    signals = ["«первая»", "вторая", "третья", "четвёртая"]
    report = ClientReport.model_validate(VALID_REPORT | {"true_need_signals": signals})
    assert report.true_need_signals == ["первая", "вторая", "третья"]
    assert ClientReport.model_validate({k: v for k, v in VALID_REPORT.items() if k != "true_need_signals"}).true_need_signals == []
