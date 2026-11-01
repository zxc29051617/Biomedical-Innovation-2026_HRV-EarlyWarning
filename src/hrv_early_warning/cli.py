"""Data auditing, ECG screening, and development-only model evaluation."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tomllib
from pathlib import Path


def load_paths(config_path: Path) -> tuple[Path, Path]:
    """Resolve relative dataset paths against the configuration file directory."""
    config_path = config_path.resolve()
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    resolved = []
    for key in ("clinical_root", "waveform_root"):
        value = config.get("data", {}).get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing nonempty [data].{key} in {config_path}")
        path = Path(value).expanduser()
        resolved.append((config_path.parent / path).resolve() if not path.is_absolute() else path.resolve())
    return resolved[0], resolved[1]


def write_report(report: dict, output: Path, source_roots: tuple[Path, Path]) -> None:
    """Write an aggregate JSON report, refusing any output inside source datasets."""
    destination = output.resolve()
    if any(destination.is_relative_to(root.resolve()) for root in source_roots):
        raise ValueError("Report output must be outside both source dataset directories")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="Read-only clinical and waveform inventory; aggregate results only")
    audit.add_argument("--paths", type=Path, required=True, help="Local TOML containing [data] paths")
    audit.add_argument("--headers", type=int, default=0, help="Number of waveform headers to inspect (default: none)")
    audit.add_argument("--output", type=Path, help="Aggregate JSON report; otherwise print to stdout")
    feasibility = commands.add_parser("feasibility", help="Audit first-use events and same-ICU ECG metadata; not signal quality or training")
    feasibility.add_argument("--paths", type=Path, required=True)
    feasibility.add_argument("--protocol", type=Path, default=Path("configs/protocol.toml"))
    feasibility.add_argument("--work-dir", type=Path, default=Path("artifacts/feasibility"), help="Private patient-level cache and outputs; keep ignored by Git")
    feasibility.add_argument("--workers", type=int, default=8)
    feasibility.add_argument("--output", type=Path, help="Aggregate report; otherwise print to stdout")
    signals = commands.add_parser("signals", help="Decode and screen five-minute ECG epochs; resumable private cache")
    signals.add_argument("--paths", type=Path, required=True)
    signals.add_argument("--feasibility-dir", type=Path, default=Path("artifacts/feasibility"))
    signals.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    signals.add_argument("--quality", type=Path, default=Path("configs/signal_quality.toml"))
    signals.add_argument("--workers", type=int, default=8)
    signals.add_argument("--output", type=Path)
    vitals = commands.add_parser("vitals", help="Stream routine clinical observations with both timestamps")
    vitals.add_argument("--paths", type=Path, required=True)
    vitals.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    vitals.add_argument("--output", type=Path)
    develop = commands.add_parser("develop", help="Fit fixed models on training patients; validation only, test locked")
    develop.add_argument("--paths", type=Path, required=True)
    develop.add_argument("--feasibility-dir", type=Path, default=Path("artifacts/feasibility"))
    develop.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    develop.add_argument("--output", type=Path)
    compare = commands.add_parser("compare", help="Paired patient-bootstrap comparison of completed development models; test locked")
    compare.add_argument("--paths", type=Path, default=Path("configs/paths.local.toml"), help="Dataset paths used to protect source directories from report writes")
    compare.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    compare.add_argument("--replicates", type=int, default=200)
    compare.add_argument("--output", type=Path, help="Aggregate JSON; defaults to WORK_DIR/paired_comparison.json")
    status = commands.add_parser("status", help="Read aggregate cache completion; never reports a partial result as final")
    status.add_argument("--work-dir", type=Path, default=Path("artifacts/m2"))
    protocol = commands.add_parser("protocol", help="Display the study specification; does not train a model")
    protocol.add_argument("--config", type=Path, default=Path("configs/protocol.toml"))
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            from .progress import pipeline_status
            print(json.dumps(pipeline_status(args.work_dir), ensure_ascii=False, indent=2))
            return 0
        if args.command == "protocol":
            with args.config.open("rb") as stream:
                report = tomllib.load(stream)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command == "audit" and args.headers < 0:
            raise ValueError("--headers must be nonnegative")
        if args.command == "compare":
            if args.replicates < 1:
                raise ValueError("--replicates must be positive")
            if args.output is None:
                args.output = args.work_dir / "paired_comparison.json"
        clinical_root, waveform_root = load_paths(args.paths)
        if args.output and any(args.output.resolve().is_relative_to(root) for root in (clinical_root, waveform_root)):
            raise ValueError("Report output must be outside both source dataset directories")
        if args.command in {"signals", "vitals", "develop", "compare"}:
            if any(args.work_dir.resolve().is_relative_to(root) for root in (clinical_root, waveform_root)):
                raise ValueError("Private work directory must be outside both source dataset directories")
            if args.command == "compare":
                from .model_comparison import compare_saved_models
                compare_saved_models(args.work_dir / "model_table.sqlite", args.work_dir / "models",
                                     args.output, replicates=args.replicates)
                print(f"Aggregate paired comparison saved to {args.output.resolve()}")
                return 0
            progress = lambda message: print(message, file=sys.stderr, flush=True)
            if args.command == "signals":
                from .signal_pipeline import run_signal_screen
                report = run_signal_screen(waveform_root, args.feasibility_dir, args.work_dir, args.quality, args.workers, progress)
            elif args.command == "vitals":
                from .cohort import load_adult_metavision_stays
                from .vitals import extract_vitals
                stays, _ = load_adult_metavision_stays(clinical_root)
                _, report = extract_vitals(clinical_root, stays, args.work_dir, progress)
            else:
                from .modeling import build_model_table, train_models
                table, table_report = build_model_table(args.feasibility_dir, args.work_dir, args.work_dir / "model_table.sqlite", progress)
                report = {"table": table_report, "models": train_models(table, args.work_dir / "models", progress)}
            if args.output:
                write_report(report, args.output, (clinical_root, waveform_root))
                print(f"Aggregate report saved to {args.output.resolve()}")
            else:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command == "feasibility":
            from .feasibility import run_feasibility

            report = run_feasibility(clinical_root, waveform_root, args.work_dir, args.protocol,
                                     workers=args.workers, progress=lambda message: print(message, file=sys.stderr))
            if args.output:
                write_report(report, args.output, (clinical_root, waveform_root))
                print(f"Aggregate feasibility saved to {args.output.resolve()}")
            else:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        from .inventory import audit_inventory

        report = audit_inventory(clinical_root, waveform_root, header_sample_size=args.headers)
        if args.output:
            write_report(report, args.output, (clinical_root, waveform_root))
            print(f"Aggregate inventory saved to {args.output.resolve()}")
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        core_files = ("PATIENTS.csv", "ADMISSIONS.csv", "ICUSTAYS.csv", "DIAGNOSES_ICD.csv")
        incomplete = any(report["clinical_tables"][name]["status"] != "ok" for name in core_files)
        incomplete |= report["waveform_indices"]["RECORDS-waveforms"]["status"] != "ok"
        if incomplete:
            print("Inventory is incomplete: inspect file statuses and warnings in the report", file=sys.stderr)
            return 2
        if report["warnings"]:
            print(f"Inventory has {len(report['warnings'])} warning(s); inspect the report", file=sys.stderr)
        return 0
    except ModuleNotFoundError as exc:
        print(f"Missing optional dependency {exc.name}; install the signal/model extras in your Python environment", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
