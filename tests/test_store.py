import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.store import (
    JsonStateStore,
    SQLiteStateStore,
    StateStore,
    create_store,
)


class JsonStateStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = JsonStateStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_set_delete_exists_keys(self):
        self.assertIsNone(self.store.get("model-tasks"))
        self.assertFalse(self.store.exists("model-tasks"))

        self.store.set("model-tasks", json.dumps({"a": 1}))
        self.assertTrue(self.store.exists("model-tasks"))
        self.assertEqual(json.loads(self.store.get("model-tasks")), {"a": 1})
        self.assertEqual(self.store.keys(), ["model-tasks"])

        self.store.set("model-quota", json.dumps({"calls": []}))
        self.assertEqual(self.store.keys(), ["model-quota", "model-tasks"])

        self.store.delete("model-tasks")
        self.assertFalse(self.store.exists("model-tasks"))
        self.assertIsNone(self.store.get("model-tasks"))
        self.assertEqual(self.store.keys(), ["model-quota"])

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.store.get("missing"))
        self.assertFalse(self.store.exists("missing"))
        # delete 不存在 key 应静默成功
        self.store.delete("missing")

    def test_set_writes_readable_file(self):
        self.store.set("model-tasks", json.dumps({"task_id": "t1", "status": "queued"}))
        path = self.state_dir / "model-tasks.json"
        self.assertTrue(path.exists())
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {"task_id": "t1", "status": "queued"},
        )

    def test_repeat_set_overwrites(self):
        self.store.set("k", json.dumps({"v": 1}))
        self.store.set("k", json.dumps({"v": 2}))
        self.assertEqual(json.loads(self.store.get("k")), {"v": 2})
        self.assertEqual(self.store.keys(), ["k"])


class SQLiteStateStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = SQLiteStateStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_set_delete_exists_keys(self):
        self.assertIsNone(self.store.get("model-tasks"))
        self.assertFalse(self.store.exists("model-tasks"))

        self.store.set("model-tasks", json.dumps({"a": 1}))
        self.assertTrue(self.store.exists("model-tasks"))
        self.assertEqual(json.loads(self.store.get("model-tasks")), {"a": 1})
        self.assertEqual(self.store.keys(), ["model-tasks"])

        self.store.set("model-quota", json.dumps({"calls": []}))
        self.assertEqual(self.store.keys(), ["model-quota", "model-tasks"])

        self.store.delete("model-tasks")
        self.assertFalse(self.store.exists("model-tasks"))
        self.assertIsNone(self.store.get("model-tasks"))
        self.assertEqual(self.store.keys(), ["model-quota"])

    def test_db_file_created(self):
        self.assertFalse((self.state_dir / "model-scheduler.db").exists())
        self.store.set("k", json.dumps({"v": 1}))
        self.assertTrue((self.state_dir / "model-scheduler.db").exists())

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.store.get("missing"))
        self.assertFalse(self.store.exists("missing"))
        self.store.delete("missing")

    def test_repeat_set_overwrites(self):
        self.store.set("k", json.dumps({"v": 1}))
        self.store.set("k", json.dumps({"v": 2}))
        self.assertEqual(json.loads(self.store.get("k")), {"v": 2})
        self.assertEqual(self.store.keys(), ["k"])


class CreateStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_json_backend(self):
        store = create_store("json", self.state_dir)
        self.assertIsInstance(store, JsonStateStore)
        self.assertEqual(store.backend_name, "json")

    def test_sqlite_backend(self):
        store = create_store("sqlite", self.state_dir)
        self.assertIsInstance(store, SQLiteStateStore)
        self.assertEqual(store.backend_name, "sqlite")

    def test_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            create_store("redis", self.state_dir)


class StateStoreProtocolTests(unittest.TestCase):
    def test_protocol_attributes(self):
        self.assertTrue(hasattr(StateStore, "get"))
        self.assertTrue(hasattr(StateStore, "set"))
        self.assertTrue(hasattr(StateStore, "delete"))
        self.assertTrue(hasattr(StateStore, "exists"))
        self.assertTrue(hasattr(StateStore, "keys"))


if __name__ == "__main__":
    unittest.main()
