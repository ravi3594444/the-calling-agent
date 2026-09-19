# Tableline — how it is built

The repo answers the phone for **many** businesses now, not one restaurant. This
document is the map: what each module is for, where the tenant is decided, and
what to check when something is wrong.

The product spec is the PRD; section numbers below point back to it.

---

## The shape of it

```
caller ──phone──▶ Twilio ──media stream──▶  relay  ──▶ AssemblyAI Voice Agent API
                                              │
                                     tool calls (§8)
                                              ▼
                       availability engine ──▶ Postgres ◀── dashboard API (§16b)
                                              │                      ▲
                                              ▼                      │
                                    jobs: sweep, send, remind    staff browser
```

One process serves four surfaces:

| Surface | Path | What it is |
|---|---|---|
| Browser agent | `/ws` | A microphone bridged to an agent session |
| Phone agent | `/twilio/*` | A call bridged to the same agent session |
| Dashboard | `/api/*` | Everything the staff screen reads and writes |
| Tool contract | `/api/tools/*` | The agent's own tools, for other systems |

---

## Where the tenant is decided

This is the one thing to get right (PRD §20). A tenant is resolved **once, at
the edge**, and passed down. Nothing below the edge infers a business from
anything a caller said, because a caller can say anything.

| Surface | What identifies the business |
|---|---|
| Phone call | the **dialled number** (`To` on the Twilio webhook) |
| Media stream | a **signed ticket** minted when the call was answered |
| Dashboard | a **hashed dashboard token** from the link |
| Tool API | an explicit `X-Business` header |
| Browser | `?business=<slug>`, else `DEFAULT_BUSINESS_SLUG` |

`businesses.resolve()` raises rather than guessing. On a shared line, guessing
hands one venue's book to another venue's caller.

---

## Modules

| File | What it owns |
|---|---|
| `availability.py` | **The one function the product rests on.** `is_available()`, alternatives, the day and month views. Reads only. |
| `holds.py` | Taking capacity, confirming, releasing, sweeping. |
| `slots.py` | The lock. Ascending `slot_start`, one statement per row. |
| `bookings.py` | The lifecycle state machine and the overflow queue. |
| `records.py` | The two inserts both booking paths share. |
| `businesses.py` | Tenancy, config cache, capacity rules, overrides. |
| `business_config.py` | The config document, its JSON Schema, and the vertical merge. |
| `agent_tools.py` | The §8 tool contract, bound to one business. |
| `agent_config.py` | The prompt template and the session payloads. |
| `notifications.py` | Queueing messages and outbound tasks. Never sends inline. |
| `jobs.py` | The sweeper, the sender, reminders, digests. |
| `call_log.py` | The transcript recorder handed to the relay. |
| `dates.py` | The boundary over dateparser / parsedatetime / phonenumbers / langcodes. |
| `holidays.py` | Real per-year public holidays, lunar calendars included. |
| `session.py` | The relay. Unchanged, and still the hardest code here. |

---

## The hold, in one paragraph

The agent calls `hold` the moment a caller states a time — **before** asking for
a name. `holds.take()` asks `is_available()` once outside the lock to decide
whether to bother, then locks every slot the turn covers, in ascending
`slot_start` order, and asks again. Only the second answer counts. `confirm()`
re-reads the hold's expiry from the database clock and refuses a lapsed one:
between the caller saying yes and that line running, the sweeper may have taken
the capacity back, and telling them to come anyway is the one outcome worse
than saying no.

`tests/test_hold_concurrency.py` was written before the hold code, per PRD §7,
and then run against five deliberately broken implementations to check it
notices. Two bugs survived the first version — first-slot-only headroom, and a
`confirm` that skipped its expiry re-check — so deterministic tests were added
for both. If you change `slots.py` or `holds.py`, break them on purpose and
confirm the suite goes red.

---

## Configuration, and the rule behind it

Everything a business can differ in is one JSONB document, validated against a
JSON Schema on save and read fresh per call. Three layers merge:

```
DEFAULTS  <-  data/verticals.json[vertical]  <-  businesses.config
```

**There is no `if business_id == N` anywhere, and a test asserts it**
(`test_no_business_id_is_ever_special_cased`). When a venue needs behaviour the
config cannot express, add a field and every venue gets it. Special-casing one
customer destroys the property that makes onboarding a ten-minute form.

Adding a vertical — dentists, garages, salons — is an entry in
`data/verticals.json`. Nothing in the engine knows what a restaurant is.

---

## Running it

```bash
make install
cp .env.example .env            # then set ASSEMBLYAI_API_KEY and TOKEN_PEPPER
docker compose up -d db         # or point DATABASE_URL at your own Postgres
make migrate
make demo                       # prints a dashboard link and a browser link
make dev
```

`make demo` creates a venue, opening hours and a dashboard token, and prints:

```
dashboard: http://localhost:8080/dashboard?token=<secret>
browser agent: http://localhost:8080/?business=demo
```

### Tests

```bash
TEST_DATABASE_URL=postgresql+psycopg://tableline@127.0.0.1:5432/tableline_test make test
```

The suite needs a real Postgres: the hold transaction is defined by row locks
and a test double of a lock tests the double. With `TEST_DATABASE_URL` unset it
skips; with it set and unreachable it **fails**, so a green run never means
"nothing ran".

---

## Putting it on a phone number

1. Buy a Twilio number.
2. Point its **A call comes in** webhook at `POST https://<host>/twilio/voice`,
   and its status callback at `POST https://<host>/twilio/status`.
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TOKEN_PEPPER` and
   `PUBLIC_HOSTNAME`.
4. Attach the number to the business:
   `python -m calling_agent.cli onboard --phone +9118005551234 ...`
   (or set it later — the dialled number is the tenant key).
5. On the venue's own line, set conditional forwarding so it rings here when
   busy or unanswered. The venue keeps its number.

Audio is G.711 μ-law at 8 kHz in both directions (`audio/pcmu`), which is what
Twilio sends and what the Voice Agent API accepts, so **nothing is transcoded**.
Leaving the output encoding at the default 24 kHz PCM is the classic failure:
the agent talks happily and the caller hears silence.

### What to check on the first real call

- `/healthz` — is the database reachable and migrated?
- `/diagnose` — walks the real upstream path and names the step that fails.
- The **Calls** screen — every call, with tool calls inline, and a separate
  "Where it fell short" list.
- `SMS_PROVIDER=log` until you want real texts. It records the message and
  sends nothing, so a first run cannot text a stranger.

---

## Known limits

- Table-level assignment is schema-only (`resources`, `resource_assignments`).
  The counter is the product; the exclusion constraint is ready when it is not.
- Billing, staff accounts and invoices are not built. The dashboard shows what
  the server can count and nothing it cannot.
- `jobs.py` runs in the web process by default. Correct for one server; set
  `RUN_JOBS_IN_PROCESS=false` and run `python -m calling_agent.jobs` when there
  is more than one.
- Outbound calling is queued (`outbound_tasks`) but the dialler is not wired —
  declines fall back to SMS.
