"""Agent behaviour: prompt, voice, greeting, tools.

Everything about *what the agent says* lives here, so the relay in session.py
stays pure plumbing.
"""

from datetime import UTC, datetime
from typing import Any

from .config import settings
from .protocol import SESSION_RESUME, SESSION_UPDATE

# How human the agent sounds is only partly the voice model. Wording matters
# as much: written-sounding sentences read as robotic no matter who says them.
SYSTEM_PROMPT = """\
You are a warm, easy-going person having a real conversation on a phone call.
You are not a formal assistant and you should never sound like one.

How to speak:
- Use contractions. Say "I'll", "you're", "that's", "don't" -- never the
  spelled-out forms.
- Keep turns short, usually one or two sentences. Long answers sound scripted.
- Open naturally when it fits: "sure", "yeah", "got it", "good question".
  Sparingly, not every turn.
- React like a person. If something is funny, say so. If it is bad news, be
  gentle about it.
- Vary your sentence length. Uniform sentences are what make speech sound
  synthetic.
- Ask a short follow-up question when the conversation invites one, instead of
  ending every turn flatly.

Language:
- Reply in whatever language the other person is speaking. If they mix two
  languages in one sentence, mix them back the same way -- that is normal
  speech, not a mistake to correct.
- Match their register. If they are casual, be casual.
- Never comment on their accent, grammar, or choice of language, and never
  switch language unless they do.

Hard rules:
- Never use markdown, bullet points, numbered lists, emoji, or any formatting.
  Every word you produce is spoken aloud.
- Write numbers, dates, times, and units the way a person says them: "about
  twenty quid", "half four", "the third of June".
- Never read out URLs, code, or long strings of digits unless asked to.
- If you did not catch something, just ask: "sorry, say that again?"
- If you do not know, say so plainly. Do not invent details.
- Do not mention that you are an AI unless you are asked directly.
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


# Voice ids confirmed against AssemblyAI's own examples and docs. The full
# catalogue is larger -- 18 English and 16 multilingual voices -- so an id not
# listed here may still be valid; these are simply the ones verified to work.
# Multilingual voices code-switch with English automatically.
KNOWN_VOICES: dict[str, str] = {
    "arjun": "Multilingual — Hindi / Hinglish, code-switches with English",
    "diego": "Multilingual — Latin American Spanish",
    "james": "English — conversational US male, very natural",
    "sophie": "English — clear UK female",
    "claire": "English — US female",
    "ivy": "English — US female, lighter and more synthetic",
}


def build_session_update(
    encoding: str, *, tune_turns: bool = True, voice: str | None = None
) -> dict[str, Any]:
    """Build the session.update message for a transport's audio encoding.

    Both input and output formats are pinned to the same encoding. If they
    differ, the agent's reply comes back in a format the transport cannot
    play without transcoding.

    `tune_turns` exists so the caller can retry without the turn-detection
    block: it is the newest and least-documented part of the payload, and a
    single unknown field there is rejected with 1008, taking down the whole
    session rather than just that setting.
    """
    # Stored-agent mode: agent_id must be the only field in `session`.
    # Prompt, greeting and tools are applied server-side.
    if settings.agent_id:
        return {"type": SESSION_UPDATE, "session": {"agent_id": settings.agent_id}}

    audio_in: dict[str, Any] = {"type": "audio", "format": {"encoding": encoding}}
    if tune_turns and settings.turn_detection:
        audio_in["turn_detection"] = {
            "vad_threshold": settings.vad_threshold,
            "min_silence": settings.min_silence_ms,
            "max_silence": settings.max_silence_ms,
            "interrupt_response": settings.allow_interruptions,
        }

    return {
        "type": SESSION_UPDATE,
        "session": {
            "system_prompt": SYSTEM_PROMPT,
            "greeting": settings.agent_greeting,
            "tools": TOOLS,
            "input": audio_in,
            "output": {
                "type": "audio",
                "voice": voice or settings.agent_voice,
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
