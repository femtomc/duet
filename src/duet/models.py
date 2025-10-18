"""Domain models used across the orchestration runtime."""

from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class Phase(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"


class AssistantRequest(BaseModel):
    """Prompt details delivered to an assistant adapter."""

    role: str
    prompt: str
    context: Dict[str, Any] = Field(default_factory=dict)


class AssistantResponse(BaseModel):
    """Normalized response returned by an assistant adapter."""

    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    concluded: bool = False


class TransitionDecision(BaseModel):
    """Represents the orchestrator's decision after evaluating a response."""

    next_phase: Phase
    rationale: str
    requires_human: bool = False


class RunSnapshot(BaseModel):
    """Persisted representation of an orchestration run checkpoint."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    iteration: int = 0
    phase: Phase = Phase.PLAN
    notes: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
