import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hrv_early_warning.progress import pipeline_status


class ProgressTests(unittest.TestCase):
    def test_job_states_do_not_promote_incomplete_artifacts_to_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for filename, state in (("signal_job_status.json", "retrying"),
                                    ("comparison_status.json", "waiting_for_complete_development_models"),
                                    ("report_status.json", "waiting_for_complete_paired_comparison")):
                (path / filename).write_text(json.dumps({"state": state, "updated_utc": "2026-11-01T05:00:00+00:00"}))
            before = {p.name: p.read_bytes() for p in path.iterdir()}
            result = pipeline_status(path)
            self.assertEqual(result["signal_job"]["state"], "retrying")
            self.assertEqual(result["automatic_comparison"]["state"], "waiting_for_complete_development_models")
            self.assertEqual(result["automatic_report"]["state"], "waiting_for_complete_paired_comparison")
            self.assertEqual(result["signals"]["status"], "not_started")
            self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})

    def test_missing_work_directory_is_not_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing"
            self.assertEqual(pipeline_status(path)["signals"]["status"], "not_started")
            self.assertFalse(path.exists())

    def test_partial_or_stale_summary_is_not_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "signal_plan.json").write_text(json.dumps({"plan": {"unique_epochs": 2}}))
            connection = sqlite3.connect(path / "epochs.sqlite")
            connection.execute("CREATE TABLE epochs (id INTEGER)")
            connection.execute("INSERT INTO epochs VALUES (1)")
            connection.commit()
            (path / "signal_summary.json").write_text(json.dumps({"complete": True, "screened_epochs": 2, "accepted_epochs": 1, "events": {}}))
            self.assertEqual(pipeline_status(path)["signals"]["status"], "incomplete")
            connection.execute("INSERT INTO epochs VALUES (2)")
            connection.commit()
            connection.close()
            self.assertEqual(pipeline_status(path)["signals"]["status"], "complete")
