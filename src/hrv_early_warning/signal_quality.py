"""Conservative engineering checks and candidate RR features for one ECG epoch.

Input is one contiguous ECG lead in physical millivolts. This module neither
classifies rhythm nor identifies normal beats: an accepted epoch is NOT proof
of sinus rhythm, absence of AF/pacing, or valid NN-based autonomic HRV. It does
not read records, join gaps, interpolate missing samples, or use outcome labels.
Thresholds are project choices, documented in docs/signal_quality_evidence.md.
"""

from dataclasses import asdict, dataclass
import math
from typing import Any

import numpy as np
from scipy.ndimage import median_filter


ALGORITHM_VERSION = "rr_quality_v1"


@dataclass(frozen=True)
class QualityConfig:
    """Serializable screening choices; freeze these before examining outcomes."""

    epoch_seconds: float = 300.0
    minimum_fs_hz: float = 100.0
    flat_difference_mv: float = 1e-7
    maximum_flat_seconds: float = 1.0
    amplitude_block_seconds: float = 10.0
    minimum_block_peak_to_peak_mv: float = 0.05
    maximum_peak_to_peak_mv: float = 10.0
    extrema_tolerance_mv: float = 1e-8
    maximum_extrema_plateau_seconds: float = 0.04
    match_tolerance_seconds: float = 0.10
    minimum_beat_agreement: float = 0.90
    agreement_block_seconds: float = 10.0
    minimum_block_beat_agreement: float = 0.80
    minimum_beats: int = 100
    maximum_edge_without_beat_seconds: float = 2.5
    minimum_rr_seconds: float = 0.30
    maximum_rr_seconds: float = 2.0
    rr_median_window: int = 11
    maximum_relative_rr_deviation: float = 0.20
    minimum_rr_valid_fraction: float = 0.95
    minimum_valid_rr_duration_fraction: float = 0.90

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in (
            "minimum_beat_agreement", "minimum_block_beat_agreement",
            "minimum_rr_valid_fraction", "minimum_valid_rr_duration_fraction",
        ):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must not exceed one")
        if self.minimum_rr_seconds >= self.maximum_rr_seconds:
            raise ValueError("minimum_rr_seconds must be less than maximum")
        if self.minimum_block_peak_to_peak_mv >= self.maximum_peak_to_peak_mv:
            raise ValueError("amplitude minimum must be less than maximum")
        if not isinstance(self.minimum_beats, int) or self.minimum_beats < 3:
            raise ValueError("minimum_beats must be an integer of at least three")
        if (not isinstance(self.rr_median_window, int)
                or self.rr_median_window < 3 or self.rr_median_window % 2 == 0):
            raise ValueError("rr_median_window must be an odd integer of at least three")
        for name in ("amplitude_block_seconds", "agreement_block_seconds"):
            quotient = self.epoch_seconds / getattr(self, name)
            if not math.isclose(quotient, round(quotient)):
                raise ValueError(f"{name} must divide epoch_seconds")


@dataclass(frozen=True)
class EpochAssessment:
    """Audit-friendly result; rejected epochs never contain feature values.

    Metrics explicitly say rhythm was not assessed, including on acceptance.
    No waveform, beat timestamps, patient identifiers, or outcomes are emitted.
    """

    accepted: bool
    reasons: tuple[str, ...]
    metrics: dict[str, Any]
    features: dict[str, float]


def _longest_true_run(mask: np.ndarray) -> int:
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False]))
    return int(np.max(edges[1::2] - edges[::2])) if len(edges) else 0


def _detect_qrs(signal_mv: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    # ImportError is deliberately not converted into a bad-ECG result.
    from wfdb.processing import gqrs_detect, xqrs_detect

    primary = xqrs_detect(signal_mv, fs, verbose=False)
    secondary = gqrs_detect(sig=signal_mv, fs=fs)
    return primary, secondary


def _validate_peaks(peaks: Any, sample_count: int) -> np.ndarray:
    array = np.asarray(peaks)
    if (array.ndim != 1 or not np.issubdtype(array.dtype, np.integer)
            or np.any(array < 0) or np.any(array >= sample_count)
            or np.any(np.diff(array) <= 0)):
        raise ValueError("detector returned invalid sample indices")
    return array.astype(np.int64, copy=False)


def _match_peaks(
    primary: np.ndarray, secondary: np.ndarray, tolerance_samples: float
) -> tuple[np.ndarray, np.ndarray]:
    """Ordered one-to-one matching; a secondary detection cannot be reused."""
    matched_primary = np.zeros(len(primary), dtype=bool)
    matched_secondary = np.zeros(len(secondary), dtype=bool)
    i = j = 0
    while i < len(primary) and j < len(secondary):
        distance = int(primary[i]) - int(secondary[j])
        if abs(distance) <= tolerance_samples:
            matched_primary[i] = matched_secondary[j] = True
            i += 1
            j += 1
        elif distance < 0:
            i += 1
        else:
            j += 1
    return matched_primary, matched_secondary


def _rr_features(rr_seconds: np.ndarray, valid: np.ndarray) -> dict[str, float]:
    """Keep original adjacency: never join RR intervals across removed data."""
    selected = rr_seconds[valid] * 1000.0
    adjacent = valid[:-1] & valid[1:]
    if len(selected) < 2 or not np.any(adjacent):
        return {}
    successive_differences = np.diff(rr_seconds)[adjacent] * 1000.0
    return {
        "mean_rr_ms": float(np.mean(selected)),
        "sdrr_ms": float(np.std(selected, ddof=1)),
        "rmssd_rr_ms": float(np.sqrt(np.mean(successive_differences ** 2))),
        "heart_rate_from_mean_rr_bpm": float(60000.0 / np.mean(selected)),
    }


def assess_ecg_epoch(
    signal_mv: Any, fs: float, config: QualityConfig | None = None
) -> EpochAssessment:
    """Screen exactly one 300-second epoch by default, then compute RR features.

    ``signal_mv`` must be a contiguous one-dimensional physical-mV array; the
    caller must establish units, chronology and absence of record gaps. A length
    other than ``round(epoch_seconds * fs)`` is rejected (at most half a sample
    rounding tolerance in duration). NaN/inf are rejected without interpolation.

    XQRS supplies beat positions; GQRS supplies a separate agreement check.
    Agreement is not an annotation-based accuracy estimate. RR range/median
    screening can remove physiological variability as well as errors: counts
    and retained duration are returned. Only originally adjacent retained RR
    intervals contribute to RMSSD. Filters use samples within this epoch, so
    callers must ensure the *entire* epoch ends at or before their cutoff.

    Malformed API inputs/configuration raise ValueError. Detector numerical or
    input errors produce ``detector_error`` with the exception class only; a
    missing package propagates, rather than masquerading as poor signal.
    """
    config = config or QualityConfig()
    if isinstance(fs, bool) or not np.isfinite(fs) or fs <= 0:
        raise ValueError("fs must be positive and finite")
    signal = np.asarray(signal_mv, dtype=float)
    if signal.ndim != 1:
        raise ValueError("signal_mv must be one-dimensional")
    fs = float(fs)
    metrics: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "sample_count": int(len(signal)),
        "fs_hz": fs,
        "duration_seconds": float(len(signal) / fs),
        "rhythm_status": "not_assessed",
        "nn_confirmed": False,
        "af_exclusion_status": "not_assessed",
        "pacing_exclusion_status": "not_assessed",
    }
    reasons: list[str] = []

    def result(features: dict[str, float] | None = None) -> EpochAssessment:
        return EpochAssessment(not reasons, tuple(reasons), metrics,
                               features if features is not None and not reasons else {})

    if len(signal) != round(config.epoch_seconds * fs):
        reasons.append("wrong_epoch_length")
    if fs < config.minimum_fs_hz:
        reasons.append("sampling_frequency_below_project_minimum")
    if not len(signal):
        reasons.append("empty_signal")
        return result()
    metrics["finite_fraction"] = float(np.mean(np.isfinite(signal)))
    if metrics["finite_fraction"] < 1:
        reasons.append("nonfinite_samples")
    if reasons:
        return result()

    flat_differences = np.abs(np.diff(signal)) <= config.flat_difference_mv
    metrics["longest_flat_seconds"] = (_longest_true_run(flat_differences) + 1) / fs
    metrics["peak_to_peak_mv"] = float(np.ptp(signal))
    amplitude_blocks = np.array_split(signal, round(config.epoch_seconds / config.amplitude_block_seconds))
    metrics["minimum_block_peak_to_peak_mv"] = float(min(np.ptp(b) for b in amplitude_blocks))
    extrema_run = max(
        _longest_true_run(np.abs(signal - extreme) <= config.extrema_tolerance_mv)
        for extreme in (np.min(signal), np.max(signal))
    )
    metrics["longest_extrema_plateau_seconds"] = extrema_run / fs
    if metrics["longest_flat_seconds"] >= config.maximum_flat_seconds:
        reasons.append("flat_run")
    if metrics["minimum_block_peak_to_peak_mv"] < config.minimum_block_peak_to_peak_mv:
        reasons.append("low_block_amplitude")
    if metrics["peak_to_peak_mv"] > config.maximum_peak_to_peak_mv:
        reasons.append("excessive_amplitude_range")
    if metrics["longest_extrema_plateau_seconds"] >= config.maximum_extrema_plateau_seconds:
        reasons.append("suspected_clipping")
    if reasons:
        return result()

    try:
        primary_raw, secondary_raw = _detect_qrs(signal, fs)
        primary = _validate_peaks(primary_raw, len(signal))
        secondary = _validate_peaks(secondary_raw, len(signal))
    except (ValueError, RuntimeError, FloatingPointError, IndexError) as error:
        metrics["detector_error_class"] = type(error).__name__
        reasons.append("detector_error")
        return result()
    metrics["primary_beat_count"] = int(len(primary))
    metrics["secondary_beat_count"] = int(len(secondary))
    matched_primary, matched_secondary = _match_peaks(
        primary, secondary, config.match_tolerance_seconds * fs
    )
    denominator = len(primary) + len(secondary)
    metrics["beat_agreement"] = float(2 * np.sum(matched_primary) / denominator) if denominator else 0.0
    block_scores = []
    for start in np.arange(0, config.epoch_seconds, config.agreement_block_seconds):
        end = start + config.agreement_block_seconds
        p_block = (primary >= start * fs) & (primary < end * fs)
        s_block = (secondary >= start * fs) & (secondary < end * fs)
        count = int(np.sum(p_block) + np.sum(s_block))
        matched_count = int(np.sum(matched_primary & p_block) + np.sum(matched_secondary & s_block))
        block_scores.append(matched_count / count if count else 0.0)
    metrics["minimum_block_beat_agreement"] = float(min(block_scores))
    metrics["low_agreement_block_count"] = sum(s < config.minimum_block_beat_agreement for s in block_scores)
    if metrics["beat_agreement"] < config.minimum_beat_agreement:
        reasons.append("low_beat_agreement")
    if metrics["low_agreement_block_count"]:
        reasons.append("low_block_beat_agreement")
    if len(primary) < config.minimum_beats or len(secondary) < config.minimum_beats:
        reasons.append("too_few_beats")
    if len(primary) < 3:
        return result()

    leading = float(primary[0] / fs)
    trailing = float((len(signal) - primary[-1]) / fs)
    metrics["leading_without_beat_seconds"] = leading
    metrics["trailing_without_beat_seconds"] = trailing
    if max(leading, trailing) > config.maximum_edge_without_beat_seconds:
        reasons.append("incomplete_beat_span")
    rr = np.diff(primary) / fs
    local_median = median_filter(rr, size=config.rr_median_window, mode="nearest")
    in_range = (rr >= config.minimum_rr_seconds) & (rr <= config.maximum_rr_seconds)
    locally_consistent = np.abs(rr - local_median) <= config.maximum_relative_rr_deviation * local_median
    agreed_ends = matched_primary[:-1] & matched_primary[1:]
    valid = in_range & locally_consistent & agreed_ends
    metrics.update({
        "rr_count": int(len(rr)),
        "rr_valid_count": int(np.sum(valid)),
        "rr_valid_fraction": float(np.mean(valid)),
        "rr_out_of_range_count": int(np.sum(~in_range)),
        "rr_local_outlier_count": int(np.sum(~locally_consistent)),
        "rr_unmatched_endpoint_count": int(np.sum(~agreed_ends)),
        "rr_original_adjacent_valid_pair_count": int(np.sum(valid[:-1] & valid[1:])),
        "rr_observed_span_seconds": float(np.sum(rr)),
        "valid_rr_duration_fraction": float(np.sum(rr[valid]) / config.epoch_seconds),
    })
    if metrics["rr_valid_fraction"] < config.minimum_rr_valid_fraction:
        reasons.append("too_many_screened_rr_intervals")
    if metrics["valid_rr_duration_fraction"] < config.minimum_valid_rr_duration_fraction:
        reasons.append("insufficient_valid_rr_duration")
    features = _rr_features(rr, valid)
    if not features:
        reasons.append("insufficient_adjacent_rr_intervals")
    return result(features)
