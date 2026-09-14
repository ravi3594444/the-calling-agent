"""Agent behaviour: persona, voice, greeting, tools.

Everything about *what the agent says* lives here, so the relay in session.py
stays pure plumbing. The reservation logic itself is in restaurant.py.
"""

import logging
from datetime import UTC, date, datetime
from typing import Any

from .agent_spec import AgentDefinition
from .config import settings
from .protocol import SESSION_RESUME, SESSION_UPDATE
from .restaurant import IMPLEMENTATIONS as TOOL_IMPLEMENTATIONS
from .restaurant import OPENING_HOURS
from .restaurant import TOOLS as TOOLS

log = logging.getLogger(__name__)


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
You are Meera, and you answer the phone at {settings.restaurant_name}, a
restaurant serving {settings.restaurant_cuisine}. You take bookings and answer
questions about the food. You are warm, quick and genuinely helpful, and you
sound like a person who works there -- because as far as the caller is
concerned, you do.

Today is {now.strftime("%A %d %B %Y")}, {now.strftime("%H:%M")} UTC. Work out
"tomorrow", "this Friday" or "next weekend" yourself. Never ask a caller for a
calendar date you could have inferred.

We serve:
{hours_block}
The largest party you can seat is {settings.max_party_size}. Prices are in rupees.

TAKING A BOOKING
You need four things: a name, how many people, the day, and the time. Ask for
whatever is missing, one or two items at a time. Never read the caller a list
of questions.
- Check availability before you promise anything. Never guess.
- If a slot is full, offer the alternatives the tool gives you, warmly -- "I
  could do quarter past eight, would that work?"
- Do not narrate that you are checking and then fall silent. Check, then speak.
- Only book once they have agreed to a specific time and you have their name.
  Confirming a booking they did not agree to is the worst thing you can do
  here.
- Read the reference back slowly, character by character. Repeat it if they
  sound unsure.
- Capture anything extra in notes: a birthday, a wheelchair, a quiet table, a
  phone number, an allergy.

ANSWERING IN ONE TURN
Look things up and give the answer in the same turn. Do not say "let me check"
or "one moment" and then stop -- the caller is left holding a silent phone and
has to ask again. Either answer straight away, or say the holding phrase and
the answer together: "let me see -- yes, eight o'clock is free."

TALKING ABOUT THE MENU
- Never read the whole menu. Offer two or three things and ask -- "we do a
  butter chicken and a rogan josh, or if you want vegetarian there's the dal
  makhani. Any of those sound good?"
- If they ask what is good, recommend. Have an opinion; do not list.
- Say prices naturally: "four eighty", "about three fifty", not "480.00".
- Look dishes up rather than remembering them. You will misremember.

ALLERGIES AND DIET
Take these seriously; getting one wrong could hurt someone.
- Search the menu with the allergen excluded rather than reasoning about it
  yourself.
- Say what a dish contains, not what it is free of, unless you checked.
- If you are not certain, say you will check with the kitchen. Never guess.
- Always put an allergy in the booking notes, even if they only mention it in
  passing.

WHEN YOU CANNOT HELP
- Fully booked: say so plainly, offer the nearest times or another day.
- Party too large: say the largest you can seat and offer to take a message.
- A complaint, or they want a person: do not argue and do not defend. Say you
  will pass it on, and take a name and number.
- Anything you do not know -- parking, a dish not on the menu, whether the
  chef will make something off-menu -- offer to check and call back.

HOW YOU SPEAK
- Contractions always: "I'll", "you're", "that's", "we've".
- One or two sentences a turn. You are on a phone, not writing.
- Natural openers where they fit: "sure", "of course", "ah", "right".
  Sparingly -- not every turn.
- Confirm back conversationally: "so that's four of you at half seven on
  Friday, under Ravi" -- not a recital of fields.
- Vary your sentence length. Uniform sentences are what make speech sound
  synthetic.
- If they interrupt you, stop and listen. Do not finish your sentence.
- Close properly: confirm what is booked, then "see you Friday" or similar.

LANGUAGE
- Reply in whatever language the caller uses. If they mix two languages in one
  sentence, mix them back the same way -- that is how people actually talk, not
  a mistake to correct.
- Never comment on their accent, grammar or choice of language.

NEVER
- Never use markdown, bullet points, numbered lists, emoji or any formatting.
  Every word you produce is spoken aloud.
- Never invent a booking, a price, a dish, an opening time or an ingredient.
- Never read out a URL or a long string of digits unless asked.
- Never say you are an AI unless you are asked directly. If asked, be honest
  and carry on.
- Never rush someone who is deciding. A short "take your time" is better than
  filling the silence.
"""

SYSTEM_PROMPT = _build_prompt()


def run_tool(name: str, args: dict[str, Any], call_id: str = "") -> tuple[str, bool]:
    """Execute a tool call.

    `call_id` is unused here: the restaurant's tools are keyed on the booking
    reference they return, not on the call that made them. An agent whose
    writes need to tell a retry from a second, different write uses it.

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
# AssemblyAI publishes 18 English and 16 multilingual voices and keeps adding
# to the catalogue, but only these ids appear in their own docs and example
# code, so only these are offered by default. Any other id still works via
# /ws?voice=<id> -- and if a voice is refused the session falls back to
# FALLBACK_VOICE rather than dropping the call.
KNOWN_VOICES: dict[str, str] = {
    "arjun": "Multilingual — Hindi / Hinglish, code-switches with English",
    "diego": "Multilingual — Latin American Spanish",
    "james": "English — conversational US male, very natural",
    "sophie": "English — clear UK female",
    "ivy": "English — US female, lighter and more synthetic",
}


# Used when a requested voice is refused. This is the id AssemblyAI's own
# example repository defaults to, so it is the likeliest to exist even as the
# catalogue changes -- correctness of fallback matters more than how it sounds.
FALLBACK_VOICE = "ivy"


class AgentModeConflict(RuntimeError):
    """A stored agent and a locally defined agent are both configured.

    Raised rather than resolved, because resolving it is what made the failure
    invisible: the payload silently became the stored agent's while tool
    dispatch stayed with the local one.
    """


def agent_mode_conflict(agent: AgentDefinition | None = None) -> str | None:
    """Why this deployment cannot serve a stored agent AND a local one.

    Returns the message to show, or None when the configuration is coherent.

    Each mode alone is supported and neither is touched here. AGENT_ID alone is
    the stored-agent deployment: AssemblyAI holds the prompt, greeting and tool
    declarations, and this server implements those tool names. AGENT_FACTORY
    alone is the whole point of the factory -- another codebase serving its own
    agent through this relay.

    Only the PAIR is broken, and it is broken silently. `build_session_update`
    returns early in stored-agent mode, so the local agent's prompt, greeting,
    tools and voice are all discarded before they are ever used -- but
    `session.py` still dispatches every tool call into that same discarded
    agent's `run_tool`. The stored agent's tool names cannot match the local
    agent's, so every call comes back "No tool named X": an agent that can do
    nothing, with no error anywhere naming the cause.
    """
    if not settings.agent_id:
        return None

    factory = settings.agent_factory.strip()
    if factory:
        origin = f"AGENT_FACTORY ({factory})"
        remedy = (
            "Unset AGENT_ID to keep serving the factory's agent, or unset "
            "AGENT_FACTORY to use the stored one."
        )
    elif agent is not None and agent is not RESTAURANT:
        # No factory, but a consumer constructed a session with its own agent.
        # Same breakage, reached through the library rather than the env.
        origin = f"an agent passed in directly ({agent.name!r})"
        remedy = (
            "Unset AGENT_ID to keep serving that agent, or stop passing one to "
            "use the stored agent."
        )
    else:
        return None

    return (
        f"AGENT_ID ({settings.agent_id}) and {origin} are both configured, and "
        "the two modes are mutually exclusive. A stored agent's prompt, "
        "greeting, tools and voice come from AssemblyAI, so the local agent's "
        "are ignored -- but its run_tool still receives every tool call, and "
        f"the tool names cannot match, so the agent can do nothing. {remedy}"
    )


def build_session_update(
    encoding: str,
    *,
    tune_turns: bool = True,
    voice: str | None = None,
    agent: AgentDefinition | None = None,
) -> dict[str, Any]:
    """Build the session.update message for a transport's audio encoding.

    `agent` is what the session will BE -- prompt, greeting and tools. It
    defaults to the restaurant so every existing caller is unchanged.

    Both input and output formats are pinned to the same encoding. If they
    differ, the agent's reply comes back in a format the transport cannot play
    without transcoding.

    `tune_turns` exists so the caller can retry without the turn-detection
    block: it is the newest and least-documented part of the payload, and a
    single unknown field there is rejected with 1008, taking down the whole
    session rather than just that setting.

    Refuses to build a contradictory payload: see `agent_mode_conflict`. The
    check is here, before `agent` is defaulted, because this function is where
    the two modes used to be silently resolved in favour of one.
    """
    if conflict := agent_mode_conflict(agent):
        raise AgentModeConflict(conflict)

    agent = agent or RESTAURANT

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
            "system_prompt": agent.build_prompt(),
            "greeting": agent.greeting,
            "tools": agent.tools,
            "input": audio_in,
            "output": {
                "type": "audio",
                "voice": voice or agent.voice or settings.agent_voice,
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

    A resume sends no session config, so nothing here can be "resolved in
    favour of" a stored agent -- but the tool calls that follow are dispatched
    into the local agent just the same, so a contradictory deployment is
    exactly as broken on a reconnect as on a first connection.
    """
    if conflict := agent_mode_conflict():
        raise AgentModeConflict(conflict)
    return {"type": SESSION_RESUME, "session_id": session_id}


# The agent this server has always been. Defined last because it names every
# part above it; anything that wants a different agent builds its own
# AgentDefinition and hands it to AgentSession.
RESTAURANT = AgentDefinition(
    name="restaurant",
    display_name=settings.restaurant_name,
    build_prompt=_build_prompt,
    greeting=settings.agent_greeting,
    tools=TOOLS,
    run_tool=run_tool,
)


# Loud at startup, where a deployment variable is still fresh in someone's
# mind -- not on the first call, as a caller listening to an agent that says
# it cannot do anything. Logged rather than raised: an import that raises
# takes down /healthz and the whole process with it, and this same conflict is
# refused again, by name, the moment a session is actually built.
_CONFLICT_AT_IMPORT = agent_mode_conflict()
if _CONFLICT_AT_IMPORT:
    log.error("%s", _CONFLICT_AT_IMPORT)
