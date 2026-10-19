-- Fresh catalog for CIC web data. SABER is kept as a separate legacy source;
-- current CIC pages drive the selected catalog values.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE SCHEMA raw;
CREATE SCHEMA catalog;

CREATE TABLE raw.source (
    code text PRIMARY KEY CHECK (code IN ('cic', 'saber_legacy', 'manual')),
    name text NOT NULL
);
INSERT INTO raw.source (code, name) VALUES
    ('cic', 'CIC public website'),
    ('saber_legacy', 'One-time SABER export'),
    ('manual', 'Reviewed correction');

CREATE TABLE raw.capture_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_code text NOT NULL REFERENCES raw.source,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    extractor_version text NOT NULL,
    notes text
);

-- HTML bodies live in the ignored scraper/data/cic_raw archive; this table records
-- how to find and verify each response without placing HTML in Git/Postgres.
CREATE TABLE raw.source_document (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES raw.capture_run ON DELETE CASCADE,
    request_key text NOT NULL,
    method text NOT NULL CHECK (method IN ('GET', 'POST', 'IMPORT')),
    request_url text NOT NULL,
    final_url text,
    form_data jsonb,
    http_status integer,
    content_type text,
    captured_at timestamptz,
    stored_at timestamptz NOT NULL DEFAULT now(),
    archive_path text,
    sha256 char(64),
    error text,
    CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX source_document_url_idx ON raw.source_document (request_url);
CREATE INDEX source_document_request_key_idx ON raw.source_document (run_id, request_key);

CREATE TYPE catalog.degree_level AS ENUM ('masters', 'doctorate', 'unknown');
CREATE TYPE catalog.completion_status AS ENUM (
    'enrolled', 'finished_coursework', 'graduated', 'withdrawn', 'unknown'
);
CREATE TYPE catalog.thesis_role AS ENUM (
    'primary_advisor', 'co_advisor', 'committee_member', 'unknown'
);

-- Keep all source assertions, including conflicts. The selected catalog value
-- can then be rebuilt without losing what CIC or SABER said at capture time.
CREATE TABLE catalog.observation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_type text NOT NULL,
    entity_key text NOT NULL,
    field_name text NOT NULL,
    value jsonb,
    source_document_id bigint REFERENCES raw.source_document,
    source_code text NOT NULL REFERENCES raw.source,
    observed_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    selected boolean NOT NULL DEFAULT false,
    note text
);
CREATE INDEX observation_entity_idx ON catalog.observation (entity_type, entity_key, field_name);
CREATE UNIQUE INDEX observation_single_selected_idx
    ON catalog.observation (entity_type, entity_key, field_name) WHERE selected;

CREATE TABLE catalog.organization (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    organization_type text NOT NULL CHECK (organization_type IN ('ipn', 'university', 'other', 'unknown')),
    source_document_id bigint REFERENCES raw.source_document
);

CREATE TABLE catalog.research_line (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE,
    source_document_id bigint REFERENCES raw.source_document
);

CREATE TABLE catalog.laboratory (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    cic_slug text UNIQUE,
    organization_id bigint REFERENCES catalog.organization,
    is_external boolean NOT NULL DEFAULT false,
    source_document_id bigint REFERENCES raw.source_document
);

CREATE TABLE catalog.laboratory_research_line (
    laboratory_id bigint NOT NULL REFERENCES catalog.laboratory ON DELETE CASCADE,
    research_line_id bigint NOT NULL REFERENCES catalog.research_line ON DELETE CASCADE,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (laboratory_id, research_line_id)
);

CREATE TABLE catalog.program (
    code text PRIMARY KEY,
    name text,
    level catalog.degree_level NOT NULL,
    source_document_id bigint REFERENCES raw.source_document
);

-- A person may be a CIC researcher, an external co-advisor, a committee
-- member, or more than one of these. Source IDs are optional and source-scoped.
CREATE TABLE catalog.person (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    display_name text NOT NULL,
    name_key text,
    email text,
    profile_url text,
    source_document_id bigint REFERENCES raw.source_document,
    reviewed_identity boolean NOT NULL DEFAULT false
);
CREATE INDEX person_name_key_idx ON catalog.person (name_key);

CREATE TABLE catalog.person_source_identity (
    source_code text NOT NULL REFERENCES raw.source,
    source_key text NOT NULL,
    person_id bigint NOT NULL REFERENCES catalog.person ON DELETE CASCADE,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (source_code, source_key),
    UNIQUE (source_code, person_id, source_key)
);

CREATE TABLE catalog.person_alias (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id bigint NOT NULL REFERENCES catalog.person ON DELETE CASCADE,
    name_seen text NOT NULL,
    name_key text,
    source_document_id bigint REFERENCES raw.source_document,
    reviewed_match boolean NOT NULL DEFAULT false,
    UNIQUE (person_id, name_seen, source_document_id)
);
CREATE INDEX person_alias_name_key_idx ON catalog.person_alias (name_key);

-- Affiliations are many-to-many and time-aware. An external advisor can have
-- an institution but no CIC laboratory; a CIC co-advisor can belong to another
-- lab from the primary advisor.
CREATE TABLE catalog.person_affiliation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id bigint NOT NULL REFERENCES catalog.person ON DELETE CASCADE,
    organization_id bigint REFERENCES catalog.organization,
    laboratory_id bigint REFERENCES catalog.laboratory,
    role text,
    is_primary boolean NOT NULL DEFAULT false,
    valid_from date,
    valid_to date,
    source_document_id bigint REFERENCES raw.source_document,
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE INDEX person_affiliation_person_idx ON catalog.person_affiliation (person_id);
CREATE INDEX person_affiliation_lab_idx ON catalog.person_affiliation (laboratory_id);

CREATE TABLE catalog.thesis (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title text NOT NULL,
    program_code text REFERENCES catalog.program,
    completion_status catalog.completion_status NOT NULL DEFAULT 'unknown',
    completion_year smallint CHECK (completion_year BETWEEN 1900 AND 2100),
    url text,
    source_document_id bigint REFERENCES raw.source_document
);

CREATE TABLE catalog.thesis_source_identity (
    source_code text NOT NULL REFERENCES raw.source,
    source_key text NOT NULL,
    thesis_id bigint NOT NULL REFERENCES catalog.thesis ON DELETE CASCADE,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (source_code, source_key)
);

-- N participants, not a hard-coded pair. The assigned lab belongs only on the
-- primary advisor row; co-advisor affiliations do not change the thesis lab.
CREATE TABLE catalog.thesis_participant (
    thesis_id bigint NOT NULL REFERENCES catalog.thesis ON DELETE CASCADE,
    person_id bigint NOT NULL REFERENCES catalog.person,
    role catalog.thesis_role NOT NULL DEFAULT 'unknown',
    participant_order smallint,
    assigned_laboratory_id bigint REFERENCES catalog.laboratory,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (thesis_id, person_id, role),
    CHECK (role = 'primary_advisor' OR assigned_laboratory_id IS NULL)
);
CREATE INDEX thesis_participant_person_idx ON catalog.thesis_participant (person_id);
CREATE INDEX thesis_participant_lab_idx ON catalog.thesis_participant (assigned_laboratory_id);
CREATE UNIQUE INDEX thesis_one_primary_advisor_idx
    ON catalog.thesis_participant (thesis_id) WHERE role = 'primary_advisor';

CREATE TABLE catalog.venue (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE
);

CREATE TABLE catalog.research_product (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('publication', 'conference', 'other')),
    title text NOT NULL,
    description text,
    year smallint CHECK (year BETWEEN 1900 AND 2100),
    venue_id bigint REFERENCES catalog.venue,
    source_document_id bigint REFERENCES raw.source_document
);
CREATE TABLE catalog.product_source_identity (
    source_code text NOT NULL REFERENCES raw.source,
    source_key text NOT NULL,
    product_id bigint NOT NULL REFERENCES catalog.research_product ON DELETE CASCADE,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (source_code, source_key)
);
CREATE TABLE catalog.product_author (
    product_id bigint NOT NULL REFERENCES catalog.research_product ON DELETE CASCADE,
    person_id bigint NOT NULL REFERENCES catalog.person,
    author_order smallint,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (product_id, person_id)
);
CREATE TABLE catalog.product_laboratory (
    product_id bigint NOT NULL REFERENCES catalog.research_product ON DELETE CASCADE,
    laboratory_id bigint NOT NULL REFERENCES catalog.laboratory,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (product_id, laboratory_id)
);

CREATE TABLE catalog.research_project (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title text NOT NULL,
    description text,
    year smallint CHECK (year BETWEEN 1900 AND 2100),
    source_document_id bigint REFERENCES raw.source_document
);
CREATE TABLE catalog.project_source_identity (
    source_code text NOT NULL REFERENCES raw.source,
    source_key text NOT NULL,
    project_id bigint NOT NULL REFERENCES catalog.research_project ON DELETE CASCADE,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (source_code, source_key)
);
CREATE TABLE catalog.project_member (
    project_id bigint NOT NULL REFERENCES catalog.research_project ON DELETE CASCADE,
    person_id bigint NOT NULL REFERENCES catalog.person,
    role text,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (project_id, person_id)
);
CREATE TABLE catalog.project_laboratory (
    project_id bigint NOT NULL REFERENCES catalog.research_project ON DELETE CASCADE,
    laboratory_id bigint NOT NULL REFERENCES catalog.laboratory,
    source_document_id bigint REFERENCES raw.source_document,
    PRIMARY KEY (project_id, laboratory_id)
);

CREATE TABLE catalog.data_issue (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_type text NOT NULL,
    entity_key text NOT NULL,
    code text NOT NULL,
    detail text,
    source_document_id bigint REFERENCES raw.source_document,
    UNIQUE (entity_type, entity_key, code)
);

-- Keep embeddings separate from source entities so multiple model versions and
-- vector dimensions can coexist without rewriting theses or products.
CREATE TABLE catalog.embedding (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_type text NOT NULL CHECK (entity_type IN ('thesis', 'research_product', 'research_project')),
    entity_id bigint NOT NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    embedding public.vector NOT NULL,
    source_text_sha256 char(64) NOT NULL CHECK (source_text_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (public.vector_dims(embedding) = dimensions),
    UNIQUE (entity_type, entity_id, model_name, model_version, source_text_sha256)
);
-- No HNSW index yet. pgvector rejects an index on a `vector` column with no
-- fixed dimension ("column does not have dimensions"), and this table keeps one
-- row per model so the dimension varies by row. Once a model and its dimension
-- are chosen, create one expression index per model:
--   CREATE INDEX embedding_<model>_hnsw_idx ON catalog.embedding
--       USING hnsw ((embedding::public.vector(1024)) public.vector_cosine_ops)
--       WHERE model_name = '<model>' AND model_version = '<version>';
