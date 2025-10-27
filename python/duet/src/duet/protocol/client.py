"""Async control-plane client for the Duet NDJSON protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
from itertools import count
from typing import Any, Dict, Tuple

PROTOCOL_VERSION = "1.0.0"


class ProtocolError(RuntimeError):
    """Raised when the runtime reports a protocol-level error."""


class ControlClient:
    """Minimal asyncio client speaking the Duet NDJSON control protocol."""

    def __init__(self, runtime_cmd: Tuple[str, ...]) -> None:
        self._runtime_cmd = runtime_cmd
        self._process: asyncio.subprocess.Process | None = None
        self._counter = count(1)

    async def connect(self) -> None:
        if self._process is not None:
            return

        self._process = await asyncio.create_subprocess_exec(
            *self._runtime_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

        await self._handshake()

    async def close(self) -> None:
        if self._process is None:
            return

        if self._process.stdin:
            self._process.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            self._process.terminate()
        await self._process.wait()
        self._process = None

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

    async def _handshake(self) -> None:
        await self._send(
            "handshake",
            {
                "client": "duet-cli",
                "protocol_version": PROTOCOL_VERSION,
            },
        )

    async def _send(self, command: str, params: Dict[str, Any]) -> Any:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("ControlClient is not connected")

        request_id = next(self._counter)
        envelope = {
            "id": request_id,
            "command": command,
            "params": params,
        }

        data = json.dumps(envelope) + "\n"
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

        line = await self._process.stdout.readline()
        if not line:
            raise RuntimeError("duetd closed the connection")

        response = json.loads(line.decode("utf-8"))
        if "error" in response:
            error = response["error"]
            message = error.get("message", "unknown error")
            raise ProtocolError(message)

        return response.get("result")
