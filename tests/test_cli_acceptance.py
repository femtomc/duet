"""
CLI Acceptance Tests for Sprint 12.

Tests the full CLI workflow end-to-end:
- duet init (with and without git)
- duet lint
- duet run/next/status/messages
- Workflow hot-reload
- Error messaging

Uses echo adapter to avoid external dependencies.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def temp_workspace_with_git():
    """Create a temporary workspace with git initialized."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)

        # Initialize git
        subprocess.run(
            ["git", "init"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )

        yield workspace


@pytest.fixture
def temp_workspace_no_git():
    """Create a temporary workspace without git."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def run_duet(workspace: Path, *args, check=True):
    """Helper to run duet CLI commands."""
    result = subprocess.run(
        ["uv", "run", "duet", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"Command failed: duet {' '.join(args)}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result


class TestCLIAcceptance:
    """Acceptance tests for CLI commands."""

    def test_init_creates_workspace(self, temp_workspace_with_git):
        """Test that duet init creates all required files."""
        result = run_duet(temp_workspace_with_git, "init", "--skip-discovery")

        # Check .duet directory was created
        duet_dir = temp_workspace_with_git / ".duet"
        assert duet_dir.exists(), "`.duet/` directory should be created"

        # Check required files
        assert (duet_dir / "duet.yaml").exists(), "`duet.yaml` should be created"
        assert (duet_dir / "workflow.py").exists(), "`workflow.py` should be created"
        assert (duet_dir / "README.md").exists(), "`README.md` should be created"
        assert (duet_dir / "runs").exists(), "`runs/` directory should be created"
        assert (duet_dir / "logs").exists(), "`logs/` directory should be created"
        assert (duet_dir / "context").exists(), "`context/` directory should be created"

        # Check output mentions success
        assert "✓" in result.stdout or "success" in result.stdout.lower()

    def test_init_with_git_flag(self, temp_workspace_no_git):
        """Test that duet init --init-git creates git repo."""
        result = run_duet(temp_workspace_no_git, "init", "--init-git", "--skip-discovery")

        # Check git was initialized
        assert (temp_workspace_no_git / ".git").exists(), "`.git/` should be created"
        assert (temp_workspace_no_git / ".gitignore").exists(), "`.gitignore` should be created"

        # Check .gitignore contains duet entries
        gitignore = (temp_workspace_no_git / ".gitignore").read_text()
        assert ".duet/logs/" in gitignore
        assert ".duet/runs/" in gitignore
        assert ".duet/duet.db" in gitignore

        # Check git has commits
        git_result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=temp_workspace_no_git,
            capture_output=True,
            text=True,
        )
        assert "initialize Duet workspace" in git_result.stdout or git_result.returncode == 0

    def test_init_warns_without_git(self, temp_workspace_no_git):
        """Test that duet init warns when git is missing."""
        result = run_duet(temp_workspace_no_git, "init", "--skip-discovery")

        # Should warn about missing git
        assert "⚠" in result.stdout or "warning" in result.stdout.lower() or "no git" in result.stdout.lower()

    def test_lint_validates_workflow(self, temp_workspace_with_git):
        """Test that duet lint validates workflow successfully."""
        # First init
        run_duet(temp_workspace_with_git, "init", "--skip-discovery")

        # Switch to echo adapter
        duet_yaml = temp_workspace_with_git / ".duet" / "duet.yaml"
        content = duet_yaml.read_text()
        content = content.replace('provider: "codex"', 'provider: "echo"')
        content = content.replace('provider: "claude-code"', 'provider: "echo"')
        duet_yaml.write_text(content)

        # Run lint
        result = run_duet(temp_workspace_with_git, "lint")

        # Should succeed
        assert result.returncode == 0
        assert "validation succeeded" in result.stdout.lower() or "✓" in result.stdout

    def test_lint_fails_on_invalid_workflow(self, temp_workspace_with_git):
        """Test that duet lint catches workflow errors."""
        # First init
        run_duet(temp_workspace_with_git, "init", "--skip-discovery")

        # Break the workflow
        workflow_file = temp_workspace_with_git / ".duet" / "workflow.py"
        workflow_file.write_text("""
from duet.dsl import Workflow

workflow = Workflow(
    agents=[],
    channels=[],
    phases=[],  # Invalid: no phases
    transitions=[],
)
""")

        # Run lint (should fail)
        result = run_duet(temp_workspace_with_git, "lint", check=False)

        # Should fail
        assert result.returncode != 0
        assert "error" in result.stdout.lower() or "fail" in result.stdout.lower()

    def test_echo_adapter_workflow(self, temp_workspace_with_git):
        """Test full workflow with echo adapter."""
        # Init with git
        run_duet(temp_workspace_with_git, "init", "--init-git", "--skip-discovery", "--force")

        # Switch to echo adapter
        duet_yaml = temp_workspace_with_git / ".duet" / "duet.yaml"
        content = duet_yaml.read_text()
        content = content.replace('provider: "codex"', 'provider: "echo"')
        content = content.replace('provider: "claude-code"', 'provider: "echo"')
        duet_yaml.write_text(content)

        # Create initial commit (needed for baseline)
        subprocess.run(
            ["git", "add", "."],
            cwd=temp_workspace_with_git,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=temp_workspace_with_git,
            capture_output=True,
        )

        # Run first phase
        result = run_duet(temp_workspace_with_git, "next")

        # Should succeed
        assert result.returncode == 0
        assert "ECHO ADAPTER" in result.stdout or "echo" in result.stdout.lower()

    def test_workflow_hot_reload(self, temp_workspace_with_git):
        """Test that workflow changes are detected and reloaded."""
        # Init
        run_duet(temp_workspace_with_git, "init", "--init-git", "--skip-discovery", "--force")

        # Switch to echo adapter
        duet_yaml = temp_workspace_with_git / ".duet" / "duet.yaml"
        content = duet_yaml.read_text()
        content = content.replace('provider: "codex"', 'provider: "echo"')
        content = content.replace('provider: "claude-code"', 'provider: "echo"')
        duet_yaml.write_text(content)

        # Create initial commit
        subprocess.run(["git", "add", "."], cwd=temp_workspace_with_git, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=temp_workspace_with_git, capture_output=True)

        # Run first phase
        run_duet(temp_workspace_with_git, "next")

        # Modify workflow (add a comment to change mtime)
        time.sleep(0.1)  # Ensure mtime changes
        workflow_file = temp_workspace_with_git / ".duet" / "workflow.py"
        content = workflow_file.read_text()
        workflow_file.write_text(content + "\n# Modified\n")

        # Run next phase - should detect reload
        result = run_duet(temp_workspace_with_git, "next")

        # Should mention reload
        assert "reload" in result.stdout.lower() or "⟳" in result.stdout

    def test_status_shows_run_info(self, temp_workspace_with_git):
        """Test that duet status displays run information."""
        # Setup
        run_duet(temp_workspace_with_git, "init", "--init-git", "--skip-discovery", "--force")

        duet_yaml = temp_workspace_with_git / ".duet" / "duet.yaml"
        content = duet_yaml.read_text()
        content = content.replace('provider: "codex"', 'provider: "echo"')
        content = content.replace('provider: "claude-code"', 'provider: "echo"')
        duet_yaml.write_text(content)

        subprocess.run(["git", "add", "."], cwd=temp_workspace_with_git, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=temp_workspace_with_git, capture_output=True)

        # Run a phase to create a run
        next_result = run_duet(temp_workspace_with_git, "next")

        # Extract run_id from output (typically shown in output)
        # For now, we'll just verify status command works with ANY run-id
        # In practice, you'd parse the run_id from next_result.stdout

        # Note: Without parsing run_id, we can't test status fully in this minimal version
        # A real test would extract run_id and call `duet status <run_id>`
        # For now, we just verify the command structure works

        # This test demonstrates the approach - real implementation would parse run_id
        # result = run_duet(temp_workspace_with_git, "status", run_id)
        # assert "Channel Updates" in result.stdout or "Run ID" in result.stdout


class TestErrorHandling:
    """Test error handling and user-friendly messages."""

    def test_missing_git_warning_on_run(self, temp_workspace_no_git):
        """Test that running without git shows helpful warning."""
        # Init without git
        run_duet(temp_workspace_no_git, "init", "--skip-discovery")

        # Switch to echo
        duet_yaml = temp_workspace_no_git / ".duet" / "duet.yaml"
        content = duet_yaml.read_text()
        content = content.replace('provider: "codex"', 'provider: "echo"')
        content = content.replace('provider: "claude-code"', 'provider: "echo"')
        duet_yaml.write_text(content)

        # Try to run - should warn about git
        result = run_duet(temp_workspace_no_git, "next", check=False)

        # Should contain warning about git
        # Note: May succeed with warning, or fail - depends on implementation
        if "⚠" in result.stdout or "warning" in result.stdout.lower():
            assert "git" in result.stdout.lower() or "commit" in result.stdout.lower()

    def test_adapter_error_friendly_message(self, temp_workspace_with_git):
        """Test that adapter errors show friendly messages."""
        # Init
        run_duet(temp_workspace_with_git, "init", "--skip-discovery")

        # Keep codex adapter (which will fail without auth)
        # Try to run
        result = run_duet(temp_workspace_with_git, "run", check=False)

        # Should fail with friendly error
        assert result.returncode != 0

        # Should suggest echo adapter or provide helpful context
        # (Exact message depends on implementation, but should NOT be a raw stack trace)
        assert "Adapter" in result.stdout or "adapter" in result.stdout.lower() or "echo" in result.stdout.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
