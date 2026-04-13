"""Tests for the MCP shim wrappers around git job tools."""

from warroom.channel import mcp_shim


async def test_git_commit_returns_job_id(monkeypatch):
    from warroom.channel import git_ops

    def fake_submit_commit_job(message, cwd, on_complete=None):
        assert message == "test commit"
        assert cwd == "E:/repo"
        assert on_complete is not None
        return "job123"

    monkeypatch.setattr(git_ops, "submit_commit_job", fake_submit_commit_job)
    monkeypatch.setattr(mcp_shim, "_repo_root", "E:/repo")

    result = await mcp_shim.git_commit("test commit")
    assert result == {"ok": True, "job_id": "job123", "status": "queued"}


async def test_git_job_status_forwards(monkeypatch):
    from warroom.channel import git_ops

    def fake_get_job_status(job_id):
        assert job_id == "job123"
        return {
            "ok": True,
            "job_id": "job123",
            "status": "succeeded",
            "result": {"ok": True, "commit": "abc123"},
        }

    monkeypatch.setattr(git_ops, "get_job_status", fake_get_job_status)

    result = await mcp_shim.git_job_status("job123")
    assert result["ok"] is True
    assert result["status"] == "succeeded"
    assert result["result"]["commit"] == "abc123"
