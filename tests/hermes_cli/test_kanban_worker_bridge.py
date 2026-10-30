"""Bridge contracts on real Kanban SQLite; remote queue is an explicit double."""

import json, sqlite3, tempfile, pathlib, time, unittest, hashlib, uuid
from unittest.mock import patch
from hermes_cli.kanban_worker_bridge import Bridge, digest, AdmissionRejected
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import _migrate_add_optional_columns


class Queue:
    """Remote admission/queue double; no WO-OS code or network runs here."""

    def __init__(self, conn):
        self.db = conn
        self.calls = 0
        self.lose_ack = False
        conn.execute(
            "CREATE TABLE fixture_queue(request TEXT PRIMARY KEY,payload TEXT,job TEXT)"
        )

    def read(self, rid):
        row = self.db.execute(
            "SELECT * FROM fixture_queue WHERE request=?", (rid,)
        ).fetchone()
        return (
            None
            if not row
            else {
                "requestId": rid,
                "payloadHash": digest(json.loads(row["payload"])),
                "jobId": row["job"],
                "deadline": json.loads(row["payload"])["deadline"],
            }
        )

    def admit(self, rid, payload):
        self.calls += 1
        if getattr(self, "reject", False):
            raise AdmissionRejected("ADMISSION_REJECTED")
        self.db.execute(
            "INSERT OR IGNORE INTO fixture_queue VALUES(?,?,?)",
            (rid, json.dumps(payload), str(uuid.uuid4())),
        )
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("synthetic lost acknowledgment")
        return self.read(rid)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kanban-synthetic-")
        self.root = pathlib.Path(self.tmp.name)
        for name, replacement in [
            ("_fire_task_hook", lambda *a, **kw: None),
            ("_fire_kanban_lifecycle_hook", lambda *a, **kw: None),
            ("get_current_board", lambda: "synthetic"),
            ("kanban_home", lambda: self.root),
        ]:
            patcher = patch.object(kb, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = sqlite3.connect(
            self.root / "kanban-synthetic.sqlite", isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript(kb.SCHEMA_SQL)
        _migrate_add_optional_columns(self.db)
        self.task = kb.create_task(
            self.db,
            title="synthetic",
            workspace_kind="scratch",
            project_id="",
            completion_contract="local-only",
            max_runtime_seconds=300,
        )
        kb.claim_task(self.db, self.task, ttl_seconds=300, claimer="synthetic")
        self.run = self.db.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (self.task,)
        ).fetchone()[0]
        self.now = time.time()
        self.clock = lambda: self.now
        self.deadline = self.now + 60
        self.queue = Queue(self.db)
        self.bridge = Bridge(kb, self.db, self.queue, self.verify, self.clock)
        self.request = dict(
            issueId="WO-100",
            title="synthetic",
            description="Local-only deterministic test",
            repository="wo-os/wo-os",
            risk="low",
            runBudgetSeconds=300,
            maxAttempts=1,
            allowedPaths=["fixture.txt"],
        )
        self.criteria = ["greeting", "quantity"]
        self.file = self.root / "fixture.txt"
        self.file.write_text("hello 42")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def verify(self, result, criteria):
        if result.get("artifact") != "fixture.txt":
            raise ValueError("ARTIFACT_PATH_REJECTED")
        data = self.file.read_bytes()
        assert len(data) < 10000
        expected = {"greeting": b"hello", "quantity": b"42"}
        return {
            "fingerprint": hashlib.sha256(data).hexdigest(),
            "failed": [k for k in criteria if expected[k] not in data],
        }

    def admit(self):
        return self.bridge.admit(
            self.task, self.run, "a" * 40, self.criteria, self.deadline, self.request
        )

    def result(self, rid):
        return self.bridge.envelope(rid) | {
            "artifact": "fixture.txt",
            "reportedSuccess": True,
        }

    def state(self):
        return self.db.execute(
            "SELECT status FROM tasks WHERE id=?", (self.task,)
        ).fetchone()[0]

    def test_real_admission_delegation_local_acceptance(self):
        row = self.admit()
        again = self.admit()
        self.assertEqual(row["job"], again["job"])
        self.assertEqual(self.queue.calls, 1)
        self.assertEqual(
            self.bridge.receive(self.result(row["request_id"]))["state"], "completed"
        )
        self.assertEqual(self.state(), "done")

    def test_lost_ack_readback_does_not_resend(self):
        self.queue.lose_ack = True
        with self.assertRaises(TimeoutError):
            self.admit()
        row = self.admit()
        self.assertEqual(row["state"], "delegated")
        self.assertEqual(self.queue.calls, 1)

    def test_identity_cannot_change_on_retry(self):
        self.admit()
        self.deadline += 10
        with self.assertRaisesRegex(ValueError, "IDENTITY_CONFLICT"):
            self.admit()

    def test_wrong_return_identity_rejected(self):
        row = self.admit()
        for field, value in [
            ("run", 999),
            ("revision", "b" * 40),
            ("jobId", "other"),
            ("delegationId", "other"),
            ("task", "other"),
        ]:
            with self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
                self.bridge.receive(self.result(row["request_id"]) | {field: value})
        self.assertEqual(self.state(), "running")

    def test_specific_correction_then_verified_success(self):
        row = self.admit()
        self.file.write_text("hello")
        reply = self.bridge.receive(self.result(row["request_id"]))
        self.assertEqual(reply["correction"]["criteria"], ["quantity"])
        old = self.result(row["request_id"])
        old["correctionId"] = None
        with self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
            self.bridge.receive(old)
        self.file.write_text("hello 42")
        self.assertEqual(
            self.bridge.receive(self.result(row["request_id"]))["state"], "completed"
        )

    def test_no_progress_blocks_with_reason_and_action(self):
        row = self.admit()
        self.file.write_text("hello")
        self.bridge.receive(self.result(row["request_id"]))
        result = self.bridge.receive(self.result(row["request_id"]))
        self.assertEqual(result["cause"], "correction_without_progress")
        self.assertTrue(result["next_action"])
        self.assertEqual(self.state(), "blocked")

    def test_deadline_not_renewed_and_no_false_success(self):
        row = self.admit()
        self.now = self.deadline + 1
        result = self.bridge.receive(self.result(row["request_id"]))
        self.assertEqual(result["cause"], "deadline_exhausted")
        self.assertEqual(self.state(), "blocked")

    def test_completed_ack_loss_reconciles_real_kanban(self):
        row = self.admit()
        result = self.result(row["request_id"])
        self.bridge.receive(result)
        self.db.execute("UPDATE synthetic_bridge SET state='completing'")
        self.assertEqual(self.bridge.receive(result)["state"], "completed")
        self.assertEqual(self.queue.calls, 1)

    def test_missing_block_diagnosis_rejected(self):
        row = self.admit()
        with self.assertRaisesRegex(ValueError, "DIAGNOSIS"):
            self.bridge.block(row["request_id"], "", "")

    def test_real_delegated_child_mutation_guard(self):
        from agent.delegation_context import _DELEGATED_CHILD_CONTEXT

        token = _DELEGATED_CHILD_CONTEXT.set(True)
        try:
            with self.assertRaises(PermissionError):
                self.admit()
        finally:
            _DELEGATED_CHILD_CONTEXT.reset(token)

    def test_rejected_remote_admission_never_delegates(self):
        self.queue.reject = True
        self.assertEqual(self.admit()["state"], "blocked")
        self.assertEqual(self.state(), "blocked")
        self.assertEqual(self.queue.calls, 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM fixture_queue").fetchone()[0], 0
        )

    def test_readback_exhausts_budget_before_send(self):
        original = self.queue.read

        def late(rid):
            self.now = self.deadline + 1
            return original(rid)

        self.queue.read = late
        self.assertEqual(self.admit()["state"], "blocked")
        self.assertEqual(self.queue.calls, 0)

    def test_job_identity_cannot_be_rebound(self):
        row = self.admit()
        original = self.queue.read
        self.queue.read = lambda rid: original(rid) | {"jobId": "other"}
        with self.assertRaisesRegex(ValueError, "JOB_IDENTITY_CONFLICT"):
            self.bridge.dispatch(row["request_id"])

    def test_crash_before_terminal_effect_resumes_after_reverification(self):
        row = self.admit()
        result = self.result(row["request_id"])
        original = kb.complete_task
        kb.complete_task = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("synthetic crash before effect")
        )
        try:
            with self.assertRaises(RuntimeError):
                self.bridge.receive(result)
        finally:
            kb.complete_task = original
        self.assertEqual(self.state(), "running")
        self.assertEqual(self.bridge.receive(result)["state"], "completed")

    def test_crash_before_block_resumes(self):
        row = self.admit()
        original = kb.block_task
        kb.block_task = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("synthetic crash before block")
        )
        try:
            with self.assertRaises(RuntimeError):
                self.bridge.block(row["request_id"], "fixture", "inspect")
        finally:
            kb.block_task = original
        self.assertEqual(self.bridge.reconcile(row["request_id"])["state"], "blocked")

    def test_invalid_verifier_contract_cannot_complete(self):
        row = self.admit()
        self.bridge.verifier = lambda *a: {"fingerprint": "fake", "failed": []}
        with self.assertRaisesRegex(ValueError, "VERIFIER_CONTRACT_INVALID"):
            self.bridge.receive(self.result(row["request_id"]))
        self.assertEqual(self.state(), "running")

    def test_atomic_deadline_gate_blocks_delayed_completion(self):
        row = self.admit()
        original = kb.complete_task

        def delayed(*a, **kw):
            self.db.execute(
                "UPDATE synthetic_bridge SET payload=json_set(payload,'$.deadline',?)",
                (time.time() - 2,),
            )
            return original(*a, **kw)

        kb.complete_task = delayed
        try:
            self.assertEqual(
                self.bridge.receive(self.result(row["request_id"]))["state"], "blocked"
            )
        finally:
            kb.complete_task = original
        self.assertEqual(self.state(), "blocked")

    def test_concurrent_old_return_cannot_bypass_new_correction(self):
        import threading, concurrent.futures

        row = self.admit()
        rid = row["request_id"]
        reply = self.result(rid)
        self.file.write_text("hello")
        barrier = threading.Barrier(2)
        written = threading.Event()

        def respond(which):
            db = sqlite3.connect(
                self.root / "kanban-synthetic.sqlite", isolation_level=None
            )
            db.row_factory = sqlite3.Row

            def verify(result, criteria):
                barrier.wait(timeout=5)
                if which == "late":
                    written.wait(timeout=5)
                    self.file.write_text("hello 42")
                return self.verify(result, criteria)

            bridge = Bridge(kb, db, self.queue, verify, self.clock)
            try:
                try:
                    return bridge.receive(reply)
                except ValueError as error:
                    return str(error)
            finally:
                if which == "first":
                    written.set()
                db.close()

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(respond, "first")
            late = pool.submit(respond, "late")
            self.assertEqual(first.result()["state"], "correction")
            self.assertEqual(late.result(), "CONCURRENT_RETURN")
        self.assertEqual(self.state(), "running")

    def test_atomic_gate_rejects_terminal_state_changed_after_verification(self):
        row = self.admit()
        calls = 0
        original = self.bridge.verifier

        def raced(result, criteria):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.db.execute(
                    "UPDATE synthetic_bridge SET state='blocking',version=version+1"
                )
            return original(result, criteria)

        self.bridge.verifier = raced
        with self.assertRaisesRegex(sqlite3.IntegrityError, "TERMINAL_STATE_CONFLICT"):
            self.bridge.receive(self.result(row["request_id"]))
        self.assertEqual(self.state(), "running")

    def test_concurrent_block_intent_is_immutable_and_readback_matches(self):
        import threading, concurrent.futures

        row = self.admit()
        rid = row["request_id"]
        intent = threading.Event()
        competed = threading.Event()
        original = kb.block_task

        def delayed(*a, **kw):
            intent.set()
            competed.wait(timeout=5)
            return original(*a, **kw)

        kb.block_task = delayed

        def respond(cause):
            db = sqlite3.connect(
                self.root / "kanban-synthetic.sqlite", isolation_level=None
            )
            db.row_factory = sqlite3.Row
            bridge = Bridge(kb, db, self.queue, self.verify, self.clock)
            try:
                if cause == "B":
                    intent.wait(timeout=5)
                try:
                    return bridge.block(rid, cause, "inspect")
                except ValueError as error:
                    return str(error)
            finally:
                if cause == "B":
                    competed.set()
                db.close()

        try:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                first = pool.submit(respond, "A")
                second = pool.submit(respond, "B")
                self.assertEqual(second.result(), "BLOCK_IDENTITY_CONFLICT")
                self.assertEqual(first.result()["cause"], "A")
        finally:
            kb.block_task = original
        self.assertEqual(self.bridge.reconcile(rid)["cause"], "A")
        self.assertEqual(self.state(), "blocked")

    def test_missing_sql_clock_fails_closed_before_schema_or_acceptance(self):
        probe = sqlite3.connect(":memory:")
        probe.create_function("unixepoch", 1, lambda _: None)
        try:
            with self.assertRaisesRegex(ValueError, "SQLITE_CLOCK_UNAVAILABLE"):
                Bridge(kb, probe, self.queue, self.verify, self.clock)
            self.assertEqual(
                probe.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0
            )
        finally:
            probe.close()
        row = self.admit()
        self.db.create_function("unixepoch", 1, lambda _: None)
        self.assertEqual(
            self.bridge.receive(self.result(row["request_id"]))["state"], "blocked"
        )
        self.assertEqual(self.state(), "blocked")
