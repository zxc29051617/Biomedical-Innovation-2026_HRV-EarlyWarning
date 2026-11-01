"""Experimental fiducial alignment and annotation-based timing measurements.

Independent of the frozen v1 signal screen and prediction models. A candidate
is not a new beat detector, rhythm classifier, or clinically validated repair.
"""

from collections import Counter
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
from pathlib import Path

import numpy as np
import scipy
from scipy.ndimage import median_filter
from scipy.signal import find_peaks, resample_poly


ALGORITHM_VERSION = "consistent_extremum_candidate_v0.1.0"
METRIC_VERSION = "beat_timing_metrics_v0.1.1"
MATCH_COMPARISON_EPSILON_SECONDS = 1e-12


@dataclass(frozen=True)
class TimingConfig:
    search_seconds: float = 0.080
    baseline_seconds: float = 0.200
    polarity_dominance: float = 1.1
    opposite_dominance: float = 1.5
    competing_peak_fraction: float = 0.9
    competing_peak_separation_seconds: float = 0.016
    maximum_plateau_seconds: float = 0.024
    maximum_half_height_width_seconds: float = 0.120
    minimum_separation_seconds: float = 0.200

    def __post_init__(self):
        values = asdict(self)
        if any(not np.isfinite(value) or value <= 0 for value in values.values()):
            raise ValueError("timing_config_must_be_positive_and_finite")
        if self.polarity_dominance <= 1 or self.opposite_dominance <= 1:
            raise ValueError("polarity_ratios_must_exceed_one")
        if self.competing_peak_fraction > 1:
            raise ValueError("competing_peak_fraction_must_not_exceed_one")


@dataclass
class TimingResult:
    seeds: np.ndarray
    indices: np.ndarray
    valid: np.ndarray
    reasons: list
    polarity: int
    metadata: dict

    def summary(self):
        return {"seed_count": len(self.seeds), "candidate_count": int(self.valid.sum()),
                "abstained_count": int((~self.valid).sum()), "polarity": self.polarity,
                "reason_counts": dict(Counter(reason for row in self.reasons for reason in row)),
                "metadata": self.metadata}


def provenance(config=None):
    config = config or TimingConfig()
    return {"algorithm_version": ALGORITHM_VERSION, "metric_version": METRIC_VERSION,
            "config": asdict(config), "numpy_version": np.__version__,
            "scipy_version": scipy.__version__,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "validation_status": "experimental_not_independently_validated",
            "threshold_status": "engineering_defaults_not_clinical_acceptance_limits"}


def _ordered(values, name, *, integer=False):
    values = np.asarray(values)
    if values.ndim != 1 or not np.all(np.isfinite(values)):
        raise ValueError(f"{name}_must_be_finite_1d")
    if integer and (np.any(values != np.floor(values)) or np.any(values < 0)):
        raise ValueError(f"{name}_must_be_nonnegative_integer_indices")
    if np.any(np.diff(values) <= 0):
        raise ValueError(f"{name}_must_be_strictly_increasing")
    return values.astype(np.int64 if integer else float)


def _sampling_rate(fs):
    if isinstance(fs, bool) or not np.isfinite(fs) or fs <= 0:
        raise ValueError("sampling_rate_must_be_positive_and_finite")
    return float(fs)


def align_fiducials(signal, fs, seeds, config=None):
    """Align XQRS seeds to one polarity; preserve seed identity on abstention.

    Rejected points have index -1, not a silently clipped/duplicated replacement.
    Wide, truncated and multipeak morphology is flagged rather than diagnosed.
    """
    config = config or TimingConfig()
    fs = _sampling_rate(fs)
    signal = np.asarray(signal, dtype=float)
    if signal.ndim != 1 or not len(signal):
        raise ValueError("signal_must_be_nonempty_1d")
    seeds = _ordered(seeds, "seeds", integer=True)
    if len(seeds) and seeds[-1] >= len(signal):
        raise ValueError("seed_outside_signal")
    reasons = [[] for _ in seeds]
    indices = np.full(len(seeds), -1, dtype=np.int64)
    metadata = provenance(config)
    metadata.update(fs_hz=fs, sample_step_ms=1000 / fs)

    def result(polarity):
        valid = np.array([not row and point >= 0 for row, point in zip(reasons, indices)], dtype=bool)
        indices[~valid] = -1
        return TimingResult(seeds, indices, valid, reasons, polarity, metadata)

    if not np.all(np.isfinite(signal)):
        for row in reasons:
            row.append("nonfinite_signal")
        return result(0)
    radius = max(1, int(round(config.search_seconds * fs)))
    baseline_size = max(3, int(round(config.baseline_seconds * fs)) | 1)
    detrended = signal - median_filter(signal, size=baseline_size, mode="reflect")
    windows = []
    for seed, row in zip(seeds, reasons):
        if seed - radius < 0 or seed + radius >= len(signal):
            row.append("truncated_search_window")
            windows.append(None)
        else:
            windows.append(detrended[seed - radius:seed + radius + 1])
    complete = [window for window in windows if window is not None]
    if not complete:
        return result(0)
    up = float(np.median([max(0.0, window.max()) for window in complete]))
    down = float(np.median([max(0.0, -window.min()) for window in complete]))
    if max(up, down) <= np.finfo(float).eps or max(up, down) < config.polarity_dominance * min(up, down):
        for row in reasons:
            row.append("ambiguous_epoch_polarity")
        return result(0)
    polarity = 1 if up > down else -1
    metadata.update(positive_amplitude_median=up, negative_amplitude_median=down)
    for index, (seed, window, row) in enumerate(zip(seeds, windows, reasons)):
        if window is None:
            continue
        directed = polarity * window
        height = float(directed.max())
        if height <= np.finfo(float).eps:
            row.append("no_peak_in_selected_polarity")
            continue
        if -float(directed.min()) > config.opposite_dominance * height:
            row.append("opposite_polarity_dominates")
        maxima = np.flatnonzero(np.isclose(directed, height, rtol=1e-7, atol=1e-12))
        peak = int(round(float(np.median(maxima))))
        if maxima[0] == 0 or maxima[-1] == len(directed) - 1:
            row.append("maximum_on_search_boundary")
        if np.any(np.diff(maxima) > 1):
            row.append("disjoint_equal_maxima")
        if len(maxima) / fs >= config.maximum_plateau_seconds:
            row.append("wide_peak_plateau")
        alternatives, _ = find_peaks(directed, height=config.competing_peak_fraction * height)
        if any(abs(int(other) - peak) / fs >= config.competing_peak_separation_seconds for other in alternatives):
            row.append("competing_peak")
        left = peak
        while left > 0 and directed[left - 1] >= height / 2:
            left -= 1
        right = peak
        while right + 1 < len(directed) and directed[right + 1] >= height / 2:
            right += 1
        if left == 0 or right == len(directed) - 1:
            row.append("half_height_width_truncated")
        if (right - left + 1) / fs > config.maximum_half_height_width_seconds:
            row.append("broad_half_height_peak")
        indices[index] = int(seed - radius + peak)
    # Check all located seeds before discarding invalid ones, so a duplicate or
    # reversal cannot disappear through sequential filtering.
    located = np.flatnonzero(indices >= 0)
    for before, after in zip(located[:-1], located[1:]):
        separation = (indices[after] - indices[before]) / fs
        if separation <= 0:
            reasons[before].append("duplicate_or_reordered_peak")
            reasons[after].append("duplicate_or_reordered_peak")
        elif separation < config.minimum_separation_seconds:
            reasons[before].append("neighbor_too_close")
            reasons[after].append("neighbor_too_close")
    return result(polarity)


def match_beats(reference_seconds, detected_seconds, tolerance_seconds=0.1):
    """Ordered one-to-one maximum-cardinality, minimum-total-error pairing.

    The tolerance defines eligibility for matching, NOT timing accuracy.
    Intended for five-minute epochs (quadratic dynamic-programming storage).
    """
    reference = _ordered(reference_seconds, "reference_times")
    detected = _ordered(detected_seconds, "detected_times")
    if not np.isfinite(tolerance_seconds) or tolerance_seconds < 0:
        raise ValueError("match_tolerance_must_be_nonnegative_and_finite")
    n, m = len(reference), len(detected)
    if (n + 1) * (m + 1) > 4_000_000:
        raise ValueError("matching_window_too_large_use_five_minute_epochs")
    counts = np.zeros((n + 1, m + 1), dtype=np.int32)
    errors = np.zeros((n + 1, m + 1), dtype=float)
    action = np.zeros((n + 1, m + 1), dtype=np.uint8)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            count, error, move = counts[i - 1, j], errors[i - 1, j], 1
            c, e = counts[i, j - 1], errors[i, j - 1]
            if c > count or (c == count and e < error):
                count, error, move = c, e, 2
            difference = abs(reference[i - 1] - detected[j - 1])
            # Absorb floating subtraction error at the inclusive tolerance
            # boundary (e.g. 396/360 - 360/360 > 0.1 by ~8e-17 seconds).
            if difference <= tolerance_seconds + MATCH_COMPARISON_EPSILON_SECONDS:
                c, e = counts[i - 1, j - 1] + 1, errors[i - 1, j - 1] + difference
                if c > count or (c == count and e <= error):
                    count, error, move = c, e, 3
            counts[i, j], errors[i, j], action[i, j] = count, error, move
    pairs = []
    i, j = n, m
    while i and j:
        move = action[i, j]
        if move == 3:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif move == 1:
            i -= 1
        else:
            j -= 1
    return np.asarray(pairs[::-1], dtype=np.int64).reshape(-1, 2)


def _error_summary(error):
    error = np.asarray(error, dtype=float)
    if not len(error):
        return {"count": 0, "signed_bias_ms": None, "mae_ms": None,
                "absolute_median_ms": None, "absolute_p95_ms": None, "absolute_max_ms": None}
    absolute = np.abs(error)
    return {"count": len(error), "signed_bias_ms": float(error.mean()),
            "mae_ms": float(absolute.mean()), "absolute_median_ms": float(np.median(absolute)),
            "absolute_p95_ms": float(np.percentile(absolute, 95)),
            "absolute_max_ms": float(absolute.max())}


def rr_features(times, identities=None):
    """RR features without joining gaps in the original beat/seed sequence."""
    times = _ordered(times, "times")
    ids = _identities(identities, len(times), "identities")
    differences = np.diff(times) * 1000.0
    adjacent = np.diff(ids) == 1
    intervals = differences[adjacent]
    triplets = adjacent[:-1] & adjacent[1:]
    rr_changes = np.diff(differences)[triplets]
    return {"beat_count": len(times), "adjacent_rr_count": len(intervals),
            "adjacent_triplet_count": int(triplets.sum()),
            "mean_rr_ms": float(np.mean(intervals)) if len(intervals) else None,
            "sdrr_ms": float(np.std(intervals, ddof=1)) if len(intervals) > 1 else None,
            "rmssd_rr_ms": float(np.sqrt(np.mean(rr_changes ** 2))) if len(rr_changes) else None}


def _identities(identities, length, name):
    ids = np.arange(length) if identities is None else _ordered(identities, name, integer=True)
    if len(ids) != length:
        raise ValueError(f"{name}_length_mismatch")
    return ids


def evaluate_timing(reference_seconds, detected_seconds, *, tolerance_seconds=0.1,
                    reference_ids=None, detected_ids=None, duration_seconds=None,
                    full_detected_available=True):
    """Separate detection, timing, adjacent RR and full-window RR metrics.

    For rejected candidate seeds pass their surviving original seed IDs, and
    full_detected_available=False; otherwise holes could masquerade as adjacency.
    Calling this with another detector gives disagreement, never accuracy.
    """
    reference = _ordered(reference_seconds, "reference_times")
    detected = _ordered(detected_seconds, "detected_times")
    reference_ids = _identities(reference_ids, len(reference), "reference_ids")
    detected_ids = _identities(detected_ids, len(detected), "detected_ids")
    if duration_seconds is not None:
        if not np.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_must_be_positive_and_finite")
        if any(len(times) and (times[0] < 0 or times[-1] >= duration_seconds) for times in (reference, detected)):
            raise ValueError("beat_time_outside_half_open_window")
    pairs = match_beats(reference, detected, tolerance_seconds)
    n = len(pairs)
    sensitivity = n / len(reference) if len(reference) else None
    ppv = n / len(detected) if len(detected) else None
    f1 = 2 * n / (len(reference) + len(detected)) if len(reference) + len(detected) else None
    result = {"metric_version": METRIC_VERSION, "match_tolerance_ms": tolerance_seconds * 1000,
              "matching_numeric_slack_seconds": MATCH_COMPARISON_EPSILON_SECONDS,
              "matching_rule": "ordered_maximum_cardinality_then_minimum_absolute_error",
              "matching_tolerance_is_accuracy_threshold": False,
              "detection": {"reference_count": len(reference), "detected_count": len(detected),
                            "matched_count": n, "missed_count": len(reference) - n,
                            "extra_count": len(detected) - n,
                            "sensitivity": sensitivity, "ppv": ppv, "f1": f1}}
    ref = reference[pairs[:, 0]]
    det = detected[pairs[:, 1]]
    result["timing_error"] = _error_summary((det - ref) * 1000)
    adjacent = ((np.diff(reference_ids[pairs[:, 0]]) == 1)
                & (np.diff(detected_ids[pairs[:, 1]]) == 1))
    rr_ref, rr_det = np.diff(ref) * 1000, np.diff(det) * 1000
    result["shared_adjacent_rr_error"] = _error_summary((rr_det - rr_ref)[adjacent])
    triplets = adjacent[:-1] & adjacent[1:]
    dr_ref, dr_det = np.diff(rr_ref)[triplets], np.diff(rr_det)[triplets]
    rmssd_ref = float(np.sqrt(np.mean(dr_ref ** 2))) if len(dr_ref) else None
    rmssd_det = float(np.sqrt(np.mean(dr_det ** 2))) if len(dr_det) else None
    result["shared_adjacent_triplets"] = {
        "count": len(dr_ref), "reference_rmssd_rr_ms": rmssd_ref,
        "detected_rmssd_rr_ms": rmssd_det,
        "rmssd_difference_ms": rmssd_det - rmssd_ref if rmssd_ref is not None else None,
        "rr_change_error": _error_summary(dr_det - dr_ref)}
    five_minutes = duration_seconds is not None and np.isclose(duration_seconds, 300, rtol=0, atol=1e-9)
    full_detected_available = bool(full_detected_available and np.all(np.diff(detected_ids) == 1))
    full_reference_available = bool(np.all(np.diff(reference_ids) == 1))
    ref_full = rr_features(reference, reference_ids) if full_reference_available else None
    det_full = rr_features(detected, detected_ids) if full_detected_available else None
    result["full_window_rr"] = {"duration_seconds": duration_seconds,
        "is_five_minute_window": bool(five_minutes), "reference": ref_full, "detected": det_full,
        "candidate_abstention": not full_detected_available,
        "qualification": "All-beat RR; sinus NN eligibility unknown; no clinical pass threshold"}
    result["full_window_rr"]["difference_ms"] = {
        key: det_full[key] - ref_full[key] if (det_full is not None and ref_full is not None
                    and det_full[key] is not None and ref_full[key] is not None) else None
        for key in ("mean_rr_ms", "sdrr_ms", "rmssd_rr_ms")}
    return result


def resample_for_timing(signal, original_fs, target_fs=125.0):
    """Anti-aliased resampling. Annotation seconds are deliberately not inputs."""
    original_fs, target_fs = _sampling_rate(original_fs), _sampling_rate(target_fs)
    signal = np.asarray(signal, dtype=float)
    if signal.ndim != 1 or not np.all(np.isfinite(signal)):
        raise ValueError("resampling_requires_finite_1d_signal")
    ratio = (Fraction(str(target_fs)) / Fraction(str(original_fs))).limit_denominator(100_000)
    if not np.isclose(original_fs * ratio.numerator / ratio.denominator, target_fs, rtol=1e-12):
        raise ValueError("sampling_ratio_not_representable")
    return resample_poly(signal, ratio.numerator, ratio.denominator)


def quantization_reference(reference_seconds, fs=125.0):
    """Describe grid quantization only; never change the benchmark reference."""
    fs = _sampling_rate(fs)
    reference = _ordered(reference_seconds, "reference_times")
    quantized = np.rint(reference * fs) / fs
    return {"fs_hz": fs, "sample_step_ms": 1000 / fs,
            "scope": "Reference-to-nearest-grid only; excludes filtering and detection errors",
            "timing_error": _error_summary((quantized - reference) * 1000),
            "rr_error": _error_summary(np.diff(quantized - reference) * 1000)}
