"""
Tests for Turn-based atomic publication (Syndicate-style).

Verifies that subscription callbacks are deferred until turn end.
"""

from duet.dataspace import Dataspace, PlanDoc, CodeArtifact, FactPattern


def test_turn_batches_notifications():
    """Test that turn defers subscription callbacks until end."""
    ds = Dataspace()
    triggered = []

    def callback(fact):
        triggered.append(fact)

    # Subscribe
    ds.subscribe(FactPattern(fact_type=PlanDoc), callback)

    # Assert facts inside turn
    with ds.in_turn():
        ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="Plan A"))
        ds.assert_fact(PlanDoc(fact_id="plan-2", task_id="task-2", content="Plan B"))

        # No callbacks triggered yet
        assert len(triggered) == 0

    # After turn ends, all notifications delivered
    assert len(triggered) == 2
    assert triggered[0].fact_id == "plan-1"
    assert triggered[1].fact_id == "plan-2"


def test_immediate_delivery_outside_turn():
    """Test that facts trigger immediately when not in turn."""
    ds = Dataspace()
    triggered = []

    def callback(fact):
        triggered.append(fact)

    ds.subscribe(FactPattern(fact_type=PlanDoc), callback)

    # Assert without turn - immediate delivery
    ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="Plan A"))

    assert len(triggered) == 1


def test_nested_turns_not_supported():
    """Test turn behavior (no nesting support currently)."""
    ds = Dataspace()
    triggered = []

    def callback(fact):
        triggered.append(fact)

    ds.subscribe(FactPattern(fact_type=PlanDoc), callback)

    # Single turn works
    with ds.in_turn():
        ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="Plan A"))

    assert len(triggered) == 1


def test_turn_with_multiple_fact_types():
    """Test turn batching with different fact types."""
    ds = Dataspace()
    plan_triggered = []
    code_triggered = []

    ds.subscribe(FactPattern(fact_type=PlanDoc), lambda f: plan_triggered.append(f))
    ds.subscribe(FactPattern(fact_type=CodeArtifact), lambda f: code_triggered.append(f))

    with ds.in_turn():
        ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="Plan"))
        ds.assert_fact(CodeArtifact(fact_id="code-1", plan_id="plan-1", summary="Code"))
        ds.assert_fact(PlanDoc(fact_id="plan-2", task_id="task-2", content="Plan 2"))

        # No triggers yet
        assert len(plan_triggered) == 0
        assert len(code_triggered) == 0

    # All delivered at end
    assert len(plan_triggered) == 2
    assert len(code_triggered) == 1


def test_turn_callback_error_doesnt_break_delivery():
    """Test that callback errors don't prevent other callbacks."""
    ds = Dataspace()
    triggered = []

    def bad_callback(fact):
        raise RuntimeError("Callback error")

    def good_callback(fact):
        triggered.append(fact)

    # Subscribe both
    ds.subscribe(FactPattern(fact_type=PlanDoc), bad_callback)
    ds.subscribe(FactPattern(fact_type=PlanDoc), good_callback)

    with ds.in_turn():
        ds.assert_fact(PlanDoc(fact_id="plan-1", task_id="task-1", content="Plan"))

    # Good callback still triggered despite bad callback error
    assert len(triggered) == 1
