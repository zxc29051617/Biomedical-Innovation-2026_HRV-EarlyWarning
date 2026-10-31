"""Fixed development-only models; held-out test features are never queried.

Patient-level tables and serialized estimators must remain in ignored local
directories. ECG record audits contain extra positive-case epochs: features here
use only six exact ICU-anchored hourly endpoints, never every epoch in a range.
"""

from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from statistics import mean, median
import time
import warnings

from .risksets import ensure_patient_split, iter_risk_windows, load_stay_outcomes
from .vitals import FEATURES as VITAL_FEATURES, features_at_landmark, load_stay_vitals


MODEL_VERSION = "development_fixed_logistic_c1_v1"
RR_FEATURES = ("mean_rr_ms", "sdrr_ms", "rmssd_rr_ms")
BASE_FEATURES = ("age_years", "age_obscured", *VITAL_FEATURES)
CURRENT_FEATURES = tuple(f"{name}_lag0" for name in RR_FEATURES)
HISTORY_FEATURES = tuple(f"{name}_lag{lag}" for lag in range(6) for name in RR_FEATURES) + ("rr_valid_count",)
TREND_FEATURES = tuple(f"{name}_slope_per_hour" for name in RR_FEATURES)
VARIANTS = {
    "vitals_all": BASE_FEATURES,
    "paired_vitals": BASE_FEATURES,
    "paired_current_rr": BASE_FEATURES + CURRENT_FEATURES,
    "paired_history_rr": BASE_FEATURES + HISTORY_FEATURES,
    "paired_history_trend_rr": BASE_FEATURES + HISTORY_FEATURES + TREND_FEATURES,
}


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _signal_readiness(feasibility, work):
    """Read completion metadata/aggregate row count, never partial RR features."""
    summary_path, database = work / "signal_summary.json", work / "epochs.sqlite"
    if not summary_path.exists():
        return False, {"status": "pending_signal_completion"}
    try:
        summary = json.loads(summary_path.read_text())
        if summary.get("complete") is not True:
            return False, {"status": "pending_signal_completion"}
        with closing(_readonly(database)) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
            fingerprint = json.loads(row[0]) if row else None
            count = connection.execute("SELECT COUNT(*) FROM epochs").fetchone()[0]
        if (not fingerprint or count != summary["screened_epochs"] or count != summary["plan"]["unique_epochs"]
                or summary["config"] != fingerprint["config"] or summary["versions"] != fingerprint["versions"]):
            return False, {"status": "signal_completion_metadata_mismatch"}
        if fingerprint["stay_outcomes_sha256"] != _hash_file(feasibility / "stay_outcomes.jsonl"):
            return False, {"status": "signal_cohort_fingerprint_mismatch"}
        if fingerprint.get("waveform_metadata_sha256") != _hash_file(feasibility / "waveform_metadata.jsonl"):
            return False, {"status": "signal_waveform_fingerprint_mismatch"}
        return True, {"status": "complete_verified", "screened_epochs": count,
                      "fingerprint": fingerprint, "summary_sha256": _hash_file(summary_path)}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False, {"status": "signal_completion_metadata_unreadable"}


def load_stay_epochs(connection, icustay_id):
    """Load only one development stay; accepted feature values must be valid."""
    result = {}
    for end, accepted, raw in connection.execute(
            "SELECT endtime,accepted,result FROM epochs WHERE icustay_id=? ORDER BY endtime", (icustay_id,)):
        timestamp = datetime.fromisoformat(end)
        if timestamp in result:
            raise ValueError("duplicate_epoch_endpoint")
        item = json.loads(raw)
        if item.get("icu") != icustay_id or datetime.fromisoformat(item["end"]) != timestamp:
            raise ValueError("epoch_identity_or_clock_mismatch")
        if accepted not in (0, 1) or bool(accepted) != item.get("accepted"):
            raise ValueError("epoch_acceptance_mismatch")
        if not accepted:
            result[timestamp] = None
            continue
        features = item.get("features", {})
        values = {}
        for name in RR_FEATURES:
            value = features.get(name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 or (name == "mean_rr_ms" and value == 0)):
                raise ValueError("accepted_epoch_has_invalid_features")
            values[name] = float(value)
        result[timestamp] = values
    return result


def rr_features_at_landmark(epochs, landmark):
    """Six exact 5-minute samples ending t,t-1h,...,t-5h; no case-audit rows.

    Slopes use elapsed hours as the x-axis and require >=3 observed samples.
    Missing samples are never forward/backward filled or replaced by a nearby
    five-minute audit epoch. A slope is an alternate representation of history,
    not independently observed physiological information.
    """
    selected = [epochs.get(landmark - timedelta(hours=lag)) for lag in range(6)]
    values = {f"{name}_lag{lag}": sample[name] if sample else None
              for lag, sample in enumerate(selected) for name in RR_FEATURES}
    values["rr_valid_count"] = sum(sample is not None for sample in selected)
    for name in RR_FEATURES:
        points = [(-lag, sample[name]) for lag, sample in enumerate(selected) if sample is not None]
        slope = None
        if len(points) >= 3:
            xbar, ybar = mean(x for x, _ in points), mean(y for _, y in points)
            slope = sum((x - xbar) * (y - ybar) for x, y in points) / sum((x - xbar) ** 2 for x, _ in points)
        values[f"{name}_slope_per_hour"] = slope
    return selected[0] is not None, values


def build_model_table(feasibility_dir, work_dir, output_path, progress=None):
    """Build train/validation hourly rows, including censored alarm opportunities.

    Read no test patient's vital or epoch values. One stay's observations are
    loaded at a time, and CHARTTIME candidates are bounded to the past six hours
    before applying the vital module's CHARTTIME<t and STORETIME<=t rules.
    Partial ECG runs produce a vitals-only table; paired models remain pending.
    Rebuild into a distinct artifact after verified ECG completion to retain the
    baseline run. No output is written to the source clinical/waveform mounts.
    """
    feasibility, work, output = map(lambda path: Path(path).resolve(), (feasibility_dir, work_dir, output_path))
    vital_path = work / "vitals.sqlite"
    vital_summary = work / "vitals_summary.json"
    if not vital_path.exists() or not vital_summary.exists():
        raise ValueError("completed_vitals_cache_required")
    if output in {vital_path, work / "epochs.sqlite", work / "patient_split.json"}:
        raise ValueError("model_table_must_not_replace_source_cache")
    records, loading = load_stay_outcomes(feasibility / "stay_outcomes.jsonl")
    splits, split_stats = ensure_patient_split(records, work / "patient_split.json")
    saved_vitals = json.loads(vital_summary.read_text())
    if saved_vitals.get("bytes") != vital_path.stat().st_size:
        raise ValueError("vitals_cache_size_mismatch")
    signature = sorted((r.subject_id, r.hadm_id, r.icustay_id, r.intime.isoformat(), r.followup_end.isoformat()) for r in records)
    cohort_hash = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    if saved_vitals.get("fingerprint", {}).get("cohort_sha256") != cohort_hash:
        raise ValueError("vitals_cohort_fingerprint_mismatch")
    rr_ready, signal = _signal_readiness(feasibility, work)
    source = {
        "model_version": MODEL_VERSION, "stay_outcomes_sha256": _hash_file(feasibility / "stay_outcomes.jsonl"),
        "patient_split_sha256": _hash_file(work / "patient_split.json"),
        "vitals_summary_sha256": _hash_file(vital_summary),
        "vitals_bytes": vital_path.stat().st_size, "vitals_mtime_ns": vital_path.stat().st_mtime_ns,
        "rr_ready": rr_ready, "signal": signal,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    destination = sqlite3.connect(temporary)
    vitals = _readonly(vital_path)
    epochs = _readonly(work / "epochs.sqlite") if rr_ready else None
    counts = Counter()
    by_split = defaultdict(Counter)
    started = time.monotonic()
    source_by_icu = {record.icustay_id: record for record in records}
    current_icu = None
    observations, charts, epoch_values, pending = [], [], {}, []
    try:
        destination.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        destination.execute("""CREATE TABLE model_rows (
            subject_id INTEGER, hadm_id INTEGER, icustay_id INTEGER, landmark TEXT,
            history_start TEXT, horizon_end TEXT, event_time TEXT, followup_end TEXT,
            first_exposure_time TEXT, label TEXT, split TEXT, monitoring_seconds REAL,
            current_rr_accepted INTEGER, features TEXT, PRIMARY KEY(icustay_id,landmark))""")
        for window in iter_risk_windows(records, splits):
            counts["full_riskset_windows"] += 1
            if window.split == "test":
                counts["heldout_windows_skipped_before_feature_query"] += 1
                continue
            if window.split not in {"train", "validation"}:
                raise ValueError("unexpected_development_split")
            stay = source_by_icu[window.icustay_id]
            if current_icu != window.icustay_id:
                current_icu = window.icustay_id
                observations = load_stay_vitals(vitals, current_icu)
                charts = [observation[1] for observation in observations]
                epoch_values = load_stay_epochs(epochs, current_icu) if epochs else {}
                counts["development_stays_queried"] += 1
            lo, hi = bisect_left(charts, window.history_start), bisect_left(charts, window.landmark)
            features = features_at_landmark(observations[lo:hi], window.landmark)
            features.update(age_years=stay.age_years, age_obscured=int(stay.age_obscured))
            accepted, rr = rr_features_at_landmark(epoch_values, window.landmark)
            features.update(rr)
            monitoring_end = min(window.landmark + timedelta(hours=1), stay.followup_end)
            if stay.first_exposure_time is not None:
                monitoring_end = min(monitoring_end, stay.first_exposure_time)
            seconds = (monitoring_end - window.landmark).total_seconds()
            if not 0 < seconds <= 3600:
                raise ValueError("invalid_monitored_interval")
            pending.append((window.subject_id, window.hadm_id, window.icustay_id, window.landmark.isoformat(),
                            window.history_start.isoformat(), window.horizon_end.isoformat(),
                            window.event_time.isoformat() if window.event_time else None, stay.followup_end.isoformat(),
                            stay.first_exposure_time.isoformat() if stay.first_exposure_time else None,
                            window.label, window.split, seconds, int(accepted), json.dumps(features, allow_nan=False)))
            counts["development_rows"] += 1
            by_split[window.split][window.label] += 1
            by_split[window.split]["paired_" + window.label] += int(accepted)
            if len(pending) >= 5000:
                destination.executemany("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pending)
                destination.commit()
                pending.clear()
                if progress and counts["development_rows"] % 50000 == 0:
                    progress(f"Development model rows: {counts['development_rows']:,}; test features remain unread")
        if pending:
            destination.executemany("INSERT INTO model_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pending)
        if (vital_path.stat().st_size != source["vitals_bytes"] or vital_path.stat().st_mtime_ns != source["vitals_mtime_ns"]):
            raise ValueError("vitals_source_changed_during_model_table_build")
        destination.execute("CREATE INDEX model_split ON model_rows(split,current_rr_accepted,label)")
        report = {
            **dict(counts), "by_split": {key: dict(value) for key, value in by_split.items()},
            "source_loading": loading, "split": split_stats, "rr_ready": rr_ready,
            "signal_status": signal["status"], "model_version": MODEL_VERSION,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "scope": "train_validation_only_including_censored_no_test_feature_queries",
            "sampling": "six_exact_hourly_5minute_epochs_not_continuous_six_hour_HRV",
        }
        destination.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("source_fingerprint", json.dumps(source, sort_keys=True)), ("report", json.dumps(report, sort_keys=True))])
        destination.commit()
    finally:
        destination.close()
        vitals.close()
        if epochs:
            epochs.close()
    temporary.replace(output)
    _json_write(output.with_suffix(".summary.json"), report)
    return output, report


def alarm_metrics(rows, probabilities, threshold, *, full_riskset_events=None):
    """Six-hour refractory alarms on validation scored windows only.

    Row metadata contains icustay_id, landmark, label, event_time and actual
    monitoring_seconds. Censored alerts are unclassified, never false alarms.
    Denominators sum observed scored time (<=1h each), not full ICU length or
    the six-hour prediction horizon. Also report evaluable-only time because
    censoring limits interpretation of the all-observed-time false-alert rate.
    """
    if len(rows) != len(probabilities) or not math.isfinite(threshold):
        raise ValueError("invalid_alarm_inputs")
    entries = sorted(zip(rows, probabilities), key=lambda pair: (pair[0]["icustay_id"], pair[0]["landmark"]))
    last_alarm = {}
    seen = set()
    catches = {}
    eligible_events = set()
    counts = Counter({"notifications": 0, "positive_notifications": 0, "false_notifications": 0,
                      "censored_notifications": 0, "refractory_suppressed_crossings": 0})
    observed_seconds = evaluable_seconds = 0.0
    for row, score in entries:
        icu, point, label = row["icustay_id"], row["landmark"], row["label"]
        if (icu, point) in seen or label not in {"positive", "negative", "censored"}:
            raise ValueError("invalid_or_duplicate_alarm_window")
        seen.add((icu, point))
        seconds = row["monitoring_seconds"]
        if not math.isfinite(seconds) or not 0 < seconds <= 3600 or not math.isfinite(float(score)):
            raise ValueError("invalid_alarm_monitoring_or_score")
        observed_seconds += seconds
        if label != "censored":
            evaluable_seconds += seconds
        if label == "positive":
            event = row["event_time"]
            if event is None or not point < event <= point + timedelta(hours=6):
                raise ValueError("inconsistent_positive_alarm_window")
            eligible_events.add((icu, event))
        if score < threshold:
            continue
        if icu in last_alarm and point - last_alarm[icu] < timedelta(hours=6):
            counts["refractory_suppressed_crossings"] += 1
            continue
        last_alarm[icu] = point
        counts["notifications"] += 1
        if label == "negative":
            counts["false_notifications"] += 1
        elif label == "censored":
            counts["censored_notifications"] += 1
        else:
            counts["positive_notifications"] += 1
            key = (icu, row["event_time"])
            catches.setdefault(key, (row["event_time"] - point).total_seconds() / 3600)
    full_count = len(eligible_events) if full_riskset_events is None else full_riskset_events
    if full_count < len(eligible_events):
        raise ValueError("full_event_denominator_smaller_than_scored_events")
    leads = list(catches.values())
    hours, evaluable_hours = observed_seconds / 3600, evaluable_seconds / 3600
    return {
        **dict(counts), "threshold": float(threshold), "refractory_hours": 6,
        "observed_scored_patient_hours": hours, "evaluable_scored_patient_hours": evaluable_hours,
        "false_notifications_per_100_observed_patient_hours": 100 * counts["false_notifications"] / hours if hours else None,
        "false_notifications_per_100_evaluable_patient_hours": 100 * counts["false_notifications"] / evaluable_hours if evaluable_hours else None,
        "scored_events": len(eligible_events), "full_riskset_events": full_count, "events_caught": len(catches),
        "event_sensitivity_among_scored_events": len(catches) / len(eligible_events) if eligible_events else None,
        "event_sensitivity_full_riskset": len(catches) / full_count if full_count else None,
        "lead_time_hours_mean": mean(leads) if leads else None,
        "lead_time_hours_median": median(leads) if leads else None,
    }


def _dataset_stats(connection, split, paired=False):
    mask = " AND current_rr_accepted=1" if paired else ""
    counts = dict(connection.execute("SELECT label,COUNT(*) FROM model_rows WHERE split=?" + mask + " GROUP BY label", (split,)))
    patients = connection.execute("SELECT COUNT(DISTINCT subject_id) FROM model_rows WHERE split=?" + mask, (split,)).fetchone()[0]
    binary_patients = connection.execute("SELECT COUNT(DISTINCT subject_id) FROM model_rows WHERE split=? AND label IN ('positive','negative')" + mask, (split,)).fetchone()[0]
    events = connection.execute("SELECT COUNT(DISTINCT icustay_id) FROM model_rows WHERE split=? AND label='positive'" + mask, (split,)).fetchone()[0]
    hours = (connection.execute("SELECT SUM(monitoring_seconds) FROM model_rows WHERE split=?" + mask, (split,)).fetchone()[0] or 0) / 3600
    return {"rows": sum(counts.values()), "binary_rows": counts.get("positive", 0) + counts.get("negative", 0),
            "label_counts": {label: counts.get(label, 0) for label in ("positive", "negative", "censored")},
            "distinct_patients": patients, "distinct_binary_patients": binary_patients,
            "distinct_events_with_positive_window": events,
            "observed_patient_hours": hours}


def _load_matrix(connection, split, names, paired, binary_only):
    import numpy as np
    where = "split=?" + (" AND current_rr_accepted=1" if paired else "")
    if binary_only:
        where += " AND label IN ('positive','negative')"
    values, labels, rows = [], [], []
    for subject, icu, landmark, label, event, seconds, raw in connection.execute(
            "SELECT subject_id,icustay_id,landmark,label,event_time,monitoring_seconds,features FROM model_rows WHERE "
            + where + " ORDER BY icustay_id,landmark", (split,)):
        features = json.loads(raw)
        vector = []
        for name in names:
            value = features.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("nonfinite_or_nonnumeric_model_feature")
            vector.append(np.nan if value is None else float(value))
        values.append(vector)
        labels.append(1 if label == "positive" else 0 if label == "negative" else -1)
        if not binary_only:
            rows.append({"subject_id": subject, "icustay_id": icu, "landmark": datetime.fromisoformat(landmark),
                         "label": label, "event_time": datetime.fromisoformat(event) if event else None,
                         "monitoring_seconds": seconds})
    return np.asarray(values, dtype=float).reshape((-1, len(names))), np.asarray(labels, dtype=int), rows


def patient_bootstrap_metrics(labels, probabilities, subject_ids, *, replicates=100, seed=20261101):
    """Resample whole validation subjects, keeping all their correlated windows.

    Multiplicity weights equal duplicating every window of each sampled patient.
    Predictions are held fixed; no estimator is refitted. Single-class replicates
    are skipped for all three metrics and their number is disclosed.
    """
    import numpy as np
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    if not isinstance(replicates, int) or isinstance(replicates, bool) or replicates < 1:
        raise ValueError("bootstrap_replicates_must_be_positive_integer")
    y, p, subjects = np.asarray(labels), np.asarray(probabilities), np.asarray(subject_ids)
    if len(y) != len(p) or len(y) != len(subjects):
        raise ValueError("bootstrap_input_length_mismatch")
    unique, inverse = np.unique(subjects, return_inverse=True)
    rng = np.random.default_rng(seed)
    values = {name: [] for name in ("AUROC", "AUPRC", "Brier")}
    for _ in range(replicates):
        if not len(unique):
            break
        multiplicity = np.bincount(rng.integers(0, len(unique), size=len(unique)), minlength=len(unique))
        weights = multiplicity[inverse]
        if not np.any(weights[y == 1]) or not np.any(weights[y == 0]):
            continue
        values["AUROC"].append(float(roc_auc_score(y, p, sample_weight=weights)))
        values["AUPRC"].append(float(average_precision_score(y, p, sample_weight=weights)))
        values["Brier"].append(float(brier_score_loss(y, p, sample_weight=weights)))
    valid = len(values["AUROC"])
    return {
        "method": "patient_cluster_percentile_bootstrap_fixed_predictions_no_refit",
        "seed": seed, "replicates_requested": replicates, "replicates_valid": valid,
        "replicates_skipped_single_class_or_empty": replicates - valid, "confidence_level": 0.95,
        "distinct_validation_patients": len(unique),
        "intervals": {name: [float(value) for value in np.percentile(samples, [2.5, 97.5])]
                      if samples else None for name, samples in values.items()},
    }


def train_models(model_table_path, output_dir, progress=None, *, bootstrap_replicates=100):
    """Fit the five fixed comparisons on train; report development validation.

    All paired variants use the identical current-epoch-accepted row mask. Fit
    imputation, missingness indicators and scaling on binary training rows only.
    Thresholds are the 99th percentile of each model's training-negative scores;
    this is a fixed heuristic, NOT proof of equal validation false-alarm burden.
    No hyperparameter search, winner selection, or held-out test evaluation.
    """
    import joblib
    import numpy as np
    import sklearn
    from sklearn.calibration import calibration_curve
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from threadpoolctl import threadpool_limits

    path, output = Path(model_table_path).resolve(), Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    with closing(_readonly(path)) as connection:
        if connection.execute("SELECT COUNT(*) FROM model_rows WHERE split NOT IN ('train','validation') OR split IS NULL").fetchone()[0]:
            raise ValueError("model_table_must_not_contain_test_or_unknown_splits")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        source = json.loads(metadata["source_fingerprint"])
        table_report = json.loads(metadata["report"])
        full = {split: _dataset_stats(connection, split) for split in ("train", "validation")}
        for variant, names in VARIANTS.items():
            paired = variant != "vitals_all"
            stats = {split: _dataset_stats(connection, split, paired) for split in ("train", "validation")}
            result = {"features": list(names), "data": stats,
                      "coverage_against_full_riskset": {split: stats[split]["rows"] / full[split]["rows"] if full[split]["rows"] else None
                                                       for split in ("train", "validation")},
                      "observed_time_coverage_against_full_riskset": {
                          split: stats[split]["observed_patient_hours"] / full[split]["observed_patient_hours"]
                          if full[split]["observed_patient_hours"] else None for split in ("train", "validation")}}
            results[variant] = result
            if paired and not table_report["rr_ready"]:
                result.update(status="pending_complete_signal_screen")
                continue
            if any(stats["train"]["label_counts"][label] == 0 for label in ("positive", "negative")):
                result.update(status="not_fit_training_requires_both_classes")
                continue
            if not stats["validation"]["rows"]:
                result.update(status="not_fit_no_validation_rows")
                continue
            if progress:
                progress(f"Fitting fixed development model: {variant}; training rows {stats['train']['binary_rows']:,}")
            train_x, train_y, _ = _load_matrix(connection, "train", names, paired, True)
            valid_x, valid_y, valid_rows = _load_matrix(connection, "validation", names, paired, False)
            pipeline = Pipeline([
                ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                ("scaler", StandardScaler()),
                ("logistic", LogisticRegression(C=1.0, class_weight=None, solver="lbfgs", max_iter=1000, random_state=20261101)),
            ])
            with warnings.catch_warnings(record=True) as caught, threadpool_limits(limits=1):
                warnings.simplefilter("always", ConvergenceWarning)
                pipeline.fit(train_x, train_y)
                training_scores = pipeline.predict_proba(train_x)[:, 1]
                valid_scores = pipeline.predict_proba(valid_x)[:, 1]
            threshold = float(np.percentile(training_scores[train_y == 0], 99, method="linear"))
            usable = valid_y >= 0
            y, probability = valid_y[usable], valid_scores[usable]
            two_classes = len(np.unique(y)) == 2
            train_prevalence = float(train_y.mean())
            calibration = None
            if two_classes:
                observed, predicted = calibration_curve(y, probability, n_bins=10, strategy="quantile")
                calibration = {"strategy": "quantile", "requested_bins": 10, "actual_bins": len(observed),
                               "mean_predicted": [float(value) for value in predicted],
                               "observed_fraction": [float(value) for value in observed]}
            metrics = {
                "binary_windows": int(len(y)), "prevalence": float(y.mean()) if len(y) else None,
                "AUROC": float(roc_auc_score(y, probability)) if two_classes else None,
                "AUPRC": float(average_precision_score(y, probability)) if two_classes else None,
                "Brier": float(brier_score_loss(y, probability)) if len(y) else None,
                "constant_train_prevalence": train_prevalence,
                "constant_train_prevalence_Brier_on_validation": float(brier_score_loss(y, np.full(len(y), train_prevalence))) if len(y) else None,
                "calibration_curve": calibration,
                "discrimination_unavailable_reason": None if two_classes else "validation_requires_both_classes",
            }
            alarms = alarm_metrics(valid_rows, valid_scores, threshold,
                                   full_riskset_events=full["validation"]["distinct_events_with_positive_window"])
            alarms["full_riskset_observed_patient_hours"] = full["validation"]["observed_patient_hours"]
            alarms["observed_time_coverage_against_full_riskset"] = result["observed_time_coverage_against_full_riskset"]["validation"]
            confidence = patient_bootstrap_metrics(y, probability,
                [row["subject_id"] for row in valid_rows if row["label"] != "censored"],
                replicates=bootstrap_replicates)
            result.update(status="development_validation_complete", validation=metrics, validation_alarms=alarms,
                          validation_patient_bootstrap=confidence,
                          convergence_warning=any(isinstance(w.message, ConvergenceWarning) for w in caught),
                          train_negative_score_quantile=0.99,
                          train_negative_crossing_fraction=float(np.mean(training_scores[train_y == 0] >= threshold)))
            temporary = output / (variant + ".joblib.tmp")
            joblib.dump({"pipeline": pipeline, "features": tuple(names), "threshold": threshold,
                         "variant": variant, "model_version": MODEL_VERSION, "source_fingerprint": source}, temporary)
            temporary.replace(output / (variant + ".joblib"))
    report = {
        "stage": "development_validation_only_test_unopened", "model_version": MODEL_VERSION,
        "versions": {"numpy": np.__version__, "sklearn": sklearn.__version__, "joblib": joblib.__version__},
        "source_fingerprint": source, "model_table": table_report, "full_development_riskset": full,
        "estimator": {"type": "LogisticRegression", "C": 1, "class_weight": None, "solver": "lbfgs", "max_iter": 1000},
        "preprocessing": "train-binary-only median imputation, missing indicators, keep-empty columns, StandardScaler",
        "threshold_method": "Each model's training-negative scores 99th percentile (linear); fixed before validation alarms; >= threshold; 6h ICU-specific refractory",
        "limitations": [
            "Training quantiles do not establish equal false-alarm rates across validation models; ties and refractory suppression change notification burden.",
            "Censored rows are not fitted or scored as binary negatives; their alarms remain unclassified and are reported separately.",
            "Alert denominators count only each scored window's observed next hour, shortened at exposure/follow-up; unscored quality-rejected hours are outside the paired denominator.",
            "Event sensitivity is reported against both scored events and all development-validation riskset events; early events before the first six-hour landmark are outside this riskset.",
            "Candidate RR features are not rhythm-adjudicated NN-based HRV; six hourly five-minute samples are not continuous six-hour HRV.",
            "History slopes re-express observed history; a comparison gain does not imply an independent physiological signal.",
            "Patient bootstrap intervals use fixed validation predictions and do not include training uncertainty; few positive-event patients can make intervals unstable.",
            "No test evaluation, external validation, clinical efficacy claim or independent calibration study is provided here.",
        ],
        "models": results,
    }
    _json_write(output / "development_report.json", report)
    return report
