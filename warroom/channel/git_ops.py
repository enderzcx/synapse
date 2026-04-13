"""Git operations for warroom agents.

Thin async wrappers around git subprocess calls. No branch isolation —
all agents work on the same branch (AI Native: conflicts are resolved
by AI, not prevented by branches).
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("a2a.channel.git_ops")

# Per-command timeout defaults (seconds).
# rev-parse is fast; status/add/commit need more headroom on large repos.
TIMEOUT_FAST = 10.0   # rev-parse, diff --cached --name-only
TIMEOUT_MEDIUM = 30.0  # status --porcelain, rev-list
TIMEOUT_SLOW = 60.0   # add -A, commit


async def _run(
    cmd: list[str], cwd: str, timeout: float = TIMEOUT_MEDIUM
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("git command timed out after %.0fs: %s", timeout, " ".join(cmd))
        proc.kill()
        await proc.wait()
        return (1, "", f"timeout after {timeout:.0f}s: {' '.join(cmd)}")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def get_status(cwd: str) -> dict:
    """Return current branch, modified/staged files, commits ahead of main."""
    rc_branch, branch, err = await _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd, timeout=TIMEOUT_FAST
    )
    if rc_branch != 0:
        return {"ok": False, "error": f"not a git repo: {err}"}

    rc_status, status_out, status_err = await _run(
        ["git", "status", "--porcelain"], cwd, timeout=TIMEOUT_MEDIUM
    )
    if rc_status != 0:
        return {"ok": False, "error": f"git status failed: {status_err}"}

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
    rc_ahead, ahead_out, ahead_err = await _run(
        ["git", "rev-list", "--count", "main..HEAD"], cwd, timeout=TIMEOUT_FAST
    )
    commits_ahead = int(ahead_out) if rc_ahead == 0 and ahead_out.isdigit() else 0
    if rc_ahead != 0:
        logger.warning("rev-list failed (rc=%d): %s", rc_ahead, ahead_err)

    result: dict = {
        "ok": True,
        "branch": branch,
        "modified": modified,
        "staged": staged,
        "commits_ahead": commits_ahead,
    }
    if rc_ahead != 0:
        result["ahead_error"] = ahead_err or "rev-list failed"
    return result


async def commit_all(message: str, cwd: str) -> dict:
    """Stage all changes and commit. Returns commit hash and changed files."""
    # Stage everything
    rc_add, _, add_err = await _run(["git", "add", "-A"], cwd, timeout=TIMEOUT_SLOW)
    if rc_add != 0:
        return {"ok": False, "error": f"git add failed: {add_err}", "step": "add"}

    # Check if there's anything to commit
    rc_diff, diff_out, diff_err = await _run(
        ["git", "diff", "--cached", "--name-only"], cwd, timeout=TIMEOUT_FAST
    )
    if rc_diff != 0:
        return {"ok": False, "error": f"git diff failed: {diff_err}", "step": "diff"}
    files = [f for f in diff_out.splitlines() if f.strip()]
    if not files:
        return {"ok": False, "error": "nothing to commit", "step": "diff"}

    # Commit
    rc_commit, commit_out, commit_err = await _run(
        ["git", "commit", "-m", message], cwd, timeout=TIMEOUT_SLOW
    )
    if rc_commit != 0:
        return {"ok": False, "error": f"git commit failed: {commit_err}", "step": "commit"}

    # Get commit hash
    rc_hash, hash_out, _ = await _run(
        ["git", "rev-parse", "--short", "HEAD"], cwd, timeout=TIMEOUT_FAST
    )
    commit_hash = hash_out if rc_hash == 0 else "unknown"

    # Get current branch
    _, branch, _ = await _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd, timeout=TIMEOUT_FAST
    )

    return {
        "ok": True,
        "commit": commit_hash,
        "branch": branch,
        "files": files,
        "message": message,
    }
