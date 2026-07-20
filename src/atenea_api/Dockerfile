# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.11-slim AS compiler

ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

WORKDIR /app/

RUN apt-get update && apt-get install -y --no-install-recommends \
    git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/
COPY ./pyproject.toml ./uv.lock /app/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app/

COPY --from=compiler /opt/venv /opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY . /app/

RUN chmod +x ./download.sh
RUN ./download.sh

EXPOSE 8000

CMD ["gunicorn", "atenea_api.wsgi:application", "--worker-class", "gevent", "--workers", "10", "--timeout", "300", "--log-level", "info", "--bind", "0.0.0.0:8000"]
