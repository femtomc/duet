"""
Tool interface for Duet workflows (Sprint DSL-2).

Tools are deterministic functions that run before or after phases,
performing operations like git validation, approval checks, or custom logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol

from .workflow import Channel


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
    - Channel state (current values)
    - Workspace information (git, paths)
    - Run metadata (iteration, phase, etc.)
    - Policy information
    """

    run_id: str
    iteration: int
    phase_name: str
    channel_state: Dict[str, Any]  # channel name -> current value
    workspace_root: str
    git_available: bool = False
    baseline_commit: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_channel(self, name: str, default: Any = None) -> Any:
        """Get current value of a channel."""
        return self.channel_state.get(name, default)


@dataclass
class ToolResult:
    """
    Result from tool execution.

    Contains:
    - Context updates (enrich local facet context, e.g. for prompt building)
    - Channel updates (write to global dataspace/channels)
    - Metadata to merge into response
    - Optional notes/logs
    - Success/failure status

    Sprint DSL-5: Separated context vs channel updates to avoid conflation.
    """

    context_updates: Dict[str, Any] = field(default_factory=dict)
    channel_updates: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    notes: Optional[str] = None
    success: bool = True
    error: Optional[str] = None

    @classmethod
    def ok(cls, context: Optional[Dict[str, Any]] = None, channels: Optional[Dict[str, Any]] = None) -> ToolResult:
        """Create a successful result with updates."""
        return cls(
            context_updates=context or {},
            channel_updates=channels or {},
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
    They run at specified times (pre/post phase) and can read/write channels.

    Attributes:
        name: Unique tool identifier
        timing: When the tool executes (pre_phase, post_phase, custom)
        consumes: Channels this tool reads from
        publishes: Channels this tool writes to (via ToolResult.channel_updates)
    """

    name: str
    timing: ToolTiming
    consumes: List[Channel]
    publishes: List[Channel]

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
    consumes: List[Channel] = field(default_factory=list)
    publishes: List[Channel] = field(default_factory=list)

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

    Runs post-phase to verify repository modifications.
    """

    name: str = "git_change_validator"
    timing: ToolTiming = ToolTiming.POST_PHASE
    require_changes: bool = True

    def run(self, context: ToolContext) -> ToolResult:
        """
        Validate that git changes occurred.

        Returns:
            ToolResult with git status in context (for prompt) and channels (for dataspace)
        """
        # Stub implementation - real logic would check git status
        # For now, always succeed with stub data
        git_info = {
            "has_changes": True,  # Stub
            "files_changed": 0,
            "commit": None,
        }
        return ToolResult.ok(
            context={"git_info": git_info},  # Local context for next steps
            channels={},  # No channel writes unless explicitly declared
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
