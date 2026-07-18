from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_real_card  # noqa: E402


class TieredParserRegressionTest(unittest.TestCase):
    def test_all_standard_tiered_rows_are_structured(self):
        expected_rows = {
            "Thunder Magic": (
                ("Thunder", "{0}"),
                ("Thundara", "{3}"),
                ("Thundaga", "{5}{R}"),
            ),
            "Vincent's Limit Break": (
                ("Galian Beast", "{0}"),
                ("Death Gigas", "{1}"),
                ("Hellmasker", "{3}"),
            ),
            "Ice Magic": (
                ("Blizzard", "{0}"),
                ("Blizzara", "{2}"),
                ("Blizzaga", "{5}{U}"),
            ),
            "Fire Magic": (
                ("Fire", "{0}"),
                ("Fira", "{2}"),
                ("Firaga", "{5}"),
            ),
            "Tifa's Limit Break": (
                ("Somersault", "{0}"),
                ("Meteor Strikes", "{2}"),
                ("Final Heaven", "{6}{G}"),
            ),
            "Restoration Magic": (
                ("Cure", "{0}"),
                ("Cura", "{1}"),
                ("Curaga", "{3}{W}"),
            ),
        }

        game_state = fresh(31901)
        for card_name, expected in expected_rows.items():
            with self.subTest(card=card_name):
                card_id = inject_real_card(
                    game_state, game_state.p1, card_name, "hand")
                card = game_state._safe_get_card(card_id)
                self.assertTrue(card.is_tiered)
                self.assertEqual(
                    tuple((mode["label"], mode["cost"])
                          for mode in card.tiered_modes),
                    expected)
                self.assertTrue(
                    all(mode["effect"] for mode in card.tiered_modes))
                self.assertTrue(
                    card._tiered_related_text_marker.startswith("Tiered"))

                if card_name == "Vincent's Limit Break":
                    self.assertIn(
                        "chosen base power and toughness",
                        card.tiered_shared_effect)
                    self.assertEqual(
                        [mode["effect"] for mode in card.tiered_modes],
                        ["3/2", "5/2", "7/2"])
                else:
                    self.assertEqual(card.tiered_shared_effect, "")

        thunder = next(
            card for card in game_state.card_db.values()
            if getattr(card, "name", "") == "Thunder Magic")
        self.assertEqual(
            [mode["effect"] for mode in thunder.tiered_modes],
            [
                "Thunder Magic deals 2 damage to target creature",
                "Thunder Magic deals 4 damage to target creature",
                "Thunder Magic deals 8 damage to target creature",
            ])


if __name__ == "__main__":
    unittest.main()
