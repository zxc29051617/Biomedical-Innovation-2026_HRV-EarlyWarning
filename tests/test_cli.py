import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from hrv_early_warning.cli import load_paths, main, write_report


class CliTests(unittest.TestCase):
    def test_relative_data_paths_are_relative_to_config_not_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "paths.toml"
            config.write_text('[data]\nclinical_root="clinical"\nwaveform_root="waveform"\n')
            self.assertEqual(load_paths(config), (root / "clinical", root / "waveform"))

    def test_report_cannot_write_inside_source_or_symlink_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clinical, waveform = root / "clinical", root / "waveform"
            clinical.mkdir()
            waveform.mkdir()
            alias = root / "alias"
            alias.symlink_to(clinical, target_is_directory=True)
            for destination in (clinical / "report.json", waveform / "report.json", alias / "report.json"):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    write_report({}, destination, (clinical, waveform))
            self.assertFalse((clinical / "report.json").exists())

    def test_missing_dataset_is_a_partial_report_and_failure_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "paths.toml"
            config.write_text('[data]\nclinical_root="missing_clinical"\nwaveform_root="missing_waveform"\n')
            destination = root / "report.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["audit", "--paths", str(config), "--output", str(destination)])
            self.assertEqual(code, 2)
            report = json.loads(destination.read_text())
            self.assertEqual(report["clinical_tables"]["PATIENTS.csv"]["status"], "missing")
            self.assertIsNone(report["diagnosis_cohorts"])

    def test_invalid_config_reports_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "paths.toml"
            config.write_text('[data]\nclinical_root=""\n')
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                code = main(["audit", "--paths", str(config)])
            self.assertEqual(code, 2)
            self.assertIn("clinical_root", error.getvalue())


if __name__ == "__main__":
    unittest.main()
