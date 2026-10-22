import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

import daily_digest as digest
from daily_digest import build_digest, collapse_breakouts, digest_target_day, send_daily_digest


class DailyDigestTests(unittest.TestCase):
    def test_delayed_runs_in_screenshot_select_different_days(self):
        # 03:15 Vancouver on Oct 19 and 02:37 on Oct 20 straddle the
        # old 6am Eastern cutoff, which selected Oct 19 BOTH times.
        self.assertEqual(digest_target_day(datetime.fromisoformat("2026-10-19T10:15:00+00:00")), date(2026, 10, 18))
        self.assertEqual(digest_target_day(datetime.fromisoformat("2026-10-20T09:37:00+00:00")), date(2026, 10, 19))

    def test_reporting_date_is_stable_through_delays_and_dst(self):
        for timestamp in ["2026-10-20T04:45:00+00:00", "2026-10-20T15:00:00+00:00"]:
            self.assertEqual(digest_target_day(datetime.fromisoformat(timestamp)), date(2026, 10, 19))
        self.assertEqual(digest_target_day(datetime.fromisoformat("2026-01-20T04:45:00+00:00")), date(2026, 1, 19))

    def test_delivery_is_persisted_and_next_run_skips_same_day(self):
        entry = {"timestamp": "2026-10-19T05:10:00+00:00", "sport": "nfl",
                 "player_name": "Matthew Golden", "message": "BREAKOUT", "notified": True}
        with tempfile.TemporaryDirectory() as folder:
            for name, data in [("config.json", {}), ("dashboard_history.json", [entry])]:
                with open(os.path.join(folder, name), "w") as f:
                    json.dump(data, f)
            with patch.object(digest, "CONFIG_PATH", os.path.join(folder, "config.json")), \
                 patch.object(digest, "DASHBOARD_HISTORY_PATH", os.path.join(folder, "dashboard_history.json")), \
                 patch.object(digest, "DIGEST_STATE_PATH", os.path.join(folder, "digest_state.json")), \
                 patch.object(digest, "digest_target_day", return_value=date(2026, 10, 19)), \
                 patch.object(digest.notifier, "send_message", return_value=123) as send:
                digest.main()
                digest.main()
            send.assert_called_once()
            with open(os.path.join(folder, "digest_state.json")) as f:
                self.assertEqual(json.load(f)["sent_days"]["2026-10-19"]["message_id"], 123)

    def test_uses_eastern_dates_and_excludes_old_or_suppressed_entries(self):
        base = {"sport": "nfl", "message": "BREAKOUT"}
        history = [
            dict(base, player_name="Included", timestamp="2026-10-20T02:00:00+00:00"),
            dict(base, player_name="Old", timestamp="2026-10-19T02:00:00+00:00"),
            dict(base, player_name="Suppressed", timestamp="2026-10-20T02:00:00+00:00", notified=False),
        ]
        with patch.object(digest.notifier, "send_message", return_value=1) as send:
            send_daily_digest({}, history, {}, datetime.fromisoformat("2026-10-20T09:37:00+00:00"))
        text = send.call_args.args[1]
        self.assertIn("Breakout digest for 2026-10-19", text)
        self.assertIn("Included", text)
        self.assertNotIn("Old", text)
        self.assertNotIn("Suppressed", text)

    def test_failed_send_remains_retryable_and_empty_day_sends_nothing(self):
        state = {}
        now = datetime.fromisoformat("2026-10-20T09:37:00+00:00")
        history = [{"timestamp": "2026-10-19T05:10:00+00:00", "sport": "nfl",
                    "player_name": "Matthew Golden", "message": "BREAKOUT"}]
        with patch.object(digest.notifier, "send_message", side_effect=RuntimeError("offline")) as send:
            with self.assertRaises(RuntimeError):
                send_daily_digest({}, history, state, now)
            self.assertNotIn("2026-10-19", state.get("sent_days", {}))
            self.assertFalse(send_daily_digest({}, [], {}, now))
            self.assertEqual(send.call_count, 1)

    def test_keeps_only_highest_breakout_for_each_player(self):
        entries = [
            {"sport": "nfl", "player_name": "Jared Goff", "message": "BREAKOUT: Jared Goff (130% of projection)"},
            {"sport": "nfl", "player_name": "Jared Goff", "message": "DOUBLE BREAKOUT: Jared Goff (182.9% of projection)"},
            {"sport": "nfl", "player_name": "Amon-Ra St. Brown", "message": "3x BREAKOUT: Amon-Ra St. Brown"},
            {"sport": "nba", "player_name": "Nikola Jokic", "message": "BREAKOUT: Nikola Jokic"},
        ]

        self.assertEqual(
            collapse_breakouts(entries),
            [
                ("nfl", "Jared Goff", 2, entries[1]),
                ("nfl", "Amon-Ra St. Brown", 3, entries[2]),
                ("nba", "Nikola Jokic", 1, entries[3]),
            ],
        )

        digest = build_digest(entries)
        self.assertIn("Today's breakout digest -- 3 total", digest)
        self.assertIn("Jared Goff — DOUBLE BREAKOUT; 182.9% of projection (+82.9% over)", digest)
        self.assertIn("Amon-Ra St. Brown — 3x BREAKOUT", digest)
        self.assertIn("Nikola Jokic — BREAKOUT", digest)
        self.assertEqual(digest.count("Jared Goff"), 1)


if __name__ == "__main__":
    unittest.main()
