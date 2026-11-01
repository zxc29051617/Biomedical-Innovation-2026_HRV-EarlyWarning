#!/usr/bin/env python3
"""Follow up the frozen public ECG pilot on exactly shared reference support.

The same six records and unchanged methods are rerun. This is a descriptive,
post-pilot comparison, not new independent validation, tuning, or ICU analysis.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from hrv_early_warning.timing_comparison import aggregate_comparisons, compare_shared_support


ROOT = Path(__file__).resolve().parents[1]


def output_directory(path):
    path = Path(path).resolve()
    if not any(path.is_relative_to(root) for root in (ROOT / "artifacts/rr_validation", ROOT / "reports/local")):
        raise ValueError("paired_timing_output_must_remain_in_ignored_validation_directories")
    path.mkdir(parents=True, exist_ok=True)
    if (path / "paired_timing_summary.json").exists():
        raise FileExistsError("paired_timing_report_exists_use_new_directory")
    return path


def verify_frozen_detection(summary, previous):
    """Ensure this follow-up did not change either method's detection coverage."""
    expected = previous["by_sampling_rate"]
    actual = summary["by_sampling_rate"]
    if set(expected) != set(actual):
        raise ValueError("sampling_rates_disagree_with_original_pilot")
    for fs, result in actual.items():
        original = expected[fs]
        if (result["total_windows"] != original["total_windows"]
                or result["evaluated_windows"] != original["evaluated_windows"]
                or result["failed_windows"] != original["failed_windows"]
                or result["all_nonempty_seeds_retained_windows"] != original["full_candidate_windows"]):
            raise ValueError("window_coverage_changed_from_original_pilot")
        for name in ("baseline_xqrs", "consistent_fiducial"):
            for key, value in original["methods"][name]["detection_totals"].items():
                if result["detection"][name][key] != value:
                    raise ValueError("detection_counts_changed_from_original_pilot")


def benchmark(manifest_path, output_path):
    import numpy as np
    import wfdb
    from wfdb.processing import xqrs_detect
    from hrv_early_warning.beat_timing import align_fiducials, match_beats, provenance, resample_for_timing
    from validate_beat_timing import BEAT_SYMBOLS, checked_manifest, sha256

    manifest = checked_manifest(manifest_path)
    output = output_directory(output_path)
    data_root = Path(manifest_path).resolve().parent
    original_summary_path = ROOT / "docs/results/beat_timing_pilot_summary.json"
    previous = json.loads(original_summary_path.read_text())
    method = provenance()
    if (method["algorithm_version"] != previous["algorithm_version"]
            or method["metric_version"] != previous["metric_version"]):
        raise ValueError("method_version_changed_from_original_pilot")
    report = {
        "schema_version": "paired_reference_timing_report_v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Descriptive same-support follow-up after observing the original development pilot; not new independent validation or ICU/NN qualification",
        "candidate": method, "wfdb_version": wfdb.__version__, "source_manifest": manifest,
        "provenance_sha256": {"adapter": sha256(__file__), "comparison": sha256(ROOT / "src/hrv_early_warning/timing_comparison.py"),
                              "analysis_plan": sha256(ROOT / "docs/timing_comparison.md"), "manifest": sha256(manifest_path),
                              "original_pilot_summary": sha256(original_summary_path),
                              "original_pilot_adapter": sha256(ROOT / "scripts/validate_beat_timing.py")},
        "original_pilot_commit": previous["source_commit"],
        "unchanged_detection_counts_verified": False, "records": [], "windows": []}
    plan = manifest["plan"]
    for item in plan["records"]:
        path = str(data_root / item["record"])
        header = wfdb.rdheader(path)
        if getattr(header, "n_seg", None) is not None or set(header.file_name) != {item["record"] + ".dat"}:
            raise ValueError("reference_header_not_single_verified_data_file")
        record = wfdb.rdrecord(path, channels=[item["channel"]])
        annotations = wfdb.rdann(path, item["annotator"])
        if annotations.fs is not None and not np.isclose(annotations.fs, record.fs):
            raise ValueError("reference_annotation_sampling_rate_mismatch")
        if np.any(annotations.sample < 0) or np.any(annotations.sample >= record.sig_len):
            raise ValueError("reference_annotation_outside_record")
        mask = np.array([symbol in BEAT_SYMBOLS for symbol in annotations.symbol])
        reference_seconds = annotations.sample[mask] / float(record.fs)
        symbols = np.asarray(annotations.symbol)[mask]
        if np.any(np.diff(reference_seconds) <= 0):
            raise ValueError("reference_beats_not_strictly_ordered")
        epoch_samples = int(round(300 * record.fs))
        window_count = record.sig_len // epoch_samples
        if window_count == 0:
            raise ValueError("record_has_no_complete_five_minute_window")
        report["records"].append({**item, "original_fs_hz": record.fs, "channel_name": record.sig_name[0],
                                  "units": record.units[0], "planned_windows_per_sampling_rate": window_count,
                                  "ignored_tail_seconds": (record.sig_len % epoch_samples) / record.fs})
        for epoch in range(window_count):
            start, end = epoch * 300, (epoch + 1) * 300
            selected = (reference_seconds >= start) & (reference_seconds < end)
            reference = reference_seconds[selected] - start
            raw = record.p_signal[epoch * epoch_samples:(epoch + 1) * epoch_samples, 0]
            for fs in dict.fromkeys([float(record.fs), 125.0]):
                row = {"record": item["record"], "window_index": epoch, "fs_hz": fs,
                       "reference_beat_count": len(reference),
                       "reference_beat_symbol_counts": dict(Counter(symbols[selected].tolist()))}
                try:
                    if not np.all(np.isfinite(raw)):
                        raise ValueError("nonfinite_reference_signal")
                    signal = raw if fs == record.fs else resample_for_timing(raw, record.fs, fs)
                    seeds = xqrs_detect(signal, fs, verbose=False)
                    candidate = align_fiducials(signal, fs, seeds)
                    baseline_times = seeds / fs
                    candidate_times = candidate.indices[candidate.valid] / fs
                    baseline_matches = match_beats(reference, baseline_times, plan["match_tolerance_seconds"])
                    candidate_matches = match_beats(reference, candidate_times, plan["match_tolerance_seconds"])
                    row["comparison"] = compare_shared_support(
                        reference.tolist(), baseline_times.tolist(), candidate_times.tolist(),
                        baseline_matches.tolist(), candidate_matches.tolist(),
                        candidate_ids=np.flatnonzero(candidate.valid).tolist(),
                        tolerance_seconds=plan["match_tolerance_seconds"], duration_seconds=300)
                    row["candidate"] = candidate.summary()
                    row["status"] = "evaluated"
                except (ValueError, IndexError, RuntimeError) as error:
                    row.update(status="failed", error=f"{type(error).__name__}: {error}")
                report["windows"].append(row)
        counts = Counter(row["status"] for row in report["windows"] if row["record"] == item["record"])
        print(f"Same-support pilot {item['record']}: {dict(counts)}", flush=True)
    report["window_status_counts"] = dict(Counter(row["status"] for row in report["windows"]))
    report["complete"] = all(row["status"] == "evaluated" for row in report["windows"])
    report["compact_summary"] = aggregate_comparisons(report["windows"])
    report["per_record_summary"] = {
        item["record"]: aggregate_comparisons([row for row in report["windows"] if row["record"] == item["record"]])
        for item in plan["records"]}
    if report["complete"]:
        try:
            verify_frozen_detection(report["compact_summary"], previous)
            report["unchanged_detection_counts_verified"] = True
        except ValueError as error:
            report["coverage_reproduction_error"] = str(error)
    with (output / "paired_timing_summary.json").open("x") as stream:
        stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print("PAIRED_TIMING_SUMMARY_JSON=" + json.dumps({**report["compact_summary"],
          "unchanged_detection_counts_verified": report["unchanged_detection_counts_verified"]},
          separators=(",", ":"), allow_nan=False), flush=True)
    if not report["complete"] or not report["unchanged_detection_counts_verified"]:
        raise RuntimeError("paired_timing_incomplete_or_detection_not_reproduced_see_report")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/rr_validation/paired"))
    args = parser.parse_args()
    benchmark(args.manifest, args.output_dir)


if __name__ == "__main__":
    main()
