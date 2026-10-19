-- Fills catalog from raw. Each fix from the data quality review is applied
-- here; what cannot be fixed is recorded in catalog.data_issue at the end.

-- ---------------------------------------------------------------------------
-- People, labs and programs
-- ---------------------------------------------------------------------------

-- Lab 999 is a placeholder for graduates, not a lab.
INSERT INTO catalog.laboratory (id, name)
SELECT id, catalog.clean_text(name) FROM raw.laboratories WHERE id <> 999;

INSERT INTO catalog.professor (id, laboratory_id, name, email, profile_url)
SELECT id, laboratory_id, catalog.clean_text(name),
       nullif(btrim(email), ''), nullif(btrim(profile_url), '')
FROM raw.professors;

INSERT INTO catalog.student (id, name, email, profile_url)
SELECT id, catalog.clean_text(name),
       nullif(btrim(email), ''), nullif(btrim(profile_url), '')
FROM raw.students;

INSERT INTO catalog.student_laboratory (student_id, laboratory_id)
SELECT student_id, laboratory_id FROM raw.student_laboratories WHERE laboratory_id <> 999;

INSERT INTO catalog.program (code, name, level) VALUES
    ('DCC', 'Doctorado en Ciencias de la Computación', 'doctorate'),
    ('MCC', 'Maestría en Ciencias de la Computación', 'masters'),
    ('MCIC', 'Maestría en Ciencias en Ingeniería de Cómputo', 'masters'),
    ('DCTIAyCD', NULL, 'doctorate'),
    ('MCTIAyCD', NULL, 'masters');

-- The CASE has no ELSE on purpose: an unknown status becomes NULL and fails
-- the NOT NULL constraint instead of being loaded silently.
-- 'Indefinido' is not a program; its single row (withdrawn, no thesis) is dropped.
INSERT INTO catalog.enrollment (id, student_id, program_code, status)
SELECT id, student_id, program,
       CASE status
           WHEN 'inscrito' THEN 'enrolled'
           WHEN 'egresado' THEN 'finished_coursework'
           WHEN 'graduado' THEN 'graduated'
           WHEN 'baja' THEN 'withdrawn'
       END::catalog.enrollment_status
FROM raw.academic_programs
WHERE program <> 'Indefinido';

-- ---------------------------------------------------------------------------
-- Theses
-- ---------------------------------------------------------------------------

-- A thesis URL ends in the SABER thesis ID. Only a URL whose ID is this
-- thesis's own is trusted; the others were copied from another thesis.
-- btrim without a character set removes spaces only, so tabs and newlines are
-- named explicitly: a stray one would otherwise fail the ID match and load a
-- null url.
WITH trimmed AS (
    SELECT id, academic_program_id, title, btrim(url, E' \t\n\r') AS url
    FROM raw.theses
)
INSERT INTO catalog.thesis (id, enrollment_id, title, url, source_url)
SELECT id, academic_program_id, catalog.clean_text(title),
       CASE WHEN substring(url FROM '/([0-9]+)$')::integer = id THEN url END,
       nullif(url, '')
FROM trimmed;

INSERT INTO catalog.thesis_advisor (thesis_id, advisor_order, professor_id)
SELECT id, 1, advisor1_id FROM raw.theses
UNION ALL
SELECT id, 2, advisor2_id FROM raw.theses WHERE advisor2_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Venues and research products
-- ---------------------------------------------------------------------------

-- Spellings that match_key cannot unify on its own.
CREATE TEMP TABLE venue_alias (from_key text PRIMARY KEY, to_key text NOT NULL);
INSERT INTO venue_alias VALUES
    ('research on computing science', 'research in computing science');

CREATE TEMP TABLE product_clean AS
SELECT r.id AS raw_id,
       r.professor_id,
       catalog.clean_text(r.title) AS title,
       r.year,
       catalog.clean_text(r.site) AS site,
       coalesce(a.to_key, catalog.match_key(r.site)) AS venue_key,
       min(r.id) OVER (PARTITION BY catalog.match_key(r.title), r.year) AS product_id
FROM raw.research_products r
LEFT JOIN venue_alias a ON a.from_key = catalog.match_key(r.site);

-- Each venue is named after its most common spelling.
INSERT INTO catalog.venue (name)
SELECT DISTINCT ON (venue_key) site
FROM (SELECT venue_key, site, count(*) AS n FROM product_clean GROUP BY 1, 2) s
ORDER BY venue_key, n DESC, site;

-- The row with the lowest raw ID represents each merged product. The LEFT JOIN
-- makes a venue that failed to match hit NOT NULL rather than drop the row.
INSERT INTO catalog.research_product (id, title, year, venue_id)
SELECT p.raw_id, p.title, p.year, v.id
FROM product_clean p
LEFT JOIN catalog.venue v ON catalog.match_key(v.name) = p.venue_key
WHERE p.raw_id = p.product_id;

INSERT INTO catalog.product_source (raw_id, product_id)
SELECT raw_id, product_id FROM product_clean;

INSERT INTO catalog.product_author (product_id, professor_id)
SELECT DISTINCT product_id, professor_id FROM product_clean;

-- ---------------------------------------------------------------------------
-- Known data problems
-- ---------------------------------------------------------------------------

INSERT INTO catalog.data_issue (entity, entity_key, code, detail)
-- A name ending in a connector word was cut off by the scraper.
SELECT 'laboratory', id::text, 'name_truncated', name
FROM catalog.laboratory WHERE name ~* '\s(y|e|de|del|la|en)$'

UNION ALL
SELECT 'program', code, 'name_unknown', NULL
FROM catalog.program WHERE name IS NULL

UNION ALL
-- The scrape keeps at most 40 products per professor, and keeps the oldest:
-- their recent work is missing, so activity looks older than it is.
SELECT 'professor', professor_id::text, 'products_truncated', 'scrape stopped at 40 products'
FROM raw.research_products GROUP BY professor_id HAVING count(*) = 40

UNION ALL
SELECT 'thesis', id::text, 'url_points_elsewhere', source_url
FROM catalog.thesis WHERE source_url IS NOT NULL AND url IS NULL

UNION ALL
SELECT 'thesis', id::text, 'url_missing', NULL
FROM catalog.thesis WHERE source_url IS NULL

UNION ALL
SELECT 'thesis', t.id::text, 'enrollment_withdrawn', NULL
FROM catalog.thesis t JOIN catalog.enrollment e ON e.id = t.enrollment_id
WHERE e.status = 'withdrawn'

UNION ALL
SELECT 'enrollment', enrollment_id::text, 'multiple_theses', count(*)::text
FROM catalog.thesis GROUP BY enrollment_id HAVING count(*) > 1

UNION ALL
-- Incomplete thesis coverage, not necessarily a wrong status.
SELECT 'enrollment', e.id::text, 'graduated_without_thesis', NULL
FROM catalog.enrollment e
WHERE e.status = 'graduated'
  AND NOT EXISTS (SELECT 1 FROM catalog.thesis t WHERE t.enrollment_id = e.id)

UNION ALL
SELECT 'student', student_id::text, 'enrolled_in_two_programs',
       string_agg(program_code, ', ' ORDER BY program_code)
FROM catalog.enrollment WHERE status = 'enrolled'
GROUP BY student_id HAVING count(*) > 1

UNION ALL
SELECT 'student', student_id::text, 'undefined_program_dropped', status
FROM raw.academic_programs WHERE program = 'Indefinido';
