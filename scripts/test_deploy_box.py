"""Tests for the deterministic box deploy script (REQ-DEP-001..004).

Deploys have been hand-rolled rsyncs per session; on 2026-07-26 one deleted the
box's runtime `reports/` dir (Monday's weekly-P&L died on mount namespacing)
and shipped untracked HEIC photos to prod. The script makes the transfer
deterministic: clean-worktree guard, gitignore-driven excludes, protected
runtime dirs, DRY-RUN by default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.deploy_box import (
    PROTECTED_PATHS,
    WorktreeDirtyError,
    build_rsync_command,
    verify_worktree_clean,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text("data/\nreports/\n*.secret\n")
    (r / "app.py").write_text("print('hi')\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


# ---------------------------------------------------------------------------
# REQ-DEP-002: clean-worktree guard
# ---------------------------------------------------------------------------


class TestWorktreeGuard:
    def test_clean_worktree_passes(self, repo: Path) -> None:
        verify_worktree_clean(repo)  # no raise

    def test_untracked_file_fails(self, repo: Path) -> None:
        (repo / "IMG_1413.HEIC").write_bytes(b"\x00")
        with pytest.raises(WorktreeDirtyError, match="IMG_1413.HEIC"):
            verify_worktree_clean(repo)

    def test_modified_tracked_file_fails(self, repo: Path) -> None:
        (repo / "app.py").write_text("print('changed')\n")
        with pytest.raises(WorktreeDirtyError):
            verify_worktree_clean(repo)

    def test_gitignored_files_are_allowed(self, repo: Path) -> None:
        (repo / "data").mkdir()
        (repo / "data" / "accounting.db").write_bytes(b"\x00")
        (repo / "creds.secret").write_text("x")
        verify_worktree_clean(repo)  # no raise


# ---------------------------------------------------------------------------
# REQ-DEP-001/003: rsync command construction
# ---------------------------------------------------------------------------


class TestRsyncCommand:
    def test_dry_run_by_default(self, repo: Path) -> None:
        cmd = build_rsync_command(repo, "travis@ubuntu:/home/travis/accounting/")
        assert "--dry-run" in cmd

    def test_apply_removes_dry_run(self, repo: Path) -> None:
        cmd = build_rsync_command(
            repo, "travis@ubuntu:/home/travis/accounting/", apply=True
        )
        assert "--dry-run" not in cmd

    def test_deletes_stale_files_but_protects_runtime_dirs(self, repo: Path) -> None:
        cmd = build_rsync_command(repo, "t@h:/dst/")
        assert "--delete" in cmd
        for p in PROTECTED_PATHS:
            assert f"protect {p}" in " ".join(cmd)

    def test_protect_filters_precede_gitignore_filter(self, repo: Path) -> None:
        """Protect rules must come before the .gitignore exclude merge, or
        --delete would remove gitignored runtime dirs (data/, reports/) on the
        box — the exact 2026-07-26 failure."""
        joined = [a for a in build_rsync_command(repo, "t@h:/dst/") if "--filter" in a or "filter=" in a or a.startswith("protect") or ":-" in a]
        flat = " || ".join(joined)
        first_protect = flat.find("protect")
        gitignore_merge = flat.find(":- .gitignore")
        assert first_protect != -1 and gitignore_merge != -1
        assert first_protect < gitignore_merge

    def test_excludes_git_dir(self, repo: Path) -> None:
        assert any(".git" in a for a in build_rsync_command(repo, "t@h:/dst/"))

    def test_source_is_repo_root_with_trailing_slash(self, repo: Path) -> None:
        cmd = build_rsync_command(repo, "t@h:/dst/")
        assert cmd[-2] == f"{repo}/"
        assert cmd[-1] == "t@h:/dst/"


class TestWithDashboard:
    def test_with_dashboard_includes_sveltekit_before_gitignore_merge(
        self, repo: Path
    ) -> None:
        """--with-dashboard must ADD include filters ahead of the .gitignore
        merge — .svelte-kit is gitignored, so lifting the protect filter alone
        still excludes the build from the transfer entirely."""
        cmd = build_rsync_command(repo, "t@h:/dst/", with_dashboard=True)
        filters = [a for a in cmd if a.startswith("--filter=")]
        include_idx = next(
            i for i, a in enumerate(filters) if a == "--filter=+ /dashboard/.svelte-kit/***"
        )
        gitignore_idx = next(i for i, a in enumerate(filters) if ":- .gitignore" in a)
        assert include_idx < gitignore_idx

    def test_without_dashboard_no_include(self, repo: Path) -> None:
        cmd = build_rsync_command(repo, "t@h:/dst/")
        assert all("+ /dashboard/.svelte-kit" not in a for a in cmd)
