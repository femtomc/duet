# Workflow DSL Reference (Sprint 9)

The Duet Workflow DSL is a Python-based declarative language for defining orchestration workflows. It replaces legacy prompt templates with a type-safe, channel-based messaging system that enables the syndicated workspace model.

## Table of Contents

- [Overview](#overview)
- [Core Concepts](#core-concepts)
- [Components](#components)
- [Guards](#guards)
- [Examples](#examples)
- [Best Practices](#best-practices)
- [Advanced Patterns](#advanced-patterns)
- [Migration Guide](#migration-guide)

---

## Overview

### Why a DSL?

The DSL provides:
- **Type Safety**: Compile-time validation of workflow structure
- **Channel-Based Messaging**: Structured data flow between phases
- **Conditional Logic**: Guard predicates for dynamic transitions
- **Composability**: Reusable agents, channels, and guard expressions
- **Tooling**: IDE autocomplete, static analysis, refactoring support

### Basic Structure

A workflow consists of:
1. **Agents** - AI models that execute phases (Codex, Claude Code)
2. **Channels** - Communication pathways for structured data
3. **Phases** - Execution steps that consume and publish to channels
4. **Transitions** - Conditional edges between phases with guard predicates

---

## Core Concepts

### Syndicated Workspace Model

Instead of prompt templates, phases communicate through **channels**:

```
┌─────────┐   task    ┌──────────┐   plan    ┌────────────┐
│ User    │ ───────▶  │ Planner  │ ───────▶  │ Implementer│
│ Input   │           │ Phase    │           │ Phase      │
└─────────┘           └──────────┘           └────────────┘
                           │                       │
                           │                       │ code
                           │                       ▼
                           │                  ┌──────────┐
                           └─────feedback────│ Reviewer │
                                             │ Phase    │
                                             └──────────┘
                                                  │ verdict
```

Each phase:
- **Consumes** messages from input channels
- **Publishes** results to output channels
- Operates on structured data, not text templates

### Channel Schemas

Channels can specify schema hints for validation and persistence:

```python
Channel(name="task", schema="text")           # Plain text
Channel(name="plan", schema="text")           # Markdown plan
Channel(name="code", schema="git_diff")       # Git changes
Channel(name="verdict", schema="verdict")     # Enum: approve/changes_requested/blocked
Channel(name="metrics", schema="json")        # Structured JSON data
```

Runtime can validate payloads against schemas and persist consistently.

---

## Components

### Agent

Defines an AI agent that executes phases.

**Fields:**
- `name: str` - Unique identifier (e.g., "planner", "implementer")
- `provider: str` - Provider name ("codex", "claude", "echo")
- `model: str` - Model identifier (e.g., "gpt-5-codex", "sonnet")
- `timeout: Optional[int]` - Timeout in seconds
- `cli_path: Optional[str]` - Custom CLI executable path
- `api_key_env: Optional[str]` - Environment variable for API key

**Example:**
```python
Agent(
    name="planner",
    provider="codex",
    model="gpt-5-codex",
    timeout=300,
)
```

### Channel

Defines a communication channel for message passing.

**Fields:**
- `name: str` - Unique identifier (e.g., "plan", "code")
- `description: str` - Human-readable purpose
- `schema: Optional[str]` - Type hint for validation ("text", "json", "git_diff", "verdict")
- `initial_value: Any` - Optional seed value (e.g., task from CLI)

**Example:**
```python
Channel(
    name="plan",
    description="Implementation plan from planner",
    schema="text",
)
```

### Phase

Defines a workflow phase with channel dependencies.

**Fields:**
- `name: str` - Unique identifier (e.g., "plan", "implement")
- `agent: str` - Name of agent that executes this phase
- `consumes: List[str]` - Input channel names
- `publishes: List[str]` - Output channel names
- `description: str` - Human-readable purpose
- `is_terminal: bool` - Whether this phase ends the workflow

**Example:**
```python
Phase(
    name="review",
    agent="reviewer",
    consumes=["plan", "code"],     # Reads plan + implementation
    publishes=["verdict", "feedback"],  # Writes verdict + feedback
    description="Review implementation against plan",
)
```

**Terminal Phases:**
```python
Phase(name="done", agent="reviewer", is_terminal=True)
Phase(name="blocked", agent="reviewer", is_terminal=True)
```

### Transition

Defines a conditional edge between phases.

**Fields:**
- `from_phase: str` - Source phase name
- `to_phase: str` - Target phase name
- `when: Guard` - Predicate that must evaluate to True (default: always)
- `priority: int` - For conflict resolution (default: 0, higher = preferred)

**Example:**
```python
Transition(
    from_phase="review",
    to_phase="done",
    when=When.channel_has("verdict", "approve"),
    priority=10,
)
```

### Workflow

Top-level workflow definition.

**Fields:**
- `agents: List[Agent]` - Agent definitions
- `channels: List[Channel]` - Channel definitions
- `phases: List[Phase]` - Phase definitions
- `transitions: List[Transition]` - Transition rules
- `initial_phase: Optional[str]` - Starting phase (defaults to first)
- `metadata: Dict[str, Any]` - Additional metadata

**Example:**
```python
workflow = Workflow(
    agents=[...],
    channels=[...],
    phases=[...],
    transitions=[...],
    initial_phase="plan",
)
```

---

## Guards

Guards are predicates that control transition firing. They evaluate runtime context to determine which path to take.

### Basic Guards

#### `When.always()`
Always evaluates to True (unconditional transition).

```python
Transition(from_phase="plan", to_phase="implement", when=When.always())
```

#### `When.never()`
Always evaluates to False (disabled transition).

```python
Transition(from_phase="plan", to_phase="blocked", when=When.never())
```

#### `When.channel_has(channel, value)`
Checks if a channel has a specific value.

```python
When.channel_has("verdict", "approve")
When.channel_has("status", "ready")
```

#### `When.empty(channel)`
Checks if a channel is empty/None.

```python
When.empty("feedback")  # True if no feedback provided
```

#### `When.verdict(verdict_string)`
Checks review verdict (case-insensitive).

```python
When.verdict("approve")
When.verdict("changes_requested")
When.verdict("blocked")
```

#### `When.git_changes(required=True)`
Checks if git changes occurred.

```python
When.git_changes(required=True)   # Must have changes
When.git_changes(required=False)  # Must NOT have changes
```

### Boolean Combinators

#### `When.all(*guards)`
AND logic - all guards must pass.

```python
When.all(
    When.channel_has("verdict", "approve"),
    When.git_changes(required=True),
)
```

#### `When.any(*guards)`
OR logic - at least one guard must pass.

```python
When.any(
    When.channel_has("verdict", "approve"),
    When.channel_has("verdict", "skip"),
)
```

#### `When.not_(guard)`
NOT logic - negates a guard.

```python
When.not_(When.empty("feedback"))  # Feedback must be provided
```

### Guard Composition

Complex conditions can be built by nesting combinators:

```python
# Approve if: (verdict is approve AND git changes exist) OR verdict is skip
When.any(
    When.all(
        When.verdict("approve"),
        When.git_changes(required=True),
    ),
    When.verdict("skip"),
)

# Replan if: changes requested AND feedback provided
When.all(
    When.verdict("changes_requested"),
    When.not_(When.empty("feedback")),
)
```

### Guard Context

Guards receive a context dictionary at runtime:

```python
{
    "verdict": "approve",                    # From response.verdict
    "git_changes": {                         # From git operations
        "has_changes": True,
        "files_changed": 5,
        "insertions": 120,
        "deletions": 30,
    },
    "task": "Implement user auth",           # Channel payloads
    "plan": "1. Add login endpoint...",
    "feedback": "Focus on error handling",
    # ... other channel values
}
```

---

## Examples

### Minimal Workflow

```python
from duet.dsl import Agent, Phase, Transition, Workflow

workflow = Workflow(
    agents=[
        Agent(name="worker", provider="codex", model="gpt-5-codex"),
    ],
    channels=[],  # No channels for simple workflow
    phases=[
        Phase(name="work", agent="worker"),
        Phase(name="done", agent="worker", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="work", to_phase="done"),
    ],
)
```

### Standard Plan → Implement → Review

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
            description="Execute plan",
        ),
        Phase(
            name="review",
            agent="reviewer",
            consumes=["plan", "code"],
            publishes=["verdict", "feedback"],
            description="Review implementation",
        ),
        Phase(name="done", agent="reviewer", is_terminal=True),
        Phase(name="blocked", agent="reviewer", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
        Transition(from_phase="implement", to_phase="review"),
        Transition(
            from_phase="review",
            to_phase="done",
            when=When.verdict("approve"),
            priority=10,
        ),
        Transition(
            from_phase="review",
            to_phase="plan",
            when=When.verdict("changes_requested"),
            priority=5,
        ),
        Transition(
            from_phase="review",
            to_phase="blocked",
            when=When.verdict("blocked"),
            priority=15,
        ),
    ],
)
```

### Multi-Stage Pipeline

```python
workflow = Workflow(
    agents=[
        Agent(name="planner", provider="codex", model="gpt-5-codex"),
        Agent(name="implementer", provider="claude", model="sonnet"),
        Agent(name="tester", provider="claude", model="sonnet"),
        Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
    ],
    channels=[
        Channel(name="task", schema="text"),
        Channel(name="plan", schema="text"),
        Channel(name="code", schema="git_diff"),
        Channel(name="test_result", schema="json"),
        Channel(name="verdict", schema="verdict"),
    ],
    phases=[
        Phase(name="plan", agent="planner",
              consumes=["task"], publishes=["plan"]),
        Phase(name="implement", agent="implementer",
              consumes=["plan"], publishes=["code"]),
        Phase(name="test", agent="tester",
              consumes=["code"], publishes=["test_result"]),
        Phase(name="review", agent="reviewer",
              consumes=["plan", "code", "test_result"], publishes=["verdict"]),
        Phase(name="done", agent="reviewer", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
        Transition(from_phase="implement", to_phase="test"),
        Transition(
            from_phase="test",
            to_phase="review",
            when=When.channel_has("test_result", "pass"),
        ),
        Transition(
            from_phase="test",
            to_phase="implement",
            when=When.channel_has("test_result", "fail"),
        ),
        Transition(from_phase="review", to_phase="done",
                   when=When.verdict("approve")),
    ],
)
```

---

## Best Practices

### 1. Name Uniqueness

All names (agents, channels, phases) must be unique within a workflow.

```python
# ❌ BAD - duplicate agent names
agents=[
    Agent(name="codex", provider="codex", model="gpt-5"),
    Agent(name="codex", provider="codex", model="gpt-6"),  # Error!
]

# ✅ GOOD - unique names
agents=[
    Agent(name="planner", provider="codex", model="gpt-5"),
    Agent(name="reviewer", provider="codex", model="gpt-5"),
]
```

### 2. Channel Declaration

Always declare channels before referencing them in phases.

```python
# ❌ BAD - undeclared channel
phases=[
    Phase(name="plan", agent="planner", publishes=["plan"]),  # Error!
]

# ✅ GOOD - declare channel first
channels=[
    Channel(name="plan", schema="text"),
]
phases=[
    Phase(name="plan", agent="planner", publishes=["plan"]),
]
```

### 3. Terminal Phases

Mark phases with no outgoing transitions as terminal.

```python
# ❌ BAD - no outgoing, not terminal
phases=[
    Phase(name="done", agent="reviewer"),  # Error: missing is_terminal=True
]

# ✅ GOOD - explicit terminal marker
phases=[
    Phase(name="done", agent="reviewer", is_terminal=True),
]
```

### 4. Transition Priorities

Use priorities to control evaluation order when multiple guards could match.

```python
transitions=[
    # Higher priority checked first
    Transition(from_phase="review", to_phase="blocked",
               when=When.verdict("blocked"), priority=15),
    Transition(from_phase="review", to_phase="done",
               when=When.verdict("approve"), priority=10),
    Transition(from_phase="review", to_phase="plan",
               when=When.verdict("changes_requested"), priority=5),
]
```

### 5. Descriptive Names and Schemas

Use clear, semantic names and document channel purposes.

```python
# ✅ GOOD - clear, documented
Channel(
    name="implementation_plan",
    description="Detailed plan with steps, files, and risks",
    schema="text",
)

# ❌ BAD - vague, undocumented
Channel(name="data")
```

---

## Advanced Patterns

### Conditional Replanning

Replan only if feedback is provided:

```python
Transition(
    from_phase="review",
    to_phase="plan",
    when=When.all(
        When.verdict("changes_requested"),
        When.not_(When.empty("feedback")),
    ),
)
```

### Multi-Path Approval

Approve on verdict OR if git changes are minor:

```python
Transition(
    from_phase="review",
    to_phase="done",
    when=When.any(
        When.verdict("approve"),
        When.all(
            When.verdict("skip"),
            When.not_(When.git_changes(required=True)),
        ),
    ),
)
```

### Retry Logic

Loop back to same phase on specific conditions:

```python
Transition(
    from_phase="implement",
    to_phase="implement",
    when=When.channel_has("retry", "true"),
    priority=5,
)
Transition(
    from_phase="implement",
    to_phase="review",
    when=When.always(),
    priority=1,
)
```

### A/B Testing Agents

Define multiple agents for the same role:

```python
agents=[
    Agent(name="planner_stable", provider="codex", model="gpt-5-codex"),
    Agent(name="planner_experimental", provider="codex", model="gpt-6-preview"),
]

# Switch agent in phase definition
Phase(name="plan", agent="planner_experimental", ...)
```

### Custom Channels for Domain Data

```python
channels=[
    Channel(name="performance_metrics", schema="json",
            description="Benchmark results: latency, throughput, memory"),
    Channel(name="security_scan", schema="json",
            description="Vulnerability scan results"),
    Channel(name="coverage_report", schema="json",
            description="Test coverage statistics"),
]

Phase(
    name="quality_gate",
    agent="validator",
    consumes=["performance_metrics", "security_scan", "coverage_report"],
    publishes=["quality_verdict"],
)
```

---

## Migration Guide

### From Prompt Templates to DSL

**Before (Legacy):**
```
.duet/prompts/plan.md       # Static text template
.duet/prompts/implement.md
.duet/prompts/review.md
```

**After (Sprint 9):**
```python
# .duet/ide.py
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
    ],
    phases=[
        Phase(name="plan", agent="planner",
              consumes=["task"], publishes=["plan"],
              description="Draft implementation plan"),
        Phase(name="implement", agent="implementer",
              consumes=["plan"], publishes=["code"],
              description="Execute plan"),
        Phase(name="review", agent="reviewer",
              consumes=["plan", "code"], publishes=["verdict"],
              description="Review implementation"),
        Phase(name="done", agent="reviewer", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
        Transition(from_phase="implement", to_phase="review"),
        Transition(from_phase="review", to_phase="done",
                   when=When.verdict("approve")),
        Transition(from_phase="review", to_phase="plan",
                   when=When.verdict("changes_requested")),
    ],
)
```

### Benefits of DSL

1. **Type Safety**: Catch errors at load time, not runtime
2. **Validation**: Compiler checks references, reachability, terminals
3. **Composition**: Reuse guards, combine with boolean logic
4. **Versioning**: Track workflow changes in git
5. **Tooling**: IDE autocomplete, refactoring, linting
6. **Flexibility**: Dynamic workflows, conditional logic, priorities

---

## Workflow Loading

### Resolution Precedence

Duet searches for workflow definitions in this order:

1. **Explicit path**: `--workflow-path` CLI argument
2. **Environment variable**: `DUET_WORKFLOW_PATH`
3. **Workspace**: `<workspace_root>/.duet/ide.py`
4. **Current directory**: `./.duet/ide.py`

### Export Options

Workflows can be exported two ways:

**Option 1: Variable export**
```python
# .duet/ide.py
from duet.dsl import Workflow, ...

workflow = Workflow(...)
```

**Option 2: Function export**
```python
# .duet/ide.py
from duet.dsl import Workflow, ...

def get_workflow():
    # Can include dynamic logic
    return Workflow(...)
```

### Validation

The loader performs these checks:

1. Module imports successfully (no syntax/import errors)
2. Exports `workflow` variable or `get_workflow()` function
3. Export is a `Workflow` instance
4. Workflow compiles (unique names, valid references)
5. All referenced channels/agents/phases exist
6. No unreachable phases
7. Terminal phases marked correctly

Errors include:
- File path (e.g., `/path/to/.duet/ide.py`)
- Specific validation failures (e.g., "Phase 'plan' consumes unknown channel: 'task'")
- Available exports in module

---

## Troubleshooting

### Workflow not found

```
Error: Workflow file not found: /path/.duet/ide.py
```

**Solution**: Run `duet init` to generate `.duet/ide.py`

### Compilation failed

```
Workflow validation failed: /path/.duet/ide.py
  - Phase 'plan' references unknown agent: 'planner'
```

**Solution**: Ensure all agents are declared in `agents=[]` list

### Module import error

```
Failed to import workflow module: /path/.duet/ide.py
Error: No module named 'custom_module'
```

**Solution**: Install dependencies or fix import statements

### Wrong workflow type

```
Module exports 'workflow' but it's not a Workflow instance: <class 'str'>
```

**Solution**: Import from `duet.dsl` and instantiate `Workflow` class

---

## API Reference

For complete API details, see:
- `src/duet/dsl/workflow.py` - Core DSL classes
- `src/duet/dsl/compiler.py` - Compilation and validation
- `src/duet/workflow_loader.py` - Loading and resolution

## Testing

Test your workflow:

```python
from duet.dsl.compiler import compile_workflow

# Your workflow definition
workflow = Workflow(...)

# Compile and validate
graph = compile_workflow(workflow)

print(f"Phases: {len(graph.phases)}")
print(f"Agents: {len(graph.agents)}")
print(f"Channels: {len(graph.channels)}")
```

Or use the loader:

```bash
# Validate workflow
python -c "from duet.workflow_loader import load_workflow; load_workflow()"
```

---

## Future Extensions

Planned enhancements:
- Runtime channel payload validation against schemas
- Channel seeding from CLI inputs (task from user)
- Guard context enrichment (metrics, timings, history)
- Dynamic workflow modification (add phases at runtime)
- Workflow visualization (graph rendering)
- Hot-reload support (module cache management)

---

For questions or issues, see: https://github.com/femtomc/duet
