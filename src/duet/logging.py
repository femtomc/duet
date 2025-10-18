"""Structured logging utilities for orchestration observability."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console


class DuetLogger:
    """
    Provides structured logging with rich console output and optional JSONL file logging.

    Logs are emitted in two formats:
    - Rich console (for human readability)
    - JSONL file (for machine parsing and analytics)
    """

    def __init__(
        self,
        console: Console,
        jsonl_path: Optional[Path] = None,
        enable_jsonl: bool = False,
    ) -> None:
        self.console = console
        self.jsonl_path = jsonl_path
        self.enable_jsonl = enable_jsonl and jsonl_path is not None

        if self.enable_jsonl and self.jsonl_path:
            # Ensure parent directory exists
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            # Open in append mode
            self._jsonl_file = self.jsonl_path.open("a", encoding="utf-8")
        else:
            self._jsonl_file = None

    def _write_jsonl(self, record: Dict[str, Any]) -> None:
        """Write a structured log record to JSONL file."""
        if self._jsonl_file:
            self._jsonl_file.write(json.dumps(record) + "\n")
            self._jsonl_file.flush()

    def log_event(
        self,
        event: str,
        level: str = "info",
        rich_message: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """
        Log a structured event.

        Args:
            event: Event type/name (e.g., "iteration_start", "phase_transition")
            level: Log level (info, warning, error, debug)
            rich_message: Human-readable message for console (defaults to event name)
            **kwargs: Additional structured fields for JSONL logging
        """
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()

        # Console output (rich formatted)
        display_message = rich_message or event
        style_map = {
            "info": "blue",
            "warning": "yellow",
            "error": "red",
            "debug": "dim",
        }
        style = style_map.get(level, "white")
        self.console.log(f"[{style}]{display_message}[/]")

        # JSONL output (structured)
        if self.enable_jsonl:
            record = {
                "timestamp": timestamp,
                "event": event,
                "level": level,
                **kwargs,
            }
            self._write_jsonl(record)

    def log_state_transition(
        self, run_id: str, iteration: int, from_phase: str, to_phase: str, rationale: str
    ) -> None:
        """Log a state machine phase transition."""
        self.log_event(
            event="state_transition",
            level="info",
            rich_message=f"[cyan]Transition:[/] {from_phase.upper()} → {to_phase.upper()}",
            run_id=run_id,
            iteration=iteration,
            from_phase=from_phase,
            to_phase=to_phase,
            rationale=rationale,
        )

    def log_iteration_start(self, run_id: str, iteration: int, phase: str) -> None:
        """Log the start of an iteration."""
        self.log_event(
            event="iteration_start",
            level="info",
            rich_message=f"[cyan]Iteration {iteration} → phase {phase.upper()}[/]",
            run_id=run_id,
            iteration=iteration,
            phase=phase,
        )

    def log_adapter_call(
        self, run_id: str, iteration: int, phase: str, adapter_name: str
    ) -> None:
        """Log an adapter invocation."""
        self.log_event(
            event="adapter_call",
            level="debug",
            rich_message=f"[dim]Calling adapter: {adapter_name} for {phase}[/]",
            run_id=run_id,
            iteration=iteration,
            phase=phase,
            adapter=adapter_name,
        )

    def log_error(
        self, run_id: str, iteration: int, phase: str, error_type: str, error_message: str
    ) -> None:
        """Log an error event."""
        self.log_event(
            event="error",
            level="error",
            rich_message=f"[red]Error during {phase}:[/] {error_message}",
            run_id=run_id,
            iteration=iteration,
            phase=phase,
            error_type=error_type,
            error_message=error_message,
        )

    def log_run_complete(
        self, run_id: str, final_phase: str, total_iterations: int, status: str
    ) -> None:
        """Log run completion."""
        self.log_event(
            event="run_complete",
            level="info",
            rich_message=f"[green]Run {status}:[/] {run_id} (Phase: {final_phase.upper()}, Iterations: {total_iterations})",
            run_id=run_id,
            final_phase=final_phase,
            total_iterations=total_iterations,
            status=status,
        )

    def close(self) -> None:
        """Close the JSONL file handler."""
        if self._jsonl_file:
            self._jsonl_file.close()
