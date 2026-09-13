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


def _definition(**overrides) -> AgentDefinition:
    base = {
        "name": "other",
        "build_prompt": lambda: "PROMPT FROM THE OTHER AGENT",
        "greeting": "greeting from the other agent",
        "tools": [{"type": "function", "name": "other_tool", "parameters": {}}],
        "run_tool": lambda name, args: ("ran", False),
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


def test_session_defaults_to_the_restaurant_when_given_no_agent():
    """Every existing caller passes nothing, and must be unchanged."""
    assert AgentSession(_Transport())._agent.name == "restaurant"


def test_session_keeps_the_agent_it_was_given():
    definition = _definition()
    assert AgentSession(_Transport(), agent=definition)._agent is definition


@pytest.mark.asyncio
async def test_session_runs_the_injected_tool_not_the_restaurants():
    """The dispatch consumer, at the session boundary.

    Mutation: `self._agent.run_tool` -> the imported `run_tool` kills this,
    because `restaurant_info` is a real restaurant tool and would succeed.
    """
    called: list[tuple[str, dict]] = []

    def only_tool(name, args):
        called.append((name, args))
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
    assert called == [("restaurant_info", {})]
