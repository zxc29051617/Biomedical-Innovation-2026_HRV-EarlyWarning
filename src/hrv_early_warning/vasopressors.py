"""Conservative MetaVision infusion parsing and the prespecified first-use endpoint.

This module processes rows supplied by callers; it never reads or writes source
datasets. Parsing failures are returned explicitly. In particular, an uncertain
row must not disappear into an otherwise negative patient: cohort code must
limit follow-up or exclude the affected stay after auditing its scope/time.

Column semantics are documented by the original MIMIC-III data dictionary:
https://mimic.mit.edu/docs/iii/tables/inputevents_mv.html
The endpoint and interval union below implement this repository's protocol,
not a clinical diagnosis or a norepinephrine-equivalent-dose calculation.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Iterable, Mapping

from .timing import _time_key, _validate_timestamps


PRIMARY_ITEMIDS = frozenset({221906, 221289, 222315, 221749})
ALL_FIVE_ITEMIDS = PRIMARY_ITEMIDS | {221662}
VASOPRESSIN_ITEMID = 222315

# These are dimension checks, not dose conversion factors. Retain the original
# RATEUOM in the parsed record. Volume rates identify a flowing drug solution,
# without implying that its concentration or dose has been established.
_MASS_RATE_UNITS = frozenset(
    f"{mass}/{denominator}"
    for mass in ("mcg", "mg", "g")
    for denominator in ("min", "hour", "kg/min", "kg/hour")
)
_ACTIVITY_RATE_UNITS = frozenset(
    f"{activity}/{denominator}"
    for activity in ("units", "unit", "milliunits", "mu")
    for denominator in ("min", "hour", "kg/min", "kg/hour")
)
_VOLUME_RATE_UNITS = frozenset({"ml/min", "ml/hour"})
_DELIVERED_STATUSES = frozenset({"changed", "paused", "finishedrunning", "stopped"})
_ONE_TICK = timedelta(microseconds=1)


@dataclass(frozen=True)
class Infusion:
    """A valid positive-rate interval; time intervals are half-open [start, end)."""

    subject_id: int
    hadm_id: int
    icustay_id: int | None
    itemid: int
    start: datetime
    end: datetime
    storetime: datetime | None = None
    row_id: int | None = None
    rate: float | None = None
    rate_unit: str | None = None


@dataclass(frozen=True)
class FirstUseResult:
    """First treatment exposure and the end of usable outcome follow-up.

    For competing/nonqualifying or uncertain-duration first exposure,
    ``followup_end`` is one microsecond BEFORE the exact exposure. This adapts an
    exclusive censoring boundary to ``timing.label_landmark``'s inclusive end:
    a window ending exactly at that exposure must not become a negative label.
    ``first_exposure_time`` always retains the exact observed start. Callers
    must also remove landmarks at/after it and exclude prevalent-at-entry stays.

    Empty input means no accepted exposure supplied, not verified drug absence.
    Callers remain responsible for complete table coverage and uncertain rows.
    """

    event_time: datetime | None
    first_exposure_time: datetime | None
    followup_end: datetime
    event_status: str
    primary_continuous_minutes: float = 0.0


def _text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    return "" if value is None else str(value).strip()


def _identifier(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise ValueError("identifiers must be positive")
    return value


def _timestamp(text: str) -> datetime:
    # A date without a clock must never acquire invented hour-level precision.
    if " " not in text and "T" not in text:
        raise ValueError("timestamp requires a date and clock")
    return datetime.fromisoformat(text)


def uncertain_parse_reason(reason: str) -> bool:
    """Whether a rejected row could conceal relevant exposure/follow-up.

    ``excluded_*`` reasons are explicit out-of-scope/non-delivery records;
    ``uncertain_*`` require cohort-level disposition, not an absence assumption.
    This does not decide which ICU stay a malformed row should affect.
    """
    return reason.startswith("uncertain_")


def parse_infusion_row(
    row: Mapping[str, object],
    allowed_itemids: Iterable[int] = ALL_FIVE_ITEMIDS,
) -> tuple[Infusion | None, str]:
    """Parse one INPUTEVENTS_MV CSV row, returning a stable audit reason.

    Field names are the original uppercase names. FinishedRunning, Changed,
    Paused and Stopped describe the end of a delivered interval and are retained.
    Rewritten rows were not delivered. Cancellation flags and unknown statuses
    are uncertain, rather than proof that there was no infusion. Explicit bolus
    metadata is excluded; a one-minute interval without continuous-infusion
    metadata is ambiguous because MIMIC also represents boluses as one minute.

    A valid positive RATE with a supported dimensional RATEUOM is required;
    AMOUNTUOM and ORIGINALRATE are never substitutes. Optional AMOUNT, when
    present, must agree with positive delivery. STORETIME is retained for audits
    but does not replace the treatment's STARTTIME when deriving outcomes.
    """
    allowed = frozenset(allowed_itemids)
    if not allowed <= ALL_FIVE_ITEMIDS:
        raise ValueError("allowed_itemids must be a subset of the five protocol drugs")
    try:
        itemid = _identifier(_text(row, "ITEMID"))
    except (ValueError, TypeError):
        return None, "uncertain_invalid_itemid"
    if itemid not in allowed:
        return None, "excluded_itemid"

    status = _text(row, "STATUSDESCRIPTION").casefold()
    if status == "rewritten":
        return None, "excluded_rewritten"
    category = " ".join(
        _text(row, field).casefold()
        for field in (
            "ORDERCATEGORYNAME",
            "SECONDARYORDERCATEGORYNAME",
            "ORDERCATEGORYDESCRIPTION",
        )
    )
    if "bolus" in category or "drug push" in category:
        return None, "excluded_bolus"

    cancellation = _text(row, "CANCELREASON")
    if cancellation:
        try:
            cancellation_number = float(cancellation)
        except ValueError:
            return None, "uncertain_cancellation"
        if not math.isfinite(cancellation_number) or cancellation_number != 0:
            return None, "uncertain_cancellation"
    if _text(row, "COMMENTS_CANCELEDBY") or "cancel" in _text(row, "COMMENTS_STATUS").casefold():
        return None, "uncertain_cancellation"
    if status not in _DELIVERED_STATUSES:
        return None, "uncertain_status"

    try:
        subject_id = _identifier(_text(row, "SUBJECT_ID"))
        hadm_id = _identifier(_text(row, "HADM_ID"))
        icustay_id = (
            _identifier(_text(row, "ICUSTAY_ID"))
            if _text(row, "ICUSTAY_ID") else None
        )
        row_id = _identifier(_text(row, "ROW_ID")) if _text(row, "ROW_ID") else None
    except (ValueError, TypeError):
        return None, "uncertain_invalid_identifier"

    try:
        start = _timestamp(_text(row, "STARTTIME"))
        end = _timestamp(_text(row, "ENDTIME"))
        storetime = _timestamp(_text(row, "STORETIME")) if _text(row, "STORETIME") else None
        _validate_timestamps([start, end] + ([storetime] if storetime else []))
    except (ValueError, TypeError):
        return None, "uncertain_invalid_timestamp"
    duration = _time_key(end) - _time_key(start)
    if duration <= timedelta(0):
        return None, "uncertain_nonpositive_interval"
    if duration == timedelta(minutes=1) and "continuous" not in category:
        return None, "uncertain_one_minute_route"

    raw_rate = _text(row, "RATE")
    if not raw_rate:
        return None, "uncertain_missing_rate"
    try:
        rate = float(raw_rate)
    except ValueError:
        return None, "uncertain_invalid_rate"
    if not math.isfinite(rate) or rate < 0:
        return None, "uncertain_invalid_rate"
    if rate == 0:
        # A contradictory delivered amount does not establish no exposure.
        # This row needs upstream adjudication just like a missing rate.
        zero_amount = _text(row, "AMOUNT")
        if zero_amount:
            try:
                amount = float(zero_amount)
            except ValueError:
                return None, "uncertain_invalid_amount"
            if not math.isfinite(amount) or amount < 0:
                return None, "uncertain_invalid_amount"
            if amount > 0:
                return None, "uncertain_rate_amount_conflict"
        return None, "excluded_zero_rate"
    rate_unit = _text(row, "RATEUOM")
    units = _ACTIVITY_RATE_UNITS if itemid == VASOPRESSIN_ITEMID else _MASS_RATE_UNITS
    if rate_unit.casefold() not in units | _VOLUME_RATE_UNITS:
        return None, "uncertain_rate_unit"

    raw_amount = _text(row, "AMOUNT")
    if raw_amount:
        try:
            amount = float(raw_amount)
        except ValueError:
            return None, "uncertain_invalid_amount"
        if not math.isfinite(amount) or amount <= 0:
            return None, "uncertain_invalid_amount"
    return Infusion(
        subject_id=subject_id,
        hadm_id=hadm_id,
        icustay_id=icustay_id,
        itemid=itemid,
        start=start,
        end=end,
        storetime=storetime,
        row_id=row_id,
        rate=rate,
        rate_unit=rate_unit,
    ), "accepted"


def _union_intervals(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Union overlapping or exactly adjacent intervals; never bridge a gap."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def derive_first_use(
    infusions: Iterable[Infusion],
    icu_intime: datetime,
    icu_outtime: datetime,
    primary_itemids: Iterable[int] = PRIMARY_ITEMIDS,
    min_duration_minutes: float = 60,
    *,
    maximum_gap_minutes: float = 0,
) -> FirstUseResult:
    """Derive first sustained primary-drug initiation for ONE admission/stay.

    Supply accepted intervals for one subject and hospital admission, including
    intervals that begin before ICU entry and overlap it. Upstream code must
    establish the target ICU mapping, including missing ICUSTAY_ID rows. This
    function uses [icu_intime, icu_outtime) and ignores intervals entirely outside
    it; it does not infer first use across the whole hospital admission.

    The first exposure to ANY of the five protocol drugs ends untreated risk.
    If its exact onset also begins a continuous primary-drug union lasting at
    least the threshold, that onset is the event. Simultaneous primary+dopamine
    exposure can qualify. Dopamine-only first exposure or a short first episode
    censors at that first exposure; later restarts never become a first event.
    Include dopamine in primary_itemids for the prespecified sensitivity run.

    Duration is capped at ICU follow-up: future data beyond ICU discharge cannot
    establish a sustained event. A short episode reaching the observation end
    is explicitly incomplete, distinct from an observed short course. Outcomes
    may use subsequent documentation to confirm duration; no fields here are
    intended as prediction-time model features.

    The present protocol permits no positive gap, so nonzero maximum_gap_minutes
    is rejected rather than silently changing the sustained-exposure definition.
    Naive timestamps assume an already aligned source clock; aware timestamps
    are compared in UTC elapsed time. Invalid/mixed-patient input raises errors.
    """
    if not isinstance(min_duration_minutes, (int, float)) or isinstance(min_duration_minutes, bool):
        raise TypeError("min_duration_minutes must be a positive finite number")
    if not math.isfinite(min_duration_minutes) or min_duration_minutes <= 0:
        raise ValueError("min_duration_minutes must be a positive finite number")
    if maximum_gap_minutes != 0:
        raise ValueError("the current protocol requires maximum_gap_minutes=0")
    primary = frozenset(primary_itemids)
    if not primary or not primary <= ALL_FIVE_ITEMIDS:
        raise ValueError("primary_itemids must be a nonempty subset of the five protocol drugs")
    rows = tuple(infusions)
    _validate_timestamps([icu_intime, icu_outtime])
    intime, outtime = _time_key(icu_intime), _time_key(icu_outtime)
    if outtime <= intime:
        raise ValueError("icu_outtime must be after icu_intime")
    if len({(row.subject_id, row.hadm_id) for row in rows}) > 1:
        raise ValueError("infusions must belong to one subject and hospital admission")

    overlapping: list[tuple[Infusion, datetime, datetime]] = []
    for row in rows:
        if row.itemid not in ALL_FIVE_ITEMIDS:
            raise ValueError("infusions contain a drug outside the five-drug risk definition")
        _validate_timestamps([icu_intime, row.start, row.end])
        start, end = _time_key(row.start), _time_key(row.end)
        if end <= start:
            raise ValueError("infusion end must be after start")
        if start < outtime and end > intime:
            overlapping.append((row, start, min(end, outtime)))

    if not overlapping:
        return FirstUseResult(None, None, icu_outtime, "no_exposure")
    first_row, first, _ = min(overlapping, key=lambda entry: entry[1])
    if first < intime:
        return FirstUseResult(None, first_row.start, icu_intime, "ongoing_at_icu_admission")

    primary_union = _union_intervals(
        (start, end)
        for row, start, end in overlapping
        if row.itemid in primary
    )
    first_primary = next(((start, end) for start, end in primary_union if start == first), None)
    # Use UTC elapsed-time subtraction for an aware time, including DST folds.
    censored_end = first - _ONE_TICK
    if first_primary is None:
        return FirstUseResult(None, first_row.start, censored_end, "nonprimary_first_use")
    _, primary_end = first_primary
    minutes = (primary_end - first).total_seconds() / 60
    if primary_end - first >= timedelta(minutes=min_duration_minutes):
        return FirstUseResult(first_row.start, first_row.start, icu_outtime, "qualifying_first_use", minutes)
    status = "incomplete_duration_followup" if primary_end == outtime else "short_first_use"
    return FirstUseResult(None, first_row.start, censored_end, status, minutes)
