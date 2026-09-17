-- Tableline core schema (PRD §6).
--
-- Every tenant-owned row carries business_id. Every timestamp is timestamptz in
-- UTC; businesses.timezone renders, it never stores. Ids are uuid rather than
-- serial: in a multi-tenant product a guessable id is a cross-tenant read
-- waiting to happen, and these ids travel in URLs.

CREATE TABLE IF NOT EXISTS businesses (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          text        NOT NULL,
    name          text        NOT NULL,
    timezone      text        NOT NULL DEFAULT 'UTC',
    phone_number  text,                              -- E.164, the dialled number
    vertical      text        NOT NULL DEFAULT 'restaurant',
    status        text        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused', 'archived')),
    config        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    config_version integer    NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS businesses_slug_key ON businesses (slug);
-- Partial: several businesses may be mid-onboarding with no number yet, but a
-- dialled number must resolve to exactly one tenant or the call is ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS businesses_phone_key
    ON businesses (phone_number) WHERE phone_number IS NOT NULL;


CREATE TABLE IF NOT EXISTS capacity_rules (
    id           uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id  uuid    NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    weekday      integer NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- Monday = 0
    start_time   time    NOT NULL,
    end_time     time    NOT NULL,
    total_units  integer NOT NULL CHECK (total_units >= 0),
    slot_minutes integer NOT NULL DEFAULT 30 CHECK (slot_minutes > 0),
    turn_minutes integer NOT NULL DEFAULT 90 CHECK (turn_minutes > 0),
    label        text                                 -- "Lunch", "Dinner"
    -- end_time <= start_time means the service runs past midnight: a kitchen
    -- open 18:00-01:00 is a normal restaurant, and a CHECK forbidding it would
    -- make every late-night venue unrepresentable.
);
CREATE INDEX IF NOT EXISTS capacity_rules_business_weekday
    ON capacity_rules (business_id, weekday);


CREATE TABLE IF NOT EXISTS overrides (
    id          uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id uuid    NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    date        date    NOT NULL,
    type        text    NOT NULL CHECK (type IN ('closed', 'reduced', 'extended')),
    total_units integer CHECK (total_units >= 0),
    start_time  time,                                 -- 'extended' only
    end_time    time,
    reason      text    NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS overrides_business_date ON overrides (business_id, date);


-- THE CONTENTION POINT (PRD §7). Rows are created lazily on first hold and are
-- the only rows ever locked. slot_minutes is copied in rather than read from
-- capacity_rules so that changing a rule tomorrow cannot silently re-interpret
-- the units committed today.
CREATE TABLE IF NOT EXISTS slots (
    business_id     uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    slot_start      timestamptz NOT NULL,
    slot_minutes    integer     NOT NULL,
    capacity_total  integer     NOT NULL CHECK (capacity_total >= 0),
    committed_units integer     NOT NULL DEFAULT 0 CHECK (committed_units >= 0),
    PRIMARY KEY (business_id, slot_start)
);


CREATE TABLE IF NOT EXISTS customers (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id    uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    phone_e164     text        NOT NULL,
    name           text        NOT NULL DEFAULT '',
    visit_count    integer     NOT NULL DEFAULT 0,
    no_show_count  integer     NOT NULL DEFAULT 0,
    do_not_call    boolean     NOT NULL DEFAULT false,
    notes          text        NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS customers_business_phone
    ON customers (business_id, phone_e164);


CREATE TABLE IF NOT EXISTS bookings (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id  uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    customer_id  uuid        REFERENCES customers (id) ON DELETE SET NULL,
    reference    text        NOT NULL,
    start_time   timestamptz NOT NULL,
    end_time     timestamptz NOT NULL,
    party_size   integer     NOT NULL CHECK (party_size > 0),   -- units
    status       text        NOT NULL DEFAULT 'confirmed'
                 CHECK (status IN ('pending', 'confirmed', 'arrived',
                                   'no_show', 'cancelled', 'declined')),
    source       text        NOT NULL DEFAULT 'voice',
    name         text        NOT NULL DEFAULT '',
    phone        text        NOT NULL DEFAULT '',
    notes        text        NOT NULL DEFAULT '',
    -- The manage link is a credential; the 6-char reference is not (PRD §12).
    manage_token_hash text,
    manage_token_expires_at timestamptz,
    slot_starts  timestamptz[] NOT NULL DEFAULT '{}',  -- slots this booking holds
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (end_time > start_time)
);
CREATE UNIQUE INDEX IF NOT EXISTS bookings_business_reference
    ON bookings (business_id, reference);
CREATE INDEX IF NOT EXISTS bookings_business_start ON bookings (business_id, start_time);
CREATE INDEX IF NOT EXISTS bookings_business_status_start
    ON bookings (business_id, status, start_time);


CREATE TABLE IF NOT EXISTS holds (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id          uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    slot_start           timestamptz NOT NULL,
    slot_starts          timestamptz[] NOT NULL,
    units                integer     NOT NULL CHECK (units > 0),
    end_time             timestamptz NOT NULL,
    expires_at           timestamptz NOT NULL,
    released_at          timestamptz,
    converted_booking_id uuid        REFERENCES bookings (id) ON DELETE SET NULL,
    call_id              uuid,
    created_at           timestamptz NOT NULL DEFAULT now()
);
-- The sweeper's only query: live holds past their expiry.
CREATE INDEX IF NOT EXISTS holds_live_expiry ON holds (expires_at)
    WHERE released_at IS NULL AND converted_booking_id IS NULL;


CREATE TABLE IF NOT EXISTS calls (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id   uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    caller_phone  text        NOT NULL DEFAULT '',
    started_at    timestamptz NOT NULL DEFAULT now(),
    ended_at      timestamptz,
    duration_s    integer,
    outcome       text        NOT NULL DEFAULT 'in_progress',
    resolved      boolean     NOT NULL DEFAULT true,   -- false => "Where it fell short"
    transcript    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    recording_url text,
    booking_id    uuid        REFERENCES bookings (id) ON DELETE SET NULL,
    provider      text        NOT NULL DEFAULT 'browser',
    provider_call_id text
);
CREATE INDEX IF NOT EXISTS calls_business_started ON calls (business_id, started_at DESC);


CREATE TABLE IF NOT EXISTS messages (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    booking_id  uuid        REFERENCES bookings (id) ON DELETE SET NULL,
    customer_id uuid        REFERENCES customers (id) ON DELETE SET NULL,
    direction   text        NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    channel     text        NOT NULL,                 -- sms | voice | push | email
    to_address  text        NOT NULL DEFAULT '',
    body        text        NOT NULL DEFAULT '',
    status      text        NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'sent', 'delivered', 'failed', 'received')),
    error       text,
    provider_id text,
    kind        text        NOT NULL DEFAULT '',      -- confirmed | declined | reminder ...
    sent_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_business_created ON messages (business_id, created_at DESC);
CREATE INDEX IF NOT EXISTS messages_booking ON messages (booking_id);


CREATE TABLE IF NOT EXISTS outbound_tasks (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id   uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    booking_id    uuid        REFERENCES bookings (id) ON DELETE CASCADE,
    reason        text        NOT NULL,
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    scheduled_for timestamptz NOT NULL DEFAULT now(),
    attempts      integer     NOT NULL DEFAULT 0,
    status        text        NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    last_error    text,
    -- Idempotency: one task per (booking, reason) unless deliberately re-queued.
    dedupe_key    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outbound_tasks_due ON outbound_tasks (scheduled_for)
    WHERE status = 'queued';
CREATE UNIQUE INDEX IF NOT EXISTS outbound_tasks_dedupe
    ON outbound_tasks (business_id, dedupe_key) WHERE dedupe_key IS NOT NULL;


-- Menu is a per-vertical optional module (PRD §14). Priced in the business's
-- own currency; no currency is stored per row because a venue has one.
CREATE TABLE IF NOT EXISTS menu_items (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    name        text        NOT NULL,
    section     text        NOT NULL DEFAULT '',
    price       numeric(12, 2),
    description text        NOT NULL DEFAULT '',
    tags        text[]      NOT NULL DEFAULT '{}',    -- allergens, diet
    available   boolean     NOT NULL DEFAULT true,
    position    integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS menu_items_business ON menu_items (business_id, position);


-- "What changed" in Settings → Account. Every config write lands here.
CREATE TABLE IF NOT EXISTS config_changes (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    section     text        NOT NULL,
    summary     text        NOT NULL,
    actor       text        NOT NULL DEFAULT '',
    before      jsonb,
    after       jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS config_changes_business ON config_changes (business_id, created_at DESC);


-- The dashboard's long secret link (PRD §14: no login during service). Stored
-- hashed, so a database read does not hand over every tenant's dashboard.
CREATE TABLE IF NOT EXISTS dashboard_tokens (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id uuid        NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    token_hash  text        NOT NULL,
    label       text        NOT NULL DEFAULT '',
    expires_at  timestamptz,
    revoked_at  timestamptz,
    last_used_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS dashboard_tokens_hash ON dashboard_tokens (token_hash);


-- Optional resource layer (PRD §6, v1.5). Schema ready, feature off.
CREATE TABLE IF NOT EXISTS resources (
    id              uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id     uuid    NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    label           text    NOT NULL,
    min_capacity    integer NOT NULL DEFAULT 1,
    max_capacity    integer NOT NULL DEFAULT 1,
    combinable_with uuid[]  NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS resource_assignments (
    booking_id  uuid        NOT NULL REFERENCES bookings (id) ON DELETE CASCADE,
    resource_id uuid        NOT NULL REFERENCES resources (id) ON DELETE CASCADE,
    start_time  timestamptz NOT NULL,
    end_time    timestamptz NOT NULL,
    PRIMARY KEY (booking_id, resource_id)
);
