from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hrv_early_warning import coverage_review as review


def make_row(*, subject=1, icu=1, hour=6, split="train", label="negative", accepted=True,
             history=3, seconds=3600, present=True, status="ok", reasons=None, errors=None):
    end = (datetime(2100, 1, 1) + timedelta(hours=hour)).isoformat()
    available = set(range(history)) if accepted else set(range(1, history + 1))
    features = {f"{key}_lag{lag}": (800.0 if key == "mean_rr_ms" else 10.0) if lag in available else None
                for lag in range(6) for key in review.RR_FEATURES}
    features["rr_valid_count"] = history
    epoch = {"icu": icu, "end": end, "accepted": accepted, "decode_status": status,
             "reasons": [] if accepted else (reasons or ["nonfinite_samples"]), "decode_errors": errors or []}
    return {"subject_id": subject, "icustay_id": icu, "landmark": end, "label": label, "split": split,
            "monitoring_seconds": seconds, "current_rr_accepted": int(accepted), "features": json.dumps(features),
            "epoch_accepted": int(accepted) if present else None,
            "epoch_result": json.dumps(epoch) if present else None}


class CoverageReducerTests(unittest.TestCase):
    def test_counts_keep_patients_events_windows_and_partial_hours_distinct(self):
        rows = [make_row(label="positive", history=6),
                make_row(hour=7, label="positive", seconds=1800),
                make_row(subject=1, icu=2, label="censored", accepted=False, history=0, present=False),
                make_row(subject=2, icu=3, split="validation", label="negative")]
        result = review.summarize_rows(rows)
        all_rows = result["development"]["stages"]["all_risk_windows"]
        self.assertEqual((all_rows["patients"], all_rows["icus"], all_rows["windows"]), (2, 3, 4))
        self.assertEqual(all_rows["events_with_at_least_one_positive_window"], 1)
        self.assertEqual(all_rows["event_patients"], 1)
        self.assertEqual(all_rows["observed_patient_hours_including_censored"], 3.5)
        self.assertEqual(all_rows["binary_evaluable_patient_hours"], 2.5)
        self.assertEqual(result["train"]["stages"]["current_and_history_all_6_of_6"]["windows"], 1)

    def test_history_only_stage_does_not_assume_current_rr(self):
        rows = [make_row(accepted=False, history=3, present=False)]
        stages = review.summarize_rows(rows)["development"]["stages"]
        self.assertEqual(stages["history_at_least_3_of_6"]["windows"], 1)
        self.assertEqual(stages["current_and_history_at_least_3_of_6"]["windows"], 0)
        self.assertEqual(stages["current_epoch_planned"]["windows"], 0)

    def test_four_statuses_partition_windows_not_patients(self):
        rows = [make_row(hour=6, accepted=False, history=0, present=False),
                make_row(hour=7, accepted=False, history=0, status="unavailable", reasons=["ecg_decode_unavailable"]),
                make_row(hour=8, accepted=False, history=0, status="partial"),
                make_row(hour=9)]
        result = review.summarize_rows(rows)["development"]
        categories = result["mutually_exclusive_current_status"]
        self.assertEqual([categories[status]["windows"] for status in review.STATUS], [1, 1, 1, 1])
        self.assertEqual(sum(value["patients"] for value in categories.values()), 4)
        self.assertEqual(result["stages"]["all_risk_windows"]["patients"], 1)
        self.assertEqual(result["stages"]["current_decoded_ok_or_partial"]["windows"], 2)

    def test_reason_counts_are_nonexclusive_and_deduplicated_per_window(self):
        row = make_row(accepted=False, history=0, reasons=["nonfinite_samples", "flat_signal", "flat_signal"])
        result = review.summarize_rows([row])["development"]
        self.assertEqual(result["rejection_reason_window_counts_nonexclusive"], {"flat_signal": 1, "nonfinite_samples": 1})
        self.assertEqual(result["mutually_exclusive_current_status"]["current_decoded_engineering_rejected"]["windows"], 1)

    def test_heldout_row_rejected_before_feature_deserialization(self):
        row = make_row(split="test")
        row["features"] = "malformed JSON must not be parsed"
        with self.assertRaisesRegex(ValueError, "heldout_or_unknown"):
            review.summarize_rows([row])

    def test_future_or_dense_epoch_cannot_replace_exact_hourly_endpoint(self):
        row = make_row()
        epoch = json.loads(row["epoch_result"])
        epoch["end"] = (datetime.fromisoformat(row["landmark"]) + timedelta(minutes=5)).isoformat()
        row["epoch_result"] = json.dumps(epoch)
        with self.assertRaisesRegex(ValueError, "identity"):
            review.classify_row(row)

    def test_bad_feature_counts_and_missingness_fail(self):
        for change in [lambda f: f.update(rr_valid_count=6),
                       lambda f: f.update(rr_valid_count=True),
                       lambda f: f.update(sdrr_ms_lag0=None),
                       lambda f: f.update(mean_rr_ms_lag0=float("nan"))]:
            row = make_row()
            features = json.loads(row["features"])
            change(features)
            row["features"] = json.dumps(features)
            with self.assertRaises(ValueError):
                review.classify_row(row)

    def test_current_cache_absence_or_inconsistent_decision_fails(self):
        row = make_row(present=False)
        with self.assertRaisesRegex(ValueError, "absent"):
            review.classify_row(row)
        row = make_row()
        row["epoch_accepted"] = 0
        with self.assertRaisesRegex(ValueError, "acceptance"):
            review.classify_row(row)

    def test_duplicate_and_out_of_order_rows_fail(self):
        row = make_row()
        with self.assertRaisesRegex(ValueError, "unique_and_ordered"):
            review.summarize_rows([row, deepcopy(row)])
        with self.assertRaisesRegex(ValueError, "unique_and_ordered"):
            review.summarize_rows([make_row(hour=8), make_row(hour=7)])

    def test_nonfinite_duration_or_reason_with_identifier_rejected(self):
        with self.assertRaisesRegex(ValueError, "monitored_interval"):
            review.classify_row(make_row(seconds=float("inf")))
        with self.assertRaisesRegex(ValueError, "nonidentifying_reason"):
            review.classify_row(make_row(accepted=False, history=0, reasons=["/private/patient/path"]))

    def test_empty_groups_remain_explicit_zero_with_null_denominator(self):
        result = review.summarize_rows([])
        for split in result.values():
            self.assertEqual(split["stages"]["all_risk_windows"]["windows"], 0)
            self.assertIsNone(split["stages"]["current_rr_accepted"]["coverage_fraction"]["patients"])

    def test_report_does_not_emit_private_identity_or_clocks(self):
        row = make_row(subject=8675309, icu=9876543)
        payload = json.dumps(review.summarize_rows([row]))
        self.assertNotIn("8675309", payload)
        self.assertNotIn("9876543", payload)
        self.assertNotIn("2100-01-01", payload)
        self.assertNotIn("mean_rr_ms", payload)


class CoverageGuardTests(unittest.TestCase):
    def test_metadata_guard_rejects_test_table_before_feature_read(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE model_rows (split TEXT, features TEXT)")
        connection.execute("INSERT INTO model_rows VALUES ('test','not valid JSON')")
        forbidden_reads = []
        def authorizer(action, first, second, *unused):
            if action == sqlite3.SQLITE_READ and first == "model_rows" and second == "features":
                forbidden_reads.append(second)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorizer)
        with self.assertRaisesRegex(ValueError, "test_or_unknown_split"):
            review._validate_metadata(connection, Path("unused"), Path("unused"))
        self.assertEqual(forbidden_reads, [])
        connection.close()

    def test_database_handle_rejects_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE value (number INTEGER)")
            connection = review._readonly(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("INSERT INTO value VALUES (1)")
            finally:
                connection.close()

    def test_output_rejects_public_path_symlink_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ignored"
            root.mkdir()
            with patch.object(review, "OUTPUT_ROOT", root):
                with self.assertRaisesRegex(ValueError, "ignored"):
                    review._output_path(Path(directory) / "public")
                link = root / "link"
                link.symlink_to(root, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    review._output_path(link)
                (root / "summary.json").write_text("existing")
                with self.assertRaises(FileExistsError):
                    review._output_path(root)


if __name__ == "__main__":
    unittest.main()
