from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from typer.testing import CliRunner

from duet.cli import app


def test_cli_cancel_waiting_facet(tmp_path):
    runner = CliRunner()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_dir = tmp_path / "runs"

    config_path = tmp_path / "duet.yaml"
    config_path.write_text(
        dedent(
            f"""
            codex:
              provider: echo
              model: echo
            claude:
              provider: echo
              model: echo
            workflow:
              max_iterations: 2
            storage:
              workspace_root: "{workspace}"
              run_artifact_dir: "{runs_dir}"
            logging:
              quiet: true
              stream_mode: "off"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    workflow_path = tmp_path / "workflow.py"
    workflow_path.write_text(
        dedent(
            """
            from duet.dsl import facet
            from duet.dsl.combinators import FacetProgram, FacetHandle


            def get_workflow() -> FacetProgram:
                approval = facet("approval").human("Need review").build()
                return FacetProgram(handles=[FacetHandle(definition=approval)])
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "cancel",
            "approval",
            "--config",
            str(config_path),
            "--workflow",
            str(workflow_path),
            "--run-id",
            "run-cli-cancel",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Facet 'approval' canceled" in result.stdout
