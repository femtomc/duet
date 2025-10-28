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
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich.tree import Tree

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
        error_content = f"[bold]{exc}[/bold]"
        details = getattr(exc, "details", None)

        if details is not None:
            if isinstance(details, (dict, list)):
                import json
                details_str = json.dumps(details, indent=2)
                error_content += f"\n\n[dim]Details:[/dim]\n{details_str}"
            else:
                error_content += f"\n\n[dim]Details:[/dim] {details}"

        console.print(Panel(
            error_content,
            title=f"[bold red]Protocol Error{suffix}[/bold red]",
            border_style="red"
        ))
        return 1
    except FileNotFoundError as exc:
        console.print(Panel(
            f"[bold]{exc}[/bold]\n\n[dim]Make sure codebased is installed or use --codebased-bin to specify the path.[/dim]",
            title="[bold red]Failed to Launch[/bold red]",
            border_style="red"
        ))
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        return 130
    except Exception as exc:  # pragma: no cover - safety net
        import traceback
        tb = traceback.format_exc()
        console.print(Panel(
            f"[bold]{exc}[/bold]\n\n[dim]{tb}[/dim]",
            title="[bold red]Unexpected Error[/bold red]",
            border_style="red"
        ))
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

        _print_result(result, args.command)
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


def _print_result(result: Any, command: str | None = None) -> None:
    """Print command result with Rich formatting based on command type."""
    if command == "status":
        _print_status(result)
    elif command == "history":
        _print_history(result)
    elif command == "list-entities":
        _print_entities(result)
    elif command == "list-capabilities":
        _print_capabilities(result)
    elif command in ("send", "register-entity", "invoke-capability"):
        _print_operation_result(result, command)
    elif command in ("goto", "back", "fork", "merge"):
        _print_navigation_result(result, command)
    elif isinstance(result, (dict, list)):
        console.print(JSON.from_data(result))
    else:
        console.print(result)


def _print_status(result: Any) -> None:
    """Format status output with panels and tree view."""
    if not isinstance(result, dict):
        console.print(JSON.from_data(result))
        return

    # Main status panel
    status_tree = Tree("[bold cyan]Runtime Status[/bold cyan]")

    # Branch information
    if "branch" in result:
        branch_info = result["branch"]
        branch_node = status_tree.add(f"[bold]Branch:[/bold] {branch_info.get('name', 'N/A')}")
        if "head" in branch_info:
            branch_node.add(f"[dim]Head Turn:[/dim] {branch_info['head']}")
        if "turn_count" in branch_info:
            branch_node.add(f"[dim]Turn Count:[/dim] {branch_info['turn_count']}")

    # Actor information
    if "actors" in result:
        actors = result["actors"]
        actors_node = status_tree.add(f"[bold]Actors:[/bold] {len(actors)} active")
        for actor in actors[:5]:  # Show first 5
            actor_id = actor.get("id", "unknown")[:8]
            actors_node.add(f"[dim]{actor_id}...[/dim]")
        if len(actors) > 5:
            actors_node.add(f"[dim]... and {len(actors) - 5} more[/dim]")

    # Show full JSON for any unhandled fields
    handled_keys = {"branch", "actors"}
    remaining = {k: v for k, v in result.items() if k not in handled_keys}

    if remaining:
        status_tree.add("[bold]Additional Data:[/bold]")
        for key, value in remaining.items():
            status_tree.add(f"[dim]{key}:[/dim] {value}")

    console.print(Panel(status_tree, border_style="cyan"))


def _print_history(result: Any) -> None:
    """Format history output as a table."""
    if not isinstance(result, dict) or "turns" not in result:
        console.print(JSON.from_data(result))
        return

    turns = result["turns"]
    if not turns:
        console.print("[yellow]No turns in history[/yellow]")
        return

    table = Table(title="Turn History", border_style="blue")
    table.add_column("Turn ID", style="cyan", no_wrap=True)
    table.add_column("Index", style="magenta", justify="right")
    table.add_column("Timestamp", style="green")
    table.add_column("Type", style="yellow")

    for turn in turns:
        turn_id = str(turn.get("id", ""))[:12] + "..."
        index = str(turn.get("index", "N/A"))
        timestamp = turn.get("timestamp", "N/A")
        turn_type = turn.get("type", "N/A")
        table.add_row(turn_id, index, timestamp, turn_type)

    console.print(table)


def _print_entities(result: Any) -> None:
    """Format entity list as a table."""
    if not isinstance(result, dict) or "entities" not in result:
        console.print(JSON.from_data(result))
        return

    entities = result["entities"]
    if not entities:
        console.print("[yellow]No entities registered[/yellow]")
        return

    table = Table(title="Registered Entities", border_style="green")
    table.add_column("Entity ID", style="cyan", no_wrap=True)
    table.add_column("Type", style="yellow")
    table.add_column("Actor", style="magenta", no_wrap=True)
    table.add_column("Facet", style="blue", no_wrap=True)

    for entity in entities:
        entity_id = str(entity.get("id", ""))[:12] + "..."
        entity_type = entity.get("type", "N/A")
        actor = str(entity.get("actor", ""))[:8] + "..."
        facet = str(entity.get("facet", ""))[:8] + "..."
        table.add_row(entity_id, entity_type, actor, facet)

    console.print(table)


def _print_capabilities(result: Any) -> None:
    """Format capabilities list as a table."""
    if not isinstance(result, dict) or "capabilities" not in result:
        console.print(JSON.from_data(result))
        return

    capabilities = result["capabilities"]
    if not capabilities:
        console.print("[yellow]No capabilities available[/yellow]")
        return

    table = Table(title="Available Capabilities", border_style="magenta")
    table.add_column("Capability ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="yellow")
    table.add_column("Provider", style="green", no_wrap=True)
    table.add_column("Status", style="blue")

    for cap in capabilities:
        cap_id = str(cap.get("id", ""))[:12] + "..."
        name = cap.get("name", "N/A")
        provider = str(cap.get("provider", ""))[:12] + "..."
        status = cap.get("status", "active")
        table.add_row(cap_id, name, provider, status)

    console.print(table)


def _print_operation_result(result: Any, operation: str) -> None:
    """Format operation results with success panels."""
    if isinstance(result, dict):
        if result.get("success") or "id" in result:
            title = "[bold green]Success[/bold green]"
        else:
            title = "[bold yellow]Result[/bold yellow]"

        content = JSON.from_data(result)
        console.print(Panel(content, title=title, border_style="green"))
    else:
        console.print(Panel(str(result), title="Result", border_style="green"))


def _print_navigation_result(result: Any, operation: str) -> None:
    """Format navigation operation results."""
    if isinstance(result, dict):
        title = f"[bold cyan]{operation.title()}[/bold cyan]"
        content = JSON.from_data(result)
        console.print(Panel(content, title=title, border_style="cyan"))
    else:
        console.print(f"[green]{result}[/green]")
