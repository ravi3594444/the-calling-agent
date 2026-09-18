"""Test database: a real Postgres, because the product is defined by its locks.

The hold transaction in PRD §7 is `SELECT ... FOR UPDATE` over a set of slot
rows. There is no sqlite equivalent, and a test double of a lock tests the
double. So these tests need a Postgres and skip loudly without one.

Point TEST_DATABASE_URL at any empty database; it is migrated once per session
and each test gets its own tenant, so tests never see each other's slots.
"""

from __future__ import annotations

import itertools
import os
import secrets
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

#: Set deliberately (CI, or a developer pointing at their own Postgres) means
#: a database is EXPECTED, so failing to reach it is an error. Unset means a
#: developer who may not have one, and the suite skips instead.
DATABASE_WAS_REQUESTED = bool(os.getenv("TEST_DATABASE_URL"))

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://tableline@127.0.0.1:54329/tableline_test",
)


@pytest.fixture(scope="session", autouse=True)
def database():
    """Migrate the test database once, or skip every test that needs it."""
    from calling_agent import db
    from calling_agent.config import settings

    settings.database_url = TEST_DATABASE_URL
    db.reset_engine()
    try:
        db.migrate()
    except SQLAlchemyError as exc:  # no server, no database, no permission
        if DATABASE_WAS_REQUESTED:
            # A silent skip here would turn "nothing was tested" into a green
            # run, and the concurrency test is the one that must never be
            # quietly absent.
            raise RuntimeError(
                f"TEST_DATABASE_URL is set but unusable: {TEST_DATABASE_URL} ({exc})"
            ) from exc
        pytest.skip(f"no test database at {TEST_DATABASE_URL}: {exc}")
    yield
    db.reset_engine()


@pytest.fixture(scope="session", autouse=True)
def default_business(database):
    """A business for DEFAULT_BUSINESS_SLUG to point at.

    The relay's default agent is a database lookup now, not a module constant
    (PRD §17). Tests that exercise "pass no agent and get the house one" need
    a house to get, so one is created here and pointed at for the session.
    """
    from calling_agent import businesses
    from calling_agent.config import settings

    slug = "default-test-venue"
    if not businesses.exists(slug):
        make_business(
            slug=slug,
            name="The Copper Kettle",
            config={
                "identity": {
                    "display_name": "The Copper Kettle",
                    "agent_name": "Meera",
                    "description": "modern North Indian food",
                }
            },
        )
    before = settings.default_business_slug
    settings.default_business_slug = slug
    yield businesses.by_slug(slug)
    settings.default_business_slug = before


@pytest.fixture(autouse=True)
def clean_caches():
    from calling_agent import businesses

    businesses.invalidate()
    yield
    businesses.invalidate()


#: A dialled number is UNIQUE across every tenant -- that constraint is what
#: stops two venues sharing a line -- so tests cannot hardcode one. Nor can
#: they use a plain counter: the test database outlives the process, so run
#: two would collide with run one. A per-run random block plus a counter is
#: unique both within a run and across them.
_run_block = secrets.randbelow(10_000)
_next_number = itertools.count(1)


def test_phone_number() -> str:
    """An E.164 number no other test in this database has used."""
    return f"+9199{_run_block:04d}{next(_next_number):04d}"


def make_business(**overrides):
    """A tenant with a known weekly shape, unique to one test.

    Open every day 12:00-22:00 local with 30-minute slots, 90-minute turns and
    a capacity the caller chooses. Nothing here is a default the product reads
    -- it is the fixture stating what this business is, which is exactly how a
    real one is onboarded.
    """
    from calling_agent import businesses

    slug = overrides.pop("slug", f"t{uuid.uuid4().hex[:12]}")
    total_units = overrides.pop("total_units", 12)
    slot_minutes = overrides.pop("slot_minutes", 30)
    turn_minutes = overrides.pop("turn_minutes", 90)
    open_at = overrides.pop("open_at", time(12, 0))
    close_at = overrides.pop("close_at", time(22, 0))
    weekdays = overrides.pop("weekdays", range(7))
    config = overrides.pop("config", None) or {}
    config.setdefault("capacity", {})
    config["capacity"].setdefault("slot_minutes", slot_minutes)
    config["capacity"].setdefault("turn_minutes", turn_minutes)
    # Tests state capacity in whole units; a sellable share would make every
    # expected number a rounding argument. Businesses default to 0.70.
    config["capacity"].setdefault("sellable_pct", 1.0)
    config.setdefault("policy", {})
    config["policy"].setdefault("min_lead_minutes", 0)

    business = businesses.create(
        slug=slug,
        name=overrides.pop("name", "Test Venue"),
        timezone=overrides.pop("timezone", "UTC"),
        vertical=overrides.pop("vertical", "restaurant"),
        phone_number=overrides.pop("phone_number", None),
        config=config,
    )
    businesses.set_capacity_rules(
        business.id,
        [
            {
                "weekday": wd,
                "start_time": open_at,
                "end_time": close_at,
                "total_units": total_units,
                "slot_minutes": slot_minutes,
                "turn_minutes": turn_minutes,
            }
            for wd in weekdays
        ],
    )
    return businesses.by_id(business.id)


@pytest.fixture
def business():
    return make_business()


@pytest.fixture
def call_record(business):
    """A row in `calls`, the way a phone call makes one before the agent starts."""
    from calling_agent.db import transaction

    with transaction() as conn:
        row = conn.execute(
            text("INSERT INTO calls (business_id) VALUES (:b) RETURNING id"),
            {"b": str(business.id)},
        ).first()
    return row[0]


def future_slot(business, *, days_ahead: int = 2, hour: int = 19, minute: int = 0) -> datetime:
    """A bookable instant, expressed in the business's own timezone."""
    local_day = datetime.now(business.tz).date() + timedelta(days=days_ahead)
    return datetime.combine(local_day, time(hour, minute), tzinfo=business.tz).astimezone(UTC)


@pytest.fixture
def slot_at(business):
    def _at(**kwargs):
        return future_slot(business, **kwargs)

    return _at


def committed(business_id, slot_start) -> int:
    """Read the counter directly. The test's only source of truth."""
    from calling_agent import db

    with db.readonly() as conn:
        row = conn.execute(
            text(
                "SELECT committed_units FROM slots WHERE business_id = :b AND slot_start = :s"
            ),
            {"b": str(business_id), "s": slot_start},
        ).fetchone()
    return row[0] if row else 0


__all__ = [
    "make_business",
    "future_slot",
    "committed",
    "test_phone_number",
    "date",
    "TEST_DATABASE_URL",
]
