import sys
import types
import unittest

# Digest formatting does not need Telegram or its HTTP dependency.
sys.modules.setdefault("telegram_notifier", types.ModuleType("telegram_notifier"))

from daily_digest import build_digest, collapse_breakouts


class DailyDigestTests(unittest.TestCase):
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
