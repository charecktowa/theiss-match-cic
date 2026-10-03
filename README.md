# theiss-match-cic
Repository for a LLM project to recommend thesis advisors and labs for the CIC

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
To load the `saber_sip` dataset (excluded from git — see `data/`):

```bash
docker compose exec -T db psql -U theiss -d theiss -v ON_ERROR_STOP=1 -f - < data/saber_sip.sql
```
