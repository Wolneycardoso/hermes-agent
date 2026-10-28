"""Natural-mission graph propagation checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_graph as graph


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_local_only_completion_contract_propagates_to_decomposed_children(kanban_home):
    with kbc.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="natural parent",
            triage=True,
            completion_contract="local-only",
        )
        child_ids = graph.decompose_triage_task(
            conn,
            root_id,
            root_assignee=None,
            children=[{"title": "local child"}],
            author="decomposer",
        )
        assert child_ids
        child = kb.get_task(conn, child_ids[0])

    assert child is not None
    assert child.completion_contract == "local-only"
