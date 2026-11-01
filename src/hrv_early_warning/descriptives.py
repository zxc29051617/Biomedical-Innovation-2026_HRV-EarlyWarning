"""Retrospective cohort composition, never eligibility or prediction features.

This read-only audit uses the existing adult cohort and already-selected infusion
cache. It does not scan CHARTEVENTS, read ECG, inspect model predictions or alter
an endpoint, split or threshold. Its return value contains aggregates only.
"""

from collections import Counter
import csv
from datetime import timedelta
from pathlib import Path

from .cohort import align_infusions
from .risksets import load_stay_outcomes
from .timing import _time_key
from .vasopressors import PRIMARY_ITEMIDS, derive_first_use


PRIMARY_DRUGS = {221906: "norepinephrine", 221289: "epinephrine",
                 222315: "vasopressin", 221749: "phenylephrine"}
CAREUNITS = frozenset({"CCU", "CSRU", "MICU", "SICU", "TSICU", "NICU"})
SEPSIS_CODES = frozenset({"99591", "99592"})


def _positive_id(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise ValueError("invalid_clinical_identity") from None
    if number <= 0:
        raise ValueError("invalid_clinical_identity")
    return number


def _read_careunits(root, records):
    target = {record.icustay_id: record for record in records}
    units, rows = {}, 0
    with (root / "ICUSTAYS.csv").open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            icu = _positive_id(row.get("ICUSTAY_ID"))
            if icu not in target:
                continue
            expected = target[icu]
            if (_positive_id(row.get("SUBJECT_ID")), _positive_id(row.get("HADM_ID"))) != (expected.subject_id, expected.hadm_id):
                raise ValueError("careunit_identity_mismatch")
            unit = (row.get("FIRST_CAREUNIT") or "").strip().upper()
            unit = unit if unit in CAREUNITS else "UNKNOWN"
            if icu in units and units[icu] != unit:
                raise ValueError("conflicting_first_careunit")
            units[icu] = unit
    return units, {"icustays_rows": rows, "adult_stays_without_careunit_row": len(target.keys() - units.keys())}


def _read_coded_admissions(root, records):
    admissions = {}
    for record in records:
        previous = admissions.setdefault(record.hadm_id, record.subject_id)
        if previous != record.subject_id:
            raise ValueError("cohort_admission_identity_conflict")
    coded, rows, code_rows = set(), 0, 0
    with (root / "DIAGNOSES_ICD.csv").open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            hadm = _positive_id(row.get("HADM_ID"))
            if hadm not in admissions:
                continue
            if _positive_id(row.get("SUBJECT_ID")) != admissions[hadm]:
                raise ValueError("diagnosis_admission_identity_mismatch")
            code = (row.get("ICD9_CODE") or "").strip().replace(".", "")
            if code in SEPSIS_CODES:
                code_rows += 1
                coded.add(hadm)
    return coded, {"diagnoses_rows": rows, "target_admission_sepsis_code_rows_including_duplicates": code_rows}


def _drug_counts():
    return Counter(dict.fromkeys((*PRIMARY_DRUGS.values(), "combination"), 0))


def audit_descriptives(clinical_root, feasibility_dir):
    """Describe the unchanged adult cohort and primary first-use events.

    Exact-onset primary drug sets count each event once: one primary drug maps
    to its name, >=2 simultaneous primary drugs map to 'combination'. Duplicate
    rows do not duplicate a drug; a later drug switch does not change the onset
    category. Combination details partition the combination category, rather
    than adding events to the single-drug categories. Dopamine is not a primary
    event drug and cannot fill primary-drug duration gaps.

    ICD-9 99591/99592 are admission-level retrospective recorded diagnoses. The
    unmarked group means no recorded selected code, not absence of sepsis. This
    is neither Sepsis-3 nor infection eligibility known at prediction time.
    Patients can occur in both descriptive groups across different admissions;
    stays/admissions partition the cohort, but subgroup patient counts need not.
    """
    root, work = Path(clinical_root), Path(feasibility_dir)
    records, source_counts = load_stay_outcomes(work / "stay_outcomes.jsonl")
    careunits, care_audit = _read_careunits(root, records)
    coded, diagnosis_audit = _read_coded_admissions(root, records)
    infusions, uncertain, infusion_audit = align_infusions(
        [record.stay for record in records], work / "vasopressor_rows.jsonl")
    if infusion_audit.get("unassignable_uncertain_rows", 0):
        raise ValueError("unassignable_infusion_uncertainty_in_descriptive_audit")
    for record in records:
        if bool(uncertain.get(record.icustay_id)) != bool(record.uncertain_reasons):
            raise ValueError("saved_cohort_infusion_uncertainty_changed")

    groups = {name: {
        "records": [], "events": [], "drug_counts": _drug_counts(), "timing": Counter({"at_or_before_6h": 0, "after_6h": 0}),
        "adult_careunits": Counter(), "event_careunits": Counter(),
    } for name in ("coded_99591_or_99592", "no_recorded_99591_99592")}
    adult_units, event_units, combinations = Counter(), Counter(), Counter()
    unit_timing = {}
    onset_drugs = _drug_counts()
    timing = Counter({"at_or_before_6h": 0, "after_6h": 0})
    outcomes = Counter()
    events = []
    for record in records:
        unit = careunits.get(record.icustay_id, "UNKNOWN")
        group_name = "coded_99591_or_99592" if record.hadm_id in coded else "no_recorded_99591_99592"
        group = groups[group_name]
        group["records"].append(record)
        group["adult_careunits"][unit] += 1
        adult_units[unit] += 1
        if record.uncertain_reasons:
            outcomes["uncertain_infusion_excluded"] += 1
            continue
        if record.outcome is None:
            outcomes["missing_outcome_excluded"] += 1
            continue
        outcomes[record.outcome.event_status] += 1
        if record.event_time is None:
            continue
        rows = infusions.get(record.icustay_id, ())
        verified = derive_first_use(rows, record.intime, record.followup_end,
                                    PRIMARY_ITEMIDS, 60, maximum_gap_minutes=0)
        if verified.event_time != record.event_time or verified.event_status != "qualifying_first_use":
            raise ValueError("saved_primary_event_does_not_match_validated_infusions")
        names = sorted({PRIMARY_DRUGS[row.itemid] for row in rows
                        if row.itemid in PRIMARY_ITEMIDS and _time_key(row.start) == _time_key(record.event_time)})
        if not names:
            raise ValueError("primary_event_has_no_exact_onset_primary_drug")
        category = names[0] if len(names) == 1 else "combination"
        if len(names) > 1:
            combinations[" + ".join(names)] += 1
        temporal = "at_or_before_6h" if _time_key(record.event_time) <= _time_key(record.intime) + timedelta(hours=6) else "after_6h"
        events.append(record)
        group["events"].append(record)
        group["drug_counts"][category] += 1
        group["timing"][temporal] += 1
        group["event_careunits"][unit] += 1
        onset_drugs[category] += 1
        timing[temporal] += 1
        event_units[unit] += 1
        unit_timing.setdefault(unit, Counter({"at_or_before_6h": 0, "after_6h": 0}))[temporal] += 1

    def population(items):
        return {"stays": len(items), "patients": len({r.subject_id for r in items}),
                "admissions": len({r.hadm_id for r in items})}

    subgroup_report = {}
    for name, group in groups.items():
        subgroup_report[name] = {
            "adult_cohort": population(group["records"]), "primary_events": population(group["events"]),
            "primary_event_timing": dict(group["timing"]),
            "primary_event_onset_drug": dict(group["drug_counts"]),
            "adult_first_careunit": dict(sorted(group["adult_careunits"].items())),
            "primary_event_first_careunit": dict(sorted(group["event_careunits"].items())),
        }
    if sum(onset_drugs.values()) != len(events) or sum(timing.values()) != len(events):
        raise ValueError("descriptive_event_partition_mismatch")
    return {
        "schema_version": 1,
        "scope": "retrospective_composition_of_existing_adult_metavision_cohort_no_model_scores",
        "adult_cohort": population(records), "outcome_status_counts": dict(outcomes),
        "primary_events": population(events),
        "primary_event_onset_drug": dict(onset_drugs),
        "primary_combination_details": dict(sorted(combinations.items())),
        "primary_event_timing": dict(timing),
        "first_careunit": {unit: {
            "adult_stays": adult_units[unit], "primary_events": event_units[unit],
            **dict(unit_timing.get(unit, {"at_or_before_6h": 0, "after_6h": 0})),
        } for unit in sorted(adult_units)},
        "retrospective_icd_sepsis": {
            "codes": sorted(SEPSIS_CODES), "unit": "same_hospital_admission",
            "groups": subgroup_report,
        },
        "source_audit": {**care_audit, **diagnosis_audit, "saved_outcome_counts": source_counts,
                         "selected_infusion_rows": infusion_audit["selected_rows"],
                         "raw_infusion_parser_reasons": infusion_audit["raw_parser_reasons"],
                         "uncertain_infusion_stays": len(uncertain)},
        "limitations": [
            "Selected discharge ICD codes are retrospective descriptive labels, not real-time infection eligibility or Sepsis-3.",
            "No recorded selected sepsis code does not establish that sepsis was absent.",
            "First care unit is the admission unit recorded in ICUSTAYS, not necessarily the bedside location at the event.",
            "Onset categories count primary drugs starting at the exact event time; combination details partition that category without double-counting events.",
            "Patient counts across retrospective groups can overlap across admissions; group stays and admissions are the disjoint units.",
            "Observed drug initiation reflects treatment and clinical decisions; these descriptions do not establish shock cause, treatment necessity or HRV model benefit.",
            "Cohort, endpoint, thresholds and patient splits are unchanged; these descriptions are not model features.",
        ],
    }
