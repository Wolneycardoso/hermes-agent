"""Experimental local-only adapter; no transport, credentials or runtime startup.

Requires the real Hermes Kanban module/connection supplied by the owner and an
idempotent queue port. Production admission/auth remains deliberately unwired.
"""

import hashlib, json, time, uuid, sqlite3, math


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AdmissionRejected(Exception):
    """Queue confirms rejection with no job written; not a transport timeout."""


class Bridge:
    def __init__(self, kanban, conn, queue, verifier, clock=time.time):
        kanban._assert_not_delegated_child_mutation()
        sql_clock = conn.execute("SELECT unixepoch('subsec')").fetchone()[0]
        if not isinstance(sql_clock, (int, float)) or not math.isfinite(sql_clock):
            raise ValueError("SQLITE_CLOCK_UNAVAILABLE")
        self.kb, self.db, self.queue, self.verifier, self.clock = (
            kanban,
            conn,
            queue,
            verifier,
            clock,
        )
        conn.execute("""CREATE TABLE IF NOT EXISTS synthetic_bridge(
          task TEXT NOT NULL, run INTEGER NOT NULL, request_id TEXT PRIMARY KEY,
          identity TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
          job TEXT, correction TEXT, fingerprint TEXT, cause TEXT, next_action TEXT,
          version INTEGER NOT NULL DEFAULT 0, terminal_result TEXT,
          UNIQUE(task,run))""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS synthetic_bridge_deadline
          BEFORE UPDATE OF status ON tasks WHEN NEW.status='done' AND EXISTS(
           SELECT 1 FROM synthetic_bridge b WHERE b.task=NEW.id AND b.run=OLD.current_run_id
           AND b.state='completing' AND (unixepoch('subsec') IS NULL
           OR json_extract(b.payload,'$.deadline')<=unixepoch('subsec')))
          BEGIN SELECT RAISE(ABORT,'BRIDGE_DEADLINE_EXHAUSTED'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS synthetic_bridge_terminal_state
          BEFORE UPDATE OF status ON tasks WHEN NEW.status='done' AND EXISTS(
           SELECT 1 FROM synthetic_bridge b WHERE b.task=NEW.id AND b.run=OLD.current_run_id
           AND b.state!='completing')
          BEGIN SELECT RAISE(ABORT,'BRIDGE_TERMINAL_STATE_CONFLICT'); END""")

    def row(self, request_id):
        row = self.db.execute(
            "SELECT * FROM synthetic_bridge WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            raise ValueError("UNKNOWN_MISSION")
        return dict(row)

    def reconcile(self, request_id):
        row = self.row(request_id)
        if row["state"] not in ("completing", "blocking", "completed", "blocked"):
            return row
        task = self.db.execute(
            "SELECT status,result,current_run_id FROM tasks WHERE id=?", (row["task"],)
        ).fetchone()
        run = self.db.execute(
            "SELECT task_id,status,metadata,summary FROM task_runs WHERE id=?",
            (row["run"],),
        ).fetchone()
        valid = False
        state = row["state"]
        if task and run and run["task_id"] == row["task"]:
            if state in ("completing", "completed"):
                valid = (
                    task["status"] == "done"
                    and run["status"] == "done"
                    and json.loads(run["metadata"] or "{}").get("local_artifact_sha256")
                    == row["fingerprint"]
                    and task["result"]
                    == "Verified local artifact SHA256 " + row["fingerprint"]
                )
                state = "completed"
            else:
                valid = (
                    task["status"] == "blocked"
                    and run["status"] == "blocked"
                    and run["summary"] == row["cause"] + "; next: " + row["next_action"]
                )
                state = "blocked"
        if not valid:
            if (
                task
                and task["status"] == "running"
                and task["current_run_id"] == row["run"]
            ):
                if row["state"] == "blocking":
                    return self.block(
                        request_id,
                        row["cause"],
                        row["next_action"],
                        expected_version=row["version"],
                    )
                if row["state"] == "completing":
                    return self.finish(row)
            raise ValueError("TERMINAL_READBACK_REQUIRED")
        with self.kb.write_txn(self.db):
            self.db.execute(
                "UPDATE synthetic_bridge SET state=? WHERE request_id=?",
                (state, request_id),
            )
        return self.row(request_id)

    def owner(self, task, run):
        row = self.db.execute(
            "SELECT current_run_id,status,completion_contract,max_runtime_seconds FROM tasks WHERE id=?",
            (task,),
        ).fetchone()
        if not row or row["current_run_id"] != run or row["status"] != "running":
            raise ValueError("STALE_RUN")
        if row["completion_contract"] != "local-only":
            raise ValueError("LOCAL_ONLY_REQUIRED")
        return row

    def admit(self, task, run, revision, criteria, deadline, request):
        if (
            not isinstance(revision, str)
            or len(revision) != 40
            or any(c not in "0123456789abcdef" for c in revision)
        ):
            raise ValueError("REVISION_REQUIRED")
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError("DEADLINE_INVALID")
        if (
            not criteria
            or len(set(criteria)) != len(criteria)
            or any(not isinstance(c, str) or not c.strip() for c in criteria)
        ):
            raise ValueError("CRITERIA_REQUIRED")
        identity = {
            "task": task,
            "run": run,
            "revision": revision,
            "criteria": criteria,
            "deadline": deadline,
            "request": request,
        }
        request_id = "kanban:" + digest({"task": task, "run": run})
        with self.kb.write_txn(self.db):
            owner = self.owner(task, run)
            started = self.db.execute(
                "SELECT started_at FROM task_runs WHERE id=?", (run,)
            ).fetchone()[0]
            limit = owner["max_runtime_seconds"] or 300
            if not 1 <= limit <= 300 or not self.clock() < deadline <= started + limit:
                raise ValueError("DEADLINE_INVALID")
            prior = self.db.execute(
                "SELECT identity FROM synthetic_bridge WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if prior and prior[0] != digest(identity):
                raise ValueError("MISSION_IDENTITY_CONFLICT")
            identity["requestId"] = request_id
            if not prior:
                payload = {
                    **identity,
                    "startedAt": started,
                    "delegationId": str(uuid.uuid4()),
                }
                self.db.execute(
                    "INSERT INTO synthetic_bridge(task,run,request_id,identity,payload,state) VALUES(?,?,?,?,?,?)",
                    (
                        task,
                        run,
                        request_id,
                        digest({k: v for k, v in identity.items() if k != "requestId"}),
                        json.dumps(payload),
                        "prepared",
                    ),
                )
        return self.dispatch(request_id)

    def dispatch(self, request_id):
        row = self.row(request_id)
        payload = json.loads(row["payload"])
        if row["state"] in ("blocking", "completing", "blocked", "completed"):
            return self.reconcile(request_id)
        self.owner(row["task"], row["run"])
        if self.clock() >= payload["deadline"]:
            return self.block(
                request_id,
                "deadline_exhausted",
                "Review evidence; a new run requires explicit admission.",
            )
        if row["state"] in ("blocked", "completed"):
            return row
        # Readback before any send. An uncertain send is never replayed blindly.
        receipt = self.queue.read(request_id)
        if receipt is None:
            if row["state"] == "uncertain":
                raise ValueError("READBACK_REQUIRED")
            if self.clock() >= payload["deadline"]:
                return self.block(
                    request_id,
                    "deadline_exhausted",
                    "No budget remains after queue readback.",
                    row["version"],
                )
            with self.kb.write_txn(self.db):
                self.owner(row["task"], row["run"])
                if self.clock() >= payload["deadline"]:
                    raise ValueError("DEADLINE_EXHAUSTED_BEFORE_SEND")
                changed = self.db.execute(
                    "UPDATE synthetic_bridge SET state='uncertain' WHERE request_id=? AND state='prepared'",
                    (request_id,),
                ).rowcount
            if not changed:
                raise ValueError("READBACK_REQUIRED")
            if self.clock() >= payload["deadline"]:
                return self.block(
                    request_id,
                    "deadline_exhausted",
                    "No budget remains before queue send.",
                    row["version"],
                )
            try:
                receipt = self.queue.admit(request_id, payload)
            except AdmissionRejected:
                return self.block(
                    request_id,
                    "admission_rejected",
                    "Review the bounded request; no job was admitted.",
                    row["version"],
                )
        if (
            receipt.get("requestId") != request_id
            or receipt.get("payloadHash") != digest(payload)
            or not isinstance(receipt.get("jobId"), str)
            or not receipt["jobId"]
            or receipt.get("deadline") != payload["deadline"]
        ):
            raise ValueError("QUEUE_RECEIPT_MISMATCH")
        if row["job"] is not None and row["job"] != receipt["jobId"]:
            raise ValueError("JOB_IDENTITY_CONFLICT")
        with self.kb.write_txn(self.db):
            self.owner(row["task"], row["run"])
            changed = self.db.execute(
                "UPDATE synthetic_bridge SET job=?,state='delegated' WHERE request_id=? AND state IN ('prepared','uncertain','delegated') AND (job IS NULL OR job=?)",
                (receipt["jobId"], request_id, receipt["jobId"]),
            ).rowcount
            if not changed:
                raise ValueError("QUEUE_STATE_CONFLICT")
        return self.row(request_id)

    def envelope(self, request_id):
        row = self.row(request_id)
        payload = json.loads(row["payload"])
        return {
            k: payload[k]
            for k in ("task", "run", "revision", "delegationId", "deadline")
        } | {
            "requestId": request_id,
            "jobId": row["job"],
            "correctionId": row["correction"],
        }

    def block(self, request_id, cause, next_action, expected_version=None):
        if not cause.strip() or not next_action.strip():
            raise ValueError("BLOCK_DIAGNOSIS_REQUIRED")
        row = self.row(request_id)
        if expected_version is not None and row["version"] != expected_version:
            raise ValueError("CONCURRENT_RETURN")
        if row["state"] in ("blocking", "blocked"):
            if row["cause"] != cause or row["next_action"] != next_action:
                raise ValueError("BLOCK_IDENTITY_CONFLICT")
            task = self.db.execute(
                "SELECT status FROM tasks WHERE id=?", (row["task"],)
            ).fetchone()
            if task["status"] != "running":
                return self.reconcile(request_id)
            intent_version = row["version"]
        else:
            # Immutable intent; a competing diagnostic must not overwrite it.
            with self.kb.write_txn(self.db):
                self.owner(row["task"], row["run"])
                changed = self.db.execute(
                    "UPDATE synthetic_bridge SET state='blocking',cause=?,next_action=?,version=version+1 WHERE request_id=? AND version=? AND state NOT IN ('blocking','blocked','completed')",
                    (cause, next_action, request_id, row["version"]),
                ).rowcount
                if not changed:
                    raise ValueError("CONCURRENT_RETURN")
            intent_version = row["version"] + 1
        if not self.kb.block_task(
            self.db,
            row["task"],
            reason=cause + "; next: " + next_action,
            kind="needs_input",
            expected_run_id=row["run"],
        ):
            return self.reconcile(request_id)
        with self.kb.write_txn(self.db):
            changed = self.db.execute(
                "UPDATE synthetic_bridge SET state='blocked' WHERE request_id=? AND version=? AND state='blocking'",
                (request_id, intent_version),
            ).rowcount
        if not changed:
            return self.reconcile(request_id)
        return self.row(request_id)

    def receive(self, result):
        rid = result["requestId"]
        row = self.row(rid)
        expected = self.envelope(rid)
        if any(result.get(k) != v for k, v in expected.items()):
            raise ValueError("RETURN_IDENTITY_MISMATCH")
        if row["state"] in ("blocking", "completing", "blocked", "completed"):
            return self.reconcile(rid)
        self.owner(row["task"], row["run"])
        payload = json.loads(row["payload"])
        if row["state"] not in ("delegated", "correction"):
            raise ValueError("RETURN_STATE_INVALID")
        if self.clock() >= payload["deadline"]:
            return self.block(
                rid,
                "deadline_exhausted",
                "Review saved evidence; do not renew this deadline.",
                row["version"],
            )
        # Verify actual local artifact; reported booleans are not acceptance.
        verified = self.verifier(result, payload["criteria"])
        if self.clock() >= payload["deadline"]:
            return self.block(
                rid,
                "deadline_exhausted",
                "Verification finished after the original deadline.",
                row["version"],
            )
        fingerprint = verified["fingerprint"]
        failed = verified["failed"]
        self.validate_evidence(verified, payload["criteria"])
        if row["state"] == "correction" and fingerprint == row["fingerprint"]:
            return self.block(
                rid,
                "correction_without_progress",
                "Change the failed criteria or request review; do not replay unchanged output.",
                row["version"],
            )
        if failed:
            if row["state"] == "correction":
                return self.block(
                    rid,
                    "acceptance_failed",
                    "Review unresolved criteria: " + ", ".join(failed),
                    row["version"],
                )
            correction = {
                "id": str(uuid.uuid4()),
                "criteria": failed,
                "revision": payload["revision"],
                "diagnostic": "Local artifact failed: " + ", ".join(failed),
            }
            with self.kb.write_txn(self.db):
                self.owner(row["task"], row["run"])
                changed = self.db.execute(
                    "UPDATE synthetic_bridge SET state='correction',correction=?,fingerprint=?,cause=?,next_action=?,version=version+1 WHERE request_id=? AND version=? AND state=? AND correction IS ?",
                    (
                        correction["id"],
                        fingerprint,
                        correction["diagnostic"],
                        "Return evidence for exactly the requested criteria.",
                        rid,
                        row["version"],
                        row["state"],
                        row["correction"],
                    ),
                ).rowcount
                if not changed:
                    raise ValueError("CONCURRENT_RETURN")
            return {
                "state": "correction",
                "correction": correction,
                "envelope": self.envelope(rid),
            }
        if self.clock() >= payload["deadline"]:
            return self.block(
                rid,
                "deadline_exhausted",
                "Verification finished after the original deadline.",
                row["version"],
            )
        with self.kb.write_txn(self.db):
            self.owner(row["task"], row["run"])
            changed = self.db.execute(
                "UPDATE synthetic_bridge SET state='completing',fingerprint=?,terminal_result=?,version=version+1 WHERE request_id=? AND version=? AND state=? AND correction IS ?",
                (
                    fingerprint,
                    json.dumps(result),
                    rid,
                    row["version"],
                    row["state"],
                    row["correction"],
                ),
            ).rowcount
            if not changed:
                raise ValueError("CONCURRENT_RETURN")
        return self.finish(self.row(rid))

    @staticmethod
    def validate_evidence(evidence, criteria):
        fp = evidence.get("fingerprint")
        failed = evidence.get("failed")
        if (
            not isinstance(fp, str)
            or len(fp) != 64
            or any(c not in "0123456789abcdef" for c in fp)
            or not isinstance(failed, list)
            or any(not isinstance(c, str) or c not in criteria for c in failed)
            or len(failed) != len(set(failed))
        ):
            raise ValueError("VERIFIER_CONTRACT_INVALID")

    def finish(self, row):
        rid = row["request_id"]
        payload = json.loads(row["payload"])
        fingerprint = row["fingerprint"]
        self.owner(row["task"], row["run"])
        evidence = self.verifier(
            json.loads(row["terminal_result"]), payload["criteria"]
        )
        self.validate_evidence(evidence, payload["criteria"])
        if evidence["fingerprint"] != fingerprint or evidence["failed"]:
            return self.block(
                rid,
                "artifact_changed",
                "Re-verify the local artifact before acceptance.",
                row["version"],
            )
        if self.clock() >= payload["deadline"]:
            return self.block(
                rid,
                "deadline_exhausted",
                "Deadline expired before Kanban acceptance.",
                row["version"],
            )
        try:
            accepted = self.kb.complete_task(
                self.db,
                row["task"],
                result="Verified local artifact SHA256 " + fingerprint,
                metadata={"local_artifact_sha256": fingerprint},
                expected_run_id=row["run"],
                fire_lifecycle_hook=False,
            )
        except sqlite3.IntegrityError as error:
            if str(error) != "BRIDGE_DEADLINE_EXHAUSTED":
                raise
            return self.block(
                rid,
                "deadline_exhausted",
                "Deadline expired at the atomic Kanban gate.",
                row["version"],
            )
        if not accepted:
            raise ValueError("KANBAN_ACCEPTANCE_REJECTED")
        with self.kb.write_txn(self.db):
            self.db.execute(
                "UPDATE synthetic_bridge SET state='completed' WHERE request_id=?",
                (rid,),
            )
        return self.row(rid)
