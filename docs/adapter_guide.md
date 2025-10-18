# Adapter Guide

Duet uses a pluggable adapter system to integrate with different AI assistants. This guide covers the available adapters, how to configure them, and how to implement custom adapters.

## Available Adapters

### Echo Adapter (`echo`)

**Purpose**: Testing and development without API calls
**Use Case**: Local validation, CI/CD testing, development

The Echo adapter is a simple passthrough adapter that mirrors the prompt back in the response. It's useful for:
- Testing orchestration logic without making real API calls
- Validating artifact persistence and state transitions
- Development without requiring authentication

**Configuration**:
```yaml
codex:
  provider: "echo"
  model: "any-model-name"
```

**Behavior**:
- Returns the prompt wrapped in `[ECHO ADAPTER]` markers
- Never sets `concluded=True` (will always loop until max iterations)
- Includes context keys in the response for debugging

---

### Codex Adapter (`codex`)

**Purpose**: Planning and review via the Codex CLI

**Prerequisites**
1. Install the Codex CLI.
2. Authenticate: `codex auth login`.
3. Verify the installation: `codex --version`.

**Configuration**
```yaml
codex:
  provider: "codex"
  model: "gpt-5-codex"
  timeout: 300
```

**CLI Invocation**
```bash
codex exec --json --model <model> "<prompt text>"
```

Codex streams JSONL events. The adapter reads each line, extracts the final `agent_message`, and records metadata such as token usage and the ordered list of event types. Invalid JSON lines are captured as synthetic `parse_error` events rather than terminating the run.

**Metadata captured**
- `stream_events`: number of JSONL lines processed
- `event_types`: event types in order of arrival
- `input_tokens`, `output_tokens`, `cached_input_tokens`: usage values (when provided)
- `thread_id`: thread identifier emitted by Codex (when available)

**Error handling**
- `CodexError` is raised on CLI exit codes, timeouts, empty streams, or missing assistant messages.
- The orchestrator treats adapter errors as `BLOCKED` and persisting artifacts for inspection.

---

### Claude Code Adapter (`claude-code`)

**Purpose**: Implementation using Claude Code
**Use Case**: Production code implementation and commits

The Claude Code adapter invokes the Claude CLI for implementation tasks.

**Prerequisites**:
1. Install the Claude Code CLI (follow official docs)
2. Authenticate: `claude auth login`
3. Verify: `claude --version`

**Configuration**:
```yaml
claude:
  provider: "claude-code"
  model: "claude-sonnet-4"
  timeout: 600  # Optional: CLI timeout in seconds (default: 600)
```

**CLI Invocation**
```bash
claude --print --output-format json --model <model> "<prompt text>"
```

Claude Code returns a JSON object with the assistant response. The adapter maps fields such as `files_modified`, `commands_executed`, and `commit_sha` into the response metadata so later policies can validate repository changes.

**Error handling**
- `ClaudeCodeError` is raised on CLI exit codes, timeouts, invalid JSON payloads, or missing `content`.
- Adapter errors propagate to the orchestrator and block the run with descriptive notes.

---

## Configuration Examples

### Development (Echo Fallback)
```yaml
codex:
  provider: "echo"
  model: "gpt-4"

claude:
  provider: "echo"
  model: "claude-sonnet-4"

workflow:
  max_iterations: 3
  require_human_approval: false
```

### Production (Real Adapters)
```yaml
codex:
  provider: "codex"
  model: "gpt-4"
  timeout: 300

claude:
  provider: "claude-code"
  model: "claude-sonnet-4"
  timeout: 600

workflow:
  max_iterations: 5
  require_human_approval: true

storage:
  workspace_root: "/path/to/your/project"
  run_artifact_dir: "/path/to/artifacts"
```

### Hybrid (Codex + Echo for Implementation)
```yaml
codex:
  provider: "codex"
  model: "gpt-4"

claude:
  provider: "echo"  # Use echo for testing implementation phase
  model: "claude-sonnet-4"

workflow:
  max_iterations: 3
```

---

## Implementing Custom Adapters

To implement a custom adapter:

1. **Create a new adapter file** in `src/duet/adapters/`:

```python
"""My custom adapter."""

from ..models import AssistantRequest, AssistantResponse
from .base import AssistantAdapter, register_adapter


@register_adapter("my-adapter")
class MyAdapter(AssistantAdapter):
    """Custom adapter implementation."""

    name = "my-adapter"
    role = "custom-role"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "default-model")
        self.timeout = kwargs.get("timeout", 300)

    def generate(self, request: AssistantRequest) -> AssistantResponse:
        # Your implementation here
        content = f"Response to: {request.prompt}"
        return AssistantResponse(
            content=content,
            concluded=False,
            metadata={"adapter": self.name}
        )
```

2. **Register the adapter** by importing it in `src/duet/adapters/__init__.py`:

```python
from .my_adapter import MyAdapter

__all__ = [
    # ... existing exports
    "MyAdapter",
]
```

3. **Use the adapter** in your configuration:

```yaml
codex:
  provider: "my-adapter"
  model: "my-model"
```

---

## Testing Adapters

### Unit Tests

Use mocked subprocess calls to test adapter logic:

```python
from unittest.mock import Mock, patch
from duet.adapters import CodexAdapter
from duet.models import AssistantRequest

def test_codex_adapter():
    adapter = CodexAdapter(model="gpt-4")
    request = AssistantRequest(role="planner", prompt="Test")

    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = '{"content": "Response", "concluded": false}'

    with patch("subprocess.run", return_value=mock_result):
        response = adapter.generate(request)

    assert response.content == "Response"
```

### Integration Tests

Test adapters in the full orchestration loop:

```python
from duet.config import DuetConfig, AssistantConfig
from duet.orchestrator import Orchestrator

config = DuetConfig(
    codex=AssistantConfig(provider="echo", model="test"),
    claude=AssistantConfig(provider="echo", model="test"),
    # ... other config
)

orchestrator = Orchestrator(config, artifact_store, console)
snapshot = orchestrator.run(run_id="test-run")
```

---

## Adapter Registry

All adapters are registered in a global registry (`REGISTRY`) that maps provider names to adapter classes.

**List available adapters**:
```python
from duet.adapters import REGISTRY
print(REGISTRY.adapters.keys())  # ['echo', 'codex', 'claude-code']
```

**Resolve an adapter**:
```python
adapter = REGISTRY.resolve("codex", model="gpt-4", timeout=300)
```

---

## Troubleshooting

### "Adapter not found" Error
- Ensure the adapter is imported in `src/duet/adapters/__init__.py`
- Check that the `@register_adapter()` decorator is present
- Verify the provider name matches the registration name

### CLI Not Found
- Verify the CLI is installed and in PATH
- Use `cli_path` parameter to specify a custom path:
  ```yaml
  codex:
    provider: "codex"
    cli_path: "/custom/path/to/codex"
  ```

### Timeout Errors
- Increase the `timeout` parameter in the configuration
- Default timeouts: Codex=300s, Claude Code=600s

### Invalid JSON Response
- Check that the CLI is outputting JSON format
- Verify `--output json` flag is supported
- Inspect stderr for CLI errors

### Missing Content Field
- Ensure CLI response includes `content`, `text`, `response`, `output`, or `message` field
- Check the adapter's `_normalize_response()` method for expected fields
