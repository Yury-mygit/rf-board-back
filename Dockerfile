FROM python:3.12-slim

WORKDIR /app

# deb.debian.org из buildkit network нестабилен (см. memory
# reference_docker_buildkit_outbound_broken). Переключаем apt на Yandex mirror.
RUN sed -i 's|http://deb.debian.org|http://mirror.yandex.ru|g' \
    /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true && \
    apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# pypi.org из buildkit тоже нестабилен и Aliyun-mirror через buildkit
# зависает. Wheels собираются вручную на dev-машине (см. карта
# board-external-ref-stable-id Этап 1) и кладутся в ./wheels/ (.gitignore'нуты).
COPY pyproject.toml ./
COPY wheels ./wheels/
COPY app ./app
RUN pip install --no-cache-dir --no-index --find-links ./wheels .

COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
