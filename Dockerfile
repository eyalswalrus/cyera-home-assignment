# --- Stage 1: build the React app ---------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Stage 2: install the backend into a virtualenv, exactly as pinned in uv.lock ---------
FROM python:3.14-slim AS server
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies first, in their own layer, so code changes don't reinstall them.
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY server/app ./app
RUN uv sync --locked --no-dev --no-editable

# --- Stage 3: runtime - just the virtualenv and the built UI, served from one origin ------
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    DATABASE_URL=sqlite+aiosqlite:////data/identityhub.db \
    STATIC_DIR=/app/static

COPY --from=server /opt/venv /opt/venv
COPY --from=web /web/dist /app/static

# Run as an unprivileged user; /data holds the SQLite database (mounted as a volume).
RUN useradd --system --no-create-home identityhub \
    && mkdir /data && chown identityhub /data
USER identityhub
WORKDIR /app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
