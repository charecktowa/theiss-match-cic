# experiments

marimo notebooks for exploring embedding models and retrieval approaches, before
anything is wired into the API.

This is a uv project of its own: it has its own `pyproject.toml` and `uv.lock`,
and shares no environment with `backend/`.

## Running a notebook

```bash
uv run marimo edit notebook.py     # edit one notebook
uv run marimo edit .               # browse every notebook here
```

`marimo edit` only reads and writes the notebook file; it does not require the
API or the database to be running. Connecting to the database from a notebook
uses the same instance the API uses (`localhost:5432`). The repository README has
the credentials; load the datasets with `db/setup.sh` before querying them.

## Dependencies

`marimo`, `numpy`, `polars`, `sentence-transformers` and the PostgreSQL driver
(`psycopg` with `pgvector`) are already declared. Add anything else with:

```bash
uv add <package>            # for every notebook
uv add --dev <package>      # for tooling only
```

### Sandbox notebooks

A notebook can instead declare its own dependencies inline and run in an
isolated environment, which is useful when a single experiment needs packages
that would otherwise conflict with the shared set:

```bash
uv run marimo edit --sandbox notebook.py
```

Both styles can live side by side.

## Linting

ruff is configured in `pyproject.toml`, overrides the repository defaults, and
ignores `B018` because in marimo the last expression of a cell is its output.
The same hooks run in CI and as pre-commit hooks from the repository root.

```bash
uv run ruff check .
uv run ruff format --check .
```
