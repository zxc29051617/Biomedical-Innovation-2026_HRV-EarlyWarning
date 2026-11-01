"""Executor equivalence and resumption tests with synthetic cohorts/signals only."""

from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timedelta
import gc
import fcntl
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from hrv_early_warning.cohort import StudyStay
from hrv_early_warning.risksets import StayOutcome
from hrv_early_warning import signal_pipeline as frozen
from hrv_early_warning import signal_runtime as runtime
from hrv_early_warning.signal_quality import QualityConfig
from hrv_early_warning.vasopressors import FirstUseResult


def fake_result(task):
    icu, ends = task
    return [{"icu": icu, "end": end.isoformat(), "accepted": end.minute == 0,
             "decode_status": "ok", "decode_errors": [], "coverage_fraction": 1.0,
             "reasons": [] if end.minute == 0 else ["synthetic_rejection"],
             "features": {"mean_rr_ms": 800.0} if end.minute == 0 else {}, "metrics": {}} for end in ends]


class ImmediateExecutor:
    def __init__(self, fail_at=None, **kwargs):
        self.kwargs, self.fail_at, self.submitted = kwargs, fail_at, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, task):
        self.submitted.append(task)
        result = Future()
        try:
            if self.fail_at == len(self.submitted):
                raise RuntimeError("synthetic_worker_failure")
            result.set_result(function(task))
        except BaseException as error:
            result.set_exception(error)
        return result


HAS_SIGNALS = all(importlib.util.find_spec(name) for name in ("numpy", "scipy", "wfdb", "threadpoolctl"))


@unittest.skipUnless(HAS_SIGNALS, "optional signal dependencies required")
class SignalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.waveforms, self.feasibility, self.work = [self.root / name for name in ("waveforms", "feasibility", "work")]
        for directory in (self.waveforms, self.feasibility, self.work):
            directory.mkdir()
        self.quality = self.root / "quality.toml"
        self.quality.write_text("")
        for name in ("stay_outcomes.jsonl", "waveform_metadata.jsonl"):
            (self.feasibility / name).write_text("synthetic source\n")
        self.start = datetime(2100, 1, 1)
        self.stays, self.records = [], []
        for subject, event in ((1, self.start + timedelta(hours=10)), (2, None)):
            stay = StudyStay(subject, subject, subject, self.start, self.start + timedelta(hours=12),
                             self.start + timedelta(hours=12), 40, False)
            outcome = FirstUseResult(event, event, stay.followup_end, "qualifying_first_use" if event else "no_exposure", 60 if event else 0)
            self.stays.append(StayOutcome(stay, outcome))
            self.records.append(SimpleNamespace(subject_id=subject, start=self.start, end=self.start + timedelta(hours=12)))
        self.splits = {1: "train", 2: "validation"}
        self.split_stats = {"split_counts": {"train": 1, "validation": 1, "test": 0}}

    def run_pipeline(self, work=None, legacy=False, fail_at=None):
        executors = []

        def factory(**kwargs):
            executor = ImmediateExecutor(fail_at=fail_at, **kwargs)
            executors.append(executor)
            return executor

        with ExitStack() as stack:
            stack.enter_context(patch.object(frozen, "load_stay_outcomes", return_value=(self.stays, {})))
            stack.enter_context(patch.object(frozen, "ensure_patient_split", return_value=(self.splits, self.split_stats)))
            stack.enter_context(patch.object(frozen, "load_waveform_index", return_value=(self.records, {})))
            stack.enter_context(patch.object(frozen, "_worker", side_effect=fake_result))
            stack.enter_context(patch.object(frozen if legacy else runtime, "ProcessPoolExecutor", side_effect=factory))
            function = frozen.run_signal_screen if legacy else runtime.run_signal_screen_spawn
            result = function(self.waveforms, self.feasibility, work or self.work, self.quality, workers=1)
        return result, executors

    def cache_rows(self, work=None):
        with closing(sqlite3.connect((work or self.work) / "epochs.sqlite")) as connection:
            return connection.execute("SELECT * FROM epochs ORDER BY icustay_id,endtime").fetchall()

    def cache_metadata(self, work=None):
        with closing(sqlite3.connect((work or self.work) / "epochs.sqlite")) as connection:
            return dict(connection.execute("SELECT key,value FROM metadata"))

    def test_original_plan_rows_fingerprint_and_summary_are_identical(self):
        legacy_work = self.root / "legacy"
        original, _ = self.run_pipeline(legacy_work, legacy=True)
        current, executors = self.run_pipeline()
        self.assertEqual(self.cache_rows(legacy_work), self.cache_rows())
        self.assertEqual(json.loads(self.cache_metadata(legacy_work)["fingerprint"]), json.loads(self.cache_metadata()["fingerprint"]))
        self.assertEqual(json.loads((legacy_work / "signal_plan.json").read_text()), json.loads((self.work / "signal_plan.json").read_text()))
        for value in (original, current):
            value.pop("elapsed_seconds")
        current.pop("execution")
        current.pop("execution_manifest_sha256")
        self.assertEqual(original, current)
        executor = executors[0]
        self.assertEqual(executor.kwargs["mp_context"].get_start_method(), "spawn")
        self.assertEqual(executor.kwargs["initargs"], (asdict(QualityConfig()),))
        self.assertIs(executor.kwargs["initializer"], runtime.runtime_worker_init)
        for icu, records, ends in executor.submitted:
            self.assertTrue(1 <= len(ends) <= 24)
            self.assertEqual([record.subject_id for record in records], [icu])

    def test_migrate_complete_legacy_cache_without_reprocessing_and_record_execution(self):
        original, _ = self.run_pipeline(legacy=True)
        before = self.cache_rows()
        result, executors = self.run_pipeline()
        self.assertEqual(executors, [])
        self.assertEqual(before, self.cache_rows())
        execution = result["execution"]
        self.assertEqual(execution["cache_before"], original["screened_epochs"])
        self.assertEqual(execution["migration"]["legacy_cached_epochs_at_first_spawn"], original["screened_epochs"])
        self.assertEqual(execution["state"], "complete")
        self.assertEqual(result["execution_manifest_sha256"], frozen._hash_file(self.work / "signal_execution.json"))
        self.assertIn("bounded_spawn_fingerprint", self.cache_metadata())

    def test_worker_failure_retains_commits_then_resumes_same_rows(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic_worker_failure"):
            self.run_pipeline(fail_at=3)
        partial = self.cache_rows()
        self.assertGreater(len(partial), 0)
        self.assertFalse((self.work / "signal_summary.json").exists())
        failed = json.loads((self.work / "signal_execution.json").read_text())
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["cache_after"], len(partial))
        result, executors = self.run_pipeline()
        self.assertEqual(result["execution"]["cache_before"], len(partial))
        self.assertEqual(result["execution"]["prior_runs"][0]["state"], "failed")
        self.assertEqual(sum(len(task[2]) for task in executors[0].submitted), result["screened_epochs"] - len(partial))
        legacy_work = self.root / "legacy"
        self.run_pipeline(legacy_work, legacy=True)
        self.assertEqual(self.cache_rows(), self.cache_rows(legacy_work))

    def test_any_frozen_source_configuration_or_runtime_code_change_is_rejected(self):
        self.run_pipeline()
        original_rows = self.cache_rows()
        for source in (self.feasibility / "stay_outcomes.jsonl", self.feasibility / "waveform_metadata.jsonl"):
            original = source.read_bytes()
            source.write_bytes(b"changed")
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "configuration/source changed"):
                self.run_pipeline()
            source.write_bytes(original)
        self.quality.write_text("minimum_beats = 101\n")
        with self.assertRaisesRegex(ValueError, "configuration/source changed"):
            self.run_pipeline()
        self.quality.write_text("")
        changed = runtime.execution_fingerprint()
        changed["driver_sha256"] = "changed"
        with patch.object(runtime, "execution_fingerprint", return_value=changed), self.assertRaisesRegex(ValueError, "execution_code_changed"):
            self.run_pipeline()
        self.assertEqual(self.cache_rows(), original_rows)

    def test_worker_receives_exact_icu_records_and_clears_them_after_failure(self):
        records = self.records[:1]
        ends = [self.start + timedelta(hours=1)]

        def inspect_task(task):
            self.assertEqual(task, (1, ends))
            self.assertEqual(list(frozen._RECORDS), [1])
            self.assertIs(frozen._RECORDS[1], records)
            raise RuntimeError("deliberate")

        with patch.object(frozen, "_worker", side_effect=inspect_task), self.assertRaisesRegex(RuntimeError, "deliberate"):
            runtime.runtime_worker((1, records, ends))
        self.assertEqual(frozen._RECORDS, {})
        for count in (0, 25):
            with self.assertRaises(ValueError):
                runtime.runtime_worker((1, records, ends * count))

    def test_bounded_submission_releases_future_results(self):
        live, peak = [0], [0]

        class Executor:
            def submit(self, function, task):
                future = Future()
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                weakref.finalize(future, lambda: live.__setitem__(0, live[0] - 1))
                future.set_result(task)
                return future

        self.assertEqual(sorted(runtime.bounded_results(Executor(), range(100), 4)), list(range(100)))
        gc.collect()
        self.assertLessEqual(peak[0], 4)
        self.assertEqual(live[0], 0)

    def test_real_spawn_frozen_worker_matches_direct_wfdb_decode(self):
        import numpy as np
        import wfdb
        from hrv_early_warning.waveforms import read_record_metadata

        folder = self.waveforms / "p76/p765432"
        folder.mkdir(parents=True)
        wfdb.wrsamp("synthetic", fs=125, units=["mV"], sig_name=["II"], p_signal=np.zeros((37500, 1)),
                    fmt=["16"], adc_gain=[1000], baseline=[0], base_datetime=self.start, write_dir=str(folder))
        records = [read_record_metadata(self.waveforms, "p76/p765432/synthetic")]
        ends = [self.start + timedelta(minutes=5)]
        config = asdict(QualityConfig())
        frozen._worker_init({7: records}, config)
        expected = frozen._worker((7, ends))
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=runtime.runtime_worker_init, initargs=(config,)) as pool:
            actual = pool.submit(runtime.runtime_worker, (7, records, ends)).result(timeout=30)
        self.assertEqual(actual, expected)
        self.assertEqual(actual[0]["decode_status"], "ok")
        self.assertFalse(actual[0]["accepted"])

    def test_sqlite_sidecar_and_source_symlinks_rejected_before_loading(self):
        target = self.waveforms / "source"
        target.write_text("unchanged")
        for name in ("epochs.sqlite", "epochs.sqlite-journal", "epochs.sqlite-wal", "epochs.sqlite-shm", "signal_execution.json"):
            link = self.work / name
            link.symlink_to(target)
            try:
                with self.subTest(name=name), patch.object(frozen, "load_stay_outcomes") as load, self.assertRaises(ValueError):
                    runtime.run_signal_screen_spawn(self.waveforms, self.feasibility, self.work, self.quality)
                load.assert_not_called()
            finally:
                link.unlink()
        self.assertEqual(target.read_text(), "unchanged")

    def test_no_summary_if_row_count_is_incomplete(self):
        original = runtime.bounded_results

        def drop_last(pool, tasks, limit):
            iterator = original(pool, tasks, limit)
            next(iterator)
            yield from iterator

        with patch.object(runtime, "bounded_results", side_effect=drop_last), self.assertRaisesRegex(ValueError, "cache_incomplete"):
            self.run_pipeline()
        self.assertFalse((self.work / "signal_summary.json").exists())

    def test_inherited_mutex_uses_same_open_description_without_unlocking_parent(self):
        path = self.work / "signal_job.lock"
        parent = runtime.acquire_signal_lock(path)
        try:
            duplicate = runtime.acquire_signal_lock(path, parent)
            self.assertNotEqual(duplicate, parent)
            os.close(duplicate)
            with path.open("a") as contender, self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(parent)
        descriptor = runtime.acquire_signal_lock(path)
        os.close(descriptor)

    def test_inherited_mutex_rejects_wrong_file_nonregular_and_standard_descriptors(self):
        path = self.work / "signal_job.lock"
        path.touch()
        with (self.work / "wrong.lock").open("a") as wrong:
            with self.assertRaisesRegex(ValueError, "does_not_match"):
                runtime.acquire_signal_lock(path, wrong.fileno())
        for fd in (-1, 0, 1, 2):
            with self.subTest(fd=fd), self.assertRaisesRegex(ValueError, "invalid_inherited"):
                runtime.acquire_signal_lock(path, fd)
        read_fd, write_fd = os.pipe()
        try:
            with self.assertRaisesRegex(ValueError, "does_not_match"):
                runtime.acquire_signal_lock(path, read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        with path.open("a") as valid:
            alias = self.work / "alias.lock"
            alias.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "must_not_be_symlink"):
                runtime.acquire_signal_lock(alias, valid.fileno())

    def test_runtime_main_accepts_verified_parent_mutex_and_rejects_wrong_fd_before_worker(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io

        config = self.root / "paths.toml"
        config.write_text('[data]\nclinical_root="clinical"\nwaveform_root="waveforms"\n')
        path = self.work / "signal_job.lock"
        parent = runtime.acquire_signal_lock(path)
        arguments = ["--paths", str(config), "--work-dir", str(self.work),
                     "--feasibility-dir", str(self.feasibility), "--inherited-lock-fd", str(parent)]
        try:
            with patch.object(runtime, "run_signal_screen_spawn", return_value={"complete": True}) as run:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runtime.main(arguments), 0)
                run.assert_called_once()
            with (self.work / "other.lock").open("a") as wrong:
                arguments[-1] = str(wrong.fileno())
                with patch.object(runtime, "run_signal_screen_spawn") as run, redirect_stderr(io.StringIO()):
                    self.assertEqual(runtime.main(arguments), 2)
                run.assert_not_called()
        finally:
            os.close(parent)


if __name__ == "__main__":
    unittest.main()
