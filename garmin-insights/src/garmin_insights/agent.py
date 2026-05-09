"""Core Claude agent — tool-calling conversation loop with medical context."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Generator

import anthropic

from garmin_insights.config import Settings, get_settings
from garmin_insights.db.sqlite_repo import SqliteRepo
from garmin_insights.db.memory import MemoryStore
from garmin_insights.db.cache import CacheBuilder
from garmin_insights.knowledge.medical import get_rules_summary_for_llm
from garmin_insights.tools.analysis_tools import AnalysisEngine
from garmin_insights.tools.query_tools import QueryToolHandler, get_all_tools_anthropic

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a personal health insights agent analyzing Garmin wearable data.

## Your Capabilities
You have access to tools that query the user's health data, analyze \
trends and correlations, and recall/save context from previous sessions.

## Communication Style
- Be conversational but precise with numbers
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
- Check baselines via get_my_baselines before making claims about "high" or "low" values
- Check get_last_session_summary at the start of each conversation for continuity

{medical_knowledge}

## Today's Date
{today}
"""


class HealthAgent:
    """Conversational health insights agent powered by Claude."""

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

        system_content = _SYSTEM_PROMPT.format(
            medical_knowledge=get_rules_summary_for_llm(),
            today=datetime.utcnow().strftime("%Y-%m-%d"),
        )
        # Cache the large system prompt to reduce token costs on repeat calls
        self._system = [
            {
                "type": "text",
                "text": system_content,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        self._history: list[dict] = []
        self._key_findings: list[str] = []

    def _get_tools(self) -> list[dict]:
        return get_all_tools_anthropic(self._tool_handler)

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

    def _run_round(self) -> anthropic.types.Message:
        return self._client.messages.create(
            model=self._settings.claude_model,
            max_tokens=8096,
            system=self._system,
            tools=self._get_tools(),
            messages=self._history,
            thinking={"type": "adaptive"},
        )

    def chat(self, user_message: str) -> str:
        """Send a message and get a response, executing tool calls as needed."""
        self._history.append({"role": "user", "content": user_message})

        for round_num in range(10):
            try:
                response = self._run_round()
            except Exception as e:
                logger.error("Claude API error: %s", e)
                return f"Error communicating with Claude: {e}"

            self._history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text_parts = [
                    block.text for block in response.content
                    if block.type == "text"
                ]
                return "\n".join(text_parts) if text_parts else ""

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": self._dispatch_tool_call(block),
                        })
                self._history.append({"role": "user", "content": tool_results})
                logger.info("Round %d: dispatched %d tool calls", round_num + 1, len(tool_results))
                continue

            break

        return "Maximum tool-calling rounds reached. Please try a simpler question."

    def chat_stream(self, user_message: str) -> Generator[str, None, None]:
        """Stream a chat response, yielding text/status chunks.

        Tool calls are dispatched synchronously between rounds.
        Yields status messages during tool calls and final text when done.
        """
        self._history.append({"role": "user", "content": user_message})

        for round_num in range(10):
            try:
                response = self._run_round()
            except Exception as e:
                yield f"\n\nError communicating with Claude: {e}"
                return

            self._history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text_parts = [
                    block.text for block in response.content
                    if block.type == "text"
                ]
                yield "\n".join(text_parts) if text_parts else ""
                return

            if response.stop_reason == "tool_use":
                tool_results = []
                tool_names = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_names.append(block.name)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": self._dispatch_tool_call(block),
                        })

                yield f"_Querying: {', '.join(tool_names)}..._\n\n"
                self._history.append({"role": "user", "content": tool_results})
                logger.info("Stream round %d: dispatched %d tool calls",
                            round_num + 1, len(tool_results))
                continue

            break

        yield "Maximum tool-calling rounds reached. Please try a simpler question."

    def generate_scan_report(self, focus: str = "general", context: str = "") -> str:
        """Generate a proactive insight report without user prompting."""
        scan_prompts = {
            "morning": (
                "Generate a morning health briefing. Check last night's sleep quality, "
                "overnight HRV, body battery at wake, and training readiness. "
                "Compare to baselines and flag anything noteworthy. "
                "If any lifestyle behaviors were logged yesterday, analyze their impact."
            ),
            "midday": (
                "Generate a midday check-in. Look at today's stress trend so far, "
                "current body battery drain rate vs normal, and step count pace. "
                "Flag any emerging patterns."
            ),
            "evening": (
                "Generate an evening activity review. Summarize today's exercise "
                "(if any), daily stress accumulation, and project tonight's sleep quality "
                "based on today's patterns. Compare today's metrics to baselines."
            ),
            "general": (
                "Run a comprehensive health scan. Check all baselines for anomalies, "
                "analyze recent trends (7-day) for all key metrics, and identify "
                "the top 3 most noteworthy findings. Prioritize actionable insights."
            ),
            "weekly": (
                "Generate a weekly health summary. Analyze the last 7 days: "
                "1) Overall trends in sleep, stress, HRV, and body battery. "
                "2) Impact of each logged lifestyle behavior on key metrics. "
                "3) Training load and recovery balance. "
                "4) Top 3 actionable recommendations for next week. "
                "Compare this week to the 30-day baseline."
            ),
        }

        base_prompt = scan_prompts.get(focus, scan_prompts["general"])

        if context:
            prompt = (
                f"Here are some preliminary findings from a local analysis:\n\n"
                f"{context}\n\n"
                f"Please verify these findings if necessary, and then proceed with the request:\n"
                f"{base_prompt}"
            )
        else:
            prompt = base_prompt

        saved_history = self._history
        self._history = []
        try:
            result = self.chat(prompt)
        finally:
            self._history = saved_history

        return result

    def save_session(self) -> None:
        """Save a summary of the current conversation session to memory."""
        if len(self._history) < 2:
            return

        summary_prompt = (
            "Summarize our conversation in 2-3 sentences. "
            "Focus on what health questions were asked and what key findings were discussed. "
            "This summary will be used to maintain context in the next session."
        )

        saved_history = self._history.copy()
        try:
            summary = self.chat(summary_prompt)
            self._memory.save_session(
                summary=summary,
                key_findings=self._key_findings,
            )
            logger.info("Session saved.")
        finally:
            self._history = saved_history

    def reset_conversation(self) -> None:
        """Clear the current conversation history."""
        self._history = []
        self._key_findings = []

    def close(self) -> None:
        """Clean up resources."""
        self._memory.close()
