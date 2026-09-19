-- Overlap exclusion for the optional resource layer (PRD §6). Separate from
-- 001 because it needs an extension, and a deployment that cannot install
-- extensions should still get the whole counter-based product.
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE resource_assignments
    DROP CONSTRAINT IF EXISTS resource_assignments_no_overlap;
ALTER TABLE resource_assignments
    ADD CONSTRAINT resource_assignments_no_overlap
    EXCLUDE USING gist (
        resource_id WITH =,
        tstzrange(start_time, end_time) WITH &&
    );
