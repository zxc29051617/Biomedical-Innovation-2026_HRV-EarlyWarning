"""Bounded parallel master-header reads with a private, reproducible cache.

The caller supplies an ignored local work directory. The JSONL cache contains
patient identifiers, waveform paths and timestamps and must not be published.
Cache validity uses the index file's size/mtime and requested subject hash;
it is not a check of every master header or signal file's integrity. Use a fresh
work directory after changing source headers without changing the index.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Callable
import uuid

from .inventory import parse_waveform_subject
from .waveforms import HeaderParseError, WaveformRecord, WaveformSegment, read_record_metadata


CACHE_SCHEMA_VERSION = "1.0"
PARSER_VERSION = "waveforms-1"
MAX_WORKERS = 8
_CACHE_NAME = "waveform_metadata.jsonl"
_SUMMARY_NAME = "summary.json"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError("unsupported cache value")


def _fingerprint(root: Path, subjects: set[int]) -> dict[str, Any]:
    try:
        info = (root / "RECORDS-waveforms").stat()
    except OSError:
        raise ValueError("waveform_index_unavailable") from None
    subject_bytes = ",".join(str(value) for value in sorted(subjects)).encode("ascii")
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "waveform_root": str(root),
        "index_size_bytes": info.st_size,
        "index_mtime_ns": info.st_mtime_ns,
        "requested_subjects_sha256": hashlib.sha256(subject_bytes).hexdigest(),
        "requested_subjects_count": len(subjects),
        "validation_scope": "index_fingerprint_only_not_master_or_signal_integrity",
    }


def _reason(value: str) -> str:
    """Only machine-readable reason codes may enter public aggregate output."""
    return value if re.fullmatch(r"[a-z][a-z0-9_]*", value) else "unclassified_header_error"


def _parse_one(root: Path, entry: str) -> tuple[str, WaveformRecord | None, str | None]:
    try:
        return entry, read_record_metadata(root, entry), None
    except HeaderParseError as error:
        return entry, None, _reason(str(error))
    except OSError:
        return entry, None, "header_os_error"
    except Exception as error:
        # Continue an inventory when one header is unexpected. Preserve the
        # exception class, never its text (which can contain record paths).
        return entry, None, "unexpected_" + type(error).__name__.lower()


def _cached_path(value: str, root: Path) -> Path:
    path = Path(value)
    # Lexical validation avoids per-leaf filesystem I/O on a cache hit.
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
        raise ValueError("invalid_cached_path")
    return path


def _restore_record(data: dict[str, Any], root: Path, subjects: set[int]) -> WaveformRecord:
    data = dict(data)
    if data["subject_id"] not in subjects or parse_waveform_subject(data["record_entry"]) != data["subject_id"]:
        raise ValueError("invalid_cached_subject")
    if Path(data["waveform_root"]) != root:
        raise ValueError("invalid_cached_root")
    data["waveform_root"] = root
    data["header_path"] = _cached_path(data["header_path"], root)
    expected_entry = data["header_path"].relative_to(root).with_suffix("").as_posix()
    if expected_entry != data["record_entry"]:
        raise ValueError("invalid_cached_entry")
    data["start"] = datetime.fromisoformat(data["start"])
    data["end"] = datetime.fromisoformat(data["end"])
    data["warnings"] = tuple(data.get("warnings", ()))
    if any(_reason(reason) != reason for reason in data["warnings"]):
        raise ValueError("invalid_cached_warning")
    if data["end"] < data["start"] or data["n_samples"] < 0:
        raise ValueError("invalid_cached_interval")
    if not math.isfinite(data["sampling_rate"]) or data["sampling_rate"] <= 0:
        raise ValueError("invalid_cached_sampling_rate")
    segments = []
    for item in data["segments"]:
        item = dict(item)
        item["start"] = datetime.fromisoformat(item["start"])
        item["end"] = datetime.fromisoformat(item["end"])
        if item["start"] < data["start"] or item["end"] > data["end"] or item["end"] < item["start"]:
            raise ValueError("invalid_cached_segment_interval")
        if item["sample_count"] < 0:
            raise ValueError("invalid_cached_sample_count")
        if item["header_path"] is not None:
            item["header_path"] = _cached_path(item["header_path"], root)
        segments.append(WaveformSegment(**item))
    data["segments"] = tuple(segments)
    return WaveformRecord(**data)


def _read_cache(work_dir: Path, fingerprint: dict[str, Any], subjects: set[int]) -> tuple[list[WaveformRecord], dict[str, Any]] | None:
    try:
        summary = json.loads((work_dir / _SUMMARY_NAME).read_text(encoding="utf-8"))
        if summary["fingerprint"] != fingerprint:
            return None
        root = Path(fingerprint["waveform_root"])
        records: list[WaveformRecord] = []
        error_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        entries: set[str] = set()
        with (work_dir / _CACHE_NAME).open(encoding="utf-8") as stream:
            first = json.loads(next(stream))
            if first != {
                "type": "metadata", "generation_id": summary["generation_id"],
                "fingerprint": fingerprint,
            }:
                return None
            for line in stream:
                item = json.loads(line)
                entry = item["entry"]
                if entry in entries or parse_waveform_subject(entry) not in subjects:
                    return None
                entries.add(entry)
                if item["type"] == "record":
                    record = _restore_record(item["record"], root, subjects)
                    if record.record_entry != entry:
                        return None
                    records.append(record)
                    warning_counts.update(record.warnings)
                elif item["type"] == "error" and _reason(item["reason"]) == item["reason"]:
                    error_counts[item["reason"]] += 1
                else:
                    return None
        stats = dict(summary["stats"])
        if (
            len(entries) != stats["selected_entries"]
            or len(records) != stats["parsed_records"]
            or sum(error_counts.values()) != stats["failed_records"]
            or dict(error_counts) != stats["error_counts"]
            or dict(warning_counts) != stats["warning_counts"]
        ):
            return None
        records.sort(key=lambda record: record.record_entry)
        stats["cache_status"] = "hit"
        stats["workers_used"] = 0
        return records, stats
    except (OSError, ValueError, TypeError, KeyError, StopIteration, OverflowError):
        return None


def _selected_entries(root: Path, subjects: set[int]) -> tuple[list[str], dict[str, int]]:
    entries: set[str] = set()
    matched_subjects: set[int] = set()
    total = unparsed = duplicates = 0
    with (root / "RECORDS-waveforms").open(encoding="utf-8") as stream:
        for line in stream:
            entry = line.strip()
            if not entry:
                continue
            total += 1
            subject = parse_waveform_subject(entry)
            if subject is None:
                unparsed += 1
            elif subject in subjects:
                matched_subjects.add(subject)
                if entry in entries:
                    duplicates += 1
                entries.add(entry)
    return sorted(entries), {
        "index_entries": total,
        "unparsed_index_entries": unparsed,
        "duplicate_selected_entries": duplicates,
        "matched_subjects": len(matched_subjects),
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".waveform_summary_", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, default=_json_default, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_waveform_index(
    waveform_root: Path,
    subject_ids: set[int],
    work_dir: Path,
    workers: int = 8,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[WaveformRecord], dict[str, Any]]:
    """Load selected master metadata, caching patient details in private work_dir.

    Parallel reads are capped at eight threads. Results are sorted by index
    entry; parse failures and warnings are counted without exposing identifiers.
    Cache hits use index fingerprint validation and do not reopen master headers.
    Failures are cached too; use a fresh work directory to retry repaired headers
    whose index file has not changed. The caller must keep work_dir ignored.
    """
    subjects = set(subject_ids)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in subjects):
        raise ValueError("subject_ids must contain non-negative integers")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
        raise ValueError("workers must be a positive integer")
    root = Path(waveform_root).resolve()
    work_dir = Path(work_dir)
    fingerprint = _fingerprint(root, subjects)
    cached = _read_cache(work_dir, fingerprint, subjects)
    if cached is not None:
        if progress is not None:
            progress(f"Waveform metadata cache hit: {cached[1]['parsed_records']} records, {cached[1]['failed_records']} failures.")
        return cached
    entries, selection = _selected_entries(root, subjects)
    if _fingerprint(root, subjects) != fingerprint:
        raise RuntimeError("waveform_index_changed_during_read")
    work_dir.mkdir(parents=True, exist_ok=True)
    workers_used = min(workers, MAX_WORKERS, len(entries)) if entries else 0
    stats: dict[str, Any] = {
        "cache_status": "rebuilt", "cache_validation": "index_fingerprint_only",
        **selection, "selected_entries": len(entries), "requested_subjects": len(subjects),
        "workers_used": workers_used,
    }
    generation = uuid.uuid4().hex
    records: list[WaveformRecord] = []
    errors: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=work_dir, prefix=".waveform_metadata_", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps({"type": "metadata", "generation_id": generation, "fingerprint": fingerprint}, sort_keys=True) + "\n")
            with ThreadPoolExecutor(max_workers=workers_used or 1) as executor:
                results = executor.map(lambda entry: _parse_one(root, entry), entries)
                for processed, (entry, record, reason) in enumerate(results, 1):
                    if record is not None:
                        records.append(record)
                        warnings.update(record.warnings)
                        item = {"type": "record", "entry": entry, "record": asdict(record)}
                    else:
                        errors[reason] += 1
                        item = {"type": "error", "entry": entry, "reason": reason}
                    stream.write(json.dumps(item, default=_json_default, sort_keys=True) + "\n")
                    if progress is not None and (processed % 500 == 0 or processed == len(entries)):
                        progress(f"Waveform master headers: {processed}/{len(entries)}; parsed {len(records)}, errors {sum(errors.values())}.")
            stream.flush()
            os.fsync(stream.fileno())
        stats.update({
            "parsed_records": len(records), "parsed_subjects": len({record.subject_id for record in records}),
            "failed_records": sum(errors.values()), "error_counts": dict(sorted(errors.items())),
            "warning_counts": dict(sorted(warnings.items())),
        })
        temporary.replace(work_dir / _CACHE_NAME)
        _atomic_json(work_dir / _SUMMARY_NAME, {"generation_id": generation, "fingerprint": fingerprint, "stats": stats})
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return records, stats
