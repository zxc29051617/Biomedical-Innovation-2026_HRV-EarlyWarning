#!/usr/bin/env python3
"""Audit frozen development coverage using only Python's standard library."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hrv_early_warning.coverage_review import review_coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--feasibility-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = review_coverage(args.work_dir, args.feasibility_dir, args.output_dir)
    print(json.dumps({"status": "coverage_review_complete", "scope": report["scope"],
                      "development_stages": report["coverage"]["development"]["stages"],
                      "test_features_read": False}, indent=2))


if __name__ == "__main__":
    main()
