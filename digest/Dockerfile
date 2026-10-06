FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 STATE_FILE=/data/digest-state.json
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY digest ./digest
RUN uv sync --locked --no-dev --no-editable && rm /bin/uv

RUN useradd --system --no-create-home digest && mkdir /data && chown digest /data
USER digest
WORKDIR /data
CMD ["digest", "--every-hours", "24"]
