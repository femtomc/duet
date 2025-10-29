"""Command-line interface for the Duet runtime using Typer + Rich."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import rich_click as click  # Must be imported before typer to patch Click
import typer
from rich.console import Console, Group
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from .protocol.client import ControlClient, ProtocolError

# Configure rich-click aesthetics
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.STYLE_OPTION = "bold cyan"
click.rich_click.STYLE_SWITCH = "bold cyan"
click.rich_click.STYLE_COMMAND = "bold yellow"
click.rich_click.STYLE_HELPTEXT = "dim"
click.rich_click.MAX_WIDTH = 100

console = Console()

app = typer.Typer(
    add_completion=False,
    help="Interact with the Duet runtime over the NDJSON control protocol.",
    rich_markup_mode="rich",
)
workspace_app = typer.Typer(
    help="Workspace operations",
    add_completion=False,
    rich_markup_mode="rich",
)
agent_app = typer.Typer(
    help="Agent operations",
    add_completion=False,
    rich_markup_mode="rich",
)
reaction_app = typer.Typer(
    help="Reactive automations",
    add_completion=False,
    rich_markup_mode="rich",
)
transcript_app = typer.Typer(
    help="Agent transcripts",
    add_completion=False,
    rich_markup_mode="rich",
)
dataspace_app = typer.Typer(
    help="Dataspace inspection",
    add_completion=False,
    rich_markup_mode="rich",
)
daemon_app = typer.Typer(
    help="Manage the codebased daemon",
    add_completion=False,
    rich_markup_mode="rich",
)
app.add_typer(workspace_app, name="workspace", rich_help_panel="Workspace")
app.add_typer(agent_app, name="agent", rich_help_panel="Agents")
app.add_typer(reaction_app, name="reaction", rich_help_panel="Runtime")
app.add_typer(transcript_app, name="transcript", rich_help_panel="Agents")
app.add_typer(dataspace_app, name="dataspace", rich_help_panel="Dataspace")
app.add_typer(daemon_app, name="daemon", rich_help_panel="Runtime")



DEFAULT_ROOT = Path('.duet')
DEFAULT_DAEMON_HOST = '127.0.0.1'
DAEMON_STATE_FILE = 'daemon.json'
DAEMON_LOG_FILE = 'daemon.log'


@dataclass
class DaemonState:
    pid: int
    host: str
    port: int
    root: Path




def _resolve_root_path(root: Optional[Path]) -> Path:
    path = root or DEFAULT_ROOT
    return path.expanduser()


def _ensure_root_dir(root: Optional[Path]) -> Path:
    path = _resolve_root_path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _daemon_state_path(root: Path) -> Path:
    return root / DAEMON_STATE_FILE


def _load_daemon_state(root: Path) -> Optional[DaemonState]:
    state_path = _daemon_state_path(root)
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text())
        state = DaemonState(
            pid=int(data["pid"]),
            host=data.get("host", DEFAULT_DAEMON_HOST),
            port=int(data["port"]),
            root=root,
        )
    except Exception:
        with contextlib.suppress(OSError):
            state_path.unlink()
        return None
    if not _is_process_alive(state.pid):
        with contextlib.suppress(OSError):
            state_path.unlink()
        return None
    return state


def _save_daemon_state(state: DaemonState) -> None:
    state_path = _daemon_state_path(state.root)
    payload = {"pid": state.pid, "host": state.host, "port": state.port}
    state_path.write_text(json.dumps(payload))


def _clear_daemon_state(root: Path) -> None:
    state_path = _daemon_state_path(root)
    with contextlib.suppress(OSError):
        state_path.unlink()


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def _ping_daemon(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _await_daemon(host: str, port: int, retries: int = 50, delay: float = 0.1) -> None:
    for _ in range(retries):
        if _ping_daemon(host, port, timeout=delay):
            return
        time.sleep(delay)
    raise RuntimeError("daemon did not become ready in time")

@dataclass
class CLIState:
    """Runtime configuration shared across commands."""

    root: Optional[Path]
    codebased_bin: Optional[Path]


def _run(coro: asyncio.Future[Any]) -> None:
    """Execute an async coroutine with unified error handling."""

    try:
        asyncio.run(coro)
    except ProtocolError as exc:  # pragma: no cover - exercised via integration
        _print_protocol_error(exc)
        raise typer.Exit(1)
    except FileNotFoundError as exc:  # pragma: no cover - exercised manually
        _print_launch_error(exc)
        raise typer.Exit(1)
    except KeyboardInterrupt:  # pragma: no cover - manual interrupt
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(130)
    except Exception as exc:  # pragma: no cover - safety net
        _print_unexpected_error(exc)
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    root: Optional[Path] = typer.Option(  # noqa: B008
        None,
        help="Runtime root directory passed to codebased.",
        rich_help_panel="Runtime",
    ),
    codebased_bin: Optional[Path] = typer.Option(  # noqa: B008
        None,
        help="Path to the codebased daemon binary (overrides auto-discovery).",
        rich_help_panel="Runtime",
    ),
) -> None:
    """Top-level callback storing shared CLI state."""

    ctx.obj = CLIState(root=root, codebased_bin=codebased_bin)

    if ctx.invoked_subcommand is None:
        _run(_run_status(ctx.obj, branch=None))
        raise typer.Exit()


@app.command(rich_help_panel="Runtime")
def status(
    ctx: typer.Context,
    branch: Optional[str] = typer.Option(  # noqa: B008
        None,
        help="Show status for a specific branch.",
    ),
) -> None:
    """Show runtime status."""

    _run(_run_status(ctx.obj, branch))


@app.command(rich_help_panel="Runtime")
def history(
    ctx: typer.Context,
    branch: str = typer.Option("main", help="Branch to inspect."),
    start: int = typer.Option(0, help="Starting index of the history slice."),
    limit: int = typer.Option(20, help="Number of turns to display."),
) -> None:
    """Show branch turn history."""

    params = {"branch": branch, "start": start, "limit": limit}
    _run(_run_call(ctx.obj, "history", params, "history"))


@app.command(rich_help_panel="Runtime")
def send(
    ctx: typer.Context,
    actor: str = typer.Argument(..., help="Target actor UUID."),
    facet: str = typer.Argument(..., help="Target facet UUID."),
    payload: str = typer.Argument(..., help="Preserves text payload."),
) -> None:
    """Send a message to an actor/facet."""

    _run(_run_send_message(ctx.obj, actor, facet, payload))


@app.command(rich_help_panel="Runtime")
def register_entity(
    ctx: typer.Context,
    actor: str = typer.Argument(..., help="Actor UUID."),
    facet: str = typer.Argument(..., help="Facet UUID."),
    entity_type: str = typer.Argument(..., help="Entity type identifier."),
    config: str = typer.Option("nil", help="Preserves config value."),
) -> None:
    """Register a new entity instance."""

    params = {
        "actor": actor,
        "facet": facet,
        "entity_type": entity_type,
        "config": config,
    }
    _run(_run_call(ctx.obj, "register_entity", params, "register-entity"))


@app.command(name="list-entities", rich_help_panel="Runtime")
def list_entities(ctx: typer.Context, actor: Optional[str] = typer.Option(None, help="Filter by actor UUID.")) -> None:  # noqa: B008,E501
    """List registered entities."""

    params = {"actor": actor} if actor else {}
    _run(_run_call(ctx.obj, "list_entities", params, "list-entities"))


@app.command(name="list-capabilities", rich_help_panel="Runtime")
def list_capabilities(
    ctx: typer.Context,
    actor: Optional[str] = typer.Option(None, help="Filter by actor UUID."),
) -> None:
    """List known capabilities."""

    params = {"actor": actor} if actor else {}
    _run(_run_call(ctx.obj, "list_capabilities", params, "list-capabilities"))


@app.command(rich_help_panel="Runtime")
def goto(
    ctx: typer.Context,
    turn_id: str = typer.Argument(..., help="Turn identifier to jump to."),
    branch: Optional[str] = typer.Option(None, help="Branch to adjust first."),
) -> None:
    """Jump to a specific turn."""

    params: Dict[str, Any] = {"turn_id": turn_id}
    if branch:
        params["branch"] = branch
    _run(_run_call(ctx.obj, "goto", params, "goto"))


@app.command(rich_help_panel="Runtime")
def back(
    ctx: typer.Context,
    count: int = typer.Option(1, help="Number of turns to rewind."),
    branch: Optional[str] = typer.Option(None, help="Branch to adjust first."),
) -> None:
    """Rewind the runtime by N turns."""

    params: Dict[str, Any] = {"count": count}
    if branch:
        params["branch"] = branch
    _run(_run_call(ctx.obj, "back", params, "back"))


@app.command(rich_help_panel="Runtime")
def fork(
    ctx: typer.Context,
    source: str = typer.Option("main", help="Source branch."),
    new_branch: str = typer.Option(..., help="Name of the new branch."),
    from_turn: Optional[str] = typer.Option(None, help="Optional base turn."),
) -> None:
    """Fork a new branch."""

    params: Dict[str, Any] = {"source": source, "new_branch": new_branch}
    if from_turn:
        params["from_turn"] = from_turn
    _run(_run_call(ctx.obj, "fork", params, "fork"))


@app.command(rich_help_panel="Runtime")
def merge(
    ctx: typer.Context,
    source: str = typer.Option(..., help="Source branch."),
    target: str = typer.Option(..., help="Target branch."),
) -> None:
    """Merge a source branch into a target branch."""

    params = {"source": source, "target": target}
    _run(_run_call(ctx.obj, "merge", params, "merge"))


@app.command(name="invoke-capability", rich_help_panel="Runtime")
def invoke_capability(
    ctx: typer.Context,
    capability: str = typer.Argument(..., help="Capability UUID."),
    payload: str = typer.Argument(..., help="Preserves payload."),
) -> None:
    """Invoke a capability by id."""

    _run(_run_invoke_capability(ctx.obj, capability, payload))


@app.command(rich_help_panel="Runtime")
def raw(
    ctx: typer.Context,
    rpc_command: str = typer.Argument(..., help="Command name to invoke."),
    params: str = typer.Argument("{}", help="JSON object of parameters."),
) -> None:
    """Send a raw command with JSON parameters."""

    payload = json_loads(params)
    if not isinstance(payload, dict):
        raise typer.BadParameter("Params must decode to a JSON object")
    _run(_run_call(ctx.obj, rpc_command, payload, "raw"))


@workspace_app.command("entries")
def workspace_entries(ctx: typer.Context) -> None:
    """List workspace dataspace entries."""

    _run(_run_call(ctx.obj, "workspace_entries", {}, "workspace:entries"))


@workspace_app.command("scan")
def workspace_scan(ctx: typer.Context) -> None:
    """Trigger a workspace rescan."""

    _run(_run_call(ctx.obj, "workspace_rescan", {}, "workspace:scan"))


@workspace_app.command("read")
def workspace_read(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Workspace-relative path to read."),
) -> None:
    """Read a file from the workspace."""

    _run(_run_call(ctx.obj, "workspace_read", {"path": path}, "workspace:read"))


@workspace_app.command("write")
def workspace_write(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Workspace-relative path to write."),
    content: str = typer.Option(..., "--content", "-c", help="Content to write."),
) -> None:
    """Write content to a workspace file."""

    params = {"path": path, "content": content}
    _run(_run_call(ctx.obj, "workspace_write", params, "workspace:write"))


@agent_app.command("invoke")
def agent_invoke(
    ctx: typer.Context,
    prompt: str = typer.Argument(..., help="Prompt text for Claude Code."),
) -> None:
    """Queue a prompt for the Claude Code agent."""

    params = {"prompt": prompt}
    _run(_run_call(ctx.obj, "agent_invoke", params, "agent:invoke"))


@agent_app.command("responses")
def agent_responses(
    ctx: typer.Context,
    request_id: Optional[str] = typer.Option(None, help="Filter responses by request id."),
    wait: float = typer.Option(0.0, help="Time (seconds) to wait for new responses.", min=0.0),
    limit: Optional[int] = typer.Option(None, help="Maximum number of responses to return.", min=1),
) -> None:
    """List cached agent responses."""

    params: Dict[str, Any] = {}
    if request_id:
        params["request_id"] = request_id
    if wait > 0:
        params["wait_ms"] = int(wait * 1000)
    if limit is not None:
        params["limit"] = limit
    _run(_run_call(ctx.obj, "agent_responses", params, "agent:responses"))


@agent_app.command("chat")
def agent_chat(
    ctx: typer.Context,
    prompt: Optional[str] = typer.Argument(None, help="Prompt to send to the agent."),
    wait_for_response: bool = typer.Option(
        False,
        "--wait-for-response",
        help="Block until at least one response is recorded.",
    ),
    wait: float = typer.Option(
        0.0,
        help="Extra time (seconds) to wait after the first response is observed (requires --wait-for-response).",
        min=0.0,
    ),
    resume_request_id: Optional[str] = typer.Option(
        None,
        "--resume",
        help="Request ID whose transcript should be prepended as conversation context.",
    ),
    history_limit: int = typer.Option(
        10,
        "--history-limit",
        help="Maximum transcript entries to include when using --resume.",
        min=1,
        show_default=True,
    ),
) -> None:
    """Send a prompt to the agent. Combine with --wait-for-response to block for results."""

    message = prompt or typer.prompt("Prompt")
    _run(
        _run_agent_chat(
            ctx.obj,
            message,
            wait_for_response,
            wait,
            resume_request_id,
            history_limit,
        )
    )


@reaction_app.command("register")
def reaction_register(
    ctx: typer.Context,
    actor: str = typer.Option(..., help="Actor UUID that owns the reaction."),
    facet: str = typer.Option(..., help="Facet UUID used for the pattern scope."),
    pattern: str = typer.Option(..., help="Preserves pattern expression."),
    effect: str = typer.Option(
        "assert",
        help="Reaction effect type.",
        case_sensitive=False,
        show_default=True,
    ),
    value: Optional[str] = typer.Option(None, help="Preserves value to assert (for assert effect)."),
    value_from_match: bool = typer.Option(
        False,
        help="Use the entire matched value as the asserted value (assert effect).",
    ),
    value_match_index: Optional[int] = typer.Option(
        None,
        help="Use the Nth element from the matched record/sequence as the asserted value.",
        min=0,
    ),
    target_facet: Optional[str] = typer.Option(None, help="Override facet for asserted value."),
    target_actor: Optional[str] = typer.Option(None, help="Target actor for send-message effect."),
    target_facet_msg: Optional[str] = typer.Option(None, help="Target facet for send-message effect."),
    payload: Optional[str] = typer.Option(None, help="Preserves payload for send-message effect."),
    payload_from_match: bool = typer.Option(
        False,
        help="Use the entire matched value as the message payload (send-message effect).",
    ),
    payload_match_index: Optional[int] = typer.Option(
        None,
        help="Use the Nth element from the matched value as the message payload.",
        min=0,
    ),
) -> None:
    """Register a new reaction."""

    effect_key = effect.lower()
    if effect_key == "assert":
        if sum(
            [
                1 if value is not None else 0,
                1 if value_from_match else 0,
                1 if value_match_index is not None else 0,
            ]
        ) > 1:
            raise typer.BadParameter(
                "Use only one of --value, --value-from-match, or --value-match-index",
                param_hint="value",
            )
        if value_match_index is not None:
            value_spec: Dict[str, Any] = {
                "type": "match-index",
                "index": value_match_index,
            }
        elif value_from_match:
            value_spec: Dict[str, Any] = {"type": "match"}
        else:
            if value is None:
                raise typer.BadParameter(
                    "--value is required unless --value-from-match or --value-match-index is provided",
                    param_hint="value",
                )
            value_spec = {"type": "literal", "value": value}

        effect_payload = {"type": "assert", "value": value_spec}
        if target_facet:
            effect_payload["target_facet"] = target_facet
    elif effect_key in {"send-message", "send_message"}:
        if target_actor is None or target_facet_msg is None:
            raise typer.BadParameter(
                "--target-actor and --target-facet-msg are required for send-message effect",
                param_hint="effect",
            )
        if sum(
            [
                1 if payload is not None else 0,
                1 if payload_from_match else 0,
                1 if payload_match_index is not None else 0,
            ]
        ) > 1:
            raise typer.BadParameter(
                "Use only one of --payload, --payload-from-match, or --payload-match-index",
                param_hint="payload",
            )
        if payload_match_index is not None:
            payload_spec = {
                "type": "match-index",
                "index": payload_match_index,
            }
        elif payload_from_match:
            payload_spec = {"type": "match"}
        else:
            if payload is None:
                raise typer.BadParameter(
                    "--payload is required unless --payload-from-match or --payload-match-index is provided",
                    param_hint="payload",
                )
            payload_spec = {"type": "literal", "value": payload}

        effect_payload = {
            "type": "send-message",
            "actor": target_actor,
            "facet": target_facet_msg,
            "payload": payload_spec,
        }
    else:
        raise typer.BadParameter(f"Unsupported effect type: {effect}", param_hint="effect")

    params = {
        "actor": actor,
        "facet": facet,
        "pattern": pattern,
        "effect": effect_payload,
    }

    _run(_run_call(ctx.obj, "reaction_register", params, "reaction:register"))


@reaction_app.command("unregister")
def reaction_unregister(
    ctx: typer.Context,
    reaction_id: str = typer.Argument(..., help="Reaction identifier to remove."),
) -> None:
    """Unregister a reaction."""

    params = {"reaction_id": reaction_id}
    _run(_run_call(ctx.obj, "reaction_unregister", params, "reaction:unregister"))


@reaction_app.command("list")
def reaction_list(ctx: typer.Context) -> None:
    """List registered reactions."""

    _run(_run_call(ctx.obj, "reaction_list", {}, "reaction:list"))


@dataspace_app.command("assertions")
def dataspace_assertions(
    ctx: typer.Context,
    actor: Optional[str] = typer.Option(None, help="Filter by actor UUID."),
    label: Optional[str] = typer.Option(None, help="Filter by record label/symbol."),
    request_id: Optional[str] = typer.Option(
        None, help="Filter record assertions whose first field matches the id."
    ),
    limit: Optional[int] = typer.Option(None, help="Maximum assertions to return."),
) -> None:
    """Inspect assertions currently in the dataspace."""

    params: Dict[str, Any] = {}
    if actor:
        params["actor"] = actor
    if label:
        params["label"] = label
    if request_id:
        params["request_id"] = request_id
    if limit is not None:
        params["limit"] = limit

    _run(_run_call(ctx.obj, "dataspace_assertions", params, "dataspace:assertions"))




@daemon_app.command("start")
def daemon_start(
    ctx: typer.Context,
    host: str = typer.Option(DEFAULT_DAEMON_HOST, help="Host interface for the daemon."),
    port: Optional[int] = typer.Option(None, help="Port for the daemon (auto if omitted)."),
) -> None:
    root = _ensure_root_dir(ctx.obj.root)
    existing = _load_daemon_state(root)
    if existing and _ping_daemon(existing.host, existing.port):
        console.print(
            f"[green]Daemon already running on {existing.host}:{existing.port} (pid {existing.pid})[/green]"
        )
        return

    if port is None:
        port = _pick_free_port(host)

    cmd = list(_codebased_command(ctx.obj))
    if "--stdio" in cmd:
        cmd.remove("--stdio")
    cmd.extend(["--root", str(root)])
    cmd.extend(["--listen", f"{host}:{port}"])

    log_path = root / DAEMON_LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log_file:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    try:
        _await_daemon(host, port)
    except RuntimeError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGTERM)
        console.print("[red]Failed to start daemon[/red]")
        raise typer.Exit(1)

    _save_daemon_state(DaemonState(pid=process.pid, host=host, port=port, root=root))
    console.print(
        f"[green]Daemon listening on {host}:{port} (pid {process.pid}). Log: {log_path}[/green]"
    )


@daemon_app.command("stop")
def daemon_stop(ctx: typer.Context) -> None:
    root = _resolve_root_path(ctx.obj.root)
    state = _load_daemon_state(root)
    if not state:
        console.print("[yellow]Daemon is not running.[/yellow]")
        return

    if _is_process_alive(state.pid):
        try:
            os.kill(state.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            for _ in range(50):
                if not _is_process_alive(state.pid):
                    break
                time.sleep(0.1)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(state.pid, signal.SIGKILL)
    _clear_daemon_state(root)
    console.print("[green]Daemon stopped.[/green]")


@daemon_app.command("status")
def daemon_status(ctx: typer.Context) -> None:
    root = _resolve_root_path(ctx.obj.root)
    state = _load_daemon_state(root)
    if not state:
        console.print("[yellow]Daemon is not running.[/yellow]")
        return

    alive = _is_process_alive(state.pid)
    reachable = _ping_daemon(state.host, state.port)
    if not alive:
        console.print(
            f"[red]Daemon record stale (pid {state.pid}); cleaning up.[/red]"
        )
        _clear_daemon_state(root)
        return

    status = "reachable" if reachable else "not responding"
    console.print(
        f"[green]Daemon pid {state.pid} listening on {state.host}:{state.port} ({status}).[/green]"
    )
@dataspace_app.command("tail")
def dataspace_tail(
    ctx: typer.Context,
    branch: str = typer.Option("main", help="Branch to inspect."),
    since: Optional[str] = typer.Option(None, help="Start streaming after this turn id."),
    limit: int = typer.Option(10, help="Number of turns to return per request.", min=1),
    actor: Optional[str] = typer.Option(None, help="Filter by actor UUID."),
    label: Optional[str] = typer.Option(None, help="Filter by record label/symbol."),
    request_id: Optional[str] = typer.Option(None, help="Filter record assertions whose first field matches the id."),
    event_type: List[str] = typer.Option([], "--event-type", "-e", help="Restrict to specific event types (assert, retract)."),
    follow: bool = typer.Option(False, help="Continue polling for new events."),
    interval: float = typer.Option(1.0, help="Polling interval (seconds) when following.", min=0.1),
) -> None:
    """Tail assertion events from the dataspace."""

    params: Dict[str, Any] = {
        "branch": branch,
        "limit": limit,
    }
    if actor:
        params["actor"] = actor
    if label:
        params["label"] = label
    if request_id:
        params["request_id"] = request_id
    if event_type:
        params["event_types"] = [et.lower() for et in event_type]
    if since:
        params["since"] = since
    if follow:
        
        wait_ms = max(int(interval * 1000), 0)
        params["wait_ms"] = wait_ms

    _run(_run_dataspace_tail(ctx.obj, params, follow, interval))


def json_loads(payload: str) -> Any:
    """Parse JSON with helpful error messages."""

    return json.loads(payload)


async def _run_status(state: CLIState, branch: Optional[str]) -> None:
    params: Dict[str, Any] = {}
    if branch:
        params["branch"] = branch
    await _run_call(state, "status", params, "status")


async def _run_send_message(state: CLIState, actor: str, facet: str, payload: str) -> None:
    client = await _connect_client(state)
    try:
        result = await client.send_message(actor, facet, payload)
        _print_result(result, "send")
    finally:
        await client.close()


async def _run_invoke_capability(state: CLIState, capability: str, payload: str) -> None:
    client = await _connect_client(state)
    try:
        result = await client.invoke_capability(capability, payload)
        _print_result(result, "invoke-capability")
    finally:
        await client.close()


async def _run_call(state: CLIState, rpc_command: str, params: Dict[str, Any], pretty_command: str) -> None:
    client = await _connect_client(state)
    try:
        result = await client.call(rpc_command, params)
        _print_result(result, pretty_command)
    finally:
        await client.close()


async def _run_agent_chat(
    state: CLIState,
    prompt: str,
    wait_for_response: bool,
    extra_wait: float,
    resume_request_id: Optional[str],
    history_limit: int,
) -> None:
    client = await _connect_client(state)
    try:
        final_prompt = prompt
        if resume_request_id:
            final_prompt = await _augment_prompt_with_history(
                client, prompt, resume_request_id, history_limit
            )

        invoke_result = await client.call("agent_invoke", {"prompt": final_prompt})
        _print_result(invoke_result, "agent:invoke")

        request_id = None
        if isinstance(invoke_result, dict):
            request_id = invoke_result.get("request_id")
        if not request_id:
            return

        if not wait_for_response:
            if extra_wait > 0:
                console.print(
                    "[yellow]Ignoring --wait value because --wait-for-response was not supplied.[/yellow]"
                )
            console.print(
                f"[dim]Request {request_id} dispatched. Retrieve results with "
                f"`duet agent responses --request-id {request_id}` or transcript commands as needed.[/dim]"
            )
            return

        params: Dict[str, Any] = {"request_id": request_id}

        while True:
            responses = await client.call("agent_responses", params)
            if isinstance(responses, dict):
                entries = responses.get("responses")
                if isinstance(entries, list) and entries:
                    _print_result(responses, "agent:responses")
                    break
            await asyncio.sleep(0.1)

        if extra_wait > 0:
            params["wait_ms"] = int(extra_wait * 1000)
            follow_up = await client.call("agent_responses", params)
            _print_result(follow_up, "agent:responses")
    finally:
        await client.close()


async def _augment_prompt_with_history(
    client: ControlClient, prompt: str, resume_request_id: str, history_limit: int
) -> str:
    params = {"request_id": resume_request_id, "limit": history_limit}
    try:
        transcript = await client.call("transcript_show", params)
    except ProtocolError as exc:
        console.print(
            Panel(
                f"[bold red]Failed to fetch transcript for {resume_request_id}[/bold red]\n\n{exc}",
                border_style="red",
            )
        )
        return prompt

    if not isinstance(transcript, dict):
        console.print(
            Panel(
                f"[yellow]Unexpected transcript payload for {resume_request_id}; proceeding without context.[/yellow]",
                border_style="yellow",
            )
        )
        return prompt

    entries = transcript.get("entries") or []
    if not entries:
        console.print(
            Panel(
                f"[yellow]No transcript entries found for {resume_request_id}; proceeding without context.[/yellow]",
                border_style="yellow",
            )
        )
        return prompt

    history_lines: List[str] = []
    for entry in entries[-history_limit:]:
        prior_prompt = entry.get("prompt")
        prior_response = entry.get("response")
        if prior_prompt:
            history_lines.append(f"User: {prior_prompt}")
        if prior_response:
            history_lines.append(f"Assistant: {prior_response}")

    history_text = "\n\n".join(history_lines).strip()
    if not history_text:
        console.print(
            Panel(
                f"[yellow]Transcript for {resume_request_id} contained no usable text; proceeding without context.[/yellow]",
                border_style="yellow",
            )
        )
        return prompt

    included = len(entries[-history_limit:])
    console.print(
        Panel(
            f"[bold]Resuming conversation[/bold]\nRequest: {resume_request_id}\nEntries included: {included}",
            border_style="blue",
        )
    )

    combined_prompt = f"{history_text}\n\nUser: {prompt}"
    if len(combined_prompt) > 16000:
        console.print(
            Panel(
                "[yellow]Combined prompt exceeds 16k characters; Claude may truncate context.[/yellow]",
                border_style="yellow",
            )
        )
    return combined_prompt


async def _connect_client(state: CLIState) -> ControlClient:
    root = _resolve_root_path(state.root)
    daemon_state = _load_daemon_state(root)
    runtime_addr: Optional[Tuple[str, int]] = None

    if daemon_state:
        if _is_process_alive(daemon_state.pid) and _ping_daemon(daemon_state.host, daemon_state.port):
            runtime_addr = (daemon_state.host, daemon_state.port)
        else:
            _clear_daemon_state(root)

    if runtime_addr:
        client = ControlClient(runtime_addr=runtime_addr)
    else:
        cmd = list(_codebased_command(state))
        if state.root:
            cmd.extend(["--root", str(state.root)])
        client = ControlClient(tuple(cmd))
    await client.connect()
    return client


async def _run_dataspace_tail(state: CLIState, params: Dict[str, Any], follow: bool, interval: float) -> None:
    base_params = params.copy()
    cursor = base_params.pop("since", None)
    client = await _connect_client(state)
    try:
        while True:
            query = base_params.copy()
            if cursor:
                query["since"] = cursor

            result = await client.call("dataspace_events", query)
            _print_result(result, "dataspace:events")

            if not isinstance(result, dict):
                break

            next_cursor = result.get("next_cursor") or cursor
            has_events = bool(result.get("events"))

            if not follow:
                break

            cursor = next_cursor
            if not has_events:
                continue
    finally:
        await client.close()


async def _run_transcript_tail(state: CLIState, params: Dict[str, Any], follow: bool, interval: float) -> None:
    base_params = params.copy()
    cursor = base_params.pop("since", None)
    client = await _connect_client(state)
    wait_ms = max(int(interval * 1000), 0)
    try:
        while True:
            query = base_params.copy()
            if cursor:
                query["since"] = cursor
            if follow and wait_ms > 0:
                query["wait_ms"] = wait_ms

            result = await client.call("transcript_tail", query)
            _print_result(result, "transcript:tail")

            if not isinstance(result, dict):
                break

            cursor = result.get("next_cursor") or cursor
            events = result.get("events") or []

            if not follow:
                break

            if not events:
                continue
    finally:
        await client.close()


async def _run_transcript_export(
    state: CLIState,
    request_id: str,
    branch: Optional[str],
    limit: int,
    destination: Optional[Path],
) -> None:
    client = await _connect_client(state)
    try:
        params: Dict[str, Any] = {"request_id": request_id, "limit": limit}
        if branch:
            params["branch"] = branch
        result = await client.call("transcript_show", params)
    finally:
        await client.close()

    if not isinstance(result, dict):
        console.print(JSON.from_data(result))
        return

    entries = result.get("entries") or []
    branch = branch or result.get("branch", "main")
    header = f"Transcript for {request_id} (branch {branch})"

    if not entries:
        content = header + "\n\n[No transcript entries recorded]"
    else:
        lines: List[str] = [header, ""]
        for idx, entry in enumerate(entries[-limit:], start=1):
            timestamp = entry.get("timestamp")
            agent = entry.get("agent", "agent")
            if timestamp:
                lines.append(f"[{idx}] {timestamp} — {agent}")
            else:
                lines.append(f"[{idx}] {agent}")

            prompt = entry.get("prompt", "")
            response = entry.get("response", "")
            if prompt:
                lines.append(f"User: {prompt}")
            if response:
                lines.append(f"Assistant: {response}")
            lines.append("")
        content = "\n".join(lines).rstrip()

    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        console.print(
            Panel(
                f"[green]Transcript exported to[/green] {destination}",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                content,
                border_style="blue",
            )
        )


def _codebased_command(state: CLIState) -> Tuple[str, ...]:
    if state.codebased_bin:
        return (str(state.codebased_bin), "--stdio")
    env_override = os.environ.get("CODEBASED_BIN") or os.environ.get("DUETD_BIN")
    if env_override:
        return (env_override, "--stdio")
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


# ---------------------------------------------------------------------------
# Rich formatting helpers
# ---------------------------------------------------------------------------

def _print_status(result: Any) -> None:
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

    console.print(Panel.fit(tree, title="[bold]Runtime Status[/bold]", border_style="cyan"))


def _print_history(result: Any) -> None:
    if not isinstance(result, dict) or "turns" not in result:
        console.print(JSON.from_data(result))
        return

    turns = result["turns"]
    if not turns:
        console.print("[yellow]No turns recorded[/yellow]")
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
        clock = str(turn.get("clock", 0))
        inputs = str(turn.get("input_count", 0))
        outputs = str(turn.get("output_count", 0))
        timestamp = turn.get("timestamp", "N/A")
        table.add_row(turn_id, actor, clock, inputs, outputs, timestamp)

    console.print(table)


def _print_entities(result: Any) -> None:
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
        entity_type = entity.get("entity_type", entity.get("type", "N/A"))
        actor = str(entity.get("actor", ""))[:12] + "..."
        facet = str(entity.get("facet", ""))[:12] + "..."
        patterns = str(entity.get("pattern_count", 0))
        table.add_row(entity_id, entity_type, actor, facet, patterns)

    console.print(table)


def _print_capabilities(result: Any) -> None:
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
    table.add_column("Issuer", style="green", no_wrap=True)
    table.add_column("Holder", style="green", no_wrap=True)
    table.add_column("Status", style="blue")
    table.add_column("Attenuation", style="dim")

    for cap in capabilities:
        cap_id = str(cap.get("id", ""))[:12] + "..."
        kind = cap.get("kind", "N/A")
        issuer = str(cap.get("issuer", ""))[:12] + "..."
        holder = str(cap.get("holder", ""))[:12] + "..."
        status = cap.get("status", "unknown")
        attenuation = ", ".join(
            value.as_string().as_ref() if hasattr(value, "as_string") else str(value)
            for value in cap.get("attenuation", [])
        )
        table.add_row(cap_id, kind, issuer, holder, status, attenuation)

    console.print(table)


def _print_workspace_entries(result: Any) -> None:
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
    table.add_column("Size", style="yellow", justify="right")
    table.add_column("Modified", style="green")
    table.add_column("Digest", style="dim")

    for entry in entries:
        table.add_row(
            entry.get("path", ""),
            entry.get("kind", ""),
            str(entry.get("size", 0)),
            entry.get("modified", "--"),
            entry.get("digest", "--"),
        )

    console.print(table)


def _print_workspace_read(result: Any) -> None:
    if not isinstance(result, dict) or "content" not in result:
        console.print(JSON.from_data(result))
        return

    path = result.get("path", "")
    content = result.get("content", "")
    console.print(
        Panel(
            content,
            title=f"[bold green]Workspace Read[/bold green] [dim]{path}[/dim]",
            border_style="green",
        )
    )


def _print_agent_invoke(result: Any) -> None:
    if not isinstance(result, dict) or "request_id" not in result:
        console.print(JSON.from_data(result))
        return

    request_id = result.get("request_id", "")
    prompt = result.get("prompt", "")
    agent = result.get("agent", "agent")
    queued_turn_value = result.get("queued_turn")
    queued_turn = queued_turn_value if queued_turn_value else "pending"
    branch = result.get("branch", "main")
    actor = result.get("actor", "")

    body_lines = [
        f"[bold]Prompt[/bold]\n{prompt}",
        "",
        f"[bold]Branch[/bold] {branch}",
        f"[bold]Actor[/bold] {actor}",
        f"[bold]Queued Turn[/bold] {queued_turn}",
        "[dim]Run `duet agent responses` to inspect replies.[/dim]",
    ]

    console.print(
        Panel(
            "\n".join(body_lines),
            title=f"[bold blue]{agent}[/bold blue] [dim]{request_id}[/dim]",
            border_style="blue",
        )
    )


def _print_agent_responses(result: Any) -> None:
    if not isinstance(result, dict) or "responses" not in result:
        console.print(JSON.from_data(result))
        return

    responses = result["responses"]
    if not responses:
        console.print("[yellow]No agent responses[/yellow]")
        return

    panels = []
    for entry in responses:
        request_id = entry.get("request_id", "")
        agent = entry.get("agent", "agent")
        prompt = entry.get("prompt", "")
        response = entry.get("response", "")
        timestamp = entry.get("timestamp")

        sections = [
            f"[bold]Agent[/bold] {agent}",
            f"[bold]Request[/bold] {request_id}",
        ]
        if timestamp:
            sections.append(f"[bold]Timestamp[/bold] {timestamp}")
        sections.append(f"[bold]Prompt[/bold]\n{prompt or '[dim]—[/dim]'}")
        sections.append(f"[bold]Response[/bold]\n{response or '[dim]—[/dim]'}")

        panels.append(
            Panel(
                "\n\n".join(sections),
                title="Agent Response",
                border_style="blue",
            )
        )

    console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _print_dataspace_assertions(result: Any) -> None:
    if not isinstance(result, dict) or "assertions" not in result:
        console.print(JSON.from_data(result))
        return

    assertions = result["assertions"]
    if not assertions:
        console.print("[yellow]No assertions matched the filters[/yellow]")
        return

    panels = []
    for entry in assertions:
        actor = entry.get("actor", "")
        handle = entry.get("handle", "")
        value = entry.get("value")

        sections = [
            f"[bold]Actor[/bold] {actor}",
            f"[bold]Handle[/bold] {handle}",
        ]
        sections.append(
            "[bold]Value[/bold]\n" + _summarize_value(value, max_length=200)
        )

        panels.append(
            Panel(
                "\n\n".join(sections),
                border_style="cyan",
            )
        )

    console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _print_dataspace_events(result: Any) -> None:
    if not isinstance(result, dict) or "events" not in result:
        console.print(JSON.from_data(result))
        return

    batches = result["events"]
    if not batches:
        console.print("[dim]No new events[/dim]")
        return

    panels = []
    for batch in batches:
        turn_id = batch.get("turn_id", "")
        actor = batch.get("actor", "")
        clock = batch.get("clock")
        timestamp = batch.get("timestamp")

        sections = [
            f"[bold]Turn[/bold] {turn_id}",
            f"[bold]Actor[/bold] {actor}",
        ]
        if clock is not None:
            sections.append(f"[bold]Clock[/bold] {clock}")
        if timestamp:
            sections.append(f"[bold]Timestamp[/bold] {timestamp}")

        event_lines: List[str] = []
        for event in batch.get("events", []):
            action = (event.get("action") or "").upper()
            handle = event.get("handle", "")
            value = event.get("value")
            rendered = f"[bold]{action}[/bold] {handle}"
            if value:
                rendered += f"\n{_summarize_value(value, max_length=200)}"
            event_lines.append(rendered)

        if event_lines:
            sections.append("[bold]Events[/bold]\n" + "\n\n".join(event_lines))

        panels.append(
            Panel(
                "\n\n".join(sections),
                border_style="cyan",
            )
        )

    console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _print_transcript_show(result: Any) -> None:
    if not isinstance(result, dict) or "entries" not in result:
        console.print(JSON.from_data(result))
        return

    entries = result["entries"]
    if not entries:
        console.print("[yellow]No transcript data[/yellow]")
        return

    request_id = result.get("request_id")
    branch = result.get("branch")
    header_lines = []
    if request_id:
        header_lines.append(f"[bold]Request[/bold] {request_id}")
    if branch:
        header_lines.append(f"[bold]Branch[/bold] {branch}")
    if header_lines:
        console.print(
            Panel(
                "\n".join(header_lines),
                title="Transcript",
                border_style="blue",
            )
        )

    panels = []
    for entry in entries:
        agent = entry.get("agent", "agent")
        actor = entry.get("actor", "")
        handle = entry.get("handle", "")
        timestamp = entry.get("timestamp")
        role = entry.get("role")
        tool = entry.get("tool")
        prompt = entry.get("prompt", "")
        response = entry.get("response", "")

        sections = [
            f"[bold]Agent[/bold] {agent}",
            f"[bold]Actor[/bold] {actor}",
        ]
        if handle:
            sections.append(f"[bold]Handle[/bold] {handle}")
        if timestamp:
            sections.append(f"[bold]Timestamp[/bold] {timestamp}")
        if role:
            sections.append(f"[bold]Role[/bold] {role}")
        if tool:
            sections.append(f"[bold]Tool[/bold] {tool}")
        sections.append(f"[bold]Prompt[/bold]\n{prompt or '[dim]—[/dim]'}")
        sections.append(f"[bold]Response[/bold]\n{response or '[dim]—[/dim]'}")

        panels.append(
            Panel(
                "\n\n".join(sections),
                border_style="blue",
            )
        )

    console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _print_transcript_tail(result: Any) -> None:
    if not isinstance(result, dict):
        console.print(JSON.from_data(result))
        return

    events = result.get("events", [])
    if not events:
        console.print("[yellow]No new events[/yellow]")
        return
    panels = []
    for batch in events:
        turn_id = batch.get("turn", "")
        actor = batch.get("actor", "")
        clock = batch.get("clock")
        timestamp = batch.get("timestamp")

        sections = [
            f"[bold]Turn[/bold] {turn_id}",
            f"[bold]Actor[/bold] {actor}",
        ]
        if clock is not None:
            sections.append(f"[bold]Clock[/bold] {clock}")
        if timestamp:
            sections.append(f"[bold]Timestamp[/bold] {timestamp}")

        event_lines: List[str] = []
        for event in batch.get("events", []):
            action = (event.get("action") or "").upper()
            handle = event.get("handle", "")
            transcript = event.get("transcript")

            if isinstance(transcript, dict):
                sections_lines = [f"[bold]{action}[/bold] {handle}"]
                request_id = transcript.get("request_id")
                agent_name = transcript.get("agent")
                response_timestamp = transcript.get("response_timestamp")
                role = transcript.get("role")
                tool = transcript.get("tool")
                if request_id:
                    sections_lines.append(f"[bold]Request[/bold] {request_id}")
                if agent_name:
                    sections_lines.append(f"[bold]Agent[/bold] {agent_name}")
                if response_timestamp:
                    sections_lines.append(
                        f"[bold]Response Timestamp[/bold] {response_timestamp}"
                    )
                if role:
                    sections_lines.append(f"[bold]Role[/bold] {role}")
                if tool:
                    sections_lines.append(f"[bold]Tool[/bold] {tool}")

                prompt_text = transcript.get("prompt") or ""
                response_text = transcript.get("response") or ""
                sections_lines.append(
                    f"[bold]Prompt[/bold]\n{prompt_text or '[dim]—[/dim]'}"
                )
                sections_lines.append(
                    f"[bold]Response[/bold]\n{response_text or '[dim]—[/dim]'}"
                )
                event_lines.append("\n\n".join(sections_lines))
            else:
                value = event.get("value")
                line = f"[bold]{action}[/bold] {handle}"
                if value:
                    line += f"\n{_summarize_value(value, max_length=200)}"
                event_lines.append(line)

        if event_lines:
            sections.append("[bold]Events[/bold]\n" + "\n\n".join(event_lines))

        panels.append(
            Panel(
                "\n\n".join(sections),
                border_style="blue",
            )
        )

    console.print(Group(*panels) if len(panels) > 1 else panels[0])

    next_cursor = result.get("next_cursor")
    has_more = result.get("has_more")
    if next_cursor or has_more:
        footer = "[dim]"
        if next_cursor:
            footer += f"next cursor: {next_cursor}"
        if has_more:
            footer += " | more events available"
        footer += "[/dim]"
        console.print(footer)


def _print_reaction_register(result: Any) -> None:
    if isinstance(result, dict) and "reaction_id" in result:
        reaction_id = result.get("reaction_id", "")
        console.print(
            Panel(
                f"[bold green]Reaction registered[/bold green]\nID: {reaction_id}",
                border_style="green",
            )
        )
    else:
        console.print(JSON.from_data(result))


def _print_reaction_unregister(result: Any) -> None:
    if isinstance(result, dict) and "removed" in result:
        removed = result.get("removed")
        message = "Removed" if removed else "Nothing to remove"
        console.print(
            Panel(
                f"[bold]Reaction unregister[/bold]\n{message}",
                border_style="yellow" if not removed else "green",
            )
        )
    else:
        console.print(JSON.from_data(result))


def _print_reaction_list(result: Any) -> None:
    if not isinstance(result, dict) or "reactions" not in result:
        console.print(JSON.from_data(result))
        return

    reactions = result.get("reactions", [])
    if not reactions:
        console.print("[yellow]No reactions registered[/yellow]")
        return

    panels = []
    for entry in reactions:
        reaction_id = entry.get("reaction_id", "")
        actor = entry.get("actor", "")
        definition = entry.get("definition", {})
        pattern = definition.get("pattern", {})
        effect = definition.get("effect", {})

        pattern_expr = pattern.get("pattern", "")
        pattern_facet = pattern.get("facet", "")

        sections = [
            f"[bold]Reaction[/bold] {reaction_id}",
            f"[bold]Actor[/bold] {actor}",
            f"[bold]Pattern Facet[/bold] {pattern_facet}",
            f"[bold]Pattern[/bold]\n{pattern_expr}",
        ]

        effect_type = effect.get("type") if isinstance(effect, dict) else None
        if effect_type == "assert":
            value_info = effect.get("value", {})
            if isinstance(value_info, dict):
                value_type = value_info.get("type")
                if value_type == "literal":
                    sections.append(
                        "[bold]Effect[/bold]\nAssert literal: {}".format(
                            value_info.get("value", "")
                        )
                    )
                elif value_type == "match":
                    sections.append("[bold]Effect[/bold]\nAssert match value")
                elif value_type == "match-index":
                    sections.append(
                        "[bold]Effect[/bold]\nAssert match index {}".format(
                            value_info.get("index", "")
                        )
                    )
                else:
                    sections.append(
                        "[bold]Effect[/bold]\n" + json.dumps(value_info, indent=2)
                    )
            if effect.get("target_facet"):
                sections.append(
                    f"[bold]Target Facet[/bold] {effect.get('target_facet')}"
                )
        elif effect_type == "send-message":
            sections.append(
                "[bold]Effect[/bold]\nSend message to actor {actor} facet {facet}".format(
                    actor=effect.get("actor", ""),
                    facet=effect.get("facet", ""),
                )
            )
            payload_info = effect.get("payload", {})
            if isinstance(payload_info, dict):
                payload_type = payload_info.get("type")
                if payload_type == "literal":
                    sections.append(
                        "[bold]Payload[/bold]\n{}".format(payload_info.get("value", ""))
                    )
                elif payload_type == "match":
                    sections.append("[bold]Payload[/bold]\nMatch value")
                elif payload_type == "match-index":
                    sections.append(
                        "[bold]Payload[/bold]\nMatch index {}".format(
                            payload_info.get("index", "")
                        )
                    )
                else:
                    sections.append(
                        "[bold]Payload[/bold]\n" + json.dumps(payload_info, indent=2)
                    )
            else:
                sections.append(f"[bold]Payload[/bold]\n{payload_info}")
        else:
            sections.append("[bold]Effect[/bold]\n" + json.dumps(effect, indent=2))

        stats = entry.get("stats", {})
        if isinstance(stats, dict):
            trigger_count = stats.get("trigger_count", 0)
            last_trigger = stats.get("last_trigger", "-")
            last_error = stats.get("last_error")
            sections.append(f"[bold]Triggers[/bold] {trigger_count}")
            sections.append(f"[bold]Last Trigger[/bold] {last_trigger}")
            if last_error:
                sections.append(f"[bold]Last Error[/bold]\n{last_error}")

        panels.append(
            Panel(
                "\n\n".join(sections),
                border_style="blue",
            )
        )

    console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _format_uuidish(value: Any, length: int = 12) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        if "0" in value:
            text = str(value["0"])
        elif "uuid" in value:
            text = str(value["uuid"])
        else:
            text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)

    if len(text) > length:
        return f"{text[:length]}..."
    return text


def _summarize_value(value: Any, max_length: int = 80) -> str:
    if value is None:
        return "null"
    try:
        text = json.dumps(value, ensure_ascii=False)
    except TypeError:
        text = str(value)
    if len(text) > max_length:
        return f"{text[: max_length - 3]}..."
    return text


def _print_operation_result(result: Any, operation: str) -> None:
    if isinstance(result, dict):
        title = "[bold green]Success[/bold green]"
        subtitle = ""
        if "queued_turn" in result:
            subtitle = f"Queued turn {result['queued_turn']}"
        elif "entity_id" in result:
            subtitle = f"Entity {result['entity_id']}"

        console.print(
            Panel(
                JSON.from_data(result),
                title=title,
                subtitle=subtitle,
                border_style="green",
            )
        )
    else:
        console.print(Panel(str(result), title="Result", border_style="green"))


def _print_navigation_result(result: Any, operation: str) -> None:
    if isinstance(result, dict):
        console.print(
            Panel(
                JSON.from_data(result),
                title=f"[bold cyan]{operation.title()}[/bold cyan]",
                border_style="cyan",
            )
        )
    else:
        console.print(f"[green]{result}[/green]")


def _print_workspace_write(result: Any) -> None:
    _print_operation_result(result, "workspace:write")


def _print_result(result: Any, command: str) -> None:
    if command == "status":
        _print_status(result)
    elif command == "history":
        _print_history(result)
    elif command == "list-entities":
        _print_entities(result)
    elif command == "list-capabilities":
        _print_capabilities(result)
    elif command in ("goto", "back", "fork", "merge"):
        _print_navigation_result(result, command)
    elif command in ("send", "register-entity", "invoke-capability", "workspace:scan", "workspace:write", "raw"):
        _print_operation_result(result, command)
    elif command == "workspace:entries":
        _print_workspace_entries(result)
    elif command == "workspace:read":
        _print_workspace_read(result)
    elif command == "agent:invoke":
        _print_agent_invoke(result)
    elif command == "agent:responses":
        _print_agent_responses(result)
    elif command == "dataspace:assertions":
        _print_dataspace_assertions(result)
    elif command == "dataspace:events":
        _print_dataspace_events(result)
    elif command == "transcript:show":
        _print_transcript_show(result)
    elif command == "transcript:tail":
        _print_transcript_tail(result)
    elif command == "reaction:register":
        _print_reaction_register(result)
    elif command == "reaction:unregister":
        _print_reaction_unregister(result)
    elif command == "reaction:list":
        _print_reaction_list(result)
    else:
        console.print(JSON.from_data(result))


def _print_protocol_error(exc: ProtocolError) -> None:
    suffix = f" ({exc.code})" if getattr(exc, "code", None) else ""
    details = getattr(exc, "details", None)

    content = f"[bold]{exc}[/bold]"
    if details is not None:
        if isinstance(details, (dict, list)):
            content += f"\n\n[dim]Details:[/dim]\n{json.dumps(details, indent=2)}"
        else:
            content += f"\n\n[dim]Details:[/dim] {details}"

    console.print(
        Panel(
            content,
            title=f"[bold red]Protocol Error{suffix}[/bold red]",
            border_style="red",
        )
    )


def _print_launch_error(exc: FileNotFoundError) -> None:
    console.print(
        Panel(
            f"[bold]{exc}[/bold]\n\n[dim]Ensure codebased is installed or specify --codebased-bin.[/dim]",
            title="[bold red]Failed to Launch[/bold red]",
            border_style="red",
        )
    )


def _print_unexpected_error(exc: Exception) -> None:
    tb = traceback.format_exc()
    console.print(
        Panel(
            f"[bold]{exc}[/bold]\n\n[dim]{tb}[/dim]",
            title="[bold red]Unexpected Error[/bold red]",
            border_style="red",
        )
    )


@transcript_app.command("show")
def transcript_show(
    ctx: typer.Context,
    request_id: str = typer.Argument(..., help="Request identifier to inspect."),
    branch: Optional[str] = typer.Option(None, help="Branch to query."),
    limit: int = typer.Option(20, help="Maximum assertions to display."),
) -> None:
    """Show stored agent transcript data."""

    params: Dict[str, Any] = {
        "request_id": request_id,
        "limit": limit,
    }
    if branch:
        params["branch"] = branch

    _run(_run_call(ctx.obj, "transcript_show", params, "transcript:show"))


@transcript_app.command("tail")
def transcript_tail(
    ctx: typer.Context,
    request_id: str = typer.Argument(..., help="Request identifier to follow."),
    branch: str = typer.Option("main", help="Branch to follow."),
    follow: bool = typer.Option(True, help="Continue polling for new events."),
    interval: float = typer.Option(1.0, help="Polling interval when following.", min=0.1),
    limit: int = typer.Option(10, help="Maximum turns to return per poll."),
) -> None:
    """Tail agent transcript events for a request."""

    params: Dict[str, Any] = {
        "branch": branch,
        "request_id": request_id,
        "limit": limit,
    }
    _run(_run_transcript_tail(ctx.obj, params, follow, interval))


@transcript_app.command("export")
def transcript_export(
    ctx: typer.Context,
    request_id: str = typer.Argument(..., help="Request identifier to export."),
    branch: Optional[str] = typer.Option(None, help="Branch to query."),
    limit: int = typer.Option(50, help="Maximum transcript entries to include.", min=1),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write transcript to this file instead of stdout.",
    ),
) -> None:
    """Export transcript history as plain text."""

    _run(
        _run_transcript_export(
            ctx.obj,
            request_id=request_id,
            branch=branch,
            limit=limit,
            destination=output,
        )
    )


def main_entrypoint() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover - manual invocation
    main_entrypoint()
