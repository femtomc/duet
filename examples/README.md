# Duet Workflow Examples

This directory contains example workflow definitions demonstrating various Duet features.

## Examples

### `custom_channels_workflow.py`

Demonstrates how to use custom channels beyond the default `plan`, `code`, and `verdict`.

**Features:**
- Custom channels for test results, documentation, and metrics
- Multiple agents (planner, implementer, tester, documenter, reviewer)
- Conditional transitions based on test status
- Automatic fix-test loops for failing tests
- Echo adapter configuration for testing

**Usage:**
```bash
# 1. Copy to your workspace
cp examples/custom_channels_workflow.py .duet/workflow.py

# 2. Validate the workflow
duet lint

# 3. Run the workflow
duet run
```

**Custom Channels:**
- `tests` - Test results and coverage report (JSON schema)
- `test_status` - Test pass/fail status ("pass", "fail", "skip")
- `docs` - Updated documentation content
- `metrics` - Performance metrics (runtime, memory, etc.)

**Workflow Flow:**
```
PLAN → IMPLEMENT → TEST → [pass] → DOCUMENT → REVIEW → DONE
                      ↓
                   [fail]
                      ↓
                  FIX_TESTS → TEST (loop)
```

## Creating Your Own Workflow

1. **Start with a template**:
   ```bash
   duet init  # Creates .duet/workflow.py with default template
   ```

2. **Define your workflow with inline agent config**:
   ```python
   from duet.dsl import Agent, Channel, Phase, Transition, Workflow

   workflow = Workflow(
       agents=[
           Agent(
               name="implementer",
               provider="claude-code",
               model="sonnet",
               auto_approve=True,  # Inline config - no duet.yaml needed!
               timeout=600,
           ),
       ],
       channels=[...],
       phases=[...],
       transitions=[...],
   )
   ```

   **Agent Configuration**:
   - **provider**: "codex", "claude-code", or "echo"
   - **model**: Model identifier
   - **auto_approve**: Skip permission prompts (use with caution)
   - **timeout**: Max execution time in seconds
   - **cli_path**: Custom CLI executable path
   - **description**: Human-readable description

3. **Validate**:
   ```bash
   duet lint  # Catches errors before running
   ```

4. **Run**:
   ```bash
   duet run --workflow .duet/workflow.py    # Use specific workflow
   duet run                                  # Use default .duet/workflow.py
   ```

### Configuration Precedence

**Inline (workflow.py)** → **Override (duet.yaml)** → **Adapter defaults**

- Workflow defines base capabilities (auto_approve, timeouts)
- duet.yaml can override for security (credentials, paths)
- Adapter defaults fill in unspecified values

This allows workflows to be **self-contained and portable** while keeping secrets external.

## Tips

### Using Custom Channels

Custom channels require adapters to return values in their `metadata`:

```python
# In your adapter's response:
response.metadata = {
    "tests": json.dumps({"passed": 42, "failed": 0}),
    "test_status": "pass",
    "metrics": json.dumps({"runtime_ms": 1234})
}
```

The orchestrator automatically persists these to the database (no manual wiring needed!).

### Testing Workflows

Use the **echo adapter** for testing without real Codex/Claude credentials:

```yaml
codex:
  provider: "echo"
  model: "echo-v1"
```

The echo adapter auto-approves reviews, making it perfect for testing workflow logic.

### Debugging

```bash
# Validate workflow structure
duet lint

# Check run status
duet status <run-id>

# Inspect channel history
duet messages <run-id> --channel tests

# View detailed events
duet inspect <run-id>
```

## Documentation

- **Workflow DSL Reference**: [`docs/workflow_dsl.md`](../docs/workflow_dsl.md)
- **Channel Schemas**: See `duet.dsl.Channel` documentation
- **Guard Types**: `When.always()`, `When.channel_has()`, `When.all()`, `When.any()`
