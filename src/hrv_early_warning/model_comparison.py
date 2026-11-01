"""Paired development-validation increments using fixed saved predictions.

No model fitting, threshold selection, partial-RR reading, or held-out test
evaluation occurs here. All candidate models share the exact same validation
rows and patient bootstrap draws. Only aggregate differences are written.
"""

from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .modeling import MODEL_VERSION, VARIANTS
from .timing import _time_key, _validate_timestamps


COMPARISON_VERSION = "paired_patient_delta_v1"
BASELINE = "paired_vitals"
CANDIDATES = tuple(name for name in VARIANTS if name.startswith("paired_") and name != BASELINE)
BOOTSTRAP_SEED = 20261101
METRICS = ("AUROC", "AUPRC", "Brier")


def _hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_replicates(replicates):
    if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 1:
        raise ValueError("replicates_must_be_positive_integer")


def _parse_clock(value):
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        raise ValueError("timestamp_requires_clock")
    return _time_key(datetime.fromisoformat(value))


def _patient_draw_weights(inverse, patient_count, rng):
    """One draw duplicates each selected patient's entire row cluster."""
    import numpy as np

    multiplicities = np.bincount(rng.integers(0, patient_count, size=patient_count), minlength=patient_count)
    return multiplicities[inverse]


def _absolute_metrics(labels, probabilities, weights=None):
    import numpy as np
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

    relevant = np.ones(len(labels), dtype=bool) if weights is None else weights > 0
    both = len(np.unique(labels[relevant])) == 2
    return {
        "AUROC": float(roc_auc_score(labels, probabilities, sample_weight=weights)) if both else None,
        "AUPRC": float(average_precision_score(labels, probabilities, sample_weight=weights)) if both else None,
        "Brier": float(brier_score_loss(labels, probabilities, sample_weight=weights)),
    }


def _improvements(baseline, candidate):
    return {name: None if baseline[name] is None or candidate[name] is None else
            (baseline[name] - candidate[name] if name == "Brier" else candidate[name] - baseline[name])
            for name in METRICS}


def _paired_bootstrap(labels, predictions, subject_ids, *, replicates=200, seed=BOOTSTRAP_SEED):
    """Internal aggregate comparison; every model uses the same patient draw.

    Predictions remain fixed. A draw lacking either outcome class is excluded
    from all metrics, including Brier, and counted explicitly. No confidence
    interval overlap test or multiplicity-adjusted inference is performed.
    """
    import numpy as np

    _validate_replicates(replicates)
    y, subjects = np.asarray(labels), np.asarray(subject_ids)
    if y.ndim != 1 or subjects.ndim != 1 or len(y) != len(subjects) or not len(y):
        raise ValueError("invalid_comparison_input_shape")
    if not np.all(np.isin(y, (0, 1))):
        raise ValueError("comparison_requires_binary_labels")
    if not np.issubdtype(subjects.dtype, np.integer) or np.any(subjects <= 0):
        raise ValueError("comparison_requires_positive_integer_patient_ids")
    if set(predictions) != {BASELINE, *CANDIDATES}:
        raise ValueError("comparison_requires_all_paired_models")
    probabilities = {}
    for name in (BASELINE, *CANDIDATES):
        values = np.asarray(predictions[name], dtype=float)
        if (values.shape != y.shape or not np.all(np.isfinite(values))
                or np.any(values < 0) or np.any(values > 1)):
            raise ValueError("invalid_comparison_probabilities")
        probabilities[name] = values
    point = {name: _absolute_metrics(y, values) for name, values in probabilities.items()}
    deltas = {name: _improvements(point[BASELINE], point[name]) for name in CANDIDATES}
    samples = {name: {metric: [] for metric in METRICS} for name in CANDIDATES}
    unique, inverse = np.unique(subjects, return_inverse=True)
    rng = np.random.default_rng(seed)
    valid = 0
    for _ in range(replicates):
        weights = _patient_draw_weights(inverse, len(unique), rng)
        if not np.any(weights[y == 1]) or not np.any(weights[y == 0]):
            continue
        current = {name: _absolute_metrics(y, values, weights) for name, values in probabilities.items()}
        for name in CANDIDATES:
            for metric, value in _improvements(current[BASELINE], current[name]).items():
                samples[name][metric].append(value)
        valid += 1
    return {
        "status": "paired_comparison_complete" if len(np.unique(y)) == 2 else "insufficient_validation_classes",
        "baseline": BASELINE,
        "metric_direction": {
            "AUROC": "candidate_minus_baseline",
            "AUPRC": "candidate_minus_baseline",
            "Brier": "baseline_minus_candidate",
            "interpretation": "positive_delta_means_improvement_for_every_metric",
        },
        "absolute_point_estimates": point,
        "comparisons": {name: {
            "delta_point_estimate": deltas[name],
            "delta_confidence_interval_95": {
                metric: [float(value) for value in np.percentile(values, (2.5, 97.5))] if values else None
                for metric, values in samples[name].items()
            },
        } for name in CANDIDATES},
        "bootstrap": {
            "method": "shared_patient_cluster_percentile_fixed_predictions_no_refit",
            "seed": seed, "confidence_level": 0.95,
            "replicates_requested": replicates, "replicates_valid": valid,
            "replicates_skipped_single_class": replicates - valid,
            "patient_count": int(len(unique)),
        },
    }


def _load_saved_models(model_dir, source):
    """Only load trusted local joblib files created by this project's trainer."""
    import joblib

    models, artifact_hashes = {}, {}
    for name in (BASELINE, *CANDIDATES):
        path = model_dir / (name + ".joblib")
        if not path.is_file():
            raise ValueError("all_completed_paired_model_artifacts_required")
        before = path.stat()
        artifact = joblib.load(path)
        if not isinstance(artifact, dict):
            raise ValueError("invalid_saved_model_artifact")
        if (artifact.get("variant") != name or artifact.get("model_version") != MODEL_VERSION
                or tuple(artifact.get("features", ())) != tuple(VARIANTS[name])):
            raise ValueError("saved_model_variant_features_or_version_mismatch")
        if artifact.get("source_fingerprint") != source:
            raise ValueError("saved_model_source_fingerprint_mismatch")
        pipeline = artifact.get("pipeline")
        if pipeline is None or not callable(getattr(pipeline, "predict_proba", None)):
            raise ValueError("saved_model_missing_probability_pipeline")
        models[name] = pipeline
        artifact_hashes[name] = _hash_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("saved_model_changed_during_comparison")
    return models, artifact_hashes


def compare_saved_models(model_table_path, model_dir, output_path, replicates=200):
    """Compare all paired saved models on identical development-validation rows.

    The model table must contain only train/validation patients, with disjoint
    patients between them and verified-complete RR metadata. Only validation,
    current-RR-accepted, binary feature rows are read. Provenance, ordered
    features and classifier class ordering are verified for every artifact.
    Binary labels must agree with the six-hour event horizon; each ICU must
    retain one subject and consistent event metadata across all selected rows.
    There is no fit/refit, imputation fitting, model selection or test access.

    The JSON contains aggregate counts, estimates, paired patient-bootstrap
    deltas and provenance hashes. Patient identifiers, per-row predictions and
    timestamps are never written. Local joblib artifacts must be trusted;
    joblib deserialization is not a sandbox for third-party model files.
    """
    import numpy as np
    import sklearn
    from threadpoolctl import threadpool_limits

    _validate_replicates(replicates)
    table, directory, output = map(lambda value: Path(value).resolve(), (model_table_path, model_dir, output_path))
    protected = {table, table.with_suffix(".summary.json"), directory / "development_report.json"}
    if output.suffix.lower() != ".json" or output in protected:
        raise ValueError("aggregate_output_must_be_a_separate_json_file")
    before = table.stat()
    with closing(sqlite3.connect(table.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("SELECT COUNT(*) FROM model_rows WHERE split NOT IN ('train','validation') OR split IS NULL").fetchone()[0]:
            raise ValueError("comparison_rejects_test_or_unknown_split_rows")
        if connection.execute("SELECT COUNT(*) FROM (SELECT subject_id FROM model_rows GROUP BY subject_id HAVING COUNT(DISTINCT split)>1)").fetchone()[0]:
            raise ValueError("patient_present_in_multiple_splits")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        try:
            source = json.loads(metadata["source_fingerprint"])
            table_report = json.loads(metadata["report"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("model_table_metadata_invalid") from None
        if (not isinstance(source, dict) or not isinstance(table_report, dict)
                or source.get("model_version") != MODEL_VERSION):
            raise ValueError("model_table_version_mismatch")
        if (table_report.get("rr_ready") is not True or source.get("rr_ready") is not True
                or source.get("signal", {}).get("status") != "complete_verified"):
            raise ValueError("comparison_requires_verified_complete_rr_table")
        models, artifact_hashes = _load_saved_models(directory, source)
        # One query/materialization defines identity, order and labels for ALL
        # models, preventing per-model dropping/reordering of missing values.
        features, labels, subjects = [], [], []
        seen, positive_stays, positive_patients = set(), set(), set()
        icu_subject, icu_event, icu_clock = {}, {}, {}
        query = ("SELECT subject_id,icustay_id,landmark,label,event_time,features FROM model_rows "
                 "WHERE split='validation' AND current_rr_accepted=1 AND label IN ('positive','negative') "
                 "ORDER BY icustay_id,landmark")
        for subject, icu, landmark, label, event, raw in connection.execute(query):
            if (isinstance(subject, bool) or not isinstance(subject, int) or subject <= 0
                    or isinstance(icu, bool) or not isinstance(icu, int) or icu <= 0):
                raise ValueError("invalid_paired_row_identity")
            try:
                point = _parse_clock(landmark)
                event_point = _parse_clock(event) if event is not None else None
                clocks = [point]
                if event_point is not None:
                    clocks.append(event_point)
                if icu in icu_clock:
                    clocks.append(icu_clock[icu])
                _validate_timestamps(clocks)
                horizon_end = point + timedelta(hours=6)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("invalid_paired_row_clock") from None
            identity = (icu, point)
            if identity in seen or (icu in icu_subject and icu_subject[icu] != subject):
                raise ValueError("duplicate_or_inconsistent_paired_row_identity")
            seen.add(identity)
            icu_subject[icu] = subject
            if label == "positive":
                if event_point is None:
                    raise ValueError("positive_comparison_row_missing_event")
                if not point < event_point <= horizon_end:
                    raise ValueError("positive_event_outside_comparison_horizon")
                positive_stays.add(icu)
                positive_patients.add(subject)
            elif event_point is not None and event_point <= horizon_end:
                raise ValueError("negative_event_within_or_before_comparison_horizon")
            if icu in icu_event and icu_event[icu] != event_point:
                raise ValueError("inconsistent_event_time_within_icu")
            icu_event[icu] = event_point
            icu_clock[icu] = point
            try:
                row_features = json.loads(raw)
            except (ValueError, TypeError):
                raise ValueError("invalid_paired_row_features") from None
            if not isinstance(row_features, dict):
                raise ValueError("invalid_paired_row_features")
            features.append(row_features)
            labels.append(int(label == "positive"))
            subjects.append(subject)
        if not labels:
            raise ValueError("no_paired_validation_binary_rows")
    predictions = {}
    for name, pipeline in models.items():
        if not np.array_equal(np.asarray(getattr(pipeline, "classes_", ())), np.array([0, 1])):
            raise ValueError("saved_model_class_order_mismatch")
        matrix = np.full((len(features), len(VARIANTS[name])), np.nan, dtype=float)
        for index, row in enumerate(features):
            for column, key in enumerate(VARIANTS[name]):
                value = row.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("nonfinite_or_nonnumeric_paired_feature")
                matrix[index, column] = float(value)
        with threadpool_limits(limits=1):
            probability = np.asarray(pipeline.predict_proba(matrix), dtype=float)
        if (probability.shape != (len(labels), 2) or not np.all(np.isfinite(probability))
                or np.any(probability < 0) or np.any(probability > 1)
                or not np.allclose(probability.sum(axis=1), 1, rtol=1e-8, atol=1e-12)):
            raise ValueError("saved_model_invalid_probability_output")
        predictions[name] = probability[:, 1]
    result = _paired_bootstrap(labels, predictions, subjects, replicates=replicates)
    after = table.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("model_table_changed_during_comparison")
    result.update({
        "stage": "paired_development_validation_only_test_unopened",
        "comparison_version": COMPARISON_VERSION, "model_version": MODEL_VERSION,
        "versions": {"numpy": np.__version__, "sklearn": sklearn.__version__},
        "provenance": {"source_fingerprint_sha256": _hash_json(source), "model_artifact_sha256": artifact_hashes},
        "samples": {
            "binary_windows": len(labels), "positive_windows": sum(labels),
            "negative_windows": len(labels) - sum(labels), "patients": len(set(subjects)),
            "positive_event_stays": len(positive_stays), "positive_event_patients": len(positive_patients),
        },
        "row_policy": "identical_validation_current_rr_accepted_binary_rows_for_all_models",
        "limitations": [
            "Fixed validation predictions; no refitting. Intervals omit training and model-selection uncertainty.",
            "Patient clusters, including repeated ICU stays/windows, are resampled together; single-class draws are skipped for every metric.",
            "No multiple-comparison correction; exploratory paired differences are not confirmatory evidence.",
            "Individual model confidence-interval overlap is not used as a test of incremental value.",
            "Paired accepted-RR windows define this comparison population; it does not quantify full-cohort utility or equal-alarm-burden benefit.",
            "No held-out test, external validation, rhythm-confirmed NN-HRV or clinical-benefit claim.",
        ],
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    return result
