"""
Duet Workflow DSL (Sprint 9).

A Python DSL for defining orchestration workflows programmatically.
Replaces the legacy .duet/prompts/*.md template system with a type-safe,
composable workflow definition language.

Example:
    from duet.dsl import Workflow, Agent, Phase, Transition, When

    workflow = Workflow(
        agents=[
            Agent(name="planner", provider="codex", model="gpt-5-codex"),
            Agent(name="implementer", provider="claude", model="sonnet"),
            Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
        ],
        phases=[
            Phase(name="plan", agent="planner",
                  prompt="Draft implementation plan for the task"),
            Phase(name="implement", agent="implementer",
                  prompt="Execute the plan and make code changes"),
            Phase(name="review", agent="reviewer",
                  prompt="Review the implementation"),
            Phase(name="done", agent="reviewer",
                  prompt="Task complete", is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="plan", to_phase="implement"),
            Transition(from_phase="implement", to_phase="review"),
            Transition(from_phase="review", to_phase="done",
                       when=When.verdict("approve")),
            Transition(from_phase="review", to_phase="plan",
                       when=When.verdict("changes_requested")),
        ],
    )
"""

from .workflow import (
    Agent,
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
