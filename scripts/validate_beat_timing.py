"""Run the fixed external annotation pilot or six frozen training diagnostics.

Never reads prediction features/scores or writes v1 signal/model artifacts.
All generated outputs stay in ignored rr_validation or private local reports.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tomllib

import numpy as np
import wfdb
from wfdb.processing import xqrs_detect

from download_reference_ecg import validate_plan
from hrv_early_warning.beat_timing import (
    align_fiducials, evaluate_timing, provenance, quantization_reference,
    resample_for_timing, rr_features,
)


REPOSITORY = Path(__file__).resolve().parents[1]
# WFDB labels describing actual beats; excludes QRS-like artifact '|',
# nonconducted P 'x', pacer spike '^', flutter-wave '!' and learning '?'.
BEAT_SYMBOLS = frozenset("NLRaVFJASEj/QBenfr")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_directory(path, *, private=False):
    path = Path(path).resolve()
    allowed = [REPOSITORY / "reports/local"]
    if not private:
        allowed.append(REPOSITORY / "artifacts/rr_validation")
    if not any(path.is_relative_to(root) for root in allowed):
        raise ValueError("timing_outputs_must_remain_in_ignored_validation_directories")
    path.mkdir(parents=True, exist_ok=True)
    return path


def checked_manifest(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != "beat_timing_reference_manifest_v1":
        raise ValueError("unsupported_reference_manifest_version")
    plan = manifest["plan"]
    validate_plan(plan)
    if manifest.get("plan_sha256") != sha256(REPOSITORY / "configs/beat_timing_reference.json"):
        raise ValueError("reference_plan_hash_changed")
    if plan != json.loads((REPOSITORY / "configs/beat_timing_reference.json").read_text()):
        raise ValueError("embedded_reference_plan_changed")
    required = {f"{row['record']}.{extension}" for row in plan["records"] for extension in ("hea", "dat", "atr")}
    entries = manifest["files"]
    if len(entries) != len(required) or {entry["path"] for entry in entries} != required:
        raise ValueError("reference_manifest_file_set_mismatch")
    for entry in entries:
        file = (manifest_path.parent / entry["path"]).resolve()
        if not file.is_relative_to(manifest_path.parent):
            raise ValueError("reference_file_outside_data_root")
        if entry.get("source_url") != plan["download_base"] + entry["path"]:
            raise ValueError("reference_file_not_from_official_source")
        if file.stat().st_size != entry["bytes"] or sha256(file) != entry["sha256"]:
            raise ValueError("reference_file_hash_or_size_mismatch")
    return manifest


def benchmark(manifest_path, output_path):
    manifest = checked_manifest(manifest_path)
    output = output_directory(output_path)
    if (output / "benchmark_summary.json").exists():
        raise FileExistsError("benchmark_output_exists_use_new_directory")
    data_root = Path(manifest_path).resolve().parent
    plan = manifest["plan"]
    report = {"schema_version": "beat_timing_benchmark_v1", "candidate": provenance(),
              "adapter_sha256": sha256(__file__),
              "protocol_sha256": sha256(REPOSITORY / "docs/beat_timing_validation.md"),
              "wfdb_version": wfdb.__version__, "manifest_sha256": sha256(manifest_path),
              "source_manifest": manifest, "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "External expert-reviewed annotation engineering pilot; method development only, not ICU validation or sinus NN qualification",
              "acceptance_status": "No clinical thresholds or heldout signal partition locked",
              "records": [], "windows": []}
    for item in plan["records"]:
        record_path = str(data_root / item["record"])
        header = wfdb.rdheader(record_path)
        if getattr(header, "n_seg", None) is not None:
            raise ValueError("pilot_requires_single_segment_wfdb_records")
        # Prevent a modified WFDB header from referencing unverified local files.
        if set(header.file_name) != {item["record"] + ".dat"}:
            raise ValueError("unverified_waveform_file_in_reference_header")
        record = wfdb.rdrecord(record_path, channels=[item["channel"]])
        annotations = wfdb.rdann(record_path, item["annotator"])
        if annotations.fs is not None and not np.isclose(annotations.fs, record.fs):
            raise ValueError("annotation_sampling_rate_disagrees_with_record")
        if np.any(annotations.sample < 0) or np.any(annotations.sample >= record.sig_len):
            raise ValueError("annotation_outside_record")
        beat_mask = np.array([symbol in BEAT_SYMBOLS for symbol in annotations.symbol])
        reference_seconds = annotations.sample[beat_mask] / float(record.fs)
        beat_symbols = np.asarray(annotations.symbol)[beat_mask]
        if np.any(np.diff(reference_seconds) <= 0):
            raise ValueError("reference_beats_not_strictly_ordered")
        epoch_samples = int(round(300 * record.fs))
        count = record.sig_len // epoch_samples
        if count == 0:
            raise ValueError("reference_record_has_no_complete_five_minute_window")
        record_summary = {**item, "original_fs_hz": record.fs, "channel_name": record.sig_name[0],
                          "units": record.units[0], "sample_count": record.sig_len,
                          "duration_seconds": record.sig_len / record.fs, "planned_five_minute_windows": count,
                          "ignored_tail_seconds": (record.sig_len % epoch_samples) / record.fs,
                          "annotation_symbol_counts": dict(Counter(annotations.symbol)),
                          "rhythm_annotation_notes": dict(Counter(note.strip() for symbol, note in zip(annotations.symbol, annotations.aux_note) if symbol == "+")),
                          "evaluated_windows_by_fs": {}, "failed_windows_by_fs": {},
                          "full_candidate_windows_by_fs": {}}
        for epoch in range(count):
            start, end = epoch * 300, (epoch + 1) * 300
            included = (reference_seconds >= start) & (reference_seconds < end)
            reference = reference_seconds[included] - start
            raw = record.p_signal[epoch * epoch_samples:(epoch + 1) * epoch_samples, 0]
            for fs in dict.fromkeys([float(record.fs), 125.0]):
                row = {"record": item["record"], "subject_group": item["subject_group"],
                       "window_index": epoch, "start_seconds": start, "duration_seconds": 300,
                       "fs_hz": fs, "reference_beat_count": len(reference),
                       "reference_beat_symbol_counts": dict(Counter(beat_symbols[included].tolist())),
                       "nn_qualification": "unknown_not_inferred_from_N_labels",
                       "reference_grid_quantization": quantization_reference(reference, fs)}
                key = str(fs)
                try:
                    if not np.all(np.isfinite(raw)):
                        raise ValueError("nonfinite_reference_signal")
                    signal = raw if fs == record.fs else resample_for_timing(raw, record.fs, fs)
                    seeds = xqrs_detect(signal, fs, verbose=False)
                    candidate = align_fiducials(signal, fs, seeds)
                    full_candidate = bool(len(seeds) and np.all(candidate.valid))
                    row["candidate"] = candidate.summary()
                    row["baseline_xqrs"] = evaluate_timing(reference, seeds / fs,
                        duration_seconds=300, tolerance_seconds=plan["match_tolerance_seconds"])
                    row["consistent_fiducial"] = evaluate_timing(reference, candidate.indices[candidate.valid] / fs,
                        detected_ids=np.flatnonzero(candidate.valid),
                        full_detected_available=full_candidate, duration_seconds=300,
                        tolerance_seconds=plan["match_tolerance_seconds"])
                    row["status"] = "evaluated"
                    record_summary["evaluated_windows_by_fs"][key] = record_summary["evaluated_windows_by_fs"].get(key, 0) + 1
                    record_summary["full_candidate_windows_by_fs"][key] = record_summary["full_candidate_windows_by_fs"].get(key, 0) + int(full_candidate)
                except (ValueError, IndexError, RuntimeError) as error:
                    row.update(status="failed", error=f"{type(error).__name__}: {error}")
                    record_summary["failed_windows_by_fs"][key] = record_summary["failed_windows_by_fs"].get(key, 0) + 1
                report["windows"].append(row)
        report["records"].append(record_summary)
        print(f"Annotation pilot {item['record']}: {record_summary['evaluated_windows_by_fs']} evaluated; {record_summary['failed_windows_by_fs']} failed", flush=True)
    report["window_status_counts"] = dict(Counter(row["status"] for row in report["windows"]))
    report["complete"] = all(row["status"] == "evaluated" for row in report["windows"])
    (output / "benchmark_summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    if not report["complete"]:
        raise RuntimeError("annotation_pilot_contains_failed_windows_see_report")


def spotcheck(output_path):
    """Use the original six training cases; never reseed or replace cases."""
    from hrv_early_warning.ecg import decode_ecg_epoch
    from hrv_early_warning.risksets import load_stay_outcomes
    from hrv_early_warning.signal_quality import QualityConfig, _detect_qrs
    from hrv_early_warning.waveform_index import _restore_record
    from hrv_early_warning.waveforms import overlap_with_icu

    output = output_directory(output_path, private=True)
    if (output / "training_diagnostic_summary.json").exists():
        raise FileExistsError("training_diagnostic_exists_use_new_directory")
    work = REPOSITORY / "artifacts/m2"
    feasibility = REPOSITORY / "artifacts/feasibility"
    selection_path = REPOSITORY / "reports/local/spotcheck/selection.private.json"
    manifest = json.loads(selection_path.read_text())
    split_path = work / "patient_split.json"
    assignments = json.loads(split_path.read_text())["assignments"]
    stays, _ = load_stay_outcomes(feasibility / "stay_outcomes.jsonl")
    training = {stay.icustay_id: stay for stay in stays if assignments[str(stay.subject_id)] == "train"}
    with (REPOSITORY / "configs/signal_quality.toml").open("rb") as stream:
        quality = QualityConfig(**tomllib.load(stream))
    connection = sqlite3.connect((work / "epochs.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        fingerprint = json.loads(connection.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()[0])
        expected_provenance = {"seed": 20261101, "split_sha256": sha256(split_path), "cache_fingerprint": fingerprint}
        if manifest["provenance"] != expected_provenance or len(manifest["selection"]) != 6:
            raise ValueError("fixed_spotcheck_selection_provenance_changed")
        if asdict(quality) != fingerprint["config"]:
            raise ValueError("frozen_signal_configuration_changed")
        for name, expected in fingerprint["code"].items():
            if sha256(REPOSITORY / "src/hrv_early_warning" / name) != expected:
                raise ValueError("frozen_signal_code_changed")
        if sha256(feasibility / "stay_outcomes.jsonl") != fingerprint["stay_outcomes_sha256"]:
            raise ValueError("stay_outcomes_changed")
        samples = []
        for sample in manifest["selection"]:
            icu = sample["icustay_id"]
            if icu not in training:
                raise ValueError("nontraining_epoch_in_spotcheck_manifest")
            row = connection.execute("SELECT result FROM epochs WHERE icustay_id=? AND endtime=?", (icu, sample["endtime"])).fetchone()
            if row is None:
                raise ValueError("frozen_spotcheck_epoch_missing")
            cached = json.loads(row[0])
            if cached["accepted"] != sample["accepted"]:
                raise ValueError("frozen_spotcheck_decision_changed")
            samples.append((training[icu], datetime.fromisoformat(sample["endtime"]), cached))
    finally:
        connection.close()
    subjects = {stay.subject_id for stay, _, _ in samples}
    metadata_path = feasibility / "waveform_metadata.jsonl"
    if sha256(metadata_path) != fingerprint["waveform_metadata_sha256"]:
        raise ValueError("waveform_metadata_changed")
    records = defaultdict(list)
    with metadata_path.open() as stream:
        root = Path(json.loads(next(stream))["fingerprint"]["waveform_root"])
        for line in stream:
            item = json.loads(line)
            if item["type"] == "record" and item["record"]["subject_id"] in subjects:
                record = _restore_record(item["record"], root, subjects)
                records[record.subject_id].append(record)
    report = {"schema_version": "beat_timing_training_diagnostic_v1", "candidate": provenance(),
              "wfdb_version": wfdb.__version__, "selection_sha256": sha256(selection_path),
              "split_sha256": sha256(split_path), "fixed_v1_cache_fingerprint": fingerprint,
              "scope": "Six previously inspected training epochs, not independent annotations; GQRS is another detector, never timing truth; no threshold tuning or model evaluation",
              "checks": []}
    for index, (stay, end, cached) in enumerate(samples, start=1):
        same_icu = [record for record in records[stay.subject_id] if overlap_with_icu(record, stay.subject_id, stay.intime, stay.followup_end)]
        decoded = decode_ecg_epoch(same_icu, end - timedelta(minutes=5), end)
        row = {"sample": index, "split": "train", "frozen_v1_accepted": cached["accepted"],
               "frozen_v1_reasons": cached["reasons"], "fs_hz": decoded.fs,
               "decode_status": decoded.status, "missing_samples": decoded.missing_samples}
        if decoded.fs is None or not np.all(np.isfinite(decoded.signal)):
            row.update(status="not_evaluable", reason="missing_or_nonfinite_signal_not_imputed")
        elif "primary_beat_count" not in cached["metrics"]:
            row.update(status="not_evaluable", reason="v1_rejected_before_beat_detection_not_overridden")
        else:
            seeds, other = _detect_qrs(decoded.signal, decoded.fs)
            candidate = align_fiducials(decoded.signal, decoded.fs, seeds)
            row.update(status="diagnostic_only", candidate=candidate.summary(),
                       baseline_xqrs_rr=rr_features(seeds / decoded.fs),
                       same_seed_adjacent_xqrs_rr=rr_features(seeds[candidate.valid] / decoded.fs, np.flatnonzero(candidate.valid)),
                       gqrs_rr_not_truth=rr_features(other / decoded.fs),
                       candidate_adjacent_rr=rr_features(candidate.indices[candidate.valid] / decoded.fs, np.flatnonzero(candidate.valid)))
            row["xqrs_vs_gqrs_disagreement_not_accuracy"] = evaluate_timing(other / decoded.fs, seeds / decoded.fs, duration_seconds=300)
            row["candidate_vs_gqrs_disagreement_not_accuracy"] = evaluate_timing(other / decoded.fs,
                candidate.indices[candidate.valid] / decoded.fs, detected_ids=np.flatnonzero(candidate.valid),
                full_detected_available=bool(np.all(candidate.valid)), duration_seconds=300)
        report["checks"].append(row)
        print(f"Fixed training diagnostic {index}/6: {row['status']}", flush=True)
    report["status_counts"] = dict(Counter(row["status"] for row in report["checks"]))
    (output / "training_diagnostic_summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check-data", "benchmark"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        if name == "benchmark":
            command.add_argument("--output-dir", type=Path, default=Path("artifacts/rr_validation/pilot"))
    command = commands.add_parser("spotcheck")
    command.add_argument("--output-dir", type=Path, default=Path("reports/local/beat_timing_v2"))
    args = parser.parse_args()
    if args.command == "check-data":
        manifest = checked_manifest(args.manifest)
        print(json.dumps({"status": "hashes_and_plan_verified", "files": len(manifest["files"]), "records": len(manifest["plan"]["records"])}))
    elif args.command == "benchmark":
        benchmark(args.manifest, args.output_dir)
    else:
        spotcheck(args.output_dir)


if __name__ == "__main__":
    main()
