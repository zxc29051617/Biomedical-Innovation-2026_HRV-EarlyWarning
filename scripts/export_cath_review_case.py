#!/usr/bin/env python3
"""Export one explicitly chosen ECG from a verified local official archive.

Give reviewers only case.json. source_mapping.local.json is a private mapping
for the data manager and must not accompany the blinded review file. No CSV,
diagnosis, model score, network service, or automatic case selection is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import audit_cath_ecg_metadata as metadata_audit
from audit_cath_waveforms import SOURCES, record_index, resolve_record
from hrv_early_warning.cath_waveforms import (
    WaveformError, ZipRecordReader, build_blinded_review_case,
)


def verify_stream(stream, source):
    """Verify the complete official archive without copying it to the output."""
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    while chunk := stream.read(min(1024 * 1024, source["bytes"] + 1 - size)):
        size += len(chunk)
        if size > source["bytes"]:
            raise metadata_audit.AuditError("review_archive_size_exceeded")
        sha.update(chunk)
        md5.update(chunk)
    if size != source["bytes"] or md5.hexdigest() != source["md5"]:
        raise metadata_audit.AuditError("review_archive_fingerprint_mismatch")
    return {"bytes": size, "md5": md5.hexdigest(), "sha256": sha.hexdigest()}


def _open_archive(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise metadata_audit.AuditError("review_archive_symlink_forbidden")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise metadata_audit.AuditError("review_archive_must_be_regular")
    return os.fdopen(descriptor, "rb")


def _new_output_directory(path):
    """Traverse directory fds without following links; create the leaf once."""
    path = metadata_audit.output_directory(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.mkdir(path.name, 0o700, dir_fd=descriptor)
        child = os.open(path.name, flags, dir_fd=descriptor)
        return child
    finally:
        os.close(descriptor)


def _write_private_at(directory_fd, filename, value):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(metadata_audit.json_bytes(value))


def export_case(archive, record, representation, output_dir, *, age_years=None, sex="unknown"):
    """Return safe status only; source linkage is saved in a private local file."""
    destination = metadata_audit.output_directory(output_dir)
    if representation not in ("raw", "median"):
        raise metadata_audit.AuditError("review_representation_invalid")
    source = SOURCES[representation]
    with _open_archive(archive) as stream:
        verified = verify_stream(stream, source)
        stream.seek(0)
        with zipfile.ZipFile(stream) as zf:
            reader = ZipRecordReader(zf)
            index = record_index(zf)
            if representation == "median" and not index[0] and any(
                    name.endswith(".med") for name in reader.members):
                raise metadata_audit.AuditError("official_median_med_format_not_supported")
            resolved, match = resolve_record(record, index)
            waveform, metadata = reader.read_record(resolved, representation=representation)
    # Match the viewer's finite plotting range. This is a display safeguard,
    # not a clinical signal-quality threshold or a disease classification.
    if float(waveform.max()) > 100 or float(waveform.min()) < -100:
        raise metadata_audit.AuditError("review_display_voltage_limit")
    case_id = "cr-" + secrets.token_hex(16)
    case = build_blinded_review_case(
        waveform, metadata["lead_names"], metadata["fs"], age_years, sex,
        representation, case_id, metadata["waveform_sha256"],
    )
    mapping = {
        "schema_version": "cath-review-private-source-mapping-v1",
        "private_do_not_share_with_reviewers": True,
        "case_id": case_id,
        "source_archive": {"local_path": str(Path(archive).absolute()),
                           "official_filename": source["filename"],
                           "figshare_file_id": source["file_id"],
                           "verified": verified},
        "requested_record": record, "resolved_record": resolved, "resolution": match,
        "representation": representation,
        "waveform_sha256": metadata["waveform_sha256"],
        "waveform_fingerprint_definition": metadata["fingerprint_definition"],
        "header_has_base_date": metadata["header_has_base_date"],
        "header_has_base_time": metadata["header_has_base_time"],
        "arrival_time": None, "ecg_absolute_time": None, "angiography_time": None,
        "is_first_arrival_ecg": None, "episode_linkage_status": "unknown",
        "demographics_origin": "operator_supplied_without_inferred_source_code_mapping",
        "source_labels_read": False, "clinical_quality_validated": False,
    }
    # Validate both serializations before creating any output. Keep the mapping
    # separate; the review file has only the builder's explicit whitelist.
    metadata_audit.json_bytes(mapping)
    metadata_audit.json_bytes(case)
    descriptor = _new_output_directory(destination)
    try:
        _write_private_at(descriptor, "source_mapping.local.json", mapping)
        _write_private_at(descriptor, "case.json", case)
    finally:
        os.close(descriptor)
    return {"status": "blinded_review_case_exported", "case_id": case_id,
            "review_file": "case.json", "private_mapping_file": "source_mapping.local.json"}


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Cath review export failed: invalid_arguments\n")


def main(argv=None):
    parser = _SafeParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="Local official raw or median ZIP; full checksum required")
    parser.add_argument("--record", required=True, help="Explicit CSV record reference; no automatic selection")
    parser.add_argument("--representation", choices=("raw", "median"), required=True)
    parser.add_argument("--age-years", type=float, default=None)
    parser.add_argument("--sex", choices=("male", "female", "unknown"), default="unknown")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory under artifacts/cath_feasibility")
    args = parser.parse_args(argv)
    try:
        result = export_case(args.archive, args.record, args.representation, args.output_dir,
                             age_years=args.age_years, sex=args.sex)
    except Exception as error:
        code = str(error) if isinstance(error, (metadata_audit.AuditError, WaveformError)) else type(error).__name__
        print("Cath review export failed: " + code, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
