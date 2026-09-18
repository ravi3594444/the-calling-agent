"""Reading a photographed menu into dishes you can check (PRD §14).

This module is a BOUNDARY. One multimodal provider does the work, the rest of
the codebase never imports it, and swapping Gemini for Anthropic is a config
change rather than a diff. The registry is the same shape as the SMS provider
registry in `notifications`, for the same reason: the thing that talks to a
third party should be the only thing that knows which third party it is.

The three providers ask for the same JSON in three different ways -- Gemini
constrains generation to a schema, the other two are handed it as a tool --
so `SCHEMA` below is written once and each provider pays its own postage.

NOTHING HERE WRITES A MENU
`read()` returns a PROPOSAL. It is handed to the owner, who edits it and says
yes, and only that yes reaches `menu_items`. The separation is not ceremony:

  - the agent reads the menu aloud to callers, so a dish it invents is a dish
    the kitchen has to explain at the table;
  - an allergen is the one field in this product that can hurt somebody, and a
    model that guesses "contains nuts" from a photo of a curry is guessing
    with someone's airway.

So the prompt below tells the model to COPY and never to infer, every field
comes back editable, and a proposal nobody confirmed expires as what it is:
a suggestion that was never a menu.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .businesses import Business
from .config import settings

log = logging.getLogger(__name__)

#: What the providers accept. A PDF only goes to a provider that reads one.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif"}
PDF_TYPE = "application/pdf"


class MenuReadError(Exception):
    """Raised when a menu could not be read. Carries something sayable."""


@dataclass(frozen=True)
class ProposedDish:
    """One dish the model believes it saw. Not a menu item until confirmed."""

    name: str
    price: float | None = None
    section: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "price": self.price,
            "section": self.section,
            "description": self.description,
            "tags": self.tags,
        }


INSTRUCTIONS = """\
You are reading a photograph or scan of a restaurant menu and listing what is
printed on it.

Copy. Do not infer, complete or improve.

- Take the dish name exactly as written, including the venue's own spelling.
- Take the price as a number only, without a currency symbol. If a dish has no
  printed price, leave the price out rather than estimating one.
- `section` is the heading the dish sits under on the page -- Starters, Sides,
  Desserts. Leave it empty if the menu has no headings.
- `description` is the line printed under the dish, if there is one. Never
  write one yourself.
- `tags` are ONLY the dietary or allergen markers actually printed next to the
  dish: a V, a leaf, "contains nuts", "gluten free". Never work out an
  allergen from the ingredients or the name of a dish. Somebody will eat this
  on the strength of it.
- If part of the menu is blurred, cut off or unreadable, leave those dishes
  out. A missing dish is a question the caller asks; a wrong one is a plate
  going back.

Return every dish you can read, in the order they appear.
"""

#: The shape the model must answer in. Enforced by the provider rather than
#: asked for in prose, because a model asked politely for JSON eventually
#: replies "Sure! Here is the menu:" and the parse fails on a real upload at a
#: real venue, in front of the owner, once.
SCHEMA = {
    "type": "object",
    "properties": {
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exactly as printed."},
                    "price": {"type": "number", "description": "Number only, no symbol."},
                    "section": {"type": "string", "description": "The heading above it."},
                    "description": {"type": "string", "description": "The printed line under it."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only markers printed on the menu.",
                    },
                },
                "required": ["name"],
            },
        }
    },
    "required": ["dishes"],
}


# --- providers ---------------------------------------------------------------

Reader = Callable[[bytes, str, Business], list[dict[str, Any]]]
PROVIDERS: dict[str, Reader] = {}


def provider(name: str) -> Callable[[Reader], Reader]:
    def register(fn: Reader) -> Reader:
        PROVIDERS[name] = fn
        return fn

    return register


def _dishes_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    dishes = payload.get("dishes")
    if not isinstance(dishes, list):
        raise MenuReadError("The reader did not answer with a list of dishes.")
    return dishes


@provider("gemini")
def _gemini(blob: bytes, content_type: str, business: Business) -> list[dict[str, Any]]:
    """Google's Interactions API. Reads photos and PDFs, including handwriting.

    `response_format` rather than function calling: Gemini constrains
    generation to the schema itself, so there is no tool block to unwrap and
    no "Sure! Here is the menu:" to strip.
    """
    import httpx

    if not settings.gemini_api_key:
        raise MenuReadError("GEMINI_API_KEY is not set on the server.")

    response = httpx.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={
            "x-goog-api-key": settings.gemini_api_key,
            "Content-Type": "application/json",
        },
        json={
            "model": settings.menu_reader_model or "gemini-3.7-flash",
            "input": [
                {
                    "type": "document" if content_type == PDF_TYPE else "image",
                    "data": base64.standard_b64encode(blob).decode(),
                    "mime_type": content_type,
                },
                {"type": "text", "text": INSTRUCTIONS},
            ],
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": SCHEMA,
            },
        },
        timeout=settings.menu_reader_timeout,
    )
    _raise_for_provider(response)

    text = _last_text(response.json())
    try:
        return _dishes_from(json.loads(text))
    except (json.JSONDecodeError, TypeError) as exc:
        log.error("menu reader returned unparseable JSON: %.300s", text)
        raise MenuReadError("The reader answered with something that was not a menu.") from exc


def _last_text(payload: dict[str, Any]) -> str:
    """The generated text, from the last step that carries any.

    Walked backwards rather than indexed, because a model that thinks first
    puts reasoning steps ahead of its answer and the answer is the last thing
    in the list, not the first.
    """
    for step in reversed(payload.get("steps") or []):
        for part in step.get("content") or []:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
    raise MenuReadError("The reader did not return a menu.")


@provider("anthropic")
def _anthropic(blob: bytes, content_type: str, business: Business) -> list[dict[str, Any]]:
    import httpx

    if not settings.anthropic_api_key:
        raise MenuReadError("ANTHROPIC_API_KEY is not set on the server.")

    encoded = base64.standard_b64encode(blob).decode()
    attachment = (
        {"type": "document", "source": {"type": "base64",
                                        "media_type": PDF_TYPE, "data": encoded}}
        if content_type == PDF_TYPE
        else {"type": "image", "source": {"type": "base64",
                                          "media_type": content_type, "data": encoded}}
    )

    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": settings.menu_reader_model or "claude-sonnet-5",
            "max_tokens": 8000,
            "tools": [{
                "name": "record_menu",
                "description": "Record every dish printed on this menu.",
                "input_schema": SCHEMA,
            }],
            "tool_choice": {"type": "tool", "name": "record_menu"},
            "messages": [{"role": "user", "content": [attachment, {"type": "text",
                                                                   "text": INSTRUCTIONS}]}],
        },
        timeout=settings.menu_reader_timeout,
    )
    _raise_for_provider(response)

    for block in response.json().get("content", []):
        if block.get("type") == "tool_use":
            return _dishes_from(block.get("input") or {})
    raise MenuReadError("The reader did not return a menu.")


@provider("openai")
def _openai(blob: bytes, content_type: str, business: Business) -> list[dict[str, Any]]:
    import httpx

    if not settings.openai_api_key:
        raise MenuReadError("OPENAI_API_KEY is not set on the server.")
    if content_type == PDF_TYPE:
        raise MenuReadError("This reader takes photos, not PDFs. Upload a photo instead.")

    encoded = base64.standard_b64encode(blob).decode()
    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        json={
            "model": settings.menu_reader_model or "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": INSTRUCTIONS},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{content_type};base64,{encoded}"}},
                ],
            }],
            "tools": [{"type": "function", "function": {
                "name": "record_menu",
                "description": "Record every dish printed on this menu.",
                "parameters": SCHEMA,
            }}],
            "tool_choice": {"type": "function", "function": {"name": "record_menu"}},
        },
        timeout=settings.menu_reader_timeout,
    )
    _raise_for_provider(response)

    calls = response.json()["choices"][0]["message"].get("tool_calls") or []
    if not calls:
        raise MenuReadError("The reader did not return a menu.")
    return _dishes_from(json.loads(calls[0]["function"]["arguments"]))


def _raise_for_provider(response: Any) -> None:
    """Turn a provider's error into one sentence, and log the rest.

    The body carries the account id and sometimes the prompt. It belongs in
    the log, not on a dashboard the whole floor can see.
    """
    if response.status_code < 400:
        return
    log.error("menu reader refused: %s %s", response.status_code, response.text[:500])
    if response.status_code in (401, 403):
        raise MenuReadError("The reader rejected the API key on the server.")
    if response.status_code == 429:
        raise MenuReadError("The reader is rate limited right now. Try again in a minute.")
    raise MenuReadError("The reader could not be reached.")


# --- the boundary ------------------------------------------------------------


def configured() -> bool:
    """Is a reader set up? The dashboard hides the button when not."""
    return bool(settings.menu_reader and settings.menu_reader in PROVIDERS)


def read(business: Business, blob: bytes, content_type: str) -> list[ProposedDish]:
    """Propose the dishes printed on `blob`. Writes nothing, ever."""
    if not configured():
        raise MenuReadError(
            "No menu reader is set up. Set MENU_READER and its API key on the server."
        )
    if content_type not in IMAGE_TYPES and content_type != PDF_TYPE:
        raise MenuReadError("That file is not a photo or a PDF.")

    raw = PROVIDERS[settings.menu_reader](blob, content_type, business)
    proposed = [dish for dish in (_clean(item) for item in raw) if dish is not None]
    log.info("menu reader proposed %d dishes for %s", len(proposed), business.slug)
    if not proposed:
        raise MenuReadError(
            "Nothing readable came back. A straight-on photo in good light reads best."
        )
    return proposed


def _clean(item: Any) -> ProposedDish | None:
    """One raw dish into a proposal, or None if it is not one.

    Defensive because this is model output: a name that came back as a number,
    a price as "£12", tags as a single string. None of that should reach the
    review screen looking like a dish.
    """
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None

    raw_price = item.get("price")
    try:
        price = round(float(raw_price), 2) if raw_price not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    if price is not None and not 0 <= price < 1_000_000:
        price = None

    raw_tags = item.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags = [
        str(tag).strip()[:40]
        for tag in (raw_tags or [])
        if isinstance(tag, (str, int, float)) and str(tag).strip()
    ]

    return ProposedDish(
        name=name[:200],
        price=price,
        section=str(item.get("section") or "").strip()[:80],
        description=str(item.get("description") or "").strip()[:500],
        tags=tags[:12],
    )
