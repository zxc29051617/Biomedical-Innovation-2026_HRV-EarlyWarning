"""Synthetic CSV/JSONL tests for cohort identity, age and time alignment."""

import csv
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from hrv_early_warning.cohort import StudyStay, align_infusions, load_adult_metavision_stays
from hrv_early_warning.vasopressors import derive_first_use


T0 = datetime(2200, 1, 1)


def clock(hours):
    return (T0 + timedelta(hours=hours)).isoformat(sep=" ")


def patient(**changes):
    result = {"SUBJECT_ID": "1", "DOB": "2160-01-01 00:00:00"}
    result.update(changes)
    return result


def admission(**changes):
    result = {"SUBJECT_ID": "1", "HADM_ID": "2", "ADMITTIME": clock(-24),
              "DISCHTIME": clock(96), "DEATHTIME": ""}
    result.update(changes)
    return result


def icu(**changes):
    result = {"SUBJECT_ID": "1", "HADM_ID": "2", "ICUSTAY_ID": "3",
              "INTIME": clock(0), "OUTTIME": clock(24), "DBSOURCE": "metavision"}
    result.update(changes)
    return result


def stay(icu_id=3, start=0, end=24, **changes):
    values = dict(subject_id=1, hadm_id=2, icustay_id=icu_id,
                  intime=T0 + timedelta(hours=start), outtime=T0 + timedelta(hours=end),
                  followup_end=T0 + timedelta(hours=end), age_years=40, age_obscured=False)
    values.update(changes)
    return StudyStay(**values)


def infusion_row(**changes):
    result = {"SUBJECT_ID": "1", "HADM_ID": "2", "ICUSTAY_ID": "3", "ROW_ID": "4",
              "ITEMID": "221906", "STARTTIME": clock(6), "ENDTIME": clock(7),
              "STORETIME": clock(7.5), "RATE": "0.1", "RATEUOM": "mcg/kg/min",
              "AMOUNT": "1", "STATUSDESCRIPTION": "FinishedRunning", "CANCELREASON": "0",
              "ORDERCATEGORYDESCRIPTION": "Continuous Med", "ORDERCATEGORYNAME": "01-Drips"}
    result.update(changes)
    return result


class CohortLoadTests(unittest.TestCase):
    def load(self, patients=None, admissions=None, icus=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tables = [("PATIENTS.csv", patients if patients is not None else [patient()], patient()),
                      ("ADMISSIONS.csv", admissions if admissions is not None else [admission()], admission()),
                      ("ICUSTAYS.csv", icus if icus is not None else [icu()], icu())]
            for name, records, template in tables:
                with (root / name).open("w", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(template))
                    writer.writeheader()
                    writer.writerows(records)
            return load_adult_metavision_stays(root)

    def test_adult_age_uses_birthday_not_365_day_approximation(self):
        before, stats = self.load(patients=[patient(DOB="2182-01-02 00:00:00")])
        self.assertEqual(before, [])
        self.assertEqual(stats["under_18_rows"], 1)
        birthday, _ = self.load(patients=[patient(DOB="2182-01-01 00:00:00")])
        self.assertEqual(birthday[0].age_years, 18)
        self.assertFalse(birthday[0].age_obscured)

    def test_leap_day_birthday_uses_calendar_comparison(self):
        before, _ = self.load(patients=[patient(DOB="2184-02-29")],
                              admissions=[admission(DISCHTIME="2202-03-03 00:00:00")],
                              icus=[icu(INTIME="2202-02-28 00:00:00", OUTTIME="2202-03-02 00:00:00")])
        self.assertEqual(before, [])
        after, _ = self.load(patients=[patient(DOB="2184-02-29")],
                             admissions=[admission(DISCHTIME="2202-03-03 00:00:00")],
                             icus=[icu(INTIME="2202-03-01 00:00:00", OUTTIME="2202-03-02 00:00:00")])
        self.assertEqual(after[0].age_years, 18)

    def test_age_89_kept_and_shifted_old_age_obscured_without_losing_adult(self):
        exact, _ = self.load(patients=[patient(DOB="2111-01-01")])
        self.assertEqual(exact[0].age_years, 89)
        obscured, stats = self.load(patients=[patient(DOB="1880-01-01")])
        self.assertIsNone(obscured[0].age_years)
        self.assertTrue(obscured[0].age_obscured)
        self.assertEqual(stats["age_obscured_stays"], 1)

    def test_death_and_discharge_shorten_followup_without_moving_structural_outtime(self):
        for changes, expected, stat in [
            ({"DEATHTIME": clock(10)}, 10, "death_limits_followup_stays"),
            ({"DISCHTIME": clock(12)}, 12, "discharge_limits_followup_stays"),
            ({"DEATHTIME": clock(10), "DISCHTIME": clock(12)}, 10, "death_limits_followup_stays"),
        ]:
            with self.subTest(changes=changes):
                records, stats = self.load(admissions=[admission(**changes)])
                self.assertEqual(records[0].followup_end, T0 + timedelta(hours=expected))
                self.assertEqual(records[0].outtime, T0 + timedelta(hours=24))
                self.assertEqual(stats[stat], 1)

    def test_carevue_and_both_are_outside_primary_cohort(self):
        records, stats = self.load(icus=[icu(DBSOURCE="carevue"), icu(ICUSTAY_ID="4", DBSOURCE="both"), icu(ICUSTAY_ID="5")])
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["non_metavision_rows"], 2)

    def test_patient_admission_identity_mismatch_is_not_joined_by_hadm_alone(self):
        records, stats = self.load(admissions=[admission(SUBJECT_ID="9")])
        self.assertEqual(records, [])
        self.assertEqual(stats["admission_subject_mismatch_rows"], 1)

    def test_missing_invalid_birth_time_and_nonpositive_followup_are_excluded(self):
        cases = [({"patients": []}, "missing_or_invalid_patient_rows"),
                 ({"patients": [patient(DOB="invalid")]}, "missing_or_invalid_patient_rows"),
                 ({"patients": [patient(DOB="2201-01-01")]}, "birth_after_icu_entry_rows"),
                 ({"admissions": []}, "missing_or_invalid_admission_rows"),
                 ({"admissions": [admission(DISCHTIME="invalid")]}, "missing_or_invalid_admission_rows"),
                 ({"icus": [icu(OUTTIME=clock(0))]}, "invalid_icu_time_rows"),
                 ({"icus": [icu(INTIME="2200-01-01")]}, "invalid_icu_time_rows"),
                 ({"admissions": [admission(DEATHTIME=clock(0))]}, "nonpositive_followup_rows")]
        for kwargs, counter in cases:
            with self.subTest(kwargs=kwargs):
                records, stats = self.load(**kwargs)
                self.assertEqual(records, [])
                self.assertEqual(stats[counter], 1)

    def test_duplicate_rows_deduplicate_but_conflicting_ids_are_excluded(self):
        records, stats = self.load(patients=[patient(), patient()],
                                  admissions=[admission(), admission()], icus=[icu(), icu()])
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["duplicate_icu_id_rows"], 1)
        for kwargs in [{"patients": [patient(), patient(DOB="2150-01-01")]},
                       {"admissions": [admission(), admission(SUBJECT_ID="9")]},
                       {"icus": [icu(), icu(OUTTIME=clock(25))]}]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.load(**kwargs)[0], [])

    def test_subject_count_deduplicates_multiple_icu_stays(self):
        records, stats = self.load(icus=[icu(), icu(ICUSTAY_ID="4", INTIME=clock(48), OUTTIME=clock(72))])
        self.assertEqual(len(records), 2)
        self.assertEqual(stats["adult_metavision_subjects"], 1)
        self.assertEqual(stats["adult_metavision_admissions"], 1)

    def test_icu_entirely_before_hospital_admission_is_not_a_valid_stay(self):
        records, stats = self.load(admissions=[admission(ADMITTIME=clock(48))])
        self.assertEqual(records, [])
        self.assertEqual(stats["icu_admission_no_overlap_rows"], 1)

    def test_partial_pre_admission_icu_interval_is_not_silently_used_for_lookback(self):
        records, stats = self.load(admissions=[admission(ADMITTIME=clock(1))])
        self.assertEqual(records, [])
        self.assertEqual(stats["icu_entry_before_admission_rows"], 1)
        boundary, _ = self.load(admissions=[admission(ADMITTIME=clock(0))])
        self.assertEqual(len(boundary), 1)


class AlignInfusionTests(unittest.TestCase):
    def align(self, records, stays=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selected.jsonl"
            with path.open("w") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            return align_infusions([stay()] if stays is None else stays, path)

    def test_valid_mapping_preserves_late_documentation_for_outcome(self):
        mapped, uncertain, stats = self.align([infusion_row()])
        self.assertEqual(len(mapped[3]), 1)
        self.assertEqual(uncertain, {})
        self.assertEqual(stats["raw_parser_reasons"], {"accepted": 1})
        self.assertEqual(stats["storetime_delay"]["after_start_count"], 1)

    def test_missing_icu_id_requires_unique_same_admission_time_overlap(self):
        mapped, uncertain, stats = self.align([infusion_row(ICUSTAY_ID="")], [stay(), stay(4, 48, 72)])
        self.assertEqual(set(mapped), {3})
        self.assertIsNone(mapped[3][0].icustay_id)
        self.assertEqual(uncertain, {})
        self.assertEqual(stats["missing_id_resolved_rows"], 1)
        ambiguous, uncertain, stats = self.align([infusion_row(ICUSTAY_ID="", STARTTIME=clock(23), ENDTIME=clock(49))],
                                                [stay(), stay(4, 48, 72)])
        self.assertEqual(ambiguous, {})
        self.assertEqual(set(uncertain), {3, 4})
        self.assertEqual(stats["missing_id_unresolved_rows"], 1)

    def test_provided_wrong_icu_id_is_not_silently_remapped(self):
        mapped, uncertain, stats = self.align([infusion_row(ICUSTAY_ID="4")], [stay(), stay(4, 48, 72)])
        self.assertEqual(mapped, {})
        self.assertEqual(set(uncertain), {3, 4})
        self.assertEqual(stats["mismatched_id_rows"], 1)

    def test_provided_unknown_id_cannot_anchor_a_valid_time_match(self):
        mapped, uncertain, stats = self.align([infusion_row(ICUSTAY_ID="999")])
        self.assertEqual(mapped, {})
        self.assertEqual(uncertain[3], {"unverified_provided_icu"})
        self.assertEqual(stats["unverified_provided_id_rows"], 1)

    def test_provided_icu_identity_mismatch_flags_declared_stay(self):
        mapped, uncertain, stats = self.align([infusion_row(HADM_ID="999")])
        self.assertEqual(mapped, {})
        self.assertEqual(uncertain[3], {"provided_icu_identity_mismatch"})
        self.assertEqual(stats["mismatched_id_rows"], 1)

    def test_same_subject_different_admission_is_never_joined(self):
        mapped, uncertain, stats = self.align([infusion_row(HADM_ID="999", ICUSTAY_ID="")])
        self.assertEqual(mapped, {})
        self.assertEqual(uncertain, {})
        self.assertEqual(stats["missing_id_no_target_overlap_rows"], 1)

    def test_known_admission_with_contradictory_subject_cannot_establish_absence(self):
        mapped, uncertain, stats = self.align([infusion_row(SUBJECT_ID="9", ICUSTAY_ID="")])
        self.assertEqual(mapped, {})
        self.assertEqual(uncertain[3], {"row_admission_subject_mismatch"})
        self.assertEqual(stats["admission_identity_mismatch_rows"], 1)

    def test_valid_anchor_can_span_two_icus_and_marks_second_as_ongoing(self):
        stays = [stay(), stay(4, 48, 72)]
        mapped, uncertain, stats = self.align([infusion_row(STARTTIME=clock(23), ENDTIME=clock(49))], stays)
        self.assertEqual(set(mapped), {3, 4})
        self.assertEqual(uncertain, {})
        self.assertEqual(stats["cross_icu_rows"], 1)
        self.assertEqual(stats["pre_icu_ongoing_assignments"], 1)
        result = derive_first_use(mapped[4], stays[1].intime, stays[1].followup_end)
        self.assertEqual(result.event_status, "ongoing_at_icu_admission")

    def test_pre_icu_ongoing_start_is_preserved_and_touching_boundary_is_not_overlap(self):
        mapped, _, stats = self.align([infusion_row(STARTTIME=clock(-1), ENDTIME=clock(1))])
        self.assertEqual(mapped[3][0].start, T0 - timedelta(hours=1))
        self.assertEqual(stats["pre_icu_ongoing_assignments"], 1)
        for start, end in [(-1, 0), (24, 25)]:
            with self.subTest(start=start):
                mapped, uncertain, _ = self.align([infusion_row(STARTTIME=clock(start), ENDTIME=clock(end))])
                self.assertEqual(mapped, {})
                self.assertEqual(uncertain, {})

    def test_uncertain_row_stays_uncertain_even_with_another_valid_infusion(self):
        mapped, uncertain, stats = self.align([infusion_row(), infusion_row(STATUSDESCRIPTION="Flushed")])
        self.assertEqual(len(mapped[3]), 1)
        self.assertEqual(uncertain[3], {"uncertain_status"})
        self.assertEqual(stats["stays_with_mapped_infusions_and_uncertainty"], 1)

    def test_invalid_time_broadens_uncertainty_only_within_identified_admission(self):
        other_admission = stay(5, 96, 120, hadm_id=9)
        mapped, uncertain, _ = self.align([infusion_row(ICUSTAY_ID="", STARTTIME="invalid")],
                                          [stay(), stay(4, 48, 72), other_admission])
        self.assertEqual(mapped, {})
        self.assertEqual(set(uncertain), {3, 4})

    def test_missing_hadm_can_still_attribute_uncertainty_by_subject_and_time(self):
        mapped, uncertain, _ = self.align([infusion_row(HADM_ID="", ICUSTAY_ID="")])
        self.assertEqual(mapped, {})
        self.assertEqual(set(uncertain), {3})

    def test_unattributable_malformed_row_is_counted_instead_of_absence(self):
        mapped, uncertain, stats = self.align([[], {"ITEMID": "221906"}])
        self.assertEqual(mapped, {})
        self.assertEqual(uncertain, {})
        self.assertEqual(stats["unassignable_uncertain_rows"], 2)
        self.assertEqual(stats["malformed_cache_rows"], 1)

    def test_demonstrably_out_of_cohort_uncertainty_is_not_globally_unassignable(self):
        mapped, uncertain, stats = self.align([
            infusion_row(STATUSDESCRIPTION="Flushed", HADM_ID="999", ICUSTAY_ID=""),
            infusion_row(STATUSDESCRIPTION="Flushed", HADM_ID="", SUBJECT_ID="9", ICUSTAY_ID=""),
            infusion_row(STATUSDESCRIPTION="Flushed", STARTTIME=clock(48), ENDTIME=clock(49), ICUSTAY_ID=""),
        ])
        self.assertEqual(mapped, {})
        self.assertEqual(uncertain, {})
        self.assertEqual(stats["unassignable_uncertain_rows"], 0)
        for key in ["outside_target_admission_uncertain_rows", "outside_target_subject_uncertain_rows",
                    "outside_target_interval_uncertain_rows"]:
            self.assertEqual(stats[key], 1)

    def test_rewritten_does_not_make_a_stay_uncertain(self):
        mapped, uncertain, stats = self.align([infusion_row(STATUSDESCRIPTION="Rewritten", CANCELREASON="1")])
        self.assertEqual(mapped, {})
        self.assertEqual(uncertain, {})
        self.assertEqual(stats["raw_parser_reasons"], {"excluded_rewritten": 1})

    def test_storetime_negative_zero_positive_delays_are_counted(self):
        _, _, stats = self.align([infusion_row(STORETIME=clock(5)), infusion_row(STORETIME=clock(6)),
                                 infusion_row(STORETIME=clock(7)), infusion_row(STORETIME="")])
        lag = stats["storetime_delay"]
        self.assertEqual((lag["before_start_count"], lag["at_start_count"], lag["after_start_count"]), (1, 1, 1))
        self.assertEqual(lag["median_seconds"], 0)
        self.assertEqual(stats["accepted_missing_storetime_rows"], 1)

    def test_post_death_interval_cannot_form_outcome_with_effective_followup(self):
        target = replace(stay(), followup_end=T0 + timedelta(hours=5))
        mapped, _, stats = self.align([infusion_row()], [target])
        self.assertEqual(stats["starts_after_effective_followup_assignments"], 1)
        result = derive_first_use(mapped[3], target.intime, target.followup_end)
        self.assertEqual(result.event_status, "no_exposure")
        self.assertEqual(result.followup_end, target.followup_end)

    def test_duplicate_stay_ids_raise_before_alignment(self):
        with self.assertRaises(ValueError):
            self.align([], [stay(), stay()])


if __name__ == "__main__":
    unittest.main()
