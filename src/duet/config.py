"""Configuration models and loader for the Duet orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, validator

from .models import StreamMode


class AssistantConfig(BaseModel):
    """Adapter configuration for a single assistant provider."""

    provider: str = Field(..., description="Human readable provider name, e.g., codex or claude")
    model: str = Field(..., description="Model identifier to request from the provider")
    api_key_env: Optional[str] = Field(
        None,
        description=(
            "Optional environment variable that provides credentials when CLI-based auth is absent"
        ),
    )
    timeout: Optional[int] = Field(
        None,
        ge=1,
        description="CLI invocation timeout in seconds (adapter-specific defaults apply if not set)",
    )
    cli_path: Optional[str] = Field(
        None, description="Custom path to CLI executable (defaults to adapter name in PATH)"
    )


class WorkflowConfig(BaseModel):
    """General settings that govern orchestration runs."""

    max_iterations: int = Field(5, ge=1, description="Maximum plan→implement→review loops per run")
    require_human_approval: bool = Field(
        True, description="Pause after review and wait for human approval before continuing"
    )
    auto_merge_on_approval: bool = Field(
        False, description="Automatically merge when review passes and guardrails allow"
    )

    # ──── Guardrail Settings ────
    max_consecutive_replans: int = Field(
        3,
        ge=1,
        description="Maximum consecutive PLAN phases before requiring human intervention",
    )
    max_phase_runtime_seconds: Optional[int] = Field(
        None,
        ge=1,
        description="Maximum runtime per phase in seconds (None = no limit)",
    )
    require_git_changes: bool = Field(
        True,
        description="Fail IMPLEMENT phase if no repository changes detected",
    )
    use_feature_branches: bool = Field(
        True,
        description="Create and switch to feature branch (<run-id>) for each run",
    )
    restore_branch_on_complete: bool = Field(
        True,
        description="Restore original branch when run completes or blocks",
    )


class LoggingConfig(BaseModel):
    """Logging and observability settings."""

    enable_jsonl: bool = Field(
        False, description="Enable JSONL structured logging to file"
    )
    jsonl_dir: Path = Field(
        Path("./logs"), description="Directory for JSONL log files"
    )
    quiet: bool = Field(
        False, description="Disable streaming console output during runs"
    )
    stream_mode: StreamMode = Field(
        StreamMode.DETAILED,
        description="Streaming display mode: detailed | compact | off",
    )

    @validator("jsonl_dir")
    def _expand_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()


class StorageConfig(BaseModel):
    """Persistence and artifact storage settings."""

    workspace_root: Path = Field(
        ..., description="Directory containing the target repository to automate"
    )
    run_artifact_dir: Path = Field(
        Path("./runs"), description="Directory where orchestration artifacts are stored"
    )

    @validator("workspace_root", "run_artifact_dir")
    def _expand_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()


class DuetConfig(BaseModel):
    """Top-level configuration that ties everything together."""

    codex: AssistantConfig
    claude: AssistantConfig
    workflow: WorkflowConfig = WorkflowConfig()
    storage: StorageConfig
    logging: LoggingConfig = LoggingConfig()

    @classmethod
    def load(cls, file_path: Path | str) -> "DuetConfig":
        """Load configuration from a YAML file."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        return cls.parse_obj(payload)


def find_config(explicit_path: Optional[Path] = None) -> DuetConfig:
    """Locate and load configuration using precedence rules."""
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(explicit_path)
    candidates.extend(
        Path(p).expanduser()
        for p in (
            "./.duet/duet.yaml",  # Primary location (duet init creates here)
            "./duet.yaml",
            "./duet.yml",
            "./config/duet.yaml",
            "./config/duet.yml",
        )
    )

    for candidate in candidates:
        if candidate.exists():
            return DuetConfig.load(candidate)

    raise FileNotFoundError(
        "Unable to locate duet configuration file. "
        "Provide --config Path or create config/duet.yaml."
    )
