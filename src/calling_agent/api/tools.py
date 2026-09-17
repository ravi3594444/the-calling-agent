"""The agent tool contract over HTTP (PRD §8).

The same nine tools the voice agent calls, exposed as endpoints so that ANOTHER
system can drive this engine: a different voice provider, a web booking widget,
a POS, someone else's chatbot. That portability is the point of §8 -- if the
latency test sends us to LiveKit or Vapi, the contract moves with us and only
the transport is rewritten.

Latency budget: under 200 ms p95 (PRD §8). Beyond that the caller hears
silence. Each handler is one indexed query plus arithmetic; nothing here calls
out to a network, and notifications are queued rather than sent.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

from .. import agent_tools, businesses
from ..businesses import Business

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _business(business_ref: str | None) -> Business:
    """One tenant per request, named explicitly. Never inferred, never default.

    Accepts a slug, a uuid or a dialled number, because the three kinds of
    caller for this API each know a different one.
    """
    if not business_ref:
        raise HTTPException(400, "Name the business: slug, id or dialled number.")

    for resolve in (
        lambda: businesses.by_slug(business_ref),
        lambda: businesses.by_id(business_ref),
        lambda: businesses.by_phone(business_ref),
    ):
        try:
            return resolve()
        except (businesses.UnknownBusiness, ValueError):
            continue

    raise HTTPException(404, f"No business matches {business_ref!r}.")


@router.post("/{name}")
def run_tool(
    name: str,
    args: dict[str, Any] = Body(default={}),
    x_business: str | None = Header(default=None, alias="X-Business"),
) -> dict[str, Any]:
    """Run one tool and return both what to say and what happened.

    `spoken` is for a voice layer to read aloud; `data` is for anything with a
    screen. Returning both means neither kind of client has to parse the other's
    format -- and nobody has to regex a sentence to find a booking reference.
    """
    business = _business(x_business or args.pop("business", None))

    # Checked against what this business DECLARES, not against one module's
    # implementations: the menu tools live elsewhere, and a business with the
    # menu switched off should not be able to call them either. The listing
    # endpoint and this one must never disagree about what exists.
    if name not in _declared_names(business):
        raise HTTPException(404, f"No tool named {name}.")

    spoken, is_error = agent_tools.run_tool_for(business, name, args)
    return {
        "tool": name,
        "spoken": str(spoken),
        "error": is_error,
        "data": getattr(spoken, "data", {}),
    }


def _declared_names(business: Business) -> set[str]:
    return {tool["name"] for tool in agent_tools.tool_declarations(business)}


@router.get("/{name}/schema")
def tool_schema(
    name: str,
    x_business: str | None = Header(default=None, alias="X-Business"),
) -> dict[str, Any]:
    """One tool's JSON Schema, worded for this business.

    Worded per business because "how many covers" and "how many patients" are
    different questions, and the schema is what a model reads.
    """
    business = _business(x_business)
    for declaration in agent_tools.tool_declarations(business):
        if declaration["name"] == name:
            return declaration
    raise HTTPException(404, f"No tool named {name}.")


@router.get("")
def list_tools(
    x_business: str | None = Header(default=None, alias="X-Business"),
) -> dict[str, Any]:
    """Every tool this business exposes, ready to paste into a model's config."""
    business = _business(x_business)
    return {
        "business": business.slug,
        "vertical": business.vertical,
        "tools": agent_tools.tool_declarations(business),
    }
