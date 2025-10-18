"""
Duet Workflow DSL (Sprint 9).

A Python DSL for defining orchestration workflows programmatically.
Replaces the legacy .duet/prompts/*.md template system with a type-safe,
composable workflow definition language.

Example:
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
    GitChangesGuard,
    NeverGuard,
    NotGuard,
    OrGuard,
    VerdictGuard,
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
]
