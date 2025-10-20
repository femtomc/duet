"""
Tests for FacetBuilder DSL API.

Validates facet construction, validation, and Phase conversion.
"""

from dataclasses import dataclass

import pytest

from duet.dataspace import ApprovalRequest, CodeArtifact, Message, PlanDoc, ReviewVerdict
from duet.dsl import FacetBuilder, facet
from duet.dsl.facet import FacetDefinition
from duet.dsl.steps import (
    AgentStep,
    HumanStep,
    ReadStep,
    ReceiveMessageStep,
    SendMessageStep,
    ToolStep,
    WriteStep,
)
from duet.dsl.tools import GitChangeTool


class TestFacetBuilder:
    """Test FacetBuilder construction and validation."""

    def test_create_facet_builder(self):
        """Test basic facet builder creation."""
        builder = FacetBuilder("test_facet")
        assert builder.name == "test_facet"
        assert builder._steps == []
        assert builder._alias_map == {}
        assert builder._emitted_facts == []

    def test_facet_factory_function(self):
        """Test facet() factory function."""
        builder = facet("test_facet", description="Test description")
        assert isinstance(builder, FacetBuilder)
        assert builder.name == "test_facet"
        assert builder.description == "Test description"

    def test_needs_method(self):
        """Test .needs() adds ReadStep."""
        definition = (
            facet("planner")
            .needs(PlanDoc, alias="plan", task_id="123")
            .build()
        )

        assert len(definition.steps) == 1
        assert isinstance(definition.steps[0], ReadStep)
        assert definition.steps[0].fact_type == PlanDoc
        assert definition.steps[0].into == "plan"
        assert definition.steps[0].constraints == {"task_id": "123"}
        assert "plan" in definition.alias_map
        assert definition.alias_map["plan"] == PlanDoc

    def test_needs_default_alias(self):
        """Test .needs() uses default alias (lowercase type name)."""
        definition = facet("test").needs(PlanDoc).build()

        assert definition.steps[0].into == "plandoc"
        assert "plandoc" in definition.alias_map

    def test_agent_method(self):
        """Test .agent() adds AgentStep."""
        definition = (
            facet("planner")
            .agent("planner_agent", prompt="Create a plan", role="planner")
            .build()
        )

        assert len(definition.steps) == 1
        assert isinstance(definition.steps[0], AgentStep)
        assert definition.steps[0].agent == "planner_agent"
        assert definition.steps[0].prompt_template == "Create a plan"
        assert definition.steps[0].role == "planner"

    def test_tool_method(self):
        """Test .tool() adds ToolStep."""
        git_tool = GitChangeTool(require_changes=True)
        definition = facet("implementer").tool(git_tool).build()

        assert len(definition.steps) == 1
        assert isinstance(definition.steps[0], ToolStep)
        assert definition.steps[0].tool == git_tool
        assert definition.steps[0].into_context is True

    def test_emit_method(self):
        """Test .emit() adds WriteStep."""
        definition = (
            facet("planner")
            .emit(
                PlanDoc,
                values={"content": "$agent_response", "task_id": "task_123"}
            )
            .build()
        )

        assert len(definition.steps) == 1
        assert isinstance(definition.steps[0], WriteStep)
        assert definition.steps[0].fact_type == PlanDoc
        assert definition.steps[0].values == {
            "content": "$agent_response",
            "task_id": "task_123"
        }
        assert definition.steps[0].relay is True
        assert PlanDoc in definition.emitted_facts

    def test_emit_local_method(self):
        """Test .emit_local() adds WriteStep without relaying to parent."""
        definition = (
            facet("planner")
            .emit_local(PlanDoc, values={"content": "local"})
            .build()
        )

        assert len(definition.steps) == 1
        step = definition.steps[0]
        assert isinstance(step, WriteStep)
        assert step.relay is False

    def test_on_message_method(self):
        """Test .on_message() adds ReceiveMessageStep with alias."""

        @dataclass
        class TestMessage(Message):
            __test__ = False
            topic: str
            payload: str

        definition = (
            facet("listener")
            .on_message(TestMessage, alias="incoming", constraints={"topic": "updates"})
            .build()
        )

        assert len(definition.steps) == 1
        step = definition.steps[0]
        assert isinstance(step, ReceiveMessageStep)
        assert step.message_type is TestMessage
        assert step.alias == "incoming"
        assert step.constraints == {"topic": "updates"}
        assert definition.alias_map["incoming"] is TestMessage

    def test_send_message_method(self):
        """Test .send_message() adds SendMessageStep."""

        @dataclass
        class TestMessage(Message):
            __test__ = False
            topic: str
            payload: str

        definition = (
            facet("speaker")
            .send_message(
                TestMessage,
                values={"topic": "updates", "payload": "hello"},
                store_as="sent_message",
                relay=True,
            )
            .build()
        )

        assert len(definition.steps) == 1
        step = definition.steps[0]
        assert isinstance(step, SendMessageStep)
        assert step.message_type is TestMessage
        assert step.values == {"topic": "updates", "payload": "hello"}
        assert step.store_as == "sent_message"
        assert step.relay is True

    def test_human_method(self):
        """Test .human() adds HumanStep."""
        definition = (
            facet("reviewer")
            .human("Review code changes", timeout=3600)
            .build()
        )

        assert len(definition.steps) == 1
        assert isinstance(definition.steps[0], HumanStep)
        assert definition.steps[0].reason == "Review code changes"
        assert definition.steps[0].timeout == 3600

    def test_chained_methods(self):
        """Test chaining multiple methods."""
        definition = (
            facet("planner")
            .needs(PlanDoc, alias="plan")
            .agent("planner")
            .tool(GitChangeTool())
            .emit(CodeArtifact, values={"summary": "$agent_response"})
            .build()
        )

        assert len(definition.steps) == 4
        assert isinstance(definition.steps[0], ReadStep)
        assert isinstance(definition.steps[1], AgentStep)
        assert isinstance(definition.steps[2], ToolStep)
        assert isinstance(definition.steps[3], WriteStep)

    def test_with_metadata(self):
        """Test .with_metadata() adds metadata."""
        definition = (
            facet("test")
            .with_metadata(priority=1, category="planning")
            .emit(PlanDoc, values={"content": "test"})
            .build()
        )

        assert definition.metadata["priority"] == 1
        assert definition.metadata["category"] == "planning"

    def test_to_phase(self):
        """Test FacetDefinition.to_phase() conversion."""
        definition = (
            facet("planner")
            .needs(PlanDoc)
            .agent("planner")
            .build()
        )

        phase = definition.to_phase()
        assert phase.name == "planner"
        assert len(phase.steps) == 2
        assert phase.steps[0] == definition.steps[0]
        assert phase.steps[1] == definition.steps[1]


class TestFacetValidation:
    """Test facet validation rules."""

    def test_validation_requires_at_least_one_step(self):
        """Test validation fails if no steps."""
        with pytest.raises(ValueError, match="must have at least one step"):
            facet("invalid").build()

    def test_validation_passes_with_needs(self):
        """Test validation passes with .needs()."""
        definition = facet("test").needs(PlanDoc).build()
        errors = definition.validate()
        assert len(errors) == 0

    def test_validation_passes_with_emit(self):
        """Test validation passes with .emit()."""
        definition = facet("test").emit(PlanDoc, values={"content": "test"}).build()
        errors = definition.validate()
        assert len(errors) == 0

    def test_validation_undefined_alias_reference(self):
        """Test validation catches undefined alias references."""
        with pytest.raises(ValueError, match="undefined alias"):
            facet("test").emit(PlanDoc, values={"content": "$undefined_alias"}).build()

    def test_validation_allows_agent_response_alias(self):
        """Test validation allows $agent_response (built-in)."""
        definition = (
            facet("test")
            .agent("planner")
            .emit(PlanDoc, values={"content": "$agent_response"})
            .build()
        )

        errors = definition.validate()
        assert len(errors) == 0

    def test_validation_allows_defined_aliases(self):
        """Test validation allows aliases from .needs() including dotted references."""
        definition = (
            facet("test")
            .needs(PlanDoc, alias="plan")
            .emit(CodeArtifact, values={"plan_id": "$plan.fact_id"})
            .build()
        )

        # Should pass as "plan" is defined (validation extracts base alias before '.')
        errors = definition.validate()
        assert len(errors) == 0


class TestComplexFacets:
    """Test complex facet patterns."""

    def test_full_pipeline_facet(self):
        """Test complete pipeline facet with all step types."""
        definition = (
            facet("implement_and_review")
            .needs(PlanDoc, alias="plan")
            .tool(GitChangeTool(require_changes=False))
            .agent("coder", prompt="Implement the plan")
            .emit(CodeArtifact, values={
                "summary": "$agent_response",
                "plan_id": "$plan.fact_id"
            })
            .human("Review implementation", timeout=7200)
            .emit(ReviewVerdict, values={
                "verdict": "approve",
                "feedback": "Looks good"
            })
            .build()
        )

        assert len(definition.steps) == 6
        assert definition.name == "implement_and_review"
        assert len(definition.emitted_facts) == 2
        assert CodeArtifact in definition.emitted_facts
        assert ReviewVerdict in definition.emitted_facts

    def test_multiple_needs(self):
        """Test facet with multiple .needs() calls."""
        definition = (
            facet("merger")
            .needs(PlanDoc, alias="plan")
            .needs(CodeArtifact, alias="code")
            .agent("merger")
            .emit(ReviewVerdict, values={"verdict": "$agent_response"})
            .build()
        )

        # Should have 2 ReadSteps
        read_steps = [s for s in definition.steps if isinstance(s, ReadStep)]
        assert len(read_steps) == 2
        assert "plan" in definition.alias_map
        assert "code" in definition.alias_map

    def test_multiple_emits(self):
        """Test facet with multiple .emit() calls."""
        definition = (
            facet("multi_emitter")
            .needs(PlanDoc)
            .emit(CodeArtifact, values={"summary": "artifact1"})
            .emit(ReviewVerdict, values={"verdict": "approve"})
            .build()
        )

        write_steps = [s for s in definition.steps if isinstance(s, WriteStep)]
        assert len(write_steps) == 2
        assert len(definition.emitted_facts) == 2
        assert CodeArtifact in definition.emitted_facts
        assert ReviewVerdict in definition.emitted_facts
