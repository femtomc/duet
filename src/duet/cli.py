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
