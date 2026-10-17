FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dev dependencies stay out of the production image. CI overrides this with
# --build-arg UV_SYNC_ARGS="--group dev" to run the test suite inside the image.
ARG UV_SYNC_ARGS="--no-dev"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project $UV_SYNC_ARGS

COPY . .
RUN uv sync --frozen $UV_SYNC_ARGS

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
