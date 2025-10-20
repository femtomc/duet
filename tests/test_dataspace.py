"""
Tests for Sprint DSL-5 dataspace model.

Verifies structured fact storage, subscriptions, and reactive patterns.
"""

from typing import List

import pytest

from dataclasses import dataclass

from duet.dataspace import (
    ApprovalGrant,
    ApprovalRequest,
    CodeArtifact,
    Dataspace,
    FactPattern,
    FactEvent,
    Message,
    MessageEvent,
    MessagePattern,
    FactInterest,
    PlanDoc,
    ReviewVerdict,
)


def test_dataspace_assert_and_query():
    """Test asserting and querying facts."""
    ds = Dataspace()

    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Build feature X", iteration=1)
    handle = ds.assert_fact(plan)

    # assert_fact returns Handle
    assert handle is not None
    assert handle.fact_id == "plan-1"
    assert handle.facet_id == "__anon__"

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
    """Test retracting facts via handle."""
    ds = Dataspace()

    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X")
    handle = ds.assert_fact(plan)

    assert len(ds.facts) == 1

    # Retract using handle (Syndicate-style)
    retracted = ds.retract(handle)

    assert retracted is not None
    assert retracted.fact_id == "plan-1"
    assert len(ds.facts) == 0


def test_dataspace_handle_lifecycle():
    """Test handle-based fact lifecycle (assert → retract)."""
    ds = Dataspace()

    # Assert fact, get handle
    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Feature X")
    handle = ds.assert_fact(plan)

    # Fact present
    assert ds.get_fact("plan-1") is not None

    # Retract via handle
    retracted = ds.retract(handle)
    assert retracted.fact_id == "plan-1"

    # Fact gone
    assert ds.get_fact("plan-1") is None


def test_dataspace_subscription():
    """Test subscription callbacks."""
    ds = Dataspace()
    triggered = []

    def callback(event: FactEvent):
        triggered.append(event.fact)

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

    def callback(event: FactEvent):
        triggered.append(event.fact)

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
    def handle_approval_request(event: FactEvent):
        if event.action != "asserted":
            return
        fact = event.fact
        # Simulates human/tool granting approval
        grant = ApprovalGrant(
            fact_id=f"grant-{fact.fact_id}",
            request_id=fact.fact_id,
            approver="human",
        )
        ds.assert_fact(grant)

    ds.subscribe(FactPattern(fact_type=ApprovalRequest), handle_approval_request)

    # Another facet subscribes to approvals
    def handle_approval_grant(event: FactEvent):
        if event.action == "asserted":
            approved.append(event.fact)

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


def test_nested_dataspace_local_scope():
    """Facts asserted in child dataspace remain local by default."""
    root = Dataspace()
    child = root.spawn_child("child")

    plan = PlanDoc(fact_id="plan-1", task_id="task-1", content="Nested Plan")
    child.assert_fact(plan, facet_id="planner")

    # Child sees fact, parent does not
    child_results = child.query(FactPattern(fact_type=PlanDoc))
    root_results = root.query(FactPattern(fact_type=PlanDoc))

    assert len(child_results) == 1
    assert child_results[0].fact_id == "plan-1"
    assert root_results == []


def test_nested_dataspace_relay_to_parent():
    """Facts can be relayed from child to parent dataspace."""
    root = Dataspace()
    child = root.spawn_child("child")

    triggered: List[FactEvent] = []
    root.subscribe(FactPattern(fact_type=PlanDoc), lambda event: triggered.append(event))

    plan = PlanDoc(fact_id="plan-2", task_id="task-2", content="Relayed Plan")
    handle = child.assert_fact(plan, facet_id="planner", relay=True)

    # Root should receive relayed event with namespaced facet id
    assert len(triggered) == 1
    assert triggered[0].action == "asserted"
    assert triggered[0].facet_id == "child.planner"

    # Both child and parent see the fact
    assert child.query(FactPattern(fact_type=PlanDoc))
    assert root.query(FactPattern(fact_type=PlanDoc))

    # Retracting from child removes from parent
    child.retract(handle)
    assert child.query(FactPattern(fact_type=PlanDoc)) == []
    assert root.query(FactPattern(fact_type=PlanDoc)) == []


def test_dataspace_interest_registration():
    """Registering interests asserts FactInterest and triggers callbacks."""
    ds = Dataspace()
    triggered: List[FactEvent] = []

    pattern = FactPattern(fact_type=PlanDoc, constraints={"task_id": "task-42"})
    handle = ds.register_interest(pattern, facet_id="listener", callback=lambda event: triggered.append(event))

    plan = PlanDoc(fact_id="plan-42", task_id="task-42", content="Interested")
    ds.assert_fact(plan, facet_id="planner")

    assert len(triggered) == 1
    assert triggered[0].fact.fact_id == "plan-42"

    interest_records = ds.query(
        FactPattern(fact_type=FactInterest, constraints={"facet_id": "listener"})
    )
    assert interest_records

    ds.unregister_interest(handle)

    interest_records_after = ds.query(
        FactPattern(fact_type=FactInterest, constraints={"facet_id": "listener"})
    )
    assert interest_records_after == []


def test_dataspace_unregister_all_interests_for_facet():
    ds = Dataspace()
    pattern = FactPattern(fact_type=PlanDoc, constraints={"task_id": "x"})
    ds.register_interest(pattern, facet_id="listener", callback=lambda event: None)
    ds.unregister_interests_for_facet("listener")
    interest_records_after = ds.query(
        FactPattern(fact_type=FactInterest, constraints={"facet_id": "listener"})
    )
    assert interest_records_after == []


def test_write_step_relay_from_child():
    """WriteStep with relay=True mirrors assertion to parent dataspace."""
    from duet.dsl.steps import FacetContext, WriteStep

    root = Dataspace()
    child = root.spawn_child("child")

    context = FacetContext(
        facet_id="writer",
        phase_name="writer",
        run_id="run-1",
        iteration=0,
        workspace_root=".",
    )

    step = WriteStep(
        fact_type=PlanDoc,
        values={"content": "Relay", "task_id": "t-1"},
        relay=True,
    )

    result = step.execute(context, child)
    assert result.success

    # Fact visible in child and parent
    assert child.query(FactPattern(fact_type=PlanDoc))
    relayed = root.query(FactPattern(fact_type=PlanDoc))
    assert relayed and relayed[0].content == "Relay"


@dataclass
class TestMessage(Message):
    __test__ = False
    topic: str
    payload: str


def test_message_subscription():
    ds = Dataspace()
    received: List[MessageEvent] = []

    ds.subscribe_message(MessagePattern(message_type=TestMessage, constraints={"topic": "test"}), lambda event: received.append(event))

    ds.send_message(TestMessage(topic="test", payload="hello"), facet_id="sender")

    assert len(received) == 1
    assert received[0].message.payload == "hello"
    assert received[0].facet_id == "sender"


def test_message_relay_to_parent():
    root = Dataspace()
    child = root.spawn_child("child")
    events: List[MessageEvent] = []

    root.subscribe_message(MessagePattern(message_type=TestMessage), lambda event: events.append(event))

    child.send_message(TestMessage(topic="info", payload="ping"), facet_id="child_facet", relay=True)

    assert len(events) == 1
    assert events[0].facet_id == "child.child_facet"
    assert events[0].message.payload == "ping"
