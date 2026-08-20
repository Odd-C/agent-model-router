import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.executor import CommandExecutor
from model_scheduler.task import Task


def make_task(payload):
    return Task(
        task_id="cmd-1",
        task_type="command",
        priority="normal",
        deadline=None,
        defer_until=None,
        status="running",
        payload=payload,
        attempts=0,
        last_error=None,
        created_at=1000.0,
        updated_at=1000.0,
    )


class CommandExecutorTests(unittest.TestCase):
    def test_success_returns_exit_code_and_stdout(self):
        executor = CommandExecutor()
        result = executor.execute(make_task({"command": [sys.executable, "-c", "print('hello')"]}))
        self.assertIsNone(result.error)
        self.assertEqual(result.result["exit_code"], 0)
        self.assertIn("hello", result.result["stdout"])

    def test_non_string_elements_are_rejected(self):
        executor = CommandExecutor()
        result = executor.execute(make_task({"command": ["echo", 123]}))
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["error_type"], "invalid_payload")
        self.assertIn("only strings", result.error["message"])

    def test_non_list_command_is_rejected(self):
        executor = CommandExecutor()
        result = executor.execute(make_task({"command": "echo hi"}))
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["error_type"], "invalid_payload")

    def test_non_zero_exit_returns_command_failed(self):
        executor = CommandExecutor()
        result = executor.execute(make_task({"command": [sys.executable, "-c", "import sys; sys.exit(3)"]}))
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["error_type"], "command_failed")
        self.assertEqual(result.error["status"], 3)

    def test_negative_timeout_rejected(self):
        with self.assertRaises(ValueError):
            CommandExecutor(timeout=-1)
        with self.assertRaises(ValueError):
            CommandExecutor(timeout=-0.1)

    def test_timeout_returns_command_error(self):
        executor = CommandExecutor(timeout=0.1)
        result = executor.execute(
            make_task({"command": [sys.executable, "-c", "import time; time.sleep(1)"]})
        )
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["error_type"], "command_error")
        self.assertIn("timed out", result.error["message"])


if __name__ == "__main__":
    unittest.main()
