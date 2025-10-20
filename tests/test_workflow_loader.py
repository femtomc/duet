"""
Tests for workflow loader.

Validates workflow loading, validation, and error handling.
"""

import pytest
from pathlib import Path

from duet.dsl import facet, seq
from duet.dataspace import TaskRequest, PlanDoc, CodeArtifact
from duet.workflow_loader import WorkflowLoadError, load_facet_program, load_and_validate


class TestWorkflowLoader:
    """Test workflow loading from Python modules."""

    def test_load_workflow_with_global_variable(self, tmp_path):
        """Test loading workflow from global 'workflow' variable."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
from duet.dsl import facet, seq
from duet.dataspace import TaskRequest, PlanDoc

workflow = seq(
    facet("plan").needs(TaskRequest).emit(PlanDoc, values={"content": "plan", "task_id": "t1"}).build(),
    facet("implement").needs(PlanDoc).emit(PlanDoc, values={"content": "code", "task_id": "t2"}).build()
)
''')

        program = load_facet_program(workflow_file)

        assert len(program.handles) == 2
        assert program.handles[0].definition.name == "plan"
        assert program.handles[1].definition.name == "implement"

    def test_load_workflow_with_function(self, tmp_path):
        """Test loading workflow from get_workflow() function."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
from duet.dsl import facet, seq
from duet.dataspace import TaskRequest, PlanDoc

def get_workflow():
    return seq(
        facet("plan").needs(TaskRequest).emit(PlanDoc, values={"content": "p", "task_id": "t"}).build(),
        facet("impl").needs(PlanDoc).emit(PlanDoc, values={"content": "c", "task_id": "t"}).build()
    )
''')

        program = load_facet_program(workflow_file)

        assert len(program.handles) == 2

    def test_load_workflow_file_not_found(self, tmp_path):
        """Test error when workflow file doesn't exist."""
        workflow_file = tmp_path / "nonexistent.py"

        with pytest.raises(WorkflowLoadError, match="not found"):
            load_facet_program(workflow_file)

    def test_load_workflow_syntax_error(self, tmp_path):
        """Test error when workflow has syntax error."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
from duet.dsl import facet
workflow = this is invalid python syntax
''')

        with pytest.raises(WorkflowLoadError, match="Error executing"):
            load_facet_program(workflow_file)

    def test_load_workflow_missing_variable(self, tmp_path):
        """Test error when workflow doesn't define 'workflow' or 'get_workflow'."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
from duet.dsl import facet
# No workflow variable or get_workflow function
''')

        with pytest.raises(WorkflowLoadError, match="must define either"):
            load_facet_program(workflow_file)

    def test_load_workflow_wrong_type(self, tmp_path):
        """Test error when workflow is wrong type."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
workflow = "not a FacetProgram"
''')

        with pytest.raises(WorkflowLoadError, match="must be a FacetProgram"):
            load_facet_program(workflow_file)

    def test_load_workflow_validation_fails(self, tmp_path):
        """Test error when workflow validation fails."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
from duet.dsl import facet
from duet.dsl.combinators import FacetHandle, FacetProgram, RunPolicy
from duet.dataspace import TaskRequest

# Create invalid program (duplicate names)
f1 = facet("duplicate").needs(TaskRequest).build()
f2 = facet("duplicate").needs(TaskRequest).build()

workflow = FacetProgram(handles=[
    FacetHandle(definition=f1, policy=RunPolicy.RUN_ONCE),
    FacetHandle(definition=f2, policy=RunPolicy.RUN_ONCE)
])
''')

        with pytest.raises(WorkflowLoadError, match="validation failed"):
            load_facet_program(workflow_file)


class TestLoadAndValidate:
    """Test load_and_validate non-throwing variant."""

    def test_load_and_validate_success(self, tmp_path):
        """Test successful load returns program with no errors."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
from duet.dsl import facet, seq
from duet.dataspace import TaskRequest, PlanDoc

workflow = seq(
    facet("plan").needs(TaskRequest).emit(PlanDoc, values={"content": "p", "task_id": "t"}).build(),
    facet("impl").needs(PlanDoc).emit(PlanDoc, values={"content": "c", "task_id": "t"}).build()
)
''')

        program, errors = load_and_validate(workflow_file)

        assert len(errors) == 0
        assert len(program.handles) == 2

    def test_load_and_validate_returns_errors(self, tmp_path):
        """Test failed load returns errors without raising."""
        workflow_file = tmp_path / "workflow.py"
        workflow_file.write_text('''
# Missing workflow variable
from duet.dsl import facet
''')

        program, errors = load_and_validate(workflow_file)

        assert len(errors) > 0
        assert any("must define either" in e for e in errors)
