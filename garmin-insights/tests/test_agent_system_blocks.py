"""Agent system-prompt assembly invariants.

The system prompt is built in two parts: a static prefix cached for the life of
the agent, and day-varying blocks appended per call. Two properties matter and
neither is visible in normal use:

* **Exactly two cache breakpoints.** The API caps `cache_control` markers at
  four; a third block silently added here would be a runtime 400 on every
  request, not a test failure.
* **A byte-stable static prefix.** Prompt caching keys on the exact prefix
  bytes. If anything day-varying leaks into it, every call misses cache and
  the system-prompt token spend jumps roughly 5x — with no error to notice.

Both are cheap to assert and expensive to discover in production.
"""

from __future__ import annotations

import types
from datetime import datetime

import pandas as pd
import pytest

from garmin_insights.agent import HealthAgent, _model_supports_adaptive
from garmin_insights.config import Settings


def _agent(sample_db, **overrides) -> HealthAgent:
    kwargs = {
        "sqlite_db_path": sample_db,
        "anthropic_api_key": "sk-ant-test-not-a-real-key",
        "display_name": "Test User",
        "biological_sex": "Female",
    }
    kwargs.update(overrides)
    return HealthAgent(Settings(**kwargs))


@pytest.fixture
def agent(sample_db):
    a = _agent(sample_db)
    yield a
    a.close()


def _breakpoints(blocks) -> list[int]:
    return [i for i, b in enumerate(blocks) if "cache_control" in b]


# ==================================================================
# Cache breakpoints
# ==================================================================
def test_exactly_two_cache_breakpoints(agent):
    """The API allows at most four; we deliberately spend two. A third block
    gaining a marker would 400 every request."""
    blocks = agent._system_for_call()
    assert len(_breakpoints(blocks)) == 2


def test_breakpoints_sit_on_the_last_static_and_last_daily_block(agent):
    blocks = agent._system_for_call()
    marks = _breakpoints(blocks)
    static_len = len(agent._system)

    # One closes the static prefix, one closes the whole prompt.
    assert marks[0] == static_len - 1
    assert marks[1] == len(blocks) - 1


def test_breakpoint_count_holds_for_a_male_user(sample_db):
    """Male users get no cycle block, so the daily section is shorter — the
    marker must still land on whatever the last daily block is."""
    a = _agent(sample_db, biological_sex="Male")
    try:
        blocks = a._system_for_call()
        assert len(_breakpoints(blocks)) == 2
        assert _breakpoints(blocks)[1] == len(blocks) - 1
    finally:
        a.close()


def test_breakpoint_count_holds_with_no_identity_configured(sample_db):
    """No name and no sex means no identity block, shortening the static
    prefix. The marker must follow it rather than a fixed index."""
    a = _agent(sample_db, display_name="", biological_sex="")
    try:
        blocks = a._system_for_call()
        assert len(_breakpoints(blocks)) == 2
        assert _breakpoints(blocks)[0] == len(a._system) - 1
    finally:
        a.close()


def test_every_block_is_a_well_formed_text_block(agent):
    for block in agent._system_for_call():
        assert block["type"] == "text"
        assert isinstance(block["text"], str) and block["text"].strip()


# ==================================================================
# Static prefix stability — the thing prompt caching depends on
# ==================================================================
def test_static_prefix_is_byte_identical_across_calls(agent):
    """A cache hit requires the prefix bytes to match exactly. Anything
    day-varying leaking in here silently ends caching."""
    static_len = len(agent._system)
    first = agent._system_for_call()[:static_len]
    second = agent._system_for_call()[:static_len]
    assert first == second


def test_static_prefix_is_not_rebuilt_per_call(agent):
    """_system_for_call must reuse the cached list, not regenerate it — the
    identity and tier blocks are expensive and must not drift."""
    static_len = len(agent._system)
    blocks = agent._system_for_call()
    assert [b["text"] for b in blocks[:static_len]] == [b["text"] for b in agent._system]


def test_appending_daily_blocks_does_not_mutate_the_cached_prefix(agent):
    """_system_for_call adds a cache_control marker to the last daily block; if
    it did that in place on a shared dict the prefix would accumulate markers."""
    before = [dict(b) for b in agent._system]
    agent._system_for_call()
    agent._system_for_call()
    assert agent._system == before
    assert len(_breakpoints(agent._system)) == 1


def test_static_prefix_carries_the_knowledge_base_and_tier_rules(agent):
    text = "\n".join(b["text"] for b in agent._system)
    assert "Evidence-Tier Output Rules" in text
    assert "illness-like recovery strain pattern" in text
    assert "load-spike context signal" in text


# ==================================================================
# Identity block
# ==================================================================
def test_identity_block_names_the_user(agent):
    block = agent._identity_block()
    assert "Test User" in block["text"]
    assert "Female" in block["text"]


def test_male_identity_block_forbids_cycle_references(sample_db):
    a = _agent(sample_db, biological_sex="Male")
    try:
        assert "does NOT have menstrual cycle data" in a._identity_block()["text"]
    finally:
        a.close()


def test_female_identity_block_does_not_carry_the_male_disclaimer(agent):
    assert "does NOT have menstrual cycle data" not in agent._identity_block()["text"]


def test_identity_block_is_absent_without_name_or_sex(sample_db):
    a = _agent(sample_db, display_name="", biological_sex="")
    try:
        assert a._identity_block() is None
    finally:
        a.close()


# ==================================================================
# Cycle block — sex-gated like the API
# ==================================================================
def test_cycle_block_present_for_a_female_user_with_cycle_data(agent):
    block = agent._cycle_context_block()
    assert block is not None
    assert "cycle" in block["text"].lower()


def test_cycle_block_is_withheld_from_a_male_user(sample_db):
    """sample_db contains cycle rows regardless of the configured sex — the
    same misconfiguration the API gate exists for. The prompt must not
    contradict the identity block's 'does NOT have menstrual cycle data'."""
    a = _agent(sample_db, biological_sex="Male")
    try:
        assert a._cycle_context_block() is None
        assert all("cycle" not in b["text"].lower() or "NOT have menstrual" in b["text"]
                   for b in a._system_for_call())
    finally:
        a.close()


def test_cycle_block_frames_phase_as_a_confounder_not_a_cause(agent):
    """Documented requirement: phase is a context label, ranked against sleep
    loss / alcohol / training / heat — never the sole explanation."""
    text = agent._cycle_context_block()["text"].lower()
    assert "confounder" in text or "context" in text


# ==================================================================
# Environment block — threshold gated
# ==================================================================
def _env_frame(**cols) -> pd.DataFrame:
    base = {"date": [datetime.now().date().isoformat()]}
    base.update({k: [v] for k, v in cols.items()})
    return pd.DataFrame(base)


def test_environment_block_absent_when_nothing_is_extreme(agent, monkeypatch):
    monkeypatch.setattr(agent._repo, "query_environment",
                        lambda *a, **k: _env_frame(apparent_temp_max_c=18.0,
                                                   european_aqi=20.0, pm25=5.0,
                                                   pollen_grass=1.0))
    assert agent._environment_context_block() is None


def test_environment_block_absent_when_no_data(agent, monkeypatch):
    monkeypatch.setattr(agent._repo, "query_environment", lambda *a, **k: pd.DataFrame())
    assert agent._environment_context_block() is None


@pytest.mark.parametrize("cols,expected", [
    ({"apparent_temp_max_c": 30.0}, "heat"),
    ({"european_aqi": 75.0}, "poor air quality"),
    ({"pm25": 30.0}, "elevated PM2.5"),
    ({"pollen_grass": 80.0}, "high pollen"),
])
def test_environment_block_fires_above_each_threshold(agent, monkeypatch, cols, expected):
    monkeypatch.setattr(agent._repo, "query_environment", lambda *a, **k: _env_frame(**cols))
    block = agent._environment_context_block()
    assert block is not None
    assert expected in block["text"]


@pytest.mark.parametrize("cols", [
    {"apparent_temp_max_c": 27.9},
    {"european_aqi": 59.9},
    {"pm25": 24.9},
    {"pollen_grass": 49.9},
])
def test_environment_block_stays_silent_just_below_each_threshold(agent, monkeypatch, cols):
    monkeypatch.setattr(agent._repo, "query_environment", lambda *a, **k: _env_frame(**cols))
    assert agent._environment_context_block() is None


def test_environment_block_survives_a_repo_failure(agent, monkeypatch):
    """A broken environment query must not take down every chat turn."""
    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(agent._repo, "query_environment", boom)
    assert agent._environment_context_block() is None


def test_environment_block_labels_values_as_a_48h_peak(agent, monkeypatch):
    """The text must not let the model read a 48h peak as today's reading."""
    monkeypatch.setattr(agent._repo, "query_environment",
                        lambda *a, **k: _env_frame(apparent_temp_max_c=31.0))
    assert "48h peak" in agent._environment_context_block()["text"]


# ==================================================================
# Today block
# ==================================================================
def test_today_block_reflects_the_current_date(agent):
    """A long-running agent process must not stay stuck on its start date."""
    assert datetime.now().strftime("%Y-%m-%d") in agent._today_block()["text"]


def test_today_block_is_always_the_first_daily_block(agent):
    blocks = agent._system_for_call()
    assert blocks[len(agent._system)]["text"].startswith("## Today's Date")


# ==================================================================
# Per-model thinking / effort configuration
# ==================================================================
def test_effort_field_is_only_settable_via_its_env_alias():
    """`claude_effort` declares validation_alias="INSIGHTS_EFFORT", so passing
    it by field name is silently dropped — a trap for anyone constructing
    Settings in code. Production only ever calls Settings() with no args, so
    this is pinned as documented behaviour rather than treated as a bug."""
    assert Settings(claude_effort="high").claude_effort == "low"
    assert Settings(INSIGHTS_EFFORT="high").claude_effort == "high"


def test_adaptive_models_get_effort_and_adaptive_thinking(sample_db):
    a = _agent(sample_db, claude_model="claude-sonnet-5", INSIGHTS_EFFORT="high")
    try:
        assert a._thinking == {"type": "adaptive"}
        assert a._extra_call_params["output_config"] == {"effort": "high"}
    finally:
        a.close()


def test_effort_defaults_to_low(sample_db):
    """The cheapest setting — an accidental default bump costs real money."""
    a = _agent(sample_db, claude_model="claude-sonnet-5")
    try:
        assert a._extra_call_params["output_config"] == {"effort": "low"}
    finally:
        a.close()


def test_legacy_models_get_a_token_budget_and_no_effort(sample_db):
    """`effort` is rejected on the legacy budget models, `adaptive` on the
    current ones — sending the wrong one is a 400."""
    a = _agent(sample_db, claude_model="claude-3-5-sonnet-20241022")
    try:
        assert a._thinking == {"type": "enabled", "budget_tokens": 8000}
        assert "output_config" not in a._extra_call_params
    finally:
        a.close()


@pytest.mark.parametrize("model,adaptive", [
    ("claude-sonnet-5", True),
    ("claude-opus-4-8", True),
    ("claude-3-5-sonnet-20241022", False),
])
def test_model_gate_matches_the_thinking_config(sample_db, model, adaptive):
    assert _model_supports_adaptive(model) is adaptive
    a = _agent(sample_db, claude_model=model)
    try:
        assert (a._thinking["type"] == "adaptive") is adaptive
    finally:
        a.close()
