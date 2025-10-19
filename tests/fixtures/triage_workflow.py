"""
Test workflow: Triage → Fix → QA → Success

Demonstrates arbitrary phase names with metadata-driven behavior.
"""

from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[
        Agent(name="qa_agent", provider="echo", model="echo-v1"),
        Agent(name="dev_agent", provider="echo", model="echo-v1"),
    ],
    channels=[
        Channel(name="issue", schema="text", description="Bug report or feature request"),
        Channel(name="diagnosis", schema="text", description="Triage assessment"),
        Channel(name="fix_description", schema="text", description="Implementation summary"),
        Channel(name="test_result", schema="verdict", description="QA verdict"),
        Channel(name="notes", schema="text", description="Feedback notes"),
    ],
    phases=[
        Phase(
            name="triage",
            agent="qa_agent",
            consumes=["issue", "notes"],
            publishes=["diagnosis"],
            description="Assess severity and provide diagnosis",
            metadata={
                "role_hint": "planner",
            },
        ),
        Phase(
            name="fix",
            agent="dev_agent",
            consumes=["diagnosis"],
            publishes=["fix_description"],
            description="Implement the fix",
            metadata={
                "role_hint": "implementer",
                "git_changes_required": True,
            },
        ),
        Phase(
            name="qa",
            agent="qa_agent",
            consumes=["diagnosis", "fix_description"],
            publishes=["test_result", "notes"],
            description="Verify the fix works",
            metadata={
                "role_hint": "reviewer",
                "replan_transition": True,  # qa -> triage counts as replan
            },
        ),
        Phase(name="success", agent="qa_agent", is_terminal=True),
        Phase(name="blocked", agent="qa_agent", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="triage", to_phase="fix"),
        Transition(from_phase="fix", to_phase="qa"),
        Transition(
            from_phase="qa",
            to_phase="success",
            when=When.verdict("approve"),
            priority=10,
        ),
        Transition(
            from_phase="qa",
            to_phase="triage",
            when=When.verdict("changes_requested"),
            priority=5,
        ),
        Transition(
            from_phase="qa",
            to_phase="blocked",
            when=When.verdict("blocked"),
            priority=15,
        ),
    ],
    initial_phase="triage",
    task_channel="issue",
)
