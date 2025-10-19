"""Human approval notification and workflow utilities."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from .artifacts import ArtifactStore
from .models import RunSnapshot


class ApprovalNotifier:
    """Handles human approval notifications and pause/resume logic."""

    def __init__(self, artifact_store: ArtifactStore, console: Optional[Console] = None):
        self.artifacts = artifact_store
        self.console = console or Console()

    def request_approval(self, snapshot: RunSnapshot, reason: str) -> None:
        """
        Notify that human approval is required and pause orchestration.

        Creates a PENDING_APPROVAL flag file with actionable instructions.
        Displays clear instructions to the user via console.

        Args:
            snapshot: Current run snapshot
            reason: Why approval is needed
        """
        # Write PENDING_APPROVAL flag
        approval_file = self.artifacts.run_dir(snapshot.run_id) / "PENDING_APPROVAL"
        approval_content = f"""DUET ORCHESTRATION - APPROVAL REQUIRED

Run ID: {snapshot.run_id}
Phase: {snapshot.phase.upper()}
Iteration: {snapshot.iteration}
Reason: {reason}

──────────────────────────────────────────────────────────────────────────────
INSTRUCTIONS
──────────────────────────────────────────────────────────────────────────────

The orchestration run has paused and requires human review.

To inspect the run:
  duet status {snapshot.run_id}
  duet summary {snapshot.run_id}

To review artifacts:
  ls {self.artifacts.run_dir(snapshot.run_id)}

To resume this run:
  1. Review the artifacts in: {self.artifacts.run_dir(snapshot.run_id)}
  2. Make any necessary manual corrections
  3. Remove this file: rm {approval_file}
  4. Re-run: duet run --run-id {snapshot.run_id}

To abandon this run:
  1. Delete the run directory: rm -rf {self.artifacts.run_dir(snapshot.run_id)}

──────────────────────────────────────────────────────────────────────────────
"""
        approval_file.write_text(approval_content, encoding="utf-8")

        # Display console notification
        self.console.print()
        self.console.print(
            Panel(
                f"[bold yellow]⚠ HUMAN APPROVAL REQUIRED[/]\n\n"
                f"[bold]Run ID:[/] {snapshot.run_id}\n"
                f"[bold]Phase:[/] {snapshot.phase.upper()}\n"
                f"[bold]Iteration:[/] {snapshot.iteration}\n"
                f"[bold]Reason:[/] {reason}\n\n"
                f"[dim]The orchestration has paused for human review.[/]\n\n"
                f"[cyan]To inspect:[/] duet status {snapshot.run_id}\n"
                f"[cyan]To summarize:[/] duet summary {snapshot.run_id}\n"
                f"[cyan]Approval flag:[/] {approval_file}\n\n"
                f"[yellow]Remove the approval flag file to resume this run.[/]",
                title="Orchestration Paused",
                expand=False,
            )
        )
        self.console.print()

    def check_approval_pending(self, run_id: str) -> bool:
        """Check if approval is still pending for a run."""
        approval_file = self.artifacts.run_dir(run_id) / "PENDING_APPROVAL"
        return approval_file.exists()

    def clear_approval(self, run_id: str) -> None:
        """Clear approval flag (approval granted)."""
        approval_file = self.artifacts.run_dir(run_id) / "PENDING_APPROVAL"
        if approval_file.exists():
            approval_file.unlink()
            self.console.log(f"[green]Approval granted, flag cleared[/]")
