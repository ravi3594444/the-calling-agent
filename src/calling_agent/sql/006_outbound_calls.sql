-- What an outbound call needs that a text did not (PRD §13).
--
-- attempts    A ring that goes unanswered is tried again, then falls back
--             to a text. A text is sent once; nobody re-sends an SMS.
-- send_after  Outside the calling window a call waits for it to open, and
--             an unanswered one waits a while before ringing again. A text
--             never waits.
-- direction   The agent now places calls as well as taking them, and the
--             call log has to say which was which.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS attempts   integer     NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS send_after timestamptz;
ALTER TABLE calls    ADD COLUMN IF NOT EXISTS direction  text        NOT NULL DEFAULT 'inbound'
    CHECK (direction IN ('inbound', 'outbound'));
