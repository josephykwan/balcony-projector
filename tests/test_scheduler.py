#!/usr/bin/env python3
"""Unit checks for the schedule window maths, including windows that cross midnight.

    python3 tests/test_scheduler.py
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class FakeCfg:
    def __init__(self, start, end):
        self.data = {"schedule": {"start": start, "end": end}}


def window(start, end, now):
    sched = app.Scheduler(FakeCfg(start, end), None, None, None)
    return sched.window(datetime.fromisoformat(now))


class WindowTests(unittest.TestCase):
    def test_inside_same_day_window(self):
        wid, start, end = window("19:00", "23:00", "2026-10-05T20:15")
        self.assertEqual(wid, "2026-10-05")
        self.assertEqual((start.hour, end.hour), (19, 23))

    def test_before_window_points_at_tonight(self):
        wid, start, _ = window("19:00", "23:00", "2026-10-05T08:00")
        self.assertIsNone(wid)
        self.assertEqual(start.isoformat(timespec="minutes"), "2026-10-05T19:00")

    def test_after_window_points_at_tomorrow(self):
        wid, start, _ = window("19:00", "23:00", "2026-10-05T23:30")
        self.assertIsNone(wid)
        self.assertEqual(start.isoformat(timespec="minutes"), "2026-10-06T19:00")

    def test_overnight_window_before_midnight(self):
        wid, start, end = window("20:00", "01:00", "2026-10-05T23:30")
        self.assertEqual(wid, "2026-10-05")
        self.assertEqual(end.isoformat(timespec="minutes"), "2026-10-06T01:00")

    def test_overnight_window_after_midnight(self):
        wid, start, end = window("20:00", "01:00", "2026-10-06T00:30")
        self.assertEqual(wid, "2026-10-05")
        self.assertEqual(start.isoformat(timespec="minutes"), "2026-10-05T20:00")

    def test_overnight_gap_points_at_tonight(self):
        wid, start, _ = window("20:00", "01:00", "2026-10-06T10:00")
        self.assertIsNone(wid)
        self.assertEqual(start.isoformat(timespec="minutes"), "2026-10-06T20:00")

    def test_end_is_exclusive(self):
        wid, _, _ = window("19:00", "23:00", "2026-10-05T23:00")
        self.assertIsNone(wid)

    def test_bad_time_is_a_plain_error(self):
        with self.assertRaises(ValueError):
            window("7pm", "23:00", "2026-10-05T20:00")


if __name__ == "__main__":
    unittest.main()
