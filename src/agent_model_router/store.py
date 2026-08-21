"""agent_model_router.store — StateStore 抽象与 JSON / SQLite 双后端。

v0.6 起，TaskStore 不再直接读写 JSON 文件，而是通过 StateStore 统一
接口持久化。value 一律为 JSON 字符串，由调用方负责序列化/反序列化。

- JsonStateStore：兼容现有 ``state_dir/<key>.json`` 文件布局；写盘复用
  ``policy.atomic_write_json``（tmp + fsync + os.replace），线程安全
  （仅单进程内安全；多进程请使用 SQLite 后端）。
- SQLiteStateStore：纯 stdlib sqlite3，文件为 ``state_dir/model-scheduler.db``，
  key-value 表结构，线程锁 + 事务。SQLite 自带文件锁，多进程安全
  （写事务串行化；并发写会按 busy timeout 等待）。
- create_store：按 backend 名创建后端，"json"（默认）/"sqlite"。

本模块 import 不产生任何 I/O 副作用（不创建目录/文件）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .policy import atomic_write_json, default_state_dir

logger = logging.getLogger(__name__)

TASKS_KEY = "model-tasks"


class StateStore(Protocol):
    """状态存储统一接口。value 一律 JSON 字符串。"""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def keys(self) -> list[str]: ...

    def atomic_update(self, key: str, mutator: Callable[[Any], Any]) -> Any:
        """读当前值 -> mutator(value) -> 写回，整个读改写在一个事务/锁内完成。

        ``value`` 为后端存储的 JSON 字符串；key 不存在时为 None。
        ``mutator`` 返回 None 表示不写回（保持原值不变）。
        """
        ...


def _validate_key(key: str) -> str:
    """规范化并校验 key；禁止路径分隔符，避免写出 state_dir。"""
    key = str(key or "").strip()
    if not key:
        raise ValueError("key must be a non-empty string")
    if Path(key).name != key or key in (".", ".."):
        raise ValueError(f"invalid key: {key!r}")
    return key


def _validate_json_value(value: str) -> None:
    """确保 value 是 JSON 字符串（无效时抛 ValueError）。"""
    if not isinstance(value, str):
        raise TypeError("value must be a JSON string")
    try:
        json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value must be a JSON string: {exc}") from exc


class JsonStateStore:
    """JSON 文件后端：一个 key 对应 state_dir 下一个 ``<key>.json`` 文件。

    文件内容为 value 反序列化后的 JSON 文档（与现有 model-tasks.json 等
    文件格式兼容）。set 会先 json.loads(value) 验证，再交给
    ``policy.atomic_write_json`` 原子落盘。
    """

    backend_name = "json"

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        return self.state_dir / f"{_validate_key(key)}.json"

    def get(self, key: str) -> str | None:
        """读取 key 对应的 JSON 值（紧凑 JSON 字符串）；文件不存在返回 None。"""
        path = self._path(key)
        with self._lock:
            if not path.exists():
                return None
            try:
                text = path.read_text(encoding="utf-8")
                return json.dumps(json.loads(text), ensure_ascii=False)
            except Exception:
                logger.warning("Failed to read state file %s", path, exc_info=True)
                return None

    def set(self, key: str, value: str) -> None:
        """写入 value 对应的 JSON 文档（原子写）。"""
        path = self._path(key)
        _validate_json_value(value)
        data = json.loads(value)
        with self._lock:
            atomic_write_json(path, data)

    def delete(self, key: str) -> None:
        """删除对应 JSON 文件；文件不存在时静默成功。"""
        path = self._path(key)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def exists(self, key: str) -> bool:
        path = self._path(key)
        with self._lock:
            return path.exists()

    def keys(self) -> list[str]:
        """返回 state_dir 下所有 JSON 文件名（不含 .json 后缀）。"""
        with self._lock:
            if not self.state_dir.exists():
                return []
            return sorted(
                p.name[: -len(".json")]
                for p in self.state_dir.glob("*.json")
                if p.name.endswith(".json")
            )

    def atomic_update(self, key: str, mutator: Callable[[Any], Any]) -> Any:
        """在单进程锁内完成读改写；返回 mutator 的结果。

        当前值为 JSON 字符串（key 不存在时为 None）。mutator 返回 None
        表示不写回；返回其它值时必须是合法 JSON 字符串，按 set 落盘。
        """
        path = self._path(key)
        with self._lock:
            current = None
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8")
                    current = json.dumps(json.loads(text), ensure_ascii=False)
                except Exception:
                    logger.warning("Failed to read state file %s", path, exc_info=True)
                    current = None
            new_value = mutator(current)
            if new_value is None:
                return None
            _validate_json_value(new_value)
            atomic_write_json(path, json.loads(new_value))
            return new_value


class SQLiteStateStore:
    """SQLite 文件后端：state_dir/model-scheduler.db 的 kv 表。

    线程锁 + 显式事务（BEGIN IMMEDIATE + COMMIT/ROLLBACK）。SQLite
    自带文件锁，因此多进程可安全并发读写；写事务串行化，busy 等待
    时间由 timeout 控制。SQLite 是全新存储，不迁移既有 JSON 文件内容。
    """

    backend_name = "sqlite"

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self.path = self.state_dir / "model-scheduler.db"
        self._lock = threading.Lock()
        self._timeout = 30.0

    def _connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL)")
        return conn

    def get(self, key: str) -> str | None:
        """读取 kv 表中的 JSON 字符串；不存在返回 None。"""
        key = _validate_key(key)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
                return None if row is None else row[0]
            finally:
                conn.close()

    def set(self, key: str, value: str) -> None:
        """写入/覆盖 kv 表中的 JSON 字符串（显式事务，写后 commit）。"""
        key = _validate_key(key)
        _validate_json_value(value)
        updated_at = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, updated_at),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def delete(self, key: str) -> None:
        """删除 key；key 不存在时静默成功。"""
        key = _validate_key(key)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM kv WHERE key = ?", (key,))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def exists(self, key: str) -> bool:
        key = _validate_key(key)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT 1 FROM kv WHERE key = ?", (key,)).fetchone()
                return row is not None
            finally:
                conn.close()

    def keys(self) -> list[str]:
        """返回 kv 表中所有 key（升序）。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT key FROM kv ORDER BY key").fetchall()
                return [row[0] for row in rows]
            finally:
                conn.close()

    def atomic_update(self, key: str, mutator: Callable[[Any], Any]) -> Any:
        """在 SQLite 写事务（BEGIN IMMEDIATE）内完成读改写；跨进程安全。

        当前值为 JSON 字符串（key 不存在时为 None）。mutator 返回 None
        表示不写回；返回其它值时必须是合法 JSON 字符串，按 set 落盘。
        """
        key = _validate_key(key)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
                current = None if row is None else row[0]
                new_value = mutator(current)
                if new_value is None:
                    conn.execute("COMMIT")
                    return None
                _validate_json_value(new_value)
                updated_at = time.time()
                conn.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, new_value, updated_at),
                )
                conn.execute("COMMIT")
                return new_value
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()


def create_store(backend: str, state_dir: str | Path | None = None) -> StateStore:
    """创建状态存储后端。

    backend="json"（默认）→ JsonStateStore；backend="sqlite" →
    SQLiteStateStore；未知 backend 抛 ValueError。
    """
    backend = str(backend or "").strip().lower()
    if backend == "json":
        return JsonStateStore(state_dir)
    if backend == "sqlite":
        return SQLiteStateStore(state_dir)
    raise ValueError(f"unknown store backend: {backend!r}")


__all__ = [
    "StateStore",
    "JsonStateStore",
    "SQLiteStateStore",
    "create_store",
]
