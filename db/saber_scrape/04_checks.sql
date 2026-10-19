-- Invariants between raw and catalog. Any failed ASSERT aborts the build, and
-- because build.sh runs the scripts in one transaction, nothing is left behind.
DO $$
DECLARE
    n bigint;
BEGIN
    -- Nothing lost.
    ASSERT (SELECT count(*) FROM catalog.thesis) = (SELECT count(*) FROM raw.theses),
        'every raw thesis is in catalog.thesis';
    ASSERT (SELECT count(*) FROM catalog.product_source) = (SELECT count(*) FROM raw.research_products),
        'every raw product row maps to a catalog product';
    ASSERT (SELECT count(*) FROM catalog.student) = (SELECT count(*) FROM raw.students),
        'every raw student is in catalog.student';
    ASSERT (SELECT count(*) FROM catalog.professor) = (SELECT count(*) FROM raw.professors),
        'every raw professor is in catalog.professor';
    ASSERT (SELECT count(*) FROM catalog.enrollment)
         = (SELECT count(*) FROM raw.academic_programs WHERE program <> 'Indefinido'),
        'only the Indefinido enrollment is dropped';

    -- Nothing invented: every author link comes from a raw row.
    ASSERT (SELECT count(*) FROM catalog.product_author)
         = (SELECT count(DISTINCT (s.product_id, r.professor_id))
            FROM catalog.product_source s JOIN raw.research_products r ON r.id = s.raw_id),
        'product authors come only from raw rows';

    -- Advisors carried over one for one.
    ASSERT (SELECT count(*) FROM catalog.thesis_advisor)
         = (SELECT count(*) + count(advisor2_id) FROM raw.theses),
        'every raw advisor link is in catalog.thesis_advisor';
    ASSERT NOT EXISTS (
        SELECT 1 FROM catalog.thesis t
        WHERE NOT EXISTS (SELECT 1 FROM catalog.thesis_advisor a
                          WHERE a.thesis_id = t.id AND a.advisor_order = 1)),
        'every thesis has a first advisor';

    -- The placeholder lab is gone everywhere.
    ASSERT NOT EXISTS (SELECT 1 FROM catalog.laboratory WHERE id = 999),
        'lab 999 is not a lab';
    ASSERT (SELECT count(*) FROM catalog.student_laboratory)
         = (SELECT count(*) FROM raw.student_laboratories WHERE laboratory_id <> 999),
        'real lab memberships are kept';

    -- Trusted URLs really are each thesis's own and are never shared.
    ASSERT NOT EXISTS (
        SELECT url FROM catalog.thesis WHERE url IS NOT NULL GROUP BY url HAVING count(*) > 1),
        'no trusted URL is shared by two theses';

    -- Every product is unique by title and year (also enforced by an index).
    SELECT count(*) - count(DISTINCT (catalog.match_key(title), year)) INTO n
    FROM catalog.research_product;
    ASSERT n = 0, 'products are unique by title and year';

    RAISE NOTICE 'checks passed';
END
$$;
