"""Scheduler for 4x daily automated scans."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import schedule

from garmin_insights.agent import HealthAgent
from garmin_insights.config import Settings

logger = logging.getLogger(__name__)


_SCAN_FOCUS_MAP = {
    "06": "morning",
    "07": "morning",
    "08": "morning",
    "12": "midday",
    "13": "midday",
    "18": "evening",
    "19": "evening",
    "22": "night",
    "23": "night",
}


def _determine_focus(time_str: str) -> str:
    """Map a scheduled time to a scan focus area."""
    hour = time_str.split(":")[0]
    return _SCAN_FOCUS_MAP.get(hour, "general")


def _run_scan(agent: HealthAgent, focus: str) -> None:
    """Execute a single scan and print results."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    now = datetime.now().strftime("%H:%M")
    console.print(f"\n[dim]{'─' * 60}[/dim]")
    console.print(
        f"[bold cyan]🔍 Scheduled Scan ({focus.title()})[/bold cyan]  "
        f"[dim]{now}[/dim]"
    )
    console.print(f"[dim]{'─' * 60}[/dim]\n")

    try:
        agent.ensure_cache_fresh(days=3)
        report = agent.generate_scan_report(focus=focus)
        console.print(Panel(report, title=f"📊 {focus.title()} Report", border_style="green"))
    except Exception as e:
        console.print(f"[red]Scan failed: {e}[/red]")
        logger.error("Scan failed: %s", e, exc_info=True)


def start_scheduler(settings: Settings) -> None:
    """Start the background scheduler that runs 4x daily scans."""
    from rich.console import Console

    console = Console()
    agent = HealthAgent(settings)

    scan_times = settings.scan_time_list
    console.print(f"[bold green]📅 Scheduler started[/bold green]")
    console.print(f"[dim]Scan times: {', '.join(scan_times)}[/dim]\n")

    for t in scan_times:
        focus = _determine_focus(t)
        schedule.every().day.at(t).do(_run_scan, agent=agent, focus=focus)
        console.print(f"  ⏰ {t} → {focus.title()} scan")

    console.print(f"\n[dim]Press Ctrl+C to stop.[/dim]\n")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped.[/yellow]")
    finally:
        agent.close()
