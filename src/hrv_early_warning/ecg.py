"""Decode one five-minute Lead II epoch with the official WFDB library.

No interpolation, ECG cleaning, beat detection, or persistent raw-signal cache
is performed. Physical amplitudes use explicit header calibration and are
converted to mV. Sample timestamps remain within the requested half-open epoch.
See https://wfdb.readthedocs.io/en/latest/io.html for rdheader/rdrecord semantics.
NumPy and WFDB are imported only when decoding is requested.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from functools import lru_cache
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING, Iterable

from .inventory import parse_waveform_subject
from .waveforms import WaveformRecord, WaveformSegment, _normalized, _safe_child

if TYPE_CHECKING:
    import numpy as np


DECODER_VERSION = "1"
EPOCH_SECONDS = 300
_UNIT_TO_MV = {"mV": 1.0, "uV": 0.001, "µV": 0.001, "μV": 0.001, "V": 1000.0}
_GAIN_UNITS = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?:\(([+-]?\d+)\))?/([^\s]+)")


@dataclass(frozen=True)
class DecodedECGEpoch:
    signal: np.ndarray
    fs: float | None
    requested_start: datetime
    requested_end: datetime
    sample_start: datetime | None
    coverage_fraction: float
    missing_fraction: float
    missing_samples: int
    status: str
    errors: tuple[str, ...]
    meta: dict[str, object]


@dataclass(frozen=True)
class _LeafMetadata:
    path: Path
    signal_file: str
    fs: float
    sample_count: int
    channel_index: int
    unit: str
    scale_to_mv: float


def _dependencies():
    try:
        import numpy
        import wfdb
    except ImportError:
        raise RuntimeError("ECG decoding requires the optional numpy and wfdb packages") from None
    return numpy, wfdb


@lru_cache(maxsize=8192)
def _cached_leaf(path_string: str, mtime_ns: int, size_bytes: int, decoder_version: str) -> _LeafMetadata | str:
    """Cache header facts or a stable rejection by path, stat fingerprint, version."""
    _, wfdb = _dependencies()
    path = Path(path_string)
    try:
        header = wfdb.rdheader(str(path.with_suffix("")))
        lines = [line.split() for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        return "leaf_header_missing"
    except (OSError, UnicodeError):
        return "leaf_header_unreadable"
    except Exception:
        return "wfdb_header_error"
    try:
        if hasattr(header, "seg_name"):
            return "nested_multisegment_unsupported"
        if not math.isfinite(header.fs) or header.fs <= 0 or header.sig_len is None or header.sig_len < 0:
            return "invalid_leaf_sampling_metadata"
        channels = [index for index, name in enumerate(header.sig_name or [])
                    if str(name).strip().upper() == "II"]
        if not channels:
            return "lead_ii_absent"
        if len(channels) != 1:
            return "lead_ii_ambiguous"
        channel = channels[0]
        raw = lines[channel + 1]
        if len(raw) < 9 or " ".join(raw[8:]).strip().upper() != "II":
            return "lead_ii_header_inconsistent"
        # WFDB permits default gain/units. Research amplitudes require explicit
        # gain, units and a baseline (or ADC zero) rather than those defaults.
        calibration = _GAIN_UNITS.fullmatch(raw[2])
        if calibration is None:
            return "explicit_calibration_required"
        gain = float(calibration.group(1))
        unit = calibration.group(3)
        baseline = int(calibration.group(2)) if calibration.group(2) is not None else int(raw[4])
        if not math.isfinite(gain) or gain == 0:
            return "invalid_adc_gain"
        if unit not in _UNIT_TO_MV:
            return "unsupported_voltage_unit"
        if header.units[channel] != unit or header.baseline[channel] != baseline or header.adc_gain[channel] != gain:
            return "calibration_header_inconsistent"
        if (header.samps_per_frame or [1] * header.n_sig)[channel] != 1:
            return "multiple_samples_per_frame_unsupported"
        if (header.skew or [0] * header.n_sig)[channel] not in (0, None):
            return "nonzero_skew_unsupported"
        if raw[0] in {"~", "-"} or str(header.fmt[channel]) == "0":
            return "lead_ii_has_no_signal_file"
        return _LeafMetadata(path, raw[0], float(header.fs), int(header.sig_len), channel, unit, _UNIT_TO_MV[unit])
    except (ValueError, TypeError, IndexError, AttributeError):
        return "invalid_leaf_header"


def _leaf_metadata(record: WaveformRecord, segment: WaveformSegment) -> _LeafMetadata | str:
    if segment.header_path is None:
        return "leaf_header_missing"
    try:
        root = record.waveform_root.resolve()
        path = _safe_child(root, segment.header_path.parent, segment.header_path.name)
        if parse_waveform_subject(path.relative_to(root).as_posix()) != record.subject_id:
            return "leaf_subject_identity_mismatch"
        stat = path.stat()
        metadata = _cached_leaf(str(path), stat.st_mtime_ns, stat.st_size, DECODER_VERSION)
        if isinstance(metadata, str):
            return metadata
        _safe_child(root, path.parent, metadata.signal_file)
    except FileNotFoundError:
        return "leaf_header_missing"
    except (OSError, ValueError):
        return "leaf_path_unsafe_or_unreadable"
    if metadata.sample_count != segment.sample_count:
        return "segment_sample_count_mismatch"
    if metadata.fs != record.sampling_rate:
        return "segment_sampling_rate_mismatch"
    return metadata


def _frames(delta: timedelta, fs: float) -> Fraction:
    microseconds = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Fraction(microseconds, 1_000_000) * Fraction(str(fs))


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _empty(start: datetime, end: datetime, status: str, errors: Counter, meta: dict[str, object]) -> DecodedECGEpoch:
    np, _ = _dependencies()
    return DecodedECGEpoch(
        np.empty(0, dtype=np.float64), None, start, end, None, 0.0, 1.0, 0,
        status, tuple(sorted(errors)), {
            "decoder_version": DECODER_VERSION, "duration_seconds": (end - start).total_seconds(),
            "expected_sample_count": 0, "decoded_sample_count": 0, "overlap_conflict_samples": 0,
            "segments_read": 0, **meta, "error_counts": dict(sorted(errors.items())),
        },
    )


def decode_ecg_epoch(records: Iterable[WaveformRecord], start: datetime, end: datetime) -> DecodedECGEpoch:
    """Decode exact Lead II sample points within the five-minute ``[start,end)``.

    All records must belong to one patient, and callers must first restrict
    them to the intended ICU stay. Invalid epoch bounds raise ValueError. A
    mixed patient set, multiple sampling frequencies or incompatible sample
    grids returns a rejected result. Missing samples remain NaN. Conflicting
    finite values from overlapping sources become permanently missing rather
    than being resolved by arbitrary source order.

    ``sample_start`` preserves the original sample-grid phase and is at or
    after ``start``. Array element i is at sample_start+i/fs, strictly before
    end. No source values outside that interval are requested from WFDB.
    Coverage is the finite fraction of this sample grid, not an ECG-quality
    score. No samples or patient identifiers are placed in meta or error text.
    """
    np, wfdb = _dependencies()
    start, end = _normalized((start, end))
    if end - start != timedelta(seconds=EPOCH_SECONDS):
        raise ValueError("ECG epochs must be exactly five minutes")
    records = tuple(records)
    errors: Counter[str] = Counter()
    if not records:
        errors["no_records"] += 1
        return _empty(start, end, "unavailable", errors, {})
    if len({record.subject_id for record in records}) != 1:
        errors["mixed_subjects"] += 1
        return _empty(start, end, "rejected", errors, {})
    if any(parse_waveform_subject(record.record_entry) != record.subject_id for record in records):
        errors["record_subject_identity_mismatch"] += 1
        return _empty(start, end, "rejected", errors, {})
    candidates = []
    gap_segments = 0
    for record in sorted(records, key=lambda value: value.record_entry):
        for segment in record.segments:
            segment_start, segment_end, _, _ = _normalized((segment.start, segment.end, start, end))
            if segment_end <= start or segment_start >= end or segment.is_layout:
                continue
            if segment.is_gap:
                gap_segments += 1
                continue
            metadata = _leaf_metadata(record, segment)
            if isinstance(metadata, str):
                errors[metadata] += 1
                continue
            candidates.append((segment_start, segment_end, segment, metadata))
    common_meta = {"gap_segments": gap_segments, "candidate_segments": len(candidates)}
    if not candidates:
        errors["no_decodable_lead_ii"] += 1
        return _empty(start, end, "unavailable", errors, common_meta)
    frequencies = {candidate[3].fs for candidate in candidates}
    if len(frequencies) != 1:
        errors["incompatible_sampling_rates"] += 1
        return _empty(start, end, "rejected", errors, common_meta)
    fs = next(iter(frequencies))
    candidates.sort(key=lambda candidate: (candidate[0], str(candidate[3].path)))
    first_start = candidates[0][0]
    # Extend the clock grid (never the values) backwards if the first available
    # segment starts late, so leading missing time still occupies NaN samples.
    first_sample = _ceil(_frames(start - first_start, fs))
    sample_start = first_start + timedelta(seconds=first_sample / fs)
    if sample_start >= end:
        errors["epoch_contains_no_samples"] += 1
        return _empty(start, end, "unavailable", errors, common_meta)
    expected = _ceil(_frames(end - sample_start, fs))
    # Metadata dates have microsecond precision. Permit only its rounding error,
    # never a material phase shift or implicit resampling between sources.
    grid_tolerance = fs * 1.1e-6 + 1e-12
    offsets = []
    for segment_start, _, _, _ in candidates:
        position = _frames(segment_start - sample_start, fs)
        offset = round(position)
        if abs(float(position - offset)) > grid_tolerance:
            errors["incompatible_sample_grids"] += 1
            return _empty(start, end, "rejected", errors, common_meta)
        offsets.append(offset)
    signal = np.full(expected, np.nan, dtype=np.float64)
    conflicts = np.zeros(expected, dtype=bool)
    segments_read = source_samples = 0
    for (segment_start, _, segment, metadata), offset in zip(candidates, offsets):
        sampfrom = max(0, _ceil(_frames(start - segment_start, fs)), -offset)
        sampto = min(segment.sample_count, _ceil(_frames(end - segment_start, fs)), expected - offset)
        if sampto <= sampfrom:
            continue
        try:
            decoded = wfdb.rdrecord(
                str(metadata.path.with_suffix("")), sampfrom=sampfrom, sampto=sampto,
                channels=[metadata.channel_index], physical=True, smooth_frames=True,
                ignore_skew=False, return_res=64,
            )
        except FileNotFoundError:
            errors["signal_file_missing"] += 1
            continue
        except OSError:
            errors["signal_file_unreadable"] += 1
            continue
        except Exception:
            errors["wfdb_decode_error"] += 1
            continue
        values = decoded.p_signal
        if decoded.fs != fs or values is None or values.shape != (sampto - sampfrom, 1):
            errors["decoded_shape_or_rate_mismatch"] += 1
            continue
        if decoded.units != [metadata.unit] or decoded.sig_name != ["II"]:
            errors["decoded_channel_or_unit_mismatch"] += 1
            continue
        values = np.asarray(values[:, 0], dtype=np.float64) * metadata.scale_to_mv
        values[~np.isfinite(values)] = np.nan
        segments_read += 1
        source_samples += len(values)
        left, right = offset + sampfrom, offset + sampto
        previous = signal[left:right]
        conflict_region = conflicts[left:right]
        both_finite = np.isfinite(previous) & np.isfinite(values)
        disagree = both_finite & ~np.isclose(previous, values, rtol=1e-9, atol=1e-12, equal_nan=True)
        conflict_region |= disagree
        fill = ~np.isfinite(previous) & np.isfinite(values) & ~conflict_region
        previous[fill] = values[fill]
        previous[conflict_region] = np.nan
    finite = int(np.isfinite(signal).sum())
    missing = expected - finite
    conflict_count = int(conflicts.sum())
    if conflict_count:
        errors["overlapping_signal_conflict"] += 1
    status = "ok" if finite == expected else "partial" if finite else "unavailable"
    return DecodedECGEpoch(
        signal, fs, start, end, sample_start, finite / expected, missing / expected, missing,
        status, tuple(sorted(errors)), {
            "decoder_version": DECODER_VERSION, "duration_seconds": EPOCH_SECONDS,
            "expected_sample_count": expected, "decoded_sample_count": finite,
            "source_sample_count": source_samples, "coverage_seconds": min(EPOCH_SECONDS, finite / fs),
            "sample_start_offset_seconds": (sample_start - start).total_seconds(),
            "overlap_conflict_samples": conflict_count, "segments_read": segments_read,
            **common_meta, "error_counts": dict(sorted(errors.items())),
            "signal_units": "mV", "scope": "Decoded amplitudes and missingness only; no ECG quality or rhythm eligibility established.",
        },
    )
