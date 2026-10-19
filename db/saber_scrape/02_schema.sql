-- Improved schema for the SABER-derived catalog.
--   catalog: what was scraped from SABER, cleaned. Read-mostly.
--   app:     what the application writes: accounts, thesis ideas, requests.
-- No embeddings live here on purpose; add them per model once one is chosen.
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA catalog;
CREATE SCHEMA app;

COMMENT ON SCHEMA catalog IS 'SABER data (labs, professors, students, theses, products), cleaned from raw.';
COMMENT ON SCHEMA app IS 'Data written by the application: users, thesis ideas, advisor and lab requests.';

-- ---------------------------------------------------------------------------
-- Text helpers
-- ---------------------------------------------------------------------------

-- Trims and collapses runs of whitespace. Titles must already be in this form.
CREATE FUNCTION catalog.clean_text(text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN regexp_replace(btrim($1), '\s+', ' ', 'g');

-- Case-, accent- and whitespace-insensitive key: two texts with the same key
-- are treated as the same title or venue. The two-argument unaccent is the
-- immutable form, which is what lets this function back a unique index.
CREATE FUNCTION catalog.match_key(text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN lower(public.unaccent('public.unaccent'::regdictionary, catalog.clean_text($1)));

-- ---------------------------------------------------------------------------
-- catalog: people, labs and programs
-- ---------------------------------------------------------------------------

CREATE TYPE catalog.degree_level AS ENUM ('masters', 'doctorate');

-- inscrito -> enrolled, egresado -> finished_coursework (no degree yet),
-- graduado -> graduated, baja -> withdrawn.
CREATE TYPE catalog.enrollment_status AS ENUM (
    'enrolled', 'finished_coursework', 'graduated', 'withdrawn'
);

CREATE TABLE catalog.laboratory (
    id   integer PRIMARY KEY,
    name text NOT NULL UNIQUE CHECK (name = catalog.clean_text(name) AND name <> '')
);

CREATE TABLE catalog.professor (
    id            integer PRIMARY KEY,
    laboratory_id integer NOT NULL REFERENCES catalog.laboratory,
    name          text NOT NULL,
    email         citext,
    profile_url   text
);
CREATE INDEX professor_laboratory_id_idx ON catalog.professor (laboratory_id);

-- Authors of past theses. Application accounts live in app.app_user.
CREATE TABLE catalog.student (
    id          integer PRIMARY KEY,
    name        text NOT NULL,
    email       citext,
    profile_url text
);

-- Real lab membership only. Graduation is a status on catalog.enrollment, not
-- a lab: the scrape's placeholder lab 999 ("Laboratorio Egresados") is gone.
CREATE TABLE catalog.student_laboratory (
    student_id    integer REFERENCES catalog.student,
    laboratory_id integer REFERENCES catalog.laboratory,
    PRIMARY KEY (student_id, laboratory_id)
);
CREATE INDEX student_laboratory_laboratory_id_idx ON catalog.student_laboratory (laboratory_id);

CREATE TABLE catalog.program (
    code  text PRIMARY KEY,
    name  text,  -- NULL where the official name is not confirmed yet
    level catalog.degree_level NOT NULL
);

CREATE TABLE catalog.enrollment (
    id           integer PRIMARY KEY,
    student_id   integer NOT NULL REFERENCES catalog.student,
    program_code text NOT NULL REFERENCES catalog.program,
    status       catalog.enrollment_status NOT NULL,
    UNIQUE (student_id, program_code)
);
CREATE INDEX enrollment_program_status_idx ON catalog.enrollment (program_code, status);

-- ---------------------------------------------------------------------------
-- catalog: theses
-- ---------------------------------------------------------------------------

CREATE TABLE catalog.thesis (
    id            integer PRIMARY KEY,
    enrollment_id integer NOT NULL REFERENCES catalog.enrollment,
    title         text NOT NULL CHECK (title = catalog.clean_text(title) AND title <> ''),
    -- Trusted link only: the scraped URL whose trailing ID is this thesis.
    url           text CHECK (url ~ '^https://'),
    -- The URL as scraped, kept for provenance. It can point to another thesis.
    source_url    text
);
CREATE INDEX thesis_enrollment_id_idx ON catalog.thesis (enrollment_id);

-- Replaces advisor1_id / advisor2_id: one row per advisor, in order.
CREATE TABLE catalog.thesis_advisor (
    thesis_id     integer REFERENCES catalog.thesis ON DELETE CASCADE,
    advisor_order smallint CHECK (advisor_order IN (1, 2)),
    professor_id  integer NOT NULL REFERENCES catalog.professor,
    PRIMARY KEY (thesis_id, advisor_order),
    UNIQUE (thesis_id, professor_id)
);
CREATE INDEX thesis_advisor_professor_id_idx ON catalog.thesis_advisor (professor_id);

-- ---------------------------------------------------------------------------
-- catalog: research products
-- ---------------------------------------------------------------------------

-- Journals and conferences, one row per venue regardless of spelling.
CREATE TABLE catalog.venue (
    id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (name = catalog.clean_text(name) AND name <> '')
);
CREATE UNIQUE INDEX venue_match_key_key ON catalog.venue (catalog.match_key(name));

-- One row per publication. A paper co-authored by several CIC professors was
-- one row per author in the scrape; here it is one product with many authors.
CREATE TABLE catalog.research_product (
    id       integer PRIMARY KEY,  -- the lowest raw.research_products.id merged into it
    title    text NOT NULL CHECK (title = catalog.clean_text(title) AND title <> ''),
    year     smallint NOT NULL CHECK (year BETWEEN 1900 AND 2100),
    venue_id integer NOT NULL REFERENCES catalog.venue
);
CREATE UNIQUE INDEX research_product_title_year_key
    ON catalog.research_product (catalog.match_key(title), year);
CREATE INDEX research_product_venue_id_idx ON catalog.research_product (venue_id);

CREATE TABLE catalog.product_author (
    product_id   integer REFERENCES catalog.research_product ON DELETE CASCADE,
    professor_id integer REFERENCES catalog.professor,
    PRIMARY KEY (product_id, professor_id)
);
CREATE INDEX product_author_professor_id_idx ON catalog.product_author (professor_id);

-- Which raw rows were merged into each product.
CREATE TABLE catalog.product_source (
    raw_id     integer PRIMARY KEY,
    product_id integer NOT NULL REFERENCES catalog.research_product ON DELETE CASCADE
);
CREATE INDEX product_source_product_id_idx ON catalog.product_source (product_id);

-- ---------------------------------------------------------------------------
-- catalog: known data problems
-- ---------------------------------------------------------------------------

-- Problems the cleaning could not fix, one row per affected row and issue, so
-- the application can flag or down-weight them. See db/saber_scrape/README.md for codes.
CREATE TABLE catalog.data_issue (
    id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity     text NOT NULL,  -- catalog table the row belongs to
    entity_key text NOT NULL,  -- that row's primary key, as text (program keys are codes)
    code       text NOT NULL,
    detail     text,
    UNIQUE (entity, entity_key, code)
);
CREATE INDEX data_issue_code_idx ON catalog.data_issue (code);

-- Per-professor summary for ranking and display.
CREATE VIEW catalog.professor_activity AS
SELECT
    p.id AS professor_id,
    p.laboratory_id,
    count(DISTINCT pa.product_id) AS products,
    max(rp.year) AS last_product_year,
    (SELECT count(*) FROM catalog.thesis_advisor ta WHERE ta.professor_id = p.id) AS theses_advised,
    EXISTS (
        SELECT 1 FROM catalog.data_issue d
        WHERE d.entity = 'professor' AND d.entity_key = p.id::text AND d.code = 'products_truncated'
    ) AS products_truncated
FROM catalog.professor p
LEFT JOIN catalog.product_author pa ON pa.professor_id = p.id
LEFT JOIN catalog.research_product rp ON rp.id = pa.product_id
GROUP BY p.id;

-- ---------------------------------------------------------------------------
-- app: what students and professors do in the application
-- ---------------------------------------------------------------------------

CREATE TYPE app.user_role AS ENUM ('student', 'professor', 'admin');
CREATE TYPE app.idea_status AS ENUM ('draft', 'published', 'archived');
CREATE TYPE app.request_status AS ENUM ('pending', 'accepted', 'declined', 'withdrawn');

CREATE FUNCTION app.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

-- Passwords and sessions belong to the auth provider, not to this table.
CREATE TABLE app.app_user (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email        citext NOT NULL UNIQUE,
    display_name text NOT NULL CHECK (btrim(display_name) <> ''),
    role         app.user_role NOT NULL,
    -- Links a professor's account to their catalog record and its history.
    professor_id integer UNIQUE REFERENCES catalog.professor,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (professor_id IS NULL OR role = 'professor')
);

CREATE TABLE app.thesis_idea (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    author_id    bigint NOT NULL REFERENCES app.app_user ON DELETE CASCADE,
    title        text NOT NULL CHECK (length(btrim(title)) BETWEEN 5 AND 300),
    description  text NOT NULL DEFAULT '' CHECK (length(description) <= 10000),
    program_code text REFERENCES catalog.program,
    status       app.idea_status NOT NULL DEFAULT 'draft',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    -- Target of the composite foreign keys below, which guarantee that a
    -- request about an idea is always made by that idea's author.
    UNIQUE (id, author_id)
);
CREATE INDEX thesis_idea_author_id_idx ON app.thesis_idea (author_id);
CREATE TRIGGER thesis_idea_touch_updated_at
    BEFORE UPDATE ON app.thesis_idea
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

CREATE TABLE app.advisor_request (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idea_id      bigint NOT NULL,
    student_id   bigint NOT NULL,
    professor_id integer NOT NULL REFERENCES catalog.professor,
    message      text NOT NULL DEFAULT '' CHECK (length(message) <= 5000),
    status       app.request_status NOT NULL DEFAULT 'pending',
    created_at   timestamptz NOT NULL DEFAULT now(),
    closed_at    timestamptz,
    FOREIGN KEY (idea_id, student_id) REFERENCES app.thesis_idea (id, author_id) ON DELETE CASCADE,
    CHECK ((status = 'pending') = (closed_at IS NULL))
);
-- At most one open request per idea and professor; a declined one may be retried.
CREATE UNIQUE INDEX advisor_request_one_pending_key
    ON app.advisor_request (idea_id, professor_id) WHERE status = 'pending';
CREATE INDEX advisor_request_professor_status_idx ON app.advisor_request (professor_id, status);

-- Asking to join a lab, with or without a specific idea.
CREATE TABLE app.lab_request (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    student_id    bigint NOT NULL REFERENCES app.app_user ON DELETE CASCADE,
    idea_id       bigint,
    laboratory_id integer NOT NULL REFERENCES catalog.laboratory,
    message       text NOT NULL DEFAULT '' CHECK (length(message) <= 5000),
    status        app.request_status NOT NULL DEFAULT 'pending',
    created_at    timestamptz NOT NULL DEFAULT now(),
    closed_at     timestamptz,
    FOREIGN KEY (idea_id, student_id) REFERENCES app.thesis_idea (id, author_id)
        ON DELETE SET NULL (idea_id),
    CHECK ((status = 'pending') = (closed_at IS NULL))
);
CREATE UNIQUE INDEX lab_request_one_pending_key
    ON app.lab_request (student_id, laboratory_id) WHERE status = 'pending';
CREATE INDEX lab_request_laboratory_status_idx ON app.lab_request (laboratory_id, status);
