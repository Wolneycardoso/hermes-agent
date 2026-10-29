"""Small, testable helpers for Kanban dispatch reporting and admission."""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import DispatchResult


def describe_suppression(results: Iterable[Optional["DispatchResult"]]) -> str:
    """Summarize why dispatcher ticks held ready work back."""
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)


def skip_restricted_dispatch(conn: sqlite3.Connection, row: sqlite3.Row, result, dry_run: bool, kb) -> bool:
    """Refuse restricted tasks before admission and record a deduplicated reason."""
    if row["completion_contract"] != "local-only" and row["isolation_policy"] != "natural-local-only":
        return False
    task_id = row["id"]
    result.skipped_local_only.append(task_id)
    if not dry_run:
        with kb.write_txn(conn):
            last = conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,),
            ).fetchone()
            payload = kb._json_or_null({"reason": "isolated_worker_backend_unavailable"})
            if last is None or last["kind"] != "skipped_local_only" or last["payload"] != payload:
                kb._append_event(conn, task_id, "skipped_local_only", {"reason": "isolated_worker_backend_unavailable"})
    return True
