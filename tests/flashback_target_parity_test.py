"""Regression for action-mask/live-cast parity on targeted Flashback spells.

Run from the repository root with::

    python tests/flashback_target_parity_test.py
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


class FlashbackTargetParityTest(unittest.TestCase):
    def _state(self):
        daydream = Card({
            "name": "Daydream",
            "type_line": "Sorcery",
            "mana_cost": "{W}",
            "cmc": 1,
            "oracle_text": (
                "Exile target creature you control, then return that card "
                "to the battlefield under its owner's control with a +1/+1 "
                "counter on it.\n"
                "Flashback {2}{W} (You may cast this card from your graveyard "
                "for its flashback cost. Then exile it.)"
            ),
            "color_identity": ["W"],
        })
        creature = Card({
            "name": "Daydream Target",
            "type_line": "Creature - Bird",
            "mana_cost": "{1}",
            "cmc": 1,
            "oracle_text": "",
            "power": 1,
            "toughness": 1,
            "color_identity": [],
        })
        game_state = GameState({0: daydream, 1: creature})
        game_state.reset([1], [0], seed=398)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        caster = game_state.p2
        opponent = game_state.p1
        caster["hand"] = []
        caster["library"] = []
        caster["battlefield"] = []
        caster["graveyard"] = [0]
        caster["exile"] = []
        caster["mana_pool"] = {
            "W": 1, "U": 0, "B": 0, "R": 0, "G": 0, "C": 2,
        }
        opponent["hand"] = []
        opponent["library"] = []
        opponent["battlefield"] = []
        opponent["graveyard"] = []
        opponent["exile"] = []
        game_state._last_card_locations[0] = (caster, "graveyard")
        game_state.turn = 2
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.stack = []
        game_state.agent_is_p1 = False
        game_state.priority_player = caster
        game_state.priority_pass_count = 0
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler, caster

    def test_targeted_flashback_requires_a_legal_target(self):
        game_state, handler, caster = self._state()

        mask = handler.generate_valid_actions()
        self.assertFalse(
            mask[398],
            "the shared Flashback action must not advertise an uncastable spell")
        self.assertFalse(
            mask[472],
            "the per-slot Flashback action must not advertise an uncastable spell")

        caster["battlefield"] = [1]
        game_state._last_card_locations[1] = (caster, "battlefield")
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[398])
        self.assertTrue(mask[472])
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(398)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)


if __name__ == "__main__":
    unittest.main()
