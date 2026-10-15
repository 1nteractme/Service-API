FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the application first so the image is self-contained.
COPY pyproject.toml README.md ./
COPY service_api ./service_api
COPY service_scraper ./service_scraper
COPY scrapy.cfg ./

RUN pip install . \
    && playwright install --with-deps chromium \
    && mkdir -p /app/data

EXPOSE 8000

# Use PORT when the hosting platform provides it; keep 8000 as the local default.
CMD ["sh", "-c", "uvicorn service_api.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
