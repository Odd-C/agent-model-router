import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler import ModelRouter, recommend_for_session


class SessionIdPassthroughTests(unittest.TestCase):
    """session_id 透传语义测试（PR #7146 核心缺口）。"""

    def _base_kwargs(self, **overrides):
        kwargs = {
            "session_text": "帮我写一个 Python 脚本",
            "message_count": 2,
            "now": datetime(2026, 1, 1, 10, 0),
            "quota_snapshot": {"claude-3-5-sonnet@anthropic": 10},
        }
        kwargs.update(overrides)
        return kwargs

    def test_recommend_with_session_id_contains_it(self):
        rec = recommend_for_session(**self._base_kwargs(session_id="sess-123"))
        self.assertEqual(rec["session_id"], "sess-123")

    def test_session_id_appended_last(self):
        rec = recommend_for_session(**self._base_kwargs(session_id="sess-123"))
        self.assertEqual(
            list(rec.keys()),
            [
                "difficulty",
                "urgent",
                "message_count",
                "peak",
                "model",
                "provider",
                "reason",
                "tier",
                "cost",
                "key",
                "session_id",
            ],
        )

    def test_without_session_id_field_absent(self):
        rec = recommend_for_session(**self._base_kwargs())
        self.assertNotIn("session_id", rec)

    def test_blank_session_ids_omitted(self):
        for blank in ("", "   ", "\t\n"):
            with self.subTest(session_id=blank):
                rec = recommend_for_session(**self._base_kwargs(session_id=blank))
                self.assertNotIn("session_id", rec)
        rec = recommend_for_session(**self._base_kwargs(session_id=None))
        self.assertNotIn("session_id", rec)

    def test_session_id_does_not_change_route(self):
        rec_with = recommend_for_session(**self._base_kwargs(session_id="sess-123"))
        rec_without = recommend_for_session(**self._base_kwargs())
        for key in (
            "difficulty",
            "urgent",
            "message_count",
            "peak",
            "model",
            "provider",
            "reason",
            "tier",
            "cost",
            "key",
        ):
            self.assertEqual(rec_with[key], rec_without[key], key)

    def test_module_function_and_router_method_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter(state_dir=Path(tmp))
            kwargs = self._base_kwargs(session_id="sess-123")
            rec_mod = recommend_for_session(**kwargs)
            rec_router = router.recommend_for_session(**kwargs)
            for key in (
                "difficulty",
                "urgent",
                "message_count",
                "peak",
                "model",
                "provider",
                "reason",
                "tier",
                "cost",
                "key",
                "session_id",
            ):
                self.assertEqual(rec_mod[key], rec_router[key], key)


if __name__ == "__main__":
    unittest.main()
