import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.task import Task, TaskStore


def _add_tasks_worker(state_dir: str, prefix: str, count: int) -> None:
    """子进程工作函数：向 SQLite 后端 TaskStore 中新增 count 条任务。"""
    store = TaskStore(state_dir, backend="sqlite")
    for i in range(count):
        task_id = f"{prefix}-{i}"
        task = Task(
            task_id=task_id,
            task_type="text",
            priority="normal",
            deadline=None,
            defer_until=None,
            status="queued",
            payload={"worker": prefix, "index": i},
            attempts=0,
            last_error=None,
            created_at=float(i),
            updated_at=float(i),
        )
        store.add(task)


class SQLiteTaskStoreConcurrencyTests(unittest.TestCase):
    """P0-1 回归测试：TaskStore SQLite 后端跨进程读改写必须原子。

    审查实测两进程各 add 30 条时，旧 _load()+_save() 实现会丢数据
    （最终仅 31/60）。atomic_update 实现后必须 60 条全在。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_processes_each_add_30_tasks_keeps_all_60(self):
        # Linux 下使用 fork 上下文，保证测试进程可稳定复现跨进程并发写。
        ctx = multiprocessing.get_context("fork")
        workers = [
            ctx.Process(target=_add_tasks_worker, args=(str(self.state_dir), "a", 30)),
            ctx.Process(target=_add_tasks_worker, args=(str(self.state_dir), "b", 30)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)

        for worker in workers:
            self.assertEqual(worker.exitcode, 0)

        store = TaskStore(self.state_dir, backend="sqlite")
        tasks = store.list(limit=None)
        task_ids = {task.task_id for task in tasks}
        self.assertEqual(len(tasks), 60)
        for prefix in ("a", "b"):
            for i in range(30):
                self.assertIn(f"{prefix}-{i}", task_ids)


if __name__ == "__main__":
    unittest.main()
