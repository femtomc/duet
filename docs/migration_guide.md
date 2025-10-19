# Duet DSL Migration Guide

## Overview

This guide helps you migrate from string-based workflows to the new object-based
facet DSL introduced in Sprint DSL-1 through DSL-5.

**BREAKING CHANGE:** String-based workflow references no longer work. All workflows
must use Phase/Channel objects.

---

## Quick Migration Checklist

- [ ] Define all Channel objects as variables
- [ ] Define all Phase objects as variables
- [ ] Update Transition references to use Phase objects
- [ ] Update When.channel_has() to use Channel objects
- [ ] Update Workflow.initial_phase to use Phase object
- [ ] Update Workflow.task_channel to use Channel object
- [ ] (Optional) Convert to step-based facet syntax

---

## Breaking Change 1: Object-Based DSL

### Before (String-Based - BROKEN):
```python
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    channels=[
        Channel(name="task"),
        Channel(name="plan"),
    ],
    phases=[
        Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
        Phase(name="implement", agent="implementer", consumes=["plan"], publishes=["code"]),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
    ],
    initial_phase="plan",
)
```

### After (Object-Based - REQUIRED):
```python
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

# 1. Define channels as variables
task = Channel(name="task")
plan_channel = Channel(name="plan")
code = Channel(name="code")

# 2. Define phases as variables (reference Channel objects)
plan = Phase(
    name="plan",
    agent="planner",
    consumes=[task],  # Channel object, not string
    publishes=[plan_channel],
)

implement = Phase(
    name="implement",
    agent="implementer",
    consumes=[plan_channel],
    publishes=[code],
)

# 3. Reference Phase objects in workflow
workflow = Workflow(
    channels=[task, plan_channel, code],
    phases=[plan, implement],
    transitions=[
        Transition(from_phase=plan, to_phase=implement),  # Phase objects
    ],
    initial_phase=plan,  # Phase object, not string
)
```

**Error if you use strings:**
```
TypeError: Transition from_phase must be Phase object, got <class 'str'>.
Migration: Define phases as variables, then reference them:
  plan = Phase(name='plan', ...)
  Transition(from_phase=plan, to_phase=...)
```

---

## Breaking Change 2: Guards Require Channel Objects

### Before (String - BROKEN):
```python
When.channel_has("verdict", "approve")
```

### After (Object - REQUIRED):
```python
verdict = Channel(name="verdict")
When.channel_has(verdict, "approve")
```

**Error if you use strings:**
```
TypeError: ChannelHasGuard requires Channel object, got <class 'str'>.
Migration: Define channel as variable:
  verdict = Channel(name='verdict')
  When.channel_has(verdict, 'approve')
```

---

## Optional: Facet Script Syntax

The new step-based facet syntax is optional but recommended for new workflows.

### Traditional Syntax (Still Works):
```python
plan = Phase(
    name="plan",
    agent="planner",
    consumes=[task, feedback],
    publishes=[plan_channel],
)
```

### Facet Script Syntax (Recommended):
```python
plan = (
    Phase(name="plan", agent="planner")
    .read(task, feedback)                    # ReadStep: load inputs
    .tool(ValidationTool())                  # ToolStep: enrich context
    .call_agent("planner", writes=[plan_channel])  # AgentStep: invoke AI
)
```

**Benefits of Facet Syntax:**
- Explicit execution order
- Tool integration
- Human approval steps
- Context management
- Deterministic dataflow

---

## Migration Steps

### Step 1: Define Channels

**Before:**
```python
channels=[Channel(name="task"), Channel(name="plan")]
```

**After:**
```python
# Define as variables
task = Channel(name="task", schema="text")
plan = Channel(name="plan", schema="text")

# Reference in workflow
channels=[task, plan]
```

### Step 2: Define Phases

**Before:**
```python
phases=[
    Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
]
```

**After:**
```python
# Define channels first
task = Channel(name="task")
plan_ch = Channel(name="plan")

# Define phases (reference Channel objects)
plan_phase = Phase(
    name="plan",
    agent="planner",
    consumes=[task],  # Object, not string
    publishes=[plan_ch],
)

# Reference in workflow
phases=[plan_phase]
```

### Step 3: Update Transitions

**Before:**
```python
Transition(from_phase="plan", to_phase="implement")
```

**After:**
```python
# Define phases first
plan = Phase(name="plan", ...)
implement = Phase(name="implement", ...)

# Reference Phase objects
Transition(from_phase=plan, to_phase=implement)
```

### Step 4: Update Guards

**Before:**
```python
When.channel_has("verdict", "approve")
When.empty("feedback")
```

**After:**
```python
# Define channels first
verdict = Channel(name="verdict")
feedback = Channel(name="feedback")

# Reference Channel objects
When.channel_has(verdict, "approve")
When.empty(feedback)
```

### Step 5: Update Workflow Config

**Before:**
```python
Workflow(
    ...,
    initial_phase="plan",
    task_channel="task",
)
```

**After:**
```python
# Define objects first
plan = Phase(name="plan", ...)
task = Channel(name="task")

Workflow(
    ...,
    initial_phase=plan,  # Phase object
    task_channel=task,   # Channel object
)
```

---

## Complete Example Migration

### Before (Old DSL):
```python
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    agents=[
        Agent(name="planner", provider="codex", model="gpt-5-codex"),
        Agent(name="implementer", provider="claude", model="sonnet"),
        Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
    ],
    channels=[
        Channel(name="task"),
        Channel(name="plan"),
        Channel(name="code"),
        Channel(name="verdict"),
    ],
    phases=[
        Phase(name="plan", agent="planner", consumes=["task"], publishes=["plan"]),
        Phase(name="implement", agent="implementer", consumes=["plan"], publishes=["code"]),
        Phase(name="review", agent="reviewer", consumes=["plan", "code"], publishes=["verdict"]),
        Phase(name="done", agent="reviewer", is_terminal=True),
    ],
    transitions=[
        Transition(from_phase="plan", to_phase="implement"),
        Transition(from_phase="implement", to_phase="review"),
        Transition(from_phase="review", to_phase="done", when=When.channel_has("verdict", "approve")),
    ],
    initial_phase="plan",
    task_channel="task",
)
```

### After (New DSL):
```python
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

# Define channels
task = Channel(name="task", schema="text")
plan = Channel(name="plan", schema="text")
code = Channel(name="code", schema="git_diff")
verdict = Channel(name="verdict", schema="verdict")

# Define phases
plan_phase = Phase(
    name="plan",
    agent="planner",
    consumes=[task],
    publishes=[plan],
)

implement = Phase(
    name="implement",
    agent="implementer",
    consumes=[plan],
    publishes=[code],
)

review = Phase(
    name="review",
    agent="reviewer",
    consumes=[plan, code],
    publishes=[verdict],
)

done = Phase(name="done", agent="reviewer", is_terminal=True)

# Define workflow
workflow = Workflow(
    agents=[
        Agent(name="planner", provider="codex", model="gpt-5-codex"),
        Agent(name="implementer", provider="claude", model="sonnet"),
        Agent(name="reviewer", provider="codex", model="gpt-5-codex"),
    ],
    channels=[task, plan, code, verdict],
    phases=[plan_phase, implement, review, done],
    transitions=[
        Transition(from_phase=plan_phase, to_phase=implement),
        Transition(from_phase=implement, to_phase=review),
        Transition(from_phase=review, to_phase=done, when=When.channel_has(verdict, "approve")),
    ],
    initial_phase=plan_phase,
    task_channel=task,
)
```

---

## Advanced: Facet Script Syntax

For new workflows, consider using the step-based facet syntax:

```python
from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow
from duet.dsl.tools import GitChangeTool

# Define channels
task = Channel(name="task", schema="text")
plan = Channel(name="plan", schema="text")
code = Channel(name="code", schema="git_diff")
verdict = Channel(name="verdict", schema="verdict")
status = Channel(name="status", schema="text")

# Define facet scripts with explicit steps
plan_phase = (
    Phase(name="plan", agent="planner")
    .read(task)                                # ReadStep
    .call_agent("planner", writes=[plan])      # AgentStep
)

implement = (
    Phase(name="implement", agent="implementer")
    .read(plan)
    .call_agent("implementer", writes=[code])
    .tool(GitChangeTool(require_changes=True))  # ToolStep: validate git
    .write(status, value="implemented")        # WriteStep
)

review = (
    Phase(name="review", agent="reviewer")
    .read(plan, code)
    .human("Code review required")             # HumanStep: pause
    .call_agent("reviewer", writes=[verdict])
)

done = Phase.terminal_phase("done", "reviewer")

# Workflow definition (same as before)
workflow = Workflow(
    agents=[...],
    channels=[task, plan, code, verdict, status],
    phases=[plan_phase, implement, review, done],
    transitions=[
        Transition(from_phase=plan_phase, to_phase=implement),
        Transition(from_phase=implement, to_phase=review),
        Transition(from_phase=review, to_phase=done, when=When.channel_has(verdict, "approve")),
    ],
    initial_phase=plan_phase,
    task_channel=task,
)
```

---

## Common Errors & Fixes

### Error: "Phase.consumes must contain Channel objects"

**Cause:** Using string in consumes list

**Fix:**
```python
# Wrong:
Phase(consumes=["task"], ...)

# Right:
task = Channel(name="task")
Phase(consumes=[task], ...)
```

### Error: "Transition from_phase must be Phase object"

**Cause:** Using string in transition

**Fix:**
```python
# Wrong:
Transition(from_phase="plan", to_phase="implement")

# Right:
plan = Phase(name="plan", ...)
implement = Phase(name="implement", ...)
Transition(from_phase=plan, to_phase=implement)
```

### Error: "ChannelHasGuard requires Channel object"

**Cause:** Using string in guard

**Fix:**
```python
# Wrong:
When.channel_has("verdict", "approve")

# Right:
verdict = Channel(name="verdict")
When.channel_has(verdict, "approve")
```

---

## Deprecated Features

### Metadata-Based Guardrails (Removed)

**Old way (no longer works):**
```python
Phase(..., metadata={"requires_approval": True, "git_changes_required": True})
```

**New way:**
```python
phase = (
    Phase(name="implement", agent="dev")
    .read(...)
    .call_agent("dev", writes=[...])
    .requires_git()  # Attaches GitChangeTool
)
```

### Global Config Flags (Ignored)

- `require_git_changes` - Use GitChangeTool instead
- `max_consecutive_replans` - Will be conversation pattern

---

## Testing Your Migration

```bash
# Validate workflow compiles
duet lint

# Test workflow loads
python -c "from duet.workflow_loader import load_workflow; load_workflow()"

# Run workflow
duet run
```

---

## Need Help?

- See `docs/workflow_dsl.md` for complete DSL reference
- See `examples/` for example workflows
- See `tests/fixtures/` for test workflows
- Report issues at: https://github.com/femtomc/duet/issues

---

## Summary of Changes

**Sprint DSL-1:** Object-only references (Phase/Channel objects, no strings)
**Sprint DSL-2:** Fluent builders and Tool interface
**Sprint DSL-3:** Facet script model (ordered steps)
**Sprint DSL-4:** Facet execution runtime
**Sprint DSL-5:** Dataspace with structured facts

All changes aim to enable Syndicate-style reactive facet architecture with
type-safe, explicit dataflow.
