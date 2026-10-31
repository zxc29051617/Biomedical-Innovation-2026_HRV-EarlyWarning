"""Synthetic tests of no-test access, temporal features and fixed evaluation."""

from dataclasses import asdict
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hrv_early_warning.cohort import StudyStay
from hrv_early_warning.modeling import (
    BASE_FEATURES, CURRENT_FEATURES, HISTORY_FEATURES, MODEL_VERSION, RR_FEATURES, TREND_FEATURES,
    alarm_metrics, build_model_table, load_stay_epochs, patient_bootstrap_metrics,
    rr_features_at_landmark, train_models,
)
from hrv_early_warning.risksets import StayOutcome, ensure_patient_split
from hrv_early_warning.vasopressors import FirstUseResult


T0 = datetime(2200, 1, 1)
HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None


def rr(value):
    return {"mean_rr_ms": value, "sdrr_ms": value / 10, "rmssd_rr_ms": value / 20}


def alarm_row(icu, hour, label, event_hour=None, seconds=3600):
    return {"icustay_id": icu, "landmark": T0 + timedelta(hours=hour), "label": label,
            "event_time": T0 + timedelta(hours=event_hour) if event_hour is not None else None,
            "monitoring_seconds": seconds}


class RRHistoryTests(unittest.TestCase):
    def test_exact_hourly_endpoints_exclude_extra_positive_audit_samples(self):
        point = T0 + timedelta(hours=6)
        epochs = {point - timedelta(hours=lag): rr(1000 - lag * 10) for lag in range(6)}
        baseline = rr_features_at_landmark(epochs, point)
        for minute in range(5, 360, 5):
            end = point - timedelta(minutes=minute)
            if end not in epochs:
                epochs[end] = rr(999999)
        self.assertEqual(rr_features_at_landmark(epochs, point), baseline)
        accepted, values = baseline
        self.assertTrue(accepted)
        self.assertEqual(values["rr_valid_count"], 6)
        self.assertAlmostEqual(values["mean_rr_ms_slope_per_hour"], 10)

    def test_missing_current_does_not_borrow_nearby_epoch_and_slopes_need_three(self):
        point = T0 + timedelta(hours=6)
        epochs = {point - timedelta(minutes=5): rr(800), point - timedelta(hours=1): rr(900),
                  point - timedelta(hours=5): rr(500)}
        accepted, values = rr_features_at_landmark(epochs, point)
        self.assertFalse(accepted)
        self.assertIsNone(values["mean_rr_ms_lag0"])
        self.assertEqual(values["rr_valid_count"], 2)
        self.assertIsNone(values["mean_rr_ms_slope_per_hour"])
        epochs[point - timedelta(hours=3)] = rr(700)
        _, values = rr_features_at_landmark(epochs, point)
        self.assertAlmostEqual(values["mean_rr_ms_slope_per_hour"], 100)

    def test_rejected_epoch_and_sample_before_lookback_do_not_fill_history(self):
        point = T0 + timedelta(hours=6)
        epochs = {point: None, point - timedelta(hours=6): rr(800)}
        accepted, values = rr_features_at_landmark(epochs, point)
        self.assertFalse(accepted)
        self.assertEqual(values["rr_valid_count"], 0)


class AlarmTests(unittest.TestCase):
    def test_false_alert_can_suppress_later_positive_then_six_hour_boundary_reopens(self):
        rows = [alarm_row(1, 6, "negative", 13), alarm_row(1, 8, "positive", 13),
                alarm_row(1, 12, "positive", 13)]
        result = alarm_metrics(rows, [0.9, 0.9, 0.9], 0.8, full_riskset_events=2)
        self.assertEqual(result["notifications"], 2)
        self.assertEqual(result["false_notifications"], 1)
        self.assertEqual(result["refractory_suppressed_crossings"], 1)
        self.assertEqual(result["events_caught"], 1)
        self.assertEqual(result["lead_time_hours_median"], 1)
        self.assertEqual(result["event_sensitivity_full_riskset"], 0.5)

    def test_censored_alert_is_unclassified_and_denominator_counts_actual_time(self):
        rows = [alarm_row(1, 6, "negative", seconds=3600), alarm_row(2, 6, "censored", seconds=1800)]
        result = alarm_metrics(rows, [0.9, 0.9], 0.8)
        self.assertEqual(result["censored_notifications"], 1)
        self.assertEqual(result["false_notifications"], 1)
        self.assertEqual(result["observed_scored_patient_hours"], 1.5)
        self.assertEqual(result["evaluable_scored_patient_hours"], 1)
        self.assertAlmostEqual(result["false_notifications_per_100_observed_patient_hours"], 100 / 1.5)

    def test_refractory_is_per_icu_and_unscored_gaps_add_no_monitoring_hours(self):
        rows = [alarm_row(1, 6, "negative"), alarm_row(2, 6, "negative"), alarm_row(1, 16, "negative")]
        result = alarm_metrics(list(reversed(rows)), [1, 1, 1], 1)
        self.assertEqual(result["notifications"], 3)
        self.assertEqual(result["observed_scored_patient_hours"], 3)

    def test_invalid_duplicate_or_inconsistent_positive_window_fails(self):
        row = alarm_row(1, 6, "negative")
        with self.assertRaises(ValueError):
            alarm_metrics([row, row], [1, 1], 0.5)
        with self.assertRaises(ValueError):
            alarm_metrics([alarm_row(1, 6, "positive", 20)], [1], 0.5)
        with self.assertRaises(ValueError):
            alarm_metrics([alarm_row(1, 6, "negative", seconds=7200)], [1], 0.5)


class BuildTableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feasibility, self.work = self.root / "feasibility", self.root / "m2"
        self.feasibility.mkdir()
        self.work.mkdir()
        self.records = []
        for subject in range(1, 6):
            stay = StudyStay(subject, 10 + subject, 100 + subject, T0, T0 + timedelta(hours=12),
                             T0 + timedelta(hours=12), 20 + subject, False)
            self.records.append(StayOutcome(stay, FirstUseResult(None, None, stay.followup_end, "no_exposure")))
        self.splits, _ = ensure_patient_split(self.records, self.work / "patient_split.json")
        (self.feasibility / "stay_outcomes.jsonl").write_text("\n".join(
            json.dumps(asdict(record), default=lambda value: value.isoformat()) for record in self.records) + "\n")
        (self.feasibility / "waveform_metadata.jsonl").write_text("synthetic metadata")
        path = self.work / "vitals.sqlite"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE vitals(icustay_id INTEGER, feature TEXT,charttime TEXT,storetime TEXT,value REAL,row_id INTEGER)")
            for record in self.records:
                for hour, stored, value, rid in [(5, 5, 60, 1), (5.5, 7, 200, 2), (6, 6, 250, 3)]:
                    connection.execute("INSERT INTO vitals VALUES (?,?,?,?,?,?)", (record.icustay_id, "heart_rate",
                        (T0 + timedelta(hours=hour)).isoformat(), (T0 + timedelta(hours=stored)).isoformat(), value, rid))
        signature = sorted((r.subject_id, r.hadm_id, r.icustay_id, r.intime.isoformat(), r.followup_end.isoformat()) for r in self.records)
        (self.work / "vitals_summary.json").write_text(json.dumps({"bytes": path.stat().st_size,
            "fingerprint": {"cohort_sha256": hashlib.sha256(json.dumps(signature).encode()).hexdigest()}}))

    def make_epochs(self, *, complete=True, wrong_count=False):
        fingerprint = {"config": {"test": True}, "versions": {"test": "1"},
                       "stay_outcomes_sha256": hashlib.sha256((self.feasibility / "stay_outcomes.jsonl").read_bytes()).hexdigest(),
                       "waveform_metadata_sha256": hashlib.sha256((self.feasibility / "waveform_metadata.jsonl").read_bytes()).hexdigest()}
        with closing(sqlite3.connect(self.work / "epochs.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
            connection.execute("CREATE TABLE epochs(icustay_id INTEGER,endtime TEXT,accepted INTEGER,result TEXT)")
            connection.execute("INSERT INTO metadata VALUES ('fingerprint',?)", (json.dumps(fingerprint),))
            for record in self.records:
                point = T0 + timedelta(hours=6)
                result = {"icu": record.icustay_id, "end": point.isoformat(), "accepted": True, "features": rr(800)}
                connection.execute("INSERT INTO epochs VALUES (?,?,?,?)", (record.icustay_id, point.isoformat(), 1, json.dumps(result)))
        count = len(self.records)
        (self.work / "signal_summary.json").write_text(json.dumps({"complete": complete, "screened_epochs": count,
            "plan": {"unique_epochs": count + int(wrong_count)}, "config": fingerprint["config"], "versions": fingerprint["versions"]}))

    def test_build_skips_test_queries_and_enforces_vital_both_clock_cutoffs(self):
        from hrv_early_warning.vitals import load_stay_vitals as actual_loader
        queried = []
        heldout = {r.icustay_id for r in self.records if self.splits[r.subject_id] == "test"}
        def guarded(connection, icu):
            self.assertNotIn(icu, heldout)
            queried.append(icu)
            return actual_loader(connection, icu)
        output = self.work / "table.sqlite"
        with patch("hrv_early_warning.modeling.load_stay_vitals", side_effect=guarded):
            _, report = build_model_table(self.feasibility, self.work, output)
        self.assertFalse(report["rr_ready"])
        self.assertEqual(report["heldout_windows_skipped_before_feature_query"], 6)
        self.assertEqual(report["development_rows"], 24)
        self.assertEqual(len(queried), 4)
        with closing(sqlite3.connect(output)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM model_rows WHERE split='test'").fetchone()[0], 0)
            features = json.loads(connection.execute("SELECT features FROM model_rows WHERE landmark=? LIMIT 1", ((T0 + timedelta(hours=6)).isoformat(),)).fetchone()[0])
        self.assertEqual(features["heart_rate"], 60)
        self.assertIsNone(features["map"])

    def test_completed_signal_reads_only_development_epochs_and_common_current_mask(self):
        self.make_epochs()
        heldout = {r.icustay_id for r in self.records if self.splits[r.subject_id] == "test"}
        def guarded(connection, icu):
            self.assertNotIn(icu, heldout)
            return load_stay_epochs(connection, icu)
        with patch("hrv_early_warning.modeling.load_stay_epochs", side_effect=guarded):
            path, report = build_model_table(self.feasibility, self.work, self.work / "table.sqlite")
        self.assertTrue(report["rr_ready"])
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM model_rows WHERE current_rr_accepted=1").fetchone()[0], 4)

    def test_partial_or_mismatched_signal_metadata_never_reads_feature_rows(self):
        for complete, wrong_count in [(False, False), (True, True)]:
            with self.subTest(complete=complete, wrong_count=wrong_count):
                for name in ("epochs.sqlite", "signal_summary.json"):
                    (self.work / name).unlink(missing_ok=True)
                self.make_epochs(complete=complete, wrong_count=wrong_count)
                with patch("hrv_early_warning.modeling.load_stay_epochs", side_effect=AssertionError("partial features read")):
                    _, report = build_model_table(self.feasibility, self.work, self.work / "table.sqlite")
                self.assertFalse(report["rr_ready"])

    def test_vitals_wrong_cohort_cannot_be_silently_reused(self):
        summary = json.loads((self.work / "vitals_summary.json").read_text())
        summary["fingerprint"]["cohort_sha256"] = "wrong"
        (self.work / "vitals_summary.json").write_text(json.dumps(summary))
        with self.assertRaisesRegex(ValueError, "vitals_cohort_fingerprint_mismatch"):
            build_model_table(self.feasibility, self.work, self.work / "table.sqlite")


@unittest.skipUnless(HAS_SKLEARN, "model fitting requires the declared scientific environment")
class ModelFitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.table = self.root / "table.sqlite"
        with closing(sqlite3.connect(self.table)) as connection, connection:
            connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [("source_fingerprint", json.dumps({"model_version": MODEL_VERSION})),
                ("report", json.dumps({"rr_ready": True}))])
            connection.execute("""CREATE TABLE model_rows(subject_id INTEGER,icustay_id INTEGER,landmark TEXT,label TEXT,
                split TEXT,event_time TEXT,monitoring_seconds REAL,current_rr_accepted INTEGER,features TEXT)""")
            for split, subjects in [("train", range(1, 7)), ("validation", range(11, 15))]:
                for subject in subjects:
                    for index, label in enumerate(["negative", "positive", "censored"]):
                        features = {name: None for name in set(BASE_FEATURES + HISTORY_FEATURES + TREND_FEATURES)}
                        features.update(age_years=(20 if subject % 2 else 30) if split == "train" else 10000,
                                        age_obscured=0, heart_rate=70 + 10 * index, respiratory_rate=12 + index, spo2=98)
                        features.update({name: 800 + index for name in CURRENT_FEATURES})
                        features["rr_valid_count"] = 1
                        if split == "validation":
                            features["map"] = 999
                        # Exactly the same paired mask across all four comparisons.
                        accepted = not (subject in (1, 11))
                        point = T0 + timedelta(hours=6 + 8 * index)
                        event = (point + timedelta(hours=2)).isoformat() if label == "positive" else None
                        connection.execute("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?,?)", (subject, subject * 10,
                            point.isoformat(), label, split, event, 3600, int(accepted), json.dumps(features)))

    def test_fixed_models_train_only_preprocessing_paired_mask_and_bootstrap(self):
        import joblib
        report = train_models(self.table, self.root / "models", bootstrap_replicates=12)
        self.assertEqual(report["stage"], "development_validation_only_test_unopened")
        self.assertEqual(len(report["models"]), 5)
        paired_counts = []
        for name, result in report["models"].items():
            self.assertEqual(result["status"], "development_validation_complete")
            self.assertEqual(result["validation_patient_bootstrap"]["replicates_requested"], 12)
            self.assertNotIn("subject_id", result["features"])
            self.assertNotIn("event_time", result["features"])
            if name != "vitals_all":
                paired_counts.append(result["data"])
        self.assertTrue(all(count == paired_counts[0] for count in paired_counts))
        saved = joblib.load(self.root / "models" / "vitals_all.joblib")
        imputer = saved["pipeline"].named_steps["imputer"]
        self.assertEqual(imputer.statistics_[BASE_FEATURES.index("age_years")], 25)
        self.assertEqual(imputer.statistics_[BASE_FEATURES.index("map")], 0)
        self.assertTrue(imputer.add_indicator)
        self.assertTrue(imputer.keep_empty_features)
        logistic = saved["pipeline"].named_steps["logistic"]
        self.assertEqual(logistic.C, 1)
        self.assertIsNone(logistic.class_weight)
        self.assertEqual(report["models"]["vitals_all"]["validation"]["binary_windows"], 8)
        metrics = report["models"]["vitals_all"]["validation"]
        self.assertEqual(metrics["constant_train_prevalence"], 0.5)
        self.assertEqual(metrics["constant_train_prevalence_Brier_on_validation"], 0.25)
        self.assertEqual(metrics["calibration_curve"]["requested_bins"], 10)
        self.assertEqual(len(metrics["calibration_curve"]["mean_predicted"]),
                         len(metrics["calibration_curve"]["observed_fraction"]))
        self.assertEqual(report["models"]["vitals_all"]["validation_alarms"]["observed_scored_patient_hours"], 12)

    def test_test_rows_fail_before_model_fit(self):
        with closing(sqlite3.connect(self.table)) as connection, connection:
            connection.execute("UPDATE model_rows SET split='test' WHERE subject_id=11")
        with self.assertRaisesRegex(ValueError, "must_not_contain_test"):
            train_models(self.table, self.root / "models", bootstrap_replicates=2)

    def test_pending_signal_fits_only_vitals_baseline(self):
        with closing(sqlite3.connect(self.table)) as connection, connection:
            connection.execute("UPDATE metadata SET value=? WHERE key='report'", (json.dumps({"rr_ready": False}),))
        report = train_models(self.table, self.root / "models", bootstrap_replicates=2)
        self.assertEqual(report["models"]["vitals_all"]["status"], "development_validation_complete")
        for name, result in report["models"].items():
            if name != "vitals_all":
                self.assertEqual(result["status"], "pending_complete_signal_screen")
                self.assertFalse((self.root / "models" / (name + ".joblib")).exists())

    def test_patient_bootstrap_preserves_clusters_and_is_order_invariant(self):
        y, scores, subjects = [0, 1, 0, 1, 0, 1], [0.1, 0.9, 0.2, 0.8, 0.4, 0.6], [1, 1, 2, 2, 3, 3]
        first = patient_bootstrap_metrics(y, scores, subjects, replicates=20)
        order = [4, 0, 5, 2, 1, 3]
        second = patient_bootstrap_metrics([y[i] for i in order], [scores[i] for i in order], [subjects[i] for i in order], replicates=20)
        self.assertEqual({k: v for k, v in first.items() if k != "intervals"},
                         {k: v for k, v in second.items() if k != "intervals"})
        for metric in ("AUROC", "AUPRC", "Brier"):
            for a, b in zip(first["intervals"][metric], second["intervals"][metric]):
                self.assertAlmostEqual(a, b, places=12)
        self.assertEqual(first["distinct_validation_patients"], 3)
        self.assertEqual(first["replicates_valid"], 20)
        self.assertEqual(first["intervals"]["AUROC"], [1, 1])


if __name__ == "__main__":
    unittest.main()
