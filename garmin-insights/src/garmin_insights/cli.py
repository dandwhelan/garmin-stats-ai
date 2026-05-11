"""CLI entry point — interactive chat and scan modes with Rich formatting."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

from garmin_insights.config import get_settings

# Custom theme
_THEME = Theme({
    "info": "dim cyan",
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
    "metric": "bold magenta",
    "heading": "bold white",
})


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_chat(args: argparse.Namespace) -> None:
    """Interactive conversational mode."""
    from garmin_insights.agent import HealthAgent

    console = Console(theme=_THEME)

    console.print(Panel(
        "[bold]🏥 Garmin Health Insights Agent[/bold]\n\n"
        "[dim]Ask questions about your health data, discover patterns,\n"
        "and get evidence-backed insights.\n\n"
        "Commands: /scan, /weekly, /baselines, /quit[/dim]",
        border_style="cyan",
    ))

    console.print("[info]Initialising...[/info]")
    agent = HealthAgent()
    agent.ensure_cache_fresh(days=90)
    console.print("[success]Ready! ✓[/success]\n")

    try:
        while True:
            try:
                user_input = console.input("[bold cyan]You:[/bold cyan] ").strip()
            except EOFError:
                break

            if not user_input:
                continue

            # Special commands
            if user_input.lower() in ("/quit", "/exit", "/q"):
                break
            elif user_input.lower() == "/scan":
                console.print("[info]Running scan...[/info]")
                report = agent.generate_scan_report(focus="general")
                console.print(Panel(
                    Markdown(report),
                    title="📊 Health Scan",
                    border_style="green",
                ))
                continue
            elif user_input.lower() == "/weekly":
                console.print("[info]Generating weekly summary...[/info]")
                report = agent.generate_scan_report(focus="weekly")
                console.print(Panel(
                    Markdown(report),
                    title="📊 Weekly Summary",
                    border_style="blue",
                ))
                continue
            elif user_input.lower() == "/baselines":
                _show_baselines(console, agent)
                continue

            # Normal chat
            console.print("[info]Thinking...[/info]")
            response = agent.chat(user_input)
            console.print()
            console.print(Panel(
                Markdown(response),
                title="🤖 Agent",
                border_style="green",
            ))
            console.print()

    except KeyboardInterrupt:
        console.print()

    # Save session on exit
    console.print("[info]Saving session...[/info]")
    agent.save_session()
    agent.close()
    console.print("[success]Session saved. Goodbye! 👋[/success]")


def _show_baselines(console: Console, agent) -> None:
    """Display current baselines in a pretty table."""
    baselines = agent._memory.get_baselines()
    if not baselines:
        console.print("[warning]No baselines computed yet.[/warning]")
        return

    table = Table(title="📏 Current Baselines", border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Latest", justify="right")
    table.add_column("7d Avg", justify="right")
    table.add_column("30d Avg", justify="right")
    table.add_column("7d StdDev", justify="right")

    for metric, vals in sorted(baselines.items()):
        table.add_row(
            metric,
            f"{vals['latest_value']:.1f}" if vals.get('latest_value') is not None else "—",
            f"{vals['avg_7d']:.1f}" if vals.get('avg_7d') is not None else "—",
            f"{vals['avg_30d']:.1f}" if vals.get('avg_30d') is not None else "—",
            f"{vals['std_7d']:.2f}" if vals.get('std_7d') is not None else "—",
        )

    console.print(table)


def cmd_scan(args: argparse.Namespace) -> None:
    """Run a one-off proactive scan."""
    from garmin_insights.agent import HealthAgent
    from garmin_insights.insights.proactive import InsightScanner

    console = Console(theme=_THEME)

    console.print("[info]Initialising...[/info]")
    agent = HealthAgent()
    agent.ensure_cache_fresh(days=90)

    focus = "weekly" if args.weekly else "general"
    console.print(f"[info]Running {focus} scan...[/info]\n")

    # First run local detection
    scanner = InsightScanner(agent._memory, agent._analysis)
    findings = scanner.run_full_scan()

    # Show local findings summary & build context for LLM
    total = sum(len(v) for v in findings.values())
    context_lines = []
    
    if total > 0:
        console.print(f"[metric]Found {total} noteworthy signals locally.[/metric]")
        for category, items in findings.items():
            if items:
                console.print(f"  • {category}: {len(items)} findings")
                
                # Add to LLM context
                context_lines.append(f"\n{category.upper()}:")
                for item in items:
                    context_lines.append(str(item))
        console.print()

    # Then get LLM interpretation with primed context
    context_str = "\n".join(context_lines)
    report = agent.generate_scan_report(focus=focus, context=context_str)
    console.print(Panel(
        Markdown(report),
        title=f"📊 {'Weekly' if args.weekly else 'Health'} Report",
        border_style="green",
    ))

    agent.close()


def cmd_web(args: argparse.Namespace) -> None:
    """Start the web interface server."""
    from garmin_insights.web.app import run_server

    console = Console(theme=_THEME)
    settings = get_settings()
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    console.print(Panel(
        f"[bold]🌐 Garmin Health Insights — Web Interface[/bold]\n\n"
        f"[dim]Dashboard + AI Chat\n"
        f"Open: [link]http://{host}:{port}[/link][/dim]",
        border_style="cyan",
    ))
    run_server(host=host, port=port)


def cmd_schedule(args: argparse.Namespace) -> None:
    """Start the 4x daily scan scheduler."""
    from garmin_insights.scheduler import start_scheduler

    settings = get_settings()
    start_scheduler(settings)


def cmd_status(args: argparse.Namespace) -> None:
    """Show system health status."""
    from garmin_insights.db.sqlite_repo import SqliteRepo
    from garmin_insights.db.memory import MemoryStore

    console = Console(theme=_THEME)
    settings = get_settings()

    console.print("[heading]System Status[/heading]\n")

    # SQLite Database
    try:
        repo = SqliteRepo(settings)
        memory = MemoryStore(settings)
        memory.initialise_schema()

        repo_health = repo.health_check()
        mem_health = memory.health_check()

        if repo_health.get("connected") and mem_health.get("connected"):
            console.print(f"[success]✓ SQLite Database[/success] ({settings.sqlite_db_path})")
            console.print("[dim]Measurements:[/dim]")
            console.print(f"  Tables: {repo_health.get('measurement_count', 0)}")
            if 'date_range' in repo_health:
                dr = repo_health['date_range']
                console.print(f"  Range: {dr.get('start')} → {dr.get('end')}")
            console.print("[dim]Memory:[/dim]")
            console.print(f"  Daily summaries: {mem_health.get('daily_summaries', 0)}")
            console.print(f"  Baselines: {mem_health.get('baselines', 0)}")
            console.print(f"  Insights: {mem_health.get('insights', 0)}")
        else:
            console.print("[error]✗ SQLite: Connection failed[/error]")
            if "error" in repo_health:
                console.print(f"  Repo Error: {repo_health['error']}")
            if "error" in mem_health:
                console.print(f"  Memory Error: {mem_health['error']}")

        memory.close()
    except Exception as e:
        console.print(f"[error]✗ SQLite: {e}[/error]")

    # Claude / Anthropic
    if settings.anthropic_api_key:
        console.print("[success]✓ Anthropic API[/success]")
        console.print(f"  Model: {settings.claude_model}")
    else:
        console.print("[warning]! Anthropic API: ANTHROPIC_API_KEY not set[/warning]")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="garmin-insights",
        description="🏥 Garmin Health Insights Agent",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command")

    # chat
    sub.add_parser("chat", help="Interactive conversational mode")

    # scan
    scan_p = sub.add_parser("scan", help="Run proactive health scan")
    scan_p.add_argument("--weekly", action="store_true", help="Full weekly summary")

    # web
    web_p = sub.add_parser("web", help="Start the web interface (dashboard + AI chat)")
    web_p.add_argument("--port", type=int, default=None, help="Port to listen on (overrides WEB_PORT env var, default 8080)")
    web_p.add_argument("--host", type=str, default=None, help="Host to bind to (overrides WEB_HOST env var, default 0.0.0.0)")

    # schedule
    sub.add_parser("schedule", help="Start 4x daily scan scheduler")

    # status
    sub.add_parser("status", help="Check system connectivity")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    if args.command == "chat":
        cmd_chat(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "web":
        cmd_web(args)
    elif args.command == "schedule":
        cmd_schedule(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
