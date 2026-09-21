"""Database access: one engine, one transaction helper, one migrator.

Everything that touches Postgres goes through `transaction()`. It is a real
transaction with a real connection, because the two operations this product
cannot get wrong -- taking a hold and confirming it -- are defined by `SELECT
... FOR UPDATE` inside one. An ORM session that flushes when it feels like it
would move the lock, which is the whole mechanism.

PgBouncer in transaction mode (PRD §16) is why pooling here is deliberately
small and why nothing relies on session state: prepared statements, temp
tables and `SET` do not survive a transaction-pooled connection.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from typing import Any

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine, Row

from .config import settings

log = logging.getLogger(__name__)

_engine: Engine | None = None
_engine_url: str | None = None
_lock = threading.Lock()


def engine() -> Engine:
    """The process-wide engine, built on first use.

    Built lazily rather than at import so that importing this package -- which
    the relay, the tests and any consumer do -- never requires a reachable
    database. A voice relay serving someone else's agent has no schema of ours
    and must still start.

    Rebuilt if DATABASE_URL changed under us, which is what a test that points
    at an ephemeral database does.
    """
    global _engine, _engine_url
    url = settings.database_url
    with _lock:
        if _engine is None or _engine_url != url:
            if _engine is not None:
                _engine.dispose()
            _engine = create_engine(
                url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_pre_ping=True,
                pool_recycle=1800,
                # Transaction-pooled PgBouncer cannot carry server-side
                # prepared statements between transactions.
                connect_args={"prepare_threshold": None} if url.startswith("postgresql") else {},
                future=True,
            )
            _engine_url = url
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Tests that switch databases call this."""
    global _engine, _engine_url
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _engine_url = None


@contextmanager
def transaction() -> Iterator[Connection]:
    """One connection, one transaction, committed on clean exit.

    Nested use is not supported on purpose: a "transaction" that silently
    joined an outer one would make the hold's locks outlive the hold, and the
    first symptom would be a booking blocking a slot that nobody holds.
    """
    with engine().begin() as conn:
        yield conn


@contextmanager
def readonly() -> Iterator[Connection]:
    """A connection for reads. Separate name so intent is visible at the call."""
    with engine().connect() as conn:
        yield conn


def fetch_all(conn: Connection, sql: str, **params: Any) -> Sequence[Row[Any]]:
    return conn.execute(text(sql), params).fetchall()


def fetch_one(conn: Connection, sql: str, **params: Any) -> Row[Any] | None:
    return conn.execute(text(sql), params).fetchone()


def execute(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params)


# --- migrations --------------------------------------------------------------

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def migration_files() -> list[tuple[str, str]]:
    """(name, sql) for every shipped migration, in lexical order.

    Read through importlib.resources so an installed wheel migrates exactly
    like a checkout does -- a consumer that pip-installs this package gets the
    schema, not a FileNotFoundError.
    """
    package = resources.files(__package__).joinpath("sql")
    out: list[tuple[str, str]] = []
    for item in sorted(package.iterdir(), key=lambda p: p.name):
        if item.name.endswith(".sql"):
            out.append((item.name, item.read_text(encoding="utf-8")))
    return out


def migrate() -> list[str]:
    """Apply every migration not yet recorded. Returns the ones applied.

    Each migration runs in its own transaction: a failure leaves the ones
    before it applied and recorded, so re-running resumes rather than restarts.
    """
    applied: list[str] = []
    with transaction() as conn:
        conn.execute(text(_MIGRATIONS_TABLE))
        done = {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}
    for name, sql in migration_files():
        if name in done:
            continue
        with transaction() as conn:
            conn.execute(text(sql))
            conn.execute(
                text("INSERT INTO schema_migrations (name) VALUES (:n)"), {"n": name}
            )
        log.info("applied migration %s", name)
        applied.append(name)
    return applied


def pending_migrations() -> list[str]:
    """Shipped migrations this database has not applied, in the order they run.

    Exact rather than a heuristic: the files in sql/ minus the names recorded
    in schema_migrations. A database with no schema_migrations table at all has
    never been migrated, so everything is pending.
    """
    with readonly() as conn:
        has_table = conn.execute(
            text("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        ).scalar()
        applied = (
            {row[0] for row in conn.execute(text("SELECT name FROM schema_migrations"))}
            if has_table
            else set()
        )
    return [name for name, _ in migration_files() if name not in applied]


def healthy() -> tuple[bool, str]:
    """Is the database reachable and FULLY migrated? Used by /healthz.

    Fully: every migration this build ships is recorded as applied. The check
    this replaces asked whether `slots` and `bookings` exist, which any database
    migrated at least once passes -- including the one a deploy that forgot
    `cli migrate` is running against, right up until the first query touches
    the column the new migration added. The report names what is missing, so
    the fix is readable from the probe.
    """
    try:
        pending = pending_migrations()
    except Exception as exc:  # noqa: BLE001 - the report is the point
        return False, str(exc).splitlines()[0][:200]
    if pending:
        shown = ", ".join(pending[:3]) + (", …" if len(pending) > 3 else "")
        return False, f"{len(pending)} migration(s) pending: {shown}"
    return True, "ok"
