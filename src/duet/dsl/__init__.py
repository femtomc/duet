"""
Duet Workflow DSL.

A Python DSL for defining orchestration workflows programmatically.
Replaces the legacy .duet/prompts/*.md template system with a type-safe,
composable workflow definition language.

Example Workflow:
    from duet.dsl import Workflow, Agent, Channel, Phase, Transition, When

    workflow = Workflow(
        agents=[
            Agent(name="planner", provider="codex", model="gpt-5-codex"),
            Agent(name="implementer", provider="claude", model="sonnet"),
            Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
        ],
        channels=[
            Channel(name="task", description="Input task specification"),
            Channel(name="plan", description="Implementation plan from planner"),
            Channel(name="code", description="Implementation artifacts"),
            Channel(name="verdict", description="Review outcome"),
        ],
        phases=[
            Phase(name="plan", agent="planner",
                  consumes=["task"], publishes=["plan"],
                  description="Draft implementation plan"),
            Phase(name="implement", agent="implementer",
                  consumes=["plan"], publishes=["code"],
                  description="Execute plan and make changes"),
            Phase(name="review", agent="reviewer",
                  consumes=["plan", "code"], publishes=["verdict"],
                  description="Review the implementation"),
            Phase(name="done", agent="reviewer",
                  description="Task complete", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="plan", to_phase="implement"),
            Transition(from_phase="implement", to_phase="review"),
            Transition(from_phase="review", to_phase="done",
                       when=When.channel_has("verdict", "approve")),
            Transition(from_phase="review", to_phase="plan",
                       when=When.channel_has("verdict", "changes_requested")),
        ],
    )

Custom Fact Types (Typed Facts):
    from dataclasses import dataclass
    from duet.dsl import Fact, fact, FactPattern

    # Define your own fact types
    @fact
    @dataclass
    class TaskRequest(Fact):
        fact_id: str
        task_description: str
        priority: int = 1

    # Use in workflows with dataspace
    from duet.dataspace import Dataspace

    ds = Dataspace()
    handle = ds.assert_fact(
        TaskRequest(
            fact_id="task_123",
            task_description="Implement feature X",
            priority=1
        )
    )

    # Query for facts
    tasks = ds.query(FactPattern(fact_type=TaskRequest, constraints={"priority": 1}))
"""

from .workflow import (
    Agent,
    Channel,
    Guard,
    Phase,
    Transition,
    When,
    Workflow,
    # Guard types for advanced usage
    AlwaysGuard,
    AndGuard,
    ChannelHasGuard,
    EmptyGuard,
    FactExistsGuard,
    FactMatchesGuard,
    GitChangesGuard,
    NeverGuard,
    NotGuard,
    OrGuard,
    VerdictGuard,
)

# Import fact types and utilities for user-defined facts
from ..dataspace import (
    ApprovalGrant,
    ApprovalRequest,
    CodeArtifact,
    Fact,
    FactPattern,
    FactRegistry,
    Handle,
    PlanDoc,
    ReviewVerdict,
    fact,
)

__all__ = [
    # Core DSL
    "Workflow",
    "Agent",
    "Channel",
    "Phase",
    "Transition",
    "When",
    "Guard",
    # Guard types
    "AlwaysGuard",
    "NeverGuard",
    "ChannelHasGuard",
    "EmptyGuard",
    "VerdictGuard",
    "GitChangesGuard",
    "AndGuard",
    "OrGuard",
    "NotGuard",
    "FactExistsGuard",
    "FactMatchesGuard",
    # Fact types and utilities
    "Fact",
    "fact",
    "FactPattern",
    "FactRegistry",
    "Handle",
    # Built-in fact types
    "PlanDoc",
    "CodeArtifact",
    "ReviewVerdict",
    "ApprovalRequest",
    "ApprovalGrant",
]
