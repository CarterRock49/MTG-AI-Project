"""Regressions for optional self-discard follow-up transactions.

Run from the repository root with::

    python tests/optional_discard_test.py
"""

from __future__ import annotations

import logging
import sys
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from Playersim.ability_types import OptionalDiscardThenEffect  # noqa: E402
from Playersim.ability_utils import EffectFactory  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
    replace_hand,
)


logging.disable(logging.CRITICAL)


class OptionalDiscardThenEffectTest(unittest.TestCase):
    TEXT = "You may discard a card. If you do, draw two cards."

    def _cast_and_resolve(self, option_count):
        game_state = fresh(9300 + option_count)
        handler = get_env().action_handler
        player = game_state.p1
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        game_state.stack.clear()
        replace_hand(game_state, player, [])
        abandon_id = inject_real_card(
            game_state, player, "Abandon Attachments", "hand")
        option_ids = [
            inject_into_zone(game_state, player, {
                "name": f"Optional Discard Probe {index}",
                "mana_cost": "{0}",
                "type_line": "Instant",
                "oracle_text": "",
            }, "hand")
            for index in range(option_count)
        ]
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 2, "G": 0, "C": 0,
        }

        valid = handler.generate_valid_actions()
        self.assertTrue(valid[20], "Abandon Attachments was not castable")
        handler.current_valid_actions = valid
        _, _, _, cast_info = handler.apply_action(20)
        self.assertFalse(cast_info.get("execution_failed", False), cast_info)
        self.assertTrue(game_state.stack)
        self.assertTrue(game_state.resolve_top_of_stack())
        return game_state, handler, player, abandon_id, option_ids

    def test_parser_preserves_optional_discard_and_followup_as_one_effect(self):
        effects = EffectFactory.create_effects(
            self.TEXT, source_name="Abandon Attachments")

        self.assertEqual(len(effects), 1)
        self.assertIsInstance(effects[0], OptionalDiscardThenEffect)
        self.assertEqual(effects[0].followup_text, "draw two cards.")
        self.assertFalse(effects[0].requires_target)

    def test_accept_discards_selected_card_then_draws_two(self):
        (game_state, handler, player,
         abandon_id, option_ids) = self._cast_and_resolve(3)
        self.assertEqual(game_state.choice_context.get("choice_kind"),
                         "optional_discard_then")
        chosen = option_ids[1]
        hand_before = len(player["hand"])
        library_before = len(player["library"])

        valid = handler.generate_valid_actions()
        self.assertTrue(valid[354])
        handler.current_valid_actions = valid
        _, _, _, info = handler.apply_action(354)

        self.assertFalse(info.get("execution_failed", False), info)
        self.assertIsNone(game_state.choice_context)
        self.assertIn(chosen, player["graveyard"])
        self.assertNotIn(chosen, player["hand"])
        self.assertEqual(len(player["library"]), library_before - 2)
        self.assertEqual(len(player["hand"]), hand_before + 1)
        self.assertIn(abandon_id, player["graveyard"])

    def test_decline_discards_nothing_and_draws_nothing(self):
        (game_state, handler, player,
         abandon_id, option_ids) = self._cast_and_resolve(3)
        hand_before = list(player["hand"])
        library_before = list(player["library"])

        valid = handler.generate_valid_actions()
        self.assertTrue(valid[11], "optional discard did not expose decline")
        handler.current_valid_actions = valid
        _, _, _, info = handler.apply_action(11)

        self.assertFalse(info.get("execution_failed", False), info)
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(player["hand"], hand_before)
        self.assertEqual(player["library"], library_before)
        self.assertTrue(all(card_id not in player["graveyard"]
                            for card_id in option_ids))
        self.assertIn(abandon_id, player["graveyard"])

    def test_empty_hand_skips_choice_and_followup(self):
        (game_state, _, player,
         abandon_id, option_ids) = self._cast_and_resolve(0)

        self.assertEqual(option_ids, [])
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(player["hand"], [])
        self.assertIn(abandon_id, player["graveyard"])

    def test_discard_selection_paginates_past_ten_cards(self):
        (game_state, handler, player,
         abandon_id, option_ids) = self._cast_and_resolve(12)
        chosen = option_ids[10]

        first_page = handler.generate_valid_actions()
        self.assertTrue(first_page[479])
        handler.current_valid_actions = first_page
        _, _, _, page_info = handler.apply_action(479)
        self.assertFalse(page_info.get("execution_failed", False), page_info)
        self.assertEqual(game_state.choice_context.get("choice_page"), 1)

        second_page = handler.generate_valid_actions()
        self.assertTrue(second_page[353])
        handler.current_valid_actions = second_page
        _, _, _, choose_info = handler.apply_action(353)

        self.assertFalse(
            choose_info.get("execution_failed", False), choose_info)
        self.assertIn(chosen, player["graveyard"])
        self.assertNotIn(chosen, player["hand"])
        self.assertIn(abandon_id, player["graveyard"])

    def test_runtime_lock_is_sanitized_for_accept_and_decline(self):
        for accept in (True, False):
            with self.subTest(accept=accept):
                game_state = fresh(9500 + int(accept))
                handler = get_env().action_handler
                player = game_state.p1
                game_state.agent_is_p1 = True
                replace_hand(game_state, player, [])
                option_id = inject_into_zone(game_state, player, {
                    "name": f"Lock Context Probe {accept}",
                    "mana_cost": "{0}",
                    "type_line": "Instant",
                    "oracle_text": "",
                }, "hand")
                source_id = inject_real_card(
                    game_state, player, "Abandon Attachments", "graveyard")
                effect = EffectFactory.create_effects(
                    self.TEXT, source_name="Abandon Attachments")[0]
                effect.resolution_context = {
                    "serializable_marker": {"value": 7},
                    "runtime_lock": threading.RLock(),
                    "card": game_state._safe_get_card(source_id),
                    "game_state": game_state,
                }
                library_before = len(player["library"])
                hand_before = list(player["hand"])

                self.assertTrue(effect._apply_effect(
                    game_state, source_id, player, {}))
                saved_context = game_state.choice_context[
                    "resolution_context"]
                self.assertEqual(
                    saved_context["serializable_marker"], {"value": 7})
                self.assertNotIn("runtime_lock", saved_context)
                self.assertNotIn("card", saved_context)
                self.assertNotIn("game_state", saved_context)

                valid = handler.generate_valid_actions()
                selected_action = 353 if accept else 11
                self.assertTrue(valid[selected_action])
                handler.current_valid_actions = valid
                _, _, _, info = handler.apply_action(selected_action)

                self.assertFalse(
                    info.get("execution_failed", False), info)
                self.assertIsNone(game_state.choice_context)
                if accept:
                    self.assertIn(option_id, player["graveyard"])
                    self.assertEqual(
                        len(player["library"]), library_before - 2)
                else:
                    self.assertEqual(player["hand"], hand_before)
                    self.assertEqual(
                        len(player["library"]), library_before)


if __name__ == "__main__":
    unittest.main()
