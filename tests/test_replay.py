"""Replay tests use synthetic private caches; no clinical artifacts are needed."""

from contextlib import closing
from datetime import datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock

import joblib
import numpy as np

from hrv_early_warning import replay
from hrv_early_warning.modeling import MODEL_VERSION, RR_FEATURES, VARIANTS, _signal_readiness


class FrozenPipeline:
    classes_ = (0, 1)

    def __init__(self, manifest):
        self.manifest = str(manifest)

    def fit(self, *args, **kwargs):
        raise AssertionError("Replay must never fit")

    def predict_proba(self, matrix):
        if not Path(self.manifest).is_file():
            raise AssertionError("Private selection must be durable before scoring")
        return np.tile([0.8, 0.2], (len(matrix), 1))


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def make_fixture(root):
    work, feasibility, output = root / "m2", root / "feasibility", root / "replay"
    (work / "models").mkdir(parents=True)
    feasibility.mkdir()
    start = datetime(2100, 1, 1)
    table = sqlite3.connect(work / "model_table.sqlite")
    table.execute("CREATE TABLE model_rows (subject_id INTEGER,hadm_id INTEGER,icustay_id INTEGER,landmark TEXT,"
                  "history_start TEXT,horizon_end TEXT,event_time TEXT,followup_end TEXT,first_exposure_time TEXT,"
                  "label TEXT,split TEXT,monitoring_seconds REAL,current_rr_accepted INTEGER,features TEXT)")
    table.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)")
    epochs = sqlite3.connect(work / "epochs.sqlite")
    epochs.execute("CREATE TABLE epochs (icustay_id INTEGER,endtime TEXT,accepted INTEGER,result TEXT,PRIMARY KEY(icustay_id,endtime))")
    epochs.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)")
    records = []
    for subject in range(1, 10):
        event = start + timedelta(hours=18) if subject <= 3 else None
        end = start + timedelta(hours=24 if event else 18)
        stay = dict(subject_id=subject, hadm_id=100 + subject, icustay_id=200 + subject,
                    intime=start.isoformat(), outtime=end.isoformat(), followup_end=end.isoformat(), age_years=50, age_obscured=False)
        outcome = dict(event_time=event.isoformat() if event else None, first_exposure_time=event.isoformat() if event else None,
                       followup_end=end.isoformat(), event_status="qualifying_first_use" if event else "no_exposure",
                       primary_continuous_minutes=60 if event else 0)
        records.append(dict(stay=stay, outcome=outcome, uncertain_reasons=[], matched_waveform_records=1))
        for hour in range(6, 18):
            clock = start + timedelta(hours=hour)
            accepted = not (subject >= 7 and hour % 2)
            features = {key: 0.0 for key in set().union(*VARIANTS.values())}
            features.update(age_years=50, age_obscured=0, heart_rate=75, map=80, respiratory_rate=18, spo2=98)
            for lag in range(6):
                for name, value in zip(RR_FEATURES, (800, 35, 30)):
                    features[f"{name}_lag{lag}"] = value if lag or accepted else None
            features["rr_valid_count"] = 6 if accepted else 5
            label = "positive" if event and hour >= 12 else "negative" if event or hour <= 12 else "censored"
            table.execute("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (subject, 100 + subject, 200 + subject, clock.isoformat(), (clock - timedelta(hours=6)).isoformat(),
                           (clock + timedelta(hours=6)).isoformat(), outcome["event_time"], end.isoformat(), outcome["first_exposure_time"],
                           label, "validation", 3600, int(accepted), json.dumps(features)))
            item = dict(icu=200 + subject, end=clock.isoformat(), accepted=accepted, reasons=[] if accepted else ["low_beat_agreement"],
                        features=dict(zip(RR_FEATURES, (800, 35, 30))) if accepted else {})
            epochs.execute("INSERT INTO epochs VALUES (?,?,?,?)", (200 + subject, clock.isoformat(), int(accepted), json.dumps(item)))
    (feasibility / "stay_outcomes.jsonl").write_text("\n".join(json.dumps(record) for record in records) + "\n")
    (feasibility / "waveform_metadata.jsonl").write_text("{}\n")
    (work / "vitals.sqlite").write_bytes(b"synthetic frozen vital cache")
    write_json(work / "vitals_summary.json", {"bytes": (work / "vitals.sqlite").stat().st_size})
    split = dict(algorithm="sha256_rank_v1", seed=replay.SEED, cohort_sha256="synthetic-cohort", population_sha256="synthetic-population",
                 split_counts={"validation": 9}, assignments={str(i): "validation" for i in range(1, 10)})
    write_json(work / "patient_split.json", split)
    fingerprint = dict(config={}, versions={}, stay_outcomes_sha256=replay._hash_file(feasibility / "stay_outcomes.jsonl"),
                       waveform_metadata_sha256=replay._hash_file(feasibility / "waveform_metadata.jsonl"))
    epochs.execute("INSERT INTO metadata VALUES ('fingerprint',?)", (json.dumps(fingerprint),))
    epochs.commit()
    epochs.close()
    write_json(work / "signal_summary.json", dict(complete=True, screened_epochs=108, plan={"unique_epochs": 108}, config={}, versions={}))
    write_json(work / "signal_plan.json", {"unique_epochs": 108})
    ready, signal = _signal_readiness(feasibility, work)
    assert ready
    source = dict(model_version=MODEL_VERSION, rr_ready=True, signal=signal,
                  stay_outcomes_sha256=fingerprint["stay_outcomes_sha256"], patient_split_sha256=replay._hash_file(work / "patient_split.json"),
                  vitals_summary_sha256=replay._hash_file(work / "vitals_summary.json"), vitals_bytes=(work / "vitals.sqlite").stat().st_size,
                  vitals_mtime_ns=(work / "vitals.sqlite").stat().st_mtime_ns)
    table_report = dict(model_version=MODEL_VERSION, rr_ready=True, signal_status="complete_verified", development_rows=108,
                        scope="train_validation_only_including_censored_no_test_feature_queries", split={key: value for key, value in split.items() if key != "assignments"})
    table.executemany("INSERT INTO metadata VALUES (?,?)", [("source_fingerprint", json.dumps(source)), ("report", json.dumps(table_report))])
    table.commit()
    table.close()
    report = dict(stage="development_validation_only_test_unopened", model_version=MODEL_VERSION, source_fingerprint=source,
                  model_table=table_report, models={})
    hashes = {}
    for name, names in VARIANTS.items():
        artifact = dict(pipeline=FrozenPipeline(output / "selection.private.json"), features=names, threshold=0.4,
                        variant=name, model_version=MODEL_VERSION, source_fingerprint=source)
        path = work / "models" / f"{name}.joblib"
        joblib.dump(artifact, path)
        hashes[name] = replay._hash_file(path)
        report["models"][name] = dict(features=list(names), status="development_validation_complete", validation_alarms={"threshold": 0.4})
    write_json(work / "models" / "development_report.json", report)
    write_json(work / "paired_comparison.json", dict(status="paired_comparison_complete", stage="paired_development_validation_only_test_unopened",
                                                     provenance=dict(source_fingerprint_sha256=replay._hash_json(source),
                                                                     model_artifact_sha256={key: value for key, value in hashes.items() if key.startswith("paired_")})))
    return work, feasibility, output


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work, self.feasibility, self.output = make_fixture(self.root)
        self.patcher = patch.object(replay, "REPLAY_ROOT", self.output)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temporary.cleanup()

    def export(self, **kwargs):
        return replay.export_replay(self.work, self.feasibility, self.output, **kwargs)

    def mutate_feature(self, value):
        with closing(sqlite3.connect(self.work / "model_table.sqlite")) as connection, connection:
            for rowid, raw in connection.execute("SELECT rowid,features FROM model_rows").fetchall():
                values = json.loads(raw)
                values["map"] = value
                connection.execute("UPDATE model_rows SET features=? WHERE rowid=?", (json.dumps(values), rowid))

    def test_export_manifest_before_prediction_quality_gate_and_private_output(self):
        summary = self.export()
        self.assertEqual(summary["cases"], 3)
        self.assertEqual(summary["hourly_points"], 36)
        self.assertFalse(summary["test_features_read"])
        first = (self.output / "replay.json").read_bytes()
        document = json.loads(first)
        self.assertEqual(document["schema_version"], "hrv-replay-v1")
        for case in document["cases"]:
            hours = [point["hour"] for point in case["points"]]
            self.assertEqual(hours, sorted(set(hours)))
            self.assertGreater(case["followup_end_hour"], hours[-1])
            for point in case["points"]:
                self.assertEqual(point["scores"]["vitals_all"], 0.2)
                for name in VARIANTS:
                    if name.startswith("paired_"):
                        self.assertEqual(point["scores"][name], 0.2 if point["rr"]["accepted"] else None)
                if not point["rr"]["accepted"]:
                    self.assertIsNone(point["rr"]["mean_rr_ms"])
                    self.assertTrue(point["rr"]["reason_codes"])
                self.assertEqual(point["rr"]["rhythm_status"], "not_assessed")
        text = first.decode()
        for forbidden in ("subject_id", "hadm_id", "icustay_id", "2100-01", str(self.root), "source_fingerprint"):
            self.assertNotIn(forbidden, text)
        self.assertEqual((self.output / "selection.private.json").stat().st_mode & 0o777, 0o600)
        self.export()
        self.assertEqual(first, (self.output / "replay.json").read_bytes())

    def test_test_split_is_rejected_before_opening_any_input(self):
        with patch.object(replay, "_readonly", side_effect=AssertionError("Must not open")):
            with self.assertRaisesRegex(ValueError, "only_allows_train_or_validation"):
                self.export(split="test")

    def test_contaminated_table_rejected_before_features_or_models(self):
        with closing(sqlite3.connect(self.work / "model_table.sqlite")) as connection, connection:
            connection.execute("UPDATE model_rows SET split='test' WHERE subject_id=1")
        with patch.object(replay, "_load_models", side_effect=AssertionError("Must not load")):
            with self.assertRaisesRegex(ValueError, "containing_test"):
                self.export()
        self.assertFalse((self.output / "replay.json").exists())

    def test_selection_does_not_query_features_and_is_invariant_to_feature_scores(self):
        with closing(replay._readonly(self.work / "model_table.sqlite")) as connection:
            queries = []
            connection.set_trace_callback(queries.append)
            first = replay.select_cases(connection, "validation")
        self.assertTrue(all("features" not in query.lower() for query in queries))
        self.mutate_feature(0.999)
        with closing(replay._readonly(self.work / "model_table.sqlite")) as connection:
            second = replay.select_cases(connection, "validation")
        self.assertEqual(first, second)

    def test_nonfinite_feature_rejected(self):
        for value in (float("nan"), float("inf"), True, "80"):
            self.mutate_feature(value)
            if self.output.exists():
                (self.output / "selection.private.json").unlink(missing_ok=True)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite_numeric"):
                self.export()
            self.assertFalse((self.output / "replay.json").exists())

    def test_future_or_invalid_clocks_rejected(self):
        with closing(sqlite3.connect(self.work / "model_table.sqlite")) as connection, connection:
            connection.execute("UPDATE model_rows SET history_start=horizon_end")
        with self.assertRaisesRegex(ValueError, "relative_clocks"):
            self.export()

    def test_saved_model_metadata_mismatch_rejected(self):
        path = self.work / "models" / "vitals_all.joblib"
        artifact = joblib.load(path)
        artifact["features"] = tuple(reversed(artifact["features"]))
        joblib.dump(artifact, path)
        with self.assertRaisesRegex(ValueError, "saved_model_metadata"):
            self.export()

    def test_quality_gate_disagreement_cannot_be_scored(self):
        with closing(sqlite3.connect(self.work / "model_table.sqlite")) as connection, connection:
            connection.execute("UPDATE model_rows SET current_rr_accepted=1 WHERE landmark LIKE '%T07:%'")
        with self.assertRaisesRegex(ValueError, "gate_feature_mismatch"):
            self.export()
        self.assertFalse((self.output / "replay.json").exists())

    def test_invalid_probability_output_rejected(self):
        with patch.object(FrozenPipeline, "predict_proba", return_value=np.array([[float("nan"), 0.2]])):
            with self.assertRaisesRegex(ValueError, "invalid_saved_model_probabilities"):
                self.export()
        self.assertFalse((self.output / "replay.json").exists())

    def test_changed_paired_model_hash_rejected_before_deserialization(self):
        with (self.work / "models" / "paired_vitals.joblib").open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaisesRegex(ValueError, "saved_model_hash"):
            self.export()

    def test_incomplete_signal_or_changed_split_rejected(self):
        path = self.work / "signal_summary.json"
        item = json.loads(path.read_text())
        item["complete"] = False
        write_json(path, item)
        with self.assertRaisesRegex(ValueError, "complete_signal"):
            self.export()
        item["complete"] = True
        write_json(path, item)
        path = self.work / "patient_split.json"
        item = json.loads(path.read_text())
        item["assignments"]["1"] = "test"
        write_json(path, item)
        with self.assertRaisesRegex(ValueError, "source_fingerprint"):
            self.export()

    def test_selection_is_frozen_across_changes(self):
        self.export()
        self.mutate_feature(79)
        with self.assertRaisesRegex(ValueError, "existing_selection_or_provenance"):
            self.export()

    def test_output_outside_private_tree_symlink_and_hardlink_rejected(self):
        with self.assertRaisesRegex(ValueError, "ignored_replay_directory"):
            replay.export_replay(self.work, self.feasibility, self.root / "public")
        self.output.mkdir()
        (self.output / "replay.json").symlink_to(self.work / "patient_split.json")
        with self.assertRaisesRegex(ValueError, "unsafe_replay_output"):
            self.export()
        (self.output / "replay.json").unlink()
        (self.output / "replay.json").hardlink_to(self.work / "patient_split.json")
        with self.assertRaisesRegex(ValueError, "hardlink"):
            self.export()


class DemoServerPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "serve_demo.py"
        spec = importlib.util.spec_from_file_location("replay_demo_server", path)
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def test_only_explicit_public_assets_no_traversal_hidden_private_or_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "web"
            root.mkdir()
            (root / "index.html").write_text("Public demo")
            (root / "private.json").write_text("private")
            self.assertEqual(self.server.resolve_public_asset(root, "/"), root / "index.html")
            for path in ("/../private.json", "/%2e%2e/private.json", "/%252e%252e/private.json", "/.git/config",
                         "/artifacts/replay/replay.json", "/private.json", "/api/replay", "//index.html",
                         "/web/", "/%2findex.html", "/..\\index.html", "http://evil/index.html"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.server.resolve_public_asset(root, path)
            (root / "app.js").symlink_to(root / "private.json")
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.server.resolve_public_asset(root, "/app.js")

    def test_dns_rebinding_host_rejected(self):
        handler = self.server.DemoHandler.__new__(self.server.DemoHandler)
        handler.headers = {"Host": "attacker.example:8765"}
        handler.send_error = Mock()
        handler._serve(False)
        handler.send_error.assert_called_once_with(403, "Loopback Host required")

    def test_http_handler_public_response_and_private_refusal_without_socket_binding(self):
        class MemorySocket:
            def __init__(self, request):
                self.request = request
                self.response = bytearray()

            def makefile(self, mode, *args):
                return io.BytesIO(self.request)

            def sendall(self, data):
                self.response.extend(data)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "index.html").write_text("<html>Public research demo</html>")
            for method, path, host, status in (
                ("GET", "/", "127.0.0.1:8765", 200),
                ("HEAD", "/", "localhost:8765", 200),
                ("GET", "/api/replay", "127.0.0.1:8765", 404),
                ("GET", "/../artifacts/replay/replay.json", "127.0.0.1:8765", 404),
                ("GET", "/", "attacker.example", 403),
            ):
                request = f"{method} {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode()
                connection = MemorySocket(request)
                self.server.DemoHandler(connection, ("127.0.0.1", 9999), object(), root=root)
                headers, body = bytes(connection.response).split(b"\r\n\r\n", 1)
                self.assertIn(f" {status} ".encode(), headers.split(b"\r\n", 1)[0])
                if status == 200:
                    self.assertIn(b"connect-src 'none'", headers)
                    self.assertIn(b"Cache-Control: no-store", headers)
                if method == "HEAD":
                    self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
