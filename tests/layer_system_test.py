"""Focused regression tests for continuous-effect layer handling.

Run from the repository root with::

    python tests/layer_system_test.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.ability_types import StaticAbility  # noqa: E402
from Playersim.card import Card  # noqa: E402
from Playersim.game_state import GameState  # noqa: E402


def _creature(name, subtypes, power, toughness, oracle_text=""):
    subtype_text = " ".join(subtypes)
    return Card({
        "name": name,
        "mana_cost": "{1}{U}",
        "cmc": 2,
        "type_line": f"Creature — {subtype_text}",
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle_text,
        "color_identity": ["U"],
    })


class CountedSubtypeCdaTest(unittest.TestCase):
    def _game(self):
        namor = _creature(
            "Namor the Sub-Mariner", ["Mutant", "Merfolk", "Villain"],
            "*", "4",
            "Flying\n"
            "Namor's power is equal to the number of Merfolk you control.")
        merfolk = _creature("Test Merfolk", ["Merfolk", "Soldier"], "3", "2")
        bear = _creature("Test Bear", ["Bear"], "2", "2")
        opposing_merfolk = _creature(
            "Opposing Merfolk", ["Merfolk", "Scout"], "1", "1")
        game_state = GameState({
            0: namor,
            1: merfolk,
            2: bear,
            3: opposing_merfolk,
        })
        game_state.reset([0, 1, 2], [3], seed=7)

        by_name = {}
        for player in (game_state.p1, game_state.p2):
            for zone in ("hand", "library"):
                for card_id in player[zone]:
                    by_name[game_state._safe_get_card(card_id).name] = (
                        card_id, player, zone)
        for name in (
                "Test Merfolk", "Test Bear", "Namor the Sub-Mariner",
                "Opposing Merfolk"):
            card_id, player, zone = by_name[name]
            self.assertTrue(game_state.move_card(
                card_id, player, zone, player, "battlefield"))
        return game_state, {
            name: card_id for name, (card_id, _, _) in by_name.items()
        }

    def test_subtype_count_cda_uses_only_controlled_matching_subtypes(self):
        game_state, ids = self._game()
        namor_id = ids["Namor the Sub-Mariner"]
        helper_id = ids["Test Merfolk"]
        bear_id = ids["Test Bear"]

        layer_effects = [
            data for _, data in game_state.layer_system.layers[7]["a"]
            if data.get("source_id") == namor_id
        ]
        self.assertEqual(len(layer_effects), 1)
        self.assertEqual(layer_effects[0]["affected_ids"], [namor_id])
        self.assertEqual(layer_effects[0]["effect_value"], {
            "kind": "subtype_count_power_self",
            "subtype": "merfolk",
        })

        game_state.layer_system.apply_all_effects()
        namor = game_state._safe_get_card(namor_id)
        self.assertEqual(namor.power, 2)  # Namor counts itself and the helper.
        self.assertEqual(namor.toughness, 4)
        self.assertEqual(game_state._safe_get_card(bear_id).power, 2)

        game_state.cards_to_graveyard_this_turn.setdefault(
            game_state.turn, [])
        self.assertTrue(game_state.move_card(
            helper_id, game_state.p1, "battlefield",
            game_state.p1, "graveyard"))
        game_state.layer_system.apply_all_effects()
        self.assertEqual(namor.power, 1)
        self.assertEqual(namor.toughness, 4)

    def test_layer_parsers_emit_generic_subtype_count_descriptor(self):
        text = "Champion's power is equal to the number of Knights you control"
        expected = {
            "sublayer": "a",
            "effect_type": "set_pt_cda",
            "effect_value": {
                "kind": "subtype_count_power_self",
                "subtype": "knights",
            },
        }
        ability = StaticAbility(0, text)
        self.assertEqual(ability._parse_layer7_effect(text.lower()), expected)

        game_state = GameState({})
        self.assertEqual(
            game_state.layer_system._parse_layer7_effect(text.lower()),
            expected)
        self.assertTrue(
            game_state.layer_system._counted_subtype_name_matches(
                "knights", "Knight"))
        self.assertTrue(
            game_state.layer_system._counted_subtype_name_matches(
                "merfolk", "Merfolk"))
        self.assertTrue(
            game_state.layer_system._counted_subtype_name_matches(
                "elves", "Elf"))


if __name__ == "__main__":
    unittest.main()
