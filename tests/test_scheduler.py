#!/usr/bin/env python3
"""Unit checks for the schedule window maths, including windows that cross midnight.

    python3 tests/test_scheduler.py
"""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class FakeCfg:
    def __init__(self, start, end, start_offset=0, end_offset=0):
        self.data = {"schedule": {"start": start, "end": end, "start_offset": start_offset,
                                  "end_offset": end_offset, "playlist": "halloween"},
                     "location": {"lat": 32.78, "lon": -96.80},
                     "seasons": [{"name": "Halloween", "from": "10-01", "to": "10-31", "playlist": "halloween"},
                                 {"name": "Campaign", "from": "2026-09-16", "to": "2026-11-03", "playlist": "campaign"},
                                 {"name": "Winter", "from": "12-20", "to": "01-05", "playlist": "movies"}]}


def scheduler(start="19:00", end="23:00", **kw):
    return app.Scheduler(FakeCfg(start, end, **kw), None, None, None, None)


def window(start, end, now, **kw):
    return scheduler(start, end, **kw).window(datetime.fromisoformat(now))


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

    def test_sunset_start_with_offset(self):
        wid, start, end = window("sunset", "23:00", "2026-10-15T12:00", start_offset=15)
        self.assertIsNone(wid)
        self.assertEqual(start.date().isoformat(), "2026-10-15")
        # Dallas sunset mid-October is around 18:50 local; plus 15 minutes
        self.assertTrue(17 <= start.hour <= 20, start)
        self.assertEqual(end.hour, 23)

    def test_end_at_sunrise_crosses_midnight(self):
        wid, start, end = window("20:00", "sunrise", "2026-10-15T23:30", end_offset=-30)
        self.assertEqual(wid, "2026-10-15")
        self.assertEqual(end.date().isoformat(), "2026-10-16")

    def test_seasons_pick_tonights_playlist(self):
        s = scheduler()
        self.assertEqual(s.tonight_playlist(date(2026, 10, 20)), ("halloween", "Halloween"))
        self.assertEqual(s.tonight_playlist(date(2026, 11, 2)), ("campaign", "Campaign"))
        self.assertEqual(s.tonight_playlist(date(2026, 12, 28)), ("movies", "Winter"))
        self.assertEqual(s.tonight_playlist(date(2027, 1, 3)), ("movies", "Winter"))
        self.assertEqual(s.tonight_playlist(date(2026, 8, 1)), ("halloween", None))
        self.assertEqual(s.tonight_playlist(date(2027, 10, 5)), ("halloween", "Halloween"))

    def test_bad_time_is_a_plain_error(self):
        with self.assertRaises(ValueError):
            window("7pm", "23:00", "2026-10-05T20:00")


if __name__ == "__main__":
    unittest.main()
