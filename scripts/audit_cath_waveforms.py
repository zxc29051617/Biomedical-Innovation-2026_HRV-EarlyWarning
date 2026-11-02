#!/usr/bin/env python3
"""Decode fixed public ACS-ECG training records and publish aggregates only."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sys
import time
import urllib.request
import zipfile

from audit_cath_ecg_metadata import (
    AuditError, archive_members, json_bytes, output_directory,
    verify_archive, write_private,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "csv": {"filename": "CSV.zip", "file_id": 62951134, "bytes": 296769,
            "md5": "1cb46279c0e68e6512bd39214fc56528"},
    "raw": {"filename": "ECG_row_data.zip", "file_id": 62951395, "bytes": 1277577332,
            "md5": "acea6ca86a2d0b937ecfe7d1df6d30a2"},
    "median": {"filename": "ECG_median_data.zip", "file_id": 62951137, "bytes": 140340429,
               "md5": "4da1650cdbfb9817aa66df58c66564e1"},
}


def acquire(source, destination, local_source=None, *, opener=urllib.request.urlopen):
    """Bound streaming bytes and verify the immutable official file fingerprint."""
    if destination.exists() or destination.is_symlink():
        raise AuditError("archive_destination_exists_no_overwrite")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    url = f"https://ndownloader.figshare.com/files/{source['file_id']}"
    request = urllib.request.Request(url, headers={"User-Agent": "CathECG-waveform-audit/1"})
    stream = Path(local_source).open("rb") if local_source else opener(request, timeout=60)
    created = False
    try:
        with stream:
            descriptor = os.open(destination, flags, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as out:
                while chunk := stream.read(min(1024 * 1024, source["bytes"] + 1 - size)):
                    size += len(chunk)
                    if size > source["bytes"]:
                        raise AuditError("waveform_archive_size_exceeded")
                    md5.update(chunk)
                    sha.update(chunk)
                    out.write(chunk)
        if size != source["bytes"] or md5.hexdigest() != source["md5"]:
            raise AuditError("waveform_archive_fingerprint_mismatch")
    except Exception:
        # The partial source is not a verified archive and must never be reused.
        if created and destination.exists():
            destination.unlink()
        raise
    return {"url": url, "filename": source["filename"], "bytes": size,
            "md5": md5.hexdigest(), "sha256": sha.hexdigest(),
            "acquisition": "local_verified" if local_source else "official_fixed_url"}


def training_rows(archive_bytes):
    """Select the named public train file; do not infer labels or read test waveforms."""
    verify_archive(archive_bytes)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        members = archive_members(archive)
        chosen = [m for m in members if m.filename == "CSV/train.csv"]
        if len(chosen) != 1:
            raise AuditError("exact_train_csv_missing")
        data = archive.read(chosen[0])
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), strict=True)
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise AuditError("train_duplicate_columns")
    rows = list(reader)
    required = {"Patient_id", "ecg_row_record", "ecg_med_record", "OMI", "STEMI"}
    if not rows or not required.issubset(rows[0]):
        raise AuditError("train_csv_required_fields_missing")
    for row in rows:
        if None in row or any(not row[key] or not row[key].strip() for key in required):
            raise AuditError("train_row_missing_required_value")
        if row["OMI"] not in {"0", "1"} or row["STEMI"] not in {"0", "1"}:
            raise AuditError("source_label_not_binary")
    return rows


def record_index(archive):
    """Resolve a CSV record by exact header path, then unique basename only."""
    exact, basename = {}, defaultdict(list)
    for member in archive.infolist():
        if not member.is_dir() and member.filename.endswith(".hea"):
            stem = member.filename[:-4]
            exact[stem] = stem
            basename[PurePosixPath(stem).name].append(stem)
    return exact, basename


def resolve_record(value, index):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or "\\" in value
            or ":" in value or any(ord(c) < 32 for c in value)):
        raise AuditError("unsafe_csv_record_reference")
    # Source columns may explicitly name either member of a WFDB record pair.
    # Strip only the two known member suffixes, never arbitrary extensions.
    value = value[:-4] if value.endswith((".hea", ".dat")) else value
    exact, basenames = index
    if value in exact:
        return exact[value], "exact_path"
    candidates = basenames.get(PurePosixPath(value).name, [])
    if len(candidates) == 1:
        return candidates[0], "unique_basename"
    raise AuditError("record_reference_missing" if not candidates else "record_reference_ambiguous")


class Aggregate:
    def __init__(self, representation):
        self.representation = representation
        self.attempted = 0
        self.decoded = 0
        self.errors = Counter()
        self.matches = Counter()
        self.sample_counts = Counter()
        self.units = Counter()
        self.formats = Counter()
        self.patients = set()
        self.omi_patients = set()
        self.omi_rows = 0
        self.source_stemi_zero_omi_rows = 0
        self.flat_records = 0
        self.raw_wrong_length = 0
        self.header_time_fields = Counter()
        self.reference_diagnostics = {}
        self.hash_rows = Counter()
        self.hash_patients = defaultdict(set)
        self.hash_labels = defaultdict(set)
        self.patient_hash_labels = defaultdict(set)
        self.minimum = None
        self.maximum = None

    def success(self, row, waveform, metadata, match):
        self.decoded += 1
        self.matches[match] += 1
        self.patients.add(row["Patient_id"])
        if row["OMI"] == "1":
            self.omi_rows += 1
            self.omi_patients.add(row["Patient_id"])
            self.source_stemi_zero_omi_rows += row["STEMI"] == "0"
        self.sample_counts[str(waveform.shape[0])] += 1
        self.units["|".join(metadata["source_units"])] += 1
        self.formats["|".join(str(x) for x in metadata["format"])] += 1
        self.flat_records += bool((waveform.max(axis=0) == waveform.min(axis=0)).any())
        self.raw_wrong_length += self.representation == "raw" and waveform.shape[0] != 5000
        low, high = float(waveform.min()), float(waveform.max())
        self.minimum = low if self.minimum is None else min(self.minimum, low)
        self.maximum = high if self.maximum is None else max(self.maximum, high)
        for key in ("header_has_base_date", "header_has_base_time"):
            if key in metadata:
                self.header_time_fields[key + ("_true" if metadata[key] else "_false")] += 1
        digest = metadata["waveform_sha256"]
        self.hash_rows[digest] += 1
        self.hash_patients[digest].add(row["Patient_id"])
        self.hash_labels[digest].add(row["OMI"])
        self.patient_hash_labels[(row["Patient_id"], digest)].add(row["OMI"])

    def report(self):
        return {
            "representation": self.representation, "attempted_train_rows": self.attempted,
            "decoded_train_rows": self.decoded, "decoded_distinct_patients": len(self.patients),
            "decoded_source_omi_one_rows": self.omi_rows,
            "decoded_source_omi_one_distinct_patients": len(self.omi_patients),
            "decoded_source_omi_one_source_stemi_zero_rows": self.source_stemi_zero_omi_rows,
            "failure_codes": dict(sorted(self.errors.items())),
            "csv_to_header_resolution": dict(sorted(self.matches.items())),
            "sample_count_distribution": dict(sorted(self.sample_counts.items())),
            "source_unit_patterns": dict(sorted(self.units.items())),
            "wfdb_format_patterns": dict(sorted(self.formats.items())),
            "header_time_field_presence": dict(sorted(self.header_time_fields.items())),
            "reference_diagnostics": self.reference_diagnostics,
            "raw_records_not_5000_samples": self.raw_wrong_length,
            "records_with_any_exactly_constant_lead": self.flat_records,
            "global_minimum_mv": self.minimum, "global_maximum_mv": self.maximum,
            "unique_decoded_waveform_hashes": len(self.hash_rows),
            "repeated_waveform_hash_groups": sum(n > 1 for n in self.hash_rows.values()),
            "rows_beyond_first_per_waveform_hash": sum(n - 1 for n in self.hash_rows.values()),
            "waveform_hash_groups_with_multiple_patient_ids": sum(len(v) > 1 for v in self.hash_patients.values()),
            "waveform_hash_groups_with_source_omi_zero_and_one": sum(len(v) > 1 for v in self.hash_labels.values()),
            "same_patient_same_waveform_groups_with_source_omi_zero_and_one":
                sum(len(v) > 1 for v in self.patient_hash_labels.values()),
        }


def run(output_dir, local_archives=None, representations=("raw", "median")):
    from hrv_early_warning.cath_waveforms import ZipRecordReader, WaveformError
    import numpy
    import wfdb
    destination = output_directory(output_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    local_archives = local_archives or {}
    sources = {}
    for name in ("csv", *representations):
        source = SOURCES[name]
        print("Acquiring fixed public archive: " + name, flush=True)
        sources[name] = acquire(source, destination / source["filename"], local_archives.get(name))
        print("Verified fixed public archive: " + name, flush=True)
    write_private(destination / "download.json", json_bytes(sources))
    rows = training_rows((destination / SOURCES["csv"]["filename"]).read_bytes())
    summaries = []
    started = time.monotonic()
    for representation in representations:
        aggregate = Aggregate(representation)
        field = "ecg_row_record" if representation == "raw" else "ecg_med_record"
        with zipfile.ZipFile(destination / SOURCES[representation]["filename"]) as archive:
            reader = ZipRecordReader(archive)
            index = record_index(archive)
            aggregate.reference_diagnostics = {
                "archive_file_extensions": dict(sorted(Counter(
                    PurePosixPath(m.filename).suffix for m in archive.infolist() if not m.is_dir()).items())),
                "csv_reference_extensions": dict(sorted(Counter(PurePosixPath(r[field]).suffix for r in rows).items())),
                "indexed_header_count": len(index[0]),
                "csv_references_with_backslash": sum("\\" in r[field] for r in rows),
                "csv_references_with_outer_whitespace": sum(r[field] != r[field].strip() for r in rows),
            }
            for row in rows:
                aggregate.attempted += 1
                try:
                    record, match = resolve_record(row[field], index)
                    waveform, metadata = reader.read_record(record, representation=representation)
                    aggregate.success(row, waveform, metadata, match)
                except (AuditError, WaveformError) as error:
                    aggregate.errors[str(error)] += 1
                if aggregate.attempted % 1000 == 0:
                    print(f"Waveform progress: {representation} {aggregate.attempted}/{len(rows)}; decoded={aggregate.decoded}", flush=True)
        summaries.append(aggregate.report())
        # Persist each completed representation without copying row-level data.
        write_private(destination / f"{representation}_summary.json", json_bytes(summaries[-1]))
    summary = {
        "schema_version": "cath-waveform-audit-v1",
        "status": "structural_audit_complete" if all(s["decoded_train_rows"] > 0 for s in summaries)
        else "audit_complete_no_decodable_records_in_one_or_more_representations",
        "dataset_doi": "10.6084/m9.figshare.29925314.v1", "license": "CC0",
        "scope": "Named public train.csv only; archive may contain official test records, whose waveforms are not decoded.",
        "train_rows": len(rows), "sources": sources, "representations": summaries,
        "environment": {"numpy": numpy.__version__, "wfdb": wfdb.__version__, "python": sys.version.split()[0]},
        "source_fingerprints": {
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "module_sha256": hashlib.sha256((ROOT / "src/hrv_early_warning/cath_waveforms.py").read_bytes()).hexdigest()},
        "elapsed_decode_seconds": round(time.monotonic() - started, 3),
        "clinical_quality_validated": False, "st_amplitude_measured": False,
        "models_fitted": False, "first_arrival_ecg_identified": False,
        "limitations": [
            "Decoding and finite voltage are engineering properties, not clinical signal quality or OMI diagnosis.",
            "Source STEMI=0 is not a same-ECG ST-amplitude-negative subgroup.",
            "A matching waveform hash tests exact decoded-signal identity only, not near-duplicates or episode identity.",
            "Different source labels for a patient remain unresolved; no any-positive relabeling is performed.",
            "All ECG-level counts retain repeated patients; this audit does not choose an index ECG or a model split.",
        ],
    }
    write_private(destination / "summary.json", json_bytes(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--csv-archive", type=Path)
    parser.add_argument("--raw-archive", type=Path)
    parser.add_argument("--median-archive", type=Path)
    parser.add_argument("--representation", choices=("raw", "median", "both"), default="both")
    args = parser.parse_args()
    local = {name: getattr(args, name + "_archive") for name in SOURCES}
    representations = ("raw", "median") if args.representation == "both" else (args.representation,)
    try:
        summary = run(args.output_dir, local, representations)
    except Exception as error:
        # Never expose source header text, record names or patient values in CI logs.
        code = str(error) if isinstance(error, AuditError) else type(error).__name__
        print("Cath waveform audit failed: " + code, file=sys.stderr)
        return 1
    print("CATH_WAVEFORM_SUMMARY_JSON=" + json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
