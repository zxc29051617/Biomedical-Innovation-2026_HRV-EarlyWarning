"""Same-reference-support timing comparison, independent of signal libraries.

Input matches come from the unchanged annotation matcher. This module never
selects detector parameters, repairs beats, imputes a gap, or qualifies NN.
"""

from collections import defaultdict
import math
from statistics import mean, median


COMPARISON_VERSION = "shared_reference_timing_v0.1.0"
NUMERIC_SLACK_SECONDS = 1e-12


def _ordered(values, name, *, integer=False):
    result = list(values)
    for value in result:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name}_must_be_finite_numbers")
        if integer and (value < 0 or int(value) != value):
            raise ValueError(f"{name}_must_be_nonnegative_integer_ids")
    if any(after <= before for before, after in zip(result, result[1:])):
        raise ValueError(f"{name}_must_be_strictly_increasing")
    return [int(value) if integer else float(value) for value in result]


def _ids(values, size, name):
    result = list(range(size)) if values is None else _ordered(values, name, integer=True)
    if len(result) != size:
        raise ValueError(f"{name}_length_mismatch")
    return result


def _match_map(pairs, reference, detected, tolerance_seconds):
    result = {}
    previous_reference = previous_detected = -1
    for pair in pairs:
        if len(pair) != 2:
            raise ValueError("match_pair_must_contain_two_indices")
        ri, di = pair
        if any(isinstance(value, bool) or not isinstance(value, int) for value in pair):
            raise ValueError("match_indices_must_be_integers")
        if not (0 <= ri < len(reference) and 0 <= di < len(detected)):
            raise ValueError("match_index_out_of_range")
        if ri <= previous_reference or di <= previous_detected:
            raise ValueError("matches_must_be_ordered_one_to_one")
        if abs(reference[ri] - detected[di]) > tolerance_seconds + NUMERIC_SLACK_SECONDS:
            raise ValueError("match_outside_fixed_tolerance")
        result[ri] = di
        previous_reference, previous_detected = ri, di
    return result


def _percentile(values, fraction):
    """Linear percentile, equivalent to the default numpy linear definition."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _errors(values):
    absolute = [abs(value) for value in values]
    return {"count": len(values), "signed_bias_ms": mean(values) if values else None,
            "mae_ms": mean(absolute) if values else None,
            "absolute_median_ms": median(absolute) if values else None,
            "absolute_p95_ms": _percentile(absolute, .95) if values else None,
            "absolute_max_ms": max(absolute) if values else None}


def _paired_errors(reference_values, baseline_values, candidate_values):
    baseline = [value - ref for value, ref in zip(baseline_values, reference_values)]
    candidate = [value - ref for value, ref in zip(candidate_values, reference_values)]
    delta = [abs(after) - abs(before) for before, after in zip(baseline, candidate)]
    return {"baseline_xqrs": _errors(baseline), "consistent_fiducial": _errors(candidate),
            "paired_absolute_error_delta_ms": {
                "direction": "candidate_minus_baseline; negative means smaller error on this same support",
                "count": len(delta), "mean": mean(delta) if delta else None,
                "median": median(delta) if delta else None,
                "minimum": min(delta) if delta else None, "maximum": max(delta) if delta else None}}


def _detection(reference_count, detected_count, matched_count):
    return {"reference_count": reference_count, "detected_count": detected_count,
            "matched_count": matched_count, "missed_count": reference_count - matched_count,
            "extra_count": detected_count - matched_count,
            "sensitivity": matched_count / reference_count if reference_count else None,
            "ppv": matched_count / detected_count if detected_count else None}


def compare_shared_support(reference_seconds, baseline_seconds, candidate_seconds,
                           baseline_matches, candidate_matches, *, candidate_ids,
                           baseline_ids=None, reference_ids=None, tolerance_seconds=.1,
                           duration_seconds=300):
    """Compare both methods against identical reference beats/pairs/triplets.

    Match indices address each unfiltered input time sequence. Surviving
    candidate IDs MUST refer to their original baseline seed identities.
    Missing detections, extra detections and abstained seeds break adjacency.
    A shared-support result is conditional, never a whole-window HRV claim.
    """
    reference = _ordered(reference_seconds, "reference_times")
    baseline = _ordered(baseline_seconds, "baseline_times")
    candidate = _ordered(candidate_seconds, "candidate_times")
    if candidate_ids is None:
        raise ValueError("candidate_original_seed_ids_required")
    ref_ids = _ids(reference_ids, len(reference), "reference_ids")
    base_ids = _ids(baseline_ids, len(baseline), "baseline_ids")
    cand_ids = _ids(candidate_ids, len(candidate), "candidate_ids")
    if not set(cand_ids).issubset(base_ids):
        raise ValueError("candidate_ids_must_be_surviving_baseline_seed_ids")
    if (isinstance(tolerance_seconds, bool) or not isinstance(tolerance_seconds, (int, float)) or not math.isfinite(tolerance_seconds)
            or tolerance_seconds < 0):
        raise ValueError("tolerance_must_be_nonnegative_and_finite")
    if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)) or not math.isfinite(duration_seconds)
            or duration_seconds <= 0):
        raise ValueError("duration_must_be_positive_and_finite")
    for sequence in (reference, baseline, candidate):
        if sequence and (sequence[0] < 0 or sequence[-1] >= duration_seconds):
            raise ValueError("times_must_be_inside_half_open_window")
    base_map = _match_map(baseline_matches, reference, baseline, tolerance_seconds)
    cand_map = _match_map(candidate_matches, reference, candidate, tolerance_seconds)
    common = sorted(base_map.keys() & cand_map.keys())
    adjacent = []
    for before, after in zip(common, common[1:]):
        adjacent.append(ref_ids[after] - ref_ids[before] == 1
                        and base_ids[base_map[after]] - base_ids[base_map[before]] == 1
                        and cand_ids[cand_map[after]] - cand_ids[cand_map[before]] == 1)
    rr_pairs = [(before, after) for before, after, keep in zip(common, common[1:], adjacent) if keep]
    triples = [(common[index], common[index + 1], common[index + 2])
               for index in range(max(0, len(common) - 2)) if adjacent[index] and adjacent[index + 1]]
    reference_pair_count = sum(after - before == 1 for before, after in zip(ref_ids, ref_ids[1:]))
    reference_triplet_count = sum(ref_ids[index + 1] - ref_ids[index] == 1
                                  and ref_ids[index + 2] - ref_ids[index + 1] == 1
                                  for index in range(max(0, len(ref_ids) - 2)))
    sequence_maps = ((reference, {index: index for index in range(len(reference))}),
                     (baseline, base_map), (candidate, cand_map))
    timing_values = [[sequence[mapping[index]] * 1000 for index in common]
                     for sequence, mapping in sequence_maps]
    rr_values = [[(sequence[mapping[after]] - sequence[mapping[before]]) * 1000
                  for before, after in rr_pairs]
                 for sequence, mapping in sequence_maps]
    changes = [[(sequence[mapping[last]] - 2 * sequence[mapping[middle]] + sequence[mapping[first]]) * 1000
                for first, middle, last in triples]
               for sequence, mapping in sequence_maps]
    rmssd = [math.sqrt(mean(value * value for value in series)) if series else None for series in changes]
    abs_rmssd_error = [abs(value - rmssd[0]) if value is not None else None for value in rmssd[1:]]
    return {
        "comparison_version": COMPARISON_VERSION,
        "match_tolerance_ms": tolerance_seconds * 1000,
        "scope": "Same-reference support only; conditional on both methods matching, never complete-window NN-HRV accuracy",
        "reference_denominators": {"beats": len(reference), "original_adjacent_rr": reference_pair_count,
                                   "original_adjacent_triplets": reference_triplet_count},
        "detection": {"baseline_xqrs": _detection(len(reference), len(baseline), len(base_map)),
                      "consistent_fiducial": _detection(len(reference), len(candidate), len(cand_map))},
        "candidate_retention": {"seed_count": len(baseline), "retained_count": len(candidate),
                                "abstained_count": len(baseline) - len(candidate),
                                "all_nonempty_seeds_retained": bool(baseline and base_ids == cand_ids)},
        "common_support": {"beats": len(common), "original_adjacent_rr": len(rr_pairs),
                           "original_adjacent_triplets": len(triples),
                           "matched_reference_same_seed_identity": sum(base_ids[base_map[index]] == cand_ids[cand_map[index]] for index in common),
                           "reference_beat_fraction": len(common) / len(reference) if reference else None,
                           "reference_adjacent_rr_fraction": len(rr_pairs) / reference_pair_count if reference_pair_count else None,
                           "reference_adjacent_triplet_fraction": len(triples) / reference_triplet_count if reference_triplet_count else None},
        "timing": _paired_errors(*timing_values),
        "rr": _paired_errors(*rr_values),
        "rr_change": _paired_errors(*changes),
        "rmssd_on_common_triplets": {"count": len(triples), "reference_ms": rmssd[0],
                                      "baseline_xqrs_ms": rmssd[1], "consistent_fiducial_ms": rmssd[2],
                                      "baseline_absolute_error_ms": abs_rmssd_error[0],
                                      "candidate_absolute_error_ms": abs_rmssd_error[1],
                                      "paired_absolute_error_delta_ms": abs_rmssd_error[1] - abs_rmssd_error[0] if triples else None},
        "nn_qualification": "unknown_not_inferred_from_reference_N_or_engineering_acceptance"}


def aggregate_comparisons(windows):
    """Descriptive window summaries, with every planned/failed window retained."""
    groups = defaultdict(list)
    for row in windows:
        groups[str(float(row["fs_hz"]))].append(row)
    output = {"comparison_version": COMPARISON_VERSION,
              "aggregation": "Counts pool reference opportunities at each sampling rate. Error summaries are medians of window statistics on shared support, not pooled beat percentiles. No independent validation or confidence interval is claimed.",
              "delta_direction": "candidate minus baseline; negative means smaller conditional error",
              "by_sampling_rate": {}}
    for fs, planned in sorted(groups.items(), key=lambda item: float(item[0])):
        evaluated = [row["comparison"] for row in planned if row["status"] == "evaluated"]
        denominator = {key: sum(row["reference_denominators"][key] for row in evaluated)
                       for key in ("beats", "original_adjacent_rr", "original_adjacent_triplets")}
        common = {key: sum(row["common_support"][key] for row in evaluated) for key in denominator}
        result = {"total_windows": len(planned), "evaluated_windows": len(evaluated),
                  "failed_windows": len(planned) - len(evaluated),
                  "reference_beats_all_planned_windows": sum(row["reference_beat_count"] for row in planned),
                  "reference_denominators_evaluated_windows": denominator, "common_support_totals": common,
                  "common_support_fractions": {key: common[key] / count if count else None for key, count in denominator.items()},
                  "all_nonempty_seeds_retained_windows": sum(row["candidate_retention"]["all_nonempty_seeds_retained"] for row in evaluated),
                  "candidate_abstained_seeds": sum(row["candidate_retention"]["abstained_count"] for row in evaluated),
                  "detection": {}, "paired_errors": {}}
        for name in ("baseline_xqrs", "consistent_fiducial"):
            values = {key: sum(row["detection"][name][key] for row in evaluated)
                      for key in ("reference_count", "detected_count", "matched_count")}
            result["detection"][name] = _detection(**values)
        for measure in ("timing", "rr", "rr_change"):
            available = [row[measure] for row in evaluated if row[measure]["baseline_xqrs"]["count"]]
            result["paired_errors"][measure] = {
                "usable_windows": len(available),
                "baseline_median_of_window_mae_ms": median(row["baseline_xqrs"]["mae_ms"] for row in available) if available else None,
                "candidate_median_of_window_mae_ms": median(row["consistent_fiducial"]["mae_ms"] for row in available) if available else None,
                "median_of_paired_window_mae_delta_ms": median(row["paired_absolute_error_delta_ms"]["mean"] for row in available) if available else None,
                "baseline_median_of_window_p95_ms_NOT_pooled_p95": median(row["baseline_xqrs"]["absolute_p95_ms"] for row in available) if available else None,
                "candidate_median_of_window_p95_ms_NOT_pooled_p95": median(row["consistent_fiducial"]["absolute_p95_ms"] for row in available) if available else None}
        available = [row["rmssd_on_common_triplets"] for row in evaluated if row["rmssd_on_common_triplets"]["count"]]
        result["paired_errors"]["rmssd_on_common_triplets"] = {
            "usable_windows": len(available),
            "baseline_median_of_window_absolute_error_ms": median(row["baseline_absolute_error_ms"] for row in available) if available else None,
            "candidate_median_of_window_absolute_error_ms": median(row["candidate_absolute_error_ms"] for row in available) if available else None,
            "median_of_paired_window_absolute_error_delta_ms": median(row["paired_absolute_error_delta_ms"] for row in available) if available else None}
        output["by_sampling_rate"][fs] = result
    return output
