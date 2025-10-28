"""Command-line interface for the Duet runtime."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from rich.console import Console
from rich.json import JSON

from .protocol.client import ControlClient, ProtocolError

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="duet",
        description="CLI for interacting with the Duet runtime over the NDJSON control protocol.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Runtime root directory (passed to codebased).",
    )
    parser.add_argument(
        "--codebased-bin",
        dest="codebased_bin",
        type=Path,
        help="Path to the codebased daemon binary (overrides auto-discovery).",
    )
    parser.add_argument(
        "--duetd-bin",
        dest="codebased_bin",
        type=Path,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show runtime status.")
    status_parser.add_argument("--branch", default=None)

    history = subparsers.add_parser("history", help="Show turn history for a branch.")
    history.add_argument("--branch", default="main")
    history.add_argument("--start", type=int, default=0)
    history.add_argument("--limit", type=int, default=20)

    send = subparsers.add_parser("send", help="Send a message to an actor/facet.")
    send.add_argument("--actor", required=True, help="Actor UUID.")
    send.add_argument("--facet", required=True, help="Facet UUID.")
    send.add_argument(
        "--payload",
        required=True,
        help="Preserves text payload (e.g. \"nil\" or \"(greeting \"\"hello\"\")\").",
    )

    register = subparsers.add_parser("register-entity", help="Register an entity.")
    register.add_argument("--actor", required=True, help="Actor UUID.")
    register.add_argument("--facet", required=True, help="Facet UUID.")
    register.add_argument("--entity-type", required=True)
    register.add_argument(
        "--config",
        default="nil",
        help="Preserves value describing the entity configuration (default: nil).",
    )

    list_entities = subparsers.add_parser("list-entities", help="List registered entities.")
    list_entities.add_argument("--actor", help="Filter by actor UUID.")

    list_capabilities = subparsers.add_parser("list-capabilities", help="List known capabilities.")
    list_capabilities.add_argument("--actor", help="Filter by actor UUID.")

    goto = subparsers.add_parser("goto", help="Jump to a specific turn.")
    goto.add_argument("turn_id", help="Turn ID to jump to.")
    goto.add_argument("--branch", default="main")

    back = subparsers.add_parser("back", help="Rewind by N turns.")
    back.add_argument("--count", type=int, default=1)
    back.add_argument("--branch", default="main")

    fork = subparsers.add_parser("fork", help="Fork a new branch.")
    fork.add_argument("--source", default="main")
    fork.add_argument("--new-branch", required=True)
    fork.add_argument("--from-turn")

    merge = subparsers.add_parser("merge", help="Merge source branch into target.")
    merge.add_argument("--source", required=True)
    merge.add_argument("--target", required=True)

    invoke = subparsers.add_parser(
        "invoke-capability",
        help="Invoke a capability by id with a preserves payload.",
    )
    invoke.add_argument("--capability", required=True, help="Capability UUID.")
    invoke.add_argument(
        "--payload",
        required=True,
        help="Preserves payload (e.g. '(workspace-read \"path\")').",
    )

    raw = subparsers.add_parser("raw", help="Send a raw command/params JSON payload.")
    raw.add_argument("rpc_command")
    raw.add_argument("params", nargs="?", default="{}")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        asyncio.run(run(args))
    except ProtocolError as exc:
        suffix = f" ({exc.code})" if getattr(exc, "code", None) else ""
        console.print(f"[red]Protocol error{suffix}:[/] {exc}")
        details = getattr(exc, "details", None)
        if details is not None:
            if isinstance(details, (dict, list)):
                console.print(JSON.from_data(details))
            else:
                console.print(f"[red]Details:[/] {details}")
        return 1
    except FileNotFoundError as exc:
        console.print(f"[red]Failed to launch codebased:[/] {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - safety net
        console.print(f"[red]Unexpected error:[/] {exc}")
        return 1
    return 0


async def run(args: argparse.Namespace) -> None:
    client = await _connect_client(args)
    try:
        if args.command == "status":
            params: Dict[str, Any] = {}
            if args.branch:
                params["branch"] = args.branch
            result = await client.call("status", params)
        elif args.command == "history":
            result = await client.call(
                "history",
                {"branch": args.branch, "start": args.start, "limit": args.limit},
            )
        elif args.command == "send":
            result = await client.send_message(args.actor, args.facet, args.payload)
        elif args.command == "register-entity":
            result = await client.call(
                "register_entity",
                {
                    "actor": args.actor,
                    "facet": args.facet,
                    "entity_type": args.entity_type,
                    "config": args.config,
                },
            )
        elif args.command == "list-entities":
            params = {"actor": args.actor} if args.actor else {}
            result = await client.call("list_entities", params)
        elif args.command == "list-capabilities":
            params = {"actor": args.actor} if args.actor else {}
            result = await client.call("list_capabilities", params)
        elif args.command == "goto":
            result = await client.call(
                "goto",
                {"branch": args.branch, "turn_id": args.turn_id},
            )
        elif args.command == "back":
            result = await client.call(
                "back", {"branch": args.branch, "count": args.count}
            )
        elif args.command == "fork":
            params = {"source": args.source, "new_branch": args.new_branch}
            if args.from_turn:
                params["from_turn"] = args.from_turn
            result = await client.call("fork", params)
        elif args.command == "merge":
            result = await client.call(
                "merge", {"source": args.source, "target": args.target}
            )
        elif args.command == "invoke-capability":
            result = await client.invoke_capability(args.capability, args.payload)
        elif args.command == "raw":
            try:
                params = json_loads(args.params)
            except ValueError as exc:
                raise ProtocolError(f"Invalid JSON params: {exc}") from exc
            if not isinstance(params, dict):
                raise ProtocolError("Raw command params must decode to a JSON object.")
            result = await client.call(args.rpc_command, params)
        else:  # pragma: no cover - argparse enforces choices
            raise ProtocolError(f"Unsupported command: {args.command}")

        _print_result(result)
    finally:
        await client.close()


def json_loads(payload: str) -> Any:
    import json

    return json.loads(payload)


async def _connect_client(args: argparse.Namespace) -> ControlClient:
    cmd = list(_codebased_command(args))
    if args.root:
        cmd.extend(["--root", str(args.root)])
    client = ControlClient(tuple(cmd))
    await client.connect()
    return client


def _codebased_command(args: argparse.Namespace) -> Tuple[str, ...]:
    if args.codebased_bin:
        return (str(args.codebased_bin), "--stdio")
    env_override = os.environ.get("CODEBASED_BIN") or os.environ.get("DUETD_BIN")
    if env_override:
        return (env_override, "--stdio")
    return discover_codebased_command()


def discover_codebased_command() -> Tuple[str, ...]:
    exe_name = "codebased.exe" if os.name == "nt" else "codebased"
    root = Path(__file__).resolve()

    for parent in root.parents:
        candidate = parent / "target" / "debug" / exe_name
        if candidate.exists():
            return (str(candidate), "--stdio")

        candidate_release = parent / "target" / "release" / exe_name
        if candidate_release.exists():
            return (str(candidate_release), "--stdio")

    return (exe_name, "--stdio")


def _print_result(result: Any) -> None:
    if isinstance(result, (dict, list)):
        console.print(JSON.from_data(result))
    else:
        console.print(result)
