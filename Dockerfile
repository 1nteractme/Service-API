FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the application first so Railway can build a self-contained image.
COPY pyproject.toml README.md ./
COPY service_api ./service_api
COPY service_scraper ./service_scraper
COPY scrapy.cfg ./

RUN pip install . \
    && playwright install --with-deps chromium \
    && mkdir -p /app/data

EXPOSE 8000

# Railway provides PORT at runtime. The fallback keeps the image convenient locally.
CMD ["sh", "-c", "uvicorn service_api.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
