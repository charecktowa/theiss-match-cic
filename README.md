# theiss-match-cic
Repository for a LLM project to recommend thesis advisors and labs for the CIC

## Repository layout

```
backend/        FastAPI service. The only uv project in the repository.
  app/          application code
  tests/        pytest suite
compose.yaml    local orchestration for every service
db/init/        SQL that runs once, when the database volume is first created
db/setup.sh     loads the datasets in data/ into the local database instance
db/saber_scrape/ builds saber_scrape from the frozen SABER export (see its README)
db/cic_scrape/  reproducible CIC catalog schema and pgvector extension setup
scraper/        CIC capture, parsing, and comparison code; raw HTML stays local
data/           datasets and generated SQL exports (ignored: inputs, not code)
experiments/    marimo notebooks; its own uv project, separate from the API
frontend/       UI (planned; technology not chosen yet)
```

Three rules the layout follows, so new work knows where to go:

- **A service owns its build.** Each service keeps its `Dockerfile` beside its
  code, so the build context stays minimal and a dependency change is one diff.
- **The database is shared, not part of the API.** `experiments/` connects to the
  same instance, so the *instance* is defined in the root `compose.yaml` and only
  the *schema* lives under `db/`.
- **`data/` holds inputs.** Dumps and datasets are read by both the backend and
  the notebooks, so neither owns them.

Each Python project keeps its own `pyproject.toml`, `uv.lock` and environment, so
their dependencies never have to agree; see `experiments/README.md` for how its
notebooks are run and linted.

## Quality checks

The hooks are the same checks CI runs, so a clean local run means a green
pipeline:

```bash
uv tool install pre-commit      # or: uv run --project backend pre-commit
pre-commit install              # run the hooks on every commit
pre-commit run --all-files      # run everything right now
```

Hooks run in their own isolated environments, so every `rev` in
`.pre-commit-config.yaml` must equal the version resolved in `backend/uv.lock`;
otherwise the versions committed are not the ones CI checks. Dependabot watches
both so they cannot drift apart.

Actions in `.github/workflows/` are pinned to commit SHAs. Keep them that way: a
tag is mutable, and pinning is what forces the reference to be resolved.

`zizmor` runs offline as a hook, so it enforces that pinning policy but skips
the audits that need the GitHub API. For the full audit, run it with a token:

```bash
uvx zizmor@1.31.1 --gh-token "$(gh auth token)" .github/
```

## Merging changes

`main` is protected, so every change arrives through a pull request:

- Both CI jobs (`quality` and `docker`) must pass, and the branch must be up to
  date with `main`.
- The only merge option is **Squash**, so `main` keeps a linear history and one
  commit per pull request.
- Every commit the pull request introduces must be signed and verified; GitHub
  refuses the merge otherwise.

Squash merging means the commit that lands on `main` is created and signed by
GitHub, not by the key used on the branch, so the branch commits' own signatures
do not survive the merge. Every commit on the branch must still be signed: an
unsigned commit blocks the merge, even though the squash commit itself would be
signed by GitHub.

## Running locally

Requires Docker with the Compose v2 plugin.

```bash
docker compose up -d --build   # API + pgvector database
docker compose up -d db        # database only
docker compose ps
docker compose logs -f api
docker compose down            # stop, keep data
docker compose down -v         # stop and delete the pgdata volume
```

| Service | Address | Notes |
| --- | --- | --- |
| `api` | http://localhost:8000 | FastAPI; OpenAPI at `/docs`. `backend/app` is bind-mounted, so edits reload without a rebuild. |
| `db` | localhost:5432 | PostgreSQL 16 + pgvector. Published so notebooks in `experiments/` can connect from the host. |

Credentials default to `theiss`/`theiss`/`theiss`. Override them with the
`POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` environment variables; the API
picks up the matching `DATABASE_URL` automatically.

`db/init/` scripts run only when the `pgdata` volume is initialized for the first time.

### Loading the datasets

The dumps in `data/` are excluded from git, so a fresh clone has no data. Put
`saber_sip.sql` and `cic_scrape.sql` in `data/`, then load everything at once:

```bash
db/setup.sh
```

The script starts the `db` service and loads each database it has not loaded yet:

| Database | Source | Holds |
| --- | --- | --- |
| `theiss` (`POSTGRES_DB`) | `data/saber_sip.sql` | the raw SABER dump in `public`, including the 768-d embeddings |
| `cic_scrape` | `data/cic_scrape.sql` | the CIC catalog (`raw` and `catalog` schemas) |
| `saber_scrape` | `data/saber_sip.sql` + `db/saber_scrape/` | the cleaned copy, built by `db/saber_scrape/build.sh` |

It is idempotent: a step is skipped when its database is already loaded, so it is
safe to re-run after adding a dump. A restore is atomic for `theiss`, and
`cic_scrape` is marked complete only after its restore succeeds, so an
interrupted load is redone on the next run instead of being skipped.
`cic_scrape` is restored without embeddings, so only `theiss` starts with
vectors. The same loads can be run by hand:

```bash
# one-off restore into the application database
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f -' < data/saber_sip.sql

# the CIC dump creates cic_scrape itself, so it runs against the server's database
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -f -' < data/cic_scrape.sql
```

The single quotes are required: they keep the host shell from expanding the
variables, so the ones configured inside the `db` container are used instead.
