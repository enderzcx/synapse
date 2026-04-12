"""Git ops tests using a temp git repo — doesn't touch the real project."""
import os

import pytest

from warroom.channel.git_ops import commit_all, get_status


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one commit."""
    cwd = str(tmp_path)
    os.system(f'cd /d "{cwd}" && git init && git checkout -b main && '
              f'echo hello > readme.txt && git add . && '
              f'git -c user.name=test -c user.email=test@test.com commit -m "init"')
    return cwd


async def test_get_status_clean(git_repo):
    result = await get_status(git_repo)
    assert result["ok"] is True
    assert result["branch"] == "main"
    assert result["modified"] == []
    assert result["staged"] == []


async def test_get_status_with_changed_file(git_repo):
    # Add a NEW file (avoids Windows CRLF/autocrlf issues with modifying existing)
    with open(os.path.join(git_repo, "new_file.py"), "w") as f:
        f.write("print('hello')\n")
    result = await get_status(git_repo)
    assert result["ok"] is True
    # New untracked file should show up in modified (untracked = visible change)
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

    # After commit, status should be clean
    status = await get_status(git_repo)
    assert status["modified"] == []


async def test_commit_all_nothing_to_commit(git_repo):
    result = await commit_all("empty", git_repo)
    assert result["ok"] is False
    assert "nothing to commit" in result["error"]


async def test_get_status_not_a_repo(tmp_path):
    result = await get_status(str(tmp_path))
    assert result["ok"] is False
