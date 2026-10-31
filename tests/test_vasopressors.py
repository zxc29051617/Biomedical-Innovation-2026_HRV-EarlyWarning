"""Synthetic boundary cases for infusion parsing and first-use outcomes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from hrv_early_warning.timing import label_landmark
from hrv_early_warning.vasopressors import (
    ALL_FIVE_ITEMIDS,
    PRIMARY_ITEMIDS,
    Infusion,
    derive_first_use,
    parse_infusion_row,
    uncertain_parse_reason,
)


T0 = datetime(2200, 1, 1)


def row(**changes):
    values = {
        "SUBJECT_ID": "1", "HADM_ID": "2", "ICUSTAY_ID": "3", "ROW_ID": "4",
        "ITEMID": "221906", "STARTTIME": "2200-01-01 06:00:00",
        "ENDTIME": "2200-01-01 07:00:00", "STORETIME": "2200-01-01 07:05:00",
        "RATE": "0.1", "RATEUOM": "mcg/kg/min", "AMOUNT": "0.3",
        "STATUSDESCRIPTION": "FinishedRunning", "CANCELREASON": "0",
        "ORDERCATEGORYDESCRIPTION": "Continuous Med", "ORDERCATEGORYNAME": "01-Drips",
        "COMMENTS_CANCELEDBY": "",
    }
    values.update(changes)
    return values


def infusion(start_minute, end_minute, itemid=221906, **changes):
    values = dict(subject_id=1, hadm_id=2, icustay_id=3, itemid=itemid,
                  start=T0 + timedelta(minutes=start_minute),
                  end=T0 + timedelta(minutes=end_minute))
    values.update(changes)
    return Infusion(**values)


class ParseInfusionTests(unittest.TestCase):
    def test_preserves_treatment_and_documentation_times_and_rate_units(self):
        result, reason = parse_infusion_row(row())
        self.assertEqual(reason, "accepted")
        self.assertEqual(result.start, T0 + timedelta(hours=6))
        self.assertEqual(result.storetime, T0 + timedelta(hours=7, minutes=5))
        self.assertEqual((result.rate, result.rate_unit), (0.1, "mcg/kg/min"))

    def test_five_drugs_and_drug_specific_units(self):
        for itemid in ALL_FIVE_ITEMIDS:
            with self.subTest(itemid=itemid):
                unit = "units/min" if itemid == 222315 else "mcg/kg/min"
                self.assertEqual(parse_infusion_row(row(ITEMID=str(itemid), RATEUOM=unit))[1], "accepted")
        for itemid, unit in [(222315, "mg/hour"), (221906, "units/min"), (221662, "kg/hour")]:
            with self.subTest(itemid=itemid, unit=unit):
                self.assertEqual(parse_infusion_row(row(ITEMID=str(itemid), RATEUOM=unit))[1], "uncertain_rate_unit")
        self.assertEqual(parse_infusion_row(row(RATEUOM="mL/hour"))[1], "accepted")

    def test_delivery_end_statuses_retained_but_rewritten_excluded(self):
        for status in ["Changed", "Paused", "Stopped", "FinishedRunning"]:
            with self.subTest(status=status):
                self.assertEqual(parse_infusion_row(row(STATUSDESCRIPTION=status))[1], "accepted")
        result, reason = parse_infusion_row(row(STATUSDESCRIPTION="Rewritten", CANCELREASON="1", RATE="-1"))
        self.assertIsNone(result)
        self.assertEqual(reason, "excluded_rewritten")
        self.assertFalse(uncertain_parse_reason(reason))

    def test_unknown_and_flushed_statuses_are_not_accepted_as_delivery(self):
        for status in ["", "Flushed", "Cancelled", "new status"]:
            with self.subTest(status=status):
                result, reason = parse_infusion_row(row(STATUSDESCRIPTION=status))
                self.assertIsNone(result)
                self.assertTrue(uncertain_parse_reason(reason))

    def test_cancellation_flags_are_uncertain_and_edits_alone_do_not_erase_delivery(self):
        for changes in [{"CANCELREASON": "1"}, {"CANCELREASON": "unknown"},
                        {"CANCELREASON": "nan"}, {"COMMENTS_CANCELEDBY": "caregiver"},
                        {"COMMENTS_STATUS": "Canceled"}]:
            with self.subTest(changes=changes):
                self.assertEqual(parse_infusion_row(row(**changes))[1], "uncertain_cancellation")
        self.assertEqual(parse_infusion_row(row(COMMENTS_EDITEDBY="caregiver"))[1], "accepted")

    def test_explicit_bolus_and_drug_push_are_excluded(self):
        for field, value in [("ORDERCATEGORYDESCRIPTION", "Bolus"),
                             ("ORDERCATEGORYNAME", "05-Med Bolus"),
                             ("SECONDARYORDERCATEGORYNAME", "Drug Push")]:
            with self.subTest(field=field):
                self.assertEqual(parse_infusion_row(row(**{field: value}))[1], "excluded_bolus")

    def test_one_minute_without_route_is_uncertain_but_explicit_drip_remains_exposure(self):
        self.assertEqual(parse_infusion_row(row(ENDTIME="2200-01-01 06:01:00"))[1], "accepted")
        self.assertEqual(parse_infusion_row(row(ENDTIME="2200-01-01 06:01:00", ORDERCATEGORYDESCRIPTION=""))[1], "uncertain_one_minute_route")

    def test_invalid_rate_never_falls_back_to_original_rate_or_amount(self):
        for rate in ["", "abc", "nan", "inf", "-0.1"]:
            with self.subTest(rate=rate):
                result, reason = parse_infusion_row(row(RATE=rate, ORIGINALRATE="0.2"))
                self.assertIsNone(result)
                self.assertTrue(uncertain_parse_reason(reason))
        self.assertEqual(parse_infusion_row(row(RATE="0", AMOUNT="0"))[1], "excluded_zero_rate")
        self.assertEqual(parse_infusion_row(row(RATEUOM="", AMOUNTUOM="mg"))[1], "uncertain_rate_unit")

    def test_zero_rate_with_positive_amount_is_uncertain_not_absence(self):
        result, reason = parse_infusion_row(row(RATE="0", AMOUNT="1"))
        self.assertIsNone(result)
        self.assertEqual(reason, "uncertain_rate_amount_conflict")
        self.assertTrue(uncertain_parse_reason(reason))

    def test_invalid_amount_conflicts_with_positive_delivery(self):
        for amount in ["-1", "0", "nan", "abc"]:
            with self.subTest(amount=amount):
                self.assertEqual(parse_infusion_row(row(AMOUNT=amount))[1], "uncertain_invalid_amount")
        self.assertEqual(parse_infusion_row(row(AMOUNT=""))[1], "accepted")

    def test_missing_optional_identifiers_and_storetime_are_retained_for_upstream_mapping(self):
        result, reason = parse_infusion_row(row(ICUSTAY_ID="", ROW_ID="", STORETIME=""))
        self.assertEqual(reason, "accepted")
        self.assertIsNone(result.icustay_id)
        self.assertIsNone(result.row_id)
        self.assertIsNone(result.storetime)

    def test_bad_ids_timestamps_and_nonpositive_intervals_are_uncertain(self):
        for changes in [{"HADM_ID": ""}, {"SUBJECT_ID": "0"}, {"ICUSTAY_ID": "bad"},
                        {"STARTTIME": "2200-01-01"}, {"STORETIME": "bad"},
                        {"ENDTIME": "2200-01-01 06:00:00"},
                        {"ENDTIME": "2200-01-01 05:59:59"},
                        {"ENDTIME": "2200-01-01 07:00:00+00:00"}]:
            with self.subTest(changes=changes):
                result, reason = parse_infusion_row(row(**changes))
                self.assertIsNone(result)
                self.assertTrue(uncertain_parse_reason(reason))

    def test_item_scope_is_explicit(self):
        self.assertEqual(parse_infusion_row(row(ITEMID="221653"))[1], "excluded_itemid")
        self.assertEqual(parse_infusion_row(row(ITEMID="bad"))[1], "uncertain_invalid_itemid")
        self.assertEqual(parse_infusion_row(row(ITEMID="221662"), PRIMARY_ITEMIDS)[1], "excluded_itemid")
        with self.assertRaises(ValueError):
            parse_infusion_row(row(), [99999])


class FirstUseTests(unittest.TestCase):
    def derive(self, records, **kwargs):
        return derive_first_use(records, T0, T0 + timedelta(hours=24), **kwargs)

    def test_cross_drug_switch_counts_without_double_counting_overlap(self):
        records = [infusion(360, 390), infusion(390, 420, 222315), infusion(365, 400, 221749)]
        result = self.derive(reversed(records))
        self.assertEqual(result.event_status, "qualifying_first_use")
        self.assertEqual(result.event_time, T0 + timedelta(hours=6))
        self.assertEqual(result.primary_continuous_minutes, 60)
        overlap_only = self.derive([infusion(360, 390), infusion(360, 390, 222315)])
        self.assertEqual(overlap_only.event_status, "short_first_use")
        self.assertEqual(overlap_only.primary_continuous_minutes, 30)

    def test_duplicates_do_not_add_duration(self):
        one = infusion(360, 390)
        result = self.derive([one, one, replace(one, row_id=99)])
        self.assertEqual(result.event_status, "short_first_use")
        self.assertEqual(result.primary_continuous_minutes, 30)

    def test_any_positive_gap_breaks_continuity_and_later_restart_is_not_first(self):
        first = infusion(360, 390)
        restart = infusion(390, 500, start=first.end + timedelta(microseconds=1))
        result = self.derive([first, restart])
        self.assertIsNone(result.event_time)
        self.assertEqual(result.event_status, "short_first_use")
        self.assertEqual(result.first_exposure_time, first.start)

    def test_dopamine_cannot_fill_primary_gap(self):
        result = self.derive([infusion(360, 390), infusion(390, 420, 221662), infusion(420, 500)])
        self.assertEqual(result.event_status, "short_first_use")
        self.assertEqual(result.primary_continuous_minutes, 30)

    def test_first_dopamine_censors_later_primary_and_sensitivity_can_include_it(self):
        records = [infusion(300, 360, 221662), infusion(360, 500)]
        result = self.derive(records)
        self.assertEqual(result.event_status, "nonprimary_first_use")
        self.assertIsNone(result.event_time)
        self.assertEqual(result.first_exposure_time, T0 + timedelta(hours=5))
        sensitivity = self.derive(records, primary_itemids=ALL_FIVE_ITEMIDS)
        self.assertEqual(sensitivity.event_time, T0 + timedelta(hours=5))
        self.assertEqual(sensitivity.primary_continuous_minutes, 200)

    def test_simultaneous_dopamine_and_primary_does_not_depend_on_row_order(self):
        records = [infusion(360, 450, 221662), infusion(360, 420)]
        for supplied in [records, list(reversed(records))]:
            with self.subTest(supplied=supplied):
                self.assertEqual(self.derive(supplied).event_status, "qualifying_first_use")

    def test_30_60_120_minute_thresholds_are_inclusive(self):
        for threshold in [30, 60, 120]:
            with self.subTest(threshold=threshold):
                records = [infusion(360, 360 + threshold)]
                self.assertIsNotNone(self.derive(records, min_duration_minutes=threshold).event_time)
                self.assertIsNone(self.derive(records, min_duration_minutes=threshold + 1 / 60).event_time)

    def test_pre_icu_overlap_is_prevalent_and_prior_completed_admission_exposure_is_outside_scope(self):
        ongoing = self.derive([infusion(-60, 30), infusion(360, 480)])
        self.assertEqual(ongoing.event_status, "ongoing_at_icu_admission")
        self.assertIsNone(ongoing.event_time)
        ended = self.derive([infusion(-60, 0), infusion(360, 480)])
        self.assertEqual(ended.event_time, T0 + timedelta(hours=6))

    def test_start_at_icu_entry_is_observed_onset_and_start_at_exit_is_outside_stay(self):
        self.assertEqual(self.derive([infusion(0, 60)]).event_time, T0)
        self.assertEqual(self.derive([infusion(1440, 1500)]).event_status, "no_exposure")

    def test_post_icu_data_cannot_complete_duration(self):
        result = self.derive([infusion(1410, 1530)])
        self.assertEqual(result.event_status, "incomplete_duration_followup")
        self.assertIsNone(result.event_time)
        self.assertEqual(result.primary_continuous_minutes, 30)
        self.assertLess(result.followup_end, result.first_exposure_time)

    def test_duration_exactly_complete_at_icu_end_is_valid(self):
        result = self.derive([infusion(1380, 1500)])
        self.assertEqual(result.event_status, "qualifying_first_use")
        self.assertEqual(result.primary_continuous_minutes, 60)

    def test_short_episode_ending_at_icu_boundary_is_incomplete(self):
        self.assertEqual(self.derive([infusion(1410, 1440)]).event_status, "incomplete_duration_followup")

    def test_censor_exact_horizon_boundary_cannot_be_negative(self):
        result = self.derive([infusion(360, 390)])
        label = label_landmark(T0, result.followup_end, result.event_time, timedelta(hours=6))
        self.assertEqual(label.status, "censored")
        earlier = label_landmark(T0, result.followup_end, result.event_time, timedelta(hours=5))
        self.assertEqual(earlier.status, "negative")

    def test_no_exposure_preserves_followup_but_does_not_certify_source_coverage(self):
        result = self.derive(iter(()))
        self.assertEqual(result.event_status, "no_exposure")
        self.assertIsNone(result.event_time)
        self.assertIsNone(result.first_exposure_time)
        self.assertEqual(result.followup_end, T0 + timedelta(hours=24))

    def test_mixed_patients_admissions_invalid_intervals_and_drugs_raise(self):
        invalid_sets = [
            [infusion(0, 60), infusion(60, 120, subject_id=9)],
            [infusion(0, 60), infusion(60, 120, hadm_id=9)],
            [infusion(60, 60)], [infusion(60, 0)], [infusion(0, 60, 221653)],
        ]
        for records in invalid_sets:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.derive(records)

    def test_invalid_configuration_and_mixed_clock_raise(self):
        for threshold in [0, -1, float("inf"), float("nan")]:
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.derive([], min_duration_minutes=threshold)
        for changes in [{"maximum_gap_minutes": 1}, {"primary_itemids": []}, {"primary_itemids": [999]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.derive([], **changes)
        with self.assertRaises(ValueError):
            self.derive([infusion(0, 60, start=T0.replace(tzinfo=timezone.utc))])
        with self.assertRaises(ValueError):
            derive_first_use([], T0, T0)

    def test_elapsed_duration_across_dst_fold_is_one_hour(self):
        zone = ZoneInfo("America/New_York")
        begin = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        end = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        record = infusion(0, 60, start=begin, end=end)
        result = derive_first_use([record], datetime(2026, 11, 1, 0, tzinfo=zone),
                                  datetime(2026, 11, 1, 4, tzinfo=zone))
        self.assertEqual(result.event_status, "qualifying_first_use")
        self.assertEqual(result.primary_continuous_minutes, 60)


if __name__ == "__main__":
    unittest.main()
