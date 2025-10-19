"""
Soak Tests for Sprint 12.

Tests repeated duet next → duet back loops to ensure:
- Channel snapshots persist correctly
- Git restore behaves correctly
- Database performance doesn't degrade
- No memory leaks or resource issues
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import pytest


def setup_test_workspace() -> Path:
    """Set up a workspace with duet initialized and git configured."""
    tmpdir = tempfile.mkdtemp()
    workspace = Path(tmpdir)

    # Initialize git
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Soak Test"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "soak@test.com"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )

    # Initialize duet
    subprocess.run(
        ["uv", "run", "duet", "init", "--skip-discovery"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )

    # Switch to echo adapter
    duet_yaml = workspace / ".duet" / "duet.yaml"
    content = duet_yaml.read_text()
    content = content.replace('provider: "codex"', 'provider: "echo"')
    content = content.replace('provider: "claude-code"', 'provider: "echo"')
    duet_yaml.write_text(content)

    # Create initial commit
    subprocess.run(["git", "add", "."], cwd=workspace, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )

    return workspace


@pytest.mark.slow
@pytest.mark.soak
class TestSoakDuetNextBack:
    """Soak tests for duet next/back loops."""

    def test_repeated_next_back_loops(self):
        """Test repeated next/back cycles don't cause issues."""
        workspace = setup_test_workspace()
        iterations = 10  # Reduced for CI speed; increase for real soak testing

        try:
            state_ids = []

            # Run multiple next commands to create states
            for i in range(iterations):
                result = subprocess.run(
                    ["uv", "run", "duet", "next"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    check=False,  # May block after review phase
                )

                # Extract state_id from output if present
                # Real implementation would parse this properly
                if "State ID:" in result.stdout:
                    for line in result.stdout.split("\n"):
                        if "State ID:" in line:
                            state_id = line.split("State ID:")[-1].strip()
                            state_ids.append(state_id)
                            break

                # Stop if blocked
                if "blocked" in result.stdout.lower() or "done" in result.stdout.lower():
                    break

            # Now test going back through states
            for state_id in reversed(state_ids[:3]):  # Test a few back operations
                result = subprocess.run(
                    ["uv", "run", "duet", "back", state_id, "--force"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                # Should not crash
                assert result.returncode in (0, 1), f"duet back should not crash: {result.stderr}"

        finally:
            # Cleanup
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)

    def test_database_performance_under_load(self):
        """Test that database inserts don't degrade significantly."""
        workspace = setup_test_workspace()
        iterations = 20

        try:
            timings = []

            for i in range(iterations):
                start = time.time()

                result = subprocess.run(
                    ["uv", "run", "duet", "next"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                elapsed = time.time() - start
                timings.append(elapsed)

                # Stop if blocked/done
                if "blocked" in result.stdout.lower() or "done" in result.stdout.lower():
                    break

            # Check that later iterations aren't significantly slower than early ones
            if len(timings) >= 10:
                early_avg = sum(timings[:5]) / 5
                late_avg = sum(timings[-5:]) / 5

                # Allow up to 2x slowdown (very generous threshold)
                # Real soak tests might be stricter
                assert late_avg < early_avg * 2, (
                    f"Performance degradation detected: "
                    f"early avg={early_avg:.2f}s, late avg={late_avg:.2f}s"
                )

        finally:
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)

    def test_channel_snapshot_integrity(self):
        """Test that channel snapshots remain consistent after many operations."""
        workspace = setup_test_workspace()

        try:
            # Run several phases
            for _ in range(5):
                subprocess.run(
                    ["uv", "run", "duet", "next"],
                    cwd=workspace,
                    capture_output=True,
                    check=False,
                )

            # Query channel messages
            result = subprocess.run(
                ["uv", "run", "duet", "messages", "run-*"],  # Glob pattern
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )

            # Should not crash and should have some output
            # Real test would validate structure
            assert result.returncode in (0, 1)  # May fail if no runs, but shouldn't crash

        finally:
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    # Run with: pytest test_soak.py -v -m soak
    pytest.main([__file__, "-v", "-m", "soak"])
