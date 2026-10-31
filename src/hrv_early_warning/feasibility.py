"""Reproducible cohort/event and waveform-metadata feasibility, not ML evaluation.

All patient-level outputs are written to an explicitly supplied private work
directory. The returned report contains only aggregates. Lead II file presence
does not establish ECG quality, rhythm eligibility, or computable HRV.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
import csv
import json
import math
from pathlib import Path
import time
import tomllib
from typing import Callable

from .vasopressors import ALL_FIVE_ITEMIDS, derive_first_use
from .waveforms import coverage_seconds, inspect_ecg_intervals, overlap_with_icu


Progress = Callable[[str], None] | None
CACHE_SCHEMA = 2
CACHE_FIELDS = (
    "ROW_ID", "SUBJECT_ID", "HADM_ID", "ICUSTAY_ID", "STARTTIME", "ENDTIME",
    "STORETIME", "ITEMID", "AMOUNT", "AMOUNTUOM", "RATE", "RATEUOM", "ORDERID",
    "LINKORDERID", "ORDERCATEGORYNAME", "SECONDARYORDERCATEGORYNAME",
    "ORDERCATEGORYDESCRIPTION", "ORDERCOMPONENTTYPEDESCRIPTION", "CANCELREASON",
    "STATUSDESCRIPTION", "COMMENTS_CANCELEDBY",
)


def _json_default(value):
    if isinstance(value, (datetime, Path)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Unsupported JSON type: {type(value).__name__}")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n")
    temporary.replace(path)


def ensure_infusion_cache(clinical_root: Path, work_dir: Path, progress: Progress = None) -> tuple[Path, dict]:
    """Scan INPUTEVENTS_MV once; cache only rows for the five protocol drugs.

    Reuse requires matching schema, source path/size/mtime, selected item IDs,
    completion marker, and cache size. This is provenance, not a checksum of
    every source byte. Replaced/corrupt caches must be rebuilt explicitly.
    """
    source = (clinical_root / "INPUTEVENTS_MV.csv").resolve()
    stat = source.stat()
    fingerprint = {"source_path": str(source), "source_size_bytes": stat.st_size, "source_mtime_ns": stat.st_mtime_ns}
    work_dir.mkdir(parents=True, exist_ok=True)
    cache = work_dir / "vasopressor_rows.jsonl"
    metadata = work_dir / "vasopressor_scan_summary.json"
    if cache.exists() and metadata.exists():
        saved = json.loads(metadata.read_text())
        if (saved.get("cache_schema_version") == CACHE_SCHEMA and saved.get("complete") is True
                and all(saved.get(k) == v for k, v in fingerprint.items())
                and saved.get("selected_itemids") == sorted(ALL_FIVE_ITEMIDS)
                and saved.get("cache_bytes") == cache.stat().st_size):
            summary = {k: v for k, v in saved.items() if k != "source_path"}
            summary["cache_reused"] = True
            return cache, summary
    temporary = cache.with_suffix(".jsonl.tmp")
    count = selected = 0
    fields = {key: Counter() for key in ("ITEMID", "RATEUOM", "STATUSDESCRIPTION", "CANCELREASON")}
    started = time.monotonic()
    with source.open(newline="", encoding="utf-8-sig") as stream, temporary.open("w", encoding="utf-8") as target:
        reader = csv.DictReader(stream)
        required = {"SUBJECT_ID", "HADM_ID", "ITEMID", "STARTTIME", "ENDTIME", "RATE", "RATEUOM", "STATUSDESCRIPTION"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("INPUTEVENTS_MV has missing required columns")
        for row in reader:
            count += 1
            if progress and count % 500_000 == 0:
                progress(f"Infusion scan: {count:,} rows, {selected:,} selected")
            try:
                item = int(row.get("ITEMID", ""))
            except (TypeError, ValueError):
                raise ValueError("INPUTEVENTS_MV contains an unidentifiable drug row; cannot establish complete exposure capture") from None
            if item not in ALL_FIVE_ITEMIDS:
                continue
            selected += 1
            target.write(json.dumps({key: row.get(key, "") for key in CACHE_FIELDS}, ensure_ascii=False) + "\n")
            for key in fields:
                fields[key][row.get(key, "")] += 1
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise ValueError("Infusion source changed during extraction; partial cache was not promoted")
    temporary.replace(cache)
    summary = {
        "cache_schema_version": CACHE_SCHEMA, **fingerprint, "complete": True,
        "selected_itemids": sorted(ALL_FIVE_ITEMIDS), "rows_scanned": count, "rows_selected": selected,
        "field_counts": {key: dict(value) for key, value in fields.items()},
        "cache_bytes": cache.stat().st_size, "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    _write_json(metadata, summary)
    return cache, {**{k: v for k, v in summary.items() if k != "source_path"}, "cache_reused": False}


def positive_landmarks(intime: datetime, event_time: datetime, lookback_hours: float, horizon_hours: float,
                       step_hours: float = 1) -> list[datetime]:
    """Regular ICU-anchored landmarks before an event within the future horizon.

    This helper selects positive windows for a *feasibility audit only*, not a
    case-control training design. A later training pipeline must include the
    full risk set with naturally occurring negatives and censored windows.
    """
    values = (lookback_hours, horizon_hours, step_hours)
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in values):
        raise TypeError("durations must be numbers, not booleans")
    if any(not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError("lookback, horizon and step must be positive finite durations")
    if any(timedelta(hours=x) <= timedelta(0) for x in values):
        raise ValueError("durations must be at least one clock tick")
    first = intime + timedelta(hours=lookback_hours)
    earliest = max(first, event_time - timedelta(hours=horizon_hours))
    step = timedelta(hours=step_hours)
    index = max(0, math.ceil((earliest - first) / step))
    point = first + index * step
    result = []
    while point < event_time:
        result.append(point)
        point += step
    return result


def _inspect_event(stay, outcome, records, lookback: float, horizon: float, step: float) -> dict:
    landmarks = positive_landmarks(stay.intime, outcome.event_time, lookback, horizon, step)
    result = {"icustay_id": stay.icustay_id, "landmarks": len(landmarks), "best_span_seconds": 0.0,
              "best_ii_metadata_seconds": 0.0, "best_ii_with_2h_lead_seconds": 0.0,
              "segments_checked": 0, "warnings": {}, "scope": "metadata_only_not_signal_quality"}
    if not landmarks or not records:
        return result
    duration = timedelta(hours=lookback)
    spans = [(r.start, r.end) for r in records]
    result["best_span_seconds"] = max(coverage_seconds(spans, t - duration, t) for t in landmarks)
    if result["best_span_seconds"] == 0:
        return result
    intervals = []
    warnings = Counter()
    start, end = landmarks[0] - duration, landmarks[-1]
    for record in records:
        if record.end <= start or record.start >= end:
            continue
        inspection = inspect_ecg_intervals(record, start, end)
        intervals.extend(inspection.intervals)
        warnings.update(inspection.warnings)
        result["segments_checked"] += inspection.segments_checked
    result["warnings"] = dict(warnings)
    result["best_ii_metadata_seconds"] = max(coverage_seconds(intervals, t - duration, t) for t in landmarks)
    earlier = [t for t in landmarks if outcome.event_time - t >= timedelta(hours=2)]
    if earlier:
        result["best_ii_with_2h_lead_seconds"] = max(coverage_seconds(intervals, t - duration, t) for t in earlier)
    return result


def run_feasibility(clinical_root: Path, waveform_root: Path, work_dir: Path, protocol_path: Path,
                    workers: int = 8, progress: Progress = None) -> dict:
    """Audit adult MV stays, accepted first-use events, and pre-event II metadata."""
    from .cohort import align_infusions, load_adult_metavision_stays
    from .waveform_index import load_waveform_index

    clinical_root, waveform_root, work_dir = map(lambda p: Path(p).resolve(), (clinical_root, waveform_root, work_dir))
    if any(work_dir.is_relative_to(source) for source in (clinical_root, waveform_root)):
        raise ValueError("Private work directory must be outside source datasets")
    if not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    with Path(protocol_path).open("rb") as stream:
        protocol = tomllib.load(stream)
    endpoint = protocol["endpoint"]
    primary = set(endpoint["primary_itemids"].values())
    if set(endpoint["prior_exclusion_itemids"]) != set(ALL_FIVE_ITEMIDS):
        raise ValueError("Prior-exposure definition differs from the implemented five-drug protocol")
    if protocol["cohort"]["minimum_age_years"] != 18 or protocol["cohort"]["icu_dbsource"] != "metavision":
        raise ValueError("This implementation requires adult MetaVision eligibility")
    work_dir.mkdir(parents=True, exist_ok=True)
    stays, cohort_stats = load_adult_metavision_stays(clinical_root)
    cache, scan = ensure_infusion_cache(clinical_root, work_dir, progress)
    infusions, uncertain, infusion_stats = align_infusions(stays, cache)
    if infusion_stats.get("unassignable_uncertain_rows", 0):
        raise ValueError("Uncertain infusion rows cannot be attributed to stays; resolve them before assuming complete exposure capture")
    if progress:
        progress(f"Adult MetaVision stays: {len(stays):,}; preparing outcome and waveform alignment")
    outcomes = {}
    outcome_counts = Counter()
    for stay in stays:
        if uncertain.get(stay.icustay_id):
            outcome_counts["unresolved_infusion_excluded"] += 1
            continue
        outcome = derive_first_use(infusions.get(stay.icustay_id, ()), stay.intime, stay.followup_end,
                                   primary, endpoint["minimum_continuous_minutes"],
                                   maximum_gap_minutes=endpoint["maximum_gap_minutes"])
        outcomes[stay.icustay_id] = outcome
        outcome_counts[outcome.event_status] += 1
    records, index_stats = load_waveform_index(waveform_root, {s.subject_id for s in stays}, work_dir, workers, progress)
    by_subject = defaultdict(list)
    for record in records:
        by_subject[record.subject_id].append(record)
    matched = {}
    for stay in stays:
        matches = [r for r in by_subject[stay.subject_id]
                   if overlap_with_icu(r, stay.subject_id, stay.intime, stay.followup_end) is not None]
        if matches:
            matched[stay.icustay_id] = matches
    time_config = protocol["time"]
    lookback, horizon, step = time_config["lookback_hours"], time_config["primary_horizon_hours"], time_config["landmark_step_hours"]
    events = [(s, outcomes[s.icustay_id]) for s in stays if s.icustay_id in outcomes and outcomes[s.icustay_id].event_time is not None]
    tasks = [(s, o) for s, o in events if s.icustay_id in matched]
    inspection_results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = pool.map(lambda pair: _inspect_event(pair[0], pair[1], matched[pair[0].icustay_id], lookback, horizon, step), tasks)
        for number, result in enumerate(iterator, 1):
            inspection_results.append(result)
            if progress and number % 100 == 0:
                progress(f"Event-window ECG metadata inspected: {number:,}/{len(tasks):,}")
    sensitivity = {}
    for minutes in endpoint["sensitivity_continuous_minutes"]:
        counts = Counter()
        for stay in stays:
            if stay.icustay_id not in outcomes:
                continue
            counts[derive_first_use(infusions.get(stay.icustay_id, ()), stay.intime, stay.followup_end, primary, minutes).event_status] += 1
        sensitivity[f"duration_{minutes}_minutes"] = dict(counts)
    counts = Counter()
    for stay in stays:
        if stay.icustay_id in outcomes:
            counts[derive_first_use(infusions.get(stay.icustay_id, ()), stay.intime, stay.followup_end,
                                    ALL_FIVE_ITEMIDS, endpoint["minimum_continuous_minutes"]).event_status] += 1
    sensitivity["including_dopamine"] = dict(counts)
    # Private hand-off for subsequent feature extraction; never returned in the report.
    with (work_dir / "stay_outcomes.jsonl").open("w", encoding="utf-8") as target:
        for stay in stays:
            outcome = outcomes.get(stay.icustay_id)
            row = {"stay": asdict(stay), "outcome": asdict(outcome) if outcome else None,
                   "uncertain_reasons": sorted(uncertain.get(stay.icustay_id, ())),
                   "matched_waveform_records": len(matched.get(stay.icustay_id, ())) }
            target.write(json.dumps(row, default=_json_default, ensure_ascii=False) + "\n")
    with (work_dir / "event_metadata_coverage.jsonl").open("w", encoding="utf-8") as target:
        for result in inspection_results:
            target.write(json.dumps(result, ensure_ascii=False) + "\n")
    seconds = lookback * 3600
    warnings = Counter()
    for result in inspection_results:
        warnings.update(result["warnings"])
    return {
        "schema_version": "1.0", "protocol_version": protocol["study"]["version"],
        "scope": "Feasibility only; positive-event metadata inspection, no HRV/quality/model evaluation",
        "clinical_cohort": cohort_stats, "infusion_scan": scan, "infusion_alignment": infusion_stats,
        "outcome_status_counts": dict(outcome_counts), "waveform_index": index_stats,
        "same_icu_waveform_span": {"stays": len(matched), "subjects": len({s.subject_id for s in stays if s.icustay_id in matched})},
        "events": {
            "qualifying_stays": len(events), "subjects": len({s.subject_id for s, _ in events}),
            "before_first_landmark": sum(o.event_time <= s.intime + timedelta(hours=lookback) for s, o in events),
            "with_any_same_icu_record_span": len(tasks),
            "with_pre_event_landmark_and_same_icu_span": sum(r["landmarks"] > 0 for r in inspection_results),
            "with_any_lookback_span": sum(r["best_span_seconds"] > 0 for r in inspection_results),
            "with_any_ii_metadata": sum(r["best_ii_metadata_seconds"] > 0 for r in inspection_results),
            "with_at_least_1h_ii_metadata": sum(r["best_ii_metadata_seconds"] >= 3600 for r in inspection_results),
            "with_at_least_3h_ii_metadata": sum(r["best_ii_metadata_seconds"] >= 10800 for r in inspection_results),
            "with_full_lookback_ii_metadata": sum(r["best_ii_metadata_seconds"] >= seconds - 0.001 for r in inspection_results),
            "with_2h_lead_and_3h_ii_metadata": sum(r["best_ii_with_2h_lead_seconds"] >= 10800 for r in inspection_results),
            "segments_checked": sum(r["segments_checked"] for r in inspection_results),
            "metadata_warning_counts": dict(warnings),
        },
        "sensitivity_outcome_status_counts": sensitivity,
        "limitations": [
            "Event counts describe observed treatment initiation, not a diagnosis or medical necessity.",
            "Lead II coverage is header/nonempty-file metadata; samples, signal quality and rhythm eligibility are not validated.",
            "Detailed ECG inspection here is limited to qualifying-event windows; this is not a training cohort or a case-control performance estimate.",
            "A training pipeline still needs full hourly positive/negative/censored risk sets and patient-level splits.",
            "Unresolved infusion records conservatively exclude affected stays in this feasibility version.",
        ],
    }
