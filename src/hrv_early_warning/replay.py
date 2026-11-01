"""Private development-case replay from frozen M2 artifacts; never fit a model.

The browser document contains relative clocks and case aliases only. It remains
patient-level research data and must stay in the ignored artifacts directory.
Selection is stratified for demonstration, not an estimate of performance.
"""

from contextlib import closing
from datetime import timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from .model_comparison import _hash_file, _hash_json, _parse_clock
from .modeling import MODEL_VERSION, RR_FEATURES, VARIANTS, _signal_readiness
from .risksets import load_stay_outcomes


REPOSITORY = Path(__file__).resolve().parents[2]
REPLAY_ROOT = REPOSITORY / "artifacts" / "replay"
SELECTION_VERSION = "development_stratified_hash_v1"
SEED = 20261101
VITAL_NAMES = {
    "heart_rate_bpm": "heart_rate", "map_mmhg": "map",
    "respiratory_rate_bpm": "respiratory_rate", "spo2_pct": "spo2",
}
CASE_TEXT = {
    "event": ("病例 01｜觀察到主要終點", "依事件狀態及固定雜湊選取；事件標記僅供回溯檢視。"),
    "no_event": ("病例 02｜未觀察到主要終點", "追蹤內未觀察到本研究主要終點；不代表沒有臨床惡化。"),
    "interruption": ("病例 03｜訊號或品質中斷", "包含可計算與不可計算 RR 的時點，用於展示品質閘門。"),
}


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _stat_signature(path):
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _safe_output_directory(directory):
    """Only the ignored replay tree is writable, with no symlink components."""
    raw = Path(directory).absolute()
    for path in (raw, *raw.parents):
        if path.is_symlink():
            raise ValueError("replay_output_symlink_forbidden")
    resolved, root = raw.resolve(), REPLAY_ROOT.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("replay_output_must_be_in_ignored_replay_directory")
    for name in ("replay.json", "selection.private.json", "export_summary.json"):
        target = resolved / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("unsafe_replay_output_file")
        if target.exists() and target.stat().st_nlink != 1:
            raise ValueError("replay_output_hardlink_forbidden")
    return resolved


def _write_private(path, value, *, exclusive=False):
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    # Exclusive manifest publication ensures simultaneous exporters cannot
    # overwrite the first pre-score selection. All files are owner-readable.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)


def _finite(value, *, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("replay_feature_must_be_finite_numeric_or_null")
    return float(value)


def _validate_metadata(connection, work, feasibility):
    # These checks inspect split/identity metadata only. No test feature SELECT
    # can occur, even if a supposedly development-only table was contaminated.
    if connection.execute("SELECT count(*) FROM model_rows WHERE split IS NULL OR split NOT IN ('train','validation')").fetchone()[0]:
        raise ValueError("replay_refuses_table_containing_test_or_unknown_split")
    if connection.execute("SELECT 1 FROM model_rows GROUP BY subject_id HAVING count(DISTINCT split)>1 LIMIT 1").fetchone():
        raise ValueError("replay_patient_split_overlap")
    if connection.execute("SELECT 1 FROM model_rows GROUP BY icustay_id HAVING count(DISTINCT subject_id)>1 OR count(DISTINCT hadm_id)>1 LIMIT 1").fetchone():
        raise ValueError("replay_stay_identity_mismatch")
    metadata = {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT key,value FROM metadata")}
    source, table = metadata["source_fingerprint"], metadata["report"]
    report = _read_json(work / "models" / "development_report.json")
    split_manifest = _read_json(work / "patient_split.json")
    ready, signal = _signal_readiness(feasibility, work)
    if (source.get("model_version") != MODEL_VERSION or source.get("rr_ready") is not True
            or not ready or source.get("signal") != signal):
        raise ValueError("replay_requires_matching_complete_signal_cache")
    for name, path in (
        ("stay_outcomes_sha256", feasibility / "stay_outcomes.jsonl"),
        ("patient_split_sha256", work / "patient_split.json"),
        ("vitals_summary_sha256", work / "vitals_summary.json"),
    ):
        if source.get(name) != _hash_file(path):
            raise ValueError("replay_source_fingerprint_mismatch")
    stat = (work / "vitals.sqlite").stat()
    if source.get("vitals_bytes") != stat.st_size or source.get("vitals_mtime_ns") != stat.st_mtime_ns:
        raise ValueError("replay_vitals_fingerprint_mismatch")
    if (report.get("stage") != "development_validation_only_test_unopened"
            or report.get("source_fingerprint") != source
            or report.get("model_version") != MODEL_VERSION or report.get("model_table") != table
            or table.get("model_version") != MODEL_VERSION or table.get("rr_ready") is not True
            or table.get("signal_status") != "complete_verified"
            or table.get("scope") != "train_validation_only_including_censored_no_test_feature_queries"
            or table.get("development_rows") != connection.execute("SELECT count(*) FROM model_rows").fetchone()[0]):
        raise ValueError("replay_development_completion_metadata_mismatch")
    for key in ("algorithm", "seed", "cohort_sha256", "population_sha256", "split_counts"):
        if table.get("split", {}).get(key) != split_manifest.get(key):
            raise ValueError("replay_split_fingerprint_mismatch")
    assignments = split_manifest.get("assignments", {})
    for row in connection.execute("SELECT DISTINCT subject_id,split FROM model_rows"):
        if assignments.get(str(row["subject_id"])) != row["split"]:
            raise ValueError("replay_split_assignment_mismatch")
    comparison = _read_json(work / "paired_comparison.json")
    if (comparison.get("status") != "paired_comparison_complete"
            or comparison.get("stage") != "paired_development_validation_only_test_unopened"
            or comparison.get("provenance", {}).get("source_fingerprint_sha256") != _hash_json(source)):
        raise ValueError("replay_comparison_provenance_mismatch")
    return source, report, comparison


def _load_models(work, source, report, comparison):
    """Load only trusted local joblib files after verifying paired model hashes."""
    import joblib

    models, hashes = {}, {}
    for variant, features in VARIANTS.items():
        path = work / "models" / f"{variant}.joblib"
        before = _stat_signature(path)
        digest = _hash_file(path)
        if variant.startswith("paired_") and comparison["provenance"]["model_artifact_sha256"].get(variant) != digest:
            raise ValueError("replay_saved_model_hash_mismatch")
        saved = joblib.load(path)
        evidence = report.get("models", {}).get(variant, {})
        if (not isinstance(saved, dict) or saved.get("variant") != variant
                or saved.get("model_version") != MODEL_VERSION or saved.get("source_fingerprint") != source
                or tuple(saved.get("features", ())) != tuple(features)
                or evidence.get("features") != list(features)
                or evidence.get("status") != "development_validation_complete"):
            raise ValueError("replay_saved_model_metadata_mismatch")
        threshold = _finite(saved.get("threshold"), nullable=False)
        if not 0 <= threshold <= 1 or threshold != evidence.get("validation_alarms", {}).get("threshold"):
            raise ValueError("replay_saved_threshold_mismatch")
        pipeline = saved.get("pipeline")
        if not callable(getattr(pipeline, "predict_proba", None)) or tuple(getattr(pipeline, "classes_", ())) != (0, 1):
            raise ValueError("replay_invalid_saved_classifier")
        if before != _stat_signature(path):
            raise ValueError("replay_model_changed_during_load")
        models[variant], hashes[variant] = saved, digest
    return models, hashes


def select_cases(connection, split, *, seed=SEED):
    """Pick three distinct patients using metadata only, without reading features.

    Each eligible stay has 6–240 hourly rows and at least one accepted current
    epoch. Categories are an observed endpoint, no observed endpoint, and a
    mixture of accepted/rejected epochs. Selection is intentionally illustrative.
    """
    if split not in ("validation", "train"):
        raise ValueError("replay_only_allows_train_or_validation")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("replay_seed_must_be_integer")
    candidates = list(connection.execute(
        "SELECT subject_id,hadm_id,icustay_id,count(*) AS n,sum(current_rr_accepted) AS accepted,"
        "max(event_time IS NOT NULL) AS has_event,max(label='positive') AS positive "
        "FROM model_rows WHERE split=? GROUP BY subject_id,hadm_id,icustay_id "
        "HAVING count(*) BETWEEN 6 AND 240 AND sum(current_rr_accepted)>0", (split,)))
    used, selected = set(), []
    predicates = {
        "event": lambda row: row["has_event"] and row["positive"],
        "no_event": lambda row: not row["has_event"],
        "interruption": lambda row: row["accepted"] < row["n"],
    }
    for category, predicate in predicates.items():
        pool = [row for row in candidates if row["subject_id"] not in used and predicate(row)]
        if not pool:
            raise ValueError("replay_insufficient_development_case_categories")
        selected_row = min(pool, key=lambda row: (
            hashlib.sha256(f"{seed}:{category}:{row['subject_id']}:{row['icustay_id']}".encode()).digest(),
            row["subject_id"], row["icustay_id"]))
        used.add(selected_row["subject_id"])
        selected.append({"category": category, **{key: selected_row[key] for key in ("subject_id", "hadm_id", "icustay_id")}})
    return selected


def _epoch_reason_codes(epochs, row, accepted, features):
    # Exact current endpoint only; no future or dense event-audit epoch query.
    saved = epochs.execute("SELECT accepted,result FROM epochs WHERE icustay_id=? AND endtime=?",
                           (row["icustay_id"], row["landmark"])).fetchone()
    if saved is None:
        if accepted:
            raise ValueError("replay_accepted_epoch_missing")
        return ["no_matching_ecg_epoch"]
    item = json.loads(saved["result"])
    if (saved["accepted"] not in (0, 1) or bool(saved["accepted"]) != accepted
            or item.get("accepted") is not accepted or item.get("icu") != row["icustay_id"]
            or _parse_clock(item.get("end")) != _parse_clock(row["landmark"])):
        raise ValueError("replay_epoch_identity_or_quality_mismatch")
    if accepted:
        for name in RR_FEATURES:
            if _finite(item.get("features", {}).get(name), nullable=False) != features[f"{name}_lag0"]:
                raise ValueError("replay_current_rr_cache_mismatch")
        return []
    # Do not send decoder errors or arbitrary free text (may include filenames).
    # Cache reasons are checked against the frozen engineering enum.
    reasons = item.get("reasons", [])
    if not isinstance(reasons, list) or any(not isinstance(code, str) for code in reasons):
        raise ValueError("replay_invalid_quality_reasons")
    allowed = {
        "wrong_epoch_length", "sampling_frequency_below_project_minimum", "empty_signal",
        "nonfinite_samples", "flat_run", "low_block_amplitude", "excessive_amplitude_range",
        "suspected_clipping", "detector_error", "low_beat_agreement", "low_block_beat_agreement",
        "too_few_beats", "incomplete_beat_span", "too_many_screened_rr_intervals",
        "insufficient_valid_rr_duration", "insufficient_adjacent_rr_intervals", "ecg_decode_unavailable",
    }
    return sorted({code if code in allowed else "other_quality_rejection" for code in reasons}) or ["quality_rejected"]


def _case_document(selection, rows, record, epochs, models):
    import numpy as np

    start = _parse_clock(record.intime.isoformat())
    physical_end = _parse_clock(record.followup_end.isoformat())
    exposure = _parse_clock(record.first_exposure_time.isoformat()) if record.first_exposure_time else None
    end = min(physical_end, exposure) if exposure else physical_end
    event = _parse_clock(record.event_time.isoformat()) if record.event_time else None
    points, features_by_row = [], []
    previous = -1.0
    for row in rows:
        clock = _parse_clock(row["landmark"])
        hour = (clock - start).total_seconds() / 3600
        row_event = _parse_clock(row["event_time"]) if row["event_time"] else None
        row_exposure = _parse_clock(row["first_exposure_time"]) if row["first_exposure_time"] else None
        if (hour < 6 or hour <= previous or not math.isclose(hour, round(hour), abs_tol=1e-9)
                or clock >= end or _parse_clock(row["history_start"]) != clock - timedelta(hours=6)
                or _parse_clock(row["horizon_end"]) != clock + timedelta(hours=6)
                or _parse_clock(row["followup_end"]) != physical_end
                or row_event != event or row_exposure != exposure):
            raise ValueError("replay_invalid_or_inconsistent_relative_clocks")
        previous = hour
        if row["current_rr_accepted"] not in (0, 1):
            raise ValueError("replay_invalid_current_rr_gate")
        accepted = bool(row["current_rr_accepted"])
        features = json.loads(row["features"])
        required = set().union(*VARIANTS.values())
        if not isinstance(features, dict) or set(features) != required:
            raise ValueError("replay_frozen_feature_schema_mismatch")
        features = {key: _finite(value) for key, value in features.items()}
        if any((features[f"{name}_lag0"] is not None) != accepted for name in RR_FEATURES):
            raise ValueError("replay_current_rr_gate_feature_mismatch")
        observed_history = 0
        for lag in range(6):
            sample = [features[f"{name}_lag{lag}"] for name in RR_FEATURES]
            if any(value is not None for value in sample):
                if any(value is None or value < 0 for value in sample) or sample[0] == 0:
                    raise ValueError("replay_invalid_rr_history_features")
                observed_history += 1
        valid_count = features["rr_valid_count"]
        if valid_count is None or valid_count != observed_history:
            raise ValueError("replay_invalid_history_count")
        rr = {name: features[f"{name}_lag0"] if accepted else None for name in RR_FEATURES}
        rr.update(accepted=accepted, reason_codes=_epoch_reason_codes(epochs, row, accepted, features),
                  rhythm_status="not_assessed", history_valid_count=int(valid_count))
        points.append({"hour": hour, "vitals": {public: features[internal] for public, internal in VITAL_NAMES.items()},
                       "rr": rr, "scores": {name: None for name in VARIANTS}})
        features_by_row.append(features)
    if not points:
        raise ValueError("replay_empty_selected_case")
    for name, model in models.items():
        eligible = [i for i, point in enumerate(points) if name == "vitals_all" or point["rr"]["accepted"]]
        if not eligible:
            continue
        matrix = np.array([[np.nan if features_by_row[i][key] is None else features_by_row[i][key]
                            for key in VARIANTS[name]] for i in eligible], dtype=float)
        probabilities = np.asarray(model["pipeline"].predict_proba(matrix), dtype=float)
        if (probabilities.shape != (len(eligible), 2) or not np.all(np.isfinite(probabilities))
                or np.any(probabilities < 0) or np.any(probabilities > 1)
                or not np.allclose(probabilities.sum(axis=1), 1, rtol=0, atol=1e-8)):
            raise ValueError("replay_invalid_saved_model_probabilities")
        for i, score in zip(eligible, probabilities[:, 1]):
            points[i]["scores"][name] = float(score)
    category = selection["category"]
    label, description = CASE_TEXT[category]
    return {
        "id": f"case-{tuple(CASE_TEXT).index(category) + 1:02d}", "label": label,
        "description": description + " RR 尚未確認為正常竇性 NN；v1 R 峰定位缺陷尚未修正，分數僅供研究。",
        "event_hour": (event - start).total_seconds() / 3600 if event else None,
        "followup_end_hour": (end - start).total_seconds() / 3600,
        "thresholds": {name: float(model["threshold"]) for name, model in models.items()}, "points": points,
    }


def export_replay(work_dir=None, feasibility_dir=None, output_dir=None, *, split="validation", seed=SEED):
    """Export three anonymous development stays, after a frozen private selection."""
    if split not in ("validation", "train"):
        raise ValueError("replay_only_allows_train_or_validation")
    work = Path(work_dir or REPOSITORY / "artifacts" / "m2").resolve()
    feasibility = Path(feasibility_dir or REPOSITORY / "artifacts" / "feasibility").resolve()
    destination = _safe_output_directory(output_dir or REPLAY_ROOT)
    if work.is_relative_to(destination) or destination.is_relative_to(work) or feasibility.is_relative_to(destination) or destination.is_relative_to(feasibility):
        raise ValueError("replay_output_must_be_separate_from_sources")
    inputs = [work / name for name in ("model_table.sqlite", "epochs.sqlite", "patient_split.json", "signal_summary.json",
                                     "signal_plan.json", "vitals.sqlite", "vitals_summary.json", "paired_comparison.json")]
    inputs += [work / "models" / "development_report.json", feasibility / "stay_outcomes.jsonl", feasibility / "waveform_metadata.jsonl"]
    inputs += [work / "models" / f"{name}.joblib" for name in VARIANTS]
    before = {path: _stat_signature(path) for path in inputs}
    with closing(_readonly(work / "model_table.sqlite")) as table, closing(_readonly(work / "epochs.sqlite")) as epochs:
        source, report, comparison = _validate_metadata(table, work, feasibility)
        models, hashes = _load_models(work, source, report, comparison)
        selected = select_cases(table, split, seed=seed)
        provenance = {
            "source_fingerprint_sha256": _hash_json(source), "model_artifact_sha256": hashes,
            "model_table_sha256": _hash_file(work / "model_table.sqlite"),
            "development_report_sha256": _hash_file(work / "models" / "development_report.json"),
        }
        manifest = {"selection_version": SELECTION_VERSION, "split": split, "seed": seed,
                    "rules": "distinct patients; 6..240 hourly rows; >=1 accepted epoch; category hash ranking; no scores",
                    "provenance": provenance, "selected": selected}
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        selection_path = destination / "selection.private.json"
        if selection_path.exists():
            if _read_json(selection_path) != manifest:
                raise ValueError("replay_existing_selection_or_provenance_mismatch_use_new_output_subdirectory")
        else:
            try:
                _write_private(selection_path, manifest, exclusive=True)
            except FileExistsError:
                if _read_json(selection_path) != manifest:
                    raise ValueError("replay_concurrent_selection_mismatch") from None
        # Selection is already durable before any model is asked to predict.
        records, _ = load_stay_outcomes(feasibility / "stay_outcomes.jsonl")
        records = {record.icustay_id: record for record in records if record.icustay_id in {item["icustay_id"] for item in selected}}
        cases = []
        for selection in selected:
            record = records[selection["icustay_id"]]
            if record.subject_id != selection["subject_id"] or record.hadm_id != selection["hadm_id"]:
                raise ValueError("replay_selected_cohort_identity_mismatch")
            rows = list(table.execute(
                "SELECT subject_id,hadm_id,icustay_id,landmark,history_start,horizon_end,event_time,followup_end,"
                "first_exposure_time,current_rr_accepted,features FROM model_rows "
                "WHERE split=? AND subject_id=? AND hadm_id=? AND icustay_id=? ORDER BY landmark",
                (split, selection["subject_id"], selection["hadm_id"], selection["icustay_id"])))
            cases.append(_case_document(selection, rows, record, epochs, models))
    if any(_stat_signature(path) != signature for path, signature in before.items()):
        raise ValueError("replay_source_changed_during_export")
    document = {"schema_version": "hrv-replay-v1", "mode": "local_research",
                "source_label": f"M2 {split}｜固定規則選取的真實病例・RR 研究版 v1", "cases": cases}
    summary = {"status": "local_replay_complete", "split": split, "cases": len(cases),
               "hourly_points": sum(len(case["points"]) for case in cases),
               "rr_accepted_points": sum(point["rr"]["accepted"] for case in cases for point in case["points"]),
               "selection_uses_scores": False, "test_features_read": False,
               "models_retrained": False, "source_validation": "frozen metadata fingerprints; not raw waveform byte checksums",
               "provenance": provenance}
    _safe_output_directory(destination)
    _write_private(destination / "replay.json", document)
    _write_private(destination / "export_summary.json", summary)
    return summary
