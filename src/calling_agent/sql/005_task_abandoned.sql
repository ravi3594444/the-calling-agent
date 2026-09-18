-- A terminal state for a task that has spent its attempts.
--
-- 'failed' means the last try failed and another is coming. Without a name
-- for "failed and we have stopped", the two are told apart only by comparing
-- attempts against a setting -- which is exactly the arithmetic nobody does
-- at the moment they are asking why a guest was never texted.
ALTER TABLE outbound_tasks DROP CONSTRAINT IF EXISTS outbound_tasks_status_check;
ALTER TABLE outbound_tasks ADD CONSTRAINT outbound_tasks_status_check
    CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'abandoned'));
