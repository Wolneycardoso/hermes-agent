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


def test_claim_task_refuses_local_only_before_opening_run(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="natural task",
            completion_contract="local-only",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert claimed is None
    assert task is not None
    assert task.status == "ready"
    assert task.current_run_id is None
    assert not any(event.kind == "claimed" for event in events)


def test_claim_review_task_refuses_local_only_before_opening_run(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="natural review",
            completion_contract="local-only",
        )
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        claimed = kb.claim_review_task(conn, task_id, claimer="reviewer")
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert claimed is None
    assert task is not None
    assert task.status == "review"
    assert task.current_run_id is None
    assert not any(event.kind == "claimed" for event in events)


@pytest.mark.parametrize("contract,policy", [("local-only", None), (None, "natural-local-only")])
def test_default_spawn_rejects_restricted_task_before_host_setup(kanban_home, monkeypatch, contract, policy):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import profiles

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="restricted", assignee="default",
                                 completion_contract=contract, isolation_policy=policy)
        task = kb.get_task(conn, task_id)
        assert task is not None

    def unexpected_setup(*args, **kwargs):
        raise AssertionError("host setup reached before isolation rejection")

    monkeypatch.setattr(profiles, "normalize_profile_name", unexpected_setup)
    with pytest.raises(RuntimeError, match="isolat"):
        dispatch._default_spawn(task, str(kanban_home))


@pytest.mark.parametrize("status", ["ready", "review"])
def test_spawnable_telemetry_ignores_restricted_tasks(kanban_home, monkeypatch, status):
    from hermes_cli import kanban_db_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="restricted", assignee="default", completion_contract="local-only")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        assert not dispatch._has_spawnable(conn, status)


def test_create_task_inherits_isolation_from_parent_and_creator(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="restricted parent", completion_contract="local-only")
        child = kb.create_task(conn, title="parent child", parents=[parent], completion_contract="owner/repo")
        creator = kb.create_task(conn, title="restricted creator", isolation_policy="natural-local-only")
        spawned = kb.create_task(conn, title="creator child", creator_task_id=creator)
        assert kb.get_task(conn, child).completion_contract == "owner/repo"
        assert kb.get_task(conn, child).isolation_policy == "natural-local-only"
        assert kb.get_task(conn, spawned).isolation_policy == "natural-local-only"


def test_link_tasks_propagates_restriction_transitively_and_demotes_ready_child(kanban_home):
    with kbc.connect() as conn:
        restricted = kb.create_task(conn, title="restricted", completion_contract="local-only")
        child = kb.create_task(conn, title="already linked")
        grandchild = kb.create_task(conn, title="grandchild", parents=[child])
        conn.execute("UPDATE tasks SET status='ready' WHERE id IN (?, ?)", (child, grandchild))
        assert kb.link_tasks(conn, restricted, child) is True
        assert kb.get_task(conn, child).completion_contract == "local-only"
        assert kb.get_task(conn, grandchild).completion_contract == "local-only"
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child, claimer="dispatcher") is None


def test_link_local_only_parent_preserves_pr_contract_and_restricts_descendants(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="restricted", completion_contract="local-only")
        child = kb.create_task(conn, title="PR child", completion_contract="owner/repo")
        grandchild = kb.create_task(conn, title="PR grandchild", parents=[child], completion_contract="owner/repo")
        kb.link_tasks(conn, parent, child)
        for task_id in (child, grandchild):
            task = kb.get_task(conn, task_id)
            assert task is not None
            assert task.completion_contract == "owner/repo"
            assert task.isolation_policy == "natural-local-only"
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            assert kb.claim_task(conn, task_id, claimer="dispatcher") is None


def test_link_restricted_parent_rejects_running_grandchild_atomically(kanban_home):
    with kbc.connect() as conn:
        restricted = kb.create_task(conn, title="restricted", isolation_policy="natural-local-only")
        child = kb.create_task(conn, title="child")
        grandchild = kb.create_task(conn, title="grandchild", parents=[child])
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (grandchild,))
        with pytest.raises(ValueError, match="descendant"):
            kb.link_tasks(conn, restricted, child)
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE parent_id=? AND child_id=?", (restricted, child)).fetchone()[0] == 0
        assert kb.get_task(conn, child).isolation_policy is None


def test_idempotency_collision_does_not_bypass_inherited_isolation(kanban_home):
    with kbc.connect() as conn:
        existing = kb.create_task(conn, title="existing", idempotency_key="same-key")
        parent = kb.create_task(conn, title="restricted", isolation_policy="natural-local-only")
        with pytest.raises(ValueError, match="idempotency collision"):
            kb.create_task(conn, title="collision", idempotency_key="same-key", parents=[parent])
        assert kb.get_task(conn, existing).title == "existing"


@pytest.mark.parametrize("contract,policy", [("local-only", None), (None, "natural-local-only"),
                                           ("local-only", "natural-local-only")])
def test_idempotency_collision_rejects_explicit_restriction(kanban_home, contract, policy):
    with kbc.connect() as conn:
        existing = kb.create_task(conn, title="ordinary", idempotency_key="collision")
        with pytest.raises(ValueError, match="idempotency collision"):
            kb.create_task(conn, title="restricted", idempotency_key="collision",
                           completion_contract=contract, isolation_policy=policy)
        task = kb.get_task(conn, existing)
        assert task is not None and task.completion_contract is None and task.isolation_policy is None


def test_linking_restricted_parent_to_running_child_fails_closed(kanban_home):
    with kbc.connect() as conn:
        restricted = kb.create_task(conn, title="restricted", isolation_policy="natural-local-only")
        child = kb.create_task(conn, title="running")
        conn.execute("UPDATE tasks SET status='running', current_run_id=7 WHERE id=?", (child,))
        with pytest.raises(ValueError, match="running"):
            kb.link_tasks(conn, restricted, child, expected_child_run_id=7)
