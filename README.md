<div align="center">
  <img src="logo-circle.png" alt="Duet Logo" width="300"/>

  # Duet

  **Programmable agents in a time machine**
</div>

This is a programmable agentic orchestration tool -- it's a kind of swiss army watch for defining structured interactions with agents. 

Many agentic IDEs (Codex, Claude Code, Kiro) bake in opinions about the _interaction model_ through which the user engages with their agents. This is not a bad thing: a stable UI and interaction model can be useful and reliable!

But _this project_ is not concerned with stability -- it's concerned with trying to have a bit of fun with these wacky, stochastic, not-at-all stable _ghosts_ of human language intelligence: being useful is an accident.

With that spirit in mind, the goal here is a _programmable interaction system_. If one wishes, they can _recover_ the interaction models of the systems mentioned above as _programs_ within Duet (for example: the "spec-driven development" interaction model from a system like Kiro is a Duet program).

These interaction models take the form of _workflow programs_ (in a lightweight Python DSL) which are executed by a graph-driven workflow executor with channel-based message passing.

A workflow program defines a type of conversation space -- not fully unstructured or spontaneous (unless you want it to be) -- a space through which multiple agents can interact to "do stuff".

Duet's backend takes care of a bunch of other things that you'd probably find yourself wanting here: the ability to jump backwards and forwards in the history of the conversation (persistence), the ability to query and inspect _everything_, etc.

## How does it work?

Here's the gloss (a human wrote this, in the style of AI):

- **Programmable agent interactions** – start by describing your workflow using agents, channels, guards, and transitions in `.duet/workflow.py`. Surprise: it's a graph!
- **Stateful execution** – the workflow is compiled into a graph representation, and Duet's orchestration backend consumes it. You can then run the execution loop one phase at a time with `duet next`, rewind with `duet back`, or continue automatically with `duet cont`.
- **Channel-based messaging** – execution is richly instrumented: agents / tools publish structured payloads to channels (e.g. `task`, `plan`, `code`, `verdict`, `feedback`, …) instead of opaque text blobs. These channels form a communication medium for the agents involved in the workflow.
- **Persistent history** – we save every checkpoint, channel update, event, and verdict -- checkpoints are stored in SQLite for replay and audit, and synced with Git.

## Quick Start

```bash
# 1. Install dependencies
uv sync --group dev

# 2. Bootstrap the workspace (creates .duet/)
duet init

# 2a. (Optional but recommended) Initialize git for time travel
duet init --init-git --force  # Creates git repo + .gitignore + initial commit
# OR manually: git init && git add . && git commit -m "Initial commit"

# 3. Inspect the generated workflow and config
cat .duet/workflow.py
cat .duet/duet.yaml

# 4. Validate the workflow (optional)
duet lint

# 5. Execute a phase or run the full loop
duet next           # phase-by-phase
duet run            # automatic loop
```

**Note on Git**: Duet needs at least one git commit to enable workspace restoration with `duet back`. If no git repository is detected during `duet init`, you'll see a warning with setup instructions.

`duet init` creates:

| Path | Description |
|------|-------------|
| `.duet/workflow.py` | Python DSL workflow definition (agents, channels, phases, transitions) |
| `.duet/duet.yaml` | Codex/Claude config, guardrails, logging options |
| `.duet/runs/` | Run artifacts, checkpoints, summaries |
| `.duet/logs/` | JSONL event stream (if enabled) |
| `.duet/duet.db` | SQLite database (runs, states, messages, events) |
| `.duet/context/` | Repository discovery notes |

### Adapter configuration

Each assistant entry in `.duet/duet.yaml` maps to a CLI provider. Fields like `timeout` or `cli_path` let you customise invocation. For Claude Code you may set `auto_approve: true` to skip permission prompts and apply edits automatically—only enable this in trusted environments.

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

## Channel History & Replay

Every channel update is persisted to the `messages` table with timestamps and metadata.  
This powers:

- `duet status RUN_ID` – shows the latest value for each channel and the active state.
- `duet inspect RUN_ID` – displays per-iteration details plus channel history (filters coming soon).
- `duet back STATE_ID` – restores git baseline **and** channel snapshot so phases resume with identical context.

Message persistence makes the workspace replayable, auditable, and ready for analytics or streaming UIs.

## Testing & Development

### Using the Echo Adapter

The **echo adapter** allows testing workflows without Codex/Claude credentials. It's perfect for:
- Validating workflow logic and transitions
- Testing channel persistence
- Developing custom workflows
- CI/CD pipelines

**Setup:**
```yaml
# In .duet/duet.yaml:
codex:
  provider: "echo"
  model: "echo-v1"

claude:
  provider: "echo"
  model: "echo-v1"
```

**Behavior:**
- Echoes back prompts with role and context information
- Auto-approves when acting as a reviewer (sets `verdict: approve`)
- No external API calls or authentication required
- Instant responses for fast iteration

### Validating Workflows

Use `duet lint` to validate your workflow before running:

```bash
duet lint                    # Validate .duet/workflow.py
duet lint --workflow custom.py  # Validate custom file
```

**Checks:**
- ✓ All phases reference valid agents
- ✓ All channel references (consumes/publishes) are defined
- ✓ No duplicate phase/channel/agent names
- ✓ At least one phase exists
- ✓ Valid Python syntax

**Workflow hot-reload:** Duet automatically detects changes to `.duet/workflow.py` and reloads before each phase execution.

## CLI Highlights

| Command | Purpose |
|---------|---------|
| `duet init [--init-git]` | Scaffold `.duet/` (config, workflow.py, context, logs, runs, database). Use `--init-git` to create git repo with .gitignore and initial commit |
| `duet run` | Execute the full plan→implement→review loop automatically |
| `duet next [--run-id ID] [FEEDBACK]` | Execute the next phase (auto-resumes most recent run) |
| `duet cont RUN_ID [--max-phases N]` | Continue phases until done or blocked |
| `duet back STATE_ID [--force]` | Restore workspace/database to a prior checkpoint (requires git commits) |
| `duet status RUN_ID` | Inspect run status, active state, latest channel values |
| `duet inspect RUN_ID [--channel NAME]` | Detailed iteration, event, and channel history |
| `duet messages RUN_ID [--channel NAME]` | Query channel message history with filters |
| `duet lint [--workflow PATH]` | Validate workflow definition without executing it |
| `duet migrate [--force]` | Apply schema upgrades to existing `.duet/duet.db` |

All commands accept `--config PATH` to point at a specific `duet.yaml`.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Codex CLI (`codex auth login`)
- Claude Code CLI (`claude auth login`)
- **Git** - Required for workspace restoration (`duet back`). Use `duet init --init-git` to set up automatically

## Documentation & Planning

- **Workflow DSL reference**: [`docs/workflow_dsl.md`](docs/workflow_dsl.md)
- **Example workflows**: [`examples/`](examples/) - Custom channels, testing patterns, and advanced features
- **Current roadmap**: [`docs/sprint_planning.md`](docs/sprint_planning.md)
- **Testing guide**: Use echo adapter and `duet lint` for validation (see [Testing & Development](#testing--development))
