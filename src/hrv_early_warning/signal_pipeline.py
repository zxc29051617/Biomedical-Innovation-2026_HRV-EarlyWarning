"""Resumable real-ECG screening; patient-level artifacts remain local.

Hourly sampled epochs support the complete risk set. Additional consecutive
epochs audit positive-event history coverage only, never define training cases.
"""

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import time
import tomllib

from .feasibility import positive_landmarks
from .risksets import ensure_patient_split, iter_risk_windows, load_stay_outcomes
from .waveform_index import load_waveform_index
from .waveforms import coverage_seconds, overlap_with_icu


_RECORDS = {}
_QUALITY = None


def _worker_init(records, config):
    global _RECORDS, _QUALITY
    from .signal_quality import QualityConfig
    _RECORDS = records
    _QUALITY = QualityConfig(**config)
    # Avoid each worker spawning a BLAS thread pool.
    from threadpoolctl import threadpool_limits
    threadpool_limits(limits=1)


def _worker(task):
    from .ecg import decode_ecg_epoch
    from .signal_quality import assess_ecg_epoch
    icu, ends = task
    results = []
    for end in ends:
        decoded = decode_ecg_epoch(_RECORDS[icu], end - timedelta(minutes=5), end)
        row = {"icu": icu, "end": end.isoformat(), "accepted": False,
               "decode_status": decoded.status, "decode_errors": list(decoded.errors),
               "coverage_fraction": decoded.coverage_fraction, "reasons": [], "features": {}, "metrics": {}}
        if decoded.fs is not None and decoded.status in {"ok", "partial"}:
            assessment = assess_ecg_epoch(decoded.signal, decoded.fs, _QUALITY)
            row.update(accepted=assessment.accepted, reasons=list(assessment.reasons),
                       features=assessment.features, metrics=assessment.metrics)
        else:
            row["reasons"] = ["ecg_decode_unavailable"]
        results.append(row)
    return results


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan_epochs(stays, splits, by_icu):
    """Plan without selecting negative windows by later outcome or quality."""
    hourly, event_audit = defaultdict(set), defaultdict(set)
    windows_by_icu = defaultdict(list)
    for window in iter_risk_windows(stays, splits):
        if window.icustay_id in by_icu:
            windows_by_icu[window.icustay_id].append(window)
    for stay in stays:
        records = by_icu.get(stay.icustay_id)
        if not records or not windows_by_icu.get(stay.icustay_id):
            continue
        spans = [(r.start, r.end) for r in records]
        # Include the preceding five hours needed by the first model landmark.
        last = windows_by_icu[stay.icustay_id][-1].landmark
        point = stay.intime + timedelta(hours=1)
        while point <= last:
            if coverage_seconds(spans, point - timedelta(minutes=5), point) > 0:
                hourly[stay.icustay_id].add(point)
            point += timedelta(hours=1)
        if stay.event_time is not None:
            landmarks = positive_landmarks(stay.intime, stay.event_time, 6, 6)
            if landmarks:
                point = landmarks[0] - timedelta(hours=6) + timedelta(minutes=5)
                while point <= landmarks[-1]:
                    if coverage_seconds(spans, point - timedelta(minutes=5), point) > 0:
                        event_audit[stay.icustay_id].add(point)
                    point += timedelta(minutes=5)
    return dict(hourly), dict(event_audit)


def run_signal_screen(waveform_root, feasibility_dir, work_dir, quality_path, workers=8, progress=None):
    from .signal_quality import QualityConfig, ALGORITHM_VERSION
    import numpy, scipy, wfdb

    root, source_work, work = map(lambda p: Path(p).resolve(), (waveform_root, feasibility_dir, work_dir))
    if work.is_relative_to(root):
        raise ValueError("signal outputs must be outside waveform dataset")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 to 8")
    with Path(quality_path).open("rb") as stream:
        config = QualityConfig(**tomllib.load(stream))
    stays, _ = load_stay_outcomes(source_work / "stay_outcomes.jsonl")
    splits, split_stats = ensure_patient_split(stays, work / "patient_split.json")
    records, _ = load_waveform_index(root, {s.subject_id for s in stays}, source_work, progress=progress)
    by_subject = defaultdict(list)
    for record in records:
        by_subject[record.subject_id].append(record)
    by_icu = {}
    for stay in stays:
        selected = [r for r in by_subject[stay.subject_id] if overlap_with_icu(r, stay.subject_id, stay.intime, stay.followup_end)]
        if selected:
            by_icu[stay.icustay_id] = selected
    hourly, audit = plan_epochs(stays, splits, by_icu)
    all_epochs = {icu: hourly.get(icu, set()) | audit.get(icu, set()) for icu in set(hourly) | set(audit)}
    code = Path(__file__).parent
    fingerprint = {"schema": 1, "algorithm": ALGORITHM_VERSION, "config": asdict(config),
                   "versions": {"numpy": numpy.__version__, "scipy": scipy.__version__, "wfdb": wfdb.__version__},
                   "code": {name: _hash_file(code / name) for name in ("ecg.py", "signal_quality.py", "signal_pipeline.py")},
                   "stay_outcomes_sha256": _hash_file(source_work / "stay_outcomes.jsonl"),
                   "waveform_metadata_sha256": _hash_file(source_work / "waveform_metadata.jsonl"),
                   "source_validation": "metadata cache fingerprint; not waveform byte checksums"}
    work.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(work / "epochs.sqlite")
    connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE IF NOT EXISTS epochs (icustay_id INTEGER, endtime TEXT, accepted INTEGER, result TEXT, PRIMARY KEY(icustay_id,endtime))")
    saved = connection.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
    if saved and json.loads(saved[0]) != fingerprint:
        connection.close()
        raise ValueError("Signal cache configuration/source changed; use a new work directory and retain the patient split")
    connection.execute("INSERT OR IGNORE INTO metadata VALUES ('fingerprint',?)", (json.dumps(fingerprint, sort_keys=True),))
    connection.commit()
    done = defaultdict(set)
    for icu, end in connection.execute("SELECT icustay_id,endtime FROM epochs"):
        done[icu].add(datetime.fromisoformat(end))
    tasks = []
    for icu, ends in sorted(all_epochs.items()):
        remaining = sorted(ends - done[icu])
        for index in range(0, len(remaining), 24):
            tasks.append((icu, remaining[index:index + 24]))
    total = sum(len(ends) for ends in all_epochs.values())
    remaining = sum(len(ends) for _, ends in tasks)
    if progress:
        progress(f"Planned unique 5-minute epochs: {total:,}; cached {total - remaining:,}; pending {remaining:,}")
    plan = {"hourly_epochs": sum(map(len, hourly.values())), "event_audit_epochs": sum(map(len, audit.values())),
            "unique_epochs": total, "stays_with_planned_epochs": len(all_epochs)}
    (work / "signal_plan.json").write_text(json.dumps({"plan": plan, "split": split_stats, "fingerprint": fingerprint}, indent=2) + "\n")
    started = time.monotonic()
    processed = 0
    try:
        if tasks:
            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("fork"),
                                     initializer=_worker_init, initargs=(by_icu, asdict(config))) as pool:
                futures = [pool.submit(_worker, task) for task in tasks]
                for future in as_completed(futures):
                    rows = future.result()
                    connection.executemany("INSERT INTO epochs VALUES (?,?,?,?)", [
                        (r["icu"], r["end"], int(r["accepted"]), json.dumps(r, allow_nan=False)) for r in rows])
                    connection.commit()
                    previous = processed
                    processed += len(rows)
                    if progress and (processed // 1000 > previous // 1000 or processed == remaining):
                        progress(f"ECG screened: {processed:,}/{remaining:,}; elapsed {time.monotonic() - started:.0f}s")
        summary = summarize_screen(connection, stays, splits, hourly, audit)
        summary.update(plan=plan, config=asdict(config), versions=fingerprint["versions"], split=split_stats,
                       scope="Engineering-screened RR features; rhythm and normal beats not adjudicated; no model performance",
                       elapsed_seconds=round(time.monotonic() - started, 2), complete=True)
        (work / "signal_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        connection.close()


def summarize_screen(connection, stays, splits, hourly, audit):
    accepted = defaultdict(set)
    counts, reasons, decode_reasons = Counter(), Counter(), Counter()
    for icu, end, good, raw in connection.execute("SELECT icustay_id,endtime,accepted,result FROM epochs"):
        result = json.loads(raw)
        counts["screened_epochs"] += 1
        counts["accepted_epochs"] += int(good)
        reasons.update(result["reasons"])
        decode_reasons.update(result["decode_errors"])
        if good:
            accepted[icu].add(datetime.fromisoformat(end))
    event_counts = Counter()
    event_split = defaultdict(Counter)
    for stay in stays:
        if stay.event_time is None:
            continue
        event_counts["qualifying_events"] += 1
        landmarks = positive_landmarks(stay.intime, stay.event_time, 6, 6)
        if not landmarks:
            event_counts["before_first_landmark"] += 1
            continue
        valid = accepted[stay.icustay_id] & audit.get(stay.icustay_id, set())
        best = max((sum(t - timedelta(hours=6) < end <= t for end in valid) for t in landmarks), default=0)
        eligible_now = any(t in accepted[stay.icustay_id] for t in landmarks)
        for key, condition in (("any_accepted_history_epoch", best > 0), ("at_least_1h_accepted_epochs", best >= 12),
                               ("at_least_3h_accepted_epochs", best >= 36), ("full_6h_accepted_epochs", best >= 72),
                               ("any_current_hourly_epoch_accepted", eligible_now)):
            event_counts[key] += int(condition)
            event_split[splits[stay.subject_id]][key] += int(condition)
    coverage = defaultdict(Counter)
    for window in iter_risk_windows(stays, splits):
        counter = coverage[window.split]
        counter["all_" + window.label] += 1
        if window.landmark in accepted[window.icustay_id]:
            counter["current_epoch_accepted_" + window.label] += 1
    return {**dict(counts), "rejection_reason_counts": dict(reasons), "decode_error_counts": dict(decode_reasons),
            "events": dict(event_counts), "event_coverage_by_split": {k: dict(v) for k, v in event_split.items()},
            "riskset_coverage_by_split": {k: dict(v) for k, v in coverage.items()},
            "hourly_sampled_epochs_accepted": sum(len(ends & accepted[icu]) for icu, ends in hourly.items())}
