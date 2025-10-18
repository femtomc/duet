"""Codex adapter using local CLI authentication."""

from __future__ import annotations

import json
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
        Generate a response by invoking the Codex CLI with JSON streaming.

        Codex with --json outputs JSONL (one JSON object per line) representing
        events during execution. We parse line-by-line and extract:
        - Final assistant message (content)
        - Metadata (tokens, tool usage, session info)
        - Stream events for debugging

        Raises:
            CodexError: If the CLI invocation fails or returns invalid data
        """
        try:
            # Invoke Codex CLI with JSON output
            # Format: codex exec --json --model <model> <prompt>
            # Note: exec mode doesn't support --ask-for-approval, --sandbox, etc.
            result = subprocess.run(
                [
                    self.cli_path,
                    "exec",  # Non-interactive mode
                    "--json",  # JSONL streaming output
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

            # Parse JSONL output (one JSON object per line)
            events = self._parse_jsonl_stream(result.stdout)

            if not events:
                raise CodexError("Codex returned no events in JSON stream")

            # Extract final assistant message and metadata
            assistant_message = None
            metadata = {
                "stream_events": len(events),
                "event_types": [],
            }

            for event in events:
                event_type = event.get("type")
                metadata["event_types"].append(event_type)

                # Extract assistant message from item.completed events with type=agent_message
                if event_type == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        text = item.get("text")
                        if text:
                            assistant_message = text

                # Extract usage/token metadata from turn.completed
                if event_type == "turn.completed" and "usage" in event:
                    usage = event["usage"]
                    metadata["tokens"] = usage
                    metadata["input_tokens"] = usage.get("input_tokens")
                    metadata["output_tokens"] = usage.get("output_tokens")
                    metadata["cached_input_tokens"] = usage.get("cached_input_tokens")

                # Preserve thread/session info
                if "thread_id" in event:
                    metadata["thread_id"] = event["thread_id"]

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

    def _parse_jsonl_stream(self, stdout: str) -> list[Dict[str, Any]]:
        """
        Parse JSONL stream from Codex --json output.

        Each line is a JSON object representing an event.
        Handles partial/invalid lines gracefully.

        Returns:
            List of parsed event dictionaries
        """
        events = []
        lines = stdout.strip().split("\n")

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue  # Skip empty lines

            try:
                event = json.loads(line)
                events.append(event)
            except json.JSONDecodeError as exc:
                # Log but don't fail on invalid lines (may be partial output)
                # Store as error event for debugging
                events.append(
                    {
                        "type": "parse_error",
                        "line_num": line_num,
                        "error": str(exc),
                        "raw_line": line[:100],  # Truncate for safety
                    }
                )

        return events

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
