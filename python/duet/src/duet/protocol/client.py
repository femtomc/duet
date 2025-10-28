"""Async control-plane client for the Duet NDJSON protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
from itertools import count
from typing import Any, Dict, Optional, Tuple

PROTOCOL_VERSION = "1.0.0"


class ProtocolError(RuntimeError):
    """Raised when the runtime reports a protocol-level error."""

    def __init__(self, message: str, *, code: str | None = None, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.details = details


class ControlClient:
    """Minimal asyncio client speaking the Duet NDJSON control protocol."""

    def __init__(
        self,
        runtime_cmd: Optional[Tuple[str, ...]] = None,
        runtime_addr: Optional[Tuple[str, int]] = None,
    ) -> None:
        if runtime_cmd is None and runtime_addr is None:
            raise ValueError("either runtime_cmd or runtime_addr must be provided")
        self._runtime_cmd = runtime_cmd
        self._runtime_addr = runtime_addr
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._counter = count(1)

    async def connect(self) -> None:
        if self._reader is not None:
            return

        if self._runtime_addr is not None:
            reader, writer = await asyncio.open_connection(*self._runtime_addr)
            self._reader = reader
            self._writer = writer
        else:
            assert self._runtime_cmd is not None
            self._process = await asyncio.create_subprocess_exec(
                *self._runtime_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            self._reader = self._process.stdout
            self._writer = self._process.stdin

        await self._handshake()

    async def close(self) -> None:
        if self._runtime_addr is not None:
            if self._writer is not None:
                self._writer.close()
                await self._writer.wait_closed()
            self._reader = None
            self._writer = None
            return

        if self._process is None:
            return

        if self._process.stdin:
            self._process.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            self._process.terminate()
        await self._process.wait()
        self._process = None
        self._reader = None
        self._writer = None

    async def status(self) -> Dict[str, Any]:
        response = await self._send("status", {})
        assert isinstance(response, dict)
        return response

    async def send_message(self, actor: str, facet: str, payload: str) -> Dict[str, Any]:
        response = await self._send(
            "send_message",
            {
                "target": {
                    "actor": actor,
                    "facet": facet,
                },
                "payload": payload,
            },
        )
        assert isinstance(response, dict)
        return response

    async def call(self, command: str, params: Dict[str, Any]) -> Any:
        return await self._send(command, params)

    async def invoke_capability(self, capability: str, payload: str) -> Any:
        response = await self._send(
            "invoke_capability",
            {"capability": capability, "payload": payload},
        )
        if isinstance(response, dict):
            return response.get("result")
        return response

    async def _handshake(self) -> None:
        await self._send(
            "handshake",
            {
                "client": "duet-cli",
                "protocol_version": PROTOCOL_VERSION,
            },
        )

    async def _send(self, command: str, params: Dict[str, Any]) -> Any:
        if self._writer is None or self._reader is None:
            raise RuntimeError("ControlClient is not connected")

        request_id = next(self._counter)
        envelope = {
            "id": request_id,
            "command": command,
            "params": params,
        }

        data = (json.dumps(envelope) + "\n").encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()

        line = await self._reader.readline()
        if not line:
            raise RuntimeError("codebased closed the connection")

        response = json.loads(line.decode("utf-8"))
        if "error" in response:
            error = response["error"]
            message = error.get("message", "unknown error")
            code = error.get("code")
            details = error.get("details")
            raise ProtocolError(message, code=code, details=details)

        return response.get("result")
