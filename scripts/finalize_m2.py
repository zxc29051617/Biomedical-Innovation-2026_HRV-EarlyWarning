"""Wait for a complete ECG cache, then build and evaluate development models.

This helper is for an already running signal job. It does not start duplicate
ECG workers. Completion/error status and all patient-level artifacts stay local.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import time

from hrv_early_warning.modeling import build_model_table, train_models
from hrv_early_warning.progress import pipeline_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    parser.add_argument("--feasibility-dir", type=Path, default=Path("artifacts/feasibility"))
    parser.add_argument("--timeout-hours", type=float, default=24)
    args = parser.parse_args()
    if args.timeout_hours <= 0:
        raise ValueError("timeout-hours must be positive")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    status_path = args.work_dir / "finalization_status.json"

    def status(state, **extra):
        temporary = status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"state": state, "updated_utc": datetime.now(timezone.utc).isoformat(), **extra}, indent=2) + "\n")
        temporary.replace(status_path)

    with (args.work_dir / "finalization.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + args.timeout_hours * 3600
        status("waiting_for_complete_signal_cache")
        print("Waiting for full ECG completion; partial RR results will not be fitted", flush=True)
        try:
            while pipeline_status(args.work_dir)["signals"]["status"] != "complete":
                if time.monotonic() >= deadline:
                    raise TimeoutError("Full signal cache not completed before finalizer deadline")
                time.sleep(30)
            status("building_model_table")
            progress = lambda message: print(message, flush=True)
            table, _ = build_model_table(args.feasibility_dir, args.work_dir, args.work_dir / "model_table.sqlite", progress)
            status("fitting_development_models")
            report = train_models(table, args.work_dir / "models", progress)
            status("complete", stage=report["stage"], models={name: model["status"] for name, model in report["models"].items()})
            print("Development comparison complete; held-out test remains unopened", flush=True)
        except Exception as error:
            status("failed", error_type=type(error).__name__)
            raise


if __name__ == "__main__":
    main()
