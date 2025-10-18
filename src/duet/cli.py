"""CLI interface for the Duet orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .artifacts import ArtifactStore
from .config import DuetConfig, find_config
from .init import DuetInitializer, InitError
from .migrate import ArtifactMigrator
from .models import StreamMode
from .orchestrator import Orchestrator
from .persistence import DuetDatabase

app = typer.Typer(help="Automate the Codex ↔ Claude workflow.")
console = Console()


@app.command()
def init(
    workspace: Path = typer.Argument(
        Path("."), help="Workspace root directory (default: current directory)"
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config-path", help="Custom path for .duet directory (default: <workspace>/.duet)"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing .duet directory"),
    skip_discovery: bool = typer.Option(
        False, "--skip-discovery", help="Skip Codex repository context discovery"
    ),
    model_codex: str = typer.Option(
        "gpt-5-codex", "--model-codex", help="Codex model for planning/review"
    ),
    model_claude: str = typer.Option(
        "sonnet", "--model-claude", help="Claude Code model for implementation"
    ),
) -> None:
    """Initialize a new Duet workspace with configuration and scaffolding."""
    try:
        initializer = DuetInitializer(
            workspace_root=workspace,
            config_path=config_path,
            force=force,
            skip_discovery=skip_discovery,
            model_codex=model_codex,
            model_claude=model_claude,
            console=console,
        )
        initializer.init()
    except InitError as exc:
        console.print(f"[red]Initialization failed:[/] {exc}")
        raise typer.Exit(1)


@app.command()
def run(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to duet configuration YAML."
    ),
    run_id: Optional[str] = typer.Option(None, help="Override generated run identifier."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Disable streaming console output (Sprint 6)."
    ),
    stream_mode: Optional[str] = typer.Option(
        None, "--stream-mode", help="Streaming display mode: detailed | compact | off (Sprint 7)."
    ),
) -> None:
    """Execute the duet orchestration loop."""
    duet_config = find_config(config)

    # Override quiet mode if CLI flag provided
    if quiet:
        duet_config.logging.quiet = True
        duet_config.logging.stream_mode = StreamMode.OFF

    # Override stream_mode if CLI flag provided (with validation)
    if stream_mode:
        # Validate against enum values
        valid_modes = [mode.value for mode in StreamMode]
        if stream_mode not in valid_modes:
            console.print(f"[red]Invalid --stream-mode: {stream_mode}[/]")
            console.print(f"[yellow]Valid options: {', '.join(valid_modes)}[/]")
            raise typer.Exit(1)
        duet_config.logging.stream_mode = StreamMode(stream_mode)

    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)

    # Initialize database if duet.db exists
    # DB is in parent of run_artifact_dir (e.g., .duet/runs -> .duet/duet.db)
    db = None
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if db_path.exists():
        db = DuetDatabase(db_path)
        console.log("[dim]Using SQLite database for persistence[/]")

    orchestrator = Orchestrator(duet_config, artifact_store, console=console, db=db)
    orchestrator.run(run_id=run_id)


@app.command()
def show_config(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
) -> None:
    """Pretty-print the resolved configuration."""
    duet_config = find_config(config)
    console.print_json(data=duet_config.model_dump(mode="json"))


@app.command()
def status(
    run_id: str = typer.Argument(..., help="Run ID to check status for."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    show_states: bool = typer.Option(True, "--show-states/--no-states", help="Show state history (Sprint 8)"),
) -> None:
    """Display the current status of a run (enhanced for Sprint 8 stateful workflow)."""
    from rich.table import Table

    duet_config = find_config(config)
    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)

    # Check if database is available for enhanced state view
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    db = DuetDatabase(db_path) if db_path.exists() else None

    # Try database first, fall back to filesystem
    if db:
        run = db.get_run(run_id)
        if not run:
            console.print(f"[red]Run not found: {run_id}[/]")
            raise typer.Exit(1)

        # Display status table
        table = Table(title=f"Run Status: {run_id}")
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")

        table.add_row("Run ID", run_id)
        table.add_row("Phase", f"[bold]{run['phase'].upper()}[/]")
        table.add_row("Iteration", str(run["iteration"]))
        table.add_row("Created", run["created_at"][:19] if run["created_at"] else "N/A")
        table.add_row("Started", run["started_at"][:19] if run["started_at"] else "N/A")
        table.add_row("Completed", run["completed_at"][:19] if run["completed_at"] else "[dim]In Progress[/]")

        # Show active state (Sprint 8)
        active_state = db.get_active_state(run_id)
        if active_state:
            table.add_row("Active State", active_state["state_id"])
            table.add_row("Phase Status", active_state["phase_status"])
            if active_state.get("baseline_commit"):
                table.add_row("Git Baseline", active_state["baseline_commit"][:8])

        if run.get("notes"):
            table.add_row("Notes", run["notes"])

        console.print(table)

        # Show state history (Sprint 8)
        if show_states and db:
            states = db.list_states(run_id)
            if states:
                console.print(f"\n[bold]State History:[/] {len(states)} states")
                state_table = Table()
                state_table.add_column("State ID", style="cyan")
                state_table.add_column("Status", style="magenta")
                state_table.add_column("Created", style="dim")
                state_table.add_column("Notes")

                for state in states[-5:]:  # Show last 5 states
                    is_active = active_state and state["state_id"] == active_state["state_id"]
                    state_id_display = f"[bold]{state['state_id']}[/]" if is_active else state["state_id"]
                    state_table.add_row(
                        state_id_display,
                        state["phase_status"],
                        state["created_at"][:19],
                        (state.get("notes") or "")[:50],
                    )

                console.print(state_table)

                # Suggest next action
                if active_state:
                    phase_status = active_state["phase_status"]
                    if phase_status == "done":
                        console.print(f"\n[green]Run completed successfully![/]")
                    elif phase_status == "blocked":
                        console.print(f"\n[yellow]Run is blocked.[/]")
                    elif "-ready" in phase_status:
                        console.print(f"\n[cyan]Run 'duet next --run-id {run_id}' to execute next phase[/]")
                    elif "-complete" in phase_status:
                        console.print(f"\n[cyan]Run 'duet next --run-id {run_id}' to continue[/]")

        # Show iteration summary
        iterations = db.list_iterations(run_id)
        if iterations:
            console.print(f"\n[bold]Iterations:[/] {len(iterations)} recorded")
            for iteration in iterations[-3:]:  # Show last 3 iterations
                console.print(
                    f"  • Iter {iteration['iteration']} ({iteration['phase']}): "
                    f"{iteration.get('decision_rationale', 'No decision recorded')[:60]}"
                )

    else:
        # Fallback to filesystem-based status (legacy)
        snapshot = artifact_store.load_checkpoint(run_id)
        if not snapshot:
            console.print(f"[red]No checkpoint found for run: {run_id}[/]")
            console.print(f"[dim]Searched in: {artifact_store.run_dir(run_id)}[/]")
            raise typer.Exit(1)

        # Display status table
        table = Table(title=f"Run Status: {run_id}")
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")

        table.add_row("Run ID", snapshot.run_id)
        table.add_row("Phase", f"[bold]{snapshot.phase.value.upper()}[/]")
        table.add_row("Iteration", str(snapshot.iteration))
        table.add_row("Created At", snapshot.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"))

        started = snapshot.metadata.get("started_at", "N/A")
        completed = snapshot.metadata.get("completed_at", "N/A")
        table.add_row("Started", started)
        table.add_row("Completed", completed if completed != "N/A" else "[dim]In Progress[/]")

        if snapshot.notes:
            table.add_row("Notes", snapshot.notes)

        console.print(table)

        # Show iteration summary
        iterations = artifact_store.list_iterations(run_id)
        if iterations:
            console.print(f"\n[bold]Iterations:[/] {len(iterations)} recorded")
            for iter_file in iterations[-3:]:  # Show last 3 iterations
                record = artifact_store.load_iteration(run_id, iter_file)
                console.print(
                    f"  • Iter {record['iteration']} ({record['phase']}): "
                    f"{record.get('decision', {}).get('rationale', 'No decision recorded')}"
                )


@app.command()
def history(
    phase: Optional[str] = typer.Option(None, "--phase", help="Filter by phase (plan/implement/review/done/blocked)"),
    verdict: Optional[str] = typer.Option(None, "--verdict", help="Filter by review verdict (approve/changes_requested/blocked)"),
    since: Optional[str] = typer.Option(None, "--since", help="Filter runs created after this date (ISO format: YYYY-MM-DD)"),
    until: Optional[str] = typer.Option(None, "--until", help="Filter runs created before this date (ISO format: YYYY-MM-DD)"),
    contains: Optional[str] = typer.Option(None, "--contains", help="Search for text in run notes"),
    limit: int = typer.Option(20, help="Maximum number of runs to display"),
    format: Optional[str] = typer.Option(None, "--format", help="Output format: json for machine-readable export"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
) -> None:
    """List recent orchestration runs with advanced filtering."""
    from rich.table import Table

    duet_config = find_config(config)

    # Derive database path from run_artifact_dir
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[yellow]Database not found. Initialize with: uv run duet init[/]")
        console.print("[dim]Or use: uv run duet summary <run-id> for filesystem-based history[/]")
        return

    db = DuetDatabase(db_path)

    # Use search_runs for advanced filtering
    runs = db.search_runs(
        phase=phase,
        date_from=since,
        date_to=until,
        run_id_prefix=None,
        limit=limit,
    )

    # Additional filtering (verdict, contains) in Python
    if verdict:
        # Filter by verdict - need to check iterations for review verdicts
        filtered_runs = []
        for run in runs:
            iterations = db.list_iterations(run["run_id"])
            has_verdict = any(
                it.get("verdict") and verdict.lower() in it["verdict"].lower()
                for it in iterations
            )
            if has_verdict:
                filtered_runs.append(run)
        runs = filtered_runs

    if contains:
        # Filter by text in notes
        runs = [r for r in runs if r.get("notes") and contains.lower() in r["notes"].lower()]

    if not runs:
        console.print("[yellow]No runs found matching filters.[/]")
        return

    # JSON export mode
    if format == "json":
        import json
        console.print_json(data=runs)
        return

    # Display as table
    table = Table(title=f"Recent Runs (limit={limit})")
    table.add_column("Run ID", style="cyan")
    table.add_column("Phase", style="magenta")
    table.add_column("Iteration", justify="right")
    table.add_column("Started", style="dim")
    table.add_column("Status", style="green")

    for run in runs:
        # No emoji - use text labels
        status_label = "DONE" if run["phase"] == "done" else ("BLOCKED" if run["phase"] == "blocked" else "RUNNING")
        completed = "Complete" if run["completed_at"] else "In Progress"

        table.add_row(
            run["run_id"],
            run["phase"].upper(),
            str(run["iteration"]),
            run["started_at"][:19] if run["started_at"] else "N/A",
            f"{status_label}: {completed}",
        )

    console.print(table)
    console.print(f"\n[dim]Showing {len(runs)} runs (after filters)[/]")


@app.command()
def inspect(
    run_id: str = typer.Argument(..., help="Run ID to inspect"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    show_events: bool = typer.Option(True, "--show-events/--no-events", help="Display streaming events (Sprint 6)."),
    output: Optional[str] = typer.Option(None, "--output", help="Output format: json for structured export."),
) -> None:
    """Display detailed per-iteration information for a run."""
    from rich.panel import Panel
    from rich.table import Table

    duet_config = find_config(config)

    # Derive database path from run_artifact_dir
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[yellow]Database not found. Use: uv run duet summary <run-id>[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)

    # Get run info
    run = db.get_run(run_id)
    if not run:
        console.print(f"[red]Run not found: {run_id}[/]")
        raise typer.Exit(1)

    # Get iterations and events
    iterations = db.list_iterations(run_id)
    events = db.list_events(run_id) if show_events else []

    # JSON export mode
    if output == "json":
        import json
        export_data = {
            "run": run,
            "iterations": iterations,
            "events": events if show_events else [],
            "statistics": db.get_run_statistics(run_id),
        }
        console.print_json(data=export_data)
        return

    # Display run overview
    console.print(Panel(f"[bold cyan]Run: {run_id}[/]", expand=False))
    console.print(f"[bold]Phase:[/] {run['phase'].upper()}")
    console.print(f"[bold]Iterations:[/] {run['iteration']}")
    console.print(f"[bold]Started:[/] {run['started_at']}")
    console.print(f"[bold]Completed:[/] {run['completed_at'] or 'In Progress'}")
    if run["notes"]:
        console.print(f"[bold]Notes:[/] {run['notes']}")

    # Display statistics
    stats = db.get_run_statistics(run_id)
    console.print(f"\n[bold]Statistics:[/]")
    console.print(f"  Total Input Tokens: {stats['total_input_tokens']:,}")
    console.print(f"  Total Output Tokens: {stats['total_output_tokens']:,}")
    if stats["total_cached_tokens"] > 0:
        console.print(f"  Cached Tokens: {stats['total_cached_tokens']:,}")

    # Display iteration details
    if iterations:
        console.print(f"\n[bold]Iterations:[/] {len(iterations)}")
        table = Table()
        table.add_column("Iter", justify="right")
        table.add_column("Phase")
        table.add_column("Verdict")
        table.add_column("Git", justify="right")
        table.add_column("Tokens", justify="right")

        for iteration in iterations:
            verdict = iteration.get("verdict") or "-"
            git_info = (
                f"{iteration['files_changed'] or 0}f"
                if iteration.get("files_changed")
                else "-"
            )
            tokens = (
                f"{iteration['input_tokens'] or 0}/{iteration['output_tokens'] or 0}"
                if iteration.get("input_tokens")
                else "-"
            )

            table.add_row(
                str(iteration["iteration"]),
                iteration["phase"].upper(),
                verdict[:20],
                git_info,
                tokens,
            )

        console.print(table)

    # Display streaming events (Sprint 6)
    if show_events and events:
        console.print(f"\n[bold]Streaming Events:[/] {len(events)}")

        # Group events by iteration/phase
        from collections import defaultdict
        events_by_iter = defaultdict(list)
        for event in events:
            key = (event.get("iteration"), event.get("phase"))
            events_by_iter[key].append(event)

        # Display events grouped by iteration/phase
        for (iter_num, phase), iter_events in sorted(events_by_iter.items()):
            header = f"Iteration {iter_num} - {phase.upper() if phase else 'N/A'}"
            console.print(f"\n[cyan]{header}[/] ({len(iter_events)} events)")

            # Show sample of events (first 10)
            for event in iter_events[:10]:
                timestamp = event["timestamp"][:19] if event.get("timestamp") else "N/A"
                event_type = event["event_type"]

                # Format based on event type
                if event_type == "item.completed":
                    item_type = event["payload"].get("item", {}).get("type", "unknown")
                    console.print(f"  [dim]{timestamp}[/] [green]✓[/] {event_type} ({item_type})")
                elif event_type == "turn.completed":
                    tokens = event["payload"].get("usage", {}).get("output_tokens", 0)
                    console.print(f"  [dim]{timestamp}[/] [green]✓[/] {event_type} ({tokens} tokens)")
                elif event_type == "parse_error":
                    console.print(f"  [dim]{timestamp}[/] [red]✗[/] {event_type}")
                else:
                    console.print(f"  [dim]{timestamp}[/] {event_type}")

            if len(iter_events) > 10:
                console.print(f"  [dim]... and {len(iter_events) - 10} more events[/]")
    elif show_events and not events:
        console.print("\n[dim]No streaming events recorded for this run[/]")


@app.command()
def summary(
    run_id: str = typer.Argument(..., help="Run ID to generate summary for."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    save: bool = typer.Option(False, "--save", help="Save summary to JSON file."),
) -> None:
    """Display a comprehensive summary of a run's iteration history."""
    from rich.panel import Panel
    from rich.table import Table

    duet_config = find_config(config)
    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)

    # Generate summary
    try:
        summary_data = artifact_store.generate_run_summary(run_id)
    except Exception as exc:
        console.print(f"[red]Failed to generate summary: {exc}[/]")
        raise typer.Exit(1)

    # Display overview
    stats = summary_data["statistics"]
    console.print(Panel(f"[bold cyan]Run Summary: {run_id}[/]", expand=False))
    console.print(f"[bold]Total Iterations:[/] {stats['total_iterations']}")
    console.print(f"[bold]Final Phase:[/] {stats['final_phase'].upper()}")
    console.print(
        f"[bold]Phase Breakdown:[/] "
        f"PLAN={stats['phase_counts']['plan']}, "
        f"IMPLEMENT={stats['phase_counts']['implement']}, "
        f"REVIEW={stats['phase_counts']['review']}"
    )

    # Display iteration table
    if summary_data["iterations"]:
        console.print("\n[bold]Iteration History:[/]")
        table = Table()
        table.add_column("Iter", style="cyan", justify="right")
        table.add_column("Phase", style="magenta")
        table.add_column("Decision", style="green")
        table.add_column("Next", style="yellow")

        for iteration in summary_data["iterations"]:
            table.add_row(
                str(iteration["iteration"]),
                iteration["phase"].upper(),
                iteration["decision"][:60] + "..." if len(iteration["decision"]) > 60 else iteration["decision"],
                str(iteration["next_phase"]),
            )

        console.print(table)

    # Save to file if requested
    if save:
        summary_path = artifact_store.save_run_summary(run_id)
        console.print(f"\n[green]Summary saved to:[/] {summary_path}")


@app.command()
def migrate(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    force: bool = typer.Option(False, "--force", help="Re-migrate runs that already exist in database"),
) -> None:
    """Migrate filesystem artifacts to SQLite database."""
    duet_config = find_config(config)

    # Derive database path from run_artifact_dir
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    # Initialize database (schema auto-created if needed)
    db = DuetDatabase(db_path)

    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)
    migrator = ArtifactMigrator(artifact_store, db, console=console)

    console.print()
    console.print("[cyan bold]Starting artifact migration to SQLite...[/]")
    console.print()

    migrator.migrate_all(force=force)

    console.print()
    console.print("[green]Migration complete. Use 'duet history' to view runs.[/]")


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 8: Stateful CLI Workflow
# ──────────────────────────────────────────────────────────────────────────────


@app.command()
def next(
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Run ID to continue (creates new run if not provided)"),
    feedback: Optional[str] = typer.Option(None, "--feedback", help="User feedback to include in the next phase"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable streaming console output."),
    stream_mode: Optional[str] = typer.Option(None, "--stream-mode", help="Streaming display mode: detailed | compact | off."),
) -> None:
    """Execute the next phase for a stateful run (Sprint 8)."""
    duet_config = find_config(config)

    # Override quiet mode if CLI flag provided
    if quiet:
        duet_config.logging.quiet = True
        duet_config.logging.stream_mode = StreamMode.OFF

    # Override stream_mode if CLI flag provided
    if stream_mode:
        valid_modes = [mode.value for mode in StreamMode]
        if stream_mode not in valid_modes:
            console.print(f"[red]Invalid --stream-mode: {stream_mode}[/]")
            console.print(f"[yellow]Valid options: {', '.join(valid_modes)}[/]")
            raise typer.Exit(1)
        duet_config.logging.stream_mode = StreamMode(stream_mode)

    # Database is required for stateful workflow
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)
    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)

    # Create orchestrator
    orchestrator = Orchestrator(duet_config, artifact_store, console=console, db=db)

    # Execute next phase
    try:
        result = orchestrator.run_next_phase(run_id=run_id, feedback=feedback)

        # Display result
        console.print()
        console.rule("[bold green]Phase Complete[/]")
        console.print(f"[bold]Run ID:[/] {result['run_id']}")
        console.print(f"[bold]State ID:[/] {result['state_id']}")
        console.print(f"[bold]Phase:[/] {result['phase'].upper()}")
        console.print(f"[bold]Status:[/] {result['phase_status']}")
        console.print(f"[bold]Next Action:[/] {result['next_action']}")
        console.print(f"[dim]{result['message']}[/]")

        # Suggest next command
        if result["next_action"] == "continue":
            console.print(f"\n[cyan]Run 'duet next --run-id {result['run_id']}' to continue[/]")
        elif result["next_action"] == "done":
            console.print(f"\n[green]Run completed successfully![/]")
        elif result["next_action"] == "blocked":
            console.print(f"\n[yellow]Run blocked. Review state with 'duet status {result['run_id']}'[/]")

    except Exception as exc:
        console.print(f"[red]Error: {exc}[/]")
        raise typer.Exit(1)


@app.command()
def cont(
    run_id: str = typer.Argument(..., help="Run ID to continue"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    max_phases: int = typer.Option(10, help="Maximum phases to execute before stopping"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable streaming console output."),
    stream_mode: Optional[str] = typer.Option(None, "--stream-mode", help="Streaming display mode: detailed | compact | off."),
) -> None:
    """Continue executing phases until done or blocked (Sprint 8)."""
    duet_config = find_config(config)

    # Override quiet mode if CLI flag provided
    if quiet:
        duet_config.logging.quiet = True
        duet_config.logging.stream_mode = StreamMode.OFF

    # Override stream_mode if CLI flag provided
    if stream_mode:
        valid_modes = [mode.value for mode in StreamMode]
        if stream_mode not in valid_modes:
            console.print(f"[red]Invalid --stream-mode: {stream_mode}[/]")
            console.print(f"[yellow]Valid options: {', '.join(valid_modes)}[/]")
            raise typer.Exit(1)
        duet_config.logging.stream_mode = StreamMode(stream_mode)

    # Database is required
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)
    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)

    # Create orchestrator
    orchestrator = Orchestrator(duet_config, artifact_store, console=console, db=db)

    console.print(f"[cyan]Continuing run:[/] {run_id}")
    console.print()

    # Execute phases until done/blocked
    phases_executed = 0
    try:
        while phases_executed < max_phases:
            result = orchestrator.run_next_phase(run_id=run_id)
            phases_executed += 1

            console.print(f"\n[dim]Phase {phases_executed}: {result['phase']} → {result['phase_status']}[/]")

            if result["next_action"] in ("done", "blocked"):
                break

        # Display final result
        console.print()
        console.rule("[bold green]Run Complete[/]")
        console.print(f"[bold]Run ID:[/] {result['run_id']}")
        console.print(f"[bold]Final State:[/] {result['state_id']}")
        console.print(f"[bold]Phases Executed:[/] {phases_executed}")
        console.print(f"[bold]Status:[/] {result['next_action']}")

        if result["next_action"] == "done":
            console.print(f"\n[green]Run completed successfully![/]")
        elif result["next_action"] == "blocked":
            console.print(f"\n[yellow]Run blocked. Review state with 'duet status {run_id}'[/]")
        elif phases_executed >= max_phases:
            console.print(f"\n[yellow]Stopped after {max_phases} phases. Run 'duet cont {run_id}' to continue.[/]")

    except Exception as exc:
        console.print(f"[red]Error: {exc}[/]")
        raise typer.Exit(1)


@app.command()
def back(
    state_id: str = typer.Argument(..., help="State ID to restore to"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    force: bool = typer.Option(False, "--force", help="Force restore even if working tree is dirty"),
) -> None:
    """Restore git workspace and database to a previous state (Sprint 8)."""
    duet_config = find_config(config)

    # Database is required
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)

    # Load state
    state = db.get_state(state_id)
    if not state:
        console.print(f"[red]State not found: {state_id}[/]")
        raise typer.Exit(1)

    run_id = state["run_id"]
    console.print(f"[cyan]Restoring state:[/] {state_id}")
    console.print(f"[cyan]Run ID:[/] {run_id}")
    console.print(f"[cyan]Phase Status:[/] {state['phase_status']}")

    # Restore git workspace if baseline available
    from .git_operations import GitWorkspace, GitError

    git = GitWorkspace(duet_config.storage.workspace_root, console=console)

    if state.get("baseline_commit") and git.is_git_repo():
        try:
            # Get state metadata
            metadata = state.get("metadata") or {}
            state_branch = metadata.get("state_branch")
            original_branch = metadata.get("branch")

            console.print(f"[cyan]Restoring git baseline:[/] {state['baseline_commit'][:8]}")
            git.restore_state(
                baseline_commit=state["baseline_commit"],
                original_branch=original_branch,
                state_branch=state_branch,
                force=force,
            )
        except GitError as exc:
            console.print(f"[red]Git restoration failed: {exc}[/]")
            raise typer.Exit(1)
    else:
        console.print("[yellow]No git baseline available for this state[/]")

    # Update active state in database
    db.update_active_state(run_id, state_id)

    console.print()
    console.print(f"[green]State restored successfully![/]")
    console.print(f"[dim]Active state set to: {state_id}[/]")
    console.print(f"\n[cyan]Run 'duet next --run-id {run_id}' to continue from this state[/]")
