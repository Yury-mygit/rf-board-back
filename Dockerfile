FROM python:3.12-slim

WORKDIR /app

# libcairo2 — runtime lib for cairosvg/cairocffi (loaded via cffi at
# runtime, no build headers needed). curl — healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
# BRD-24 / REG-2: test-deps (pytest+asyncio) ставим вместе с runtime — dev
# image, prod-инстанса пока нет; акт как pilot test-harness для платформы.
RUN pip install --no-cache-dir '.[test]'

COPY alembic.ini ./
COPY alembic ./alembic
COPY tests ./tests

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
