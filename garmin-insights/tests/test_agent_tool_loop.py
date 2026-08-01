"""Agent tool loop and portable prompt.

The tool loop's failure modes are all *sticky*: a dangling tool_use block or a
broken role alternation doesn't fail the current call, it 400s every later
call in that session. Those repair paths are the bulk of what's tested here.

``build_portable_prompt`` re-implements the live agent's context assembly for
paste-into-another-LLM use, so it drifts silently whenever the real path
changes. The tests pin the contract it advertises: no tool language, the same
system blocks, and a minified snapshot.
"""

from __future__ import annotations

import json
import types

import pytest

from garmin_insights.agent import HealthAgent
from garmin_insights.config import Settings


# ------------------------------------------------------------------
# Fake Anthropic client
# ------------------------------------------------------------------
class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


def _text(t):
    return _Block("text", text=t)


def _tool_use(id_, name, input_=None):
    return _Block("tool_use", id=id_, name=name, input=input_ or {})


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    """Replays a scripted list of responses and records every request."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        # messages IS the live history list and keeps mutating after the
        # call returns, so snapshot it here.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self._script:
            raise AssertionError("fake client ran out of scripted responses")
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _FakeClient:
    def __init__(self, script):
        self.messages = _FakeMessages(script)


@pytest.fixture
def agent(sample_db):
    a = HealthAgent(Settings(
        sqlite_db_path=sample_db,
        anthropic_api_key="sk-ant-test-not-a-real-key",
        display_name="Test User",
        biological_sex="Female",
    ))
    yield a
    a.close()


def _script(agent, *responses):
    agent._client = _FakeClient(responses)
    return agent._client.messages


# ==================================================================
# _dispatch_tool_call
# ==================================================================
def test_dispatch_runs_a_real_tool(agent):
    out = agent._dispatch_tool_call(
        _tool_use("t1", "get_daily_metrics",
                  {"start_date": "2026-07-01", "end_date": "2026-07-05"}))
    assert isinstance(out, str)
    json.loads(out)


def test_unknown_tool_returns_a_json_error_not_an_exception(agent):
    """The model occasionally invents a tool name. That must come back as a
    tool_result the conversation can continue from, not blow up the turn."""
    out = json.loads(agent._dispatch_tool_call(_tool_use("t1", "no_such_tool")))
    assert "Unknown tool: no_such_tool" in out["error"]


def test_private_attributes_are_not_reachable_as_tools(agent):
    """Dispatch resolves by getattr on the handler, so a name like `_repo`
    would otherwise return a live object rather than a tool result."""
    out = json.loads(agent._dispatch_tool_call(_tool_use("t1", "_repo")))
    assert "error" in out


def test_a_failing_tool_is_reported_as_a_tool_error(agent):
    """A tool raising must not abort the loop — the model needs the error back
    so it can recover or explain."""
    out = json.loads(agent._dispatch_tool_call(
        _tool_use("t1", "get_daily_metrics", {"start_date": 42})))
    assert "error" in out
    assert "get_daily_metrics" in out["error"]


def test_dispatch_tolerates_empty_tool_input(agent):
    out = agent._dispatch_tool_call(_Block("tool_use", id="t1",
                                           name="get_my_baselines", input=None))
    assert isinstance(out, str)


# ==================================================================
# _build_tool_result
# ==================================================================
def test_tool_result_carries_no_cache_control(agent):
    """The API caps cache_control at 4 markers and 3 are already claimed
    (static system, per-day system, tools list). A per-result marker would hit
    the cap on the second tool round and 400."""
    result = agent._build_tool_result("t1", "{}")
    assert result == {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}
    assert "cache_control" not in result


# ==================================================================
# chat loop
# ==================================================================
def test_simple_turn_returns_text(agent):
    _script(agent, _Response([_text("Your RHR looks steady.")], "end_turn"))
    assert agent.chat("how am I doing?") == "Your RHR looks steady."


def test_tool_round_then_answer(agent):
    messages = _script(
        agent,
        _Response([_tool_use("t1", "get_my_baselines")], "tool_use"),
        _Response([_text("Here is the answer.")], "end_turn"),
    )
    assert agent.chat("what are my baselines?") == "Here is the answer."
    assert len(messages.calls) == 2

    # The tool result must be threaded back in as a user-role turn.
    final_messages = messages.calls[-1]["messages"]
    tool_turn = final_messages[-1]
    assert tool_turn["role"] == "user"
    assert tool_turn["content"][0]["tool_use_id"] == "t1"


def test_multiple_tool_calls_in_one_round_all_get_results(agent):
    """Every tool_use block needs a matching tool_result or the next request
    400s with 'tool_use ids not found'."""
    messages = _script(
        agent,
        _Response([_tool_use("t1", "get_my_baselines"),
                   _tool_use("t2", "get_daily_metrics",
                             {"start_date": "2026-07-01",
                              "end_date": "2026-07-02"})], "tool_use"),
        _Response([_text("done")], "end_turn"),
    )
    agent.chat("two things please")

    results = messages.calls[-1]["messages"][-1]["content"]
    assert {r["tool_use_id"] for r in results} == {"t1", "t2"}


def test_loop_stops_after_ten_rounds(agent):
    """An unbounded loop is a runaway spend, not just a hang."""
    messages = _script(agent, *[
        _Response([_tool_use(f"t{i}", "get_my_baselines")], "tool_use")
        for i in range(12)
    ])
    reply = agent.chat("loop forever")

    assert "Maximum tool-calling rounds reached" in reply
    assert len(messages.calls) == 10


def test_history_after_the_round_cap_still_alternates(agent):
    """The loop bails while history ends on a user-role tool_results turn.
    Without a closing assistant turn the next message creates two consecutive
    user turns — a 400 on the following request."""
    _script(agent, *[
        _Response([_tool_use(f"t{i}", "get_my_baselines")], "tool_use")
        for i in range(12)
    ])
    agent.chat("loop forever")
    assert agent._history[-1]["role"] == "assistant"


# ==================================================================
# Failure recovery — the sticky ones
# ==================================================================
def test_api_error_rolls_the_history_back(agent):
    """A dangling user turn with no assistant reply breaks role alternation
    and poisons every later call in the session."""
    agent._history.append({"role": "user", "content": "earlier"})
    agent._history.append({"role": "assistant", "content": "earlier reply"})
    before = list(agent._history)

    _script(agent, RuntimeError("connection reset"))
    reply = agent.chat("this will fail")

    assert "Error communicating with Claude" in reply
    assert agent._history == before, "the failed exchange must leave no residue"


def test_a_later_turn_succeeds_after_an_api_error(agent):
    _script(agent, RuntimeError("boom"))
    agent.chat("fails")

    _script(agent, _Response([_text("recovered")], "end_turn"))
    assert agent.chat("works") == "recovered"
    assert agent._history[-1]["role"] == "assistant"


def test_max_tokens_truncation_strips_dangling_tool_use(agent):
    """Hitting the output cap mid-tool-call leaves tool_use blocks that will
    never get results. Left in place they 400 every later call."""
    _script(agent, _Response(
        [_text("partial answer"), _tool_use("t1", "get_my_baselines")], "max_tokens"))
    reply = agent.chat("long question")

    assert "truncated" in reply.lower()
    assert "partial answer" in reply
    kept = agent._history[-1]["content"]
    assert all(getattr(b, "type", None) != "tool_use" for b in kept)


def test_truncation_with_no_text_drops_the_whole_exchange(agent):
    """Nothing displayable survived, so the exchange is removed entirely
    rather than left as an assistant turn with empty content."""
    base = len(agent._history)
    _script(agent, _Response([_tool_use("t1", "get_my_baselines")], "max_tokens"))
    reply = agent.chat("question")

    assert "truncated" in reply.lower()
    assert len(agent._history) == base


def test_sanitize_keeps_text_and_drops_tool_use():
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "placeholder"}]
    content = [_text("kept"), _tool_use("t1", "x"), _text("also kept")]
    HealthAgent._sanitize_truncated_turn(history, 1, content)

    kept = history[-1]["content"]
    assert [b.text for b in kept] == ["kept", "also kept"]


def test_sanitize_truncates_to_base_when_only_tool_use_remains():
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "placeholder"}]
    HealthAgent._sanitize_truncated_turn(history, 1, [_tool_use("t1", "x")])
    assert len(history) == 1


# ==================================================================
# Per-call effort override
# ==================================================================
def test_effort_override_does_not_mutate_the_shared_params(agent):
    """One agent instance serves concurrent web requests; mutating the shared
    dict would leak a scan's raised effort into unrelated chat turns."""
    before = json.loads(json.dumps(agent._extra_call_params))

    messages = _script(agent, _Response([_text("ok")], "end_turn"))
    agent.chat("scan please", effort="high")

    assert agent._extra_call_params == before
    assert messages.calls[0]["output_config"] == {"effort": "high"}


def test_without_an_override_the_configured_effort_is_sent(agent):
    messages = _script(agent, _Response([_text("ok")], "end_turn"))
    agent.chat("normal question")
    assert messages.calls[0]["output_config"] == {"effort": "low"}


def test_every_request_carries_tools_and_system_blocks(agent):
    messages = _script(agent, _Response([_text("ok")], "end_turn"))
    agent.chat("hello")
    call = messages.calls[0]
    assert call["tools"], "the tool list must be sent"
    assert call["system"], "the system blocks must be sent"
    assert call["thinking"] == agent._thinking


# ==================================================================
# reset / history management
# ==================================================================
def test_reset_clears_the_conversation(agent):
    _script(agent, _Response([_text("ok")], "end_turn"))
    agent.chat("hello")
    assert agent._history

    agent.reset_conversation()
    assert agent._history == []


def test_explicit_history_is_used_instead_of_the_shared_one(agent):
    """The web layer keeps per-session histories; passing one must not touch
    the agent's own."""
    own_before = list(agent._history)
    external: list[dict] = []

    _script(agent, _Response([_text("ok")], "end_turn"))
    agent.chat("hello", history=external)

    assert external, "the passed history should have been extended"
    assert agent._history == own_before


# ==================================================================
# build_portable_prompt
# ==================================================================
def test_portable_prompt_requires_a_message_or_focus(agent):
    with pytest.raises(ValueError, match="requires a message or focus"):
        agent.build_portable_prompt()


def test_portable_prompt_embeds_the_same_system_context(agent):
    prompt = agent.build_portable_prompt(message="how am I doing?")
    assert "Evidence-Tier Output Rules" in prompt
    assert "Test User" in prompt


def test_portable_prompt_removes_tool_language(agent):
    """The receiving chat window has no tools; leaving the capability sentence
    in makes some models attempt phantom tool calls."""
    prompt = agent.build_portable_prompt(message="how am I doing?")
    assert "You have access to tools that query the user's health data" not in prompt
    assert "no tool calls are needed or available" in prompt


def test_portable_prompt_carries_a_data_snapshot(agent):
    prompt = agent.build_portable_prompt(message="how am I doing?")
    assert "DATA SNAPSHOT" in prompt
    assert "restingHeartRate" in prompt


def test_portable_snapshot_json_is_minified(agent):
    """Whitespace is pure token cost in a pasted prompt."""
    prompt = agent.build_portable_prompt(message="q")
    assert '", "' not in prompt.split("DATA SNAPSHOT")[-1][:4000], (
        "snapshot JSON should use compact separators")


@pytest.mark.parametrize("focus", ["morning", "midday", "evening", "night",
                                   "general", "weekly"])
def test_every_scan_focus_builds_a_portable_prompt(agent, focus):
    prompt = agent.build_portable_prompt(focus=focus)
    assert prompt.strip()
    assert "DATA SNAPSHOT" in prompt


def test_explicit_range_adds_a_restriction_header(agent, sample_rows):
    start, end = sample_rows[-20]["date"], sample_rows[-1]["date"]
    prompt = agent.build_portable_prompt(focus="general", start_date=start, end_date=end)
    assert f"Restrict your analysis to the date range {start} → {end}" in prompt


def test_a_one_sided_range_is_resolved_to_a_full_one(agent, sample_rows):
    """A half-filled UI date box used to produce an unbounded snapshot with no
    range note at all."""
    start = sample_rows[-20]["date"]
    prompt = agent.build_portable_prompt(focus="general", start_date=start)
    assert "Restrict your analysis to the date range" in prompt
    assert start in prompt


def test_message_and_focus_are_combined_not_replaced(agent):
    prompt = agent.build_portable_prompt(message="I flew to Tokyo yesterday",
                                         focus="morning")
    assert "I flew to Tokyo yesterday" in prompt
