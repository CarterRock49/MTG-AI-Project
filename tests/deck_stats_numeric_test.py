"""Numeric-safety regressions for deck analytics.

Run from the repository root with::

    python tests/deck_stats_numeric_test.py
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
