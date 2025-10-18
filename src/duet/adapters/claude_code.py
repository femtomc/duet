"""Claude Code adapter using local CLI authentication."""

from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any, Callable, Dict, Optional

from ..models import AssistantRequest, AssistantResponse
from .base import AssistantAdapter, StreamEvent, register_adapter


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
        timeout: int - CLI invocation timeout in seconds (default: 600)
        workspace_root: str - Path to workspace for code operations
        cli_path: str - Path to CLI executable (default: "claude")
    """

    name = "claude-code"
    role = "implementer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "claude-sonnet-4")
        self.timeout = kwargs.get("timeout", 600)  # Longer default for code operations
        self.workspace_root = kwargs.get("workspace_root", ".")
        self.cli_path = kwargs.get("cli_path", "claude")

    def stream(
        self,
        request: AssistantRequest,
        on_event: Optional[Callable[[StreamEvent], None]] = None,
    ) -> AssistantResponse:
        """
        Generate a response by invoking the Claude Code CLI with streaming support.

        Claude Code outputs JSON (potentially JSONL in the future). We parse output
        line-by-line and:
        - Emit StreamEvent via on_event callback for each line
        - Extract final response content
        - Collect metadata (usage, files modified, commits)

        Args:
            request: The assistant request to process
            on_event: Optional callback invoked for each streaming event

        Returns:
            Final AssistantResponse after stream completes

        Raises:
            ClaudeCodeError: If the CLI invocation fails or returns invalid data
        """
        try:
            # Invoke Claude CLI with Popen for streaming
            # Format: claude --print --output-format json --model <model> <prompt>
            process = subprocess.Popen(
                [
                    self.cli_path,
                    "--print",  # Non-interactive mode
                    "--output-format",
                    "json",
                    "--model",
                    self.model,
                    request.prompt,  # Positional argument
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.workspace_root,  # Workspace context
            )

            # Track metadata across stream
            final_response_data: Optional[Dict[str, Any]] = None
            metadata: Dict[str, Any] = {
                "stream_events": 0,
                "parse_errors": 0,
            }
            accumulated_lines: list[str] = []

            # Read stdout line-by-line as it streams
            if process.stdout:
                for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue

                    accumulated_lines.append(line)

                    # Try to parse JSON from this line
                    try:
                        event_data = json.loads(line)
                        metadata["stream_events"] += 1

                        # Emit StreamEvent if callback provided
                        if on_event:
                            stream_event: StreamEvent = {
                                "event_type": event_data.get("type", "output"),
                                "payload": event_data,
                                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                            }
                            on_event(stream_event)

                        # Store this as potential final response
                        # (Claude Code typically sends a single JSON object)
                        final_response_data = event_data

                    except json.JSONDecodeError:
                        # Line might be partial JSON or non-JSON output
                        # Try to parse accumulated lines as single JSON object
                        try:
                            combined = "".join(accumulated_lines)
                            event_data = json.loads(combined)
                            metadata["stream_events"] += 1

                            if on_event:
                                stream_event: StreamEvent = {
                                    "event_type": event_data.get("type", "output"),
                                    "payload": event_data,
                                    "timestamp": datetime.datetime.now(datetime.timezone.utc),
                                }
                                on_event(stream_event)

                            final_response_data = event_data
                            accumulated_lines = []  # Reset after successful parse

                        except json.JSONDecodeError as exc:
                            # Still can't parse - might get more lines
                            # Emit parse_error only if we've accumulated many lines
                            if len(accumulated_lines) > 10:
                                metadata["parse_errors"] += 1

                                if on_event:
                                    error_event: StreamEvent = {
                                        "event_type": "parse_error",
                                        "payload": {
                                            "error": str(exc),
                                            "accumulated_lines": len(accumulated_lines),
                                            "sample": combined[:200],
                                        },
                                        "timestamp": datetime.datetime.now(datetime.timezone.utc),
                                    }
                                    on_event(error_event)

                                accumulated_lines = []  # Reset to prevent memory issues

            # Try final parse if we have accumulated lines
            if accumulated_lines and not final_response_data:
                combined = "".join(accumulated_lines)
                try:
                    final_response_data = json.loads(combined)
                    metadata["stream_events"] += 1
                except json.JSONDecodeError as exc:
                    metadata["parse_errors"] += 1
                    if on_event:
                        error_event: StreamEvent = {
                            "event_type": "parse_error",
                            "payload": {
                                "error": str(exc),
                                "raw_output": combined[:500],
                            },
                            "timestamp": datetime.datetime.now(datetime.timezone.utc),
                        }
                        on_event(error_event)

            # Read stderr for diagnostics
            stderr_output: list[str] = []
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
                raise ClaudeCodeError(f"Claude Code CLI timeout after {self.timeout} seconds")

            # Check for errors
            if returncode != 0:
                error_msg = metadata.get("stderr", "Unknown error")
                raise ClaudeCodeError(
                    f"Claude Code CLI failed with exit code {returncode}: {error_msg}"
                )

            # Validate we got a response
            if not final_response_data:
                raise ClaudeCodeError(
                    f"No valid JSON response from Claude Code. "
                    f"Stream events: {metadata['stream_events']}, "
                    f"Parse errors: {metadata['parse_errors']}"
                )

            # Add stream metadata to response
            if "metadata" not in final_response_data:
                final_response_data["metadata"] = {}
            final_response_data["metadata"].update(metadata)

            # Normalize response
            return self._normalize_response(final_response_data)

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

        Actual JSON structure from claude --print --output-format json:
        {
            "type": "result",
            "subtype": "success",
            "result": "The actual response text...",
            "session_id": "...",
            "usage": {...},
            ...
        }

        Falls back to extracting content from various common fields.
        """
        # Primary extraction from Claude Code's actual output format
        content = data.get("result")  # Claude Code uses "result" field

        # Fallback extraction for common response formats
        if not content:
            content = (
                data.get("content")
                or data.get("text")
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
