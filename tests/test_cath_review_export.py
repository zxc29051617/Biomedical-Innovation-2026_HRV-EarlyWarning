"""Export isolation and official-WFDB synthetic integration; no real ECGs."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from hrv_early_warning.cath_waveforms import LEAD_NAMES

try:
    import numpy as np
    import wfdb
except ImportError:
    np = wfdb = None

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cath_review_export_test_target", ROOT / "scripts/export_cath_review_case.py")
exporter = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    SPEC.loader.exec_module(exporter)


def source_for(data):
    return {"filename": "synthetic.zip", "file_id": 62951395, "bytes": len(data),
            "md5": hashlib.md5(data, usedforsecurity=False).hexdigest()}


class RecordingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class VerificationAndOutputTests(unittest.TestCase):
    def test_official_med_container_is_reported_unsupported_without_inventing_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "median.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("median/synthetic.med", b"undocumented synthetic bytes")
            destination = root / "out"
            with patch.object(exporter.metadata_audit, "OUTPUT_ROOT", root), \
                    patch.dict(exporter.SOURCES, {"median": source_for(archive.read_bytes())}):
                with self.assertRaisesRegex(exporter.metadata_audit.AuditError,
                                            "^official_median_med_format_not_supported$"):
                    exporter.export_case(archive, "synthetic.med", "median", destination)
            self.assertFalse(destination.exists())

    def test_full_archive_verification_streams_and_checks_size_and_digest(self):
        payload = b"x" * (2 * 1024 * 1024 + 7)
        stream = RecordingStream(payload)
        result = exporter.verify_stream(stream, source_for(payload))
        self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["bytes"], len(payload))
        self.assertTrue(all(0 < size <= 1024 * 1024 for size in stream.read_sizes))
        for bad in (payload[:-1], payload + b"extra", b"y" * len(payload)):
            with self.subTest(size=len(bad)), self.assertRaises(exporter.metadata_audit.AuditError):
                exporter.verify_stream(RecordingStream(bad), source_for(payload))

    def test_output_existing_outside_and_symlink_are_rejected_before_input_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "allowed"
            root.mkdir()
            existing = root / "exists"
            existing.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            link = root / "link"
            link.symlink_to(outside, target_is_directory=True)
            with patch.object(exporter.metadata_audit, "OUTPUT_ROOT", root):
                for target in (existing, outside / "new", link / "new"):
                    with self.subTest(target=target.name), patch.object(exporter, "_open_archive") as opener:
                        with self.assertRaises(exporter.metadata_audit.AuditError):
                            exporter.export_case("source.zip", "private_record", "raw", target)
                        opener.assert_not_called()

    def test_bad_archive_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "wrong.zip"
            archive.write_bytes(b"wrong")
            destination = root / "out"
            with patch.object(exporter.metadata_audit, "OUTPUT_ROOT", root), \
                    patch.dict(exporter.SOURCES, {"raw": source_for(b"right")}):
                with self.assertRaisesRegex(exporter.metadata_audit.AuditError, "fingerprint_mismatch"):
                    exporter.export_case(archive, "private_record", "raw", destination)
                self.assertFalse(destination.exists())

    def test_source_symlink_and_nonregular_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.zip"
            source.write_bytes(b"test")
            link = root / "link.zip"
            link.symlink_to(source)
            with self.assertRaisesRegex(exporter.metadata_audit.AuditError, "symlink_forbidden"):
                exporter._open_archive(link)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(exporter.metadata_audit.AuditError, "must_be_regular"):
                exporter._open_archive(fifo)

    def test_private_directory_and_files_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "allowed"
            destination = root / "parent" / "out"
            with patch.object(exporter.metadata_audit, "OUTPUT_ROOT", root):
                descriptor = exporter._new_output_directory(destination)
                try:
                    exporter._write_private_at(descriptor, "case.json", {"synthetic": True})
                    with self.assertRaises(FileExistsError):
                        exporter._write_private_at(descriptor, "case.json", {"overwrite": True})
                finally:
                    os.close(descriptor)
                with self.assertRaisesRegex(exporter.metadata_audit.AuditError, "exists_no_overwrite"):
                    exporter._new_output_directory(destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((destination / "case.json").stat().st_mode), 0o600)
            self.assertEqual(json.loads((destination / "case.json").read_text()), {"synthetic": True})

    def test_cli_errors_do_not_echo_source_identifiers(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["--archive", "private_source.zip", "--record", "private_record",
                "--representation", "raw", "--output-dir", "unused"]
        with patch.object(exporter, "export_case", side_effect=OSError("private_secret")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = exporter.main(args)
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("private_", stderr.getvalue())
        self.assertIn("OSError", stderr.getvalue())


@unittest.skipUnless(np is not None and wfdb is not None, "optional numpy and wfdb required")
class OfficialWFDBExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        wfdb.wrsamp("private_record", fs=500, units=["mV"] * 12,
                    sig_name=list(LEAD_NAMES),
                    p_signal=np.tile(np.linspace(-1, 1, 500)[:, None], (1, 12)),
                    fmt=["16"] * 12, adc_gain=[1000] * 12, baseline=[0] * 12,
                    comments=["Patient_id=private_patient OMI=1 STEMI=0"], write_dir=str(fixtures))
        self.archive = self.root / "private_source.zip"
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in fixtures.iterdir():
                zf.write(file, "folder/" + file.name)
        self.sources = patch.dict(exporter.SOURCES, {"median": source_for(self.archive.read_bytes())})
        self.sources.start()
        self.addCleanup(self.sources.stop)
        self.output_root = patch.object(exporter.metadata_audit, "OUTPUT_ROOT", self.root / "out")
        self.output_root.start()
        self.addCleanup(self.output_root.stop)

    def test_end_to_end_blind_case_private_mapping_and_safe_stdout(self):
        destination = self.root / "out" / "review"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = exporter.main(["--archive", str(self.archive), "--record", "private_record",
                                  "--representation", "median", "--output-dir", str(destination)])
        self.assertEqual(code, 0, stderr.getvalue())
        status = json.loads(stdout.getvalue())
        self.assertRegex(status["case_id"], r"^cr-[0-9a-f]{32}$")
        case = json.loads((destination / "case.json").read_text())
        mapping = json.loads((destination / "source_mapping.local.json").read_text())
        self.assertEqual(case["case_id"], status["case_id"])
        self.assertEqual(case["age_years"], None)
        self.assertEqual(case["sex"], "unknown")
        self.assertEqual(len(case["signals_mv"]), 500)
        self.assertEqual(mapping["resolved_record"], "folder/private_record")
        self.assertEqual(mapping["resolution"], "unique_basename")
        self.assertIsNone(mapping["is_first_arrival_ecg"])
        self.assertFalse(mapping["source_labels_read"])
        self.assertTrue(mapping["private_do_not_share_with_reviewers"])
        for forbidden in ("private_record", "private_patient", "private_source", "OMI", "STEMI"):
            self.assertNotIn(forbidden, json.dumps(case))
            self.assertNotIn(forbidden, stdout.getvalue())
        self.assertEqual(set(file.name for file in destination.iterdir()), {"case.json", "source_mapping.local.json"})
        for file in destination.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_exact_path_operator_demographics_and_random_case_ids(self):
        ids = []
        for index in range(2):
            destination = self.root / "out" / f"review{index}"
            result = exporter.export_case(self.archive, "folder/private_record.hea", "median", destination,
                                          age_years=47, sex="male")
            ids.append(result["case_id"])
            case = json.loads((destination / "case.json").read_text())
            mapping = json.loads((destination / "source_mapping.local.json").read_text())
            self.assertEqual((case["age_years"], case["sex"]), (47, "male"))
            self.assertEqual(mapping["resolution"], "exact_path")
        self.assertNotEqual(ids[0], ids[1])

    def test_ambiguous_basename_does_not_create_output(self):
        with zipfile.ZipFile(self.archive, "a") as zf:
            zf.writestr("another/private_record.hea", zf.read("folder/private_record.hea"))
        with patch.dict(exporter.SOURCES, {"median": source_for(self.archive.read_bytes())}):
            destination = self.root / "out" / "ambiguous"
            with self.assertRaisesRegex(exporter.metadata_audit.AuditError, "ambiguous"):
                exporter.export_case(self.archive, "private_record", "median", destination)
            self.assertFalse(destination.exists())

    def test_viewer_voltage_guard_rejects_out_of_range_without_reclassifying_signal(self):
        fixtures = self.root / "fixtures"
        for index, voltage in enumerate((100.0, -100.0, 100.1, -100.1)):
            with self.subTest(voltage=voltage):
                wfdb.wrsamp("private_record", fs=500, units=["mV"] * 12,
                            sig_name=list(LEAD_NAMES), p_signal=np.full((500, 12), voltage),
                            fmt=["16"] * 12, adc_gain=[100] * 12, baseline=[0] * 12,
                            write_dir=str(fixtures))
                with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as zf:
                    for file in fixtures.iterdir():
                        zf.write(file, "folder/" + file.name)
                destination = self.root / "out" / f"voltage{index}"
                with patch.dict(exporter.SOURCES, {"median": source_for(self.archive.read_bytes())}):
                    if abs(voltage) > 100:
                        with self.assertRaisesRegex(exporter.metadata_audit.AuditError,
                                                    "^review_display_voltage_limit$"):
                            exporter.export_case(self.archive, "private_record", "median", destination)
                        self.assertFalse(destination.exists())
                    else:
                        result = exporter.export_case(self.archive, "private_record", "median", destination)
                        self.assertEqual(result["status"], "blinded_review_case_exported")
                        case = json.loads((destination / "case.json").read_text())
                        self.assertEqual(case["signals_mv"][0][0], voltage)


if __name__ == "__main__":
    unittest.main()
