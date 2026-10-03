-- Runs once, on first initialization of an empty data volume.
-- pgvector is what makes the vector(768) columns and HNSW indexes possible.
CREATE EXTENSION IF NOT EXISTS vector;
