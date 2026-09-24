"""MCP server exposing Garmin Insights tools, scan prompts, and resources.

Lets Cursor / Claude Desktop query the same health data the in-app agent uses,
without going through the web UI or spending Anthropic tokens on tool routing.

Requires ``mcp`` 2.x (``pip install 'garmin-insights[mcp]'``).

Default is read-only (no note/experiment writes). Pass ``--allow-writes`` to
enable save_user_note / save_daily_note / start_experiment / evaluate_experiment.

One server serves one user (``--user``). It is named ``garmin-insights-<user>``,
its instructions are the in-app agent's static system prompt (KB, evidence-tier
rules, identity), and it adds ``get_current_context`` / ``get_evidence`` plus
scan prompts built live with today's deterministic findings.

Usage (from the garmin-data repo root, with .env / USERS configured)::

    garmin-insights-mcp --user dan                        # stdio
    garmin-insights-mcp --user dan --transport streamable-http --port 8765

Client setup, the Pi's systemd services and troubleshooting: ``docs/mcp.md``.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

from garmin_insights.agent import ALL_SCAN_FOCUSES, _SCAN_PROMPTS, _SYSTEM_PROMPT, HealthAgent
from garmin_insights.config import Settings, get_settings
from garmin_insights.db.cache import CacheBuilder
from garmin_insights.db.memory import MemoryStore
from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.knowledge.medical import INSIGHT_RULES, get_rules_summary_for_llm
from garmin_insights.tools.analysis_tools import AnalysisEngine
from garmin_insights.tools.query_tools import QueryToolHandler, get_all_tools_anthropic

logger = logging.getLogger(__name__)

# Tools that mutate memory / experiments. Omitted unless --allow-writes.
WRITE_TOOL_NAMES = frozenset({
    "save_user_note",
    "save_daily_note",
    "start_experiment",
    "evaluate_experiment",
})

_JSON_TO_PY = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class AgentContext:
    """The data stack HealthAgent's prompt-block methods read (``_settings``,
    ``_repo``, ``_memory``, ``_analysis``) without an Anthropic client, so the
    MCP server can call those methods unbound and stay byte-identical to the
    in-app agent's identity / cycle / environment / evidence-tier guidance."""

    def __init__(self, settings: Settings, user_id: str | None = None) -> None:
        self._settings = settings
        self.user_id = user_id
        self._repo = SqliteRepo(settings)
        self._memory = MemoryStore(settings)
        self._memory.initialise_schema()
        self._analysis = AnalysisEngine(self._memory)
        try:
            CacheBuilder(self._repo, self._memory).refresh(days=90)
        except Exception as e:
            logger.warning("Cache refresh failed (non-fatal): %s", e)
        self.handler = QueryToolHandler(
            repo=self._repo, memory=self._memory, analysis=self._analysis
        )

    @property
    def display_name(self) -> str:
        return (self._settings.display_name or self.user_id or "the user").strip()

    def static_instructions(self) -> str:
        """Server instructions: whose data this is + the agent's cached prefix
        (base rules + medical KB + evidence-tier rules + identity)."""
        sex = (self._settings.biological_sex or "").strip() or "unspecified"
        header = (
            f"# Garmin data for {self.display_name}\n"
            f"Every tool on this server reads **{self.display_name}**'s database only "
            f"(user id: {self.user_id or 'default'}, biological sex: {sex}). "
            "If the conversation is about someone else, you are on the wrong server — say so.\n"
            "Call get_current_context at the start of each conversation: it returns today's "
            "date, current cycle phase (if tracked), active environmental confounders and the "
            "deterministic anomaly findings, which change daily and are not in these instructions. "
            "Use get_evidence(rule) to quote a knowledge-base rule's citation, tier and "
            "confounders instead of citing research from memory."
        )
        base = _SYSTEM_PROMPT.format(
            medical_knowledge=get_rules_summary_for_llm(self._settings.biological_sex),
        )
        blocks = [header, base, HealthAgent._evidence_tier_block(self)["text"]]
        identity = HealthAgent._identity_block(self)
        if identity:
            blocks.append(identity["text"])
        return "\n\n".join(blocks)

    def current_context(self, include_findings: bool = True) -> str:
        """Day-varying blocks, recomputed per call."""
        parts = [
            f"## Current User\n{self.display_name} (user id: {self.user_id or 'default'})",
            HealthAgent._today_block(self)["text"],
        ]
        for fn in (HealthAgent._cycle_context_block, HealthAgent._environment_context_block):
            block = fn(self)
            if block:
                parts.append(block["text"])
        if include_findings:
            findings = HealthAgent._local_scan_context(self)
            parts.append(
                "## Precomputed Local Findings\n"
                "Deterministic analysis (anomalies vs 30-day baseline, composite recovery "
                "strain, overnight physiology, behavior impacts with p-values, 14-day trends). "
                "Lead with these rather than re-deriving them from raw rows.\n"
                + (findings or "No findings above thresholds today.")
            )
        return "\n\n".join(parts)

    def scan_prompt(self, focus: str) -> str:
        return self.current_context() + "\n\n## Scan Request\n" + _SCAN_PROMPTS[focus]


def rule_evidence(name: str, biological_sex: str | None = None) -> str:
    """One knowledge-base rule's full evidence record as JSON."""
    sex = (biological_sex or "").strip().lower()
    visible = [
        r for r in INSIGHT_RULES
        if not r.sex_specific or not sex or r.sex_specific.lower()[:1] == sex[:1]
    ]
    for r in visible:
        if r.name == name:
            return json.dumps({
                "name": r.name,
                "category": r.category,
                "evidence_tier": r.evidence_tier,
                "claim_strength": r.claim_strength,
                "measurement_confidence": r.measurement_confidence,
                "citation": r.research_citation,
                "summary": r.research_summary,
                "trigger_metric": r.trigger_metric,
                "trigger_behavior": r.trigger_behavior,
                "confounders": r.confounders,
                "requires_user_context": r.requires_user_context,
            })
    return json.dumps({
        "error": f"Unknown rule '{name}'",
        "available": sorted(r.name for r in visible),
    })


def build_handler(settings: Settings) -> QueryToolHandler:
    """Construct the same tool stack the HealthAgent uses (no Anthropic client)."""
    return AgentContext(settings).handler


def filter_tool_defs(tool_defs: list[dict], *, allow_writes: bool) -> list[dict]:
    """Drop mutating tools unless writes are explicitly enabled."""
    if allow_writes:
        return tool_defs
    return [t for t in tool_defs if t.get("name") not in WRITE_TOOL_NAMES]


def resolve_user_settings(user: str | None) -> Settings:
    """Load Settings, optionally switched to a named user from USERS / users_dir."""
    settings = get_settings()
    if not user:
        return settings
    if user not in settings.user_map:
        known = ", ".join(sorted(settings.user_map)) or "(none — single-user mode)"
        raise SystemExit(f"Unknown user '{user}'. Configured users: {known}")
    return settings.settings_for_user(user)


def _py_type(pschema: dict) -> Any:
    json_type = pschema.get("type", "string")
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), "string")
    if json_type == "array":
        items = pschema.get("items") or {}
        item_t = _JSON_TO_PY.get(items.get("type", "string"), Any)
        return list[item_t]  # type: ignore[valid-type]
    return _JSON_TO_PY.get(json_type, Any)


def make_tool_callable(handler: QueryToolHandler, tool_def: dict):
    """Build a typed callable MCP can introspect from an Anthropic tool schema."""
    name = tool_def["name"]
    schema = tool_def.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    method = getattr(handler, name)

    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {"return": str}
    for pname, pschema in props.items():
        py_type = _py_type(pschema)
        if pname in required:
            parameters.append(
                inspect.Parameter(
                    pname,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=py_type,
                )
            )
            annotations[pname] = py_type
        else:
            opt = Optional[py_type]
            parameters.append(
                inspect.Parameter(
                    pname,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=opt,
                )
            )
            annotations[pname] = opt

    def fn(*args, **kwargs):
        # Drop Nones so handler defaults still apply for optional kwargs.
        call_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        # Bind positional (required) args by name order when MCP sends them that way.
        if args:
            req_names = [p.name for p in parameters if p.default is inspect.Parameter.empty]
            for key, val in zip(req_names, args):
                call_kwargs.setdefault(key, val)
        result = method(**call_kwargs)
        return result if isinstance(result, str) else json.dumps(result)

    fn.__name__ = name
    fn.__doc__ = tool_def.get("description") or name
    fn.__signature__ = inspect.Signature(parameters, return_annotation=str)
    fn.__annotations__ = annotations
    return fn


def create_mcp_server(
    handler: QueryToolHandler,
    *,
    allow_writes: bool = False,
    context: AgentContext | None = None,
):
    """Build an MCPServer with tools, scan prompts, and baseline resources.

    With ``context`` the server is named for its user, carries the in-app
    agent's full system guidance as instructions, and adds the
    get_current_context / get_evidence tools plus live-findings scan prompts.
    """
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as e:
        raise SystemExit(
            "The 'mcp' package (2.x) is required. Install with:\n"
            "  pip install 'garmin-insights[mcp]'\n"
            "  # or: pip install 'mcp>=2'"
        ) from e

    tool_defs = filter_tool_defs(
        get_all_tools_anthropic(handler),
        allow_writes=allow_writes,
    )

    if context is not None:
        name = f"garmin-insights-{context.user_id}" if context.user_id else "garmin-insights"
        instructions = context.static_instructions()
    else:
        name = "garmin-insights"
        instructions = (
            "Personal Garmin health data tools. Prefer get_my_baselines and "
            "get_daily_metrics before wide raw ranges. Sleep is keyed to wake-up "
            "date; today's cumulative metrics are incomplete. Use scan prompts "
            "(morning/weekly/…) for structured briefings."
        )
    mcp = MCPServer(name=name, instructions=instructions)

    for tool_def in tool_defs:
        # Strip Anthropic-only cache_control before registration.
        cleaned = {k: v for k, v in tool_def.items() if k != "cache_control"}
        fn = make_tool_callable(handler, cleaned)
        mcp.add_tool(fn, name=cleaned["name"], description=cleaned.get("description"))

    for focus in sorted(ALL_SCAN_FOCUSES):
        text = _SCAN_PROMPTS[focus]

        def _make_prompt(prompt_text: str = text, focus_name: str = focus):
            def prompt_fn() -> str:
                # Live: today's date, cycle/environment context and the
                # deterministic findings, mirroring generate_scan_report.
                if context is not None:
                    return context.scan_prompt(focus_name)
                return prompt_text

            prompt_fn.__name__ = f"scan_{focus_name}"
            prompt_fn.__doc__ = f"Predefined Garmin Insights {focus_name} health scan"
            return prompt_fn

        mcp.prompt(name=focus, description=f"Predefined {focus} health scan")(_make_prompt())

    if context is not None:
        sex = context._settings.biological_sex

        def get_current_context(include_findings: bool = True) -> str:
            return context.current_context(include_findings=include_findings)

        mcp.add_tool(
            get_current_context,
            name="get_current_context",
            description=(
                f"Whose data this is ({context.display_name}), today's date, current "
                "menstrual-cycle phase (if tracked), active environmental confounders "
                "(heat / air quality / pollen) and the deterministic anomaly, composite-"
                "strain, overnight and trend findings. Call this first in every "
                "conversation. include_findings=false skips the (slower) scan."
            ),
        )

        def get_evidence(rule: str) -> str:
            return rule_evidence(rule, sex)

        mcp.add_tool(
            get_evidence,
            name="get_evidence",
            description=(
                "Full evidence record for one medical knowledge-base rule by name "
                "(e.g. 'alcohol_hrv_suppression'): citation, research summary, evidence "
                "tier, claim strength, measurement confidence and confounders. Use it "
                "to cite research accurately; an unknown name returns the list of rules."
            ),
        )

        @mcp.resource(
            "garmin://context/today",
            name="Today's context",
            description="Current user, date, cycle phase, environment and local findings",
            mime_type="text/markdown",
        )
        def context_resource() -> str:
            return context.current_context()

        @mcp.resource(
            "garmin://knowledge/{rule}",
            name="Knowledge-base rule",
            description="Evidence record for one medical knowledge-base rule",
            mime_type="application/json",
        )
        def knowledge_resource(rule: str) -> str:
            return rule_evidence(rule, sex)

    @mcp.resource(
        "garmin://baselines",
        name="Personal baselines",
        description="30-day personal baselines (RHR, HRV, sleep, body battery, …)",
        mime_type="application/json",
    )
    def baselines_resource() -> str:
        return handler.get_my_baselines()

    @mcp.resource(
        "garmin://daily/recent",
        name="Recent daily metrics",
        description="Cached daily summaries for the last 14 complete days",
        mime_type="application/json",
    )
    def recent_daily_resource() -> str:
        today = datetime.now().date()
        end = (today - timedelta(days=1)).isoformat()
        start = (today - timedelta(days=14)).isoformat()
        return handler.get_daily_metrics(start, end)

    @mcp.resource(
        "garmin://profile",
        name="User profile",
        description="Configured identity and available data hints",
        mime_type="application/json",
    )
    def profile_resource() -> str:
        profile = json.loads(handler.get_user_profile() or "{}")
        if context is not None:
            profile = {
                "whoami": {
                    "user_id": context.user_id,
                    "display_name": context.display_name,
                    "biological_sex": context._settings.biological_sex,
                },
                "saved_profile": profile,
            }
        return json.dumps(profile, default=str)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="MCP server for Garmin Insights health data tools",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="User id from USERS / users/*.env (default: single-user settings)",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="Expose note/experiment write tools (off by default)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
        help="stdio for local clients; streamable-http/sse for always-on LAN server",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for HTTP transports (use 0.0.0.0 for LAN)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="TCP port for HTTP transports (default 8765)",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        metavar="HOST",
        help=(
            "Allowed Host header (repeatable), e.g. 192.168.4.148:*. "
            "Defaults include localhost + --host when using HTTP."
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Debug logging to stderr",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = resolve_user_settings(args.user)
    context = AgentContext(settings, user_id=args.user)
    mcp = create_mcp_server(
        context.handler, allow_writes=args.allow_writes, context=context
    )

    mode = "read-write" if args.allow_writes else "read-only"
    logger.info(
        "Starting garmin-insights MCP (%s, transport=%s) for db=%s",
        mode,
        args.transport,
        settings.sqlite_db_path,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # HTTP transports — allow LAN clients to connect from Windows.
    from mcp.server.transport_security import TransportSecuritySettings

    allowed = list(args.allowed_host or [])
    for h in ("localhost:*", "127.0.0.1:*", "pi5:*"):
        if h not in allowed:
            allowed.append(h)
    # Always allow the bind host (and common LAN IP form with any port).
    if args.host not in ("0.0.0.0", "::"):
        pattern = f"{args.host}:*"
        if pattern not in allowed:
            allowed.append(pattern)
    else:
        # Listening on all interfaces — accept any Host on private LAN ranges
        # the operator listed, plus the machine hostname. Operators should
        # pass --allowed-host 192.168.x.x:* for their Pi's LAN IP.
        pass

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=["*"],
    )

    if args.transport == "streamable-http":
        path = "/mcp"
        logger.info(
            "HTTP MCP listening on http://%s:%s%s (allowed_hosts=%s)",
            args.host if args.host != "0.0.0.0" else "<all-interfaces>",
            args.port,
            path,
            allowed,
        )
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=path,
            transport_security=security,
        )
    else:  # sse
        logger.info(
            "SSE MCP listening on http://%s:%s/sse (allowed_hosts=%s)",
            args.host if args.host != "0.0.0.0" else "<all-interfaces>",
            args.port,
            allowed,
        )
        mcp.run(
            transport="sse",
            host=args.host,
            port=args.port,
            transport_security=security,
        )


if __name__ == "__main__":
    main()
