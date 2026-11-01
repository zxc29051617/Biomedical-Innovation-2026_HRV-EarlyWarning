"""Download the fixed, small MIT-BIH annotation pilot from official PhysioNet.

No credentials, mirrors, MIMIC access, or background downloads. SHA-256 values
record acquired bytes; they are not independently published official checksums.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.request import Request, urlopen


BASE = "https://physionet.org/files/mitdb/1.0.0/"
RECORDS = ("100", "118", "119", "200", "207", "214")


def validate_plan(plan):
    if (plan.get("schema_version") != "beat_timing_reference_plan_v1"
            or plan.get("dataset_key") != "mitdb" or plan.get("version") != "1.0.0"
            or plan.get("download_base") != BASE
            or plan.get("annotation_kind") != "expert_reviewed_beats_with_subsequent_peak_alignment"
            or plan.get("epoch_seconds") != 300
            or plan.get("target_sampling_rates_hz") != [125]
            or plan.get("match_tolerance_seconds") != 0.1):
        raise ValueError("unsupported_reference_plan")
    if tuple(row.get("record") for row in plan.get("records", [])) != RECORDS:
        raise ValueError("reference_record_selection_changed_update_protocol_first")
    for row in plan["records"]:
        if (row.get("annotator") != "atr" or row.get("channel") != 0
                or row.get("role") != "development_pilot"
                or row.get("subject_group") != "mitdb_" + row["record"]):
            raise ValueError("unsupported_record_annotation_channel_or_group")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("configs/beat_timing_reference.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/rr_validation/reference"))
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    destination = args.output_dir.resolve()
    if not destination.is_relative_to(repository / "artifacts/rr_validation"):
        raise ValueError("reference_downloads_must_remain_under_artifacts_rr_validation")
    plan_bytes = args.plan.read_bytes()
    plan = json.loads(plan_bytes)
    validate_plan(plan)
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "manifest.json").exists():
        raise FileExistsError("reference_manifest_already_exists_validate_or_use_a_new_output_directory")
    files = []
    for row in plan["records"]:
        for extension in ("hea", "dat", "atr"):
            filename = f"{row['record']}.{extension}"
            assert re.fullmatch(r"[0-9]{3}\.(hea|dat|atr)", filename)
            target = destination / filename
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"refusing_to_overwrite_reference_file:{filename}")
            request = Request(BASE + filename, headers={"User-Agent": "HRV-EarlyWarning-reference-pilot/0.1"})
            with urlopen(request, timeout=30) as response:
                # Only accept the official host/path; do not follow a mirror.
                if response.url != BASE + filename:
                    raise ValueError("unexpected_reference_download_redirect")
                content = response.read(4_000_001)
            if not content or len(content) > 4_000_000:
                raise ValueError("reference_file_empty_or_exceeds_pilot_size_limit")
            with tempfile.NamedTemporaryFile(dir=destination, delete=False) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            temporary_path.replace(target)
            files.append({"path": filename, "bytes": len(content),
                          "sha256": hashlib.sha256(content).hexdigest(), "source_url": BASE + filename})
            print(f"Downloaded {filename}: {len(content)} bytes", flush=True)
    manifest = {"schema_version": "beat_timing_reference_manifest_v1", "plan": plan,
                "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(), "files": files,
                "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
                "checksum_status": "SHA-256 recorded at official HTTPS acquisition; not independently published expected hashes"}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Reference manifest: {destination / 'manifest.json'}", flush=True)


if __name__ == "__main__":
    main()
