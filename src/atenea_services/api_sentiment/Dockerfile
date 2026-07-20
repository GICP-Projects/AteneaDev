# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.11-slim

LABEL stage=builder
ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/backend/.venv \
    UV_LINK_MODE=copy \
    PATH="/backend/.venv/bin:$PATH"

WORKDIR /backend

COPY --from=uv /uv /uvx /bin/
COPY ./pyproject.toml ./uv.lock /backend/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY . /backend/

EXPOSE 8000
ENTRYPOINT ["uvicorn"]
CMD ["src.api:app", "--host", "0.0.0.0"]
