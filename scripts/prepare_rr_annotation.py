#!/usr/bin/env python3
"""Freeze 20 training-only ICU annotation selections; do not decode waveforms.

Private identities and quality strata stay in an ignored, owner-only manifest.
The separate reviewer template contains no outcomes, quality decisions, detector
points or identifiable clocks. This is selection preparation, not an ECG package.
"""
from contextlib import closing
from datetime import datetime, timedelta
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hrv_early_warning.replay import _readonly, _validate_metadata, _write_private

VERSION = "training_rr_annotation_selection_v1"
SEED = 20261101
PER_GROUP = 10
OUTPUT_ROOT = ROOT / "artifacts/rr_annotation"
PRIOR_SELECTION = ROOT / "reports/local/spotcheck/selection.private.json"


def hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stat_signature(path):
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def rank(purpose, *parts):
    return hashlib.sha256(json.dumps([SEED, purpose, *parts], separators=(",", ":")).encode()).digest()


def excluded_patients(connection, prior, assignments, expected_split_hash, expected_cache_fingerprint):
    if (prior.get("provenance") != {"seed": SEED, "split_sha256": expected_split_hash,
                                    "cache_fingerprint": expected_cache_fingerprint}
            or len(prior.get("selection", [])) != 6):
        raise ValueError("annotation_prior_spotcheck_provenance_mismatch")
    excluded = set()
    for item in prior["selection"]:
        rows = list(connection.execute(
            "SELECT DISTINCT subject_id,split FROM model_rows WHERE icustay_id=?", (item["icustay_id"],)))
        if len(rows) != 1 or rows[0]["split"] != "train" or assignments.get(str(rows[0]["subject_id"])) != "train":
            raise ValueError("annotation_prior_spotcheck_is_not_unique_training_identity")
        cached = connection.execute("SELECT accepted FROM signals.epochs WHERE icustay_id=? AND endtime=?",
                                    (item["icustay_id"], item["endtime"])).fetchone()
        if cached is None or cached["accepted"] not in (0, 1) or bool(cached["accepted"]) is not item["accepted"]:
            raise ValueError("annotation_prior_spotcheck_cache_decision_mismatch")
        excluded.add(rows[0]["subject_id"])
    if len(excluded) != 6:
        raise ValueError("annotation_expected_six_distinct_previously_seen_patients")
    return excluded


def select_cases(connection, assignments, excluded):
    """Patient-first hash selection, without event/feature/score columns.

Choose ten accepted patients first, then ten rejected patients among the
remaining identities. For each group/patient retain the minimum-ranked exact
hourly epoch. This fixed order is a workflow pilot quota, not random prevalence.
"""
    pools = {True: {}, False: {}}
    pool_hash = hashlib.sha256()
    scanned, excluded_rows = 0, 0
    if connection.execute("""SELECT count(*) FROM model_rows m
            WHERE m.split='train' AND m.current_rr_accepted=1
            AND NOT EXISTS (SELECT 1 FROM signals.epochs e
                            WHERE e.icustay_id=m.icustay_id AND e.endtime=m.landmark)""").fetchone()[0]:
        raise ValueError("annotation_accepted_current_epoch_missing")
    cursor = connection.execute("""SELECT m.subject_id,m.hadm_id,m.icustay_id,m.landmark,
        m.current_rr_accepted,e.accepted AS epoch_accepted
        FROM model_rows m JOIN signals.epochs e
        ON m.icustay_id=e.icustay_id AND m.landmark=e.endtime
        WHERE m.split='train'
        ORDER BY m.subject_id,m.icustay_id,m.landmark""")
    for row in cursor:
        scanned += 1
        subject = row["subject_id"]
        if assignments.get(str(subject)) != "train":
            raise ValueError("annotation_train_identity_disagrees_with_fixed_split")
        decision = row["current_rr_accepted"]
        if decision not in (0, 1) or row["epoch_accepted"] != decision:
            raise ValueError("annotation_exact_epoch_acceptance_mismatch")
        if subject in excluded:
            excluded_rows += 1
            continue
        accepted = bool(decision)
        item = {"subject_id": subject, "hadm_id": row["hadm_id"], "icustay_id": row["icustay_id"],
                "endtime": row["landmark"], "v1_accepted": accepted}
        # Parsing verifies an actual clock without exposing it in messages.
        end = datetime.fromisoformat(item["endtime"])
        item["starttime"] = (end - timedelta(seconds=300)).isoformat()
        pool_hash.update((json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode())
        group = "accepted" if accepted else "rejected"
        key = (rank("epoch", group, subject, item["icustay_id"], item["endtime"]),
               item["icustay_id"], item["endtime"])
        previous = pools[accepted].get(subject)
        if previous is None or key < previous[0]:
            pools[accepted][subject] = key, item
    chosen, used = [], set()
    for accepted in (True, False):
        group = "accepted" if accepted else "rejected"
        ranked = sorted((subject for subject in pools[accepted] if subject not in used),
                        key=lambda subject: (rank("patient", group, subject), subject))
        if len(ranked) < PER_GROUP:
            raise ValueError("annotation_insufficient_" + group + "_training_patients_after_fixed_exclusions")
        for subject in ranked[:PER_GROUP]:
            chosen.append(dict(pools[accepted][subject][1]))
            used.add(subject)
    # Mixing is independent of group, outcomes, models and waveform appearance.
    chosen.sort(key=lambda item: (rank("blind_order", item["subject_id"], item["icustay_id"], item["endtime"]),
                                 item["subject_id"], item["icustay_id"], item["endtime"]))
    for index, item in enumerate(chosen, start=1):
        item["clip_code"] = f"RR-A{index:03d}"
    return chosen, {"train_exact_current_epochs_examined": scanned,
                    "rows_excluded_for_previously_seen_patients": excluded_rows,
                    "eligible_patient_counts_nonexclusive": {"accepted": len(pools[True]), "rejected": len(pools[False])},
                    "candidate_pool_sha256": pool_hash.hexdigest()}


def reviewer_template(selected):
    """No source IDs, original dates, acceptance groups or detector annotations."""
    def empty_review():
        return {"reviewer_code": None, "annotation_version": None,
                "signal_intervals": [], "beats": [], "rhythm_intervals": [],
                "nn_eligible_intervals": [], "notes": ""}
    return {
        "schema_version": "rr_independent_review_template_v1",
        "waveform_package_ready": False,
        "instructions": "Selection only. Waveforms have not been decoded. Distribute separate private copies to independent reviewers; do not reveal the private selection manifest or the other reviewer's annotations.",
        "clips": [{"clip_code": item["clip_code"], "duration_seconds": 300,
                   "requested_lead": "II", "actual_lead": None, "sampling_rate_hz": None,
                   "sample_count": None, "waveform_sha256": None,
                   "independent_review_1": empty_review(), "independent_review_2": empty_review(),
                   "adjudication": {"reviewer_code": None, "disagreements": [], "decisions": [], "rationale": ""}}
                  for item in selected],
    }


def output_directory(directory):
    raw = Path(directory).absolute()
    if any(path.is_symlink() for path in (raw, *raw.parents)):
        raise ValueError("annotation_output_symlink_forbidden")
    if not raw.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("annotation_output_must_remain_under_ignored_rr_annotation")
    for name in ("selection.private.json", "reviewer_template.json"):
        target = raw / name
        if target.exists() or target.is_symlink():
            raise FileExistsError("annotation_selection_or_template_exists_no_overwrite")
    return raw


def prepare(output_dir=None):
    destination = output_directory(output_dir or OUTPUT_ROOT)
    work, feasibility = ROOT / "artifacts/m2", ROOT / "artifacts/feasibility"
    source_files = [work / name for name in ("model_table.sqlite", "epochs.sqlite", "patient_split.json", "signal_summary.json", "vitals.sqlite", "vitals_summary.json", "models/development_report.json", "paired_comparison.json")]
    source_files += [feasibility / "stay_outcomes.jsonl", feasibility / "waveform_metadata.jsonl", PRIOR_SELECTION, Path(__file__)]
    before = {str(path): stat_signature(path) for path in source_files}
    for path in source_files:
        wal = Path(str(path) + "-wal")
        if path.suffix == ".sqlite" and wal.exists() and wal.stat().st_size:
            raise ValueError("annotation_refuses_active_wal")
    with closing(_readonly(work / "model_table.sqlite")) as connection:
        connection.execute("PRAGMA query_only=ON")
        source, _, _ = _validate_metadata(connection, work, feasibility)
        fingerprint = source["signal"]["fingerprint"]
        for name, expected in fingerprint["code"].items():
            if hash_file(ROOT / "src/hrv_early_warning" / name) != expected:
                raise ValueError("annotation_frozen_signal_source_changed")
        assignments = json.loads((work / "patient_split.json").read_text())["assignments"]
        prior = json.loads(PRIOR_SELECTION.read_text())
        connection.execute("ATTACH DATABASE ? AS signals", ((work / "epochs.sqlite").resolve().as_uri() + "?mode=ro",))
        excluded = excluded_patients(connection, prior, assignments, hash_file(work / "patient_split.json"), fingerprint)
        selected, counts = select_cases(connection, assignments, excluded)
    if before != {str(path): stat_signature(path) for path in source_files}:
        raise ValueError("annotation_sources_changed_during_selection")
    manifest = {
        "schema_version": VERSION, "seed": SEED, "split": "train", "patients_per_quality_group": PER_GROUP,
        "selection_rule": "patient_hash_then_exact_hourly_epoch_hash; accepted quota first; rejected quota from remaining patients; separately hash-mixed blind codes",
        "purpose": "20-patient annotation workflow pilot, not a prevalence or population-accuracy estimate",
        "waveform_decoded": False, "annotations_completed": False,
        "selection_uses_outcomes_or_model_scores": False, "model_features_read": False,
        "validation_or_test_selected": False, "dense_event_audit_epochs_used": False,
        "previously_seen_patients_excluded": len(excluded),
        "provenance": {"source_fingerprint_sha256": hash_json(source),
                       "patient_split_sha256": hash_file(work / "patient_split.json"),
                       "prior_selection_sha256": hash_file(PRIOR_SELECTION),
                       "frozen_epoch_fingerprint_sha256": hash_json(fingerprint),
                       "selection_script_sha256": hash_file(__file__),
                       "source_validation": "Verified frozen completion/source metadata, exact hourly identity/acceptance join, stable source sizes/mtimes; raw waveform bytes not decoded or hashed"},
        "candidate_pool": counts, "selection": selected,
    }
    template = reviewer_template(selected)
    # Serialize both before publication, then preserve the first selection even
    # if later template publication fails. Reruns never silently resample.
    json.dumps(manifest, allow_nan=False)
    json.dumps(template, allow_nan=False)
    destination.mkdir(parents=True, exist_ok=True)
    _write_private(destination / "selection.private.json", manifest, exclusive=True)
    _write_private(destination / "reviewer_template.json", template, exclusive=True)
    return {"preparation": "selection_frozen_waveforms_pending", "selected_patients": len(selected),
            "accepted_quota": sum(item["v1_accepted"] for item in selected),
            "rejected_quota": sum(not item["v1_accepted"] for item in selected),
            "previously_seen_patients_excluded": len(excluded), "waveform_package_ready": False,
            "model_features_read": False, "validation_or_test_selected": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()
    try:
        summary = prepare(args.output_dir)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        message = str(error) if isinstance(error, ValueError) and str(error).startswith("annotation_") else type(error).__name__
        print("Annotation preparation failed: " + message, file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
