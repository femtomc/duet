"""Claude Code adapter using local CLI authentication."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..models import AssistantRequest, AssistantResponse
from .base import AssistantAdapter, register_adapter


class ClaudeCodeError(Exception):
    """Exception raised when Claude Code CLI invocation fails."""

    pass


@register_adapter("claude-code")
class ClaudeCodeAdapter(AssistantAdapter):
    """
    Adapter for Claude Code using local CLI authentication.

    Assumes the 'claude' CLI is installed and authenticated on the host machine.
    Invokes claude via subprocess and parses the response.

    Configuration:
        model: str - Model identifier (e.g., "claude-sonnet-4")
        temperature: float - Sampling temperature (default: 0.0)
        timeout: int - CLI invocation timeout in seconds (default: 600)
        workspace_root: str - Path to workspace for code operations
    """

    name = "claude-code"
    role = "implementer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "claude-sonnet-4")
        self.temperature = kwargs.get("temperature", 0.0)
        self.timeout = kwargs.get("timeout", 600)  # Longer default for code operations
        self.workspace_root = kwargs.get("workspace_root", ".")
        self.cli_path = kwargs.get("cli_path", "claude")  # Allow override for testing

    def generate(self, request: AssistantRequest) -> AssistantResponse:
        """
        Generate a response by invoking the Claude Code CLI.

        Uses a temporary file to pass the prompt to avoid shell escaping issues.
        Parses JSON output from the CLI.

        Raises:
            ClaudeCodeError: If the CLI invocation fails or returns invalid data
        """
        try:
            # Create temporary file for prompt
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
                tmp.write(request.prompt)
                prompt_file = Path(tmp.name)

            try:
                # Invoke Claude CLI
                # Expected format: claude --model <model> --temperature <temp> --prompt-file <file>
                cmd = [
                    self.cli_path,
                    "--model",
                    self.model,
                    "--temperature",
                    str(self.temperature),
                    "--prompt-file",
                    str(prompt_file),
                    "--output",
                    "json",
                ]

                # Add workspace context if available
                if self.workspace_root != ".":
                    cmd.extend(["--workspace", str(self.workspace_root)])

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,  # Don't raise on non-zero exit
                    cwd=self.workspace_root,  # Run in workspace directory
                )

                # Check for errors
                if result.returncode != 0:
                    error_msg = result.stderr.strip() or "Unknown error"
                    raise ClaudeCodeError(
                        f"Claude Code CLI failed with exit code {result.returncode}: {error_msg}"
                    )

                # Parse JSON response
                try:
                    response_data = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    raise ClaudeCodeError(
                        f"Failed to parse Claude Code JSON response: {exc}"
                    ) from exc

                # Normalize response
                return self._normalize_response(response_data)

            finally:
                # Clean up temporary file
                prompt_file.unlink(missing_ok=True)

        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError(
                f"Claude Code CLI timeout after {self.timeout} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise ClaudeCodeError(
                f"Claude Code CLI not found at '{self.cli_path}'. "
                "Ensure it is installed and in PATH."
            ) from exc
        except Exception as exc:
            # Catch-all for unexpected errors
            if isinstance(exc, ClaudeCodeError):
                raise
            raise ClaudeCodeError(f"Unexpected error invoking Claude Code: {exc}") from exc

    def _normalize_response(self, data: Dict[str, Any]) -> AssistantResponse:
        """
        Normalize Claude Code CLI response to AssistantResponse format.

        Expected JSON structure:
        {
            "content": "...",
            "concluded": false,
            "metadata": {
                "files_modified": [...],
                "commands_executed": [...],
                ...
            }
        }

        Falls back to extracting content from various common fields.
        """
        # Primary extraction
        content = data.get("content")

        # Fallback extraction for common response formats
        if not content:
            content = (
                data.get("text")
                or data.get("response")
                or data.get("output")
                or data.get("message")
            )

        if not content:
            raise ClaudeCodeError(
                f"No content field found in Claude Code response. "
                f"Available keys: {list(data.keys())}"
            )

        # Extract concluded flag (defaults to False)
        concluded = data.get("concluded", False)

        # Pass through metadata
        metadata = data.get("metadata", {})
        metadata["adapter"] = self.name
        metadata["model"] = self.model
        metadata["workspace_root"] = self.workspace_root
        metadata["raw_response_keys"] = list(data.keys())

        # Capture code-specific metadata if present
        if "files_modified" in data:
            metadata["files_modified"] = data["files_modified"]
        if "commands_executed" in data:
            metadata["commands_executed"] = data["commands_executed"]
        if "commit_sha" in data:
            metadata["commit_sha"] = data["commit_sha"]

        return AssistantResponse(content=str(content), concluded=bool(concluded), metadata=metadata)
