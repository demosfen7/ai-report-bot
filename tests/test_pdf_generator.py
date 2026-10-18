"""Тесты рендеринга шаблонов и генерации PDF."""

from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from utils.pdf_generator import (
    PDFGenerator,
    format_date_long,
    format_price,
)
from utils.validators import ClientReport, ProductCard

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@pytest.fixture
def generator(tmp_path: Path) -> PDFGenerator:
    return PDFGenerator(TEMPLATES_DIR, tmp_path)


@pytest.fixture
def report() -> ClientReport:
    return ClientReport(
        client_name="Анна Ковалёва",
        topic="Редизайн интернет-магазина",
        main_request="Ускорить сайт и добавить Apple Pay к декабрю.",
        mood="Заинтересованная, но переживает за сроки",
        next_steps=["Отправить КП до пятницы", "Созвон в понедельник"],
        true_need="Боится повторить неудачу с прошлой студией, которая сорвала сроки.",
        true_need_signals=["Прошлая студия сорвала нам сроки на два месяца"],
        recommendation="Показать план с контрольными точками и предложить штраф за срыв сроков.",
    )


@pytest.fixture
def card() -> ProductCard:
    return ProductCard(
        product_name="Кофемашина DeLonghi Magnifica S",
        short_description="Автоматическая кофемашина с капучинатором.",
        price=449.99,
        currency="EUR",
        key_features=["Давление 15 бар", "Резервуар 1,8 л"],
        image_url=None,
        source_url="https://example.com/item",
    )


def _png_data_uri() -> str:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), (200, 120, 80)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _assert_pdf(path: Path) -> None:
    assert path.exists()
    assert path.suffix == ".pdf"
    assert path.read_bytes()[:5] == b"%PDF-"
    assert path.stat().st_size > 1000


def test_report_pdf_created(generator: PDFGenerator, report: ClientReport) -> None:
    _assert_pdf(generator.generate_report_pdf(report))


def test_report_html_contains_data(generator: PDFGenerator, report: ClientReport) -> None:
    html = generator.render_html(
        "report_template.html",
        {"report": report, "generated_at": "18.10.2026 12:00", "generated_at_long": "", "document_id": "X"},
    )
    assert "Отчёт по клиентской консультации" in html
    assert "Анна Ковалёва" in html
    assert "Сгенерировано AI Report Bot" in html
    assert "Созвон в понедельник" in html


def test_report_without_next_steps(generator: PDFGenerator, report: ClientReport) -> None:
    _assert_pdf(generator.generate_report_pdf(report.model_copy(update={"next_steps": []})))


def test_html_is_escaped(generator: PDFGenerator, report: ClientReport) -> None:
    hostile = report.model_copy(update={"client_name": "<script>alert(1)</script>"})
    html = generator.render_html(
        "report_template.html",
        {"report": hostile, "generated_at": "", "generated_at_long": "", "document_id": "X"},
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_product_pdf_with_image(generator: PDFGenerator, card: ProductCard) -> None:
    _assert_pdf(generator.generate_product_pdf(card, image_src=_png_data_uri()))


def test_product_pdf_without_image_uses_placeholder(generator: PDFGenerator, card: ProductCard) -> None:
    html = generator.render_html(
        "product_card_template.html",
        {"card": card, "image_src": None, "origin": "manual",
         "generated_at": "", "generated_at_long": "", "document_id": "X"},
    )
    assert "Фото товара не предоставлено" in html
    assert "<img" not in html
    _assert_pdf(generator.generate_product_pdf(card, image_src=None, origin="manual"))


def test_product_pdf_minimal_data(generator: PDFGenerator, card: ProductCard) -> None:
    minimal = card.model_copy(
        update={"price": None, "currency": None, "key_features": [], "short_description": "", "source_url": None}
    )
    _assert_pdf(generator.generate_product_pdf(minimal))


def test_external_resources_are_not_fetched(generator: PDFGenerator, card: ProductCard) -> None:
    # Внешний URL картинки не должен ни загружаться, ни ронять генерацию.
    _assert_pdf(generator.generate_product_pdf(card, image_src="http://127.0.0.1:1/photo.jpg"))


@pytest.mark.parametrize(
    ("price", "currency", "expected"),
    [
        (1299, "EUR", "1 299 €"),
        (1299.5, "USD", "1 299,50 $"),
        (99.9, "CHF", "99,90 CHF"),
        (100, None, "100"),
        (None, "EUR", "Цена не указана"),
    ],
)
def test_format_price(price: float | None, currency: str | None, expected: str) -> None:
    assert format_price(price, currency) == expected


def test_format_date_long() -> None:
    assert format_date_long(datetime(2026, 10, 18, 9, 5)) == "18 октября 2026, 09:05"


def test_report_html_has_true_need_section(generator: PDFGenerator, report: ClientReport) -> None:
    html = generator.render_html(
        "report_template.html",
        {"report": report, "generated_at": "", "generated_at_long": "", "document_id": "X"},
    )
    assert "Истинный запрос и рекомендация" in html
    assert report.true_need in html and report.recommendation in html
    assert "«Прошлая студия сорвала нам сроки на два месяца»" in html


def test_report_without_signals(generator: PDFGenerator, report: ClientReport) -> None:
    _assert_pdf(generator.generate_report_pdf(report.model_copy(update={"true_need_signals": []})))
