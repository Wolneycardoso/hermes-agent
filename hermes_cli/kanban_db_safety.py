"""Safety and ancestry helpers for the kanban database facade.

The facade late-binds these helpers so its public database seams remain stable
while security-sensitive inheritance logic has a focused implementation home.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


class TaskIdCollision(Exception):
    """Abort the transaction before retrying an ignored task ID insertion."""


def validate_inserted_task(conn: sqlite3.Connection, cursor: sqlite3.Cursor, task_id: str) -> None:
    """Distinguish a real ID collision from another ignored insert."""
    if cursor.rowcount == 1:
        return
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise sqlite3.IntegrityError("task insert was ignored without an id collision")
    raise TaskIdCollision


def record_initial_state(conn, task_id, task_status, parents, created_by, append_event, get_status) -> None:
    """Record blocked and dependency-wait events in the creation transaction."""
    if task_status == "blocked":
        append_event(conn, task_id, "blocked", {
            "reason": "initial_status", "status": "blocked", "actor": created_by or "user",
        })
    if task_status == "todo":
        gating = [p for p in parents if get_status(conn, p) not in ("done", "archived")]
        if gating:
            append_event(conn, task_id, "dependency_wait", {"reason": "parent_not_done", "parent": gating[0]})


def find_idempotent_task(
    conn: sqlite3.Connection,
    key: Optional[str],
    contract: Optional[str],
    policy: Optional[str],
    inherited_contract: Optional[str],
    inherited_policy: Optional[str],
) -> Optional[str]:
    """Read and validate a reusable task while the caller holds its write transaction."""
    if not key:
        return None
    row = conn.execute(
        "SELECT id, completion_contract, isolation_policy FROM tasks WHERE idempotency_key = ? "
        "AND status != 'archived' ORDER BY created_at DESC LIMIT 1", (key,),
    ).fetchone()
    if row is None:
        return None
    if ((contract == "local-only" and row["completion_contract"] != "local-only")
            or (policy == "natural-local-only" and row["isolation_policy"] != "natural-local-only")
            or ((inherited_contract or inherited_policy)
                and row["completion_contract"] != "local-only"
                and row["isolation_policy"] != "natural-local-only")):
        raise ValueError("idempotency collision would bypass isolation")
    return row["id"]


def inherited_isolation(
    conn: sqlite3.Connection,
    parents: tuple[str, ...],
    creator_task_id: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return the strongest restriction carried by dependency/creator ancestry."""
    sources = list(parents)
    if creator_task_id:
        sources.append(creator_task_id)
    if not sources:
        return None, None
    placeholders = ",".join("?" * len(sources))
    row = conn.execute(
        "WITH RECURSIVE ancestors(id) AS ("
        "SELECT id FROM tasks WHERE id IN (" + placeholders + ") "
        "UNION SELECT l.parent_id FROM task_links l JOIN ancestors a ON l.child_id = a.id) "
        "SELECT MAX(CASE WHEN t.completion_contract = 'local-only' THEN 1 ELSE 0 END) AS local_contract, "
        "MAX(CASE WHEN t.isolation_policy = 'natural-local-only' THEN 1 ELSE 0 END) AS local_policy "
        "FROM tasks t JOIN ancestors a ON a.id = t.id",
        sources,
    ).fetchone()
    return (
        "local-only" if row and row["local_contract"] else None,
        "natural-local-only" if row and row["local_policy"] else None,
    )


def descendants_inclusive(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        """WITH RECURSIVE descendants(id) AS (
             SELECT ?
             UNION
             SELECT l.child_id FROM task_links l JOIN descendants d ON l.parent_id = d.id
           ) SELECT id FROM descendants""",
        (task_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def claim_is_restricted(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        "SELECT completion_contract, isolation_policy FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return bool(row and (
        row["completion_contract"] == "local-only"
        or row["isolation_policy"] == "natural-local-only"
    ))


def propagate_link_restriction(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    contract, policy = inherited_isolation(conn, (parent_id,), None)
    if not contract and not policy:
        return False
    descendants = descendants_inclusive(conn, child_id)
    placeholders = ",".join("?" * len(descendants))
    running = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND status = 'running' LIMIT 1",
        descendants,
    ).fetchone()
    if running:
        raise ValueError(f"cannot link {parent_id} -> {child_id}: descendant {running['id']} is already running")
    assignments = []
    if contract:
        assignments.append("completion_contract = CASE WHEN completion_contract IS NULL OR completion_contract = 'local-only' THEN 'local-only' ELSE completion_contract END")
    assignments.append("isolation_policy = 'natural-local-only'")
    conn.execute(
        "UPDATE tasks SET " + ", ".join(assignments) + f" WHERE id IN ({placeholders})",
        descendants,
    )
    return True


def reject_restricted_claim(conn: sqlite3.Connection, task_id: str, append_event) -> bool:
    if not claim_is_restricted(conn, task_id):
        return False
    append_event(conn, task_id, "claim_rejected", {"reason": "local_only_completion_contract"})
    return True
