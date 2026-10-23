# CIC scraper and catalog export

This directory holds the CIC web capture, parsers, and comparison tools. The
PostgreSQL schema is in `db/cic_scrape/`; the full restorable export is in
`data/cic_scrape.sql`. CIC pages are the source of current catalog values.
The old SABER dump remains outside this database as a frozen comparison source;
it is not queried or loaded into `cic_scrape`.

## Capture and local archive

```bash
python3 scraper/capture.py
```

The default capture writes to `scraper/data/cic_raw/runs/<run-id>/`, archives
each returned HTML body, and records request metadata and SHA-256 hashes in
`manifest.jsonl`. It waits one second between requests, retries a request up to
twice after a network error (`--retries`), and does not request
student-directory detail pages. Thesis detail forms are captured by default;
`--kinds thesis,project,publication` also requests individual project and
publication detail forms. The October 2026 snapshot used below contains lab and
researcher pages plus thesis details; publications and projects are parsed from
the researcher-page tables.

The raw archive includes names shown in thesis pages. It is excluded from Git
and must remain local. The catalog export intentionally excludes student names
and email addresses. The CIC footer permits non-commercial reproduction with
attribution and requires written authorization for other uses; the database
retains CIC source URLs and capture dates for attribution and provenance.

## Data model

`db/cic_scrape/01_schema.sql` creates the `raw` and `catalog` schemas and enables
`pgvector`. The model separates people from affiliations, supports multiple
labs/institutions, and stores thesis participant roles independently. CIC's
`Director 1` and `Director 2` labels map to primary and co-advisor. Names found
only in the thesis committee are stored as committee members because the detail
page does not say which committee member is a co-advisor. Embeddings have a
versioned pgvector table but are not populated yet.

Current CIC IDs are stored as source-scoped identities. A primary lab is linked
to a thesis only when the CIC page identifies one unambiguous lab for the
primary advisor; unresolved cases are recorded in `catalog.data_issue`.
External people can exist without a CIC profile, and their affiliation is left
blank unless a source states it.

Each CIC profile is one person; a name is evidence for a match, never an
identity. A lab member link or a committee name is attached to a profile only
when exactly one profile has the same accent-, case- and order-insensitive name.
A committee name that matches no profile becomes an external person, and one
that matches several is not guessed: it is recorded in `catalog.data_issue`.
Spellings that differ from the profile name (for example without accents) are
kept in `catalog.person_alias` with `reviewed_match = false`.

Thesis status comes from the profile tabs: `tab1` lists concluded theses and
maps to `graduated`. Everything else stays `unknown`, because checked against
SABER the other tab mixes enrolled students with graduates and does not say
whether a student withdrew.

`catalog.data_issue` codes written by the export:

| Code | Meaning |
| --- | --- |
| `multiple_director_1_profiles` | Several profiles list the thesis as Director 1; none is kept as primary |
| `primary_lab_not_unambiguous` | The primary advisor does not map to exactly one lab |
| `thesis_detail_missing` | A profile lists the thesis, but its detail page is missing (only with `--allow-incomplete`) |
| `ambiguous_committee_name` | A committee name matches several profiles |
| `ambiguous_member_name` | A lab member link matches several profiles |
| `unmatched_member_link` | A lab member link matches no archived profile; no affiliation is recorded |
| `not_a_researcher_profile` | An archived researcher page names no researcher; it is skipped |
| `conflicting_<field>s_across_profiles` | Profiles disagree on a product's or project's title, kind, year or venue; the first value is kept |

## Build the PostgreSQL export

```bash
python3 scraper/build_postgres_export.py
```

This reads the newest finished capture (or `--run-dir`) and produces
`data/cic_scrape.sql`, a generated file that is not committed. It refuses a
capture that did not finish or that has a request which never archived a
response; a failure retried later in the same manifest counts as resolved.
`--allow-incomplete` exports such a capture anyway, notes it in
`raw.capture_run.notes`, and records each thesis it could not load. The psql script creates a
separate `cic_scrape` database, installs the `vector` extension, creates the
schema, and inserts parsed CIC data and capture provenance. It can be restored
alongside the existing database on a PostgreSQL instance with pgvector installed:

```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -f -' < data/cic_scrape.sql
```

The export expects the target database name not to exist yet. It does not drop,
rename, or modify `theiss` or `saber_scrape`. Everything after `CREATE DATABASE`
runs in one transaction, so a failed restore leaves an empty `cic_scrape`; drop
it before trying again (`db/setup.sh` does so itself). The raw HTML stays in the local
archive; the SQL export contains the complete `cic_scrape` database structure
and parsed catalog data, plus relative archive paths, but not HTML bodies.

## Compare the frozen SABER export

```bash
python3 scraper/build_comparison.py
sqlite3 -header -column scraper/data/saber_cic_comparison.sqlite \
  "SELECT * FROM source_summary;"
```

The comparison SQLite file is a local review artifact, not the recommendation
database. The current snapshot has 1,335 SABER thesis rows and 1,401 CIC details;
1,298 numeric IDs are present in both, 1,247 titles match after quote/spacing
normalization, and 51 titles differ. SABER's URL suffix differs from its row ID
in 208 shared records, so ID matches and lab-name pairings remain reviewable
claims, not automatic identity merges. The comparison uses the newest finished
capture unless `--run-dir` names another, refuses a capture without lab pages,
researcher pages or (when requested) thesis details, and replaces the previous
SQLite file only once the new one is complete.

## Dependencies and linting

The scripts are standard-library only, so the project declares no runtime
dependencies. It is still a uv project, like `backend/` and `experiments/`, so
that ruff and pytest have a configuration of their own and CI has something to
check:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The tests use small invented HTML fixtures, so they need neither the network
nor a capture. Lint and format also run as pre-commit hooks; all three run in
the `scraper` CI job.
