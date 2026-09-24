"""MCP helper + scan-focus alignment tests (no live Anthropic / stdio needed)."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from garmin_insights.agent import ALL_SCAN_FOCUSES, _SCAN_PROMPTS, _FOCUS_SNAPSHOT_DAYS
from garmin_insights.mcp_server import WRITE_TOOL_NAMES, filter_tool_defs, make_tool_callable


def test_all_scan_focuses_match_prompts_and_snapshot_windows():
    assert ALL_SCAN_FOCUSES == frozenset(_SCAN_PROMPTS)
    assert ALL_SCAN_FOCUSES == frozenset(_FOCUS_SNAPSHOT_DAYS)
    assert {"morning", "midday", "evening", "night", "weekly", "general"} <= ALL_SCAN_FOCUSES


def test_filter_tool_defs_strips_writes_by_default():
    defs = [
        {"name": "get_my_baselines"},
        {"name": "save_user_note"},
        {"name": "start_experiment"},
        {"name": "get_daily_metrics"},
    ]
    readonly = filter_tool_defs(defs, allow_writes=False)
    assert {t["name"] for t in readonly} == {"get_my_baselines", "get_daily_metrics"}
    assert WRITE_TOOL_NAMES >= {"save_user_note", "start_experiment"}

    full = filter_tool_defs(defs, allow_writes=True)
    assert len(full) == 4


def test_make_tool_callable_signature_and_dispatch():
    class _FakeHandler:
        def get_daily_metrics(self, start_date, end_date, metrics=None):
            return {"start": start_date, "end": end_date, "metrics": metrics}

    tool_def = {
        "name": "get_daily_metrics",
        "description": "Query daily metrics",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["start_date", "end_date"],
        },
    }
    fn = make_tool_callable(_FakeHandler(), tool_def)
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["start_date", "end_date", "metrics"]
    assert sig.parameters["metrics"].default is None

    out = fn(start_date="2026-01-01", end_date="2026-01-07")
    assert '"start": "2026-01-01"' in out


def test_create_mcp_server_registers_expected_surfaces(monkeypatch):
    pytest.importorskip("mcp.server.mcpserver")

    from garmin_insights.mcp_server import create_mcp_server

    class _Handler:
        def get_my_baselines(self):
            return "{}"

        def get_user_profile(self):
            return "{}"

        def get_daily_metrics(self, start_date, end_date, metrics=None):
            return "[]"

        def save_user_note(self, note: str = ""):
            return "{}"

    monkeypatch.setattr(
        "garmin_insights.mcp_server.get_all_tools_anthropic",
        lambda handler: [
            {
                "name": "get_my_baselines",
                "description": "baselines",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "save_user_note",
                "description": "write",
                "input_schema": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                },
            },
        ],
    )

    server = create_mcp_server(_Handler(), allow_writes=False)

    async def _check():
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert "get_my_baselines" in names
        assert "save_user_note" not in names

        prompts = await server.list_prompts()
        assert {p.name for p in prompts} == set(ALL_SCAN_FOCUSES)

        resources = await server.list_resources()
        uris = {str(r.uri) for r in resources}
        assert "garmin://baselines" in uris
        assert "garmin://daily/recent" in uris
        assert "garmin://profile" in uris

    asyncio.run(_check())


def test_rule_evidence_returns_record_and_lists_on_miss():
    import json

    from garmin_insights.knowledge.medical import INSIGHT_RULES
    from garmin_insights.mcp_server import rule_evidence

    rule = INSIGHT_RULES[0]
    rec = json.loads(rule_evidence(rule.name))
    assert rec["name"] == rule.name
    assert rec["evidence_tier"] == rule.evidence_tier
    assert rec["citation"] == rule.research_citation

    miss = json.loads(rule_evidence("does_not_exist"))
    assert "error" in miss and rule.name in miss["available"]


def test_rule_evidence_hides_cycle_rules_from_male_users():
    import json

    from garmin_insights.knowledge.medical import INSIGHT_RULES
    from garmin_insights.mcp_server import rule_evidence

    female_rule = next(r for r in INSIGHT_RULES if r.sex_specific == "female")
    assert "error" in json.loads(rule_evidence(female_rule.name, "Male"))
    assert "error" not in json.loads(rule_evidence(female_rule.name, "Female"))


def test_context_server_is_named_per_user_and_exposes_context_tools(monkeypatch):
    pytest.importorskip("mcp.server.mcpserver")

    from garmin_insights.mcp_server import create_mcp_server

    class _Settings:
        biological_sex = "Female"

    class _Ctx:
        user_id = "helen"
        display_name = "Helen"
        _settings = _Settings()

        def static_instructions(self):
            return "# Garmin data for Helen"

        def current_context(self, include_findings=True):
            return f"ctx findings={include_findings}"

        def scan_prompt(self, focus):
            return f"live {focus}"

    class _Handler:
        def get_user_profile(self):
            return "{}"

    monkeypatch.setattr(
        "garmin_insights.mcp_server.get_all_tools_anthropic", lambda handler: []
    )
    server = create_mcp_server(_Handler(), context=_Ctx())

    async def _check():
        names = {t.name for t in await server.list_tools()}
        assert {"get_current_context", "get_evidence"} <= names
        prompt = await server.get_prompt("morning", {})
        assert "live morning" in str(prompt)
        templates = {str(t.uri_template) for t in await server.list_resource_templates()}
        assert "garmin://knowledge/{rule}" in templates

    asyncio.run(_check())
    assert server.name == "garmin-insights-helen"
