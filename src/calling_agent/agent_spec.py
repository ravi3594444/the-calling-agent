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

    #: (name, arguments) -> (result, is_error). Runs in a worker thread, so it
    #: may block; it must not raise, because the API wants an error flagged
    #: rather than a dropped call.
    run_tool: Callable[[str, dict[str, Any]], tuple[str, bool]]

    #: Overrides AGENT_VOICE for this agent. None means use the configured one.
    voice: str | None = None
