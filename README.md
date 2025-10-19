<div align="center">
  <img src="logo-circle.png" alt="Duet Logo" width="300"/>

  # Duet

  **Programmable agentic time machine for Codex ↔ Claude Code**
</div>

Duet coordinates planning, implementation, and review across Codex and Claude Code.  
Workflows are defined in Python, executed phase‑by‑phase, and recorded in a durable message log so every channel, prompt, and verdict can be inspected or replayed.

---

## Why Duet?

- **Programmable workflows** – describe agents, channels, guards, and transitions in `.duet/workflow.py`.
- **Channel-based messaging** – phases publish structured payloads (`task`, `plan`, `code`, `verdict`, `feedback`, …) instead of opaque text blobs.
- **Stateful execution** – run the loop one phase at a time with `duet next`, rewind with `duet back`, or continue automatically.
- **Persistent history** – every checkpoint, channel update, event, and verdict is stored in SQLite for replay and audit.
- **Guardrails built-in** – git change detection, iteration limits, human approvals, baseline management, and more.

---

## Quick Start

```bash
# 1. Install dependencies
uv sync --group dev

# 2. Bootstrap the workspace (creates .duet/)
uv run duet init

# 3. Inspect the generated workflow and config
cat .duet/workflow.py
cat .duet/duet.yaml

# 4. Execute a phase or run the full loop
uv run duet next           # phase-by-phase
uv run duet run            # automatic loop
```

`duet init` creates:

| Path | Description |
|------|-------------|
| `.duet/workflow.py` | Python DSL workflow definition (agents, channels, phases, transitions) |
| `.duet/duet.yaml` | Codex/Claude config, guardrails, logging options |
| `.duet/runs/` | Run artifacts, checkpoints, summaries |
| `.duet/logs/` | JSONL event stream (if enabled) |
| `.duet/duet.db` | SQLite database (runs, states, messages, events) |
| `.duet/context/` | Repository discovery notes |

---

## Workflow at a Glance

```python
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[
        Agent(name="planner", provider="codex", model="gpt-5-codex"),
        Agent(name="implementer", provider="claude", model="sonnet"),
        Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
    ],
    channels=[
        Channel(name="task", schema="text"),
        Channel(name="plan", schema="text"),
        Channel(name="code", schema="git_diff"),
        Channel(name="verdict", schema="verdict"),
        Channel(name="feedback", schema="text"),
    ],
    phases=[
        Phase(name="plan", agent="planner", consumes=["task", "feedback"], publishes=["plan"]),
        Phase(name="implement", agent="implementer", consumes=["plan"], publishes=["code"]),
        Phase(name="review", agent="reviewer", consumes=["plan", "code"], publishes=["verdict", "feedback"]),
        Phase(name="done", agent="reviewer", is_terminal=True),
        Phase(name="blocked", agent="reviewer", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
        Transition(from_phase="implement", to_phase="review"),
        Transition(from_phase="review", to_phase="done", when=When.channel_has("verdict", "approve")),
        Transition(from_phase="review", to_phase="plan", when=When.channel_has("verdict", "changes_requested")),
        Transition(from_phase="review", to_phase="blocked", when=When.channel_has("verdict", "blocked")),
    ],
)
```

Prompt builders receive channel payloads through a `PromptContext`, so the generated instructions are always aligned with the syndicated workspace.

Learn more in [`docs/workflow_dsl.md`](docs/workflow_dsl.md).

---

## Channel History & Replay

Every channel update is persisted to the `messages` table with timestamps and metadata.  
This powers:

- `duet status RUN_ID` – shows the latest value for each channel and the active state.
- `duet inspect RUN_ID` – displays per-iteration details plus channel history (filters coming soon).
- `duet back STATE_ID` – restores git baseline **and** channel snapshot so phases resume with identical context.

Message persistence makes the workspace replayable, auditable, and ready for analytics or streaming UIs.

---

## CLI Highlights

| Command | Purpose |
|---------|---------|
| `duet init` | Scaffold `.duet/` (config, workflow.py, context, logs, runs, database) |
| `duet run` | Execute the full plan→implement→review loop automatically |
| `duet next [--run-id ID] [FEEDBACK]` | Execute the next phase (auto-resumes most recent run) |
| `duet cont RUN_ID [--max-phases N]` | Continue phases until done or blocked |
| `duet back STATE_ID [--force]` | Restore workspace/database to a prior checkpoint |
| `duet status RUN_ID` | Inspect run status, active state, latest channel values |
| `duet inspect RUN_ID [--channel NAME]` | Detailed iteration, event, and channel history |
| `duet messages RUN_ID [--channel NAME]` | Query channel message history with filters |
| `duet migrate [--force]` | Apply schema upgrades to existing `.duet/duet.db` |

All commands accept `--config PATH` to point at a specific `duet.yaml`.

---

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Codex CLI (`codex auth login`)
- Claude Code CLI (`claude auth login`)
- Git repository for the workspace

---

## Documentation & Planning

- Workflow DSL reference: [`docs/workflow_dsl.md`](docs/workflow_dsl.md)
- Current roadmap & sprint notes: [`docs/sprint_planning.md`](docs/sprint_planning.md)
- Message persistence overview: (coming soon) additional CLI docs and examples

---

Duet is evolving rapidly—feedback and contributions are welcome.  
File an issue or open a PR to help shape the future of programmable AI workflows.
