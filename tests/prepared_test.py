"""Focused transaction regressions for the Prepared mechanic.

Run from the repository root with::

    python tests/prepared_test.py
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


def prepared_card():
    return Card({
        "name": "Emeritus of Ideation // Ancestral Recall",
        "layout": "prepare",
        "mana_cost": "{3}{U}{U} // {U}",
        "type_line": "Creature - Human Wizard",
        "color_identity": ["U"],
        "card_faces": [
            {
                "name": "Emeritus of Ideation",
                "mana_cost": "{3}{U}{U}",
                "type_line": "Creature - Human Wizard",
                "oracle_text": (
                    "Flying, ward {2}\n"
                    "This creature enters prepared.\n"
                    "Whenever this creature attacks, you may exile eight "
                    "cards from your graveyard. If you do, this creature "
                    "becomes prepared."
                ),
                "power": "5", "toughness": "5",
            },
            {
                "name": "Ancestral Recall", "mana_cost": "{U}",
                "type_line": "Instant",
                "oracle_text": "Target player draws three cards.",
            },
        ],
    })


class PreparedTest(unittest.TestCase):
    def _state(self):
        cards = {0: prepared_card()}
        for card_id in range(1, 31):
            cards[card_id] = Card({
                "name": f"Prepared Fixture {card_id}",
                "mana_cost": "{1}", "cmc": 1,
                "type_line": "Sorcery", "oracle_text": "",
            })
        game_state = GameState(cards)
        game_state.reset(list(range(15)), list(range(15, 30)), seed=73)
        for participant in (game_state.p1, game_state.p2):
            for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
                participant[zone] = []
            participant["mana_pool"] = {
                "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0,
            }
        player = game_state.p1
        opponent = game_state.p2
        player["hand"] = [0]
        game_state._last_card_locations[0] = (player, "hand")
        opponent["library"] = [1, 2, 3, 4, 5]
        for card_id in opponent["library"]:
            game_state._last_card_locations[card_id] = (opponent, "library")
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
        return game_state, handler, player, opponent

    @staticmethod
    def _apply(handler, action):
        mask = handler.generate_valid_actions()
        if not mask[action]:
            raise AssertionError(
                f"action {action} absent: {handler.action_reasons}")
        handler.current_valid_actions = mask
        result = handler.apply_action(action)
        if result[3].get("execution_failed", False):
            raise AssertionError(result[3])
        return result

    def _enter(self, game_state, player):
        self.assertTrue(game_state.move_card(
            0, player, "hand", player, "battlefield", cause="fixture_entry"))
        self.assertIn(0, game_state.prepared_cards)

    def test_layout_entry_cast_copy_resolution_and_clone(self):
        game_state, handler, player, opponent = self._state()
        card = game_state._safe_get_card(0)
        self.assertFalse(card.is_mdfc(), "prepare layout became an MDFC")
        self._enter(game_state, player)
        player["mana_pool"]["U"] = 1

        self._apply(handler, 451)
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        self.assertIn(0, game_state.prepared_cards)
        self.assertEqual(player["mana_pool"]["U"], 1)

        target_mask = handler.generate_valid_actions()
        candidates = handler._get_target_selection_candidates(
            player, game_state.targeting_context)
        target_action = 274 + candidates.index("p2")
        self.assertTrue(target_mask[target_action])
        handler.current_valid_actions = target_mask
        result = handler.apply_action(target_action)
        self.assertFalse(result[3].get("execution_failed", False), result[3])

        self.assertIn(0, player["battlefield"])
        self.assertNotIn(0, game_state.prepared_cards)
        self.assertEqual(player["mana_pool"]["U"], 0)
        self.assertEqual(len(game_state.stack), 1)
        item_type, source_id, controller, context = game_state.stack[-1]
        self.assertEqual((item_type, source_id, controller),
                         ("SPELL", 0, player))
        self.assertTrue(context.get("prepared_copy"))
        self.assertTrue(context.get("is_copy"))
        self.assertEqual(context.get("source_zone"), "prepared_exile")
        self.assertEqual(context["prepared_face"]["name"], "Ancestral Recall")
        self.assertNotIn("card", context)

        clone = game_state.clone()
        self.assertIsNotNone(clone)
        before = len(clone.p2["hand"])
        self.assertTrue(clone.resolve_top_of_stack())
        self.assertEqual(len(clone.p2["hand"]), before + 3)
        self.assertIn(0, clone.p1["battlefield"])
        self.assertNotIn(0, clone.p1["graveyard"])
        self.assertFalse(clone.stack)

        self.assertEqual(opponent["hand"], [])
        self.assertTrue(game_state.stack)

    def test_attack_payment_exiles_exactly_eight_and_reprepares(self):
        game_state, handler, player, _ = self._state()
        self._enter(game_state, player)
        game_state.prepared_cards.discard(0)
        player["graveyard"] = list(range(6, 15))
        for card_id in player["graveyard"]:
            game_state._last_card_locations[card_id] = (player, "graveyard")

        game_state.ability_handler.handle_attack_triggers(0)
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.stack)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.choice_context.get("type"),
                         "prepared_payment")

        for selection in range(8):
            graveyard_before = list(player["graveyard"])
            self._apply(handler, 353)
            if selection == 0:
                choice_clone = game_state.clone()
                self.assertIsNotNone(choice_clone)
                self.assertEqual(
                    choice_clone.choice_context.get("selected_cards"), [6])
                self.assertEqual(choice_clone.p1["graveyard"],
                                 player["graveyard"])
            if selection < 7:
                self.assertEqual(player["graveyard"], graveyard_before)
                self.assertNotIn(0, game_state.prepared_cards)

        self.assertEqual(len(player["exile"]), 8)
        self.assertEqual(len(player["graveyard"]), 1)
        self.assertIn(0, game_state.prepared_cards)
        self.assertIsNone(game_state.choice_context)

    def test_decline_is_atomic_and_leaving_removes_virtual_copy(self):
        game_state, handler, player, _ = self._state()
        self._enter(game_state, player)
        game_state.prepared_cards.discard(0)
        player["graveyard"] = list(range(6, 14))
        before = list(player["graveyard"])
        for card_id in before:
            game_state._last_card_locations[card_id] = (player, "graveyard")

        game_state.ability_handler.handle_attack_triggers(0)
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.resolve_top_of_stack())
        self._apply(handler, 353)
        self._apply(handler, 11)
        self.assertEqual(player["graveyard"], before)
        self.assertFalse(player["exile"])
        self.assertNotIn(0, game_state.prepared_cards)

        game_state.prepared_cards.add(0)
        self.assertTrue(game_state.move_card(
            0, player, "battlefield", player, "hand", cause="bounce"))
        self.assertNotIn(0, game_state.prepared_cards)


if __name__ == "__main__":
    unittest.main()
