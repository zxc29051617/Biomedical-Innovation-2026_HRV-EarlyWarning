#!/usr/bin/env python3
"""Preflight or fit fixed availability/rate/variability controls locally."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hrv_early_warning.incremental_value import run_controls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "artifacts/m2")
    parser.add_argument("--feasibility-dir", type=Path, default=ROOT / "artifacts/feasibility")
    parser.add_argument("--spec", type=Path, default=ROOT / "configs/rr_incremental_controls.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fit", action="store_true", help="Fit on train and evaluate development validation; never heldout test")
    args = parser.parse_args()
    result = run_controls(args.work_dir, args.feasibility_dir, args.spec, args.output_dir, fit=args.fit)
    print(json.dumps({"status": result["status"], "population": result["same_paired_population"],
                      "dependencies": result.get("dependencies"), "heldout_features_read": False}, allow_nan=False))


if __name__ == "__main__":
    main()
