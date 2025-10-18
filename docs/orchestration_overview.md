# Duet Automation Overview

## Current Manual Workflow
- `Codex` performs sprint planning and authoring of implementation documents.
- `Claude Code` consumes the plan, performs coding work, and commits the results.
- `Codex` reviews the committed changes and either approves or requests iterations.
- The loop repeats until the implementation satisfies acceptance criteria.

## Automation Goal
Create a coordinator that sequences the two assistants so that hand-offs are automated, artifacts are persisted, and progress is tracked without manual intervention.

## Desired Outcomes
- Deterministic loop that alternates between planning, implementation, and review.
- Persistent record of each step (requests, responses, artifacts, commits, review notes).
- Ability to resume from the last successful phase after interruptions or failures.
- Configurable guardrails (max iterations, quality gates, human checkpoints).
- Extensible integrations (different model providers, SCM backends, notification channels).

## High-Level Architecture
1. **Orchestrator Core**
   - State machine that represents the workflow phases (`PLAN`, `IMPLEMENT`, `REVIEW`, `DONE`, `BLOCKED`).
   - Scheduler that transitions states based on assistant responses and result evaluation.
   - Persistence layer for run metadata (e.g., JSONL log, SQLite, or git-backed artifacts).
2. **Assistant Adapters**
   - `CodexAdapter` (design/review) and `ClaudeAdapter` (implementation).
   - Shared interface: `generate(prompt, context) -> AssistantResponse`.
   - Responsible for API invocation, prompt templating, retry/backoff, and error normalization.
3. **Artifact Manager**
   - Stores prompts, responses, and any generated files.
   - Interfaces with git for branch management, commit verification, and diff capture.
4. **Policy Engine**
   - Validates responses (e.g., ensures implementation produced commits, review returned pass/fail).
   - Enforces iteration limits and triggers human intervention when required.
5. **CLI / Service Layer**
   - Entry point to start, resume, or inspect orchestration runs.
   - Exposes status dashboards (TUI/CLI) and emits structured logs.

## Key Questions To Resolve
1. How are API credentials for Codex and Claude provided and rotated?
2. What persistence medium is acceptable (local filesystem vs. hosted DB)?
3. Should git interactions be performed directly by the orchestrator or delegated to Claude?
4. What constitutes a "successful" review, and can Codex approve merges autonomously?
5. How can humans intervene mid-loop (e.g., pause, edit plan, override decisions)?

## Next Steps
1. Define concrete state transition rules and failure handling strategy.
2. Decide on language/runtime for the orchestrator (Python CLI recommended for rapid iteration).
3. Specify prompt templates and response schemas for both assistants.
4. Implement prototype adapters with dummy providers for local testing.
5. Layer in real API integrations and git automation once the skeleton is validated.
