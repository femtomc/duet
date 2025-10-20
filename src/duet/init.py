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
        init_git: bool = False,
        model_codex: str = "gpt-5-codex",
        model_claude: str = "sonnet",
        console: Optional[Console] = None,
    ):
        self.workspace_root = workspace_root.resolve()
        self.config_path = config_path or (self.workspace_root / ".duet")
        self.force = force
        self.skip_discovery = skip_discovery
        self.init_git = init_git
        self.model_codex = model_codex
        self.model_claude = model_claude
        self.console = console or Console()

    def _display_path(self, path: Path) -> str:
        """Get display path (relative to workspace if possible, otherwise absolute)."""
        try:
            return str(path.relative_to(self.workspace_root))
        except ValueError:
            return str(path)

    def _is_git_repo(self) -> bool:
        """Check if workspace is already a Git repository without invoking git."""
        git_dir = self.workspace_root / ".git"

        if git_dir.is_dir():
            return True

        if git_dir.is_file():
            try:
                contents = git_dir.read_text().strip()
            except OSError:
                return False

            if contents.startswith("gitdir:"):
                target = contents.split("gitdir:", 1)[1].strip()
                return bool((self.workspace_root / target).exists())

        return False

    def _initialize_git_repo(self) -> None:
        """
        Initialize git repository with .gitignore and initial commit.

        Creates:
        - git repository (git init)
        - .gitignore with duet-specific entries
        - initial commit with .gitignore and .duet/README.md
        """
        import subprocess

        self.console.print()
        self.console.print("[cyan]Initializing Git repository...[/]")

        # 1. Check if git is installed
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            raise InitError(
                "Git is not installed or not in PATH. "
                "Install Git to use --init-git flag."
            )

        # 2. Initialize repository
        try:
            subprocess.run(
                ["git", "-C", str(self.workspace_root), "init"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.console.log("[green]Created:[/] .git/ (git repository)")
        except subprocess.CalledProcessError as exc:
            raise InitError(f"Failed to initialize git repository: {exc.stderr}")

        # 3. Create .gitignore
        gitignore_path = self.workspace_root / ".gitignore"
        gitignore_content = """# Duet orchestration artifacts
.duet/logs/
.duet/runs/
.duet/duet.db
.duet/duet.db-journal
.duet/__pycache__/

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python

# Virtual environments
venv/
env/
ENV/

# IDEs
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db
"""

        if gitignore_path.exists():
            # Append duet entries if .gitignore already exists
            existing = gitignore_path.read_text(encoding="utf-8")
            if ".duet/logs/" not in existing:
                with gitignore_path.open("a", encoding="utf-8") as f:
                    f.write("\n" + gitignore_content)
                self.console.log("[green]Updated:[/] .gitignore (appended duet entries)")
            else:
                self.console.log("[dim].gitignore already contains duet entries[/]")
        else:
            gitignore_path.write_text(gitignore_content, encoding="utf-8")
            self.console.log("[green]Created:[/] .gitignore")

        # 4. Stage files for initial commit
        try:
            # Stage .gitignore
            subprocess.run(
                ["git", "-C", str(self.workspace_root), "add", ".gitignore"],
                capture_output=True,
                text=True,
                check=True,
            )

            # Stage .duet/README.md (created earlier in init flow)
            duet_readme = self.config_path / "README.md"
            if duet_readme.exists():
                subprocess.run(
                    ["git", "-C", str(self.workspace_root), "add", str(duet_readme.relative_to(self.workspace_root))],
                    capture_output=True,
                    text=True,
                    check=True,
                )
        except subprocess.CalledProcessError as exc:
            self.console.log(f"[yellow]Warning: Failed to stage files: {exc.stderr}[/]")

        # 5. Create initial commit
        try:
            subprocess.run(
                ["git", "-C", str(self.workspace_root), "commit", "-m", "chore: initialize Duet workspace"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.console.print("[green]✓ Created initial git commit[/]")
        except subprocess.CalledProcessError as exc:
            # Check if error is because there's nothing to commit (already has commits)
            if "nothing to commit" in exc.stderr.lower():
                self.console.log("[dim]Repository already has commits - skipping initial commit[/]")
            else:
                self.console.log(f"[yellow]Warning: Failed to create initial commit: {exc.stderr}[/]")

    def init(self) -> None:
        """
        Initialize Duet workspace.

        Creates:
        - .duet/ directory structure
        - duet.yaml configuration
        - workflow.py workflow definition (Python DSL)
        - Context discovery (optional)
        - Scaffold files (.gitkeep, SQLite DB)
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

        # Create workflow definition (DSL-based)
        self._create_workflow_definition()

        # Create scaffold files
        self._create_scaffold_files()

        # Run context discovery (optional)
        if not self.skip_discovery:
            self._run_context_discovery()
        else:
            self._create_placeholder_context()

        # Git setup: detect and warn/initialize
        is_git_repo = self._is_git_repo()

        if not is_git_repo:
            if self.init_git:
                # User requested git initialization
                self._initialize_git_repo()
            else:
                # Warn about missing git
                self.console.print()
                self.console.print("[yellow bold]⚠ No Git repository detected[/]")
                self.console.print(
                    "[yellow]Duet needs at least one commit to enable workspace restoration (duet back).[/]"
                )
                self.console.print()
                self.console.print("[dim]To set up Git:[/]")
                self.console.print("  • Run: [cyan]duet init --init-git --force[/] (in this workspace)")
                self.console.print("  • Or manually: [cyan]git init && git add . && git commit -m 'Initial commit'[/]")
                self.console.print()
        elif self.init_git:
            # Repo already exists, --init-git is a no-op
            self.console.print()
            self.console.print("[dim]Git repository already exists - skipping initialization[/]")

        self.console.print()
        self.console.print("[green bold]✓ Duet workspace initialized successfully![/]")
        self.console.print()
        self.console.print("[bold]Next steps:[/]")
        self.console.print("  1. Review configuration: cat .duet/duet.yaml")
        self.console.print("  2. Customize workflow: edit .duet/workflow.py")
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
- **workflow.py**: Workflow definition using Python DSL
- **runs/**: Orchestration run artifacts (checkpoints, iterations, summaries)
- **logs/**: Structured JSONL event logs
- **context/**: Repository context and discovery outputs
- **duet.db**: SQLite database for run metadata and state checkpoints

## Workflow Customization
Edit `workflow.py` to customize your workflow:
- Define agents (Codex, Claude Code, custom adapters)
- Declare channels for message passing
- Configure phases with consumes/publishes patterns
- Set transition guards and priorities

See `docs/workflow_dsl.md` for reference.

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
  # timeout: 300  # Optional: uncomment to set timeout in seconds

# ──── Claude Code Configuration (Implementation) ────
claude:
  provider: "claude-code"
  model: "{self.model_claude}"
  # timeout: 600  # Optional: uncomment to set timeout in seconds

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
  quiet: false  # Set to true to disable live streaming output
  stream_mode: "detailed"  # Display mode: detailed | compact | off

# ────────────────────────────────────────────────────────────────────────────
# Tips:
# - To use echo adapters for testing, set provider: "echo"
# - Customize workflow in .duet/workflow.py
# - Review context discovery in .duet/context/context.md
# - Monitor runs in .duet/runs/<run-id>/
# - See docs/workflow_dsl.md for DSL reference
# ────────────────────────────────────────────────────────────────────────────
"""
        config_file = self.config_path / "duet.yaml"
        config_file.write_text(config_content, encoding="utf-8")
        self.console.log(f"[green]Created:[/] {self._display_path(config_file)}")

    def _create_workflow_definition(self) -> None:
        """Create example workflow definition using facet DSL."""
        template = '''"""
Duet workflow definition using facet DSL.

This file defines your workflow as a reactive facet program.
Facets execute based on fact availability, not sequential order.

To run: uv run duet run
To validate: uv run duet lint
"""

from duet.dsl import facet, seq
from duet.dataspace import TaskRequest, PlanDoc, CodeArtifact, ReviewVerdict

# ──────────────────────────────────────────────────────────────────────────────
# Define your workflow
# ──────────────────────────────────────────────────────────────────────────────

workflow = seq(
    # 1. Planning facet - creates implementation plan
    facet("plan")
        .needs(TaskRequest, alias="task")
        .agent("planner", prompt="Create a detailed implementation plan")
        .emit(PlanDoc, values={
            "content": "$agent_response",
            "task_id": "$task.fact_id",
            "iteration": 0
        })
        .build(),

    # 2. Implementation facet - writes code
    facet("implement")
        .needs(PlanDoc, alias="plan")
        .agent("coder", prompt="Implement the plan")
        .emit(CodeArtifact, values={
            "summary": "$agent_response",
            "plan_id": "$plan.fact_id",
            "files_changed": 0
        })
        .build(),

    # 3. Review facet - validates implementation
    facet("review")
        .needs(CodeArtifact, alias="code")
        .agent("reviewer", prompt="Review the code changes")
        .emit(ReviewVerdict, values={
            "verdict": "$agent_response",
            "code_id": "$code.fact_id"
        })
        .build()
)

# ──────────────────────────────────────────────────────────────────────────────
# How to use:
# ──────────────────────────────────────────────────────────────────────────────
#
# 1. Seed initial facts:
#    duet seed TaskRequest --description "Build authentication" --priority 1
#
# 2. Run the workflow:
#    duet run
#
# 3. The workflow executes reactively:
#    - "plan" facet waits for TaskRequest fact
#    - "implement" facet waits for PlanDoc from plan
#    - "review" facet waits for CodeArtifact from implement
#
# 4. Customize by:
#    - Adding .tool() steps for git validation
#    - Adding .human() steps for approval gates
#    - Using loop() for test-fix cycles
#    - Using branch() for conditional logic
#
# See docs/facet_dsl.md for full DSL reference.
# ──────────────────────────────────────────────────────────────────────────────
'''
        workflow_file = self.config_path / "workflow.py"
        workflow_file.write_text(template, encoding="utf-8")
        self.console.log(f"[green]Created:[/] {self._display_path(workflow_file)}")

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

        # Placeholder SQLite database
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
        self.console.print("[dim]Tip: Use --skip-discovery to bypass this step[/]")

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
            # Use CodexAdapter for streaming discovery
            from .adapters import CodexAdapter
            from .adapters.base import StreamEvent
            from .models import AssistantRequest
            from rich.live import Live
            from rich.panel import Panel

            adapter = CodexAdapter(model=self.model_codex)
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
                """Handle streaming events during discovery."""
                nonlocal event_count, reasoning_count, agent_message_snippet, token_count, reasoning_snippet, last_command, command_output
                event_count += 1

                event_type = event["event_type"]

                # Extract agent message from enriched field
                if event_type == "assistant_message":
                    text = event.get("text_snippet", "")
                    if text:
                        agent_message_snippet = text

                # Track reasoning steps and capture text
                elif event_type == "reasoning":
                    reasoning_count += 1
                    text = event.get("text_snippet", "")
                    if text:
                        reasoning_snippet = text

                # Track tool use
                elif event_type == "tool_use":
                    tool_info = event.get("tool_info", {})
                    if tool_info:
                        last_command = tool_info.get("tool_name", "unknown")
                        command_output = tool_info.get("output_preview", "")

                # Extract token usage from enriched field
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

        except PermissionError as exc:
            # Handle EPERM specifically with concise message
            self.console.print("[yellow]⚠ Context discovery skipped (permission denied)[/]")
            self.console.print("[dim]  Run with --skip-discovery to avoid this check, or use echo adapter for testing[/]")
            self._create_placeholder_context()
        except Exception as exc:
            # Handle CodexError, timeout, or any other discovery failure with concise messages
            error_msg = str(exc)
            if "not found" in error_msg.lower() or "codex cli not found" in error_msg.lower():
                self.console.print("[yellow]⚠ Context discovery skipped (Codex CLI not found)[/]")
                self.console.print("[dim]  Install Codex CLI or use --skip-discovery flag[/]")
            elif "timeout" in error_msg.lower():
                self.console.print("[yellow]⚠ Context discovery timeout - skipping[/]")
            elif "permission" in error_msg.lower() or "eperm" in error_msg.lower():
                self.console.print("[yellow]⚠ Context discovery skipped (permission denied)[/]")
                self.console.print("[dim]  Run with --skip-discovery to avoid this check[/]")
            else:
                # Generic error - show concise message without full traceback
                first_line = error_msg.split("\n")[0][:100]
                self.console.print(f"[yellow]⚠ Context discovery failed: {first_line}[/]")
                self.console.print("[dim]  Using placeholder context. Run with --skip-discovery to bypass[/]")
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
