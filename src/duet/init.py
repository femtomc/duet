"""Bootstrap utilities for initializing a new Duet workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel


class InitError(Exception):
    """Exception raised during duet init."""

    pass


class DuetInitializer:
    """Handles duet init command for workspace bootstrapping."""

    def __init__(
        self,
        workspace_root: Path,
        config_path: Optional[Path] = None,
        force: bool = False,
        skip_discovery: bool = False,
        model_codex: str = "gpt-5-codex",
        model_claude: str = "sonnet",
        console: Optional[Console] = None,
    ):
        self.workspace_root = workspace_root.resolve()
        self.config_path = config_path or (self.workspace_root / ".duet")
        self.force = force
        self.skip_discovery = skip_discovery
        self.model_codex = model_codex
        self.model_claude = model_claude
        self.console = console or Console()

    def _display_path(self, path: Path) -> str:
        """Get display path (relative to workspace if possible, otherwise absolute)."""
        try:
            return str(path.relative_to(self.workspace_root))
        except ValueError:
            return str(path)

    def init(self) -> None:
        """
        Initialize Duet workspace.

        Creates:
        - .duet/ directory structure
        - duet.yaml configuration
        - Prompt templates
        - Context discovery (optional)
        - Scaffold files (.gitkeep, placeholder DB)
        """
        self.console.print(
            Panel(
                "[bold cyan]Duet Workspace Initialization[/]\n\n"
                f"[bold]Workspace:[/] {self.workspace_root}\n"
                f"[bold]Config Path:[/] {self.config_path}",
                title="duet init",
                expand=False,
            )
        )

        # Check if .duet already exists
        if self.config_path.exists() and not self.force:
            raise InitError(
                f".duet directory already exists at: {self.config_path}\n"
                "Use --force to overwrite existing configuration."
            )

        # Create directory structure
        self._create_directories()

        # Generate configuration
        self._create_config()

        # Create workflow definition (Sprint 9: DSL-based)
        self._create_workflow_definition()

        # Create scaffold files
        self._create_scaffold_files()

        # Run context discovery (optional)
        if not self.skip_discovery:
            self._run_context_discovery()
        else:
            self._create_placeholder_context()

        self.console.print()
        self.console.print("[green bold]✓ Duet workspace initialized successfully![/]")
        self.console.print()
        self.console.print("[bold]Next steps:[/]")
        self.console.print("  1. Review configuration: cat .duet/duet.yaml")
        self.console.print("  2. Customize workflow: edit .duet/ide.py (Sprint 9 DSL)")
        self.console.print("  3. Run orchestration: uv run duet run")
        self.console.print("  4. Or run phase-by-phase: uv run duet next")
        self.console.print()

    def _create_directories(self) -> None:
        """Create .duet directory structure."""
        directories = [
            self.config_path,
            self.config_path / "runs",
            self.config_path / "logs",
            self.config_path / "context",
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            self.console.log(f"[green]Created:[/] {self._display_path(directory)}")

        # Create README in .duet/
        readme_content = """# Duet Workspace

This directory contains Duet orchestration artifacts and configuration.

## Structure

- **duet.yaml**: Configuration file (models, workflow, guardrails)
- **ide.py**: Workflow definition using Python DSL (Sprint 9)
- **runs/**: Orchestration run artifacts (checkpoints, iterations, summaries)
- **logs/**: Structured JSONL event logs
- **context/**: Repository context and discovery outputs
- **duet.db**: SQLite database for run metadata and state checkpoints

## Workflow Customization (Sprint 9)

Edit `ide.py` to customize your workflow:
- Define agents (Codex, Claude Code, custom adapters)
- Declare channels for message passing
- Configure phases with consumes/publishes patterns
- Set transition guards and priorities

See `docs/workflow_dsl.md` for DSL reference.

## Usage

Start an orchestration run:
```bash
uv run duet run
```

Stateful workflow (phase-by-phase):
```bash
uv run duet next
uv run duet next "provide feedback"
uv run duet cont <run-id>
```

Check run status:
```bash
uv run duet status <run-id>
```

## Documentation

See https://github.com/femtomc/duet for full documentation.
"""
        readme_path = self.config_path / "README.md"
        readme_path.write_text(readme_content, encoding="utf-8")
        self.console.log(f"[green]Created:[/] {self._display_path(readme_path)}")

    def _create_config(self) -> None:
        """Generate duet.yaml configuration file."""
        config_content = f"""# ────────────────────────────────────────────────────────────────────────────
# Duet Configuration
# Generated by: duet init
# ────────────────────────────────────────────────────────────────────────────

# ──── Codex Configuration (Planning & Review) ────
codex:
  provider: "codex"
  model: "{self.model_codex}"
  timeout: 300

# ──── Claude Code Configuration (Implementation) ────
claude:
  provider: "claude-code"
  model: "{self.model_claude}"
  timeout: 600

# ──── Workflow Settings ────
workflow:
  max_iterations: 5
  require_human_approval: true
  auto_merge_on_approval: false

  # Guardrails
  max_consecutive_replans: 3
  # max_phase_runtime_seconds: 600  # Optional: max runtime per phase
  require_git_changes: true
  use_feature_branches: true
  restore_branch_on_complete: true

# ──── Storage Settings ────
storage:
  workspace_root: "{self.workspace_root}"
  run_artifact_dir: "{self.config_path / 'runs'}"

# ──── Logging Settings ────
logging:
  enable_jsonl: true
  jsonl_dir: "{self.config_path / 'logs'}"
  quiet: false  # Set to true to disable live streaming output (Sprint 6)
  stream_mode: "detailed"  # Display mode: detailed | compact | off (Sprint 7)

# ────────────────────────────────────────────────────────────────────────────
# Tips:
# - To use echo adapters for testing, set provider: "echo"
# - Customize prompts in .duet/prompts/ directory
# - Review context discovery in .duet/context/context.md
# - Monitor runs in .duet/runs/<run-id>/
# ────────────────────────────────────────────────────────────────────────────
"""
        config_file = self.config_path / "duet.yaml"
        config_file.write_text(config_content, encoding="utf-8")
        self.console.log(f"[green]Created:[/] {self._display_path(config_file)}")

    def _create_workflow_definition(self) -> None:
        """Create workflow definition using Python DSL (Sprint 9)."""
        # Read template from package
        from importlib import resources

        try:
            # Try to read template from package resources
            import duet.templates
            template_path = Path(duet.templates.__file__).parent / "ide.py.template"

            if template_path.exists():
                template_content = template_path.read_text(encoding="utf-8")
            else:
                # Fallback: inline template if package resource missing
                template_content = self._get_inline_workflow_template()
        except Exception:
            # Fallback: inline template
            template_content = self._get_inline_workflow_template()

        # Write ide.py
        workflow_file = self.config_path / "ide.py"
        workflow_file.write_text(template_content, encoding="utf-8")
        self.console.log(f"[green]Created:[/] {self._display_path(workflow_file)}")

        # Validate that generated workflow loads correctly
        try:
            from .workflow_loader import load_workflow
            graph = load_workflow(workflow_path=workflow_file)
            self.console.log(f"[dim]Validated workflow:[/] {len(graph.phases)} phases, {len(graph.agents)} agents")
        except Exception as exc:
            self.console.log(f"[yellow]Warning: Generated workflow validation failed: {exc}[/]")

    def _get_inline_workflow_template(self) -> str:
        """Fallback inline workflow template if package resource missing."""
        return '''"""
Duet Workflow Definition (Sprint 9 DSL).

This file defines the orchestration workflow for your project using Duet's
Python DSL. Customize agents, channels, phases, and transitions to fit your
development process.

Documentation: See docs/workflow_dsl.md for detailed DSL reference.
"""

from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[
        Agent(name="planner", provider="codex", model="gpt-5-codex", timeout=300),
        Agent(name="implementer", provider="claude", model="sonnet", timeout=600),
        Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
    ],
    channels=[
        Channel(name="task", description="Input task specification", schema="text"),
        Channel(name="plan", description="Implementation plan", schema="text"),
        Channel(name="code", description="Implementation artifacts", schema="git_diff"),
        Channel(name="verdict", description="Review outcome", schema="verdict"),
        Channel(name="feedback", description="Review feedback", schema="text"),
    ],
    phases=[
        Phase(
            name="plan",
            agent="planner",
            consumes=["task", "feedback"],
            publishes=["plan"],
            description="Draft implementation plan",
        ),
        Phase(
            name="implement",
            agent="implementer",
            consumes=["plan"],
            publishes=["code"],
            description="Execute plan and make changes",
        ),
        Phase(
            name="review",
            agent="reviewer",
            consumes=["plan", "code"],
            publishes=["verdict", "feedback"],
            description="Review implementation",
        ),
        Phase(name="done", agent="reviewer", description="Complete", is_terminal=True),
        Phase(name="blocked", agent="reviewer", description="Blocked", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
        Transition(from_phase="implement", to_phase="review"),
        Transition(from_phase="review", to_phase="done",
                   when=When.channel_has("verdict", "approve"), priority=10),
        Transition(from_phase="review", to_phase="plan",
                   when=When.channel_has("verdict", "changes_requested"), priority=5),
        Transition(from_phase="review", to_phase="blocked",
                   when=When.channel_has("verdict", "blocked"), priority=15),
    ],
    initial_phase="plan",
)
'''

    def _create_scaffold_files(self) -> None:
        """Create .gitkeep files and placeholder database."""
        # .gitkeep files
        gitkeep_paths = [
            self.config_path / "logs" / ".gitkeep",
            self.config_path / "runs" / ".gitkeep",
            self.config_path / "context" / ".gitkeep",
        ]

        for gitkeep in gitkeep_paths:
            gitkeep.write_text("", encoding="utf-8")
            self.console.log(f"[green]Created:[/] {self._display_path(gitkeep)}")

        # Placeholder SQLite database (for Sprint 5)
        db_path = self.config_path / "duet.db"
        if not db_path.exists():
            # Touch the file to reserve the path
            db_path.write_bytes(b"")
            self.console.log(f"[dim]Reserved:[/] {self._display_path(db_path)} (for future use)")

    def _run_context_discovery(self) -> None:
        """
        Run Codex discovery to map repository structure.

        Invokes Codex with a discovery prompt and saves output to context.md.
        Gracefully handles failures (CLI missing, auth issues, etc.).
        """
        self.console.print()
        self.console.print("[cyan]Running repository context discovery with Codex...[/]")

        discovery_prompt = """Analyze this repository and provide a comprehensive overview:

1. **Project Purpose**: What does this project do?
2. **Structure**: Key directories and their roles
3. **Technologies**: Languages, frameworks, build tools
4. **Entry Points**: Main files, CLI commands, APIs
5. **Dependencies**: Package managers, key dependencies
6. **TODOs/Issues**: Notable TODOs or open issues in code
7. **Development Workflow**: How to build, test, run

Be concise but comprehensive. This will help the orchestrator understand the codebase.
"""

        try:
            # Use CodexAdapter for streaming discovery (Sprint 6)
            from .adapters import CodexAdapter
            from .adapters.base import StreamEvent
            from .models import AssistantRequest
            from rich.live import Live
            from rich.panel import Panel

            adapter = CodexAdapter(model=self.model_codex, timeout=120)
            request = AssistantRequest(
                role="discovery",
                prompt=discovery_prompt,
                context={},
            )

            # Track streaming progress
            import time
            start_time = time.time()
            event_count = 0
            reasoning_count = 0
            agent_message_snippet = None
            reasoning_snippet = None
            last_command = None
            command_output = None
            token_count = 0

            def render_progress():
                """Render enriched progress panel."""
                elapsed = int(time.time() - start_time)

                # Build status lines
                lines = []
                lines.append(f"[bold cyan]Context Discovery[/] [dim]({elapsed}s elapsed)[/]")
                lines.append("")

                # Show progress metrics
                lines.append(f"[bold]Progress:[/]")
                lines.append(f"  • Events: {event_count}")
                if reasoning_count > 0:
                    lines.append(f"  • Reasoning steps: {reasoning_count}")
                if token_count > 0:
                    lines.append(f"  • Tokens generated: {token_count}")

                # Show latest agent message snippet
                if agent_message_snippet:
                    lines.append("")
                    lines.append(f"[bold]Latest Analysis:[/]")
                    snippet = agent_message_snippet[:200]
                    first_line = snippet.split("\n", 1)[0]
                    lines.append(f"  [dim]{first_line}...[/]")
                elif reasoning_snippet:
                    lines.append("")
                    lines.append(f"[bold]Current Focus:[/]")
                    snippet = reasoning_snippet[:200]
                    first_line = snippet.split("\n", 1)[0]
                    lines.append(f"  [dim]{first_line}[/]")
                elif last_command:
                    lines.append("")
                    lines.append(f"[bold]Last Command:[/]")
                    lines.append(f"  [dim]{last_command}[/]")
                    if command_output:
                        preview = command_output[:120]
                        first_line = preview.split("\n", 1)[0]
                        lines.append(f"  [dim]{first_line}...[/]")
                else:
                    lines.append("")
                    lines.append("[dim]Waiting for Codex response...[/]")

                content = "\n".join(lines)
                return Panel(content, border_style="cyan", expand=False)

            def on_event(event: StreamEvent) -> None:
                """Handle streaming events during discovery (Sprint 7: canonical events)."""
                nonlocal event_count, reasoning_count, agent_message_snippet, token_count, reasoning_snippet, last_command, command_output
                event_count += 1

                event_type = event["event_type"]

                # Extract agent message from enriched field (Sprint 7)
                if event_type == "assistant_message":
                    text = event.get("text_snippet", "")
                    if text:
                        agent_message_snippet = text

                # Track reasoning steps and capture text (Sprint 7)
                elif event_type == "reasoning":
                    reasoning_count += 1
                    text = event.get("text_snippet", "")
                    if text:
                        reasoning_snippet = text

                # Track tool use (Sprint 7)
                elif event_type == "tool_use":
                    tool_info = event.get("tool_info", {})
                    if tool_info:
                        last_command = tool_info.get("tool_name", "unknown")
                        command_output = tool_info.get("output_preview", "")

                # Extract token usage from enriched field (Sprint 7)
                elif event_type == "turn_complete":
                    usage = event.get("usage", {})
                    if usage:
                        token_count = usage.get("output_tokens", 0)

            # Run discovery with live display
            with Live(render_progress(), console=self.console, refresh_per_second=4, transient=True) as live:
                def live_event_handler(event: StreamEvent) -> None:
                    on_event(event)
                    live.update(render_progress())

                response = adapter.stream(request, on_event=live_event_handler)

                # If no streaming snippet was captured, fall back to final response content
                if not agent_message_snippet and response.content:
                    agent_message_snippet = response.content
                    live.update(render_progress())

            # Extract context from response
            context_text = response.content

            if not context_text or not context_text.strip():
                self.console.log("[yellow]No context extracted from Codex response[/]")
                self._create_placeholder_context()
                return

            # Write context to file
            import datetime as dt

            timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
            context_content = f"""# Repository Context

**Generated**: {timestamp}
**Model**: {self.model_codex}
**Method**: Codex repository discovery

---

{context_text}

---

**Rerun Discovery**: `uv run duet init --force` (omit --skip-discovery flag)

<!-- This file can be manually edited to improve orchestrator context -->
"""

            context_file = self.config_path / "context" / "context.md"
            context_file.write_text(context_content, encoding="utf-8")
            self.console.log(f"[green]Created:[/] {self._display_path(context_file)}")
            self.console.print("[green]✓ Context discovery complete[/]")

        except Exception as exc:
            # Handle CodexError, timeout, or any other discovery failure
            error_msg = str(exc)
            if "not found" in error_msg.lower():
                self.console.log("[yellow]Codex CLI not found - skipping context discovery[/]")
            elif "timeout" in error_msg.lower():
                self.console.log("[yellow]Context discovery timeout - skipping[/]")
            else:
                self.console.log(f"[yellow]Context discovery error: {exc}[/]")
            self._create_placeholder_context()

    def _create_placeholder_context(self) -> None:
        """Create placeholder context.md when discovery is skipped or fails."""
        context_content = """# Repository Context

**Status**: Discovery skipped or failed

To generate context discovery:
```bash
uv run duet init --force
```

Or manually document your repository context here:

## Project Purpose
<!-- What does this project do? -->

## Structure
<!-- Key directories and their roles -->

## Technologies
<!-- Languages, frameworks, tools -->

## Development Workflow
<!-- How to build, test, run -->

<!-- This file helps the orchestrator understand your codebase -->
"""
        context_file = self.config_path / "context" / "context.md"
        context_file.write_text(context_content, encoding="utf-8")
        self.console.log(f"[dim]Created:[/] {self._display_path(context_file)} (placeholder)")
