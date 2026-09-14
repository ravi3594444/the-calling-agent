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
    #: thread, so it may block. It should not raise -- the API wants an error
    #: flagged rather than a dropped call -- but the relay no longer takes that
    #: on trust: anything raised here, and anything returned that is not the
    #: documented pair, becomes an error result for the agent instead of ending
    #: the call. `is_error` is what lets the agent say something sensible to
    #: the caller rather than going quiet.
    #:
    #: `call_id` identifies ONE tool call within the session, and it is here
    #: because an agent whose tools have side effects needs to tell a repeat of
    #: the same call from a second, different one. Without it the only
    #: identifier available is the session, which is the same for every call in
    #: a conversation: an agent that keyed an order on it would answer the
    #: caller's second order with their first.
    #:
    #: IT IS A CONTRACT, NOT A DETAIL. Another codebase derives the idempotency
    #: key for a real side effect (creating a customer order) from this value,
    #: so all three of these hold and none of them may be "simplified":
    #:
    #:  * It is the PROVIDER'S id, passed through verbatim. The relay never
    #:    invents one, never renumbers, never substitutes the session id and
    #:    never uses a counter. An id this server made up would be a different
    #:    id after a reconnect, which turns a retry into a second order.
    #:  * It is STABLE ACROSS A RECONNECT, which is what makes it useful: a
    #:    dropped socket is the same call, the upstream session outlives it and
    #:    re-issues the tool call it never got an answer to under the same
    #:    call_id. (The relay answers such a re-issue from its own per-session
    #:    cache, keyed on the same value, so the side effect runs once even if
    #:    the agent's own tools are not idempotent.)
    #:  * It MAY BE EMPTY, when the provider omits it -- and empty is the
    #:    ABSENCE of an id, never an identity. Two unrelated calls can both
    #:    arrive as "", so anything keyed on the empty string merges them into
    #:    one. Treat it as a hint: no id, no idempotency, just run the tool.
    run_tool: Callable[[str, dict[str, Any], str], tuple[str, bool]]

    #: Overrides AGENT_VOICE for this agent. None means use the configured one.
    voice: str | None = None

    #: What the BROWSER should call this, if it asks (/experience). None means
    #: the configured restaurant name, which is right only for the restaurant.
    #: A page that serves another agent under this relay otherwise shows the
    #: restaurant's name over someone else's product.
    display_name: str | None = None
