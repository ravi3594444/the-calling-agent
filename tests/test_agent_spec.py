"""The relay carries the agent it was GIVEN, not the one it imports.

`AgentSession._agent` feeds three separate consumers -- the system prompt, the
tool declarations and the voice -- and a test that checked one of them would
pass while the other two silently fell back to the restaurant. So each is
mutated on its own.

Tool DISPATCH, the fourth consumer, is covered in test_experience.py by
`_agent_running`, which constructs the session with the tool under test.
"""

import pytest

from calling_agent import protocol as p
from calling_agent.agent_config import build_session_update
from calling_agent.agent_spec import AgentDefinition
from calling_agent.config import settings
from calling_agent.session import AgentSession
from calling_agent.transport.base import AudioTransport
from tests.conftest import make_business


def _definition(**overrides) -> AgentDefinition:
    base = {
        "name": "other",
        "build_prompt": lambda: "PROMPT FROM THE OTHER AGENT",
        "greeting": "greeting from the other agent",
        "tools": [{"type": "function", "name": "other_tool", "parameters": {}}],
        "run_tool": lambda name, args, call_id="": ("ran", False),
    }
    return AgentDefinition(**{**base, **overrides})


class _Transport(AudioTransport):
    encoding = p.ENCODING_PCM
    sample_rate = p.PCM_SAMPLE_RATE

    async def recv_audio(self):  # pragma: no cover - never pumped here
        yield ""

    async def send_audio(self, chunk_b64: str) -> None: ...
    async def clear(self) -> None: ...
    async def send_event(self, event: dict) -> None: ...
    async def close(self) -> None: ...


def test_injected_prompt_reaches_the_opening_payload():
    """Mutation: `agent.build_prompt()` -> `_build_prompt()` kills this."""
    session = build_session_update(p.ENCODING_PCM, agent=_definition())["session"]
    assert session["system_prompt"] == "PROMPT FROM THE OTHER AGENT"
    # Not merely "some prompt": the restaurant's must be absent, or a payload
    # carrying both would pass on the assertion above alone.
    assert "Meera" not in session["system_prompt"]


def test_injected_greeting_reaches_the_opening_payload():
    """Mutation: `agent.greeting` -> `settings.agent_greeting` kills this."""
    session = build_session_update(p.ENCODING_PCM, agent=_definition())["session"]
    assert session["greeting"] == "greeting from the other agent"


def test_injected_tools_replace_the_restaurant_tools():
    """Mutation: `agent.tools` -> `TOOLS` kills this."""
    session = build_session_update(p.ENCODING_PCM, agent=_definition())["session"]
    assert [tool["name"] for tool in session["tools"]] == ["other_tool"]


def test_agent_voice_is_used_when_the_definition_names_one():
    """Mutation: dropping `agent.voice` from the chain kills this."""
    session = build_session_update(
        p.ENCODING_PCM, agent=_definition(voice="diego")
    )["session"]
    assert session["output"]["voice"] == "diego"


def test_per_call_voice_still_outranks_the_definition():
    """?voice= is a human comparing voices by ear, so it wins over both."""
    session = build_session_update(
        p.ENCODING_PCM, voice="sophie", agent=_definition(voice="diego")
    )["session"]
    assert session["output"]["voice"] == "sophie"


def test_definition_voice_outranks_the_configured_default(monkeypatch):
    """An agent that names a voice is not overridden by AGENT_VOICE."""
    monkeypatch.setattr(settings, "agent_voice", "james")
    session = build_session_update(
        p.ENCODING_PCM, agent=_definition(voice="diego")
    )["session"]
    assert session["output"]["voice"] == "diego"


def test_session_defaults_to_the_default_business_when_given_no_agent():
    """Passing no agent resolves DEFAULT_BUSINESS_SLUG, not a module constant."""
    assert AgentSession(_Transport())._agent.name == "business:default-test-venue"


def test_session_keeps_the_agent_it_was_given():
    definition = _definition()
    assert AgentSession(_Transport(), agent=definition)._agent is definition


@pytest.mark.asyncio
async def test_session_runs_the_injected_tool_not_the_restaurants():
    """The dispatch consumer, at the session boundary.

    Mutation: `self._agent.run_tool` -> the imported `run_tool` kills this,
    because `restaurant_info` is a real restaurant tool and would succeed.
    """
    called: list[tuple[str, dict, str]] = []

    def only_tool(name, args, call_id=""):
        called.append((name, args, call_id))
        return "from the other agent", False

    session = AgentSession(_Transport(), agent=_definition(run_tool=only_tool))

    class _Upstream:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            import json

            self.sent.append(json.loads(raw))

    upstream = _Upstream()
    await session._handle_tool_call(
        upstream, {"name": "restaurant_info", "call_id": "c1", "arguments": {}}
    )
    # The call id reaches the tool: it is what lets an agent tell a retry of
    # one call from a second, different call in the same conversation.
    assert called == [("restaurant_info", {}, "c1")]


# --- AGENT_FACTORY: another codebase serving its own agent through this relay ---

_FACTORY_CALLS: list[int] = []


def _factory_for_tests(params):
    """Module-level so AGENT_FACTORY can address it as a real import path.

    The prompt captures the count AT BUILD TIME. A lambda reading the list when
    called instead reports the latest count for every agent ever built, which
    is the bug this test exists to catch -- so the double must not have it.
    """
    _FACTORY_CALLS.append(params)
    numero = len(_FACTORY_CALLS)
    return _definition(build_prompt=lambda: f"call #{numero}")


def _broken_factory(params):
    raise RuntimeError("this deployment variable has a typo in it")


def _not_an_agent(params):
    return {"name": "a dict is not an AgentDefinition"}


def test_no_factory_configured_means_the_restaurant():
    from calling_agent.main import _build_agent

    assert _build_agent() is None


def test_factory_runs_once_per_connection_not_once_per_process(monkeypatch):
    """The one that matters: a per-caller agent must not outlive its caller.

    Mutation: resolving AGENT_FACTORY at import and reusing the result kills
    this, because the second call would return the first call's prompt.
    """
    from calling_agent.main import _build_agent

    _FACTORY_CALLS.clear()
    monkeypatch.setattr(
        settings, "agent_factory", "test_agent_spec:_factory_for_tests"
    )
    first = _build_agent()
    second = _build_agent()
    assert len(_FACTORY_CALLS) == 2
    assert first.build_prompt() == "call #1"
    assert second.build_prompt() == "call #2"


def test_a_broken_factory_still_answers_the_phone(monkeypatch):
    """A typo in a deployment variable must not be a phone that rings out."""
    from calling_agent.main import _build_agent

    monkeypatch.setattr(
        settings, "agent_factory", "test_agent_spec:_broken_factory"
    )
    assert _build_agent() is None


def test_a_factory_naming_nothing_falls_back(monkeypatch):
    from calling_agent.main import _build_agent

    monkeypatch.setattr(settings, "agent_factory", "calling_agent.main:no_such_name")
    assert _build_agent() is None


def test_a_factory_returning_the_wrong_type_falls_back(monkeypatch):
    """Otherwise the failure surfaces as an AttributeError mid-call."""
    from calling_agent.main import _build_agent

    monkeypatch.setattr(settings, "agent_factory", "test_agent_spec:_not_an_agent")
    assert _build_agent() is None


def test_the_factory_receives_this_connection_s_query_parameters(monkeypatch):
    """The only channel by which who-is-calling reaches an agent without going
    through the conversation -- so a caller cannot talk their way into another
    identity, and the model never sees the value.

    Mutation: calling `factory()` with no argument kills this.
    """
    from calling_agent.main import _build_agent

    _FACTORY_CALLS.clear()
    monkeypatch.setattr(
        settings, "agent_factory", "test_agent_spec:_factory_for_tests"
    )
    _build_agent({"telefono": "5493511234567"})
    assert _FACTORY_CALLS == [{"telefono": "5493511234567"}]


def test_a_connection_with_no_parameters_still_builds(monkeypatch):
    from calling_agent.main import _build_agent

    _FACTORY_CALLS.clear()
    monkeypatch.setattr(
        settings, "agent_factory", "test_agent_spec:_factory_for_tests"
    )
    assert _build_agent() is not None
    assert _FACTORY_CALLS == [{}]


@pytest.mark.asyncio
async def test_two_different_tool_calls_get_two_different_ids():
    """What the call id is FOR.

    An agent whose tools write (an order, a booking) must tell a retry of one
    call from a second, different call. The session id cannot do it -- it is
    the same for every call in the conversation -- so an agent keyed on it
    answers the caller's second order with their first.

    Mutation: passing a constant, or the session id, in place of call_id kills
    this.
    """
    vistos: list[str] = []

    def recordar(name, args, call_id=""):
        vistos.append(call_id)
        return "ok", False

    session = AgentSession(_Transport(), agent=_definition(run_tool=recordar))

    class _Upstream:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            import json

            self.sent.append(json.loads(raw))

    upstream = _Upstream()
    await session._handle_tool_call(
        upstream, {"name": "book_table", "call_id": "call-1", "arguments": {}}
    )
    await session._handle_tool_call(
        upstream, {"name": "book_table", "call_id": "call-2", "arguments": {}}
    )
    assert vistos == ["call-1", "call-2"]


@pytest.mark.asyncio
async def test_a_tool_call_with_no_id_still_runs():
    """The provider may omit it; a missing id is a weaker hint, not a failure."""
    vistos: list[str] = []

    def recordar(name, args, call_id=""):
        vistos.append(call_id)
        return "ok", False

    session = AgentSession(_Transport(), agent=_definition(run_tool=recordar))

    class _Upstream:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            import json

            self.sent.append(json.loads(raw))

    await session._handle_tool_call(_Upstream(), {"name": "book_table", "arguments": {}})
    assert vistos == [""]


def test_the_factory_sees_the_resume_id_of_a_reconnect(monkeypatch):
    """A reconnect is a new connection, so the factory runs again -- but the
    upstream session it rejoins keeps the FIRST agent's prompt and tools.

    A factory whose answer varies between those two calls gets the first
    agent's tool calls dispatched into the second agent's tools. `resume` is
    what lets it stay stable, so it has to reach the factory.

    Mutation: filtering `resume` out of the parameters kills this.
    """
    from calling_agent.main import _build_agent

    _FACTORY_CALLS.clear()
    monkeypatch.setattr(settings, "agent_factory", "test_agent_spec:_factory_for_tests")
    _build_agent({"resume": "s-1", "telefono": "549351"})
    assert _FACTORY_CALLS == [{"resume": "s-1", "telefono": "549351"}]


def test_experience_names_the_configured_agent_not_the_restaurant(monkeypatch):
    """A relay serving someone else's agent must not label their product with
    the restaurant's name.

    Mutation: reading settings.restaurant_name first kills this.
    """
    from fastapi.testclient import TestClient

    from calling_agent.main import app

    monkeypatch.setattr(settings, "restaurant_name", "The Copper Kettle")
    monkeypatch.setattr(settings, "agent_factory", "test_agent_spec:_dairy_factory")
    body = TestClient(app).get("/experience").json()
    assert body["restaurant"] == "Lácteos Plus"
    assert body["agent"] == "dairy"


def test_experience_falls_back_to_the_default_business_with_no_factory(monkeypatch):
    from fastapi.testclient import TestClient

    from calling_agent.main import app

    monkeypatch.setattr(settings, "agent_factory", "")
    body = TestClient(app).get("/experience").json()
    assert body["restaurant"] == "The Copper Kettle"
    assert body["agent"] == "business:default-test-venue"


def _dairy_factory(params):
    return _definition(name="dairy", display_name="Lácteos Plus")


def test_the_diagnostic_payload_carries_the_configured_agent():
    """Mutation: dropping `agent=` from the diagnostic payload kills this.

    Without it /diagnose validates the restaurant's prompt and tools while
    every live call sends the configured agent's -- a green report next to a
    phone that is refused on every session.
    """
    from calling_agent.diagnostics import _payload_de_la_sesion

    sesion = _payload_de_la_sesion(_definition(build_prompt=lambda: "DAIRY PROMPT"))["session"]
    assert sesion["system_prompt"] == "DAIRY PROMPT"
    assert [tool["name"] for tool in sesion["tools"]] == ["other_tool"]


def test_diagnose_reports_which_agent_it_built(monkeypatch):
    """Mutation: calling run_diagnostics() without the agent kills this."""
    from fastapi.testclient import TestClient

    from calling_agent.main import app

    monkeypatch.setattr(settings, "assemblyai_api_key", "")  # no billable call
    monkeypatch.setattr(settings, "agent_factory", "test_agent_spec:_dairy_factory")
    body = TestClient(app).get("/diagnose").json()
    paso = next(s for s in body["steps"] if s["step"] == "agent")
    assert "dairy" in paso["detail"]


def test_the_prompt_carries_the_connect_date_so_no_round_trip_is_needed(business):
    """The year has to be in the prompt, or the model asks a tool for it.

    Asking now() before every booking cost a tool call, a wait for reply.done
    and a second inference -- several seconds of phone silence to learn
    something that had not changed since the caller said hello.
    """
    from datetime import UTC, datetime

    from calling_agent import formatting
    from calling_agent.agent_config import build_prompt_for

    today = formatting.local(business, datetime.now(UTC)).date()
    prompt = " ".join(build_prompt_for(business).split())

    assert today.isoformat() in prompt, "the model still has to ask what year it is"
    assert "now() only when" in prompt, "nothing stops it asking anyway"


# --- what the agent knows about the place ------------------------------------


def test_venue_facts_reach_the_prompt_and_only_the_ones_that_are_set():
    """An empty field is not "no". A venue that left parking blank may have
    plenty, so the block lists only what the owner wrote and the standing
    rule -- offer to check -- covers the rest."""
    from calling_agent.agent_config import build_prompt_for, venue_block_for

    business = make_business(
        config={"venue": {"parking": "Free on the street after 6", "children": ""}}
    )
    block = venue_block_for(business)
    assert "Parking: Free on the street after 6" in block
    assert "Children" not in block, "a blank field must not be listed as anything"
    assert "offer to check" in block

    prompt = " ".join(build_prompt_for(business).split())
    assert "ABOUT THE PLACE" in prompt

    silent = make_business()
    assert venue_block_for(silent) == "", "nothing set, nothing said"
    assert "ABOUT THE PLACE" not in build_prompt_for(silent)


def test_business_info_answers_from_the_venue_fields():
    from calling_agent import agent_tools

    business = make_business(config={"venue": {"wheelchair_access": "Step-free from the street"}})
    said = agent_tools.business_info(business, {})
    assert "Wheelchair access: Step-free from the street." in said
    assert said.data["venue"] == {"Wheelchair access": "Step-free from the street"}


def test_things_people_ask_reach_the_prompt():
    """The venue block covers what every venue is asked; this covers what THIS
    one is asked -- the questions no fixed field could have anticipated."""
    from calling_agent.agent_config import build_prompt_for, faq_block_for

    business = make_business(
        config={
            "venue": {
                "faq": [
                    {"q": "Do you do gift vouchers?", "a": "Yes, any amount, at the bar."},
                    {"q": "Can I bring my dog?", "a": "Outside tables only."},
                ]
            }
        }
    )
    block = faq_block_for(business)
    assert "Do you do gift vouchers?" in block
    assert "Yes, any amount, at the bar." in block
    assert "Outside tables only." in block
    assert "THINGS PEOPLE ASK" in build_prompt_for(business)

    silent = make_business()
    assert faq_block_for(silent) == "", "nothing set, nothing said"
    assert "THINGS PEOPLE ASK" not in build_prompt_for(silent)


def test_the_prompt_drops_a_half_written_pair_even_if_one_reaches_it():
    """The second of two guards. Save-time validation refuses a half-written
    pair, so this is unreachable through the dashboard -- it covers a document
    written straight to the database, and the rule it enforces is the reason
    the first guard exists: a question in front of the agent with no answer
    beside it is an invitation to supply one.

    Built from a stub rather than make_business precisely BECAUSE the fixture
    validates and would refuse this; that refusal is the other test.
    """
    from types import SimpleNamespace

    from calling_agent.agent_config import faq_block_for

    block = faq_block_for(
        SimpleNamespace(
            config={
                "venue": {
                    "faq": [
                        {"q": "Do you do gift vouchers?", "a": "Yes, at the bar."},
                        {"q": "Can I bring my dog?", "a": "   "},
                        {"q": "", "a": "An answer nobody asked for."},
                    ]
                }
            }
        )
    )
    assert "gift vouchers" in block
    assert "dog" not in block, "a question with no answer must not reach the agent"
    assert "nobody asked for" not in block
    assert faq_block_for(SimpleNamespace(config={})) == "", "no venue section at all"


def test_a_question_with_no_answer_is_refused_at_save():
    """Refused rather than quietly dropped: the owner typed it and meant it,
    and a form that swallows half a row teaches nobody anything."""
    from calling_agent import business_config

    for faq, expected in [
        ([{"q": "Do you do gift vouchers?", "a": ""}], "has no answer"),
        ([{"q": "  ", "a": "Yes we do."}], "an answer with no question"),
    ]:
        with pytest.raises(business_config.ConfigError) as caught:
            business_config.validate({"venue": {"faq": faq}})
        assert expected in str(caught.value)

    # Both halves present is fine, and so is no list at all.
    business_config.validate({"venue": {"faq": [{"q": "Parking?", "a": "Out the back."}]}})
    business_config.validate({"venue": {}})


def test_saving_the_questions_leaves_the_other_venue_facts_alone():
    """The two cards write the same config section. If saving one replaced the
    section rather than merging into it, filling in the questions would wipe
    the parking answer -- and the owner would find out from a caller."""
    from calling_agent import businesses

    business = make_business(config={"venue": {"parking": "Free after six"}})
    businesses.save_config(
        business.id, "venue", {"faq": [{"q": "Vouchers?", "a": "Yes, at the bar."}]}
    )
    after = businesses.by_id(business.id).config["venue"]
    assert after["parking"] == "Free after six", "the fixed fields must survive"
    assert after["faq"] == [{"q": "Vouchers?", "a": "Yes, at the bar."}]
