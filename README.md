# Garmin Stats AI

A privacy-first health analytics platform: fetches data from Garmin Connect, stores it locally in SQLite, and uses Claude AI to provide actionable health insights via a web dashboard and chat interface.

## Project Structure

- **`garmin-grafana/`** — Data ingestion engine. Fetches metrics (HR, sleep, stress, HRV, activities, body composition) from Garmin Connect and writes them to SQLite.
- **`garmin-insights/`** — AI analysis layer. Web interface (dashboard + chat) and CLI, powered by Claude (`claude-opus-4-7`).

## Prerequisites

- Python 3.11+
- Garmin Connect account
- Anthropic API key ([get one here](https://console.anthropic.com))

## Installation

```bash
git clone https://github.com/dandwhelan/garmin-stats-ai.git
cd garmin-stats-ai

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -e garmin-grafana
pip install -e garmin-insights
```

## Configuration

Create a `.env` file (or set environment variables):

```bash
# Garmin credentials
GARMINCONNECT_EMAIL=your@email.com
GARMINCONNECT_PASSWORD=yourpassword

# Shared database path
SQLITE_DB_PATH=/absolute/path/to/garmin.db

# Claude AI (for garmin-insights)
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### Step 1 — Fetch your Garmin data

```bash
python -m garmin_grafana.garmin_fetch
```

This creates `garmin.db` and populates it with your health history (up to 1 year back on first run). Re-run daily to keep data fresh.

### Step 2 — Start the web interface

```bash
garmin-insights web
```

Open **http://localhost:8080** in your browser.

The web interface has two views:

- **Dashboard** — metric cards (sleep score, RHR, HRV, body battery, steps, stress) with 14-day trend charts and AI scan buttons
- **Chat** — conversational AI with full access to your health data

### CLI alternatives

```bash
garmin-insights chat          # interactive terminal chat
garmin-insights scan          # one-off general health scan
garmin-insights scan --weekly # full weekly summary
garmin-insights status        # check DB + API connectivity
```

## Example questions for the chat

- "How has my sleep been this week compared to my baseline?"
- "Does alcohol affect my overnight HRV? Show me the data."
- "What's been happening with my resting heart rate over the last month?"
- "Am I recovering well enough between workouts?"
- "Which behaviors have the biggest impact on my sleep score?"

## AI Architecture

The agent uses **Claude `claude-opus-4-7`** with:

- **Adaptive thinking** — Claude reasons about complex health patterns before responding
- **Prompt caching** — the large medical knowledge system prompt is cached, reducing API costs on repeat queries by ~80%
- **15 analysis tools** — for querying health data, detecting trends, finding anomalies, computing correlations, and comparing lifestyle behaviors
- **Medical knowledge base** — 18 evidence-backed insight rules (sleep, HRV, stress, exercise, nutrition) injected into the system prompt with research citations
- **Session memory** — conversation summaries are saved so the agent remembers context across sessions

## Privacy

All data stays local. Nothing is sent to external servers except:
- Garmin Connect API (to fetch your own data)
- Anthropic API (to generate AI responses — only the content of your queries and health summaries, not raw data)

## Troubleshooting

- **Login issues**: Delete `~/.garminconnect` to clear stale tokens, then re-run the fetcher
- **No data in dashboard**: Run the fetcher first, then restart the web server so it rebuilds the cache
- **Database locked**: Only one process should write to `garmin.db` at a time — don't run the fetcher while the web server is actively caching

## For developers

See [CLAUDE.md](CLAUDE.md) for architecture details, file map, and instructions for extending the agent.
