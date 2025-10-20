"""
Tests for facet-based orchestrator.

Uses echo adapter to simulate end-to-end workflow execution without
external dependencies.
"""

import pytest

from duet.adapters.echo import EchoAdapter
from duet.artifacts import ArtifactStore
from duet.config import DuetConfig
from duet.dataspace import CodeArtifact, PlanDoc, TaskRequest
from duet.dsl import facet, seq
from duet.orchestrator import Orchestrator, OrchestrationResult


@pytest.fixture
def echo_adapter():
    """Create echo adapter for testing."""
    return EchoAdapter()


@pytest.fixture
def artifact_store(tmp_path):
    """Create artifact store in temp directory."""
    from pathlib import Path
    return ArtifactStore(root=Path(tmp_path))


@pytest.fixture
def config(tmp_path):
    """Create minimal config for testing."""
    return DuetConfig(
        codex={"provider": "echo", "model": "echo"},
        claude={"provider": "echo", "model": "echo"},
        storage={
            "workspace_root": str(tmp_path),
            "run_artifact_dir": str(tmp_path / "runs")
        }
    )


class TestOrchestratorBasics:
    """Test basic orchestrator functionality."""

    def test_create_orchestrator(self, config, artifact_store):
        """Test orchestrator creation."""
        orch = Orchestrator(config=config, artifact_store=artifact_store)

        assert orch.config == config
        assert orch.artifact_store == artifact_store
        assert orch.workspace_root == "."

    def test_run_compilation_failure(self, config, artifact_store):
        """Test orchestrator handles compilation errors."""
        from duet.dsl.combinators import FacetHandle, FacetProgram, RunPolicy

        # Create invalid program (duplicate names)
        f1 = facet("duplicate").needs(TaskRequest).build()
        f2 = facet("duplicate").needs(PlanDoc).build()

        program = FacetProgram(handles=[
            FacetHandle(definition=f1, policy=RunPolicy.RUN_ONCE),
            FacetHandle(definition=f2, policy=RunPolicy.RUN_ONCE)
        ])

        orch = Orchestrator(config=config, artifact_store=artifact_store)

        result = orch.run(program, run_id="test_run", adapter=None)

        assert not result.success
        assert result.error is not None
        assert "validation failed" in result.error.lower()


class TestSingleFacetExecution:
    """Test single facet execution."""

    def test_execute_simple_facet_with_seed(self, config, artifact_store, echo_adapter):
        """Test executing a single facet with seeded fact."""
        from duet.dsl.combinators import FacetHandle, FacetProgram, RunPolicy

        # Single facet that needs TaskRequest
        plan_facet = (
            facet("plan")
            .needs(TaskRequest, alias="task")
            .agent("planner")
            .emit(PlanDoc, values={"content": "$agent_response", "task_id": "$task.fact_id"})
            .build()
        )

        program = FacetProgram(handles=[
            FacetHandle(definition=plan_facet, policy=RunPolicy.RUN_ONCE)
        ])

        orch = Orchestrator(
            config=config,
            artifact_store=artifact_store,
            workspace_root=str(artifact_store.root)
        )

        # Seed TaskRequest
        task = TaskRequest(fact_id="task_1", description="Test task", priority=1)

        result = orch.run(
            program,
            run_id="test_run",
            adapter=echo_adapter,
            initial_facts=[task]
        )

        # Should execute successfully
        assert result.success
        assert result.facets_executed == 1
        assert "plan" in result.completed_facets


class TestSequentialPipeline:
    """Test sequential pipeline execution."""

    def test_execute_two_facet_pipeline(self, config, artifact_store, echo_adapter, tmp_path):
        """Test executing a two-facet sequential pipeline."""
        # Build pipeline
        program = seq(
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

        orch = Orchestrator(
            config=config,
            artifact_store=artifact_store,
            workspace_root=str(tmp_path)
        )

        # Seed TaskRequest to start pipeline
        task = TaskRequest(fact_id="task_1", description="Build feature X", priority=1)

        result = orch.run(
            program,
            run_id="test_run",
            adapter=echo_adapter,
            max_iterations=10,
            initial_facts=[task]
        )

        # Should execute both facets
        assert result.success
        assert result.facets_executed == 2
        assert "plan" in result.completed_facets
        assert "implement" in result.completed_facets


class TestFullPipelineExecution:
    """Test full pipeline execution with initial facts."""

    def test_three_facet_pipeline(self, config, artifact_store, echo_adapter, tmp_path):
        """Test executing a complete plan→implement→review pipeline."""
        program = seq(
            facet("plan")
                .needs(TaskRequest, alias="task")
                .agent("planner")
                .emit(PlanDoc, values={"content": "$agent_response", "task_id": "$task.fact_id"})
                .build(),

            facet("implement")
                .needs(PlanDoc, alias="plan")
                .agent("coder")
                .emit(CodeArtifact, values={"summary": "$agent_response", "plan_id": "$plan.fact_id"})
                .build(),

            facet("review")
                .needs(CodeArtifact, alias="code")
                .agent("reviewer")
                .emit(PlanDoc, values={"content": "$agent_response", "task_id": "review_task"})
                .build()
        )

        orch = Orchestrator(config=config, artifact_store=artifact_store, workspace_root=str(tmp_path))

        task = TaskRequest(fact_id="task_1", description="Build authentication system", priority=1)

        result = orch.run(
            program,
            run_id="test_3stage",
            adapter=echo_adapter,
            initial_facts=[task],
            max_iterations=20
        )

        assert result.success
        assert result.facets_executed == 3
        assert result.iterations == 3
        assert len(result.completed_facets) == 3

    def test_pipeline_without_seed_waits(self, config, artifact_store, echo_adapter, tmp_path):
        """Test pipeline without seed facts waits."""
        program = seq(
            facet("plan").needs(TaskRequest).emit(PlanDoc, values={"c": "p"}).build(),
            facet("implement").needs(PlanDoc).emit(CodeArtifact, values={"s": "c"}).build()
        )

        orch = Orchestrator(config=config, artifact_store=artifact_store, workspace_root=str(tmp_path))

        # Run without seeding
        result = orch.run(program, run_id="test", adapter=echo_adapter, max_iterations=5)

        # No facets should execute (waiting for TaskRequest)
        assert result.facets_executed == 0
        assert "plan" in result.waiting_facets


class TestOrchestratorErrorHandling:
    """Test error handling in orchestrator."""

    def test_max_iterations_safety(self, config, artifact_store, echo_adapter):
        """Test max_iterations prevents infinite loops."""
        program = seq(
            facet("f1").needs(TaskRequest).emit(PlanDoc, values={"c": "p"}).build(),
            facet("f2").needs(PlanDoc).emit(CodeArtifact, values={"s": "c"}).build()
        )

        orch = Orchestrator(config=config, artifact_store=artifact_store)

        # Run with low max_iterations
        result = orch.run(program, run_id="test", adapter=echo_adapter, max_iterations=2)

        # Should respect max_iterations limit
        assert result.iterations <= 2

    def test_facet_execution_failure_stops_orchestration(self, config, artifact_store):
        """Test that facet failure stops orchestration."""
        # Create facet that will fail (no adapter provided)
        program = seq(
            facet("plan").needs(TaskRequest).agent("planner").emit(PlanDoc, values={"c": "p"}).build(),
            facet("implement").needs(PlanDoc).build()
        )

        orch = Orchestrator(config=config, artifact_store=artifact_store)

        # Seed TaskRequest to start execution
        task = TaskRequest(fact_id="task_1", description="Test", priority=1)

        result = orch.run(program, run_id="test", adapter=None, max_iterations=10, initial_facts=[task])

        # Should fail due to no adapter
        assert not result.success
        assert result.error is not None

    def test_partial_completion_reports_failure(self, config, artifact_store, echo_adapter):
        """Test that partial pipeline completion reports failure (Issue #2)."""
        from duet.dataspace import FactPattern, ReviewVerdict
        from duet.dsl.combinators import FacetHandle, FacetProgram, RunPolicy

        # Create program where first facet triggers immediately, second waits for fact that never arrives
        f1 = facet("f1").emit(TaskRequest, values={"description": "task", "priority": 1}).build()
        f2 = facet("f2").needs(ReviewVerdict).emit(CodeArtifact, values={"summary": "code", "plan_id": "p1"}).build()

        # f1 has no triggers (starts immediately), f2 triggers on ReviewVerdict (never provided)
        program = FacetProgram(handles=[
            FacetHandle(definition=f1, triggers=[], policy=RunPolicy.RUN_ONCE),
            FacetHandle(definition=f2, triggers=[FactPattern(fact_type=ReviewVerdict)], policy=RunPolicy.RUN_ONCE)
        ])

        orch = Orchestrator(config=config, artifact_store=artifact_store)

        result = orch.run(program, run_id="test", adapter=echo_adapter, max_iterations=10)

        # First facet executes, second waits for ReviewVerdict
        assert result.facets_executed == 1
        assert len(result.waiting_facets) == 1
        assert "f2" in result.waiting_facets
        # Success should be False (f2 waiting for facts, not approval)
        assert not result.success
