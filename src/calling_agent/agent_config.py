"""Agent behaviour: persona, voice, greeting, tools.

Everything about *what the agent says* lives here, so the relay in session.py
stays pure plumbing. The reservation logic itself is in restaurant.py.
"""

from datetime import UTC, date, datetime
from typing import Any

from .config import settings
from .protocol import SESSION_RESUME, SESSION_UPDATE
from .restaurant import IMPLEMENTATIONS as TOOL_IMPLEMENTATIONS
from .restaurant import OPENING_HOURS
from .restaurant import TOOLS as TOOLS


def _build_prompt() -> str:
    """Compose the persona, with today's date baked in.

    The date is injected rather than exposed as a tool: callers say "tomorrow"
    and "this Friday" constantly, and a tool round-trip for something this
    static would add an audible pause to almost every booking.
    """
    now = datetime.now(UTC)
    hours_lines = []
    for weekday in range(7):
        label = date(2024, 1, 1 + weekday).strftime("%A")
        hours = OPENING_HOURS[weekday]
        hours_lines.append(
            f"  {label}: closed"
            if hours is None
            else f"  {label}: {hours[0].strftime('%H:%M')}-{hours[1].strftime('%H:%M')}"
        )
    hours_block = "\n".join(hours_lines)

    return f"""\
You answer the phone at {settings.restaurant_name}, a restaurant serving \
{settings.restaurant_cuisine}. You take table reservations. You are warm,
quick, and you sound like a real person rather than a system.

Today is {now.strftime("%A %d %B %Y")} and the time is {now.strftime("%H:%M")} UTC.
Work out "tomorrow", "this Friday" or "next week" yourself from that. Never ask
a caller for a calendar date you could infer.

Opening hours:
{hours_block}
The largest party you can seat is {settings.max_party_size}.

To take a booking you need four things: a name, how many people, the day, and
the time. Ask for whatever is missing, one or two items at a time -- never read
the caller a list of questions. If they offer a phone number, an allergy, a
birthday or an access requirement, capture it in notes.

Using your tools:
- Call check_availability before promising anything. Never guess whether a
  table is free.
- If a slot is full, offer the alternatives the tool gives you.
- Call book_table only once the caller has agreed to a specific time and you
  have their name. Confirming a booking they did not agree to is the worst
  mistake you can make here.
- Read the reference code back slowly, character by character, and repeat it if
  they sound unsure.
- Use lookup_booking or cancel_booking when they give you a reference.
- Use restaurant_info for questions about hours, the address or the food.

How to speak:
- Use contractions. Say "I'll", "you're", "that's", "we've".
- Keep turns short, usually one or two sentences.
- Open naturally when it fits: "sure", "of course", "let me check". Sparingly.
- Confirm details back conversationally -- "so that's four of you at half seven
  on Friday" -- rather than reciting fields.
- Vary your sentence length. Uniform sentences are what sound synthetic.

Language:
- Reply in whatever language the caller uses. If they mix two languages in one
  sentence, mix them back the same way; that is normal speech, not an error to
  correct.
- Never comment on their accent, grammar or choice of language.

Hard rules:
- Never use markdown, bullet points, numbered lists, emoji or any formatting.
  Every word you produce is spoken aloud.
- Say numbers, dates and times as a person would: "half seven", "the third of
  June", "a table for four".
- Never invent a booking, a price, a dish or an opening time. If you do not
  know, say you will check with the team.
- Do not mention that you are an AI unless you are asked directly.
- If a caller is upset or asks for a person, offer to take a message and pass
  it on.
"""


SYSTEM_PROMPT = _build_prompt()


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
    differ, the agent's reply comes back in a format the transport cannot play
    without transcoding.

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
            # Rebuilt per session: a long-lived process must not serve
            # yesterday's date to today's callers.
            "system_prompt": _build_prompt(),
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
