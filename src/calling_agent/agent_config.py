"""Agent behaviour: prompt, voice, greeting, tools.

Everything about *what the agent says* lives here, so the relay in session.py
stays pure plumbing.
"""

from datetime import UTC, datetime
from typing import Any

from .config import settings
from .protocol import SESSION_RESUME, SESSION_UPDATE

SYSTEM_PROMPT = """\
You are a helpful voice assistant on a live phone-style call.

Keep these rules in mind at all times:
- Speak naturally and conversationally, as a person would out loud.
- Be brief. One or two sentences per turn unless asked for detail.
- Never use markdown, bullet points, emoji, or any formatting. Your output is
  spoken aloud, so write only what should be said.
- Spell out numbers, dates, and units the way a person would say them.
- If you did not catch something, ask the caller to repeat it.
- If you do not know an answer, say so plainly rather than guessing.
"""

# Tool definitions use JSON Schema. Each name here needs a matching entry in
# TOOL_IMPLEMENTATIONS below.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": (
            "Get the current date and time in UTC. Use when the caller asks "
            "what time or what day it is."
        ),
        # Note: "parameters", not "input_schema". The API rejects the whole
        # session with a 1008 policy violation if this shape is wrong.
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def _get_current_time(_args: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    return now.strftime("%A, %d %B %Y at %H:%M UTC")


TOOL_IMPLEMENTATIONS = {
    "get_current_time": _get_current_time,
}


def run_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Execute a tool call.

    Returns (result, is_error) -- the API wants errors flagged rather than
    disguised as a successful result, so the agent can say something sensible.
    """
    impl = TOOL_IMPLEMENTATIONS.get(name)
    if impl is None:
        return f"No tool named {name}.", True
    try:
        return impl(args), False
    except Exception as exc:  # reported to the agent, not raised at the caller
        return f"Error running {name}: {exc}", True


def build_session_update(encoding: str) -> dict[str, Any]:
    """Build the session.update message for a transport's audio encoding.

    Both input and output formats are pinned to the same encoding. If they
    differ, the agent's reply comes back in a format the transport cannot
    play without transcoding.
    """
    # Stored-agent mode: agent_id must be the only field in `session`.
    # Prompt, greeting and tools are applied server-side.
    if settings.agent_id:
        return {"type": SESSION_UPDATE, "session": {"agent_id": settings.agent_id}}

    return {
        "type": SESSION_UPDATE,
        "session": {
            "system_prompt": SYSTEM_PROMPT,
            "greeting": settings.agent_greeting,
            "tools": TOOLS,
            "input": {"type": "audio", "format": {"encoding": encoding}},
            "output": {
                "type": "audio",
                "voice": settings.agent_voice,
                "format": {"encoding": encoding},
            },
        },
    }


def build_session_resume(session_id: str) -> dict[str, Any]:
    """Rejoin an existing agent session instead of starting a new one.

    The API holds a session open for ~30 seconds after the socket drops. On a
    platform that closes connections on a timer (any serverless function), this
    is what turns a forced disconnect into a seam the caller does not hear
    rather than a dropped call.
    """
    return {"type": SESSION_RESUME, "session_id": session_id}
