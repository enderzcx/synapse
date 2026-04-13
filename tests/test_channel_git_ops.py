"""Git ops tests using a temp git repo; they do not touch the real project."""

import os
import sys
import asyncio

import pytest

from warroom.channel import git_ops
from warroom.channel.git_ops import (
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    _run,
    commit_all,
    get_job_status,
    get_status,
    submit_commit_job,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one commit."""
    cwd = str(tmp_path)
    os.system(
        f'cd /d "{cwd}" && git init && git checkout -b main && '
        f'echo hello > readme.txt && git add . && '
        f'git -c user.name=test -c user.email=test@test.com commit -m "init"'
    )
    return cwd


async def test_get_status_clean(git_repo):
    result = await get_status(git_repo)
    assert result["ok"] is True
    assert result["branch"] == "main"
    assert result["modified"] == []
    assert result["staged"] == []


async def test_get_status_with_changed_file(git_repo):
    # Add a NEW file to avoid CRLF/autocrlf noise from editing tracked content.
    with open(os.path.join(git_repo, "new_file.py"), "w") as f:
        f.write("print('hello')\n")
    result = await get_status(git_repo)
    assert result["ok"] is True
    all_changes = result["modified"] + result["staged"]
    assert len(all_changes) > 0, f"expected changes, got {result}"


async def test_commit_all_success(git_repo):
    with open(os.path.join(git_repo, "new.py"), "w") as f:
        f.write("print('hello')")
    result = await commit_all("add new.py", git_repo)
    assert result["ok"] is True
    assert "new.py" in result["files"]
    assert result["message"] == "add new.py"
    assert len(result["commit"]) > 0

    status = await get_status(git_repo)
    assert status["modified"] == []


async def test_commit_all_nothing_to_commit(git_repo):
    result = await commit_all("empty", git_repo)
    assert result["ok"] is False
    assert "nothing to commit" in result["error"]
    assert result["step"] == "diff"


async def test_get_status_not_a_repo(tmp_path):
    result = await get_status(str(tmp_path))
    assert result["ok"] is False


async def test_run_timeout_returns_structured_error(tmp_path):
    rc, stdout, stderr = await _run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        str(tmp_path),
        timeout=0.1,
    )
    assert rc == 1
    assert stdout == ""
    assert "timeout after" in stderr


async def test_commit_all_commit_failure_reports_step(monkeypatch, git_repo):
    async def fake_run(cmd, cwd, timeout=git_ops.TIMEOUT_MEDIUM):
        if cmd[:3] == ["git", "add", "-A"]:
            return 0, "", ""
        if cmd[:4] == ["git", "diff", "--cached", "--name-only"]:
            return 0, "new.py", ""
        if cmd[:2] == ["git", "commit"]:
            return 1, "", "timeout after 60s: git commit -m add new.py"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(git_ops, "_run", fake_run)

    result = await commit_all("add new.py", git_repo)
    assert result["ok"] is False
    assert result["step"] == "commit"
    assert "timeout after 60s" in result["error"]


async def test_get_job_status_unknown_job():
    result = get_job_status("missing")
    assert result["ok"] is False
    assert "unknown job_id" in result["error"]


async def test_submit_commit_job_succeeds(monkeypatch, git_repo):
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_commit_all(message, cwd):
        started.set()
        await release.wait()
        return {
            "ok": True,
            "commit": "abc123",
            "branch": "main",
            "files": ["new.py"],
            "message": message,
        }

    monkeypatch.setattr(git_ops, "commit_all", fake_commit_all)

    job_id = submit_commit_job("add new.py", git_repo)
    first = get_job_status(job_id)
    assert first["ok"] is True
    assert first["job_id"] == job_id
    assert first["status"] in {JOB_QUEUED, JOB_RUNNING}
    assert first["result"] is None

    await started.wait()
    mid = get_job_status(job_id)
    assert mid["status"] == JOB_RUNNING
    assert mid["result"] is None

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    done = get_job_status(job_id)
    assert done["status"] == JOB_SUCCEEDED
    assert done["result"]["ok"] is True
    assert done["result"]["commit"] == "abc123"


async def test_submit_commit_job_failure(monkeypatch, git_repo):
    async def fake_commit_all(message, cwd):
        await asyncio.sleep(0)
        return {"ok": False, "error": "boom", "step": "commit"}

    monkeypatch.setattr(git_ops, "commit_all", fake_commit_all)

    job_id = submit_commit_job("add new.py", git_repo)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    result = get_job_status(job_id)
    assert result["ok"] is True
    assert result["status"] == JOB_FAILED
    assert result["result"]["ok"] is False
    assert result["result"]["step"] == "commit"
