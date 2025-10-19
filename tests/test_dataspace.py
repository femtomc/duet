"""
Tests for Sprint DSL-5 dataspace model.

Verifies structured fact storage, subscriptions, and reactive patterns.
"""

import pytest

from duet.dataspace import (
    ApprovalGrant,
    ApprovalRequest,
    CodeArtifact,
    Dataspace,
    FactPattern,
    PlanDoc,
    ReviewVerdict,
)


def test_dataspace_assert_and_query():
    """Test asserting and querying facts."""
    ds = Dataspace()

    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Build feature X", iteration=1)
    ds.assert_fact(plan)

    # Query by type
    pattern = FactPattern(fact_type=PlanDoc)
    results = ds.query(pattern)

    assert len(results) == 1
    assert results[0].fact_id == "plan-1"
    assert results[0].content == "Build feature X"


def test_dataspace_query_with_constraints():
    """Test querying with constraints."""
    ds = Dataspace()

    ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X", iteration=1))
    ds.assert_fact(PlanDoc(fact_id="plan-2", task_id="task-2", content="Feature Y", iteration=1))
    ds.assert_fact(PlanDoc(fact_id="plan-3", task_id="task-1", content="Feature X v2", iteration=2))

    # Query for specific task_id
    pattern = FactPattern(fact_type=PlanDoc, constraints={"task_id": "task-1"})
    results = ds.query(pattern)

    assert len(results) == 2
    assert all(f.task_id == "task-1" for f in results)


def test_dataspace_retract_fact():
    """Test retracting facts."""
    ds = Dataspace()

    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X")
    ds.assert_fact(plan)

    assert len(ds.facts) == 1

    retracted = ds.retract_fact("plan-1")

    assert retracted is not None
    assert retracted.fact_id == "plan-1"
    assert len(ds.facts) == 0


def test_dataspace_subscription():
    """Test subscription callbacks."""
    ds = Dataspace()
    triggered = []

    def callback(fact):
        triggered.append(fact)

    # Subscribe before asserting
    pattern = FactPattern(fact_type=PlanDoc)
    ds.subscribe(pattern, callback)

    # Assert fact
    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X")
    ds.assert_fact(plan)

    # Callback should have been triggered
    assert len(triggered) == 1
    assert triggered[0].fact_id == "plan-1"


def test_dataspace_subscription_existing_facts():
    """Test subscription triggers for existing facts."""
    ds = Dataspace()

    # Assert fact first
    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X")
    ds.assert_fact(plan)

    triggered = []

    def callback(fact):
        triggered.append(fact)

    # Subscribe after asserting
    pattern = FactPattern(fact_type=PlanDoc)
    ds.subscribe(pattern, callback)

    # Should immediately trigger for existing fact
    assert len(triggered) == 1
    assert triggered[0].fact_id == "plan-1"


def test_dataspace_multiple_fact_types():
    """Test dataspace with multiple fact types."""
    ds = Dataspace()

    ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X"))
    ds.assert_fact(CodeArtifact(fact_id="code-1", plan_id="plan-1", summary="Implemented X", files_changed=3))
    ds.assert_fact(ReviewVerdict(fact_id="review-1", code_id="code-1", verdict="approve"))

    # Query each type
    plans = ds.query(FactPattern(fact_type=PlanDoc))
    code = ds.query(FactPattern(fact_type=CodeArtifact))
    reviews = ds.query(FactPattern(fact_type=ReviewVerdict))

    assert len(plans) == 1
    assert len(code) == 1
    assert len(reviews) == 1


def test_dataspace_approval_conversation():
    """Test approval conversation pattern with facts."""
    ds = Dataspace()
    approved = []

    # Facet subscribes to approval requests
    def handle_approval_request(fact):
        # Simulates human/tool granting approval
        grant = ApprovalGrant(
            fact_id=f"grant-{fact.fact_id}",
            request_id=fact.fact_id,
            approver="human",
        )
        ds.assert_fact(grant)

    ds.subscribe(FactPattern(fact_type=ApprovalRequest), handle_approval_request)

    # Another facet subscribes to approvals
    def handle_approval_grant(fact):
        approved.append(fact)

    ds.subscribe(FactPattern(fact_type=ApprovalGrant), handle_approval_grant)

    # Assert approval request
    request = ApprovalRequest(
        fact_id="request-1",
        requester="reviewer",
        reason="Code review needed",
    )
    ds.assert_fact(request)

    # Should have triggered both subscriptions
    assert len(approved) == 1
    assert approved[0].request_id == "request-1"


def test_dataspace_clear():
    """Test clearing dataspace."""
    ds = Dataspace()

    ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="X"))
    ds.assert_fact(CodeArtifact(fact_id="code-1", plan_id="plan-1", summary="Y"))

    assert len(ds.facts) == 2

    ds.clear()

    assert len(ds.facts) == 0
    assert len(ds.subscriptions) == 0


def test_fact_pattern_matching():
    """Test fact pattern matching logic."""
    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X", iteration=1)

    # Match by type only
    pattern1 = FactPattern(fact_type=PlanDoc)
    assert pattern1.matches(plan)

    # Match by type + constraint
    pattern2 = FactPattern(fact_type=PlanDoc, constraints={"task_id": "task-1"})
    assert pattern2.matches(plan)

    # Mismatch constraint
    pattern3 = FactPattern(fact_type=PlanDoc, constraints={"task_id": "task-2"})
    assert not pattern3.matches(plan)

    # Wrong type
    pattern4 = FactPattern(fact_type=CodeArtifact)
    assert not pattern4.matches(plan)
