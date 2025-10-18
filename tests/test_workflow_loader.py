"""
Unit tests for Sprint 9 Workflow Loader.

Tests workflow loading, path resolution, validation, and error handling.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow
from duet.workflow_loader import WorkflowLoadError, load_workflow


# ──────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def simple_workflow():
    """Create a simple valid workflow for testing."""
    return Workflow(
        agents=[Agent(name="agent1", provider="codex", model="gpt-5")],
        channels=[Channel(name="task"), Channel(name="result")],
        phases=[
            Phase(name="work", agent="agent1", consumes=["task"], publishes=["result"]),
            Phase(name="done", agent="agent1", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="work", to_phase="done"),
        ],
    )


@pytest.fixture
def workflow_module_with_variable(tmp_path, simple_workflow):
    """Create a temp module with 'workflow' variable."""
    module_path = tmp_path / "test_workflow.py"

    # Write module with workflow variable
    module_content = """
from duet.dsl import Agent, Channel, Phase, Transition, Workflow

workflow = Workflow(
    agents=[Agent(name="agent1", provider="codex", model="gpt-5")],
    channels=[Channel(name="task"), Channel(name="result")],
    phases=[
        Phase(name="work", agent="agent1", consumes=["task"], publishes=["result"]),
        Phase(name="done", agent="agent1", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="work", to_phase="done"),
    ],
)
"""
    module_path.write_text(module_content)
    return module_path


@pytest.fixture
def workflow_module_with_function(tmp_path):
    """Create a temp module with 'get_workflow()' function."""
    module_path = tmp_path / "test_workflow_fn.py"

    module_content = """
from duet.dsl import Agent, Channel, Phase, Transition, Workflow

def get_workflow():
    return Workflow(
        agents=[Agent(name="agent1", provider="codex", model="gpt-5")],
        channels=[Channel(name="task")],
        phases=[
            Phase(name="work", agent="agent1"),
            Phase(name="done", agent="agent1", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="work", to_phase="done"),
        ],
    )
"""
    module_path.write_text(module_content)
    return module_path


@pytest.fixture
def workflow_module_missing_export(tmp_path):
    """Create a temp module with no workflow export."""
    module_path = tmp_path / "no_export.py"
    module_path.write_text("# Empty module\nfoo = 42\n")
    return module_path


@pytest.fixture
def workflow_module_wrong_type(tmp_path):
    """Create a temp module with wrong workflow type."""
    module_path = tmp_path / "wrong_type.py"
    module_path.write_text("workflow = 'not a workflow object'\n")
    return module_path


@pytest.fixture
def workflow_module_invalid_workflow(tmp_path):
    """Create a temp module with invalid workflow (compilation error)."""
    module_path = tmp_path / "invalid.py"

    module_content = """
from duet.dsl import Agent, Phase, Transition, Workflow

# Invalid: references unknown agent
workflow = Workflow(
    agents=[Agent(name="agent1", provider="codex", model="gpt-5")],
    channels=[],
    phases=[
        Phase(name="work", agent="unknown_agent"),  # Invalid reference
    ],
    transitions=[
        Transition(from_phase="work", to_phase="work"),
    ],
)
"""
    module_path.write_text(module_content)
    return module_path


# ──────────────────────────────────────────────────────────────────────────────
# Loading Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_load_workflow_with_variable(workflow_module_with_variable):
    """Test loading workflow from 'workflow' variable."""
    graph = load_workflow(workflow_path=workflow_module_with_variable)

    assert graph is not None
    assert "agent1" in graph.agents
    assert "work" in graph.phases
    assert "done" in graph.phases
    assert graph.initial_phase == "work"


def test_load_workflow_with_function(workflow_module_with_function):
    """Test loading workflow from 'get_workflow()' function."""
    graph = load_workflow(workflow_path=workflow_module_with_function)

    assert graph is not None
    assert "agent1" in graph.agents
    assert "work" in graph.phases
    assert graph.initial_phase == "work"


def test_load_workflow_missing_file():
    """Test loading from non-existent file."""
    nonexistent = Path("/tmp/does_not_exist_12345.py")

    with pytest.raises(WorkflowLoadError, match="Workflow file not found"):
        load_workflow(workflow_path=nonexistent)


def test_load_workflow_missing_export(workflow_module_missing_export):
    """Test loading module with no workflow export."""
    with pytest.raises(WorkflowLoadError, match="does not export"):
        load_workflow(workflow_path=workflow_module_missing_export)


def test_load_workflow_wrong_type(workflow_module_wrong_type):
    """Test loading module with wrong workflow type."""
    with pytest.raises(WorkflowLoadError, match="not a Workflow instance"):
        load_workflow(workflow_path=workflow_module_wrong_type)


def test_load_workflow_compilation_error(workflow_module_invalid_workflow):
    """Test loading workflow that fails compilation."""
    with pytest.raises(WorkflowLoadError, match="Workflow validation failed"):
        load_workflow(workflow_path=workflow_module_invalid_workflow)


def test_load_workflow_syntax_error(tmp_path):
    """Test loading module with Python syntax errors."""
    module_path = tmp_path / "syntax_error.py"
    module_path.write_text("this is not valid python +++")

    with pytest.raises(WorkflowLoadError, match="Failed to import workflow module"):
        load_workflow(workflow_path=module_path)


def test_load_workflow_get_workflow_raises(tmp_path):
    """Test get_workflow() that raises an exception."""
    module_path = tmp_path / "raises.py"
    module_content = """
def get_workflow():
    raise RuntimeError("Intentional error")
"""
    module_path.write_text(module_content)

    with pytest.raises(WorkflowLoadError, match="get_workflow\\(\\) raised an exception"):
        load_workflow(workflow_path=module_path)


def test_load_workflow_get_workflow_wrong_type(tmp_path):
    """Test get_workflow() that returns wrong type."""
    module_path = tmp_path / "wrong_return.py"
    module_content = """
def get_workflow():
    return "not a workflow"
"""
    module_path.write_text(module_content)

    with pytest.raises(WorkflowLoadError, match="returned invalid type"):
        load_workflow(workflow_path=module_path)


# ──────────────────────────────────────────────────────────────────────────────
# Path Resolution Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_resolve_explicit_path(tmp_path, workflow_module_with_variable):
    """Test explicit path has highest priority."""
    # Create workspace with different workflow
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_workflow = workspace / ".duet" / "ide.py"
    workspace_workflow.parent.mkdir(parents=True)
    workspace_workflow.write_text("workflow = None  # Should not be loaded")

    # Explicit path should override workspace
    graph = load_workflow(
        workflow_path=workflow_module_with_variable,
        workspace_root=workspace,
    )
    assert graph is not None


def test_resolve_workspace_root(tmp_path):
    """Test workspace_root/.duet/ide.py resolution."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create .duet/ide.py in workspace
    duet_dir = workspace / ".duet"
    duet_dir.mkdir()
    workflow_file = duet_dir / "ide.py"
    workflow_file.write_text("""
from duet.dsl import Agent, Phase, Transition, Workflow

workflow = Workflow(
    agents=[Agent(name="a1", provider="codex", model="gpt-5")],
    channels=[],
    phases=[
        Phase(name="p1", agent="a1"),
        Phase(name="done", agent="a1", is_terminal=True),
    ],
    transitions=[Transition(from_phase="p1", to_phase="done")],
)
""")

    graph = load_workflow(workspace_root=workspace)
    assert graph is not None
    assert "a1" in graph.agents


def test_resolve_env_variable(tmp_path, workflow_module_with_variable, monkeypatch):
    """Test DUET_WORKFLOW_PATH environment variable resolution."""
    # Set env variable
    monkeypatch.setenv("DUET_WORKFLOW_PATH", str(workflow_module_with_variable))

    # Should load from env path
    graph = load_workflow()
    assert graph is not None


def test_resolve_fallback_current_directory(tmp_path, monkeypatch):
    """Test fallback to ./.duet/ide.py in current directory."""
    # Change to temp directory
    monkeypatch.chdir(tmp_path)

    # Create .duet/ide.py in current directory
    duet_dir = tmp_path / ".duet"
    duet_dir.mkdir()
    workflow_file = duet_dir / "ide.py"
    workflow_file.write_text("""
from duet.dsl import Agent, Phase, Transition, Workflow

workflow = Workflow(
    agents=[Agent(name="fallback", provider="codex", model="gpt-5")],
    channels=[],
    phases=[
        Phase(name="p1", agent="fallback"),
        Phase(name="done", agent="fallback", is_terminal=True),
    ],
    transitions=[Transition(from_phase="p1", to_phase="done")],
)
""")

    # Should find workflow in current directory
    graph = load_workflow()
    assert graph is not None
    assert "fallback" in graph.agents


def test_resolve_precedence_order(tmp_path, monkeypatch):
    """Test that resolution follows correct precedence order."""
    # Setup: Create workflows in multiple locations

    # 1. Explicit path
    explicit_workflow = tmp_path / "explicit.py"
    explicit_workflow.write_text("""
from duet.dsl import Agent, Phase, Transition, Workflow
workflow = Workflow(
    agents=[Agent(name="explicit", provider="codex", model="gpt-5")],
    channels=[],
    phases=[Phase(name="p", agent="explicit"), Phase(name="d", agent="explicit", is_terminal=True)],
    transitions=[Transition(from_phase="p", to_phase="d")],
)
""")

    # 2. Env variable
    env_workflow = tmp_path / "env.py"
    env_workflow.write_text("""
from duet.dsl import Agent, Phase, Transition, Workflow
workflow = Workflow(
    agents=[Agent(name="env", provider="codex", model="gpt-5")],
    channels=[],
    phases=[Phase(name="p", agent="env"), Phase(name="d", agent="env", is_terminal=True)],
    transitions=[Transition(from_phase="p", to_phase="d")],
)
""")

    # 3. Workspace
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_duet = workspace / ".duet"
    workspace_duet.mkdir()
    workspace_workflow = workspace_duet / "ide.py"
    workspace_workflow.write_text("""
from duet.dsl import Agent, Phase, Transition, Workflow
workflow = Workflow(
    agents=[Agent(name="workspace", provider="codex", model="gpt-5")],
    channels=[],
    phases=[Phase(name="p", agent="workspace"), Phase(name="d", agent="workspace", is_terminal=True)],
    transitions=[Transition(from_phase="p", to_phase="d")],
)
""")

    # Test: Explicit path wins
    graph = load_workflow(workflow_path=explicit_workflow, workspace_root=workspace)
    assert "explicit" in graph.agents

    # Test: Env variable wins over workspace
    monkeypatch.setenv("DUET_WORKFLOW_PATH", str(env_workflow))
    graph = load_workflow(workspace_root=workspace)
    assert "env" in graph.agents

    # Test: Workspace used when no env/explicit
    monkeypatch.delenv("DUET_WORKFLOW_PATH")
    graph = load_workflow(workspace_root=workspace)
    assert "workspace" in graph.agents


# ──────────────────────────────────────────────────────────────────────────────
# Error Handling Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_load_workflow_not_callable(tmp_path):
    """Test module with 'get_workflow' that's not callable."""
    module_path = tmp_path / "not_callable.py"
    module_path.write_text("get_workflow = 'not a function'\n")

    with pytest.raises(WorkflowLoadError, match="not callable"):
        load_workflow(workflow_path=module_path)


def test_load_workflow_import_error(tmp_path):
    """Test module with import errors."""
    module_path = tmp_path / "import_error.py"
    module_path.write_text("import nonexistent_module\n")

    with pytest.raises(WorkflowLoadError, match="Failed to import"):
        load_workflow(workflow_path=module_path)


def test_load_workflow_validation_errors(tmp_path):
    """Test workflow with multiple validation errors."""
    module_path = tmp_path / "validation_errors.py"
    module_content = """
from duet.dsl import Agent, Phase, Transition, Workflow

workflow = Workflow(
    agents=[
        Agent(name="agent1", provider="codex", model="gpt-5"),
        Agent(name="agent1", provider="claude", model="sonnet"),  # Duplicate
    ],
    channels=[],
    phases=[
        Phase(name="plan", agent="unknown"),  # Unknown agent
        Phase(name="plan", agent="agent1"),  # Duplicate phase name
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="missing"),  # Unknown phase
    ],
)
"""
    module_path.write_text(module_content)

    with pytest.raises(WorkflowLoadError, match="Workflow validation failed"):
        load_workflow(workflow_path=module_path)


# ──────────────────────────────────────────────────────────────────────────────
# Complex Workflow Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_load_complex_workflow_with_guards(tmp_path):
    """Test loading a complex workflow with guard conditions."""
    module_path = tmp_path / "complex.py"
    module_content = """
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[
        Agent(name="planner", provider="codex", model="gpt-5-codex"),
        Agent(name="implementer", provider="claude", model="sonnet"),
        Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
    ],
    channels=[
        Channel(name="task", schema="text"),
        Channel(name="plan", schema="text"),
        Channel(name="code", schema="git_diff"),
        Channel(name="verdict", schema="verdict"),
    ],
    phases=[
        Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
        Phase(name="implement", agent="implementer", consumes=["plan"], publishes=["code"]),
        Phase(name="review", agent="reviewer", consumes=["plan", "code"], publishes=["verdict"]),
        Phase(name="done", agent="reviewer", is_terminal=True),
        Phase(name="blocked", agent="reviewer", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
        Transition(from_phase="implement", to_phase="review"),
        Transition(from_phase="review", to_phase="done",
                   when=When.channel_has("verdict", "approve"), priority=10),
        Transition(from_phase="review", to_phase="plan",
                   when=When.channel_has("verdict", "changes_requested"), priority=5),
        Transition(from_phase="review", to_phase="blocked",
                   when=When.channel_has("verdict", "blocked"), priority=15),
    ],
)
"""
    module_path.write_text(module_content)

    graph = load_workflow(workflow_path=module_path)

    # Verify structure
    assert len(graph.agents) == 3
    assert len(graph.channels) == 4
    assert len(graph.phases) == 5
    assert graph.terminal_phases == {"done", "blocked"}

    # Verify transitions sorted by priority
    review_transitions = graph.get_next_transitions("review")
    assert len(review_transitions) == 3
    assert review_transitions[0].priority == 15  # blocked (highest)
    assert review_transitions[1].priority == 10  # done
    assert review_transitions[2].priority == 5   # plan


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
