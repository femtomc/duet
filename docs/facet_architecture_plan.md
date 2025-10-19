# Facet Architecture Evolution Plan

## Vision

Evolve Duet from sequential orchestration to a Syndicate-style dataspace with reactive facets.

## Phases

### 1. Expressive Facet Scripts (DSL-2/DSL-3)
**Status: In Progress**

- ✅ Fluent Phase API implemented (`.consume()`, `.publish()`, `.describe()`)
- ✅ Tool interface defined (ToolContext, ToolResult, BaseTool)
- ✅ Phase tool attachment (`.with_tool()`)
- 🔄 Lock down fluent scripts to be deterministic
- 🔄 Introduce workflow combinators (`.and_then()`, `.if_else()`)
- 🔄 Make structure explicit (DAG instead of hidden metadata)

**Current State:**
```python
# Phase is a shell for future facet scripts
review = (
    Phase(name="review", agent="reviewer")
    .consume(plan, code)  # Declarative read
    .publish(verdict, feedback)  # Declarative write
    .with_tool(ValidationTool())  # Deterministic tool
)
```

**Target State:**
```python
# Phase becomes a facet script
review = (
    Facet(name="review")
    .read(plan, code)  # Subscribe to facts
    .tool(ValidationTool())  # Pre-processing
    .agent("reviewer")  # Agent invocation
    .human("approval_required")  # Conversation pattern
    .write(verdict, feedback)  # Assert facts
)
```

### 2. Structured Dataspace Representation (DSL-4)

**Goals:**
- Channel updates become structured facts: `PlanDoc(task, doc)`, `ReviewVerdict(task, status)`
- Maintain dataspace object: `assert_fact()`, `retract_fact()`, `subscribe(pattern)`
- Phase `.read()` calls become subscriptions
- Phase outputs translate to assertions

**Current:**
```python
channels["plan"] = "implementation plan text..."
```

**Target:**
```python
dataspace.assert_fact(PlanDoc(task_id="...", content="...", metadata={...}))
```

### 3. Facet Execution Runtime

**Goals:**
- Wrap each phase script in a "facet runner"
- Facets watch for their inputs (facts in dataspace) and fire when present
- Event-driven scheduler instead of sequential loop
- When a facet asserts something, subscribed facets become ready

**Current:**
```python
while not terminal:
    response = adapter.call(phase)
    decision = evaluate_guards()
    current_phase = decision.next_phase
```

**Target:**
```python
scheduler = FacetScheduler(dataspace)
for facet in workflow.facets:
    scheduler.subscribe(facet, facet.input_pattern)

while scheduler.has_ready_facets():
    facet = scheduler.next_ready()
    result = facet.run(dataspace)
    dataspace.assert_facts(result.outputs)
    # Subscribed facets automatically become ready
```

### 4. Conversation/Policy Infrastructure

**Goals:**
- Approvals, git checks, tools as conversations in dataspace
- `ApprovalNeeded(task)` awaits `ApprovalGranted(task)`
- Facets suspend until response facts appear

**Example:**
```python
approval_facet = (
    Facet(name="approve")
    .read(ApprovalNeeded)  # Subscribe to approval requests
    .human("review_required")  # Suspend for human
    .write(ApprovalGranted)  # Assert grant fact
)
```

### 5. Actor Multitenancy

**Goals:**
- Each workflow run is an actor with its own dataspace view
- Cross-run coordination through shared facts
- True concurrency, supervision, actor restarts

---

## Migration Strategy

### Phase 1: Cleanup (Current Sprint)
- Remove metadata-based guardrails from orchestrator
- Remove global config flags (require_git_changes)
- Simplify Phase to be a shell
- Keep fluent API but prepare for tool-based steps

### Phase 2: Tool-Based Policies
- Implement actual Tool execution in orchestrator
- Convert git checks, approvals to tools
- Tools read/write channels deterministically

### Phase 3: Structured Facts
- Define fact types (PlanDoc, ReviewVerdict, etc.)
- Implement dataspace (assert, retract, subscribe)
- Convert channels to fact storage

### Phase 4: Facet Scheduler
- Implement event-driven scheduler
- Wrap phases in facet runners
- Replace sequential loop

### Phase 5: Actor System
- Multi-actor support
- Dataspace isolation/sharing
- Supervision trees

---

## Why This Matters

**Current Limitations:**
- Sequential execution (can't parallelize independent phases)
- Hidden behavior in metadata (hard to reason about)
- Tight coupling between orchestrator and phase logic
- No clear conversation patterns

**Facet Architecture Benefits:**
- Declarative, deterministic phase scripts
- Reactive execution (phases fire when inputs ready)
- Clear conversation patterns (request/response facts)
- Path to concurrency and actor model
- Better testability (facets are pure functions over dataspace)

---

## Implementation Notes

- Keep backward compatibility during migration
- Each sprint adds new capability without breaking existing workflows
- Tests validate both old and new patterns during transition
- Documentation tracks evolution clearly
