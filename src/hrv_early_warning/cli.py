"""Command line entry points; the initial release audits data, not model performance."""

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
    protocol = commands.add_parser("protocol", help="Display the study specification; does not train a model")
    protocol.add_argument("--config", type=Path, default=Path("configs/protocol.toml"))
    args = parser.parse_args(argv)
    try:
        if args.command == "protocol":
            with args.config.open("rb") as stream:
                report = tomllib.load(stream)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.headers < 0:
            raise ValueError("--headers must be nonnegative")
        clinical_root, waveform_root = load_paths(args.paths)
        if args.output and any(args.output.resolve().is_relative_to(root) for root in (clinical_root, waveform_root)):
            raise ValueError("Report output must be outside both source dataset directories")
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
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
