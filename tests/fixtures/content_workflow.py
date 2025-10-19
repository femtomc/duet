"""
Test workflow: Analyze → Draft → Edit → Publish

Demonstrates content creation workflow with different phase names.
"""

from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[
        Agent(name="analyst", provider="echo", model="echo-v1"),
        Agent(name="writer", provider="echo", model="echo-v1"),
        Agent(name="editor", provider="echo", model="echo-v1"),
    ],
    channels=[
        Channel(name="topic", schema="text", description="Content topic"),
        Channel(name="research", schema="text", description="Analysis and research"),
        Channel(name="draft", schema="text", description="Initial draft"),
        Channel(name="editorial_verdict", schema="verdict", description="Editor's verdict"),
        Channel(name="revision_notes", schema="text", description="Editorial feedback"),
    ],
    phases=[
        Phase(
            name="analyze",
            agent="analyst",
            consumes=["topic", "revision_notes"],
            publishes=["research"],
            description="Research the topic",
            metadata={"role_hint": "planner"},
        ),
        Phase(
            name="draft",
            agent="writer",
            consumes=["research"],
            publishes=["draft"],
            description="Write initial draft",
            metadata={"role_hint": "implementer"},
        ),
        Phase(
            name="edit",
            agent="editor",
            consumes=["research", "draft"],
            publishes=["editorial_verdict", "revision_notes"],
            description="Review and edit draft",
            metadata={
                "role_hint": "reviewer",
                "replan_transition": True,
            },
        ),
        Phase(name="publish", agent="editor", is_terminal=True),
        Phase(name="rejected", agent="editor", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="analyze", to_phase="draft"),
        Transition(from_phase="draft", to_phase="edit"),
        Transition(
            from_phase="edit",
            to_phase="publish",
            when=When.channel_has("editorial_verdict", "approve"),
            priority=10,
        ),
        Transition(
            from_phase="edit",
            to_phase="analyze",
            when=When.channel_has("editorial_verdict", "changes_requested"),
            priority=5,
        ),
        Transition(
            from_phase="edit",
            to_phase="rejected",
            when=When.channel_has("editorial_verdict", "blocked"),
            priority=15,
        ),
    ],
    initial_phase="analyze",
    task_channel="topic",
)
