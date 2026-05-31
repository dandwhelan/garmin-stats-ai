"""Core Claude agent — tool-calling conversation loop with medical context."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Generator

import anthropic

from garmin_insights.config import Settings, get_settings
from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.db.memory import MemoryStore
from garmin_insights.db.cache import CacheBuilder
from garmin_insights.knowledge.medical import get_rules_summary_for_llm
from garmin_insights.tools.analysis_tools import AnalysisEngine
from garmin_insights.tools.query_tools import (
    QueryToolHandler,
    get_all_tools_anthropic,
    _round_floats,
    _strip_zero_lifestyle,
)

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a personal health insights agent analyzing Garmin wearable data.

## Your Capabilities
You have access to tools that query the user's health data, analyze \
trends and correlations, and recall/save context from previous sessions.

## Communication Style
- Do not use strikethrough (`~~text~~`) under any circumstance. If you need to correct an earlier value or statement, restate the correct figure plainly — never cross out the old one. Strikethrough is rendered as a literal crossed-out span in the UI and confuses the reader.
- Be conversational but precise with numbers
- Ask 1-3 brief follow-up questions when a metric is out-of-range (e.g., how they felt, what they ate/drank, stressors, travel, illness symptoms, late workouts)
- Always cite the date range you analyzed
- When discussing correlations, mention sample size (N days) and statistical significance
- Reference medical research when relevant (use the knowledge base below)
- Use units: bpm for heart rate, ms for HRV, points for scores, seconds→minutes/hours for durations
- Convert durations for readability (e.g., "7h 23m" instead of "26580 seconds")
- Flag anomalies relative to the user's personal baselines, not population norms
- If you don't have enough data, say so honestly
- You have access to **90 days** of history. Use this longer window for finding \
  meaningful trends and correlations.

## Important Rules
- **Today's data is INCOMPLETE** — do not compare today's cumulative metrics (steps, \
  calories, stress duration, active minutes) against baselines or previous full days. \
  Only overnight/morning metrics (sleep score, RHR, HRV, body battery at wake) are \
  valid for today. For cumulative metrics, use yesterday as the most recent complete day.
- Query cached daily summaries first (get_daily_metrics) — they're much faster
- When comparing behaviors, always use compare_behavior_impact for statistical rigor
- Check baselines via get_my_baselines before making claims about "high" or "low" values — \
  prefer this over fetching large raw date ranges for long-term averages
- Check get_last_session_summary at the start of each conversation for continuity
- If the user shares useful context (symptoms, diet/alcohol/caffeine timing, travel, illness, stressors, meds, major events), save it with save_user_note for future sessions
- The user can write their own free-text note for any day (what they did, ate, how they felt). \
  These notes appear inline under a `note` key in get_daily_metrics and via get_daily_notes — \
  treat them as first-hand ground truth and weight them heavily when explaining that day's \
  metric deviations. When the user tells you in chat what happened on a specific day, record it \
  with save_daily_note so it stays attached to that date.
- Fetch the minimum date range needed: use get_my_baselines for 30-day context rather than \
  requesting 30 days of raw data unless you need day-by-day detail

{medical_knowledge}
"""


_SCAN_PROMPTS = {
    "morning": (
        "Generate a morning health briefing. Check last night's sleep quality, "
        "overnight HRV, body battery at wake, and training readiness. "
        "Compare to baselines and flag anything noteworthy. "
        "If any lifestyle behaviors were logged yesterday, analyze their impact. "
        "Fetch at most 3 days of raw data — use get_my_baselines for context."
    ),
    "midday": (
        "Generate a midday check-in. Look at today's stress trend so far, "
        "current body battery drain rate vs normal, and step count pace. "
        "Flag any emerging patterns. "
        "Fetch at most 7 days of raw data — use get_my_baselines for context."
    ),
    "evening": (
        "Generate an evening activity review. Summarize today's exercise "
        "(if any), daily stress accumulation, and project tonight's sleep quality "
        "based on today's patterns. Compare today's metrics to baselines. "
        "Fetch at most 3 days of raw data — use get_my_baselines for context."
    ),
    "general": (
        "Run a comprehensive health scan. Check all baselines for anomalies, "
        "analyze recent trends (7-day) for all key metrics, and identify "
        "the top 3 most noteworthy findings. Prioritize actionable insights. "
        "Fetch at most 14 days of raw data — use get_my_baselines for 30-day context."
    ),
    "weekly": (
        "Generate a weekly health summary. Analyze the last 7 days: "
        "1) Overall trends in sleep, stress, HRV, and body battery. "
        "2) Impact of each logged lifestyle behavior on key metrics. "
        "3) Training load and recovery balance. "
        "4) Top 3 actionable recommendations for next week. "
        "Compare this week to the 30-day baseline. "
        "Fetch at most 30 days of raw data — use get_my_baselines for the baseline reference."
    ),
}


class HealthAgent:
    """Conversational health insights agent powered by Claude.

    Conversation history can either be managed internally (CLI use) or
    passed in by the caller (web use, where each session has its own list).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            settings = get_settings()
        self._settings = settings

        self._repo = SqliteRepo(settings)
        self._memory = MemoryStore(settings)
        self._memory.initialise_schema()
        self._cache = CacheBuilder(self._repo, self._memory)
        self._analysis = AnalysisEngine(self._memory)

        self._tool_handler = QueryToolHandler(
            repo=self._repo,
            memory=self._memory,
            analysis=self._analysis,
        )

        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        # Thinking config differs per model: Opus supports adaptive; Sonnet
        # needs an explicit budget.
        if "opus" in settings.claude_model.lower():
            self._thinking = {"type": "adaptive"}
        else:
            self._thinking = {"type": "enabled", "budget_tokens": 8000}

        system_content = _SYSTEM_PROMPT.format(
            medical_knowledge=get_rules_summary_for_llm(),
        )
        # Cache the large system prompt to reduce token costs on repeat calls
        self._system = [
            {
                "type": "text",
                "text": system_content,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Tool definitions never change — build once
        self._tools_cache = get_all_tools_anthropic(self._tool_handler)

        # Opus 4.7+ supports "adaptive" thinking; older models use explicit budget
        self._thinking = (
            {"type": "adaptive"}
            if "opus-4-7" in settings.claude_model
            else {"type": "enabled", "budget_tokens": 8000}
        )

        # Default history for CLI use; web callers pass their own list
        self._history: list[dict] = []
        self._key_findings: list[str] = []

    def _today_block(self) -> dict:
        """Inject the current local date on every call so a long-running
        agent process doesn't end up stuck on the day it was started."""
        today = datetime.now().strftime("%Y-%m-%d")
        return {"type": "text", "text": f"## Today's Date\n{today}"}

    def _identity_block(self) -> dict | None:
        """Tell the model which user it's talking to, so replies can address
        them by name and use sex-appropriate physiological priors. Returns
        None when neither name nor biological sex is configured."""
        name = (self._settings.display_name or "").strip()
        sex = (self._settings.biological_sex or "").strip()
        if not name and not sex:
            return None
        parts = ["## Current User"]
        if name:
            parts.append(f"You are speaking with **{name}**. Address them by their first name where natural.")
        if sex:
            parts.append(
                f"Biological sex: **{sex}**. Apply sex-specific physiological reference "
                "ranges (e.g. typical HRV, RHR, hemoglobin, iron status) when interpreting metrics."
            )
            if sex.lower().startswith("m"):
                parts.append("This user does NOT have menstrual cycle data — do not reference cycle phase.")
        return {"type": "text", "text": "\n".join(parts)}

    def _cycle_context_block(self) -> dict | None:
        """Look up the most recent menstrual_cycle entry; if found, return a
        small system block so every reply is phase-aware without the model
        having to call a tool. Returns None when the user doesn't track cycles."""
        try:
            today = datetime.utcnow().date()
            start = (today - timedelta(days=7)).isoformat()
            end = today.isoformat()
            df = self._repo.query_menstrual_cycle(start, end)
            if df.empty:
                return None
            latest = df.iloc[-1].to_dict()
            phase = (latest.get("current_cycle_phase") or "").title() or "Unknown"
            day = latest.get("current_day_of_cycle")
            length = latest.get("cycle_length") or latest.get("predicted_cycle_length")
            day_txt = f"day {int(day)}" if day else "day unknown"
            len_txt = f" of ~{int(length)}" if length else ""
            text = (
                "## Current Menstrual Cycle Context\n"
                f"User is in the **{phase}** phase ({day_txt}{len_txt}).\n"
                "Use cycle phase as a CONFOUNDER / CONTEXT LABEL, not a single cause. "
                "Luteal-phase RHR↑/HRV↓ is normal physiology (Shilaih 2017; Brar 2015; "
                "Alzueta 2022; Symons Downs 2025 SR) — do NOT flag this as illness or "
                "overtraining unless other clear symptoms are present. Before attributing "
                "any change to cycle phase, check sleep duration, alcohol, late training, "
                "heat, and travel — these are often stronger same-day drivers."
            )
            return {"type": "text", "text": text}
        except Exception as e:
            logger.debug("Cycle context unavailable: %s", e)
            return None

    def _environment_context_block(self) -> dict | None:
        """If the user has Open-Meteo data and recent days show an environmental
        extreme (heat, poor air quality, high pollen), surface it as a system
        block so the model considers it as a confounder without having to call
        a tool. Returns None when no environment data exists or nothing is
        out of range."""
        try:
            today = datetime.utcnow().date()
            start = (today - timedelta(days=2)).isoformat()
            end = today.isoformat()
            df = self._repo.query_environment(start, end)
            if df is None or df.empty:
                return None
            if df.index.name is not None:
                df = df.reset_index()

            def _peak(col: str) -> float | None:
                if col not in df.columns:
                    return None
                series = df[col].dropna()
                if series.empty:
                    return None
                try:
                    return float(series.max())
                except (TypeError, ValueError):
                    return None

            apparent_max = _peak("apparent_temp_max_c")
            aqi_max = _peak("european_aqi")
            pm25_max = _peak("pm25")
            pollen_max = max(
                (v for v in (_peak("pollen_grass"), _peak("pollen_birch"),
                             _peak("pollen_ragweed"), _peak("pollen_alder"),
                             _peak("pollen_olive"), _peak("pollen_mugwort"))
                 if v is not None),
                default=None,
            )

            flags: list[str] = []
            if apparent_max is not None and apparent_max >= 28.0:
                flags.append(f"heat (apparent max {apparent_max:.1f}°C)")
            if aqi_max is not None and aqi_max >= 60.0:
                flags.append(f"poor air quality (EU AQI {aqi_max:.0f})")
            if pm25_max is not None and pm25_max >= 25.0:
                flags.append(f"elevated PM2.5 ({pm25_max:.1f} µg/m³)")
            if pollen_max is not None and pollen_max >= 50.0:
                flags.append(f"high pollen ({pollen_max:.0f} grains/m³)")
            if not flags:
                return None

            text = (
                "## Current Environmental Context (last 48h, user's home location)\n"
                f"Active environmental confounders: {', '.join(flags)}.\n"
                "When ranking causes for RHR↑ / HRV↓ / respiration↑ / sleep "
                "fragmentation, include these alongside training load, alcohol, "
                "sleep loss and illness — they are research-validated wearable-cohort "
                "confounders (Buekers 2023 next-day RHR; Niu 2020 PM2.5↔HRV "
                "meta-analysis; Baniak 2023 + Minor 2025 heat↔sleep; Cokorudy 2024 "
                "asthma digital-marker SR). Call get_environment_data if you need "
                "the full per-day numbers."
            )
            return {"type": "text", "text": text}
        except Exception as e:
            logger.debug("Environment context unavailable: %s", e)
            return None

    def _evidence_tier_block(self) -> dict:
        """Tier-aware output rules. Garmin data detects deviations from baseline;
        it does not diagnose. This block tells the model how to phrase findings
        based on the evidence tier of the rule it is citing, and forbids
        single-cause claims for multi-signal deviations."""
        text = (
            "## Evidence-Tier Output Rules\n"
            "When citing a rule from the Medical Evidence Knowledge Base, match "
            "your language to its tier:\n"
            "- Tier A → \"Well-established in research.\"\n"
            "- Tier B → \"Observed in wearable studies; not diagnostic.\"\n"
            "- Tier C → \"Plausible contributor — strongest if your own logs confirm it.\"\n"
            "- Tier D → \"Experimental / preprint — treat as a personal tracking hypothesis.\"\n"
            "\n"
            "Never name a single cause for a multi-signal deviation "
            "(e.g. RHR↑ + HRV↓ + respiration↑). Instead, list ranked plausible "
            "contributors and prefer the one(s) the user logged in the prior 24-48h.\n"
            "\n"
            "If fewer than 21 days of baseline data are available, prepend "
            "\"Low-confidence (sparse baseline):\" to any trend/deviation finding.\n"
            "\n"
            "Word substitutions (apply consistently):\n"
            "- Never \"diagnose\". For multi-signal RHR↑/HRV↓/resp↑ patterns, say "
            "\"illness-like recovery strain pattern\".\n"
            "- For Garmin stress, say \"physiological / autonomic strain\", "
            "not \"mental stress\".\n"
            "- For Garmin deep/REM, say \"device-estimated\" or \"personal trend vs "
            "your own baseline\" — never quote absolute clinical ranges as a deficit.\n"
            "- For ACWR, say \"load-spike context signal\" — never \"injury "
            "prediction\".\n"
            "- For SpO2 patterns, say \"screening signal worth discussing with a "
            "clinician\" — never \"sleep apnoea\".\n"
        )
        return {"type": "text", "text": text}

    def _system_for_call(self) -> list[dict]:
        """Build the system blocks for a single API call: cached prompt + dynamic context."""
        blocks = list(self._system)
        blocks.append(self._today_block())
        blocks.append(self._evidence_tier_block())
        identity = self._identity_block()
        if identity is not None:
            blocks.append(identity)
        cycle = self._cycle_context_block()
        if cycle is not None:
            blocks.append(cycle)
        env = self._environment_context_block()
        if env is not None:
            blocks.append(env)
        return blocks

    def ensure_cache_fresh(self, days: int = 90) -> None:
        """Ensure recent daily summaries and baselines are up to date."""
        try:
            self._cache.refresh(days=days)
            logger.info("Cache refreshed for last %d days.", days)
        except Exception as e:
            logger.warning("Cache refresh failed: %s", e)

    def _dispatch_tool_call(self, tool_use_block) -> str:
        """Execute a tool call and return the result string."""
        name = tool_use_block.name
        args = dict(tool_use_block.input) if tool_use_block.input else {}
        logger.info("Tool call: %s(%s)", name, args)

        method = getattr(self._tool_handler, name, None)
        if method is None:
            return json.dumps({"error": f"Unknown tool: {name}"})

        try:
            result = method(**args)
            logger.debug("Tool result (first 200 chars): %s", str(result)[:200])
            return result
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e)
            return json.dumps({"error": f"Tool {name} failed: {str(e)}"})

    def _build_tool_result(self, tool_id: str, result: str) -> dict:
        """Wrap a tool result.

        We deliberately do NOT attach cache_control here. The Anthropic API caps
        cache_control markers at 4 per request; the system prompt and tools list
        already claim 2 of those, and a multi-tool-round conversation can easily
        produce 3+ large tool results — which would push the total over 4 and
        cause a 400. The static system + tools caches alone deliver most of the
        cost saving; per-result caching is not worth the failure mode.
        """
        return {"type": "tool_result", "tool_use_id": tool_id, "content": result}

    # ------------------------------------------------------------------
    # Non-streaming chat (CLI / scan reports)
    # ------------------------------------------------------------------
    def chat(self, user_message: str, history: list[dict] | None = None) -> str:
        """Send a message and get a response, executing tool calls as needed."""
        history = self._history if history is None else history
        history.append({"role": "user", "content": user_message})

        for round_num in range(10):
            try:
                response = self._client.messages.create(
                    model=self._settings.claude_model,
                    max_tokens=8096,
                    system=self._system_for_call(),
                    tools=self._tools_cache,
                    messages=history,
                    thinking=self._thinking,
                )
            except Exception as e:
                logger.error("Claude API error: %s", e)
                return f"Error communicating with Claude: {e}"

            history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text_parts = [b.text for b in response.content if b.type == "text"]
                return "\n".join(text_parts) if text_parts else ""

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_results.append(
                            self._build_tool_result(block.id, self._dispatch_tool_call(block))
                        )
                history.append({"role": "user", "content": tool_results})
                logger.info("Round %d: dispatched %d tool calls", round_num + 1, len(tool_results))
                continue

            break

        return "Maximum tool-calling rounds reached. Please try a simpler question."

    # ------------------------------------------------------------------
    # Streaming chat (web SSE)
    # ------------------------------------------------------------------
    def chat_stream(
        self,
        user_message: str,
        history: list[dict] | None = None,
    ) -> Generator[dict, None, None]:
        """Stream a chat response, yielding events as they arrive.

        Each yielded event is a dict with a `type` field:
          - {"type": "text", "text": "..."} — incremental text from the model
          - {"type": "tool", "names": ["foo", "bar"]} — tools being dispatched
          - {"type": "error", "error": "..."} — error happened mid-stream

        Tool calls are dispatched synchronously between rounds; the generator
        keeps yielding once the next round begins streaming.
        """
        history = self._history if history is None else history
        history.append({"role": "user", "content": user_message})

        for round_num in range(10):
            try:
                with self._client.messages.stream(
                    model=self._settings.claude_model,
                    max_tokens=8096,
                    system=self._system_for_call(),
                    tools=self._tools_cache,
                    messages=history,
                    thinking=self._thinking,
                ) as stream:
                    # Stream text deltas as they arrive
                    for event in stream:
                        if event.type == "content_block_delta":
                            delta = event.delta
                            if getattr(delta, "type", None) == "text_delta":
                                yield {"type": "text", "text": delta.text}
                    final = stream.get_final_message()
            except Exception as e:
                logger.error("Stream error: %s", e)
                yield {"type": "error", "error": str(e)}
                return

            history.append({"role": "assistant", "content": final.content})

            if final.stop_reason == "end_turn":
                return

            if final.stop_reason == "tool_use":
                tool_results = []
                tool_names = []
                for block in final.content:
                    if block.type == "tool_use":
                        tool_names.append(block.name)
                        tool_results.append(
                            self._build_tool_result(block.id, self._dispatch_tool_call(block))
                        )

                yield {"type": "tool", "names": tool_names}
                history.append({"role": "user", "content": tool_results})
                logger.info("Stream round %d: dispatched %d tool calls",
                            round_num + 1, len(tool_results))
                continue

            break

        yield {"type": "text", "text": "\n\n_Maximum tool-calling rounds reached._"}

    # ------------------------------------------------------------------
    # Portable prompt (copy/paste into a free LLM chat)
    # ------------------------------------------------------------------
    def build_portable_prompt(
        self,
        message: str | None = None,
        focus: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        snapshot_days: int = 30,
    ) -> str:
        """Build a single self-contained text prompt the user can paste into
        any LLM chat (Claude.ai, ChatGPT, Gemini, ...). Combines the full
        system context plus a pre-fetched data snapshot so the receiving
        model has the same picture our tool-calling agent would build for
        itself — but as plain inline data, since a chat window has no tools.

        Either `message` or `focus` must be provided. When `focus` is set the
        matching scan prompt is used as the user message.
        """
        if not message and not focus:
            raise ValueError("build_portable_prompt requires a message or focus")

        user_text = message
        if focus:
            scan = _SCAN_PROMPTS.get(focus, _SCAN_PROMPTS["general"])
            if start_date and end_date:
                scan = (
                    f"IMPORTANT: Restrict your analysis to the date range "
                    f"{start_date} → {end_date}.\n\n" + scan
                )
            user_text = scan if not message else f"{scan}\n\nAdditional notes: {message}"

        # Resolve snapshot window (default last 30 days; respect explicit bounds)
        today = datetime.utcnow().date()
        end = end_date or today.isoformat()
        try:
            end_d = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            end_d = today
            end = end_d.isoformat()
        start = start_date or (end_d - timedelta(days=snapshot_days)).isoformat()

        # Lazily build cache for any uncached dates in the requested window,
        # so historical ranges (older than the rolling 90-day refresh) get a
        # snapshot built on demand from the raw daily_stats table.
        try:
            self._cache.build_range(start, end)
        except Exception as e:
            logger.warning("Portable prompt: cache build_range failed: %s", e)

        # Pull data the agent would normally fetch via tools
        try:
            summaries = self._memory.get_daily_summaries_range(start, end)
        except Exception as e:
            summaries = []
            logger.warning("Portable prompt: summaries fetch failed: %s", e)
        try:
            baselines = self._memory.get_baselines()
        except Exception as e:
            baselines = {}
            logger.warning("Portable prompt: baselines fetch failed: %s", e)

        # Merge menstrual cycle fields directly into daily summaries (keyed by date)
        try:
            df = self._repo.query_menstrual_cycle(start, end)
            if df is not None and not df.empty:
                cycle_by_date: dict = {}
                for row in df.reset_index().to_dict(orient="records"):
                    d = str(row.get("date", ""))[:10]
                    cycle_by_date[d] = {
                        k: row[k] for k in ("current_day_of_cycle", "current_cycle_phase")
                        if row.get(k) is not None
                    }
                summaries = [
                    {**s, **cycle_by_date[str(s.get("date", ""))[:10]]}
                    if str(s.get("date", ""))[:10] in cycle_by_date else s
                    for s in summaries
                ]
        except Exception:
            pass

        # Merge environment fields (temp / AQI / pollen) directly into summaries
        environment_by_date: dict = {}
        try:
            env_df = self._repo.query_environment(start, end)
            if env_df is not None and not env_df.empty:
                if env_df.index.name is not None:
                    env_df = env_df.reset_index()
                env_keys = (
                    "temp_max_c", "temp_min_c", "apparent_temp_max_c",
                    "precipitation_mm", "humidity_mean", "uv_index_max",
                    "european_aqi", "pm25", "pm10", "o3", "no2",
                    "pollen_alder", "pollen_birch", "pollen_grass",
                    "pollen_mugwort", "pollen_olive", "pollen_ragweed",
                )
                for row in env_df.to_dict(orient="records"):
                    d = str(row.get("date", ""))[:10]
                    if not d:
                        continue
                    environment_by_date[d] = {
                        k: row[k] for k in env_keys
                        if row.get(k) is not None
                    }
                summaries = [
                    {**s, **{f"env_{k}": v for k, v in environment_by_date[str(s.get("date", ""))[:10]].items()}}
                    if str(s.get("date", ""))[:10] in environment_by_date else s
                    for s in summaries
                ]
        except Exception as e:
            logger.debug("Portable prompt: environment merge failed: %s", e)

        # Concatenate every system block we'd send to the live API
        system_text_parts: list[str] = []
        for block in self._system_for_call():
            text = block.get("text") if isinstance(block, dict) else None
            if text:
                system_text_parts.append(text)
        system_text = "\n\n".join(system_text_parts)

        sep = "-" * 40
        header_note = (
            "Full system prompt + pre-fetched Garmin data snapshot below. "
            "You have NO tools — use only the inline data; say so honestly if a "
            "question can't be answered from it."
        )

        # Convert to date-keyed dict — removes the repeated "date" field from every row
        def _to_date_dict(rows: list[dict]) -> dict:
            return {
                str(s.get("date", ""))[:10]: {k: v for k, v in s.items() if k != "date"}
                for s in rows
            }

        clean_summaries = _round_floats(_to_date_dict(_strip_zero_lifestyle(summaries)))
        clean_baselines = _round_floats({
            m: {k: v for k, v in vals.items() if v is not None}
            for m, vals in baselines.items()
            if any(v is not None for v in vals.values())
        })

        # When the user picked an explicit historical window, compute window-
        # local stats so the LLM has something to compare against — the rolling
        # baselines above are always anchored to today and so are misleading
        # for older ranges.
        explicit_window = bool(start_date and end_date)
        window_stats: dict = {}
        if explicit_window and summaries:
            numeric_keys: set[str] = set()
            for s in summaries:
                for k, v in s.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        numeric_keys.add(k)
            numeric_keys -= {"is_complete"}
            for k in sorted(numeric_keys):
                vals = [s[k] for s in summaries if isinstance(s.get(k), (int, float))]
                if not vals:
                    continue
                window_stats[k] = {
                    "avg": round(float(sum(vals) / len(vals)), 1),
                    "min": float(min(vals)),
                    "max": float(max(vals)),
                    "n": len(vals),
                }

        return (
            f"{header_note}\n\n"
            f"{sep}\n"
            f"SYSTEM CONTEXT\n"
            f"{sep}\n\n"
            f"{system_text}\n\n"
            f"{sep}\n"
            f"DATA SNAPSHOT (pre-fetched — treat as your tool results)\n"
            f"{sep}\n\n"
            f"## Date range: {start} → {end}\n\n"
            f"## Baselines ({len(clean_baselines)} metrics — rolling 7d/30d "
            f"anchored to TODAY ({datetime.utcnow().date().isoformat()}), "
            f"NOT the snapshot window above)\n"
            "```json\n"
            f"{json.dumps(clean_baselines, default=str)}\n"
            "```\n\n"
            + (
                f"## Window-local stats ({len(window_stats)} metrics, "
                f"computed across the {start} → {end} snapshot)\n"
                "```json\n"
                f"{json.dumps(window_stats, default=str)}\n"
                "```\n\n"
                if window_stats else ""
            )
            + f"## Daily summaries ({len(clean_summaries)} days, keyed by date)\n"
            "```json\n"
            f"{json.dumps(clean_summaries, default=str)}\n"
            "```\n\n"
            f"{sep}\n"
            f"USER QUESTION\n"
            f"{sep}\n\n"
            f"{user_text}\n"
        )

    # ------------------------------------------------------------------
    # Scan reports & session save
    # ------------------------------------------------------------------
    def generate_scan_report(
        self,
        focus: str = "general",
        context: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Generate a proactive insight report without user prompting."""
        base_prompt = _SCAN_PROMPTS.get(focus, _SCAN_PROMPTS["general"])

        if start_date and end_date:
            # Lazily backfill the daily_summaries cache for the selected window
            # so the agent's tools have data to query even for dates older than
            # the rolling 90-day refresh.
            try:
                self._cache.build_range(start_date, end_date)
            except Exception as e:
                logger.warning("Scan: cache build_range failed: %s", e)
            date_context = (
                f"IMPORTANT: The user has selected a custom date range: {start_date} to {end_date}. "
                f"Restrict your analysis to this date range when fetching data and drawing conclusions. "
                f"Use these exact dates as the start and end for any tool calls that require a date range.\n\n"
            )
            base_prompt = date_context + base_prompt

        if context:
            prompt = (
                f"Here are some preliminary findings from a local analysis:\n\n"
                f"{context}\n\n"
                f"Please verify these findings if necessary, and then proceed with the request:\n"
                f"{base_prompt}"
            )
        else:
            prompt = base_prompt

        # Scans use a fresh transient history so they don't pollute conversations
        scan_history: list[dict] = []
        return self.chat(prompt, history=scan_history)

    def save_session(self, history: list[dict] | None = None) -> None:
        """Save a summary of a conversation to memory."""
        history = self._history if history is None else history
        if len(history) < 2:
            return

        summary_prompt = (
            "Summarize our conversation in 2-3 sentences. "
            "Focus on what health questions were asked and what key findings were discussed. "
            "This summary will be used to maintain context in the next session."
        )

        # Don't mutate the caller's history when generating the summary
        summary_history = list(history)
        try:
            summary = self.chat(summary_prompt, history=summary_history)
            self._memory.save_session(
                summary=summary,
                key_findings=self._key_findings,
            )
            logger.info("Session saved.")
        except Exception as e:
            logger.warning("Failed to save session: %s", e)

    def reset_conversation(self) -> None:
        """Clear the agent's internal CLI conversation history."""
        self._history = []
        self._key_findings = []

    def close(self) -> None:
        """Clean up resources."""
        self._memory.close()
