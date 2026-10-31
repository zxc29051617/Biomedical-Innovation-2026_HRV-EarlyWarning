"""Boundary tests for time arithmetic, independent of clinical label extraction."""

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hrv_early_warning.timing import evidence_ready_at, label_landmark


class LabelLandmarkTests(unittest.TestCase):
    def setUp(self):
        self.landmark = datetime(2026, 1, 1, 12)
        self.horizon = timedelta(hours=6)
        self.horizon_end = self.landmark + self.horizon

    def test_horizon_includes_right_boundary_but_not_time_after_it(self):
        followup = self.horizon_end + timedelta(hours=1)
        positive = label_landmark(
            self.landmark, followup, self.horizon_end, self.horizon
        )
        negative = label_landmark(
            self.landmark,
            followup,
            self.horizon_end + timedelta(microseconds=1),
            self.horizon,
        )
        self.assertEqual((positive.label, positive.status), (1, "positive"))
        self.assertEqual((negative.label, negative.status), (0, "negative"))

    def test_event_at_or_before_landmark_is_not_at_risk(self):
        for event in [self.landmark, self.landmark - timedelta(microseconds=1)]:
            with self.subTest(event=event):
                result = label_landmark(
                    self.landmark, self.horizon_end, event, self.horizon
                )
                self.assertEqual((result.label, result.status), (None, "not_at_risk"))

    def test_event_just_after_landmark_is_positive(self):
        result = label_landmark(
            self.landmark,
            self.horizon_end,
            self.landmark + timedelta(microseconds=1),
            self.horizon,
        )
        self.assertEqual(result.label, 1)

    def test_no_event_requires_followup_through_exact_horizon_boundary(self):
        for followup in [self.horizon_end, self.horizon_end + timedelta(hours=1)]:
            with self.subTest(followup=followup):
                result = label_landmark(self.landmark, followup, None, self.horizon)
                self.assertEqual((result.label, result.status), (0, "negative"))

    def test_incomplete_followup_is_censored_not_negative(self):
        for followup in [
            self.landmark,
            self.horizon_end - timedelta(microseconds=1),
        ]:
            with self.subTest(followup=followup):
                result = label_landmark(self.landmark, followup, None, self.horizon)
                self.assertEqual((result.label, result.status), (None, "censored"))
                self.assertEqual(result.reason, "followup_ends_before_horizon")

    def test_observed_event_at_followup_end_remains_positive(self):
        end = self.landmark + timedelta(hours=2)
        result = label_landmark(self.landmark, end, end, self.horizon)
        self.assertEqual((result.label, result.status), (1, "positive"))

    def test_event_at_landmark_and_followup_end_is_not_at_risk(self):
        result = label_landmark(
            self.landmark, self.landmark, self.landmark, self.horizon
        )
        self.assertEqual(result.status, "not_at_risk")
        self.assertIsNone(result.label)

    def test_event_after_followup_is_rejected_even_if_beyond_horizon(self):
        for end, event in [
            (self.landmark + timedelta(hours=1), self.landmark + timedelta(hours=2)),
            (self.horizon_end, self.horizon_end + timedelta(microseconds=1)),
        ]:
            with self.subTest(end=end, event=event):
                with self.assertRaisesRegex(ValueError, "after followup_end"):
                    label_landmark(self.landmark, end, event, self.horizon)

    def test_followup_before_landmark_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "precede landmark"):
            label_landmark(
                self.landmark,
                self.landmark - timedelta(microseconds=1),
                None,
                self.horizon,
            )

    def test_nonpositive_horizon_is_rejected(self):
        for horizon in [timedelta(0), timedelta(microseconds=-1)]:
            with self.subTest(horizon=horizon):
                with self.assertRaisesRegex(ValueError, "positive"):
                    label_landmark(self.landmark, self.horizon_end, None, horizon)

    def test_mixed_naive_and_aware_timestamps_are_rejected(self):
        aware = self.landmark.replace(tzinfo=timezone.utc)
        for start, end, event in [
            (self.landmark, aware + self.horizon, None),
            (aware, self.horizon_end, None),
            (self.landmark, self.horizon_end, aware + timedelta(hours=1)),
        ]:
            with self.subTest(start=start, end=end, event=event):
                with self.assertRaisesRegex(ValueError, "mix timezone-aware"):
                    label_landmark(start, end, event, self.horizon)

    def test_different_aware_timezones_compare_as_instants(self):
        start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, 2, tzinfo=timezone(timedelta(hours=8)))
        result = label_landmark(start, end, end, self.horizon)
        self.assertEqual(result.label, 1)

    def test_horizon_is_elapsed_time_across_daylight_saving(self):
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 3, 8, 1, 30, tzinfo=zone)
        event = datetime(2026, 3, 8, 3, 30, tzinfo=zone)
        result = label_landmark(start, event, event, timedelta(hours=1))
        self.assertEqual((result.label, result.status), (1, "positive"))

    def test_repeated_hour_does_not_make_later_event_prevalent(self):
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        event = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        result = label_landmark(start, event, event, timedelta(hours=1))
        self.assertEqual((result.label, result.status), (1, "positive"))


class EvidenceReadyAtTests(unittest.TestCase):
    def test_ready_only_when_all_evidence_is_available(self):
        culture = datetime(2026, 1, 1, 9)
        antibiotic = datetime(2026, 1, 1, 12)
        for evidence in [(culture, antibiotic), (antibiotic, culture)]:
            with self.subTest(evidence=evidence):
                self.assertEqual(evidence_ready_at(evidence), antibiotic)

    def test_accepts_generator_and_single_timestamp(self):
        timestamp = datetime(2026, 1, 1, 12)
        self.assertEqual(evidence_ready_at(t for t in [timestamp]), timestamp)

    def test_empty_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            evidence_ready_at(iter(()))

    def test_mixed_naive_and_aware_evidence_is_rejected(self):
        timestamp = datetime(2026, 1, 1, 12)
        with self.assertRaisesRegex(ValueError, "mix timezone-aware"):
            evidence_ready_at([timestamp, timestamp.replace(tzinfo=timezone.utc)])

    def test_latest_aware_evidence_is_selected_by_instant(self):
        first = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        later = datetime(2026, 1, 1, 21, tzinfo=timezone(timedelta(hours=8)))
        self.assertIs(evidence_ready_at([later, first]), later)

    def test_evidence_ready_at_handles_repeated_daylight_saving_hour(self):
        zone = ZoneInfo("America/New_York")
        earlier = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        later = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        self.assertIs(evidence_ready_at([earlier, later]), later)

    def test_non_datetime_evidence_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "datetime instances"):
            evidence_ready_at([datetime(2026, 1, 1).date()])


if __name__ == "__main__":
    unittest.main()
