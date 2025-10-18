"""Git workspace management and change detection utilities."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console


@dataclass
class GitChangesSummary:
    """Summary of git changes in the workspace."""

    has_changes: bool
    staged_files: list[str]
    unstaged_files: list[str]
    commit_sha: Optional[str]
    diff_stat: str
    files_changed: int
    insertions: int
    deletions: int


class GitError(Exception):
    """Exception raised when git operations fail."""

    pass


class GitWorkspace:
    """Manages git operations for a Duet workspace."""

    def __init__(self, workspace_root: Path, console: Optional[Console] = None):
        self.workspace_root = workspace_root
        self.console = console or Console()

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the workspace."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.workspace_root), *args],
                capture_output=True,
                text=True,
                check=check,
            )
            return result
        except subprocess.CalledProcessError as exc:
            raise GitError(f"Git command failed: git {' '.join(args)}\n{exc.stderr}") from exc
        except FileNotFoundError as exc:
            raise GitError("Git executable not found. Is git installed?") from exc

    def is_git_repo(self) -> bool:
        """Check if workspace is a git repository."""
        try:
            self._run_git("rev-parse", "--git-dir", check=False)
            return True
        except GitError:
            return False

    def get_current_branch(self) -> str:
        """Get the currently checked out branch name."""
        result = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip()

    def get_current_commit(self) -> str:
        """Get the current commit SHA."""
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def detect_changes(self, baseline_commit: Optional[str] = None) -> GitChangesSummary:
        """
        Detect changes in the workspace.

        Args:
            baseline_commit: Compare against this commit instead of HEAD.
                           If provided, detects new commits and changes since baseline.
                           If None, only detects working tree changes (staged/unstaged).

        Returns a summary of:
        - Staged files
        - Unstaged files
        - Latest commit SHA
        - Diff statistics (against baseline if provided, otherwise HEAD)
        - New commits since baseline
        """
        # Get staged files
        staged_result = self._run_git("diff", "--cached", "--name-only")
        staged_files = [f for f in staged_result.stdout.strip().split("\n") if f]

        # Get unstaged files (modified but not staged)
        unstaged_result = self._run_git("diff", "--name-only")
        unstaged_files = [f for f in unstaged_result.stdout.strip().split("\n") if f]

        # Get untracked files
        untracked_result = self._run_git("ls-files", "--others", "--exclude-standard")
        untracked_files = [f for f in untracked_result.stdout.strip().split("\n") if f]

        # Combine unstaged and untracked
        all_unstaged = list(set(unstaged_files + untracked_files))

        # Get current commit
        try:
            commit_sha = self.get_current_commit()
        except GitError:
            commit_sha = None  # No commits yet

        # Get diff statistics against baseline (or HEAD if no baseline)
        if baseline_commit and commit_sha:
            # Compare current commit against baseline to detect new commits
            diff_stat_result = self._run_git("diff", "--stat", baseline_commit, check=False)
            new_commits = self.get_commits_since(baseline_commit) if baseline_commit != commit_sha else []
        else:
            # No baseline: compare working tree against HEAD
            diff_stat_result = self._run_git("diff", "--stat", "HEAD", check=False)
            new_commits = []

        diff_stat = diff_stat_result.stdout.strip()

        # Parse diff statistics
        files_changed = 0
        insertions = 0
        deletions = 0

        if diff_stat:
            # Last line usually has summary: "X files changed, Y insertions(+), Z deletions(-)"
            lines = diff_stat.strip().split("\n")
            if lines:
                summary_line = lines[-1]
                if "file" in summary_line or "changed" in summary_line:
                    parts = summary_line.split(",")
                    for part in parts:
                        if "file" in part and "changed" in part:
                            try:
                                files_changed = int(part.split()[0])
                            except (ValueError, IndexError):
                                pass
                        elif "insertion" in part:
                            try:
                                insertions = int(part.split()[0])
                            except (ValueError, IndexError):
                                pass
                        elif "deletion" in part:
                            try:
                                deletions = int(part.split()[0])
                            except (ValueError, IndexError):
                                pass

        # Has changes if: working tree changes OR new commits since baseline
        has_changes = bool(
            staged_files
            or all_unstaged
            or files_changed > 0
            or new_commits
            or (baseline_commit and commit_sha and baseline_commit != commit_sha)
        )

        return GitChangesSummary(
            has_changes=has_changes,
            staged_files=staged_files,
            unstaged_files=all_unstaged,
            commit_sha=commit_sha,
            diff_stat=diff_stat,
            files_changed=files_changed,
            insertions=insertions,
            deletions=deletions,
        )

    def create_branch(self, branch_name: str) -> None:
        """Create a new git branch."""
        self._run_git("branch", branch_name)
        self.console.log(f"[green]Created branch:[/] {branch_name}")

    def checkout_branch(self, branch_name: str, create: bool = False) -> None:
        """Checkout a git branch, optionally creating it."""
        if create:
            self._run_git("checkout", "-b", branch_name)
            self.console.log(f"[green]Created and checked out branch:[/] {branch_name}")
        else:
            self._run_git("checkout", branch_name)
            self.console.log(f"[green]Checked out branch:[/] {branch_name}")

    def branch_exists(self, branch_name: str) -> bool:
        """Check if a branch exists."""
        result = self._run_git("rev-parse", "--verify", f"refs/heads/{branch_name}", check=False)
        return result.returncode == 0

    def get_last_commit_message(self) -> str:
        """Get the last commit message."""
        result = self._run_git("log", "-1", "--pretty=%B")
        return result.stdout.strip()

    def get_commits_since(self, base_commit: str) -> list[str]:
        """Get list of commit SHAs since a base commit."""
        result = self._run_git("rev-list", f"{base_commit}..HEAD")
        commits = [c for c in result.stdout.strip().split("\n") if c]
        return commits

    # ──────────────────────────────────────────────────────────────────────────
    # State Baseline Management
    # ──────────────────────────────────────────────────────────────────────────

    def create_state_baseline(
        self, state_id: str, branch_prefix: str = "duet/state"
    ) -> dict[str, str]:
        """
        Create a git baseline for a state checkpoint.

        Args:
            state_id: State identifier
            branch_prefix: Prefix for state branch (default: duet/state)

        Returns:
            Dictionary with baseline metadata:
            - commit: Current commit SHA
            - branch: Current branch name
            - state_branch: Created state branch name (if created)
            - clean: Whether working tree is clean
        """
        # Get current commit and branch
        commit = self.get_current_commit()
        branch = self.get_current_branch()

        # Check if working tree is clean
        status_result = self._run_git("status", "--porcelain")
        clean = not bool(status_result.stdout.strip())

        # Create state branch for checkpoint (optional)
        state_branch = f"{branch_prefix}/{state_id}"
        if not self.branch_exists(state_branch):
            try:
                self._run_git("branch", state_branch)
                self.console.log(f"[dim]Created state branch:[/] {state_branch}")
            except GitError as exc:
                self.console.log(f"[yellow]Warning: Could not create state branch: {exc}[/]")
                state_branch = None
        else:
            state_branch = None

        return {
            "commit": commit,
            "branch": branch,
            "state_branch": state_branch,
            "clean": clean,
        }

    def restore_state(
        self,
        baseline_commit: str,
        original_branch: Optional[str] = None,
        state_branch: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """
        Restore git workspace to a state checkpoint.

        Args:
            baseline_commit: Commit SHA to restore to
            original_branch: Original branch to checkout (if provided)
            state_branch: State-specific branch to checkout (if provided)
            force: Force reset even if working tree is dirty

        Raises:
            GitError: If restoration fails
        """
        # Check working tree status
        status_result = self._run_git("status", "--porcelain")
        dirty = bool(status_result.stdout.strip())

        if dirty and not force:
            raise GitError(
                "Working tree has uncommitted changes. "
                "Commit or stash changes before restoring state, or use force=True."
            )

        # Determine target: state_branch > original_branch > commit
        if state_branch and self.branch_exists(state_branch):
            # Checkout state branch
            self.console.log(f"[cyan]Restoring state branch:[/] {state_branch}")
            self._run_git("checkout", state_branch)
        elif original_branch:
            # Checkout original branch
            self.console.log(f"[cyan]Restoring branch:[/] {original_branch}")
            self._run_git("checkout", original_branch)
            # Reset to baseline commit
            self.console.log(f"[cyan]Resetting to commit:[/] {baseline_commit[:8]}")
            reset_flag = "--hard" if force else "--mixed"
            self._run_git("reset", reset_flag, baseline_commit)
        else:
            # Detached HEAD at baseline commit
            self.console.log(f"[cyan]Checking out commit:[/] {baseline_commit[:8]}")
            self._run_git("checkout", baseline_commit)

        self.console.log("[green]State restored successfully[/]")

    def has_uncommitted_changes(self) -> bool:
        """Check if working tree has uncommitted changes."""
        status_result = self._run_git("status", "--porcelain")
        return bool(status_result.stdout.strip())

    def stash_changes(self, message: Optional[str] = None) -> bool:
        """
        Stash current working tree changes.

        Args:
            message: Optional stash message

        Returns:
            True if changes were stashed, False if nothing to stash
        """
        if not self.has_uncommitted_changes():
            return False

        stash_args = ["stash", "push"]
        if message:
            stash_args.extend(["-m", message])

        self._run_git(*stash_args)
        self.console.log("[cyan]Stashed working tree changes[/]")
        return True

    def pop_stash(self) -> None:
        """Pop the most recent stash."""
        self._run_git("stash", "pop")
        self.console.log("[cyan]Restored stashed changes[/]")
