FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Pango и HarfBuzz нужны WeasyPrint; DejaVu и Liberation (замена Arial) — шрифты с кириллицей
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
        fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Только headless shell Chromium: для scraping полный браузер не нужен, образ легче
RUN pip install -r requirements.txt \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY config.py bot.py ./
COPY utils ./utils
COPY templates ./templates
COPY examples ./examples

RUN useradd --create-home --uid 1000 app \
    && mkdir -p data/json data/access reports logs \
    && chown -R app:app data reports logs
USER app

CMD ["python", "bot.py"]
