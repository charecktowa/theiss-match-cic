# saber_scrape

A cleaned, application-ready copy of the `saber_sip` scrape, built into its own
database next to the original `theiss`, which it never modifies. The former `v2` name referred to the second-generation clean copy. Its source is the one-time SABER export.

```bash
db/saber_scrape/build.sh          # builds saber_scrape (drops and rebuilds it if it exists)
TARGET_DB=scratch db/saber_scrape/build.sh  # same, under another name
```

`TARGET_DB` is dropped first, so the build refuses the databases the stack
relies on: `theiss` (or `POSTGRES_DB`), `postgres`, the templates and `cic_scrape`.

The build restores `data/saber_sip.sql`, then runs the four scripts here in a
single transaction. `04_checks.sql` asserts that nothing was lost or invented;
if any check fails, the transaction rolls back and the database is left holding
only the restored dump.

## Schemas

| Schema | Holds | Written by |
| --- | --- | --- |
| `raw` | The dump as scraped, minus embeddings | the build only |
| `catalog` | Labs, professors, students, programs, theses, products, cleaned | the build only |
| `app` | Accounts, thesis ideas, advisor and lab requests | the application |

**Student records.** Unlike `cic_scrape`, which leaves students out, this
database keeps the export's student names and emails in `catalog.student`: they
were already in the one-time SABER export, and theses link to them through
enrollments. Keep the database local, like the dump it comes from.

**No embeddings.** There are no vector columns and the `vector` extension is not
installed, so any embedding model can be compared from a clean slate. The
original `theiss` database still holds the earlier 768-d vectors, reported to
come from a Qwen3 embedding model.

## What changed from the scrape

| Scrape | `catalog` |
| --- | --- |
| Lab 999, "Laboratorio Egresados", held 98% of students | Removed; graduation is `enrollment.status` |
| `status` free text in Spanish | `enrollment_status` enum: `enrolled`, `finished_coursework`, `graduated`, `withdrawn` |
| `program` free text, including `Indefinido` | `program` lookup with degree level; the one `Indefinido` row is dropped |
| `advisor1_id`, `advisor2_id` | `thesis_advisor`, one row per advisor with `advisor_order` |
| One product row per CIC co-author (1,835 rows) | One `research_product` per publication (1,464) with `product_author` |
| `site` free text, with spelling variants | `venue` table, unique regardless of case, accents and spacing |
| Thesis URLs copied from other theses | `url` keeps only links whose ID is the thesis's own; `source_url` keeps the original |
| Double spaces, empty strings | Titles trimmed and collapsed, enforced by `CHECK`; `''` stored as `NULL` |

`catalog.product_source` maps every raw product row to the product it was merged
into, so nothing about the merge is lost.

## Known data problems: `catalog.data_issue`

What cleaning cannot fix is recorded here, one row per affected record, so the
application can flag or down-weight it rather than trust it.

| `entity` | `code` | Meaning |
| --- | --- | --- |
| `professor` | `products_truncated` | The scrape kept only 40 products, mostly older ones: recent work is missing |
| `thesis` | `url_points_elsewhere` | The scraped link belongs to another thesis; `url` is `NULL` |
| `thesis` | `url_missing` | No link was scraped |
| `thesis` | `enrollment_withdrawn` | Thesis attached to a withdrawn enrollment |
| `enrollment` | `graduated_without_thesis` | Graduated, but the thesis was not scraped |
| `enrollment` | `multiple_theses` | More than one thesis for one enrollment |
| `student` | `enrolled_in_two_programs` | Enrolled in a master's and the doctorate at once |
| `student` | `undefined_program_dropped` | Had an `Indefinido` enrollment, not loaded |
| `laboratory` | `name_truncated` | Name cut off by the scraper |
| `program` | `name_unknown` | Official program name not confirmed yet |

`catalog.professor_activity` summarizes each professor (products, last product
year, theses advised, `products_truncated`) for ranking.

## Rules the `app` schema enforces

- A request about an idea can only come from that idea's author (composite
  foreign key on `thesis_idea (id, author_id)`).
- One pending request per idea and professor, and per student and lab; a
  declined request can be sent again.
- `closed_at` is set exactly when a request leaves `pending`.
- Only a `professor` account can be linked to a `catalog.professor`.
- Emails are unique regardless of case (`citext`).
- Deleting an idea deletes its advisor requests but keeps lab requests,
  with `idea_id` set to `NULL`.

Passwords and sessions are left to an auth provider; `app_user` holds no credentials.

## Adding embeddings later

Install the extension and add one table per embedded entity, keyed by model, so
several models can live side by side while you compare them:

```sql
CREATE EXTENSION vector;
-- e.g. catalog.thesis_embedding (thesis_id, model_id, embedding vector(<dims>))
```

Store the model name, its dimensions, the exact input text (title only, or title
plus abstract), and any query instruction it needs. A student's idea has to be
embedded the same way as the documents it is compared against.
