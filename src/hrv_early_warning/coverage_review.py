"""Read-only development coverage audit of frozen hourly M2 artifacts.

Only aggregate counts leave this module. No model loading, fitting, scoring,
threshold change, test feature query, or dense event-audit epoch is used.
"""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .modeling import RR_FEATURES
from .replay import _validate_metadata, _readonly, _write_private
from .risksets import load_stay_outcomes


VERSION = "development_hourly_coverage_review_v1"
ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "reports/local/coverage_review"
STAGES = ("all_risk_windows", "current_epoch_planned", "current_decoded_ok_or_partial",
          "current_rr_accepted", "history_at_least_3_of_6", "history_all_6_of_6",
          "current_and_history_at_least_3_of_6", "current_and_history_all_6_of_6")
STATUS = ("no_current_epoch_planned", "current_decode_unavailable",
          "current_decoded_engineering_rejected", "current_rr_accepted")


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stat(path):
    value = Path(path).stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


class _Counts:
    def __init__(self):
        self.patients, self.icus, self.events, self.event_patients = set(), set(), set(), set()
        self.binary_patients = set()
        self.labels = Counter()
        self.seconds, self.binary_seconds = 0.0, 0.0

    def add(self, row):
        self.patients.add(row["subject_id"])
        self.icus.add(row["icustay_id"])
        self.labels[row["label"]] += 1
        self.seconds += row["monitoring_seconds"]
        if row["label"] != "censored":
            self.binary_patients.add(row["subject_id"])
            self.binary_seconds += row["monitoring_seconds"]
        if row["label"] == "positive":
            self.events.add(row["icustay_id"])
            self.event_patients.add(row["subject_id"])

    def report(self):
        binary = self.labels["positive"] + self.labels["negative"]
        return {"patients": len(self.patients), "icus": len(self.icus),
                "events_with_at_least_one_positive_window": len(self.events),
                "event_patients": len(self.event_patients),
                "windows": sum(self.labels.values()),
                "labels": {key: self.labels[key] for key in ("positive", "negative", "censored")},
                "binary_patients": len(self.binary_patients), "binary_windows": binary,
                "observed_patient_hours_including_censored": self.seconds / 3600,
                "binary_evaluable_patient_hours": self.binary_seconds / 3600,
                "positive_fraction_of_binary_windows": self.labels["positive"] / binary if binary else None}


def _reason_codes(value):
    if not isinstance(value, list) or any(not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code) for code in value):
        raise ValueError("invalid_nonidentifying_reason_codes")
    return set(value)


def classify_row(row):
    """Validate cached history and exact-current identity before counting."""
    if row["split"] not in ("train", "validation"):
        raise ValueError("heldout_or_unknown_row_forbidden")
    if row["label"] not in ("positive", "negative", "censored"):
        raise ValueError("invalid_risk_label")
    seconds = row["monitoring_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise ValueError("invalid_monitored_interval")
    accepted = row["current_rr_accepted"]
    if accepted not in (0, 1):
        raise ValueError("invalid_current_acceptance")
    features = json.loads(row["features"])
    count = features.get("rr_valid_count")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 6:
        raise ValueError("invalid_history_count")
    observed = []
    for lag in range(6):
        values = [features[f"{name}_lag{lag}"] for name in RR_FEATURES]
        present = [value is not None for value in values]
        if any(present) != all(present):
            raise ValueError("inconsistent_rr_feature_missingness")
        if all(present) and any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("invalid_cached_rr_feature")
        if all(present) and values[0] <= 0:
            raise ValueError("invalid_mean_rr")
        observed.append(all(present))
    if sum(observed) != count or bool(accepted) != observed[0]:
        raise ValueError("cached_history_or_current_acceptance_mismatch")
    raw = row["epoch_result"]
    if raw is None:
        if accepted:
            raise ValueError("accepted_current_epoch_absent")
        return count, "no_current_epoch_planned", set(), set(), "not_planned"
    epoch = json.loads(raw)
    if (epoch.get("icu") != row["icustay_id"]
            or datetime.fromisoformat(epoch["end"]) != datetime.fromisoformat(row["landmark"])
            or epoch.get("accepted") is not bool(accepted)
            or row["epoch_accepted"] != accepted):
        raise ValueError("current_epoch_identity_or_acceptance_mismatch")
    reasons, errors = _reason_codes(epoch["reasons"]), _reason_codes(epoch["decode_errors"])
    status = epoch["decode_status"]
    if status not in ("ok", "partial", "unavailable", "rejected"):
        raise ValueError("unknown_decode_status")
    if accepted:
        if reasons or status not in ("ok", "partial"):
            raise ValueError("accepted_epoch_has_rejection")
        category = "current_rr_accepted"
    elif status not in ("ok", "partial"):
        category = "current_decode_unavailable"
    else:
        category = "current_decoded_engineering_rejected"
    return count, category, reasons, errors, status


def summarize_rows(rows, *, age_bands=None):
    """Pure aggregate reducer; IDs are held only in local sets, never returned."""
    counters = {split: {stage: _Counts() for stage in STAGES} for split in ("train", "validation", "development")}
    statuses = {split: {status: _Counts() for status in STATUS} for split in counters}
    history = {split: Counter() for split in counters}
    reasons, decode_errors, decode_statuses = ({split: Counter() for split in counters} for _ in range(3))
    strata = {split: defaultdict(lambda: {"all": _Counts(), "current_rr_accepted": _Counts()}) for split in counters}
    previous_key = None
    for row in rows:
        count, category, rejected_reasons, errors, decode_status = classify_row(row)
        key = (row["split"], row["icustay_id"], row["landmark"])
        if previous_key is not None and key <= previous_key:
            raise ValueError("rows_must_be_unique_and_ordered_by_split_icu_time")
        previous_key = key
        accepted = category == "current_rr_accepted"
        flags = {
            "all_risk_windows": True, "current_epoch_planned": category != "no_current_epoch_planned",
            "current_decoded_ok_or_partial": decode_status in ("ok", "partial"),
            "current_rr_accepted": accepted, "history_at_least_3_of_6": count >= 3,
            "history_all_6_of_6": count == 6,
            "current_and_history_at_least_3_of_6": accepted and count >= 3,
            "current_and_history_all_6_of_6": accepted and count == 6,
        }
        band = age_bands.get(row["icustay_id"], "unknown") if age_bands is not None else "unknown"
        for split in (row["split"], "development"):
            for stage, include in flags.items():
                if include:
                    counters[split][stage].add(row)
            statuses[split][category].add(row)
            history[split][str(count)] += 1
            reasons[split].update(rejected_reasons)
            decode_errors[split].update(errors)
            decode_statuses[split][decode_status] += 1
            strata[split][band]["all"].add(row)
            if accepted:
                strata[split][band]["current_rr_accepted"].add(row)
    result = {}
    for split in counters:
        stages = {stage: counter.report() for stage, counter in counters[split].items()}
        denominator = stages["all_risk_windows"]
        for value in stages.values():
            value["coverage_fraction"] = {
                key: value[key] / denominator[key] if denominator[key] else None
                for key in ("patients", "icus", "events_with_at_least_one_positive_window", "windows", "observed_patient_hours_including_censored")}
        result[split] = {
            "stages": stages,
            "mutually_exclusive_current_status": {status: counter.report() for status, counter in statuses[split].items()},
            "hourly_history_valid_count_distribution": {str(count): history[split][str(count)] for count in range(7)},
            "rejection_reason_window_counts_nonexclusive": dict(sorted(reasons[split].items())),
            "decode_error_window_counts_nonexclusive_including_warnings": dict(sorted(decode_errors[split].items())),
            "decode_status_window_counts": dict(sorted(decode_statuses[split].items())),
            "descriptive_age_strata": {band: {key: counter.report() for key, counter in group.items()} for band, group in sorted(strata[split].items())},
        }
    return result


def _output_path(directory):
    raw = Path(directory).absolute()
    if any(path.is_symlink() for path in (raw, *raw.parents)):
        raise ValueError("coverage_output_symlink_forbidden")
    if not raw.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("coverage_output_must_remain_under_ignored_coverage_review")
    target = raw / "summary.json"
    if target.exists() or target.is_symlink():
        raise FileExistsError("coverage_review_exists_use_new_directory")
    return target


def review_coverage(work_dir=None, feasibility_dir=None, output_dir=None):
    work = Path(work_dir or ROOT / "artifacts/m2").resolve()
    feasibility = Path(feasibility_dir or ROOT / "artifacts/feasibility").resolve()
    target = _output_path(output_dir or OUTPUT_ROOT)
    sources = [work / name for name in ("model_table.sqlite", "epochs.sqlite", "patient_split.json", "signal_summary.json", "vitals.sqlite", "vitals_summary.json", "models/development_report.json", "paired_comparison.json")]
    sources += [feasibility / "stay_outcomes.jsonl", feasibility / "waveform_metadata.jsonl"]
    before = {str(path): _stat(path) for path in sources}
    if any(Path(str(path) + "-wal").exists() and Path(str(path) + "-wal").stat().st_size for path in sources if path.suffix == ".sqlite"):
        raise ValueError("coverage_review_refuses_active_wal")
    with closing(_readonly(work / "model_table.sqlite")) as connection:
        connection.execute("PRAGMA query_only=ON")
        # Full source/split checks precede every feature SELECT. A contaminated
        # table is rejected rather than filtering it after JSON deserialization.
        source, _, _ = _validate_metadata(connection, work, feasibility)
        fingerprint = source["signal"]["fingerprint"]
        for filename, expected in fingerprint["code"].items():
            if _hash_file(ROOT / "src/hrv_early_warning" / filename) != expected:
                raise ValueError("frozen_signal_code_fingerprint_changed")
        manifest = json.loads((work / "patient_split.json").read_text())
        stays, _ = load_stay_outcomes(feasibility / "stay_outcomes.jsonl")
        development_stays = [stay for stay in stays if manifest["assignments"][str(stay.subject_id)] in ("train", "validation")]
        age_bands = {stay.icustay_id: ("age_obscured_over_89" if stay.age_obscured else "age_unknown" if stay.age_years is None else "18_44" if stay.age_years < 45 else "45_64" if stay.age_years < 65 else "65_89") for stay in development_stays}
        cohort = {}
        for split in ("train", "validation", "development"):
            selected = [stay for stay in development_stays if split == "development" or manifest["assignments"][str(stay.subject_id)] == split]
            cohort[split] = {"adult_patients_before_risk_time_exclusions": len({stay.subject_id for stay in selected}),
                             "adult_icus_before_risk_time_exclusions": len(selected),
                             "qualifying_events_before_six_hour_rule": sum(stay.event_time is not None for stay in selected),
                             "icus_with_any_matched_waveform_metadata": sum(stay.matched_waveform_records > 0 for stay in selected)}
        connection.execute("ATTACH DATABASE ? AS signals", ((work / "epochs.sqlite").as_uri() + "?mode=ro",))
        rows = connection.execute("""SELECT m.subject_id,m.icustay_id,m.landmark,m.label,m.split,
            m.monitoring_seconds,m.current_rr_accepted,m.features,
            e.accepted AS epoch_accepted,e.result AS epoch_result
            FROM model_rows m LEFT JOIN signals.epochs e
            ON m.icustay_id=e.icustay_id AND m.landmark=e.endtime
            WHERE m.split IN ('train','validation')
            ORDER BY m.split,m.icustay_id,m.landmark""")
        results = summarize_rows(rows, age_bands=age_bands)
        expected = json.loads(connection.execute("SELECT value FROM metadata WHERE key='report'").fetchone()[0])["development_rows"]
        if results["development"]["stages"]["all_risk_windows"]["windows"] != expected:
            raise ValueError("coverage_row_count_disagrees_with_frozen_table")
    if before != {str(path): _stat(path) for path in sources}:
        raise ValueError("coverage_sources_changed_during_review")
    report = {"schema_version": VERSION, "scope": "train_validation_hourly_riskset_only_test_features_unopened",
              "cohort": cohort, "coverage": results,
              "provenance": {"source_fingerprint_sha256": _hash_json(source),
                             "patient_split_sha256": _hash_file(work / "patient_split.json"),
                             "signal_summary_sha256": _hash_file(work / "signal_summary.json"),
                             "review_code_sha256": _hash_file(__file__),
                             "source_validation": "Frozen completion/source metadata, source file sizes/mtimes stable during scan, and frozen signal source hashes; no raw waveform revalidation"},
              "definitions": {
                  "patient_counts": "Distinct patients with at least one included window; patient counts in disjoint window categories can overlap and must not be summed.",
                  "events": "Distinct ICU first-treatment events with at least one positive prediction window at the named stage, not sensitivity or prediction success.",
                  "current_epoch_planned": "Exact hourly endpoint exists in completed epoch cache; metadata overlap caused planning, not necessarily available lead II or decodable ECG.",
                  "current_decoded_ok_or_partial": "Decoder returned ok or partial; incomplete/nonfinite signal may still fail engineering screening.",
                  "history": "Current and preceding five exact hourly five-minute samples. Three/six samples do not mean three/six continuous ECG hours. History-only stages do not necessarily have accepted current RR.",
                  "hours": "Sum of each hourly landmark's actual untreated observation duration, including partial final intervals. Binary-evaluable hours exclude censored rows.",
                  "reasons": "Reason/error counts are nonexclusive and can exceed rejected windows; decode errors include warnings even if another source decoded successfully.",
                  "age": "Descriptive, unadjusted window/stay composition. Patients may have multiple ICU stays and age bands; no causal selection-bias or significance claim.",
                  "ceiling": "Planned/decodable stages are optimistic ceilings under unchanged data and hourly selection, not forecasts of recoverable or clinically valid HRV.",
                  "validity": "Engineering acceptance is not adjudicated timing accuracy, sinus rhythm, normal NN qualification, or clinical HRV validity."},
              "test_features_read": False, "models_loaded_or_refitted": False,
              "quality_rules_changed": False, "dense_event_audit_epochs_used": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_private(target, report, exclusive=True)
    return report
