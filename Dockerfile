# --- Stage 1: build the React app ---------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Stage 2: Python runtime serving API + built UI from one origin -----------------------
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/identityhub.db \
    STATIC_DIR=/app/static

WORKDIR /app
COPY server/pyproject.toml ./
COPY server/app ./app
RUN pip install .

COPY --from=web /web/dist ./static

# Run as an unprivileged user; /data holds the SQLite database (mounted as a volume).
RUN useradd --system --no-create-home identityhub \
    && mkdir /data && chown identityhub /data
USER identityhub

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
