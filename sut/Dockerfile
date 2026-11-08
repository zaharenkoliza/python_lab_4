FROM python:3.12.6-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: сборка падает, если uv.lock не соответствует pyproject.toml
RUN uv sync --frozen --no-dev
COPY app ./app
ENV DRIVER=async_asyncpg POOL_SIZE=20 PATH="/app/.venv/bin:$PATH"
# 1 воркер: SUT ограничен 1 ядром, число процессов не должно маскировать эффект драйвера
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--log-level", "warning"]
