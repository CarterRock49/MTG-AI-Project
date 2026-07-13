"""Regression for action-mask/live-cast parity on Gift alternatives.

Run from the repository root with::

    python tests/gift_target_parity_test.py
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


class GiftTargetParityTest(unittest.TestCase):
    def _state(self):
        flood_maw = Card({
            "name": "Into the Flood Maw",
            "type_line": "Instant",
            "mana_cost": "{U}",
            "cmc": 1,
            "oracle_text": (
                "Gift a tapped Fish (You may promise an opponent a gift as "
                "you cast this spell. If you do, they create a tapped 1/1 "
                "blue Fish creature token before its other effects.)\n"
                "Return target creature an opponent controls to its owner's "
                "hand. If the gift was promised, instead return target "
                "nonland permanent an opponent controls to its owner's hand."
            ),
            "color_identity": ["U"],
        })
        creature = Card({
            "name": "Only Flood Maw Target",
            "type_line": "Creature - Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["G"],
        })
        game_state = GameState({0: flood_maw, 1: creature})
        game_state.reset([1], [0], seed=461)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        caster = game_state.p2
        opponent = game_state.p1
        caster["hand"] = [0]
        caster["library"] = []
        caster["battlefield"] = []
        caster["graveyard"] = []
        caster["exile"] = []
        caster["mana_pool"] = {
            "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 0,
        }
        opponent["hand"] = []
        opponent["library"] = []
        opponent["battlefield"] = [1]
        opponent["graveyard"] = []
        opponent["exile"] = []
        game_state._last_card_locations[0] = (caster, "hand")
        game_state._last_card_locations[1] = (opponent, "battlefield")
        game_state.phase = game_state.PHASE_UPKEEP
        game_state.previous_priority_phase = None
        game_state.stack = []
        game_state.agent_is_p1 = False
        game_state.priority_player = caster
        game_state.priority_pass_count = 0
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler, caster

    def test_mask_valid_gift_spell_starts_one_target_cast(self):
        game_state, handler, caster = self._state()
        flood_maw = game_state._safe_get_card(0)

        self.assertEqual(
            game_state._ordinary_target_slots(flood_maw.oracle_text), [],
            "the conditional Gift branch became a simultaneous target slot")
        active_text = game_state._ordinary_single_targeting_text(
            flood_maw.oracle_text)
        self.assertEqual(
            active_text,
            "Return target creature an opponent controls to its owner's hand.")
        self.assertEqual(game_state._target_bounds_from_text(active_text), (1, 1))

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[20], "the exact canary action must be mask-valid")
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(20)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        self.assertEqual(game_state.targeting_context["required_count"], 1)
        self.assertEqual(game_state.targeting_context["min_targets"], 1)
        self.assertEqual(game_state.targeting_context["effect_text"], active_text)

        candidates = handler._get_target_selection_candidates(
            caster, game_state.targeting_context)
        self.assertEqual(candidates, [1])
        _, success = handler._handle_select_target(0, {})
        self.assertTrue(success)
        self.assertIsNone(game_state.targeting_context)
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][1], 0)
        self.assertEqual(
            game_state.stack[-1][3].get("targeting_text"), active_text)


if __name__ == "__main__":
    unittest.main()
