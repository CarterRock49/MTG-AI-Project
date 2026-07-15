"""Numeric-safety regressions for deck analytics.

Run from the repository root with::

    python tests/deck_stats_numeric_test.py
"""

from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.card import Card  # noqa: E402
from Playersim.deck_stats_tracker import (  # noqa: E402
    DeckArchetype,
    DeckStatsTracker,
    _finite_card_number,
)


class DeckStatsNumericSafetyTest(unittest.TestCase):
    def _cards(self):
        namor = Card({
            "name": "Namor the Sub-Mariner",
            "type_line": "Legendary Creature — Mutant Merfolk Villain",
            "mana_cost": "{1}{U}{U}",
            "cmc": 3,
            "power": "*",
            "toughness": "4",
            "oracle_text": (
                "Flying\nNamor's power is equal to the number of Merfolk "
                "you control."),
            "color_identity": ["U"],
        })
        malformed_creature = Card({
            "name": "Unknown Statistics",
            "type_line": "Creature — Merfolk",
            "mana_cost": "{U}",
            "cmc": None,
            "power": "*",
            "toughness": "*",
            "oracle_text": "",
            "color_identity": ["U"],
        })
        malformed_creature.cmc = float("nan")
        return {0: namor, 1: malformed_creature}

    def test_finite_card_number_normalizes_unknown_and_nonfinite_values(self):
        cards = self._cards()
        self.assertEqual(_finite_card_number(cards[0], "power"), 0.0)
        self.assertEqual(_finite_card_number(cards[0], "toughness"), 4.0)
        self.assertEqual(_finite_card_number(cards[1], "cmc"), 0.0)
        cards[1].power = float("inf")
        self.assertEqual(_finite_card_number(cards[1], "power"), 0.0)
        self.assertTrue(math.isfinite(
            _finite_card_number(cards[1], "power")))

    def test_archetype_scoring_accepts_symbolic_pt_and_missing_cmc(self):
        cards = self._cards()
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = DeckStatsTracker(
                storage_path=temp_dir, card_db=cards,
                use_compression=False)
            deck = [0] * 4 + [1] * 4
            winner_archetype = tracker.identify_archetype(deck)
            loser_archetype = tracker.identify_archetype(list(reversed(deck)))

        valid = {archetype.value for archetype in DeckArchetype}
        self.assertIn(winner_archetype, valid)
        self.assertEqual(loser_archetype, winner_archetype)

    def test_training_persistence_batches_episode_flushes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = DeckStatsTracker(
                storage_path=temp_dir, use_compression=False,
                persistence_interval_games=3)
            flushes = []
            tracker.identify_archetype = lambda _deck: "midrange"
            tracker.update_meta_with_game_result = lambda **_kwargs: True
            tracker._update_deck_stats = lambda **_kwargs: True
            tracker._update_card_stats = lambda **_kwargs: True

            def record_flush():
                flushes.append(True)
                tracker._games_since_persistence = 0
                return True

            tracker.save_updates_sync = record_flush
            for game_index in range(5):
                self.assertTrue(tracker.record_game(
                    winner_deck=[], loser_deck=[], card_db={},
                    turn_count=game_index + 1,
                    winner_deck_name="Winner",
                    loser_deck_name="Loser"))
            self.assertEqual(len(flushes), 1)
            self.assertEqual(tracker._games_since_persistence, 2)

    def test_failed_deck_batch_is_retained_for_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = DeckStatsTracker(
                storage_path=temp_dir, use_compression=False)
            tracker.batch_updates["deck-id"] = {"name": "Retry Deck"}
            tracker.validate_deck_stats = lambda _stats: (True, [])
            tracker._generate_deck_filename = (
                lambda *_args: "retry_deck.json")

            async def fail_save(_path, _stats):
                return False

            tracker.save_async = fail_save
            self.assertFalse(asyncio.run(tracker.save_batch_updates()))
            self.assertIn("deck-id", tracker.batch_updates)

            async def pass_save(_path, _stats):
                return True

            tracker.save_async = pass_save
            self.assertTrue(asyncio.run(tracker.save_batch_updates()))
            self.assertNotIn("deck-id", tracker.batch_updates)


if __name__ == "__main__":
    unittest.main()
