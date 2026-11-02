#!/usr/bin/env python3
"""Audit fixed public CathECG metadata; never read ECG waveforms or fit models."""
from collections import Counter
import argparse
import csv
import hashlib
import io
import itertools
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "artifacts/cath_feasibility"
SOURCE_URL = "https://ndownloader.figshare.com/files/62951134"
EXPECTED_BYTES = 296769
EXPECTED_MD5 = "1cb46279c0e68e6512bd39214fc56528"
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_CSV_MEMBERS = 256
MISSING_TOKENS = frozenset(("", "na", "n/a", "nan", "null", "none"))
LABELS = ("AMI", "OMI", "PCI", "ACS", "STEMI", "NSTEMI", "CTO", "Prior_PCI")
FIELD_ALIASES = {
    "patient_id": ("patient_id",),
    "time_interval": ("time_interval",),
    # ecg_row_record is the spelling used in the source paper's Table 2.
    "raw_ecg_filename": ("ecg_row_record", "ecg_raw_record", "raw_ecg_filename", "raw_filename"),
    "median_ecg_filename": ("ecg_med_record", "median_ecg_filename", "median_filename"),
}
TIME_BINS = ("negative", "zero_to_60_minutes", "over_60_to_360_minutes",
             "over_360_to_1440_minutes", "over_1440_to_10080_minutes", "over_10080_minutes")


class AuditError(ValueError):
    """Only fixed codes, never patient identifiers or cell contents."""


def normalize_header(value):
    return re.sub(r"\s+", "_", value.strip().casefold())


def is_missing(value):
    return value is None or value.strip().casefold() in MISSING_TOKENS


def finite_number(value):
    if is_missing(value):
        return None
    try:
        number = float(value.strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def classify_label(value):
    if is_missing(value):
        return "missing"
    number = finite_number(value)
    return "zero" if number == 0 else "one" if number == 1 else "other"


def time_bin(value):
    if value < 0:
        return TIME_BINS[0]
    for boundary, name in zip((60, 360, 1440, 10080), TIME_BINS[1:5]):
        if value <= boundary:
            return name
    return TIME_BINS[5]


def verify_archive(data, expected_size=EXPECTED_BYTES, expected_md5=EXPECTED_MD5):
    if len(data) != expected_size:
        raise AuditError("archive_size_mismatch")
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    if md5 != expected_md5:
        raise AuditError("archive_md5_mismatch")
    return {"bytes": len(data), "md5": md5, "sha256": hashlib.sha256(data).hexdigest()}


def archive_members(archive):
    members, names, total = [], set(), 0
    for member in archive.infolist():
        name = member.filename
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                or any(ord(char) < 32 for char in name) or re.match(r"^[A-Za-z]:", name)):
            raise AuditError("unsafe_zip_member_name")
        if name.casefold() in names:
            raise AuditError("duplicate_zip_member_name")
        names.add(name.casefold())
        if member.flag_bits & 1:
            raise AuditError("encrypted_zip_member")
        if stat.S_ISLNK(member.external_attr >> 16):
            raise AuditError("symlink_zip_member")
        total += member.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise AuditError("zip_uncompressed_limit_exceeded")
        if member.is_dir():
            continue
        if path.suffix.casefold() != ".csv":
            raise AuditError("non_csv_zip_member")
        if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise AuditError("unsupported_zip_compression")
        members.append(member)
    if not members:
        raise AuditError("zip_contains_no_csv")
    if len(members) > MAX_CSV_MEMBERS:
        raise AuditError("too_many_csv_members")
    return sorted(members, key=lambda member: member.filename)


class BoundedReader(io.RawIOBase):
    """Bound actual expanded bytes as well as trusting ZIP header sizes."""
    def __init__(self, raw, budget):
        self.raw, self.budget = raw, budget

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.raw.read(len(buffer))
        self.budget[0] += len(data)
        if self.budget[0] > MAX_UNCOMPRESSED_BYTES:
            raise AuditError("zip_actual_expansion_limit_exceeded")
        buffer[:len(data)] = data
        return len(data)


def inspect_csv(stream, member_name):
    """Return aggregates, schema, and in-memory IDs for overlap counts only."""
    reader = csv.reader(stream, strict=True)
    try:
        columns = next(reader)
    except StopIteration as error:
        raise AuditError("csv_header_missing") from error
    normalized = [normalize_header(column) for column in columns]
    nonempty = [column for column in normalized if column]
    if len(nonempty) != len(set(nonempty)):
        raise AuditError("duplicate_normalized_csv_columns")
    matched = {field: [index for index, column in enumerate(normalized) if column in aliases]
               for field, aliases in FIELD_ALIASES.items()}
    label_columns = {label: normalized.index(normalize_header(label))
                     for label in LABELS if normalize_header(label) in normalized}
    for field in ("patient_id", "time_interval"):
        if len(matched[field]) > 1:
            raise AuditError("ambiguous_metadata_field")
    id_column = matched["patient_id"][0] if matched["patient_id"] else None
    time_column = matched["time_interval"][0] if matched["time_interval"] else None
    rows, blank_lines, id_missing, id_nonempty = 0, 0, 0, 0
    patients = set()
    times = Counter({"missing": 0, "invalid": 0, "valid": 0, "noninteger_valid": 0})
    bins = Counter({name: 0 for name in TIME_BINS})
    minimum = maximum = None
    labels = {label: Counter({"zero": 0, "one": 0, "missing": 0, "other": 0}) for label in label_columns}
    filename_counts = {index: Counter({"nonempty": 0, "missing": 0})
                       for field in ("raw_ecg_filename", "median_ecg_filename") for index in matched[field]}
    for row in reader:
        if not row:
            blank_lines += 1
            continue
        if len(row) != len(columns):
            raise AuditError("csv_row_width_mismatch")
        rows += 1
        if id_column is not None:
            if is_missing(row[id_column]):
                id_missing += 1
            else:
                id_nonempty += 1
                patients.add(row[id_column].strip())
        if time_column is not None:
            value = row[time_column]
            if is_missing(value):
                times["missing"] += 1
            else:
                minutes = finite_number(value)
                if minutes is None:
                    times["invalid"] += 1
                else:
                    times["valid"] += 1
                    times["noninteger_valid"] += not minutes.is_integer()
                    bins[time_bin(minutes)] += 1
                    minimum = minutes if minimum is None else min(minimum, minutes)
                    maximum = minutes if maximum is None else max(maximum, minutes)
        for label, index in label_columns.items():
            labels[label][classify_label(row[index])] += 1
        for index, counts in filename_counts.items():
            counts["missing" if is_missing(row[index]) else "nonempty"] += 1
    summary = {
        "csv_member": member_name, "row_count": rows, "blank_lines_skipped": blank_lines,
        "columns": columns,
        "patient_id": {"column_present": id_column is not None,
                       "nonempty_rows": id_nonempty if id_column is not None else None,
                       "missing_rows": id_missing if id_column is not None else None,
                       "distinct_count": len(patients) if id_column is not None else None,
                       "duplicate_rows_beyond_first_per_id": id_nonempty - len(patients) if id_column is not None else None},
        "time_interval_minutes": {"column_present": time_column is not None,
                                  "counts": dict(times) if time_column is not None else None,
                                  "minimum": minimum, "maximum": maximum,
                                  "ranges": dict(bins) if time_column is not None else None},
        "labels": {label: {"column_present": label in label_columns,
                           "counts": dict(labels[label]) if label in label_columns else None}
                   for label in LABELS},
        "ecg_filename_fields": {field: [{"column": columns[index], **filename_counts[index]}
                                         for index in matched[field]]
                                for field in ("raw_ecg_filename", "median_ecg_filename")},
    }
    schema = {"csv_member": member_name, "columns": columns,
              "normalization": [{"index": index, "original": column, "normalized": normalized[index]}
                                for index, column in enumerate(columns)],
              "matched_fields": {field: [columns[index] for index in indices] for field, indices in matched.items()},
              "matched_labels": {label: columns[index] for label, index in label_columns.items()},
              "missing_expected_fields": [field for field, indices in matched.items() if not indices],
              "missing_expected_labels": [label for label in LABELS if label not in label_columns]}
    return summary, schema, patients if id_column is not None else None


def audit_archive(data):
    summaries, schemas, patient_sets, budget = [], [], {}, [0]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive_members(archive)
        for member in members:
            with archive.open(member) as raw:
                with io.TextIOWrapper(io.BufferedReader(BoundedReader(raw, budget)), encoding="utf-8-sig", newline="") as text:
                    summary, schema, patients = inspect_csv(text, member.filename)
            summaries.append(summary)
            schemas.append(schema)
            patient_sets[member.filename] = patients
    overlaps = []
    for left, right in itertools.combinations(sorted(patient_sets), 2):
        known = patient_sets[left] is not None and patient_sets[right] is not None
        overlaps.append({"left_csv": left, "right_csv": right,
                         "shared_patient_id_count": len(patient_sets[left] & patient_sets[right]) if known else None,
                         "both_patient_id_columns_present": known})
    assumptions = {"header_normalization": "strip outer whitespace, casefold, replace whitespace runs with underscore; no punctuation guessing",
                   "patient_id_normalization": "strip outer whitespace; preserve case and leading zeros; never emit values",
                   "missing_tokens_case_insensitive": sorted(MISSING_TOKENS),
                   "time_interval_unit": "minutes, as specified by source paper Table 2",
                   "time_parser": "finite numeric minutes only; nonnumeric values count as invalid; no duration-format guessing",
                   "label_parser": "numeric 0/1, explicit missing tokens, otherwise other; no clinical meaning inferred",
                   "filename_field_aliases": FIELD_ALIASES,
                   "no_target_or_cohort_selected": True, "no_ecg_waveforms_read": True,
                   "no_models_fitted_or_evaluated": True}
    return {"schema_version": "cath-ecg-metadata-audit-v1", "status": "metadata_audit_complete",
            "csv_count": len(summaries), "expanded_bytes_read": budget[0],
            "csv_summaries": summaries, "cross_csv_patient_id_overlap": overlaps,
            "interpretation_limits": ["Procedure/treatment flags do not establish treatment necessity or angiographic stenosis ground truth.",
                                      "Counts do not establish label definitions, pre-procedure timing, independent patients, or model performance.",
                                      "No file is automatically chosen as a training target, cohort, or evaluation set."],
            "audit_rules": assumptions}, {"schema_version": "cath-ecg-metadata-schema-v1", "csv_schemas": schemas, "audit_rules": assumptions}


def output_directory(value):
    path = Path(value).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise AuditError("output_symlink_forbidden")
    if not path.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
        raise AuditError("output_must_be_under_ignored_cath_feasibility")
    if path.exists():
        raise AuditError("output_exists_no_overwrite")
    return path


def write_private(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(data)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def run_audit(output_dir, archive_path=None):
    destination = output_directory(output_dir)
    if archive_path is None:
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "CathECG-metadata-audit/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(EXPECTED_BYTES + 1)
        acquisition = "fixed_official_url"
    else:
        with Path(archive_path).open("rb") as stream:
            data = stream.read(EXPECTED_BYTES + 1)
        acquisition = "provided_local_archive_verified_against_official_checksum"
    hashes = verify_archive(data, EXPECTED_BYTES, EXPECTED_MD5)
    provenance = {"source_url": SOURCE_URL, "figshare_article_id": 29925314, "figshare_version": 1,
                  "figshare_file_id": 62951134, "license": "CC0", "acquisition": acquisition,
                  "expected_bytes": EXPECTED_BYTES, "expected_md5": EXPECTED_MD5, "verified": hashes,
                  "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700, exist_ok=False)
    write_private(destination / "CSV.zip", data)
    write_private(destination / "download.json", json_bytes(provenance))
    try:
        summary, schema = audit_archive(data)
    except (AuditError, csv.Error, UnicodeError, zipfile.BadZipFile, OSError) as error:
        code = str(error) if isinstance(error, AuditError) else type(error).__name__
        write_private(destination / "failure.json", json_bytes({"status": "metadata_audit_failed", "error": code}))
        raise
    summary["source"] = provenance
    write_private(destination / "schema.json", json_bytes(schema))
    write_private(destination / "summary.json", json_bytes(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="Optional local official CSV.zip; same size and checksum required")
    args = parser.parse_args()
    try:
        summary = run_audit(args.output_dir, args.archive)
    except (AuditError, OSError, csv.Error, UnicodeError, zipfile.BadZipFile) as error:
        code = str(error) if isinstance(error, AuditError) else type(error).__name__
        print("Cath metadata audit failed: " + code, file=sys.stderr)
        return 1
    print("CATH_METADATA_SUMMARY_JSON=" + json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
