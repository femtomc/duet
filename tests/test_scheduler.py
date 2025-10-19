"""
Tests for reactive facet scheduler.

Verifies event-driven execution based on fact availability.
"""

import pytest

from duet.dsl import Channel, Phase
from duet.dataspace import ChannelFact, Dataspace
from duet.scheduler import FacetScheduler


def test_scheduler_facet_ready_when_inputs_available():
    """Test that facet becomes ready when input facts are available."""
    ds = Dataspace()
    scheduler = FacetScheduler(ds)

    # Define facet that reads task channel
    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    plan_facet = (
        Phase(name="plan", agent="planner")
        .read(task)
        .call_agent("planner", writes=[plan_ch])
    )

    # Facet starts waiting (no task fact yet)
    scheduler.register_facet("plan_facet", plan_facet)
    assert "plan_facet" in scheduler.waiting
    assert not scheduler.has_ready_facets()

    # Assert task fact
    ds.assert_fact(ChannelFact(
        fact_id="task_1",
        channel_name="task",
        value="Build feature",
        iteration=0,
    ))

    # Facet should now be ready
    assert scheduler.has_ready_facets()
    facet_id = scheduler.next_ready()
    assert facet_id == "plan_facet"


def test_scheduler_facet_immediately_ready():
    """Test facet immediately ready if inputs already present."""
    ds = Dataspace()

    # Seed dataspace with task fact
    ds.assert_fact(ChannelFact(
        fact_id="task_1",
        channel_name="task",
        value="Build feature",
        iteration=0,
    ))

    scheduler = FacetScheduler(ds)

    # Define facet
    task = Channel(name="task")
    plan_ch = Channel(name="plan")

    plan_facet = (
        Phase(name="plan", agent="planner")
        .read(task)
        .call_agent("planner", writes=[plan_ch])
    )

    # Register facet - should be immediately ready
    scheduler.register_facet("plan_facet", plan_facet)

    assert scheduler.has_ready_facets()
    assert "plan_facet" not in scheduler.waiting


def test_scheduler_multiple_facets():
    """Test scheduling multiple facets in order."""
    ds = Dataspace()

    # Seed with task
    ds.assert_fact(ChannelFact(fact_id="task_1", channel_name="task", value="X", iteration=0))

    scheduler = FacetScheduler(ds)

    # Register two facets
    task = Channel(name="task")
    plan = Channel(name="plan")
    code = Channel(name="code")

    plan_facet = Phase(name="plan", agent="p").read(task).call_agent("p", writes=[plan])
    impl_facet = Phase(name="impl", agent="i").read(plan).call_agent("i", writes=[code])

    scheduler.register_facet("plan", plan_facet)
    scheduler.register_facet("impl", impl_facet)

    # Plan ready (has task), impl waiting (no plan yet)
    assert scheduler.has_ready_facets()
    assert "impl" in scheduler.waiting

    # Get plan facet
    next_facet = scheduler.next_ready()
    assert next_facet == "plan"

    # After plan executes, assert plan fact
    ds.assert_fact(ChannelFact(fact_id="plan_1", channel_name="plan", value="Plan doc", iteration=1))

    # Now impl should be ready
    assert scheduler.has_ready_facets()
    next_facet = scheduler.next_ready()
    assert next_facet == "impl"


def test_scheduler_marks_executing_completed():
    """Test facet state tracking (executing → completed)."""
    ds = Dataspace()
    ds.assert_fact(ChannelFact(fact_id="task_1", channel_name="task", value="X", iteration=0))

    scheduler = FacetScheduler(ds)

    task = Channel(name="task")
    plan = Channel(name="plan")
    facet = Phase(name="plan", agent="p").read(task).call_agent("p", writes=[plan])

    scheduler.register_facet("facet_1", facet)

    # Mark executing
    facet_id = scheduler.next_ready()
    scheduler.mark_executing(facet_id)
    assert scheduler.executing == "facet_1"

    # Mark completed
    scheduler.mark_completed(facet_id)
    assert scheduler.executing is None


def test_scheduler_mark_waiting_for_approval():
    """Test facet marked waiting after HumanStep."""
    ds = Dataspace()
    ds.assert_fact(ChannelFact(fact_id="code_1", channel_name="code", value="X", iteration=0))

    scheduler = FacetScheduler(ds)

    code = Channel(name="code")
    verdict = Channel(name="verdict")

    review_facet = (
        Phase(name="review", agent="r")
        .read(code)
        .human("Approval needed")
        .call_agent("r", writes=[verdict])
    )

    scheduler.register_facet("review", review_facet)
    facet_id = scheduler.next_ready()
    scheduler.mark_executing(facet_id)

    # After HumanStep, mark waiting
    scheduler.mark_waiting(facet_id)

    assert facet_id in scheduler.waiting
    assert scheduler.executing is None
