"""
Prompt builder framework for workflow-driven prompt construction (Sprint 10).

Provides programmable prompt generation from channel payloads, replacing
hardcoded _compose_*_request methods with extensible builder pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .models import AssistantRequest, Phase as ModelPhase


@dataclass
class PromptContext:
    """
    Context data passed to prompt builders.

    Contains all information available when building prompts:
    - Channel payloads (plan, code, verdict, feedback, etc.)
    - Run metadata (run_id, iteration, phase)
    - Git information (changes, commits, baseline)
    - Guardrail state (max iterations, consecutive replans)
    - Prior execution history

    Attributes:
        run_id: Current run identifier
        iteration: Current iteration number
        phase: Current phase name
        agent: Agent name executing this phase
        max_iterations: Maximum allowed iterations
        channel_payloads: Current channel values
        git_changes: Git change metadata (if available)
        consecutive_replans: Count of consecutive REVIEW→PLAN loops
        workspace_root: Workspace directory path
        metadata: Additional context (timestamps, etc.)
    """

    run_id: str
    iteration: int
    phase: str
    agent: str
    max_iterations: int
    channel_payloads: Dict[str, Any] = field(default_factory=dict)
    git_changes: Optional[Dict[str, Any]] = None
    consecutive_replans: int = 0
    workspace_root: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_channel(self, name: str, default: Any = None) -> Any:
        """Get value from a channel payload."""
        return self.channel_payloads.get(name, default)

    def has_channel(self, name: str) -> bool:
        """Check if channel has a value."""
        return name in self.channel_payloads and self.channel_payloads[name] is not None


class PromptBuilder:
    """
    Base class for prompt builders.

    Builders construct AssistantRequest from PromptContext, enabling
    programmable prompt generation based on channel payloads and metadata.
    """

    def build(self, context: PromptContext) -> AssistantRequest:
        """
        Build an AssistantRequest from context.

        Args:
            context: Execution context with channel payloads and metadata

        Returns:
            AssistantRequest ready for adapter invocation
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement build()")


class DefaultPlanningBuilder(PromptBuilder):
    """
    Default builder for PLAN phase.

    Constructs planning prompts from task channel and optional feedback.
    """

    def build(self, context: PromptContext) -> AssistantRequest:
        """Build planning prompt from task and feedback channels."""
        prompt_parts = [
            "Draft the implementation plan for the next increment.",
            "",
            f"Iteration: {context.iteration}/{context.max_iterations}",
        ]

        # Task specification (from channel)
        task = context.get_channel("task")
        if task:
            prompt_parts.extend([
                "",
                "──── Task ────",
                str(task),
            ])

        # Feedback from prior review (from channel)
        feedback = context.get_channel("feedback")
        if feedback and context.iteration > 1:
            prompt_parts.extend([
                "",
                "──── Prior Review Feedback ────",
                str(feedback),
                "",
                "Please revise the plan to address the review feedback above.",
            ])

        prompt = "\n".join(prompt_parts)

        return AssistantRequest(
            role="planner",
            prompt=prompt,
            context={
                "iteration": context.iteration,
                "run_id": context.run_id,
                "phase": context.phase,
                "max_iterations": context.max_iterations,
                "workspace_root": context.workspace_root,
                "channel_payloads": context.channel_payloads,
            },
        )


class DefaultImplementationBuilder(PromptBuilder):
    """
    Default builder for IMPLEMENT phase.

    Constructs implementation prompts from plan channel.
    """

    def build(self, context: PromptContext) -> AssistantRequest:
        """Build implementation prompt from plan channel."""
        prompt_parts = [
            "Apply the plan to the repository and provide a commit summary.",
            "",
            f"Iteration: {context.iteration}/{context.max_iterations}",
            f"Workspace: {context.workspace_root}",
        ]

        # Implementation plan (from channel)
        plan = context.get_channel("plan")
        if plan:
            prompt_parts.extend([
                "",
                "──── Implementation Plan ────",
                str(plan),
                "",
                "Follow the plan above to implement the changes.",
            ])

        prompt = "\n".join(prompt_parts)

        return AssistantRequest(
            role="implementer",
            prompt=prompt,
            context={
                "iteration": context.iteration,
                "run_id": context.run_id,
                "phase": context.phase,
                "max_iterations": context.max_iterations,
                "workspace_root": context.workspace_root,
                "channel_payloads": context.channel_payloads,
            },
        )


class DefaultReviewBuilder(PromptBuilder):
    """
    Default builder for REVIEW phase.

    Constructs review prompts from plan and code channels.
    """

    def build(self, context: PromptContext) -> AssistantRequest:
        """Build review prompt from plan and code channels."""
        prompt_parts = [
            "Review the latest changes and provide a structured verdict.",
            "",
            f"Iteration: {context.iteration}/{context.max_iterations}",
        ]

        # Implementation plan (from channel)
        plan = context.get_channel("plan")
        if plan:
            prompt_parts.extend([
                "",
                "──── Plan ────",
                str(plan),
            ])

        # Implementation artifacts (from channel)
        code = context.get_channel("code")
        if code:
            # Code might be git diff string or summary
            code_str = str(code)
            if len(code_str) > 1000:
                code_str = code_str[:1000] + "\n... (truncated)"

            prompt_parts.extend([
                "",
                "──── Implementation ────",
                code_str,
            ])

        prompt_parts.extend([
            "",
            "Assess whether the implementation meets the plan's requirements.",
            "",
            "Provide your verdict as one of:",
            "- APPROVE: Changes are acceptable, ready to proceed",
            "- CHANGES_REQUESTED: Revisions needed, will loop back to planning",
            "- BLOCKED: Critical issues requiring human intervention",
            "",
            "Include your verdict in the response metadata as 'verdict'.",
            "Set 'concluded' to True only if verdict is APPROVE.",
        ])

        prompt = "\n".join(prompt_parts)

        return AssistantRequest(
            role="reviewer",
            prompt=prompt,
            context={
                "iteration": context.iteration,
                "run_id": context.run_id,
                "phase": context.phase,
                "max_iterations": context.max_iterations,
                "workspace_root": context.workspace_root,
                "channel_payloads": context.channel_payloads,
            },
        )


# Builder registry (maps phase names to builders)
DEFAULT_BUILDERS: Dict[str, PromptBuilder] = {
    "plan": DefaultPlanningBuilder(),
    "implement": DefaultImplementationBuilder(),
    "review": DefaultReviewBuilder(),
}


def get_builder(phase_name: str) -> PromptBuilder:
    """
    Get prompt builder for a phase.

    Args:
        phase_name: Name of the phase

    Returns:
        PromptBuilder instance (uses default if no custom builder registered)
    """
    return DEFAULT_BUILDERS.get(phase_name, DefaultPlanningBuilder())
