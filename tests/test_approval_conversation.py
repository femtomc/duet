"""
Test approval conversation pattern using dataspace facts.

Verifies Syndicate-style conversations: ApprovalRequest → ApprovalGranted.
"""

from duet.dataspace import ApprovalGrant, ApprovalRequest, Dataspace, FactPattern


def test_approval_conversation_pattern():
    """Test approval request/grant conversation."""
    ds = Dataspace()

    # Facet asserts approval request
    request = ApprovalRequest(
        fact_id="req-1",
        requester="review_phase",
        reason="Manual code review required",
        context={"run_id": "run-1", "iteration": 3},
    )

    req_handle = ds.assert_fact(request)

    # Check no approval yet
    assert ds.check_approval("req-1") is None

    # Human/system grants approval
    grant = ApprovalGrant(
        fact_id="grant-1",
        request_id="req-1",
        approver="human",
        notes="Looks good",
    )

    grant_handle = ds.assert_fact(grant)

    # Check approval granted
    granted = ds.check_approval("req-1")
    assert granted is not None
    assert granted.request_id == "req-1"
    assert granted.approver == "human"

    # Can retract both via handles
    ds.retract(req_handle)
    ds.retract(grant_handle)

    assert ds.check_approval("req-1") is None


def test_latest_only_query():
    """Test latest_only parameter for ChannelFact queries."""
    from duet.dataspace import ChannelFact

    ds = Dataspace()

    # Assert multiple versions of same channel
    ds.assert_fact(ChannelFact(
        fact_id="plan_v1",
        channel_name="plan",
        value="version 1",
        iteration=1,
    ))

    ds.assert_fact(ChannelFact(
        fact_id="plan_v2",
        channel_name="plan",
        value="version 2",
        iteration=2,
    ))

    ds.assert_fact(ChannelFact(
        fact_id="plan_v3",
        channel_name="plan",
        value="version 3",
        iteration=3,
    ))

    # Query without latest_only returns all
    pattern = FactPattern(fact_type=ChannelFact, constraints={"channel_name": "plan"})
    all_facts = ds.query(pattern, latest_only=False)
    assert len(all_facts) == 3

    # Query with latest_only returns newest
    latest = ds.query(pattern, latest_only=True)
    assert len(latest) == 1
    assert latest[0].value == "version 3"
    assert latest[0].iteration == 3


def test_multiple_channel_latest_only():
    """Test latest_only with multiple channels."""
    from duet.dataspace import ChannelFact

    ds = Dataspace()

    # Two channels with multiple versions each
    ds.assert_fact(ChannelFact(fact_id="plan_v1", channel_name="plan", value="plan v1", iteration=1))
    ds.assert_fact(ChannelFact(fact_id="plan_v2", channel_name="plan", value="plan v2", iteration=2))
    ds.assert_fact(ChannelFact(fact_id="code_v1", channel_name="code", value="code v1", iteration=1))
    ds.assert_fact(ChannelFact(fact_id="code_v2", channel_name="code", value="code v2", iteration=2))

    # Query all ChannelFacts with latest_only
    pattern = FactPattern(fact_type=ChannelFact)
    latest = ds.query(pattern, latest_only=True)

    # Should get latest of each channel
    assert len(latest) == 2
    plan_facts = [f for f in latest if f.channel_name == "plan"]
    code_facts = [f for f in latest if f.channel_name == "code"]
    assert len(plan_facts) == 1
    assert len(code_facts) == 1
    assert plan_facts[0].value == "plan v2"
    assert code_facts[0].value == "code v2"
