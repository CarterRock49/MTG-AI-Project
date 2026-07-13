"""Auto-tap parity tests for phase and conditional mana provenance.

Run from the repository root with::

    python tests/mana_auto_tap_test.py
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


class AutoTapPoolProvenanceTest(unittest.TestCase):
    def _state(self, land_text="{T}: Add {G}."):
        abandon = Card({
            "name": "Abandon Attachments",
            "type_line": "Instant - Lesson",
            "mana_cost": "{1}{U/R}",
            "cmc": 2,
            "oracle_text": "You may discard a card. If you do, draw two cards.",
            "color_identity": ["R", "U"],
        })
        land = Card({
            "name": "Planner Provenance Land",
            "type_line": "Land",
            "mana_cost": "",
            "oracle_text": land_text,
        })
        game_state = GameState({0: abandon, 1: land})
        game_state.reset([0], [0], seed=29)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        player = game_state.p1
        player["library"] = []
        player["hand"] = [0]
        player["battlefield"] = [1]
        player["graveyard"] = []
        player["exile"] = []
        player["tapped_permanents"] = set()
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0,
        }
        player["phase_restricted_mana"] = {}
        player["conditional_mana"] = {}
        game_state._last_card_locations[0] = (player, "hand")
        game_state._last_card_locations[1] = (player, "battlefield")
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.agent_is_p1 = True
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        card = game_state._safe_get_card(0)
        context = {"card": card, "source_zone": "hand", "hand_idx": 0}
        cost = game_state.mana_system.parse_mana_cost(card.mana_cost)
        return game_state, handler, player, context, cost

    def test_phase_mana_and_land_share_one_auto_tap_plan(self):
        game_state, _, player, context, cost = self._state()
        player["phase_restricted_mana"] = {"U": 1}

        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context))
        plan = game_state.mana_system._plan_auto_tap(
            player, cost, context)
        self.assertEqual(
            [(card_id, option.get("symbol")) for card_id, option in plan],
            [(1, "G")])
        self.assertTrue(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, cost, context))

        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context)
        self.assertIsNotNone(details)
        self.assertEqual(details["payment"]["phase_restricted"]["U"], 1)
        self.assertEqual(details["payment"]["colors"]["G"], 1)
        self.assertIn(1, player["tapped_permanents"])

    def test_conditional_mana_is_counted_only_for_matching_spell(self):
        game_state, handler, player, context, cost = self._state()
        player["conditional_mana"] = {
            "cast_only: spell": {"U": 1},
        }

        plan = game_state.mana_system._plan_auto_tap(
            player, cost, context)
        self.assertEqual(
            [(card_id, option.get("symbol")) for card_id, option in plan],
            [(1, "G")])
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[20],
                        "mask ignored conditional mana usable by the spell")
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(20)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertTrue(game_state.stack and game_state.stack[-1][1] == 0)
        self.assertIn(1, player["tapped_permanents"])
        self.assertFalse(player["conditional_mana"])

        game_state, _, player, context, cost = self._state()
        player["conditional_mana"] = {
            "spend_only: activated abilities": {"U": 1},
        }
        self.assertIsNone(game_state.mana_system._plan_auto_tap(
            player, cost, context))

    def test_restricted_land_output_requires_a_matching_predicate(self):
        allowed = (
            "{T}: Add {U}. Spend this mana only to cast instant spells.")
        game_state, _, player, context, cost = self._state(allowed)
        player["mana_pool"]["G"] = 1
        plan = game_state.mana_system._plan_auto_tap(
            player, cost, context)
        self.assertEqual(len(plan), 1)
        self.assertIn("instant", plan[0][1].get("restriction", "").lower())
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context)
        self.assertIsNotNone(details)
        self.assertIn(1, player["tapped_permanents"])
        self.assertEqual(sum(
            restricted_pool.get("U", 0)
            for restricted_pool in details["payment"][
                "conditional"].values()), 1)

        forbidden = (
            "{T}: Add {U}. Spend this mana only to cast creature spells.")
        game_state, _, player, context, cost = self._state(forbidden)
        player["mana_pool"]["G"] = 1
        self.assertIsNone(game_state.mana_system._plan_auto_tap(
            player, cost, context))


if __name__ == "__main__":
    unittest.main()
