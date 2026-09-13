"""What a voice agent IS, separated from the relay that carries it.

The relay in session.py used to import the restaurant's prompt and tools
directly, which made it a restaurant server rather than a voice server. It is
the restaurant that is the detail here: the same relay carries any agent that
can answer two questions -- what do you say, and what can you do.

An `AgentDefinition` is those answers. The restaurant is one instance of it
(`agent_config.RESTAURANT`); a second one lives in another repository
entirely, which is the point.

WHY `build_prompt` IS A CALLABLE AND NOT A STRING
The prompt is rebuilt per session because it bakes in today's date. A
long-lived process that captured the string at import serves yesterday's date
to today's callers -- the bug this signature prevents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentDefinition:
    """One voice agent: its persona, its tools, and how to run them."""

    #: Identifies the agent in logs. Never spoken to a caller.
    name: str

    #: Called once per session. Must return the full system prompt.
    build_prompt: Callable[[], str]

    #: The first thing the caller hears.
    greeting: str

    #: Tool declarations, in the API's JSON Schema shape (see protocol.py).
    tools: list[dict[str, Any]]

    #: (name, arguments, call_id) -> (result, is_error). Runs in a worker
    #: thread, so it may block; it must not raise, because the API wants an
    #: error flagged rather than a dropped call.
    #:
    #: `call_id` identifies ONE tool call within the session, and it is here
    #: because an agent whose tools have side effects needs to tell a repeat of
    #: the same call from a second, different one. Without it the only
    #: identifier available is the session, which is the same for every call in
    #: a conversation: an agent that keyed an order on it would answer the
    #: caller's second order with their first. It may be empty if the provider
    #: omits it, so treat it as a hint and not a guarantee.
    run_tool: Callable[[str, dict[str, Any], str], tuple[str, bool]]

    #: Overrides AGENT_VOICE for this agent. None means use the configured one.
    voice: str | None = None

    #: What the BROWSER should call this, if it asks (/experience). None means
    #: the configured restaurant name, which is right only for the restaurant.
    #: A page that serves another agent under this relay otherwise shows the
    #: restaurant's name over someone else's product.
    display_name: str | None = None
