-- One message per task, however many times that task runs (PRD §12).
--
-- outbound_tasks already had this; messages did not, which is why a task
-- could never safely be retried: the retry would text the guest a second
-- time. Without retries, a task interrupted by a deploy stayed 'running'
-- forever and its guest heard nothing at all -- the silence §11 forbids.
--
-- Partial, so the column stays NULL for everything that SHOULD be allowed to
-- repeat: a resent confirmation is a second text on purpose.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS dedupe_key text;
CREATE UNIQUE INDEX IF NOT EXISTS messages_dedupe
    ON messages (business_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
