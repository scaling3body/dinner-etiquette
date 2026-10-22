import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock

import bot_commands
import daily_digest
import delivery
import game_context
import main
import health
import player_insights
import scoring
import sleeper_client
import telegram_notifier as telegram
from state_store import load
from yahoo_client import YahooRosterManager, _normalize


def event(name="Alice", player="1", text=None):
    return {"send": True, "text": text or f"BREAKOUT: {name}", "addable": False,
            "player_name": name, "sport": "nba", "sleeper_player_id": player,
            "alert_key": "nba-2026-10-21", "kind": "breakout", "count": 1, "baseline": 8,
            "history": {"sport": "nba", "player_name": name, "message": text or f"BREAKOUT: {name}",
                        "game_date": "2026-10-21", "score": 8}}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = os.path.join(self.folder.name, "delivery.json")
        self.alerted, self.history, self.pending, self.health = {}, [], {}, {}

    def dispatch(self, batch):
        return delivery.dispatch({}, self.pending, batch, self.health, self.alerted, self.history, path=self.path)

    def test_queue_is_saved_before_send_and_failure_does_not_advance_baseline(self):
        def fail(*args, **kwargs):
            self.assertEqual(len(load(self.path, {})["items"]), 1)
            self.assertEqual(self.alerted, {})
            raise telegram.TelegramError("offline", 0)
        with patch.object(telegram, "send_message", side_effect=fail):
            summary = self.dispatch([event()])
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(self.history, [])
        self.assertEqual(self.alerted, {})
        with patch.object(telegram, "send_message", return_value=17) as send:
            self.dispatch([event()])
        send.assert_called_once()
        self.assertEqual(self.alerted["nba-2026-10-21"]["1"]["count"], 1)
        self.assertTrue(self.history[0]["notified"])
        self.assertEqual(self.history[0]["message_ids"], [17])

    def test_partial_batch_retries_only_failed_message_and_receipts_repair_state(self):
        a, b = event(text="A" * 2500), event("Bob", "2", "B" * 2500)
        with patch.object(telegram, "send_message", side_effect=[11, telegram.TelegramError("offline", 0)]):
            self.dispatch([a, b])
        self.assertEqual(len(self.history), 1)
        self.assertNotIn("2", self.alerted["nba-2026-10-21"])
        with patch.object(telegram, "send_message", return_value=12) as send:
            self.dispatch([a, b])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.history), 2)
        self.alerted.clear()
        self.history.clear()  # simulate a crash before the other JSON stores were saved
        with patch.object(telegram, "send_message") as send:
            self.dispatch([a, b])
        send.assert_not_called()
        self.assertEqual(len(self.history), 2)
        self.assertEqual(len(self.alerted["nba-2026-10-21"]), 2)

    def test_oversized_individual_alert_is_only_delivered_after_all_parts(self):
        item = event(text="🏀" * 4500)
        with patch.object(telegram, "send_message", side_effect=[1, telegram.TelegramError("offline", 0)]):
            self.dispatch([item])
        self.assertFalse(self.history)
        with patch.object(telegram, "send_message", side_effect=[2, 3]) as send:
            self.dispatch([])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.history[0]["message_ids"], [1, 2, 3])
        for call in send.call_args_list:
            self.assertLessEqual(telegram.text_units(call.args[1]), telegram.MAX_TEXT_UNITS)

    def test_retry_after_and_no_false_health_refresh(self):
        with patch.object(telegram, "send_message", side_effect=telegram.TelegramError("limited", 3600)):
            self.dispatch([event()])
        with patch.object(telegram, "send_message") as send:
            self.dispatch([])
        send.assert_not_called()
        self.assertIsNone(self.health["telegram"]["last_success"])

    def test_command_replies_are_not_in_breakout_history(self):
        item = dict(event(), event_id="command:123", kind="command", exclude_history=True)
        with patch.object(telegram, "send_message", return_value=1):
            self.dispatch([item])
        self.assertFalse(self.alerted)
        self.assertFalse(self.history)

    def test_success_without_new_send_does_not_change_last_success(self):
        with patch.object(telegram, "send_message", return_value=1):
            self.dispatch([event()])
        previous = self.health["telegram"]["last_success"]
        with patch.object(telegram, "send_message") as send:
            self.dispatch([])
        self.assertEqual(self.health["telegram"]["last_success"], previous)
        send.assert_not_called()


class ScoringInsightTests(unittest.TestCase):
    def test_perfect_low_volume_shooting_does_not_inflate_score(self):
        actual = {"pts": 2, "fgp": 1, "ftp": 1, "fgm": 1, "fga": 1, "ftm": 1, "fta": 1}
        projected = {"pts": 2, "fgp": .4, "ftp": .75, "fga": 5, "fta": 3}
        score, breakdown = scoring.compute_zscore(actual, projected, {"1": projected}, ["pts", "fgp", "ftp"], [])
        self.assertEqual(score, 0)
        self.assertEqual(breakdown["fgp"]["z"], 0)
        self.assertFalse(player_insights.qualifies(dict(actual, min=4), {}))

    def test_zero_attempts_neither_rewards_nor_penalizes_percentages(self):
        score, _ = scoring.compute_zscore({"fgp": 0}, {"fgp": .5}, {}, ["fgp"], [])
        self.assertEqual(score, 0)

    def test_non_numeric_and_null_football_stats_do_not_crash(self):
        self.assertEqual(scoring.calc_fantasy_points({"pts": None, "reb": "bad", "ast": float("nan")}, {"pts": 1, "reb": 1, "ast": 1}), 0)

    def test_big_minutes_signal_requires_live_observed_clock(self):
        actual, projected = {"min": 18, "fga": 8}, {"min": 20, "fga": 6}
        live = {"state": "in", "period": 2, "remaining_seconds": 360}
        self.assertIn("Minutes opportunity", player_insights.opportunity(actual, projected, live, {}))
        self.assertIsNone(player_insights.opportunity(actual, projected, {"state": "post"}, {}))
        self.assertIsNone(player_insights.opportunity(actual, projected, None, {}))

    def test_observed_start_is_not_inferred_when_missing(self):
        game = {"state": "in", "period": 2, "remaining_seconds": 360}
        actual, projected = {"min": 10, "gs": 1}, {"min": 30, "gs": 0}
        self.assertIn("Starting-role", player_insights.opportunity(actual, projected, game, {}))
        self.assertIsNone(player_insights.opportunity({"min": 10}, {"min": 30}, game, {}))

    def test_trend_counts_completed_distinct_games_only(self):
        state = {}
        result = {"total_zscore": 8}
        for _ in range(4):
            self.assertIsNone(player_insights.observe(state, "nba", "1", "2026-10-19", result, {"min": 30}, True, {}))
        self.assertEqual(len(state["players"]["nba:1"]), 1)
        self.assertIsNone(player_insights.observe(state, "nba", "1", "2026-10-20", result, {}, False, {}))
        player_insights.observe(state, "nba", "1", "2026-10-20", result, {}, True, {})
        self.assertIn("3 of the last 3", player_insights.observe(state, "nba", "1", "2026-10-21", result, {}, True, {}))

    def test_filtered_tiny_stat_line_does_not_count_as_trend(self):
        state = {}
        player_insights.observe(state, "nba", "1", "2026-10-21", {"total_zscore": 20, "eligible": False}, {}, True, {})
        self.assertFalse(state["players"]["nba:1"][0]["qualifies"])

    def test_week_rollover_uses_requested_date(self):
        state = {"season_start_date": "2026-10-20", "week": 2}
        self.assertEqual(sleeper_client.nba_week_for_day("2026-10-25", state), 1)
        self.assertEqual(sleeper_client.nba_week_for_day("2026-10-26", state), 2)


class ContextCommandTests(unittest.TestCase):
    def test_nfl_scoreboard_keeps_root_week_metadata_for_catchup(self):
        response = Mock()
        response.json.return_value = {'week': {'number': 6}, 'season': {'year': 2026, 'type': 2},
            'events': [{'id': 'game', 'status': {'type': {'state': 'post', 'completed': True}},
                        'competitions': [{'competitors': [{'team': {'abbreviation': 'KC'}}, {'team': {'abbreviation': 'BUF'}}]}]}]}
        game_context._cache.clear()
        with patch.object(game_context._session, 'get', return_value=response):
            games = game_context.fetch_games('nfl', '2026-10-19')
        self.assertEqual(games[0]['week'], 6)
        self.assertEqual(games[0]['season'], 2026)
        self.assertEqual(games[0]['season_type'], 'regular')
        game_context._cache.clear()

    def test_progress_aliases_match_opponent_and_game_date(self):
        game = {"teams": ["NYK", "GSW"], "state": "in", "period": 5, "clock": "2:30"}
        self.assertIs(game_context.for_team([game], "NY", "GS"), game)
        self.assertIsNone(game_context.for_team([game], "NY", "BOS"))
        self.assertEqual(game_context.display(game), "OT1 · 2:30")

    def test_game_context_unavailable_fails_open(self):
        with patch.object(game_context._session, "get", side_effect=__import__('requests').RequestException):
            game_context._cache.clear()
            self.assertIsNone(game_context.fetch_games("nba", "2026-10-21"))
        game_context._cache.clear()

    def test_commands_report_settings_without_secrets_and_filter_delivered(self):
        with open("config.json", encoding="utf-8") as source:
            config = json.load(source)
        config["notifications"]["telegram_bot_token"] = "PRIVATE"
        response = bot_commands.reply("/settings@MyBot", config, {}, [], {})
        self.assertIn("minimum 12 minutes", response)
        self.assertNotIn("PRIVATE", response)
        self.assertIn("/status", bot_commands.reply("/help", config, {}, [], {}))
        rows = [{"sport": "nba", "notified": True, "message": "NBA delivered"},
                {"sport": "nfl", "notified": True, "message": "NFL delivered"},
                {"sport": "nba", "notified": False, "message": "SUPPRESSED"}]
        self.assertEqual(bot_commands.reply("/recent nba 1", config, {}, rows, {}), "Recent delivered alerts:\n\nNBA delivered")

    def test_telegram_api_rejection_with_http_200_is_failure_and_token_is_hidden(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": False, "error_code": 429, "parameters": {"retry_after": 17}}
        with patch.object(telegram.requests, "post", return_value=response):
            with self.assertRaises(telegram.TelegramError) as raised:
                telegram.send_message({"notifications": {"telegram_bot_token": "PRIVATE", "telegram_chat_id": "1"}}, "test")
        self.assertEqual(raised.exception.retry_after, 17)
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_unicode_long_lines_are_split_without_losing_characters(self):
        text = "🏀" * 5000
        parts = telegram.split_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(telegram.text_units(part) <= 3900 for part in parts))


class YahooTests(unittest.TestCase):
    def test_failure_health_is_isolated_per_sport(self):
        state = {}
        health.record_failure(state, 'yahoo_nfl')
        health.open_yahoo_circuit(state, component='yahoo_nfl')
        health.record_success(state, 'yahoo_nba')
        self.assertTrue(health.yahoo_circuit_is_open(state, 'yahoo_nfl'))
        self.assertFalse(health.yahoo_circuit_is_open(state, 'yahoo_nba'))

    def test_expired_and_unknown_roster_actions_do_not_execute(self):
        item = {'sport': 'nba', 'player_id': '1', 'player_name': 'Alice', 'message_id': 1,
                'created_at': '2010-01-01T00:00:00+00:00'}
        manager = Mock()
        pending = {'code': item}
        with patch.object(main.notifier, 'acknowledge_tap'), patch.object(main.notifier, 'edit_message'), patch.object(main, 'save_json'):
            main.process_confirmations({}, pending, {'nba': manager}, [{'callback_data': 'add:code', 'callback_query_id': 'q'}])
        manager.add_player.assert_not_called()
        self.assertFalse(pending)

    def test_failed_telegram_edit_does_not_replay_successful_roster_action(self):
        item = {'sport': 'nba', 'player_id': '1', 'player_name': 'Alice', 'message_id': 1,
                'created_at': datetime.now(timezone.utc).isoformat()}
        manager = Mock()
        pending = {'code': item}
        tap = {'callback_data': 'add:code', 'callback_query_id': 'q'}
        with patch.object(main.notifier, 'acknowledge_tap'), patch.object(main.notifier, 'edit_message', side_effect=RuntimeError('offline')), patch.object(main, 'save_json'):
            main.process_confirmations({}, pending, {'nba': manager}, [tap])
            main.process_confirmations({}, pending, {'nba': manager}, [tap])
        manager.add_player.assert_called_once_with('1')
        self.assertFalse(pending)

    def manager(self):
        manager = object.__new__(YahooRosterManager)
        manager.sport = "nba"
        manager._free_agent_cache = manager._name_index = manager._roster_cache = None
        manager._open_slot_cache = manager._settings_cache = None
        manager.league, manager.team = Mock(), Mock()
        manager.league.free_agents.return_value = []
        manager.league.waivers.return_value = []
        manager.team.roster.return_value = []
        manager.league.positions.return_value = {"PG": {"count": 1}, "BN": {"count": 1}, "IR": {"count": 1}}
        manager.league.settings.return_value = {}
        return manager

    def test_names_support_accents_suffixes_and_own_roster(self):
        self.assertEqual(_normalize("Nikola Jokić Jr."), _normalize("Nikola Jokic"))
        manager = self.manager()
        manager.team.roster.return_value = [{"name": "Nikola Jokić"}]
        self.assertEqual(manager.get_player_status("Nikola Jokic")[0], "On your roster")

    def test_waivers_are_scanned_and_scans_are_cached(self):
        manager = self.manager()
        manager.league.waivers.return_value = [{"player_id": 1, "name": "Alice"}]
        self.assertEqual(manager.get_player_status("Alice")[0], "On waivers")
        manager.get_player_status("Alice")
        self.assertEqual(manager.league.free_agents.call_count, 5)
        self.assertEqual(manager.league.waivers.call_count, 1)

    def test_partial_scan_and_ambiguous_names_do_not_guess(self):
        manager = self.manager()
        manager.league.free_agents.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            manager.get_player_status("Alice")
        manager = self.manager()
        manager.league.waivers.return_value = [{"player_id": 1, "name": "Alice"}, {"player_id": 2, "name": "Alice"}]
        with self.assertRaises(ValueError):
            manager.find_player("Alice")

    def test_empty_ir_slot_does_not_allow_healthy_add(self):
        manager = self.manager()
        manager.team.roster.return_value = [{"selected_position": "PG"}, {"selected_position": "BN"}]
        self.assertFalse(manager.get_open_bench_or_ir_slots())
        with self.assertRaises(ValueError):
            manager.add_player(1)
        manager.team.add_player.assert_not_called()


class DigestRetryTests(unittest.TestCase):
    def test_grouped_digest_uses_game_day_for_late_alerts_and_keeps_strongest(self):
        rows = [{"sport": "nba", "player_name": "Alice", "message": "BREAKOUT", "game_date": "2026-10-21",
                 "game_label": "BOS vs NYK", "score": score, "actual_stats": {"pts": points}}
                for score, points in [(7, 20), (9, 30)]]
        text = daily_digest.build_digest(rows)
        self.assertIn("BOS vs NYK", text)
        self.assertIn("30 PTS", text)
        self.assertNotIn("20 PTS", text)
        self.assertEqual(text.count("Alice"), 1)

    def test_partial_digest_delivery_resumes_after_last_confirmed_part(self):
        state = {}
        now = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
        rows = [{"sport": "nba", "player_name": "Player " + str(i) + "x" * 90, "message": "BREAKOUT",
                 "game_date": "2026-10-21", "timestamp": "2026-10-22T04:00:00+00:00", "notified": True} for i in range(70)]
        with patch.object(telegram, "send_message", side_effect=[11, telegram.TelegramError("offline")]):
            with self.assertRaises(telegram.TelegramError):
                daily_digest.send_daily_digest({}, rows, state, now)
        self.assertNotIn("2026-10-21", state.get("sent_days", {}))
        remaining = len(state["in_progress"]["2026-10-21"]["parts"]) - 1
        with patch.object(telegram, "send_message", return_value=12) as send:
            daily_digest.send_daily_digest({}, [], state, now)
        self.assertEqual(send.call_count, remaining)
        with patch.object(telegram, "send_message") as send:
            daily_digest.send_daily_digest({}, rows, state, now)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
