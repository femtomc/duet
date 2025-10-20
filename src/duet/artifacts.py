"""Artifact management utilities."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from collections import Counter
from typing import Any, Dict

from rich.console import Console

from .models import AssistantRequest, AssistantResponse, RunSnapshot


class ArtifactStore:
    """Persists run metadata, prompts, and responses to the filesystem."""

    def __init__(self, root: Path, console: Console | None = None) -> None:
        self.root = root
        self.console = console or Console()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, run_id: str, relative_path: str, content: str) -> None:
        target = self.run_dir(run_id) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.console.log(f"[green]Saved artifact[/] {target}")

    def write_json(self, run_id: str, relative_path: str, payload: Dict[str, Any]) -> None:
        target = self.run_dir(run_id) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.console.log(f"[green]Saved JSON[/] {target}")

    def checkpoint(self, snapshot: RunSnapshot) -> None:
        self.write_json(snapshot.run_id, "checkpoint.json", snapshot.model_dump(mode="json"))

    def persist_iteration(
        self,
        run_id: str,
        iteration: int,
        phase: str,
        request: AssistantRequest,
        response: AssistantResponse,
        summary: str | None = None,
    ) -> None:
        """
        Persist a complete iteration record with structured data.

        Creates a single JSON file per iteration containing:
        - Timestamp
        - Iteration number
        - Facet ID (stored in phase field for backward compatibility)
        - Request (prompt + context)
        - Response (content + metadata)
        - Summary (if available)

        Filename format: iterations/iter-{iteration:03d}-{facet_id}-{timestamp}.json

        Note: 'phase' parameter represents facet_id in the new model.
        """
        now = dt.datetime.now(dt.timezone.utc)
        timestamp_iso = now.isoformat()  # For record data
        timestamp_safe = now.strftime("%Y%m%dT%H%M%S%fZ")  # For filename (Windows-safe)
        record = {
            "timestamp": timestamp_iso,
            "iteration": iteration,
            "facet_id": phase,  # New terminology
            "phase": phase,  # Backward compatibility
            "request": request.model_dump(),
            "response": response.model_dump(),
        }

        if summary:
            record["summary"] = summary

        filename = f"iterations/iter-{iteration:03d}-{phase}-{timestamp_safe}.json"
        self.write_json(run_id, filename, record)

    def list_iterations(self, run_id: str) -> list[Path]:
        """List all iteration record files for a given run, sorted by iteration number."""
        iterations_dir = self.run_dir(run_id) / "iterations"
        if not iterations_dir.exists():
            return []
        return sorted(iterations_dir.glob("iter-*.json"))

    def load_iteration(self, run_id: str, iteration_file: Path) -> Dict[str, Any]:
        """Load a specific iteration record."""
        with iteration_file.open("r", encoding="utf-8") as f:
            return json.load(f)

    def load_checkpoint(self, run_id: str) -> RunSnapshot | None:
        """Load the most recent checkpoint for a run."""
        checkpoint_path = self.run_dir(run_id) / "checkpoint.json"
        if not checkpoint_path.exists():
            return None

        with checkpoint_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return RunSnapshot.model_validate(data)

    def generate_run_summary(self, run_id: str) -> Dict[str, Any]:
        """
        Generate a comprehensive summary of a run's iteration history.

        Returns:
            Dictionary containing:
            - run_id
            - checkpoint (if available)
            - iterations: list of iteration summaries
            - statistics: counts by phase, total iterations, etc.
        """
        checkpoint = self.load_checkpoint(run_id)
        iterations = self.list_iterations(run_id)

        iteration_summaries = []
        phase_counts: Counter[str] = Counter()

        for iter_file in iterations:
            record = self.load_iteration(run_id, iter_file)
            phase = record.get("phase", "unknown")
            phase_counts.update([phase])

            summary = {
                "iteration": record.get("iteration"),
                "phase": phase,
                "timestamp": record.get("timestamp"),
                "decision": record.get("decision", {}).get("rationale", "N/A"),
                "next_phase": record.get("decision", {}).get("next_phase", "N/A"),
                "requires_human": record.get("decision", {}).get("requires_human", False),
                "response_concluded": record.get("response", {}).get("concluded", False),
            }
            iteration_summaries.append(summary)

        summary = {
            "run_id": run_id,
            "checkpoint": checkpoint.model_dump(mode="json") if checkpoint else None,
            "iterations": iteration_summaries,
            "statistics": {
                "total_iterations": len(iterations),
                "phase_counts": dict(phase_counts),
                "final_phase": checkpoint.phase if checkpoint else "unknown",
                "final_iteration": checkpoint.iteration if checkpoint else 0,
            },
        }

        return summary

    def save_run_summary(self, run_id: str) -> Path:
        """Generate and save a run summary to a JSON file."""
        summary = self.generate_run_summary(run_id)
        summary_path = "summary.json"
        self.write_json(run_id, summary_path, summary)
        return self.run_dir(run_id) / summary_path
