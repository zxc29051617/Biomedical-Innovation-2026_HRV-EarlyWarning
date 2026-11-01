#!/usr/bin/env python3
"""Export private local M2 replay. Existing trusted model files are required."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hrv_early_warning.replay import export_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("validation", "train"), default="validation")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "replay")
    args = parser.parse_args()
    try:
        summary = export_replay(output_dir=args.output_dir, split=args.split)
    except (OSError, ValueError, KeyError, TypeError) as error:
        # No IDs, source paths, serialized estimator contents or raw rows in CLI logs.
        safe = str(error) if isinstance(error, ValueError) and str(error).startswith("replay_") else type(error).__name__
        print(f"Replay export failed: {safe}", file=sys.stderr)
        return 1
    print(json.dumps({key: summary[key] for key in ("status", "split", "cases", "hourly_points", "rr_accepted_points",
                                                  "selection_uses_scores", "test_features_read", "models_retrained")}, indent=2))
    print("Import the private replay.json from the chosen artifacts/replay directory in the local demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
