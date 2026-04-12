"""Git operations for warroom agents.

Thin async wrappers around git subprocess calls. No branch isolation —
all agents work on the same branch (AI Native: conflicts are resolved
by AI, not prevented by branches).
"""
from __future__ import annotations

import asyncio
import os


async def _run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def get_status(cwd: str) -> dict:
    """Return current branch, modified/staged files, commits ahead of main."""
    rc_branch, branch, _ = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if rc_branch != 0:
        return {"ok": False, "error": "not a git repo"}

    rc_status, status_out, _ = await _run(["git", "status", "--porcelain"], cwd)
    modified = []
    staged = []
    for line in status_out.splitlines():
        line = line.rstrip("\r")  # Windows CRLF
        if len(line) < 4:
            continue
        idx, wt = line[0], line[1]
        fname = line[3:].strip()
        # Skip untracked files for modified/staged lists
        if idx == "?" and wt == "?":
            modified.append(fname)  # treat untracked as "modified" for visibility
            continue
        if idx not in (" ", "?"):
            staged.append(fname)
        if wt not in (" ", "?"):
            modified.append(fname)

    # Commits ahead of main (if main exists)
    rc_ahead, ahead_out, _ = await _run(
        ["git", "rev-list", "--count", "main..HEAD"], cwd
    )
    commits_ahead = int(ahead_out) if rc_ahead == 0 and ahead_out.isdigit() else 0

    return {
        "ok": True,
        "branch": branch,
        "modified": modified,
        "staged": staged,
        "commits_ahead": commits_ahead,
    }


async def commit_all(message: str, cwd: str) -> dict:
    """Stage all changes and commit. Returns commit hash and changed files."""
    # Stage everything
    rc_add, _, add_err = await _run(["git", "add", "-A"], cwd)
    if rc_add != 0:
        return {"ok": False, "error": f"git add failed: {add_err}"}

    # Check if there's anything to commit
    rc_diff, diff_out, _ = await _run(["git", "diff", "--cached", "--name-only"], cwd)
    files = [f for f in diff_out.splitlines() if f.strip()]
    if not files:
        return {"ok": False, "error": "nothing to commit"}

    # Commit
    rc_commit, commit_out, commit_err = await _run(
        ["git", "commit", "-m", message], cwd
    )
    if rc_commit != 0:
        return {"ok": False, "error": f"git commit failed: {commit_err}"}

    # Get commit hash
    rc_hash, hash_out, _ = await _run(
        ["git", "rev-parse", "--short", "HEAD"], cwd
    )
    commit_hash = hash_out if rc_hash == 0 else "unknown"

    # Get current branch
    _, branch, _ = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)

    return {
        "ok": True,
        "commit": commit_hash,
        "branch": branch,
        "files": files,
        "message": message,
    }
