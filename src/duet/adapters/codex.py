"""Codex adapter using local CLI authentication."""

from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any, Callable, Dict, Optional

from ..models import AssistantRequest, AssistantResponse
from .base import AssistantAdapter, StreamEvent, register_adapter


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

    def stream(
        self,
        request: AssistantRequest,
        on_event: Optional[Callable[[StreamEvent], None]] = None,
    ) -> AssistantResponse:
        """
        Generate a response by invoking the Codex CLI with streaming JSONL output.

        Codex with --json outputs JSONL (one JSON object per line) representing
        events during execution. We parse line-by-line and:
        - Emit StreamEvent via on_event callback for each line
        - Extract final assistant message (content)
        - Collect metadata (tokens, tool usage, session info)

        Args:
            request: The assistant request to process
            on_event: Optional callback invoked for each streaming event

        Returns:
            Final AssistantResponse after stream completes

        Raises:
            CodexError: If the CLI invocation fails or returns invalid data
        """
        try:
            # Invoke Codex CLI with Popen for streaming
            # Format: codex exec --json --model <model> <prompt>
            process = subprocess.Popen(
                [
                    self.cli_path,
                    "exec",  # Non-interactive mode
                    "--json",  # JSONL streaming output
                    "--model",
                    self.model,
                    request.prompt,  # Positional argument
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Track metadata across stream
            assistant_message = None
            metadata: Dict[str, Any] = {
                "stream_events": 0,
                "event_types": [],
                "parse_errors": 0,
            }
            stderr_output = []

            # Read stdout line-by-line as it streams
            if process.stdout:
                for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue

                    # Try to parse JSON and emit event
                    try:
                        event_data = json.loads(line)
                        metadata["stream_events"] += 1

                        event_type = event_data.get("type", "unknown")
                        metadata["event_types"].append(event_type)

                        # Emit StreamEvent if callback provided
                        if on_event:
                            stream_event: StreamEvent = {
                                "event_type": event_type,
                                "payload": event_data,
                                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                            }
                            on_event(stream_event)

                        # Extract assistant message from item.completed
                        if event_type == "item.completed":
                            item = event_data.get("item", {})
                            if item.get("type") == "agent_message":
                                text = item.get("text")
                                if text:
                                    assistant_message = text

                        # Extract usage/token metadata from turn.completed
                        if event_type == "turn.completed" and "usage" in event_data:
                            usage = event_data["usage"]
                            metadata["tokens"] = usage
                            metadata["input_tokens"] = usage.get("input_tokens")
                            metadata["output_tokens"] = usage.get("output_tokens")
                            metadata["cached_input_tokens"] = usage.get("cached_input_tokens")

                        # Preserve thread/session info
                        if "thread_id" in event_data:
                            metadata["thread_id"] = event_data["thread_id"]

                    except json.JSONDecodeError as exc:
                        # Emit parse_error event
                        metadata["parse_errors"] += 1

                        if on_event:
                            error_event: StreamEvent = {
                                "event_type": "parse_error",
                                "payload": {
                                    "error": str(exc),
                                    "raw_line": line[:200],  # Truncate for safety
                                },
                                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                            }
                            on_event(error_event)

            # Read stderr for diagnostics
            if process.stderr:
                stderr_output = process.stderr.readlines()
                if stderr_output:
                    metadata["stderr"] = "".join(stderr_output).strip()

            # Wait for process to complete with timeout
            try:
                returncode = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise CodexError(f"Codex CLI timeout after {self.timeout} seconds")

            # Check for errors
            if returncode != 0:
                error_msg = metadata.get("stderr", "Unknown error")
                raise CodexError(f"Codex CLI failed with exit code {returncode}: {error_msg}")

            # Validate we got a response
            if metadata["stream_events"] == 0:
                raise CodexError("Codex returned no events in JSON stream")

            if not assistant_message:
                raise CodexError(
                    f"No assistant message found in Codex stream. "
                    f"Event types: {metadata['event_types']}"
                )

            # Create normalized response
            response_data = {
                "content": assistant_message,
                "concluded": False,  # Default
                "metadata": metadata,
            }

            return self._normalize_response(response_data)

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
