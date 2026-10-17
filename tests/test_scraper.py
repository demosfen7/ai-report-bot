"""Тесты разбора HTML товара и распознавания блокировок (без сети)."""

from __future__ import annotations

import pytest

from utils.scraper import (
    ScrapingError,
    detect_block_reason,
    normalize_input_url,
    parse_product_html,
)

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "BreadcrumbList"},
  {"@type": "Product", "name": "Чайник Bosch TWK3A011",
   "description": "Электрический чайник 1,7 л.",
   "image": ["/img/kettle.jpg"],
   "offers": {"@type": "Offer", "price": "39.99", "priceCurrency": "EUR"},
   "additionalProperty": [{"name": "Мощность", "value": "2400 Вт"}]}
]}
</script></head><body><h1>Другой заголовок</h1></body></html>
"""

OG_PAGE = """
<html><head>
<meta property="og:title" content="Наушники Sony WH-1000XM5">
<meta property="og:description" content="Беспроводные наушники с шумоподавлением.">
<meta property="og:image" content="https://cdn.shop.test/sony.jpg">
<meta property="product:price:amount" content="349">
<meta property="product:price:currency" content="EUR">
</head><body>
<table class="product-specs">
  <tr><th>Время работы</th><td>30 ч</td></tr>
  <tr><th>Вес</th><td>250 г</td></tr>
</table>
</body></html>
"""


def test_parse_jsonld_product() -> None:
    product = parse_product_html(JSONLD_PAGE, "https://shop.test/p/1")
    assert product.title == "Чайник Bosch TWK3A011"
    assert product.price == "39.99"
    assert product.currency == "EUR"
    assert product.image_url == "https://shop.test/img/kettle.jpg"
    assert product.features == ["Мощность: 2400 Вт"]
    assert product.is_sufficient()


def test_parse_opengraph_and_spec_table() -> None:
    product = parse_product_html(OG_PAGE, "https://shop.test/p/2")
    assert product.title == "Наушники Sony WH-1000XM5"
    assert product.price == "349"
    assert product.image_url == "https://cdn.shop.test/sony.jpg"
    assert product.features == ["Время работы: 30 ч", "Вес: 250 г"]


def test_empty_page_is_insufficient() -> None:
    product = parse_product_html("<html><body><div id='root'></div></body></html>", "https://shop.test")
    assert not product.is_sufficient()


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        ("<html><body>Please complete the CAPTCHA</body></html>", "captcha"),
        ("<html><body><noscript>Please enable JavaScript</noscript></body></html>", "javascript"),
        ('<html><body><div id="root"></div></body></html>', "javascript"),
        ("<html><body><p>" + "Обычный текст. " * 30 + "</p></body></html>", "unrecognized"),
    ],
)
def test_detect_block_reason(html: str, reason: str) -> None:
    assert detect_block_reason(html) == reason


def test_normalize_input_url() -> None:
    assert normalize_input_url("Вот ссылка: https://shop.test/item?id=5.") == "https://shop.test/item?id=5"
    assert normalize_input_url("shop.test/item") == "https://shop.test/item"


def test_normalize_input_url_rejects_text() -> None:
    with pytest.raises(ScrapingError) as info:
        normalize_input_url("просто текст без ссылки")
    assert info.value.reason == "invalid_url"
