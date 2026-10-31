"""Synthetic-file tests for feasibility boundaries, cache provenance and linkage."""

import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hrv_early_warning import feasibility
from hrv_early_warning.feasibility import ensure_infusion_cache, positive_landmarks, run_feasibility


class PositiveLandmarkTests(unittest.TestCase):
    def setUp(self):
        self.intime = datetime(2101, 2, 3)

    def test_first_landmark_requires_six_hours_and_event_is_strictly_future(self):
        for hours in (3, 6):
            self.assertEqual(positive_landmarks(self.intime, self.intime + timedelta(hours=hours), 6, 6), [])
        self.assertEqual(
            positive_landmarks(self.intime, self.intime + timedelta(hours=6, microseconds=1), 6, 6),
            [self.intime + timedelta(hours=6)],
        )

    def test_event_at_horizon_endpoint_is_included_but_landmark_at_event_is_not(self):
        event = self.intime + timedelta(hours=12)
        actual = positive_landmarks(self.intime, event, 6, 6)
        self.assertEqual(actual, [self.intime + timedelta(hours=h) for h in range(6, 12)])
        self.assertEqual(event - actual[0], timedelta(hours=6))
        self.assertNotIn(event, actual)

    def test_noninteger_event_uses_icu_anchored_grid(self):
        start = self.intime + timedelta(minutes=17)
        event = start + timedelta(hours=13, minutes=30)
        self.assertEqual(
            positive_landmarks(start, event, 6, 6),
            [start + timedelta(hours=h) for h in range(8, 14)],
        )

    def test_nonunit_step_stays_anchored_to_first_landmark(self):
        event = self.intime + timedelta(hours=16)
        self.assertEqual(
            positive_landmarks(self.intime, event, 6, 6, step_hours=2.5),
            [self.intime + timedelta(hours=h) for h in (11, 13.5)],
        )

    def test_two_hour_horizon_does_not_select_earlier_positives(self):
        event = self.intime + timedelta(hours=12, minutes=30)
        self.assertEqual(
            positive_landmarks(self.intime, event, 6, 2),
            [self.intime + timedelta(hours=11), self.intime + timedelta(hours=12)],
        )

    def test_nonpositive_or_nonfinite_durations_are_rejected(self):
        for position in range(3):
            for value in (0, -1, float("nan"), float("inf"), -float("inf")):
                args = [6, 6, 1]
                args[position] = value
                with self.subTest(position=position, value=value), self.assertRaises(ValueError):
                    positive_landmarks(self.intime, self.intime + timedelta(hours=12), *args)

    def test_boolean_or_subclock_durations_are_rejected(self):
        for position in range(3):
            for value in (True, 1e-20):
                args = [6, 6, 1]
                args[position] = value
                with self.subTest(position=position, value=value), self.assertRaises((TypeError, ValueError)):
                    positive_landmarks(self.intime, self.intime + timedelta(hours=12), *args)


class FeasibilityFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clinical = self.root / "clinical"
        self.waveforms = self.root / "waveforms"
        self.work = self.root / "private-work"
        self.clinical.mkdir()
        self.waveforms.mkdir()

    def table(self, name, fields, rows):
        with (self.clinical / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def infusion(self, **changes):
        row = {field: "" for field in feasibility.CACHE_FIELDS}
        row.update({
            "ROW_ID": "500001", "SUBJECT_ID": "765432", "HADM_ID": "810001", "ICUSTAY_ID": "910001",
            "STARTTIME": "2101-02-03 10:00:00", "ENDTIME": "2101-02-03 12:00:00",
            "STORETIME": "2101-02-03 10:01:00", "ITEMID": "221906", "AMOUNT": "1",
            "AMOUNTUOM": "mg", "RATE": "0.1", "RATEUOM": "mcg/kg/min",
            "ORDERCATEGORYDESCRIPTION": "Continuous Med", "CANCELREASON": "0",
            "STATUSDESCRIPTION": "FinishedRunning",
        })
        row.update(changes)
        return row

    def infusion_source(self, rows=None):
        rows = rows if rows is not None else [self.infusion(), self.infusion(ROW_ID="500002", ITEMID="225158")]
        self.table("INPUTEVENTS_MV.csv", feasibility.CACHE_FIELDS, rows)

    def test_infusion_cache_keeps_protocol_drugs_and_reuses_complete_matching_source(self):
        self.infusion_source()
        source_before = (self.clinical / "INPUTEVENTS_MV.csv").read_bytes()
        cache, summary = ensure_infusion_cache(self.clinical, self.work)
        self.assertFalse(summary["cache_reused"])
        self.assertEqual(summary["rows_scanned"], 2)
        self.assertEqual(summary["rows_selected"], 1)
        rows = [json.loads(line) for line in cache.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ITEMID"], "221906")
        self.assertEqual(rows[0]["ICUSTAY_ID"], "910001")
        self.assertNotIn("source_path", summary)
        with patch("hrv_early_warning.feasibility.csv.DictReader", side_effect=AssertionError("cache hit must not scan CSV")):
            hit_path, hit = ensure_infusion_cache(self.clinical, self.work)
        self.assertEqual(hit_path, cache)
        self.assertTrue(hit["cache_reused"])
        self.assertEqual(source_before, (self.clinical / "INPUTEVENTS_MV.csv").read_bytes())

    def test_infusion_source_change_rebuilds_cache(self):
        self.infusion_source()
        ensure_infusion_cache(self.clinical, self.work)
        self.infusion_source([self.infusion(), self.infusion(ROW_ID="500003", ITEMID="221662")])
        cache, summary = ensure_infusion_cache(self.clinical, self.work)
        self.assertFalse(summary["cache_reused"])
        self.assertEqual(summary["rows_selected"], 2)
        self.assertEqual(len(cache.read_text().splitlines()), 2)

    def test_schema_change_rebuilds_infusion_cache(self):
        self.infusion_source()
        ensure_infusion_cache(self.clinical, self.work)
        next_version = feasibility.CACHE_SCHEMA + 1
        with patch("hrv_early_warning.feasibility.CACHE_SCHEMA", next_version):
            _, summary = ensure_infusion_cache(self.clinical, self.work)
        self.assertFalse(summary["cache_reused"])
        self.assertEqual(summary["cache_schema_version"], next_version)

    def test_truncated_infusion_cache_is_rebuilt(self):
        self.infusion_source()
        cache, _ = ensure_infusion_cache(self.clinical, self.work)
        cache.write_text("")
        _, summary = ensure_infusion_cache(self.clinical, self.work)
        self.assertFalse(summary["cache_reused"])
        self.assertEqual(len(cache.read_text().splitlines()), 1)

    def test_missing_required_column_does_not_promote_partial_cache(self):
        columns = [field for field in feasibility.CACHE_FIELDS if field != "RATEUOM"]
        row = self.infusion()
        del row["RATEUOM"]
        self.table("INPUTEVENTS_MV.csv", columns, [row])
        with self.assertRaisesRegex(ValueError, "missing required columns"):
            ensure_infusion_cache(self.clinical, self.work)
        self.assertFalse((self.work / "vasopressor_rows.jsonl").exists())
        self.assertFalse((self.work / "vasopressor_scan_summary.json").exists())

    def test_unidentifiable_drug_does_not_turn_into_clean_negative_exposure(self):
        self.infusion_source([self.infusion(), self.infusion(ROW_ID="500004", ITEMID="unknown")])
        with self.assertRaisesRegex(ValueError, "unidentifiable drug"):
            ensure_infusion_cache(self.clinical, self.work)
        self.assertFalse((self.work / "vasopressor_rows.jsonl").exists())

    def cohort_source(self):
        people = [(765432, 810001, 910001, "2070-01-01", "metavision"),
                  (765433, 810002, 910002, "2075-01-01", "metavision"),
                  (765434, 810003, 910003, "2090-01-01", "metavision"),
                  (765435, 810004, 910004, "2070-01-01", "carevue")]
        self.table("PATIENTS.csv", ["SUBJECT_ID", "DOB"],
                   [{"SUBJECT_ID": subject, "DOB": dob} for subject, _, _, dob, _ in people])
        self.table("ADMISSIONS.csv", ["SUBJECT_ID", "HADM_ID", "ADMITTIME", "DISCHTIME", "DEATHTIME"],
                   [{"SUBJECT_ID": subject, "HADM_ID": hadm, "ADMITTIME": "2101-02-03 00:00:00",
                     "DISCHTIME": "2101-02-05 00:00:00", "DEATHTIME": ""}
                    for subject, hadm, _, _, _ in people])
        self.table("ICUSTAYS.csv", ["SUBJECT_ID", "HADM_ID", "ICUSTAY_ID", "DBSOURCE", "INTIME", "OUTTIME"],
                   [{"SUBJECT_ID": subject, "HADM_ID": hadm, "ICUSTAY_ID": icu, "DBSOURCE": source,
                     "INTIME": "2101-02-03 00:00:00", "OUTTIME": "2101-02-04 00:00:00"}
                    for subject, hadm, icu, _, source in people])

    def waveform_source(self):
        entries = []
        for subject, day in [(765432, "03"), (765433, "05")]:
            folder = self.waveforms / f"p76/p{subject:06d}"
            folder.mkdir(parents=True)
            (folder / "record.hea").write_text(
                f"record 1 125 5400000 00:00:00 {day}/02/2101\nrecord.dat 16 200/mV 16 0 0 0 0 II\n"
            )
            # A deliberately tiny synthetic file proves metadata checks do not
            # establish that all claimed samples or usable ECG actually exist.
            (folder / "record.dat").write_bytes(b"\x01\x02")
            entries.append(f"p76/p{subject:06d}/record")
        (self.waveforms / "RECORDS-waveforms").write_text("\n".join(entries) + "\n")

    def protocol(self):
        path = self.root / "protocol.toml"
        path.write_text('''[study]
version = "synthetic-test"
[cohort]
minimum_age_years = 18
icu_dbsource = "metavision"
[time]
lookback_hours = 6
primary_horizon_hours = 6
landmark_step_hours = 1
[endpoint]
minimum_continuous_minutes = 60
maximum_gap_minutes = 0
sensitivity_continuous_minutes = [30, 120]
prior_exclusion_itemids = [221906, 221289, 222315, 221749, 221662]
[endpoint.primary_itemids]
norepinephrine = 221906
epinephrine = 221289
vasopressin = 222315
phenylephrine = 221749
''')
        return path

    def test_minimal_end_to_end_links_same_icu_and_reports_metadata_not_model_performance(self):
        self.cohort_source()
        self.infusion_source()
        self.waveform_source()
        messages = []
        report = run_feasibility(self.clinical, self.waveforms, self.work, self.protocol(), workers=2, progress=messages.append)
        self.assertEqual(report["clinical_cohort"]["adult_metavision_stays"], 2)
        self.assertEqual(report["clinical_cohort"]["under_18_rows"], 1)
        self.assertEqual(report["clinical_cohort"]["non_metavision_rows"], 1)
        self.assertEqual(report["outcome_status_counts"], {"qualifying_first_use": 1, "no_exposure": 1})
        self.assertEqual(report["waveform_index"]["parsed_records"], 2)
        self.assertEqual(report["same_icu_waveform_span"], {"stays": 1, "subjects": 1})
        events = report["events"]
        self.assertEqual(events["qualifying_stays"], 1)
        self.assertEqual(events["before_first_landmark"], 0)
        self.assertEqual(events["with_full_lookback_ii_metadata"], 1)
        self.assertEqual(events["with_2h_lead_and_3h_ii_metadata"], 1)
        self.assertEqual(events["segments_checked"], 1)
        self.assertIn("no HRV/quality/model evaluation", report["scope"])
        self.assertTrue((self.work / "stay_outcomes.jsonl").is_file())
        self.assertTrue((self.work / "event_metadata_coverage.jsonl").is_file())
        public = json.dumps({"report": report, "progress": messages})
        for private in ["765432", "810001", "910001", "2101-02-03", str(self.clinical), "p76/"]:
            self.assertNotIn(private, public)
        repeat = run_feasibility(self.clinical, self.waveforms, self.work, self.protocol(), workers=1)
        self.assertTrue(repeat["infusion_scan"]["cache_reused"])
        self.assertEqual(repeat["waveform_index"]["cache_status"], "hit")
        self.assertEqual(repeat["events"], report["events"])

    def test_truly_unassignable_exposure_aborts_before_waveform_scan(self):
        self.cohort_source()
        self.infusion_source([
            self.infusion(),
            self.infusion(ROW_ID="500005", SUBJECT_ID="", HADM_ID="", ICUSTAY_ID=""),
        ])
        with patch("hrv_early_warning.waveform_index.load_waveform_index") as waveform_scan:
            with self.assertRaisesRegex(ValueError, "cannot be attributed"):
                run_feasibility(self.clinical, self.waveforms, self.work, self.protocol(), workers=1)
            waveform_scan.assert_not_called()
        self.assertFalse((self.work / "stay_outcomes.jsonl").exists())
        self.assertFalse((self.work / "event_metadata_coverage.jsonl").exists())

    def test_private_output_directory_cannot_be_inside_sources(self):
        for source in [self.clinical, self.waveforms]:
            with self.subTest(source=source.name), self.assertRaisesRegex(ValueError, "outside source"):
                run_feasibility(self.clinical, self.waveforms, source / "outputs", self.root / "unused.toml")


if __name__ == "__main__":
    unittest.main()
