"""Read-only, aggregate inventory of local MIMIC-III and matched waveforms.

Only four small clinical tables are scanned. Large event tables are opened for
their headers only. Patient identifiers remain internal to the calculation.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path, PurePosixPath
import re
from typing import Any


SCHEMA_VERSION = "1.0"
CLINICAL_SCHEMAS = {
    "PATIENTS.csv": {"SUBJECT_ID"},
    "ADMISSIONS.csv": {"SUBJECT_ID", "HADM_ID"},
    "ICUSTAYS.csv": {"SUBJECT_ID", "HADM_ID", "ICUSTAY_ID", "DBSOURCE"},
    "DIAGNOSES_ICD.csv": {"SUBJECT_ID", "HADM_ID", "ICD9_CODE"},
    "CHARTEVENTS.csv": {"SUBJECT_ID", "ITEMID", "CHARTTIME"},
    "LABEVENTS.csv": {"SUBJECT_ID", "ITEMID", "CHARTTIME"},
    "MICROBIOLOGYEVENTS.csv": {"SUBJECT_ID", "CHARTDATE"},
    "INPUTEVENTS_MV.csv": {"SUBJECT_ID", "ITEMID", "STARTTIME", "ENDTIME"},
    "INPUTEVENTS_CV.csv": {"SUBJECT_ID", "ITEMID", "CHARTTIME"},
    "OUTPUTEVENTS.csv": {"SUBJECT_ID", "ITEMID", "CHARTTIME"},
    "PRESCRIPTIONS.csv": {"SUBJECT_ID", "STARTDATE", "DRUG"},
    "D_ITEMS.csv": {"ITEMID", "LABEL", "LINKSTO"},
}
SCANNED_TABLES = ("PATIENTS.csv", "ADMISSIONS.csv", "ICUSTAYS.csv", "DIAGNOSES_ICD.csv")
INDEX_NAMES = ("RECORDS", "RECORDS-waveforms", "RECORDS-numerics")
COHORT_RULES = {
    "myocardial_infarction": "410*",
    "heart_failure": "428*",
    "sepsis": "99591 or 99592",
    "septic_shock": "78552",
    "cardiac_arrest": "4275",
}
KNOWN_CHANNELS = {
    "I", "II", "III", "V", "V1", "V2", "V3", "V4", "V5", "V6",
    "AVR", "AVL", "AVF", "MCL", "MCL1", "ECG", "ECG1", "ECG2",
    "ABP", "ART", "AOBP", "CVP", "PAP", "ICP", "PLETH", "RESP",
    "CO2", "ETCO2", "SPO2", "HR", "PULSE", "RR",
}


def parse_waveform_subject(entry: str) -> int | None:
    """Parse a six-digit patient directory, never a record's timestamp."""
    path = PurePosixPath(entry.strip())
    if path.is_absolute() or ".." in path.parts:
        return None
    matches = re.findall(r"(?:^|/)p(\d{6})(?=/|$)", entry.strip())
    return int(matches[0]) if len(matches) == 1 else None


def _read_table_header(path: Path, required: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok", "size_bytes": None, "columns": []}
    try:
        result["size_bytes"] = path.stat().st_size
        # A text decoder may prefetch and decode body bytes. Read exactly the
        # first physical line so even large/corrupt event bodies stay untouched.
        with path.open("rb") as stream:
            line = stream.readline(1024 * 1024)
        header = next(csv.reader([line.decode("utf-8-sig")]), None) if line else None
    except FileNotFoundError:
        result["status"] = "missing"
        return result
    except (OSError, UnicodeError, csv.Error):
        result["status"] = "unreadable"
        return result
    if not header:
        result["status"] = "empty"
        return result
    # Invalid input must not accidentally emit a patient row as column metadata.
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", field) for field in header):
        result["status"] = "invalid_header"
        result["missing_required_columns"] = sorted(required)
        return result
    result["columns"] = header
    missing = required - set(header)
    if missing or len(header) != len(set(header)):
        result["status"] = "invalid_header"
        result["missing_required_columns"] = sorted(missing)
    return result


def _read_index(path: Path, sample_size: int = 0) -> tuple[dict[str, Any], set[int] | None, list[str]]:
    subjects: set[int] = set()
    sample: list[str] = []
    entries = malformed = 0
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                entry = line.strip()
                if not entry:
                    continue
                entries += 1
                subject = parse_waveform_subject(entry)
                if subject is None:
                    malformed += 1
                else:
                    subjects.add(subject)
                if len(sample) < sample_size:
                    sample.append(entry)
    except FileNotFoundError:
        return {"status": "missing", "entries": None, "unique_subjects": None}, None, []
    except (OSError, UnicodeError):
        return {"status": "unreadable", "entries": None, "unique_subjects": None}, None, []
    return {
        "status": "ok", "entries": entries, "unique_subjects": len(subjects),
        "unparsed_entries": malformed,
    }, subjects, sample


def _identifier(value: str | None) -> int | None:
    if value is None or not value.strip().isdigit():
        return None
    return int(value)


def _matching_cohorts(code: str) -> list[str]:
    code = code.strip().replace(".", "")
    matches = []
    if code.startswith("410"):
        matches.append("myocardial_infarction")
    if code.startswith("428"):
        matches.append("heart_failure")
    if code in {"99591", "99592"}:
        matches.append("sepsis")
    if code == "78552":
        matches.append("septic_shock")
    if code == "4275":
        matches.append("cardiac_arrest")
    return matches


def _scan_table(path: Path, waveform_subjects: set[int] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    subjects: set[int] = set()
    admissions: set[int] = set()
    stays: set[int] = set()
    mv_subjects: set[int] = set()
    mv_stays: set[int] = set()
    mv_waveform_stays: set[int] = set()
    cohort_subjects = {name: set() for name in COHORT_RULES}
    cohort_admissions = {name: set() for name in COHORT_RULES}
    rows = invalid_rows = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            rows += 1
            subject = _identifier(row.get("SUBJECT_ID"))
            admission = _identifier(row.get("HADM_ID"))
            stay = _identifier(row.get("ICUSTAY_ID"))
            if subject is None or (path.name != "PATIENTS.csv" and admission is None) or (
                path.name == "ICUSTAYS.csv" and stay is None
            ):
                invalid_rows += 1
                continue
            subjects.add(subject)
            if admission is not None:
                admissions.add(admission)
            if stay is not None:
                stays.add(stay)
            if path.name == "ICUSTAYS.csv" and (row.get("DBSOURCE") or "").strip().lower() == "metavision":
                mv_subjects.add(subject)
                mv_stays.add(stay)
                if waveform_subjects is not None and subject in waveform_subjects:
                    mv_waveform_stays.add(stay)
            if path.name == "DIAGNOSES_ICD.csv":
                for name in _matching_cohorts(row.get("ICD9_CODE") or ""):
                    cohort_subjects[name].add(subject)
                    cohort_admissions[name].add(admission)
    summary: dict[str, Any] = {
        "rows": rows, "invalid_identifier_rows": invalid_rows,
        "unique_subjects": len(subjects),
    }
    if path.name != "PATIENTS.csv":
        summary["unique_admissions"] = len(admissions)
    if path.name == "ICUSTAYS.csv":
        summary["unique_icu_stays"] = len(stays)
        summary["metavision"] = {
            "unique_subjects": len(mv_subjects), "unique_icu_stays": len(mv_stays),
            "waveform_subject_overlap": (
                len(mv_subjects & waveform_subjects) if waveform_subjects is not None else None
            ),
            "icu_stays_with_subject_in_waveform_index": (
                len(mv_waveform_stays) if waveform_subjects is not None else None
            ),
        }
    cohorts = None
    if path.name == "DIAGNOSES_ICD.csv":
        cohorts = {
            name: {
                "icd9_rule": rule, "unique_subjects": len(cohort_subjects[name]),
                "unique_admissions": len(cohort_admissions[name]),
                "waveform_subject_overlap": (
                    len(cohort_subjects[name] & waveform_subjects)
                    if waveform_subjects is not None else None
                ),
            }
            for name, rule in COHORT_RULES.items()
        }
    return summary, cohorts


def _safe_path(root: Path, parent: Path, name: str) -> Path | None:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (parent / name).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _sample_headers(root: Path, entries: list[str]) -> dict[str, Any]:
    root = root.resolve()
    counts: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    rates: Counter[str] = Counter()

    def read_header(path: Path | None, kind: str) -> list[list[str]] | None:
        if path is None:
            counts["unsafe_paths"] += 1
            return None
        try:
            lines = [line.split() for line in path.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
        except FileNotFoundError:
            counts[f"missing_{kind}_headers"] += 1
            return None
        except (OSError, UnicodeError):
            counts["unreadable_headers"] += 1
            return None
        if not lines or len(lines[0]) < 3:
            counts["invalid_headers"] += 1
            return None
        counts["headers_read"] += 1
        return lines

    def signal_channels(lines: list[list[str]]) -> set[str]:
        result: set[str] = set()
        for line in lines[1:]:
            if len(line) >= 9:
                name = line[-1].upper()
                result.add(name if name in KNOWN_CHANNELS else "OTHER")
        return result

    for entry in entries:
        path = _safe_path(root, root, entry + ".hea")
        master = read_header(path, "master")
        if master is None:
            continue
        counts["master_headers_read"] += 1
        rate = master[0][2].split("/")[0].split("(")[0]
        try:
            numeric_rate = float(rate)
            if numeric_rate <= 0 or not numeric_rate < float("inf"):
                raise ValueError
            rates[f"{numeric_rate:g}"] += 1
        except ValueError:
            counts["invalid_sampling_rates"] += 1
        names: set[str] = set()
        if "/" in master[0][0]:
            for segment in master[1:]:
                if not segment or segment[0] == "~":
                    continue
                leaf = _safe_path(root, path.parent, segment[0] + ".hea")
                lines = read_header(leaf, "segment")
                if lines is not None:
                    names = signal_channels(lines)
                    if names:
                        break
        else:
            names = signal_channels(master)
        if names:
            counts["records_with_channels"] += 1
            channels.update(names)
        if "II" in names:
            counts["records_with_lead_ii"] += 1
    return {
        "selection": "first N non-empty waveform index entries; not a random sample",
        "requested_records": len(entries),
        "counts": dict(sorted(counts.items())),
        "sampling_rate_hz_record_counts": dict(sorted(rates.items())),
        "channel_record_counts": dict(sorted(channels.items())),
        "scope": "First available signal or layout header per record; not all segments or signal quality.",
    }


def audit_inventory(clinical_root: Path, waveform_root: Path, header_sample_size: int = 0) -> dict[str, Any]:
    """Return aggregate counts; no patient IDs, record paths, or clinical rows.

    Missing files and invalid schemas are reported rather than treated as empty
    cohorts. Waveform overlap means subject overlap only, never temporal linkage.
    """
    if isinstance(header_sample_size, bool) or not isinstance(header_sample_size, int) or header_sample_size < 0:
        raise ValueError("header_sample_size must be a non-negative integer")
    clinical_root, waveform_root = Path(clinical_root), Path(waveform_root)
    warnings: list[str] = []
    indices = {}
    waveform_subjects = None
    header_entries: list[str] = []
    for name in INDEX_NAMES:
        info, subjects, sample = _read_index(
            waveform_root / name, header_sample_size if name == "RECORDS-waveforms" else 0
        )
        indices[name] = info
        if info["status"] != "ok":
            warnings.append(f"{name}: {info['status']}")
        elif info["unparsed_entries"]:
            warnings.append(f"{name}: some entries have no valid patient directory")
        if name == "RECORDS-waveforms":
            waveform_subjects, header_entries = subjects, sample
    tables: dict[str, Any] = {}
    cohorts = None
    for name, required in CLINICAL_SCHEMAS.items():
        info = _read_table_header(clinical_root / name, required)
        info["read_scope"] = "full table scan" if name in SCANNED_TABLES else "header only"
        if info["status"] == "ok" and name in SCANNED_TABLES:
            try:
                summary, cohort_result = _scan_table(clinical_root / name, waveform_subjects)
                info["summary"] = summary
                if cohort_result is not None:
                    cohorts = cohort_result
                if summary["invalid_identifier_rows"]:
                    warnings.append(f"{name}: invalid identifier rows excluded from unique counts")
            except (OSError, UnicodeError, csv.Error):
                info["status"] = "scan_failed"
        tables[name] = info
        if info["status"] != "ok":
            warnings.append(f"{name}: {info['status']}")
    header_sample = _sample_headers(waveform_root, header_entries) if header_sample_size else None
    if header_sample and any(
        value for key, value in header_sample["counts"].items()
        if key.startswith(("missing_", "invalid_", "unsafe_", "unreadable_"))
    ):
        warnings.append("waveform header sample: missing, invalid, unreadable, or unsafe header entries")
    return {
        "schema_version": SCHEMA_VERSION,
        "clinical_tables": tables,
        "waveform_indices": indices,
        "diagnosis_cohorts": cohorts,
        "waveform_header_sample": header_sample,
        "warnings": warnings,
        "limitations": [
            "Waveform subject overlap does not establish the same admission, ICU stay, time window, ECG channel, or signal quality.",
            "Diagnosis codes describe a coded admission; they do not establish real-time cohort eligibility or event onset.",
            "Disease cohorts can overlap and their counts must not be added together.",
            "Header sampling is not waveform decoding, a complete file-integrity check, or a representative quality assessment.",
        ],
    }
