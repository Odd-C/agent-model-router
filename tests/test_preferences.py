import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_model_router.preferences import DEFAULT_WEIGHTS, Preferences, PreferencesStore


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = PreferencesStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_returns_defaults_when_no_file(self):
        prefs = self.store.load()
        self.assertEqual(prefs.mode, "balanced")
        self.assertEqual(prefs.weights, {})
        self.assertEqual(self.store.get_effective_weights(), DEFAULT_WEIGHTS["balanced"])

    def test_set_mode_persists(self):
        self.store.set_mode("cost-first")
        self.assertEqual(self.store.load().mode, "cost-first")

        data = json.loads((self.state_dir / "preferences.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "cost-first")

    def test_weights_override_effective_weights(self):
        self.store.set_mode("latency-first")
        self.store.save(Preferences(mode="latency-first", weights={"cost_penalty": 2.5, "failure_risk": 0.5}))

        effective = self.store.get_effective_weights()
        self.assertEqual(effective["cost_penalty"], 2.5)
        self.assertEqual(effective["failure_risk"], 0.5)
        self.assertEqual(effective["quality_fit"], 1.0)
        self.assertEqual(effective["latency_penalty"], 3.0)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            Preferences(mode="turbo", weights={})
        with self.assertRaises(ValueError):
            self.store.set_mode("turbo")

    def test_invalid_weight_key_or_value_raises(self):
        with self.assertRaises(ValueError):
            Preferences(mode="balanced", weights={"not_a_weight": 1.0})
        with self.assertRaises(ValueError):
            Preferences(mode="balanced", weights={"quality_fit": 0})
        with self.assertRaises(ValueError):
            Preferences(mode="balanced", weights={"quality_fit": -1.0})
        with self.assertRaises(ValueError):
            Preferences(mode="balanced", weights={"quality_fit": True})

    def test_persistence_roundtrip(self):
        self.store.save(Preferences(mode="quality-first", weights={"deadline_pressure": 2.0}))
        store2 = PreferencesStore(self.state_dir)
        prefs = store2.load()
        self.assertEqual(prefs.mode, "quality-first")
        self.assertEqual(prefs.weights["deadline_pressure"], 2.0)

    def test_from_dict_rejects_non_dict_weights(self):
        for bad_weights in (["quality_fit", 2.0], "quality_fit", 42, True):
            with self.subTest(bad_weights=bad_weights):
                with self.assertRaises(ValueError):
                    Preferences.from_dict({"mode": "balanced", "weights": bad_weights})
        # 缺省 weights 仍合法。
        prefs = Preferences.from_dict({"mode": "balanced"})
        self.assertEqual(prefs.weights, {})


if __name__ == "__main__":
    unittest.main()
