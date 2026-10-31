"""Read-only adult MetaVision cohort construction and conservative row alignment.

Only aggregate counters leave these functions alongside in-memory study records.
Callers are responsible for keeping patient-level objects in ignored local files.
No source CSV, cache, or derived output is written by this module.
"""

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping

from .timing import _time_key, _validate_timestamps
from .vasopressors import Infusion, parse_infusion_row, uncertain_parse_reason


@dataclass(frozen=True)
class StudyStay:
    subject_id: int
    hadm_id: int
    icustay_id: int
    intime: datetime
    outtime: datetime
    followup_end: datetime
    age_years: int | None
    age_obscured: bool


@dataclass(frozen=True)
class _Admission:
    subject_id: int
    admittime: datetime
    dischtime: datetime
    deathtime: datetime | None


def _value(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _id(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError("identifiers must be positive")
    return result


def _time(value: str) -> datetime:
    if " " not in value and "T" not in value:
        raise ValueError("clock precision is required")
    return datetime.fromisoformat(value)


def _optional_id(value: str) -> int | None:
    try:
        return _id(value)
    except (ValueError, TypeError):
        return None


def _read_csv(root: Path, name: str):
    with (root / name).open(newline="", encoding="utf-8-sig") as handle:
        yield from csv.DictReader(handle)


def load_adult_metavision_stays(
    clinical_root: str | Path,
) -> tuple[list[StudyStay], dict[str, int]]:
    """Load PATIENTS, ADMISSIONS and ICUSTAYS without reading large event tables.

    Age is completed calendar years at ICU entry, not a days/365 approximation.
    Ages above 89 are treated as obscured: they remain adult, but the shifted age
    is never returned as a clinical value. DOB after ICU entry is invalid.
    Outcome follow-up ends at the earliest ICU exit, hospital discharge or death.
    Missing/invalid identity or required clocks, conflicting duplicate IDs and
    nonpositive follow-up are excluded with aggregate audit counters. ICU stays
    without hospital-admission overlap or beginning before ADMITTIME are also
    excluded, rather than silently creating pre-admission prediction windows.

    Exact duplicate rows are counted and deduplicated. Conflicting duplicate
    source identities are never resolved by taking whichever row appears last.
    """
    root = Path(clinical_root)
    stats: Counter[str] = Counter(dict.fromkeys((
        "patient_rows", "admission_rows", "icu_rows", "metavision_rows",
        "invalid_patient_identity_rows", "invalid_patient_dob_rows", "duplicate_patient_id_rows",
        "invalid_admission_identity_rows", "invalid_admission_time_rows", "duplicate_admission_id_rows",
        "unparseable_or_mixed_admission_time_rows", "admission_discharge_not_after_admit_rows",
        "admission_death_before_admit_rows", "invalid_icu_identity_rows", "duplicate_icu_id_rows",
        "non_metavision_rows", "missing_or_invalid_patient_rows", "missing_or_invalid_admission_rows",
        "admission_subject_mismatch_rows", "invalid_icu_time_rows", "birth_after_icu_entry_rows",
        "under_18_rows", "nonpositive_followup_rows", "icu_admission_no_overlap_rows",
        "icu_entry_before_admission_rows",
    ), 0))
    patients: dict[int, date] = {}
    bad_patients: set[int] = set()
    for row in _read_csv(root, "PATIENTS.csv"):
        stats["patient_rows"] += 1
        try:
            subject = _id(_value(row, "SUBJECT_ID"))
        except ValueError:
            stats["invalid_patient_identity_rows"] += 1
            continue
        try:
            dob = datetime.fromisoformat(_value(row, "DOB")).date()
        except ValueError:
            stats["invalid_patient_dob_rows"] += 1
            bad_patients.add(subject)
            continue
        if subject in patients:
            stats["duplicate_patient_id_rows"] += 1
            if patients[subject] != dob:
                bad_patients.add(subject)
        else:
            patients[subject] = dob

    admissions: dict[int, _Admission] = {}
    bad_admissions: set[int] = set()
    for row in _read_csv(root, "ADMISSIONS.csv"):
        stats["admission_rows"] += 1
        try:
            hadm = _id(_value(row, "HADM_ID"))
        except ValueError:
            stats["invalid_admission_identity_rows"] += 1
            continue
        try:
            subject = _id(_value(row, "SUBJECT_ID"))
        except ValueError:
            stats["invalid_admission_identity_rows"] += 1
            bad_admissions.add(hadm)
            continue
        time_reason = "unparseable_or_mixed_admission_time_rows"
        try:
            admit = _time(_value(row, "ADMITTIME"))
            discharge = _time(_value(row, "DISCHTIME"))
            death = _time(_value(row, "DEATHTIME")) if _value(row, "DEATHTIME") else None
            _validate_timestamps([admit, discharge] + ([death] if death else []))
            if _time_key(discharge) <= _time_key(admit):
                time_reason = "admission_discharge_not_after_admit_rows"
                raise ValueError("invalid admission interval")
            if death is not None and _time_key(death) < _time_key(admit):
                time_reason = "admission_death_before_admit_rows"
                raise ValueError("death before admission")
        except (ValueError, TypeError):
            stats["invalid_admission_time_rows"] += 1
            stats[time_reason] += 1
            bad_admissions.add(hadm)
            continue
        record = _Admission(subject, admit, discharge, death)
        if hadm in admissions:
            stats["duplicate_admission_id_rows"] += 1
            if admissions[hadm] != record:
                bad_admissions.add(hadm)
        else:
            admissions[hadm] = record

    candidates: dict[int, StudyStay] = {}
    seen_icu: dict[int, tuple[str, ...]] = {}
    bad_icu: set[int] = set()
    identity_fields = ("SUBJECT_ID", "HADM_ID", "DBSOURCE", "INTIME", "OUTTIME")
    for row in _read_csv(root, "ICUSTAYS.csv"):
        stats["icu_rows"] += 1
        try:
            icu = _id(_value(row, "ICUSTAY_ID"))
        except ValueError:
            stats["invalid_icu_identity_rows"] += 1
            continue
        signature = tuple(_value(row, field) for field in identity_fields)
        if icu in seen_icu:
            stats["duplicate_icu_id_rows"] += 1
            if seen_icu[icu] != signature:
                bad_icu.add(icu)
            continue
        seen_icu[icu] = signature
        if _value(row, "DBSOURCE").casefold() != "metavision":
            stats["non_metavision_rows"] += 1
            continue
        stats["metavision_rows"] += 1
        try:
            subject = _id(_value(row, "SUBJECT_ID"))
            hadm = _id(_value(row, "HADM_ID"))
        except ValueError:
            stats["invalid_icu_identity_rows"] += 1
            continue
        if subject not in patients or subject in bad_patients:
            stats["missing_or_invalid_patient_rows"] += 1
            continue
        if hadm not in admissions or hadm in bad_admissions:
            stats["missing_or_invalid_admission_rows"] += 1
            continue
        admission = admissions[hadm]
        if admission.subject_id != subject:
            stats["admission_subject_mismatch_rows"] += 1
            continue
        try:
            intime, outtime = _time(_value(row, "INTIME")), _time(_value(row, "OUTTIME"))
            clocks = [intime, outtime, admission.admittime, admission.dischtime]
            if admission.deathtime:
                clocks.append(admission.deathtime)
            _validate_timestamps(clocks)
            if _time_key(outtime) <= _time_key(intime):
                raise ValueError("invalid ICU interval")
        except (ValueError, TypeError):
            stats["invalid_icu_time_rows"] += 1
            continue
        if (_time_key(outtime) <= _time_key(admission.admittime)
                or _time_key(intime) >= _time_key(admission.dischtime)):
            stats["icu_admission_no_overlap_rows"] += 1
            continue
        if _time_key(intime) < _time_key(admission.admittime):
            stats["icu_entry_before_admission_rows"] += 1
            continue
        dob = patients[subject]
        if dob > intime.date():
            stats["birth_after_icu_entry_rows"] += 1
            continue
        age = intime.year - dob.year - ((intime.month, intime.day) < (dob.month, dob.day))
        if age < 18:
            stats["under_18_rows"] += 1
            continue
        ends = [outtime, admission.dischtime]
        if admission.deathtime:
            ends.append(admission.deathtime)
        followup = min(ends, key=_time_key)
        if _time_key(followup) <= _time_key(intime):
            stats["nonpositive_followup_rows"] += 1
            continue
        candidates[icu] = StudyStay(subject, hadm, icu, intime, outtime, followup,
                                    None if age > 89 else age, age > 89)

    stays = [stay for icu, stay in candidates.items() if icu not in bad_icu]
    stays.sort(key=lambda stay: (stay.subject_id, stay.hadm_id, _time_key(stay.intime), stay.icustay_id))
    stats["conflicting_or_invalid_patient_ids"] = len(bad_patients)
    stats["conflicting_or_invalid_admission_ids"] = len(bad_admissions)
    stats["conflicting_icu_ids"] = len(bad_icu)
    stats["adult_metavision_stays"] = len(stays)
    stats["adult_metavision_subjects"] = len({stay.subject_id for stay in stays})
    stats["adult_metavision_admissions"] = len({stay.hadm_id for stay in stays})
    stats["age_obscured_stays"] = sum(stay.age_obscured for stay in stays)
    stats["followup_shortened_stays"] = sum(_time_key(stay.followup_end) < _time_key(stay.outtime) for stay in stays)
    stats["death_limits_followup_stays"] = sum(
        admission.deathtime is not None and _time_key(admission.deathtime) == _time_key(stay.followup_end)
        and _time_key(stay.followup_end) < _time_key(stay.outtime)
        for stay in stays for admission in [admissions[stay.hadm_id]]
    )
    stats["discharge_limits_followup_stays"] = sum(
        _time_key(admissions[stay.hadm_id].dischtime) == _time_key(stay.followup_end)
        and _time_key(stay.followup_end) < _time_key(stay.outtime)
        for stay in stays
    )
    return stays, dict(stats)


def _overlaps(stay: StudyStay, start: datetime, end: datetime) -> bool:
    _validate_timestamps([stay.intime, stay.outtime, start, end])
    return _time_key(start) < _time_key(stay.outtime) and _time_key(end) > _time_key(stay.intime)


def align_infusions(
    stays: Iterable[StudyStay],
    selected_rows_path: str | Path,
) -> tuple[dict[int, list[Infusion]], dict[int, set[str]], dict[str, object]]:
    """Read selected five-drug JSONL rows and align them without borrowing IDs.

    A missing ICUSTAY_ID is resolved only for one same-subject/admission time
    overlap. A supplied target ID must agree with identity and overlap that ICU;
    then a truly continuous row spanning further same-admission ICUs can be
    carried to each overlapping ICU, preserving its original start for prevalent
    exposure exclusion. A provided but unknown/out-of-cohort ID cannot establish
    a valid anchor and flags overlapping target stays as uncertain.

    A supplied target ID with no overlap in ANY study ICU is counted as outside
    the study interval. If it overlaps a different ICU instead, both the stated
    and potential target stays are uncertain; no valid row is silently remapped.
    Out-of-cohort admissions are not joined merely by SUBJECT_ID.

    Rejected uncertain rows flag every plausibly affected stay using available
    identity and time information. Missing/invalid clocks broaden attribution to
    the identifiable admission (or subject if admission is also unknown). Rows
    without enough identity to attribute are counted explicitly; callers must
    assess these rather than assuming they establish clean negative follow-up.
    Uncertain rows demonstrably outside target subjects/admissions/time intervals
    are counted separately and do not become unassignable in-cohort uncertainty.

    Returned mappings have only nonempty entries. Uncertainty never disappears
    merely because another valid row was also mapped to that stay. Structural
    alignment uses ICU OUTTIME; endpoint derivation must use followup_end, which
    can be earlier due to hospital discharge/death.
    """
    stays = tuple(stays)
    by_icu = {stay.icustay_id: stay for stay in stays}
    if len(by_icu) != len(stays):
        raise ValueError("stays must have unique ICUSTAY_ID values")
    by_admission: dict[tuple[int, int], list[StudyStay]] = defaultdict(list)
    by_subject: dict[int, list[StudyStay]] = defaultdict(list)
    by_hadm: dict[int, list[StudyStay]] = defaultdict(list)
    for stay in stays:
        by_admission[(stay.subject_id, stay.hadm_id)].append(stay)
        by_subject[stay.subject_id].append(stay)
        by_hadm[stay.hadm_id].append(stay)
    mapped: dict[int, list[Infusion]] = defaultdict(list)
    uncertain: dict[int, set[str]] = defaultdict(set)
    stats: Counter[str] = Counter(dict.fromkeys((
        "selected_rows", "malformed_cache_rows", "unassignable_uncertain_rows",
        "raw_missing_icustay_id_rows", "uncertain_parser_rows", "assigned_uncertain_rows",
        "accepted_parser_rows", "accepted_missing_storetime_rows", "mismatched_id_rows",
        "admission_identity_mismatch_rows", "alignment_clock_mismatch_rows",
        "accepted_missing_icustay_id_rows", "missing_id_unresolved_rows", "missing_id_ambiguous_rows",
        "missing_id_no_target_overlap_rows", "missing_id_resolved_rows", "outside_target_interval_rows",
        "outside_target_admission_rows", "unverified_provided_id_rows", "cross_icu_rows",
        "mapped_source_rows", "mapped_interval_assignments", "pre_icu_ongoing_assignments",
        "starts_after_effective_followup_assignments",
        "outside_target_subject_uncertain_rows", "outside_target_admission_uncertain_rows",
        "outside_target_interval_uncertain_rows",
    ), 0))
    reasons: Counter[str] = Counter()
    lags: list[float] = []

    def flag(targets: Iterable[StudyStay], reason: str) -> None:
        for target in targets:
            uncertain[target.icustay_id].add(reason)

    def plausible_uncertain_targets(raw: Mapping[str, object]) -> list[StudyStay]:
        subject = _optional_id(_value(raw, "SUBJECT_ID"))
        hadm = _optional_id(_value(raw, "HADM_ID"))
        icu = _optional_id(_value(raw, "ICUSTAY_ID"))
        if subject is not None and hadm is not None:
            possible = list(by_admission.get((subject, hadm), []))
            if not possible and by_hadm.get(hadm):
                # Known admission with a contradictory subject: neither identity
                # can establish a clean negative. This is not a subject-only join
                # of a valid, separate out-of-cohort admission.
                possible = list(by_hadm[hadm]) + list(by_subject.get(subject, []))
        elif subject is not None:
            possible = list(by_subject.get(subject, []))
        elif hadm is not None:
            possible = list(by_hadm.get(hadm, []))
        else:
            possible = []
        try:
            start, end = _time(_value(raw, "STARTTIME")), _time(_value(raw, "ENDTIME"))
            _validate_timestamps([start, end])
            if _time_key(end) <= _time_key(start):
                raise ValueError("unknown interval")
            possible = [stay for stay in possible if _overlaps(stay, start, end)]
        except (ValueError, TypeError):
            pass  # Cannot safely narrow a malformed interval by time.
        if icu in by_icu:
            possible.append(by_icu[icu])
        return list({stay.icustay_id: stay for stay in possible}.values())

    def outside_uncertainty_reason(raw: Mapping[str, object]) -> str | None:
        """Separate attributable out-of-scope rows from unknown source coverage."""
        subject = _optional_id(_value(raw, "SUBJECT_ID"))
        hadm = _optional_id(_value(raw, "HADM_ID"))
        if subject is not None and hadm is not None:
            possible = by_admission.get((subject, hadm), [])
            if not possible:
                if by_hadm.get(hadm):
                    return None  # Contradictory known identities remain unknown.
                return "outside_target_admission_uncertain_rows"
        elif subject is not None:
            possible = by_subject.get(subject, [])
            if not possible:
                return "outside_target_subject_uncertain_rows"
        elif hadm is not None:
            possible = by_hadm.get(hadm, [])
            if not possible:
                return "outside_target_admission_uncertain_rows"
        else:
            return None
        try:
            start, end = _time(_value(raw, "STARTTIME")), _time(_value(raw, "ENDTIME"))
            _validate_timestamps([start, end])
            if _time_key(end) > _time_key(start) and not any(_overlaps(stay, start, end) for stay in possible):
                return "outside_target_interval_uncertain_rows"
        except (ValueError, TypeError):
            pass
        return None

    with Path(selected_rows_path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            stats["selected_rows"] += 1
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("row must be an object")
            except (ValueError, TypeError):
                stats["malformed_cache_rows"] += 1
                stats["unassignable_uncertain_rows"] += 1
                reasons["uncertain_malformed_cache_row"] += 1
                continue
            if not _value(raw, "ICUSTAY_ID"):
                stats["raw_missing_icustay_id_rows"] += 1
            infusion, reason = parse_infusion_row(raw)
            reasons[reason] += 1
            if infusion is None:
                if uncertain_parse_reason(reason):
                    stats["uncertain_parser_rows"] += 1
                    targets = plausible_uncertain_targets(raw)
                    flag(targets, reason)
                    if targets:
                        stats["assigned_uncertain_rows"] += 1
                    else:
                        stats[outside_uncertainty_reason(raw) or "unassignable_uncertain_rows"] += 1
                continue
            stats["accepted_parser_rows"] += 1
            if infusion.storetime is None:
                stats["accepted_missing_storetime_rows"] += 1
            else:
                lags.append((_time_key(infusion.storetime) - _time_key(infusion.start)).total_seconds())
            admission_stays = by_admission.get((infusion.subject_id, infusion.hadm_id), [])
            declared = by_icu.get(infusion.icustay_id)
            if by_hadm.get(infusion.hadm_id) and not admission_stays:
                stats["admission_identity_mismatch_rows"] += 1
                flag(plausible_uncertain_targets(raw), "row_admission_subject_mismatch")
                continue
            if declared and (declared.subject_id != infusion.subject_id or declared.hadm_id != infusion.hadm_id):
                stats["mismatched_id_rows"] += 1
                flag([declared] + plausible_uncertain_targets(raw), "provided_icu_identity_mismatch")
                continue
            try:
                overlaps = [stay for stay in admission_stays if _overlaps(stay, infusion.start, infusion.end)]
            except (ValueError, TypeError):
                stats["alignment_clock_mismatch_rows"] += 1
                flag(admission_stays, "alignment_clock_mismatch")
                continue
            if infusion.icustay_id is None:
                stats["accepted_missing_icustay_id_rows"] += 1
                if len(overlaps) != 1:
                    stats["missing_id_unresolved_rows"] += 1
                    if len(overlaps) > 1:
                        flag(overlaps, "missing_icu_multiple_overlaps")
                        stats["missing_id_ambiguous_rows"] += 1
                    else:
                        stats["missing_id_no_target_overlap_rows"] += 1
                    continue
                stats["missing_id_resolved_rows"] += 1
                targets = overlaps
            else:
                if not overlaps:
                    stats["outside_target_interval_rows" if admission_stays else "outside_target_admission_rows"] += 1
                    continue
                if declared is None:
                    stats["unverified_provided_id_rows"] += 1
                    stats["mismatched_id_rows"] += 1
                    flag(overlaps, "unverified_provided_icu")
                    continue
                if declared not in overlaps:
                    stats["mismatched_id_rows"] += 1
                    flag([declared] + overlaps, "provided_icu_time_mismatch")
                    continue
                targets = overlaps
                if len(targets) > 1:
                    stats["cross_icu_rows"] += 1
            stats["mapped_source_rows"] += 1
            for target in targets:
                mapped[target.icustay_id].append(infusion)
                stats["mapped_interval_assignments"] += 1
                if _time_key(infusion.start) < _time_key(target.intime):
                    stats["pre_icu_ongoing_assignments"] += 1
                if _time_key(infusion.start) >= _time_key(target.followup_end):
                    stats["starts_after_effective_followup_assignments"] += 1

    for records in mapped.values():
        records.sort(key=lambda record: (_time_key(record.start), _time_key(record.end), record.itemid))
    stats["stays_with_mapped_infusions"] = len(mapped)
    stats["uncertain_stays"] = len(uncertain)
    stats["stays_with_mapped_infusions_and_uncertainty"] = len(set(mapped) & set(uncertain))
    lag_summary = {
        "scope": "all accepted parser rows before cohort alignment",
        "count": len(lags),
        "before_start_count": sum(lag < 0 for lag in lags),
        "at_start_count": sum(lag == 0 for lag in lags),
        "after_start_count": sum(lag > 0 for lag in lags),
        "minimum_seconds": min(lags) if lags else None,
        "maximum_seconds": max(lags) if lags else None,
        "mean_seconds": sum(lags) / len(lags) if lags else None,
        "median_seconds": median(lags) if lags else None,
    }
    return dict(mapped), dict(uncertain), {
        **dict(stats), "raw_parser_reasons": dict(reasons), "storetime_delay": lag_summary,
    }
