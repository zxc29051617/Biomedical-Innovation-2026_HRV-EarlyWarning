"""Bounded spawn execution for the unchanged, frozen ECG screening algorithm.

This module changes execution and memory ownership only. Frozen plan/worker/
summary functions and their cache fingerprint remain authoritative. The CLI
acquires signal_job.lock; do not start it alongside another signal writer.
"""

from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
import time
import tomllib

from . import signal_pipeline as frozen


EXECUTION_VERSION = "bounded_spawn_v1"
BATCH_SIZE = 24


def runtime_worker_init(config):
    """Spawn initializer receives quality settings, never cohort metadata."""
    frozen._worker_init({}, config)


def runtime_worker(task):
    """Call the exact frozen worker with just the current ICU's records."""
    icu, records, ends = task
    if not 1 <= len(ends) <= BATCH_SIZE:
        raise ValueError("runtime_batch_must_contain_one_to_24_epochs")
    frozen._RECORDS = {icu: records}
    try:
        return frozen._worker((icu, ends))
    finally:
        frozen._RECORDS = {}


def bounded_results(executor, tasks, limit):
    """Keep at most limit tasks/results alive; never collect all futures."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("positive_inflight_limit_required")
    iterator, pending = iter(tasks), set()
    exhausted = False
    try:
        while pending or not exhausted:
            while not exhausted and len(pending) < limit:
                try:
                    task = next(iterator)
                except StopIteration:
                    exhausted = True
                else:
                    pending.add(executor.submit(runtime_worker, task))
            if not pending:
                break
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            while completed:
                future = completed.pop()
                pending.remove(future)
                result = future.result()
                del future
                yield result
                del result
            del completed
    finally:
        for future in pending:
            future.cancel()


def pending_tasks(all_epochs, done, by_icu):
    for icu, ends in sorted(all_epochs.items()):
        remaining = sorted(ends - done.get(icu, set()))
        for index in range(0, len(remaining), BATCH_SIZE):
            yield icu, by_icu[icu], remaining[index:index + BATCH_SIZE]


def cache_fingerprint(source_work, config):
    """Exactly the dictionary constructed by frozen.run_signal_screen."""
    from .signal_quality import ALGORITHM_VERSION
    import numpy
    import scipy
    import wfdb

    code = Path(frozen.__file__).parent
    return {"schema": 1, "algorithm": ALGORITHM_VERSION, "config": asdict(config),
            "versions": {"numpy": numpy.__version__, "scipy": scipy.__version__, "wfdb": wfdb.__version__},
            "code": {name: frozen._hash_file(code / name) for name in ("ecg.py", "signal_quality.py", "signal_pipeline.py")},
            "stay_outcomes_sha256": frozen._hash_file(source_work / "stay_outcomes.jsonl"),
            "waveform_metadata_sha256": frozen._hash_file(source_work / "waveform_metadata.jsonl"),
            "source_validation": "metadata cache fingerprint; not waveform byte checksums"}


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError("runtime_output_must_not_be_symlink")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def execution_fingerprint():
    return {"executor": EXECUTION_VERSION, "driver_sha256": frozen._hash_file(Path(__file__)),
            "worker_wrapper_sha256": hashlib.sha256(inspect.getsource(runtime_worker).encode()).hexdigest(),
            "initializer_sha256": hashlib.sha256(inspect.getsource(runtime_worker_init).encode()).hexdigest()}


def run_signal_screen_spawn(waveform_root, feasibility_dir, work_dir, quality_path, workers=2, progress=None):
    from .signal_quality import QualityConfig

    root, source_work, work = map(lambda p: Path(p).resolve(), (waveform_root, feasibility_dir, work_dir))
    if work.is_relative_to(root) or source_work.is_relative_to(root):
        raise ValueError("signal_outputs_must_be_outside_waveform_dataset")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers_must_be_one_to_eight")
    for name in ("epochs.sqlite", "epochs.sqlite-journal", "epochs.sqlite-wal", "epochs.sqlite-shm",
                 "signal_plan.json", "signal_summary.json", "patient_split.json", "signal_execution.json"):
        if (work / name).is_symlink() or (work / (name + ".tmp")).is_symlink():
            raise ValueError("runtime_output_must_not_be_symlink")
    if any((source_work / name).is_symlink() for name in ("waveform_metadata.jsonl", "summary.json")):
        raise ValueError("runtime_metadata_cache_must_not_be_symlink")
    with Path(quality_path).open("rb") as stream:
        config = QualityConfig(**tomllib.load(stream))
    stays, _ = frozen.load_stay_outcomes(source_work / "stay_outcomes.jsonl")
    splits, split_stats = frozen.ensure_patient_split(stays, work / "patient_split.json")
    records, _ = frozen.load_waveform_index(root, {s.subject_id for s in stays}, source_work, progress=progress)
    by_subject = defaultdict(list)
    for record in records:
        by_subject[record.subject_id].append(record)
    by_icu = {}
    for stay in stays:
        selected = [r for r in by_subject[stay.subject_id]
                    if frozen.overlap_with_icu(r, stay.subject_id, stay.intime, stay.followup_end)]
        if selected:
            by_icu[stay.icustay_id] = selected
    hourly, audit = frozen.plan_epochs(stays, splits, by_icu)
    all_epochs = {icu: hourly.get(icu, set()) | audit.get(icu, set()) for icu in set(hourly) | set(audit)}
    # Records not selected for an ICU are no longer needed after planning.
    del by_subject, records
    fingerprint = cache_fingerprint(source_work, config)
    work.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(work / "epochs.sqlite")
    execution_path = work / "signal_execution.json"
    execution = None
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS epochs (icustay_id INTEGER, endtime TEXT, accepted INTEGER, result TEXT, PRIMARY KEY(icustay_id,endtime))")
        saved = connection.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
        if saved and json.loads(saved[0]) != fingerprint:
            raise ValueError("Signal cache configuration/source changed; use a new work directory and retain the patient split")
        if not saved and connection.execute("SELECT COUNT(*) FROM epochs").fetchone()[0]:
            raise ValueError("nonempty_signal_cache_missing_frozen_fingerprint")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('fingerprint',?)", (json.dumps(fingerprint, sort_keys=True),))
        connection.commit()
        done = defaultdict(set)
        for icu, end in connection.execute("SELECT icustay_id,endtime FROM epochs"):
            value = datetime.fromisoformat(end)
            if value not in all_epochs.get(icu, set()):
                raise ValueError("cached_epoch_not_in_frozen_plan")
            done[icu].add(value)
        total = sum(len(ends) for ends in all_epochs.values())
        before = sum(map(len, done.values()))
        remaining = total - before
        runtime_fingerprint = execution_fingerprint()
        runtime_saved = connection.execute("SELECT value FROM metadata WHERE key='bounded_spawn_fingerprint'").fetchone()
        if runtime_saved and json.loads(runtime_saved[0]) != runtime_fingerprint:
            raise ValueError("runtime_execution_code_changed_use_new_cache_or_explicit_reviewed_migration")
        migration_row = connection.execute("SELECT value FROM metadata WHERE key='bounded_spawn_migration'").fetchone()
        if runtime_saved and not migration_row:
            raise ValueError("runtime_migration_provenance_missing")
        migration = json.loads(migration_row[0]) if migration_row else {
            "legacy_cached_epochs_at_first_spawn": before,
            "frozen_algorithm_fingerprint_sha256": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest(),
            "policy": "Reuse frozen plan/worker/summary and identical source/configuration; no algorithm migration"}
        if migration["frozen_algorithm_fingerprint_sha256"] != hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest():
            raise ValueError("runtime_migration_source_fingerprint_mismatch")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('bounded_spawn_fingerprint',?)", (json.dumps(runtime_fingerprint, sort_keys=True),))
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('bounded_spawn_migration',?)", (json.dumps(migration, sort_keys=True),))
        connection.commit()
        plan = {"hourly_epochs": sum(map(len, hourly.values())), "event_audit_epochs": sum(map(len, audit.values())),
                "unique_epochs": total, "stays_with_planned_epochs": len(all_epochs)}
        atomic_json(work / "signal_plan.json", {"plan": plan, "split": split_stats, "fingerprint": fingerprint})
        previous_hash = frozen._hash_file(execution_path) if execution_path.exists() else None
        prior_runs = []
        if execution_path.exists():
            prior = json.loads(execution_path.read_text())
            if (any(prior.get(key) != value for key, value in runtime_fingerprint.items())
                    or prior.get("migration") != migration or prior.get("cache_after", before + 1) > before):
                raise ValueError("prior_runtime_execution_manifest_mismatch")
            prior_runs = prior.pop("prior_runs", []) + [prior]
        execution = {**runtime_fingerprint, "start_method": "spawn", "workers": workers,
                     "batch_epochs_max": BATCH_SIZE, "max_inflight_tasks": 2 * workers,
                     "frozen_code_sha256": fingerprint["code"], "previous_execution_manifest_sha256": previous_hash,
                     "migration": migration, "prior_runs": prior_runs,
                     "cache_before": before, "cache_after": before, "planned_epochs": total,
                     "started_utc": datetime.now(timezone.utc).isoformat(), "state": "running"}
        atomic_json(execution_path, execution)
        if progress:
            progress(f"Bounded spawn: planned {total:,}; cached {before:,}; pending {remaining:,}; workers {workers}")
        started, processed = time.monotonic(), 0
        if remaining:
            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                     initializer=runtime_worker_init, initargs=(asdict(config),)) as pool:
                tasks = pending_tasks(all_epochs, done, by_icu)
                for rows in bounded_results(pool, tasks, 2 * workers):
                    connection.executemany("INSERT INTO epochs VALUES (?,?,?,?)", [
                        (r["icu"], r["end"], int(r["accepted"]), json.dumps(r, allow_nan=False)) for r in rows])
                    connection.commit()
                    previous = processed
                    processed += len(rows)
                    if progress and (processed // 1000 > previous // 1000 or processed == remaining):
                        progress(f"ECG screened: {processed:,}/{remaining:,}; elapsed {time.monotonic() - started:.0f}s")
                    del rows
        after = connection.execute("SELECT COUNT(*) FROM epochs").fetchone()[0]
        if after != total:
            raise ValueError("runtime_cache_incomplete_after_workers")
        summary = frozen.summarize_screen(connection, stays, splits, hourly, audit)
        if summary.get("screened_epochs", 0) != total:
            raise ValueError("runtime_summary_count_mismatch")
        execution.update(cache_after=after, state="complete", finished_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(execution_path, execution)
        summary.update(plan=plan, config=asdict(config), versions=fingerprint["versions"], split=split_stats,
                       scope="Engineering-screened RR features; rhythm and normal beats not adjudicated; no model performance",
                       elapsed_seconds=round(time.monotonic() - started, 2), complete=True,
                       execution=execution, execution_manifest_sha256=frozen._hash_file(execution_path))
        atomic_json(work / "signal_summary.json", summary)
        return summary
    except BaseException as error:
        if execution is not None:
            connection.rollback()
            execution.update(state="failed", error_type=type(error).__name__,
                             cache_after=connection.execute("SELECT COUNT(*) FROM epochs").fetchone()[0],
                             finished_utc=datetime.now(timezone.utc).isoformat())
            atomic_json(execution_path, execution)
        raise
    finally:
        connection.close()


def acquire_signal_lock(path, inherited_fd=None):
    """Duplicate an inherited mutex's open description, or acquire a new one."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("signal_lock_must_not_be_symlink")
    if inherited_fd is not None:
        if isinstance(inherited_fd, bool) or not isinstance(inherited_fd, int) or inherited_fd <= 2:
            raise ValueError("invalid_inherited_lock_descriptor")
        inherited_info = os.fstat(inherited_fd)
        path_info = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(inherited_info.st_mode) or not stat.S_ISREG(path_info.st_mode)
                or (inherited_info.st_dev, inherited_info.st_ino) != (path_info.st_dev, path_info.st_ino)):
            raise ValueError("inherited_lock_descriptor_does_not_match_job_lock")
        descriptor = os.dup(inherited_fd)
    else:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("signal_lock_must_be_regular_file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def main(argv=None):
    import argparse
    import sys
    from .cli import load_paths, write_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", type=Path, required=True)
    parser.add_argument("--feasibility-dir", type=Path, default=Path("artifacts/feasibility"))
    parser.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    parser.add_argument("--quality", type=Path, default=Path("configs/signal_quality.toml"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inherited-lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        roots = load_paths(args.paths)
        outputs = [args.work_dir, args.feasibility_dir, args.work_dir / "signal_job.lock"]
        if args.output:
            outputs.append(args.output)
        if any(path.is_symlink() or path.resolve().is_relative_to(root) for path in outputs for root in roots):
            raise ValueError("runtime_outputs_must_be_outside_source_datasets_and_not_symlinks")
        stop = args.work_dir / "signal_stop_request.json"
        if stop.exists() or stop.is_symlink():
            raise ValueError("existing_stop_request_refuses_start")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        descriptor = acquire_signal_lock(args.work_dir / "signal_job.lock", args.inherited_lock_fd)
        try:
            if stop.exists() or stop.is_symlink():
                raise ValueError("existing_stop_request_refuses_start")
            result = run_signal_screen_spawn(roots[1], args.feasibility_dir, args.work_dir, args.quality,
                                             args.workers, lambda message: print(message, file=sys.stderr, flush=True))
            if args.output:
                write_report(result, args.output, roots)
            else:
                print(json.dumps(result, indent=2))
        finally:
            os.close(descriptor)
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print("Spawn signal runtime failed: " + type(error).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
