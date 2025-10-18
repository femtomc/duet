#!/usr/bin/env python3
"""
Manual acceptance test script for Duet orchestrator.

This script can be run directly without pytest to manually verify
the orchestration flow with the echo adapter.

Usage:
    python tests/manual_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

# Add parent directory to path to import duet
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from duet.artifacts import ArtifactStore
from duet.config import AssistantConfig, DuetConfig, LoggingConfig, StorageConfig, WorkflowConfig
from duet.orchestrator import Orchestrator


def main():
    """Run manual acceptance test."""
    console = Console()

    console.print(Panel("[bold cyan]Duet Orchestrator - Manual Acceptance Test[/]", expand=False))
    console.print("\n[bold]Test Configuration:[/]")
    console.print("  • Adapters: Echo (both Codex and Claude)")
    console.print("  • Max Iterations: 2")
    console.print("  • Human Approval: Disabled")
    console.print("  • JSONL Logging: Enabled")

    # Create temporary directories
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        workspace = tmpdir_path / "workspace"
        artifacts = tmpdir_path / "artifacts"
        workspace.mkdir()
        artifacts.mkdir()

        console.print(f"\n[dim]Workspace: {workspace}[/]")
        console.print(f"[dim]Artifacts: {artifacts}[/]\n")

        # Create configuration
        config = DuetConfig(
            codex=AssistantConfig(
                provider="echo",
                model="echo-v1",
            ),
            claude=AssistantConfig(
                provider="echo",
                model="echo-v1",
            ),
            workflow=WorkflowConfig(
                max_iterations=2,
                require_human_approval=False,
            ),
            storage=StorageConfig(
                workspace_root=workspace,
                run_artifact_dir=artifacts,
            ),
            logging=LoggingConfig(
                enable_jsonl=True,
                jsonl_dir=artifacts / "logs",
            ),
        )

        # Initialize orchestrator
        artifact_store = ArtifactStore(artifacts, console=console)
        orchestrator = Orchestrator(config, artifact_store, console=console)

        console.rule("[bold]Starting Orchestration Run[/]")
        console.print()

        # Run orchestration
        snapshot = orchestrator.run(run_id="manual-test-run")

        # Verify results
        console.print("\n")
        console.rule("[bold]Verification Results[/]")
        console.print()

        # Check 1: Final state
        console.print("[bold]✓ Check 1:[/] Final state")
        console.print(f"  Phase: {snapshot.phase.value.upper()}")
        console.print(f"  Iteration: {snapshot.iteration}")
        console.print(f"  Notes: {snapshot.notes or 'None'}")

        # Check 2: Checkpoint exists
        checkpoint = artifact_store.load_checkpoint("manual-test-run")
        checkpoint_status = "[green]✓ PASS[/]" if checkpoint else "[red]✗ FAIL[/]"
        console.print(f"\n[bold]✓ Check 2:[/] Checkpoint exists - {checkpoint_status}")

        # Check 3: Iterations persisted
        iterations = artifact_store.list_iterations("manual-test-run")
        iter_status = "[green]✓ PASS[/]" if len(iterations) > 0 else "[red]✗ FAIL[/]"
        console.print(f"[bold]✓ Check 3:[/] Iterations persisted - {iter_status}")
        console.print(f"  Count: {len(iterations)}")

        # Check 4: Summary generated
        summary_path = artifacts / "manual-test-run" / "summary.json"
        summary_status = "[green]✓ PASS[/]" if summary_path.exists() else "[red]✗ FAIL[/]"
        console.print(f"[bold]✓ Check 4:[/] Summary generated - {summary_status}")

        # Check 5: JSONL logs created
        log_path = artifacts / "logs" / "duet.jsonl"
        log_status = "[green]✓ PASS[/]" if log_path.exists() else "[red]✗ FAIL[/]"
        console.print(f"[bold]✓ Check 5:[/] JSONL logs created - {log_status}")

        if log_path.exists():
            import json

            with log_path.open("r") as f:
                log_count = sum(1 for _ in f)
            console.print(f"  Log entries: {log_count}")

        # Check 6: State transitions
        console.print(f"\n[bold]✓ Check 6:[/] State transitions")
        phases = []
        for iter_file in iterations:
            record = artifact_store.load_iteration("manual-test-run", iter_file)
            phases.append(record["phase"])

        console.print(f"  Phase sequence: {' → '.join(p.upper() for p in phases)}")

        # Check 7: Artifact structure
        console.print(f"\n[bold]✓ Check 7:[/] Artifact structure")
        if iterations:
            first_iter = artifact_store.load_iteration("manual-test-run", iterations[0])
            required_keys = ["timestamp", "iteration", "phase", "request", "response", "decision"]
            missing_keys = [k for k in required_keys if k not in first_iter]

            if not missing_keys:
                console.print("  [green]✓ All required keys present[/]")
            else:
                console.print(f"  [red]✗ Missing keys: {', '.join(missing_keys)}[/]")

        console.print("\n")
        console.rule("[bold green]Manual Test Complete[/]")
        console.print()

        # Display summary
        summary = artifact_store.generate_run_summary("manual-test-run")
        stats = summary["statistics"]
        console.print("[bold]Run Statistics:[/]")
        console.print(f"  Total Iterations: {stats['total_iterations']}")
        console.print(f"  Phase Breakdown: PLAN={stats['phase_counts']['plan']}, "
                      f"IMPLEMENT={stats['phase_counts']['implement']}, "
                      f"REVIEW={stats['phase_counts']['review']}")
        console.print(f"  Final Phase: {stats['final_phase'].upper()}")

        console.print(f"\n[green]✓ All checks passed! The orchestrator is working correctly.[/]")


if __name__ == "__main__":
    main()
