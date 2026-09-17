"""Resolving WHICH business a request is for, before it can read anything.

This is the only place a tenant is decided for an HTTP request, and it is the
one line of defence against the risk in PRD §20: a multi-tenant data leak.
Every handler takes the Business this returns and passes it down; no handler
reads a business id out of a body or a query string, because those are written
by whoever is calling.

The dashboard has no login during service (PRD §14). What it has instead is a
long secret link: 32+ random characters, stored hashed, revocable. That is a
credential; the six-character booking reference is not, and is never accepted
here.
"""

from __future__ import annotations

import logging

from fastapi import Header, HTTPException, Query, Request

from .. import businesses, tokens
from ..businesses import Business
from ..config import settings
from ..db import fetch_one, transaction

log = logging.getLogger(__name__)


def _from_token(raw_token: str) -> Business | None:
    """Look a dashboard token up by its hash, and refuse a dead one."""
    if not raw_token:
        return None
    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT business_id, expires_at, revoked_at FROM dashboard_tokens"
            " WHERE token_hash = :h",
            h=tokens.hash_secret(raw_token),
        )
        if row is None:
            return None
        if row.revoked_at is not None:
            return None
        conn.execute(
            _sql("UPDATE dashboard_tokens SET last_used_at = now() WHERE token_hash = :h"),
            {"h": tokens.hash_secret(raw_token)},
        )
    if row.expires_at is not None and _expired(row.expires_at):
        return None
    try:
        return businesses.by_id(row.business_id)
    except businesses.UnknownBusiness:
        return None


def _expired(expires_at) -> bool:
    from datetime import UTC, datetime

    return expires_at <= datetime.now(UTC)


def current_business(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tableline_token: str | None = Header(default=None, alias="X-Tableline-Token"),
    token: str | None = Query(default=None),
) -> Business:
    """The tenant for this request, from its token.

    Accepts the token in a header or a query string. The query string exists
    because the dashboard is opened from a link saved to a home screen, and a
    home-screen shortcut cannot set a header.

    DEV_DASHBOARD_BUSINESS binds the dashboard to one venue with no token at
    all. It needs ALLOW_INSECURE_DASHBOARD set as well, deliberately: one
    variable is something you set by accident, two is something you meant.
    """
    raw = (
        x_tableline_token
        or (authorization.removeprefix("Bearer ").strip() if authorization else None)
        or token
        or request.cookies.get("tableline_token")
        or ""
    ).strip()

    found = _from_token(raw)
    if found is not None:
        return found

    if settings.dev_dashboard_business and settings.allow_insecure_dashboard:
        try:
            return businesses.by_slug(settings.dev_dashboard_business)
        except businesses.UnknownBusiness as exc:
            raise HTTPException(
                500, "DEV_DASHBOARD_BUSINESS names a business that does not exist"
            ) from exc

    raise HTTPException(401, "This dashboard link is not valid. Ask for a new one.")


def issue_dashboard_token(business_id, *, label: str = "", expires_at=None) -> str:
    """Mint a dashboard link. The plain token is returned ONCE and never stored."""
    secret = tokens.new_secret(32)
    with transaction() as conn:
        conn.execute(
            _sql(
                "INSERT INTO dashboard_tokens (business_id, token_hash, label, expires_at)"
                " VALUES (:b, :h, :label, :exp)"
            ),
            {
                "b": str(business_id),
                "h": tokens.hash_secret(secret),
                "label": label,
                "exp": expires_at,
            },
        )
    return secret


def revoke_dashboard_token(business_id, raw_token: str) -> bool:
    with transaction() as conn:
        result = conn.execute(
            _sql(
                "UPDATE dashboard_tokens SET revoked_at = now()"
                " WHERE business_id = :b AND token_hash = :h AND revoked_at IS NULL"
            ),
            {"b": str(business_id), "h": tokens.hash_secret(raw_token)},
        )
    return result.rowcount > 0


def _sql(text_sql: str):
    from sqlalchemy import text

    return text(text_sql)
