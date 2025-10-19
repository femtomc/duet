"""
Duet Workflow DSL with Typed Facts.

A Python DSL for defining orchestration workflows with type-safe fact-based dataflow.
Uses Syndicate-inspired reactive execution with structured facts instead of string channels.

Example Workflow with Typed Facts:
    from dataclasses import dataclass
    from duet.dsl import (
        Workflow, Agent, Phase, Transition, When,
        Fact, fact, PlanDoc, CodeArtifact, ReviewVerdict
    )
    from duet.dsl.steps import ReadStep, WriteStep, AgentStep

    # Define custom fact type
    @fact
    @dataclass
    class TaskRequest(Fact):
        fact_id: str
        task_description: str
        priority: int = 1

    workflow = Workflow(
        agents=[
            Agent(name="planner", provider="codex", model="gpt-5-codex"),
            Agent(name="implementer", provider="claude", model="sonnet"),
            Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
        ],
        phases=[
            Phase(
                name="plan",
                agent="planner",
                steps=[
                    ReadStep(fact_type=TaskRequest, into="task"),
                    AgentStep(agent="planner", writes=[]),
                    WriteStep(
                        fact_type=PlanDoc,
                        values={
                            "task_id": "$task.fact_id",
                            "content": "$agent_response"
                        }
                    ),
                ],
                description="Draft implementation plan"
            ),
            Phase(
                name="implement",
                agent="implementer",
                steps=[
                    ReadStep(fact_type=PlanDoc, into="plan"),
                    AgentStep(agent="implementer", writes=[]),
                    WriteStep(
                        fact_type=CodeArtifact,
                        values={
                            "plan_id": "$plan.fact_id",
                            "summary": "$agent_response"
                        }
                    ),
                ],
                description="Execute plan and make changes"
            ),
            Phase(
                name="review",
                agent="reviewer",
                steps=[
                    ReadStep(fact_type=CodeArtifact, into="code"),
                    AgentStep(agent="reviewer", writes=[]),
                    WriteStep(
                        fact_type=ReviewVerdict,
                        values={
                            "code_id": "$code.fact_id",
                            "verdict": "$verdict",
                            "feedback": "$feedback"
                        }
                    ),
                ],
                description="Review the implementation"
            ),
            Phase(name="done", agent="reviewer", steps=[], is_terminal=True),
        ],
        transitions=[
            Transition(from_phase="plan", to_phase="implement"),
            Transition(from_phase="implement", to_phase="review"),
            Transition(
                from_phase="review",
                to_phase="done",
                when=When.fact_exists(ReviewVerdict, constraints={"verdict": "approve"})
            ),
            Transition(
                from_phase="review",
                to_phase="plan",
                when=When.fact_exists(ReviewVerdict, constraints={"verdict": "changes_requested"})
            ),
        ],
    )

Using Facts Directly:
    from duet.dsl import FactPattern
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
    FactExistsGuard,
    FactMatchesGuard,
    NeverGuard,
    NotGuard,
    OrGuard,
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
