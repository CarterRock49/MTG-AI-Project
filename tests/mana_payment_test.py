"""Affordability/payment parity regressions for the enhanced mana system.

Run from the repository root with::

    python tests/mana_payment_test.py
"""

from __future__ import annotations

from collections import defaultdict
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


class HybridGenericAffordabilityTest(unittest.TestCase):
    def _state(self):
        abandon = Card({
            "name": "Abandon Attachments",
            "type_line": "Instant — Lesson",
            "mana_cost": "{1}{U/R}",
            "cmc": 2,
            "oracle_text": "You may discard a card. If you do, draw two cards.",
            "color_identity": ["R", "U"],
        })
        game_state = GameState({0: abandon})
        game_state.reset([0], [0], seed=19)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.agent_is_p1 = True
        game_state.priority_player = game_state.p1
        game_state.priority_pass_count = 0
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler

    def test_hybrid_unit_cannot_also_pay_generic(self):
        game_state, _ = self._state()
        player = game_state.p1
        cost = game_state.mana_system.parse_mana_cost("{1}{U/R}")

        player["mana_pool"]["R"] = 1
        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))

        player["mana_pool"]["R"] = 2
        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))

    def test_overlapping_hybrid_pips_use_a_complete_assignment(self):
        game_state, _ = self._state()
        player = game_state.p1
        cost = game_state.mana_system.parse_mana_cost("{W/U}{W/B}")
        player["mana_pool"]["U"] = 1
        player["mana_pool"]["W"] = 1

        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertEqual(player["mana_pool"]["U"], 0)
        self.assertEqual(player["mana_pool"]["W"], 0)

    def test_snow_unit_cannot_also_pay_generic_and_failure_mints_nothing(self):
        game_state, _ = self._state()
        player = game_state.p1
        player["mana_pool"]["U"] = 1
        player["snow_mana_pool"]["U"] = 1
        cost = game_state.mana_system.parse_mana_cost("{1}{S}")

        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        self.assertFalse(game_state.mana_system.pay_mana_cost(
            player, cost, context={}))
        self.assertEqual(player["mana_pool"]["U"], 1)
        self.assertEqual(player["snow_mana_pool"]["U"], 1)

        # Tracked mana spends belong to transaction-local pool copies. A
        # rollback must not add them to the untouched live pool.
        payment = {
            "colors": defaultdict(int, {"U": 1}),
            "conditional": defaultdict(lambda: defaultdict(int)),
            "phase_restricted": defaultdict(int),
            "life": 0, "snow": 1, "snow_tapped_sources": [],
            "tapped_creatures": [], "exiled_cards": [],
            "sacrificed_perms": [], "discarded_cards": [],
        }
        game_state.mana_system._refund_payment(player, payment)
        self.assertEqual(player["mana_pool"]["U"], 1)

    def test_phyrexian_pips_reserve_mana_and_aggregate_life(self):
        game_state, _ = self._state()
        player = game_state.p1
        cost = game_state.mana_system.parse_mana_cost("{U/P}{U/P}")

        player["life"] = 3
        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        player["life"] = 4
        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertEqual(player["life"], 0)

    def test_abandon_auto_taps_generic_land_after_hybrid_pool_unit(self):
        game_state, handler = self._state()
        player = game_state.p1
        forest = Card({
            "name": "Test Forest", "type_line": "Basic Land — Forest",
            "mana_cost": "", "cmc": 0, "oracle_text": "{T}: Add {G}.",
            "color_identity": ["G"],
        })
        forest_id = max(
            [key for key in game_state.card_db if isinstance(key, int)],
            default=-1) + 1
        forest.card_id = forest_id
        game_state.card_db[forest_id] = forest
        player["battlefield"].append(forest_id)
        game_state._last_card_locations[forest_id] = (player, "battlefield")
        abandon_id = player["hand"][0]
        player["mana_pool"]["R"] = 1

        valid = handler.generate_valid_actions()
        self.assertTrue(valid[20])
        handler.current_valid_actions = valid
        _, _, _, info = handler.apply_action(20)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertNotIn(abandon_id, player["hand"])
        self.assertIn(forest_id, player["tapped_permanents"])
        self.assertEqual(player["mana_pool"]["R"], 0)

    def test_mask_only_exposes_abandon_when_live_payment_can_succeed(self):
        game_state, handler = self._state()
        player = game_state.p1
        card_id = player["hand"][0]
        self.assertEqual(game_state._safe_get_card(card_id).name,
                         "Abandon Attachments")

        player["mana_pool"]["R"] = 1
        invalid_mask = handler.generate_valid_actions()
        self.assertFalse(invalid_mask[20])

        player["mana_pool"]["R"] = 2
        valid_mask = handler.generate_valid_actions()
        self.assertTrue(valid_mask[20])
        handler.current_valid_actions = valid_mask
        _, _, _, info = handler.apply_action(20)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertNotIn(card_id, player["hand"])
        self.assertEqual(player["mana_pool"]["R"], 0)


if __name__ == "__main__":
    unittest.main()
