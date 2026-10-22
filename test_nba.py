import json
import unittest
from datetime import date, datetime
from unittest.mock import patch

import game_schedule_checker
import main
import sleeper_client as sleeper


class NbaDataTests(unittest.TestCase):
    def test_overnight_check_keeps_the_evenings_game_date(self):
        for timestamp in ["2026-10-22T00:58:00+00:00", "2026-10-22T04:30:00+00:00",
                          "2026-10-22T09:59:00+00:00"]:
            self.assertEqual(main.nba_game_day(datetime.fromisoformat(timestamp)), date(2026, 10, 21))
        self.assertEqual(main.nba_game_day(datetime.fromisoformat("2026-10-22T10:00:00+00:00")), date(2026, 10, 22))

    def test_overnight_game_date_handles_winter_time(self):
        self.assertEqual(main.nba_game_day(datetime.fromisoformat("2026-01-22T05:30:00+00:00")), date(2026, 1, 21))
        self.assertEqual(main.nba_game_day(datetime.fromisoformat("2026-01-22T11:00:00+00:00")), date(2026, 1, 22))

    def test_parses_records_filters_dates_and_normalizes_shooting(self):
        records = [
            {"player_id": "1", "date": "2026-10-20", "stats": {
                "pts": 30, "tpm": 4, "fgm": 10, "fga": 20, "ftm": 6, "fta": 8,
            }},
            {"player_id": "1", "date": "2026-10-21", "stats": {"pts": 99}},
            {"player_id": "2", "date": "2026-10-20", "stats": {}},
            {"player_id": "3", "date": "2026-10-20", "stats": None},
        ]
        with patch.object(sleeper, "_get", return_value=records) as get:
            data = sleeper.get_nba_day_stats("2026-10-20", "2026", week=1)
        self.assertEqual(set(data), {"1"})
        self.assertEqual(data["1"]["pts"], 30)
        self.assertEqual(data["1"]["fg3m"], 4)
        self.assertEqual(data["1"]["fgp"], .5)
        self.assertEqual(data["1"]["ftp"], .75)
        get.assert_called_once_with("https://api.sleeper.app/stats/nba/2026/1", params={
            "season_type": "regular", "grouping": "day", "date": "2026-10-20",
        })

    def test_zero_attempts_and_absent_three_pointers_are_zero(self):
        data = sleeper._normalize_nba_stats({"pts": 2, "fgm": 1, "fga": 2})
        self.assertEqual(data["fg3m"], 0)
        self.assertEqual(data["fgp"], .5)
        self.assertEqual(data["ftp"], 0)

    def test_projections_use_the_current_week_and_postseason_type(self):
        records = [{"player_id": 1, "date": "2026-10-20", "stats": {"pts": 12}}]
        with patch.object(sleeper, "_get", return_value=records) as get:
            self.assertIn("1", sleeper.get_nba_day_projections("2026-10-20", "2026", "post", week=25))
        self.assertEqual(get.call_args.args[0], "https://api.sleeper.app/projections/nba/2026/25")
        self.assertEqual(get.call_args.kwargs["params"]["season_type"], "post")

    def test_looks_up_week_when_not_supplied(self):
        with patch.object(sleeper, "get_nba_state", return_value={"season": "2026", "week": 2}), \
             patch.object(sleeper, "_get", return_value=[]) as get:
            self.assertEqual(sleeper.get_nba_day_stats("2026-10-28", "2026"), {})
        self.assertTrue(get.call_args.args[0].endswith("/2026/2"))

    def test_empty_player_dictionaries_are_not_accepted_as_success(self):
        with patch.object(sleeper, "_get", return_value={"1": {}, "2": {}}):
            self.assertIsNone(sleeper.get_nba_day_stats("2026-10-20", "2026", week=1))
            self.assertIsNone(sleeper.get_nfl_week_stats("2026", 7))

    def test_schedule_failure_allows_runtime_checks_but_empty_slate_skips(self):
        with patch.object(sleeper, "get_nba_state", return_value={"season": "2026", "week": 1}), \
             patch.object(sleeper, "get_nba_day_projections", return_value=None):
            self.assertTrue(game_schedule_checker.check_nba_active_today({}))
        with patch.object(sleeper, "get_nba_state", return_value={"season": "2026", "week": 1}), \
             patch.object(sleeper, "get_nba_day_projections", return_value={}):
            self.assertFalse(game_schedule_checker.check_nba_active_today({}))

    def test_game_records_reach_breakout_batch_without_sending_telegram(self):
        with open("config.json") as f:
            config = json.load(f)
        projections = {str(i): {"pts": 10+i, "reb": 3+i, "ast": 2+i, "stl": .5+i/10,
                                "blk": .5+i/10, "tpm": 1+i/10, "fgm": 5+i, "fga": 10+i,
                                "ftm": 2, "fta": 3, "to": 2} for i in range(6)}
        projection_records = [{"player_id": pid, "date": "2026-10-20", "stats": stats}
                              for pid, stats in projections.items()]
        stats_records = [{"player_id": "0", "date": "2026-10-20", "stats": {
            "pts": 40, "reb": 15, "ast": 12, "stl": 4, "blk": 4, "tpm": 6,
            "fgm": 15, "fga": 20, "ftm": 4, "fta": 4, "to": 1,
        }}]
        batch, history, state = [], [], {}
        with patch.object(main, "nba_game_day", return_value=date(2026, 10, 20)), \
             patch.object(sleeper, "get_nba_state", return_value={"season": "2026", "week": 1}), \
             patch.object(sleeper, "_get", side_effect=[projection_records, stats_records]), \
             patch.object(sleeper, "get_schedule", return_value=None), \
             patch.object(main.notifier, "send_message") as send:
            main.run_basketball(config, {}, {"0": {"full_name": "Test Player", "team": "BOS"}},
                                history, None, batch, state)
        self.assertEqual(len(batch), 1)
        self.assertTrue(batch[0]["send"])
        self.assertEqual(history[0]["sport"], "nba")
        self.assertEqual(state["sleeper_nba"]["consecutive_failures"], 0)
        send.assert_not_called()

    def test_unmatched_or_empty_data_records_health_failure(self):
        with open("config.json") as f:
            config = json.load(f)
        state = {}
        with patch.object(sleeper, "get_nba_state", return_value={"season": "2026", "week": 1}), \
             patch.object(sleeper, "get_nba_day_projections", return_value={"1": {"pts": 20}}), \
             patch.object(sleeper, "get_nba_day_stats", return_value={"2": {"pts": 30}}), \
             patch.object(main, "maybe_send_failure_alert"):
            main.run_basketball(config, {}, {}, [], None, [], state)
        self.assertEqual(state["sleeper_nba"]["consecutive_failures"], 1)
        self.assertIsNone(state["sleeper_nba"]["last_success"])


if __name__ == "__main__":
    unittest.main()
