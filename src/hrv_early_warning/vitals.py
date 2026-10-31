"""Stream routine vital signs and enforce observation AND availability cutoffs.

Item IDs and broad plausibility ranges follow the official MIMIC concept:
https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/concepts/pivot/pivoted_vital.sql
The six-hour freshness rule, exact units and tie handling are project choices.
"""

from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time


ITEMS = {
    "220045": ("heart_rate", "bpm", 300),
    "220052": ("map", "mmhg", 300),
    "220181": ("map", "mmhg", 300),
    "225312": ("map", "mmhg", 300),
    "220210": ("respiratory_rate", "insp/min", 70),
    "224690": ("respiratory_rate", "insp/min", 70),
    "220277": ("spo2", "%", 100),
}
FEATURES = ("heart_rate", "map", "respiratory_rate", "spo2")


def extract_vitals(clinical_root, stays, work_dir, progress=None, chunksize=500_000):
    """Read all CHARTEVENTS in bounded chunks; store only aligned vital rows.

    Requires pandas only at execution. No raw values or patient IDs are returned
    in the report. A SQLite cache retains both clocks and original row IDs.
    """
    import pandas as pd

    root, work = Path(clinical_root).resolve(), Path(work_dir).resolve()
    if work.is_relative_to(root):
        raise ValueError("vital outputs must be outside the source dataset")
    stays = list(stays)
    source = root / "CHARTEVENTS.csv"
    stat = source.stat()
    signature = sorted((s.subject_id, s.hadm_id, s.icustay_id, s.intime.isoformat(), s.followup_end.isoformat()) for s in stays)
    fingerprint = {"schema": 1, "source": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                   "cohort_sha256": hashlib.sha256(json.dumps(signature).encode()).hexdigest(), "items": ITEMS}
    fingerprint = json.loads(json.dumps(fingerprint))
    work.mkdir(parents=True, exist_ok=True)
    destination, metadata = work / "vitals.sqlite", work / "vitals_summary.json"
    if destination.exists() and metadata.exists():
        saved = json.loads(metadata.read_text())
        if saved.get("fingerprint") == fingerprint and saved.get("bytes") == destination.stat().st_size:
            return destination, {**saved["report"], "cache_reused": True}
    temporary = work / "vitals.sqlite.tmp"
    temporary.unlink(missing_ok=True)
    cohort = pd.DataFrame(signature, columns=["expected_subject", "expected_hadm", "ICUSTAY_ID", "intime", "endtime"])
    for name in ("expected_subject", "expected_hadm", "ICUSTAY_ID"):
        cohort[name] = cohort[name].astype(str)
    # CSV source clocks use a space; normalize both sides to the same ISO form.
    needed = ["ROW_ID", "SUBJECT_ID", "HADM_ID", "ICUSTAY_ID", "ITEMID", "CHARTTIME", "STORETIME", "VALUENUM", "VALUEUOM", "ERROR"]
    counts = Counter()
    units = Counter()
    started = time.monotonic()
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("CREATE TABLE vitals (icustay_id INTEGER, feature TEXT, charttime TEXT, storetime TEXT, value REAL, row_id INTEGER)")
        for chunk in pd.read_csv(source, usecols=needed, dtype=str, keep_default_na=False, chunksize=chunksize):
            counts["rows_scanned"] += len(chunk)
            selected = chunk[chunk.ITEMID.isin(ITEMS)].copy()
            counts["selected_item_rows"] += len(selected)
            selected = selected.merge(cohort, on="ICUSTAY_ID", how="inner", validate="many_to_one")
            counts["target_icu_rows"] += len(selected)
            if not selected.empty:
                good = (selected.SUBJECT_ID == selected.expected_subject) & (selected.HADM_ID == selected.expected_hadm)
                counts["identity_mismatch_rows"] += int((~good).sum())
                selected = selected[good].copy()
                good = selected.ERROR.isin(("", "0", "0.0"))
                counts["error_or_unknown_flag_rows"] += int((~good).sum())
                selected = selected[good].copy()
                selected["value"] = pd.to_numeric(selected.VALUENUM, errors="coerce")
                selected["feature"] = selected.ITEMID.map({key: spec[0] for key, spec in ITEMS.items()})
                expected_units = selected.ITEMID.map({key: spec[1] for key, spec in ITEMS.items()})
                observed_units = selected.VALUEUOM.str.strip().str.lower()
                units.update(observed_units)
                good = observed_units == expected_units
                counts["unrecognized_unit_rows"] += int((~good).sum())
                selected = selected[good].copy()
                upper = selected.ITEMID.map({key: spec[2] for key, spec in ITEMS.items()})
                good = (selected.value > 0) & ((selected.value < upper) | ((selected.feature == "spo2") & (selected.value == 100)))
                counts["invalid_value_rows"] += int((~good).sum())
                selected = selected[good].copy()
                chart = pd.to_datetime(selected.CHARTTIME, format="%Y-%m-%d %H:%M:%S", errors="coerce")
                stored = pd.to_datetime(selected.STORETIME, format="%Y-%m-%d %H:%M:%S", errors="coerce")
                good = chart.notna() & stored.notna()
                counts["missing_or_invalid_clock_rows"] += int((~good).sum())
                selected["charttime"] = chart.dt.strftime("%Y-%m-%dT%H:%M:%S")
                selected["storetime"] = stored.dt.strftime("%Y-%m-%dT%H:%M:%S")
                selected = selected[good].copy()
                good = (selected.charttime >= selected.intime) & (selected.charttime < selected.endtime)
                counts["outside_followup_rows"] += int((~good).sum())
                selected = selected[good].copy()
                selected["icustay_id"] = selected.ICUSTAY_ID.astype(int)
                selected["row_id"] = selected.ROW_ID.astype(int)
                selected[["icustay_id", "feature", "charttime", "storetime", "value", "row_id"]].to_sql("vitals", connection, if_exists="append", index=False, chunksize=10_000)
                counts["retained_rows"] += len(selected)
            if progress and counts["rows_scanned"] % 5_000_000 < chunksize:
                progress(f"Vitals scan: {counts['rows_scanned']:,} rows; retained {counts['retained_rows']:,}")
        after = source.stat()
        if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError("CHARTEVENTS changed during extraction")
        connection.execute("CREATE INDEX vital_stay_time ON vitals(icustay_id, charttime)")
        connection.commit()
        counts["stays_with_vitals"] = connection.execute("SELECT COUNT(DISTINCT icustay_id) FROM vitals").fetchone()[0]
    finally:
        connection.close()
    temporary.replace(destination)
    report = {**dict(counts), "unit_counts": dict(units), "elapsed_seconds": round(time.monotonic() - started, 2),
              "scope": "aligned observations; prediction features must still enforce both clocks", "cache_reused": False}
    metadata.write_text(json.dumps({"fingerprint": fingerprint, "bytes": destination.stat().st_size, "report": report}, indent=2) + "\n")
    return destination, report


def load_stay_vitals(connection, icustay_id):
    """Return a private stay list; sorting does not make future values available."""
    return [(feature, datetime.fromisoformat(chart), datetime.fromisoformat(stored), float(value), int(row))
            for feature, chart, stored, value, row in connection.execute(
                "SELECT feature,charttime,storetime,value,row_id FROM vitals WHERE icustay_id=? ORDER BY charttime,storetime,row_id", (icustay_id,))]


def features_at_landmark(observations, landmark, lookback_hours=6):
    """Most recent known value; equal-chart-time observations are averaged.

    Both CHARTTIME < t and STORETIME <= t are required. At a common measurement
    clock, the mean of the available observations avoids inventing a modality
    preference. Missingness is preserved for train-only model preprocessing.
    """
    if not math.isfinite(lookback_hours) or lookback_hours <= 0:
        raise ValueError("lookback must be positive")
    start = landmark - timedelta(hours=lookback_hours)
    candidates = {name: [] for name in FEATURES}
    for name, chart, stored, value, row_id in observations:
        if name in candidates and start <= chart < landmark and stored <= landmark and math.isfinite(value):
            candidates[name].append((chart, value))
    result = {}
    for name, rows in candidates.items():
        if not rows:
            result[name] = None
            continue
        latest = max(chart for chart, _ in rows)
        values = [value for chart, value in rows if chart == latest]
        result[name] = sum(values) / len(values)
    return result
