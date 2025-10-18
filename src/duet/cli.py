"""CLI interface for the Duet orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .artifacts import ArtifactStore
from .config import DuetConfig, find_config
from .init import DuetInitializer, InitError
from .orchestrator import Orchestrator

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
) -> None:
    """Execute the duet orchestration loop."""
    duet_config = find_config(config)
    artifact_store = ArtifactStore(duet_config.storage.run_artifact_dir, console=console)
    orchestrator = Orchestrator(duet_config, artifact_store, console=console)
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
