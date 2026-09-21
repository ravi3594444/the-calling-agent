-- An owner who can get back in without the link.
--
-- The dashboard link (PRD §14) stays exactly what it was: a bearer secret, no
-- login during service, because a host mid-rush should not type a password on
-- a shared tablet. What it could never do is survive a cleared browser -- the
-- token lives in localStorage and only its hash is stored, so a lost link was
-- a locked-out venue and an operator ticket to mint a new one.
--
-- An account is the way back. It does not replace the link and does not gate
-- the dashboard; signing in simply mints an ordinary dashboard token and sets
-- it as a cookie, so everything downstream of `current_business` is unchanged.

CREATE TABLE IF NOT EXISTS owners (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id   uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    email         text        NOT NULL,
    -- scrypt, salted per row and peppered from the environment. Never a fast
    -- hash: `dashboard_tokens` may use one because 32 random bytes cannot be
    -- guessed, and a password someone chose can.
    password_hash text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- Case-insensitively unique across the whole product: an email is how you say
-- which venue you are signing in to, so two venues sharing one would make the
-- answer ambiguous. One person running two venues uses two addresses until
-- there are real accounts with roles (§14, "team and roles").
CREATE UNIQUE INDEX IF NOT EXISTS owners_email_key ON owners (lower(email));
CREATE INDEX IF NOT EXISTS owners_business_idx ON owners (business_id);
