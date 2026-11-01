from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_rr_annotation", ROOT / "scripts/prepare_rr_annotation.py")
annotation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(annotation)


def fixture(*, reverse=False):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("ATTACH DATABASE ':memory:' AS signals")
    connection.execute("""CREATE TABLE model_rows (subject_id INTEGER,hadm_id INTEGER,icustay_id INTEGER,
        landmark TEXT,current_rr_accepted INTEGER,split TEXT,features TEXT,label TEXT,event_time TEXT,score REAL,
        PRIMARY KEY(icustay_id,landmark))""")
    connection.execute("CREATE TABLE signals.epochs (icustay_id INTEGER,endtime TEXT,accepted INTEGER,result TEXT,PRIMARY KEY(icustay_id,endtime))")
    assignments, data = {}, []
    for split, ids in (("train", range(100, 160)), ("validation", range(200, 220)), ("test", range(300, 320))):
        for subject in ids:
            assignments[str(subject)] = split
            for hour in (6, 7, 8):
                accepted = subject < 130 if split == "train" else True
                if split == "train" and 120 <= subject < 130 and hour == 8:
                    accepted = False
                end = (datetime(2100, 1, 1) + timedelta(hours=hour)).isoformat()
                data.append((subject, subject * 2, subject * 3, end, int(accepted), split,
                             "invalid JSON that must never be read", "positive", "secret outcome clock", 0.99))
    if reverse:
        data.reverse()
    connection.executemany("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?,?,?)", data)
    connection.executemany("INSERT INTO signals.epochs VALUES (?,?,?,?)",
                           [(row[2], row[3], row[4], "private waveform features must not be read") for row in data])
    # An audit-only epoch has no hourly model row and must never be selected.
    connection.execute("INSERT INTO signals.epochs VALUES (9999,'2100-01-01T06:05:00',1,'dense audit')")
    return connection, assignments


class AnnotationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.connection, self.assignments = fixture()
        self.excluded = set(range(100, 106))

    def tearDown(self):
        self.connection.close()

    def test_twenty_unique_train_patients_ten_per_group_and_prior_exclusion(self):
        selected, counts = annotation.select_cases(self.connection, self.assignments, self.excluded)
        self.assertEqual(len(selected), 20)
        self.assertEqual(len({row["subject_id"] for row in selected}), 20)
        self.assertEqual(sum(row["v1_accepted"] for row in selected), 10)
        self.assertFalse({row["subject_id"] for row in selected} & self.excluded)
        self.assertTrue(all(self.assignments[str(row["subject_id"])] == "train" for row in selected))
        self.assertEqual(counts["train_exact_current_epochs_examined"], 180)
        self.assertEqual(counts["rows_excluded_for_previously_seen_patients"], 18)
        self.assertTrue(all((datetime.fromisoformat(row["endtime"]) - datetime.fromisoformat(row["starttime"])).total_seconds() == 300 for row in selected))

    def test_hash_selection_repeats_and_is_independent_of_insert_order(self):
        first = annotation.select_cases(self.connection, self.assignments, self.excluded)
        second_connection, assignments = fixture(reverse=True)
        try:
            second = annotation.select_cases(second_connection, assignments, self.excluded)
            self.assertEqual(first, second)
        finally:
            second_connection.close()

    def test_no_features_outcomes_scores_or_epoch_results_are_queried(self):
        prohibited = []
        def authorizer(action, table, column, *unused):
            if action == sqlite3.SQLITE_READ and ((table == "model_rows" and column in {"features", "label", "event_time", "score"}) or (table == "epochs" and column == "result")):
                prohibited.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.connection.set_authorizer(authorizer)
        selected, _ = annotation.select_cases(self.connection, self.assignments, self.excluded)
        self.assertEqual(len(selected), 20)
        self.assertEqual(prohibited, [])

    def test_changes_in_outcomes_and_scores_cannot_change_selection(self):
        before = annotation.select_cases(self.connection, self.assignments, self.excluded)
        self.connection.execute("UPDATE model_rows SET label='negative',event_time=NULL,score=0.01,features='different values'")
        self.connection.execute("UPDATE signals.epochs SET result='other private features'")
        self.assertEqual(before, annotation.select_cases(self.connection, self.assignments, self.excluded))

    def test_insufficient_train_group_never_borrows_validation_or_test(self):
        self.connection.execute("DELETE FROM model_rows WHERE split='train' AND subject_id>=129")
        with self.assertRaisesRegex(ValueError, "insufficient_rejected_training"):
            annotation.select_cases(self.connection, self.assignments, self.excluded)

    def test_insufficient_accepted_group_reports_failure(self):
        self.connection.execute("DELETE FROM model_rows WHERE split='train' AND subject_id<130 AND subject_id>=115")
        with self.assertRaisesRegex(ValueError, "insufficient_accepted_training"):
            annotation.select_cases(self.connection, self.assignments, self.excluded)

    def test_split_manifest_mismatch_is_rejected(self):
        self.assignments["110"] = "test"
        with self.assertRaisesRegex(ValueError, "fixed_split"):
            annotation.select_cases(self.connection, self.assignments, self.excluded)

    def test_current_epoch_decision_mismatch_is_rejected(self):
        self.connection.execute("UPDATE signals.epochs SET accepted=0 WHERE icustay_id=330")
        with self.assertRaisesRegex(ValueError, "acceptance_mismatch"):
            annotation.select_cases(self.connection, self.assignments, self.excluded)

    def test_missing_accepted_epoch_is_not_silently_removed(self):
        self.connection.execute("DELETE FROM signals.epochs WHERE icustay_id=330")
        with self.assertRaisesRegex(ValueError, "accepted_current_epoch_missing"):
            annotation.select_cases(self.connection, self.assignments, self.excluded)

    def test_audit_only_epoch_cannot_be_selected(self):
        selected, _ = annotation.select_cases(self.connection, self.assignments, self.excluded)
        self.assertNotIn(9999, {row["icustay_id"] for row in selected})
        self.assertTrue(all(datetime.fromisoformat(row["endtime"]).minute == 0 for row in selected))

    def test_blind_codes_do_not_group_acceptance_and_template_has_no_private_fields(self):
        selected, _ = annotation.select_cases(self.connection, self.assignments, self.excluded)
        self.assertNotEqual([row["v1_accepted"] for row in selected], [True] * 10 + [False] * 10)
        template = annotation.reviewer_template(selected)
        self.assertFalse(template["waveform_package_ready"])
        forbidden = {"subject_id", "hadm_id", "icustay_id", "starttime", "endtime", "landmark",
                     "accepted", "v1_accepted", "status", "event_time", "label", "score", "split"}
        def check(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden & value.keys())
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
        check(template)
        text = json.dumps(template)
        self.assertNotIn("2100-01-01", text)
        for clip in template["clips"]:
            self.assertIsNone(clip["sampling_rate_hz"])
            self.assertIsNone(clip["waveform_sha256"])
            self.assertEqual(clip["independent_review_1"]["beats"], [])
            self.assertEqual(clip["independent_review_2"]["beats"], [])
        template["clips"][0]["independent_review_1"]["beats"].append({"sample": 12})
        self.assertEqual(template["clips"][0]["independent_review_2"]["beats"], [])
        self.assertEqual(template["clips"][1]["independent_review_1"]["beats"], [])

    def test_prior_six_patient_manifest_must_match_current_sources(self):
        prior = {"provenance": {"seed": annotation.SEED, "split_sha256": "split", "cache_fingerprint": {"v": 1}},
                 "selection": [{"icustay_id": subject * 3, "endtime": "2100-01-01T06:00:00", "accepted": True} for subject in range(100, 106)]}
        result = annotation.excluded_patients(self.connection, prior, self.assignments, "split", {"v": 1})
        self.assertEqual(result, self.excluded)
        corrupt = deepcopy(prior)
        corrupt["provenance"]["split_sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "provenance"):
            annotation.excluded_patients(self.connection, corrupt, self.assignments, "split", {"v": 1})
        corrupt = deepcopy(prior)
        corrupt["selection"][0]["icustay_id"] = 200 * 3
        with self.assertRaisesRegex(ValueError, "training_identity"):
            annotation.excluded_patients(self.connection, corrupt, self.assignments, "split", {"v": 1})


class AnnotationOutputTests(unittest.TestCase):
    def test_output_rejects_public_location_symlink_or_existing_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ignored"
            root.mkdir()
            with patch.object(annotation, "OUTPUT_ROOT", root):
                with self.assertRaisesRegex(ValueError, "ignored_rr_annotation"):
                    annotation.output_directory(Path(directory) / "web")
                link = root / "link"
                link.symlink_to(root, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    annotation.output_directory(link)
                (root / "selection.private.json").write_text("frozen")
                with self.assertRaises(FileExistsError):
                    annotation.output_directory(root)

    def test_private_publication_is_exclusive_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.private.json"
            annotation._write_private(path, {"selection": "frozen"}, exclusive=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                annotation._write_private(path, {"selection": "changed"}, exclusive=True)
            self.assertEqual(json.loads(path.read_text()), {"selection": "frozen"})


if __name__ == "__main__":
    unittest.main()
