# backend

The API for the thesis-advisor recommender: FastAPI on Python 3.13, managed with
[uv](https://docs.astral.sh/uv/).

This is the only uv project in the repository, and CI runs its commands from
this directory.

## Layout

```
app/        FastAPI application (routers under app/routers/)
tests/      pytest suite
```

## Commands

```bash
uv sync --frozen                                  # install from the lockfile
uv run uvicorn app.main:app --reload              # serve locally
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run bandit -r . -x .venv -c pyproject.toml
```

ruff, mypy, bandit and pytest are configured in `pyproject.toml`. The same
commands also run as pre-commit hooks, from the repository root, in isolated
environments.

## Docker

The image is built from the repository root (`docker compose build`), which
resolves `build` to this directory. `UV_SYNC_ARGS` defaults to `--no-dev`, so
the production image excludes the dev group; CI overrides it with
`--group dev` to run the test suite inside a built image.

## Database

The API expects a pgvector-backed PostgreSQL and reads its connection string
from `DATABASE_URL`. Locally that instance is provided by the root
`compose.yaml`; see the repository README for credentials and how to load
`data/saber_sip.sql`.
