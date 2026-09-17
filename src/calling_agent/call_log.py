"""Recording what happened on a call (PRD §14, Calls screen).

The call log is the screen that drives both product fixes and renewals: it
shows the turn-by-turn transcript with TOOL CALLS INLINE -- where the agent
checked availability, took the hold, confirmed -- and a separate list of calls
it could not finish.

This is a RECORDER, handed to the relay rather than reached for by it. The
relay stays a relay: it announces what happened and does not know whether
anything is writing it down. A browser demo passes nothing and nothing is
stored; a phone call passes a recorder bound to a row.

Turns are buffered and written on flush rather than per turn, because a call
produces a few dozen events and each one is not worth a round trip while a
caller is waiting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from .db import transaction

log = logging.getLogger(__name__)

#: A call that used one of these is a call that did something. Used to guess
#: an outcome when nothing set one explicitly.
BOOKING_TOOLS = {"confirm", "cancel_booking", "request_overflow"}

#: Transcript entries past this are dropped. A stuck call must not fill the
#: table; the tail is what anyone reads anyway.
MAX_TURNS = 400


class NullRecorder:
    """Records nothing. What a browser demo gets, so no branch is needed above."""

    call_id = None

    def user(self, text: str) -> None: ...
    def agent(self, text: str) -> None: ...
    def tool(
        self, name: str, args: dict[str, Any], result: str, is_error: bool = False
    ) -> None: ...
    def note_outcome(self, outcome: str, *, resolved: bool = True) -> None: ...
    def flush(self) -> None: ...


@dataclass
class CallRecorder:
    """Buffers one call's turns and writes them to `calls` on flush."""

    business_id: UUID | str
    call_id: UUID | str
    turns: list[dict[str, Any]] = field(default_factory=list)
    booking_id: str | None = None
    outcome: str = ""
    resolved: bool = True
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def _add(self, role: str, text_value: str, **extra: Any) -> None:
        if not text_value:
            return
        if len(self.turns) >= MAX_TURNS:
            return
        self.turns.append(
            {
                "role": role,
                "text": text_value,
                "at": datetime.now(UTC).isoformat(),
                **extra,
            }
        )

    def user(self, text_value: str) -> None:
        self._add("user", text_value)

    def agent(self, text_value: str) -> None:
        self._add("agent", text_value)

    def tool(
        self, name: str, args: dict[str, Any], result: str, is_error: bool = False
    ) -> None:
        """One tool call, written the way the Calls screen reads it aloud.

        The arguments matter as much as the result: "checked 21 Sep, 7:00 PM,
        5 covers -- room for it" is what tells somebody debugging a bad booking
        what the agent actually asked.
        """
        summary = _describe(name, args)
        self._add(
            "tool",
            f"{summary} — {result}" if summary else result,
            tool=name,
            args=_safe(args),
            error=is_error,
        )
        if is_error:
            self.resolved = False
        if name in BOOKING_TOOLS and not is_error:
            self.outcome = self.outcome or _OUTCOMES.get(name, "")

    def note_outcome(self, outcome: str, *, resolved: bool = True) -> None:
        self.outcome = outcome
        self.resolved = resolved

    def flush(self) -> None:
        """Write the call. Never raises: a lost transcript must not drop a call."""
        try:
            with transaction() as conn:
                conn.execute(
                    text(
                        "UPDATE calls SET transcript = CAST(:turns AS jsonb),"
                        " outcome = :outcome, resolved = :resolved,"
                        " ended_at = COALESCE(ended_at, now()),"
                        " duration_s = COALESCE(duration_s,"
                        "   EXTRACT(EPOCH FROM (now() - started_at))::int)"
                        " WHERE id = :i AND business_id = :b"
                    ),
                    {
                        "turns": json.dumps(self.turns),
                        "outcome": self.outcome or _guess_outcome(self.turns),
                        "resolved": self.resolved,
                        "i": str(self.call_id),
                        "b": str(self.business_id),
                    },
                )
        except Exception:  # noqa: BLE001
            log.exception("could not write the transcript for call %s", self.call_id)


_OUTCOMES = {
    "confirm": "Booked",
    "cancel_booking": "Cancelled",
    "request_overflow": "Waiting on you",
}


def _describe(name: str, args: dict[str, Any]) -> str:
    """A human sentence for one tool call, for the transcript's inline line."""
    date = args.get("date", "")
    at = args.get("time", "")
    units = args.get("units", args.get("party_size", ""))
    when = " ".join(str(part) for part in (date, at) if part)

    if name == "check_availability":
        return f"Checked what's free — {when}, {units}" if when else "Checked what's free"
    if name == "hold":
        return f"Held it — {when}, {units}" if when else "Held it"
    if name == "confirm":
        return "Confirmed the booking"
    if name == "cancel_booking":
        return f"Cancelled {args.get('reference', '')}".strip()
    if name == "lookup_booking":
        return "Looked the booking up"
    if name == "lookup_customer":
        return "Looked the caller up"
    if name == "request_overflow":
        return f"Put them on the list — {when}" if when else "Put them on the list"
    if name in ("now", "resolve_date", "business_info"):
        return ""
    return f"Ran {name}"


def _guess_outcome(turns: list[dict[str, Any]]) -> str:
    """What to show in the Calls list when nothing set an outcome.

    "Hung up" rather than "" when nothing happened: a blank outcome reads as a
    missing row, and a call where the agent did nothing is exactly what the
    "Where it fell short" card exists to surface.
    """
    for turn in reversed(turns):
        if turn.get("role") == "tool" and turn.get("tool") in _OUTCOMES:
            return _OUTCOMES[turn["tool"]]
    if any(turn.get("role") == "user" for turn in turns):
        return "Asked a question"
    return "Hung up"


def _safe(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments, trimmed. A transcript is read by staff, not just by us."""
    return {k: (v if isinstance(v, int | float | bool) else str(v)[:120])
            for k, v in list(args.items())[:12]}
