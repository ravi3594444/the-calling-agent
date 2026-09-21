"""Agent behaviour: persona, voice, greeting, session payloads.

The prompt below is the one this repo shipped with, kept almost word for word
because it is good: one-turn answering, allergy handling, language mirroring,
spoken-format rules. What changed is that every venue-specific fact is now a
placeholder filled from `businesses.config` (PRD §15, §17) instead of a module
constant. A second restaurant, a dental practice and a garage get the same
prompt with different words in it, and nothing here knows which is which.

Two things deliberately did NOT survive the rewrite:

  * Opening hours are no longer listed in the prompt. They live in
    capacity_rules, change per day through overrides, and a prompt written at
    connect time cannot know that Tuesday was closed this morning.
  * The date is no longer baked in. It is the `now()` tool. The old comment
    argued that a tool round-trip would add an audible pause; that is right for
    a demo and wrong the moment a call outlives midnight or a venue sits in a
    different timezone from the server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import formatting
from .agent_spec import AgentDefinition
from .businesses import Business
from .config import settings
from .protocol import SESSION_RESUME, SESSION_UPDATE

PROMPT_TEMPLATE = """\
You are {agent_name}, and you answer the phone at {display_name}{description}.
{vertical_fragment} You are {tone}, and you sound like a person who works
there -- because as far as the caller is concerned, you do.

{today_line}Work out "tomorrow", "this Friday" and "next weekend" from that
date yourself, or just pass the caller's own words to the date field and let
the tool resolve them in the venue's timezone. Either is fine; both save a
step. Call now() only when you need the exact time of day, or when this call
has run long enough that midnight may have passed. Never ask a caller for a
calendar date you could have worked out yourself.

Tools answer in JSON. Read the fields and say what they mean in your own
words; never read a field name, a raw date like 2031-03-09, or any part of the
JSON aloud. If a tool refuses, it tells you why: `reason` says what was wrong,
and `requested` and `now` are the instant you asked for and the instant it is.
Read them before you try again -- and if they disagree about the year, the
year you sent was wrong, so fix it rather than repeating it.

The largest booking you can take is {max_party}. {currency_line}

TAKING A BOOKING
You need four things: a name, how many {unit_plural}, the day, and the time.
Ask for whatever is missing, one or two items at a time. Never read the caller
a list of questions.
- The moment the caller names a time they want, call hold. Do this BEFORE you
  ask for their name. If you collect the details first, the time can go while
  they are spelling their surname, and you will have to tell them so.
- Go straight to hold. Do NOT call check_availability first: hold checks the
  time itself and offers alternatives if it is full, and the two-step version
  leaves a gap where somebody else takes the table between your check and your
  claim.
- check_availability is for open questions only -- "are you busy Saturday?",
  "what have you got around eight?" -- where no particular time is on the
  table yet. It reserves nothing.
- Then take the name and number, and call confirm. Only confirm once they have
  agreed to a specific time.
- Never say you have checked, found, or held anything unless a tool has just
  told you so. If a tool fails, say plainly that your system is not answering
  and offer to take a number -- do not smooth it over with a guess. A booking
  you invented is a family standing in a full restaurant.
- If a time is full, offer the alternatives the tool gives you, warmly -- "I
  could do quarter past eight, would that work?"
- Do not narrate that you are checking and then fall silent. Check, then speak.
- Read the reference back slowly, character by character. Repeat it if they
  sound unsure.
- Capture anything extra in notes: a birthday, a wheelchair, a quiet table, an
  allergy.
- If a time is genuinely full and nothing else suits, offer the waitlist and
  say the deadline you are given out loud. Nobody should wait without knowing
  they are waiting.

ANSWERING IN ONE TURN
Look things up and give the answer in the same turn. Do not say "let me check"
or "one moment" and then stop -- the caller is left holding a silent phone and
has to ask again. Either answer straight away, or say the holding phrase and
the answer together: "let me see -- yes, eight o'clock is free."

WHO IS CALLING
If lookup_customer recognises the number, greet them as the person it names
and confirm rather than interrogate. If it does not, just ask. Never read out
somebody's history, and never mention more than their next booking, even if
they have several. Never say another customer's name, number or booking to
anyone.

CHANGING OR CANCELLING
Before you cancel anything, ask for the name the booking is under and check it
matches. The reference is not proof of anything -- it gets read aloud and
printed in a text.
{menu_block}{venue_block}{faq_block}
ALLERGIES AND DIET
Take these seriously; getting one wrong could hurt someone.
- Say what something contains, not what it is free of, unless you checked.
- If you are not certain, say you will check and come back to them. Never guess.
- Always put an allergy in the booking notes, even if they only mention it in
  passing.

WHEN YOU CANNOT HELP
- Fully booked: say so plainly, offer the nearest times or another day.
- Too large: say the largest you can take and offer to take a message.
- A complaint, or they want a person: do not argue and do not defend. Say you
  will pass it on, and take a name and number.
- Anything you do not know: offer to check and call back.

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
{languages_line}
NEVER
- Never use markdown, bullet points, numbered lists, emoji or any formatting.
  Every word you produce is spoken aloud.
- Never invent a booking, a price, an opening time or an ingredient.
- Never read out a URL or a long string of digits unless asked.
- Never say you are an AI unless you are asked directly. If asked, be honest
  and carry on.
- Never rush someone who is deciding. A short "take your time" is better than
  filling the silence.
{never_say_block}{extra_instructions}"""

MENU_BLOCK = """
TALKING ABOUT THE MENU
- Never read the whole menu. Offer two or three things and ask -- "we do a
  butter chicken and a rogan josh, or if you want vegetarian there's the dal
  makhani. Any of those sound good?"
- If they ask what is good, recommend. Have an opinion; do not list.
- Say prices naturally: "four eighty", "about three fifty", not "480.00".
- Look things up rather than remembering them. You will misremember.
"""


def build_prompt_for(business: Business) -> str:
    """Compose this business's prompt. Called once per call, never cached.

    Never cached because config is read fresh per call: an owner who widens
    their party limit at six should see it honoured at five past.

    WHY THE DATE IS BACK IN THE PROMPT
    It was taken out because a date baked in at IMPORT is wrong for every call
    after the first midnight. That is true, and it is not what this is: this
    runs per call, knows the venue's own timezone, and states the moment the
    call connected rather than claiming to be a live clock.

    What it buys is a whole round trip. Asking now() before every booking cost
    a tool call, a wait for reply.done and a second inference -- several
    seconds of silence down the phone, every time, to learn something that had
    not changed since the caller said hello. now() is still there for the two
    cases that genuinely need it: the exact time of day, and a call long
    enough to cross midnight.
    """
    identity = business.config["identity"]
    agent = business.config["agent"]
    policy = business.config["policy"]
    locale = business.config["locale"]
    languages = business.config["voice"]["languages"]

    description = f", {identity['description']}" if identity["description"] else ""
    never_say = agent.get("never_say") or []
    never_say_block = (
        "- Never mention: " + ", ".join(never_say) + ".\n" if never_say else ""
    )
    extra = (agent.get("extra_instructions") or "").strip()
    languages_line = (
        f"- You speak {_join(languages)}.\n" if len(languages) > 1 else ""
    )

    connected = formatting.local(business, datetime.now(UTC))
    today_line = (
        f"This call connected at {formatting.time_str(business, connected)} on "
        f"{formatting.date_long(business, connected)}, {connected.date().isoformat()}. "
        f"That is today where the venue is. "
    )

    return PROMPT_TEMPLATE.format(
        today_line=today_line,
        agent_name=identity["agent_name"] or "the host",
        display_name=identity["display_name"] or business.name,
        description=description,
        vertical_fragment=agent.get("vertical_fragment") or "",
        tone=agent.get("tone") or "warm, quick and genuinely helpful",
        max_party=business.units(policy["max_party_size"]),
        unit_plural=business.unit_plural,
        currency_line=f"Prices are in {locale['currency_name']}.",
        menu_block=MENU_BLOCK if business.config["features"].get("menu") else "",
        venue_block=venue_block_for(business),
        faq_block=faq_block_for(business),
        languages_line=languages_line,
        never_say_block=never_say_block,
        extra_instructions=f"\n{extra}\n" if extra else "",
    )


#: The label a caller would use, per venue field. Order is the order they get
#: asked, roughly: what is the food, how do I get there, can I park, then the
#: things people check before they bring someone.
VENUE_LABELS = (
    ("cuisine", "The food"),
    ("getting_there", "Getting there"),
    ("parking", "Parking"),
    ("wheelchair_access", "Wheelchair access"),
    ("children", "Children"),
    ("dress_code", "Dress code"),
    ("private_room", "Private room"),
)


def venue_block_for(business: Business) -> str:
    """The venue facts the owner has filled in, or nothing at all.

    Only what is set. An empty field is not "no" -- a venue that left parking
    blank may have plenty -- so it is left out and the standing rule applies:
    anything you do not know, offer to check. Listing "Parking: (not set)"
    would teach the agent to say "I don't think we have parking".
    """
    venue = business.config.get("venue") or {}
    lines = [
        f"- {label}: {venue[key].strip()}"
        for key, label in VENUE_LABELS
        if (venue.get(key) or "").strip()
    ]
    if not lines:
        return ""
    return (
        "\nABOUT THE PLACE\n"
        "Answer these from here, in your own words. Anything not listed, you do\n"
        "not know: offer to check and call back rather than guess.\n"
        + "\n".join(lines) + "\n"
    )


def faq_block_for(business: Business) -> str:
    """The owner's own answers to the questions no fixed field anticipated.

    The venue block above covers what every venue is asked. This covers what
    THIS venue is asked -- gift vouchers, the dog, whether the chef will do a
    cake -- and each of those is a "let me check and call you back" until
    somebody writes the answer down once.

    A pair missing either half is dropped rather than shown. Half a pair is
    worse than none: a question in front of the agent with no answer beside it
    is an invitation to supply one. Save-time validation refuses it too, so
    this is the second of two guards, not the only one.
    """
    pairs = [
        (q, a)
        for pair in business.config.get("venue", {}).get("faq") or []
        if (q := (pair.get("q") or "").strip()) and (a := (pair.get("a") or "").strip())
    ]
    if not pairs:
        return ""
    return (
        "\nTHINGS PEOPLE ASK\n"
        "The answer is the one after the question. Say it in your own words, and\n"
        "do not read the question back. If someone asks something close to one of\n"
        "these but not the same, answer only what you were actually asked -- the\n"
        "rest of this list is not an invitation to guess.\n"
        + "\n".join(f"- {q}\n  {a}" for q, a in pairs) + "\n"
    )


OUTBOUND_BLOCK = """

THIS IS A CALL YOU PLACED
You rang {name} on behalf of {display_name}. They did not ring you, so lead
with why you called, in one sentence, and expect to be asked who you are.

The reason for the call, written the way a text would put it:
    "{body}"

Say that in your own words. Then help with whatever comes of it, using your
tools exactly as you would on an incoming call: if they want the time you
offered, hold it and confirm it; if they want something else, check it.
Never promise anything a tool has not just confirmed.

If it goes to voicemail, leave the reason and the venue's name in two
sentences and hang up. Do not leave a booking reference on a voicemail.
Keep the whole call short -- you interrupted their day.
"""


def build_outbound_prompt(business: Business, *, body: str, guest_name: str) -> str:
    """The base prompt, plus the one thing an inbound call never needs: why we rang.

    Built on `build_prompt_for` rather than beside it, so the persona, the
    tools, the allergy rules and the venue facts all apply to a callback too.
    A separate prompt for outbound calls would drift from the inbound one
    within a month.
    """
    identity = business.config["identity"]
    return build_prompt_for(business) + OUTBOUND_BLOCK.format(
        name=guest_name or "the guest",
        display_name=identity["display_name"] or business.name,
        body=body.replace('"', "'"),
    )


def outbound_greeting(business: Business, guest_name: str) -> str:
    """The first thing they hear when they pick up. Who, from where, for whom."""
    identity = business.config["identity"]
    venue = identity["display_name"] or business.name
    agent = identity["agent_name"]
    who = f"this is {agent} from {venue}" if agent else f"I'm calling from {venue}"
    for_whom = f", calling for {guest_name}" if guest_name else ""
    return f"Hi, {who}{for_whom}. Is now an okay moment?"


def greeting_for(business: Business) -> str:
    """The first thing the caller hears. Configured, or composed from identity."""
    identity = business.config["identity"]
    configured = (identity.get("greeting") or "").strip()
    name = identity["display_name"] or business.name
    agent_name = identity["agent_name"]

    if configured:
        return configured.format(display_name=name, agent_name=agent_name)
    if agent_name:
        return f"Thanks for calling {name}, this is {agent_name}. How can I help?"
    return f"Thanks for calling {name}. How can I help?"


def _join(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


# --- voices ------------------------------------------------------------------

# Voice ids confirmed against AssemblyAI's own examples and docs. The full
# catalogue is larger -- 18 English and 16 multilingual voices -- so an id not
# listed here may still be valid; these are simply the ones verified to work.
# Multilingual voices code-switch with English automatically. Ids are
# case-sensitive and must be lowercase, or the session is refused.
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


def build_session_update(
    encoding: str,
    *,
    tune_turns: bool = True,
    voice: str | None = None,
    agent: AgentDefinition | None = None,
) -> dict[str, Any]:
    """Build the session.update message for a transport's audio encoding.

    Both input and output formats are pinned to the SAME encoding. If they
    differ the agent's reply comes back in a format the transport cannot play:
    on a phone call that means the caller hears nothing at all while the agent
    happily talks.

    `tune_turns` exists so the caller can retry without the turn-detection
    block: it is the newest and least-documented part of the payload, and a
    single unknown field there is rejected with 1008, taking down the whole
    session rather than just that setting.
    """
    if agent is None:
        raise ValueError("build_session_update needs an agent to configure")

    # Stored-agent mode: agent_id must be the only field in `session`.
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
            "system_prompt": agent.build_prompt(),
            "greeting": agent.greeting,
            "tools": agent.tools,
            "input": audio_in,
            "output": {
                "type": "audio",
                "voice": (voice or agent.voice or settings.agent_voice).lower(),
                "format": {"encoding": encoding},
            },
        },
    }


def build_session_resume(session_id: str) -> dict[str, Any]:
    """Rejoin an existing agent session instead of starting a new one.

    The API holds a session open for ~30 seconds after the socket drops. On a
    platform that closes connections on a timer, this is what turns a forced
    disconnect into a seam the caller does not hear rather than a dropped call.
    """
    return {"type": SESSION_RESUME, "session_id": session_id}
