"""/healthz and the one thing it must not say "ok" about.

A health check that passes a database missing a migration is worse than none:
the deploy looks fine, the container stays in rotation, and the first caller
whose request touches the new column gets a 500 with nobody watching. The
probe has to know what this build ships and what this database has.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from calling_agent import db
from calling_agent.main import app


def test_a_migrated_database_has_nothing_pending():
    assert db.pending_migrations() == []
    assert db.healthy() == (True, "ok")


def test_a_shipped_migration_this_database_lacks_is_reported_by_name(monkeypatch):
    """Simulated by shipping one more file than the database has seen -- which
    is exactly what a deploy without `cli migrate` looks like from inside."""
    shipped = db.migration_files()
    monkeypatch.setattr(
        db, "migration_files", lambda: shipped + [("999_not_yet_applied.sql", "SELECT 1")]
    )
    assert db.pending_migrations() == ["999_not_yet_applied.sql"]

    ok, detail = db.healthy()
    assert ok is False
    assert "999_not_yet_applied.sql" in detail, "the fix must be readable from the probe"

    body = TestClient(app).get("/healthz").json()
    assert body["status"] == "degraded"
    assert body["database"]["ok"] is False
    assert "999_not_yet_applied.sql" in body["database"]["detail"]
