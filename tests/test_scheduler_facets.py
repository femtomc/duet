"""
Tests for scheduler integration with facet registrations.

Validates that the scheduler correctly handles FacetRegistration with
execution policies, triggers, and guards.
"""

import pytest

from duet.dataspace import CodeArtifact, Dataspace, PlanDoc, ReviewVerdict, TaskRequest
from duet.dsl import compile_program, facet, loop, seq
from duet.scheduler import ExecutionPolicy, FacetRegistration, FacetScheduler


class TestFacetRegistration:
    """Test FacetRegistration model."""

    def test_create_registration(self):
        """Test basic FacetRegistration creation."""
        plan_facet = (
            facet("plan")
            .needs(TaskRequest)
            .emit(PlanDoc, values={"content": "test"})
            .build()
        )

        phase = plan_facet.to_phase()

        registration = FacetRegistration(
            facet_id="plan",
            phase=phase,
            trigger_patterns=[],
            policy=ExecutionPolicy.RUN_ONCE
        )

        assert registration.facet_id == "plan"
        assert registration.phase == phase
        assert registration.policy == ExecutionPolicy.RUN_ONCE
        assert not registration.completed

    def test_should_execute_run_once_not_completed(self):
        """Test should_execute for RUN_ONCE when not completed."""
        plan_facet = facet("plan").needs(TaskRequest).build()

        registration = FacetRegistration(
            facet_id="plan",
            phase=plan_facet.to_phase(),
            trigger_patterns=[],
            policy=ExecutionPolicy.RUN_ONCE,
            completed=False
        )

        assert registration.should_execute()

    def test_should_execute_run_once_completed(self):
        """Test should_execute for RUN_ONCE when completed."""
        plan_facet = facet("plan").needs(TaskRequest).build()

        registration = FacetRegistration(
            facet_id="plan",
            phase=plan_facet.to_phase(),
            trigger_patterns=[],
            policy=ExecutionPolicy.RUN_ONCE,
            completed=True
        )

        assert not registration.should_execute()

    def test_should_execute_loop_until_with_guard(self):
        """Test should_execute for LOOP_UNTIL with guard."""
        test_facet = facet("test").needs(CodeArtifact).build()

        # Guard returns True to stop looping
        guard = lambda fact: fact.verdict == "pass"

        registration = FacetRegistration(
            facet_id="test",
            phase=test_facet.to_phase(),
            trigger_patterns=[],
            policy=ExecutionPolicy.LOOP_UNTIL,
            guard=guard
        )

        # Mock fact with verdict="fail" - should execute (guard False)
        from dataclasses import dataclass

        @dataclass
        class MockFact:
            verdict: str

        fail_fact = MockFact(verdict="fail")
        assert registration.should_execute([fail_fact])

        # Mock fact with verdict="pass" - should NOT execute (guard True)
        pass_fact = MockFact(verdict="pass")
        assert not registration.should_execute([pass_fact])


class TestSchedulerRegistration:
    """Test scheduler.register() with FacetRegistration."""

    def test_register_facet_registration(self):
        """Test registering a facet using FacetRegistration."""
        ds = Dataspace()
        scheduler = FacetScheduler(ds)

        plan_facet = (
            facet("plan")
            .needs(TaskRequest)
            .emit(PlanDoc, values={"content": "test"})
            .build()
        )

        registration = FacetRegistration(
            facet_id="plan",
            phase=plan_facet.to_phase(),
            trigger_patterns=plan_facet.to_phase().get_fact_reads(),
            policy=ExecutionPolicy.RUN_ONCE
        )

        scheduler.register(registration)

        assert "plan" in scheduler.registrations
        assert scheduler.registrations["plan"] == registration

    def test_facet_waits_for_triggers(self):
        """Test facet waits in waiting set until triggers satisfied."""
        ds = Dataspace()
        scheduler = FacetScheduler(ds)

        plan_facet = facet("plan").needs(TaskRequest).build()

        registration = FacetRegistration(
            facet_id="plan",
            phase=plan_facet.to_phase(),
            trigger_patterns=plan_facet.to_phase().get_fact_reads(),
            policy=ExecutionPolicy.RUN_ONCE
        )

        scheduler.register(registration)

        # Should be waiting (no TaskRequest in dataspace yet)
        assert "plan" in scheduler.waiting
        assert not scheduler.has_ready_facets()

    def test_facet_becomes_ready_when_triggers_satisfied(self):
        """Test facet moves to ready when triggers are satisfied."""
        ds = Dataspace()
        scheduler = FacetScheduler(ds)

        plan_facet = facet("plan").needs(TaskRequest).build()

        registration = FacetRegistration(
            facet_id="plan",
            phase=plan_facet.to_phase(),
            trigger_patterns=plan_facet.to_phase().get_fact_reads(),
            policy=ExecutionPolicy.RUN_ONCE
        )

        scheduler.register(registration)

        # Assert TaskRequest fact
        task = TaskRequest(
            fact_id="task_1",
            description="Test task",
            priority=1
        )
        ds.assert_fact(task)

        # Should now be ready
        assert "plan" in scheduler.ready_queue or scheduler.has_ready_facets()

    def test_run_once_not_re_queued_after_completion(self):
        """Test RUN_ONCE facet not re-queued after completion."""
        ds = Dataspace()
        scheduler = FacetScheduler(ds)

        plan_facet = facet("plan").needs(TaskRequest).emit(PlanDoc, values={"c": "p"}).build()

        registration = FacetRegistration(
            facet_id="plan",
            phase=plan_facet.to_phase(),
            trigger_patterns=plan_facet.to_phase().get_fact_reads(),
            policy=ExecutionPolicy.RUN_ONCE
        )

        scheduler.register(registration)

        # Seed task
        task = TaskRequest(fact_id="task_1", description="Test", priority=1)
        ds.assert_fact(task)

        # Execute facet
        facet_id = scheduler.next_ready()
        assert facet_id == "plan"

        scheduler.mark_executing(facet_id)
        scheduler.mark_completed(facet_id)

        # Registration should be marked completed
        assert scheduler.registrations["plan"].completed

        # New TaskRequest should NOT re-queue the facet
        task2 = TaskRequest(fact_id="task_2", description="Test 2", priority=1)
        ds.assert_fact(task2)

        # Should not be in ready queue
        assert "plan" not in scheduler.ready_queue


class TestCompiler:
    """Test compiler integration."""

    def test_compile_simple_seq(self):
        """Test compiling a simple sequential pipeline."""
        program = seq(
            facet("plan").needs(TaskRequest).emit(PlanDoc, values={"content": "p"}).build(),
            facet("implement").needs(PlanDoc).emit(CodeArtifact, values={"summary": "c"}).build()
        )

        registrations = compile_program(program)

        assert len(registrations) == 2
        assert registrations[0].facet_id == "plan"
        assert registrations[1].facet_id == "implement"
        assert all(r.policy == ExecutionPolicy.RUN_ONCE for r in registrations)

    def test_compile_preserves_triggers(self):
        """Test compiler preserves trigger patterns from combinators."""
        program = seq(
            facet("plan").needs(TaskRequest).emit(PlanDoc, values={"c": "p"}).build(),
            facet("implement").needs(PlanDoc).emit(CodeArtifact, values={"s": "c"}).build()
        )

        registrations = compile_program(program)

        # First facet triggers on TaskRequest
        assert len(registrations[0].trigger_patterns) == 1
        assert registrations[0].trigger_patterns[0].fact_type == TaskRequest

        # Second facet triggers on PlanDoc
        assert len(registrations[1].trigger_patterns) == 1
        assert registrations[1].trigger_patterns[0].fact_type == PlanDoc

    def test_compile_loop_preserves_guard(self):
        """Test compiler preserves guard predicate for loop()."""
        test_facet = facet("test").needs(CodeArtifact).emit(ReviewVerdict, values={"v": "test"}).build()
        guard = lambda result: result.verdict == "pass"

        handle = loop(test_facet, until=guard)

        from duet.dsl.compiler import compile_handle

        registration = compile_handle(handle)

        assert registration.policy == ExecutionPolicy.LOOP_UNTIL
        assert registration.guard is guard

    def test_compile_validates_program(self):
        """Test compiler validates program before compiling."""
        # Create program with duplicate facet names
        from duet.dsl.combinators import FacetHandle, FacetProgram, RunPolicy

        f1 = facet("duplicate").needs(TaskRequest).build()
        f2 = facet("duplicate").needs(PlanDoc).build()

        program = FacetProgram(handles=[
            FacetHandle(definition=f1, policy=RunPolicy.RUN_ONCE),
            FacetHandle(definition=f2, policy=RunPolicy.RUN_ONCE)
        ])

        with pytest.raises(ValueError, match="validation failed"):
            compile_program(program)


class TestEndToEndScheduling:
    """Test end-to-end scheduling with compiled programs."""

    def test_schedule_and_execute_pipeline(self):
        """Test full pipeline: compile → register → schedule → execute."""
        ds = Dataspace()
        scheduler = FacetScheduler(ds)

        # Build program
        program = seq(
            facet("plan")
                .needs(TaskRequest, alias="task")
                .emit(PlanDoc, values={"content": "plan", "task_id": "$task.fact_id"})
                .build(),

            facet("implement")
                .needs(PlanDoc, alias="plan")
                .emit(CodeArtifact, values={"summary": "code", "plan_id": "$plan.fact_id"})
                .build()
        )

        # Compile
        registrations = compile_program(program)

        # Register with scheduler
        for reg in registrations:
            scheduler.register(reg)

        # No facets ready yet (waiting for TaskRequest)
        assert not scheduler.has_ready_facets()

        # Seed TaskRequest
        task = TaskRequest(fact_id="task_1", description="Build feature", priority=1)
        ds.assert_fact(task)

        # First facet (plan) should be ready
        assert scheduler.has_ready_facets()
        facet_id = scheduler.next_ready()
        assert facet_id == "plan"

        # Execute plan facet (simulate)
        scheduler.mark_executing(facet_id)
        plan = PlanDoc(fact_id="plan_1", task_id="task_1", content="Implementation plan")
        ds.assert_fact(plan)
        scheduler.mark_completed(facet_id)

        # Second facet (implement) should now be ready
        assert scheduler.has_ready_facets()
        facet_id = scheduler.next_ready()
        assert facet_id == "implement"
