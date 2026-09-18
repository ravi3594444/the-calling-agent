"""One versioned config document per business (PRD §15).

Everything a business can differ in lives here: identity, voice, language,
locale, capacity shape, policy, message templates, digest and nudge timing,
outbound rules and per-vertical prompt fragments.

THE HARD RULE THIS FILE EXISTS TO ENFORCE
`if business_id == N` appears nowhere in the codebase. When a venue needs
behaviour the config cannot express, a field is added here and every venue
gets it. Special-casing one customer would destroy the only property that
makes onboarding a ten-minute form.

The effective config a caller sees is three layers deep-merged in order:

    DEFAULTS  <-  verticals.json[vertical]  <-  businesses.config

so a vertical changes a class of business, and a business overrides its
vertical, and neither requires a code change.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

# --- defaults ----------------------------------------------------------------

#: Bumped when a migration of stored config documents is required. Stored on
#: `businesses.config_version` so an old document is recognisable rather than
#: silently reinterpreted.
CONFIG_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "identity": {
        "display_name": "",
        "description": "",
        "address": "",
        "agent_name": "",
        # {display_name} and {agent_name} are substituted. Empty means the
        # greeting is composed from identity, which is what onboarding wants.
        "greeting": "",
        "price_band": "",
        "public_url": "",
    },
    "locale": {
        "country": "IN",
        "currency": "INR",
        "currency_symbol": "₹",
        "currency_name": "Indian rupee",
        "timezone": "Asia/Kolkata",
        "clock": "12",
        "date_format": "DMY",
        # Public holidays close the venue unless the owner opens the day.
        "observe_public_holidays": True,
    },
    "voice": {
        "voice_id": "",
        "languages": ["English"],
    },
    "capacity": {
        # PRD §15d: a venue that sets no capacity gets an agent that takes
        # everything. No bars, no "of 28", no blocking.
        "enabled": True,
        "unit_label_singular": "place",
        "unit_label_plural": "places",
        "slot_minutes": 30,
        "turn_minutes": 60,
        # The share never sold to the agent, kept for walk-ins.
        "sellable_pct": 0.70,
    },
    "policy": {
        "auto_approve_max_party": 8,
        "max_party_size": 12,
        "min_lead_minutes": 120,
        "hold_ttl_seconds": 300,
        "overflow_timeout_minutes": 60,
        "overflow_cutoff_local_time": "18:00",
        "max_pending_per_slot": 3,
        # PRD §10: silence confirms.
        "default_yes": True,
    },
    "booking_window": {
        # "rolling" = today + days, moving forward each night.
        # "fixed"   = stops on `until`, for a season or a pop-up.
        "mode": "rolling",
        "days": 60,
        "until": None,
    },
    "messaging": {
        "send_confirmation": True,
        "send_reminder": True,
        "reminder_hours_before": 24,
        "digest_local_time": "17:00",
        "arrival_nudge_minutes": 30,
        "templates": {
            "confirmed": (
                "{display_name}: booked for {party_size} on {date_long} at {time}. "
                "Reference {reference}.[[ Manage it here: {manage_url}]]"
            ),
            "declined": (
                "{display_name}: sorry, {time} on {date_long} is full."
                "[[ The nearest we have is {alternative} — call us to take it.]]"
            ),
            "reminder": (
                "{display_name}: see you {date_long} at {time}, {party_size} people."
                "[[ Need to change it? {manage_url}]]"
            ),
            "cancelled": (
                "{display_name}: your booking {reference} on {date_long} at {time} "
                "is cancelled."
            ),
            "pending": (
                "{display_name}: we have your request for {party_size} at {time} on "
                "{date_long}. We will confirm within the hour."
            ),
        },
    },
    "telling_guest": {
        # PRD §15e: bad news lands better spoken. "ask" prompts each time.
        "on_accept": "text",
        "on_decline": "call",
    },
    "outbound": {
        "enabled": True,
        "window_start": "09:00",
        "window_end": "20:00",
        "max_attempts": 2,
        "sms_first": True,
    },
    "agent": {
        "vertical_fragment": "",
        "booking_noun": "booking",
        "party_noun": "people",
        "extra_instructions": "",
        "never_say": [],
        "tone": "warm, quick and genuinely helpful",
    },
    "features": {
        "menu": False,
        # PRD §6: schema ready, feature off.
        "resources": False,
        "overflow": True,
    },
}


# --- schema ------------------------------------------------------------------

_TIME = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Tableline business config",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "identity": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "display_name": {"type": "string", "maxLength": 200},
                "description": {"type": "string", "maxLength": 500},
                "address": {"type": "string", "maxLength": 500},
                "agent_name": {"type": "string", "maxLength": 80},
                "greeting": {"type": "string", "maxLength": 500},
                "price_band": {"type": "string", "maxLength": 120},
                "public_url": {"type": "string", "maxLength": 500},
            },
        },
        "locale": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "country": {"type": "string", "pattern": "^[A-Z]{2}$"},
                "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
                "currency_symbol": {"type": "string", "maxLength": 8},
                "currency_name": {"type": "string", "maxLength": 80},
                "timezone": {"type": "string", "maxLength": 80},
                "clock": {"enum": ["12", "24"]},
                "date_format": {"enum": ["DMY", "MDY", "YMD"]},
                "observe_public_holidays": {"type": "boolean"},
            },
        },
        "voice": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "voice_id": {"type": "string", "maxLength": 80},
                "languages": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 40},
                    "maxItems": 12,
                },
            },
        },
        "capacity": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "enabled": {"type": "boolean"},
                "unit_label_singular": {"type": "string", "minLength": 1, "maxLength": 40},
                "unit_label_plural": {"type": "string", "minLength": 1, "maxLength": 40},
                "slot_minutes": {"type": "integer", "minimum": 5, "maximum": 240},
                "turn_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                "sellable_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            },
        },
        "policy": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "auto_approve_max_party": {"type": "integer", "minimum": 1},
                "max_party_size": {"type": "integer", "minimum": 1, "maximum": 1000},
                "min_lead_minutes": {"type": "integer", "minimum": 0, "maximum": 20160},
                "hold_ttl_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                "overflow_timeout_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                "overflow_cutoff_local_time": {"type": "string", "pattern": _TIME},
                "max_pending_per_slot": {"type": "integer", "minimum": 0, "maximum": 50},
                "default_yes": {"type": "boolean"},
            },
        },
        "booking_window": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"enum": ["rolling", "fixed"]},
                "days": {"type": "integer", "minimum": 1, "maximum": 730},
                "until": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            },
        },
        "messaging": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "send_confirmation": {"type": "boolean"},
                "send_reminder": {"type": "boolean"},
                "reminder_hours_before": {"type": "integer", "minimum": 1, "maximum": 168},
                "digest_local_time": {"type": "string", "pattern": _TIME},
                "arrival_nudge_minutes": {"type": "integer", "minimum": 0, "maximum": 240},
                "templates": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "maxLength": 1000},
                },
            },
        },
        "telling_guest": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "on_accept": {"enum": ["text", "call", "none", "ask"]},
                "on_decline": {"enum": ["text", "call", "none", "ask"]},
            },
        },
        "outbound": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "enabled": {"type": "boolean"},
                "window_start": {"type": "string", "pattern": _TIME},
                "window_end": {"type": "string", "pattern": _TIME},
                "max_attempts": {"type": "integer", "minimum": 0, "maximum": 5},
                "sms_first": {"type": "boolean"},
            },
        },
        "agent": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "vertical_fragment": {"type": "string", "maxLength": 2000},
                "booking_noun": {"type": "string", "maxLength": 40},
                "party_noun": {"type": "string", "maxLength": 40},
                "extra_instructions": {"type": "string", "maxLength": 4000},
                "never_say": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 200},
                    "maxItems": 50,
                },
                "tone": {"type": "string", "maxLength": 200},
            },
        },
        "features": {
            "type": "object",
            "additionalProperties": {"type": "boolean"},
        },
    },
}

_VALIDATOR = Draft202012Validator(SCHEMA)

#: The sections the dashboard may PUT. Anything else is refused by name rather
#: than silently dropped, so a typo in a client is visible.
SECTIONS = tuple(SCHEMA["properties"].keys())


class ConfigError(ValueError):
    """A config document the product refuses to run on."""


# --- vertical profiles -------------------------------------------------------


@lru_cache(maxsize=1)
def _verticals() -> dict[str, Any]:
    raw = resources.files(__package__).joinpath("data/verticals.json").read_text("utf-8")
    data = json.loads(raw)
    data.pop("_comment", None)
    return data


def known_verticals() -> list[str]:
    return sorted(k for k in _verticals() if k != "default")


# --- merging -----------------------------------------------------------------


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return base with patch merged in. Dicts recurse; everything else replaces.

    Lists replace rather than concatenate: "the languages we speak" set to
    ["English"] must mean exactly that, not "English in addition to whatever
    was there before".
    """
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def effective(stored: dict[str, Any] | None, vertical: str = "default") -> dict[str, Any]:
    """DEFAULTS <- vertical profile <- the business's own document."""
    profile = _verticals().get(vertical) or _verticals().get("default") or {}
    merged = deep_merge(DEFAULTS, profile)
    return deep_merge(merged, stored or {})


# --- validation --------------------------------------------------------------


def validate(document: dict[str, Any]) -> None:
    """Raise ConfigError naming every problem, not just the first.

    Every problem, because this runs behind a Save button: a form that reports
    one error per round trip makes a six-field mistake a six-save chore.
    """
    errors = sorted(_VALIDATOR.iter_errors(document), key=lambda e: list(e.absolute_path))
    problems = [
        f"{'.'.join(str(p) for p in e.absolute_path) or 'config'}: {e.message}" for e in errors
    ]
    problems += _semantic_problems(document)
    if problems:
        raise ConfigError("; ".join(problems))


def _semantic_problems(document: dict[str, Any]) -> list[str]:
    """Rules JSON Schema cannot state, checked against the MERGED document.

    Checked here rather than at use because a config that cannot be satisfied
    is a venue whose phone answers and then refuses every caller -- and the
    owner finds out from a guest, not from us.
    """
    problems: list[str] = []
    merged = effective(document)

    capacity = merged["capacity"]
    if capacity["turn_minutes"] % capacity["slot_minutes"]:
        problems.append(
            "capacity: turn_minutes must be a whole number of slots "
            f"({capacity['turn_minutes']} is not a multiple of {capacity['slot_minutes']})"
        )

    window = merged["booking_window"]
    if window["mode"] == "fixed" and not window.get("until"):
        problems.append("booking_window: a fixed window needs an end date")

    outbound = merged["outbound"]
    if outbound["window_start"] >= outbound["window_end"]:
        problems.append("outbound: the calling window must start before it ends")

    policy = merged["policy"]
    if policy["auto_approve_max_party"] > policy["max_party_size"]:
        problems.append(
            "policy: auto_approve_max_party cannot exceed max_party_size -- "
            "the agent would auto-approve a party it must refuse"
        )

    templates = merged["messaging"]["templates"]
    for name, body in templates.items():
        for field in re.findall(r"{(\w+)}", body):
            if field not in TEMPLATE_FIELDS:
                problems.append(
                    f"messaging.templates.{name}: unknown field {{{field}}}. "
                    f"Available: {', '.join(sorted(TEMPLATE_FIELDS))}"
                )
    return problems


#: Everything a message template may interpolate. A template naming anything
#: else is refused at save, because the alternative is a customer receiving
#: "{manage_link}" as literal text at eleven at night.
TEMPLATE_FIELDS = frozenset(
    {
        "display_name",
        "agent_name",
        "reference",
        "name",
        "party_size",
        "unit_label",
        "date",
        "date_long",
        "time",
        "manage_url",
        "alternative",
        "address",
        "phone",
        "currency_symbol",
    }
)


#: A segment in [[double brackets]] is dropped whole when any field inside it
#: is empty. Without it a template that mentions a value which is sometimes
#: absent leaves its label dangling: "Manage it here: Reply C to cancel."
OPTIONAL_SEGMENT = re.compile(r"\[\[(.*?)\]\]", re.S)


def render_template(body: str, values: dict[str, Any]) -> str:
    """Fill a template, dropping optional segments whose values are missing.

    Validation already refused unknown fields at save; this handles the case
    where a value is simply absent for one booking -- no alternative to offer,
    no manage link because the venue has no public URL yet. An empty string is
    the right answer for the field, and [[...]] is how a template says "and if
    it is empty, say nothing at all".
    """
    safe = {field: "" for field in TEMPLATE_FIELDS}
    safe.update({k: ("" if v is None else str(v)) for k, v in values.items()})

    def fill(text: str) -> str:
        return re.sub(r"{(\w+)}", lambda m: safe.get(m.group(1), m.group(0)), text)

    def segment(match: re.Match[str]) -> str:
        inner = match.group(1)
        named = re.findall(r"{(\w+)}", inner)
        if any(not safe.get(field, "") for field in named):
            return ""
        return fill(inner)

    out = OPTIONAL_SEGMENT.sub(segment, body)
    out = fill(out)
    return re.sub(r"\s{2,}", " ", out).strip()
