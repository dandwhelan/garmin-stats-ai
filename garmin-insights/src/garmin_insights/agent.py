"""Core Claude agent — tool-calling conversation loop with medical context."""

from __future__ import annotations

import json
import logging
import re
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
    split_lifestyle_by_category,
    aggregate_workouts,
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
- **Sleep is keyed to the wake-up date.** A date's sleep metrics (sleep score, overnight \
  HRV, sleep-derived RHR, deep/REM/light, awakenings, body-battery change) describe the \
  night that ENDED that morning. So *last night's* sleep lives on **today's** date, the \
  night before that on yesterday's date, and so on. Always label a night by its true \
  wake date (e.g. today's record is "last night, <yesterday>→<today>") — never shift a \
  sleep record onto a night it doesn't belong to.
- **Confirm last night's sleep exists before reporting it.** If today's entry has no \
  sleep fields, last night's sleep has not synced from the watch yet (the device often \
  uploads it a few hours after waking). Say so plainly and report only the overnight \
  metrics that are actually present — do NOT pull an earlier night's sleep and present \
  it as last night's. You may mention the most recent night on record, but label it with \
  its real date.
- **Align behaviors to the night they affect.** Lifestyle entries are logged on the \
  calendar day they happened, but a date's sleep/overnight metrics describe the night that \
  *ended* that morning. So an evening behavior logged on date X (alcohol, late caffeine, \
  late/heavy meal, late or pre-bed exercise, screens before bed) shows up in the sleep score, \
  overnight HRV, sleep-derived RHR and body-battery-at-wake keyed to **X+1** — the NEXT day's \
  record. When estimating how a behavior affected sleep or overnight recovery, compare it \
  against the following morning's record, never the same date's (that row is the night *before* \
  the behavior). Same-day metrics like daytime stress or steps still line up with the behavior's \
  own date.
- Query cached daily summaries first (get_daily_metrics) — they're much faster
- When comparing behaviors, always use compare_behavior_impact for statistical rigor
- Check baselines via get_my_baselines before making claims about "high" or "low" values — \
  prefer this over fetching large raw date ranges for long-term averages
- **Rolling baselines can be skewed by recent events.** The 7-/30-day baselines are simple \
  rolling averages, so a discrete multi-day strain stretch inside the window (a festival or big \
  event, illness, travel, or a hard training block) drags the baseline toward that period — which \
  can make a genuinely elevated RHR or suppressed HRV look "near baseline." Before calling a value \
  normal, check whether the baseline window contains such a stretch (use the daily data and the \
  user's notes); if it does, flag that the baseline itself may be temporarily inflated/depressed \
  and compare against the user's pre-event typical instead.
- Check get_last_session_summary at the start of each conversation for continuity
- If the user shares useful context (symptoms, diet/alcohol/caffeine timing, travel, illness, stressors, meds, major events), save it with save_user_note for future sessions
- The user can write their own free-text note for any day (what they did, ate, how they felt). \
  These notes appear inline under a `note` key in get_daily_metrics and via get_daily_notes — \
  treat them as first-hand ground truth and weight them heavily when explaining that day's \
  metric deviations. When the user tells you in chat what happened on a specific day, record it \
  with save_daily_note so it stays attached to that date. save_daily_note ALWAYS appends — it \
  never overwrites or removes anything the user wrote by hand, so their own notes are preserved.
- Fetch the minimum date range needed: use get_my_baselines for 30-day context rather than \
  requesting 30 days of raw data unless you need day-by-day detail

{medical_knowledge}
"""


_SCAN_PROMPTS = {
    "morning": (
        "Generate a morning health briefing. Last night's sleep is the sleep record "
        "dated TODAY (sleep is keyed to the wake-up date). FIRST confirm today's entry "
        "actually has sleep fields — if it does not, last night's sleep has not synced "
        "from the watch yet: say so explicitly and do NOT report an earlier night as if "
        "it were last night. Then check last night's sleep quality, overnight HRV, body "
        "battery at wake, and training readiness (if your watch reports it — many models "
        "don't, so skip it silently when no readiness data is present). Compare to "
        "baselines and flag anything "
        "noteworthy. If any lifestyle behaviors were logged yesterday, analyze their "
        "impact. Fetch at most 3 days of raw data — use get_my_baselines for context."
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
        "Run a focused health scan. Surface the top 3 most noteworthy findings, in "
        "priority order: recovery strain (RHR / HRV / respiration) > sleep disruption > "
        "training load > environment & lifestyle confounders > long-term fitness markers. "
        "Lead with the precomputed anomaly/trend findings if they are provided, rather than "
        "re-deriving them from the raw daily rows; check all baselines for anything they miss, "
        "and analyze recent trends (7-day) for the key metrics. Prioritize actionable insights. "
        "Fetch at most 14 days of raw data — use get_my_baselines for 30-day context."
    ),
    "weekly": (
        "Generate a weekly health summary. Analyze the last 7 days: "
        "1) Overall trends in sleep, stress, HRV, and body battery. "
        "2) Impact of lifestyle behaviors that have BOTH present and absent days in the window "
        "— note the on/off day counts and skip behaviors logged every day (no comparator) or "
        "on only 1-2 days (too sparse). Treat symptoms/states (illness, injury, allergy/asthma, "
        "low energy) as outcomes/confounders to explain, not causes. "
        "3) Training load and recovery balance (frame as approximate if detailed Garmin load / "
        "ACWR / HR-zone data isn't available). "
        "4) Top 3 actionable recommendations for next week. "
        "Compare this week to the 30-day baseline. "
        "Fetch at most 30 days of raw data — use get_my_baselines for the baseline reference."
    ),
}

# Per-focus default snapshot window for the portable prompt. A morning brief
# only needs the last few days (last night's sleep is keyed to today; yesterday
# carries the relevant lifestyle/workouts), whereas a weekly/general scan wants
# more trend context. Anomaly detection still runs over its own longer internal
# window (InsightScanner), so a short raw window does not blind the scan. Used
# only when the caller passes no explicit snapshot_days and no explicit dates.
_FOCUS_SNAPSHOT_DAYS = {
    "morning": 5,
    "midday": 5,
    "evening": 5,
    "general": 14,
    "weekly": 14,
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
            medical_knowledge=get_rules_summary_for_llm(settings.biological_sex),
        )
        # Static system prefix — identical for the life of this (per-user) agent.
        # Beyond the general instructions + KB, the evidence-tier rules and the
        # user-identity block never change, so we fold them into the cached
        # prefix (cache_control on the LAST static block) rather than re-sending
        # them uncached on every tool-loop round. The day-varying blocks (date,
        # cycle phase, environment) are appended per-call in _system_for_call().
        self._system = [{"type": "text", "text": system_content}]
        self._system.append(self._evidence_tier_block())
        _identity = self._identity_block()
        if _identity is not None:
            self._system.append(_identity)
        self._system[-1] = {**self._system[-1], "cache_control": {"type": "ephemeral"}}

        # Tool definitions never change — build once
        self._tools_cache = get_all_tools_anthropic(self._tool_handler)

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
            today = datetime.now().date()
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
            today = datetime.now().date()
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
                f"Active environmental confounders: {', '.join(flags)} "
                "(48h peak values — today's actual reading may be lower than the "
                "peak, so weight these by how close today is to the peak).\n"
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
            "\n"
            "For any persistent or concerning symptom, recommend the user discuss it "
            "with a clinician. This analysis detects deviations from personal baselines "
            "and is not medical advice or a diagnosis.\n"
        )
        return {"type": "text", "text": text}

    def _system_for_call(self) -> list[dict]:
        """Build the system blocks for a single API call.

        The static prefix (general instructions + KB + tier rules + identity) is
        already cached for the life of the agent via ``self._system``. Here we
        append only the day-varying blocks (today's date, cycle phase,
        environment extremes) and put a second cache breakpoint on the last of
        them, so the whole system prompt stays warm across the rounds of a
        single conversation turn instead of being re-billed each round.
        """
        daily = [self._today_block()]
        cycle = self._cycle_context_block()
        if cycle is not None:
            daily.append(cycle)
        env = self._environment_context_block()
        if env is not None:
            daily.append(env)
        daily[-1] = {**daily[-1], "cache_control": {"type": "ephemeral"}}
        return list(self._system) + daily

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
        # Remember where this exchange started so a failed API call can roll
        # the history back — otherwise a dangling user turn (no assistant
        # reply) breaks role alternation and poisons every later call in the
        # session.
        base_len = len(history)
        history.append({"role": "user", "content": user_message})

        for round_num in range(10):
            try:
                response = self._client.messages.create(
                    model=self._settings.claude_model,
                    max_tokens=16000,
                    system=self._system_for_call(),
                    tools=self._tools_cache,
                    messages=history,
                    thinking=self._thinking,
                )
            except Exception as e:
                logger.error("Claude API error: %s", e)
                del history[base_len:]
                return f"Error communicating with Claude: {e}"

            history.append({"role": "assistant", "content": response.content})

            if response.stop_reason in ("end_turn", "max_tokens"):
                text_parts = [b.text for b in response.content if b.type == "text"]
                text = "\n".join(text_parts) if text_parts else ""
                if response.stop_reason == "max_tokens":
                    text = (text + "\n\n" if text else "") + "_Response truncated (output token limit reached)._"
                return text

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
        # See chat(): roll back on failure so a dangling user turn can't
        # poison the session history.
        base_len = len(history)
        history.append({"role": "user", "content": user_message})

        for round_num in range(10):
            try:
                with self._client.messages.stream(
                    model=self._settings.claude_model,
                    max_tokens=16000,
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
                del history[base_len:]
                yield {"type": "error", "error": str(e)}
                return

            history.append({"role": "assistant", "content": final.content})

            if final.stop_reason == "end_turn":
                return

            if final.stop_reason == "max_tokens":
                yield {"type": "text", "text": "\n\n_Response truncated (output token limit reached)._"}
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
        snapshot_days: int | None = None,
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

        # Tailor the default raw-data window to the task: a morning brief needs
        # only the last few days, a weekly/general scan a bit more. Explicit
        # snapshot_days or an explicit start/end always win.
        if snapshot_days is None:
            snapshot_days = _FOCUS_SNAPSHOT_DAYS.get(focus or "", 30)

        user_text = message
        if focus:
            scan = _SCAN_PROMPTS.get(focus, _SCAN_PROMPTS["general"])
            if start_date and end_date:
                scan = (
                    f"IMPORTANT: Restrict your analysis to the date range "
                    f"{start_date} → {end_date}.\n\n" + scan
                )
            user_text = scan if not message else f"{scan}\n\nAdditional notes: {message}"

        # Neutralise tool-call language in scan prompts — these instructions make no
        # sense in a portable context where no tools are callable. Replace the most
        # common patterns: "Fetch at most N days … get_my_baselines" lines.
        user_text = re.sub(
            r"Fetch at most \d+ days of raw data[^.]*\.",
            "Use the Baselines section in the DATA SNAPSHOT for context.",
            user_text or "",
        )
        user_text = re.sub(
            r" — use get_my_baselines for [^\n.]+",
            " — use the Baselines section in the DATA SNAPSHOT",
            user_text,
        )

        # Resolve snapshot window (default last 30 days; respect explicit bounds).
        # Use the LOCAL calendar day (like _today_block) — daily_stats rows are
        # keyed by local date, and a UTC "today" here desynced the snapshot end and
        # baseline-anchor label from the printed "Today's Date" during the BST/UTC
        # offset window just after local midnight.
        today = datetime.now().date()
        end = end_date or today.isoformat()
        try:
            end_d = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            end_d = today
            end = end_d.isoformat()
        start = start_date or (end_d - timedelta(days=snapshot_days)).isoformat()
        actual_days = (end_d - datetime.strptime(start, "%Y-%m-%d").date()).days + 1

        # Baselines always anchor to the live "today" and EXCLUDE it (today is
        # incomplete), so each metric's `latest_value` is in fact the last
        # COMPLETE day — yesterday relative to today, not the snapshot end. We
        # surface both dates so the reader doesn't mistake `latest_value` for
        # today's overnight reading (which lives in the daily summaries instead).
        today_iso = today.isoformat()
        last_complete_day = (today - timedelta(days=1)).isoformat()

        # Pin the scan prompts' relative "last 7 days" / "7-day" phrasing to the
        # concrete last 7 COMPLETE days (ending yesterday — today is incomplete),
        # so the receiving model isn't left to guess the window against a
        # date-stamped snapshot that may span more days than the question implies.
        seven_start = (today - timedelta(days=7)).isoformat()
        user_text = user_text.replace(
            "Analyze the last 7 days:",
            f"Analyze the last 7 complete days ({seven_start} → {last_complete_day}) "
            f"— treat {today_iso} as incomplete (overnight/morning metrics only):",
        ).replace(
            "analyze recent trends (7-day) for the key metrics",
            f"analyze recent trends ({seven_start} → {last_complete_day}) for the key metrics",
        )

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

        # Map each logged behaviour to its lifestyle_journal category so the
        # snapshot can separate genuine actions from self-reported states /
        # symptoms / confounders (the cache drops the category column). Behaviour
        # names are byte-identical across both code paths (both read the journal),
        # so the join below is exact.
        behavior_category: dict[str, str] = {}
        try:
            lj = self._repo.query_lifestyle_journal(start, end)
            if lj is not None and not lj.empty:
                for row in lj.reset_index().to_dict(orient="records"):
                    b, c = row.get("behavior"), row.get("category")
                    if b is not None and c is not None:
                        behavior_category[str(b)] = str(c)
        except Exception as e:
            logger.debug("Portable prompt: lifestyle category map failed: %s", e)

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
                # Keep only the environmental fields that actually drive an
                # analysis. Secondary pollutants (pm10/o3/no2) and humidity/UV
                # rarely change a finding and were ~15% of the whole snapshot, so
                # they're dropped. Pollen species are zero-stripped per-day (see
                # below) so permanently-absent species don't repeat on every row.
                env_keys = (
                    "temp_max_c", "temp_min_c", "apparent_temp_max_c",
                    "precipitation_mm", "european_aqi", "pm25",
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
                        and not (k.startswith("pollen_") and row[k] in (0, 0.0))
                    }
                summaries = [
                    {**s, **{f"env_{k}": v for k, v in environment_by_date[str(s.get("date", ""))[:10]].items()}}
                    if str(s.get("date", ""))[:10] in environment_by_date else s
                    for s in summaries
                ]
        except Exception as e:
            logger.debug("Portable prompt: environment merge failed: %s", e)

        # Workouts / activities in the window. The live agent fetches these via
        # get_activity_history; the daily summaries only carry aggregate steps /
        # calories, so without this a pasted prompt has no per-session training
        # data to analyse (run vs strength vs walk, HR, duration).
        activities: list[dict] = []
        try:
            adf = self._repo.query_activity_summary(start, end)
            if adf is not None and not adf.empty:
                adf = adf.reset_index()
                for row in adf.to_dict(orient="records"):
                    name = row.get("activity_name")
                    atype = row.get("activity_type")
                    if name in (None, "", "END") or atype in (None, "", "No Activity"):
                        continue
                    entry: dict = {
                        "date": str(row.get("time", ""))[:10],
                        "type": atype,
                        "name": name,
                    }
                    dist = row.get("distance")
                    if dist not in (None, 0, 0.0):
                        entry["km"] = round(float(dist) / 1000.0, 2)
                    dur = row.get("elapsed_duration")
                    if dur:
                        entry["min"] = round(float(dur) / 60.0, 1)
                    for src, dst in (("average_hr", "avg_hr"), ("max_hr", "max_hr"),
                                     ("calories", "kcal")):
                        v = row.get(src)
                        if v not in (None, 0, 0.0):
                            entry[dst] = round(float(v))
                    activities.append(entry)
        except Exception as e:
            logger.debug("Portable prompt: activities fetch failed: %s", e)

        # Slow-moving fitness markers (VO2 max, fitness age, weight). These
        # update infrequently, so we look back up to a year and keep the most
        # recent readings — the latest value may legitimately predate the
        # snapshot window above.
        fitness_markers: dict = {}
        marker_start = (end_d - timedelta(days=365)).isoformat()

        def _marker_series(df, value_col, scale=1.0, ndp=1, keep=8) -> dict | None:
            if df is None or getattr(df, "empty", True) or value_col not in df.columns:
                return None
            d = df.reset_index() if df.index.name is not None else df
            series: dict = {}
            for row in d.to_dict(orient="records"):
                v = row.get(value_col)
                if v is None:
                    continue
                date = str(row.get("time", ""))[:10]
                if not date:
                    continue
                series[date] = round(float(v) * scale, ndp)
            if not series:
                return None
            return dict(sorted(series.items())[-keep:])

        try:
            vo2 = _marker_series(self._repo.query_vo2_max(marker_start, end), "vo2_max_value")
            if vo2:
                fitness_markers["vo2_max"] = vo2
        except Exception as e:
            logger.debug("Portable prompt: vo2 fetch failed: %s", e)
        try:
            fa = _marker_series(self._repo.query_fitness_age(marker_start, end), "fitness_age")
            if fa:
                fitness_markers["fitness_age_years"] = fa
        except Exception as e:
            logger.debug("Portable prompt: fitness age fetch failed: %s", e)
        try:
            bc_df = self._repo.query_body_composition(marker_start, end)
            wt = _marker_series(bc_df, "weight", scale=0.001)  # grams → kg
            if wt:
                fitness_markers["weight_kg"] = wt
            bf = _marker_series(bc_df, "body_fat")
            if bf:
                fitness_markers["body_fat_pct"] = bf
        except Exception as e:
            logger.debug("Portable prompt: body composition fetch failed: %s", e)

        # Training readiness & status — device-dependent. Garmin performance
        # watches report these; many models (e.g. the Venu series) do not, so
        # the tables can be legitimately empty and the section is then omitted.
        # The morning/weekly scans ask about training readiness, so embed it
        # when present rather than asking the receiving model for data the
        # snapshot lacks. Several readings land per day; we keep the latest one
        # per date, which is the value relevant to a morning briefing.
        def _latest_per_day(df, keys: tuple[str, ...]) -> dict:
            if df is None or getattr(df, "empty", True):
                return {}
            d = df.reset_index() if df.index.name is not None else df
            by_date: dict = {}
            for row in sorted(d.to_dict(orient="records"),
                              key=lambda r: str(r.get("time", ""))):
                day = str(row.get("time", ""))[:10]
                if not day:
                    continue
                vals = {k: row[k] for k in keys if row.get(k) is not None}
                if vals:
                    by_date[day] = vals
            return dict(sorted(by_date.items()))

        training: dict = {}
        try:
            readiness = _latest_per_day(
                self._repo.query_training_readiness(start, end),
                ("score", "level", "recovery_time", "acute_load",
                 "hrv_factor_percent", "sleep_score_factor_percent"),
            )
            if readiness:
                training["readiness"] = readiness
        except Exception as e:
            logger.debug("Portable prompt: training readiness fetch failed: %s", e)
        try:
            status = _latest_per_day(
                self._repo.query_training_status(start, end),
                ("training_status_feedback_phrase",
                 "daily_acute_chronic_workload_ratio", "weekly_training_load",
                 "heat_acclimation_percentage", "altitude_acclimation_percentage"),
            )
            if status:
                training["status"] = status
        except Exception as e:
            logger.debug("Portable prompt: training status fetch failed: %s", e)

        # Precomputed anomaly / trend / behaviour-impact findings — the same
        # deterministic local detection the CLI `scan` command runs before the
        # LLM. Embedding it means the receiving model can lead with code-computed
        # findings instead of hunting for anomalies in the raw rows itself (and
        # it runs over InsightScanner's own internal window, so a short snapshot
        # window does not blind it).
        scan_findings: dict = {}
        try:
            from garmin_insights.insights.proactive import InsightScanner

            scanner = InsightScanner(
                self._memory, self._analysis, self._settings.biological_sex
            )
            # Drop the per-finding prose (medical_context / citation / confounders /
            # claim_strength / measurement_confidence) — it duplicates the medical
            # KB already embedded in the system text above and was ~75% of this
            # section's bytes. Keep the numeric facts, the rule name, the evidence
            # tier, and (for behaviour impacts) the code-computed n / p_value /
            # significant fields the model can legitimately cite.
            _drop_finding_keys = {
                "medical_context", "citation", "confounders",
                "claim_strength", "measurement_confidence",
            }
            scan_findings = {
                category: [
                    {k: v for k, v in item.items() if k not in _drop_finding_keys}
                    for item in items
                ]
                for category, items in scanner.run_full_scan().items()
                if items
            }
        except Exception as e:
            logger.debug("Portable prompt: local scan failed: %s", e)

        # Per-type workout aggregate (sessions / minutes / km / kcal) so the model
        # gets a training-volume digest without summing dozens of raw sessions.
        workout_summary = aggregate_workouts(activities) if activities else []

        # Concatenate every system block we'd send to the live API
        system_text_parts: list[str] = []
        for block in self._system_for_call():
            text = block.get("text") if isinstance(block, dict) else None
            if text:
                system_text_parts.append(text)
        system_text = "\n\n".join(system_text_parts)

        # The system text is written for the live tool-calling agent, which has a
        # 90-day cache window. In portable mode the only history is the snapshot
        # below, so rewrite the "90 days" claim to the real window rather than
        # leaving a contradiction the reader has to mentally discount.
        system_text = system_text.replace(
            "You have access to **90 days** of history.",
            f"You have access to a **{actual_days}-day** snapshot "
            f"({start} → {end}) — the data below is all the history you have.",
        ).replace("Use this longer window for finding", "Use it for finding")

        # The environment block ends with a "Call get_environment_data …" hint
        # aimed at the live agent. In portable mode no tools are callable, so drop
        # that one live call-to-action (a no-op when no environment block fired).
        system_text = system_text.replace(
            " Call get_environment_data if you need the full per-day numbers.",
            "",
        )

        # The live prompt tells the agent to report statistical significance and
        # to lean on compare_behavior_impact "for statistical rigor". In portable
        # mode no correlation/p-value data is computed and that tool is gone, so
        # soften both — otherwise the receiving model is invited to invent
        # significance it cannot ground.
        system_text = system_text.replace(
            "When discussing correlations, mention sample size (N days) and statistical significance",
            "When discussing correlations, mention sample size (N days); do NOT compute your "
            "own p-values or significance from the raw rows — cite only the precomputed "
            "`p_value`/`significant` fields in the snapshot, if any",
        ).replace(
            "When comparing behaviors, always use compare_behavior_impact for statistical rigor",
            "When comparing behaviors, rely only on the daily data provided and report "
            "on/off day counts — do not imply statistical significance",
        )

        sep = "-" * 40
        header_note = (
            "Full system prompt + pre-fetched Garmin data snapshot below. "
            "You have NO tools — use only the inline data; say so honestly if a "
            "question can't be answered from it."
        )

        # The system text is written for a tool-calling agent ("you have access to
        # tools…", "call get_my_baselines", etc.). This override block is prepended
        # so the receiving model clearly understands it is in portable mode and must
        # ignore those instructions — including the cross-session continuity ones,
        # since a pasted prompt has no prior session to recall.
        portable_override = (
            "PORTABLE PROMPT — NO TOOLS AVAILABLE\n\n"
            "You have NO tools in this context. Every tool-calling instruction in "
            "the system text below (get_daily_metrics, get_my_baselines, "
            "compare_behavior_impact, save_user_note, get_last_session_summary, "
            "get_environment_data, save_daily_note, etc.) is non-callable here, and "
            "there is no prior session to recall. Ignore all such instructions — "
            "analyse only the pre-fetched data in the DATA SNAPSHOT section that "
            "follows, which is the entire history available to you "
            f"({actual_days} days, {start} → {end})."
        )

        # Portable-mode analytic guardrails, shown immediately before the user
        # question — the one spot the live system text gives no portable-specific
        # guidance. Counters the failure modes a tool-less paste is prone to:
        # off-by-one behaviour↔sleep alignment, treating outcomes as causes,
        # false-precision impact claims, and invented statistics.
        analytic_guardrails = (
            "ANALYSIS GUARDRAILS (read before answering)\n"
            "- Lag behaviours to the night they affect: a behaviour logged the evening of "
            "date X (alcohol, late caffeine/meal, late or pre-bed exercise, screens) shows up "
            "in the sleep score / overnight HRV / sleep-derived RHR / body-battery-at-wake "
            "keyed to X+1, because those overnight metrics describe the night that ended the "
            "next morning. Never pair an evening behaviour with the SAME date's overnight "
            "metrics — that is the night before it happened.\n"
            "- Each day separates `lifestyle` (things the user actively did) from "
            "`states_symptoms` (self-reported outcomes / confounders — illness, injury, "
            "allergy/asthma, low morning energy, work incidents, travel). Treat "
            "`states_symptoms` as outcomes to explain or confounders to weigh, NOT as causes "
            "you attribute other metric changes to.\n"
            "- Only estimate a behaviour's impact when it has BOTH present and absent days in "
            "the window; state the on/off day counts. A behaviour logged every day (no "
            "comparator) or on only 1-2 days (too sparse) cannot support an effect estimate — "
            "say so rather than asserting one.\n"
            "- Statistics: the Precomputed findings section may carry code-computed "
            "`n_with`/`n_without`, `p_value` and `significant` flags for some behaviours — you "
            "may cite those as given. Do NOT compute or invent your OWN p-values, correlation "
            "coefficients or significance claims from the raw daily rows; for anything the "
            "precomputed findings don't cover, describe associations qualitatively with the "
            "sample size (N days) and flag small N.\n"
            "- Training load: unless a 'Training readiness & status' section appears below, you "
            "do NOT have Garmin training load / ACWR / HR-zone data — keep any training-load or "
            "recovery-balance assessment qualitative and approximate, based on the workout "
            "volume provided.\n"
        )

        # Convert to date-keyed dict — removes the repeated "date" field from
        # every row, plus a few fields that carry no extra signal in the
        # snapshot: bodyBattery charged/drained are derivable from
        # bodyBatteryChange, so they're dropped to save tokens.
        _drop_fields = {"bodyBatteryChargedValue", "bodyBatteryDrainedValue"}

        def _to_date_dict(rows: list[dict]) -> dict:
            return {
                str(s.get("date", ""))[:10]: {
                    k: v for k, v in s.items()
                    if k != "date" and k not in _drop_fields
                }
                for s in rows
            }

        clean_summaries = _round_floats(
            _to_date_dict(split_lifestyle_by_category(summaries, behavior_category))
        )
        clean_baselines = _round_floats({
            m: {k: v for k, v in vals.items() if v is not None}
            for m, vals in baselines.items()
            if any(v is not None for v in vals.values())
        })

        # Always compute window-local avg/min/max/n per metric so the model has a
        # code-computed digest of the snapshot beside the raw rows (and a valid
        # comparator for explicit historical windows, where the rolling baselines
        # — anchored to today — would be misleading).
        window_stats: dict = {}
        if summaries:
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
            f"{portable_override}\n\n"
            f"{sep}\n\n"
            f"{system_text}\n\n"
            f"{sep}\n"
            f"DATA SNAPSHOT (pre-fetched — treat as your tool results)\n"
            f"{sep}\n\n"
            f"## Date range: {start} → {end}\n\n"
            f"## Baselines ({len(clean_baselines)} metrics — rolling 7d/30d "
            f"averages anchored to TODAY ({today_iso}), NOT the snapshot window "
            f"above. Each metric's `latest_value` is the last COMPLETE day "
            f"({last_complete_day}) — today is excluded from baselines, so for "
            f"today's overnight readings (sleep score, RHR, HRV, body battery at "
            f"wake) use the {today_iso} row in Daily summaries, which will differ)\n"
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
            + (
                f"## Precomputed findings (code-computed local detection — "
                f"anomalies vs baseline, composite recovery strain, behaviour "
                f"impacts, and trends; lead with these rather than re-deriving "
                f"from the raw rows)\n"
                "```json\n"
                f"{json.dumps(_round_floats(scan_findings), default=str)}\n"
                "```\n\n"
                if scan_findings else ""
            )
            + f"## Daily summaries ({len(clean_summaries)} days, keyed by date — "
            f"`lifestyle` = actions the user did, `states_symptoms` = "
            f"outcomes/confounders, not causes)\n"
            "```json\n"
            f"{json.dumps(clean_summaries, default=str)}\n"
            "```\n\n"
            + (
                f"## Workout summary (per-type totals across {len(activities)} "
                f"sessions in window)\n"
                "```json\n"
                f"{json.dumps(workout_summary, default=str)}\n"
                "```\n\n"
                if workout_summary else ""
            )
            + (
                (
                    f"## Workouts ({len(activities)} sessions in window — "
                    f"km / min / HR / kcal per session)\n"
                    "```json\n"
                    f"{json.dumps(activities[-30:], default=str)}\n"
                    "```\n\n"
                    if len(activities) <= 30 else
                    f"## Workouts (most recent 30 of {len(activities)} sessions — "
                    f"km / min / HR / kcal per session; see Workout summary above "
                    f"for full-window totals)\n"
                    "```json\n"
                    f"{json.dumps(activities[-30:], default=str)}\n"
                    "```\n\n"
                )
                if activities else ""
            )
            + (
                f"## Fitness markers (latest known readings + recent trend; "
                f"weight in kg — these update infrequently and may predate the "
                f"snapshot window)\n"
                "```json\n"
                f"{json.dumps(fitness_markers, default=str)}\n"
                "```\n\n"
                if fitness_markers else ""
            )
            + (
                f"## Training readiness & status "
                f"({len(training.get('readiness', {}))} readiness days, "
                f"{len(training.get('status', {}))} status days — device-reported, "
                f"latest reading per day; the most recent day is the one relevant "
                f"to a morning briefing)\n"
                "```json\n"
                f"{json.dumps(_round_floats(training), default=str)}\n"
                "```\n\n"
                if training else ""
            )
            + f"{sep}\n"
            f"{analytic_guardrails}"
            f"{sep}\n\n"
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
