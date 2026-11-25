"""The quality gate scores authorized recordings/predictions, never synthetic quality claims."""
import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_english as benchmark


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"schema_version": 1, "fixtures": [
            {"id": "speech", "duration": 12, "reference": "Alice paid 42 dollars", "entities": ["Alice", "42"],
             "tags": ["clean", "quiet", "noisy", "names_numbers"],
             "words": [{"w": "Alice", "s": 1, "e": 2}, {"w": "paid", "s": 2, "e": 3},
                       {"w": "42", "s": 3, "e": 4}, {"w": "dollars", "s": 4, "e": 5}],
             "positive_spans": [{"start": 0, "end": 6}], "silence_spans": [{"start": 8, "end": 12}]},
            {"id": "negative", "duration": 8, "reference": "", "words": [], "entities": [],
             "tags": ["negative"], "positive_spans": [], "silence_spans": [{"start": 0, "end": 8}]}]}
        meta = {"provider": "OpenRouter", "model": "saved-model", "recognizer": "parakeet-en-int8",
                "hardware": "same-machine", "concurrency": 2, "cache_state": "warm"}
        speech = {"id": "speech", "words": copy.deepcopy(self.manifest["fixtures"][0]["words"]),
                  "clips": [{"start": 0, "end": 6, "human_hook_ok": True, "human_standalone_ok": True}],
                  "seconds_to_first_useful_clip": 3.5, "elapsed_seconds": 8, "cpu_peak_percent": 80,
                  "memory_peak_mb": 500}
        negative = {"id": "negative", "words": [], "clips": [], "seconds_to_first_useful_clip": None,
                    "elapsed_seconds": 2, "cpu_peak_percent": 20, "memory_peak_mb": 480}
        self.baseline = {"name": "baseline", "metadata": meta, "fixtures": [speech, negative]}
        self.candidate = copy.deepcopy(self.baseline)
        self.candidate["name"] = "candidate"

    def evaluate(self):
        return benchmark.evaluate(self.manifest, [self.baseline, self.candidate])

    def test_equal_measured_runs_pass_without_claiming_performance_improvement(self):
        result = self.evaluate()
        self.assertEqual(result["gate"], "pass")
        self.assertEqual(result["runs"]["baseline"]["wer"], 0)
        self.assertEqual(result["runs"]["candidate"]["selection_precision"], 1)
        self.assertIn("manifest_sha256", result)
        self.assertEqual(self.baseline["fixtures"][0]["words"][0]["w"], "Alice")

    def test_wrong_words_entities_and_silence_hallucinations_block_promotion(self):
        words = self.candidate["fixtures"][0]["words"]
        words[0]["w"] = "Bob"
        words[2]["w"] = "24"
        words.append({"w": "imaginary", "s": 9, "e": 10})
        result = self.evaluate()
        self.assertEqual(result["gate"], "fail")
        self.assertGreater(result["runs"]["candidate"]["wer"], 0)
        self.assertEqual(result["runs"]["candidate"]["entity_errors"], 2)
        self.assertEqual(result["runs"]["candidate"]["silence_words"], 1)

    def test_negative_duplicate_and_incomplete_clips_are_counted(self):
        self.candidate["fixtures"][0]["clips"] += [
            {"start": 1, "end": 5, "human_hook_ok": False, "human_standalone_ok": False}]
        self.candidate["fixtures"][1]["clips"] = [{"start": 0, "end": 4, "human_hook_ok": False, "human_standalone_ok": False}]
        result = self.evaluate()
        candidate = result["runs"]["candidate"]
        self.assertEqual(result["gate"], "fail")
        self.assertEqual(candidate["negative_clips"], 1)
        self.assertEqual(candidate["duplicate_clips"], 1)
        self.assertEqual(candidate["incomplete_clips"], 2)

    def test_missing_annotations_resources_and_coverage_are_not_a_pass(self):
        self.candidate["fixtures"][0]["clips"][0].pop("human_hook_ok")
        self.candidate["fixtures"][0].pop("cpu_peak_percent")
        self.manifest["fixtures"][0]["tags"] = ["clean"]
        result = self.evaluate()
        self.assertEqual(result["gate"], "not_ready")
        self.assertTrue(any("annotations" in reason for reason in result["reasons"]))
        self.assertTrue(any("coverage" in reason for reason in result["reasons"]))

    def test_provider_model_hardware_and_cache_must_be_held_fixed(self):
        for key in ("provider", "model", "recognizer", "hardware", "concurrency", "cache_state"):
            with self.subTest(key=key):
                candidate = copy.deepcopy(self.candidate)
                candidate["metadata"][key] = "different"
                with self.assertRaises(ValueError):
                    benchmark.evaluate(self.manifest, [self.baseline, candidate])

    def test_nan_out_of_bounds_and_duplicate_fixture_ids_are_rejected(self):
        for value in (math.nan, math.inf, -1, 15):
            with self.subTest(value=value):
                bad = copy.deepcopy(self.candidate)
                bad["fixtures"][0]["words"][0]["e"] = value
                with self.assertRaises(ValueError):
                    benchmark.evaluate(self.manifest, [self.baseline, bad])
        self.candidate["fixtures"].append(copy.deepcopy(self.candidate["fixtures"][0]))
        with self.assertRaises(ValueError):
            self.evaluate()

    def test_first_useful_clip_and_resources_cannot_regress_without_failing(self):
        self.candidate["fixtures"][0]["seconds_to_first_useful_clip"] = 7
        self.candidate["fixtures"][0]["memory_peak_mb"] = 800
        self.assertEqual(self.evaluate()["gate"], "fail")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BenchmarkTests))
    if result.wasSuccessful():
        for name in unittest.defaultTestLoader.getTestCaseNames(BenchmarkTests):
            print("PASS " + name)
    sys.exit(0 if result.wasSuccessful() else 1)
