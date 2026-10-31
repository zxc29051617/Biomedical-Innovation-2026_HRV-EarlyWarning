"""WFDB metadata timelines and conservative Lead II file availability.

Record spans include gaps. Confirmed metadata intervals only establish that a
segment declares Lead II and its signal file exists and is nonempty; they do not
establish decoded samples, rhythm eligibility, ECG quality, or usable HRV.
Header syntax follows https://physionet.org/physiotools/wag/header-5.htm.
No files are written and no cache or concurrent executor is created here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path, PurePosixPath
import re
from typing import Iterable

from .inventory import parse_waveform_subject


Interval = tuple[datetime, datetime]


class HeaderParseError(ValueError):
    """Missing or inconsistent WFDB timing/structure; message contains no IDs."""


@dataclass(frozen=True)
class WaveformSegment:
    name: str
    start: datetime
    end: datetime
    sample_count: int
    is_gap: bool
    is_layout: bool
    header_path: Path | None


@dataclass(frozen=True)
class WaveformRecord:
    subject_id: int
    start: datetime
    end: datetime
    sampling_rate: float
    n_samples: int
    header_path: Path
    waveform_root: Path
    record_entry: str
    segments: tuple[WaveformSegment, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ECGInspection:
    intervals: tuple[Interval, ...]
    warnings: tuple[str, ...]
    segments_checked: int
    segments_with_ii: int
    scope: str = "Metadata and nonempty signal file presence; not decoded ECG quality or HRV availability."


@dataclass(frozen=True)
class _RecordLine:
    name: str
    segment_count: int | None
    signal_count: int
    sampling_rate: Decimal
    samples: int
    fields: tuple[str, ...]


def _header_lines(path: Path) -> list[list[str]]:
    try:
        with path.open(encoding="ascii") as stream:
            lines = [line.split() for line in stream if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        raise HeaderParseError("header_missing") from None
    except (OSError, UnicodeError):
        raise HeaderParseError("header_unreadable") from None
    if not lines:
        raise HeaderParseError("header_empty")
    return lines


def _record_line(fields: list[str]) -> _RecordLine:
    # This research parser requires explicit frame count and frequency; WFDB's
    # general-purpose defaults cannot establish reliable clinical alignment.
    if len(fields) < 4:
        raise HeaderParseError("frequency_or_sample_count_missing")
    name_parts = fields[0].rsplit("/", 1)
    try:
        segment_count = int(name_parts[1]) if len(name_parts) == 2 else None
        signal_count = int(fields[1])
        rate = Decimal(fields[2].split("/", 1)[0])
        samples = int(fields[3])
    except (ValueError, InvalidOperation):
        raise HeaderParseError("invalid_record_numbers") from None
    if signal_count < 0 or samples < 0 or not rate.is_finite() or rate <= 0 or not math.isfinite(float(rate)):
        raise HeaderParseError("invalid_record_numbers")
    if segment_count is not None and segment_count <= 0:
        raise HeaderParseError("invalid_segment_count")
    return _RecordLine(name_parts[0], segment_count, signal_count, rate, samples, tuple(fields))


def _timestamp(fields: tuple[str, ...]) -> datetime:
    if len(fields) < 6:
        raise HeaderParseError("explicit_header_date_and_time_required")
    clock = re.fullmatch(r"(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,6}))?", fields[4])
    day_first = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", fields[5])
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", fields[5])
    if clock is None or (day_first is None and iso is None):
        raise HeaderParseError("invalid_header_date_or_time")
    if day_first is not None:
        day, month, year = map(int, day_first.groups())
    else:
        year, month, day = map(int, iso.groups())
    hour, minute, second = map(int, clock.groups()[:3])
    microsecond = int((clock.group(4) or "").ljust(6, "0"))
    try:
        return datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        raise HeaderParseError("invalid_header_date_or_time") from None


def _safe_child(root: Path, parent: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise HeaderParseError("unsafe_relative_path")
    child = (parent / name).resolve()
    if not child.is_relative_to(root):
        raise HeaderParseError("path_outside_waveform_root")
    return child


def _offset(start: datetime, samples: int, rate: Decimal) -> datetime:
    try:
        return start + timedelta(seconds=float(Decimal(samples) / rate))
    except (ValueError, OverflowError):
        raise HeaderParseError("sample_duration_out_of_range") from None


def _name_timestamp_warning(name: str, start: datetime) -> str | None:
    match = re.search(r"(?:^|-)(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})(?:-(\d{2}))?(?:$|_)", name)
    if match is None:
        return None
    try:
        named = datetime(*(int(value) for value in match.groups() if value is not None))
    except ValueError:
        return "invalid_record_name_timestamp"
    header = start.replace(microsecond=0) if match.group(6) else start.replace(second=0, microsecond=0)
    return "record_name_timestamp_mismatch_header_used" if named != header else None


def parse_record_header(path: Path, waveform_root: Path) -> WaveformRecord:
    """Read a master header; its start/end are a span upper bound, including gaps.

    The explicit header date/time is authoritative. Missing dates never fall
    back to today's date or to a record filename. Filename disagreement is
    retained as a warning. Returned identifiers belong only in private caches.
    """
    root = Path(waveform_root).resolve()
    path = Path(path)
    path = path.resolve() if path.is_absolute() else _safe_child(root, root, str(path))
    if not path.is_relative_to(root):
        raise HeaderParseError("path_outside_waveform_root")
    entry = path.relative_to(root).with_suffix("").as_posix()
    subject = parse_waveform_subject(entry)
    if subject is None:
        raise HeaderParseError("patient_directory_missing_or_ambiguous")
    lines = _header_lines(path)
    line = _record_line(lines[0])
    start = _timestamp(line.fields)
    warnings: list[str] = []
    if line.name != path.stem:
        warnings.append("record_name_differs_from_header_filename")
    name_warning = _name_timestamp_warning(line.name, start)
    if name_warning:
        warnings.append(name_warning)
    segments: list[WaveformSegment] = []
    samples = line.samples
    if line.segment_count is not None:
        if len(lines) - 1 < line.segment_count:
            raise HeaderParseError("segment_lines_missing")
        offset = 0
        for index, fields in enumerate(lines[1:line.segment_count + 1]):
            if len(fields) != 2:
                raise HeaderParseError("invalid_segment_line")
            try:
                count = int(fields[1])
            except ValueError:
                raise HeaderParseError("invalid_segment_sample_count") from None
            if count < 0:
                raise HeaderParseError("invalid_segment_sample_count")
            name = fields[0]
            is_gap = name == "~"
            is_layout = index == 0 and count == 0 and not is_gap
            if count == 0 and not is_layout:
                warnings.append("zero_length_nonlayout_segment")
            leaf = None if is_gap else _safe_child(root, path.parent, name + ".hea")
            segments.append(WaveformSegment(
                name=name, start=_offset(start, offset, line.sampling_rate),
                end=_offset(start, offset + count, line.sampling_rate),
                sample_count=count, is_gap=is_gap, is_layout=is_layout, header_path=leaf,
            ))
            offset += count
        if samples == 0 and offset > 0:
            samples = offset
            warnings.append("master_sample_count_unspecified_derived_from_segments")
        elif samples != offset:
            raise HeaderParseError("master_segment_sample_count_mismatch")
    else:
        if len(lines) - 1 < line.signal_count:
            raise HeaderParseError("signal_lines_missing")
        segments.append(WaveformSegment(
            name=line.name, start=start, end=_offset(start, samples, line.sampling_rate),
            sample_count=samples, is_gap=False, is_layout=False, header_path=path,
        ))
    if samples == 0:
        warnings.append("zero_samples_no_known_span")
    return WaveformRecord(
        subject_id=subject, start=start, end=_offset(start, samples, line.sampling_rate),
        sampling_rate=float(line.sampling_rate), n_samples=samples, header_path=path,
        waveform_root=root, record_entry=entry, segments=tuple(segments), warnings=tuple(warnings),
    )


def read_record_metadata(root: Path, record_entry: str) -> WaveformRecord:
    """Read one entry from RECORDS-waveforms; do not scan other records."""
    root = Path(root).resolve()
    entry = record_entry.strip()
    if not entry:
        raise HeaderParseError("empty_record_entry")
    name = entry if entry.endswith(".hea") else entry + ".hea"
    return parse_record_header(_safe_child(root, root, name), root)


def _normalized(values: Iterable[datetime]) -> tuple[datetime, ...]:
    timestamps = tuple(values)
    if any(not isinstance(value, datetime) for value in timestamps):
        raise TypeError("interval bounds must be datetime instances")
    aware = {value.utcoffset() is not None for value in timestamps}
    if len(aware) > 1:
        raise ValueError("cannot mix timezone-aware and naive timestamps")
    return tuple(value.astimezone(timezone.utc) if value.utcoffset() is not None else value for value in timestamps)


def _intersection(first: Interval, second: Interval) -> Interval | None:
    a, b, c, d = _normalized((*first, *second))
    if b < a or d < c:
        raise ValueError("interval end must not precede start")
    start, end = max(a, c), min(b, d)
    return (start, end) if start < end else None


def overlap_with_icu(record: WaveformRecord, subject_id: int, icu_start: datetime, icu_end: datetime) -> Interval | None:
    """Patient-matched record-span overlap, not confirmed ECG coverage."""
    overlap = _intersection((record.start, record.end), (icu_start, icu_end))
    return overlap if record.subject_id == subject_id else None


def coverage_seconds(intervals: Iterable[Interval], window_start: datetime, window_end: datetime) -> float:
    """Clip half-open intervals to the target window and measure their union."""
    start, end = _normalized((window_start, window_end))
    if end < start:
        raise ValueError("window end must not precede start")
    clipped = []
    for interval in intervals:
        overlap = _intersection(interval, (start, end))
        if overlap is not None:
            clipped.append(overlap)
    if not clipped:
        return 0.0
    clipped.sort()
    total = 0.0
    left, right = clipped[0]
    for following_start, following_end in clipped[1:]:
        if following_start <= right:
            right = max(right, following_end)
        else:
            total += (right - left).total_seconds()
            left, right = following_start, following_end
    return total + (right - left).total_seconds()


def inspect_ecg_intervals(record: WaveformRecord, target_start: datetime, target_end: datetime) -> ECGInspection:
    """Inspect only overlapping non-gap/non-layout segments for exact Lead II.

    Layout declarations alone never establish signal coverage. Missing leaves,
    inconsistent counts/frequencies, null formats, and absent/empty signal
    files do not contribute intervals. Nonzero WFDB skew is conservatively
    excluded until a decoder can confirm its usable bounds. Intervals from
    several records can be passed together to coverage_seconds without double
    counting. No waveform samples are decoded by this function.
    """
    _intersection((record.start, record.end), (target_start, target_end))
    intervals: list[Interval] = []
    warnings: list[str] = []
    checked = with_ii = 0
    for segment in record.segments:
        overlap = _intersection((segment.start, segment.end), (target_start, target_end))
        if overlap is None or segment.is_gap or segment.is_layout:
            continue
        checked += 1
        try:
            if segment.header_path is None:
                raise HeaderParseError("segment_header_missing")
            lines = _header_lines(segment.header_path)
            leaf = _record_line(lines[0])
            if leaf.segment_count is not None:
                raise HeaderParseError("nested_multisegment_not_supported")
            # Both sides use the same float representation; comparing the
            # parsed Decimal with a float's string would reject equivalent
            # noninteger frequencies after a metadata cache round trip.
            if float(leaf.sampling_rate) != record.sampling_rate:
                raise HeaderParseError("segment_sampling_rate_mismatch")
            if leaf.samples != segment.sample_count:
                raise HeaderParseError("segment_sample_count_mismatch")
            if len(lines) - 1 < leaf.signal_count:
                raise HeaderParseError("signal_lines_missing")
        except HeaderParseError as error:
            warnings.append(str(error))
            continue
        lead_ii = [signal for signal in lines[1:leaf.signal_count + 1]
                   if len(signal) >= 9 and " ".join(signal[8:]).strip().upper() == "II"]
        if not lead_ii:
            warnings.append("segment_has_no_explicit_lead_ii")
            continue
        with_ii += 1
        confirmed = False
        for signal in lead_ii:
            if signal[0] in {"~", "-"}:
                warnings.append("lead_ii_has_no_signal_file")
                continue
            format_match = re.fullmatch(r"(\d+)(?:x(\d+))?(?::(\d+))?(?:\+(\d+))?", signal[1])
            if format_match is None or int(format_match.group(1)) == 0:
                warnings.append("lead_ii_invalid_or_null_format")
                continue
            if format_match.group(3) and int(format_match.group(3)) != 0:
                warnings.append("lead_ii_skew_requires_decoder")
                continue
            try:
                signal_path = _safe_child(record.waveform_root, segment.header_path.parent, signal[0])
                if not signal_path.is_file():
                    warnings.append("lead_ii_signal_file_missing")
                    continue
                if signal_path.stat().st_size <= 0:
                    warnings.append("lead_ii_signal_file_empty")
                    continue
            except (HeaderParseError, OSError):
                warnings.append("lead_ii_signal_file_unsafe_or_unreadable")
                continue
            confirmed = True
            break
        if confirmed:
            intervals.append(overlap)
    return ECGInspection(tuple(intervals), tuple(warnings), checked, with_ii)
