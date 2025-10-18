"""Codex adapter using local CLI authentication."""

from __future__ import annotations

import datetime
import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional

from ..models import AssistantRequest, AssistantResponse, CanonicalEventType
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
        self._reasoning_counter = 0  # Track reasoning step numbers

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

            # Read stdout with timeout protection using background thread
            # This prevents hanging if the CLI stalls without closing the pipe
            line_queue: queue.Queue = queue.Queue()

            def reader_thread():
                """Background thread that reads stdout line-by-line."""
                try:
                    if process.stdout:
                        for line in process.stdout:
                            line_queue.put(("line", line))
                except Exception as e:
                    line_queue.put(("error", str(e)))
                finally:
                    line_queue.put(("done", None))

            # Start reader thread as daemon so it doesn't block shutdown
            thread = threading.Thread(target=reader_thread, daemon=True)
            thread.start()

            # Main loop: poll queue with timeout protection
            start_time = time.time()
            while True:
                # Check if we've exceeded the overall timeout
                elapsed = time.time() - start_time
                if elapsed > self.timeout:
                    process.kill()
                    process.wait()
                    raise CodexError(f"Codex CLI timeout after {self.timeout} seconds")

                # Poll queue with 1-second timeout to check for new lines
                try:
                    msg_type, data = line_queue.get(timeout=1.0)
                except queue.Empty:
                    # No new data, check if process is still alive
                    if process.poll() is not None:
                        # Process finished, wait for thread to complete
                        thread.join(timeout=1.0)
                        break
                    continue  # Keep polling

                if msg_type == "done":
                    break
                elif msg_type == "error":
                    # Reader thread encountered an error
                    metadata["reader_error"] = data
                    break
                elif msg_type == "line":
                    line = data.strip()
                    if not line:
                        continue

                    # Try to parse JSON and emit event
                    try:
                        event_data = json.loads(line)
                        metadata["stream_events"] += 1

                        raw_event_type = event_data.get("type", "unknown")
                        metadata["event_types"].append(raw_event_type)

                        # Map to canonical event type and enrich
                        canonical_type, enriched_fields = self._normalize_event(event_data)

                        # Emit enriched StreamEvent if callback provided
                        if on_event:
                            stream_event: StreamEvent = {
                                "event_type": canonical_type,
                                "payload": event_data,
                                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                            }
                            # Add enriched fields
                            stream_event.update(enriched_fields)
                            on_event(stream_event)

                        # Extract assistant message from item.completed
                        if raw_event_type == "item.completed":
                            item = event_data.get("item", {})
                            if item.get("type") == "agent_message":
                                text = item.get("text")
                                if text:
                                    assistant_message = text

                        # Extract usage/token metadata from turn.completed
                        if raw_event_type == "turn.completed" and "usage" in event_data:
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
                                "event_type": CanonicalEventType.PARSE_ERROR.value,
                                "payload": {
                                    "error": str(exc),
                                    "raw_line": line[:200],  # Truncate for safety
                                },
                                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                            }
                            on_event(error_event)

            # Read stderr for diagnostics (non-blocking since process finished)
            stderr_output = []
            if process.stderr:
                stderr_output = process.stderr.readlines()
                if stderr_output:
                    metadata["stderr"] = "".join(stderr_output).strip()

            # Wait for process to complete (should be immediate since loop exited)
            returncode = process.poll()
            if returncode is None:
                # Process still running somehow, wait with short timeout
                try:
                    returncode = process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait()

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

    def _normalize_event(self, event_data: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """
        Normalize Codex raw event to canonical type with enriched fields.

        Args:
            event_data: Raw event from Codex JSONL stream

        Returns:
            Tuple of (canonical_event_type, enriched_fields_dict)
        """
        raw_type = event_data.get("type", "unknown")
        enriched = {}

        # Map item.completed to canonical types based on item.type
        if raw_type == "item.completed":
            item = event_data.get("item", {})
            item_type = item.get("type")
            text = item.get("text", "")

            if item_type == "agent_message":
                enriched["text_snippet"] = text
                return CanonicalEventType.ASSISTANT_MESSAGE.value, enriched

            elif item_type == "reasoning":
                enriched["text_snippet"] = text
                enriched["reasoning_step"] = self._reasoning_counter
                self._reasoning_counter += 1
                return CanonicalEventType.REASONING.value, enriched

            elif item_type == "tool_use":
                enriched["tool_info"] = {
                    "tool_name": item.get("name", "unknown"),
                    "status": item.get("status", "unknown"),
                    "output_preview": text[:100] if text else "",
                }
                return CanonicalEventType.TOOL_USE.value, enriched

            else:
                # Unknown item type
                return CanonicalEventType.UNKNOWN.value, enriched

        # Map thread.started
        elif raw_type == "thread.started":
            return CanonicalEventType.THREAD_STARTED.value, enriched

        # Map turn.completed
        elif raw_type == "turn.completed":
            usage = event_data.get("usage", {})
            if usage:
                enriched["usage"] = usage
            return CanonicalEventType.TURN_COMPLETE.value, enriched

        # Unknown raw type
        else:
            return CanonicalEventType.UNKNOWN.value, enriched

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
