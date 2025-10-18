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
        """Stream method for echo adapter (emits single event)."""
        content = (
            f"[ECHO ADAPTER]\n"
            f"Role: {request.role}\n"
            f"Prompt:\n{request.prompt}\n"
            f"Context keys: {', '.join(request.context.keys()) or 'none'}"
        )

        # Emit a single echo event if callback provided (Sprint 7: canonical type)
        if on_event:
            event: StreamEvent = {
                "event_type": CanonicalEventType.SYSTEM_NOTICE.value,
                "payload": {
                    "role": request.role,
                    "prompt_length": len(request.prompt),
                    "context_keys": list(request.context.keys()),
                    "adapter": "echo",
                },
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                "text_snippet": content,  # Enriched field
            }
            on_event(event)

        return AssistantResponse(content=content, metadata={"adapter": self.name})
