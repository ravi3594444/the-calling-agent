-- The menu as the kitchen actually prints it (PRD §14).
--
-- Stored in Postgres rather than on disk because the app container is
-- replaced on every deploy: a file written beside the process is gone by the
-- next release, and a menu photo that vanishes is worse than none. These are
-- a handful of rows per business, not a media library.
--
-- NOT parsed. There is no OCR here, and the dashboard copy says so: the photo
-- sits beside the dish form so the owner types from it. A parser that half
-- reads a menu puts food on the agent's lips that the kitchen never made.
CREATE TABLE IF NOT EXISTS menu_uploads (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id  uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    filename     text        NOT NULL DEFAULT '',
    content_type text        NOT NULL,
    bytes        bytea       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS menu_uploads_business
    ON menu_uploads (business_id, created_at DESC);
