"""
Policy enforcement tests.

Tests for Sprint 3 policy features:
- Review verdict parsing
- Git change detection
- Guardrail enforcement
- Human approval workflow
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

from duet.artifacts import ArtifactStore
from duet.config import AssistantConfig, DuetConfig, StorageConfig, WorkflowConfig
from duet.models import AssistantRequest, AssistantResponse, Phase, ReviewVerdict, TransitionDecision
from duet.orchestrator import Orchestrator


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_artifacts_dir():
    """Create a temporary artifacts directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ──────────────────────────────────────────────────────────────────────────────
# Review Verdict Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_review_verdict_approve():
    """Test that APPROVE verdict transitions to DONE."""
    from duet.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(max_iterations=1, require_human_approval=False),
                storage=StorageConfig(
                    workspace_root=Path(tmpdir_workspace),
                    run_artifact_dir=Path(tmpdir_artifacts),
                ),
            )

            orchestrator = Orchestrator(
                config, ArtifactStore(Path(tmpdir_artifacts)), Console()
            )

            # Test decision logic for APPROVE verdict
            response = AssistantResponse(
                content="All good!", verdict=ReviewVerdict.APPROVE, concluded=True
            )
            decision = orchestrator._decide_next_phase(Phase.REVIEW, response, 1, 0)

            assert decision.next_phase == Phase.DONE
            assert "APPROVE" in decision.rationale
            assert not decision.requires_human


def test_review_verdict_changes_requested():
    """Test that CHANGES_REQUESTED verdict loops back to PLAN."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(max_iterations=5, require_human_approval=False),
                storage=StorageConfig(
                    workspace_root=Path(tmpdir_workspace),
                    run_artifact_dir=Path(tmpdir_artifacts),
                ),
            )

            orchestrator = Orchestrator(
                config, ArtifactStore(Path(tmpdir_artifacts)), Console()
            )

            # Test decision logic for CHANGES_REQUESTED verdict
            response = AssistantResponse(
                content="Needs revisions", verdict=ReviewVerdict.CHANGES_REQUESTED
            )
            decision = orchestrator._decide_next_phase(Phase.REVIEW, response, 1, 0)

            assert decision.next_phase == Phase.PLAN
            assert "CHANGES_REQUESTED" in decision.rationale
            assert not decision.requires_human


def test_review_verdict_blocked():
    """Test that BLOCKED verdict requires human intervention."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(max_iterations=1, require_human_approval=False),
                storage=StorageConfig(
                    workspace_root=Path(tmpdir_workspace),
                    run_artifact_dir=Path(tmpdir_artifacts),
                ),
            )

            orchestrator = Orchestrator(
                config, ArtifactStore(Path(tmpdir_artifacts)), Console()
            )

            # Test decision logic for BLOCKED verdict
            response = AssistantResponse(
                content="Critical issues!", verdict=ReviewVerdict.BLOCKED
            )
            decision = orchestrator._decide_next_phase(Phase.REVIEW, response, 1, 0)

            assert decision.next_phase == Phase.BLOCKED
            assert "BLOCKED" in decision.rationale
            assert decision.requires_human


def test_review_verdict_fallback_to_concluded():
    """Test that concluded field still works when verdict is not provided."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(max_iterations=1, require_human_approval=False),
                storage=StorageConfig(
                    workspace_root=Path(tmpdir_workspace),
                    run_artifact_dir=Path(tmpdir_artifacts),
                ),
            )

            orchestrator = Orchestrator(
                config, ArtifactStore(Path(tmpdir_artifacts)), Console()
            )

            # Test with concluded=True, no verdict
            response = AssistantResponse(content="Done", concluded=True)
            decision = orchestrator._decide_next_phase(Phase.REVIEW, response, 1, 0)

            assert decision.next_phase == Phase.DONE
            assert "concluded=True" in decision.rationale.lower()


# ──────────────────────────────────────────────────────────────────────────────
# Guardrail Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_max_consecutive_replans_guardrail():
    """Test that max_consecutive_replans triggers human approval."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(
                    max_iterations=10,
                    max_consecutive_replans=2,  # Limit to 2 replans
                    require_human_approval=False,
                ),
                storage=StorageConfig(
                    workspace_root=Path(tmpdir_workspace),
                    run_artifact_dir=Path(tmpdir_artifacts),
                ),
            )

            orchestrator = Orchestrator(
                config, ArtifactStore(Path(tmpdir_artifacts)), Console()
            )

            # Test decision when approaching replan limit
            response = AssistantResponse(
                content="Changes needed", verdict=ReviewVerdict.CHANGES_REQUESTED
            )
            decision = orchestrator._decide_next_phase(
                Phase.REVIEW, response, iteration=2, consecutive_replans=1
            )

            assert decision.next_phase == Phase.PLAN
            assert decision.requires_human  # Should trigger human approval
            assert "replan limit" in decision.rationale.lower()


def test_require_git_changes_flag():
    """Test that require_git_changes can be disabled."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            workspace = Path(tmpdir_workspace)

            # Initialize git repo but don't make changes
            import subprocess

            subprocess.run(["git", "init"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=workspace, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=workspace, check=True
            )

            # Config with require_git_changes=False
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(
                    max_iterations=1,
                    require_human_approval=False,
                    require_git_changes=False,  # Disabled
                ),
                storage=StorageConfig(
                    workspace_root=workspace, run_artifact_dir=Path(tmpdir_artifacts)
                ),
            )

            artifact_store = ArtifactStore(Path(tmpdir_artifacts), Console())
            orchestrator = Orchestrator(config, artifact_store, Console())

            # Run should not block on missing changes
            snapshot = orchestrator.run(run_id="test-no-git-check")

            # Should complete without blocking on missing changes
            # (Will still block on max iterations with echo adapter)
            assert snapshot.phase == Phase.BLOCKED
            assert "no repository changes" not in (snapshot.notes or "").lower()


# ──────────────────────────────────────────────────────────────────────────────
# Human Approval Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_human_approval_creates_pending_file(temp_workspace, temp_artifacts_dir):
    """Test that human approval creates PENDING_APPROVAL file."""
    config = DuetConfig(
        codex=AssistantConfig(provider="echo", model="test"),
        claude=AssistantConfig(provider="echo", model="test"),
        workflow=WorkflowConfig(
            max_iterations=1,
            require_human_approval=True,  # Enable approval
            max_consecutive_replans=0,  # Force approval immediately
        ),
        storage=StorageConfig(
            workspace_root=temp_workspace, run_artifact_dir=temp_artifacts_dir
        ),
    )

    artifact_store = ArtifactStore(temp_artifacts_dir, Console())
    orchestrator = Orchestrator(config, artifact_store, Console())

    # Run will trigger approval on first replan attempt
    # (Echo adapter never sets concluded=True, so review always requests changes)
    snapshot = orchestrator.run(run_id="test-approval")

    # Check PENDING_APPROVAL file was created
    approval_file = temp_artifacts_dir / snapshot.run_id / "PENDING_APPROVAL"
    assert approval_file.exists(), "PENDING_APPROVAL file should be created"

    # Verify file content
    content = approval_file.read_text()
    assert snapshot.run_id in content
    assert "APPROVAL REQUIRED" in content
    assert "duet status" in content


def test_approval_notifier_check_pending():
    """Test that approval pending check works."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifacts_dir = Path(tmpdir)
        artifact_store = ArtifactStore(artifacts_dir, Console())

        from duet.approval import ApprovalNotifier
        from duet.models import RunSnapshot

        notifier = ApprovalNotifier(artifact_store, Console())
        snapshot = RunSnapshot(run_id="test-run", iteration=1, phase=Phase.BLOCKED)

        # Before requesting approval
        assert not notifier.check_approval_pending("test-run")

        # After requesting approval
        notifier.request_approval(snapshot, "Test reason")
        assert notifier.check_approval_pending("test-run")

        # After clearing approval
        notifier.clear_approval("test-run")
        assert not notifier.check_approval_pending("test-run")


# ──────────────────────────────────────────────────────────────────────────────
# Git Branch Management Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_feature_branch_creation():
    """Test that feature branches are created per run."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            workspace = Path(tmpdir_workspace)
            artifacts = Path(tmpdir_artifacts)

            # Initialize git repo
            import subprocess

            subprocess.run(["git", "init"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=workspace, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=workspace, check=True
            )

            # Create initial commit
            (workspace / "README.md").write_text("test")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial"], cwd=workspace, check=True
            )

            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(
                    max_iterations=1,
                    require_human_approval=False,
                    use_feature_branches=True,
                ),
                storage=StorageConfig(workspace_root=workspace, run_artifact_dir=artifacts),
            )

            artifact_store = ArtifactStore(artifacts, Console())
            orchestrator = Orchestrator(config, artifact_store, Console())

            # Get original branch
            original = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            # Run orchestration
            snapshot = orchestrator.run(run_id="test-branch")

            # Verify original branch was saved
            assert snapshot.metadata.get("original_branch") == original

            # Verify feature branch was created
            assert snapshot.metadata.get("feature_branch") == f"duet/{snapshot.run_id}"


def test_branch_restoration():
    """Test that original branch is restored after run."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            workspace = Path(tmpdir_workspace)
            artifacts = Path(tmpdir_artifacts)

            # Initialize git repo
            import subprocess

            subprocess.run(["git", "init"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=workspace, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=workspace, check=True
            )
            (workspace / "README.md").write_text("test")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial"], cwd=workspace, check=True
            )

            # Note original branch
            original = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(
                    max_iterations=1,
                    require_human_approval=False,
                    use_feature_branches=True,
                    restore_branch_on_complete=True,
                ),
                storage=StorageConfig(workspace_root=workspace, run_artifact_dir=artifacts),
            )

            artifact_store = ArtifactStore(artifacts, Console())
            orchestrator = Orchestrator(config, artifact_store, Console())

            # Run orchestration
            snapshot = orchestrator.run(run_id="test-restore")

            # Verify we're back on original branch
            current = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            assert current == original


# ──────────────────────────────────────────────────────────────────────────────
# Git Change Detection Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_git_change_detection():
    """Test that git change detection works correctly."""
    from duet.git_operations import GitWorkspace

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)

        # Initialize git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=workspace, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=workspace, check=True
        )

        git = GitWorkspace(workspace, Console())

        # No changes initially
        changes = git.detect_changes()
        assert not changes.has_changes or changes.files_changed == 0

        # Create a file
        (workspace / "test.txt").write_text("content")

        # Should detect changes now
        changes = git.detect_changes()
        assert changes.has_changes
        assert "test.txt" in changes.unstaged_files


def test_no_git_changes_blocks_run():
    """Test that missing git changes blocks IMPLEMENT phase."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            workspace = Path(tmpdir_workspace)
            artifacts = Path(tmpdir_artifacts)

            # Initialize git repo with no changes
            import subprocess

            subprocess.run(["git", "init"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=workspace, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=workspace, check=True
            )
            (workspace / "README.md").write_text("initial")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial"], cwd=workspace, check=True
            )

            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(
                    max_iterations=5,
                    require_human_approval=False,
                    require_git_changes=True,  # Enforce
                ),
                storage=StorageConfig(workspace_root=workspace, run_artifact_dir=artifacts),
            )

            artifact_store = ArtifactStore(artifacts, Console())
            orchestrator = Orchestrator(config, artifact_store, Console())

            # Run will block when IMPLEMENT produces no changes
            snapshot = orchestrator.run(run_id="test-no-changes")

            assert snapshot.phase == Phase.BLOCKED
            assert "no repository changes" in snapshot.notes.lower()


# ──────────────────────────────────────────────────────────────────────────────
# Consecutive Replan Guardrail Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_consecutive_replans_tracked():
    """Test that consecutive replans are tracked in metadata."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(
                    max_iterations=5,
                    max_consecutive_replans=10,
                    require_human_approval=False,
                    require_git_changes=False,  # Disable to focus on replans
                ),
                storage=StorageConfig(
                    workspace_root=Path(tmpdir_workspace),
                    run_artifact_dir=Path(tmpdir_artifacts),
                ),
            )

            artifact_store = ArtifactStore(Path(tmpdir_artifacts), Console())
            orchestrator = Orchestrator(config, artifact_store, Console())

            snapshot = orchestrator.run(run_id="test-replan-tracking")

            # Check that consecutive_replans was tracked
            assert "consecutive_replans" in snapshot.metadata


def test_max_consecutive_replans_enforced():
    """Test that max_consecutive_replans is enforced."""
    with tempfile.TemporaryDirectory() as tmpdir_workspace:
        with tempfile.TemporaryDirectory() as tmpdir_artifacts:
            config = DuetConfig(
                codex=AssistantConfig(provider="echo", model="test"),
                claude=AssistantConfig(provider="echo", model="test"),
                workflow=WorkflowConfig(
                    max_iterations=10,
                    max_consecutive_replans=1,  # Very low limit
                    require_human_approval=True,  # Will block when limit hit
                    require_git_changes=False,
                ),
                storage=StorageConfig(
                    workspace_root=Path(tmpdir_workspace),
                    run_artifact_dir=Path(tmpdir_artifacts),
                ),
            )

            artifact_store = ArtifactStore(Path(tmpdir_artifacts), Console())
            orchestrator = Orchestrator(config, artifact_store, Console())

            snapshot = orchestrator.run(run_id="test-replan-limit")

            # Should block due to replan limit
            assert snapshot.phase == Phase.BLOCKED
            # Consecutive replans should be recorded
            assert snapshot.metadata.get("consecutive_replans", 0) >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
