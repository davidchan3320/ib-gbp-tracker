# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.12-alpine3.23
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.29

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /opt/app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="FX Tape" \
      org.opencontainers.image.description="GBP/USD OHLC collector for Interactive Brokers" \
      org.opencontainers.image.version="0.6.0"

ENV PATH="/opt/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL="sqlite+aiosqlite:////data/fx_tape.db"

RUN apk upgrade --no-cache \
    && addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app -s /sbin/nologin app \
    && mkdir -p /opt/app /data \
    && chown app:app /opt/app /data

WORKDIR /opt/app

COPY --from=builder --chown=app:app /opt/app/.venv /opt/app/.venv

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

STOPSIGNAL SIGINT

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
