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

    def detect_changes(self) -> GitChangesSummary:
        """
        Detect changes in the workspace.

        Returns a summary of:
        - Staged files
        - Unstaged files
        - Latest commit SHA
        - Diff statistics
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

        # Get diff statistics
        diff_stat_result = self._run_git("diff", "--stat", "HEAD", check=False)
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
                            files_changed = int(part.split()[0])
                        elif "insertion" in part:
                            insertions = int(part.split()[0])
                        elif "deletion" in part:
                            deletions = int(part.split()[0])

        has_changes = bool(staged_files or all_unstaged or files_changed > 0)

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
