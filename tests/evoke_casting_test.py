"""Focused transaction regressions for Evoke's alternative casting cost.

Run from the repository root with::

    python tests/evoke_casting_test.py
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.actions import ActionHandler  # noqa: E402
from Playersim.card import Card  # noqa: E402
from Playersim.game_state import GameState  # noqa: E402


logging.disable(logging.CRITICAL)


class EvokeCastingTest(unittest.TestCase):
    def _state(self):
        deceit = Card({
            "name": "Deceit",
            "mana_cost": "{4}{U/B}{U/B}",
            "cmc": 6,
            "type_line": "Creature — Elemental Incarnation",
            "oracle_text": (
                "When this creature enters, if {U}{U} was spent to cast it, "
                "return up to one other target nonland permanent to its "
                "owner's hand.\n"
                "When this creature enters, if {B}{B} was spent to cast it, "
                "target opponent reveals their hand. You choose a nonland "
                "card from it. That player discards that card.\n"
                "Evoke {U/B}{U/B}"
            ),
            "power": 5,
            "toughness": 5,
            "keywords": ["Evoke"],
            "color_identity": ["U", "B"],
        })
        game_state = GameState({0: deceit})
        game_state.reset([0], [0], seed=31)
        for participant in (game_state.p1, game_state.p2):
            for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
                participant[zone] = []
            participant["mana_pool"] = {
                "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0,
            }
        player = game_state.p1
        player["hand"] = [0]
        game_state._last_card_locations[0] = (player, "hand")
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        game_state.turn = 1
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state._last_turn_phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.agent_is_p1 = True
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler, player

    def test_evoke_cost_parser_preserves_both_hybrid_symbols(self):
        game_state, _, player = self._state()

        cost = game_state.mana_system.calculate_alternative_cost(
            0, player, "evoke", {"card": game_state._safe_get_card(0)})

        self.assertIsNotNone(cost)
        self.assertEqual(cost["hybrid"], [("U", "B"), ("U", "B")])
        self.assertEqual(cost["generic"], 0)
        self.assertEqual(
            sum(cost[color] for color in ("W", "U", "B", "R", "G", "C")),
            0)

    def test_mask_exposes_payable_evoke_but_not_unpayable_normal_cast(self):
        game_state, handler, player = self._state()
        player["mana_pool"]["U"] = 2

        mask = handler.generate_valid_actions()

        self.assertFalse(mask[20], "six-mana normal cast was incorrectly payable")
        self.assertTrue(mask[221], "two-mana Evoke cast was absent")
        self.assertEqual(
            handler.action_reasons_with_context[221]["context"],
            {"hand_idx": 0})

    def test_evoke_action_pays_alt_cost_and_preserves_stack_context(self):
        game_state, handler, player = self._state()
        player["mana_pool"]["B"] = 2
        mask = handler.generate_valid_actions()
        handler.current_valid_actions = mask

        _, _, _, info = handler.apply_action(221)

        self.assertFalse(info.get("execution_failed", False), info)
        self.assertNotIn(0, player["hand"])
        self.assertEqual(player["mana_pool"]["B"], 0)
        self.assertTrue(game_state.stack)
        item_type, card_id, controller, context = game_state.stack[-1]
        self.assertEqual((item_type, card_id, controller),
                         ("SPELL", 0, player))
        self.assertEqual(context.get("use_alt_cost"), "evoke")
        self.assertEqual(context.get("source_zone"), "hand")
        self.assertEqual(
            context.get("final_paid_details", {}).get("spent_specific"),
            {"B": 2})

    def test_evoke_respects_normal_creature_timing(self):
        game_state, handler, player = self._state()
        player["mana_pool"]["U"] = 2
        game_state.stack.append(("ABILITY", 99, game_state.p2, {}))

        mask = handler.generate_valid_actions()

        self.assertFalse(mask[221],
                         "Evoke incorrectly granted creature-spell flash")


if __name__ == "__main__":
    unittest.main()
