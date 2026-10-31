"""Synthetic prospective-grid, patient isolation and immutable-manifest checks."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from hrv_early_warning.cohort import StudyStay
from hrv_early_warning.risksets import (
    StayOutcome, ensure_patient_split, iter_risk_windows, load_stay_outcomes,
    summarize_riskset,
)
from hrv_early_warning.vasopressors import FirstUseResult


T0 = datetime(2200, 1, 1)


def record(subject=1, icu=None, event_hour=None, status=None, followup_hours=24,
           uncertainty=(), missing_outcome=False):
    stay = StudyStay(subject, 100 + subject, icu or 1000 + subject,
                     T0, T0 + timedelta(hours=24), T0 + timedelta(hours=followup_hours), 40, False)
    exposure = None if event_hour is None else T0 + timedelta(hours=event_hour)
    status = status or ("qualifying_first_use" if exposure is not None else "no_exposure")
    event = exposure if status == "qualifying_first_use" else None
    if status == "ongoing_at_icu_admission":
        outcome_end = T0
    elif status in {"short_first_use", "nonprimary_first_use", "incomplete_duration_followup"}:
        outcome_end = exposure - timedelta(microseconds=1)
    else:
        outcome_end = stay.followup_end
    outcome = None if missing_outcome else FirstUseResult(event, exposure, outcome_end, status, 60 if event else 0)
    return StayOutcome(stay, outcome, tuple(uncertainty), 0)


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError


class LoadStayOutcomesTests(unittest.TestCase):
    def load(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.jsonl"
            path.write_text("\n".join(json.dumps(row, default=json_default) for row in rows))
            return load_stay_outcomes(path)

    def test_loads_full_cohort_and_retains_uncertain_for_split_population(self):
        first, second = record(), record(2, uncertainty=("uncertain_status",), missing_outcome=True)
        values, stats = self.load([asdict(first), asdict(second)])
        self.assertEqual(values, [first, second])
        self.assertEqual(stats, {"adult_stays": 2, "adult_subjects": 2, "uncertain_stays": 1, "missing_outcome_stays": 1})
        self.assertEqual(values[0].followup_end, first.stay.followup_end)
        self.assertEqual(values[0].age_years, 40)

    def test_duplicate_stays_fail_instead_of_changing_denominator(self):
        with self.assertRaisesRegex(ValueError, "invalid_stay_outcome_record_at_line_2"):
            self.load([asdict(record()), asdict(record())])

    def test_invalid_identity_age_and_outcome_clocks_fail_without_patient_data(self):
        variants = []
        for section, key, value in [("stay", "subject_id", "patient-secret-identifier"),
                                    ("stay", "age_years", 300), ("stay", "intime", "2200-01-01"),
                                    ("outcome", "event_status", "unknown")]:
            raw = asdict(record())
            raw[section][key] = value
            variants.append(raw)
        raw = asdict(record(event_hour=12))
        raw["outcome"]["followup_end"] = T0 + timedelta(hours=10)
        variants.append(raw)
        for raw in variants:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    self.load([raw])
                self.assertEqual(str(caught.exception), "invalid_stay_outcome_record_at_line_1")

    def test_obscured_age_is_preserved_without_returning_shifted_age(self):
        item = record()
        item = replace(item, stay=replace(item.stay, age_years=None, age_obscured=True))
        result, _ = self.load([asdict(item)])
        self.assertIsNone(result[0].age_years)
        self.assertTrue(result[0].age_obscured)


class PatientSplitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "patient_split.json"
        self.records = [record(subject) for subject in range(1, 11)]

    def test_split_is_patient_level_and_has_requested_nested_fractions(self):
        rows = self.records + [record(1, icu=9001)]
        assignment, stats = ensure_patient_split(rows, self.path)
        self.assertEqual(stats["split_counts"], {"train": 6, "validation": 2, "test": 2})
        self.assertEqual(len(assignment), 10)
        self.assertEqual(stats["adult_stays"], 11)
        windows = list(iter_risk_windows(rows, assignment))
        for subject in assignment:
            self.assertEqual({window.split for window in windows if window.subject_id == subject}, {assignment[subject]})

    def test_reordered_rerun_reuses_same_bytes_and_same_assignments(self):
        first, stats = ensure_patient_split(self.records, self.path)
        self.assertFalse(stats["reused_manifest"])
        before = self.path.read_bytes()
        second, stats = ensure_patient_split(reversed(self.records), self.path)
        self.assertTrue(stats["reused_manifest"])
        self.assertEqual(first, second)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list(self.path.parent.glob(".split-manifest-*.tmp")), [])

    def test_labels_uncertainty_and_waveform_availability_do_not_change_split(self):
        first, _ = ensure_patient_split(self.records, self.path)
        altered = [replace(record(subject, event_hour=14), matched_waveform_records=99,
                           uncertain_reasons=("uncertain_status",)) for subject in range(1, 11)]
        second, stats = ensure_patient_split(altered, self.path)
        self.assertEqual(first, second)
        self.assertTrue(stats["reused_manifest"])

    def test_cohort_growth_shrinkage_same_subject_new_stay_and_metadata_change_are_rejected(self):
        ensure_patient_split(self.records, self.path)
        before = self.path.read_bytes()
        variations = [self.records + [record(11)], self.records[:-1],
                      self.records + [record(1, icu=9999)],
                      [replace(self.records[0], stay=replace(self.records[0].stay, age_years=41))] + self.records[1:]]
        for rows in variations:
            with self.subTest(size=len(rows)):
                with self.assertRaisesRegex(ValueError, "manifest_mismatch"):
                    ensure_patient_split(rows, self.path)
                self.assertEqual(before, self.path.read_bytes())

    def test_new_seed_or_fraction_never_overwrites_existing_manifest(self):
        ensure_patient_split(self.records, self.path)
        before = self.path.read_bytes()
        for kwargs in [{"seed": 7}, {"test_fraction": 0.3}, {"validation_fraction_of_development": 0.3}]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "manifest_mismatch"):
                ensure_patient_split(self.records, self.path, **kwargs)
        self.assertEqual(before, self.path.read_bytes())

    def test_tampered_assignments_and_corrupt_manifest_fail_closed(self):
        ensure_patient_split(self.records, self.path)
        raw = json.loads(self.path.read_text())
        key = next(iter(raw["assignments"]))
        raw["assignments"][key] = "test" if raw["assignments"][key] != "test" else "train"
        self.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "manifest_mismatch"):
            ensure_patient_split(self.records, self.path)
        self.path.write_text("broken json")
        with self.assertRaisesRegex(ValueError, "unreadable_or_corrupt"):
            ensure_patient_split(self.records, self.path)

    def test_population_too_small_and_invalid_parameters_raise(self):
        with self.assertRaisesRegex(ValueError, "too_small"):
            ensure_patient_split(self.records[:2], self.path)
        for kwargs in [{"seed": True}, {"test_fraction": 0}, {"test_fraction": 1},
                       {"validation_fraction_of_development": float("nan")}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ensure_patient_split(self.records, self.path, **kwargs)


class RiskWindowTests(unittest.TestCase):
    def windows(self, item, **kwargs):
        return list(iter_risk_windows([item], {item.subject_id: "train"}, **kwargs))

    def test_event_free_stay_includes_all_negative_and_censored_hours(self):
        windows = self.windows(record())
        self.assertEqual(len(windows), 18)
        self.assertEqual(windows[0].landmark, T0 + timedelta(hours=6))
        self.assertEqual(windows[-1].landmark, T0 + timedelta(hours=23))
        self.assertEqual(sum(window.label == "negative" for window in windows), 13)
        self.assertEqual(sum(window.label == "censored" for window in windows), 5)
        self.assertEqual(windows[0].history_start, T0)
        self.assertEqual(windows[0].history_end, windows[0].landmark)
        self.assertEqual(windows[0].target, 0)
        self.assertIsNone(windows[-1].target)

    def test_event_stay_keeps_early_negatives_and_stops_at_first_exposure(self):
        windows = self.windows(record(event_hour=14))
        self.assertEqual([window.label for window in windows], ["negative"] * 2 + ["positive"] * 6)
        self.assertEqual(windows[2].horizon_end, T0 + timedelta(hours=14))
        self.assertTrue(all(window.landmark < T0 + timedelta(hours=14) for window in windows))

    def test_event_exactly_at_first_landmark_is_already_treated(self):
        self.assertEqual(self.windows(record(event_hour=6)), [])
        item = record(event_hour=6)
        event = item.event_time + timedelta(microseconds=1)
        item = replace(item, outcome=replace(item.outcome, event_time=event, first_exposure_time=event))
        windows = self.windows(item)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].label, "positive")

    def test_short_and_dopamine_first_use_censor_exact_horizon_boundary(self):
        for status in ["short_first_use", "nonprimary_first_use", "incomplete_duration_followup"]:
            with self.subTest(status=status):
                windows = self.windows(record(event_hour=12, status=status))
                self.assertEqual(len(windows), 6)
                self.assertTrue(all(window.label == "censored" for window in windows))
                self.assertEqual(windows[0].horizon_end, T0 + timedelta(hours=12))

    def test_uncertain_missing_outcome_and_ongoing_stays_never_become_negatives(self):
        for item in [record(uncertainty=("unknown_rate",)), record(missing_outcome=True),
                     record(event_hour=-1, status="ongoing_at_icu_admission")]:
            with self.subTest(item=item):
                self.assertEqual(self.windows(item), [])

    def test_followup_limit_short_stay_and_last_landmark_are_finite(self):
        self.assertEqual(len(self.windows(record(followup_hours=10))), 4)
        self.assertEqual(self.windows(record(followup_hours=6)), [])
        self.assertEqual(len(self.windows(record(followup_hours=6.1))), 1)
        self.assertTrue(all(window.label == "censored" for window in self.windows(record(followup_hours=10))))

    def test_two_hour_secondary_horizon_uses_same_grid_and_risk_population(self):
        primary = self.windows(record(event_hour=14))
        secondary = self.windows(record(event_hour=14), horizon_hours=2)
        self.assertEqual([window.landmark for window in primary], [window.landmark for window in secondary])
        self.assertEqual([window.label for window in secondary], ["negative"] * 6 + ["positive"] * 2)

    def test_waveform_annotation_never_selects_negative_or_positive_windows(self):
        item = record(event_hour=14)
        baseline = self.windows(item)
        future_only = {1: [(T0 + timedelta(hours=20), T0 + timedelta(hours=23))]}
        annotated = self.windows(item, waveform_intervals=future_only)
        self.assertEqual([(w.landmark, w.label) for w in baseline], [(w.landmark, w.label) for w in annotated])
        self.assertTrue(all(window.waveform_overlap_seconds == 0 for window in annotated))

    def test_waveform_union_does_not_double_count_and_excludes_future_at_landmark(self):
        intervals = {1: [(T0 - timedelta(hours=1), T0 + timedelta(hours=2)),
                         (T0 + timedelta(hours=1), T0 + timedelta(hours=3)),
                         (T0 + timedelta(hours=6), T0 + timedelta(hours=8))]}
        windows = self.windows(record(), waveform_intervals=intervals)
        self.assertEqual(windows[0].waveform_overlap_seconds, 3 * 3600)
        self.assertEqual(windows[1].waveform_overlap_seconds, 3 * 3600)
        self.assertEqual(len(windows), 18)

    def test_missing_split_duplicate_stays_and_bad_duration_fail(self):
        with self.assertRaisesRegex(ValueError, "missing_from_patient_split"):
            list(iter_risk_windows([record()], {}))
        with self.assertRaisesRegex(ValueError, "duplicate_stay"):
            list(iter_risk_windows([record(), record()], {1: "train"}))
        for kwargs in [{"lookback_hours": 0}, {"horizon_hours": -1}, {"step_hours": float("nan")}, {"step_hours": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.windows(record(), **kwargs)

    def test_aware_times_use_elapsed_clock_consistently(self):
        item = record()
        zone = timezone(timedelta(hours=8))
        stay = replace(item.stay, intime=item.intime.replace(tzinfo=zone),
                       outtime=item.stay.outtime.replace(tzinfo=zone),
                       followup_end=item.followup_end.replace(tzinfo=zone))
        item = replace(item, stay=stay, outcome=replace(item.outcome, followup_end=stay.followup_end))
        windows = self.windows(item)
        self.assertEqual(len(windows), 18)
        self.assertEqual(windows[0].landmark, stay.intime + timedelta(hours=6))

    def test_complete_summary_accounts_for_all_windows_and_events(self):
        rows = [record(1), record(2, event_hour=14), record(3, event_hour=12, status="short_first_use"),
                record(4, uncertainty=("unknown_rate",)), record(5, event_hour=-1, status="ongoing_at_icu_admission"),
                record(6, event_hour=4)]
        splits = {subject: "train" if subject <= 3 else "validation" for subject in range(1, 7)}
        intervals = {subject: [(T0, T0 + timedelta(hours=24))] for subject in range(1, 7)}
        report = summarize_riskset(rows, splits, waveform_intervals=intervals)
        self.assertEqual(report["nominal_windows"], 108)
        self.assertEqual(report["risk_windows"], 32)
        self.assertEqual(report["label_counts"], {"positive": 6, "negative": 15, "censored": 11})
        excluded = sum(report[key] for key in ["excluded_windows_uncertain_stay", "excluded_windows_missing_outcome",
                                               "excluded_windows_ongoing", "excluded_windows_at_or_after_first_exposure"])
        self.assertEqual(report["nominal_windows"], report["risk_windows"] + excluded)
        self.assertEqual(report["qualifying_events"], 2)
        self.assertEqual(report["qualifying_events_at_or_before_first_landmark"], 1)
        self.assertEqual(report["events_with_positive_window"], 1)
        self.assertEqual(report["windows_with_past_waveform_overlap"], 32)
        self.assertEqual(report["by_split"]["train"]["subjects"], 3)
        self.assertEqual(report["by_split"]["test"]["risk_windows"], 0)


if __name__ == "__main__":
    unittest.main()
