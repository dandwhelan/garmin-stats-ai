# AI Cost & Scan Reference

How the app talks to Anthropic, what tokens get sent, how costs are kept down, and what each premade health scan asks.

---

## Simple version (plain English)

Think of it like sending a letter to a very smart doctor every time you ask the app something.

That letter has a few parts bundled together:

- **The doctor's briefing** — instructions telling the AI who it is, what it knows about sleep, stress, and exercise science, and how to talk to you. About 3,000 words. Sent every time, but Anthropic keeps a photocopy after the first call so you're not paying the full price to re-read it each time.
- **The menu of things it can look up** — your sleep data, heart rate, stress trends, activity history, etc. Also kept on file after the first time.
- **Your question** — what you actually asked.

The AI reads all of that, then instead of answering straight away it often goes "let me check something first" and looks up a piece of your data. That result gets added to the letter, and it reads everything again. This can loop up to 10 times before it writes a final answer. Each loop costs a small amount.

**The pre-made health scans** (morning, midday, evening, general, weekly) work exactly the same way but with a fixed question pre-written — no chat history included, clean slate every scan.

**Why it stays cheap:** The expensive parts (the briefing, the menu) are cached — Anthropic photocopies them so you only pay to read them once. Large chunks of data returned by tool calls are also cached mid-conversation. And the app uses Sonnet (the default model) which is 5× cheaper than Opus.

---

## Model & pricing

| Model | When used | Input | Output |
|-------|-----------|-------|--------|
| `claude-sonnet-4-6` | Default | $3 / M tokens | $15 / M tokens |
| `claude-opus-4-7` | Set `CLAUDE_MODEL=claude-opus-4-7` in `.env` | $15 / M tokens | $75 / M tokens |

**Use Sonnet unless you specifically want deeper reasoning.** The cost difference is 5×.

---

## What gets sent to Anthropic on every request

### 1. System prompt — ~3,000 tokens (cached)

Built once at agent startup from two parts:

- **Fixed instructions** (~400 tokens): communication style, rules about today's incomplete data, tool usage guidance, date
- **Medical knowledge base** (~2,600 tokens): 34 evidence-backed insight rules injected as text (sleep, stress, exercise, lifestyle, recovery, body composition, illness detection). Sourced from `knowledge/medical.py`.

The entire system prompt has `cache_control: ephemeral`. After the first call Anthropic serves it from cache, saving ~80% of those input tokens on every subsequent call.

### 2. Tool definitions — ~2,500 tokens (cached)

17 tool schemas describing what Claude can call. These are static — they never change at runtime. The last tool in the list (`get_user_profile`) carries `cache_control: ephemeral`, which caches the entire tool block. This saves ~2,500 tokens on every round after the first.

### 3. Conversation history — variable

Each message in the current conversation is re-sent on every round (this is how the Anthropic API works). For scan reports a **fresh empty history is used** (`scan_history = []`), so no prior conversation accumulates. For chat sessions the history grows with each exchange.

### 4. Tool results — variable, large ones cached

When Claude calls a tool, the result is appended to the conversation and re-sent on the next round. If a result is **≥ 4,096 characters** (e.g. 30 days of daily metrics) it gets `cache_control: ephemeral`, so it's cached and not re-billed on subsequent rounds within the same session.

### 5. Extended thinking

Every API call includes `thinking`:

- **Sonnet 4.6**: `{"type": "enabled", "budget_tokens": 8000}` — Claude may use up to 8,000 tokens of internal reasoning before responding. These are billed as output tokens.
- **Opus 4.7**: `{"type": "adaptive"}` — Opus decides how much thinking to use.

Thinking adds cost on complex queries but the model typically uses far fewer than the budget on simple ones.

---

## How a scan works (API call flow)

`garmin-insights scan` (or the dashboard scan buttons) calls `generate_scan_report()` in `agent.py`.

1. A fresh `scan_history = []` is created — no chat history baggage.
2. One of the premade prompts below is sent as the first user message.
3. Claude responds with tool calls. The agent dispatches them (fetching data from SQLite, running statistical analysis locally), appends results, and loops — up to **10 rounds**.
4. When Claude signals `end_turn` the final text is returned.
5. Scan history is discarded after the response — it does **not** persist to the main conversation.

`max_tokens` is set to **8,096** for all calls (covers thinking + response together).

---

## Premade scan prompts

### General (default) — `garmin-insights scan`

```
Run a comprehensive health scan. Check all baselines for anomalies,
analyze recent trends (7-day) for all key metrics, and identify
the top 3 most noteworthy findings. Prioritize actionable insights.
```

**Likely tool calls:** `get_my_baselines`, `get_daily_metrics` (7 days), `find_anomalies` (per metric), `detect_metric_trend`, possibly `detect_illness_signature`.

---

### Weekly — `garmin-insights scan --weekly`

```
Generate a weekly health summary. Analyze the last 7 days:
1) Overall trends in sleep, stress, HRV, and body battery.
2) Impact of each logged lifestyle behavior on key metrics.
3) Training load and recovery balance.
4) Top 3 actionable recommendations for next week.
Compare this week to the 30-day baseline.
```

**Likely tool calls:** `get_daily_metrics` (7 days + 30 days), `get_sleep_data`, `get_my_baselines`, `compare_behavior_impact` (per behavior), `get_activity_history`, `detect_metric_trend`.

---

### Morning — triggered from dashboard

```
Generate a morning health briefing. Check last night's sleep quality,
overnight HRV, body battery at wake, and training readiness.
Compare to baselines and flag anything noteworthy.
If any lifestyle behaviors were logged yesterday, analyze their impact.
```

**Likely tool calls:** `get_sleep_data` (1–2 days), `get_my_baselines`, `get_training_readiness`, `get_lifestyle_behaviors` (yesterday), `compare_behavior_impact`.

---

### Midday — triggered from dashboard

```
Generate a midday check-in. Look at today's stress trend so far,
current body battery drain rate vs normal, and step count pace.
Flag any emerging patterns.
```

**Likely tool calls:** `get_daily_metrics` (today + recent days), `get_my_baselines`, `detect_metric_trend`.

---

### Evening — triggered from dashboard

```
Generate an evening activity review. Summarize today's exercise (if any),
daily stress accumulation, and project tonight's sleep quality
based on today's patterns. Compare today's metrics to baselines.
```

**Likely tool calls:** `get_activity_history` (today), `get_daily_metrics`, `get_my_baselines`, `get_lifestyle_behaviors` (today).

---

## Available tools (what Claude can call)

| Tool | Purpose |
|------|---------|
| `get_daily_metrics` | RHR, stress, body battery, steps, sleep score — from the fast pre-computed cache |
| `get_sleep_data` | Detailed sleep: deep/REM/light seconds, HRV, SpO2, sleep score |
| `get_lifestyle_behaviors` | Caffeine, alcohol, meals, exercise logs from the journal |
| `get_activity_history` | Workouts: type, HR, calories, distance, duration |
| `get_body_composition` | Weight, body fat %, BMI |
| `get_training_readiness` | Garmin training readiness score + contributing factors |
| `compare_behavior_impact` | t-test: metric on days with vs without a behavior |
| `detect_metric_trend` | Linear regression — is a metric trending up/down/stable? |
| `find_anomalies` | Z-score anomaly detection against 30-day baseline |
| `get_metric_correlations` | Pearson correlation between any set of metrics |
| `detect_illness_signature` | Multi-signal illness check (elevated RHR + low HRV + high respiration) |
| `detect_social_jet_lag` | Weekday vs weekend sleep variance |
| `get_my_baselines` | 7-day and 30-day rolling averages + standard deviations |
| `get_recent_insights` | Previously saved insights from memory |
| `get_last_session_summary` | Continuity — what was discussed last time |
| `save_user_note` | Save a preference or sensitivity to the user profile |
| `get_user_profile` | Retrieve all saved notes/preferences |

---

## Cost optimisations already in place

| What | How | Saving |
|------|-----|--------|
| System prompt caching | `cache_control: ephemeral` on system block | ~80% off ~3k tokens per call |
| Tool definition caching | `cache_control: ephemeral` on last tool | ~80% off ~2.5k tokens per call |
| Large tool result caching | `cache_control: ephemeral` on any result ≥ 4,096 chars | ~80% off bulky payloads within a session |
| Scan uses fresh history | `scan_history = []` per scan | No chat history re-sent |
| Pre-computed cache | `get_daily_metrics` hits a pre-built summary table, not raw per-minute data | Smaller, faster tool results |
| Statistical analysis runs locally | Trend detection, anomaly scoring, t-tests all run in Python before Claude sees them | Claude only interprets results, not raw data |
| Sonnet by default | 5× cheaper than Opus | Significant |

---

## Things to watch

- **Thinking tokens on Sonnet**: The 8,000-token thinking budget is a cap, not a guarantee — simple queries use far fewer. But on a complex general scan with multiple tool rounds, thinking can add up. If costs are a concern, reduce `budget_tokens` in `agent.py:105`.
- **90-day history window**: The system prompt tells Claude it has 90 days of history available. If Claude requests 90 days of daily metrics in one call that's a large payload. It will be cached within the scan but costs on the first round.
- **10 tool-call rounds max**: Each round is a separate API call with the growing history re-sent. A scan that hits the 10-round limit is expensive. Check the logs if you see unexpectedly high bills.
