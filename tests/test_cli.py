import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hrv_early_warning.cli import load_paths, main, write_report


class CliTests(unittest.TestCase):
    def test_compare_defaults_and_explicit_report_use_completed_local_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            Path("configs").mkdir()
            Path("configs/paths.local.toml").write_text('[data]\nclinical_root="../clinical"\nwaveform_root="../waveform"\n')
            for extra, replicates, output in (([], 200, Path("private/paired_comparison.json")),
                                              (["--replicates", "17", "--output", "reports/pair.json"], 17, Path("reports/pair.json"))):
                with self.subTest(extra=extra), patch("hrv_early_warning.model_comparison.compare_saved_models") as compare:
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = main(["compare", "--work-dir", "private", *extra])
                    self.assertEqual(code, 0)
                    compare.assert_called_once_with(Path("private/model_table.sqlite"), Path("private/models"), output,
                                                    replicates=replicates)

    def test_compare_rejects_source_writes_and_symlinks_before_loading_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clinical, waveform, work = root / "clinical", root / "waveform", root / "work"
            clinical.mkdir()
            waveform.mkdir()
            work.mkdir()
            config = root / "paths.toml"
            config.write_text('[data]\nclinical_root="clinical"\nwaveform_root="waveform"\n')
            target = clinical / "existing.json"
            target.write_text("source must survive")
            (work / "paired_comparison.json").symlink_to(target)
            choices = [([], "Report output"),
                       (["--output", str(waveform / "new.json")], "Report output"),
                       (["--work-dir", str(clinical / "private"), "--output", str(root / "safe.json")], "Private work directory")]
            for extra, error_text in choices:
                with self.subTest(extra=extra), patch("hrv_early_warning.model_comparison.compare_saved_models") as compare:
                    error = io.StringIO()
                    with contextlib.redirect_stderr(error):
                        code = main(["compare", "--paths", str(config), "--work-dir", str(work), *extra])
                    self.assertEqual(code, 2)
                    self.assertIn(error_text, error.getvalue())
                    compare.assert_not_called()
            self.assertEqual(target.read_text(), "source must survive")
            self.assertFalse((waveform / "new.json").exists())
            self.assertFalse((root / "safe.json").exists())

    def test_compare_rejects_nonpositive_replicates_before_reading_config(self):
        for replicates in ("0", "-1"):
            with self.subTest(replicates=replicates), patch("hrv_early_warning.cli.load_paths") as paths:
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    code = main(["compare", "--replicates", replicates])
                self.assertEqual(code, 2)
                self.assertIn("--replicates must be positive", error.getvalue())
                paths.assert_not_called()

    def test_compare_incomplete_models_returns_failure_without_success_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "paths.toml"
            config.write_text('[data]\nclinical_root="clinical"\nwaveform_root="waveform"\n')
            error, output = io.StringIO(), io.StringIO()
            with patch("hrv_early_warning.model_comparison.compare_saved_models",
                       side_effect=ValueError("all_completed_paired_model_artifacts_required")):
                with contextlib.redirect_stderr(error), contextlib.redirect_stdout(output):
                    code = main(["compare", "--paths", str(config), "--work-dir", str(root / "work")])
            self.assertEqual(code, 2)
            self.assertIn("all_completed_paired_model_artifacts_required", error.getvalue())
            self.assertEqual(output.getvalue(), "")

    def test_signal_vitals_and_develop_refuse_private_outputs_in_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clinical, waveform = root / "clinical", root / "waveform"
            clinical.mkdir()
            waveform.mkdir()
            config = root / "paths.toml"
            config.write_text('[data]\nclinical_root="clinical"\nwaveform_root="waveform"\n')
            for command in ("signals", "vitals", "develop"):
                for source in (clinical, waveform):
                    error = io.StringIO()
                    with self.subTest(command=command, source=source), contextlib.redirect_stderr(error):
                        code = main([command, "--paths", str(config), "--work-dir", str(source / "cache")])
                    self.assertEqual(code, 2)
                    self.assertIn("Private work directory", error.getvalue())
                    self.assertFalse((source / "cache").exists())

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

    def test_feasibility_rejects_source_output_before_starting_any_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clinical, waveform = root / "clinical", root / "waveform"
            clinical.mkdir()
            waveform.mkdir()
            alias = root / "source-alias"
            alias.symlink_to(clinical, target_is_directory=True)
            config = root / "paths.toml"
            config.write_text('[data]\nclinical_root="clinical"\nwaveform_root="waveform"\n')
            for destination in (clinical / "report.json", waveform / "report.json", alias / "report.json"):
                with self.subTest(destination=destination):
                    error = io.StringIO()
                    with patch("hrv_early_warning.feasibility.run_feasibility") as scan:
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
                            code = main(["feasibility", "--paths", str(config),
                                         "--work-dir", str(root / "private-work"),
                                         "--output", str(destination)])
                        scan.assert_not_called()
                    self.assertEqual(code, 2)
                    self.assertIn("outside both source dataset directories", error.getvalue())
                    self.assertFalse(destination.exists())
            self.assertFalse((root / "private-work").exists())

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
