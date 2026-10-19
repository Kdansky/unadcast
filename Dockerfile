FROM python:3.12-slim AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv PATH=/venv/bin:$PATH UNADCAST_CONFIG=/work/config.toml PYTHONUNBUFFERED=1
WORKDIR /app

FROM base AS app
COPY pyproject.toml uv.lock ./
# The cache mount keeps uv's download cache out of the image; it would double its size.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY unadcast ./unadcast
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
WORKDIR /work

FROM app AS dev
# bin/on-demand's tests: it uses busybox-style wget, as in the watcher image. node checks the admin page's script parses.
RUN apt-get update && apt-get install -y --no-install-recommends wget nodejs && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY bin ./bin
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
COPY tests ./tests
