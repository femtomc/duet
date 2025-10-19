"""
Tests for user-defined typed facts and fact-based operations.

Tests the migration from string channels to structured fact types.
"""

import uuid
from dataclasses import dataclass

import pytest

from duet.dataspace import (
    ApprovalGrant,
    ApprovalRequest,
    CodeArtifact,
    Dataspace,
    Fact,
    FactPattern,
    FactRegistry,
    PlanDoc,
    ReviewVerdict,
    fact,
)
from duet.dsl import FactExistsGuard, FactMatchesGuard, When
from duet.dsl.steps import FacetContext, ReadStep, WriteStep


# ──────────────────────────────────────────────────────────────────────────────
# User-Defined Fact Types
# ──────────────────────────────────────────────────────────────────────────────


@fact
@dataclass
class TaskRequest(Fact):
    """Custom fact type for testing."""

    fact_id: str
    task_description: str
    priority: int = 1


@fact
@dataclass
class FeatureSpec(Fact):
    """Custom fact type for testing."""

    fact_id: str
    feature_name: str
    status: str = "pending"
    assignee: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Fact Registry Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_fact_registration():
    """Test fact decorator registers types."""
    assert FactRegistry.get("TaskRequest") == TaskRequest
    assert FactRegistry.get("FeatureSpec") == FeatureSpec
    assert FactRegistry.get("PlanDoc") == PlanDoc


def test_fact_registry_lists_all_types():
    """Test registry contains all registered fact types."""
    all_types = FactRegistry.all_types()
    assert "TaskRequest" in all_types
    assert "PlanDoc" in all_types
    assert "CodeArtifact" in all_types


# ──────────────────────────────────────────────────────────────────────────────
# Dataspace Typed Fact Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_assert_and_query_typed_fact():
    """Test asserting and querying user-defined facts."""
    ds = Dataspace()

    # Assert custom fact
    task = TaskRequest(
        fact_id="task_001", task_description="Implement feature X", priority=1
    )
    handle = ds.assert_fact(task)

    # Query by type
    pattern = FactPattern(fact_type=TaskRequest)
    results = ds.query(pattern)

    assert len(results) == 1
    assert results[0].task_description == "Implement feature X"
    assert results[0].priority == 1


def test_query_with_constraints():
    """Test querying facts with field constraints."""
    ds = Dataspace()

    # Assert multiple tasks
    ds.assert_fact(TaskRequest(fact_id="t1", task_description="Task 1", priority=1))
    ds.assert_fact(TaskRequest(fact_id="t2", task_description="Task 2", priority=2))
    ds.assert_fact(TaskRequest(fact_id="t3", task_description="Task 3", priority=1))

    # Query with constraints
    pattern = FactPattern(fact_type=TaskRequest, constraints={"priority": 1})
    results = ds.query(pattern)

    assert len(results) == 2
    assert all(t.priority == 1 for t in results)


def test_retract_typed_fact():
    """Test retracting facts by handle."""
    ds = Dataspace()

    task = TaskRequest(fact_id="task_001", task_description="Test task", priority=1)
    handle = ds.assert_fact(task)

    # Verify exists
    pattern = FactPattern(fact_type=TaskRequest)
    assert len(ds.query(pattern)) == 1

    # Retract
    ds.retract(handle)

    # Verify removed
    assert len(ds.query(pattern)) == 0


def test_subscription_to_typed_facts():
    """Test subscribing to typed fact assertions."""
    ds = Dataspace()
    triggered = []

    # Subscribe to TaskRequest facts
    pattern = FactPattern(fact_type=TaskRequest, constraints={"priority": 1})
    ds.subscribe(pattern, lambda fact: triggered.append(fact))

    # Assert matching fact
    task = TaskRequest(fact_id="t1", task_description="High priority", priority=1)
    ds.assert_fact(task)

    # Verify callback triggered
    assert len(triggered) == 1
    assert triggered[0].task_description == "High priority"

    # Assert non-matching fact
    ds.assert_fact(TaskRequest(fact_id="t2", task_description="Low priority", priority=2))

    # Verify callback not triggered for non-matching
    assert len(triggered) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Fact-Based Guard Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_fact_exists_guard():
    """Test FactExistsGuard with typed facts."""
    ds = Dataspace()

    # Guard checking for approval verdict
    guard = When.fact_exists(ReviewVerdict, constraints={"verdict": "approve"})

    # Initially fails (no fact)
    assert not guard.evaluate({}, ds)

    # Assert matching fact
    ds.assert_fact(
        ReviewVerdict(
            fact_id="review_001", code_id="code_001", verdict="approve", feedback="LGTM"
        )
    )

    # Now passes
    assert guard.evaluate({}, ds)


def test_fact_matches_guard_with_predicate():
    """Test FactMatchesGuard with custom predicate."""
    ds = Dataspace()

    # Guard with complex predicate
    guard = When.fact_matches(
        ReviewVerdict,
        lambda fact: fact.verdict == "approve" and fact.feedback is not None,
    )

    # Initially fails
    assert not guard.evaluate({}, ds)

    # Assert fact without feedback
    ds.assert_fact(
        ReviewVerdict(fact_id="r1", code_id="c1", verdict="approve", feedback=None)
    )
    assert not guard.evaluate({}, ds)  # Predicate fails

    # Assert fact with feedback
    ds.assert_fact(
        ReviewVerdict(
            fact_id="r2", code_id="c1", verdict="approve", feedback="Looks good!"
        )
    )
    assert guard.evaluate({}, ds)  # Predicate passes


# ──────────────────────────────────────────────────────────────────────────────
# Facet Step Tests with Typed Facts
# ──────────────────────────────────────────────────────────────────────────────


def test_read_step_with_fact_type():
    """Test ReadStep querying typed facts from dataspace."""
    ds = Dataspace()
    context = FacetContext(phase_name="test", run_id="run_001", iteration=1)

    # Assert facts into dataspace
    ds.assert_fact(PlanDoc(fact_id="plan_001", task_id="task_001", content="Plan A"))

    # ReadStep with fact_type
    step = ReadStep(fact_type=PlanDoc, constraints={"task_id": "task_001"}, into="plan")

    result = step.execute(context, ds)

    assert result.success
    assert "plan" in result.context_updates
    assert result.context_updates["plan"].content == "Plan A"


def test_write_step_with_fact_type():
    """Test WriteStep asserting typed facts to dataspace."""
    ds = Dataspace()
    context = FacetContext(phase_name="test", run_id="run_001", iteration=1)

    # WriteStep with fact_type
    step = WriteStep(
        fact_type=ReviewVerdict,
        values={"code_id": "code_001", "verdict": "approve", "feedback": "LGTM"},
    )

    result = step.execute(context, ds)

    assert result.success

    # Verify fact was asserted
    pattern = FactPattern(fact_type=ReviewVerdict)
    facts = ds.query(pattern)
    assert len(facts) == 1
    assert facts[0].verdict == "approve"
    assert facts[0].feedback == "LGTM"


def test_write_step_with_context_values():
    """Test WriteStep using context values via $ syntax."""
    ds = Dataspace()
    context = FacetContext(phase_name="test", run_id="run_001", iteration=1)

    # Set values in context
    context.set("my_verdict", "approve")
    context.set("my_feedback", "Excellent work!")

    # WriteStep with $ syntax for context lookup
    step = WriteStep(
        fact_type=ReviewVerdict,
        values={
            "code_id": "code_001",
            "verdict": "$my_verdict",
            "feedback": "$my_feedback",
        },
    )

    result = step.execute(context, ds)

    assert result.success

    # Verify fact values came from context
    facts = ds.query(FactPattern(fact_type=ReviewVerdict))
    assert facts[0].verdict == "approve"
    assert facts[0].feedback == "Excellent work!"


# ──────────────────────────────────────────────────────────────────────────────
# Approval Flow Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_approval_request_and_grant():
    """Test ApprovalRequest and ApprovalGrant workflow."""
    ds = Dataspace()

    # Assert approval request
    request = ApprovalRequest(
        fact_id="req_001",
        requester="planner",
        reason="Need human review",
        context={"run_id": "run_001", "iteration": 1},
    )
    ds.assert_fact(request)

    # Check for approval (should be None initially)
    approval = ds.check_approval("req_001")
    assert approval is None

    # Grant approval
    grant = ApprovalGrant(
        fact_id="grant_001",
        request_id="req_001",
        approver="user",
        notes="Approved!",
    )
    ds.assert_fact(grant)

    # Check for approval (should now exist)
    approval = ds.check_approval("req_001")
    assert approval is not None
    assert approval.approver == "user"
    assert approval.notes == "Approved!"


def test_approval_subscription():
    """Test subscribing to approval grants."""
    ds = Dataspace()
    triggered = []

    # Subscribe to grants for specific request
    pattern = FactPattern(fact_type=ApprovalGrant, constraints={"request_id": "req_001"})
    ds.subscribe(pattern, lambda fact: triggered.append(fact))

    # Assert grant
    ds.assert_fact(
        ApprovalGrant(
            fact_id="grant_001", request_id="req_001", approver="user", notes="OK"
        )
    )

    # Verify callback triggered
    assert len(triggered) == 1
    assert triggered[0].request_id == "req_001"


# ──────────────────────────────────────────────────────────────────────────────
# Integration Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_end_to_end_typed_fact_workflow():
    """Test complete workflow with typed facts."""
    ds = Dataspace()

    # 1. Assert task request
    task = TaskRequest(
        fact_id="task_001", task_description="Build auth system", priority=1
    )
    ds.assert_fact(task)

    # 2. Assert plan
    plan = PlanDoc(
        fact_id="plan_001", task_id="task_001", content="Plan: Add OAuth support"
    )
    ds.assert_fact(plan)

    # 3. Assert code artifact
    code = CodeArtifact(
        fact_id="code_001",
        plan_id="plan_001",
        summary="Implemented OAuth",
        files_changed=3,
    )
    ds.assert_fact(code)

    # 4. Assert review verdict
    verdict = ReviewVerdict(
        fact_id="review_001", code_id="code_001", verdict="approve", feedback="LGTM"
    )
    ds.assert_fact(verdict)

    # Verify all facts exist
    assert len(ds.query(FactPattern(fact_type=TaskRequest))) == 1
    assert len(ds.query(FactPattern(fact_type=PlanDoc))) == 1
    assert len(ds.query(FactPattern(fact_type=CodeArtifact))) == 1
    assert len(ds.query(FactPattern(fact_type=ReviewVerdict))) == 1

    # Verify relationships
    plans = ds.query(FactPattern(fact_type=PlanDoc, constraints={"task_id": "task_001"}))
    assert len(plans) == 1

    codes = ds.query(FactPattern(fact_type=CodeArtifact, constraints={"plan_id": "plan_001"}))
    assert len(codes) == 1
