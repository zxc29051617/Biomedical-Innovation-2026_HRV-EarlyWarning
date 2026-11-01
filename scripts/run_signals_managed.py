"""Run the unchanged ECG CLI with a mutex, bounded OOM recovery and stop control.

All logs/status remain in the ignored work directory. A stop request is never
deleted: remove it deliberately before a later restart. No cache, algorithm,
quality parameter, patient split or fingerprint is modified by this wrapper.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

from hrv_early_warning.cli import load_paths
from hrv_early_warning.progress import pipeline_status


HEARTBEAT_SECONDS = 30
POLL_SECONDS = 1
STOP_GRACE_SECONDS = 10
WORK_FILES = ("signal_job.lock", "signal_job_status.json", "signal_job.log", "signal_stop_request.json",
              "signals.json", "signal_summary.json", "signal_plan.json", "patient_split.json",
              "epochs.sqlite", "epochs.sqlite-journal", "epochs.sqlite-wal", "epochs.sqlite-shm")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def protected_paths(paths, source_roots):
    for path in paths:
        path = Path(path)
        if path.is_symlink() or any(path.resolve().is_relative_to(root) for root in source_roots):
            raise ValueError("output_or_cache_path_overlaps_source_or_is_symlink")


def oom_kills():
    """Return this cgroup's counter, or None when unavailable; never guess OOM."""
    try:
        group = next(line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines()
                     if line.startswith("0::"))
        root = Path("/sys/fs/cgroup")
        paths = (root / group.lstrip("/\n"), root)
        for directory in paths:
            events = directory / "memory.events"
            if events.is_file():
                values = dict(line.split() for line in events.read_text().splitlines())
                return int(values["oom_kill"])
    except (OSError, ValueError, KeyError, StopIteration):
        pass
    return None


def retryable_failure(returncode, log_tail, before, after):
    oom_increased = before is not None and after is not None and after > before
    worker_loss = returncode == -signal.SIGKILL or (returncode != 0 and "BrokenProcessPool" in log_tail)
    return oom_increased and worker_loss


def stop_requested(path):
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        raise ValueError("stop_request_must_not_be_symlink")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError("invalid_stop_request") from None
    if not isinstance(value, dict) or value.get("action") != "stop":
        raise ValueError("invalid_stop_request")
    return True


def stop_process_group(process, signum=signal.SIGTERM, grace=STOP_GRACE_SECONDS):
    """Terminate the whole dedicated child group, including orphaned workers."""
    def send(signum):
        try:
            os.killpg(process.pid, signum)
            return True
        except ProcessLookupError:
            return False

    send(signum)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()  # Reap our direct child while waiting for its workers.
        if not send(0):
            break
        time.sleep(.1)
    if send(0):
        send(signal.SIGKILL)
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError("child_did_not_exit_after_kill") from None


def cleanup_cli_group(pgid):
    """The CLI is not our direct child when the watchdog dies."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def exec_gated(gate_fd, command):
    """No ECG may start until its PGID has been reported to the runner."""
    permit = os.read(gate_fd, 1)
    os.close(gate_fd)
    if permit != b"G":
        return 130
    os.execvpe(command[0], command, os.environ)


def supervise(lifeline_fd, lock_fd, group_fd, command):
    """Small watchdog: keep the mutex until the entire CLI group is cleaned.

    The runner owns the only writer of lifeline_fd. Even SIGKILL closes it;
    this process then kills the CLI's separate process group. The watchdog
    never imports signal/model dependencies or reads any patient artifacts.
    """
    requested = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda received, frame: requested.append(received))
    child = None
    gate_write = None
    try:
        ready, _, _ = select.select([lifeline_fd], [], [], 0)
        if ready and os.read(lifeline_fd, 1) == b"":
            return 130
        gate_read, gate_write = os.pipe()
        try:
            # The CLI and its forked workers retain the mutex too: simultaneous
            # loss of both controllers must not permit a second active writer.
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--exec-gated",
                                      str(gate_read), "--", *command], pass_fds=(gate_read, lock_fd),
                                     start_new_session=True, stdin=subprocess.DEVNULL)
        finally:
            os.close(gate_read)
        # A watchdog killed before this write leaves the gate closed (EOF), so
        # the helper exits without running ECG. Afterwards the runner knows PGID.
        os.write(group_fd, (str(child.pid) + "\n").encode("ascii"))
        os.close(group_fd)
        group_fd = None
        os.write(gate_write, b"G")
        os.close(gate_write)
        gate_write = None
        while child.poll() is None:
            ready, _, _ = select.select([lifeline_fd], [], [], .2)
            if requested or (ready and os.read(lifeline_fd, 1) == b""):
                stop_process_group(child, requested[0] if requested else signal.SIGTERM, grace=5)
                return 130
        code = child.returncode
        # A crashed CLI may have surviving workers; cleanup precedes mutex release.
        stop_process_group(child, grace=5)
        if code < 0:
            if -code not in (signal.SIGKILL, signal.SIGSTOP):
                signal.signal(-code, signal.SIG_DFL)
            os.kill(os.getpid(), -code)  # Preserve killed-by-signal semantics.
        return code
    finally:
        if gate_write is not None:
            os.close(gate_write)
        if child is not None and child.poll() is None:
            stop_process_group(child, grace=5)
        if group_fd is not None:
            os.close(group_fd)
        os.close(lifeline_fd)
        os.close(lock_fd)


def atomic_status(path, payload, roots):
    protected_paths((path,), roots)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".signal_status_", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_managed(paths, work_dir, feasibility_dir, workers=2, max_attempts=3):
    if isinstance(workers, bool) or not isinstance(workers, int) or workers not in (1, 2):
        raise ValueError("workers_must_be_one_or_two")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts_must_be_one_to_three")
    roots = load_paths(Path(paths))
    work, feasibility = Path(work_dir).resolve(), Path(feasibility_dir).resolve()
    protected_paths((work, feasibility, *(work / name for name in WORK_FILES),
                     feasibility / "waveform_metadata.jsonl", feasibility / "summary.json"), roots)
    stop_path = work / "signal_stop_request.json"
    if stop_path.exists() or stop_path.is_symlink():
        raise ValueError("existing_stop_request_refuses_start")
    work.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(work / "signal_job.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Recheck after obtaining the mutex to close the initial request race.
        if stop_path.exists() or stop_path.is_symlink():
            raise ValueError("existing_stop_request_refuses_start")
        status_path, log_path = work / "signal_job_status.json", work / "signal_job.log"
        started = utc_now()
        state = {"started_utc": started, "runner_pid": os.getpid(), "attempt": 0, "workers": workers}
        requested_signal = []
        prior_handlers = {}
        process = None
        lifeline_write = None
        group_read = None
        child_group = None

        def on_signal(signum, frame):
            requested_signal.append(signum)

        def update(name, **extra):
            state.update(state=name, updated_utc=utc_now(), oom_kill=oom_kills(), **extra)
            atomic_status(status_path, state, roots)

        def stopping():
            return bool(requested_signal) or stop_requested(stop_path)

        def read_child_group():
            nonlocal child_group
            if group_read is None or child_group is not None:
                return
            try:
                message = os.read(group_read, 64)
            except BlockingIOError:
                return
            if message:
                if not message.endswith(b"\n") or not message.strip().isdigit():
                    raise RuntimeError("invalid_child_group_handshake")
                child_group = int(message)
                if child_group <= 1:
                    raise RuntimeError("invalid_child_group_handshake")
                state["cli_pid"] = child_group

        def cleanup(signum=signal.SIGTERM):
            if process is not None:
                stop_process_group(process, signum)
            read_child_group()
            if child_group is not None:
                cleanup_cli_group(child_group)

        for signum in (signal.SIGINT, signal.SIGTERM):
            prior_handlers[signum] = signal.signal(signum, on_signal)
        try:
            update("starting")
            for attempt in range(1, max_attempts + 1):
                state.update(attempt=attempt, workers=workers, attempt_started_utc=utc_now())
                if stopping():
                    update("stopped", reason="stop_requested_before_child_start")
                    return 130
                before = oom_kills()
                env = os.environ.copy()
                env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
                source_dir = str(Path(__file__).resolve().parents[1] / "src")
                env["PYTHONPATH"] = source_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
                command = [sys.executable, "-u", "-m", "hrv_early_warning", "signals",
                           "--paths", str(Path(paths).resolve()), "--work-dir", str(work),
                           "--feasibility-dir", str(feasibility), "--workers", str(workers),
                           "--output", str(work / "signals.json")]
                protected_paths((work / name for name in WORK_FILES), roots)
                log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                lifeline_read, lifeline_write = os.pipe()
                group_read, group_write = os.pipe()
                os.set_blocking(group_read, False)
                child_group = None
                try:
                    log_start = os.fstat(log_fd).st_size
                    supervisor = [sys.executable, "-u", str(Path(__file__).resolve()), "--supervise",
                                  str(lifeline_read), str(lock_fd), str(group_write), "--", *command]
                    process = subprocess.Popen(supervisor, stdout=log_fd, stderr=log_fd, env=env,
                                               start_new_session=True, pass_fds=(lifeline_read, lock_fd, group_write),
                                               cwd=Path(__file__).resolve().parents[1])
                finally:
                    os.close(log_fd)
                    os.close(lifeline_read)
                    os.close(group_write)
                update("running", supervisor_pid=process.pid, attempt_oom_kill_start=before)
                heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                while process.poll() is None:
                    read_child_group()
                    if stopping():
                        update("stopping", reason="signal_received" if requested_signal else "stop_requested")
                        cleanup(requested_signal[0] if requested_signal else signal.SIGTERM)
                        update("stopped", child_returncode=process.returncode)
                        return 130
                    if time.monotonic() >= heartbeat:
                        update("running")
                        heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                    time.sleep(POLL_SECONDS)
                code = process.returncode
                read_child_group()
                os.close(lifeline_write)
                lifeline_write = None
                if stopping():
                    cleanup()
                    update("stopped", reason="stop_requested_at_child_exit", child_returncode=code)
                    return 130
                after = oom_kills()
                if code == 0:
                    if pipeline_status(work)["signals"]["status"] != "complete":
                        update("failed", reason="child_success_without_complete_signal_cache", child_returncode=0)
                        return 2
                    update("complete", child_returncode=0)
                    return 0
                cleanup()
                with log_path.open("rb") as stream:
                    stream.seek(max(log_start, log_path.stat().st_size - 65536))
                    tail = stream.read().decode("utf-8", errors="replace")
                retry = retryable_failure(code, tail, before, after)
                if not retry or attempt == max_attempts:
                    update("failed", reason="oom_retry_limit" if retry else "nonretryable_child_failure", child_returncode=code)
                    return 2
                workers = 1
                update("retrying", reason="oom_worker_loss", child_returncode=code, next_workers=workers)
                os.close(group_read)
                group_read = None
            raise RuntimeError("attempt_loop_exhausted")
        except BaseException as error:
            cleanup()
            update("failed", reason=type(error).__name__)
            raise
        finally:
            if lifeline_write is not None:
                os.close(lifeline_write)
            if group_read is not None:
                os.close(group_read)
            for signum, handler in prior_handlers.items():
                signal.signal(signum, handler)
    finally:
        os.close(lock_fd)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--exec-gated":
        if len(argv) < 4 or argv[2] != "--":
            raise ValueError("invalid_internal_gate_arguments")
        return exec_gated(int(argv[1]), argv[3:])
    if argv and argv[0] == "--supervise":
        if len(argv) < 6 or argv[4] != "--":
            raise ValueError("invalid_internal_supervisor_arguments")
        return supervise(int(argv[1]), int(argv[2]), int(argv[3]), argv[5:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", type=Path, default=Path("configs/paths.local.toml"))
    parser.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    parser.add_argument("--feasibility-dir", type=Path, default=Path("artifacts/feasibility"))
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--max-attempts", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args(argv)
    try:
        return run_managed(args.paths, args.work_dir, args.feasibility_dir, args.workers, args.max_attempts)
    except (OSError, ValueError, RuntimeError) as error:
        # Do not echo source contents or raw errors containing patient paths.
        print("Managed signal job refused or failed: " + type(error).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
