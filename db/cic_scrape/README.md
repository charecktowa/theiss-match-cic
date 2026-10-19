# CIC catalog database

`01_schema.sql` defines the standalone `raw` and `catalog` schemas and enables
pgvector. PostgreSQL must have the pgvector extension package installed; the
SQL statement enables it in the database. The full data snapshot is generated
at `data/cic_scrape.sql` by `scraper/build_postgres_export.py`.

The export creates a separate database named `cic_scrape`, so it can coexist
with `theiss` and `saber_scrape` on the same PostgreSQL instance. Restore it with:

```bash
psql -d postgres -f data/cic_scrape.sql
```

The target database must not already exist. The dump includes the schema,
pgvector extension activation, CIC catalog rows, source identities, and capture
provenance. The raw HTML bodies are kept in the local ignored archive under
`scraper/data/`; PostgreSQL stores their relative paths and SHA-256 hashes.

## Entity relationship diagram

```mermaid
erDiagram
    SOURCE ||--o{ CAPTURE_RUN : records
    CAPTURE_RUN ||--o{ SOURCE_DOCUMENT : archives
    SOURCE_DOCUMENT o|--o{ OBSERVATION : supports
    SOURCE ||--o{ OBSERVATION : asserts
    SOURCE_DOCUMENT o|--o{ ORGANIZATION : supports
    SOURCE_DOCUMENT o|--o{ RESEARCH_LINE : supports
    ORGANIZATION o|--o{ LABORATORY : contains
    LABORATORY ||--o{ LABORATORY_RESEARCH_LINE : tagged
    RESEARCH_LINE ||--o{ LABORATORY_RESEARCH_LINE : classifies
    PROGRAM o|--o{ THESIS : classifies
    SOURCE_DOCUMENT o|--o{ PERSON : supports
    PERSON ||--o{ PERSON_SOURCE_IDENTITY : identified_by
    SOURCE ||--o{ PERSON_SOURCE_IDENTITY : scopes
    PERSON ||--o{ PERSON_ALIAS : has
    PERSON ||--o{ PERSON_AFFILIATION : affiliated
    ORGANIZATION o|--o{ PERSON_AFFILIATION : employs
    LABORATORY o|--o{ PERSON_AFFILIATION : hosts
    THESIS ||--o{ THESIS_SOURCE_IDENTITY : identified_by
    SOURCE ||--o{ THESIS_SOURCE_IDENTITY : scopes
    THESIS ||--o{ THESIS_PARTICIPANT : has
    PERSON ||--o{ THESIS_PARTICIPANT : advises_or_serves
    LABORATORY o|--o{ THESIS_PARTICIPANT : primary_lab
    VENUE o|--o{ RESEARCH_PRODUCT : publishes
    RESEARCH_PRODUCT ||--o{ PRODUCT_SOURCE_IDENTITY : identified_by
    SOURCE ||--o{ PRODUCT_SOURCE_IDENTITY : scopes
    RESEARCH_PRODUCT ||--o{ PRODUCT_AUTHOR : has_author
    PERSON ||--o{ PRODUCT_AUTHOR : authors
    RESEARCH_PRODUCT ||--o{ PRODUCT_LABORATORY : associated_with
    LABORATORY ||--o{ PRODUCT_LABORATORY : hosts
    RESEARCH_PROJECT ||--o{ PROJECT_SOURCE_IDENTITY : identified_by
    SOURCE ||--o{ PROJECT_SOURCE_IDENTITY : scopes
    RESEARCH_PROJECT ||--o{ PROJECT_MEMBER : has_member
    PERSON ||--o{ PROJECT_MEMBER : participates
    RESEARCH_PROJECT ||--o{ PROJECT_LABORATORY : associated_with
    LABORATORY ||--o{ PROJECT_LABORATORY : hosts
    SOURCE_DOCUMENT o|--o{ DATA_ISSUE : explains
```

`catalog.embedding` stores versioned pgvector vectors for theses, research
products, or projects. Its `(entity_type, entity_id)` pair is polymorphic, so
it does not use a foreign key to just one of those entity tables. `catalog.data_issue`
keeps quality warnings; `catalog.observation` preserves source assertions and
conflicts. Source identity tables preserve external IDs without treating them
as globally unique. `person_affiliation` and the product/project join tables
allow people and work to relate to multiple institutions or labs.

`thesis_participant.role` distinguishes primary advisor, co-advisor, committee
member, and unknown. The thesis's `assigned_laboratory_id` is attached only to
the primary-advisor participant. Committee members listed without roles by the
CIC thesis detail page are not guessed to be co-advisors.
