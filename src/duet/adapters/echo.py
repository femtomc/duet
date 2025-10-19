"""Fallback adapter that echoes prompts for offline development."""

from __future__ import annotations

import datetime
from typing import Callable, Optional

from ..models import AssistantRequest, AssistantResponse, CanonicalEventType
from .base import AssistantAdapter, StreamEvent, register_adapter


@register_adapter("echo")
class EchoAdapter(AssistantAdapter):
    """Simple adapter that mirrors the prompt back to the orchestrator."""

    name = "echo"
    role = "utility"

    def stream(
        self,
        request: AssistantRequest,
        on_event: Optional[Callable[[StreamEvent], None]] = None,
    ) -> AssistantResponse:
        """
        Stream method for echo adapter (emits single event).

        For testing workflows, the echo adapter will:
        - Auto-approve when acting as a reviewer (sets verdict: approve)
        - Echo back the prompt for other roles

        This allows test workflows to progress through review gates without manual intervention.

        Detection strategy:
        1. Check for 'reviewer' in role string
        2. Check context for phase metadata indicating review phase
        """
        # Detect if this is a review role - check both role string and context
        role_lower = request.role.lower()
        is_reviewer = (
            "review" in role_lower
            or request.context.get("phase") == "review"  # Legacy
            or request.role in ("reviewer", "qa", "verifier")  # Common reviewer roles
        )

        if is_reviewer:
            content = (
                f"[ECHO ADAPTER - Auto-Approve]\n"
                f"Role: {request.role}\n"
                f"Phase: {request.context.get('phase', 'unknown')}\n"
                f"Verdict: approve\n"
                f"Feedback: Echo adapter auto-approved for testing\n"
                f"Context keys: {', '.join(request.context.keys()) or 'none'}"
            )
        else:
            content = (
                f"[ECHO ADAPTER]\n"
                f"Role: {request.role}\n"
                f"Phase: {request.context.get('phase', 'unknown')}\n"
                f"Prompt:\n{request.prompt}\n"
                f"Context keys: {', '.join(request.context.keys()) or 'none'}"
            )

        # Emit a single echo event if callback provided (Sprint 7: canonical type)
        if on_event:
            event: StreamEvent = {
                "event_type": CanonicalEventType.SYSTEM_NOTICE.value,
                "payload": {
                    "role": request.role,
                    "phase": request.context.get("phase"),
                    "prompt_length": len(request.prompt),
                    "context_keys": list(request.context.keys()),
                    "adapter": "echo",
                    "auto_approved": is_reviewer,
                },
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                "text_snippet": content,  # Enriched field
            }
            on_event(event)

        # Build metadata with verdict if this is a reviewer
        metadata = {"adapter": self.name}
        if is_reviewer:
            metadata["verdict"] = "approve"

        return AssistantResponse(content=content, metadata=metadata)
