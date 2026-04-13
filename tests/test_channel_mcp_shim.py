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


async def test_channel_history_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "history"
            assert kwargs == {"room": "room1", "limit": 20, "since_id": 5}
            return {"ok": True, "messages": [{"id": 6, "content": "hello"}]}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_history(room="room1", limit=20, since_id=5)
    assert result["ok"] is True
    assert result["messages"][0]["id"] == 6


async def test_channel_state_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "room_state"
            assert kwargs == {"room": "room1"}
            return {"ok": True, "active_agents": [{"actor": "claude"}], "claims": [], "last_msg_id": 9}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_state(room="room1")
    assert result["ok"] is True
    assert result["active_agents"][0]["actor"] == "claude"
    assert result["last_msg_id"] == 9


async def test_channel_peek_inbox_forwards(monkeypatch):
    class FakeClient:
        def peek_new(self, room):
            assert room == "room1"
            return [{"id": 7, "content": "ping"}]

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_peek_inbox(room="room1")
    assert result == {
        "ok": True,
        "messages": [{"id": 7, "content": "ping"}],
        "count": 1,
    }


async def test_channel_send_control_forwards(monkeypatch):
    class FakeClient:
        async def send_control(self, **kwargs):
            assert kwargs == {
                "room": "room1",
                "target": "codex",
                "action": "interrupt",
                "task_id": "task-1",
                "data": {"reason": "user_override"},
            }
            return {"ok": True}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_send_control(
        target="codex",
        action="interrupt",
        room="room1",
        task_id="task-1",
        data={"reason": "user_override"},
    )
    assert result == {"ok": True}


async def test_channel_peek_control_forwards(monkeypatch):
    class FakeClient:
        def peek_control(self):
            return [{"op": "control", "action": "interrupt"}]

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_peek_control(room="room1")
    assert result == {
        "ok": True,
        "controls": [{"op": "control", "action": "interrupt"}],
        "count": 1,
    }


async def test_channel_task_create_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "task_create"
            assert kwargs == {
                "room": "room1",
                "title": "Implement task registry",
                "goal": "Reduce drift",
                "owner": "codex",
                "reviewer": "claude",
                "acceptance": ["tasks visible in room_state"],
                "write_set": ["warroom/channel/broker.py"],
            }
            return {"ok": True, "task": {"task_id": "t-001"}}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)
    monkeypatch.setattr(mcp_shim, "_actor", "codex")

    result = await mcp_shim.channel_task_create(
        title="Implement task registry",
        goal="Reduce drift",
        owner="codex",
        reviewer="claude",
        room="room1",
        acceptance=["tasks visible in room_state"],
        write_set=["warroom/channel/broker.py"],
    )
    assert result == {"ok": True, "task": {"task_id": "t-001"}}


async def test_channel_task_update_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "task_update"
            assert kwargs == {
                "room": "room1",
                "task_id": "t-001",
                "status": "doing",
                "owner": "claude",
            }
            return {"ok": True, "task": {"task_id": "t-001", "status": "doing"}}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_task_update(
        task_id="t-001",
        status="doing",
        owner="claude",
        room="room1",
    )
    assert result["ok"] is True
    assert result["task"]["status"] == "doing"


async def test_channel_task_get_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "task_get"
            assert kwargs == {"room": "room1", "task_id": "t-001"}
            return {"ok": True, "task": {"task_id": "t-001"}}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_task_get("t-001", room="room1")
    assert result == {"ok": True, "task": {"task_id": "t-001"}}


async def test_channel_task_list_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "task_list"
            assert kwargs == {"room": "room1", "status": "doing"}
            return {"ok": True, "tasks": [{"task_id": "t-001"}], "count": 1}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_task_list(room="room1", status="doing")
    assert result["ok"] is True
    assert result["count"] == 1

async def test_channel_task_handoff_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "task_handoff"
            assert kwargs == {
                "room": "room1",
                "task_id": "t-001",
                "artifacts": ["warroom/channel/broker.py"],
                "verified": ["32/32 tests pass"],
                "assumptions": [],
                "next_action": "review edge cases",
            }
            return {"ok": True, "task_id": "t-001", "status": "review"}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_task_handoff(
        task_id="t-001",
        artifacts=["warroom/channel/broker.py"],
        verified=["32/32 tests pass"],
        assumptions=[],
        next_action="review edge cases",
        room="room1",
    )
    assert result["ok"] is True
    assert result["status"] == "review"


async def test_channel_task_verdict_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "task_verdict"
            assert kwargs == {
                "room": "room1",
                "task_id": "t-001",
                "verdict": "fail",
                "findings": ["missing retry test"],
                "blocking": True,
            }
            return {
                "ok": True,
                "task_id": "t-001",
                "verdict": "fail",
                "new_status": "doing",
            }

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_task_verdict(
        task_id="t-001",
        verdict="fail",
        findings=["missing retry test"],
        blocking=True,
        room="room1",
    )
    assert result["ok"] is True
    assert result["new_status"] == "doing"

async def test_channel_set_status_forwards(monkeypatch):
    class FakeClient:
        async def _request(self, op, **kwargs):
            assert op == "agent_status"
            assert kwargs == {
                "room": "room1",
                "phase": "coding",
                "detail": "editing broker",
                "task_id": "t-001",
            }
            return {"ok": True, "actor": "codex", "phase": "coding"}

    async def fake_ensure_client():
        return FakeClient()

    monkeypatch.setattr(mcp_shim, "_ensure_client", fake_ensure_client)

    result = await mcp_shim.channel_set_status(
        phase="coding",
        task_id="t-001",
        detail="editing broker",
        room="room1",
    )
    assert result == {"ok": True, "actor": "codex", "phase": "coding"}
