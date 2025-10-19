"""
Test workflow: Analyze → Draft → Edit → Publish

Demonstrates content creation workflow with different phase names.
Uses object-based DSL (no strings).
"""

from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

# Define channels
topic = Channel(name="topic", schema="text", description="Content topic")
research = Channel(name="research", schema="text", description="Analysis and research")
draft = Channel(name="draft", schema="text", description="Initial draft")
editorial_verdict = Channel(name="editorial_verdict", schema="verdict", description="Editor's verdict")
revision_notes = Channel(name="revision_notes", schema="text", description="Editorial feedback")

# Define phases
analyze = Phase(
    name="analyze",
    agent="analyst",
    consumes=[topic, revision_notes],
    publishes=[research],
    description="Research the topic",
    metadata={"role_hint": "planner"},
)

draft_phase = Phase(
    name="draft",
    agent="writer",
    consumes=[research],
    publishes=[draft],
    description="Write initial draft",
    metadata={"role_hint": "implementer"},
)

edit = Phase(
    name="edit",
    agent="editor",
    consumes=[research, draft],
    publishes=[editorial_verdict, revision_notes],
    description="Review and edit draft",
    metadata={
        "role_hint": "reviewer",
        "replan_transition": True,
    },
)

publish = Phase(name="publish", agent="editor", is_terminal=True)
rejected = Phase(name="rejected", agent="editor", is_terminal=True)

# Define workflow
workflow = Workflow(
    agents=[
        Agent(name="analyst", provider="echo", model="echo-v1"),
        Agent(name="writer", provider="echo", model="echo-v1"),
        Agent(name="editor", provider="echo", model="echo-v1"),
    ],
    channels=[topic, research, draft, editorial_verdict, revision_notes],
    phases=[analyze, draft_phase, edit, publish, rejected],
    transitions=[
        Transition(from_phase=analyze, to_phase=draft_phase),
        Transition(from_phase=draft_phase, to_phase=edit),
        Transition(
            from_phase=edit,
            to_phase=publish,
            when=When.channel_has(editorial_verdict, "approve"),
            priority=10,
        ),
        Transition(
            from_phase=edit,
            to_phase=analyze,
            when=When.channel_has(editorial_verdict, "changes_requested"),
            priority=5,
        ),
        Transition(
            from_phase=edit,
            to_phase=rejected,
            when=When.channel_has(editorial_verdict, "blocked"),
            priority=15,
        ),
    ],
    initial_phase=analyze,
    task_channel=topic,
)
