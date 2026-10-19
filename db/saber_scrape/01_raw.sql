-- Moves the restored saber_sip tables out of public into raw.
-- raw is the untouched copy of the scrape: every later step reads from it and
-- nothing writes to it, so the cleaned data can always be rebuilt and compared.
CREATE SCHEMA raw;

ALTER TABLE public.academic_programs SET SCHEMA raw;
ALTER TABLE public.laboratories SET SCHEMA raw;
ALTER TABLE public.professors SET SCHEMA raw;
ALTER TABLE public.research_products SET SCHEMA raw;
ALTER TABLE public.student_laboratories SET SCHEMA raw;
ALTER TABLE public.students SET SCHEMA raw;
ALTER TABLE public.theses SET SCHEMA raw;

-- This database carries no embeddings, so models can be compared from a clean
-- slate. Dropping the columns also drops their HNSW indexes.
ALTER TABLE raw.research_products DROP COLUMN embedding;
ALTER TABLE raw.theses DROP COLUMN embedding;
DROP EXTENSION vector;

COMMENT ON SCHEMA raw IS 'Verbatim copy of data/saber_sip.sql, minus embeddings. Read-only input for catalog.';
