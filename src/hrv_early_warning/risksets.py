"""Complete hourly untreated risk sets and an immutable patient split manifest.

Input/output patient-level artifacts belong in ignored local directories. Splits
use the entire adult cohort, before outcome uncertainty, ECG availability or
signal-quality selection. Neither labels nor waveform availability determine a
patient's assignment. The optional waveform annotation covers past record spans,
not ECG channel presence, continuity, quality or rhythm eligibility.
"""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Iterable, Iterator, Mapping

from .cohort import StudyStay
from .timing import _time_key, _validate_timestamps, label_landmark
from .vasopressors import FirstUseResult


SPLITS = ("train", "validation", "test")
LABELS = ("positive", "negative", "censored")
_OUTCOME_STATUSES = frozenset({
    "no_exposure", "qualifying_first_use", "short_first_use", "nonprimary_first_use",
    "ongoing_at_icu_admission", "incomplete_duration_followup",
})


@dataclass(frozen=True)
class StayOutcome:
    stay: StudyStay
    outcome: FirstUseResult | None
    uncertain_reasons: tuple[str, ...] = ()
    matched_waveform_records: int = 0

    @property
    def subject_id(self) -> int:
        return self.stay.subject_id

    @property
    def hadm_id(self) -> int:
        return self.stay.hadm_id

    @property
    def icustay_id(self) -> int:
        return self.stay.icustay_id

    @property
    def intime(self) -> datetime:
        return self.stay.intime

    @property
    def followup_end(self) -> datetime:
        """Physical observation end, before first-exposure risk-set stopping."""
        return self.stay.followup_end

    @property
    def event_time(self) -> datetime | None:
        return self.outcome.event_time if self.outcome else None

    @property
    def first_exposure_time(self) -> datetime | None:
        return self.outcome.first_exposure_time if self.outcome else None

    @property
    def age_years(self) -> int | None:
        return self.stay.age_years

    @property
    def age_obscured(self) -> bool:
        return self.stay.age_obscured


@dataclass(frozen=True)
class RiskWindow:
    subject_id: int
    hadm_id: int
    icustay_id: int
    landmark: datetime
    history_start: datetime
    horizon_end: datetime
    label: str
    split: str
    event_time: datetime | None
    waveform_overlap_seconds: float | None = None

    @property
    def history_end(self) -> datetime:
        return self.landmark

    @property
    def status(self) -> str:
        return self.label

    @property
    def target(self) -> int | None:
        return {"positive": 1, "negative": 0, "censored": None}[self.label]


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        raise ValueError("timestamp_requires_clock")
    return datetime.fromisoformat(value)


def _positive_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("invalid_identifier")
    return value


def _validate_record(record: StayOutcome) -> None:
    stay, outcome = record.stay, record.outcome
    for value in (stay.subject_id, stay.hadm_id, stay.icustay_id):
        _positive_id(value)
    _validate_timestamps([stay.intime, stay.outtime, stay.followup_end])
    if not (_time_key(stay.intime) < _time_key(stay.followup_end) <= _time_key(stay.outtime)):
        raise ValueError("invalid_stay_followup")
    if not isinstance(stay.age_obscured, bool):
        raise ValueError("invalid_age_obscured")
    if stay.age_obscured:
        if stay.age_years is not None:
            raise ValueError("obscured_age_must_not_be_numeric")
    elif (not isinstance(stay.age_years, int) or isinstance(stay.age_years, bool)
          or not 18 <= stay.age_years <= 89):
        raise ValueError("invalid_adult_age")
    if not all(isinstance(reason, str) and reason for reason in record.uncertain_reasons):
        raise ValueError("invalid_uncertainty_reasons")
    if not isinstance(record.matched_waveform_records, int) or record.matched_waveform_records < 0:
        raise ValueError("invalid_waveform_count")
    if outcome is None:
        return  # Missing outcome is an explicit whole-stay exclusion.
    if outcome.event_status not in _OUTCOME_STATUSES:
        raise ValueError("unknown_outcome_status")
    clocks = [stay.intime, stay.followup_end, outcome.followup_end]
    clocks += [value for value in (outcome.event_time, outcome.first_exposure_time) if value is not None]
    _validate_timestamps(clocks)
    if _time_key(outcome.followup_end) > _time_key(stay.followup_end):
        raise ValueError("outcome_followup_exceeds_observed_followup")
    first, event = outcome.first_exposure_time, outcome.event_time
    if outcome.event_status == "no_exposure":
        if first is not None or event is not None or outcome.followup_end != stay.followup_end:
            raise ValueError("inconsistent_no_exposure")
    elif outcome.event_status == "ongoing_at_icu_admission":
        if first is None or _time_key(first) >= _time_key(stay.intime) or event is not None:
            raise ValueError("inconsistent_ongoing_exposure")
    else:
        if first is None or not (_time_key(stay.intime) <= _time_key(first) < _time_key(stay.followup_end)):
            raise ValueError("invalid_first_exposure")
        if outcome.event_status == "qualifying_first_use":
            if event is None or _time_key(event) != _time_key(first) or _time_key(event) > _time_key(outcome.followup_end):
                raise ValueError("inconsistent_qualifying_event")
        elif event is not None or _time_key(outcome.followup_end) >= _time_key(first):
            raise ValueError("nonqualifying_exposure_requires_exclusive_censoring")


def load_stay_outcomes(path: str | Path) -> tuple[list[StayOutcome], dict[str, int]]:
    """Read the full private feasibility hand-off, retaining uncertain adults.

    Invalid or duplicate records fail explicitly instead of silently shrinking
    the population used to establish the split. Error messages contain a source
    line number but never a patient identifier or the original row contents.
    """
    records: list[StayOutcome] = []
    seen: set[int] = set()
    stats: Counter[str] = Counter({"adult_stays": 0, "uncertain_stays": 0, "missing_outcome_stays": 0})
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                stay_values = dict(raw["stay"])
                for key in ("intime", "outtime", "followup_end"):
                    stay_values[key] = _parse_timestamp(stay_values[key])
                stay = StudyStay(**stay_values)
                outcome_values = raw.get("outcome")
                outcome = None
                if outcome_values is not None:
                    outcome_values = dict(outcome_values)
                    for key in ("event_time", "first_exposure_time", "followup_end"):
                        if outcome_values[key] is not None:
                            outcome_values[key] = _parse_timestamp(outcome_values[key])
                    outcome = FirstUseResult(**outcome_values)
                reasons = raw.get("uncertain_reasons", [])
                if not isinstance(reasons, list):
                    raise ValueError("invalid_uncertainty_reasons")
                record = StayOutcome(stay, outcome, tuple(reasons), raw.get("matched_waveform_records", 0))
                _validate_record(record)
                if stay.icustay_id in seen:
                    raise ValueError("duplicate_icu")
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ValueError(f"invalid_stay_outcome_record_at_line_{line_number}") from None
            seen.add(stay.icustay_id)
            records.append(record)
            stats["adult_stays"] += 1
            stats["uncertain_stays"] += bool(record.uncertain_reasons)
            stats["missing_outcome_stays"] += record.outcome is None
    stats["adult_subjects"] = len({record.subject_id for record in records})
    return records, dict(stats)


def _records_tuple(records: Iterable[StayOutcome]) -> tuple[StayOutcome, ...]:
    result = tuple(records)
    seen: set[int] = set()
    for record in result:
        _validate_record(record)
        if record.icustay_id in seen:
            raise ValueError("duplicate_stay_in_population")
        seen.add(record.icustay_id)
    return result


def _sha_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def ensure_patient_split(
    records: Iterable[StayOutcome],
    manifest_path: str | Path,
    *,
    seed: int = 20261101,
    test_fraction: float = 0.2,
    validation_fraction_of_development: float = 0.2,
) -> tuple[dict[int, str], dict[str, object]]:
    """Create once or verify a stable private split for ALL adult subjects.

    Ranked SHA256(seed, split stage, SUBJECT_ID) selects ceil(20%) for test and
    then ceil(20%) of the remaining development subjects for validation. The rest
    train. Ranking is stable across input order, Python versions and labels.
    A fingerprint locks the complete adult stay identities/times/age metadata,
    including uncertain and waveform-absent stays. Changed cohorts, parameters,
    corrupted or edited assignments require an explicit new study/version; this
    function never replaces an existing manifest. Outcome/ECG changes do not
    alter assignments when the adult cohort itself has not changed.

    Pass an ignored local manifest path. Publication is atomic and exclusive;
    concurrent identical creation is safe, and interrupted temporary files do
    not become a valid manifest. This is the only writing function in this module.
    """
    records = _records_tuple(records)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("split_seed_must_be_integer")
    for fraction in (test_fraction, validation_fraction_of_development):
        if not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or not 0 < fraction < 1:
            raise ValueError("split_fractions_must_be_between_zero_and_one")
    subjects = sorted({record.subject_id for record in records})
    cohort = []
    for record in sorted(records, key=lambda item: item.icustay_id):
        values = asdict(record.stay)
        for key in ("intime", "outtime", "followup_end"):
            values[key] = _time_key(values[key]).isoformat()
        cohort.append(values)

    def ranked(values: Iterable[int], stage: str) -> list[int]:
        return sorted(values, key=lambda subject: (hashlib.sha256(f"{seed}:{stage}:{subject}".encode("ascii")).digest(), subject))

    n_test = math.ceil(len(subjects) * test_fraction)
    test = set(ranked(subjects, "test")[:n_test])
    development = [subject for subject in subjects if subject not in test]
    n_validation = math.ceil(len(development) * validation_fraction_of_development)
    validation = set(ranked(development, "validation")[:n_validation])
    if not test or not validation or len(development) <= len(validation):
        raise ValueError("population_too_small_for_three_nonempty_splits")
    assignments = {subject: "test" if subject in test else "validation" if subject in validation else "train" for subject in subjects}
    split_counts = {split: sum(value == split for value in assignments.values()) for split in SPLITS}
    expected = {
        "schema_version": 1, "algorithm": "sha256_rank_v1", "split_unit": "SUBJECT_ID",
        "seed": seed, "test_fraction": test_fraction,
        "validation_fraction_of_development": validation_fraction_of_development,
        "adult_subjects": len(subjects), "adult_stays": len(records),
        "population_sha256": _sha_json(subjects), "cohort_sha256": _sha_json(cohort),
        "split_counts": split_counts,
        "assignments": {str(subject): split for subject, split in assignments.items()},
    }
    path = Path(manifest_path)
    reused = path.exists()
    if not reused:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".split-manifest-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(expected, target, sort_keys=True, indent=2)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                reused = True
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ValueError("patient_split_manifest_unreadable_or_corrupt") from None
    if actual != expected:
        raise ValueError("patient_split_manifest_mismatch_do_not_resplit_or_overwrite")
    return assignments, {
        "adult_subjects": len(subjects), "adult_stays": len(records), "split_counts": split_counts,
        "seed": seed, "algorithm": expected["algorithm"], "reused_manifest": reused,
        "population_sha256": expected["population_sha256"], "cohort_sha256": expected["cohort_sha256"],
    }


def _durations(lookback_hours: float, horizon_hours: float, step_hours: float) -> tuple[timedelta, timedelta, timedelta]:
    values = []
    for hours in (lookback_hours, horizon_hours, step_hours):
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours <= 0:
            raise ValueError("window_hours_must_be_positive_finite_numbers")
        value = timedelta(hours=hours)
        if value <= timedelta(0):
            raise ValueError("window_duration_below_clock_resolution")
        values.append(value)
    return tuple(values)


def _count_before(first: datetime, exclusive_end: datetime, step: timedelta) -> int:
    length = exclusive_end - first
    return 0 if length <= timedelta(0) else length // step + bool(length % step)


def _window_plan(record: StayOutcome, lookback: timedelta, step: timedelta) -> tuple[datetime, int, int, str | None]:
    first_landmark = _time_key(record.intime) + lookback
    nominal = _count_before(first_landmark, _time_key(record.followup_end), step)
    if record.uncertain_reasons:
        return first_landmark, nominal, 0, "uncertain_stay"
    if record.outcome is None:
        return first_landmark, nominal, 0, "missing_outcome"
    if record.outcome.event_status == "ongoing_at_icu_admission":
        return first_landmark, nominal, 0, "ongoing_at_icu_admission"
    first_exposure = record.first_exposure_time
    risk_count = nominal if first_exposure is None else min(nominal, _count_before(first_landmark, _time_key(first_exposure), step))
    return first_landmark, nominal, risk_count, None


def _prepare_waveforms(
    intervals: Mapping[int, Iterable[tuple[datetime, datetime]]] | None,
) -> dict[int, tuple[tuple[datetime, datetime], ...]] | None:
    if intervals is None:
        return None
    result = {}
    for subject, entries in intervals.items():
        _positive_id(subject)
        values = []
        for start, end in entries:
            _validate_timestamps([start, end])
            start, end = _time_key(start), _time_key(end)
            if end <= start:
                raise ValueError("invalid_waveform_record_interval")
            values.append((start, end))
        merged: list[tuple[datetime, datetime]] = []
        for start, end in sorted(values):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        result[subject] = tuple(merged)
    return result


def _overlap_seconds(intervals: Iterable[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    seconds = 0.0
    for left, right in intervals:
        _validate_timestamps([start, end, left, right])
        if left >= end:
            break
        if right > start:
            seconds += (min(end, right) - max(start, left)).total_seconds()
    return seconds


def _iterate_windows(records, assignments, lookback, horizon, step, waveforms):
    for record in records:
        if assignments.get(record.subject_id) not in SPLITS:
            raise ValueError("subject_missing_from_patient_split")
        first, _, count, _ = _window_plan(record, lookback, step)
        for index in range(count):
            landmark = first + index * step
            decision = label_landmark(landmark, record.outcome.followup_end, record.event_time, horizon)
            if decision.status not in LABELS:
                raise ValueError("unexpected_treated_landmark_in_riskset")
            history_start = landmark - lookback
            overlap = None if waveforms is None else _overlap_seconds(waveforms.get(record.subject_id, ()), history_start, landmark)
            yield RiskWindow(record.subject_id, record.hadm_id, record.icustay_id, landmark,
                             history_start, landmark + horizon, decision.status,
                             assignments[record.subject_id], record.event_time, overlap)


def iter_risk_windows(
    records: Iterable[StayOutcome],
    splits: Mapping[int, str],
    *,
    lookback_hours: float = 6,
    horizon_hours: float = 6,
    step_hours: float = 1,
    waveform_intervals: Mapping[int, Iterable[tuple[datetime, datetime]]] | None = None,
) -> Iterator[RiskWindow]:
    """Yield every eligible hourly window, including negatives and censoring.

    The grid starts at ICU INTIME + lookback, stops strictly before physical
    follow-up and never includes a landmark at/after any first exposure. It is
    not aligned backwards from events or discharge. Whole stays with uncertain
    or absent outcomes and prevalent-at-entry treatment are excluded. Censored
    windows remain explicit and must not be used as binary negatives.

    Optional waveform intervals annotate the UNION of record spans overlapping
    [landmark-lookback, landmark). They never select or delete a window. A zero
    annotation means no past span overlap, not a negative outcome or a quality
    decision. All timestamps are elapsed-time aligned for timezone-aware input.
    """
    records = _records_tuple(records)
    lookback, horizon, step = _durations(lookback_hours, horizon_hours, step_hours)
    yield from _iterate_windows(records, splits, lookback, horizon, step, _prepare_waveforms(waveform_intervals))


def summarize_riskset(
    records: Iterable[StayOutcome],
    splits: Mapping[int, str],
    *,
    lookback_hours: float = 6,
    horizon_hours: float = 6,
    step_hours: float = 1,
    waveform_intervals: Mapping[int, Iterable[tuple[datetime, datetime]]] | None = None,
) -> dict[str, object]:
    """Return aggregates only, with patient split and full exclusion accounting."""
    records = _records_tuple(records)
    lookback, horizon, step = _durations(lookback_hours, horizon_hours, step_hours)
    waveforms = _prepare_waveforms(waveform_intervals)
    counts = Counter({
        "adult_stays": len(records), "adult_subjects": len({r.subject_id for r in records}),
        "nominal_windows": 0, "risk_windows": 0, "stays_without_full_history": 0,
        "uncertain_stays_excluded": 0, "missing_outcome_stays_excluded": 0,
        "ongoing_stays_excluded": 0, "excluded_windows_uncertain_stay": 0,
        "excluded_windows_missing_outcome": 0, "excluded_windows_ongoing": 0,
        "excluded_windows_at_or_after_first_exposure": 0,
        "qualifying_events": 0, "qualifying_events_at_or_before_first_landmark": 0,
    })
    by_split = {split: Counter({"subjects": 0, "stays": 0, "risk_windows": 0,
                               "qualifying_events": 0, "positive": 0, "negative": 0, "censored": 0,
                               "windows_with_past_waveform_overlap": 0,
                               "windows_with_full_record_span_history": 0}) for split in SPLITS}
    subjects = defaultdict(set)
    event_stays_with_positive = defaultdict(set)
    event_stays_with_waveform_positive = defaultdict(set)
    contributing_stays = set()
    labels = Counter(dict.fromkeys(LABELS, 0))
    waveform_labels = Counter(dict.fromkeys(LABELS, 0))
    for record in records:
        split = splits.get(record.subject_id)
        if split not in SPLITS:
            raise ValueError("subject_missing_from_patient_split")
        subjects[split].add(record.subject_id)
        by_split[split]["stays"] += 1
        first, nominal, risk_count, exclusion = _window_plan(record, lookback, step)
        counts["nominal_windows"] += nominal
        counts["stays_without_full_history"] += nominal == 0
        if exclusion:
            stay_key, window_key = {
                "uncertain_stay": ("uncertain_stays_excluded", "excluded_windows_uncertain_stay"),
                "missing_outcome": ("missing_outcome_stays_excluded", "excluded_windows_missing_outcome"),
                "ongoing_at_icu_admission": ("ongoing_stays_excluded", "excluded_windows_ongoing"),
            }[exclusion]
            counts[stay_key] += 1
            counts[window_key] += nominal
        else:
            counts["excluded_windows_at_or_after_first_exposure"] += nominal - risk_count
            if record.event_time is not None:
                counts["qualifying_events"] += 1
                by_split[split]["qualifying_events"] += 1
                counts["qualifying_events_at_or_before_first_landmark"] += _time_key(record.event_time) <= first
    for window in _iterate_windows(records, splits, lookback, horizon, step, waveforms):
        counts["risk_windows"] += 1
        labels[window.label] += 1
        by_split[window.split]["risk_windows"] += 1
        by_split[window.split][window.label] += 1
        contributing_stays.add(window.icustay_id)
        if window.label == "positive":
            event_stays_with_positive[window.split].add(window.icustay_id)
        if window.waveform_overlap_seconds is not None and window.waveform_overlap_seconds > 0:
            waveform_labels[window.label] += 1
            by_split[window.split]["windows_with_past_waveform_overlap"] += 1
            if window.label == "positive":
                event_stays_with_waveform_positive[window.split].add(window.icustay_id)
            if window.waveform_overlap_seconds >= lookback.total_seconds():
                by_split[window.split]["windows_with_full_record_span_history"] += 1
    for split in SPLITS:
        by_split[split]["subjects"] = len(subjects[split])
        by_split[split]["events_with_positive_window"] = len(event_stays_with_positive[split])
        by_split[split]["events_with_positive_window_and_past_waveform_overlap"] = len(event_stays_with_waveform_positive[split])
    counts["stays_with_risk_windows"] = len(contributing_stays)
    counts["events_with_positive_window"] = sum(map(len, event_stays_with_positive.values()))
    return {
        **dict(counts), "label_counts": dict(labels), "by_split": {key: dict(value) for key, value in by_split.items()},
        "lookback_hours": lookback_hours, "horizon_hours": horizon_hours, "step_hours": step_hours,
        "waveform_annotation": "past_record_span_union_only_not_channel_or_signal_quality" if waveforms is not None else "not_requested",
        "windows_with_past_waveform_overlap": sum(waveform_labels.values()) if waveforms is not None else None,
        "waveform_overlap_label_counts": dict(waveform_labels) if waveforms is not None else None,
    }
