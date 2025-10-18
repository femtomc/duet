# Smoke Testing Guide

This guide covers end-to-end validation of Duet adapters against real Codex and Claude Code CLIs.

## ⚠️ Important

**All automated tests use mocked subprocess calls.** Before relying on Duet for production tasks, you must run manual smoke tests against the real CLIs to validate:

1. CLI executables are found and accessible
2. Prompt formatting is accepted by the CLIs
3. JSON response parsing works correctly
4. Error handling behaves as expected
5. Timeout behavior is appropriate

---

## Prerequisites

### For Codex Tests

1. **Install Codex CLI**:
   ```bash
   npm install -g codex-cli  # (or equivalent)
   ```

2. **Authenticate**:
   ```bash
   codex auth login
   ```

3. **Verify**:
   ```bash
   codex --version
   ```

### For Claude Code Tests

1. **Install Claude Code CLI** (follow official docs)

2. **Authenticate**:
   ```bash
   claude auth login
   ```

3. **Verify**:
   ```bash
   claude --version
   ```

---

## Running Smoke Tests

### Test Both Adapters (Recommended)

```bash
uv run python tests/smoke_tests.py --both
```

### Test Codex Only

```bash
uv run python tests/smoke_tests.py --codex
```

### Test Claude Code Only

```bash
uv run python tests/smoke_tests.py --claude
```

---

## What Gets Tested

### Codex Adapter Tests

1. **CLI Exists**
   - Validates `codex` command is in PATH
   - Runs `codex --version` to confirm installation
   - **Expected**: Version output or success

2. **Simple Request**
   - Sends basic prompt requesting JSON response
   - Validates response is received and non-empty
   - **Expected**: Response with content

3. **JSON Parsing**
   - Sends prompt requesting specific JSON structure
   - Validates adapter correctly parses response
   - Checks `content`, `concluded`, and `metadata` fields
   - **Expected**: Correctly parsed AssistantResponse

4. **Error Handling**
   - Attempts request with invalid model name
   - Validates adapter raises `CodexError` (not crash)
   - **Expected**: CodexError exception caught

### Claude Code Adapter Tests

1. **CLI Exists**
   - Validates `claude` command is in PATH
   - Runs `claude --version` to confirm installation
   - **Expected**: Version output or success

2. **Simple Request**
   - Sends basic prompt in temporary workspace
   - Validates response is received and non-empty
   - **Expected**: Response with content

3. **Workspace Context**
   - Creates temporary workspace with test file
   - Sends prompt about workspace contents
   - Validates Claude receives workspace context
   - **Expected**: Response acknowledges workspace files

4. **JSON Parsing**
   - Sends prompt requesting specific JSON structure
   - Validates adapter correctly parses response
   - Checks `content`, `files_modified`, and metadata
   - **Expected**: Correctly parsed AssistantResponse

5. **Error Handling**
   - Attempts request with invalid model name
   - Validates adapter raises `ClaudeCodeError` (not crash)
   - **Expected**: ClaudeCodeError exception caught

---

## Interpreting Results

### ✓ All Tests Pass

Adapters are validated and ready for production use. You can safely:
- Use `provider: "codex"` in production configs
- Use `provider: "claude-code"` in production configs
- Run real orchestration loops with confidence

### ✗ Some Tests Fail

**Common Failure Modes**:

#### CLI Not Found
```
Codex CLI not found at: codex
```

**Fix**: Install CLI and ensure it's in PATH

#### Authentication Error
```
Codex error: Authentication failed
```

**Fix**: Run authentication command (`codex auth login`)

#### JSON Parsing Error
```
JSON parsing failed: Failed to parse Codex JSON response
```

**Possible Causes**:
- CLI doesn't support `--output json` flag
- CLI returns non-JSON format
- Response structure differs from expected

**Fix**: Check CLI documentation for correct flags and response format. May need to update adapter's CLI invocation or response normalization.

#### Timeout
```
Codex error: Codex CLI timeout after 60 seconds
```

**Fix**: Increase timeout in adapter config or check network connectivity

---

## Expected CLI Behavior

### Codex CLI Expected Interface

```bash
# Command format we use
codex --model <model> --prompt-file <file> --output json

# Expected output format
{
  "content": "The response text...",
  "concluded": false,
  "metadata": { ... }
}
```

**OR** with fallback content extraction:
```json
{
  "text": "The response...",
  "response": "The response...",
  "output": "The response..."
}
```

### Claude Code CLI Expected Interface

```bash
# Command format we use
claude --model <model> --prompt-file <file> --workspace <path> --output json

# Expected output format
{
  "content": "Implementation summary...",
  "concluded": false,
  "files_modified": ["src/main.py"],
  "commands_executed": ["pytest"],
  "commit_sha": "abc123",
  "metadata": { ... }
}
```

**OR** with fallback content extraction (same as Codex)

---

## Manual Validation Checklist

After smoke tests pass, manually verify:

- [ ] Codex CLI installed and authenticated
- [ ] Claude Code CLI installed and authenticated
- [ ] Codex adapter responds to simple prompts
- [ ] Claude adapter responds in workspace context
- [ ] JSON responses parse correctly
- [ ] Error handling catches failures gracefully
- [ ] Timeouts are reasonable for your network
- [ ] Response content meets expectations
- [ ] Metadata fields are populated
- [ ] No unexpected crashes or hangs

---

## Troubleshooting

### "Git executable not found"

Git change detection requires git to be installed. Install git or disable git validation during smoke tests.

### "Permission denied"

Check file permissions on:
- Workspace directory
- Temporary directories
- CLI executables

### "Model not found"

Update model names in smoke tests to match available models:
- Codex: `gpt-4`, `gpt-3.5-turbo`, etc.
- Claude: `claude-sonnet-4`, `claude-opus-4`, etc.

### Adapter Works But Orchestrator Fails

If smoke tests pass but orchestration fails:
1. Check workspace permissions
2. Verify git repository is initialized
3. Review orchestrator logs for specific errors
4. Try running with `enable_jsonl: true` for detailed logs

---

## Continuous Validation

**Best Practice**: Run smoke tests after:
- Upgrading Codex CLI
- Upgrading Claude Code CLI
- Changing API credentials
- Deploying to new environment
- Modifying adapter code

**Command**:
```bash
# Quick check before production use
uv run python tests/smoke_tests.py --both

# Add to deployment scripts
./deploy.sh && uv run python tests/smoke_tests.py --both || echo "Smoke tests failed!"
```

---

## Automated Testing vs Smoke Testing

| Test Type | Purpose | When to Run | Requirements |
|-----------|---------|-------------|--------------|
| **Unit Tests** | Validate logic with mocks | Every commit | None (mocked) |
| **Integration Tests** | Validate component interaction | Every commit | None (mocked) |
| **Smoke Tests** | Validate real CLI behavior | Before production | Real CLIs + Auth |
| **Acceptance Tests** | Validate full orchestration | Before releases | Echo adapter only |

**All tests except smoke tests run in CI automatically.**
**Smoke tests must be run manually with real API access.**

---

## Next Steps

Once smoke tests pass:
1. Update configuration files with real provider names
2. Run full orchestration with small test task
3. Monitor artifacts and logs for unexpected behavior
4. Gradually increase task complexity
5. Establish error handling procedures

**Remember**: Smoke tests validate CLI integration only. Full orchestration validation requires running real plan → implement → review loops with the echo adapter first, then with real adapters on non-critical tasks.
