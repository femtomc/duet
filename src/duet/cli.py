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
) -> None:
    """Execute the duet orchestration loop."""
    duet_config = find_config(config)

    # Override quiet mode if CLI flag provided
    if quiet:
        duet_config.logging.quiet = True

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
) -> None:
    """Display the current status of a run from its checkpoint."""
    from rich.table import Table

    duet_config = find_config(config)
    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)

    # Load checkpoint
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
    phase: Optional[str] = typer.Option(None, help="Filter by phase (plan/implement/review/done/blocked)"),
    limit: int = typer.Option(20, help="Maximum number of runs to display"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
) -> None:
    """List recent orchestration runs."""
    from rich.table import Table

    duet_config = find_config(config)

    # Derive database path from run_artifact_dir
    db_path = Path(duet_config.storage.run_artifact_dir).parent / "duet.db"
    if not db_path.exists():
        console.print("[yellow]Database not found. Initialize with: uv run duet init[/]")
        console.print("[dim]Or use: uv run duet summary <run-id> for filesystem-based history[/]")
        return

    db = DuetDatabase(db_path)
    runs = db.list_runs(phase=phase, limit=limit)

    if not runs:
        console.print("[yellow]No runs found in database.[/]")
        return

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
    console.print(f"\n[dim]Showing {len(runs)} of {limit} runs[/]")


@app.command()
def inspect(
    run_id: str = typer.Argument(..., help="Run ID to inspect"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path."),
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
    iterations = db.list_iterations(run_id)
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
