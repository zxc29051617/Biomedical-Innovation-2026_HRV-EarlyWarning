import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hrv_early_warning import incremental_value as controls


def features(offset=0, missing=(2, 4)):
    raw = {"age_years": 55, "age_obscured": 0, "heart_rate": 80 + offset,
           "map": 75, "respiratory_rate": 18, "spo2": 98}
    for lag in range(6):
        for name, value in (("mean_rr_ms", 800 + lag * 20 + offset),
                            ("sdrr_ms", 30 + offset), ("rmssd_rr_ms", 20 + offset)):
            raw[f"{name}_lag{lag}"] = None if lag in missing else value
    raw["rr_valid_count"] = 6 - len(missing)
    return raw


def database(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE model_rows (subject_id INTEGER, icustay_id INTEGER, landmark TEXT, label TEXT, event_time TEXT, monitoring_seconds REAL, features TEXT, split TEXT, current_rr_accepted INTEGER)")
    connection.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
    connection.executemany("INSERT INTO metadata VALUES (?,?)", [
        ("source_fingerprint", json.dumps({"synthetic_fixture": True})),
        ("report", json.dumps({"rr_ready": True})),
    ])
    for split, base, count in (("train", 100, 16), ("validation", 200, 8)):
        for patient in range(count):
            for hour in (6, 7):
                label = "positive" if patient % 2 else "negative"
                raw = features(patient % 5, missing=(2, 4) if patient % 3 else ())
                # Hold validation-only extremes out of fitted preprocessing.
                raw["heart_rate"] = (1000 + patient if split == "validation" else
                                     None if patient % 3 == 0 else 75 + patient)
                for name in controls.RR_FEATURES:
                    raw[f"{name}_slope_per_hour"] = controls.slope([raw[f"{name}_lag{i}"] for i in range(6)])
                connection.execute("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?,?)", (
                    base + patient, base + patient, f"2020-01-01T{hour:02}:00:00", label,
                    "2020-01-01T09:00:00" if label == "positive" else None,
                    3600, json.dumps(raw), split, 1))
    connection.commit()
    return connection


class ControlFeatureTests(unittest.TestCase):
    def test_nested_design_keeps_same_rate_and_availability_in_variability_models(self):
        for a, b in controls.CONTRASTS:
            self.assertTrue(set(controls.VARIANTS[a]) < set(controls.VARIANTS[b]))
        self.assertFalse(any("sdrr" in name or "rmssd" in name for name in controls.VARIANTS["ecg_rate_history"]))

    def test_rate_conversion_missingness_and_past_only_slope(self):
        raw = features()
        raw["future_heart_rate"] = 99999
        raw["mean_rr_ms_lag_minus_1"] = 1
        result = controls.controlled_features(raw)
        self.assertEqual(result["ecg_rate_from_mean_rr_bpm_lag0"], 75)
        self.assertIsNone(result["ecg_rate_from_mean_rr_bpm_lag2"])
        self.assertEqual(result["rr_available_lag2"], 0)
        self.assertEqual(result["rr_valid_count"], 4)
        self.assertEqual(result["rr_slope_available"], 1)
        self.assertAlmostEqual(result["mean_rr_ms_slope_per_hour"], -20)
        self.assertNotIn("future_heart_rate", result)

    def test_slope_requires_three_actual_samples(self):
        self.assertIsNone(controls.slope([800, None, 900, None, None, None]))
        self.assertAlmostEqual(controls.slope([800, None, 840, None, 880, None]), -20)

    def test_partial_epoch_or_inconsistent_count_is_rejected(self):
        for key, value in (("sdrr_ms_lag0", None), ("rr_valid_count", 6), ("rr_valid_count", True),
                           ("mean_rr_ms_lag0", 0), ("rmssd_rr_ms_lag0", -1),
                           ("heart_rate", float("inf")), ("map", True)):
            with self.subTest(key=key, value=value):
                raw = features()
                raw[key] = value
                with self.assertRaises(ValueError):
                    controls.controlled_features(raw)

    def test_current_quality_required_even_with_good_history(self):
        with self.assertRaisesRegex(ValueError, "current_accepted"):
            controls.controlled_features(features(missing=(0,)))

    def test_spec_file_exactly_matches_declared_analysis(self):
        path = Path(__file__).resolve().parents[1] / "configs/rr_incremental_controls.json"
        self.assertEqual(json.loads(path.read_text()), controls.SPEC)

    def test_test_contamination_is_rejected_before_feature_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection = database(Path(tmp) / "table.sqlite")
            connection.execute("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?,?)", (999, 999, "2020-01-01T06:00:00", "positive", None, 3600, "DO NOT PARSE", "test", 1))
            traces = []
            connection.set_trace_callback(traces.append)
            with self.assertRaisesRegex(ValueError, "containing_test"):
                list(controls.iter_control_rows(connection, "train"))
            self.assertFalse(any("SELECT subject_id" in sql for sql in traces))
            with self.assertRaisesRegex(ValueError, "forbid_test"):
                list(controls.iter_control_rows(connection, "test"))
            connection.close()

    def test_rejected_rows_never_have_features_deserialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection = database(Path(tmp) / "table.sqlite")
            connection.execute("UPDATE model_rows SET current_rr_accepted=0, features='DO NOT PARSE' WHERE subject_id=100")
            rows = list(controls.iter_control_rows(connection, "train"))
            self.assertEqual(len(rows), 30)
            self.assertTrue(all(row["subject_id"] != 100 for row, _ in rows))
            connection.close()

    def test_output_cannot_overwrite_source_or_previous_result(self):
        with self.assertRaisesRegex(ValueError, "ignored_artifacts"):
            controls.safe_output(controls.ROOT / "artifacts/m2")
        with tempfile.TemporaryDirectory() as tmp, patch.object(controls, "OUTPUT_ROOT", Path(tmp)):
            occupied = Path(tmp) / "existing"
            occupied.mkdir()
            (occupied / "plan.json").write_text("preserved")
            with self.assertRaises(FileExistsError):
                controls.safe_output(occupied)
            link = Path(tmp) / "link"
            link.symlink_to(occupied)
            with self.assertRaisesRegex(ValueError, "symlink"):
                controls.safe_output(link)


SCIENTIFIC = all(importlib.util.find_spec(name) for name in ("numpy", "scipy", "sklearn", "joblib", "threadpoolctl"))


@unittest.skipUnless(SCIENTIFIC, "Scientific dependencies required; full fit is tested in CI")
class ControlFitTests(unittest.TestCase):
    def test_reopened_table_is_revalidated_before_any_fit_feature_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database(root / "model_table.sqlite").close()
            plan = {"dependencies": {"numpy": True}, "provenance": {
                "model_table_sha256": "changed", "source_fingerprint_sha256": "changed"}}
            def metadata(connection, work, feasibility):
                self.assertTrue(connection.in_transaction)
                return {"synthetic_fixture": True}, {}, {}
            with patch.object(controls, "OUTPUT_ROOT", root / "controls"), patch.object(controls, "preflight", return_value=plan), patch.object(controls, "_validate_metadata", side_effect=metadata), patch.object(controls, "iter_control_rows", side_effect=AssertionError("Must not read features")):
                with self.assertRaisesRegex(ValueError, "source_changed_before_control_feature_query"):
                    controls.run_controls(root, root, root / "unused.json", root / "controls/run", fit=True)

    def test_original_baseline_reproduction_and_training_only_preprocessing(self):
        import joblib
        import numpy as np
        from hrv_early_warning.modeling import train_models
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "model_table.sqlite"
            database(path).close()
            original = train_models(path, root / "original", bootstrap_replicates=2)
            reference = original["models"]["paired_vitals"]
            plan = {"status": "preflight_complete_no_models_fitted", "specification": controls.SPEC,
                    "provenance": {"model_table_sha256": controls.hash_file(path),
                                   "source_fingerprint_sha256": hashlib.sha256(json.dumps({"synthetic_fixture": True}, sort_keys=True).encode()).hexdigest()},
                    "dependencies": {"numpy": True}, "same_paired_population": reference["data"],
                    "reference_routine_metrics": {key: reference["validation"][key] for key in ("AUROC", "AUPRC", "Brier")},
                    "reference_routine_threshold": reference["validation_alarms"]["threshold"]}
            with patch.object(controls, "OUTPUT_ROOT", root / "controls"), patch.object(controls, "preflight", return_value=plan), patch.object(controls, "_validate_metadata", return_value=({"synthetic_fixture": True}, {}, {})):
                result = controls.run_controls(root, root, root / "unused.json", root / "controls/run", fit=True)
            self.assertEqual(result["status"], "exploratory_control_comparison_complete")
            self.assertTrue(result["frozen_routine_reference_reproduced"])
            self.assertEqual(len(result["paired_metrics"]["comparisons"]), 4)
            for name in controls.VARIANTS:
                model = joblib.load(root / "controls/run" / (name + ".joblib"))
                imputer = model["pipeline"].named_steps["imputer"]
                base_imputer = imputer if name == "routine" else imputer.named_transformers_["base"]
                imputed_heart_rate = base_imputer.statistics_[2]
                self.assertLess(imputed_heart_rate, 100)  # Validation is 1000+.
                self.assertEqual(model["pipeline"].named_steps["logistic"].class_weight, None)
                if name != "routine":
                    rr_imputer = imputer.named_transformers_["rr"]
                    self.assertFalse(rr_imputer.add_indicator)
                    # Adding variability cannot duplicate history availability masks.
                    self.assertEqual(len(rr_imputer.statistics_), len(controls.VARIANTS[name]) - len(controls.BASE_FEATURES))
            self.assertEqual(result["paired_metrics"]["replicates_requested"], 200)
            self.assertGreater(result["paired_metrics"]["replicates_valid"], 0)

    def test_identical_predictions_have_zero_shared_patient_delta(self):
        import numpy as np
        y = np.asarray([0, 1, 0, 1, 0, 1])
        p = np.asarray([.2, .7, .3, .6, .4, .8])
        result = controls.shared_intervals(y, {name: p for name in controls.VARIANTS}, [1, 1, 2, 2, 3, 3])
        for contrast in result["comparisons"].values():
            self.assertEqual(set(contrast["point_improvement"].values()), {0.0})
            self.assertTrue(all(value == [0.0, 0.0] for value in contrast["interval_95"].values()))


if __name__ == "__main__":
    unittest.main()
