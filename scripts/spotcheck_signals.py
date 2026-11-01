"""Re-decode six frozen training-only epochs and compare the saved decisions.

This is a reproducibility spot check, not annotated QRS accuracy or clinical
rhythm validation. The first run freezes three accepted and three decoded but
rejected epochs from distinct training ICU stays. All identifiers, timestamps,
waveform plots and detailed outputs remain under ignored reports/local/.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tomllib

import numpy as np

from hrv_early_warning.ecg import decode_ecg_epoch
from hrv_early_warning.risksets import load_stay_outcomes
from hrv_early_warning.signal_quality import QualityConfig, _detect_qrs, assess_ecg_epoch
from hrv_early_warning.waveform_index import _restore_record
from hrv_early_warning.waveforms import overlap_with_icu


SEED = 20261101


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detector_timing_diagnostics(primary, secondary, fs, tolerance_seconds):
    """Describe timing disagreements, never label either detector as truth."""
    pairs = []
    i = j = 0
    while i < len(primary) and j < len(secondary):
        delta = int(primary[i]) - int(secondary[j])
        if abs(delta) <= tolerance_seconds * fs:
            pairs.append((i, j))
            i += 1
            j += 1
        elif delta < 0:
            i += 1
        else:
            j += 1
    if not pairs:
        return {"matched_beats": 0}
    pairs = np.asarray(pairs, dtype=int)
    p, s = primary[pairs[:, 0]], secondary[pairs[:, 1]]
    offsets_ms = (p - s) * 1000.0 / fs
    result = {
        "matched_beats": len(pairs),
        "primary_minus_secondary_offset_ms_quantiles_0_5_50_95_100": np.percentile(offsets_ms, [0, 5, 50, 95, 100]).tolist(),
        "primary_minus_secondary_offset_ms_histogram": {str(offset): count for offset, count in sorted(Counter(offsets_ms.tolist()).items())},
        "scope": "Detector disagreement only; neither detector is annotated truth. RMSSD below uses shared originally adjacent matched triplets without model RR screening, not replacement model features.",
    }
    if len(pairs) >= 3:
        adjacent = np.all(np.diff(pairs, axis=0) == 1, axis=1)
        triplets = adjacent[:-1] & adjacent[1:]
        dp = np.diff(p, n=2)[triplets] * 1000.0 / fs
        ds = np.diff(s, n=2)[triplets] * 1000.0 / fs
        offset_component = np.diff(offsets_ms, n=2)[triplets]
        result["shared_original_adjacent_triplets"] = int(triplets.sum())
        if len(dp):
            result.update({
                "shared_triplet_primary_rmssd_ms": float(np.sqrt(np.mean(dp ** 2))),
                "shared_triplet_secondary_rmssd_ms": float(np.sqrt(np.mean(ds ** 2))),
                "timing_offset_second_difference_rms_ms": float(np.sqrt(np.mean(offset_component ** 2))),
                "timing_identity_max_absolute_error_ms": float(np.max(np.abs(dp - ds - offset_component))),
            })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    parser.add_argument("--feasibility-dir", type=Path, default=Path("artifacts/feasibility"))
    parser.add_argument("--quality", type=Path, default=Path("configs/signal_quality.toml"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/local/spotcheck"))
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    if not output.is_relative_to(repository / "reports/local"):
        raise ValueError("spotcheck_outputs_must_remain_in_private_reports_local")
    output.mkdir(parents=True, exist_ok=True)
    split_path = args.work_dir / "patient_split.json"
    split = json.loads(split_path.read_text())["assignments"]
    stays, _ = load_stay_outcomes(args.feasibility_dir / "stay_outcomes.jsonl")
    train = {stay.icustay_id: stay for stay in stays if split[str(stay.subject_id)] == "train"}
    with args.quality.open("rb") as stream:
        config = QualityConfig(**tomllib.load(stream))
    connection = sqlite3.connect((args.work_dir / "epochs.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        fingerprint = json.loads(connection.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()[0])
        for name, expected in fingerprint["code"].items():
            if _sha256(repository / "src/hrv_early_warning" / name) != expected:
                raise ValueError("frozen_signal_code_changed")
        from dataclasses import asdict
        if asdict(config) != fingerprint["config"]:
            raise ValueError("frozen_signal_configuration_changed")
        if _sha256(args.feasibility_dir / "stay_outcomes.jsonl") != fingerprint["stay_outcomes_sha256"]:
            raise ValueError("stay_outcomes_changed")
        manifest_path = output / "selection.private.json"
        provenance = {"seed": SEED, "split_sha256": _sha256(split_path), "cache_fingerprint": fingerprint}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest["provenance"] != provenance:
                raise ValueError("spotcheck_manifest_provenance_changed")
        else:
            # TEMP tables reside in this connection; the source DB is read-only.
            connection.execute("CREATE TEMP TABLE train_icus (icustay_id INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO train_icus VALUES (?)", [(icu,) for icu in train])
            candidates = defaultdict(list)
            query = ("SELECT e.icustay_id,e.endtime,e.accepted FROM epochs e JOIN train_icus t "
                     "ON e.icustay_id=t.icustay_id WHERE e.accepted=1 OR "
                     "json_extract(e.result,'$.decode_status') IN ('ok','partial')")
            for icu, end, accepted in connection.execute(query):
                rank = hashlib.sha256(f"{SEED}:{icu}:{end}".encode()).hexdigest()
                candidates[bool(accepted)].append((rank, icu, end))
            selected, selected_icus = [], set()
            for accepted in (True, False):
                count = 0
                for _, icu, end in sorted(candidates[accepted]):
                    if icu in selected_icus:
                        continue
                    selected.append({"icustay_id": icu, "endtime": end, "accepted": accepted})
                    selected_icus.add(icu)
                    count += 1
                    if count == 3:
                        break
                if count != 3:
                    raise ValueError("insufficient_distinct_training_stays_for_spotcheck")
            manifest = {"provenance": provenance, "selection": selected,
                        "scope": "Three accepted and three decoded rejected training epochs, one per ICU; not representative or accuracy validation"}
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        samples = []
        for sample in manifest["selection"]:
            icu, end = sample["icustay_id"], sample["endtime"]
            if icu not in train:
                raise ValueError("nontraining_epoch_in_spotcheck_manifest")
            row = connection.execute("SELECT result FROM epochs WHERE icustay_id=? AND endtime=?", (icu, end)).fetchone()
            if row is None:
                raise ValueError("frozen_spotcheck_epoch_missing")
            cached = json.loads(row[0])
            if cached["accepted"] != sample["accepted"]:
                raise ValueError("frozen_spotcheck_decision_changed")
            samples.append((train[icu], datetime.fromisoformat(end), cached))
    finally:
        connection.close()

    subjects = {stay.subject_id for stay, _, _ in samples}
    records = defaultdict(list)
    metadata_path = args.feasibility_dir / "waveform_metadata.jsonl"
    if _sha256(metadata_path) != fingerprint["waveform_metadata_sha256"]:
        raise ValueError("waveform_metadata_changed")
    # Stream the cache, restoring only selected subjects rather than all records.
    with metadata_path.open() as stream:
        header = json.loads(next(stream))
        waveform_root = Path(header["fingerprint"]["waveform_root"])
        for line in stream:
            item = json.loads(line)
            if item["type"] == "record" and item["record"]["subject_id"] in subjects:
                record = _restore_record(item["record"], waveform_root, subjects)
                records[record.subject_id].append(record)

    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    checks, reason_counts, rates, units = [], Counter(), Counter(), Counter()
    for index, (stay, end, cached) in enumerate(samples, start=1):
        start = end - timedelta(minutes=5)
        same_icu = [record for record in records[stay.subject_id]
                    if overlap_with_icu(record, stay.subject_id, stay.intime, stay.followup_end)]
        decoded = decode_ecg_epoch(same_icu, start, end)
        result = {"accepted": False, "decode_status": decoded.status,
                  "decode_errors": list(decoded.errors), "coverage_fraction": decoded.coverage_fraction,
                  "reasons": ["ecg_decode_unavailable"], "features": {}, "metrics": {}}
        if decoded.fs is not None and decoded.status in {"ok", "partial"}:
            assessed = assess_ecg_epoch(decoded.signal, decoded.fs, config)
            result.update(accepted=assessed.accepted, reasons=list(assessed.reasons),
                          features=assessed.features, metrics=assessed.metrics)
        mismatches = [key for key, value in result.items() if value != cached[key]]
        check = {"sample": index, "split": "train", "exact_cached_result_match": not mismatches,
                 "mismatched_fields": mismatches, "fs_hz": decoded.fs,
                 "sample_count": len(decoded.signal), "missing_samples": decoded.missing_samples,
                 "signal_units": decoded.meta.get("signal_units"), **result}
        checks.append(check)
        reason_counts.update(result["reasons"])
        rates[str(decoded.fs)] += 1
        units[str(decoded.meta.get("signal_units"))] += 1
        if decoded.fs is not None and len(decoded.signal):
            # Detector marks are diagnostic displays, not independent annotations.
            peaks = (np.array([], dtype=int), np.array([], dtype=int))
            if "primary_beat_count" in result["metrics"]:
                peaks = _detect_qrs(decoded.signal, decoded.fs)
                check["detector_timing_diagnostics"] = _detector_timing_diagnostics(*peaks, decoded.fs, config.match_tolerance_seconds)
            seconds = np.arange(len(decoded.signal)) / decoded.fs
            fig, axes = plt.subplots(2, 1, figsize=(14, 6), constrained_layout=True)
            axes[0].plot(seconds, decoded.signal, linewidth=0.35)
            axes[0].set(xlabel="Seconds within epoch", ylabel="Lead II (mV)", xlim=(0, 300))
            zoom = (seconds >= 120) & (seconds < 130)
            axes[1].plot(seconds[zoom], decoded.signal[zoom], linewidth=0.8, color="black")
            for points, label, marker, color in zip(peaks, ("XQRS", "GQRS"), ("o", "x"), ("tab:blue", "tab:orange")):
                points = points[(points / decoded.fs >= 120) & (points / decoded.fs < 130)]
                axes[1].scatter(points / decoded.fs, decoded.signal[points], s=30, marker=marker, color=color, label=label)
            axes[1].set(xlabel="Seconds within epoch", ylabel="Lead II (mV)", xlim=(120, 130))
            if len(peaks[0]) or len(peaks[1]):
                axes[1].legend(loc="upper right")
            else:
                axes[1].text(0.99, 0.95, "Rejected before beat detection", transform=axes[1].transAxes, ha="right", va="top")
            fig.suptitle(f"Private training sample {index}: {'accepted' if result['accepted'] else 'rejected'} | "
                         + (", ".join(result["reasons"]) or "engineering screen passed")
                         + "\nDetector comparison only; rhythm and normal beats are not adjudicated")
            fig.savefig(output / f"sample_{index:02d}.png", dpi=130)
            plt.close(fig)
        print(f"Training spotcheck {index}/{len(samples)}: cache match={not mismatches}", flush=True)
    report = {"scope": manifest["scope"], "seed": SEED, "selected_epochs": len(checks),
              "accepted_epochs": sum(check["accepted"] for check in checks),
              "exact_cached_result_matches": sum(check["exact_cached_result_match"] for check in checks),
              "sampling_rate_counts": dict(rates), "output_unit_counts": dict(units),
              "selected_rejection_reason_counts": dict(reason_counts), "checks": checks,
              "limitations": "Stratified small cached training sample; no QRS annotations, no clinical rhythm/NN adjudication, no parameter tuning; not an estimate of acceptance rate or sensitivity/specificity"}
    (output / "spotcheck_summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    if any(not check["exact_cached_result_match"] for check in checks):
        raise RuntimeError("spotcheck_cached_result_mismatch")


if __name__ == "__main__":
    main()
