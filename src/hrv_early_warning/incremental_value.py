"""Exploratory development-only controls for ECG rate, availability and RR variation.

Frozen v1 features are reused without changing the signal algorithm or split.
No heldout features are queried. Patient outputs stay in ignored artifacts.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
from statistics import mean
import warnings

from .modeling import BASE_FEATURES, RR_FEATURES, _dataset_stats, alarm_metrics
from .replay import _readonly, _validate_metadata

VERSION = "rr_incremental_controls_v1"
ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "artifacts" / "rr_controls"
MASKS = tuple(f"rr_available_lag{i}" for i in range(6)) + ("rr_valid_count", "rr_slope_available")
RATE = tuple(f"mean_rr_ms_lag{i}" for i in range(6)) + tuple(
    f"ecg_rate_from_mean_rr_bpm_lag{i}" for i in range(6)) + (
    "mean_rr_ms_slope_per_hour", "ecg_rate_from_mean_rr_bpm_slope_per_hour")
VARIABILITY = tuple(f"{name}_lag{i}" for i in range(6) for name in ("sdrr_ms", "rmssd_rr_ms"))
VARIANTS = {
    "routine": BASE_FEATURES,
    "availability": BASE_FEATURES + MASKS,
    "ecg_rate_history": BASE_FEATURES + MASKS + RATE,
    "rr_variability_history": BASE_FEATURES + MASKS + RATE + VARIABILITY,
    "rr_variability_trend": BASE_FEATURES + MASKS + RATE + VARIABILITY + (
        "sdrr_ms_slope_per_hour", "rmssd_rr_ms_slope_per_hour"),
}
CONTRASTS = (
    ("routine", "availability"),
    ("availability", "ecg_rate_history"),
    ("ecg_rate_history", "rr_variability_history"),
    ("rr_variability_history", "rr_variability_trend"),
)
SPEC = {
    "version": VERSION,
    "status": "exploratory_followup_specified_after_v1_results_before_new_control_fits",
    "population": "identical_current_rr_accepted_train_validation_hourly_rows",
    "variants": {name: list(features) for name, features in VARIANTS.items()},
    "contrasts": [list(pair) for pair in CONTRASTS],
    "primary_contrast": ["ecg_rate_history", "rr_variability_history"],
    "estimator": {"C": 1.0, "class_weight": None, "solver": "lbfgs", "max_iter": 1000},
    "preprocessing": {"base": "training_median_with_missing_indicators",
                      "rr": "training_median_without_duplicate_indicators; shared_explicit_lag_and_slope_availability_masks",
                      "scaling": "training_only_StandardScaler"},
    "seed": 20261101, "patient_bootstrap_replicates": 200,
    "threshold": "training_negative_99th_percentile_linear_then_six_hour_refractory",
    "alarm_comparison": "report_achieved_burdens; not_an_equal_false_alarm_burden_claim",
    "test_features": "forbidden",
}


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finite(value, *, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("control_feature_not_finite_numeric_or_null")
    return float(value)


def slope(values):
    points = [(-lag, value) for lag, value in enumerate(values) if value is not None]
    if len(points) < 3:
        return None
    xbar, ybar = mean(p[0] for p in points), mean(p[1] for p in points)
    return sum((x - xbar) * (y - ybar) for x, y in points) / sum((x - xbar) ** 2 for x, _ in points)


def controlled_features(raw):
    """Only the six fixed past samples; missingness is explicit, never imputed here."""
    result = {name: finite(raw.get(name)) for name in BASE_FEATURES}
    rates = []
    valid_count = 0
    for lag in range(6):
        values = {name: finite(raw.get(f"{name}_lag{lag}")) for name in RR_FEATURES}
        present = [value is not None for value in values.values()]
        if any(present) != all(present):
            raise ValueError("partial_rr_epoch_features")
        available = all(present)
        if lag == 0 and not available:
            raise ValueError("controls_require_current_accepted_rr")
        if available and (values["mean_rr_ms"] <= 0 or any(v < 0 for v in values.values())):
            raise ValueError("invalid_rr_epoch_feature_range")
        result.update({f"{name}_lag{lag}": value for name, value in values.items()})
        result[f"rr_available_lag{lag}"] = int(available)
        valid_count += available
        rate = 60000 / values["mean_rr_ms"] if available else None
        rates.append(finite(rate))
        result[f"ecg_rate_from_mean_rr_bpm_lag{lag}"] = rates[-1]
    if isinstance(raw.get("rr_valid_count"), bool) or raw.get("rr_valid_count") != valid_count:
        raise ValueError("rr_history_count_disagrees_with_samples")
    result["rr_valid_count"] = valid_count
    result["rr_slope_available"] = int(valid_count >= 3)
    # Recompute slopes only from the already frozen six samples, no new ECG.
    for name in RR_FEATURES:
        result[f"{name}_slope_per_hour"] = slope([result[f"{name}_lag{i}"] for i in range(6)])
    result["ecg_rate_from_mean_rr_bpm_slope_per_hour"] = slope(rates)
    return result


def safe_output(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("controls_output_symlink_forbidden")
    if not path.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("controls_outputs_must_remain_in_ignored_artifacts_rr_controls")
    if path.exists() and any(path.iterdir()):
        raise FileExistsError("controls_output_exists_choose_new_directory")
    return path


def write_private(path, value):
    data = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def iter_control_rows(connection, split):
    if split not in ("train", "validation"):
        raise ValueError("controls_forbid_test_split")
    # Validate split metadata before selecting or parsing ANY feature JSON.
    if connection.execute("SELECT count(*) FROM model_rows WHERE split IS NULL OR split NOT IN ('train','validation')").fetchone()[0]:
        raise ValueError("controls_refuse_table_containing_test_or_unknown_split")
    query = ("SELECT subject_id,icustay_id,landmark,label,event_time,monitoring_seconds,features "
             "FROM model_rows WHERE split=? AND current_rr_accepted=1 ORDER BY icustay_id,landmark")
    for subject, icu, landmark, label, event, seconds, raw in connection.execute(query, (split,)):
        if label not in ("positive", "negative", "censored"):
            raise ValueError("unknown_control_label")
        point = datetime.fromisoformat(landmark)
        event = datetime.fromisoformat(event) if event else None
        seconds = finite(seconds, nullable=False)
        if not 0 < seconds <= 3600:
            raise ValueError("invalid_control_monitoring_duration")
        yield ({"subject_id": subject, "icustay_id": icu, "landmark": point,
                "label": label, "event_time": event, "monitoring_seconds": seconds},
               controlled_features(json.loads(raw)))


def preflight(work, feasibility, spec_path):
    work, feasibility, spec_path = map(Path, (work, feasibility, spec_path))
    if json.loads(spec_path.read_text()) != SPEC:
        raise ValueError("control_specification_changed")
    with closing(_readonly(work / "model_table.sqlite")) as connection:
        connection.execute("BEGIN")
        source, reference_report, _ = _validate_metadata(connection, work, feasibility)
        table_hash = hash_file(work / "model_table.sqlite")
        stats = {split: _dataset_stats(connection, split, True) for split in ("train", "validation")}
        checked = {}
        for split in ("train", "validation"):
            checked[split] = sum(1 for _ in iter_control_rows(connection, split))
            if checked[split] != stats[split]["rows"]:
                raise ValueError("control_row_count_mismatch")
        if hash_file(work / "model_table.sqlite") != table_hash:
            raise ValueError("source_model_table_changed_during_preflight")
    return {"status": "preflight_complete_no_models_fitted", "specification": SPEC,
            "provenance": {"spec_sha256": hash_file(spec_path), "code_sha256": hash_file(__file__),
                           "model_table_sha256": table_hash,
                           "patient_split_sha256": hash_file(work / "patient_split.json"),
                           "source_fingerprint_sha256": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()},
            "same_paired_population": stats, "feature_rows_checked": checked,
            "reference_routine_metrics": {key: reference_report["models"]["paired_vitals"]["validation"][key]
                                          for key in ("AUROC", "AUPRC", "Brier")},
            "reference_routine_threshold": reference_report["models"]["paired_vitals"]["validation_alarms"]["threshold"],
            "dependencies": {name: importlib.util.find_spec(name) is not None
                             for name in ("numpy", "scipy", "sklearn", "joblib", "threadpoolctl")},
            "heldout_features_read": False}


def shared_intervals(y, predictions, subjects):
    import numpy as np
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    y, subjects = np.asarray(y), np.asarray(subjects)
    unique, inverse = np.unique(subjects, return_inverse=True)
    if not len(unique) or set(np.unique(y)) != {0, 1} or set(predictions) != set(VARIANTS):
        raise ValueError("paired_control_metrics_require_all_models_and_both_classes")
    def metrics(p, weights=None):
        return {"AUPRC": float(average_precision_score(y, p, sample_weight=weights)),
                "AUROC": float(roc_auc_score(y, p, sample_weight=weights)),
                "Brier": float(brier_score_loss(y, p, sample_weight=weights))}
    def difference(a, b):
        return {key: a[key] - b[key] if key == "Brier" else b[key] - a[key] for key in a}
    points = {name: metrics(p) for name, p in predictions.items()}
    samples = {f"{a}__to__{b}": {metric: [] for metric in points[a]} for a, b in CONTRASTS}
    rng = np.random.default_rng(SPEC["seed"])
    valid = 0
    for _ in range(SPEC["patient_bootstrap_replicates"]):
        multiplicities = np.bincount(rng.integers(0, len(unique), len(unique)), minlength=len(unique))
        weights = multiplicities[inverse]
        if not weights[y == 0].sum() or not weights[y == 1].sum():
            continue
        valid += 1
        current = {name: metrics(p, weights) for name, p in predictions.items()}
        for a, b in CONTRASTS:
            for key, value in difference(current[a], current[b]).items():
                samples[f"{a}__to__{b}"][key].append(value)
    return {"absolute_metrics": points, "replicates_requested": SPEC["patient_bootstrap_replicates"],
            "replicates_valid": valid, "replicates_skipped_single_class": SPEC["patient_bootstrap_replicates"] - valid,
            "method": "shared_patient_percentile_bootstrap_fixed_predictions_no_refit_unadjusted_multiple_comparisons",
            "comparisons": {f"{a}__to__{b}": {"point_improvement": difference(points[a], points[b]),
                "interval_95": {key: np.percentile(values, [2.5, 97.5]).tolist() if values else None
                                for key, values in samples[f"{a}__to__{b}"].items()}} for a, b in CONTRASTS}}


def run_controls(work, feasibility, spec_path, output_path, *, fit=False):
    output = safe_output(output_path)
    plan = preflight(work, feasibility, spec_path)
    if fit and not all(plan["dependencies"].values()):
        raise RuntimeError("missing_scientific_dependencies_restore_local_environment_no_patient_upload")
    output.mkdir(parents=True, mode=0o700)
    write_private(output / "plan.json", plan)  # Persist before any model fit.
    if not fit:
        return plan
    import joblib
    import numpy as np
    import sklearn
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from threadpoolctl import threadpool_limits
    with closing(_readonly(Path(work) / "model_table.sqlite")) as connection:
        connection.execute("BEGIN")
        source, _, _ = _validate_metadata(connection, Path(work), Path(feasibility))
        source_hash = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        if (hash_file(Path(work) / "model_table.sqlite") != plan["provenance"]["model_table_sha256"]
                or source_hash != plan["provenance"]["source_fingerprint_sha256"]):
            raise ValueError("source_changed_before_control_feature_query")
        train = [(row, features) for row, features in iter_control_rows(connection, "train") if row["label"] != "censored"]
        validation = list(iter_control_rows(connection, "validation"))
        full = _dataset_stats(connection, "validation")
    train_y = np.asarray([int(row["label"] == "positive") for row, _ in train])
    valid_y = np.asarray([-1 if row["label"] == "censored" else int(row["label"] == "positive") for row, _ in validation])
    if set(np.unique(train_y)) != {0, 1} or set(np.unique(valid_y[valid_y >= 0])) != {0, 1}:
        raise ValueError("control_training_and_validation_require_both_classes")
    results, scores = {}, {}
    for name, names in VARIANTS.items():
        def matrix(records):
            return np.asarray([[np.nan if features[key] is None else features[key] for key in names] for _, features in records], dtype=float)
        train_x, valid_x = matrix(train), matrix(validation)
        base_imputer = SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)
        imputer = base_imputer if name == "routine" else ColumnTransformer([
            ("base", base_imputer, list(range(len(BASE_FEATURES)))),
            ("rr", SimpleImputer(strategy="median", add_indicator=False, keep_empty_features=True),
             list(range(len(BASE_FEATURES), len(names)))),
        ])
        pipeline = Pipeline([
            ("imputer", imputer),
            ("scaler", StandardScaler()),
            ("logistic", LogisticRegression(**SPEC["estimator"], random_state=SPEC["seed"])),
        ])
        with warnings.catch_warnings(record=True) as captured, threadpool_limits(limits=1):
            warnings.simplefilter("always", ConvergenceWarning)
            pipeline.fit(train_x, train_y)
            train_scores = pipeline.predict_proba(train_x)[:, 1]
            probability = pipeline.predict_proba(valid_x)[:, 1]
        threshold = float(np.percentile(train_scores[train_y == 0], 99, method="linear"))
        scores[name] = probability[valid_y >= 0]
        if name == "routine":
            y = valid_y[valid_y >= 0]
            baseline_metrics = {"AUROC": float(roc_auc_score(y, scores[name])),
                                "AUPRC": float(average_precision_score(y, scores[name])),
                                "Brier": float(brier_score_loss(y, scores[name]))}
            if any(not math.isclose(value, baseline_metrics[key], rel_tol=1e-8, abs_tol=1e-10)
                   for key, value in plan["reference_routine_metrics"].items()):
                raise ValueError("frozen_routine_baseline_not_reproduced")
            if not math.isclose(plan["reference_routine_threshold"], threshold, rel_tol=1e-8, abs_tol=1e-10):
                raise ValueError("frozen_routine_threshold_not_reproduced")
        results[name] = {"features": list(names), "convergence_warning": any(isinstance(w.message, ConvergenceWarning) for w in captured),
                         "alarms": alarm_metrics([row for row, _ in validation], probability, threshold,
                                                 full_riskset_events=full["distinct_events_with_positive_window"])}
        bundle_path = output / (name + ".joblib")
        joblib.dump({"pipeline": pipeline, "features": names, "threshold": threshold,
                     "version": VERSION, "provenance": plan["provenance"]}, bundle_path)
        bundle_path.chmod(0o600)
    if hash_file(Path(work) / "model_table.sqlite") != plan["provenance"]["model_table_sha256"]:
        raise ValueError("source_model_table_changed_during_controls")
    paired_metrics = shared_intervals(valid_y[valid_y >= 0], scores,
                  [row["subject_id"] for row, _ in validation if row["label"] != "censored"])
    for key, value in plan.get("reference_routine_metrics", {}).items():
        if not math.isclose(value, paired_metrics["absolute_metrics"]["routine"][key], rel_tol=1e-8, abs_tol=1e-10):
            raise ValueError("frozen_routine_baseline_not_reproduced")
    if not math.isclose(plan["reference_routine_threshold"], results["routine"]["alarms"]["threshold"], rel_tol=1e-8, abs_tol=1e-10):
        raise ValueError("frozen_routine_threshold_not_reproduced")
    report = {"status": "exploratory_control_comparison_complete", "version": VERSION,
              "stage": "development_validation_only_no_test_features_or_scores",
              "specification": SPEC, "provenance": plan["provenance"],
              "versions": {"numpy": np.__version__, "sklearn": sklearn.__version__, "joblib": joblib.__version__},
              "same_paired_population": plan["same_paired_population"], "full_validation_riskset": full,
              "models": results, "paired_metrics": paired_metrics,
              "frozen_routine_reference_reproduced": True,
              "limitations": ["Frozen v1 RR are not validated sinus NN or accurate physiological HRV.",
                              "This follow-up was designed after observing v1 development results; no confirmatory or independent clinical claim.",
                              "All models share availability masks from the availability comparator onward; missingness can still be informative.",
                              "Rate controls include mean RR, its reciprocal-rate representation and their six-sample histories; neither equals mean instantaneous HR.",
                              "Routine controls are latest available vital signs, not a full vital-history/trend model.",
                              "Slopes are derived representations of the same history; any gain is not independent physiological information.",
                              "Train quantiles do not guarantee equal achieved validation false-alarm burdens; alarm comparisons are descriptive.",
                              "The locked M2 split is unchanged; prior legacy MI reuse overlap is documented separately."]}
    write_private(output / "comparison.json", report)
    return report
