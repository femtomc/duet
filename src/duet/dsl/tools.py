"""
Tool interface for Duet workflows (Sprint DSL-2).

Tools are deterministic functions that run before or after phases,
performing operations like git validation, approval checks, or custom logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Type


class ToolTiming(Enum):
    """When a tool should execute relative to its phase."""

    PRE_PHASE = "pre_phase"  # Before assistant is called
    POST_PHASE = "post_phase"  # After assistant responds
    CUSTOM = "custom"  # Custom timing (orchestrator decides)


@dataclass
class ToolContext:
    """
    Context provided to tools during execution.

    Provides access to:
    - Fact state (current values)
    - Workspace information (git, paths)
    - Run metadata (iteration, facet, etc.)
    - Policy information
    """

    run_id: str
    iteration: int
    phase_name: str  # Represents facet_id in new model
    fact_state: Dict[str, Any]  # fact alias -> fact object
    workspace_root: str
    git_available: bool = False
    baseline_commit: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_fact(self, name: str, default: Any = None) -> Any:
        """Get current value of a fact."""
        return self.fact_state.get(name, default)

    # Backward compatibility
    @property
    def channel_state(self) -> Dict[str, Any]:
        """Deprecated: Use fact_state instead."""
        return self.fact_state

    def get_channel(self, name: str, default: Any = None) -> Any:
        """Deprecated: Use get_fact() instead."""
        return self.get_fact(name, default)


@dataclass
class ToolResult:
    """
    Result from tool execution.

    Contains:
    - Context updates (enrich local facet context, e.g. for prompt building)
    - Fact updates (write to global dataspace as facts)
    - Metadata to merge into response
    - Optional notes/logs
    - Success/failure status

    Sprint DSL-5: Separated context vs fact updates to avoid conflation.
    """

    context_updates: Dict[str, Any] = field(default_factory=dict)
    fact_updates: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    notes: Optional[str] = None
    success: bool = True
    error: Optional[str] = None

    # Backward compatibility
    @property
    def channel_updates(self) -> Dict[str, Any]:
        """Deprecated: Use fact_updates instead."""
        return self.fact_updates

    @classmethod
    def ok(cls, context_updates: Optional[Dict[str, Any]] = None, fact_updates: Optional[Dict[str, Any]] = None) -> ToolResult:
        """
        Create a successful result with updates.

        Args:
            context_updates: Updates to local facet context (for prompt building)
            fact_updates: Writes to global dataspace as facts
        """
        return cls(
            context_updates=context_updates or {},
            fact_updates=fact_updates or {},
            success=True
        )

    @classmethod
    def fail(cls, error: str, notes: Optional[str] = None) -> ToolResult:
        """Create a failed result with error message."""
        return cls(success=False, error=error, notes=notes)


class Tool(Protocol):
    """
    Protocol for deterministic workflow tools.

    Tools perform operations like git validation, approval checks, or custom logic.
    They run at specified times (pre/post phase) and can read/write facts.

    Attributes:
        name: Unique tool identifier
        timing: When the tool executes (pre_phase, post_phase, custom)
        consumes: Fact types this tool reads
        publishes: Fact types this tool writes (via ToolResult.channel_updates)
    """

    name: str
    timing: ToolTiming
    consumes: List[Type]
    publishes: List[Type]

    def run(self, context: ToolContext) -> ToolResult:
        """
        Execute the tool logic.

        Args:
            context: Execution context with channel state and workspace info

        Returns:
            ToolResult with channel updates, metadata, and status
        """
        ...


@dataclass
class BaseTool:
    """
    Base implementation of Tool protocol.

    Subclasses should override run() method to implement tool logic.
    """

    name: str
    timing: ToolTiming = ToolTiming.PRE_PHASE
    consumes: List[Type] = field(default_factory=list)
    publishes: List[Type] = field(default_factory=list)

    def run(self, context: ToolContext) -> ToolResult:
        """
        Execute the tool logic (override in subclasses).

        Args:
            context: Execution context

        Returns:
            ToolResult with updates
        """
        raise NotImplementedError(f"{self.__class__.__name__}.run() must be implemented")


# ──────────────────────────────────────────────────────────────────────────────
# Built-in Tools (Stubs for now - implement in future sprints)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class GitChangeTool(BaseTool):
    """
    Tool that validates git changes were made.

    **Requirements:**
    - Requires git repository in workspace_root
    - Executes 'git status --porcelain' and 'git rev-parse HEAD'
    - Safe for large repos (only checks status, doesn't diff content)

    **Behavior:**
    - If require_changes=True: Fails if working tree is clean
    - If require_changes=False: Always succeeds, provides git info
    - If git_available=False: Skips validation gracefully

    **Context Enrichment:**
    Adds git_info to context for use in agent prompts:
    - has_changes: bool
    - status_output: str (git status output)
    - commit: str (current commit SHA)

    **Usage:**
    ```python
    implement = (
        Phase(name="implement", agent="dev")
        .read(plan)
        .call_agent("dev", writes=[code])
        .requires_git()  # Adds GitChangeTool(require_changes=True)
    )
    ```
    """

    name: str = "git_change_validator"
    timing: ToolTiming = ToolTiming.POST_PHASE
    require_changes: bool = True

    def run(self, context: ToolContext) -> ToolResult:
        """
        Validate that git changes occurred.

        Returns:
            ToolResult with git status in context, fails if no changes and required
        """
        if not context.git_available:
            # No git repo - skip validation
            return ToolResult.ok(
                context_updates={"git_info": {"available": False}},
                fact_updates={},
            )

        # Check git status via subprocess
        import subprocess

        try:
            # Check for unstaged changes
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=context.workspace_root,
                capture_output=True,
                text=True,
            )

            has_changes = bool(status_result.stdout.strip())

            # Get commit info if available
            commit_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=context.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            current_commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None

            git_info = {
                "has_changes": has_changes,
                "status_output": status_result.stdout,
                "commit": current_commit,
            }

            # Enforce if required
            if self.require_changes and not has_changes:
                return ToolResult.fail(
                    error="Git changes required but none detected",
                    notes="Phase requires repository modifications but working tree is clean",
                )

            return ToolResult.ok(
                context_updates={"git_info": git_info},
                fact_updates={},  # Can write facts if outputs declared
            )

        except Exception as exc:
            return ToolResult.fail(
                error=f"Git validation failed: {exc}",
                notes="Could not check git status",
            )


@dataclass
class ApprovalTool(BaseTool):
    """
    Tool that requires human approval before proceeding.

    Runs post-phase to check for manual approval.
    """

    name: str = "approval_check"
    timing: ToolTiming = ToolTiming.POST_PHASE
    approval_message: str = "Human approval required"

    def run(self, context: ToolContext) -> ToolResult:
        """
        Check for human approval.

        Returns:
            ToolResult indicating approval status
        """
        # Stub implementation - real logic would check approval state
        # For now, always succeed with no updates
        return ToolResult.ok()
