"""Tests for the local-only Kanban execution gate."""

from __future__ import annotations

import json


def test_local_only_blocks_external_tool_calls(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from model_tools import handle_function_call

    result = json.loads(handle_function_call("manage_connections", {}))

    assert result["error"]
    assert "local-only" in result["error"]


def test_local_only_filters_tool_schemas(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from model_tools import get_tool_definitions

    names = {item["function"]["name"] for item in get_tool_definitions(quiet_mode=True)}

    assert "write_file" in names
    assert "terminal" in names
    assert "manage_connections" not in names
    assert "execute_code" not in names


def test_local_only_terminal_guard_blocks_publication_commands(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_EFFECTS", "blocked")

    from tools.terminal_tool import terminal_tool

    result = json.loads(terminal_tool("git push origin main", _host_local=True))

    assert result["status"] == "blocked"
    assert "external-effect" in result["error"]

    restart = json.loads(terminal_tool("hermes gateway restart", _host_local=True))
    assert restart["status"] == "blocked"


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