"""Command-line interface for the Duet runtime using Typer + Rich."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import shutil
from collections import Counter
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple, NoReturn

import rich_click as click  # Must be imported before typer to patch Click
import typer
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.json import JSON
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.tree import Tree
from rich.text import Text

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

STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "your", "you", "have", "into", "about",
    "than", "then", "there", "their", "what", "when", "where", "which", "would",
    "could", "should", "shall", "will", "cant", "can't", "dont", "don't", "does",
    "doesn't", "did", "didn't", "over", "under", "once", "again", "ever", "never",
    "just", "more", "most", "less", "least", "also", "very", "much", "many", "some",
    "such", "into", "onto", "between", "while", "because", "since", "across", "after",
    "before", "around", "through", "ensure", "please", "help", "next",
    "hello", "thanks", "thank", "hi", "hey", "okay", "ok",
    "user", "assistant", "doing", "today",
    "i", "we", "me", "my", "mine", "our", "ours", "ourselves",
}

USER_PROMPT_RE = re.compile(r"(?is)user:\s*(.*?)(?=(?:\n\s*(?:assistant|system):|\Z))")
ASSISTANT_RESPONSE_RE = re.compile(r"(?is)assistant:\s*(.*?)(?=(?:\n\s*(?:user|system):|\Z))")

app = typer.Typer(
    add_completion=True,
    help="Interact with the Duet runtime through its NDJSON control protocol.",
    rich_markup_mode="rich",
)
chat_app = typer.Typer(
    help="Fire-and-forget chats with code agents.",
    add_completion=True,
    rich_markup_mode="rich",
)
run_app = typer.Typer(
    help="Execute workflows and other runtime actions.",
    add_completion=True,
    rich_markup_mode="rich",
)
codebased_app = typer.Typer(
    help="Manage the local codebased daemon.",
    add_completion=True,
    rich_markup_mode="rich",
)
time_app = typer.Typer(
    help="Navigate runtime time-travel features.",
    add_completion=True,
    rich_markup_mode="rich",
)
query_app = typer.Typer(
    help="Inspect runtime state, transcripts, and assertions.",
    add_completion=True,
    rich_markup_mode="rich",
)
actors_app = typer.Typer(
    help="Inspect active actors, their entities, and dataspace activity.",
    add_completion=True,
    rich_markup_mode="rich",
)
reaction_app = typer.Typer(
    help="Manage reactive automations attached to the runtime.",
    add_completion=True,
    rich_markup_mode="rich",
)
debug_app = typer.Typer(
    help="Advanced runtime and protocol tooling.",
    add_completion=True,
    rich_markup_mode="rich",
    hidden=True,
)
app.add_typer(chat_app, name="chat", rich_help_panel="Chat")
app.add_typer(run_app, name="run", rich_help_panel="Run")
app.add_typer(codebased_app, name="codebased", rich_help_panel="Codebased")
app.add_typer(time_app, name="time", rich_help_panel="Time")
app.add_typer(query_app, name="query", rich_help_panel="Query")
app.add_typer(debug_app, name="debug")
query_app.add_typer(actors_app, name="actors", rich_help_panel="Actors")
debug_app.add_typer(reaction_app, name="reaction")



DEFAULT_ROOT_NAME = ".duet"
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
    if root:
        return root.expanduser().resolve()

    cwd = Path.cwd()
    search_roots = [cwd, *cwd.parents]
    for candidate_parent in search_roots:
        candidate = candidate_parent / DEFAULT_ROOT_NAME
        if candidate.exists():
            return candidate.resolve()

    return (cwd / DEFAULT_ROOT_NAME).resolve()


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


def _stop_daemon_if_running(root: Path, *, quiet: bool = False) -> bool:
    state = _load_daemon_state(root)
    if not state:
        if not quiet:
            console.print("[yellow]Daemon is not running.[/yellow]")
        return False

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
    if not quiet:
        console.print("[green]Daemon stopped.[/green]")
    return True


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
    daemon_host: Optional[str]
    daemon_port: Optional[int]


def _show_group_help(ctx: typer.Context, examples: Optional[List[str]] = None) -> NoReturn:
    """Display help text (optionally with examples) and exit."""

    typer.echo(ctx.get_help())
    if examples:
        console.print("\nExamples:", style="bold")
        for example in examples:
            console.print(f"  [dim]{example}[/dim]")
    raise typer.Exit()


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
        help="Runtime root directory (defaults to the nearest .duet folder).",
        rich_help_panel="Global Options",
    ),
    codebased_bin: Optional[Path] = typer.Option(  # noqa: B008
        None,
        help="Path to the codebased daemon executable (overrides auto-discovery).",
        rich_help_panel="Global Options",
    ),
    daemon_host: Optional[str] = typer.Option(  # noqa: B008
        None,
        "--daemon-host",
        help="Connect to an existing daemon at this host (requires --daemon-port).",
        rich_help_panel="Global Options",
    ),
    daemon_port: Optional[int] = typer.Option(  # noqa: B008
        None,
        "--daemon-port",
        help="Connect to an existing daemon listening on this port.",
        min=1,
        max=65535,
        rich_help_panel="Global Options",
    ),
) -> None:
    """Top-level callback storing shared CLI state."""

    if (daemon_host is None) != (daemon_port is None):
        raise typer.BadParameter(
            "Provide both --daemon-host and --daemon-port to connect to a remote daemon."
        )

    ctx.obj = CLIState(
        root=root,
        codebased_bin=codebased_bin,
        daemon_host=daemon_host,
        daemon_port=daemon_port,
    )

    if ctx.invoked_subcommand is None:
        _show_group_help(ctx)


@time_app.command("status")
def status(
    ctx: typer.Context,
    branch: Optional[str] = typer.Option(  # noqa: B008
        None,
        help="Show status for a specific branch.",
    ),
) -> None:
    """Show runtime status."""

    _run(_run_status(ctx.obj, branch))


@time_app.command("history")
def history(
    ctx: typer.Context,
    branch: str = typer.Option("main", help="Branch name to inspect."),
    start: int = typer.Option(0, help="Starting index of the history slice."),
    limit: int = typer.Option(20, help="Number of turns to display."),
) -> None:
    """Show branch turn history."""

    params = {"branch": branch, "start": start, "limit": limit}
    _run(_run_call(ctx.obj, "history", params, "history"))


@debug_app.command("send")
def send(
    ctx: typer.Context,
    actor: str = typer.Argument(..., help="Target actor identifier (UUID)."),
    facet: str = typer.Argument(..., help="Target facet identifier (UUID)."),
    payload: str = typer.Argument(..., help="Message payload encoded as Preserves text."),
) -> None:
    """Send a message to an actor/facet."""

    _run(_run_send_message(ctx.obj, actor, facet, payload))


    params = {
        "actor": actor,
        "facet": facet,
        "entity_type": entity_type,
        "config": config,
    }
    _run(_run_call(ctx.obj, "register_entity", params, "register-entity"))


@debug_app.command("list-entities")
def list_entities(ctx: typer.Context, actor: Optional[str] = typer.Option(None, help="Filter by actor identifier (UUID).")) -> None:  # noqa: B008,E501
    """List registered entities."""

    params = {"actor": actor} if actor else {}
    _run(_run_call(ctx.obj, "list_entities", params, "list-entities"))


@debug_app.command("list-capabilities")
def list_capabilities(
    ctx: typer.Context,
    actor: Optional[str] = typer.Option(None, help="Filter by actor identifier (UUID)."),
) -> None:
    """List known capabilities."""

    params = {"actor": actor} if actor else {}
    _run(_run_call(ctx.obj, "list_capabilities", params, "list-capabilities"))


@time_app.command("goto")
def goto(
    ctx: typer.Context,
    turn_id: str = typer.Argument(..., help="Turn identifier to activate."),
    branch: Optional[str] = typer.Option(None, help="Switch to this branch before executing the command."),
) -> None:
    """Jump to a specific turn."""

    params: Dict[str, Any] = {"turn_id": turn_id}
    if branch:
        params["branch"] = branch
    _run(_run_call(ctx.obj, "goto", params, "goto"))


@time_app.command("back")
def back(
    ctx: typer.Context,
    count: int = typer.Option(1, help="Number of turns to rewind."),
    branch: Optional[str] = typer.Option(None, help="Switch to this branch before executing the command."),
) -> None:
    """Rewind the runtime by N turns."""

    params: Dict[str, Any] = {"count": count}
    if branch:
        params["branch"] = branch
    _run(_run_call(ctx.obj, "back", params, "back"))


@time_app.command("fork")
def fork(
    ctx: typer.Context,
    source: str = typer.Option("main", help="Source branch name."),
    new_branch: str = typer.Option(..., help="Name for the new branch."),
    from_turn: Optional[str] = typer.Option(None, help="Optional turn identifier to fork from."),
) -> None:
    """Fork a new branch."""

    params: Dict[str, Any] = {"source": source, "new_branch": new_branch}
    if from_turn:
        params["from_turn"] = from_turn
    _run(_run_call(ctx.obj, "fork", params, "fork"))


@time_app.command("merge")
def merge(
    ctx: typer.Context,
    source: str = typer.Option(..., help="Name of the branch to merge from."),
    target: str = typer.Option(..., help="Name of the branch to merge into."),
) -> None:
    """Merge a source branch into a target branch."""

    params = {"source": source, "target": target}
    _run(_run_call(ctx.obj, "merge", params, "merge"))


@debug_app.command("invoke-capability")
def invoke_capability(
    ctx: typer.Context,
    capability: str = typer.Argument(..., help="Capability identifier (UUID)."),
    payload: str = typer.Argument(..., help="Capability payload encoded as Preserves text."),
) -> None:
    """Invoke a capability by id."""

    _run(_run_invoke_capability(ctx.obj, capability, payload))


@debug_app.command("raw")
def raw(
    ctx: typer.Context,
    rpc_command: str = typer.Argument(..., help="Runtime RPC command to invoke."),
    params: str = typer.Argument("{}", help="JSON object describing command parameters."),
) -> None:
    """Send a raw command with JSON parameters."""

    payload = json_loads(params)
    if not isinstance(payload, dict):
        raise typer.BadParameter("Params must decode to a JSON object")
    _run(_run_call(ctx.obj, rpc_command, payload, "raw"))


@debug_app.command("workspace-entries")
def workspace_entries(ctx: typer.Context) -> None:
    """List workspace dataspace entries."""

    _run(_run_call(ctx.obj, "workspace_entries", {}, "workspace:entries"))


@debug_app.command("agent-invoke")
def agent_invoke(
    ctx: typer.Context,
    prompt: str = typer.Argument(..., help="Prompt text for the agent."),
    agent: str = typer.Option(
        "claude-code",
        "--agent",
        help="Agent kind to invoke (e.g., 'claude-code', 'codex', 'noface').",
        show_default=True,
    ),
) -> None:
    """Queue a prompt for a configured agent."""

    params = {"prompt": prompt, "agent": agent}
    _run(_run_call(ctx.obj, "agent_invoke", params, "agent:invoke"))


@query_app.command("responses")
def agent_responses(
    ctx: typer.Context,
    request_id: Optional[str] = typer.Option(None, help="Only include responses for this request identifier."),
    wait: float = typer.Option(0.0, help="Seconds to wait for new responses before returning.", min=0.0),
    limit: Optional[int] = typer.Option(None, help="Maximum number of responses to return.", min=1),
    select: bool = typer.Option(
        False,
        "--select",
        help="Interactively choose a request identifier to filter responses.",
    ),
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        help="Agent kind to query (e.g., 'claude-code', 'codex', 'noface').",
    ),
) -> None:
    """List cached agent responses."""

    if select and request_id:
        raise typer.BadParameter("--select cannot be combined with --request-id")
    if select:
        request_id = _choose_request_id(
            ctx.obj,
            title="Agent Requests",
            agent=agent,
        )
        if not request_id:
            console.print("[yellow]No request selected; showing all responses.[/yellow]")

    params: Dict[str, Any] = {}
    if request_id:
        params["request_id"] = request_id
    params["wait_ms"] = int(wait * 1000)
    if limit is not None:
        params["limit"] = limit
    if agent:
        params["agent"] = agent
    _run(_run_call(ctx.obj, "agent_responses", params, "agent:responses"))


@chat_app.callback(invoke_without_command=True)
def chat(
    ctx: typer.Context,
    prompt: Optional[str] = typer.Argument(None, help="Prompt text to send to the agent."),
    wait_for_response: bool = typer.Option(
        False,
        "--wait-for-response",
        help="Block until at least one response is recorded.",
    ),
    wait: float = typer.Option(
        0.0,
        help="Extra seconds to wait after the first response arrives (requires --wait-for-response).",
        min=0.0,
    ),
    resume_request_id: Optional[str] = typer.Option(
        None,
        "--resume",
        help="Request identifier whose transcript should seed the conversation context.",
    ),
    resume_select: bool = typer.Option(
        False,
        "--resume-select",
        help="Interactively choose a request identifier to seed conversation context.",
    ),
    continue_last: bool = typer.Option(
        False,
        "--continue",
        help="Resume the most recent agent conversation.",
    ),
    history_limit: int = typer.Option(
        10,
        "--history-limit",
        help="Transcript entries to include when using --resume.",
        min=1,
        show_default=True,
    ),
    agent: str = typer.Option(
        "claude-code",
        "--agent",
        help="Agent kind to use (e.g., 'claude-code', 'codex', 'noface').",
        show_default=True,
    ),
    inspect: bool = typer.Option(
        False,
        "--inspect",
        help="Show full invocation metadata before returning.",
    ),
) -> None:
    """Send a prompt to the agent. Combine with --wait-for-response to block for results."""

    if ctx.invoked_subcommand:
        return

    if prompt is None:
        _show_group_help(
            ctx,
            examples=[
                "duet chat \"Outline the deployment plan\"",
                "duet chat --agent codex \"Summarise src/lib.rs\"",
                "duet chat --wait-for-response \"What failed in CI?\"",
            ],
        )

    if not agent.strip():
        raise typer.BadParameter("--agent cannot be empty")

    message = prompt or typer.prompt("Prompt")

    resume_id = resume_request_id
    if continue_last:
        if resume_request_id or resume_select:
            raise typer.BadParameter("--continue cannot be combined with --resume or --resume-select")
        resume_id = _latest_request_id(ctx.obj, agent=agent)
        if resume_id:
            preview = _preview_text(_recent_prompt_for_request(ctx.obj, resume_id, agent=agent))
            label = f"[bold]{_short_id(resume_id)}[/bold]"
            if preview:
                label += f" · {preview}"
            console.print(f"[dim]Continuing conversation {label}[/dim]")
            console.print(f"[dim]Full request id: {resume_id}[/dim]")
        else:
            console.print("[yellow]No prior conversations found; starting a new one.[/yellow]")
    if resume_select:
        if resume_request_id:
            raise typer.BadParameter("--resume-select cannot be combined with --resume")
        resume_id = _choose_request_id(
            ctx.obj,
            title="Select request to resume",
            agent=agent,
        )
        if not resume_id:
            console.print("[yellow]No request selected; starting a fresh conversation.[/yellow]")

    _run(
        _run_agent_chat(
            ctx.obj,
            message,
            wait_for_response,
            wait,
            resume_id,
            history_limit,
            agent,
            inspect,
        )
    )


@run_app.callback(invoke_without_command=True)
def run_group(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_group_help(
            ctx,
            examples=[
                "duet run workflow-start pipelines/deploy.yaml",
                "duet run workflow-start pipelines/deploy.yaml --label staged",
            ],
        )


@codebased_app.callback(invoke_without_command=True)
def codebased_group(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_group_help(
            ctx,
            examples=[
                "duet codebased start --host 127.0.0.1",
                "duet codebased status",
                "duet codebased stop",
            ],
        )


@time_app.callback(invoke_without_command=True)
def time_group(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_group_help(
            ctx,
            examples=[
                "duet time status",
                "duet time history --branch feature/login",
                "duet time goto turn_abcd1234 --branch main",
            ],
        )


@query_app.callback(invoke_without_command=True)
def query_group(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_group_help(
            ctx,
            examples=[
                "duet query actors",
                "duet query responses --request-id <uuid>",
                "duet query transcript-tail --request-id <uuid>",
                "duet query workflows",
            ],
        )


@reaction_app.callback(invoke_without_command=True)
def reaction_group(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_group_help(ctx)


@actors_app.callback(invoke_without_command=True)
def actors_root(
    ctx: typer.Context,
    actor: Optional[str] = typer.Option(
        None,
        help="Filter to a specific actor identifier (UUID).",
    ),
    include_assertions: bool = typer.Option(
        False,
        "--include-assertions",
        help="Fetch dataspace assertions for each actor.",
    ),
    assertions_limit: int = typer.Option(
        5,
        "--assertions-limit",
        min=0,
        help="Maximum assertions to display per actor when including assertions (0 shows all returned).",
    ),
) -> None:
    """Show active actors along with their entities and faceted dataspace activity."""

    if ctx.invoked_subcommand:
        return

    _run(
        _run_query_actors(
            ctx.obj,
            actor.strip() if actor else None,
            include_assertions,
            assertions_limit,
        )
    )


@reaction_app.command("register")
def reaction_register(
    ctx: typer.Context,
    actor: str = typer.Option(..., help="Actor identifier (UUID) that owns the reaction."),
    facet: str = typer.Option(..., help="Facet identifier (UUID) that scopes the pattern."),
    pattern: str = typer.Option(..., help="Pattern expression encoded as Preserves text."),
    effect: str = typer.Option(
        "assert",
        help="Reaction effect type (assert or send-message).",
        case_sensitive=False,
        show_default=True,
    ),
    value: Optional[str] = typer.Option(None, help="Literal Preserves value to assert (for assert effect)."),
    value_from_match: bool = typer.Option(
        False,
        help="Use the entire matched value as the asserted value (assert effect).",
    ),
    value_match_index: Optional[int] = typer.Option(
        None,
        help="Use the Nth element from the matched record or sequence as the asserted value.",
        min=0,
    ),
    target_facet: Optional[str] = typer.Option(None, help="Facet identifier to attach the asserted value to."),
    target_actor: Optional[str] = typer.Option(None, help="Destination actor identifier for send-message."),
    target_facet_msg: Optional[str] = typer.Option(None, help="Destination facet identifier for send-message."),
    payload: Optional[str] = typer.Option(None, help="Literal Preserves payload for send-message effect."),
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
    reaction_id: str = typer.Argument(..., help="Reaction identifier to remove (UUID)."),
) -> None:
    """Unregister a reaction."""

    params = {"reaction_id": reaction_id}
    _run(_run_call(ctx.obj, "reaction_unregister", params, "reaction:unregister"))


@reaction_app.command("list")
def reaction_list(ctx: typer.Context) -> None:
    """List registered reactions."""

    _run(_run_call(ctx.obj, "reaction_list", {}, "reaction:list"))


@debug_app.command("dataspace-assertions")
def dataspace_assertions(
    ctx: typer.Context,
    actor: Optional[str] = typer.Option(None, help="Filter by actor identifier (UUID)."),
    label: Optional[str] = typer.Option(None, help="Filter by record label or symbol."),
    request_id: Optional[str] = typer.Option(
        None, help="Only include assertions whose first field matches this request identifier."
    ),
    limit: Optional[int] = typer.Option(None, help="Maximum number of assertions to return."),
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


@codebased_app.command("start")
def daemon_start(
    ctx: typer.Context,
    host: str = typer.Option(DEFAULT_DAEMON_HOST, help="Host interface for the daemon to bind."),
    port: Optional[int] = typer.Option(None, help="Port to listen on (auto-select if omitted)."),
) -> None:
    """Start the local codebased daemon in the background."""

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


@codebased_app.command("stop")
def daemon_stop(ctx: typer.Context) -> None:
    """Stop the background daemon if it is running."""

    root = _resolve_root_path(ctx.obj.root)
    _stop_daemon_if_running(root)


@codebased_app.command("status")
def daemon_status(ctx: typer.Context) -> None:
    """Report the status of the local codebased daemon."""

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


@app.command("clear")
def clear_runtime(
    ctx: typer.Context,
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip confirmation and clear runtime state immediately.",
    ),
) -> None:
    """Stop the daemon and delete all local runtime state."""

    root = _resolve_root_path(ctx.obj.root)
    root_display = str(root)

    if not force:
        warning = Panel(
            "[bold red]This will stop the codebased daemon and delete all cached runtime state.[/bold red]\n\n"
            f"Folder: [bold]{root_display}[/bold]\n\n"
            "Proceed only if you are sure you want a clean slate.",
            border_style="red",
            title="[bold red]Danger Zone[/bold red]",
        )
        console.print(warning)
        try:
            proceed = typer.confirm("Proceed with clearing the runtime?", default=False)
        except typer.Abort:
            console.print("[yellow]Aborted; runtime state left intact.[/yellow]")
            raise typer.Exit(1)
        if not proceed:
            console.print("[yellow]Aborted; runtime state left intact.[/yellow]")
            return

    daemon_stopped = _stop_daemon_if_running(root, quiet=True)

    removed = False
    if root.exists():
        try:
            shutil.rmtree(root)
            removed = True
        except Exception as exc:
            console.print(
                Panel(
                    f"[bold red]Failed to remove {root_display}[/bold red]\n\n{exc}",
                    border_style="red",
                )
            )
            raise typer.Exit(1)

    if daemon_stopped:
        console.print("[dim]Stopped running daemon before clearing state.[/dim]")
    if removed:
        console.print(f"[green]Cleared runtime state at {root_display}.[/green]")
    else:
        console.print(f"[green]No runtime state found at {root_display}; nothing to remove.[/green]")


@query_app.command("dataspace-tail")
def dataspace_tail(
    ctx: typer.Context,
    branch: str = typer.Option("main", help="Branch name to inspect."),
    since: Optional[str] = typer.Option(None, help="Start streaming after this turn identifier."),
    limit: int = typer.Option(10, help="Number of events to return per request.", min=1),
    actor: Optional[str] = typer.Option(None, help="Filter by actor identifier (UUID)."),
    label: Optional[str] = typer.Option(None, help="Filter by record label or symbol."),
    request_id: Optional[str] = typer.Option(None, help="Only include events whose first field matches this request identifier."),
    request_select: bool = typer.Option(
        False,
        "--request-select",
        help="Interactively choose a request identifier to filter events.",
    ),
    event_type: List[str] = typer.Option([], "--event-type", "-e", help="Restrict to specific event types (assert or retract)."),
    follow: bool = typer.Option(False, help="Continue polling for new events."),
    interval: float = typer.Option(1.0, help="Polling interval in seconds when following.", min=0.1),
) -> None:
    """Tail assertion events from the dataspace."""

    if request_select and request_id:
        raise typer.BadParameter("--request-select cannot be combined with --request-id")
    if request_select:
        request_id = _choose_request_id(ctx.obj, title="Select request to filter")
        if not request_id:
            console.print("[yellow]No request selected; streaming without request filter.[/yellow]")

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


async def _workflow_start_command(
    state: CLIState, params: Dict[str, Any], interactive: bool
) -> None:
    client = await _connect_client(state)
    try:
        result = await client.call("workflow_start", params)
        if not interactive:
            _print_workflow_start(result)
            return

        if not isinstance(result, dict):
            _print_workflow_start(result)
            console.print(
                "[red]Interactive mode requires definition metadata; falling back to batch output.[/red]"
            )
            return

        instance = result.get("instance")
        if not isinstance(instance, dict):
            _print_workflow_start(result)
            console.print(
                "[red]Workflow start response did not include instance details; cannot enter interactive mode.[/red]"
            )
            return

        instance_id = instance.get("id")
        if not isinstance(instance_id, str):
            _print_workflow_start(result)
            console.print(
                "[red]Workflow start response missing instance id; cannot enter interactive mode.[/red]"
            )
            return

        console.print(
            Panel(
                f"Interactive session attached to workflow [bold]{instance_id}[/bold]. Press Ctrl+C to exit.",
                border_style="cyan",
            )
        )

        await _workflow_interactive_loop(client, instance_id)
    finally:
        await client.close()


async def _workflow_interactive_loop(client: ControlClient, instance_id: str) -> None:
    refresh_interval = 0.5

    last_instance: Dict[str, Any] = {}

    try:
        with Live(refresh_per_second=4, console=console) as live:
            while True:
                result = await client.call("workflow_follow", {"instance_id": instance_id})
                if not isinstance(result, dict):
                    live.stop()
                    _print_result(result, "workflow:follow")
                    break

                instance = result.get("instance") or {}
                last_instance = instance
                prompts = result.get("prompts") or []

                live.update(_render_workflow_view(instance, prompts))

                status = (instance.get("status") or {}).get("state")
                if status in {"completed", "failed"}:
                    break

                if status == "waiting":
                    wait_info = (instance.get("status") or {}).get("wait") or {}
                    if wait_info.get("type") == "user-input":
                        prompt_entry = prompts[0] if prompts else wait_info
                        live.stop()
                        response = await _prompt_for_input(prompt_entry)
                        if response is None:
                            console.print("[yellow]Interactive session aborted by user.[/yellow]")
                            break

                        request_id = prompt_entry.get("request_id")
                        if not request_id:
                            console.print(
                                "[red]Prompt is missing a request id; cannot submit response.[/red]"
                            )
                            break

                        await client.call(
                            "workflow_input",
                            {
                                "instance_id": instance_id,
                                "request_id": request_id,
                                "response": response,
                            },
                        )

                        live.start()
                        continue

                await asyncio.sleep(refresh_interval)
    except KeyboardInterrupt:
        console.print("[yellow]Interactive session interrupted by user.[/yellow]")
        return

    if last_instance:
        status = (last_instance.get("status") or {}).get("state", "-")
        console.print(
            Panel(
                f"Workflow [bold]{instance_id}[/bold] finished with status [bold]{status}[/bold].",
                border_style="cyan",
            )
        )


async def _prompt_for_input(prompt_entry: Dict[str, Any]) -> Optional[str]:
    header = f"Prompt {prompt_entry.get('request_id', '?')}"
    tag = prompt_entry.get("tag")
    if tag:
        header += f" ({tag})"

    prompt_json = prompt_entry.get("prompt")
    if prompt_json is not None:
        body = JSON.from_data(prompt_json)
    else:
        body = Text(prompt_entry.get("summary") or "(no details)")

    console.print(Panel(body, title=header, border_style="cyan"))
    console.print("[dim]Leave empty to cancel.[/dim]")

    loop = asyncio.get_event_loop()

    def _read_input() -> Optional[str]:
        try:
            return console.input("[bold green]Response> [/bold green]")
        except (KeyboardInterrupt, EOFError):
            return None

    response = await loop.run_in_executor(None, _read_input)
    if response is None:
        return None

    response = response.strip()
    if not response:
        return None

    if response.lower() in {":q", ":quit", ":exit"}:
        return None

    return response


def _render_workflow_view(instance: Dict[str, Any], prompts: List[Dict[str, Any]]) -> Group:
    status_panel = _render_instance_panel(instance)
    prompt_panel = _render_prompt_panel(prompts)
    return Group(status_panel, prompt_panel)


def _render_instance_panel(instance: Dict[str, Any]) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_row("Program", instance.get("program_name", "-"))
    table.add_row("Instance", instance.get("id", "-"))

    status = instance.get("status") or {}
    status_text = status.get("state", "-")
    wait_info = status.get("wait") or {}
    if status_text == "waiting" and wait_info.get("type") == "user-input":
        summary = wait_info.get("summary") or wait_info.get("label") or "user input"
        status_text = f"waiting ({summary})"
    table.add_row("Status", status_text)

    if instance.get("state"):
        table.add_row("State", instance["state"])

    progress = instance.get("progress") or {}
    if progress:
        pending = "yes" if progress.get("entry_pending") else "no"
        table.add_row("Entry Pending", pending)
        table.add_row("Frame Depth", str(progress.get("frame_depth", 0)))

    return Panel(table, title="Workflow", border_style="cyan")


def _render_prompt_panel(prompts: List[Dict[str, Any]]) -> Panel:
    if not prompts:
        return Panel("[dim]No pending prompts[/dim]", title="Prompts", border_style="cyan")

    rows = []
    for prompt in prompts:
        request_id = prompt.get("request_id", "?")
        tag = prompt.get("tag")
        summary = prompt.get("summary") or "(no summary)"
        label = f"{request_id}"
        if tag:
            label += f" [{tag}]"
        rows.append(f"• [bold]{label}[/bold]\n  {summary}")

    body = Text("\n".join(rows))
    return Panel(body, title="Prompts", border_style="cyan")


async def _run_agent_chat(
    state: CLIState,
    prompt: str,
    wait_for_response: bool,
    extra_wait: float,
    resume_request_id: Optional[str],
    history_limit: int,
    agent: str,
    inspect: bool,
) -> None:
    client = await _connect_client(state)
    try:
        final_prompt = prompt
        if resume_request_id:
            final_prompt = await _augment_prompt_with_history(
                client, prompt, resume_request_id, history_limit
            )

        invoke_params: Dict[str, Any] = {"prompt": final_prompt, "agent": agent}
        invoke_result = await client.call("agent_invoke", invoke_params)
        if not isinstance(invoke_result, dict):
            _print_result(invoke_result, "agent:invoke")
            return

        if prompt is not None:
            invoke_result.setdefault("prompt_preview", prompt)

        if inspect:
            _print_result(invoke_result, "agent:invoke")

        if wait_for_response:
            _print_chat_waiting(invoke_result, agent)
        else:
            _print_chat_ack(invoke_result, agent, show_hint=not inspect)

        request_id = invoke_result.get("request_id")
        if not request_id:
            return

        if not wait_for_response:
            if extra_wait > 0:
                console.print(
                    "[yellow]Ignoring --wait value because --wait-for-response was not supplied.[/yellow]"
                )
            return

        params: Dict[str, Any] = {"request_id": request_id, "wait_ms": 0, "agent": agent}

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


async def _run_query_actors(
    state: CLIState,
    actor_filter: Optional[str],
    include_assertions: bool,
    assertions_limit: int,
) -> None:
    client = await _connect_client(state)
    try:
        params: Dict[str, Any] = {}
        if actor_filter:
            params["actor"] = actor_filter

        entities_result = await client.call("list_entities", params)
        if not isinstance(entities_result, dict):
            console.print(JSON.from_data(entities_result))
            return

        raw_entities = entities_result.get("entities") or []
        summaries: Dict[str, Dict[str, Any]] = {}

        def ensure_summary(actor_id: str) -> Dict[str, Any]:
            summary = summaries.get(actor_id)
            if summary is None:
                summary = {
                    "entities": [],
                    "entity_types": Counter(),
                    "facets": set(),
                    "assertions": [],
                }
                summaries[actor_id] = summary
            return summary

        for entry in raw_entities:
            if not isinstance(entry, dict):
                continue
            actor_id = str(entry.get("actor") or "").strip()
            if not actor_id:
                continue
            summary = ensure_summary(actor_id)
            summary["entities"].append(entry)
            entity_type = str(entry.get("entity_type") or "").strip()
            if entity_type:
                summary["entity_types"][entity_type] += 1
            facet = str(entry.get("facet") or "").strip()
            if facet:
                summary["facets"].add(facet)

        if actor_filter and actor_filter not in summaries:
            ensure_summary(actor_filter)

        if include_assertions:
            limit_param = None if assertions_limit == 0 else assertions_limit
            target_actor_ids = [actor_filter] if actor_filter else list(summaries.keys())

            if not target_actor_ids and not actor_filter:
                fetch_params: Dict[str, Any] = {}
                if limit_param:
                    fetch_params["limit"] = limit_param
                assertions_result = await client.call("dataspace_assertions", fetch_params)
                if isinstance(assertions_result, dict):
                    for assertion in assertions_result.get("assertions") or []:
                        if not isinstance(assertion, dict):
                            continue
                        actor_id = str(assertion.get("actor") or "").strip()
                        if not actor_id:
                            continue
                        summary = ensure_summary(actor_id)
                        summary["assertions"].append(assertion)
                target_actor_ids = list(summaries.keys())

            for actor_id in target_actor_ids:
                if not actor_id:
                    continue
                assertion_params: Dict[str, Any] = {"actor": actor_id}
                if limit_param:
                    assertion_params["limit"] = limit_param
                assertions_result = await client.call("dataspace_assertions", assertion_params)
                if isinstance(assertions_result, dict):
                    assertions = [
                        item
                        for item in assertions_result.get("assertions") or []
                        if isinstance(item, dict)
                    ]
                    ensure_summary(actor_id)["assertions"] = assertions

        if not summaries:
            console.print("[yellow]No actors found.[/yellow]")
            return

        panels = [
            _render_actor_summary(actor_id, summary, include_assertions, assertions_limit)
            for actor_id, summary in sorted(summaries.items(), key=lambda item: item[0])
        ]
        console.print(Group(*panels) if len(panels) > 1 else panels[0])
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
                "[yellow]Combined prompt exceeds 16k characters; the agent may truncate context.[/yellow]",
                border_style="yellow",
            )
        )
    return combined_prompt


async def _connect_client(state: CLIState) -> ControlClient:
    runtime_addr: Optional[Tuple[str, int]] = None
    root = _resolve_root_path(state.root)

    if state.daemon_host and state.daemon_port:
        runtime_addr = (state.daemon_host, state.daemon_port)
    else:
        daemon_state = _load_daemon_state(root)
        if daemon_state:
            if _is_process_alive(daemon_state.pid) and _ping_daemon(daemon_state.host, daemon_state.port):
                runtime_addr = (daemon_state.host, daemon_state.port)
            else:
                _clear_daemon_state(root)

    if runtime_addr:
        client = ControlClient(runtime_addr=runtime_addr)
    else:
        cmd = list(_codebased_command(state))
        cmd.extend(["--root", str(root)])
        client = ControlClient(tuple(cmd))
    await client.connect()
    return client


async def _fetch_recent_requests(
    state: CLIState, limit: int, agent: Optional[str] = None
) -> List[Dict[str, Any]]:
    client = await _connect_client(state)
    try:
        params: Dict[str, Any] = {"limit": limit, "wait_ms": 0}
        if agent:
            params["agent"] = agent
        result = await client.call("agent_responses", params)
    finally:
        await client.close()

    if isinstance(result, dict):
        responses = result.get("responses")
        if isinstance(responses, list):
            return responses
    return []


def _choose_request_id(
    state: CLIState, *, title: str, limit: int = 20, agent: Optional[str] = None
) -> Optional[str]:
    try:
        responses = asyncio.run(_fetch_recent_requests(state, limit, agent))
    except ProtocolError as exc:  # pragma: no cover - interactive path
        _print_protocol_error(exc)
        return None
    except FileNotFoundError as exc:  # pragma: no cover - interactive path
        _print_launch_error(exc)
        return None
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        raise typer.Exit(130)
    except Exception as exc:  # pragma: no cover - safety net
        _print_unexpected_error(exc)
        return None

    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in responses:
        request_id = str(entry.get("request_id", "")).strip()
        if not request_id or request_id in seen:
            continue
        seen.add(request_id)
        unique.append(entry)

    if not unique:
        console.print("[yellow]No agent requests found.[/yellow]")
        return None

    max_width = console.width if console.width else 80
    idx_col_width = max(len(str(len(unique))), 2)
    truncated = [str(entry.get("request_id", "")) for entry in unique]

    for idx, (entry, full_rid) in enumerate(zip(unique, truncated), start=1):
        agent = entry.get("agent", "agent")
        timestamp_raw = entry.get("timestamp", "")
        timestamp = _format_timestamp(timestamp_raw) or "-"
        short_rid = _short_id(full_rid)
        tags = _extract_keywords([_clean_user_message(entry.get("prompt"))])
        tag_text = _format_tags(tags) or "(no tags)"
        console.print(Panel.fit(
            Group(
                Text.assemble(("Request: ", "dim"), (short_rid or "-", "yellow")),
                Text.assemble(("Agent: ", "dim"), (agent, "magenta")),
                Text.assemble(("Started: ", "dim"), (timestamp, "white")),
                Text.assemble(("Tags: ", "dim"), (tag_text, "white")),
                Text.assemble(("Full ID: ", "dim"), (full_rid, "dim")),
            ),
            title=f"Option {idx}",
            border_style="blue",
            box=box.ROUNDED,
        ))

    while True:
        choice = typer.prompt("Select request (blank to cancel)", default="").strip()
        if choice == "":
            return None
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(unique):
                return str(unique[index - 1].get("request_id"))
        console.print(f"[yellow]Enter a number between 1 and {len(unique)}, or press Enter to cancel.[/yellow]")


def _metadata_block(pairs: Iterable[Tuple[str, Optional[str]]]) -> Optional[Group]:
    lines: List[Text] = []
    for label, value in pairs:
        if not value:
            continue
        value_style = "dim" if label.lower() in {"full id", "full request"} else "white"
        lines.append(Text.assemble((f"{label}: ", "dim"), (str(value), value_style)))
    if not lines:
        return None
    return Group(*lines)


def _latest_request_id(state: CLIState, agent: Optional[str] = None) -> Optional[str]:
    try:
        responses = asyncio.run(_fetch_recent_requests(state, 1, agent))
    except Exception:
        return None
    if responses:
        return str(responses[0].get("request_id"))
    return None


def _recent_prompt_for_request(
    state: CLIState, request_id: str, agent: Optional[str] = None
) -> Optional[str]:
    try:
        responses = asyncio.run(_fetch_recent_requests(state, 20, agent))
    except Exception:
        return None
    for entry in responses:
        if str(entry.get("request_id")) == request_id:
            prompt = entry.get("prompt")
            if isinstance(prompt, str):
                return prompt
    return None


@debug_app.command("agent-requests")
def debug_agent_requests(
    ctx: typer.Context,
    limit: int = typer.Option(20, help="Maximum number of recent requests to display.", min=1),
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        help="Agent kind to inspect (e.g., 'claude-code', 'codex', 'noface').",
    ),
) -> None:
    """List recent agent request identifiers with full metadata."""

    try:
        responses = asyncio.run(_fetch_recent_requests(ctx.obj, limit, agent))
    except ProtocolError as exc:
        _print_protocol_error(exc)
        raise typer.Exit(1)
    except FileNotFoundError as exc:
        _print_launch_error(exc)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except Exception as exc:
        _print_unexpected_error(exc)
        raise typer.Exit(1)

    if not responses:
        console.print("[yellow]No agent requests recorded yet.[/yellow]")
        return

    for idx, entry in enumerate(responses, start=1):
        request_id = str(entry.get("request_id", ""))
        agent = entry.get("agent", "agent")
        timestamp_raw = entry.get("timestamp", "")
        timestamp = _format_timestamp(timestamp_raw) or "-"
        prompt = entry.get("prompt")
        clean_prompt = _clean_user_message(prompt)
        tags = _extract_keywords([clean_prompt])

        metadata = _metadata_block([
            ("Request", _short_id(request_id)),
            ("Full ID", request_id),
            ("Agent", agent),
            ("Timestamp", timestamp),
        ])

        body_parts: List[Any] = [metadata] if metadata else []
        tag_line = _format_tags(tags)
        if tag_line:
            body_parts.append(Text("Tags: " + tag_line, style="dim"))
        else:
            body_parts.append(Text("Tags: (none)", style="dim"))
        body = Group(*body_parts) if len(body_parts) > 1 else body_parts[0]

        console.print(Panel(body, border_style="blue", box=box.ROUNDED, title=f"Request {idx}"))


def _message_panel(label: str, content: Optional[str], *, border_style: str, subtitle: Optional[str] = None) -> Panel:
    if content and content.strip():
        body = Markdown(content, code_theme="monokai")
    else:
        body = Text("No content", style="dim")
    return Panel(
        body,
        title=f"[bold]{label}[/bold]",
        subtitle=subtitle,
        border_style=border_style,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _render_chat_exchange(
    prompt: Optional[str],
    response: Optional[str],
    *,
    user_label: str = "You",
    assistant_label: str = "Assistant",
    metadata: Optional[List[Tuple[str, Optional[str]]]] = None,
    tags: Optional[List[str]] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    assistant_role: Optional[str] = None,
    tool: Optional[str] = None,
    border_style: str = "blue",
) -> Panel:
    renderables: List[Any] = []

    meta_renderable = _metadata_block(metadata or [])
    if meta_renderable is not None:
        renderables.append(meta_renderable)

    if tags:
        tag_line = Text("Tags: " + ", ".join(tags), style="dim")
        renderables.append(tag_line)

    cleaned_prompt = _clean_user_message(prompt)
    if cleaned_prompt:
        renderables.append(_message_panel(user_label, cleaned_prompt, border_style="cyan"))

    cleaned_response = _clean_assistant_message(response)
    if cleaned_response is not None:
        label = assistant_label or "Assistant"
        if assistant_role and assistant_role.lower() not in {"assistant", "agent"}:
            label = f"{label} ({assistant_role})"
        if tool:
            label = f"{label} · {tool}"

        assistant_border = "magenta" if assistant_role and assistant_role.lower() == "tool" else "green"
        renderables.append(_message_panel(label, cleaned_response, border_style=assistant_border))

    if not renderables:
        renderables.append(Text("No conversation content", style="dim"))

    body = Group(*renderables) if len(renderables) > 1 else renderables[0]
    formatted_subtitle = _format_timestamp(subtitle)
    return Panel(body, border_style=border_style, box=box.ROUNDED, title=title, subtitle=formatted_subtitle, padding=(0, 1))


def _preview_text(text: Optional[str], length: int = 60) -> Optional[str]:
    if not text:
        return None
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= length:
        return collapsed
    return collapsed[: length - 1] + "…"


def _short_id(value: Optional[str], length: int = 8) -> Optional[str]:
    if not value:
        return None
    value = str(value)
    if len(value) <= length:
        return value
    return value[:length] + "…"


def _format_timestamp(timestamp: Optional[str]) -> Optional[str]:
    if not timestamp or not isinstance(timestamp, str):
        return None
    ts = timestamp.strip()
    if not ts:
        return None
    candidates = []
    if ts.endswith("Z"):
        candidates.append((ts[:-1] + "+0000", "%Y-%m-%dT%H:%M:%S.%f%z"))
        candidates.append((ts[:-1] + "+0000", "%Y-%m-%dT%H:%M:%S%z"))
        candidates.append((ts[:-1] + "+00:00", "%Y-%m-%dT%H:%M:%S.%f%z"))
        candidates.append((ts[:-1] + "+00:00", "%Y-%m-%dT%H:%M:%S%z"))
        candidates.append((ts, "%Y-%m-%dT%H:%M:%S.%fZ"))
        candidates.append((ts, "%Y-%m-%dT%H:%M:%S%Z"))
    candidates.append((ts, "%Y-%m-%dT%H:%M:%S.%f%z"))
    candidates.append((ts, "%Y-%m-%dT%H:%M:%S%z"))
    candidates.append((ts, "%Y-%m-%d %H:%M:%S"))

    for candidate, fmt in candidates:
        try:
            dt = datetime.strptime(candidate, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            local_dt = dt.astimezone()
            return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        except ValueError:
            continue
    if "T" in ts:
        simplified = ts.rstrip("Z").split("T")
        if len(simplified) == 2:
            date_part, time_part = simplified
            time_part = time_part.split(".")[0]
            return f"{date_part} {time_part} UTC"
    return timestamp


def _extract_keywords(texts: Iterable[Optional[str]], limit: int = 3) -> List[str]:
    scores: Dict[str, float] = {}
    for text in texts:
        if not text:
            continue
        snippet = text
        user_matches = list(re.finditer(r"\buser\s*:\s*", text, flags=re.IGNORECASE))
        if user_matches:
            snippet = text[user_matches[-1].end():]
        assistant_boundary = re.search(r"\bassistant\s*:\s*", snippet, flags=re.IGNORECASE)
        if assistant_boundary:
            snippet = snippet[:assistant_boundary.start()]
        cleaned = re.sub(r"\b(?:user|assistant)\s*:\s*", " ", snippet, flags=re.IGNORECASE)
        words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", cleaned.lower())
        for word in words:
            if len(word) < 3 or word in STOPWORDS:
                continue
            base = len(word)
            scores[word] = scores.get(word, 0.0) + base + 1.0

    ranked = sorted(scores.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [word for word, _ in ranked[:limit]]


def _clean_user_message(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = USER_PROMPT_RE.findall(text)
    if match:
        cleaned = match[-1]
    else:
        cleaned = text
    cleaned = cleaned.strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _clean_assistant_message(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = ASSISTANT_RESPONSE_RE.findall(text)
    if match:
        cleaned = match[-1]
    else:
        cleaned = text
    cleaned = cleaned.strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _format_tags(tags: List[str]) -> Optional[str]:
    if not tags:
        return None
    return " ".join(f"#{tag}" for tag in tags)


def _conversation_title(short_id: Optional[str], tags: List[str], fallback: str) -> str:
    formatted = _format_tags(tags)
    if formatted:
        return formatted
    if short_id:
        return short_id
    return fallback


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


def _format_counter_summary(counter: Counter, max_items: int = 4) -> str:
    if not counter:
        return "—"
    items = counter.most_common()
    display = [f"{name} ({count})" for name, count in items[:max_items]]
    if len(items) > max_items:
        display.append(f"+{len(items) - max_items} more")
    return ", ".join(display)


def _format_compact_list(items: Iterable[str], max_items: int = 3) -> str:
    unique = [item for item in dict.fromkeys(items) if item]
    if not unique:
        return "—"
    display = [(_short_id(value) or value) for value in unique[:max_items]]
    if len(unique) > max_items:
        display.append(f"+{len(unique) - max_items} more")
    return ", ".join(display)


def _extract_assertion_label(entry: Dict[str, Any]) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    structured = entry.get("value_structured")
    if isinstance(structured, dict):
        label = structured.get("label")
        if isinstance(label, str) and label.strip():
            return label
    summary = entry.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary
    return None


def _render_actor_summary(
    actor_id: str,
    summary: Dict[str, Any],
    include_assertions: bool,
    assertions_limit: int,
) -> Panel:
    entities: List[Dict[str, Any]] = summary.get("entities", [])
    entity_types: Counter = summary.get("entity_types", Counter())
    facets = sorted(str(facet) for facet in summary.get("facets", set()))

    metadata_pairs = [
        ("Actor", _short_id(actor_id) or actor_id),
        ("Entities", str(len(entities))),
        ("Facets", str(len(facets))),
        ("Entity Types", _format_counter_summary(entity_types)),
    ]
    if len(actor_id) > 12:
        metadata_pairs.append(("Full Actor ID", actor_id))

    body_parts: List[Any] = []
    metadata = _metadata_block(metadata_pairs)
    if metadata is not None:
        body_parts.append(metadata)

    if entities:
        entity_table = Table(title="Entities", box=box.SIMPLE, show_header=True)
        entity_table.add_column("Type", style="cyan", no_wrap=True)
        entity_table.add_column("Count", style="white", justify="right")
        entity_table.add_column("Facets", style="dim")
        for type_name, count in sorted(entity_types.items(), key=lambda item: (-item[1], item[0])):
            facets_for_type = [
                str(item.get("facet") or "")
                for item in entities
                if item.get("entity_type") == type_name
            ]
            entity_table.add_row(type_name or "?", str(count), _format_compact_list(facets_for_type))
        body_parts.append(entity_table)
    else:
        body_parts.append(Text("No entities registered.", style="dim"))

    if include_assertions:
        assertions = summary.get("assertions") or []
        if assertions:
            assertion_table = Table(title="Assertions", box=box.SIMPLE, show_header=True)
            assertion_table.add_column("Handle", style="dim", no_wrap=True)
            assertion_table.add_column("Label", style="cyan", no_wrap=True)
            assertion_table.add_column("Summary", style="white")
            for entry in assertions:
                handle = entry.get("handle") if isinstance(entry, dict) else None
                handle_display = _short_id(handle) or str(handle) if handle else "-"
                label = _extract_assertion_label(entry) or "-"
                summary_text = entry.get("summary") if isinstance(entry, dict) else None
                if not isinstance(summary_text, str) or not summary_text:
                    value = entry.get("value") if isinstance(entry, dict) else None
                    summary_text = _summarize_value(value, max_length=80)
                assertion_table.add_row(handle_display, label, summary_text)
            if assertions_limit > 0:
                assertion_table.caption = f"Showing up to {assertions_limit} assertions."
            body_parts.append(assertion_table)
        else:
            body_parts.append(Text("No assertions for this actor.", style="dim"))

    body = Group(*body_parts) if len(body_parts) > 1 else body_parts[0]
    title = f"Actor {_short_id(actor_id) or actor_id}"
    subtitle = f"{len(entities)} entities · {len(facets)} facets"
    return Panel(body, title=title, subtitle=subtitle, border_style="cyan", box=box.ROUNDED)


def _print_chat_ack(result: Dict[str, Any], agent: str, *, show_hint: bool = True) -> None:
    request_id = result.get("request_id")
    short_id = _short_id(request_id)
    prompt_preview = _preview_text(result.get("prompt_preview") or result.get("prompt"))

    label_parts = ["Queued", agent]
    if short_id:
        label_parts.append(f"[bold]{short_id}[/bold]")
    headline = " ".join(label_parts)
    if prompt_preview:
        headline += f" · {prompt_preview}"

    console.print(f"[green]{headline}[/green]")
    if show_hint and request_id:
        console.print(
            f"[dim]Follow up with `duet query responses --request-id {request_id}` or `duet query transcript-show {request_id}` as needed.[/dim]"
        )


def _print_chat_waiting(result: Dict[str, Any], agent: str) -> None:
    request_id = result.get("request_id")
    short_id = _short_id(request_id)
    label = agent
    if short_id:
        label = f"{label} ({short_id})"
    console.print(f"[cyan]Waiting for {label} to respond…[/cyan]")


def _print_agent_invoke(result: Any) -> None:
    if not isinstance(result, dict) or "request_id" not in result:
        console.print(JSON.from_data(result))
        return

    request_id = result.get("request_id")
    prompt = result.get("prompt_preview") or result.get("prompt")
    agent = result.get("agent", "Agent")
    branch = result.get("branch")
    actor = result.get("actor")
    queued_turn_value = result.get("queued_turn")
    queued_turn = queued_turn_value if queued_turn_value else "pending"

    metadata = [
        ("Branch", branch),
        ("Actor", _short_id(actor)),
        (
            "Queued Turn",
            _short_id(queued_turn if isinstance(queued_turn, str) else str(queued_turn)),
        ),
        ("Full Request", request_id),
    ]
    clean_prompt = _clean_user_message(prompt)
    tags = _extract_keywords([clean_prompt])

    panel = _render_chat_exchange(
        prompt=prompt,
        response=None,
        user_label="You",
        assistant_label=agent,
        metadata=metadata,
        title=_short_id(request_id) or "Agent Invocation",
        tags=tags,
        border_style="blue",
    )

    console.print(panel)

    if request_id:
        console.print(
            f"[dim]Track responses with `duet query responses --request-id {request_id}` or `duet query transcript-show {request_id}`.[/dim]"
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
        request_id = entry.get("request_id")
        agent = entry.get("agent", "Agent")
        prompt = entry.get("prompt")
        response = entry.get("response")
        timestamp_raw = entry.get("timestamp")
        formatted_timestamp = _format_timestamp(timestamp_raw)
        role = entry.get("role")
        tool = entry.get("tool")
        clean_prompt = _clean_user_message(prompt)
        tags = _extract_keywords([clean_prompt])
        conversation_title = _conversation_title(_short_id(request_id), tags, agent)

        panel = _render_chat_exchange(
            prompt=prompt,
            response=response,
            user_label="You",
            assistant_label=agent,
            metadata=[
                ("Request", _short_id(request_id)),
                ("Full ID", request_id),
                ("Timestamp", formatted_timestamp),
            ],
            title=conversation_title,
            subtitle=formatted_timestamp,
            assistant_role=role,
            tool=tool,
            tags=tags,
            border_style="blue",
        )

        panels.append(panel)

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
        actor = entry.get("actor")
        actor_info = entry.get("actor_info") if isinstance(entry, dict) else None
        handle = entry.get("handle")
        summary = entry.get("summary")
        value_structured = entry.get("value_structured")
        value_raw = entry.get("value")

        actor_display = None
        entities_display = None
        if isinstance(actor_info, dict):
            actor_display = actor_info.get("short_id") or actor_info.get("id")
            entity_types = actor_info.get("entity_types")
            if isinstance(entity_types, list):
                names = [str(name) for name in entity_types if isinstance(name, str)]
                if names:
                    visible = names[:3]
                    remainder = len(names) - len(visible)
                    text = ", ".join(visible)
                    if remainder > 0:
                        text += f" (+{remainder} more)"
                    entities_display = text

        metadata_pairs = [
            ("Actor", actor_display or _short_id(actor)),
            ("Handle", _short_id(handle)),
        ]
        if actor and actor_display and actor_display != actor:
            metadata_pairs.append(("Actor ID", actor))
        if summary and summary != value_raw:
            metadata_pairs.append(("Summary", summary))
        if entities_display:
            metadata_pairs.append(("Entities", entities_display))

        metadata = _metadata_block(metadata_pairs)
        value_renderable = _structured_value_renderable(value_structured)
        if value_renderable is None and value_raw is not None:
            value_renderable = Text(_summarize_value(value_raw, max_length=200))

        body_parts: List[Any] = []
        if metadata is not None:
            body_parts.append(metadata)
        if value_renderable is not None:
            body_parts.append(value_renderable)
        if not body_parts and value_raw:
            body_parts.append(Text(_summarize_value(value_raw, max_length=200)))

        if body_parts:
            body = Group(*body_parts) if len(body_parts) > 1 else body_parts[0]
            panels.append(Panel(body, border_style="cyan", box=box.ROUNDED))

    console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _print_dataspace_events(result: Any) -> None:
    if not isinstance(result, dict) or "events" not in result:
        console.print(JSON.from_data(result))
        return

    batches = result["events"]
    if not batches:
        console.print("[dim]No new events[/dim]")
        return

    panels: List[Any] = []
    for batch in batches:
        turn_id = batch.get("turn_id") or batch.get("turn")
        actor = batch.get("actor")
        actor_info = batch.get("actor_info") if isinstance(batch, dict) else None
        clock = batch.get("clock")
        timestamp = batch.get("timestamp")
        formatted_batch_timestamp = _format_timestamp(timestamp)

        actor_display = None
        entities_display = None
        if isinstance(actor_info, dict):
            actor_display = actor_info.get("short_id") or actor_info.get("id")
            entity_types = actor_info.get("entity_types")
            if isinstance(entity_types, list):
                names = [str(name) for name in entity_types if isinstance(name, str)]
                if names:
                    visible = names[:3]
                    remainder = len(names) - len(visible)
                    text = ", ".join(visible)
                    if remainder > 0:
                        text += f" (+{remainder} more)"
                    entities_display = text

        metadata_pairs = [
            ("Turn", _short_id(turn_id)),
            ("Actor", actor_display or _short_id(actor)),
            ("Clock", str(clock) if clock is not None else None),
            ("Timestamp", formatted_batch_timestamp),
        ]
        if actor and actor_display and actor_display != actor:
            metadata_pairs.append(("Actor ID", actor))
        if entities_display:
            metadata_pairs.append(("Entities", entities_display))

        metadata = _metadata_block(metadata_pairs)
        entry_renderables: List[Any] = [metadata] if metadata is not None else []

        for event in batch.get("events", []):
            action = (event.get("action") or "").upper()
            handle = event.get("handle")
            transcript = event.get("transcript")

            if isinstance(transcript, dict):
                request_id = transcript.get("request_id")
                agent_name = transcript.get("agent", "Agent")
                response_timestamp_raw = transcript.get("response_timestamp")
                role = transcript.get("role")
                tool = transcript.get("tool")
                prompt_text = transcript.get("prompt")
                response_text = transcript.get("response")
                formatted_response_timestamp = _format_timestamp(response_timestamp_raw)
                clean_prompt = _clean_user_message(prompt_text)
                tags = _extract_keywords([clean_prompt])
                conversation_title = _conversation_title(_short_id(request_id), tags, agent_name)

                role_meta = role if role and role.lower() not in {"assistant", "agent"} else None
                event_metadata = [
                    ("Action", action),
                    ("Handle", _short_id(handle)),
                    ("Request", _short_id(request_id)),
                    ("Full Request", request_id),
                    ("Role", role_meta),
                    ("Tool", tool),
                    ("Timestamp", formatted_response_timestamp),
                ]
                entry_renderables.append(
                    _render_chat_exchange(
                        prompt=prompt_text,
                        response=response_text,
                        user_label="User",
                        assistant_label=agent_name,
                        metadata=event_metadata,
                        title=conversation_title,
                        subtitle=formatted_response_timestamp or _short_id(request_id) or action,
                        assistant_role=role,
                        tool=tool,
                        tags=tags,
                        border_style="cyan",
                    )
                )
            else:
                value_structured = event.get("value_structured")
                summary_text = event.get("summary")
                value_raw = event.get("value")
                metadata_pairs = [
                    ("Action", action),
                    ("Handle", _short_id(handle)),
                ]
                structured_meta = _structured_value_metadata(value_structured)
                if structured_meta:
                    metadata_pairs.extend(structured_meta)
                elif summary_text and summary_text != value_raw:
                    metadata_pairs.append(("Summary", summary_text))
                metadata = _metadata_block(metadata_pairs)
                value_renderable = _structured_value_renderable(value_structured)
                if value_renderable is None and value_raw is not None:
                    value_renderable = Text(_summarize_value(value_raw, max_length=200))
                body_parts: List[Any] = []
                if metadata is not None:
                    body_parts.append(metadata)
                if value_renderable is not None:
                    body_parts.append(value_renderable)
                if not body_parts and value_raw:
                    body_parts.append(Text(_summarize_value(value_raw, max_length=200)))
                if body_parts:
                    body = Group(*body_parts) if len(body_parts) > 1 else body_parts[0]
                    entry_renderables.append(Panel(body, border_style="cyan", box=box.ROUNDED))

        if entry_renderables:
            body = Group(*entry_renderables) if len(entry_renderables) > 1 else entry_renderables[0]
            panels.append(Panel(body, border_style="cyan", box=box.ROUNDED))

    if panels:
        console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _print_transcript_show(result: Any) -> None:
    if not isinstance(result, dict) or "entries" not in result:
        console.print(JSON.from_data(result))
        return

    entries = result["entries"]
    if not entries:
        console.print("[yellow]No transcript data[/yellow]")
        return

    timestamp_values = [
        entry.get("timestamp")
        for entry in entries
        if isinstance(entry, dict) and entry.get("timestamp")
    ]
    started_timestamp = _format_timestamp(timestamp_values[0]) if timestamp_values else None
    updated_timestamp = _format_timestamp(timestamp_values[-1]) if timestamp_values else None

    request_id = result.get("request_id")
    branch = result.get("branch")
    header_meta = _metadata_block([
        ("Request", _short_id(request_id)),
        ("Full ID", request_id),
        ("Branch", branch),
        ("Started", started_timestamp),
        ("Updated", updated_timestamp),
        ("Entries", str(len(entries))),
    ])
    if header_meta is not None:
        console.print(Panel(header_meta, title="Transcript", border_style="blue", box=box.ROUNDED))

    panels: List[Any] = []
    for idx, entry in enumerate(entries, start=1):
        agent = entry.get("agent", "Agent")
        actor = entry.get("actor")
        handle = entry.get("handle")
        timestamp_raw = entry.get("timestamp")
        formatted_timestamp = _format_timestamp(timestamp_raw)
        role = entry.get("role")
        tool = entry.get("tool")
        prompt = entry.get("prompt")
        response = entry.get("response")
        request_for_entry = entry.get("request_id") or request_id

        role_meta = role if role and role.lower() not in {"assistant", "agent"} else None
        metadata = [
            ("Actor", _short_id(actor)),
            ("Handle", _short_id(handle)),
            ("Role", role_meta),
            ("Tool", tool),
            ("Timestamp", formatted_timestamp),
        ]

        clean_prompt = _clean_user_message(prompt)
        tags = _extract_keywords([clean_prompt])
        conversation_title = _conversation_title(_short_id(request_for_entry), tags, agent)
        panel = _render_chat_exchange(
            prompt=prompt,
            response=response,
            user_label="User",
            assistant_label=agent,
            metadata=metadata,
            title=conversation_title,
            subtitle=formatted_timestamp or (request_for_entry if isinstance(request_for_entry, str) else None),
            assistant_role=role,
            tool=tool,
            tags=tags,
            border_style="blue",
        )

        panels.append(panel)

    console.print(Group(*panels) if len(panels) > 1 else panels[0])


def _print_transcript_tail(result: Any) -> None:
    if not isinstance(result, dict):
        console.print(JSON.from_data(result))
        return

    events = result.get("events", [])
    if not events:
        console.print("[yellow]No new events[/yellow]")
        return
    panels: List[Any] = []
    for batch in events:
        turn_id = batch.get("turn") or batch.get("turn_id")
        actor = batch.get("actor")
        clock = batch.get("clock")
        timestamp_raw = batch.get("timestamp")
        formatted_batch_timestamp = _format_timestamp(timestamp_raw)
        timestamp_display = formatted_batch_timestamp or timestamp_raw or "-"

        header_lines: List[Text] = [
            Text.assemble(("Turn: ", "dim"), (_short_id(turn_id) or "-", "white")),
            Text.assemble(("Actor: ", "dim"), (_short_id(actor) or "-", "white")),
            Text.assemble(("Clock: ", "dim"), (str(clock) if clock is not None else "-", "white")),
            Text.assemble(("Timestamp: ", "dim"), (timestamp_display, "white")),
        ]

        batch_renderables: List[Any] = [Group(*header_lines)]

        for event in batch.get("events", []):
            action = (event.get("action") or "").upper()
            handle = event.get("handle")
            transcript = event.get("transcript")

            if isinstance(transcript, dict):
                request_id = transcript.get("request_id")
                agent_name = transcript.get("agent", "Agent")
                role = transcript.get("role")
                tool = transcript.get("tool")
                prompt_text = transcript.get("prompt")
                response_text = transcript.get("response")
                response_timestamp_raw = transcript.get("response_timestamp")
                formatted_response_timestamp = _format_timestamp(response_timestamp_raw)

                role_meta = role if role and role.lower() not in {"assistant", "agent"} else None
                metadata = [
                    ("Action", action),
                    ("Handle", _short_id(handle)),
                    ("Request", _short_id(request_id)),
                    ("Full Request", request_id),
                    ("Role", role_meta),
                    ("Tool", tool),
                    ("Timestamp", formatted_response_timestamp),
                ]

                clean_prompt = _clean_user_message(prompt_text)
                tags = _extract_keywords([clean_prompt])
                conversation_title = _conversation_title(_short_id(request_id), tags, agent_name)
                exchange = _render_chat_exchange(
                    prompt=prompt_text,
                    response=response_text,
                    user_label="User",
                    assistant_label=agent_name,
                    metadata=metadata,
                    title=conversation_title,
                    subtitle=formatted_response_timestamp or _short_id(request_id) or action,
                    assistant_role=role,
                    tool=tool,
                    tags=tags,
                    border_style="blue",
                )
                batch_renderables.append(exchange)
            else:
                value = event.get("value")
                summary = f"[bold]{action}[/bold] {_short_id(handle) or ''}"
                if value:
                    summary += f"\n{_summarize_value(value, max_length=200)}"
                batch_renderables.append(
                    Panel(summary, border_style="cyan", box=box.ROUNDED, title="Event")
                )

        if batch_renderables:
            body = Group(*batch_renderables) if len(batch_renderables) > 1 else batch_renderables[0]
            panels.append(Panel(body, border_style="blue", box=box.ROUNDED))

    if panels:
        console.print(Group(*panels) if len(panels) > 1 else panels[0])

    next_cursor = result.get("next_cursor")
    has_more = result.get("has_more")
    if next_cursor or has_more:
        footer_parts = []
        if next_cursor:
            footer_parts.append(f"next cursor: {next_cursor}")
        if has_more:
            footer_parts.append("more events available")
        console.print(f"[dim]{' | '.join(footer_parts)}[/dim]")



def _print_workflow_list(result: Any) -> None:
    if not isinstance(result, dict):
        console.print(JSON.from_data(result))
        return

    definitions = result.get("definitions") or []
    instances = result.get("instances") or []
    examples = result.get("examples") or []

    if not definitions and not instances and not examples:
        console.print(
            "[yellow]No interpreter definitions, instances, or example programs found.[/yellow]"
        )
        return

    panels: List[Any] = []

    if definitions:
        definition_table = Table(
            title="Definitions", show_lines=False, header_style="bold cyan"
        )
        definition_table.add_column("ID", style="bold")
        definition_table.add_column("Name")
        definition_table.add_column("Preview")

        for item in definitions:
            if isinstance(item, dict):
                preview = _short_source(item.get("source"))
                definition_table.add_row(
                    str(item.get("id", "?")),
                    str(item.get("name", "?")),
                    preview,
                )
            else:
                definition_table.add_row(str(item), "-", "-")

        panels.append(definition_table)

    if instances:
        instance_table = Table(
            title="Instances", show_lines=False, header_style="bold magenta"
        )
        instance_table.add_column("ID", style="bold")
        instance_table.add_column("Status")
        instance_table.add_column("State")
        instance_table.add_column("Program")
        instance_table.add_column("Bindings")
        instance_table.add_column("Details")

        for item in instances:
            if isinstance(item, dict):
                status_label, status_detail = _describe_instance_status(
                    item.get("status")
                )
                progress_detail = _describe_progress(item.get("progress"))
                detail_parts = [status_detail, progress_detail]
                details = " | ".join(part for part in detail_parts if part)

                state = item.get("state")
                if not state and isinstance(item.get("progress"), dict):
                    state = item["progress"].get("state")

                bindings_inline = "; ".join(_instance_binding_lines(item))

                instance_table.add_row(
                    str(item.get("id", "?")),
                    status_label,
                    str(state or "-"),
                    _describe_program(item.get("program"), item.get("program_name")),
                    bindings_inline or "-",
                    details,
                )
            else:
                instance_table.add_row(str(item), "-", "-", "-", "-", "")

        panels.append(instance_table)

    if examples:
        example_table = Table(
            title="Example Programs", show_lines=False, header_style="bold green"
        )
        example_table.add_column("Path", style="bold")
        example_table.add_column("Description")

        for item in examples:
            if isinstance(item, dict):
                example_table.add_row(
                    str(item.get("path", "?")),
                    str(item.get("description", "")),
                )
            else:
                example_table.add_row(str(item), "")

        panels.append(example_table)

    if len(panels) == 1:
        console.print(panels[0])
    else:
        console.print(Group(*panels))



def _print_workflow_start(result: Any) -> None:
    if not isinstance(result, dict):
        console.print(JSON.from_data(result))
        return

    status = result.get("status", "started")
    lines = [f"[bold]Status[/bold] {status}"]

    if result.get("turn"):
        lines.append(f"[bold]Turn[/bold] {result['turn']}")

    if result.get("label"):
        lines.append(f"[bold]Label[/bold] {result['label']}")

    if result.get("definition_path"):
        lines.append(f"[bold]Source[/bold] {result['definition_path']}")

    instance = result.get("instance")
    if isinstance(instance, dict):
        status_label, status_detail = _describe_instance_status(instance.get("status"))
        progress_detail = _describe_progress(instance.get("progress"))
        detail_parts = [status_detail, progress_detail]
        details = " | ".join(part for part in detail_parts if part)

        state = instance.get("state") or (
            instance.get("progress", {}).get("state")
            if isinstance(instance.get("progress"), dict)
            else None
        )

        lines.append(
            f"[bold]Instance[/bold] {instance.get('id', '?')} [{status_label}]"
        )
        lines.append(
            f"[bold]Program[/bold] {_describe_program(instance.get('program'), instance.get('program_name'))}"
        )
        if state:
            lines.append(f"[bold]State[/bold] {state}")
        if details:
            lines.append(f"[bold]Details[/bold] {details}")

        bindings = _instance_binding_lines(instance)
        if bindings:
            lines.append("[bold]Bindings[/bold]")
            lines.extend(f"  {entry}" for entry in bindings)

    console.print(
        Panel(
            "\n".join(lines),
            border_style="green" if status in {"started", "accepted"} else "yellow",
        )
    )


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


def _structured_value_metadata(structured: Any) -> List[Tuple[str, Optional[str]]]:
    if not isinstance(structured, dict):
        return []

    meta: List[Tuple[str, Optional[str]]] = []
    value_type = structured.get("type")
    if isinstance(value_type, str):
        meta.append(("Type", value_type))

    summary = structured.get("summary")
    if isinstance(summary, str):
        meta.append(("Summary", summary))

    if value_type == "record":
        label = structured.get("label")
        if isinstance(label, str):
            meta.append(("Label", label))
        field_count = structured.get("field_count")
        if field_count is not None:
            meta.append(("Fields", str(field_count)))
    elif value_type in {"sequence", "set"}:
        length = structured.get("length")
        if length is not None:
            meta.append(("Items", str(length)))
    elif value_type == "dictionary":
        length = structured.get("length")
        if length is not None:
            meta.append(("Entries", str(length)))

    return meta


def _structured_value_renderable(structured: Any) -> Optional[Any]:
    if structured is None:
        return None
    try:
        return JSON.from_data(structured)
    except Exception:
        return Text(_summarize_value(structured, max_length=200))


def _short_source(source: Any, limit: int = 60) -> str:
    if not isinstance(source, str):
        return _summarize_value(source, max_length=limit)

    stripped = source.strip()
    if not stripped:
        return "-"

    first_line = stripped.splitlines()[0]
    if len(first_line) > limit:
        return f"{first_line[: limit - 3]}..."
    return first_line


def _describe_wait(wait: Any) -> str:
    if not isinstance(wait, dict):
        return _summarize_value(wait, max_length=60)

    wait_type = wait.get("type")
    if wait_type == "signal":
        label = wait.get("label", "?")
        return f"signal {label}"
    if wait_type == "record-field-eq":
        label = wait.get("label", "?")
        field = wait.get("field", "?")
        value = wait.get("value", "?")
        return f"{label}[{field}] == {value}"

    return _summarize_value(wait, max_length=60)


def _describe_instance_status(status: Any) -> Tuple[str, str]:
    if isinstance(status, dict):
        state = str(status.get("state", "?"))
        detail = ""
        if state == "waiting":
            detail = _describe_wait(status.get("wait"))
        elif state == "failed":
            detail = str(status.get("message", ""))
        return state, detail

    if isinstance(status, str):
        return status, ""

    return str(status), ""


def _describe_program(program: Any, program_name: Any) -> str:
    if isinstance(program, dict):
        kind = program.get("type")
        if kind == "definition":
            identifier = program.get("id", "?")
            if program_name:
                return f"definition:{identifier} ({program_name})"
            return f"definition:{identifier}"
        if kind == "inline":
            return f"inline: {_short_source(program.get('source', ''), limit=40)}"

    if program_name:
        return str(program_name)

    return "-"


def _describe_progress(progress: Any) -> str:
    if not isinstance(progress, dict):
        return ""

    waiting = progress.get("waiting")
    if waiting:
        return _describe_wait(waiting)

    if progress.get("entry_pending"):
        return "entry actions pending"

    frame_depth = progress.get("frame_depth")
    if isinstance(frame_depth, int) and frame_depth > 0:
        return f"frames {frame_depth}"

    return ""


def _instance_binding_lines(instance: Any) -> List[str]:
    if not isinstance(instance, dict):
        return []

    lines: List[str] = []
    entities = instance.get("entities")
    if isinstance(entities, list) and entities:
        for entry in entities:
            if not isinstance(entry, dict):
                continue
            role = entry.get("role") or "?"
            entity_type = entry.get("entity_type") or entry.get("agent_kind") or "-"
            extras = []
            agent_kind = entry.get("agent_kind")
            if agent_kind and agent_kind != entity_type:
                extras.append(agent_kind)
            actor = _short_id(entry.get("actor"))
            if actor:
                extras.append(f"actor {actor}")
            facet = _short_id(entry.get("facet"))
            if facet:
                extras.append(f"facet {facet}")
            entity_id = _short_id(entry.get("entity"))
            if entity_id:
                extras.append(f"entity {entity_id}")
            detail = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"{role}: {entity_type}{detail}")
        if lines:
            return lines

    roles = instance.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if not isinstance(role, dict):
                continue
            name = role.get("name") or "?"
            props = role.get("properties") or {}
            entity_type = props.get("entity-type")
            agent_kind = props.get("agent-kind")
            descriptor = entity_type or agent_kind or "-"
            extras = []
            for key in ("actor", "facet", "entity"):
                short = _short_id(props.get(key))
                if short:
                    extras.append(f"{key} {short}")
            if extras:
                descriptor = f"{descriptor} ({', '.join(extras)})"
            lines.append(f"{name}: {descriptor}")

    return lines


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
    elif command in ("send", "invoke-capability", "workspace:scan", "workspace:write", "raw"):
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
    elif command == "workflow:list":
        _print_workflow_list(result)
    elif command == "workflow:start":
        _print_workflow_start(result)
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


@query_app.command("transcript-show")
def transcript_show(
    ctx: typer.Context,
    request_id: Optional[str] = typer.Argument(None, help="Request identifier to inspect."),
    branch: Optional[str] = typer.Option(None, help="Branch name to query."),
    limit: int = typer.Option(20, help="Maximum transcript entries to display."),
) -> None:
    """Show stored agent transcript data."""

    if request_id is None:
        request_id = _choose_request_id(ctx.obj, title="Select transcript request")
        if request_id is None:
            console.print("[yellow]No request selected; aborting transcript display.[/yellow]")
            return

    params: Dict[str, Any] = {
        "request_id": request_id,
        "limit": limit,
    }
    if branch:
        params["branch"] = branch

    _run(_run_call(ctx.obj, "transcript_show", params, "transcript:show"))


@query_app.command("transcript-tail")
def transcript_tail(
    ctx: typer.Context,
    request_id: Optional[str] = typer.Argument(None, help="Request identifier to follow."),
    branch: str = typer.Option("main", help="Branch name to follow."),
    follow: bool = typer.Option(True, help="Continue polling for new events."),
    interval: float = typer.Option(1.0, help="Polling interval in seconds when following.", min=0.1),
    limit: int = typer.Option(10, help="Maximum transcript entries to return per poll."),
) -> None:
    """Tail agent transcript events for a request."""

    if request_id is None:
        request_id = _choose_request_id(ctx.obj, title="Select transcript request")
        if request_id is None:
            console.print("[yellow]No request selected; aborting tail operation.[/yellow]")
            return

    params: Dict[str, Any] = {
        "branch": branch,
        "request_id": request_id,
        "limit": limit,
    }
    _run(_run_transcript_tail(ctx.obj, params, follow, interval))


@debug_app.command("transcript-export")
def transcript_export(
    ctx: typer.Context,
    request_id: Optional[str] = typer.Argument(None, help="Request identifier to export."),
    branch: Optional[str] = typer.Option(None, help="Branch name to query."),
    limit: int = typer.Option(50, help="Maximum transcript entries to include.", min=1),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write transcript to this file instead of stdout.",
    ),
) -> None:
    """Export transcript history as plain text."""

    if request_id is None:
        request_id = _choose_request_id(ctx.obj, title="Select transcript request")
        if request_id is None:
            console.print("[yellow]No request selected; aborting export.[/yellow]")
            return

    _run(
        _run_transcript_export(
            ctx.obj,
            request_id=request_id,
            branch=branch,
            limit=limit,
            destination=output,
        )
    )


@query_app.command("workflows")
def workflow_list(ctx: typer.Context) -> None:
    """List workflow definitions and running instances."""
    params: Dict[str, Any] = {}
    _run(_run_call(ctx.obj, "workflow_list", params, "workflow:list"))


@run_app.command("workflow-start")
def workflow_start(
    ctx: typer.Context,
    definition: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Path to a workflow definition file.",
    ),
    label: Optional[str] = typer.Option(
        None,
        "--label",
        help="Optional label for the workflow instance.",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Follow the workflow in an interactive TUI and provide user input when prompted.",
    ),
) -> None:
    """Start a workflow using the provided definition file."""
    definition_text = definition.read_text(encoding="utf-8")
    params: Dict[str, Any] = {
        "definition": definition_text,
        "definition_path": str(definition),
    }
    if label:
        params["label"] = label
    _run(_workflow_start_command(ctx.obj, params, interactive))


def main_entrypoint() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover - manual invocation
    main_entrypoint()
