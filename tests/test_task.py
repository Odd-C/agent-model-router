import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.task import Task, TaskStore, valid_transition


def make_task(task_id="t1", status="queued", priority="normal", defer_until=None, deadline=None, attempts=0):
    return Task(
        task_id=task_id,
        task_type="text",
        priority=priority,
        deadline=deadline,
        defer_until=defer_until,
        status=status,
        payload={"prompt": "hello"},
        attempts=attempts,
        last_error=None,
        created_at=1000.0,
        updated_at=1000.0,
    )


class TaskModelTests(unittest.TestCase):
    def test_valid_transition_matrix(self):
        self.assertTrue(valid_transition("queued", "running"))
        self.assertTrue(valid_transition("queued", "deferred"))
        self.assertTrue(valid_transition("queued", "cancelled"))
        self.assertTrue(valid_transition("queued", "expired"))
        self.assertTrue(valid_transition("deferred", "queued"))
        self.assertTrue(valid_transition("deferred", "cancelled"))
        self.assertTrue(valid_transition("deferred", "expired"))
        self.assertTrue(valid_transition("running", "done"))
        self.assertTrue(valid_transition("running", "failed"))
        self.assertTrue(valid_transition("failed", "queued"))
        self.assertTrue(valid_transition("failed", "cancelled"))
        self.assertFalse(valid_transition("queued", "done"))
        self.assertFalse(valid_transition("running", "queued"))
        self.assertFalse(valid_transition("done", "queued"))
        self.assertFalse(valid_transition("cancelled", "queued"))
        self.assertFalse(valid_transition("expired", "queued"))
        self.assertFalse(valid_transition("queued", "queued"))
        self.assertFalse(valid_transition("", "queued"))
        self.assertFalse(valid_transition("queued", ""))

    def test_validate_rejects_invalid_priority_status_and_times(self):
        with self.assertRaises(ValueError):
            make_task(priority="urgent").validate()
        with self.assertRaises(ValueError):
            make_task(status="unknown").validate()
        with self.assertRaises(ValueError):
            make_task(task_id="").validate()
        with self.assertRaises(ValueError):
            make_task(deadline=-1).validate()
        with self.assertRaises(ValueError):
            make_task(defer_until=0).validate()
        # 正数时间合法
        make_task(deadline=2000.0, defer_until=1500.0).validate()

    def test_to_dict_and_from_dict_roundtrip(self):
        task = make_task(status="running", attempts=1, defer_until=1200.0)
        task.result = {"ok": True}
        task.cost = 1.5
        task.last_error = {"error_type": "x", "status": None, "action_taken": "abort", "message": "boom"}

        restored = Task.from_dict(task.to_dict())
        self.assertEqual(restored.task_id, task.task_id)
        self.assertEqual(restored.status, "running")
        self.assertEqual(restored.attempts, 1)
        self.assertEqual(restored.defer_until, 1200.0)
        self.assertEqual(restored.result, {"ok": True})
        self.assertEqual(restored.cost, 1.5)
        self.assertEqual(restored.last_error["action_taken"], "abort")

    def test_from_dict_rejects_bad_shape(self):
        with self.assertRaises(ValueError):
            Task.from_dict({"task_id": ""})

    def test_validate_rejects_invalid_last_error(self):
        task = make_task()
        task.last_error = {"error_type": "x"}
        with self.assertRaises(ValueError):
            task.validate()
        task.last_error = {"action_taken": "abort"}
        with self.assertRaises(ValueError):
            task.validate()
        task.last_error = {"error_type": "x", "action_taken": "abort"}
        task.validate()


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_get_update_remove(self):
        task = make_task("a")
        self.store.add(task)
        self.assertEqual(self.store.get("a").task_id, "a")
        self.assertIsNone(self.store.get("missing"))

        task.status = "running"
        self.store.update(task)
        self.assertEqual(self.store.get("a").status, "running")

        removed = self.store.remove("a")
        self.assertEqual(removed.status, "running")
        self.assertIsNone(self.store.get("a"))
        self.assertIsNone(self.store.remove("a"))

    def test_add_duplicate_raises(self):
        self.store.add(make_task("a"))
        with self.assertRaises(ValueError):
            self.store.add(make_task("a"))

    def test_update_missing_raises(self):
        with self.assertRaises(KeyError):
            self.store.update(make_task("missing"))

    def test_add_rejects_non_initial_status(self):
        for status in ("running", "done", "failed", "cancelled", "expired"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    self.store.add(make_task("bad-" + status, status=status))

    def test_update_rejects_illegal_transition(self):
        # 构造终态 done：queued -> running -> done（均为合法迁移）
        self.store.add(make_task("done-task", status="queued"))
        task = self.store.get("done-task")
        task.status = "running"
        self.store.update(task)
        task.status = "done"
        self.store.update(task)

        task = self.store.get("done-task")
        task.status = "queued"
        with self.assertRaises(ValueError) as ctx:
            self.store.update(task)
        self.assertEqual(str(ctx.exception), "invalid transition: done -> queued")
        # 存储中的数据未被污染。
        self.assertEqual(self.store.get("done-task").status, "done")

    def test_update_allows_queued_to_deferred(self):
        self.store.add(make_task("qd", status="queued"))
        task = self.store.get("qd")
        task.status = "deferred"
        task.defer_until = 1500.0
        self.store.update(task)
        self.assertEqual(self.store.get("qd").status, "deferred")

    def test_list_filters_and_paginates(self):
        for i in range(5):
            task = make_task(task_id=f"t{i}", status="queued" if i % 2 == 0 else "deferred")
            task.task_type = "text" if i < 4 else "coding"
            task.created_at = float(i)
            self.store.add(task)

        queued = self.store.list(status="queued", limit=None)
        self.assertEqual([t.task_id for t in queued], ["t0", "t2", "t4"])

        coding = self.store.list(task_type="coding", limit=None)
        self.assertEqual([t.task_id for t in coding], ["t4"])

        page = self.store.list(status="queued", offset=1, limit=1)
        self.assertEqual([t.task_id for t in page], ["t2"])

        with self.assertRaises(ValueError):
            self.store.list(offset=-1)
        with self.assertRaises(ValueError):
            self.store.list(limit=-1)

    def test_persistence_roundtrip(self):
        self.store.add(make_task("p1", status="deferred", defer_until=1200.0))
        store2 = TaskStore(self.state_dir)
        self.assertEqual(store2.get("p1").status, "deferred")
        self.assertEqual(store2.get("p1").defer_until, 1200.0)

    def test_atomic_write_no_tmp_files(self):
        self.store.add(make_task("a"))
        self.assertEqual(list(self.state_dir.glob(".model-tasks.json.*.tmp")), [])
        data = json.loads((self.state_dir / "model-tasks.json").read_text(encoding="utf-8"))
        self.assertIn("a", data)


class TaskStoreSQLiteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir, backend="sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def test_backend_name(self):
        self.assertEqual(self.store.backend_name, "sqlite")

    def test_add_get_update_remove(self):
        task = make_task("a")
        self.store.add(task)
        self.assertEqual(self.store.get("a").task_id, "a")
        self.assertIsNone(self.store.get("missing"))

        task.status = "running"
        self.store.update(task)
        self.assertEqual(self.store.get("a").status, "running")

        removed = self.store.remove("a")
        self.assertEqual(removed.status, "running")
        self.assertIsNone(self.store.get("a"))
        self.assertIsNone(self.store.remove("a"))

    def test_list_filters_and_paginates(self):
        for i in range(5):
            task = make_task(task_id=f"t{i}", status="queued" if i % 2 == 0 else "deferred")
            task.task_type = "text" if i < 4 else "coding"
            task.created_at = float(i)
            self.store.add(task)

        queued = self.store.list(status="queued", limit=None)
        self.assertEqual([t.task_id for t in queued], ["t0", "t2", "t4"])

        coding = self.store.list(task_type="coding", limit=None)
        self.assertEqual([t.task_id for t in coding], ["t4"])

        page = self.store.list(status="queued", offset=1, limit=1)
        self.assertEqual([t.task_id for t in page], ["t2"])

    def test_persistence_roundtrip(self):
        self.store.add(make_task("p1", status="deferred", defer_until=1200.0))
        store2 = TaskStore(self.state_dir, backend="sqlite")
        self.assertEqual(store2.get("p1").status, "deferred")
        self.assertEqual(store2.get("p1").defer_until, 1200.0)

    def test_sqlite_backend_does_not_create_json_file(self):
        self.store.add(make_task("a"))
        self.assertFalse((self.state_dir / "model-tasks.json").exists())
        self.assertTrue((self.state_dir / "model-scheduler.db").exists())


class TaskStoreBackendParityTests(unittest.TestCase):
    def test_json_and_sqlite_same_operations_same_results(self):
        results = {}
        for backend in ("json", "sqlite"):
            with tempfile.TemporaryDirectory() as tmp:
                state_dir = Path(tmp)
                store = TaskStore(state_dir, backend=backend)

                t1 = make_task("t1", status="queued")
                t1.task_type = "text"
                t1.created_at = 1.0
                store.add(t1)

                t2 = make_task("t2", status="deferred", defer_until=1500.0)
                t2.task_type = "coding"
                t2.created_at = 2.0
                store.add(t2)

                t3 = make_task("t3", status="queued")
                t3.task_type = "text"
                t3.created_at = 3.0
                store.add(t3)

                updated = store.get("t1")
                updated.status = "running"
                store.update(updated)

                store.remove("t3")

                results[backend] = {
                    "list": [t.to_dict() for t in store.list(limit=None)],
                    "t1": store.get("t1").to_dict(),
                    "t2": store.get("t2").to_dict(),
                    "missing": store.get("missing"),
                }

        self.assertEqual(results["json"], results["sqlite"])


if __name__ == "__main__":
    unittest.main()
