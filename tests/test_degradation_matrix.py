import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_model_router.scheduler import DEGRADATION_MATRIX, decide_action


class DegradationMatrixTests(unittest.TestCase):
    def test_matrix_covers_five_error_categories(self):
        self.assertEqual(DEGRADATION_MATRIX["invalid_payload"], "abort")
        self.assertEqual(DEGRADATION_MATRIX["auth_error"], "abort")
        self.assertEqual(DEGRADATION_MATRIX["invalid_request"], "abort")
        self.assertEqual(DEGRADATION_MATRIX["rate_limit"], "cooldown_retry")
        self.assertEqual(DEGRADATION_MATRIX["server_error"], "retry_then_fallback")
        self.assertEqual(DEGRADATION_MATRIX["transport_error"], "retry_then_fallback")
        self.assertEqual(DEGRADATION_MATRIX["timeout"], "retry_then_fallback")
        self.assertEqual(DEGRADATION_MATRIX["model_not_found"], "fallback")

    def test_decide_action_returns_expected_action(self):
        for error_type, action in DEGRADATION_MATRIX.items():
            with self.subTest(error_type=error_type):
                self.assertEqual(decide_action(error_type), action)

    def test_decide_action_unknown_is_abort(self):
        self.assertEqual(decide_action("unknown_error"), "abort")
        self.assertEqual(decide_action(""), "abort")
        self.assertEqual(decide_action(None), "abort")


if __name__ == "__main__":
    unittest.main()
