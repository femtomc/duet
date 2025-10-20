from __future__ import annotations

from pathlib import Path

from rich.console import Console

from duet.artifacts import ArtifactStore
from duet.config import (
    AssistantConfig,
    DuetConfig,
    LoggingConfig,
    StorageConfig,
    WorkflowConfig,
)
from duet.models import StreamMode
from duet.orchestrator import Orchestrator
from duet.dsl import FacetProgram, FacetHandle, facet
from duet.adapters import get_adapter


def test_orchestrator_cancels_waiting_facet(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = tmp_path / "runs"

    config = DuetConfig(
        codex=AssistantConfig(provider="echo", model="echo"),
        claude=AssistantConfig(provider="echo", model="echo"),
        workflow=WorkflowConfig(max_iterations=3),
        storage=StorageConfig(
            workspace_root=workspace,
            run_artifact_dir=artifacts,
        ),
        logging=LoggingConfig(quiet=True, stream_mode=StreamMode.OFF),
    )

    artifact_store = ArtifactStore(config.storage.run_artifact_dir, console=Console(record=True))
    orchestrator = Orchestrator(
        config,
        artifact_store,
        console=Console(record=True),
        db=None,
        workspace_root=str(config.storage.workspace_root),
    )

    approval_facet = facet("approval").human("Need review").build()
    program = FacetProgram(handles=[FacetHandle(definition=approval_facet)])

    adapter = get_adapter(config.codex)

    result = orchestrator.run(
        program=program,
        run_id="run-cancel-test",
        adapter=adapter,
        cancel_facet_id="approval",
    )

    assert result.success
    assert result.waiting_facets == []
    assert result.canceled_facets == ["approval"]
