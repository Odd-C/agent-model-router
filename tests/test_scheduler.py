import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.executor import ExecutorResult, MockExecutor
from model_scheduler.scheduler import DEGRADATION_MATRIX, TaskScheduler, decide_action
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


class SchedulerDeferTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _scheduler(self, **kwargs):
        return TaskScheduler(self.store, MockExecutor(), **kwargs)

    def test_high_priority_immediate_queued(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            task = self._scheduler().submit("text", {"x": 1}, priority="high")
        self.assertEqual(task.status, "queued")
        self.assertIsNone(task.defer_until)

    def test_normal_priority_deferred_with_base_delay(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            task = self._scheduler(base_delay=300).submit("text", {"x": 1}, priority="normal")
        self.assertEqual(task.status, "deferred")
        self.assertEqual(task.defer_until, 1300.0)

    def test_low_priority_deferred_longer(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            task = self._scheduler(base_delay=100).submit("text", {"x": 1}, priority="low")
        self.assertEqual(task.status, "deferred")
        self.assertEqual(task.defer_until, 1200.0)

    def test_near_deadline_immediate_queued(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            task = self._scheduler(deadline_horizon=3600).submit("text", {"x": 1}, deadline=1400.0)
        self.assertEqual(task.status, "queued")
        self.assertIsNone(task.defer_until)

    def test_far_deadline_defers(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            task = self._scheduler().submit("text", {"x": 1}, deadline=10000.0)
        self.assertEqual(task.status, "deferred")
        self.assertIsNotNone(task.defer_until)

    def test_invalid_priority_raises(self):
        with self.assertRaises(ValueError):
            self._scheduler().submit("text", {}, priority="urgent")

    def test_past_deadline_submit_is_expired(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            task = self._scheduler().submit("text", {"x": 1}, deadline=1000.0)
        self.assertEqual(task.status, "expired")
        self.assertIsNone(task.defer_until)
        self.assertEqual(self.store.get(task.task_id).status, "expired")

        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            task = self._scheduler().submit("text", {"x": 1}, deadline=900.0)
        self.assertEqual(task.status, "expired")
        self.assertEqual(self.store.get(task.task_id).status, "expired")

    def test_deadline_must_be_number_or_none(self):
        with self.assertRaises(ValueError):
            self._scheduler().submit("text", {}, deadline=True)
        with self.assertRaises(ValueError):
            self._scheduler().submit("text", {}, deadline="tomorrow")
        with self.assertRaises(ValueError):
            self._scheduler().submit("text", {}, deadline=0)


class SchedulerTickTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_tick_executes_queued_task_success(self):
        task = make_task("ok")
        self.store.add(task)
        executor = MockExecutor(result={"done": True}, cost=2.5)
        scheduler = TaskScheduler(self.store, executor)

        processed = scheduler.tick(now=2000.0)

        self.assertEqual(processed, ["ok"])
        stored = self.store.get("ok")
        self.assertEqual(stored.status, "done")
        self.assertEqual(stored.result, {"done": True})
        self.assertEqual(stored.cost, 2.5)
        self.assertEqual(stored.attempts, 0)

    def test_tick_failure_retries_up_to_limit_then_cancels(self):
        task = make_task("bad")
        self.store.add(task)
        executor = MockExecutor(error={"error_type": "mock_error", "status": None, "message": "boom"})
        scheduler = TaskScheduler(self.store, executor, max_retries=3)

        for expected_attempts in (1, 2, 3):
            processed = scheduler.tick(now=2000.0)
            self.assertEqual(processed, ["bad"])
            stored = self.store.get("bad")
            self.assertEqual(stored.attempts, expected_attempts)
            self.assertEqual(stored.status, "queued")
            self.assertEqual(stored.last_error["error_type"], "mock_error")
            self.assertEqual(stored.last_error["action_taken"], "abort")

        # 超过重试上限后取消。
        processed = scheduler.tick(now=2000.0)
        self.assertEqual(processed, ["bad"])
        stored = self.store.get("bad")
        self.assertEqual(stored.attempts, 4)
        self.assertEqual(stored.status, "cancelled")

    def test_deferred_promoted_to_queued_when_due(self):
        task = make_task("d1", status="deferred", defer_until=1500.0)
        self.store.add(task)
        executor = MockExecutor(result={"ok": True})
        scheduler = TaskScheduler(self.store, executor)

        processed = scheduler.tick(now=1400.0)
        self.assertEqual(processed, [])
        self.assertEqual(self.store.get("d1").status, "deferred")

        processed = scheduler.tick(now=1500.0)
        self.assertEqual(processed, ["d1"])  # 同一轮内先转 queued，再执行
        stored = self.store.get("d1")
        self.assertEqual(stored.status, "done")
        self.assertIsNone(stored.defer_until)

    def test_executor_exception_is_captured_as_failure(self):
        class ExplodingExecutor:
            def execute(self, task):
                raise RuntimeError("executor blew up")

        task = make_task("boom")
        self.store.add(task)
        scheduler = TaskScheduler(self.store, ExplodingExecutor(), max_retries=1)

        scheduler.tick(now=2000.0)
        stored = self.store.get("boom")
        self.assertEqual(stored.status, "queued")
        self.assertEqual(stored.attempts, 1)
        self.assertEqual(stored.last_error["error_type"], "executor_exception")

        scheduler.tick(now=2000.0)
        stored = self.store.get("boom")
        self.assertEqual(stored.status, "cancelled")
        self.assertEqual(stored.attempts, 2)

    def test_cancel_allowed_only_for_legal_statuses(self):
        # queued 可取消
        self.store.add(make_task("q", status="queued"))
        # deferred 可取消
        self.store.add(make_task("d", status="deferred"))

        # failed 可取消：queued -> running -> failed 逐步迁移
        self.store.add(make_task("f", status="queued"))
        f = self.store.get("f")
        f.status = "running"
        self.store.update(f)
        f.status = "failed"
        self.store.update(f)

        # running/done 不可取消：queued -> running；running -> done
        self.store.add(make_task("r", status="queued"))
        r = self.store.get("r")
        r.status = "running"
        self.store.update(r)

        self.store.add(make_task("x", status="queued"))
        x = self.store.get("x")
        x.status = "running"
        self.store.update(x)
        x.status = "done"
        self.store.update(x)

        scheduler = TaskScheduler(self.store, MockExecutor())
        self.assertTrue(scheduler.cancel("q"))
        self.assertTrue(scheduler.cancel("d"))
        self.assertTrue(scheduler.cancel("f"))
        self.assertFalse(scheduler.cancel("r"))
        self.assertFalse(scheduler.cancel("x"))
        self.assertFalse(scheduler.cancel("missing"))
        self.assertEqual(self.store.get("q").status, "cancelled")
        self.assertEqual(self.store.get("d").status, "cancelled")
        self.assertEqual(self.store.get("f").status, "cancelled")

    def test_run_once_is_tick_alias(self):
        scheduler = TaskScheduler(self.store, MockExecutor())
        with mock.patch.object(scheduler, "tick", return_value=["x"]) as mocked_tick:
            self.assertEqual(scheduler.run_once(now=2000.0), ["x"])
            mocked_tick.assert_called_once_with(now=2000.0)

    def test_tick_expires_queued_and_deferred_past_deadline(self):
        q = make_task("q-expired", status="queued", deadline=1999.0)
        d = make_task("d-expired", status="deferred", deadline=1999.0, defer_until=2500.0)
        future = make_task("future", status="queued", deadline=2001.0)
        self.store.add(q)
        self.store.add(d)
        self.store.add(future)
        scheduler = TaskScheduler(self.store, MockExecutor())

        processed = scheduler.tick(now=2000.0)

        self.assertEqual(self.store.get("q-expired").status, "expired")
        self.assertEqual(self.store.get("d-expired").status, "expired")
        # 未来 deadline 的 queued 任务正常执行。
        self.assertEqual(self.store.get("future").status, "done")
        self.assertIn("q-expired", processed)
        self.assertIn("d-expired", processed)

    def test_tick_expires_when_deadline_equals_now(self):
        task = make_task("edge", status="queued", deadline=2000.0)
        self.store.add(task)
        scheduler = TaskScheduler(self.store, MockExecutor())
        scheduler.tick(now=2000.0)
        self.assertEqual(self.store.get("edge").status, "expired")

    def test_success_clears_last_error(self):
        task = make_task("retry-ok")
        task.last_error = {"error_type": "mock_error", "status": None, "action_taken": "abort", "message": "boom"}
        self.store.add(task)
        scheduler = TaskScheduler(self.store, MockExecutor(result={"ok": True}))

        scheduler.tick(now=2000.0)

        stored = self.store.get("retry-ok")
        self.assertEqual(stored.status, "done")
        self.assertIsNone(stored.last_error)



class SchedulerDegradationActionTests(unittest.TestCase):
    """v0.4：失败任务 last_error.action_taken 按降级矩阵分类记录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_tick_failure_action_taken_follows_degradation_matrix(self):
        for error_type, expected in DEGRADATION_MATRIX.items():
            with self.subTest(error_type=error_type):
                task = make_task(f"task-{error_type}")
                self.store.add(task)
                executor = MockExecutor(error={"error_type": error_type, "status": None, "message": "boom"})
                scheduler = TaskScheduler(self.store, executor, max_retries=3)

                scheduler.tick(now=2000.0)

                stored = self.store.get(task.task_id)
                self.assertEqual(stored.last_error["error_type"], error_type)
                self.assertEqual(stored.last_error["action_taken"], expected)
                # v0.3 状态机/重试语义不变：失败后仍回到 queued 等待重试。
                self.assertEqual(stored.status, "queued")

    def test_decide_action_unknown_aborts(self):
        self.assertEqual(decide_action("unknown_error"), "abort")
        self.assertEqual(decide_action(None), "abort")

if __name__ == "__main__":
    unittest.main()
