"""Placeholder orchestrator for upcoming facet-based runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rich.console import Console

from .artifacts import ArtifactStore
from .config import DuetConfig
from .persistence import DuetDatabase


@dataclass
class Orchestrator:
    """
    Temporary stub orchestrator.

    The legacy channel-based orchestrator has been removed. A new implementation
    targeting the facet/combinator DSL will replace this placeholder.
    """

    config: DuetConfig
    artifact_store: ArtifactStore
    console: Console = Console()
    db: Optional[DuetDatabase] = None
    workflow_path: Optional[str] = None

    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("Facet-based orchestrator not implemented yet.")

    def run_next_phase(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("Facet-based orchestrator not implemented yet.")
