"""Render checked aggregate M2 results; never fit models or query patient rows.

Run from the repository with PYTHONPATH=src and matplotlib installed. Outputs are review drafts in the
ignored reports/local/m2_publishable directory; nothing is published by this
script. Model files are hashed, never deserialized.
"""

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile

from hrv_early_warning.model_comparison import BOOTSTRAP_SEED, COMPARISON_VERSION
from hrv_early_warning.modeling import MODEL_VERSION, VARIANTS


REPORT_DIRECTORY = Path(__file__).resolve().parents[1] / "reports/local/m2_publishable"
BASELINE = "paired_vitals"
PAIRED = (BASELINE, "paired_current_rr", "paired_history_rr", "paired_history_trend_rr")
ALL_MODELS = ("vitals_all", *PAIRED)
METRICS = ("AUROC", "AUPRC", "Brier")
LABELS = {"vitals_all": "全風險集合：常規資訊", BASELINE: "配對：常規資訊",
          "paired_current_rr": "配對：加入當下 RR", "paired_history_rr": "配對：加入歷史 RR",
          "paired_history_trend_rr": "配對：歷史 RR 與斜率"}
PLOT_LABELS = ("Vitals", "+ Current RR", "+ RR history", "+ RR history + slopes")
EVENT_KEYS = ("any_accepted_history_epoch", "at_least_1h_accepted_epochs",
              "at_least_3h_accepted_epochs", "full_6h_accepted_epochs",
              "any_current_hourly_epoch_accepted")
REJECTION_CODES = frozenset({
    "ecg_decode_unavailable", "wrong_epoch_length", "sampling_frequency_below_project_minimum",
    "empty_signal", "nonfinite_samples", "flat_run", "low_block_amplitude", "excessive_amplitude_range",
    "suspected_clipping", "detector_error", "low_beat_agreement", "low_block_beat_agreement",
    "too_few_beats", "incomplete_beat_span", "too_many_screened_rr_intervals",
    "insufficient_valid_rr_duration", "insufficient_adjacent_rr_intervals",
})
DECODE_CODES = frozenset({
    "leaf_header_missing", "leaf_header_unreadable", "wfdb_header_error", "nested_multisegment_unsupported",
    "invalid_leaf_sampling_metadata", "lead_ii_absent", "lead_ii_ambiguous", "lead_ii_header_inconsistent",
    "explicit_calibration_required", "invalid_adc_gain", "unsupported_voltage_unit", "calibration_header_inconsistent",
    "multiple_samples_per_frame_unsupported", "nonzero_skew_unsupported", "lead_ii_has_no_signal_file",
    "invalid_leaf_header", "leaf_subject_identity_mismatch", "leaf_path_unsafe_or_unreadable",
    "segment_sample_count_mismatch", "segment_sampling_rate_mismatch", "no_records", "mixed_subjects",
    "record_subject_identity_mismatch", "no_decodable_lead_ii", "incompatible_sampling_rates",
    "epoch_contains_no_samples", "incompatible_sample_grids", "signal_file_missing", "signal_file_unreadable",
    "wfdb_decode_error", "decoded_shape_or_rate_mismatch", "decoded_channel_or_unit_mismatch",
    "overlapping_signal_conflict",
})


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def number(value, name, low=0, high=None):
    require(not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= low and (high is None or value <= high),
            "invalid_number_" + name)
    return value


def count(value, name):
    number(value, name)
    require(isinstance(value, int), "invalid_count_" + name)
    return value


def validate_reason_counts(value, name, allowed_codes, maximum):
    require(isinstance(value, dict), "invalid_reason_counts_" + name)
    for code, total in value.items():
        # Only fixed, reviewed diagnostic codes may enter a publishable table;
        # arbitrary strings could contain a patient path or exception details.
        require(code in allowed_codes, "unsupported_reason_code_" + name)
        require(count(total, name) <= maximum, "reason_count_exceeds_epochs_" + name)


def current_event_counts_by_split(value):
    require(isinstance(value, dict), "invalid_event_coverage_by_split")
    counts = {}
    for split in ("train", "validation", "test"):
        coverage = value.get(split, {})
        require(isinstance(coverage, dict), "invalid_split_event_coverage")
        # Counter/defaultdict serializations can omit a never-observed key or
        # an entire split with no eligible events. Neither means missing work.
        counts[split] = count(coverage.get("any_current_hourly_epoch_accepted", 0), "current_event_coverage")
    return counts


def same_number(actual, expected, name):
    require(actual is not None and math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-12),
            "inconsistent_" + name)


def interval(value, name, low=0):
    require(isinstance(value, list) and len(value) == 2, "missing_interval_" + name)
    number(value[0], name, low, 1)
    number(value[1], name, low, 1)
    require(value[0] <= value[1], "reversed_interval_" + name)


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream)
    require(isinstance(value, dict), "report_must_be_an_object")
    return value


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def validate_bootstrap(value, paired=False):
    requested = count(value["replicates_requested"], "bootstrap_requested")
    valid = count(value["replicates_valid"], "bootstrap_valid")
    skipped_key = "replicates_skipped_single_class" if paired else "replicates_skipped_single_class_or_empty"
    skipped = count(value[skipped_key], "bootstrap_skipped")
    require(valid > 0 and requested == valid + skipped, "incomplete_bootstrap")
    require(requested == (200 if paired else 100), "bootstrap_replicates_mismatch")
    require(value["seed"] == BOOTSTRAP_SEED, "bootstrap_seed_mismatch")
    same_number(value["confidence_level"], .95, "confidence_level")
    expected = ("shared_patient_cluster_percentile_fixed_predictions_no_refit" if paired else
                "patient_cluster_percentile_bootstrap_fixed_predictions_no_refit")
    require(value["method"] == expected, "unsupported_bootstrap_method")


def validate_stats(stats):
    for key in ("rows", "binary_rows", "distinct_patients", "distinct_binary_patients",
                "distinct_events_with_positive_window"):
        count(stats[key], key)
    counts = stats["label_counts"]
    for label in ("positive", "negative", "censored"):
        count(counts[label], label)
    require(stats["binary_rows"] == counts["positive"] + counts["negative"]
            and stats["rows"] == stats["binary_rows"] + counts["censored"], "inconsistent_row_counts")
    require(0 < stats["distinct_binary_patients"] <= stats["distinct_patients"] <= stats["rows"],
            "inconsistent_patient_counts")
    require(0 < counts["positive"] and 0 < counts["negative"], "both_validation_classes_required")
    require(0 < stats["distinct_events_with_positive_window"] <= counts["positive"], "invalid_event_counts")
    require(number(stats["observed_patient_hours"], "observed_hours") > 0, "nonpositive_observed_hours")


def validate_alarms(alarms, stats, full):
    for key in ("notifications", "positive_notifications", "false_notifications", "censored_notifications",
                "refractory_suppressed_crossings", "scored_events", "full_riskset_events", "events_caught"):
        count(alarms[key], key)
    require(alarms["notifications"] == sum(alarms[key] for key in
            ("positive_notifications", "false_notifications", "censored_notifications")), "inconsistent_notifications")
    scored, full_events, caught = alarms["scored_events"], alarms["full_riskset_events"], alarms["events_caught"]
    require(scored == stats["distinct_events_with_positive_window"]
            and full_events == full["distinct_events_with_positive_window"]
            and 0 <= caught <= scored <= full_events and scored > 0, "inconsistent_event_denominators")
    same_number(alarms["event_sensitivity_among_scored_events"], caught / scored, "scored_event_sensitivity")
    same_number(alarms["event_sensitivity_full_riskset"], caught / full_events, "full_event_sensitivity")
    observed = number(alarms["observed_scored_patient_hours"], "observed_hours")
    evaluable = number(alarms["evaluable_scored_patient_hours"], "evaluable_hours")
    require(0 < evaluable <= observed, "invalid_alarm_hour_denominators")
    same_number(observed, stats["observed_patient_hours"], "scored_hours")
    same_number(alarms["full_riskset_observed_patient_hours"], full["observed_patient_hours"], "full_hours")
    same_number(alarms["observed_time_coverage_against_full_riskset"], observed / full["observed_patient_hours"], "time_coverage")
    for kind, hours in (("observed", observed), ("evaluable", evaluable)):
        same_number(alarms[f"false_notifications_per_100_{kind}_patient_hours"],
                    100 * alarms["false_notifications"] / hours, "false_alarm_rate")
    number(alarms["threshold"], "alarm_threshold", 0, 1)
    require(alarms["refractory_hours"] == 6, "unsupported_alarm_refractory")
    for key in ("lead_time_hours_mean", "lead_time_hours_median"):
        if caught:
            require(number(alarms[key], key, 0, 6) > 0, "invalid_caught_event_lead_time")
        else:
            require(alarms[key] is None, "lead_time_without_caught_events")


def load_validated(work_dir):
    """Whitelist aggregates and validate provenance without reading feature rows."""
    work = Path(work_dir).resolve()
    signal = read_json(work / "signal_summary.json")
    plan = read_json(work / "signal_plan.json")
    require(signal["complete"] is True, "complete_signal_summary_required")
    with closing(readonly(work / "epochs.sqlite")) as connection:
        cached, accepted = connection.execute("SELECT COUNT(*), SUM(accepted) FROM epochs").fetchone()
        fingerprint = json.loads(connection.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()[0])
    require(cached == plan["plan"]["unique_epochs"] == signal["screened_epochs"] > 0,
            "incomplete_or_mismatched_signal_count")
    require(signal["plan"] == plan["plan"] and fingerprint == plan["fingerprint"]
            and signal["config"] == fingerprint["config"] and signal["versions"] == fingerprint["versions"],
            "signal_metadata_mismatch")
    require(count(signal["accepted_epochs"], "accepted_epochs") == accepted, "signal_acceptance_count_mismatch")
    require(0 <= accepted <= cached, "invalid_signal_acceptance")
    validate_reason_counts(signal["rejection_reason_counts"], "rejection", REJECTION_CODES, cached - accepted)
    # Every rejected epoch has at least one reason in the frozen worker. The
    # reasons overlap, so a lower bound is required, not equality with rejects.
    require(sum(signal["rejection_reason_counts"].values()) >= cached - accepted,
            "rejection_reason_counts_incomplete")
    # Decoder error codes are de-duplicated within each epoch. An alternate
    # record can still supply a complete accepted signal, so the upper bound
    # here is all screened epochs, not only rejected epochs.
    validate_reason_counts(signal["decode_error_counts"], "decode", DECODE_CODES, cached)
    require(count(signal["hourly_sampled_epochs_accepted"], "hourly_accepted") <= plan["plan"]["hourly_epochs"],
            "invalid_hourly_acceptance")
    events = signal["events"]
    # summarize_screen serializes Counter objects: a never-observed early
    # event or split/label coverage category is omitted and means zero.
    events.setdefault("before_first_landmark", 0)
    total = count(events["qualifying_events"], "qualifying_events")
    early = count(events["before_first_landmark"], "early_events")
    require(early < total, "no_eligible_events")
    for key in EVENT_KEYS:
        require(count(events[key], key) <= total - early, "invalid_event_coverage")
    require(events["full_6h_accepted_epochs"] <= events["at_least_3h_accepted_epochs"] <=
            events["at_least_1h_accepted_epochs"] <= events["any_accepted_history_epoch"], "inconsistent_event_coverage")
    current_events = current_event_counts_by_split(signal["event_coverage_by_split"])
    require(sum(current_events.values()) == events["any_current_hourly_epoch_accepted"],
            "split_current_event_count_mismatch")
    current_total = 0
    for split in ("train", "validation", "test"):
        coverage = signal["riskset_coverage_by_split"][split]
        for label in ("positive", "negative", "censored"):
            coverage.setdefault("current_epoch_accepted_" + label, 0)
            coverage.setdefault("all_" + label, 0)
            require(count(coverage["current_epoch_accepted_" + label], "accepted_windows") <=
                    count(coverage["all_" + label], "all_windows"), "invalid_window_coverage")
            current_total += coverage["current_epoch_accepted_" + label]
    require(current_total <= signal["hourly_sampled_epochs_accepted"] <= signal["accepted_epochs"],
            "inconsistent_hourly_coverage")
    development = read_json(work / "models/development_report.json")
    comparison = read_json(work / "paired_comparison.json")
    require(development["stage"] == "development_validation_only_test_unopened", "development_stage_mismatch")
    require(comparison["stage"] == "paired_development_validation_only_test_unopened"
            and comparison["status"] == "paired_comparison_complete", "complete_paired_comparison_required")
    source = development["source_fingerprint"]
    with closing(readonly(work / "model_table.sqlite")) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    require(source == json.loads(metadata["source_fingerprint"])
            and development["model_table"] == json.loads(metadata["report"]), "model_table_provenance_mismatch")
    require(source["rr_ready"] is True and development["model_table"]["rr_ready"] is True
            and source["signal"]["status"] == "complete_verified", "verified_rr_models_required")
    require(source["signal"]["fingerprint"] == fingerprint and source["signal"]["screened_epochs"] == cached
            and source["signal"]["summary_sha256"] == file_hash(work / "signal_summary.json"), "model_signal_provenance_mismatch")
    require(source["patient_split_sha256"] == file_hash(work / "patient_split.json"), "patient_split_changed")
    require(development["model_version"] == comparison["model_version"] == source["model_version"] == MODEL_VERSION,
            "model_version_mismatch")
    require(comparison["comparison_version"] == COMPARISON_VERSION, "comparison_version_mismatch")
    require(comparison["provenance"]["source_fingerprint_sha256"] == object_hash(source), "comparison_source_mismatch")
    require(comparison["baseline"] == BASELINE and set(comparison["comparisons"]) == set(PAIRED[1:])
            and set(comparison["absolute_point_estimates"]) == set(PAIRED), "paired_variants_mismatch")
    require(comparison["row_policy"] == "identical_validation_current_rr_accepted_binary_rows_for_all_models",
            "comparison_population_mismatch")
    require(set(comparison["provenance"]["model_artifact_sha256"]) == set(PAIRED), "model_hash_set_mismatch")
    for name in PAIRED:
        require(comparison["provenance"]["model_artifact_sha256"][name] == file_hash(work / "models" / (name + ".joblib")),
                "comparison_model_artifact_changed")
    models, full = development["models"], development["full_development_riskset"]
    require(set(models) == set(ALL_MODELS), "five_model_variants_required")
    require(models["vitals_all"]["data"] == full, "full_cohort_baseline_population_mismatch")
    for split in ("train", "validation"):
        require(current_events[split] == models[BASELINE]["data"][split]["distinct_events_with_positive_window"],
                "model_signal_event_count_mismatch")
        for label in ("positive", "negative", "censored"):
            coverage = signal["riskset_coverage_by_split"][split]
            require(full[split]["label_counts"][label] == coverage["all_" + label]
                    and models[BASELINE]["data"][split]["label_counts"][label] == coverage["current_epoch_accepted_" + label],
                    "model_signal_population_count_mismatch")
    for name in ALL_MODELS:
        model = models[name]
        require(model["status"] == "development_validation_complete", "all_five_models_must_be_complete")
        require(tuple(model["features"]) == tuple(VARIANTS[name]), "model_features_mismatch")
        for split in ("train", "validation"):
            validate_stats(model["data"][split])
        if name in PAIRED:
            require(model["data"] == models[BASELINE]["data"], "paired_model_populations_differ")
        stats, values = model["data"]["validation"], model["validation"]
        require(values["binary_windows"] == stats["binary_rows"], "validation_row_count_mismatch")
        same_number(values["prevalence"], stats["label_counts"]["positive"] / stats["binary_rows"], "prevalence")
        validate_bootstrap(model["validation_patient_bootstrap"])
        require(model["validation_patient_bootstrap"]["distinct_validation_patients"] == stats["distinct_binary_patients"],
                "individual_bootstrap_patient_count_mismatch")
        for metric in METRICS:
            number(values[metric], metric, 0, 1)
            interval(model["validation_patient_bootstrap"]["intervals"][metric], metric)
            if name in PAIRED:
                same_number(values[metric], comparison["absolute_point_estimates"][name][metric], "comparison_metric")
        validate_alarms(model["validation_alarms"], stats, full["validation"])
    samples, stats = comparison["samples"], models[BASELINE]["data"]["validation"]
    require(samples["binary_windows"] == stats["binary_rows"]
            and samples["positive_windows"] == stats["label_counts"]["positive"]
            and samples["negative_windows"] == stats["label_counts"]["negative"]
            and samples["patients"] == stats["distinct_binary_patients"]
            and samples["positive_event_stays"] == stats["distinct_events_with_positive_window"], "paired_sample_count_mismatch")
    validate_bootstrap(comparison["bootstrap"], paired=True)
    require(comparison["bootstrap"]["patient_count"] == samples["patients"], "bootstrap_patient_count_mismatch")
    for name in PAIRED[1:]:
        for metric in METRICS:
            expected = models[name]["validation"][metric] - models[BASELINE]["validation"][metric]
            if metric == "Brier":
                expected = -expected
            same_number(comparison["comparisons"][name]["delta_point_estimate"][metric], expected, "paired_delta")
            interval(comparison["comparisons"][name]["delta_confidence_interval_95"][metric], "delta_" + metric, -1)
            expected_direction = "baseline_minus_candidate" if metric == "Brier" else "candidate_minus_baseline"
            require(comparison["metric_direction"][metric] == expected_direction, "paired_delta_direction_mismatch")
    return signal, development, comparison


def ratio(numerator, denominator):
    return f"{numerator:,} / {denominator:,}（{100 * numerator / denominator:.2f}%）" if denominator else "不適用（分母為 0）"


def estimate(value, bounds, *, scientific=False):
    precision = ".3e" if scientific else ".5f"
    return f"{value:{precision}} [{bounds[0]:{precision}}, {bounds[1]:{precision}}]"


def markdown_report(signal, development, comparison):
    models = development["models"]
    events = signal["events"]
    eligible = events["qualifying_events"] - events["before_first_landmark"]
    lines = ["# M2 開發驗證彙總（待團隊審閱）", "", "完成狀態、彙總數量與模型來源指紋已核對；以下只呈現彙總，保留測試未開封。", "",
             "## 訊號與事件覆蓋", "", "| 項目 | 通過／分母 |", "|---|---:|",
             f"| 全部計畫五分鐘片段通過工程篩選 | {ratio(signal['accepted_epochs'], signal['screened_epochs'])} |",
             f"| 逐小時採樣五分鐘片段通過 | {ratio(signal['hourly_sampled_epochs_accepted'], signal['plan']['hourly_epochs'])} |",
             f"| 首個六小時預測點之前或當下事件（無預警機會） | {ratio(events['before_first_landmark'], events['qualifying_events'])} |"]
    for label, key in zip(("至少一個歷史片段通過", "歷史合格片段累計至少 1 小時", "歷史合格片段累計至少 3 小時",
                           "歷史完整 6 小時片段通過", "至少一個陽性逐小時更新點當下片段通過"), EVENT_KEYS):
        lines.append(f"| {label} | {ratio(events[key], eligible)} |")
    lines += ["", f"事件覆蓋分母為 {eligible:,} 個至少有一次合法陽性預測點的事件；全部首次事件為 {events['qualifying_events']:,}。"
              "歷史累計片段數不表示連續可用時間。密集事件稽核片段不進入模型。", "",
              "| 分組／時窗結局 | 當下片段通過／完整風險時窗 |", "|---|---:|"]
    for split, label in (("train", "訓練"), ("validation", "開發驗證"), ("test", "保留測試（僅覆蓋計數）")):
        data = signal["riskset_coverage_by_split"][split]
        for outcome, title in (("positive", "陽性"), ("negative", "陰性"), ("censored", "截尾")):
            lines.append(f"| {label}／{title} | {ratio(data['current_epoch_accepted_' + outcome], data['all_' + outcome])} |")
    lines += ["", "## 工程拒絕原因與解碼問題", "",
              f"工程拒絕片段總數為 {signal['screened_epochs'] - signal['accepted_epochs']:,}。"
              "下列拒絕原因非互斥，同一片段可有多項原因，不能將各列相加當作片段總數；"
              "`ecg_decode_unavailable` 與 `detector_error` 也可能反映技術問題，不能全稱為臨床訊號品質不良。", "",
              "| 工程拒絕原因代碼 | 出現該原因的片段數 |", "|---|---:|"]
    reasons = signal["rejection_reason_counts"]
    for code, total in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| `{code}` | {total:,} |")
    if not reasons:
        lines.append("| 無已記錄拒絕原因 | 0 |")
    lines += ["", "解碼錯誤另表列出，代表資料、格式、通道或讀取的技術問題，不能歸為臨床品質差。"
              "每個代碼在單一片段內只計一次；不同代碼仍可重疊，不能相加當作失敗片段總數。"
              "其他重疊紀錄可能補足訊號，因此有解碼錯誤代碼的片段仍可能通過工程篩選。", "",
              "| 解碼錯誤代碼 | 出現該錯誤的片段數 |", "|---|---:|"]
    errors = signal["decode_error_counts"]
    for code, total in sorted(errors.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| `{code}` | {total:,} |")
    if not errors:
        lines.append("| 無已記錄解碼錯誤 | 0 |")
    lines += ["", "## 相同 RR 通過時窗的四組配對模型", "",
              f"驗證患者 {comparison['samples']['patients']:,} 位、非截尾時窗 {comparison['samples']['binary_windows']:,} 個、"
              f"陽性事件 {comparison['samples']['positive_event_stays']:,} 個；陽性時窗比例 {models[BASELINE]['validation']['prevalence']:.5f}。", "",
              "| 模型 | AUROC [95% CI] | AUPRC [95% CI] | Brier [95% CI] |", "|---|---:|---:|---:|"]
    for name in PAIRED:
        model = models[name]
        lines.append("| " + LABELS[name] + " | " + " | ".join(estimate(model["validation"][metric],
                     model["validation_patient_bootstrap"]["intervals"][metric]) for metric in METRICS) + " |")
    lines += ["", "![配對模型 AUPRC 與相同族群盛行率](paired_auprc.svg)", "",
              "## 相對配對常規模型的差值", "", "三項正值均代表改善：AUROC／AUPRC 為加入 RR 減常規模型；Brier 為常規模型減加入 RR。", "",
              "| 加入 RR 的模型 | ΔAUROC [95% CI] | ΔAUPRC [95% CI] | Brier 改善 [95% CI] |", "|---|---:|---:|---:|"]
    for name in PAIRED[1:]:
        value = comparison["comparisons"][name]
        lines.append("| " + LABELS[name] + " | " + " | ".join(estimate(value["delta_point_estimate"][metric],
                     value["delta_confidence_interval_95"][metric], scientific=metric == "Brier") for metric in METRICS) + " |")
    lines += ["", "![三組配對增量與區間](paired_delta.svg)", "", "## 全風險集合生命徵象基準（族群不同，獨立呈現）", ""]
    full_model = models["vitals_all"]
    full_stats = full_model["data"]["validation"]
    lines += [f"驗證患者 {full_stats['distinct_binary_patients']:,} 位、非截尾時窗 {full_stats['binary_rows']:,} 個；"
              f"陽性時窗比例 {full_model['validation']['prevalence']:.5f}。不能與上述配對模型直接相減宣稱 RR 增益。", "",
              "| 指標 | 估計值 [95% CI] |", "|---|---:|"]
    for metric in METRICS:
        lines.append(f"| {metric} | {estimate(full_model['validation'][metric], full_model['validation_patient_bootstrap']['intervals'][metric])} |")
    a = full_model["validation_alarms"]
    leads = "無抓到事件，不適用" if not a["events_caught"] else f"{a['lead_time_hours_median']:.3f} / {a['lead_time_hours_mean']:.3f} 小時"
    lines += ["", "| 全風險集合基準警報項目 | 數值及分母 |", "|---|---:|",
              f"| 抓到／已評分事件 | {ratio(a['events_caught'], a['scored_events'])} |",
              f"| 抓到／完整風險事件 | {ratio(a['events_caught'], a['full_riskset_events'])} |",
              f"| 訓練陰性第 99 百分位門檻 | {a['threshold']:.6f} |",
              f"| 抓到事件的提前中位／平均 | {leads} |",
              f"| 誤報數 | {a['false_notifications']:,} |",
              f"| 已評分觀察病人小時；每 100 小時誤報 | {a['observed_scored_patient_hours']:,.3f}；{a['false_notifications_per_100_observed_patient_hours']:.5f} |",
              f"| 結局可評估病人小時；每 100 小時誤報 | {a['evaluable_scored_patient_hours']:,.3f}；{a['false_notifications_per_100_evaluable_patient_hours']:.5f} |",
              f"| 全部／真陽性／結局不明通知 | {a['notifications']:,} / {a['positive_notifications']:,} / {a['censored_notifications']:,} |",
              f"| 六小時間隔抑制次數 | {a['refractory_suppressed_crossings']:,} |",
              f"| 已評分／完整風險時間（病人小時） | {a['observed_scored_patient_hours']:,.3f} / {a['full_riskset_observed_patient_hours']:,.3f}（{100*a['observed_time_coverage_against_full_riskset']:.2f}%） |"]
    lines += ["", "## 配對模型通知、事件與實際觀察時間", "", "每組門檻固定為其訓練陰性分數第 99 百分位，ICU 內六小時不重複通知。"
              "不同模型實際誤報負擔可能不同，因此下列敏感度不構成相同誤報率比較。", "",
              "| 模型 | 抓到／已評分事件 | 抓到／完整風險事件 | 門檻 | 提前中位／平均（小時） |", "|---|---:|---:|---:|---:|"]
    for name in PAIRED:
        a = models[name]["validation_alarms"]
        leads = "無抓到事件，不適用" if not a["events_caught"] else f"{a['lead_time_hours_median']:.3f} / {a['lead_time_hours_mean']:.3f}"
        lines.append(f"| {LABELS[name]} | {ratio(a['events_caught'], a['scored_events'])} | {ratio(a['events_caught'], a['full_riskset_events'])} | {a['threshold']:.6f} | {leads} |")
    lines += ["", "| 模型 | 誤報數 | 已評分觀察病人小時 | 誤報／100 觀察小時 | 結局可評估病人小時 | 誤報／100 可評估小時 |", "|---|---:|---:|---:|---:|---:|"]
    for name in PAIRED:
        a = models[name]["validation_alarms"]
        lines.append(f"| {LABELS[name]} | {a['false_notifications']:,} | {a['observed_scored_patient_hours']:,.3f} | {a['false_notifications_per_100_observed_patient_hours']:.5f} | {a['evaluable_scored_patient_hours']:,.3f} | {a['false_notifications_per_100_evaluable_patient_hours']:.5f} |")
    lines += ["", "| 模型 | 全部通知 | 真陽性通知 | 結局不明通知 | 間隔抑制次數 | 已評分時間／完整風險時間 |", "|---|---:|---:|---:|---:|---:|"]
    for name in PAIRED:
        a = models[name]["validation_alarms"]
        lines.append(f"| {LABELS[name]} | {a['notifications']:,} | {a['positive_notifications']:,} | {a['censored_notifications']:,} | {a['refractory_suppressed_crossings']:,} | {a['observed_scored_patient_hours']:,.3f} / {a['full_riskset_observed_patient_hours']:,.3f}（{100*a['observed_time_coverage_against_full_riskset']:.2f}%） |")
    boot = comparison["bootstrap"]
    lines += ["", "## 第一版限制", "",
              "- 預測的是已記錄的首次持續升壓劑開始使用，不能等同敗血性休克診斷、實際治療需求或一般病況惡化。不同 ICU 與藥物使用情境混合，需要臨床分層審查。",
              "- ICU 前六小時的事件無法由目前第一個預測點預警；品質不足時窗不出分，完整族群效益不能只看通過品質的配對分數。",
              "- 工程品質通過不代表正常 NN 心搏；尚未確認 AF、pacing、異位心搏或 R 波偵測的臨床準確率。候選 mean RR、SDRR、RMSSD of RR 尚不是經心律確認的 NN-HRV。",
              "- 本輪六例固定訓練樣本重現結果為 6／6 一致，但其中兩個工程接受樣本已觀察到 XQRS／GQRS 定位與 RMSSD 明顯不一致（62.22／5.75 ms、49.61／18.19 ms）。GQRS 不是標註真值，此小型分層抽查不能外推發生比例；目前工程通過不足以證實 HRV 可靠性，需另外驗證搏動定位與正常心律。",
              "- 模型使用六個逐小時的五分鐘片段，不是連續六小時 HRV；歷史斜率也可能只重表達既有資訊。",
              "- RR 模型包含 mean_rr_ms、rr_valid_count 與缺失指標；相對僅有當下生命徵象的基準，任何增益可能來自更即時的心率、歷史資訊或資料可用性，不能單獨歸因於 HRV 變異。下一版應另設同一 ECG 來源、相同歷史的平均心率控制組；本報告未新增模型或變更既有 variants。",
              "- 誤報分母只含實際已評分時窗的下一小時，按暴露／追蹤終止截短；品質拒絕時間不在配對誤報分母。結局不明通知不能當陰性。",
              f"- 配對差值為探索性共同患者 bootstrap：要求 {boot['replicates_requested']} 次、有效 {boot['replicates_valid']} 次、單一類別略過 {boot['replicates_skipped_single_class']} 次；不重訓，區間不含訓練不確定性，未校正三組多重比較。個別模型區間重疊不能代替配對差值分析。",
              "- 保留測試未開封；尚無外部驗證、相同實際誤報負擔下的確認性比較或臨床效益證據。沒有依本報告自動選勝出模型。", ""]
    for name in ALL_MODELS:
        b = models[name]["validation_patient_bootstrap"]
        lines.append(f"{LABELS[name]}個別區間：有效 {b['replicates_valid']}／{b['replicates_requested']} 次患者 bootstrap；收斂警告：{'有' if models[name]['convergence_warning'] else '無'}。")
        lines.append("")
    return "\n".join(lines)


def render_figures(development, comparison, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with plt.rc_context({"svg.hashsalt": "hrv-m2-aggregate-v1", "svg.fonttype": "none", "font.size": 10}):
        fig, axes = plt.subplots(1, 3, figsize=(12, 3.3), layout="constrained")
        for ax, metric in zip(axes, METRICS):
            for y, name in enumerate(PAIRED[1:]):
                values = comparison["comparisons"][name]
                low, high = values["delta_confidence_interval_95"][metric]
                ax.hlines(y, low, high, color="#1b6f8a", linewidth=2)
                ax.scatter(values["delta_point_estimate"][metric], y, color="#1b6f8a", s=32)
            ax.axvline(0, color="#555555", linewidth=1, linestyle="--")
            ax.set_yticks(range(3), PLOT_LABELS[1:] if metric == "AUROC" else [""] * 3)
            ax.invert_yaxis()
            ax.set_xlabel(metric + (" improvement" if metric == "Brier" else " difference"))
            ax.grid(axis="x", alpha=.2)
        fig.suptitle("Paired validation differences vs vitals (95% patient-bootstrap CI)\nPositive values favor added RR; exploratory, test unopened", fontsize=11)
        fig.savefig(output / "paired_delta.svg", metadata={"Date": None})
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(8, 3.6), layout="constrained")
        models = development["models"]
        for y, name in enumerate(PAIRED):
            low, high = models[name]["validation_patient_bootstrap"]["intervals"]["AUPRC"]
            ax.hlines(y, low, high, color="#1b6f8a", linewidth=2)
            ax.scatter(models[name]["validation"]["AUPRC"], y, color="#1b6f8a", s=32)
        ax.axvline(models[BASELINE]["validation"]["prevalence"], color="#b54c2c", linestyle="--", label="Positive-window prevalence")
        ax.set_yticks(range(4), PLOT_LABELS)
        ax.invert_yaxis()
        ax.set_xlabel("AUPRC (average precision), individual 95% patient-bootstrap CI")
        ax.set_xlim(left=0)
        ax.set_title("Identical RR-accepted validation windows; test unopened")
        ax.grid(axis="x", alpha=.2)
        fig.legend(loc="outside upper right", fontsize=8)
        fig.savefig(output / "paired_auprc.svg", metadata={"Date": None})
        plt.close(fig)


def render_report(work_dir):
    signal, development, comparison = load_validated(work_dir)
    document = markdown_report(signal, development, comparison)
    output = REPORT_DIRECTORY
    require(not any(path.is_symlink() for path in (output, *output.parents)), "report_directory_must_not_be_symlink")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".m2_report_", dir=output.parent) as temporary:
        stage = Path(temporary)
        (stage / "summary.md").write_text(document, encoding="utf-8")
        render_figures(development, comparison, stage)
        for path in stage.glob("*.svg"):
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n", encoding="utf-8")
        output.mkdir(exist_ok=True)
        for name in ("summary.md", "paired_delta.svg", "paired_auprc.svg"):
            (stage / name).replace(output / name)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    args = parser.parse_args(argv)
    try:
        output = render_report(args.work_dir)
    except (OSError, ValueError, KeyError, TypeError, IndexError, sqlite3.Error, ImportError) as error:
        # Error class/reason only: malformed patient files must never be echoed.
        reason = str(error) if isinstance(error, ValueError) and str(error).replace("_", "").isalnum() else type(error).__name__
        print("Aggregate report refused: " + reason, file=sys.stderr)
        return 2
    print("Aggregate review draft saved to " + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
