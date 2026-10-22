"""Offline end-to-end cycles with real state files and mocked upstream services."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

import alert_engine
import delivery
import main
from state_store import load, save


class CycleTests(unittest.TestCase):
    def test_polling_failure_does_not_stop_collection(self):
        with patch.object(main, 'poll_and_handle_telegram', side_effect=RuntimeError('offline')):
            rows, _ = self.run_twice()
        self.assertEqual(len(rows), 2)

    def setUp(self):
        with open('config.json', encoding='utf-8') as source:
            self.config = json.load(source)
        self.config['football']['enabled'] = False
        self.config['catchup']['lookback_days'] = 1
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.original_dir = os.getcwd()
        os.chdir(self.folder.name)
        self.addCleanup(os.chdir, self.original_dir)
        save('config.json', self.config)
        self.players = {'1': {'full_name': 'Alice', 'team': 'BOS'}, '2': {'full_name': 'Bob', 'team': 'BOS'}}

    def stats(self, day, season, *args, **kwargs):
        pid = '1' if day == '2026-10-20' else '2'
        return {pid: {'pts': 40, 'reb': 12, 'ast': 10, 'stl': 4, 'blk': 3,
                      'fg3m': 6, 'to': 1, 'min': 32, 'fgm': 15, 'fga': 20,
                      'fta': 4, 'ftm': 4, 'fgp': .75, 'ftp': 1}}

    def projections(self, day, season, *args, **kwargs):
        return {str(i): {'pts': 10 + i, 'reb': 3 + i, 'ast': 2 + i, 'stl': .5,
                         'blk': .5, 'fg3m': 1, 'to': 2, 'min': 24,
                         'fgm': 5, 'fga': 10, 'fta': 3, 'ftm': 2, 'fgp': .5, 'ftp': 2/3} for i in range(1, 6)}

    def games(self, sport, day):
        return [{'id': str(day), 'date': str(day), 'teams': ['BOS', 'NYK'], 'matchup': 'BOS vs NYK',
                 'state': 'post', 'final': True, 'period': 4, 'clock': '0:00', 'source': 'ESPN',
                 'fetched_at': '2026-10-22T04:00:00+00:00'}]

    def run_twice(self, commands=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, 'nba_game_day', return_value=date(2026, 10, 21)))
            stack.enter_context(patch.object(main, 'today_et', return_value=date(2026, 10, 21)))
            stack.enter_context(patch.object(main, 'get_players_cached', return_value=self.players))
            yahoo = stack.enter_context(patch.object(main, 'build_yahoo_manager', return_value=None))
            stack.enter_context(patch.object(main.sleeper, 'get_nba_state', return_value={'season': '2026', 'week': 1}))
            stack.enter_context(patch.object(main.sleeper, 'get_nba_day_stats', side_effect=self.stats))
            stack.enter_context(patch.object(main.sleeper, 'get_nba_day_projections', side_effect=self.projections))
            stack.enter_context(patch.object(alert_engine.game_context, 'fetch_games', side_effect=self.games))
            stack.enter_context(patch.object(main.notifier, 'get_new_updates', return_value={'taps': [], 'commands': commands or []}))
            send = stack.enter_context(patch.object(main.notifier, 'send_message', side_effect=range(100, 200)))
            main.main()
            first_count = send.call_count
            first_history = copy.deepcopy(load('dashboard_history.json', []))
            main.main()
            self.assertEqual(send.call_count, first_count, 'second poll must not resend alerts or identical command updates')
        return first_history, yahoo

    def test_catchup_and_repeated_runs_deliver_once_then_write_health_and_dashboard(self):
        rows, yahoo = self.run_twice()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['game_date'] for row in rows}, {'2026-10-20', '2026-10-21'})
        self.assertTrue(next(row for row in rows if row['game_date'] == '2026-10-20')['delayed'])
        self.assertTrue(all(row['notified'] for row in rows))
        dashboard = load('docs/data.json', {})
        self.assertEqual(dashboard['delivery']['pending'], 0)
        self.assertEqual(dashboard['runtime']['sports']['nba']['matched'], 1)
        self.assertTrue(dashboard['runtime']['last_completed'])
        self.assertEqual(len(dashboard['runtime']['games']), 2)
        self.assertEqual(len(load('performance_state.json', {})['players']), 2)

    def test_help_command_is_durable_and_not_added_to_digest_history(self):
        rows, _ = self.run_twice([{'text': '/help', 'update_id': 101}])
        self.assertEqual(len(rows), 2)
        journal = load(delivery.PATH, {})
        command = [item for item in journal['items'].values() if item['kind'] == 'command']
        self.assertEqual(len(command), 1)
        self.assertTrue(command[0]['delivered_at'])


if __name__ == '__main__':
    unittest.main()
