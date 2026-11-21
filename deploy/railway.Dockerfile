# syntax=docker/dockerfile:1
# The image Railway runs, for both services (phase 22b):
#   api    — alembic upgrade, then uvicorn serving the API and the built dashboard
#   worker — `python -m app.worker`, set as the service's start command
# Build context is the repository root:
#   docker build -f deploy/railway.Dockerfile .
#
# Differs from backend/Dockerfile on purpose: it builds the dashboard, installs
# the embedding runtime (CPU-only torch — the CUDA wheels are gigabytes nobody
# here can use), and runs as root, because a Railway volume is mounted owned by
# root and the model weights, the embedding cache and staged uploads live there.

FROM node:22-alpine AS dashboard
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build


FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
# No cache mounts: Railway accepts them only under its own service-id prefix.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
RUN uv sync --frozen --no-dev \
    && uv pip install --python /src/.venv/bin/python \
        --index-url https://download.pytorch.org/whl/cpu "torch>=2.4" \
    && uv pip install --python /src/.venv/bin/python "sentence-transformers>=6.0"


FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/src/.venv/bin:${PATH}" \
    ENVIRONMENT=production \
    FRONTEND_DIST_DIR=/src/frontend/dist \
    HF_HOME=/data/huggingface \
    EMBEDDING_CACHE_DIR=/data/embeddings \
    UPLOAD_DIR=/data/uploads
WORKDIR /src
COPY --from=builder /src/.venv /src/.venv
COPY --from=builder /src/backend /src/backend
COPY alembic.ini ./
COPY --from=dashboard /frontend/dist /src/frontend/dist
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
