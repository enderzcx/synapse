"""Git ops tests using a temp git repo; they do not touch the real project."""

import os
import sys

import pytest

from warroom.channel import git_ops
from warroom.channel.git_ops import _run, commit_all, get_status


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
