"""Tests for the local-only Kanban execution gate."""

from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import pytest


def test_local_only_blocks_external_tool_calls(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from model_tools import handle_function_call

    result = json.loads(handle_function_call("manage_connections", {}))

    assert result["error"]
    assert "local-only" in result["error"]


def test_local_only_blocks_inline_executor_boundary(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from agent.tool_executor import _ManagedToolResult, _ToolCallRef, _dispatch_authorized_once

    class _Agent:
        session_id = "session"

        def _guardrail_block_result(self, _decision):
            return "guardrail should not be reached"

    ref = _ToolCallRef("delegate_task", {}, "t_local", "call-1", [])
    state = _ManagedToolResult(result=None, args={}, middleware_trace=[], blocked=False, dispatched=False)
    executed = []
    result = _dispatch_authorized_once(
        _Agent(), state, ref, execute=lambda _args: executed.append(True), scope_block=None,
        display_index=1, begin_execution=None, authorization_gate=None,
    )

    assert "local-only" in result
    assert state.blocked is True
    assert executed == []


def test_local_only_filters_tool_schemas(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from model_tools import get_tool_definitions

    names = {item["function"]["name"] for item in get_tool_definitions(quiet_mode=True)}

    assert "write_file" in names
    assert "terminal" not in names
    assert "manage_connections" not in names
    assert "execute_code" not in names


def test_local_only_terminal_guard_fails_closed_without_proven_isolation(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from tools.terminal_tool import terminal_tool

    result = json.loads(terminal_tool("printf safe", _host_local=True))

    assert result["status"] == "blocked"
    assert "isolation" in result["error"]


def test_local_only_terminal_guard_blocks_publication_commands(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from tools.terminal_tool import terminal_tool

    result = json.loads(terminal_tool("git push origin main", _host_local=True))

    assert result["status"] == "blocked"
    assert "isolation" in result["error"]

    restart = json.loads(terminal_tool("hermes gateway restart", _host_local=True))
    assert restart["status"] == "blocked"

    for command in (
        "python -c 'import socket; socket.create_connection((\"example.com\", 443))'",
        "python -m http.server 8000",
        "echo ok && git push origin main",
        "pytest -q",
        "python -m unittest discover",
        "python worker.py",
        "npm test",
    ):
        blocked = json.loads(terminal_tool(command, _host_local=True))
        assert blocked["status"] == "blocked"


def test_local_only_gate_precedes_gateway_lifecycle_guard(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    import tools.terminal_tool as tt

    def fail_if_called(**_kwargs):
        raise AssertionError("gateway lifecycle guard must not run first")

    monkeypatch.setattr(tt, "gateway_lifecycle_block", fail_if_called)
    with pytest.raises(tt._Rejected) as exc_info:
        tt._pre_exec_block(
            "pytest -q", env=None, env_type="local", cwd="", workdir=None, session_key="",
        )

    assert "local-only" in exc_info.value.result_json


def test_local_only_gate_precedes_terminal_approval_preparation(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    import tools.terminal_tool as tt
    from agent.terminal_approval_batch import _TerminalSlot

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("generic approval guards must not run first")

    monkeypatch.setattr(tt, "_check_all_guards", fail_if_called)
    batch = SimpleNamespace(
        cancelled=Event(),
        agent=SimpleNamespace(_interrupt_requested=False, session_id="session"),
    )
    slot = _TerminalSlot(batch, None, 0)
    slot.release.set()
    ref = SimpleNamespace(args={"command": "pytest -q"}, call_id="call-1")

    slot.prepare(ref)

    decision = slot.decision
    assert decision is not None
    assert decision["status"] == "blocked"
    assert "local-only" in decision["message"]


def test_local_only_policy_survives_delegated_child_scrub(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from agent.delegation_context import scrub_kanban_env

    cleaned = scrub_kanban_env({
        "HERMES_KANBAN_TASK": "t_parent",
        "HERMES_KANBAN_EFFECTS": "blocked",
        "PATH": "/usr/bin",
    })

    assert "HERMES_KANBAN_TASK" not in cleaned
    assert cleaned["HERMES_KANBAN_EFFECTS"] == "blocked"