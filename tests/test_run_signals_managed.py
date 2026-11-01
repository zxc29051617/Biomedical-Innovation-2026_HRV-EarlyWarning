"""Managed-launcher tests; no source datasets or real ECG processing are used."""

from contextlib import ExitStack
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_signals_managed.py"
SPEC = importlib.util.spec_from_file_location("run_signals_managed", SCRIPT)
managed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(managed)


class ManagedSignalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work, self.feasibility = self.root / "work", self.root / "feasibility"
        self.work.mkdir()
        self.feasibility.mkdir()
        self.clinical, self.waveform = self.root / "clinical", self.root / "waveform"
        self.clinical.mkdir()
        self.waveform.mkdir()
        self.paths = self.root / "paths.toml"
        self.paths.write_text('[data]\nclinical_root="clinical"\nwaveform_root="waveform"\n')

    def status(self):
        return json.loads((self.work / "signal_job_status.json").read_text())

    def fake_run(self, outcomes, complete=True, on_start=None, **run_options):
        calls, counter = [], [5]

        def popen(command, **kwargs):
            result, oom, text = outcomes[len(calls)]
            calls.append((command, kwargs))
            self.assertIsInstance(kwargs["stdout"], int)
            self.assertEqual(kwargs["stdout"], kwargs["stderr"])
            os.write(kwargs["stdout"], text.encode())
            if oom:
                counter[0] += 1
            if on_start:
                on_start()

            class Child:
                pid = 999999
                returncode = result

                def poll(self):
                    return self.returncode

            return Child()

        def cleanup(child, *args, **kwargs):
            child.returncode = child.returncode if child.returncode is not None else -15

        with ExitStack() as stack:
            stack.enter_context(patch.object(managed.subprocess, "Popen", side_effect=popen))
            stack.enter_context(patch.object(managed, "oom_kills", side_effect=lambda: counter[0]))
            stack.enter_context(patch.object(managed, "stop_process_group", side_effect=cleanup))
            stack.enter_context(patch.object(managed, "pipeline_status", return_value={"signals": {"status": "complete" if complete else "incomplete"}}))
            code = managed.run_managed(self.paths, self.work, self.feasibility, **run_options)
        return code, calls

    def test_retry_classifier_requires_worker_loss_and_new_oom_evidence(self):
        cases = [(1, "BrokenProcessPool", 2, 3, True), (-9, "", 2, 3, True),
                 (1, "BrokenProcessPool", 2, 2, False), (-9, "", None, 3, False),
                 (2, "invalid cache", 2, 3, False), (0, "BrokenProcessPool", 2, 3, False),
                 (137, "", 2, 3, False), (1, "BrokenProcessPool", 3, 2, False)]
        for code, tail, before, after, expected in cases:
            with self.subTest(case=(code, before, after)):
                self.assertEqual(managed.retryable_failure(code, tail, before, after), expected)

    def test_retry_limited_to_three_attempts_and_reduces_to_one_worker(self):
        code, calls = self.fake_run([(1, True, "BrokenProcessPool\n")] * 3)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual([command[command.index("--workers") + 1] for command, _ in calls], ["2", "1", "1"])
        self.assertEqual(self.status()["reason"], "oom_retry_limit")
        self.assertEqual(self.status()["attempt"], 3)
        self.assertEqual((self.work / "signal_job.log").read_text().count("BrokenProcessPool"), 3)
        for _, kwargs in calls:
            for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
                self.assertEqual(kwargs["env"][name], "1")
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(len(kwargs["pass_fds"]), 3)

    def test_non_oom_error_never_retries_or_claims_completion(self):
        code, calls = self.fake_run([(1, False, "BrokenProcessPool\n")])
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.status()["state"], "failed")
        self.assertEqual(self.status()["reason"], "nonretryable_child_failure")

    def test_bounded_engine_command_inherits_mutex_and_halves_workers_on_oom(self):
        for initial, expected in ((8, ["8", "4", "2"]), (4, ["4", "2", "1"])):
            with self.subTest(initial=initial):
                code, calls = self.fake_run([(1, True, "BrokenProcessPool\n")] * 3,
                                           engine="bounded-spawn", workers=initial)
                self.assertEqual(code, 2)
                self.assertEqual([command[command.index("--workers") + 1] for command, _ in calls], expected)
                for command, kwargs in calls:
                    self.assertIn("hrv_early_warning.signal_runtime", command)
                    fd = int(command[command.index("--inherited-lock-fd") + 1])
                    self.assertEqual(fd, kwargs["pass_fds"][1])
                self.assertEqual(self.status()["engine"], "bounded-spawn")

    def test_legacy_remains_default_and_rejects_more_than_two_workers(self):
        code, calls = self.fake_run([(0, False, "")])
        self.assertEqual(code, 0)
        self.assertIn("hrv_early_warning", calls[0][0])
        self.assertNotIn("--inherited-lock-fd", calls[0][0])
        self.assertEqual(self.status()["engine"], "legacy")
        with patch.object(managed.subprocess, "Popen") as child:
            for workers, engine in ((4, "legacy"), (9, "bounded-spawn"), (0, "bounded-spawn")):
                with self.subTest(workers=workers, engine=engine), self.assertRaises(ValueError):
                    managed.run_managed(self.paths, self.work, self.feasibility, workers=workers, engine=engine)
            child.assert_not_called()

    def test_success_requires_complete_cache_and_can_follow_oom_retry(self):
        code, calls = self.fake_run([(1, True, "BrokenProcessPool\n"), (0, False, "complete\n")])
        self.assertEqual(code, 0)
        self.assertEqual(self.status()["state"], "complete")
        self.assertEqual(self.status()["workers"], 1)
        code, _ = self.fake_run([(0, False, "exit zero\n")], complete=False)
        self.assertEqual(code, 2)
        self.assertEqual(self.status()["reason"], "child_success_without_complete_signal_cache")

    def test_existing_stop_request_refuses_start_without_deleting_request(self):
        path = self.work / "signal_stop_request.json"
        for content in ('{"action":"stop"}', "invalid json"):
            path.write_text(content)
            with patch.object(managed.subprocess, "Popen") as child, self.assertRaisesRegex(ValueError, "existing_stop_request"):
                managed.run_managed(self.paths, self.work, self.feasibility)
            child.assert_not_called()
            self.assertEqual(path.read_text(), content)
            self.assertFalse((self.work / "signal_job_status.json").exists())

    def test_stop_request_during_attempt_stops_and_retains_request(self):
        request = self.work / "signal_stop_request.json"
        code, calls = self.fake_run([(None, False, "")], on_start=lambda: request.write_text('{"action":"stop"}'))
        self.assertEqual(code, 130)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.status()["state"], "stopped")
        self.assertTrue(request.exists())

    def test_sigterm_handler_stops_active_group_and_restores_handler(self):
        before = signal.getsignal(signal.SIGTERM)
        code, _ = self.fake_run([(None, False, "")], on_start=lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.assertEqual(code, 130)
        self.assertEqual(self.status()["state"], "stopped")
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_mutex_refuses_duplicate_without_replacing_status(self):
        path = self.work / "signal_job_status.json"
        path.write_text("existing owner")
        with (self.work / "signal_job.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(managed.subprocess, "Popen") as child, self.assertRaises(BlockingIOError):
                managed.run_managed(self.paths, self.work, self.feasibility)
            child.assert_not_called()
        self.assertEqual(path.read_text(), "existing owner")

    def test_output_source_and_symlink_guards_run_before_child_start(self):
        for source in (self.clinical, self.waveform):
            with self.subTest(source=source), self.assertRaises(ValueError):
                managed.run_managed(self.paths, source, self.feasibility)
        for name in ("signal_job.log", "signal_job_status.json", "signal_job.lock", "signals.json", "epochs.sqlite"):
            target = self.clinical / "source"
            target.write_text("keep")
            link = self.work / name
            link.symlink_to(target)
            try:
                with self.subTest(name=name), patch.object(managed.subprocess, "Popen") as child, self.assertRaises(ValueError):
                    managed.run_managed(self.paths, self.work, self.feasibility)
                child.assert_not_called()
                self.assertEqual(target.read_text(), "keep")
            finally:
                link.unlink()

    @staticmethod
    def alive(pid):
        try:
            # A zombie cannot run or retain a lock and awaits the host's reaper.
            return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def test_killed_launcher_watchdog_cleans_cli_and_worker_before_unlock(self):
        pid_path, lock_path = self.root / "pids.json", self.root / "guard.lock"
        child_code = ("import json,os,subprocess,sys,time; "
                      "worker=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                      f"open({str(pid_path)!r},'w').write(json.dumps([os.getpid(),worker.pid])); "
                      "time.sleep(60)")
        # Launcher creates a watchdog with the same lifeline/lock handoff as run_managed.
        launcher_code = (
            "import fcntl,os,subprocess,sys,time; "
            f"lock=os.open({str(lock_path)!r},os.O_CREAT|os.O_RDWR,0o600); "
            "fcntl.flock(lock,fcntl.LOCK_EX); r,w=os.pipe(); gr,gw=os.pipe(); "
            f"p=subprocess.Popen([sys.executable,{str(SCRIPT)!r},'--supervise',str(r),str(lock),str(gw),'--',sys.executable,'-c',{child_code!r}],"
            "pass_fds=(r,lock,gw),start_new_session=True); os.close(r); os.close(gw); time.sleep(60)"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SCRIPT.parents[1] / "src")
        launcher = subprocess.Popen([sys.executable, "-c", launcher_code], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        pids = []
        try:
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pid_path.exists(), "synthetic child did not start")
            pids = json.loads(pid_path.read_text())
            self.assertTrue(all(self.alive(pid) for pid in pids))
            launcher.kill()
            launcher.wait(timeout=5)
            deadline = time.monotonic() + 8
            while any(self.alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(any(self.alive(pid) for pid in pids), "CLI or worker survived launcher SIGKILL")
            # Watchdog retains mutex throughout its bounded group cleanup.
            deadline = time.monotonic() + 3
            with lock_path.open("a") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail("watchdog did not release mutex after cleanup")
                        time.sleep(.05)
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait(timeout=5)
            for pid in pids:
                if self.alive(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_killed_watchdog_runner_cleans_known_cli_group_and_does_not_retry_without_oom(self):
        pid_path = self.root / "child_pids.json"
        child_code = ("import json,os,subprocess,sys,time; "
                      "worker=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                      f"open({str(pid_path)!r},'w').write(json.dumps([os.getpid(),worker.pid])); time.sleep(60)")
        controller_code = f"""
import importlib.util, sys
spec=importlib.util.spec_from_file_location('managed', {str(SCRIPT)!r})
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
original=m.subprocess.Popen
def synthetic_cli(command, **kwargs):
    command=command[:command.index('--')+1]+[sys.executable,'-c',{child_code!r}]
    return original(command, **kwargs)
m.subprocess.Popen=synthetic_cli
sys.exit(m.run_managed({str(self.paths)!r},{str(self.work)!r},{str(self.feasibility)!r}))
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SCRIPT.parents[1] / "src")
        controller = subprocess.Popen([sys.executable, "-c", controller_code], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        pids = []
        try:
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pid_path.exists())
            pids = json.loads(pid_path.read_text())
            watchdog = self.status()["supervisor_pid"]
            os.kill(watchdog, signal.SIGKILL)
            self.assertEqual(controller.wait(timeout=15), 2)
            deadline = time.monotonic() + 2
            while any(self.alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(any(self.alive(pid) for pid in pids), "orphan survived watchdog SIGKILL")
            self.assertEqual(self.status()["state"], "failed")
            self.assertEqual(self.status()["attempt"], 1)
            with (self.work / "signal_job.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=5)
            for pid in pids:
                if self.alive(pid):
                    os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
