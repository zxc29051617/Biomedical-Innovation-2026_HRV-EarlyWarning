"""Read-only aggregate progress; an incomplete cache does not prove a live job."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def pipeline_status(work_dir):
    work = Path(work_dir).resolve()
    result = {"vitals": {"status": "not_complete"}, "signals": {"status": "not_started"}}
    if (work / "vitals_summary.json").exists() and (work / "vitals.sqlite").exists():
        report = json.loads((work / "vitals_summary.json").read_text())["report"]
        result["vitals"] = {"status": "complete", **{key: report[key] for key in ("rows_scanned", "retained_rows", "stays_with_vitals")}}
    plan_path, database = work / "signal_plan.json", work / "epochs.sqlite"
    if plan_path.exists() and database.exists():
        plan = json.loads(plan_path.read_text())["plan"]
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            count = connection.execute("SELECT COUNT(*) FROM epochs").fetchone()[0]
        finally:
            connection.close()
        result["signals"] = {"status": "incomplete", "completed_epochs": count,
                             "planned_epochs": plan["unique_epochs"],
                             "cache_modified_utc": datetime.fromtimestamp(database.stat().st_mtime, timezone.utc).isoformat(),
                             "note": "Incomplete means cached work remains; it does not establish that a worker is alive. Partial acceptance rates are not representative."}
        summary_path = work / "signal_summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text())
            if summary.get("complete") is True and count == plan["unique_epochs"] == summary.get("screened_epochs"):
                result["signals"].update(status="complete", accepted_epochs=summary["accepted_epochs"], events=summary["events"])
    for directory in ("baseline_models", "models"):
        path = work / directory / "development_report.json"
        if path.exists():
            report = json.loads(path.read_text())
            result[directory] = {"stage": report["stage"], "variants": {name: model["status"] for name, model in report["models"].items()}}
    finalizer = work / "finalization_status.json"
    if finalizer.exists():
        result["automatic_finalization"] = json.loads(finalizer.read_text())
    for name, filename in (("signal_job", "signal_job_status.json"),
                           ("automatic_comparison", "comparison_status.json"),
                           ("automatic_report", "report_status.json")):
        path = work / filename
        if path.exists():
            result[name] = json.loads(path.read_text())
    return result
