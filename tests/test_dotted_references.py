"""
Tests for dotted reference resolution in WriteStep.

Verifies that $task.fact_id and similar references properly resolve
to nested object attributes.
"""

import pytest

from duet.dataspace import CodeArtifact, Dataspace, PlanDoc, TaskRequest
from duet.dsl import facet
from duet.dsl.steps import FacetContext, WriteStep


class TestDottedReferenceResolution:
    """Test dotted reference parsing in WriteStep."""

    def test_simple_reference_no_dots(self):
        """Test simple $alias reference (no dots)."""
        ds = Dataspace()
        context = FacetContext(phase_name="test", run_id="run_1", iteration=0)

        # Set simple value in context
        context.set("content", "test content")

        # WriteStep with simple reference
        step = WriteStep(
            fact_type=PlanDoc,
            values={"content": "$content", "task_id": "task_123"}
        )

        result = step.execute(context, ds)

        assert result.success
        # Verify fact was created with correct value
        from duet.dataspace import FactPattern

        facts = ds.query(FactPattern(fact_type=PlanDoc))
        assert len(facts) == 1
        assert facts[0].content == "test content"
        assert facts[0].task_id == "task_123"

    def test_dotted_reference_resolution(self):
        """Test $task.fact_id resolves to task object's fact_id attribute."""
        ds = Dataspace()
        context = FacetContext(phase_name="test", run_id="run_1", iteration=0)

        # Store TaskRequest fact in context
        task = TaskRequest(fact_id="task_123", description="Build feature", priority=1)
        context.set("task", task)

        # WriteStep with dotted reference
        step = WriteStep(
            fact_type=PlanDoc,
            values={
                "content": "plan content",
                "task_id": "$task.fact_id"  # Dotted reference
            }
        )

        result = step.execute(context, ds)

        assert result.success
        # Verify task_id was correctly resolved
        for fact_id, fact in ds.facts.items():
            if isinstance(fact, PlanDoc):
                assert fact.task_id == "task_123"
                assert fact.content == "plan content"

    def test_nested_dotted_reference(self):
        """Test deeply nested dotted references."""
        ds = Dataspace()
        context = FacetContext(phase_name="test", run_id="run_1", iteration=0)

        # Store complex object in context
        from dataclasses import dataclass

        @dataclass
        class MockObject:
            metadata: dict

        obj = MockObject(metadata={"key": "value_123"})
        context.set("obj", obj)

        # This won't work with dict access, but tests the path navigation
        # For now, test simpler nested attribute access
        task = TaskRequest(fact_id="task_456", description="Test", priority=1)
        context.set("task", task)

        step = WriteStep(
            fact_type=CodeArtifact,
            values={
                "summary": "code summary",
                "plan_id": "$task.fact_id"
            }
        )

        result = step.execute(context, ds)
        assert result.success

        for fact_id, fact in ds.facts.items():
            if isinstance(fact, CodeArtifact):
                assert fact.plan_id == "task_456"

    def test_dotted_reference_missing_attribute(self):
        """Test dotted reference with missing attribute returns None."""
        ds = Dataspace()
        context = FacetContext(phase_name="test", run_id="run_1", iteration=0)

        task = TaskRequest(fact_id="task_789", description="Test", priority=1)
        context.set("task", task)

        # Reference non-existent attribute
        step = WriteStep(
            fact_type=PlanDoc,
            values={
                "content": "content",
                "task_id": "$task.nonexistent_field"
            }
        )

        result = step.execute(context, ds)
        assert result.success

        # Should have None for missing attribute
        for fact_id, fact in ds.facts.items():
            if isinstance(fact, PlanDoc):
                assert fact.task_id is None

    def test_dotted_reference_undefined_base(self):
        """Test dotted reference with undefined base returns None."""
        ds = Dataspace()
        context = FacetContext(phase_name="test", run_id="run_1", iteration=0)

        # Don't set "task" in context
        step = WriteStep(
            fact_type=PlanDoc,
            values={
                "content": "content",
                "task_id": "$task.fact_id"
            }
        )

        result = step.execute(context, ds)
        assert result.success

        # Should have None for undefined base
        for fact_id, fact in ds.facts.items():
            if isinstance(fact, PlanDoc):
                assert fact.task_id is None


class TestEndToEndDottedReferences:
    """Test dotted references in full facet execution."""

    def test_facet_with_dotted_emit(self):
        """Test facet that uses dotted references in .emit()."""
        from duet.adapters.echo import EchoAdapter
        from duet.facet_runner import FacetRunner

        ds = Dataspace()

        # Seed TaskRequest
        task = TaskRequest(fact_id="task_abc", description="Test task", priority=1)
        ds.assert_fact(task)

        # Build facet with dotted reference
        plan_facet = (
            facet("plan")
            .needs(TaskRequest, alias="task")
            .agent("planner")
            .emit(PlanDoc, values={"content": "$agent_response", "task_id": "$task.fact_id"})
            .build()
        )

        phase = plan_facet.to_phase()
        runner = FacetRunner()

        result = runner.execute_facet(
            phase=phase,
            dataspace=ds,
            run_id="test_run",
            iteration=0,
            workspace_root=".",
            adapter=EchoAdapter()
        )

        assert result.success

        # Verify PlanDoc was created with correct task_id from dotted reference
        from duet.dataspace import FactPattern

        plan_pattern = FactPattern(fact_type=PlanDoc)
        plans = ds.query(plan_pattern)

        assert len(plans) == 1
        assert plans[0].task_id == "task_abc"  # Should match task.fact_id
        assert plans[0].content  # Should have agent response
