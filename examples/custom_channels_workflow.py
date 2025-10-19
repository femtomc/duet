"""
Example Workflow with Custom Channels.

This demonstrates how to use custom channels beyond the default plan/code/verdict.
The workflow includes:
- A testing phase that publishes test results
- A documentation phase that publishes docs
- A metrics phase that tracks performance
- Custom guards based on test results

Usage:
    1. Copy this file to .duet/workflow.py
    2. Run: duet lint (to validate)
    3. Run: duet run (to execute)

Note: Uses echo adapter for demonstration. Replace with real adapters in production.
"""

from duet.dsl import Agent, Channel, Phase, Transition, When, Workflow

workflow = Workflow(
    # ──── Agents ────
    # Define agents for each phase
    agents=[
        Agent(
            name="planner",
            provider="echo",  # Replace with "codex" in production
            model="gpt-5-codex",
            description="Creates implementation plans",
        ),
        Agent(
            name="implementer",
            provider="echo",  # Replace with "claude-code" in production
            model="sonnet",
            description="Implements code changes",
        ),
        Agent(
            name="tester",
            provider="echo",  # Replace with "codex" in production
            model="gpt-5-codex",
            description="Runs tests and analyzes results",
        ),
        Agent(
            name="documenter",
            provider="echo",  # Replace with "claude-code" in production
            model="sonnet",
            description="Updates documentation",
        ),
        Agent(
            name="reviewer",
            provider="echo",  # Replace with "codex" in production
            model="gpt-5-codex",
            description="Final quality review",
        ),
    ],

    # ──── Channels ────
    # Standard channels + custom ones for tests/docs/metrics
    channels=[
        # Standard workflow channels
        Channel(
            name="task",
            description="Input task specification from user",
            schema="text",
        ),
        Channel(
            name="plan",
            description="Implementation plan drafted by planner",
            schema="text",
        ),
        Channel(
            name="code",
            description="Implementation artifacts and changes",
            schema="git_diff",
        ),

        # Custom channels for enhanced workflow
        Channel(
            name="tests",
            description="Test results and coverage report",
            schema="json",  # Could store structured test data
        ),
        Channel(
            name="test_status",
            description="Test pass/fail status",
            schema="text",  # Values: "pass", "fail", "skip"
        ),
        Channel(
            name="docs",
            description="Updated documentation content",
            schema="text",
        ),
        Channel(
            name="metrics",
            description="Performance metrics (runtime, memory, etc.)",
            schema="json",
        ),

        # Standard verdict channels
        Channel(
            name="verdict",
            description="Final review outcome",
            schema="verdict",  # approve/changes_requested/blocked
        ),
        Channel(
            name="feedback",
            description="Review feedback for replanning",
            schema="text",
        ),
    ],

    # ──── Phases ────
    # Extended workflow with testing and documentation phases
    phases=[
        Phase(
            name="plan",
            agent="planner",
            consumes=["task", "feedback"],
            publishes=["plan"],
            description="Draft implementation plan with test strategy",
        ),
        Phase(
            name="implement",
            agent="implementer",
            consumes=["plan"],
            publishes=["code"],
            description="Implement changes and write tests",
        ),
        Phase(
            name="test",
            agent="tester",
            consumes=["code", "plan"],
            publishes=["tests", "test_status"],
            description="Run tests and analyze results",
        ),
        Phase(
            name="document",
            agent="documenter",
            consumes=["code", "tests"],
            publishes=["docs", "metrics"],
            description="Update documentation and collect metrics",
        ),
        Phase(
            name="review",
            agent="reviewer",
            consumes=["plan", "code", "tests", "docs"],
            publishes=["verdict", "feedback"],
            description="Comprehensive review of implementation, tests, and docs",
        ),

        # Terminal phases
        Phase(
            name="done",
            agent="reviewer",
            description="Workflow completed successfully",
            is_terminal=True,
        ),
        Phase(
            name="fix_tests",
            agent="implementer",
            consumes=["code", "tests", "feedback"],
            publishes=["code"],
            description="Fix failing tests",
        ),
        Phase(
            name="blocked",
            agent="reviewer",
            description="Workflow blocked, requires human intervention",
            is_terminal=True,
        ),
    ],

    # ──── Transitions ────
    # Custom transitions based on test results
    transitions=[
        # PLAN → IMPLEMENT
        Transition(
            from_phase="plan",
            to_phase="implement",
            when=When.always(),
        ),

        # IMPLEMENT → TEST
        Transition(
            from_phase="implement",
            to_phase="test",
            when=When.always(),
        ),

        # TEST → DOCUMENT (if tests pass)
        Transition(
            from_phase="test",
            to_phase="document",
            when=When.channel_has("test_status", "pass"),
            priority=10,
        ),

        # TEST → FIX_TESTS (if tests fail)
        Transition(
            from_phase="test",
            to_phase="fix_tests",
            when=When.channel_has("test_status", "fail"),
            priority=9,
        ),

        # FIX_TESTS → TEST (re-run tests)
        Transition(
            from_phase="fix_tests",
            to_phase="test",
            when=When.always(),
        ),

        # DOCUMENT → REVIEW
        Transition(
            from_phase="document",
            to_phase="review",
            when=When.always(),
        ),

        # REVIEW → DONE (if approved)
        Transition(
            from_phase="review",
            to_phase="done",
            when=When.channel_has("verdict", "approve"),
            priority=15,
        ),

        # REVIEW → PLAN (if changes requested)
        Transition(
            from_phase="review",
            to_phase="plan",
            when=When.channel_has("verdict", "changes_requested"),
            priority=10,
        ),

        # REVIEW → BLOCKED (if critical issues)
        Transition(
            from_phase="review",
            to_phase="blocked",
            when=When.channel_has("verdict", "blocked"),
            priority=20,
        ),
    ],

    # Start with planning
    initial_phase="plan",
)

# ──────────────────────────────────────────────────────────────────────────────
# Notes for Adapter Implementation
# ──────────────────────────────────────────────────────────────────────────────

# To use custom channels with real adapters, ensure adapters return
# the channel values in their metadata:

# Example for tester agent:
# response.metadata = {
#     "tests": json.dumps({"passed": 42, "failed": 0, "coverage": 85}),
#     "test_status": "pass"
# }

# Example for documenter agent:
# response.metadata = {
#     "docs": updated_docs_content,
#     "metrics": json.dumps({"runtime_ms": 1234, "memory_mb": 256})
# }

# The orchestrator will automatically persist these to the database
# thanks to the generalized channel persistence (Sprint 12).
