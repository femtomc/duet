"""
Tests for facet combinators (seq, loop, branch, once, parallel).

Validates facet composition, trigger wiring, and program validation.
"""

import pytest

from duet.dataspace import CodeArtifact, FactPattern, PlanDoc, ReviewVerdict, TaskRequest
from duet.dsl import branch, facet, loop, once, parallel, seq
from duet.dsl.combinators import FacetHandle, FacetProgram, RunPolicy


class TestSeqCombinator:
    """Test sequential pipeline combinator."""

    def test_seq_basic_pipeline(self):
        """Test seq() creates sequential pipeline."""
        plan_facet = (
            facet("plan")
            .needs(TaskRequest)
            .emit(PlanDoc, values={"content": "plan"})
            .build()
        )

        implement_facet = (
            facet("implement")
            .needs(PlanDoc)
            .emit(CodeArtifact, values={"summary": "code"})
            .build()
        )

        program = seq(plan_facet, implement_facet)

        assert isinstance(program, FacetProgram)
        assert len(program.handles) == 2
        assert program.handles[0].definition.name == "plan"
        assert program.handles[1].definition.name == "implement"

    def test_seq_auto_wiring_triggers(self):
        """Test seq() auto-wires triggers based on emissions."""
        plan_facet = (
            facet("plan")
            .needs(TaskRequest)
            .emit(PlanDoc, values={"content": "plan"})
            .build()
        )

        implement_facet = (
            facet("implement")
            .needs(PlanDoc)
            .emit(CodeArtifact, values={"summary": "code"})
            .build()
        )

        program = seq(plan_facet, implement_facet)

        # First facet triggers on TaskRequest (from its .needs())
        assert len(program.handles[0].triggers) == 1
        assert program.handles[0].triggers[0].fact_type == TaskRequest

        # Second facet triggers on first facet's emission (PlanDoc)
        assert len(program.handles[1].triggers) > 0
        assert program.handles[1].triggers[0].fact_type == PlanDoc

    def test_seq_three_stage_pipeline(self):
        """Test seq() with 3+ facets."""
        f1 = facet("f1").needs(TaskRequest).emit(PlanDoc, values={"content": "p"}).build()
        f2 = facet("f2").needs(PlanDoc).emit(CodeArtifact, values={"summary": "c"}).build()
        f3 = facet("f3").needs(CodeArtifact).emit(ReviewVerdict, values={"verdict": "v"}).build()

        program = seq(f1, f2, f3)

        assert len(program.handles) == 3
        # f1: triggers on TaskRequest (from its .needs())
        assert len(program.handles[0].triggers) == 1
        assert program.handles[0].triggers[0].fact_type == TaskRequest
        # f2: triggers on PlanDoc
        assert program.handles[1].triggers[0].fact_type == PlanDoc
        # f3: triggers on CodeArtifact
        assert program.handles[2].triggers[0].fact_type == CodeArtifact

    def test_seq_requires_min_two_facets(self):
        """Test seq() requires at least 2 facets."""
        f1 = facet("f1").needs(TaskRequest).build()

        with pytest.raises(ValueError, match="at least 2 facets"):
            seq(f1)

    def test_seq_fails_if_no_emissions(self):
        """Test seq() fails if facet emits nothing."""
        f1 = facet("f1").needs(TaskRequest).build()  # No emissions!
        f2 = facet("f2").needs(PlanDoc).build()

        with pytest.raises(ValueError, match="emits no facts"):
            seq(f1, f2)

    def test_seq_sets_run_once_policy(self):
        """Test seq() sets RUN_ONCE policy for all facets."""
        f1 = facet("f1").needs(TaskRequest).emit(PlanDoc, values={"content": "p"}).build()
        f2 = facet("f2").needs(PlanDoc).emit(CodeArtifact, values={"summary": "c"}).build()

        program = seq(f1, f2)

        assert all(h.policy == RunPolicy.RUN_ONCE for h in program.handles)

    def test_seq_first_facet_triggers_on_needs(self):
        """Test seq() makes first facet trigger on its required facts."""
        f1 = facet("f1").needs(TaskRequest, alias="task").emit(PlanDoc, values={"content": "p"}).build()
        f2 = facet("f2").needs(PlanDoc).emit(CodeArtifact, values={"summary": "c"}).build()

        program = seq(f1, f2)

        # First facet should trigger on TaskRequest (from its .needs())
        assert len(program.handles[0].triggers) == 1
        assert program.handles[0].triggers[0].fact_type == TaskRequest

    def test_seq_fails_on_mismatched_fact_contracts(self):
        """Test seq() raises error when facets don't share fact contracts."""
        # f1 emits PlanDoc, but f2 needs CodeArtifact - mismatch!
        f1 = facet("f1").needs(TaskRequest).emit(PlanDoc, values={"content": "p"}).build()
        f2 = facet("f2").needs(CodeArtifact).emit(ReviewVerdict, values={"verdict": "v"}).build()

        with pytest.raises(ValueError, match="emits .* but .* needs"):
            seq(f1, f2)


class TestLoopCombinator:
    """Test loop combinator."""

    def test_loop_creates_handle(self):
        """Test loop() creates FacetHandle with LOOP_UNTIL policy."""
        test_facet = (
            facet("test")
            .needs(CodeArtifact)
            .emit(ReviewVerdict, values={"verdict": "test"})
            .build()
        )

        handle = loop(test_facet, until=lambda x: x.verdict == "pass")

        assert isinstance(handle, FacetHandle)
        assert handle.policy == RunPolicy.LOOP_UNTIL
        assert handle.guard is not None
        assert handle.definition.name == "test"

    def test_loop_extracts_triggers_from_needs(self):
        """Test loop() extracts trigger patterns from facet needs."""
        test_facet = (
            facet("test")
            .needs(CodeArtifact, alias="code")
            .emit(ReviewVerdict, values={"verdict": "test"})
            .build()
        )

        handle = loop(test_facet, until=lambda x: True)

        assert len(handle.triggers) > 0
        assert handle.triggers[0].fact_type == CodeArtifact

    def test_loop_stores_predicate_in_metadata(self):
        """Test loop() stores predicate in metadata."""
        predicate = lambda x: x.all_pass

        test_facet = facet("test").needs(CodeArtifact).build()
        handle = loop(test_facet, until=predicate)

        assert "loop_predicate" in handle.metadata
        assert handle.metadata["loop_predicate"] is predicate


class TestOnceCombinator:
    """Test once combinator."""

    def test_once_with_explicit_trigger(self):
        """Test once() with explicit trigger pattern."""
        deploy_facet = facet("deploy").needs(CodeArtifact).build()
        trigger = FactPattern(CodeArtifact, constraints={"approved": True})

        handle = once(deploy_facet, trigger=trigger)

        assert handle.policy == RunPolicy.RUN_ONCE
        assert len(handle.triggers) == 1
        assert handle.triggers[0] == trigger

    def test_once_without_trigger(self):
        """Test once() auto-extracts triggers from needs."""
        cleanup_facet = facet("cleanup").needs(ReviewVerdict).build()

        handle = once(cleanup_facet)

        assert handle.policy == RunPolicy.RUN_ONCE
        assert len(handle.triggers) > 0
        assert handle.triggers[0].fact_type == ReviewVerdict

    def test_once_with_no_needs_no_trigger(self):
        """Test once() with no trigger and no needs (starts immediately)."""
        seed_facet = facet("seed").emit(TaskRequest, values={"description": "t"}).build()

        handle = once(seed_facet)

        assert handle.policy == RunPolicy.RUN_ONCE
        assert len(handle.triggers) == 0  # No triggers - starts immediately


class TestBranchCombinator:
    """Test conditional branch combinator."""

    def test_branch_creates_program_with_two_handles(self):
        """Test branch() creates program with true/false branches."""
        deploy = facet("deploy").needs(CodeArtifact).build()
        revert = facet("revert").needs(CodeArtifact).build()

        program = branch(
            predicate=lambda x: x.approved,
            on_true=deploy,
            on_false=revert
        )

        assert isinstance(program, FacetProgram)
        assert len(program.handles) == 2

    def test_branch_sets_guards(self):
        """Test branch() sets guard predicates."""
        predicate = lambda x: x.verdict == "approve"
        deploy = facet("deploy").needs(ReviewVerdict).build()
        reject = facet("reject").needs(ReviewVerdict).build()

        program = branch(predicate, deploy, reject)

        # True branch has original predicate
        assert program.handles[0].guard is predicate
        # False branch has inverted predicate
        assert program.handles[1].guard is not None

    def test_branch_sets_metadata(self):
        """Test branch() marks branches in metadata."""
        deploy = facet("deploy").needs(CodeArtifact).build()
        reject = facet("reject").needs(CodeArtifact).build()

        program = branch(lambda x: True, deploy, reject)

        assert program.handles[0].metadata["branch"] == "true"
        assert program.handles[1].metadata["branch"] == "false"


class TestParallelCombinator:
    """Test parallel execution combinator."""

    def test_parallel_creates_multiple_handles(self):
        """Test parallel() creates handle for each facet."""
        f1 = facet("analyze_sec").needs(CodeArtifact).build()
        f2 = facet("analyze_perf").needs(CodeArtifact).build()
        f3 = facet("analyze_style").needs(CodeArtifact).build()

        program = parallel(f1, f2, f3)

        assert len(program.handles) == 3
        assert all(h.policy == RunPolicy.RUN_ONCE for h in program.handles)

    def test_parallel_extracts_triggers(self):
        """Test parallel() extracts triggers from each facet."""
        f1 = facet("f1").needs(CodeArtifact).build()
        f2 = facet("f2").needs(CodeArtifact).build()

        program = parallel(f1, f2)

        # Both should trigger on CodeArtifact
        assert all(len(h.triggers) > 0 for h in program.handles)
        assert all(h.triggers[0].fact_type == CodeArtifact for h in program.handles)


class TestFacetProgram:
    """Test FacetProgram validation."""

    def test_program_validate_duplicate_names(self):
        """Test validation catches duplicate facet names."""
        f1 = facet("duplicate").needs(TaskRequest).build()
        f2 = facet("duplicate").needs(PlanDoc).build()

        program = FacetProgram(handles=[
            FacetHandle(definition=f1),
            FacetHandle(definition=f2)
        ])

        errors = program.validate()
        assert any("duplicate" in e.lower() for e in errors)

    def test_program_validate_facet_level_errors(self):
        """Test validation passes facet-level errors through."""
        # Note: Program validation no longer checks missing dependencies
        # (facts may be seeded externally), but does check facet-level validation
        implement = facet("implement").needs(PlanDoc).build()

        program = FacetProgram(handles=[FacetHandle(definition=implement)])

        errors = program.validate()
        # Should have no errors (facet has needs, which is valid)
        assert len(errors) == 0

    def test_program_validate_satisfied_dependencies(self):
        """Test validation passes for well-formed programs."""
        plan = facet("plan").needs(TaskRequest).emit(PlanDoc, values={"c": "p"}).build()
        implement = facet("implement").needs(PlanDoc).emit(CodeArtifact, values={"s": "c"}).build()

        program = FacetProgram(handles=[
            FacetHandle(definition=plan),
            FacetHandle(definition=implement)
        ])

        errors = program.validate()
        # Should have no errors (all facets are valid)
        assert len(errors) == 0


class TestComplexWorkflows:
    """Test complex workflow patterns."""

    def test_end_to_end_pipeline(self):
        """Test complete plan→implement→review pipeline."""
        workflow = seq(
            facet("plan")
                .needs(TaskRequest, alias="task")
                .agent("planner")
                .emit(PlanDoc, values={"content": "$agent_response"})
                .build(),

            facet("implement")
                .needs(PlanDoc, alias="plan")
                .agent("coder")
                .emit(CodeArtifact, values={"summary": "$agent_response"})
                .build(),

            facet("review")
                .needs(CodeArtifact, alias="code")
                .agent("reviewer")
                .emit(ReviewVerdict, values={"verdict": "$agent_response"})
                .build()
        )

        assert len(workflow.handles) == 3
        assert workflow.handles[0].definition.name == "plan"
        assert workflow.handles[1].definition.name == "implement"
        assert workflow.handles[2].definition.name == "review"

        # Validate no errors
        errors = workflow.validate()
        assert len(errors) == 0
