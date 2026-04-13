"""Git operations for warroom agents.

Thin async wrappers around git subprocess calls. No branch isolation —
all agents work on the same branch (AI Native: conflicts are resolved
by AI, not prevented by branches).

Layer 2: async job model — submit_commit_job() returns immediately with a
job_id; the actual git work runs in a background task. Callers poll via
get_job_status() or receive channel notifications on completion.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

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


# ---------------------------------------------------------------------------
# Layer 2: Async job model
# ---------------------------------------------------------------------------

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

# In-memory job store — sufficient for local single-process use.
_jobs: dict[str, dict[str, Any]] = {}


def get_job_status(job_id: str) -> dict:
    """Return current status of a commit job."""
    job = _jobs.get(job_id)
    if job is None:
        return {"ok": False, "error": f"unknown job_id: {job_id}"}
    return {
        "ok": True,
        "job_id": job_id,
        "status": job["status"],
        "result": job.get("result"),
    }


async def _run_commit_job(job_id: str, message: str, cwd: str) -> None:
    """Background task that executes commit_all and stores the result."""
    job = _jobs[job_id]
    job["status"] = JOB_RUNNING
    try:
        result = await commit_all(message=message, cwd=cwd)
    except Exception as exc:
        logger.exception("commit job %s crashed", job_id)
        result = {"ok": False, "error": str(exc), "step": "unknown"}
    job["result"] = result
    job["status"] = JOB_SUCCEEDED if result.get("ok") else JOB_FAILED
    # Fire completion callback if registered
    cb = job.get("on_complete")
    if cb is not None:
        try:
            await cb(job_id, result)
        except Exception:
            logger.exception("on_complete callback failed for job %s", job_id)


def submit_commit_job(
    message: str,
    cwd: str,
    on_complete: Any = None,
) -> str:
    """Submit a commit job that runs in the background.

    Returns the job_id immediately. The caller can poll with
    get_job_status(job_id) or provide an async on_complete(job_id, result)
    callback that fires when the job finishes.
    """
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {
        "status": JOB_QUEUED,
        "message": message,
        "result": None,
        "on_complete": on_complete,
    }
    asyncio.get_event_loop().create_task(_run_commit_job(job_id, message, cwd))
    return job_id
