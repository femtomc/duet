# Duet Automation Overview

## Manual Workflow (Baseline)
- Codex plans the work and prepares implementation guidance.
- Claude Code executes the plan, edits the repository, and commits changes.
- Codex reviews the commits and either approves or requests revisions.
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
   - `CodexAdapter` handles planning and review; `ClaudeCodeAdapter` handles implementation.
   - Shared interface: `generate(request: AssistantRequest) -> AssistantResponse`.
   - Responsible for CLI invocation, prompt templating, retries, and error normalization.
3. **Artifact Manager**
   - Persists prompts, responses, and run metadata under `.duet/runs/<run-id>/`.
   - Records git state (branch, commit summary, diff statistics).
4. **Policy Engine**
   - Validates responses (for example, ensures implementation produced commits and review returned a verdict).
   - Enforces iteration limits and triggers human intervention when required.
5. **CLI / Service Layer**
   - Provides `duet init`, `run`, `status`, `summary`, and `show-config`.
   - Emits Rich console output and optional JSONL for downstream analysis.

## Key Questions To Resolve
1. How should long-term persistence evolve beyond filesystem artifacts (SQLite support is planned for Sprint 5)?
2. Which additional adapters or model providers should be supported?
3. Which guardrails or approval workflows are required for production usage in larger teams?
4. How should notifications and external system integrations (issue trackers, chat) be incorporated?
5. What observability surfaces (dashboards, metrics, alerts) are necessary for ongoing operations?

## Next Steps
1. Deliver Sprint 5 (SQLite persistence) to enable historical queries and resumability.
2. Expand CLI ergonomics with history/inspection commands backed by the database.
3. Implement observability improvements (Sprint 6) such as richer summaries or dashboards.
4. Harden retry and resume semantics (Sprint 7), including adapter backoff and metrics.
5. Revisit integration opportunities once persistence and observability are complete.
