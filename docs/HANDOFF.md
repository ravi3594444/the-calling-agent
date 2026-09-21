# Tableline — handoff

Where the product stands, how the pieces fit, what to build next, and the
handful of decisions that look arbitrary and are not.

Read §4 before changing anything. Everything in it was paid for once.

---

## 1. What is done

Working end to end, proven on live calls unless noted.

| Area | State |
|---|---|
| **Availability engine + hold transaction** (PRD §7) | Done. Concurrency-tested and mutation-tested: eight callers, room for six, exactly six win. |
| **Agent tools** (§8) — all ten | Done. A full booking (`now` → `hold` → `confirm`) has run on a live call. |
| **Booking lifecycle** (§9) | Explicit state machine: pending → confirmed → arrived / no_show / cancelled. |
| **Policy** (§10) — default-yes, escalation | Config-driven, per business. |
| **Overflow queue** (§11) | Request, accept/decline, timeout auto-decline, and promotion of the oldest waitlisted booking when a cancellation frees a seat. |
| **Customer SMS** (§12) | Confirmation, decline, reminder, cancellation. Real texts via Twilio. |
| **Staff digest + arrival nudges** (§12) | Texted to one staff phone, each behind its own switch in Settings → Messages. |
| **Inbound SMS** (§12) | `reply C` genuinely cancels. `/twilio/sms`, signature-verified. |
| **Outbound dialler** (§13) | Queued voice messages ring the guest; unanswered twice → the same words go as a text. **Not yet exercised against a real Twilio call.** |
| **Dashboard** (§14) | Six screens, 39 API endpoints, every button wired. Sound, unseen markers, wake lock. Polished UI, logo reveal. |
| **Menu reader** | Photo or PDF → proposed dishes → owner corrects → saved. Gemini 3.7 Flash. |
| **Self-serve onboarding** (§15) | `/start` creates the tenant, the week, an owner account and the first dashboard link, no terminal. `/start/ready` shows that link once and says to keep it. Off unless `SIGNUP_ENABLED`. |
| **Owner accounts** | `/login` mints an ordinary dashboard token into the cookie `current_business` already reads, so a cleared browser is no longer a locked-out venue. scrypt, salted and peppered. No login during service: the saved link still works (§14). Reset is `cli owner` until something can send mail. |
| **Multi-tenancy** (§15) | Config-driven. No `if business_id ==` anywhere. Proven by onboarding a UK dental practice on a fresh database with zero code changes. |
| **Locale, booking window, optional capacity, channel choice** (§15b–e) | Done. |
| **Venue facts** | Cuisine, getting there, parking, wheelchair access, children, dress code, private room. The agent answers from them. |

**Tests:** 223 backend, 47 frontend, ruff clean. Backend needs a Postgres —
see `TEST_DATABASE_URL` in `tests/conftest.py`.

---

## 2. How it works now

### One call, start to finish

```
Twilio rings  →  POST /twilio/voice
                 ├── resolves the business from the DIALLED number
                 ├── opens a `calls` row
                 └── returns TwiML: <Connect><Stream url="…?ticket=…">
                                                          │
                     a signed, 120-second ticket naming the business + call
                                                          │
              WS /twilio/stream  →  AgentSession  ←→  AssemblyAI Voice Agent
                                         │
                                    tool.call
                                         │
                            agent_tools.run_tool_for(business, …)
                                         │
                          availability → holds → bookings → Postgres
```

The tenant is decided **once, at the edge**, from the dialled number. Nothing
below that line can be talked into another venue's book, because the model
never sees the mapping.

### The three boundaries

Each wraps one external system, and nothing else imports that vendor. All
three share the same registry shape — a decorator, a dict, an env var picking
the entry:

- `notifications.PROVIDERS` — SMS (`SMS_PROVIDER=twilio|log`)
- `dialler.PROVIDERS` — placing calls (`VOICE_PROVIDER=twilio|log`)
- `menu_reader.PROVIDERS` — reading a menu photo (`MENU_READER=gemini|anthropic|openai`)

`log` is the default for the first two. A fresh checkout with real keys in the
env must not text or ring a stranger on its first tick.

`dates.py` is the same idea over the date/phone parsing libraries.

### Work that happens later

`jobs.tick()` runs every 60 s (`SWEEP_INTERVAL_SECONDS`) and does three things:

1. **Sweeps expired holds** — returns their capacity. One transaction, so a
   crash rolls back and the next pass redoes it.
2. **Runs due tasks** — reminders, overflow timeouts, digests, nudges.
3. **Sends queued messages** — texts via the SMS provider, `channel=voice`
   rows via the dialler.

A claimed task that dies mid-flight (a deploy, an OOM) is reclaimed after
`TASK_LEASE_SECONDS`; a failed one retries up to `TASK_MAX_ATTEMPTS` and then
becomes `abandoned`. Retrying is only safe because messages carry a dedupe key
keyed on the **task** — see §4.

### Configuration

One `businesses.config` JSONB per tenant, validated against a JSON Schema on
save, read fresh on every call. Twelve sections:

`identity, venue, locale, voice, capacity, policy, booking_window, messaging,
telling_guest, outbound, agent, features`

Merged three ways: `DEFAULTS` ← vertical profile (`data/verticals.json`) ←
this business. Adding a field means adding it to `DEFAULTS` and `SCHEMA` in
`business_config.py`; nothing else needs to know.

### Migrations

`src/calling_agent/sql/00*.sql`, applied in order by
`python -m calling_agent.cli migrate`, each in its own transaction and
recorded by name. **Never edit an applied migration** — add a new one.

Currently 001 → 007.

---

## 3. What to do next

In the order I would do it.

1. **Ring a real phone.** The dialler's routing, window-parking, retry and
   SMS fallback are all tested, but no Twilio call has actually been placed.
   Set `VOICE_PROVIDER=twilio` and decline a waitlist request with **Call**.
   This is the largest untested surface in the product.

2. **Call recording** (§14). Half built: `/twilio/status` stores a
   `RecordingUrl` when one arrives, but the TwiML never asks Twilio to record,
   so none ever does — and the Calls screen's audio player has nothing to
   play. Add `record="record-from-answer"` to the `<Connect>` and check the
   consent rules for the jurisdiction first.

3. **"Things people ask"** — a per-venue Q&A the agent answers from
   ("do you do gift vouchers?"). The cheapest real win left: one config field
   plus a prompt block, the same shape as the `venue` section. It removes the
   five most common "let me check and call you back" moments.

4. **Team and roles.** One dashboard token currently means full access. The
   mockup has host vs owner ("hosts can seat people; only you can change
   settings").

5. **Billing and usage.** Nothing exists. The mockup has plan, next charge,
   and counters for calls answered / texts sent / bookings taken.

6. ~~**Self-serve onboarding.**~~ Done: `/start` is the form. Off by default
   (`SIGNUP_ENABLED`), optional invite code (`SIGNUP_CODE`). What remains is a
   real rate limiter at the edge -- the one in `signup.py` is per process and
   is a floor, not a ceiling.

7. **Amend the PRD.** §16 still specifies Celery + Redis; this uses a plain
   loop over Postgres with `SKIP LOCKED` (fewer moving parts, and it satisfies
   the PRD's own rule that losing Redis must not corrupt a booking). §15f
   still describes WhatsApp as the staff surface; it is SMS. A PRD that is
   quietly out of date is worse than none.

**Out of scope, deliberately:** POS/Square (v2), the table-level resource
layer (schema ready, feature off), payments, multi-location, native app,
WhatsApp (§15e removes it).

---

## 4. Do not change these

Each of these looks like a detail and is load-bearing. Most were paid for by a
real failure.

### The booking path

**Hold before you ask for a name.** The agent calls `hold` the moment a caller
states a time, then collects details. Collecting first and finding the table
gone while they spell their surname is the failure the whole module exists to
prevent.

**Do not add a `check_availability` before `hold`.** `hold` checks the time
itself and returns alternatives. The two-step version reintroduces, in the
conversation, exactly the race the hold transaction closes — and it cost a
whole booking on a live call before it was removed.

**Slots are locked one statement per row, in ascending order.**
`WHERE slot_start = ANY(…) ORDER BY … FOR UPDATE` reads like it locks in
order and usually does, but the planner may sort *after* locking. Multi-slot
bookings would then deadlock against each other under load.

**`confirm` re-reads expiry from the database clock.** It must never succeed
on the strength of an earlier check. A mutation test that removed this
survived the first concurrency suite — the test exists now because of it.

### Identifiers

**`tool_call_id` and `call_id` are two different things and must never share a
name.** The first is AssemblyAI's opaque string for one tool invocation
(`"call_abc123"`); the second is a row in our `calls` table. They were one
name once, the vendor's string reached a `uuid` column, and **every hold on
every live call crashed**.

**A booking reference is not a credential** (§12). Six characters, read aloud
on the phone and printed in a text. It must never authorise anything. Cancel
by text works off the *sending number*; cancel on a call asks for the name.

### The relay

**Tool results go to the model as a JSON string**, not prose
(`session.encode_tool_result`). `Spoken` is a `str` subclass, so
`json.dumps` silently serialised the sentence and dropped every field beside
it — the agent could not see the year, guessed one, and booked into the past.

**Tool results are only sent when `reply.done` is the latest event.** Not
earlier, not later — the API's rule. `TOOL_RESULT_TIMEOUT` is 15 s and is a
stuck-session guard, **not** a mechanism. It was 2 s once, which made it the
normal path and put an audible pause on every turn.

**Refusals carry `requested` and `now`.** A refusal the model cannot diagnose
is one it repeats: told only "that is in the past", it argued with the clock
three times and gave up.

### Messages and tasks

**Messages carry a dedupe key, keyed on the task.** Without it a retried task
texts the guest twice; without retries a task interrupted by a deploy is lost
forever, and for `overflow_timeout` that is precisely the silence §11 forbids.
Keyed on the *task*, not the booking, so a resend the owner asked for still
sends.

**`SMS_PROVIDER` and `VOICE_PROVIDER` default to `log`.** Not an oversight.

### The menu reader

**Nothing it proposes is ever saved.** `read()` returns a proposal; the owner
edits it and confirms, and only that reaches `menu_items`. The agent reads
this menu aloud, so a dish the model invented is one the kitchen must explain
at the table — and a mis-read allergen is the one mistake here that hurts
somebody. The prompt says *copy, never infer*, allergy tags are taken only
when the menu prints them, and a mutation that auto-saves fails its test.

### The dashboard

**Gold means exactly one thing: something needs a decision.** The unseen-row
dot is deliberately the ink colour. A new confirmed booking does not need a
decision, and if the two share a colour the host loses the only signal that
separates "look at this" from "decide this".

**The `<dialog>` lives directly under `<body>`, outside every view.** Inside a
hidden section it still enters the top layer and makes the page inert, but
paints nothing: "Add a booking" did nothing and "Move" froze the page, at
once.

**Audio unlocks on the first tap. No permission prompt, ever** (§14).

**The logo reveal is decided in an inline `<head>` script, and CSS is what
shows it.** `app.js` is deferred, so anything it un-hides necessarily arrives
*after* the dashboard has painted — which is exactly what you saw: the
bookings list, then the logo on top of it. Moving that decision back into
`app.js` would look tidier and bring the flash straight back. The markup keeps
`hidden` so a scriptless page has no overlay at all, and the fade ends on
`visibility:hidden` with `forwards` so a broken `app.js` leaves a usable page
rather than a covered one.

**Stale data is the highest-severity client bug here.** The freshness marker
is driven by when the server *answered*, not when we asked. A host seating
guests from a frozen list is worse than a blank screen.

### Tests

**Write the concurrency test before the code it covers** (§18). An agent asked
for both at once writes a test that passes against its own bug — which is
exactly what happened, and two mutations survived the first suite.

**Drain paginated queue workers in tests** (`tests/conftest.drain`). The test
database is shared and never truncated; `send_queued` and `run_due_tasks` take
a page at a time, so a single call works through an old backlog and never
reaches the row under test. This has bitten twice.

---

## 5. Running it

```bash
make install                 # venv + dev extras
make migrate                 # apply 001..007
make demo                    # a venue to talk to; prints a dashboard link
make dev                     # http://localhost:8080
make test                    # backend (needs TEST_DATABASE_URL)
npm test                     # frontend
```

Deploy and the GCP/DuckDNS/Twilio setup: `docs/DEPLOY.md`.

A dashboard link is minted, printed **once**, and stored only as a hash:

```bash
python -m calling_agent.cli link --slug demo --base-url https://your.host
```

### Environment

`.env.example` documents everything. The ones that change behaviour most:

| Variable | Note |
|---|---|
| `ASSEMBLYAI_API_KEY` | Without it the agent refuses the call rather than failing oddly. |
| `SMS_PROVIDER` | `log` until you want real texts. |
| `VOICE_PROVIDER` | `log` until you want real calls. |
| `MENU_READER` + `GEMINI_API_KEY` | Empty hides the button rather than offering one that fails. |
| `PUBLIC_HOSTNAME` | Required for Twilio to reach back on an outbound call. |
| `TOKEN_PEPPER` | **Never change it.** Every issued dashboard and guest-manage link is invalidated. |
| `VERIFY_TWILIO_SIGNATURE` | On by default. Turning it off is a deliberate act. |
