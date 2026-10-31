from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from hrv_early_warning.cohort import StudyStay
from hrv_early_warning.risksets import StayOutcome
from hrv_early_warning.signal_pipeline import plan_epochs
from hrv_early_warning.vasopressors import FirstUseResult


class SignalPlanTests(unittest.TestCase):
    def test_hourly_history_includes_controls_and_never_post_treatment(self):
        t = datetime(2100, 1, 1)
        stays = []
        for subject, event in ((1, t + timedelta(hours=10)), (2, None)):
            stay = StudyStay(subject, subject, subject, t, t + timedelta(hours=12), t + timedelta(hours=12), 40, False)
            outcome = FirstUseResult(event, event, stay.followup_end, "qualifying_first_use" if event else "no_exposure", 60 if event else 0)
            stays.append(StayOutcome(stay, outcome))
        spans = {i: [SimpleNamespace(start=t, end=t + timedelta(hours=12))] for i in (1, 2)}
        hourly, events = plan_epochs(stays, {1: "train", 2: "validation"}, spans)
        self.assertEqual(hourly[1], {t + timedelta(hours=h) for h in range(1, 10)})
        self.assertEqual(hourly[2], {t + timedelta(hours=h) for h in range(1, 12)})
        self.assertNotIn(2, events)
        self.assertEqual(min(events[1]), t + timedelta(minutes=5))
        self.assertEqual(max(events[1]), t + timedelta(hours=9))
        self.assertTrue(all(end < stays[0].event_time for end in events[1]))
