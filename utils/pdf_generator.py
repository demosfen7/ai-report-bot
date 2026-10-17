"""Рендеринг HTML-шаблонов через Jinja2 и генерация PDF через WeasyPrint."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML
from weasyprint.urls import URLFetcher

from utils.validators import ClientReport, ProductCard, build_output_path

logger = logging.getLogger(__name__)

REPORT_TEMPLATE = "report_template.html"
PRODUCT_TEMPLATE = "product_card_template.html"

CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "€", "USD": "$", "RUB": "₽", "GBP": "£", "UAH": "₴",
    "KZT": "₸", "PLN": "zł", "TRY": "₺", "CNY": "¥", "JPY": "¥", "BYN": "Br",
}
_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_NBSP = " "


class PDFGenerationError(Exception):
    """Не удалось отрендерить шаблон или записать PDF."""


def format_price(price: float | None, currency: str | None = None) -> str:
    """Отформатировать цену по-русски: `1 299 €`, `1 299,99 USD`."""
    if price is None:
        return "Цена не указана"
    amount = Decimal(str(price)).quantize(Decimal("0.01"))
    if amount == amount.to_integral_value():
        number = f"{int(amount):,}".replace(",", _NBSP)
    else:
        number = f"{amount:,.2f}".replace(",", _NBSP).replace(".", ",")
    symbol = CURRENCY_SYMBOLS.get(currency or "", currency or "")
    return f"{number}{_NBSP}{symbol}" if symbol else number


def format_date_long(value: datetime) -> str:
    """Дата вида «18 октября 2026, 14:30»."""
    return f"{value.day} {_MONTHS_GENITIVE[value.month - 1]} {value.year}, {value:%H:%M}"


def url_domain(url: str | None) -> str:
    """Домен ссылки без `www.` для компактного отображения."""
    if not url:
        return ""
    host = urlparse(url).hostname or url
    return host.removeprefix("www.")


class PDFGenerator:
    """Рендерит отчёты и карточки товаров в PDF формата A4."""

    def __init__(self, templates_dir: Path, output_dir: Path) -> None:
        """Подготовить окружение Jinja2.

        Args:
            templates_dir: папка с HTML-шаблонами.
            output_dir: куда сохранять PDF.
        """
        self._templates_dir = templates_dir
        self._output_dir = output_dir
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._env.filters["price"] = format_price
        self._env.filters["domain"] = url_domain
        # Внешние ресурсы не загружаем: фото встраивается заранее как data URI.
        # Это защищает от зависаний и запросов WeasyPrint к произвольным адресам.
        self._url_fetcher = URLFetcher(allowed_protocols=["data"])

    def render_html(self, template_name: str, context: dict[str, Any]) -> str:
        """Отрендерить шаблон в строку HTML."""
        try:
            html = self._env.get_template(template_name).render(**context)
        except Exception as exc:
            logger.exception("Ошибка рендеринга шаблона %s", template_name)
            raise PDFGenerationError(f"Ошибка рендеринга {template_name}: {exc}") from exc
        logger.info("HTML отрендерен: %s (%d символов)", template_name, len(html))
        return html

    def html_to_pdf(self, html: str, output_path: Path) -> Path:
        """Сконвертировать HTML в PDF-файл."""
        try:
            HTML(
                string=html,
                base_url=str(self._templates_dir),
                url_fetcher=self._url_fetcher,
            ).write_pdf(str(output_path))
        except Exception as exc:
            logger.exception("Ошибка WeasyPrint при создании %s", output_path)
            raise PDFGenerationError(f"Не удалось создать PDF: {exc}") from exc
        logger.info("PDF создан: %s (%d КБ)", output_path, output_path.stat().st_size // 1024)
        return output_path

    def _base_context(self, generated_at: datetime | None, document_id: str) -> dict[str, Any]:
        """Общие поля шаблонов: даты генерации и номер документа."""
        moment = generated_at or datetime.now()
        return {
            "generated_at": moment.strftime("%d.%m.%Y %H:%M"),
            "generated_at_long": format_date_long(moment),
            "document_id": document_id,
        }

    def generate_report_pdf(self, report: ClientReport, generated_at: datetime | None = None) -> Path:
        """Создать PDF «Отчёт по клиентской консультации»."""
        output_path = build_output_path(self._output_dir, "report", ".pdf")
        document_id = "RPT-" + output_path.stem.split("_")[-1].upper()
        context = self._base_context(generated_at, document_id) | {"report": report}
        html = self.render_html(REPORT_TEMPLATE, context)
        return self.html_to_pdf(html, output_path)

    def generate_product_pdf(
        self,
        card: ProductCard,
        image_src: str | None = None,
        manual_entry: bool = False,
        generated_at: datetime | None = None,
    ) -> Path:
        """Создать PDF «Карточка товара».

        Args:
            card: данные карточки.
            image_src: фото как data URI; None — будет placeholder.
            manual_entry: данные введены вручную (влияет на подпись источника).
            generated_at: время генерации, по умолчанию сейчас.
        """
        output_path = build_output_path(self._output_dir, "product", ".pdf")
        document_id = "PRD-" + output_path.stem.split("_")[-1].upper()
        context = self._base_context(generated_at, document_id) | {
            "card": card,
            "image_src": image_src,
            "manual_entry": manual_entry,
        }
        html = self.render_html(PRODUCT_TEMPLATE, context)
        return self.html_to_pdf(html, output_path)
