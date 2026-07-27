#!/usr/bin/env python3
"""Deterministic Mac→box deploy (REQ-DEP-001..004).

Every deploy before this script was a hand-rolled rsync; on 2026-07-26 one
deleted the box's runtime ``reports/`` dir (Monday's weekly-P&L then failed on
mount namespacing) and shipped untracked HEIC photos to prod. This script makes
the transfer deterministic:

- **Clean-worktree guard** (REQ-DEP-002): refuses to run if the worktree has
  modified tracked files or untracked non-ignored files — the committed tree is
  what ships, junk physically cannot.
- **gitignore-driven excludes** (REQ-DEP-001): ``--filter=':- .gitignore'``
  keeps every gitignored path out of the transfer.
- **Protected runtime dirs** (REQ-DEP-003): ``data/``, ``reports/``, ``.venv/``
  and friends are protected from ``--delete`` so a deploy can never wipe them
  again.
- **DRY-RUN default** (REQ-DEP-004): prints what would change; ``--apply``
  transfers, ``--restart`` bounces units afterwards.

Usage (from the deploy worktree, e.g. /Users/travis/dev/accounting-deploy):

    python -m scripts.deploy_box                    # dry-run against the box
    python -m scripts.deploy_box --apply            # transfer
    python -m scripts.deploy_box --apply --restart accounting-api accounting-dashboard

The dashboard build (``dashboard/.svelte-kit``) is gitignored, so it does NOT
ship through this path by default — pass ``--with-dashboard`` after running
``cd dashboard && doppler run --config srv -- npm run build`` to push it too.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_DEST = "travis@ubuntu:/home/travis/accounting/"

# Runtime state on the box that a deploy must never delete. `protect` filters
# beat --delete; they MUST precede the .gitignore merge filter (a gitignored
# path that is also protected stays protected only if protect comes first).
PROTECTED_PATHS = (
    "data/***",
    "reports/***",
    ".venv/***",
    "dashboard/node_modules/***",
    "dashboard/.svelte-kit/***",
)


class WorktreeDirtyError(RuntimeError):
    """The worktree has changes that a deterministic deploy refuses to ship."""


def verify_worktree_clean(repo: Path) -> None:
    """REQ-DEP-002: modified tracked files or untracked non-ignored files abort.

    ``git status --porcelain`` omits ignored files, so runtime junk that is
    properly gitignored (data/, *.secret, …) does not block a deploy — but a
    stray photo or hand-edit does.
    """
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if out.strip():
        raise WorktreeDirtyError(
            "worktree is not clean — commit, stash, or remove these before "
            f"deploying:\n{out}"
        )


def build_rsync_command(
    repo: Path,
    dest: str,
    *,
    apply: bool = False,
    with_dashboard: bool = False,
) -> list[str]:
    """REQ-DEP-001/003: one rsync invocation, fully determined by the repo state."""
    cmd = ["rsync", "-az", "--itemize-changes", "--delete"]
    if not apply:
        cmd.append("--dry-run")
    for p in PROTECTED_PATHS:
        if with_dashboard and p.startswith("dashboard/.svelte-kit"):
            continue  # explicitly pushing a fresh build this run
        cmd.append(f"--filter=protect {p}")
    cmd += [
        "--exclude=.git",
        "--filter=:- .gitignore",
        f"{repo}/",
        dest,
    ]
    return cmd


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deterministic Mac→box deploy.")
    p.add_argument("--apply", action="store_true", help="Transfer (default: DRY-RUN).")
    p.add_argument(
        "--dest", default=DEFAULT_DEST, help=f"rsync destination (default {DEFAULT_DEST})"
    )
    p.add_argument(
        "--with-dashboard",
        action="store_true",
        help="Also push dashboard/.svelte-kit (run the srv-config build first).",
    )
    p.add_argument(
        "--restart",
        nargs="*",
        default=None,
        metavar="UNIT",
        help="After an applied transfer, restart these systemd units via ssh root@ubuntu.",
    )
    p.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repo root to deploy from (default: this checkout).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        verify_worktree_clean(args.repo)
    except WorktreeDirtyError as exc:
        print(f"deploy_box: REFUSING — {exc}", file=sys.stderr)
        return 1

    cmd = build_rsync_command(
        args.repo, args.dest, apply=args.apply, with_dashboard=args.with_dashboard
    )
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"deploy_box {mode}: {' '.join(cmd)}")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"deploy_box: rsync exited {rc}", file=sys.stderr)
        return rc

    if args.apply and args.restart:
        units = " ".join(args.restart)
        print(f"deploy_box: restarting on box: {units}")
        rc = subprocess.run(
            ["ssh", "root@ubuntu", f"systemctl restart {units}"]
        ).returncode
        if rc != 0:
            print("deploy_box: restart failed", file=sys.stderr)
            return rc
    elif not args.apply:
        print("deploy_box: dry-run only — re-run with --apply to transfer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
