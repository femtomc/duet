"""
Workflow loader for facet-based DSL programs.

Loads user-defined workflows from Python modules and validates them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

from .dsl.combinators import FacetProgram


class WorkflowLoadError(Exception):
    """Error loading or validating workflow."""

    pass


def load_facet_program(
    workflow_path: str | Path,
    workspace_root: Optional[str | Path] = None
) -> FacetProgram:
    """
    Load FacetProgram from a Python module.

    Expects the module to define either:
    - A global variable: workflow = FacetProgram(...)
    - A function: def get_workflow() -> FacetProgram

    Args:
        workflow_path: Path to workflow Python file (e.g., .duet/workflow.py)
        workspace_root: Optional workspace root for path resolution

    Returns:
        FacetProgram instance

    Raises:
        WorkflowLoadError: If loading or validation fails

    Example workflow module:
        ```python
        # .duet/workflow.py
        from duet.dsl import facet, seq
        from duet.dataspace import TaskRequest, PlanDoc, CodeArtifact

        workflow = seq(
            facet("plan")
                .needs(TaskRequest, alias="task")
                .agent("planner")
                .emit(PlanDoc, values={"content": "$agent_response", "task_id": "$task.fact_id"})
                .build(),

            facet("implement")
                .needs(PlanDoc, alias="plan")
                .agent("coder")
                .emit(CodeArtifact, values={"summary": "$agent_response", "plan_id": "$plan.fact_id"})
                .build()
        )
        ```
    """
    workflow_path = Path(workflow_path)

    if not workflow_path.exists():
        raise WorkflowLoadError(f"Workflow file not found: {workflow_path}")

    if not workflow_path.is_file():
        raise WorkflowLoadError(f"Workflow path is not a file: {workflow_path}")

    # Load module dynamically
    module_name = workflow_path.stem
    spec = importlib.util.spec_from_file_location(module_name, workflow_path)

    if spec is None or spec.loader is None:
        raise WorkflowLoadError(f"Could not load module spec from: {workflow_path}")

    module = importlib.util.module_from_spec(spec)

    # Add workspace_root to sys.path if provided (for relative imports)
    if workspace_root:
        workspace_path = str(Path(workspace_root).resolve())
        if workspace_path not in sys.path:
            sys.path.insert(0, workspace_path)

    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise WorkflowLoadError(f"Error executing workflow module: {e}") from e

    # Extract FacetProgram
    program = None

    # Try: workflow = FacetProgram(...)
    if hasattr(module, 'workflow'):
        program = module.workflow
        if not isinstance(program, FacetProgram):
            raise WorkflowLoadError(
                f"'workflow' must be a FacetProgram instance, got: {type(program)}"
            )

    # Try: def get_workflow() -> FacetProgram
    elif hasattr(module, 'get_workflow'):
        get_workflow_fn = module.get_workflow
        if not callable(get_workflow_fn):
            raise WorkflowLoadError("'get_workflow' must be callable")

        try:
            program = get_workflow_fn()
        except Exception as e:
            raise WorkflowLoadError(f"Error calling get_workflow(): {e}") from e

        if not isinstance(program, FacetProgram):
            raise WorkflowLoadError(
                f"get_workflow() must return FacetProgram, got: {type(program)}"
            )

    else:
        raise WorkflowLoadError(
            "Workflow module must define either 'workflow' variable or 'get_workflow()' function"
        )

    # Validate program
    errors = program.validate()
    if errors:
        error_msg = "Workflow validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise WorkflowLoadError(error_msg)

    return program


def load_and_validate(workflow_path: str | Path) -> tuple[FacetProgram, list[str]]:
    """
    Load workflow and return program + validation diagnostics.

    Non-throwing version for use in CLI lint command.

    Args:
        workflow_path: Path to workflow file

    Returns:
        Tuple of (program, errors) where errors is empty list if valid

    Example:
        program, errors = load_and_validate(".duet/workflow.py")
        if errors:
            print("Validation failed:", errors)
        else:
            print(f"Workflow has {len(program.handles)} facets")
    """
    try:
        program = load_facet_program(workflow_path)
        return program, []
    except WorkflowLoadError as e:
        # Return empty program and error messages
        errors = [str(e)]
        from .dsl.combinators import FacetProgram
        return FacetProgram(), errors
