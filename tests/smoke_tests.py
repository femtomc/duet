#!/usr/bin/env python3
"""
Smoke tests for validating real Codex and Claude Code CLI integrations.

These tests are NOT run automatically in CI - they require:
1. Codex CLI installed and authenticated
2. Claude Code CLI installed and authenticated
3. Real API access

Run manually with: python tests/smoke_tests.py [--codex] [--claude] [--both]

These tests validate:
- CLI executables are found and working
- Prompt formatting is accepted
- JSON response parsing works
- Error handling for various failure modes
- Timeout behavior
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from duet.adapters import ClaudeCodeAdapter, CodexAdapter
from duet.adapters.claude_code import ClaudeCodeError
from duet.adapters.codex import CodexError
from duet.models import AssistantRequest


class SmokeTestRunner:
    """Runs smoke tests against real CLI adapters."""

    def __init__(self):
        self.console = Console()
        self.results = []

    def run_test(self, name: str, test_fn):
        """Run a single test and record result."""
        self.console.print(f"\n[bold cyan]Test:[/] {name}")
        try:
            result = test_fn()
            if result:
                self.console.print("[green]✓ PASS[/]")
                self.results.append((name, "PASS", None))
            else:
                self.console.print("[red]✗ FAIL[/]")
                self.results.append((name, "FAIL", "Test returned False"))
        except Exception as exc:
            self.console.print(f"[red]✗ ERROR:[/] {exc}")
            self.results.append((name, "ERROR", str(exc)))

    def print_summary(self):
        """Print test summary table."""
        table = Table(title="Smoke Test Results")
        table.add_column("Test", style="cyan")
        table.add_column("Result", style="bold")
        table.add_column("Error", style="red")

        for name, result, error in self.results:
            style = "green" if result == "PASS" else "red"
            table.add_row(name, f"[{style}]{result}[/]", error or "")

        self.console.print()
        self.console.print(table)

        # Overall summary
        passed = sum(1 for _, result, _ in self.results if result == "PASS")
        total = len(self.results)
        self.console.print(f"\n[bold]Summary:[/] {passed}/{total} tests passed")

        return passed == total


# ──────────────────────────────────────────────────────────────────────────────
# Codex Smoke Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_codex_cli_exists():
    """Test that Codex CLI is installed and in PATH."""
    adapter = CodexAdapter(model="gpt-4")
    import subprocess

    try:
        result = subprocess.run(
            [adapter.cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        print(f"  Codex version: {result.stdout.strip() or result.stderr.strip()}")
        return True
    except FileNotFoundError:
        print(f"  Codex CLI not found at: {adapter.cli_path}")
        return False
    except Exception as exc:
        print(f"  Error checking Codex CLI: {exc}")
        return False


def test_codex_simple_request():
    """Test Codex adapter with a simple request."""
    # Note: Model availability depends on Codex configuration
    # Check ~/.codex/config.toml for available models
    adapter = CodexAdapter(model="o3-mini", timeout=60)
    request = AssistantRequest(
        role="planner",
        prompt="Say hello and confirm you received this message.",
        context={},
    )

    try:
        response = adapter.generate(request)
        print(f"  Response content length: {len(response.content)} chars")
        print(f"  Response metadata: {response.metadata}")
        return bool(response.content)
    except CodexError as exc:
        print(f"  Codex error: {exc}")
        print(f"  NOTE: This may fail if model not available in your Codex config")
        return False


def test_codex_json_parsing():
    """Test that Codex response is parsed correctly."""
    adapter = CodexAdapter(model="o3-mini", timeout=60)
    request = AssistantRequest(
        role="planner",
        prompt="Create a brief implementation plan for adding a new feature.",
        context={},
    )

    try:
        response = adapter.generate(request)
        print(f"  Content: {response.content[:50]}...")
        print(f"  Concluded: {response.concluded}")
        print(f"  Metadata keys: {list(response.metadata.keys())}")
        return True
    except CodexError as exc:
        print(f"  Response parsing failed: {exc}")
        print(f"  NOTE: This may fail if model not available in your Codex config")
        return False


def test_codex_error_handling():
    """Test Codex error handling with invalid model."""
    adapter = CodexAdapter(model="invalid-model-name-xyz", timeout=30)
    request = AssistantRequest(role="planner", prompt="Test", context={})

    try:
        response = adapter.generate(request)
        print(f"  Unexpected success: {response}")
        return False  # Should have raised error
    except CodexError as exc:
        print(f"  Correctly caught error: {type(exc).__name__}")
        return True


# ──────────────────────────────────────────────────────────────────────────────
# Claude Code Smoke Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_claude_cli_exists():
    """Test that Claude CLI is installed and in PATH."""
    adapter = ClaudeCodeAdapter(model="claude-sonnet-4")
    import subprocess

    try:
        result = subprocess.run(
            [adapter.cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        print(f"  Claude version: {result.stdout.strip() or result.stderr.strip()}")
        return True
    except FileNotFoundError:
        print(f"  Claude CLI not found at: {adapter.cli_path}")
        return False
    except Exception as exc:
        print(f"  Error checking Claude CLI: {exc}")
        return False


def test_claude_simple_request():
    """Test Claude Code adapter with a simple request."""
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = ClaudeCodeAdapter(
            model="sonnet", timeout=90, workspace_root=tmpdir
        )
        request = AssistantRequest(
            role="implementer",
            prompt="Say hello and confirm you received this message.",
            context={},
        )

        try:
            response = adapter.generate(request)
            print(f"  Response content length: {len(response.content)} chars")
            print(f"  Response metadata: {response.metadata}")
            return bool(response.content)
        except ClaudeCodeError as exc:
            print(f"  Claude error: {exc}")
            return False


def test_claude_workspace_context():
    """Test that Claude receives workspace context."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "test_file.txt").write_text("test content")

        adapter = ClaudeCodeAdapter(
            model="sonnet", timeout=90, workspace_root=str(workspace)
        )
        request = AssistantRequest(
            role="implementer",
            prompt="List the files in the current directory.",
            context={},
        )

        try:
            response = adapter.generate(request)
            print(f"  Workspace: {workspace}")
            print(f"  Response mentions test_file: {'test_file' in response.content}")
            return True
        except ClaudeCodeError as exc:
            print(f"  Workspace context test failed: {exc}")
            return False


def test_claude_json_parsing():
    """Test that Claude response is parsed correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = ClaudeCodeAdapter(
            model="sonnet", timeout=90, workspace_root=tmpdir
        )
        request = AssistantRequest(
            role="implementer",
            prompt="Respond with a one-sentence summary of what you can do.",
            context={},
        )

        try:
            response = adapter.generate(request)
            print(f"  Content: {response.content[:50]}...")
            print(f"  Metadata keys: {list(response.metadata.keys())}")
            return True
        except ClaudeCodeError as exc:
            print(f"  Response parsing failed: {exc}")
            return False


def test_claude_error_handling():
    """Test Claude error handling with invalid model."""
    adapter = ClaudeCodeAdapter(model="invalid-model-xyz", timeout=30, workspace_root=".")
    request = AssistantRequest(role="implementer", prompt="Test", context={})

    try:
        response = adapter.generate(request)
        print(f"  Unexpected success: {response}")
        return False  # Should have raised error
    except ClaudeCodeError as exc:
        print(f"  Correctly caught error: {type(exc).__name__}")
        return True


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Run Duet smoke tests against real CLIs")
    parser.add_argument("--codex", action="store_true", help="Test Codex adapter only")
    parser.add_argument("--claude", action="store_true", help="Test Claude Code adapter only")
    parser.add_argument("--both", action="store_true", help="Test both adapters (default)")
    args = parser.parse_args()

    # Default to both if no flags specified
    if not args.codex and not args.claude:
        args.both = True

    console = Console()
    console.print(
        Panel(
            "[bold cyan]Duet Smoke Tests - Real CLI Validation[/]\n\n"
            "[yellow]⚠ WARNING:[/] These tests make real API calls and require:\n"
            "  • Codex CLI installed and authenticated (codex auth login)\n"
            "  • Claude Code CLI installed and authenticated (claude auth login)\n"
            "  • Active internet connection and API access",
            expand=False,
        )
    )

    runner = SmokeTestRunner()

    # Run Codex tests
    if args.codex or args.both:
        console.print("\n[bold magenta]═══ Codex Adapter Tests ═══[/]")
        runner.run_test("Codex CLI exists", test_codex_cli_exists)
        runner.run_test("Codex simple request", test_codex_simple_request)
        runner.run_test("Codex JSON parsing", test_codex_json_parsing)
        runner.run_test("Codex error handling", test_codex_error_handling)

    # Run Claude tests
    if args.claude or args.both:
        console.print("\n[bold magenta]═══ Claude Code Adapter Tests ═══[/]")
        runner.run_test("Claude CLI exists", test_claude_cli_exists)
        runner.run_test("Claude simple request", test_claude_simple_request)
        runner.run_test("Claude workspace context", test_claude_workspace_context)
        runner.run_test("Claude JSON parsing", test_claude_json_parsing)
        runner.run_test("Claude error handling", test_claude_error_handling)

    # Print summary
    all_passed = runner.print_summary()

    if all_passed:
        console.print("\n[green bold]✓ All smoke tests passed![/]")
        console.print("[green]Adapters are ready for production use.[/]")
        sys.exit(0)
    else:
        console.print("\n[red bold]✗ Some smoke tests failed[/]")
        console.print("[yellow]Review failures before using adapters in production.[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
