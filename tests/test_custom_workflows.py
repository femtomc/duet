"""
Integration tests for custom workflows with arbitrary phase names.

Verifies that the orchestrator handles non-canonical workflows correctly.
"""

import tempfile
from pathlib import Path

import pytest

from duet.workflow_loader import load_workflow


def test_triage_workflow_loads():
    """Test that triage workflow loads and compiles correctly."""
    workflow_path = Path(__file__).parent / "fixtures" / "triage_workflow.py"
    graph = load_workflow(workflow_path=workflow_path)

    # Verify basic structure
    assert graph.initial_phase == "triage"
    assert "triage" in graph.phases
    assert "fix" in graph.phases
    assert "qa" in graph.phases
    assert "success" in graph.phases
    assert "blocked" in graph.phases

    # Verify terminal phases
    assert graph.is_terminal("success")
    assert graph.is_terminal("blocked")
    assert not graph.is_terminal("triage")

    # Verify task channel
    assert graph.task_channel == "issue"
    assert graph.get_task_channel() == "issue"


def test_content_workflow_loads():
    """Test that content workflow loads and compiles correctly."""
    workflow_path = Path(__file__).parent / "fixtures" / "content_workflow.py"
    graph = load_workflow(workflow_path=workflow_path)

    # Verify basic structure
    assert graph.initial_phase == "analyze"
    assert "analyze" in graph.phases
    assert "draft" in graph.phases
    assert "edit" in graph.phases
    assert "publish" in graph.phases
    assert "rejected" in graph.phases

    # Verify terminal phases
    assert graph.is_terminal("publish")
    assert graph.is_terminal("rejected")
    assert not graph.is_terminal("analyze")

    # Verify task channel
    assert graph.task_channel == "topic"


def test_triage_workflow_metadata():
    """Test that metadata helpers work for triage workflow."""
    workflow_path = Path(__file__).parent / "fixtures" / "triage_workflow.py"
    graph = load_workflow(workflow_path=workflow_path)

    # Check phase metadata
    assert graph.get_phase_metadata("fix", "git_changes_required") is True
    assert graph.get_phase_metadata("qa", "replan_transition") is True
    assert graph.get_phase_metadata("triage", "role_hint") == "planner"
    assert graph.get_phase_metadata("fix", "role_hint") == "implementer"

    # Check metadata helpers
    assert graph.requires_git_changes("fix") is True
    assert graph.requires_git_changes("triage") is False

    # Check replan transitions
    assert graph.is_replan_transition("qa", "triage") is True
    assert graph.is_replan_transition("triage", "fix") is False


def test_content_workflow_metadata():
    """Test that metadata helpers work for content workflow."""
    workflow_path = Path(__file__).parent / "fixtures" / "content_workflow.py"
    graph = load_workflow(workflow_path=workflow_path)

    # Check phase metadata
    assert graph.get_phase_metadata("edit", "replan_transition") is True
    assert graph.get_phase_metadata("analyze", "role_hint") == "planner"
    assert graph.get_phase_metadata("draft", "role_hint") == "implementer"
    assert graph.get_phase_metadata("edit", "role_hint") == "reviewer"

    # Check replan transitions
    assert graph.is_replan_transition("edit", "analyze") is True
    assert graph.is_replan_transition("analyze", "draft") is False


def test_phase_order():
    """Test that phase_order helper works."""
    workflow_path = Path(__file__).parent / "fixtures" / "triage_workflow.py"
    graph = load_workflow(workflow_path=workflow_path)

    order = graph.get_phase_order()
    assert order[0] == "triage"
    assert "fix" in order
    assert "qa" in order


def test_channel_introspection():
    """Test channel consumer/publisher helpers."""
    workflow_path = Path(__file__).parent / "fixtures" / "triage_workflow.py"
    graph = load_workflow(workflow_path=workflow_path)

    # Check consumers
    diagnosis_consumers = graph.get_channel_consumers("diagnosis")
    assert "fix" in diagnosis_consumers
    assert "qa" in diagnosis_consumers

    # Check publishers
    diagnosis_publishers = graph.get_channel_publishers("diagnosis")
    assert "triage" in diagnosis_publishers
