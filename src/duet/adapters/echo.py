"""Fallback adapter that echoes prompts for offline development."""

from __future__ import annotations

from ..models import AssistantRequest, AssistantResponse
from .base import AssistantAdapter, register_adapter


@register_adapter("echo")
class EchoAdapter(AssistantAdapter):
    """Simple adapter that mirrors the prompt back to the orchestrator."""

    name = "echo"
    role = "utility"

    def generate(self, request: AssistantRequest) -> AssistantResponse:
        content = (
            f"[ECHO ADAPTER]\n"
            f"Role: {request.role}\n"
            f"Prompt:\n{request.prompt}\n"
            f"Context keys: {', '.join(request.context.keys()) or 'none'}"
        )
        return AssistantResponse(content=content, metadata={"adapter": self.name})
