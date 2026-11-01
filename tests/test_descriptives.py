"""Synthetic admission-level descriptors and nonduplicating onset categories."""

import csv
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from hrv_early_warning.cohort import StudyStay
from hrv_early_warning.descriptives import audit_descriptives
from hrv_early_warning.risksets import StayOutcome
from hrv_early_warning.vasopressors import FirstUseResult


T0 = datetime(2200, 1, 1)


def outcome(subject, hadm, icu, event=None, uncertain=False):
    stay = StudyStay(subject, hadm, icu, T0, T0 + timedelta(days=1), T0 + timedelta(days=1), 40, False)
    result = None if uncertain else FirstUseResult(event, event, stay.followup_end,
        "qualifying_first_use" if event else "no_exposure", 60 if event else 0)
    return StayOutcome(stay, result, ("uncertain_status",) if uncertain else ())


def infusion(record, itemid, start, *, end=None, status="FinishedRunning"):
    return {"SUBJECT_ID": str(record.subject_id), "HADM_ID": str(record.hadm_id), "ICUSTAY_ID": str(record.icustay_id),
            "ROW_ID": "1", "ITEMID": str(itemid), "STARTTIME": start.isoformat(),
            "ENDTIME": (end or start + timedelta(hours=1)).isoformat(), "STORETIME": start.isoformat(),
            "RATE": "0.1", "RATEUOM": "units/hour" if itemid == 222315 else "mcg/kg/min", "AMOUNT": "1",
            "CANCELREASON": "0", "STATUSDESCRIPTION": status, "ORDERCATEGORYDESCRIPTION": "Continuous Med"}


class DescriptiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clinical, self.work = self.root / "clinical", self.root / "feasibility"
        self.clinical.mkdir()
        self.work.mkdir()
        self.records = [outcome(1, 11, 101, T0 + timedelta(hours=6)),
                        outcome(2, 22, 202, T0 + timedelta(hours=6, seconds=1)),
                        outcome(1, 12, 102), outcome(3, 33, 303, uncertain=True)]
        self.infusions = [infusion(self.records[0], 221906, self.records[0].event_time),
                          infusion(self.records[1], 221906, self.records[1].event_time),
                          infusion(self.records[1], 222315, self.records[1].event_time),
                          infusion(self.records[1], 221906, self.records[1].event_time),
                          infusion(self.records[3], 221906, T0 + timedelta(hours=2), status="Flushed")]
        self.icu_rows = [{"SUBJECT_ID": record.subject_id, "HADM_ID": record.hadm_id, "ICUSTAY_ID": record.icustay_id,
                          "FIRST_CAREUNIT": "CSRU" if index == 0 else "MICU"}
                         for index, record in enumerate(self.records)]
        self.diagnoses = [{"SUBJECT_ID": 1, "HADM_ID": 11, "ICD9_CODE": code} for code in ("99591", "99592", "995.91")]
        self.diagnoses += [{"SUBJECT_ID": 2, "HADM_ID": 20, "ICD9_CODE": "99592"},
                           {"SUBJECT_ID": 2, "HADM_ID": 22, "ICD9_CODE": "995910"}]

    def write_inputs(self):
        for name, rows in [("ICUSTAYS.csv", self.icu_rows), ("DIAGNOSES_ICD.csv", self.diagnoses)]:
            with (self.clinical / name).open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        (self.work / "stay_outcomes.jsonl").write_text("\n".join(
            json.dumps(asdict(record), default=lambda value: value.isoformat()) for record in self.records) + "\n")
        (self.work / "vasopressor_rows.jsonl").write_text("\n".join(json.dumps(row) for row in self.infusions) + "\n")

    def audit(self):
        self.write_inputs()
        before = {path: path.read_bytes() for directory in (self.clinical, self.work) for path in directory.iterdir()}
        result = audit_descriptives(self.clinical, self.work)
        self.assertTrue(all(path.read_bytes() == value for path, value in before.items()))
        return result

    def test_duplicate_drug_rows_and_simultaneous_drugs_count_each_event_once(self):
        result = self.audit()
        self.assertEqual(result["adult_cohort"], {"stays": 4, "patients": 3, "admissions": 4})
        self.assertEqual(result["primary_events"]["stays"], 2)
        self.assertEqual(result["primary_event_onset_drug"]["norepinephrine"], 1)
        self.assertEqual(result["primary_event_onset_drug"]["combination"], 1)
        self.assertEqual(result["primary_combination_details"], {"norepinephrine + vasopressin": 1})
        self.assertEqual(sum(result["primary_event_onset_drug"].values()), 2)

    def test_six_hour_boundary_and_first_careunit_event_distribution(self):
        result = self.audit()
        self.assertEqual(result["primary_event_timing"], {"at_or_before_6h": 1, "after_6h": 1})
        self.assertEqual(result["first_careunit"]["CSRU"]["primary_events"], 1)
        self.assertEqual(result["first_careunit"]["MICU"]["adult_stays"], 3)
        self.assertEqual(result["first_careunit"]["MICU"]["after_6h"], 1)

    def test_sepsis_uses_exact_admission_codes_not_prior_patient_diagnoses(self):
        result = self.audit()["retrospective_icd_sepsis"]["groups"]
        self.assertEqual(result["coded_99591_or_99592"]["adult_cohort"]["stays"], 1)
        self.assertEqual(result["coded_99591_or_99592"]["primary_events"]["stays"], 1)
        self.assertEqual(result["no_recorded_99591_99592"]["adult_cohort"]["stays"], 3)
        self.assertEqual(result["no_recorded_99591_99592"]["primary_events"]["stays"], 1)
        # One patient has admissions in both groups; patient counts need not add.
        self.assertEqual(sum(group["adult_cohort"]["patients"] for group in result.values()), 4)

    def test_later_drug_switch_does_not_relabel_exact_onset_category(self):
        first = self.infusions[0]
        first["ENDTIME"] = (self.records[0].event_time + timedelta(minutes=30)).isoformat()
        self.infusions.append(infusion(self.records[0], 222315, self.records[0].event_time + timedelta(minutes=30)))
        result = self.audit()
        self.assertEqual(result["primary_event_onset_drug"]["norepinephrine"], 1)
        self.assertEqual(result["primary_event_onset_drug"]["combination"], 1)

    def test_only_aggregates_no_patient_identifiers_or_timestamps_returned(self):
        text = json.dumps(self.audit())
        self.assertNotIn("subject_id", text)
        self.assertNotIn("icustay_id", text)
        self.assertNotIn("2200-01-01", text)
        self.assertNotIn(str(self.root), text)

    def test_first_careunit_identity_conflict_fails(self):
        self.icu_rows[0]["HADM_ID"] = 999
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, "careunit_identity_mismatch"):
            audit_descriptives(self.clinical, self.work)

    def test_diagnosis_identity_conflict_fails(self):
        self.diagnoses[0]["SUBJECT_ID"] = 999
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, "diagnosis_admission_identity_mismatch"):
            audit_descriptives(self.clinical, self.work)

    def test_unknown_or_missing_careunit_is_explicit(self):
        self.icu_rows[0]["FIRST_CAREUNIT"] = "unrecognized"
        result = self.audit()
        self.assertEqual(result["first_careunit"]["UNKNOWN"]["primary_events"], 1)

    def test_saved_event_must_match_validated_first_use_cache(self):
        self.infusions[0]["ENDTIME"] = (self.records[0].event_time + timedelta(minutes=30)).isoformat()
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, "saved_primary_event_does_not_match"):
            audit_descriptives(self.clinical, self.work)

    def test_new_uncertainty_cannot_silently_change_saved_cohort(self):
        self.infusions[0]["STATUSDESCRIPTION"] = "Flushed"
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, "saved_cohort_infusion_uncertainty_changed"):
            audit_descriptives(self.clinical, self.work)


if __name__ == "__main__":
    unittest.main()
