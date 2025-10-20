"""Tests for facet runner dataspace scoping and cleanup."""

from dataclasses import dataclass

from duet.dataspace import Dataspace, FactPattern, Message, MessageEvent, MessagePattern, PlanDoc
from duet.scheduler import FacetScheduler
from duet.dsl.steps import HumanStep, ReceiveMessageStep, SendMessageStep, WriteStep
from duet.dsl.workflow import Phase
from duet.facet_runner import FacetRunner


def test_facet_runner_cleans_child_dataspace_on_success():
    root = Dataspace()
    phase = Phase(
        name="writer",
        steps=[WriteStep(fact_type=PlanDoc, values={"content": "done", "task_id": "t-1"}, relay=True)],
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        dataspace=root,
        run_id="run-1",
        iteration=0,
        workspace_root=".",
        adapter=None,
    )

    assert result.success
    assert root.get_child("writer") is None
    facts = root.query(FactPattern(fact_type=PlanDoc))
    assert len(facts) == 1
    assert facts[0].content == "done"


def test_facet_runner_keeps_child_when_waiting():
    root = Dataspace()
    phase = Phase(name="approval", steps=[HumanStep(reason="Need review")])

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        dataspace=root,
        run_id="run-2",
        iteration=0,
        workspace_root=".",
        adapter=None,
    )

    assert result.human_approval_needed
    assert root.get_child("approval") is not None


def test_scheduler_cancel_waiting_facet_cleans_child():
    root = Dataspace()
    scheduler = FacetScheduler(root)
    child = root.ensure_child("pending")
    scheduler.waiting.add("pending")
    scheduler.set_waiting_child_dataspace("pending", child)

    canceled = scheduler.cancel_facet("pending")

    assert canceled
    assert root.get_child("pending") is None
    assert "pending" not in scheduler.waiting


@dataclass
class TestMessage(Message):
    __test__ = False
    topic: str
    payload: str


def test_facet_runner_receives_message_event():
    root = Dataspace()
    phase = Phase(
        name="listener",
        steps=[
            ReceiveMessageStep(
                message_type=TestMessage,
                alias="msg",
                constraints={"topic": "ping"},
            ),
            WriteStep(
                fact_type=PlanDoc,
                values={"content": "$msg.payload", "task_id": "task-1"},
                relay=True,
            ),
        ],
    )

    runner = FacetRunner()
    event = MessageEvent(message=TestMessage(topic="ping", payload="hello"), facet_id="sender")
    result = runner.execute_facet(
        phase=phase,
        dataspace=root,
        run_id="run-3",
        iteration=0,
        workspace_root=".",
        adapter=None,
        message_events=[event],
    )

    assert result.success
    assert root.get_child("listener") is None
    facts = root.query(FactPattern(fact_type=PlanDoc))
    assert len(facts) == 1
    assert facts[0].content == "hello"


def test_send_message_step_emits_to_parent():
    root = Dataspace()
    received = []
    root.subscribe_message(
        MessagePattern(message_type=TestMessage),
        lambda event: received.append(event),
    )

    phase = Phase(
        name="speaker",
        steps=[
            SendMessageStep(
                message_type=TestMessage,
                values={"topic": "ping", "payload": "hello"},
                relay=True,
            )
        ],
    )

    runner = FacetRunner()
    result = runner.execute_facet(
        phase=phase,
        dataspace=root,
        run_id="run-4",
        iteration=0,
        workspace_root=".",
        adapter=None,
    )

    assert result.success
    assert root.get_child("speaker") is None
    assert len(received) == 1
    assert received[0].message.payload == "hello"
    assert received[0].facet_id == "speaker.speaker"


def test_scheduler_wakes_on_message_event():
    root = Dataspace()
    scheduler = FacetScheduler(root)
    phase = Phase(
        name="listener",
        steps=[ReceiveMessageStep(message_type=TestMessage, alias="msg")],
    )

    scheduler.register_facet("listener", phase)
    assert "listener" in scheduler.waiting
    assert not scheduler.has_ready_facets()

    root.send_message(TestMessage(topic="ping", payload="hello"), facet_id="sender")

    assert scheduler.has_ready_facets()
    facet_id = scheduler.next_ready()
    assert facet_id == "listener"
    events = scheduler.pop_pending_messages(facet_id)
    assert len(events) == 1
    assert events[0].message.payload == "hello"
