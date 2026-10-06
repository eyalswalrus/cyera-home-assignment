# --- Stage 1: build the React app ---------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Stage 2: install the backend and its dependencies into a virtualenv ------------------
FROM python:3.14-slim AS server
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY server/pyproject.toml /src/
COPY server/app /src/app
RUN /opt/venv/bin/pip install /src

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
