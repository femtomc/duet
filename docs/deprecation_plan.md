# Duet Deprecation & Cleanup Plan

## Overview

This document tracks deprecated fields and features scheduled for removal
as the facet architecture stabilizes.

---

## Phase 1: Immediate (Current State)

### Deprecated Fields (Still Present):

**Phase:**
- `consumes: List[Channel]` - Use `phase.get_reads()` or step-based `.read()`
- `publishes: List[Channel]` - Use `phase.get_writes()` or step-based `.call_agent()/.write()`
- `tools: List[Tool]` - Use ToolStep in phase.steps

**Status:** Marked with `TODO(DSL-cleanup)` comments
**Fallback:** get_reads()/get_writes() fall back to these if no steps
**Removal:** Once all workflows use step-based syntax

### Deprecated Config Flags:

**WorkflowConfig:**
- `require_git_changes: bool` - Ignored (use GitChangeTool)
- `max_consecutive_replans: int` - Ignored (will be conversation pattern)

**Status:** Present but not enforced
**Removal:** Next cleanup sweep

### Deprecated Methods:

**Phase:**
- `.with_tool(tool)` - Use `.tool(tool, outputs=[...])` instead
- `.counts_as_replan(loop_to)` - No-op (will be conversation pattern)

**Status:** Work but discouraged
**Removal:** After step-based adoption

---

## Phase 2: Next Cleanup Sweep

### Target for Removal:

1. **Phase.consumes field**
   - All workflows using step-based `.read()` or old executor updated
   - Compiler validates via get_reads() only
   - Remove field and validation code

2. **Phase.publishes field**
   - All workflows using step-based `.call_agent()/.write()`
   - Compiler validates via get_writes() only
   - Remove field and validation code

3. **Phase.tools field**
   - All tools moved to ToolStep in phase.steps
   - Remove with_tool() method
   - Remove field

4. **Config flags**
   - Remove `require_git_changes` from WorkflowConfig
   - Remove `max_consecutive_replans` from WorkflowConfig
   - Update config validation

5. **Metadata helpers**
   - Remove `.counts_as_replan()` (no-op)
   - Consider removing `.with_human()/.requires_git()` if redundant with `.human()/.tool()`

### Prerequisites:

- [ ] All test workflows migrated to steps
- [ ] Old executor path removed or clearly deprecated
- [ ] Migration guide published
- [ ] Users notified of deprecation

---

## Phase 3: Dual Execution Path Decision

### Current State:

Orchestrator has two execution paths:
```python
if phase.steps:
    # New: FacetRunner (step-based)
    response = _execute_facet_script(...)
else:
    # Old: Adapter (consumes/publishes)
    response = adapter.stream(...)
```

### Options:

**Option A: Remove Old Path**
- Force all phases to use steps
- Single execution model
- Simpler codebase
- Requires migration effort

**Option B: Maintain Both**
- Support both models long-term
- More complexity
- Easier migration
- Ongoing maintenance burden

**Decision Needed:** Choose A or B based on migration timeline

### If Option A (Recommended):

1. Migrate all internal test workflows to steps
2. Publish migration guide
3. Deprecate old path with warnings
4. Remove after deprecation period (1-2 releases)

### If Option B:

1. Clearly document both paths
2. Ensure parity in features
3. Test both paths equally
4. Accept ongoing complexity

---

## Phase 4: Full Facet Migration

### When Steps-Only:

**Remove:**
- Old `.consume()/.publish()` fluent methods
- `Phase.consumes/publishes` fields
- Executor code path for old-style phases
- All consumes/publishes validation in compiler

**Keep:**
- Step-based fluent API only
- FacetRunner execution
- Step introspection (get_reads/get_writes)

**Benefits:**
- Single execution model
- Simpler codebase
- Clear semantics
- Better error messages

---

## Timeline Recommendation

### Sprint N (Current):
- ✅ Mark fields deprecated with TODO comments
- ✅ Implement step validation
- ✅ Add migration hints to error messages
- ⬜ Publish migration guide

### Sprint N+1:
- Migrate all internal tests to step-based syntax
- Add deprecation warnings to old methods
- Measure adoption

### Sprint N+2:
- Remove deprecated fields if adoption high
- OR extend deprecation if migration incomplete

### Sprint N+3:
- Remove dual execution paths
- Single facet-only model
- Clean, minimal codebase

---

## Migration Checklist

Before removing deprecated features:

- [ ] Migration guide published
- [ ] All duet init templates use new syntax
- [ ] All example workflows use new syntax
- [ ] All internal tests migrated
- [ ] Deprecation warnings added to old methods
- [ ] Users notified (changelog, docs, warnings)
- [ ] Grace period elapsed (2+ releases)

---

## Code Markers

Use these markers to track cleanup:

```python
# TODO(DSL-cleanup): Remove this field once workflows migrated
# TODO(step-validation): Implement ordering checks
# TODO(migration): Update after dual-path decision
```

Search codebase for TODOs to track progress.

---

## Related Documents

- `docs/session_review.md` - Technical review of transformation
- `docs/facet_architecture_plan.md` - Long-term architecture vision
- Future: `docs/migration_guide.md` - User migration instructions
