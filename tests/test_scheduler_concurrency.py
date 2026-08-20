import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.executor import MockExecutor
from model_scheduler.scheduler import TaskScheduler
from model_scheduler.task import Task, TaskStore


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


class SchedulerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_concurrent_tick_threads_do_not_throw_and_no_running_left(self):
        n_tasks = 30
        for i in range(n_tasks):
            self.store.add(make_task(task_id=f"t{i}", status="queued"))

        scheduler = TaskScheduler(self.store, MockExecutor(result={"ok": True}))
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=5)
                scheduler.tick(now=2000.0)
            except Exception as exc:  # noqa: BLE001 - 测试需收集线程异常
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        for i in range(n_tasks):
            stored = self.store.get(f"t{i}")
            self.assertIsNotNone(stored)
            self.assertIn(stored.status, ("done", "failed", "cancelled", "expired"))
            self.assertNotEqual(stored.status, "running")
        self.assertEqual(
            sum(1 for i in range(n_tasks) if self.store.get(f"t{i}").status == "done"),
            n_tasks,
        )


class SchedulerDeadlineBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_tick_expires_queued_task_when_deadline_equals_now(self):
        self.store.add(make_task("queued-edge", status="queued", deadline=2000.0))
        scheduler = TaskScheduler(self.store, MockExecutor(result={"ok": True}))

        processed = scheduler.tick(now=2000.0)

        stored = self.store.get("queued-edge")
        self.assertEqual(stored.status, "expired")
        self.assertIn("queued-edge", processed)
        # 已过期任务不应被 queued -> running 执行。
        self.assertEqual(stored.attempts, 0)
        self.assertIsNone(stored.result)

    def test_tick_expires_deferred_task_when_deadline_equals_now(self):
        self.store.add(
            make_task(
                "deferred-edge",
                status="deferred",
                deadline=2000.0,
                defer_until=2500.0,
            )
        )
        scheduler = TaskScheduler(self.store, MockExecutor(result={"ok": True}))

        processed = scheduler.tick(now=2000.0)

        stored = self.store.get("deferred-edge")
        self.assertEqual(stored.status, "expired")
        self.assertIn("deferred-edge", processed)
        # deadline == now 的任务不应再被转 queued 执行。
        self.assertEqual(stored.defer_until, 2500.0)

    def test_submit_with_deadline_equal_now_is_expired(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            scheduler = TaskScheduler(self.store, MockExecutor(result={"ok": True}))
            task = scheduler.submit("text", {"x": 1}, deadline=1000.0)

        self.assertEqual(task.status, "expired")
        self.assertEqual(self.store.get(task.task_id).status, "expired")


if __name__ == "__main__":
    unittest.main()
