"""Paired cluster-difference tests using only synthetic trusted artifacts."""

from contextlib import closing
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hrv_early_warning.model_comparison import (
    BASELINE, CANDIDATES, METRICS, _paired_bootstrap, _patient_draw_weights,
    compare_saved_models,
)
from hrv_early_warning.modeling import MODEL_VERSION, VARIANTS


HAS_DEPS = all(importlib.util.find_spec(name) is not None for name in ("numpy", "sklearn", "joblib"))


class FixedColumnPipeline:
    """A trusted synthetic predictor; fitting would be an explicit test error."""

    classes_ = (0, 1)

    def __init__(self, column, invalid=False):
        self.column, self.invalid = column, invalid

    def fit(self, *args, **kwargs):
        raise AssertionError("comparison must never fit preprocessing or estimators")

    def predict_proba(self, matrix):
        import numpy as np
        values = matrix[:, self.column]
        return np.column_stack((1 - values, values)) if not self.invalid else np.full((len(matrix), 2), 0.9)


@unittest.skipUnless(HAS_DEPS, "requires scientific model dependencies")
class PairedBootstrapTests(unittest.TestCase):
    def predictions(self, baseline, candidate=None):
        return {BASELINE: baseline, **{name: baseline if candidate is None else candidate for name in CANDIDATES}}

    def test_identical_predictions_have_exact_zero_deltas_and_intervals(self):
        result = _paired_bootstrap([0, 1, 0, 1], self.predictions([0.2, 0.8, 0.4, 0.7]),
                                   [1, 1, 2, 2], replicates=12)
        for comparison in result["comparisons"].values():
            for metric in METRICS:
                self.assertEqual(comparison["delta_point_estimate"][metric], 0)
                self.assertEqual(comparison["delta_confidence_interval_95"][metric], [0, 0])

    def test_all_three_improvement_directions_are_positive(self):
        result = _paired_bootstrap([0, 1, 0, 1], self.predictions([0.6, 0.4] * 2, [0.1, 0.9] * 2),
                                   [1, 1, 2, 2], replicates=10)
        for comparison in result["comparisons"].values():
            delta = comparison["delta_point_estimate"]
            self.assertEqual(delta["AUROC"], 1)
            self.assertEqual(delta["AUPRC"], 0.5)
            self.assertAlmostEqual(delta["Brier"], 0.35)
            self.assertTrue(all(comparison["delta_confidence_interval_95"][name][0] > 0 for name in METRICS))

    def test_patient_draw_assigns_same_multiplicity_to_entire_cluster(self):
        import numpy as np
        class Draw:
            def integers(self, low, high, size):
                self.called = (low, high, size)
                return np.array([0, 0, 2])
        draw = Draw()
        weights = _patient_draw_weights(np.array([0, 0, 1, 1, 1, 2]), 3, draw)
        np.testing.assert_array_equal(weights, [2, 2, 0, 0, 0, 1])
        self.assertEqual(draw.called, (0, 3, 3))

    def test_one_shared_patient_draw_per_replicate_not_per_model(self):
        with patch("hrv_early_warning.model_comparison._patient_draw_weights", wraps=_patient_draw_weights) as draw:
            result = _paired_bootstrap([0, 1, 0, 1], self.predictions([0.4, 0.6] * 2),
                                       [1, 1, 2, 2], replicates=9)
        self.assertEqual(draw.call_count, 9)
        self.assertEqual(result["bootstrap"]["replicates_valid"], 9)

    def test_single_class_draws_are_skipped_for_every_metric(self):
        import numpy as np
        draws = [np.array([2, 2, 0, 0]), np.array([0, 0, 2, 2]), np.array([1, 1, 1, 1])]
        with patch("hrv_early_warning.model_comparison._patient_draw_weights", side_effect=draws):
            result = _paired_bootstrap([0, 0, 1, 1], self.predictions([0.2, 0.4, 0.6, 0.8]),
                                       [1, 1, 2, 2], replicates=3)
        self.assertEqual(result["bootstrap"]["replicates_valid"], 1)
        self.assertEqual(result["bootstrap"]["replicates_skipped_single_class"], 2)
        for comparison in result["comparisons"].values():
            self.assertEqual(comparison["delta_confidence_interval_95"]["Brier"], [0, 0])

    def test_one_class_population_has_no_discrimination_ci(self):
        result = _paired_bootstrap([0, 0], self.predictions([0.2, 0.3]), [1, 2], replicates=4)
        self.assertEqual(result["status"], "insufficient_validation_classes")
        self.assertEqual(result["bootstrap"]["replicates_valid"], 0)
        self.assertEqual(result["bootstrap"]["replicates_skipped_single_class"], 4)
        for comparison in result["comparisons"].values():
            self.assertIsNone(comparison["delta_point_estimate"]["AUROC"])
            self.assertIsNone(comparison["delta_point_estimate"]["AUPRC"])
            self.assertEqual(comparison["delta_point_estimate"]["Brier"], 0)
            self.assertTrue(all(interval is None for interval in comparison["delta_confidence_interval_95"].values()))

    def test_patient_and_row_reordering_preserve_shared_cluster_inference(self):
        y, patients = [0, 1, 0, 0, 1], [3, 3, 9, 9, 9]
        predictions = self.predictions([0.2, 0.7, 0.6, 0.1, 0.4], [0.1, 0.9, 0.3, 0.2, 0.8])
        first = _paired_bootstrap(y, predictions, patients, replicates=15)
        order = [4, 2, 0, 3, 1]
        second = _paired_bootstrap([y[i] for i in order], {name: [p[i] for i in order] for name, p in predictions.items()},
                                   [patients[i] for i in order], replicates=15)
        self.assertEqual(first["bootstrap"], second["bootstrap"])
        for name in CANDIDATES:
            for metric in METRICS:
                self.assertAlmostEqual(first["comparisons"][name]["delta_point_estimate"][metric],
                                       second["comparisons"][name]["delta_point_estimate"][metric], places=12)
                for a, b in zip(first["comparisons"][name]["delta_confidence_interval_95"][metric],
                                second["comparisons"][name]["delta_confidence_interval_95"][metric]):
                    self.assertAlmostEqual(a, b, places=12)

    def test_invalid_bootstrap_inputs_fail(self):
        for count in (0, -1, True, 2.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                _paired_bootstrap([0, 1], self.predictions([0.2, 0.8]), [1, 2], replicates=count)
        for y, p, ids in [([0, -1], [0.2, 0.8], [1, 2]), ([0, 1], [0.2, float("nan")], [1, 2]),
                          ([0, 1], [0.2, 1.1], [1, 2]), ([0, 1], [0.2, 0.8], [1]),
                          ([0, 1], [0.2, 0.8], ["patient", "other"])]:
            with self.subTest(y=y, ids=ids), self.assertRaises(ValueError):
                _paired_bootstrap(y, self.predictions(p), ids, replicates=1)


@unittest.skipUnless(HAS_DEPS, "requires scientific model dependencies")
class SavedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.table = self.root / "model_table.sqlite"
        self.models = self.root / "models"
        self.models.mkdir()
        self.output = self.root / "comparison.json"
        self.source = {"model_version": MODEL_VERSION, "rr_ready": True,
                       "signal": {"status": "complete_verified", "fingerprint": {"synthetic": True}}}
        with closing(sqlite3.connect(self.table)) as connection, connection:
            connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("source_fingerprint", json.dumps(self.source)), ("report", json.dumps({"rr_ready": True}))])
            connection.execute("CREATE TABLE model_rows(subject_id INTEGER,icustay_id INTEGER,landmark TEXT,label TEXT,"
                               "event_time TEXT,split TEXT,current_rr_accepted INTEGER,features TEXT)")
            for split, patients in (("train", (1, 2)), ("validation", (101, 102, 103, 104))):
                for subject in patients:
                    for index, label in enumerate(("negative", "positive", "censored")):
                        point = datetime(2200, 1, 1) + timedelta(hours=6 + 2 * index if label != "censored" else 48)
                        features = {name: None for names in VARIANTS.values() for name in names}
                        features.update(heart_rate=0.4 if label == "positive" else 0.6,
                                        mean_rr_ms_lag0=0.9 if label == "positive" else 0.1)
                        # These rows must not have their feature blobs parsed.
                        raw = json.dumps(features) if split == "validation" and subject != 104 and label != "censored" else "not-json"
                        connection.execute("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?)", (
                            subject, subject * 10 + int(label == "censored"), point.isoformat(), label,
                            "2200-01-01T14:00:00" if label != "censored" else None,
                            split, int(subject != 104), raw))
        self.write_models(improved=True)

    def write_models(self, improved=False):
        import joblib
        for name in (BASELINE, *CANDIDATES):
            feature = "mean_rr_ms_lag0" if improved and name != BASELINE else "heart_rate"
            joblib.dump({"pipeline": FixedColumnPipeline(VARIANTS[name].index(feature)),
                         "features": tuple(VARIANTS[name]), "variant": name,
                         "model_version": MODEL_VERSION, "source_fingerprint": self.source}, self.models / (name + ".joblib"))

    def update_table(self, statement, parameters=()):
        with closing(sqlite3.connect(self.table)) as connection, connection:
            connection.execute(statement, parameters)

    def change_artifact(self, mutate):
        import joblib
        path = self.models / (CANDIDATES[0] + ".joblib")
        artifact = joblib.load(path)
        mutate(artifact)
        joblib.dump(artifact, path)

    def compare(self):
        return compare_saved_models(self.table, self.models, self.output, replicates=8)

    def test_aggregate_same_validation_rows_all_models_no_refit_and_no_ids_written(self):
        result = self.compare()
        self.assertEqual(result["samples"], {"binary_windows": 6, "positive_windows": 3, "negative_windows": 3,
                                             "patients": 3, "positive_event_stays": 3, "positive_event_patients": 3})
        self.assertEqual(result["bootstrap"]["replicates_valid"], 8)
        self.assertEqual(json.loads(self.output.read_text()), result)
        text = self.output.read_text()
        self.assertNotIn("subject_id", text)
        self.assertNotIn("icustay_id", text)
        self.assertNotIn("2200-01-01", text)
        self.assertNotIn(str(self.root), text)
        self.assertIn("No multiple-comparison correction", text)
        for name in CANDIDATES:
            self.assertGreater(result["comparisons"][name]["delta_point_estimate"]["AUROC"], 0)
            self.assertGreater(result["comparisons"][name]["delta_point_estimate"]["Brier"], 0)

    def test_identical_saved_predictions_have_zero_increments(self):
        self.write_models(improved=False)
        result = self.compare()
        for comparison in result["comparisons"].values():
            self.assertEqual(comparison["delta_point_estimate"], dict.fromkeys(METRICS, 0))
            self.assertEqual(comparison["delta_confidence_interval_95"], {metric: [0, 0] for metric in METRICS})

    def test_any_test_row_rejects_before_loading_models(self):
        self.update_table("UPDATE model_rows SET split='test' WHERE subject_id=1")
        with patch("hrv_early_warning.model_comparison._load_saved_models") as loader:
            with self.assertRaisesRegex(ValueError, "rejects_test"):
                self.compare()
        loader.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_patient_overlap_between_train_and_validation_rejects(self):
        self.update_table("UPDATE model_rows SET subject_id=1 WHERE subject_id=101")
        with self.assertRaisesRegex(ValueError, "multiple_splits"):
            self.compare()

    def test_partial_rr_table_rejects_without_loading_models(self):
        self.update_table("UPDATE metadata SET value=? WHERE key='report'", (json.dumps({"rr_ready": False}),))
        with patch("hrv_early_warning.model_comparison._load_saved_models") as loader:
            with self.assertRaisesRegex(ValueError, "verified_complete_rr"):
                self.compare()
        loader.assert_not_called()

    def test_joblib_source_fingerprint_mismatch_rejects(self):
        self.change_artifact(lambda artifact: artifact.update(source_fingerprint={"wrong": True}))
        with self.assertRaisesRegex(ValueError, "source_fingerprint_mismatch"):
            self.compare()
        self.assertFalse(self.output.exists())

    def test_feature_order_or_variant_mismatch_rejects(self):
        self.change_artifact(lambda artifact: artifact.update(features=tuple(reversed(artifact["features"]))))
        with self.assertRaisesRegex(ValueError, "variant_features_or_version_mismatch"):
            self.compare()

    def test_missing_model_rejects_instead_of_comparing_partial_candidates(self):
        (self.models / (CANDIDATES[0] + ".joblib")).unlink()
        with self.assertRaisesRegex(ValueError, "all_completed_paired"):
            self.compare()

    def test_wrong_class_order_and_invalid_probabilities_reject(self):
        self.change_artifact(lambda artifact: setattr(artifact["pipeline"], "classes_", (1, 0)))
        with self.assertRaisesRegex(ValueError, "class_order_mismatch"):
            self.compare()
        self.write_models(improved=True)
        self.change_artifact(lambda artifact: setattr(artifact["pipeline"], "invalid", True))
        with self.assertRaisesRegex(ValueError, "invalid_probability_output"):
            self.compare()

    def test_duplicate_validation_identity_rejects(self):
        self.update_table("INSERT INTO model_rows SELECT * FROM model_rows WHERE subject_id=101 AND label='negative'")
        with self.assertRaisesRegex(ValueError, "duplicate_or_inconsistent"):
            self.compare()

    def test_no_accepted_binary_validation_rows_rejects(self):
        self.update_table("UPDATE model_rows SET current_rr_accepted=0 WHERE split='validation'")
        with self.assertRaisesRegex(ValueError, "no_paired_validation_binary_rows"):
            self.compare()

    def test_positive_missing_event_and_nonfinite_feature_reject(self):
        self.update_table("UPDATE model_rows SET event_time=NULL WHERE subject_id=101 AND label='positive'")
        with self.assertRaisesRegex(ValueError, "missing_event"):
            self.compare()
        self.update_table("UPDATE model_rows SET event_time='2200-01-01T14:00:00' WHERE subject_id=101 AND label='positive'")
        self.update_table("UPDATE model_rows SET features=? WHERE subject_id=101 AND label='negative'", (json.dumps({"heart_rate": float("nan")}),))
        with self.assertRaisesRegex(ValueError, "nonfinite_or_nonnumeric"):
            self.compare()

    def test_malformed_or_date_only_landmark_and_event_clocks_reject(self):
        for field, original in (("landmark", "2200-01-01T08:00:00"), ("event_time", "2200-01-01T14:00:00")):
            for value in ("not-a-time", "2200-01-01", "2200-13-01T08:00:00"):
                with self.subTest(field=field, value=value):
                    self.update_table(f"UPDATE model_rows SET {field}=? WHERE subject_id=101 AND label='positive'", (value,))
                    with self.assertRaisesRegex(ValueError, "invalid_paired_row_clock"):
                        self.compare()
                    self.update_table(f"UPDATE model_rows SET {field}=? WHERE subject_id=101 AND label='positive'", (original,))
        self.update_table("UPDATE model_rows SET event_time='not-a-time' WHERE subject_id=101 AND label='negative'")
        with self.assertRaisesRegex(ValueError, "invalid_paired_row_clock"):
            self.compare()

    def test_positive_event_must_be_strictly_after_landmark_and_within_horizon(self):
        for event in ("2199-12-31T12:00:00", "2200-01-01T08:00:00", "2200-01-01T14:00:00.000001", "2200-01-10T00:00:00"):
            with self.subTest(event=event):
                self.update_table("UPDATE model_rows SET event_time=? WHERE subject_id=101 AND label='positive'", (event,))
                with self.assertRaisesRegex(ValueError, "positive_event_outside_comparison_horizon"):
                    self.compare()
        # The fixture's 14:00 event at the 08:00 landmark is the inclusive bound.
        self.update_table("UPDATE model_rows SET event_time='2200-01-01T14:00:00' WHERE subject_id=101 AND label='positive'")
        self.assertEqual(self.compare()["samples"]["positive_windows"], 3)

    def test_negative_event_if_present_must_be_after_horizon(self):
        for event in ("2199-12-31T12:00:00", "2200-01-01T06:00:00", "2200-01-01T11:00:00", "2200-01-01T12:00:00"):
            with self.subTest(event=event):
                self.update_table("UPDATE model_rows SET event_time=? WHERE subject_id=101 AND label='negative'", (event,))
                with self.assertRaisesRegex(ValueError, "negative_event_within_or_before_comparison_horizon"):
                    self.compare()
        self.update_table("UPDATE model_rows SET event_time='2200-01-01T12:00:00.000001' WHERE subject_id=101 AND label IN ('positive','negative')")
        self.assertEqual(self.compare()["samples"]["binary_windows"], 6)

    def test_same_icu_requires_one_event_time_including_null_consistency(self):
        for event in (None, "2200-01-01T13:00:00"):
            with self.subTest(event=event):
                self.update_table("UPDATE model_rows SET event_time=? WHERE subject_id=101 AND label='negative'", (event,))
                with self.assertRaisesRegex(ValueError, "inconsistent_event_time_within_icu"):
                    self.compare()

    def test_same_icu_cannot_change_subject(self):
        self.update_table("UPDATE model_rows SET subject_id=105 WHERE subject_id=101 AND label='positive'")
        with self.assertRaisesRegex(ValueError, "duplicate_or_inconsistent_paired_row_identity"):
            self.compare()

    def test_mixed_naive_aware_clocks_reject_and_equivalent_event_timezones_agree(self):
        self.update_table("UPDATE model_rows SET event_time='2200-01-01T14:00:00+00:00' WHERE subject_id=101 AND label='positive'")
        with self.assertRaisesRegex(ValueError, "invalid_paired_row_clock"):
            self.compare()
        self.update_table("UPDATE model_rows SET landmark=landmark || '+00:00', event_time='2200-01-01T14:00:00+00:00' WHERE subject_id=101 AND label IN ('positive','negative')")
        self.update_table("UPDATE model_rows SET event_time='2200-01-01T22:00:00+08:00' WHERE subject_id=101 AND label='positive'")
        self.assertEqual(self.compare()["samples"]["positive_event_stays"], 3)

    def test_output_cannot_replace_input_or_original_training_report(self):
        for output in (self.table, self.models / "development_report.json", self.table.with_suffix(".summary.json")):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "separate_json"):
                compare_saved_models(self.table, self.models, output, replicates=1)


if __name__ == "__main__":
    unittest.main()
