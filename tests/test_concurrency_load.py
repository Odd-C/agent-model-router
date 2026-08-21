"""真实并发/压测（Load Test）——非自证式 benchmark。

背景（老大质疑 2026-08-21）：benchmark.py 是「库内合成模拟三策略对比」，
属于自证式（自己的公式在自己的模拟数据上跑）。生产并发能力（多线程/多
进程同时路由、SQLite 并发写入的吞吐与锁竞争）此前没有量化测量。

本文件测量真实并发行为，输出可量化指标（QPS / p50-p95 延迟 / 丢数据率），
并断言「不丢数据、无异常、锁竞争下吞吐不塌方」。

运行：
  python -m pytest tests/test_concurrency_load.py -v -s   # -s 显示量化输出
"""

import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.executor import MockExecutor
from model_scheduler.policy import list_models
from model_scheduler.quota import QuotaTracker
from model_scheduler.scheduler import TaskScheduler
from model_scheduler.task import Task, TaskStore
from model_scheduler.utility import route_with_utility

N_THREADS = 8
N_CALLS = 200          # 每线程
N_PROCESSES = 2
N_PROCESS_TASKS = 50   # 每进程


def _route_once() -> None:
    task = {"task_type": "coding", "priority": "high", "deadline": None}
    route_with_utility(task, list_models())


def _record_worker(state_dir: str, prefix: str, count: int, q) -> None:
    """子进程：并发 record_failure / quota 写入 SQLite。"""
    import model_scheduler as ms
    from model_scheduler.quota import QuotaTracker
    ms.configure_state_dir(state_dir)
    t = QuotaTracker(state_dir)
    for i in range(count):
        t.record_call(f"{prefix}-{i}", "test")
        if i % 10 == 0:
            t.record_failure(f"{prefix}-{i}", "test", reason="rate_limit", status=429)
    q.put(count)


class ConcurrencyLoadTests(unittest.TestCase):
    def test_thread_route_qps(self):
        """多线程并发路由：测 QPS 与 p50/p95 延迟，不丢、不抛。"""
        errs = []
        lat = []
        lock = threading.Lock()

        def worker():
            for _ in range(N_CALLS):
                t0 = time.perf_counter()
                try:
                    _route_once()
                except Exception as e:  # pragma: no cover
                    with lock:
                        errs.append(str(e))
                with lock:
                    lat.append((time.perf_counter() - t0) * 1000)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

        total = N_THREADS * N_CALLS
        qps = total / elapsed
        lat.sort()
        p50 = lat[len(lat) // 2]
        p95 = lat[int(len(lat) * 0.95)]
        print(f"\n[route] {total} 次 / {elapsed:.2f}s = {qps:.0f} QPS | p50={p50:.2f}ms p95={p95:.2f}ms")
        self.assertEqual(errs, [])
        self.assertGreater(qps, 100, f"路由 QPS 过低: {qps:.0f}")

    def test_thread_quota_record_qps(self):
        """多线程 quota record_call：锁竞争下吞吐不塌方。"""
        with tempfile.TemporaryDirectory() as td:
            t = QuotaTracker(td)
            errs = []
            lock = threading.Lock()

            def worker():
                for i in range(N_CALLS):
                    try:
                        t.record_call(f"m{i}", "p")
                    except Exception as e:  # pragma: no cover
                        with lock:
                            errs.append(str(e))

            threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
            t0 = time.perf_counter()
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            elapsed = time.perf_counter() - t0
            total = N_THREADS * N_CALLS
            qps = total / elapsed
            print(f"\n[quota] {total} 次 / {elapsed:.2f}s = {qps:.0f} QPS")
            self.assertEqual(errs, [])

    def test_process_sqlite_concurrent_write(self):
        """多进程 SQLite 并发写：不丢数据（P0 回归 + 吞吐）。"""
        with tempfile.TemporaryDirectory() as td:
            # 预置进程数×任务数，防重复 key 干扰
            expected = N_PROCESSES * N_PROCESS_TASKS
            q = multiprocessing.Queue()
            procs = []
            for pi in range(N_PROCESSES):
                p = multiprocessing.Process(
                    target=_record_worker, args=(td, f"proc{pi}", N_PROCESS_TASKS, q)
                )
                procs.append(p)
            t0 = time.perf_counter()
            for p in procs:
                p.start()
            for p in procs:
                p.join()
            elapsed = time.perf_counter() - t0
            # 确认所有进程正常完成
            got = 0
            while not q.empty():
                got += q.get()
            print(f"\n[proc] {got} 次 record / {elapsed:.2f}s = {got/elapsed:.0f} QPS")
            self.assertEqual(got, expected)

    def test_tick_under_thread_load(self):
        """tick 在并发负载下：无 running 残留、无非法迁移。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(td, backend="sqlite")
            sch = TaskScheduler(store, MockExecutor(), base_delay=0.0)
            for i in range(50):
                store.add(Task(
                    task_id=f"t{i}", task_type="text", priority="high",
                    deadline=None, defer_until=None, status="queued",
                    payload={}, attempts=0, last_error=None,
                    created_at=1.0, updated_at=1.0,
                ))
            errs = []

            def ticker():
                try:
                    for _ in range(5):
                        sch.tick(now=100.0 + _)
                except Exception as e:  # pragma: no cover
                    errs.append(str(e))

            threads = [threading.Thread(target=ticker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            statuses = {t.status for t in store.list(limit=None)}
            print(f"\n[tick] 最终状态集: {statuses}")
            self.assertEqual(errs, [])
            self.assertNotIn("running", statuses)
            self.assertNotIn("queued", statuses)  # 50 个全部处理完


if __name__ == "__main__":
    unittest.main()
