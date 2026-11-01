"""Small aggregate fixtures: publishing guards never need patient feature rows."""

from contextlib import closing, redirect_stderr
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/render_m2_report.py"
SPEC = importlib.util.spec_from_file_location("render_m2_report", MODULE_PATH)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class RenderReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "models").mkdir()
        self.output = self.root / "reports/m2_publishable"
        self.sentinel = "PRIVATE_PATIENT_981234_AT_2199-08-03T09:01:00"
        (self.work / "patient_split.json").write_text(json.dumps({self.sentinel: "test"}))
        self.fingerprint = {"config": {"agreement": .9}, "versions": {"wfdb": "fixture"}}
        plan = {"unique_epochs": 30, "hourly_epochs": 28, "event_audit_epochs": 3, "stays_with_planned_epochs": 3}
        self.signal = {
            "complete": True, "screened_epochs": 30, "accepted_epochs": 24,
            "hourly_sampled_epochs_accepted": 24, "plan": plan, **self.fingerprint,
            "rejection_reason_counts": {"flat_run": 4, "low_block_amplitude": 4, "ecg_decode_unavailable": 2},
            "decode_error_counts": {"lead_ii_absent": 8, "wfdb_decode_error": 2},
            "events": {"qualifying_events": 5, "before_first_landmark": 2,
                       "any_accepted_history_epoch": 3, "at_least_1h_accepted_epochs": 2,
                       "at_least_3h_accepted_epochs": 1, "full_6h_accepted_epochs": 0,
                       "any_current_hourly_epoch_accepted": 2},
            "event_coverage_by_split": {
                "train": {"any_current_hourly_epoch_accepted": 1},
                "validation": {"any_current_hourly_epoch_accepted": 1},
                "test": {},
            },
            "riskset_coverage_by_split": {split: {
                "all_positive": 2, "all_negative": 8, "all_censored": 2,
                "current_epoch_accepted_positive": 1, "current_epoch_accepted_negative": 3,
                "current_epoch_accepted_censored": 1} for split in ("train", "validation", "test")},
            "unexpected_private_field": self.sentinel,
        }
        self.save("signal_summary.json", self.signal)
        self.save("signal_plan.json", {"plan": plan, "fingerprint": self.fingerprint})
        with closing(sqlite3.connect(self.work / "epochs.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE epochs(accepted INTEGER)")
            connection.executemany("INSERT INTO epochs VALUES(?)", [(1,)] * 24 + [(0,)] * 6)
            connection.execute("CREATE TABLE metadata(key TEXT,value TEXT)")
            connection.execute("INSERT INTO metadata VALUES('fingerprint',?)", (json.dumps(self.fingerprint),))
        self.source = {"rr_ready": True, "model_version": renderer.MODEL_VERSION,
                       "patient_split_sha256": renderer.file_hash(self.work / "patient_split.json"),
                       "signal": {"status": "complete_verified", "screened_epochs": 30,
                                  "fingerprint": self.fingerprint,
                                  "summary_sha256": renderer.file_hash(self.work / "signal_summary.json")}}
        full_stats = {"rows": 12, "binary_rows": 10, "distinct_patients": 3, "distinct_binary_patients": 3,
                      "distinct_events_with_positive_window": 2, "observed_patient_hours": 11.5,
                      "label_counts": {"positive": 2, "negative": 8, "censored": 2}}
        paired_stats = {"rows": 5, "binary_rows": 4, "distinct_patients": 2, "distinct_binary_patients": 2,
                        "distinct_events_with_positive_window": 1, "observed_patient_hours": 4.5,
                        "label_counts": {"positive": 1, "negative": 3, "censored": 1}}
        models = {}
        artifact_hashes = {}
        for index, name in enumerate(renderer.ALL_MODELS):
            stats = full_stats if name == "vitals_all" else paired_stats
            values = {"AUROC": .6 + index * .02, "AUPRC": .3 + index * .01, "Brier": .2 - index * .005}
            values.update(binary_windows=stats["binary_rows"],
                          prevalence=stats["label_counts"]["positive"] / stats["binary_rows"])
            hours, evaluable = stats["observed_patient_hours"], stats["observed_patient_hours"] - 1
            alarms = {"notifications": 3, "positive_notifications": 1, "false_notifications": 1,
                      "censored_notifications": 1, "refractory_suppressed_crossings": 2,
                      "scored_events": stats["distinct_events_with_positive_window"], "full_riskset_events": 2,
                      "events_caught": 1, "event_sensitivity_among_scored_events": 1 / stats["distinct_events_with_positive_window"],
                      "event_sensitivity_full_riskset": .5, "threshold": .3, "refractory_hours": 6,
                      "observed_scored_patient_hours": hours, "evaluable_scored_patient_hours": evaluable,
                      "false_notifications_per_100_observed_patient_hours": 100 / hours,
                      "false_notifications_per_100_evaluable_patient_hours": 100 / evaluable,
                      "full_riskset_observed_patient_hours": 11.5, "observed_time_coverage_against_full_riskset": hours / 11.5,
                      "lead_time_hours_mean": 2, "lead_time_hours_median": 2}
            bootstrap = {"replicates_requested": 100, "replicates_valid": 90,
                         "replicates_skipped_single_class_or_empty": 10, "confidence_level": .95,
                         "seed": renderer.BOOTSTRAP_SEED, "distinct_validation_patients": stats["distinct_binary_patients"],
                         "method": "patient_cluster_percentile_bootstrap_fixed_predictions_no_refit",
                         "intervals": {metric: [values[metric] - .02, values[metric] + .02] for metric in renderer.METRICS}}
            models[name] = {"status": "development_validation_complete", "data": {"train": copy.deepcopy(stats), "validation": copy.deepcopy(stats)},
                            "features": list(renderer.VARIANTS[name]),
                            "validation": values, "validation_patient_bootstrap": bootstrap,
                            "validation_alarms": alarms, "convergence_warning": False, "private": self.sentinel}
            artifact = self.work / "models" / (name + ".joblib")
            artifact.write_bytes(b"synthetic bytes: never deserialize")
            if name in renderer.PAIRED:
                artifact_hashes[name] = renderer.file_hash(artifact)
        self.development = {"stage": "development_validation_only_test_unopened", "model_version": renderer.MODEL_VERSION,
                            "source_fingerprint": self.source, "model_table": {"rr_ready": True}, "models": models,
                            "full_development_riskset": {"train": full_stats, "validation": full_stats}}
        self.save("models/development_report.json", self.development)
        with closing(sqlite3.connect(self.work / "model_table.sqlite")) as connection, connection:
            # Deliberately NO model_rows table. The renderer can only use metadata.
            connection.execute("CREATE TABLE metadata(key TEXT,value TEXT)")
            connection.executemany("INSERT INTO metadata VALUES(?,?)", [
                ("source_fingerprint", json.dumps(self.source)), ("report", json.dumps({"rr_ready": True}))])
        comparisons = {}
        for name in renderer.PAIRED[1:]:
            deltas = {metric: (models[name]["validation"][metric] - models[renderer.BASELINE]["validation"][metric])
                      * (-1 if metric == "Brier" else 1) for metric in renderer.METRICS}
            comparisons[name] = {"delta_point_estimate": deltas,
                                 "delta_confidence_interval_95": {metric: [value - .03, value + .03] for metric, value in deltas.items()}}
        self.comparison = {"stage": "paired_development_validation_only_test_unopened", "status": "paired_comparison_complete",
                           "model_version": renderer.MODEL_VERSION, "comparison_version": renderer.COMPARISON_VERSION,
                           "baseline": renderer.BASELINE,
                           "row_policy": "identical_validation_current_rr_accepted_binary_rows_for_all_models",
                           "provenance": {"source_fingerprint_sha256": renderer.object_hash(self.source), "model_artifact_sha256": artifact_hashes},
                           "absolute_point_estimates": {name: {metric: models[name]["validation"][metric] for metric in renderer.METRICS}
                                                        for name in renderer.PAIRED},
                           "comparisons": comparisons, "metric_direction": {"AUROC": "candidate_minus_baseline", "AUPRC": "candidate_minus_baseline", "Brier": "baseline_minus_candidate"},
                           "samples": {"binary_windows": 4, "positive_windows": 1, "negative_windows": 3, "patients": 2, "positive_event_stays": 1},
                           "bootstrap": {"method": "shared_patient_cluster_percentile_fixed_predictions_no_refit", "confidence_level": .95,
                                         "replicates_requested": 200, "replicates_valid": 180, "replicates_skipped_single_class": 20,
                                         "seed": renderer.BOOTSTRAP_SEED, "patient_count": 2}}
        self.save("paired_comparison.json", self.comparison)

    def save(self, name, value):
        (self.work / name).write_text(json.dumps(value), encoding="utf-8")

    def render(self):
        with patch.object(renderer, "REPORT_DIRECTORY", self.output):
            return renderer.render_report(self.work)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib required for SVG rendering")
    def test_complete_fixture_renders_only_whitelisted_aggregates_and_deterministic_svgs(self):
        self.render()
        names = {p.name for p in self.output.iterdir()}
        self.assertEqual(names, {"summary.md", "paired_delta.svg", "paired_auprc.svg"})
        first = {p.name: p.read_bytes() for p in self.output.iterdir()}
        self.render()
        self.assertEqual(first, {p.name: p.read_bytes() for p in self.output.iterdir()})
        content = "\n".join(p.read_text() for p in self.output.iterdir())
        for secret in (self.sentinel, "2199-08-03", "synthetic bytes", str(self.work), "<dc:date>"):
            self.assertNotIn(secret, content)
        self.assertIn("1 / 1（100.00%）", content)
        self.assertIn("1 / 2（50.00%）", content)
        self.assertIn("NN-HRV", content)
        self.assertIn("不同模型實際誤報負擔", content)
        self.assertIn("族群不同", content)
        self.assertIn("6／6 一致", content)
        self.assertIn("GQRS 不是標註真值", content)
        self.assertIn("不能外推發生比例", content)
        self.assertIn("工程拒絕片段總數為 6", content)
        self.assertIn("| `flat_run` | 4 |", content)
        self.assertIn("| `lead_ii_absent` | 8 |", content)
        self.assertIn("拒絕原因非互斥", content)
        self.assertIn("不能歸為臨床品質差", content)
        self.assertIn("仍可能通過工程篩選", content)

    def refresh_signal_hash(self):
        self.save("signal_summary.json", self.signal)
        self.source["signal"]["summary_sha256"] = renderer.file_hash(self.work / "signal_summary.json")
        self.save("models/development_report.json", self.development)
        with closing(sqlite3.connect(self.work / "model_table.sqlite")) as connection, connection:
            connection.execute("UPDATE metadata SET value=? WHERE key='source_fingerprint'", (json.dumps(self.source),))
        self.comparison["provenance"]["source_fingerprint_sha256"] = renderer.object_hash(self.source)
        self.save("paired_comparison.json", self.comparison)

    def test_sparse_counter_zero_coverage_is_valid_and_rendered_as_zero(self):
        # The production summarizer omits Counter keys never incremented.
        del self.signal["riskset_coverage_by_split"]["test"]["current_epoch_accepted_positive"]
        del self.signal["riskset_coverage_by_split"]["test"]["all_censored"]
        del self.signal["riskset_coverage_by_split"]["test"]["current_epoch_accepted_censored"]
        self.refresh_signal_hash()
        signal, development, comparison = renderer.load_validated(self.work)
        report = renderer.markdown_report(signal, development, comparison)
        self.assertIn("保留測試（僅覆蓋計數）／陽性 | 0 / 2（0.00%）", report)
        self.assertIn("保留測試（僅覆蓋計數）／截尾 | 不適用（分母為 0）", report)

    def test_no_early_events_may_be_omitted_by_counter(self):
        del self.signal["events"]["before_first_landmark"]
        self.refresh_signal_hash()
        signal, development, comparison = renderer.load_validated(self.work)
        self.assertEqual(signal["events"]["before_first_landmark"], 0)
        self.assertIn("0 / 5（0.00%）", renderer.markdown_report(signal, development, comparison))

    def test_empty_reason_counters_are_valid_only_without_rejected_epochs(self):
        self.signal["accepted_epochs"] = self.signal["screened_epochs"]
        with closing(sqlite3.connect(self.work / "epochs.sqlite")) as connection, connection:
            connection.execute("UPDATE epochs SET accepted=1")
        self.signal["rejection_reason_counts"] = {}
        self.signal["decode_error_counts"] = {}
        self.refresh_signal_hash()
        signal, development, comparison = renderer.load_validated(self.work)
        report = renderer.markdown_report(signal, development, comparison)
        self.assertIn("無已記錄拒絕原因 | 0", report)
        self.assertIn("無已記錄解碼錯誤 | 0", report)

    def test_empty_decode_counter_is_valid_with_quality_rejections(self):
        self.signal["rejection_reason_counts"] = {"flat_run": 4, "low_block_amplitude": 4}
        self.signal["decode_error_counts"] = {}
        self.refresh_signal_hash()
        signal, development, comparison = renderer.load_validated(self.work)
        report = renderer.markdown_report(signal, development, comparison)
        self.assertIn("工程拒絕片段總數為 6", report)
        self.assertIn("無已記錄解碼錯誤 | 0", report)

    def test_rejected_epochs_require_sufficient_aggregate_reasons(self):
        for reasons in ({}, {"flat_run": 5}):
            self.signal["rejection_reason_counts"] = reasons
            self.refresh_signal_hash()
            with self.subTest(reasons=reasons), self.assertRaisesRegex(ValueError, "rejection_reason_counts_incomplete"):
                self.render()
            self.assertFalse(self.output.exists())

    def test_invalid_reason_counts_and_unreviewed_codes_refused(self):
        original = copy.deepcopy(self.signal)
        for field, code, maximum in (("rejection_reason_counts", "flat_run", 6),
                                     ("decode_error_counts", "lead_ii_absent", 30)):
            for value in (-1, True, 1.5, "2", float("nan"), maximum + 1):
                self.signal = copy.deepcopy(original)
                self.signal[field] = {code: value}
                self.refresh_signal_hash()
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.render()
                self.assertFalse(self.output.exists())
            for invalid in ([], None, {self.sentinel: 1}):
                self.signal = copy.deepcopy(original)
                self.signal[field] = invalid
                self.refresh_signal_hash()
                error = io.StringIO()
                with self.subTest(field=field, value=invalid), redirect_stderr(error):
                    result = renderer.main(["--work-dir", str(self.work)])
                self.assertEqual(result, 2)
                self.assertNotIn(self.sentinel, error.getvalue())
                self.assertFalse(self.output.exists())

    def test_event_coverage_is_cross_checked_for_each_development_split(self):
        original = copy.deepcopy(self.signal)
        for split in ("train", "validation"):
            for value in (0, 2):
                self.signal = copy.deepcopy(original)
                self.signal["event_coverage_by_split"][split]["any_current_hourly_epoch_accepted"] = value
                self.signal["events"]["any_current_hourly_epoch_accepted"] = value + 1
                self.refresh_signal_hash()
                with self.subTest(split=split, value=value), self.assertRaisesRegex(ValueError, "model_signal_event_count_mismatch"):
                    self.render()
                self.assertFalse(self.output.exists())
            self.signal = copy.deepcopy(original)
            del self.signal["event_coverage_by_split"][split]["any_current_hourly_epoch_accepted"]
            self.signal["events"]["any_current_hourly_epoch_accepted"] = 1
            self.refresh_signal_hash()
            # An omitted Counter key means zero, which still cannot agree with
            # this fixture's one scored event. It must not silently pass.
            with self.subTest(split=split, omitted=True), self.assertRaisesRegex(ValueError, "model_signal_event_count_mismatch"):
                self.render()

    def test_split_current_event_counts_must_sum_to_all_event_coverage(self):
        self.signal["event_coverage_by_split"]["test"]["any_current_hourly_epoch_accepted"] = 1
        self.refresh_signal_hash()
        with self.assertRaisesRegex(ValueError, "split_current_event_count_mismatch"):
            self.render()
        self.assertFalse(self.output.exists())

    def test_sparse_split_event_counters_normalize_only_legal_zeros(self):
        self.assertEqual(renderer.current_event_counts_by_split({"train": {}}),
                         {"train": 0, "validation": 0, "test": 0})
        for invalid in (-1, True, 1.5, "1", None):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                renderer.current_event_counts_by_split({"validation": {"any_current_hourly_epoch_accepted": invalid}})
        for invalid in (None, [], {"train": None}):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                renderer.current_event_counts_by_split(invalid)

    def test_unknown_comparison_version_and_changed_feature_order_refused(self):
        self.comparison["comparison_version"] = "unreviewed_version"
        self.save("paired_comparison.json", self.comparison)
        with self.assertRaisesRegex(ValueError, "comparison_version_mismatch"):
            self.render()
        self.comparison["comparison_version"] = renderer.COMPARISON_VERSION
        self.save("paired_comparison.json", self.comparison)
        self.development["models"]["paired_current_rr"]["features"].reverse()
        self.save("models/development_report.json", self.development)
        with self.assertRaisesRegex(ValueError, "model_features_mismatch"):
            self.render()
        self.assertFalse(self.output.exists())

    def test_bootstrap_seed_replicates_and_patient_count_match_frozen_analysis(self):
        for key, value, reason in (("seed", 9, "seed"), ("replicates_requested", 101, "incomplete_bootstrap"),
                                  ("distinct_validation_patients", 9, "patient_count")):
            changed = copy.deepcopy(self.development)
            changed["models"]["paired_current_rr"]["validation_patient_bootstrap"][key] = value
            self.save("models/development_report.json", changed)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, reason):
                self.render()
        self.save("models/development_report.json", self.development)
        self.comparison["bootstrap"].update(replicates_requested=100, replicates_valid=100, replicates_skipped_single_class=0)
        self.save("paired_comparison.json", self.comparison)
        with self.assertRaisesRegex(ValueError, "bootstrap_replicates_mismatch"):
            self.render()
        self.assertFalse(self.output.exists())

    def test_incomplete_signal_or_count_mismatch_never_writes(self):
        for key, value in (("complete", False), ("screened_epochs", 29), ("accepted_epochs", 23)):
            changed = copy.deepcopy(self.signal)
            changed[key] = value
            self.save("signal_summary.json", changed)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.render()
            self.assertFalse(self.output.exists())

    def test_signal_plan_fingerprint_mismatch_refused(self):
        self.save("signal_plan.json", {"plan": self.signal["plan"], "fingerprint": {"changed": True}})
        with self.assertRaisesRegex(ValueError, "signal_metadata_mismatch"):
            self.render()

    def test_pending_model_or_comparison_refused(self):
        self.development["models"]["paired_current_rr"]["status"] = "pending_complete_signal_screen"
        self.save("models/development_report.json", self.development)
        with self.assertRaisesRegex(ValueError, "all_five_models"):
            self.render()
        self.development["models"]["paired_current_rr"]["status"] = "development_validation_complete"
        self.save("models/development_report.json", self.development)
        self.comparison["status"] = "insufficient_validation_classes"
        self.save("paired_comparison.json", self.comparison)
        with self.assertRaisesRegex(ValueError, "complete_paired_comparison"):
            self.render()
        self.assertFalse(self.output.exists())

    def test_source_fingerprint_and_model_hash_are_checked(self):
        self.comparison["provenance"]["source_fingerprint_sha256"] = "changed"
        self.save("paired_comparison.json", self.comparison)
        with self.assertRaisesRegex(ValueError, "comparison_source_mismatch"):
            self.render()
        self.comparison["provenance"]["source_fingerprint_sha256"] = renderer.object_hash(self.source)
        self.save("paired_comparison.json", self.comparison)
        (self.work / "models/paired_current_rr.joblib").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "artifact_changed"):
            self.render()

    def test_changed_split_manifest_and_model_table_metadata_refused(self):
        (self.work / "patient_split.json").write_text("changed")
        with self.assertRaisesRegex(ValueError, "patient_split_changed"):
            self.render()
        (self.work / "patient_split.json").write_text(json.dumps({self.sentinel: "test"}))
        with closing(sqlite3.connect(self.work / "model_table.sqlite")) as connection, connection:
            connection.execute("UPDATE metadata SET value='{}' WHERE key='source_fingerprint'")
        with self.assertRaisesRegex(ValueError, "model_table_provenance_mismatch"):
            self.render()

    def test_different_paired_population_or_metric_refused(self):
        changed = copy.deepcopy(self.development)
        changed["models"]["paired_current_rr"]["data"]["validation"]["observed_patient_hours"] = 4
        self.save("models/development_report.json", changed)
        with self.assertRaisesRegex(ValueError, "paired_model_populations_differ"):
            self.render()
        self.save("models/development_report.json", self.development)
        self.comparison["absolute_point_estimates"]["paired_current_rr"]["AUROC"] = .99
        self.save("paired_comparison.json", self.comparison)
        with self.assertRaisesRegex(ValueError, "comparison_metric"):
            self.render()

    def test_invalid_interval_missing_metric_and_alarm_denominator_refused(self):
        for mutate in (
            lambda m: m["validation_patient_bootstrap"]["intervals"].update(AUPRC=None),
            lambda m: m["validation"].update(AUROC=float("nan")),
            lambda m: m["validation_alarms"].update(observed_scored_patient_hours=100),
            lambda m: m["validation_alarms"].update(false_notifications_per_100_evaluable_patient_hours=0),
        ):
            changed = copy.deepcopy(self.development)
            mutate(changed["models"]["paired_current_rr"])
            self.save("models/development_report.json", changed)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.render()
        self.assertFalse(self.output.exists())

    def test_reversed_delta_direction_and_changed_delta_refused(self):
        self.comparison["metric_direction"]["Brier"] = "candidate_minus_baseline"
        self.save("paired_comparison.json", self.comparison)
        with self.assertRaisesRegex(ValueError, "paired_delta_direction"):
            self.render()
        self.comparison["metric_direction"]["Brier"] = "baseline_minus_candidate"
        self.comparison["comparisons"]["paired_current_rr"]["delta_point_estimate"]["Brier"] = -.5
        self.save("paired_comparison.json", self.comparison)
        with self.assertRaisesRegex(ValueError, "paired_delta"):
            self.render()

    def test_no_catches_shows_unavailable_lead_time_not_zero(self):
        for model in self.development["models"].values():
            a = model["validation_alarms"]
            a.update(events_caught=0, event_sensitivity_among_scored_events=0, event_sensitivity_full_riskset=0,
                     positive_notifications=0, notifications=2, lead_time_hours_mean=None, lead_time_hours_median=None)
        self.save("models/development_report.json", self.development)
        signal, development, comparison = renderer.load_validated(self.work)
        self.assertIn("無抓到事件，不適用", renderer.markdown_report(signal, development, comparison))

    def test_tiny_negative_brier_delta_interval_keeps_nonzero_upper_bound(self):
        value = self.comparison["comparisons"]["paired_current_rr"]
        value["delta_point_estimate"]["Brier"] = -7.648487050640994e-6
        value["delta_confidence_interval_95"]["Brier"] = [-1.4166152008175301e-5, -3.996413717774481e-7]
        report = renderer.markdown_report(self.signal, self.development, self.comparison)
        self.assertIn("-7.648e-06 [-1.417e-05, -3.996e-07]", report)
        delta_section = report.split("## 相對配對常規模型的差值", 1)[1].split("## 全風險集合", 1)[0]
        row = next(line for line in delta_section.splitlines() if line.startswith("| 配對：加入當下 RR |"))
        self.assertNotIn("-0.00000", row.split("|")[-2])
        # Absolute Brier and discrimination estimates retain their established
        # fixed-point display; only the Brier increment needs this precision.
        self.assertIn("0.19000 [0.17000, 0.21000]", report)
        self.assertIn("0.02000 [-0.01000, 0.05000]", report)

    def test_tiny_brier_delta_crossing_zero_keeps_both_interval_directions(self):
        value = self.comparison["comparisons"]["paired_history_rr"]
        value["delta_point_estimate"]["Brier"] = -2.3663146966196313e-7
        value["delta_confidence_interval_95"]["Brier"] = [-1.3438632691385495e-5, 1.1658152238444733e-5]
        report = renderer.markdown_report(self.signal, self.development, self.comparison)
        self.assertIn("-2.366e-07 [-1.344e-05, 1.166e-05]", report)
        delta_section = report.split("## 相對配對常規模型的差值", 1)[1].split("## 全風險集合", 1)[0]
        row = next(line for line in delta_section.splitlines() if line.startswith("| 配對：加入歷史 RR |"))
        self.assertNotIn("-0.00000", row.split("|")[-2])

    def test_refusal_preserves_existing_review_files_and_reports_safe_error(self):
        self.output.mkdir(parents=True)
        report = self.output / "summary.md"
        report.write_text("previous review")
        self.signal["complete"] = False
        self.save("signal_summary.json", self.signal)
        error = io.StringIO()
        with patch.object(renderer, "REPORT_DIRECTORY", self.output), redirect_stderr(error):
            code = renderer.main(["--work-dir", str(self.work)])
        self.assertEqual(code, 2)
        self.assertEqual(report.read_text(), "previous review")
        self.assertIn("complete_signal_summary_required", error.getvalue())
        self.assertNotIn(self.sentinel, error.getvalue())

    def test_symlink_output_directory_rejected(self):
        target = self.root / "target"
        target.mkdir()
        self.output.parent.mkdir()
        self.output.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must_not_be_symlink"):
            self.render()
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
