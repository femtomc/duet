"""
Workflow loader for .duet/ide.py DSL programs.

Loads, validates, and compiles workflow definitions from Python modules.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from .dsl import Workflow
from .dsl.compiler import CompilationError, WorkflowGraph, compile_workflow


class WorkflowLoadError(Exception):
    """Raised when workflow loading or validation fails."""

    pass


def load_workflow(
    workflow_path: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> WorkflowGraph:
    """
    Load and compile a workflow from a Python module.

    Resolution order:
    1. Explicit workflow_path argument
    2. DUET_WORKFLOW_PATH environment variable
    3. <workspace_root>/.duet/ide.py
    4. ./.duet/ide.py (current directory)

    The module must export either:
    - A 'workflow' variable (Workflow instance)
    - A 'get_workflow()' function returning Workflow

    Args:
        workflow_path: Explicit path to workflow module
        workspace_root: Workspace root for default resolution

    Returns:
        Compiled WorkflowGraph ready for execution

    Raises:
        WorkflowLoadError: If loading or validation fails
    """
    # Resolve workflow module path
    resolved_path = _resolve_workflow_path(workflow_path, workspace_root)

    if not resolved_path.exists():
        raise WorkflowLoadError(
            f"Workflow file not found: {resolved_path}\n"
            f"Initialize with 'duet init' to create .duet/ide.py"
        )

    # Import the module
    try:
        workflow_module = _import_module_from_path(resolved_path)
    except Exception as exc:
        raise WorkflowLoadError(
            f"Failed to import workflow module: {resolved_path}\n"
            f"Error: {exc}"
        ) from exc

    # Extract workflow definition
    try:
        workflow = _extract_workflow(workflow_module, resolved_path)
    except Exception as exc:
        raise WorkflowLoadError(
            f"Failed to extract workflow from module: {resolved_path}\n"
            f"Error: {exc}"
        ) from exc

    # Validate workflow type
    if not isinstance(workflow, Workflow):
        raise WorkflowLoadError(
            f"Module exports invalid workflow type: {type(workflow)}\n"
            f"Expected: duet.dsl.Workflow\n"
            f"Module: {resolved_path}"
        )

    # Compile and validate
    try:
        workflow_graph = compile_workflow(workflow)
    except CompilationError as exc:
        raise WorkflowLoadError(
            f"Workflow validation failed: {resolved_path}\n"
            f"{exc}"
        ) from exc

    return workflow_graph


def _resolve_workflow_path(
    explicit_path: Optional[Path],
    workspace_root: Optional[Path],
) -> Path:
    """
    Resolve workflow module path using precedence rules.

    Args:
        explicit_path: Explicit path provided by caller
        workspace_root: Workspace root for default resolution

    Returns:
        Resolved Path to workflow module
    """
    # 1. Explicit path
    if explicit_path:
        return explicit_path.expanduser().resolve()

    # 2. Environment variable
    env_path = os.environ.get("DUET_WORKFLOW_PATH")
    if env_path:
        return Path(env_path).expanduser().resolve()

    # 3. Workspace root
    if workspace_root:
        candidate = workspace_root / ".duet" / "ide.py"
        return candidate.expanduser().resolve()

    # 4. Current directory fallback
    return Path(".duet/ide.py").expanduser().resolve()


def _import_module_from_path(module_path: Path) -> Any:
    """
    Import a Python module from a file path.

    Args:
        module_path: Path to the Python module

    Returns:
        Imported module object

    Raises:
        ImportError: If import fails
    """
    module_name = f"duet_workflow_{module_path.stem}"

    # Create module spec
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create module spec for: {module_path}")

    # Import the module
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    return module


def _extract_workflow(module: Any, module_path: Path) -> Workflow:
    """
    Extract workflow definition from imported module.

    Looks for 'workflow' variable or 'get_workflow()' function.

    Args:
        module: Imported Python module
        module_path: Path to module (for error messages)

    Returns:
        Workflow instance

    Raises:
        WorkflowLoadError: If workflow cannot be extracted
    """
    # Try 'workflow' variable first
    if hasattr(module, "workflow"):
        workflow = module.workflow
        if isinstance(workflow, Workflow):
            return workflow
        else:
            raise WorkflowLoadError(
                f"Module exports 'workflow' but it's not a Workflow instance: {type(workflow)}\n"
                f"Module: {module_path}"
            )

    # Try 'get_workflow()' function
    if hasattr(module, "get_workflow"):
        get_workflow = module.get_workflow
        if callable(get_workflow):
            try:
                workflow = get_workflow()
            except Exception as exc:
                raise WorkflowLoadError(
                    f"get_workflow() raised an exception: {exc}\n"
                    f"Module: {module_path}"
                ) from exc

            if isinstance(workflow, Workflow):
                return workflow
            else:
                raise WorkflowLoadError(
                    f"get_workflow() returned invalid type: {type(workflow)}\n"
                    f"Expected: Workflow\n"
                    f"Module: {module_path}"
                )
        else:
            raise WorkflowLoadError(
                f"Module exports 'get_workflow' but it's not callable: {type(get_workflow)}\n"
                f"Module: {module_path}"
            )

    # No workflow found
    raise WorkflowLoadError(
        f"Module does not export 'workflow' variable or 'get_workflow()' function\n"
        f"Module: {module_path}\n"
        f"Available exports: {[name for name in dir(module) if not name.startswith('_')]}"
    )
