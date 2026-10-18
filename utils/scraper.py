"""Базовый scraping страницы товара (requests + BeautifulSoup) с распознаванием блокировок.

Источники данных в порядке приоритета: JSON-LD schema.org/Product, мета-теги
OpenGraph, микроразметка itemprop и, наконец, заголовок страницы. Если данных
не хватает, `scrape_product` поднимает `ScrapingError` с причиной — по ней бот
переходит в ручной режим.
"""

from __future__ import annotations

import base64
import importlib.util
import ipaddress
import json
import logging
import re
import socket
import threading
from dataclasses import asdict, dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8,de;q=0.7",
}
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_FEATURES = 12
MAX_DESCRIPTION_CHARS = 1500
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_CAPTCHA_MARKERS = (
    "captcha", "cf-chl", "challenge-platform", "attention required",
    "are you a robot", "not a robot", "не робот", "access denied",
    "доступ запрещ", "datadome", "perimeterx", "incapsula", "verify you are human",
)
_JS_MARKERS = (
    "enable javascript", "javascript is required", "turn on javascript",
    "javascript is disabled", "включите javascript",
)
_FEATURE_CONTAINER_RE = re.compile(
    r"spec|character|feature|attribute|param|propert|tech|характер|свойств", re.IGNORECASE
)

REASON_HINTS: dict[str, str] = {
    "invalid_url": "некорректная ссылка",
    "network": "сайт не отвечает или недоступен",
    "blocked": "сайт защищён от автоматического чтения (антибот-защита)",
    "rate_limited": "сайт ограничил частоту запросов (429)",
    "http_error": "сайт вернул ошибку",
    "not_html": "по ссылке не HTML-страница",
    "too_large": "страница слишком большая",
    "captcha": "сайт показывает капчу — он защищён от автоматического чтения",
    "javascript": "страница требует JavaScript",
    "unrecognized": "не удалось распознать данные о товаре на странице",
    "browser_unavailable": "не удалось запустить браузер для страниц с JavaScript",
}


class ScrapingError(Exception):
    """Не удалось получить данные со страницы.

    Attributes:
        reason: машинный код причины (ключ `REASON_HINTS`).
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason

    @property
    def hint(self) -> str:
        """Короткое объяснение причины на русском для пользователя."""
        return REASON_HINTS.get(self.reason, "неизвестная ошибка")


@dataclass
class ScrapedProduct:
    """Сырые данные о товаре, извлечённые со страницы."""

    source_url: str
    title: str | None = None
    description: str | None = None
    price: str | None = None
    currency: str | None = None
    features: list[str] = field(default_factory=list)
    image_url: str | None = None

    def is_sufficient(self) -> bool:
        """Достаточно ли данных для карточки: нужно название и цена или описание."""
        return bool(self.title) and bool(self.price or self.description)

    def to_dict(self) -> dict[str, Any]:
        """Представить данные словарём для передачи в LLM."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# Сеть
# --------------------------------------------------------------------------- #


def normalize_input_url(text: str) -> str:
    """Найти URL в сообщении пользователя и проверить схему и домен.

    Raises:
        ScrapingError: с причиной `invalid_url`.
    """
    match = re.search(r"(https?://\S+|www\.\S+|\b[\w-]+(?:\.[\w-]+)+/\S*)", text.strip())
    if not match:
        raise ScrapingError("invalid_url", f"В сообщении не найден URL: {text[:100]!r}")
    url = match.group(1).rstrip(").,;!?»\"'")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "." not in parsed.hostname:
        raise ScrapingError("invalid_url", f"Некорректный URL: {url!r}")
    return url


def ensure_public_host(url: str) -> None:
    """Запретить запросы к localhost и внутренним сетям (защита от SSRF).

    Raises:
        ScrapingError: адрес внутренний (`invalid_url`) или домен не резолвится (`network`).
    """
    hostname = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ScrapingError("network", f"Домен {hostname} не найден: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ScrapingError("invalid_url", f"{hostname} указывает на внутренний адрес {address}")


def _safe_get(session: requests.Session, url: str, timeout: float) -> requests.Response:
    """GET с ручной обработкой редиректов: каждый адрес проверяется `ensure_public_host`."""
    for _ in range(MAX_REDIRECTS + 1):
        ensure_public_host(url)
        response = session.get(url, timeout=timeout, allow_redirects=False, stream=True)
        location = response.headers.get("Location")
        if response.is_redirect and location:
            response.close()
            url = urljoin(url, location)
            continue
        return response
    raise ScrapingError("http_error", "Слишком много перенаправлений")


def _read_limited(response: requests.Response, limit: int) -> bytes:
    """Прочитать тело ответа, но не больше `limit` байт."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > limit:
            raise ScrapingError("too_large", f"Ответ больше {limit} байт")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_page(url: str, timeout: float = 15.0) -> tuple[bytes, str, str | None]:
    """Скачать HTML-страницу.

    Returns:
        Тело страницы, итоговый URL после редиректов и кодировку из заголовков.

    Raises:
        ScrapingError: сетевая ошибка, блокировка (401/403/429/503) или не HTML.
    """
    with requests.Session() as session:
        session.headers.update(DEFAULT_HEADERS)
        try:
            response = _safe_get(session, url, timeout)
            with response:
                status = response.status_code
                logger.info("Scraping: %s → HTTP %d", url, status)
                if status in (401, 403):
                    raise ScrapingError("blocked", f"HTTP {status}")
                if status == 429:
                    raise ScrapingError("rate_limited", "HTTP 429")
                if status == 503:
                    raise ScrapingError("blocked", "HTTP 503 (часто антибот-защита)")
                if status >= 400:
                    raise ScrapingError("http_error", f"HTTP {status}")
                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and "html" not in content_type:
                    raise ScrapingError("not_html", f"Content-Type: {content_type}")
                body = _read_limited(response, MAX_HTML_BYTES)
                encoding = response.encoding if "charset" in content_type else None
                return body, response.url, encoding
        except requests.RequestException as exc:
            raise ScrapingError("network", f"{type(exc).__name__}: {exc}") from exc


def download_image_as_data_uri(url: str, timeout: float = 15.0) -> str | None:
    """Скачать картинку и вернуть её как data URI для встраивания в PDF.

    Возвращает None при любой ошибке: сайт запретил хотлинк, не картинка,
    слишком большой файл и т.п. — тогда PDF строится с placeholder-блоком.
    """
    try:
        with requests.Session() as session:
            session.headers.update(DEFAULT_HEADERS)
            session.headers["Accept"] = "image/avif,image/webp,image/png,image/*;q=0.8"
            response = _safe_get(session, url, timeout)
            with response:
                if response.status_code != 200:
                    logger.warning("Фото: %s → HTTP %d", url, response.status_code)
                    return None
                mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if mime not in ALLOWED_IMAGE_TYPES:
                    logger.warning("Фото: неподдерживаемый тип %r (%s)", mime, url)
                    return None
                data = _read_limited(response, MAX_IMAGE_BYTES)
    except (ScrapingError, requests.RequestException) as exc:
        logger.warning("Фото: не удалось скачать %s: %s", url, exc)
        return None
    return image_bytes_to_data_uri(data, mime)


def image_bytes_to_data_uri(data: bytes, mime: str = "image/jpeg") -> str | None:
    """Проверить, что байты — корректная картинка, и закодировать их в data URI."""
    try:
        from PIL import Image  # Pillow ставится вместе с WeasyPrint

        with Image.open(BytesIO(data)) as image:
            image.verify()
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — Pillow бросает разные типы ошибок
        logger.warning("Фото: файл повреждён или не является изображением: %s", exc)
        return None
    encoded = base64.b64encode(data).decode("ascii")
    logger.info("Фото: подготовлено для PDF (%d КБ)", len(data) // 1024)
    return f"data:{mime};base64,{encoded}"


# --------------------------------------------------------------------------- #
# Разбор HTML
# --------------------------------------------------------------------------- #


def _clean(text: Any) -> str | None:
    """Схлопнуть пробелы; пустую строку превратить в None."""
    if text is None:
        return None
    value = re.sub(r"\s+", " ", str(text)).strip()
    return value or None


def _first(*values: Any) -> str | None:
    """Первое непустое значение после очистки."""
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return None


def _iter_jsonld_objects(node: Any):
    """Обойти все словари в JSON-LD, включая вложенные и @graph."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_jsonld_objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_jsonld_objects(item)


def _is_product(obj: dict[str, Any]) -> bool:
    """Является ли объект JSON-LD товаром schema.org."""
    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.split("/")[-1] in ("Product", "ProductGroup") for t in types)


def _jsonld_image(value: Any) -> str | None:
    """Достать URL картинки из поля image (строка, список или ImageObject)."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    return value if isinstance(value, str) else None


def _parse_jsonld(soup: BeautifulSoup) -> dict[str, Any]:
    """Извлечь товар из блоков `<script type="application/ld+json">`."""
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "", strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _iter_jsonld_objects(data):
            if not _is_product(obj):
                continue
            offers = obj.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}
            spec = offers.get("priceSpecification") or {}
            if isinstance(spec, list):
                spec = spec[0] if spec else {}
            features = []
            for prop in obj.get("additionalProperty") or []:
                if isinstance(prop, dict) and prop.get("name") and prop.get("value") is not None:
                    features.append(f"{prop['name']}: {prop['value']}")
            return {
                "title": obj.get("name"),
                "description": obj.get("description"),
                "price": _first(offers.get("price"), offers.get("lowPrice"), spec.get("price") if isinstance(spec, dict) else None),
                "currency": _first(offers.get("priceCurrency"), spec.get("priceCurrency") if isinstance(spec, dict) else None),
                "image": _jsonld_image(obj.get("image")),
                "features": features,
            }
    return {}


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    """Значение первого найденного мета-тега по property или name."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if isinstance(tag, Tag) and tag.get("content"):
            return _clean(tag["content"])
    return None


def _itemprop(soup: BeautifulSoup, prop: str) -> str | None:
    """Значение микроразметки itemprop (content, src или текст)."""
    tag = soup.find(attrs={"itemprop": prop})
    if not isinstance(tag, Tag):
        return None
    return _first(tag.get("content"), tag.get("src"), tag.get("href"), tag.get_text(" "))


def _guess_price(soup: BeautifulSoup) -> str | None:
    """Последний шанс найти цену: короткий текст с цифрами в элементе с «price» в классе."""
    for tag in soup.select('[class*="price" i], [id*="price" i]')[:20]:
        text = _clean(tag.get_text(" "))
        if text and len(text) <= 40 and re.search(r"\d", text):
            return text
    return None


def _guess_image(soup: BeautifulSoup) -> str | None:
    """Последний шанс найти фото: картинка внутри блока товара или галереи."""
    selector = (
        'img[class*="product" i], img[id*="product" i], [class*="product" i] img, '
        '[id*="product" i] img, [class*="gallery" i] img'
    )
    for img in soup.select(selector)[:10]:
        src = img.get("src") or img.get("data-src")
        if src and not src.startswith("data:"):
            return src
    return None


def _extract_features(soup: BeautifulSoup) -> list[str]:
    """Найти характеристики в таблицах, списках определений и списках с «spec»-классами."""
    features: list[str] = []
    containers = soup.find_all(
        lambda t: isinstance(t, Tag)
        and t.name in ("div", "section", "table", "dl", "ul")
        and _FEATURE_CONTAINER_RE.search(" ".join(t.get("class", [])) + " " + (t.get("id") or ""))
    )
    for container in containers:
        for row in container.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) == 2:
                key, value = _clean(cells[0].get_text(" ")), _clean(cells[1].get_text(" "))
                if key and value:
                    features.append(f"{key}: {value}")
        for dt in container.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            key, value = _clean(dt.get_text(" ")), _clean(dd.get_text(" ") if dd else None)
            if key and value:
                features.append(f"{key}: {value}")
        if not features:
            for li in container.find_all("li"):
                text = _clean(li.get_text(" "))
                if text and len(text) <= 200:
                    features.append(text)
        if len(features) >= MAX_FEATURES:
            break
    unique = list(dict.fromkeys(features))
    return unique[:MAX_FEATURES]


def parse_product_html(html: bytes | str, url: str, encoding: str | None = None) -> ScrapedProduct:
    """Разобрать HTML страницы товара в `ScrapedProduct` (без сетевых запросов)."""
    soup = BeautifulSoup(html, "html.parser", from_encoding=encoding if isinstance(html, bytes) else None)
    ld = _parse_jsonld(soup)
    h1 = soup.find("h1")
    title_tag = soup.find("title")

    title = _first(
        ld.get("title"),
        _meta(soup, "og:title", "twitter:title"),
        _itemprop(soup, "name"),
        h1.get_text(" ") if h1 else None,
        title_tag.get_text(" ") if title_tag else None,
    )
    description = _first(
        ld.get("description"),
        _meta(soup, "og:description", "twitter:description"),
        _itemprop(soup, "description"),
        _meta(soup, "description"),
    )
    image = _first(
        ld.get("image"),
        _meta(soup, "og:image", "og:image:url", "twitter:image"),
        _itemprop(soup, "image"),
        _guess_image(soup),
    )
    image_url = urljoin(url, image) if image else None
    if image_url and not image_url.lower().startswith(("http://", "https://")):
        image_url = None

    return ScrapedProduct(
        source_url=url,
        title=title[:300] if title else None,
        description=description[:MAX_DESCRIPTION_CHARS] if description else None,
        price=_first(
            ld.get("price"),
            _meta(soup, "product:price:amount", "og:price:amount"),
            _itemprop(soup, "price"),
            _guess_price(soup),
        ),
        currency=_first(ld.get("currency"), _meta(soup, "product:price:currency", "og:price:currency"), _itemprop(soup, "priceCurrency")),
        features=ld.get("features") or _extract_features(soup),
        image_url=image_url,
    )


def detect_block_reason(html: bytes | str) -> str:
    """Определить, почему со страницы не удалось извлечь данные."""
    text = html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else html
    lowered = text.lower()
    if any(marker in lowered for marker in _CAPTCHA_MARKERS):
        return "captcha"
    if any(marker in lowered for marker in _JS_MARKERS):
        return "javascript"
    visible = BeautifulSoup(text, "html.parser")
    for tag in visible(["script", "style", "noscript"]):
        tag.decompose()
    if len(visible.get_text(" ", strip=True)) < 200 and re.search(r'id="(root|app|__next)"', text):
        return "javascript"
    return "unrecognized"


def _check_parsed(product: ScrapedProduct, html: bytes | str, source: str) -> ScrapedProduct:
    """Залогировать результат разбора и поднять ScrapingError, если данных мало."""
    logger.info(
        "Scraping (%s): title=%s, price=%s, description=%s, features=%d, image=%s",
        source, bool(product.title), bool(product.price), bool(product.description),
        len(product.features), bool(product.image_url),
    )
    if not product.is_sufficient():
        reason = detect_block_reason(html)
        raise ScrapingError(reason, f"Недостаточно данных на странице {product.source_url} ({reason})")
    return product


def scrape_product_static(url: str, timeout: float = 15.0) -> ScrapedProduct:
    """Скачать страницу обычным HTTP-запросом и разобрать её.

    Raises:
        ScrapingError: страница недоступна, заблокирована или данных недостаточно.
    """
    logger.info("Scraping: старт %s", url)
    html, final_url, encoding = fetch_page(url, timeout)
    return _check_parsed(parse_product_html(html, final_url, encoding), html, "http")


# --------------------------------------------------------------------------- #
# Рендеринг страниц, которым нужен JavaScript
# --------------------------------------------------------------------------- #

# Причины, при которых имеет смысл открыть страницу в настоящем браузере:
# браузер выполняет JavaScript и часто проходит простые проверки по TLS/User-Agent.
BROWSER_RETRY_REASONS = frozenset({"javascript", "unrecognized", "captcha", "blocked", "rate_limited"})
# Картинки, шрифты и видео для разбора не нужны — не тратим на них время и трафик.
_SKIPPED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
# Chromium тяжёлый: одновременно работает не больше одного браузера,
# остальные запросы ждут своей очереди до BROWSER_QUEUE_TIMEOUT секунд.
_BROWSER_SLOTS = threading.BoundedSemaphore(1)
BROWSER_QUEUE_TIMEOUT = 90.0


def should_try_browser(error: ScrapingError) -> bool:
    """Стоит ли повторить попытку в браузере после неудачи обычного запроса."""
    return error.reason in BROWSER_RETRY_REASONS and is_browser_available()


def is_browser_available() -> bool:
    """Установлен ли Playwright (сам Chromium проверяется при запуске)."""
    return importlib.util.find_spec("playwright") is not None


def _is_public_url(url: str, cache: dict[str, bool]) -> bool:
    """Ведёт ли URL на публичный адрес. Результат кэшируется по хосту на время одной загрузки."""
    parsed = urlparse(url)
    if parsed.scheme in ("data", "blob", "about"):
        return True
    host = parsed.hostname or ""
    if host not in cache:
        try:
            ensure_public_host(url)
            cache[host] = True
        except ScrapingError:
            cache[host] = False
    return cache[host]


def render_page_with_browser(url: str, timeout: float = 30.0) -> tuple[str, str, int | None]:
    """Открыть страницу в headless Chromium и вернуть HTML после выполнения JavaScript.

    Каждый запрос страницы (включая скрипты и XHR) проверяется: обращения к
    localhost и внутренним сетям блокируются, как и при обычном scraping.

    Returns:
        HTML страницы, итоговый URL и HTTP-статус основного документа.

    Raises:
        ScrapingError: Playwright не установлен, браузер не запустился или страница не загрузилась.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapingError("javascript", "Playwright не установлен") from exc

    ensure_public_host(url)
    host_cache: dict[str, bool] = {}

    def guard(route: Any) -> None:
        request = route.request
        if request.resource_type in _SKIPPED_RESOURCE_TYPES or not _is_public_url(request.url, host_cache):
            route.abort()
        else:
            route.continue_()

    if not _BROWSER_SLOTS.acquire(timeout=BROWSER_QUEUE_TIMEOUT):
        raise ScrapingError("browser_unavailable", "Браузер занят другими запросами")
    logger.info("Browser: открываю %s", url)
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            except PlaywrightError as exc:
                raise ScrapingError("browser_unavailable", f"Chromium не запустился: {exc}") from exc
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["User-Agent"],
                    locale="ru-RU",
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()
                page.route("**/*", guard)
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    logger.debug("Browser: networkidle не дождались, берём что есть")
                html = page.content()
                final_url = page.url
                status = response.status if response else None
            finally:
                browser.close()
    except PlaywrightTimeout as exc:
        raise ScrapingError("network", f"Браузер не дождался загрузки страницы: {exc}") from exc
    except PlaywrightError as exc:
        raise ScrapingError("network", f"Ошибка браузера: {exc}") from exc
    finally:
        _BROWSER_SLOTS.release()

    logger.info("Browser: %s → HTTP %s, %d символов HTML", final_url, status, len(html))
    if not _is_public_url(final_url, host_cache):
        raise ScrapingError("invalid_url", f"Страница перенаправила на внутренний адрес {final_url}")
    return html, final_url, status


def scrape_product_browser(url: str, timeout: float = 30.0) -> ScrapedProduct:
    """Открыть страницу в браузере (с JavaScript) и разобрать её.

    Raises:
        ScrapingError: браузер недоступен, страница не загрузилась или данных недостаточно.
    """
    html, final_url, status = render_page_with_browser(url, timeout)
    try:
        return _check_parsed(parse_product_html(html, final_url), html, "browser")
    except ScrapingError as exc:
        # Сайт ответил браузеру 401/403/429 — это антибот, а не «непонятная разметка».
        if status in (401, 403, 429) and exc.reason == "unrecognized":
            raise ScrapingError("blocked", f"HTTP {status} в браузере: {exc}") from exc
        raise


def scrape_product(url: str, timeout: float = 15.0, use_browser: bool = True) -> ScrapedProduct:
    """Получить данные товара: сначала HTTP-запросом, при необходимости — через браузер.

    Raises:
        ScrapingError: данные не удалось получить ни одним способом.
    """
    try:
        return scrape_product_static(url, timeout)
    except ScrapingError as exc:
        if not (use_browser and should_try_browser(exc)):
            raise
        static_error = exc
        logger.info("Scraping: HTTP не дал данных (%s), пробую браузер", exc.reason)
    return scrape_with_browser_fallback(url, static_error, timeout * 2)


def scrape_with_browser_fallback(url: str, static_error: ScrapingError, timeout: float = 30.0) -> ScrapedProduct:
    """Попытка через браузер после неудачи обычного запроса.

    Если сам браузер недоступен, поднимается исходная ошибка обычного запроса:
    для пользователя она информативнее («сайт показывает капчу» и т.п.).
    """
    try:
        return scrape_product_browser(url, timeout)
    except ScrapingError as exc:
        if exc.reason == "browser_unavailable":
            logger.warning("Browser недоступен: %s", exc)
            raise static_error from exc
        raise
