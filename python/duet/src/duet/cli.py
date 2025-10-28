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
    subparsers = parser.add_subparsers(dest="command")
    parser.set_defaults(command="status", branch=None)

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

    workspace = subparsers.add_parser("workspace", help="Workspace operations")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)

    workspace_sub.add_parser("scan", help="Force a workspace rescan.")
    workspace_sub.add_parser("entries", help="List workspace dataspace entries.")

    read_parser = workspace_sub.add_parser("read", help="Read a file from the workspace.")
    read_parser.add_argument("path", help="Workspace-relative path to read.")

    write_parser = workspace_sub.add_parser("write", help="Write content to a workspace file.")
    write_parser.add_argument("path", help="Workspace-relative path to write.")
    write_parser.add_argument(
        "--content",
        "-c",
        required=True,
        help="Content to write to the file.",
    )

    raw = subparsers.add_parser("raw", help="Send a raw command/params JSON payload.")
    raw.add_argument("rpc_command")
    raw.add_argument("params", nargs="?", default="{}")

    agent = subparsers.add_parser("agent", help="Agent operations")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)

    agent_invoke = agent_sub.add_parser("invoke", help="Invoke the Claude Code agent.")
    agent_invoke.add_argument("prompt", help="Prompt text to send to the agent.")

    agent_sub.add_parser("responses", help="List cached agent responses.")

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
        elif args.command == "workspace":
            if args.workspace_command == "scan":
                result = await client.call("workspace_rescan", {})
            elif args.workspace_command == "entries":
                result = await client.call("workspace_entries", {})
            elif args.workspace_command == "read":
                result = await client.call("workspace_read", {"path": args.path})
            elif args.workspace_command == "write":
                result = await client.call(
                    "workspace_write",
                    {"path": args.path, "content": args.content},
                )
            else:  # pragma: no cover
                raise ProtocolError(f"Unsupported workspace command: {args.workspace_command}")
        elif args.command == "agent":
            if args.agent_command == "invoke":
                result = await client.call("agent_invoke", {"prompt": args.prompt})
            elif args.agent_command == "responses":
                result = await client.call("agent_responses", {})
            else:  # pragma: no cover
                raise ProtocolError(f"Unsupported agent command: {args.agent_command}")
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

        workspace_command = getattr(args, "workspace_command", None)
        agent_command = getattr(args, "agent_command", None)

        command_key = (
            f"workspace:{workspace_command}"
            if args.command == "workspace"
            else f"agent:{agent_command}"
            if args.command == "agent"
            else args.command
        )
        _print_result(result, command_key)
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
    elif command == "workspace:entries":
        _print_workspace_entries(result)
    elif command == "workspace:read":
        _print_workspace_read(result)
    elif command in ("workspace:scan", "workspace:write"):
        _print_operation_result(result, command)
    elif command == "agent:invoke":
        _print_agent_invoke(result)
    elif command == "agent:responses":
        _print_agent_responses(result)
    elif isinstance(result, (dict, list)):
        console.print(JSON.from_data(result))
    else:
        console.print(result)


def _print_status(result: Any) -> None:
    """Format status output with panels and tree view."""
    if not isinstance(result, dict):
        console.print(JSON.from_data(result))
        return

    branch = result.get("active_branch", "main")
    head_turn = result.get("head_turn", "turn_0")
    pending_inputs = result.get("pending_inputs", 0)
    snapshot_interval = result.get("snapshot_interval", 0)

    tree = Tree(f"[bold cyan]Branch[/bold cyan] [white]{branch}[/white]")
    tree.add(f"[dim]Head Turn:[/dim] {head_turn}")
    tree.add(f"[dim]Pending Inputs:[/dim] {pending_inputs}")
    tree.add(f"[dim]Snapshot Interval:[/dim] {snapshot_interval}")

    console.print(
        Panel.fit(
            tree,
            title="[bold]Runtime Status[/bold]",
            border_style="cyan",
        )
    )


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
    table.add_column("Actor", style="magenta", no_wrap=True)
    table.add_column("Clock", style="yellow", justify="right")
    table.add_column("Inputs", style="green", justify="right")
    table.add_column("Outputs", style="green", justify="right")
    table.add_column("Timestamp", style="dim")

    for turn in turns:
        turn_id = str(turn.get("turn_id", ""))[:16] + "..."
        actor = str(turn.get("actor", ""))[:12] + "..."
        clock = str(turn.get("clock", "0"))
        inputs = str(turn.get("input_count", 0))
        outputs = str(turn.get("output_count", 0))
        timestamp = turn.get("timestamp", "N/A")
        table.add_row(turn_id, actor, clock, inputs, outputs, timestamp)

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
    table.add_column("Patterns", style="dim", justify="right")

    for entity in entities:
        entity_id = str(entity.get("id", ""))[:12] + "..."
        entity_type = entity.get("entity_type", "N/A")
        actor = str(entity.get("actor", ""))[:12] + "..."
        facet = str(entity.get("facet", ""))[:12] + "..."
        patterns = str(entity.get("pattern_count", 0))
        table.add_row(entity_id, entity_type, actor, facet, patterns)

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

    table = Table(title="Capabilities", border_style="magenta")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Kind", style="yellow")
    table.add_column("Holder", style="green", no_wrap=True)
    table.add_column("Status", style="blue")
    table.add_column("Attenuation", style="dim")

    for cap in capabilities:
        cap_id = str(cap.get("id", ""))[:12] + "..."
        kind = cap.get("kind", "N/A")
        holder = str(cap.get("holder", ""))[:12] + "..."
        status = cap.get("status", "unknown")
        attenuation = ", ".join(
            str(value) for value in cap.get("attenuation", [])
        )
        table.add_row(cap_id, kind, holder, status, attenuation)

    console.print(table)


def _print_workspace_entries(result: Any) -> None:
    """Render workspace entries from the dataspace."""
    if not isinstance(result, dict) or "entries" not in result:
        console.print(JSON.from_data(result))
        return

    entries = result["entries"]
    if not entries:
        console.print("[yellow]Workspace is empty[/yellow]")
        return

    table = Table(title="Workspace Entries", border_style="green")
    table.add_column("Path", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Size", justify="right", style="yellow")
    table.add_column("Modified", style="green")
    table.add_column("Digest", style="dim")

    for entry in entries:
        path = entry.get("path", "")
        kind = entry.get("kind", "")
        size = str(entry.get("size", 0))
        modified = entry.get("modified") or "--"
        digest = entry.get("digest") or "--"
        table.add_row(path, kind, size, modified, digest)

    console.print(table)


def _print_workspace_read(result: Any) -> None:
    """Display workspace read output."""
    if not isinstance(result, dict) or "content" not in result:
        console.print(JSON.from_data(result))
        return

    content = result.get("content", "")
    path = result.get("path", "")
    panel = Panel(
        content,
        title=f"[bold green]Workspace Read[/bold green] [dim]{path}[/dim]",
        border_style="green",
    )
    console.print(panel)


def _print_agent_invoke(result: Any) -> None:
    """Show the response from an agent invocation."""
    if not isinstance(result, dict) or "response" not in result:
        console.print(JSON.from_data(result))
        return

    prompt = result.get("prompt", "")
    response = result.get("response", "")
    request_id = result.get("request_id", "")
    agent = result.get("agent", "agent")

    panel = Panel(
        response,
        title=f"[bold blue]{agent}[/bold blue] [dim]{request_id}[/dim]",
        subtitle=f"Prompt: {prompt}",
        border_style="blue",
    )
    console.print(panel)


def _print_agent_responses(result: Any) -> None:
    """Render cached agent responses."""
    if not isinstance(result, dict) or "responses" not in result:
        console.print(JSON.from_data(result))
        return

    responses = result["responses"]
    if not responses:
        console.print("[yellow]No agent responses[/yellow]")
        return

    table = Table(title="Agent Responses", border_style="blue")
    table.add_column("Request ID", style="cyan", no_wrap=True)
    table.add_column("Agent", style="magenta")
    table.add_column("Prompt", style="white")
    table.add_column("Response", style="green")

    for entry in responses:
        table.add_row(
            str(entry.get("request_id", ""))[:12] + "...",
            entry.get("agent", ""),
            entry.get("prompt", ""),
            entry.get("response", ""),
        )

    console.print(table)


def _print_operation_result(result: Any, operation: str) -> None:
    """Format operation results with success panels."""
    if isinstance(result, dict):
        status = result.get("status", "ok")
        if "queued_turn" in result:
            title = "[bold green]Message Queued[/bold green]"
            subtitle = f"Turn {result['queued_turn']}"
        elif "entity_id" in result:
            title = "[bold green]Entity Registered[/bold green]"
            subtitle = f"ID {result['entity_id']}"
        else:
            title = "[bold green]Success[/bold green]" if status == "ok" else "[bold yellow]Result[/bold yellow]"
            subtitle = ""

        content = JSON.from_data(result)
        console.print(
            Panel(
                content,
                title=title,
                subtitle=subtitle,
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                str(result),
                title="[bold green]Result[/bold green]",
                border_style="green",
            )
        )


def _print_navigation_result(result: Any, operation: str) -> None:
    """Format navigation operation results."""
    if isinstance(result, dict):
        title = f"[bold cyan]{operation.title()}[/bold cyan]"
        content = JSON.from_data(result)
        console.print(Panel(content, title=title, border_style="cyan"))
    else:
        console.print(f"[green]{result}[/green]")
