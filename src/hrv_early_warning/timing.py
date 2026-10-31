"""Small, clinical-definition-independent helpers for retrospective time labels.

These utilities do not identify infection, derive a vasopressor episode, or
implement a clinical labeling pipeline. Callers must supply a validated first
event and the end of reliable outcome observation. Evidence timestamps must
represent when information became available; an event date or a retrospectively
paired infection timestamp does not necessarily meet that requirement.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Optional


@dataclass(frozen=True)
class LandmarkLabel:
    """A binary outcome, or an explicit reason why no binary label is valid."""

    label: Optional[Literal[0, 1]]
    status: Literal["positive", "negative", "censored", "not_at_risk"]
    reason: str


def _validate_timestamps(timestamps: Iterable[datetime]) -> None:
    awareness = set()
    for timestamp in timestamps:
        if not isinstance(timestamp, datetime):
            raise TypeError("timestamps must be datetime instances")
        awareness.add(timestamp.utcoffset() is not None)
    if len(awareness) > 1:
        raise ValueError("cannot mix timezone-aware and naive datetimes")


def _time_key(timestamp: datetime) -> datetime:
    """Use elapsed time for aware values, including across daylight saving."""
    if timestamp.utcoffset() is not None:
        return timestamp.astimezone(timezone.utc)
    return timestamp


def label_landmark(
    landmark: datetime,
    followup_end: datetime,
    first_event: Optional[datetime],
    horizon: timedelta,
) -> LandmarkLabel:
    """Label the first event in the interval ``(landmark, landmark + horizon]``.

    An event at or before the landmark makes the patient not at risk. A later
    observed event within the horizon is positive, even if observation ends
    before the full horizon. A negative label requires observation through the
    horizon with no event in it. Otherwise the label is censored, never zero.

    ``followup_end`` is the inclusive end of reliable outcome observation, not
    necessarily the end of waveform coverage. An event later than that time
    raises ValueError: callers must not supply an event unavailable within the
    stated outcome follow-up. This explicit error avoids silently using future
    information or concealing inconsistent input.

    A nonpositive horizon, follow-up before the landmark, and mixed aware/naive
    timestamps also raise ValueError. Aware values may use different timezones;
    horizons are elapsed durations calculated in UTC. Naive values are accepted
    on the assumption that the caller has already established a common clock.
    """
    if not isinstance(horizon, timedelta):
        raise TypeError("horizon must be a timedelta")
    if horizon <= timedelta(0):
        raise ValueError("horizon must be positive")

    timestamps = [landmark, followup_end]
    if first_event is not None:
        timestamps.append(first_event)
    _validate_timestamps(timestamps)

    start = _time_key(landmark)
    end = _time_key(followup_end)
    event = _time_key(first_event) if first_event is not None else None
    if end < start:
        raise ValueError("followup_end must not precede landmark")
    if event is not None and event > end:
        raise ValueError("first_event must not occur after followup_end")

    if event is not None and event <= start:
        return LandmarkLabel(
            None, "not_at_risk", "first_event_at_or_before_landmark"
        )

    horizon_end = start + horizon
    if event is not None and event <= horizon_end:
        return LandmarkLabel(1, "positive", "event_observed_within_horizon")
    if end < horizon_end:
        return LandmarkLabel(None, "censored", "followup_ends_before_horizon")
    return LandmarkLabel(0, "negative", "no_event_within_observed_horizon")


def evidence_ready_at(evidence_timestamps: Iterable[datetime]) -> datetime:
    """Return when all supplied evidence was available: the latest timestamp.

    For example, a rule requiring both culture and antibiotic evidence cannot
    become available before the later evidence timestamp. The caller must pass
    *availability* times, with any date-only uncertainty already handled. This
    function does not establish infection or infer missing timestamps.

    The input may be an iterable or generator. An empty iterable or mixed
    aware/naive values raises ValueError. The original latest datetime is
    returned, preserving its timezone representation.
    """
    timestamps = tuple(evidence_timestamps)
    if not timestamps:
        raise ValueError("at least one evidence timestamp is required")
    _validate_timestamps(timestamps)
    return max(timestamps, key=_time_key)
