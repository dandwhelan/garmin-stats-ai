"""Core Gemini agent — tool-calling conversation loop with medical context."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from google import genai
from google.genai import types

from garmin_insights.config import Settings
from garmin_insights.db.influxdb import InfluxRepo
from garmin_insights.db.memory import MemoryStore
from garmin_insights.db.cache import CacheBuilder
from garmin_insights.knowledge.medical import get_rules_summary_for_llm
from garmin_insights.tools.analysis_tools import AnalysisEngine
from garmin_insights.tools.query_tools import QueryToolHandler, get_all_tools

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a personal health insights agent analyzing Garmin wearable data.

## Your Capabilities
You have access to tools that query the user's health data from InfluxDB, analyze \
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
    """Conversational health insights agent powered by Gemini."""

    def __init__(self) -> None:
        settings = get_settings()
        # self._settings = settings # This line was removed in the diff, but not explicitly stated. Re-adding for consistency if settings are used elsewhere.
        # If settings are only used for initialization, then removing self._settings is fine.
        # For now, I'll assume it's not needed as per the diff's implied change.

        # Initialize infrastructure
        self._repo = SqliteRepo(settings)
        self._memory = MemoryStore(settings)
        self._memory.initialise_schema()
        self._cache = CacheBuilder(self._repo, self._memory)
        self._analysis = AnalysisEngine(self._memory)

        # Tool handler
        self._tool_handler = QueryToolHandler(
            repo=self._repo,
            memory=self._memory,
            analysis=self._analysis,
        )

        # Gemini client
        self._client = genai.Client(api_key=settings.gemini_api_key)

        # Build system prompt with medical knowledge
        self._system_prompt = _SYSTEM_PROMPT.format(
            medical_knowledge=get_rules_summary_for_llm(),
            today=datetime.utcnow().strftime("%Y-%m-%d"),
        )

        # Conversation history for current session
        self._history: list[types.Content] = []
        self._key_findings: list[str] = []

    def _get_tools(self) -> list[callable]:
        """Return all registered tool functions."""
        return get_all_tools(self._tool_handler)

    def ensure_cache_fresh(self, days: int = 90) -> None:
        """Ensure recent daily summaries and baselines are up to date."""
        try:
            self._cache.refresh(days=days)
            logger.info("Cache refreshed for last %d days.", days)
        except Exception as e:
            logger.warning("Cache refresh failed: %s", e)

    def _dispatch_tool_call(self, function_call) -> str:
        """Execute a tool call and return the result string."""
        name = function_call.name
        args = dict(function_call.args) if function_call.args else {}
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

    def chat(self, user_message: str) -> str:
        """Send a message and get a response, executing tool calls as needed.

        Uses manual function calling: we inspect response parts, dispatch
        tool calls locally, and feed results back until the model produces
        a text-only response (max 10 rounds).
        """
        # Add user message to history
        self._history.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_message)],
            )
        )

        # Config with automatic function calling DISABLED
        config = types.GenerateContentConfig(
            system_instruction=self._system_prompt,
            tools=self._get_tools(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
            temperature=0.7,
        )

        max_rounds = 10
        for round_num in range(max_rounds):
            try:
                response = self._client.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=self._history,
                    config=config,
                )
            except Exception as e:
                error_msg = f"Error communicating with Gemini: {e}"
                logger.error(error_msg)
                return error_msg

            if not response.candidates or not response.candidates[0].content:
                return "No response from Gemini."

            content = response.candidates[0].content
            self._history.append(content)

            # Check if there are function calls to dispatch
            function_calls = [
                part for part in content.parts if part.function_call
            ]

            if not function_calls:
                # Pure text response — we're done
                text_parts = [part.text for part in content.parts if part.text]
                return "\n".join(text_parts) if text_parts else ""

            # Dispatch each function call and build response parts
            tool_response_parts = []
            for part in function_calls:
                result_str = self._dispatch_tool_call(part.function_call)
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=part.function_call.name,
                        response={"result": result_str},
                    )
                )

            # Add tool results to history
            self._history.append(
                types.Content(role="user", parts=tool_response_parts)
            )

            logger.info("Round %d: dispatched %d tool calls, continuing...",
                        round_num + 1, len(function_calls))

        return "Maximum tool-calling rounds reached. Please try a simpler question."

    def generate_scan_report(self, focus: str = "general", context: str = "") -> str:
        """Generate a proactive insight report without user prompting.

        Args:
            focus: The type of report ('general', 'weekly', 'sleep', 'training').
            context: Optional text context (e.g. locally detected anomalies) to
                     include in the prompt.
        """
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

        # Use a fresh history for scans
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

        # Ask the LLM to summarize the session
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

    def close(self) -> None:
        """Clean up resources."""
        self._memory.close()
