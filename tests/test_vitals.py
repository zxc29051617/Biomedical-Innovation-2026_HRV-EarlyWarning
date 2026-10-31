import csv
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hrv_early_warning.cohort import StudyStay
from hrv_early_warning.vitals import extract_vitals, features_at_landmark, load_stay_vitals


class VitalTests(unittest.TestCase):
    def test_both_clocks_and_freshness_are_enforced(self):
        t = datetime(2100, 1, 1, 12)
        rows = [("heart_rate", t - timedelta(hours=1), t + timedelta(seconds=1), 150, 1),
                ("heart_rate", t - timedelta(hours=2), t - timedelta(hours=1), 70, 2),
                ("heart_rate", t, t, 180, 3),
                ("map", t - timedelta(hours=7), t - timedelta(hours=7), 40, 4)]
        self.assertEqual(features_at_landmark(rows, t)["heart_rate"], 70)
        self.assertIsNone(features_at_landmark(rows, t)["map"])

    def test_ties_average_only_available_observations(self):
        t = datetime(2100, 1, 1, 12)
        earlier = t - timedelta(minutes=10)
        rows = [("map", earlier, earlier, 60, 1), ("map", earlier, t, 80, 2),
                ("map", earlier, t + timedelta(seconds=1), 130, 3)]
        self.assertEqual(features_at_landmark(rows, t)["map"], 70)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "requires model extras")
    def test_csv_extract_identity_units_errors_and_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clinical"
            source.mkdir()
            t = datetime(2100, 1, 1)
            stay = StudyStay(1, 10, 100, t, t + timedelta(days=1), t + timedelta(days=1), 50, False)
            base = dict(ROW_ID="1", SUBJECT_ID="1", HADM_ID="10", ICUSTAY_ID="100", ITEMID="220045", CHARTTIME="2100-01-01 01:00:00", STORETIME="2100-01-01 01:10:00", VALUENUM="70", VALUEUOM="bpm", ERROR="0")
            rows = [base, {**base, "ROW_ID": "2", "SUBJECT_ID": "2"},
                    {**base, "ROW_ID": "3", "VALUEUOM": "unknown"},
                    {**base, "ROW_ID": "4", "ERROR": "1"},
                    {**base, "ROW_ID": "5", "VALUENUM": "inf"},
                    {**base, "ROW_ID": "6", "STORETIME": ""},
                    {**base, "ROW_ID": "7", "CHARTTIME": "2099-12-31 23:00:00"}]
            with (source / "CHARTEVENTS.csv").open("w") as stream:
                writer = csv.DictWriter(stream, fieldnames=base)
                writer.writeheader()
                writer.writerows(rows)
            path, report = extract_vitals(source, [stay], root / "work", chunksize=2)
            self.assertEqual(report["rows_scanned"], 7)
            self.assertEqual(report["retained_rows"], 1)
            conn = sqlite3.connect(path)
            try:
                actual = load_stay_vitals(conn, 100)
            finally:
                conn.close()
            self.assertEqual(len(actual), 1)
            self.assertEqual(actual[0][3], 70)
            _, report = extract_vitals(source, [stay], root / "work")
            self.assertTrue(report["cache_reused"])
