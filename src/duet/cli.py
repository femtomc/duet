"""CLI interface for the Duet orchestrator."""

from __future__ import annotations

import datetime as dt
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
    init_git: bool = typer.Option(
        False, "--init-git", help="Initialize git repository with .gitignore and initial commit"
    ),
    model_codex: str = typer.Option(
        "gpt-5-codex", "--model-codex", help="Codex model for planning/review"
    ),
    model_claude: str = typer.Option(
        "sonnet", "--model-claude", help="Claude Code model for implementation"
    ),
) -> None:
    """
    Initialize a new Duet workspace with configuration and workflow definition.

    Creates .duet/ directory with:
    - duet.yaml: Configuration (models, guardrails, logging)
    - workflow.py: Workflow definition using Python DSL
    - context/: Repository discovery outputs
    - runs/, logs/: Artifact directories
    - duet.db: SQLite database for state management
    """
    try:
        initializer = DuetInitializer(
            workspace_root=workspace,
            config_path=config_path,
            force=force,
            skip_discovery=skip_discovery,
            init_git=init_git,
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
    workflow: Optional[Path] = typer.Option(
        None, "--workflow", help="Workflow file path (defaults to .duet/workflow.py)"
    ),
    run_id: Optional[str] = typer.Option(None, help="Override generated run identifier."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Disable streaming console output."
    ),
    stream_mode: Optional[str] = typer.Option(
        None, "--stream-mode", help="Streaming display mode: detailed | compact | off."
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

    # Load workflow
    workflow_path = Path(workflow) if workflow else Path(".duet/workflow.py")

    try:
        from .workflow_loader import load_facet_program

        program = load_facet_program(workflow_path, workspace_root=duet_config.storage.workspace_root)
        console.log(f"[dim]Loaded workflow from {workflow_path}[/]")
        console.log(f"[dim]Workflow has {len(program.handles)} facet(s)[/]")
    except Exception as e:
        console.print(f"[red]Failed to load workflow: {e}[/]")
        console.print(f"[yellow]Check {workflow_path} for errors[/]")
        raise typer.Exit(1)

    # Get adapter
    from .adapters import get_adapter

    try:
        adapter = get_adapter(duet_config.codex)
    except Exception as e:
        console.print(f"[red]Failed to initialize adapter: {e}[/]")
        raise typer.Exit(1)

    # Generate run_id if not provided
    if not run_id:
        import uuid
        run_id = f"run_{uuid.uuid4().hex[:8]}"

    try:
        orchestrator = Orchestrator(
            duet_config,
            artifact_store,
            console=console,
            db=db,
            workspace_root=str(duet_config.storage.workspace_root)
        )

        result = orchestrator.run(
            program=program,
            run_id=run_id,
            adapter=adapter,
            max_iterations=duet_config.workflow.max_iterations
        )

        if not result.success:
            console.print(f"\n[red]Orchestration failed: {result.error or 'Incomplete execution'}[/]")
            console.print(f"[yellow]Facets executed: {result.facets_executed}[/]")
            console.print(f"[yellow]Facets waiting: {len(result.waiting_facets)}[/]")
            raise typer.Exit(1)

        console.print(f"\n[green]✓ Orchestration completed successfully[/]")
        console.print(f"[dim]Run ID: {run_id}[/]")
        console.print(f"[dim]Facets executed: {result.facets_executed}[/]")
    except Exception as exc:
        # Handle adapter and orchestration errors with friendly messages
        error_msg = str(exc)

        # Check for workflow load errors
        if "failed to load workflow" in error_msg.lower() or "workflow validation failed" in error_msg.lower():
            console.print("[red bold]Workflow Error:[/]")
            # Extract the core error message without traceback
            lines = error_msg.split("\n")
            for line in lines[:5]:  # Show first few lines only
                console.print(f"[red]{line}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • Run 'duet lint' to validate your workflow")
            console.print("  • Check .duet/workflow.py for syntax errors")
            console.print("  • See docs/workflow_dsl.md for DSL reference")
            raise typer.Exit(1)
        # Check for adapter-specific errors
        elif "not found" in error_msg.lower() and ("codex" in error_msg.lower() or "claude" in error_msg.lower()):
            console.print("[red bold]Adapter Error:[/]")
            console.print(f"[red]{error_msg}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • For testing without real adapters, set provider: 'echo' in .duet/duet.yaml")
            console.print("  • Install the required CLI: 'codex' or 'claude'")
            console.print("  • Ensure the CLI is authenticated (e.g., 'codex auth login')")
            raise typer.Exit(1)
        elif "permission denied" in error_msg.lower():
            console.print("[red bold]Adapter Permission Error:[/]")
            console.print(f"[red]{error_msg}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • Check file permissions for CLI executables")
            console.print("  • For testing, use provider: 'echo' in .duet/duet.yaml")
            raise typer.Exit(1)
        else:
            # Re-raise other errors
            raise


@app.command()
def lint(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    workflow: Optional[Path] = typer.Option(None, "--workflow", help="Workflow file path (defaults to .duet/workflow.py)"),
) -> None:
    """
    Validate facet workflow without executing.

    Loads the workflow, runs validation, and reports any errors.
    """
    from .workflow_loader import load_and_validate
    from .dsl.compiler import validate_and_compile

    workflow_path = Path(workflow) if workflow else Path(".duet/workflow.py")

    console.print(f"[bold]Linting workflow: {workflow_path}[/]")

    # Load and validate
    program, load_errors = load_and_validate(workflow_path)

    if load_errors:
        console.print("[red]✗ Workflow loading failed:[/]")
        for error in load_errors:
            console.print(f"  [red]{error}[/]")
        raise typer.Exit(1)

    console.print(f"[green]✓ Workflow loaded successfully[/]")
    console.print(f"  Facets: {len(program.handles)}")

    # Run compiler validation
    try:
        registrations = validate_and_compile(program)
        console.print(f"[green]✓ Compilation successful[/]")
        console.print(f"  Registrations: {len(registrations)}")

        # Show facet summary
        console.print("\n[bold]Facet Summary:[/]")
        for handle in program.handles:
            facet_def = handle.definition
            console.print(f"  • {facet_def.name}")
            console.print(f"      Needs: {[t.__name__ for t in facet_def.alias_map.values()] or 'none'}")
            console.print(f"      Emits: {[t.__name__ for t in facet_def.emitted_facts] or 'none'}")
            console.print(f"      Triggers: {len(handle.triggers)} pattern(s)")
            console.print(f"      Policy: {handle.policy.value}")

        console.print("\n[green bold]✓ Workflow validation passed[/]")

    except ValueError as e:
        console.print(f"[red]✗ Compilation failed:[/]")
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)


@app.command()
def status(
    run_id: str = typer.Argument(..., help="Run ID to check status for."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    show_states: bool = typer.Option(True, "--show-states/--no-states", help="Show state history"),
) -> None:
    """Display the current status of a run with state history and active facts."""
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

        # Show active state
        active_state = db.get_active_state(run_id)
        if active_state:
            table.add_row("Active State", active_state["state_id"])
            table.add_row("Phase Status", active_state["phase_status"])
            if active_state.get("baseline_commit"):
                table.add_row("Git Baseline", active_state["baseline_commit"][:8])

        if run.get("notes"):
            table.add_row("Notes", run["notes"])

        console.print(table)

        # Show state history
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

        # Show facts (latest per fact type)
        try:
            facts = db.get_facts(run_id, active_only=True)
            if facts:
                console.print(f"\n[bold]Active Facts:[/] {len(facts)} facts")
                fact_table = Table()
                fact_table.add_column("Fact Type", style="cyan")
                fact_table.add_column("Fact ID", style="green")
                fact_table.add_column("Created", style="dim")

                for fact_record in facts[:10]:  # Show first 10
                    fact_table.add_row(
                        fact_record["fact_type"],
                        fact_record["fact_id"][:16] + "..." if len(fact_record["fact_id"]) > 16 else fact_record["fact_id"],
                        fact_record["created_at"][:19] if fact_record["created_at"] else "-",
                    )

                console.print(fact_table)

                if len(facts) > 10:
                    console.print(f"[dim]  ... and {len(facts) - 10} more facts[/]")
                    console.print(f"[dim]  Use 'duet facts {run_id}' to see all facts[/]")
        except Exception as exc:
            console.log(f"[dim]Facts unavailable: {exc}[/]")

    else:
        # Database required for status
        console.print(f"[red]Database not found at: {db_path}[/]")
        console.print("[yellow]Run 'duet init' to initialize database[/]")
        raise typer.Exit(1)


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
    show_events: bool = typer.Option(True, "--show-events/--no-events", help="Display streaming events."),
    show_channels: bool = typer.Option(True, "--show-channels/--no-channels", help="Display channel history."),
    channel: Optional[str] = typer.Option(None, "--channel", help="Filter by specific channel name."),
    output: Optional[str] = typer.Option(None, "--output", help="Output format: json for structured export."),
) -> None:
    """Display detailed per-iteration information for a run with channel history."""
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
    messages = db.list_messages(run_id, channel=channel) if show_channels else []

    # JSON export mode
    if output == "json":
        import json
        export_data = {
            "run": run,
            "iterations": iterations,
            "events": events if show_events else [],
            "messages": messages if show_channels else [],
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

    # Display streaming events
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

    # Display channel history
    if show_channels:
        # Limit messages to avoid materializing huge result sets
        messages = db.list_messages(run_id, channel=channel, limit=100)
        if messages:
            console.print(f"\n[bold]Channel History:[/] {len(messages)} messages")
            if channel:
                console.print(f"[dim]Filtered by channel: {channel}[/]")

            channel_table = Table()
            channel_table.add_column("Channel", style="cyan")
            channel_table.add_column("Value", style="green")
            channel_table.add_column("Phase", style="magenta")
            channel_table.add_column("Iteration", justify="right")
            channel_table.add_column("Created", style="dim")

            for msg in messages[:20]:  # Show first 20 messages
                # Trim to first line and limit to 60 chars for inspect (slightly more detail than status)
                payload_str = str(msg["payload"])
                first_line = payload_str.split("\n")[0]
                payload_preview = first_line[:60]
                if len(payload_str) > 60 or "\n" in payload_str:
                    payload_preview += "..."

                channel_table.add_row(
                    msg["channel"],
                    payload_preview,
                    msg["phase"] or "-",
                    str(msg["iteration"]) if msg["iteration"] else "-",
                    msg["created_at"][:19] if msg["created_at"] else "-",
                )

            console.print(channel_table)

            if len(messages) > 20:
                console.print(f"\n[dim]... and {len(messages) - 20} more messages[/]")
                console.print(f"[dim]Use --channel <name> to filter or --output json for full export[/]")
        elif show_channels:
            console.print("\n[dim]No channel messages recorded for this run[/]")


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
    feedback: Optional[str] = typer.Argument(None, help="User feedback to include in the next phase"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Run ID to continue (auto-resumes most recent if not provided)"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    workflow: Optional[Path] = typer.Option(
        None, "--workflow", help="Workflow file path (defaults to .duet/workflow.py)"
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable streaming console output."),
    stream_mode: Optional[str] = typer.Option(None, "--stream-mode", help="Streaming display mode: detailed | compact | off."),
) -> None:
    """
    Execute the next phase for a stateful run.

    Examples:
        duet next                      # Auto-resume most recent run
        duet next "try variant B"      # Provide feedback
        duet next --run-id run-123     # Target specific run
        duet next "fix errors" --run-id run-123
    """
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
    orchestrator = Orchestrator(duet_config, artifact_store, console=console, db=db, workflow_path=workflow)

    # Auto-resume: If no run_id specified, find the most recent active run
    if not run_id:
        # Query for most recent run that's not done/blocked
        recent_runs = db.search_runs(limit=10)
        for run in recent_runs:
            # Skip completed or blocked runs
            if run["phase"] not in ("done", "blocked"):
                # Check if it has an active state
                active_state = db.get_active_state(run["run_id"])
                if active_state and active_state["phase_status"] not in ("done", "blocked"):
                    run_id = run["run_id"]
                    console.print(f"[dim]Auto-resuming run: {run_id}[/]")
                    break

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
        # Handle adapter and orchestration errors with friendly messages
        error_msg = str(exc)

        # Check for workflow load errors
        if "failed to load workflow" in error_msg.lower() or "workflow validation failed" in error_msg.lower():
            console.print("[red bold]Workflow Error:[/]")
            # Extract the core error message without traceback
            lines = error_msg.split("\n")
            for line in lines[:5]:  # Show first few lines only
                console.print(f"[red]{line}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • Run 'duet lint' to validate your workflow")
            console.print("  • Check .duet/workflow.py for syntax errors")
            console.print("  • See docs/workflow_dsl.md for DSL reference")
            raise typer.Exit(1)
        # Check for adapter-specific errors
        elif "not found" in error_msg.lower() and ("codex" in error_msg.lower() or "claude" in error_msg.lower()):
            console.print("[red bold]Adapter Error:[/]")
            console.print(f"[red]{error_msg}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • For testing without real adapters, set provider: 'echo' in .duet/duet.yaml")
            console.print("  • Install the required CLI: 'codex' or 'claude'")
            console.print("  • Ensure the CLI is authenticated (e.g., 'codex auth login')")
            raise typer.Exit(1)
        elif "permission denied" in error_msg.lower():
            console.print("[red bold]Adapter Permission Error:[/]")
            console.print(f"[red]{error_msg}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • Check file permissions for CLI executables")
            console.print("  • For testing, use provider: 'echo' in .duet/duet.yaml")
            raise typer.Exit(1)
        else:
            # Default error handling
            console.print(f"[red]Error: {error_msg}[/]")
            raise typer.Exit(1)


@app.command()
def cont(
    run_id: str = typer.Argument(..., help="Run ID to continue"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    workflow: Optional[Path] = typer.Option(
        None, "--workflow", help="Workflow file path (defaults to .duet/workflow.py)"
    ),
    max_phases: int = typer.Option(10, help="Maximum phases to execute before stopping"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable streaming console output."),
    stream_mode: Optional[str] = typer.Option(None, "--stream-mode", help="Streaming display mode: detailed | compact | off."),
) -> None:
    """Continue executing phases until done or blocked."""
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
    orchestrator = Orchestrator(duet_config, artifact_store, console=console, db=db, workflow_path=workflow)

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
        # Handle adapter and orchestration errors with friendly messages
        error_msg = str(exc)

        # Check for workflow load errors
        if "failed to load workflow" in error_msg.lower() or "workflow validation failed" in error_msg.lower():
            console.print("[red bold]Workflow Error:[/]")
            # Extract the core error message without traceback
            lines = error_msg.split("\n")
            for line in lines[:5]:  # Show first few lines only
                console.print(f"[red]{line}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • Run 'duet lint' to validate your workflow")
            console.print("  • Check .duet/workflow.py for syntax errors")
            console.print("  • See docs/workflow_dsl.md for DSL reference")
            raise typer.Exit(1)
        # Check for adapter-specific errors
        elif "not found" in error_msg.lower() and ("codex" in error_msg.lower() or "claude" in error_msg.lower()):
            console.print("[red bold]Adapter Error:[/]")
            console.print(f"[red]{error_msg}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • For testing without real adapters, set provider: 'echo' in .duet/duet.yaml")
            console.print("  • Install the required CLI: 'codex' or 'claude'")
            console.print("  • Ensure the CLI is authenticated (e.g., 'codex auth login')")
            raise typer.Exit(1)
        elif "permission denied" in error_msg.lower():
            console.print("[red bold]Adapter Permission Error:[/]")
            console.print(f"[red]{error_msg}[/]")
            console.print()
            console.print("[yellow]Suggestions:[/]")
            console.print("  • Check file permissions for CLI executables")
            console.print("  • For testing, use provider: 'echo' in .duet/duet.yaml")
            raise typer.Exit(1)
        else:
            # Default error handling
            console.print(f"[red]Error: {error_msg}[/]")
            raise typer.Exit(1)


@app.command()
def back(
    state_id: str = typer.Argument(..., help="State ID to restore to"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
    force: bool = typer.Option(False, "--force", help="Force restore even if working tree is dirty"),
) -> None:
    """Restore git workspace and database to a previous state."""
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
        console.print("[yellow bold]⚠ No git baseline available for this state[/]")
        console.print(
            "[yellow]This state was created without git commits. Workspace changes cannot be restored.[/]"
        )
        console.print("[dim]To enable time travel in future runs:[/]")
        console.print("  • Run: [cyan]duet init --init-git --force[/] [dim]to set up git[/]")
        console.print("  • Or manually: [cyan]git init && git add . && git commit -m 'Initial commit'[/]")

    # Update active state in database
    db.update_active_state(run_id, state_id)

    console.print()
    console.print(f"[green]State restored successfully![/]")
    console.print(f"[dim]Active state set to: {state_id}[/]")
    console.print(f"\n[cyan]Run 'duet next --run-id {run_id}' to continue from this state[/]")


@app.command()
def messages(
    run_id: str = typer.Argument(..., help="Run ID to query messages for"),
    channel: Optional[str] = typer.Option(None, "--channel", help="Filter by channel name"),
    phase: Optional[str] = typer.Option(None, "--phase", help="Filter by phase"),
    limit: int = typer.Option(50, "--limit", help="Maximum messages to display"),
    output: Optional[str] = typer.Option(None, "--output", help="Output format: json for export"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
) -> None:
    """Display channel message history for a run."""
    from rich.table import Table

    duet_config = find_config(config)

    # Database required
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)

    # Get messages
    msgs = db.list_messages(run_id, channel=channel, phase=phase, limit=limit)

    if not msgs:
        console.print(f"[yellow]No messages found for run: {run_id}[/]")
        if channel:
            console.print(f"[dim]Channel filter: {channel}[/]")
        if phase:
            console.print(f"[dim]Phase filter: {phase}[/]")
        return

    # JSON export
    if output == "json":
        console.print_json(data=msgs)
        return

    # Display as table
    console.print(f"[bold]Channel Messages:[/] {run_id}")
    console.print(f"[dim]Showing newest messages first[/]")
    if channel:
        console.print(f"[dim]Filtered by channel: {channel}[/]")
    if phase:
        console.print(f"[dim]Filtered by phase: {phase}[/]")
    console.print()

    table = Table()
    table.add_column("Channel", style="cyan")
    table.add_column("Value", style="green")
    table.add_column("Phase", style="magenta")
    table.add_column("Iteration", justify="right")
    table.add_column("Created", style="dim")

    for msg in msgs:
        payload_preview = str(msg["payload"])[:100]
        if len(str(msg["payload"])) > 100:
            payload_preview += "..."

        table.add_row(
            msg["channel"],
            payload_preview,
            msg["phase"] or "-",
            str(msg["iteration"]) if msg["iteration"] else "-",
            msg["created_at"][:19] if msg["created_at"] else "-",
        )

    console.print(table)
    console.print(f"\n[dim]Showing {len(msgs)} messages{f' (limited to {limit})' if len(msgs) == limit else ''}[/]")


@app.command()
def seed(
    fact_type: str = typer.Argument(..., help="Fact type name (e.g., TaskRequest, PlanDoc)"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Run ID to associate fact with"),
    fact_id: Optional[str] = typer.Option(None, "--fact-id", help="Custom fact ID (auto-generated if not provided)"),
    data: str = typer.Option("{}", "--data", help="JSON dict of fact field values"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
) -> None:
    """
    Assert an initial fact into the dataspace to seed a workflow.

    Use this to provide input facts that typed workflows depend on.

    Usage:
        duet seed TaskRequest --data '{"task_description": "Build auth system", "priority": 1}'
        duet seed PlanDoc --run-id abc123 --data '{"task_id": "t1", "content": "Plan..."}'
    """
    import json
    import uuid

    from .dataspace import FactRegistry

    duet_config = find_config(config)

    # Database required
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)

    # Get or create run_id
    if not run_id:
        # Create new run
        run_id = f"run_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        console.print(f"[cyan]Creating new run:[/] {run_id}")
        # Insert run record
        from .models import RunSnapshot
        snapshot = RunSnapshot(run_id=run_id, iteration=0, phase="seeding")
        db.insert_run(snapshot)
    else:
        # Verify run exists
        run = db.get_run(run_id)
        if not run:
            console.print(f"[red]Run not found:[/] {run_id}")
            raise typer.Exit(1)

    # Parse data JSON
    try:
        field_values = json.loads(data)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON in --data:[/] {e}")
        raise typer.Exit(1)

    # Get fact class from registry
    fact_class = FactRegistry.get(fact_type)
    if not fact_class:
        console.print(f"[red]Unknown fact type:[/] {fact_type}")
        console.print(f"[dim]Available types: {', '.join(FactRegistry.all_types().keys())}[/]")
        raise typer.Exit(1)

    # Generate fact_id if not provided
    if not fact_id:
        if "fact_id" not in field_values:
            fact_id = f"{fact_type.lower()}_{uuid.uuid4().hex[:8]}"
            field_values["fact_id"] = fact_id
    else:
        field_values["fact_id"] = fact_id

    # Construct fact
    try:
        fact = fact_class(**field_values)
    except TypeError as e:
        console.print(f"[red]Failed to construct {fact_type}:[/] {e}")
        console.print(f"[dim]Provided fields: {list(field_values.keys())}[/]")
        raise typer.Exit(1)

    # Save to database
    db.save_fact(run_id, fact)

    console.print(f"[green]✓ Fact asserted![/]")
    console.print(f"[dim]Type: {fact_type}[/]")
    console.print(f"[dim]Fact ID: {field_values['fact_id']}[/]")
    console.print(f"[dim]Run ID: {run_id}[/]")
    console.print()
    console.print(f"[cyan]Continue workflow with:[/] duet next --run-id {run_id}")


@app.command()
def facts(
    run_id: str = typer.Argument(..., help="Run ID to inspect facts for"),
    fact_type: Optional[str] = typer.Option(None, "--type", help="Filter by fact type"),
    active_only: bool = typer.Option(True, "--active-only/--all", help="Show only active (non-retracted) facts"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
) -> None:
    """
    Inspect facts in the dataspace for a run.

    Shows all typed facts asserted during workflow execution.

    Usage:
        duet facts RUN_ID
        duet facts RUN_ID --type ApprovalRequest
        duet facts RUN_ID --all  # Include retracted facts
    """
    from rich.table import Table

    duet_config = find_config(config)

    # Database required
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)

    # Verify run exists
    run = db.get_run(run_id)
    if not run:
        console.print(f"[red]Run not found:[/] {run_id}")
        raise typer.Exit(1)

    # Query facts
    fact_records = db.get_facts(run_id, fact_type=fact_type, active_only=active_only)

    console.print(f"[bold]Facts for run:[/] {run_id}")
    if fact_type:
        console.print(f"[dim]Filtered by type: {fact_type}[/]")
    console.print(f"[dim]Status: {'Active only' if active_only else 'All (including retracted)'}[/]")
    console.print()

    if not fact_records:
        console.print("[yellow]No facts found[/]")
        return

    # Display as table
    table = Table()
    table.add_column("Fact Type", style="cyan")
    table.add_column("Fact ID", style="green")
    table.add_column("Created", style="dim")
    table.add_column("Status", style="magenta")
    table.add_column("Fields", style="white")

    for record in fact_records:
        payload = record["payload"]
        # Remove fact_id from display (already in column)
        display_fields = {k: v for k, v in payload.items() if k != "fact_id"}
        fields_preview = str(display_fields)[:60]
        if len(str(display_fields)) > 60:
            fields_preview += "..."

        status = "retracted" if record["retracted_at"] else "active"
        status_style = "red" if record["retracted_at"] else "green"

        table.add_row(
            record["fact_type"],
            record["fact_id"][:20] + "..." if len(record["fact_id"]) > 20 else record["fact_id"],
            record["created_at"][:19],
            f"[{status_style}]{status}[/]",
            fields_preview,
        )

    console.print(table)
    console.print(f"\n[dim]Showing {len(fact_records)} fact(s)[/]")


@app.command()
def approve(
    run_id: str = typer.Argument(..., help="Run ID that is awaiting approval"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Optional approval notes/feedback"),
    approver: str = typer.Option("user", "--approver", help="Approver name (default: user)"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
) -> None:
    """
    Grant approval for a paused workflow run.

    When a workflow pauses at a HumanStep (approval required), this command
    asserts an ApprovalGrant fact into the dataspace, allowing the workflow
    to resume execution.

    Usage:
        duet approve RUN_ID --notes "Looks good, approved!"
    """
    import uuid

    duet_config = find_config(config)

    # Database required
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[red]Database not found. Initialize with: uv run duet init[/]")
        raise typer.Exit(1)

    db = DuetDatabase(db_path)

    # Check if run exists and is blocked
    run = db.get_run(run_id)
    if not run:
        console.print(f"[red]Run not found:[/] {run_id}")
        raise typer.Exit(1)

    if run["status"] != "blocked":
        console.print(f"[yellow]Run is not awaiting approval:[/] status={run['status']}")
        console.print(f"[dim]Only runs with status='blocked' require approval[/]")
        raise typer.Exit(1)

    console.print(f"[cyan]Run:[/] {run_id}")
    console.print(f"[dim]Status:[/] {run['status']}")
    console.print(f"[dim]Approval reason:[/] {run.get('approval_reason', 'N/A')}")
    console.print()

    # Query persisted ApprovalRequest facts from database
    approval_facts = db.get_facts(run_id, fact_type="ApprovalRequest", active_only=True)

    if not approval_facts:
        console.print(
            f"[yellow]No pending approval requests found for run {run_id}[/]"
        )
        console.print(f"[dim]The approval request may have been completed or expired[/]")
        # Continue anyway - update run status
    else:
        console.print(f"[dim]Found {len(approval_facts)} pending approval request(s)[/]")
        for fact_data in approval_facts:
            payload = fact_data["payload"]
            console.print(
                f"  • [cyan]{payload['fact_id']}[/] from {payload.get('requester', 'unknown')}: {payload.get('reason', 'N/A')}"
            )

    # Create ApprovalGrant fact
    # Use the first pending request ID if available, or generate a generic grant
    if approval_facts:
        request_id = approval_facts[0]["payload"]["fact_id"]
    else:
        # No pending requests - grant approval generically
        request_id = f"approval_request_{run_id}"

    grant_id = f"approval_grant_{uuid.uuid4().hex[:8]}"
    grant_payload = {
        "fact_id": grant_id,
        "request_id": request_id,
        "approver": approver,
        "notes": notes,
        "metadata": {
            "run_id": run_id,
            "granted_at": str(db.now()),
        },
    }

    # Save ApprovalGrant to database (so orchestrator can find it)
    from .dataspace import ApprovalGrant

    grant = ApprovalGrant(**grant_payload)
    db.save_fact(run_id, grant)

    console.print()
    console.print(f"[green]✓ Approval granted![/]")
    console.print(f"[dim]Grant ID: {grant_id}[/]")
    console.print(f"[dim]Request ID: {request_id}[/]")
    if notes:
        console.print(f"[dim]Notes: {notes}[/]")

    # Retract the approval request (it's been handled)
    if approval_facts:
        db.retract_fact(approval_facts[0]["fact_id"])

    # Update run status in database
    db.update_run_status(run_id, "active")
    console.print()
    console.print(f"[cyan]Run status updated to 'active'[/]")
    console.print(f"\n[green]Continue the run with:[/] duet next --run-id {run_id}")
    console.print(f"[dim]Or auto-resume:[/] duet cont {run_id}")
