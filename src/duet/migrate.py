"""Migration utilities for backfilling SQLite database from artifact files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .artifacts import ArtifactStore
from .models import Phase, ReviewVerdict, RunSnapshot
from .persistence import DuetDatabase


class MigrationError(Exception):
    """Exception raised during migration."""

    pass


class ArtifactMigrator:
    """Migrates filesystem artifacts to SQLite database."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        db: DuetDatabase,
        console: Optional[Console] = None,
    ):
        self.artifacts = artifact_store
        self.db = db
        self.console = console or Console()

    def migrate_all(self, force: bool = False) -> None:
        """
        Migrate all runs from artifact directory to database.

        Args:
            force: If True, re-migrate runs that already exist in DB
        """
        # Find all run directories
        run_dirs = [d for d in self.artifacts.root.iterdir() if d.is_dir()]

        if not run_dirs:
            self.console.print("[yellow]No runs found in artifact directory.[/]")
            return

        self.console.print(f"[cyan]Found {len(run_dirs)} runs to migrate[/]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
        ) as progress:
            task = progress.add_task("Migrating runs...", total=len(run_dirs))

            migrated = 0
            skipped = 0
            errors = 0

            for run_dir in run_dirs:
                run_id = run_dir.name

                # Check if already migrated
                if not force:
                    existing = self.db.get_run(run_id)
                    if existing:
                        skipped += 1
                        progress.update(task, advance=1)
                        continue

                try:
                    self._migrate_run(run_id)
                    migrated += 1
                except Exception as exc:
                    self.console.log(f"[red]Failed to migrate {run_id}: {exc}[/]")
                    errors += 1

                progress.update(task, advance=1)

        # Summary
        self.console.print()
        self.console.print(f"[green bold]✓ Migration complete[/]")
        self.console.print(f"  Migrated: {migrated}")
        self.console.print(f"  Skipped: {skipped}")
        if errors > 0:
            self.console.print(f"  [red]Errors: {errors}[/]")

    def _migrate_run(self, run_id: str) -> None:
        """Migrate a single run and its iterations."""
        # Load checkpoint
        checkpoint = self.artifacts.load_checkpoint(run_id)
        if not checkpoint:
            raise MigrationError(f"No checkpoint found for {run_id}")

        # Insert/update run record
        self.db.upsert_run(checkpoint)

        # Migrate iterations
        iteration_files = self.artifacts.list_iterations(run_id)
        for iter_file in iteration_files:
            record = self.artifacts.load_iteration(run_id, iter_file)

            # Extract metadata
            request_data = record.get("request", {})
            response_data = record.get("response", {})
            decision_data = record.get("decision", {})
            response_meta = response_data.get("metadata", {})

            # Parse verdict
            verdict_str = response_data.get("verdict") or response_meta.get("verdict")
            verdict = None
            if verdict_str:
                try:
                    verdict = ReviewVerdict(verdict_str)
                except ValueError:
                    pass  # Invalid verdict string

            # Parse next phase
            next_phase_str = decision_data.get("next_phase")
            next_phase = Phase(next_phase_str) if next_phase_str else None

            # Extract git metadata
            git_meta = response_meta.get("git_changes", {})

            # Extract usage metadata
            usage_meta = {
                "input_tokens": response_meta.get("input_tokens"),
                "output_tokens": response_meta.get("output_tokens"),
                "cached_input_tokens": response_meta.get("cached_input_tokens"),
            }

            # Extract stream metadata
            stream_meta = {
                "stream_events": response_meta.get("stream_events"),
                "thread_id": response_meta.get("thread_id"),
            }

            self.db.insert_iteration(
                run_id=run_id,
                iteration=record.get("iteration", 0),
                phase=Phase(record.get("phase", "plan")),
                prompt=request_data.get("prompt", ""),
                response_content=response_data.get("content", ""),
                verdict=verdict,
                concluded=response_data.get("concluded", False),
                next_phase=next_phase,
                requires_human=decision_data.get("requires_human", False),
                decision_rationale=decision_data.get("rationale"),
                git_metadata=git_meta if git_meta else None,
                usage_metadata=usage_meta if any(usage_meta.values()) else None,
                stream_metadata=stream_meta if any(stream_meta.values()) else None,
            )
