"""Codex adapter using local CLI authentication."""

from __future__ import annotations

import subprocess
from typing import Any, Dict

from ..models import AssistantRequest, AssistantResponse
from .base import AssistantAdapter, register_adapter


class CodexError(Exception):
    """Exception raised when Codex CLI invocation fails."""

    pass


@register_adapter("codex")
class CodexAdapter(AssistantAdapter):
    """
    Adapter for Codex using local CLI authentication.

    Assumes the 'codex' CLI is installed and authenticated on the host machine.
    Invokes codex via subprocess and parses the response.

    Configuration:
        model: str - Model identifier (e.g., "gpt-4")
        timeout: int - CLI invocation timeout in seconds (default: 300)
        cli_path: str - Path to CLI executable (default: "codex")
    """

    name = "codex"
    role = "planner/reviewer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "gpt-4")
        self.timeout = kwargs.get("timeout", 300)
        self.cli_path = kwargs.get("cli_path", "codex")

    def generate(self, request: AssistantRequest) -> AssistantResponse:
        """
        Generate a response by invoking the Codex CLI.

        Uses a temporary file to pass the prompt to avoid shell escaping issues.
        Parses JSON output from the CLI.

        Raises:
            CodexError: If the CLI invocation fails or returns invalid data
        """
        try:
            # Invoke Codex CLI
            # Actual format: codex exec --model <model> <prompt>
            # Prompt is passed as positional argument
            result = subprocess.run(
                [
                    self.cli_path,
                    "exec",  # Non-interactive mode
                    "--model",
                    self.model,
                    request.prompt,  # Positional argument
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,  # Don't raise on non-zero exit
            )

            # Check for errors
            if result.returncode != 0:
                error_msg = result.stderr.strip() or "Unknown error"
                raise CodexError(
                    f"Codex CLI failed with exit code {result.returncode}: {error_msg}"
                )

            # Codex exec returns plain text, wrap in expected format
            # Since Codex doesn't output JSON, we create a normalized response
            content = result.stdout.strip()
            if not content:
                raise CodexError("Codex returned empty response")

            # Create normalized response (Codex doesn't provide structured output)
            response_data = {
                "content": content,
                "concluded": False,  # Default
                "metadata": {"raw_output": True},
            }

            return self._normalize_response(response_data)

        except subprocess.TimeoutExpired as exc:
            raise CodexError(f"Codex CLI timeout after {self.timeout} seconds") from exc
        except FileNotFoundError as exc:
            raise CodexError(
                f"Codex CLI not found at '{self.cli_path}'. "
                "Ensure it is installed and in PATH."
            ) from exc
        except Exception as exc:
            # Catch-all for unexpected errors
            if isinstance(exc, CodexError):
                raise
            raise CodexError(f"Unexpected error invoking Codex: {exc}") from exc

    def _normalize_response(self, data: Dict[str, Any]) -> AssistantResponse:
        """
        Normalize Codex CLI response to AssistantResponse format.

        Expected JSON structure:
        {
            "content": "...",
            "concluded": false,
            "metadata": { ... }
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
            raise CodexError(
                f"No content field found in Codex response. Available keys: {list(data.keys())}"
            )

        # Extract concluded flag (defaults to False)
        concluded = data.get("concluded", False)

        # Pass through metadata
        metadata = data.get("metadata", {})
        metadata["adapter"] = self.name
        metadata["model"] = self.model
        metadata["raw_response_keys"] = list(data.keys())

        return AssistantResponse(content=str(content), concluded=bool(concluded), metadata=metadata)
