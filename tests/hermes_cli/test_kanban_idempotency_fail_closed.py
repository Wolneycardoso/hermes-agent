from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    try:
        yield conn
    finally:
        conn.close()


def test_integrity_error_from_event_is_not_idempotency_fallback(db, monkeypatch):
    original = kb._append_event
    calls = []

    def fail_after_insert(*args, **kwargs):
        calls.append(args[1])
        original(*args, **kwargs)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: task_events.id")

    monkeypatch.setattr(kb, "_append_event", fail_after_insert)

    with pytest.raises(sqlite3.IntegrityError, match="task_events.id"):
        kb.create_task(db, title="must fail closed", idempotency_key="same")

    assert len(calls) == 1
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_task_id_collision_retries_but_other_integrity_errors_do_not(db, monkeypatch):
    existing = kb.create_task(db, title="existing", idempotency_key="existing-key")
    ids = iter([existing, "fresh-task-id"])
    monkeypatch.setattr(kb, "_new_task_id", lambda: next(ids))

    created = kb.create_task(db, title="retry collision", idempotency_key="new-key")

    assert created == "fresh-task-id"
    assert db.execute("SELECT title FROM tasks WHERE id = ?", (created,)).fetchone()[0] == "retry collision"


def test_id_collision_rolls_back_trigger_side_effects(db, monkeypatch):
    existing = kb.create_task(db, title="existing")
    db.execute("CREATE TABLE insert_audit (task_id TEXT)")
    db.execute("CREATE TRIGGER audit_task BEFORE INSERT ON tasks BEGIN INSERT INTO insert_audit VALUES (NEW.id); END")
    ids = iter([existing, "fresh-task-id"])
    monkeypatch.setattr(kb, "_new_task_id", lambda: next(ids))

    assert kb.create_task(db, title="retry") == "fresh-task-id"
    assert [row[0] for row in db.execute("SELECT task_id FROM insert_audit")] == ["fresh-task-id"]


def test_same_key_reuse_and_explicit_isolation_are_validated(db):
    task, created = kb.create_task(db, title="first", idempotency_key="key", return_created=True)
    assert created is True
    assert kb.create_task(db, title="again", idempotency_key="key") == task
    assert kb.create_task(db, title="again", idempotency_key="key", return_created=True) == (task, False)
    with pytest.raises(ValueError, match="idempotency collision"):
        kb.create_task(db, title="restricted", idempotency_key="key", isolation_policy="natural-local-only")


def test_concurrent_creates_reuse_one_row(db):
    barrier = threading.Barrier(2)

    def create():
        conn = kbc.connect()
        try:
            barrier.wait(timeout=10)
            return kb.create_task(conn, title="concurrent", idempotency_key="key", return_created=True)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert len({task_id for task_id, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, True]
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = 'key'").fetchone()[0] == 1


def test_nested_transaction_rolls_back_failed_create(db, monkeypatch):
    with kb.write_txn(db):
        existing = kb.create_task(db, title="prior", idempotency_key="prior")

        def fail(*args, **kwargs):
            raise sqlite3.IntegrityError("event failure")

        monkeypatch.setattr(kb, "_append_event", fail)
        with pytest.raises(sqlite3.IntegrityError, match="event failure"):
            kb.create_task(db, title="failed", idempotency_key="failed")
        assert kb.get_task(db, existing) is not None
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = 'failed'").fetchone()[0] == 0


def test_ignored_insert_without_collision_fails_closed(db):
    db.execute("CREATE TRIGGER ignore_task BEFORE INSERT ON tasks BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(sqlite3.IntegrityError, match="ignored without an id collision"):
        kb.create_task(db, title="ignored", idempotency_key="key")
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
