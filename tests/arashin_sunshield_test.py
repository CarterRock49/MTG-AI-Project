"""Real-card regression for Arashin Sunshield's graveyard-exile ETB.

Run from the repository root with::

    python tests/arashin_sunshield_test.py
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
for path in (REPO_ROOT, TESTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


logging.disable(logging.CRITICAL)


def graveyard_card(name):
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": "Sorcery",
        "oracle_text": "",
        "keywords": [],
        "color_identity": [],
    }


class ArashinSunshieldTest(unittest.TestCase):
    def _target_actions_named(self, handler, mask, target_name):
        return [
            action
            for action in range(274, 284)
            if mask[action]
            and target_name in handler.action_reasons_with_context.get(
                action, {}).get("reason", "")
        ]

    def test_real_etb_exiles_two_selected_cards_from_one_graveyard(self):
        game_state = fresh(1175)
        handler = get_env().action_handler
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.ability_handler.active_triggers = []
        controller["graveyard"] = []
        opponent["graveyard"] = []

        targets = [
            inject_into_zone(
                game_state, opponent, graveyard_card(f"Sunshield Target {index}"),
                "graveyard")
            for index in range(2)
        ]
        other_graveyard_target = inject_into_zone(
            game_state, controller, graveyard_card("Other Graveyard Card"),
            "graveyard")
        sunshield = inject_real_card(
            game_state, controller, "Arashin Sunshield", "hand")

        self.assertTrue(game_state.move_card(
            sunshield, controller, "hand", controller, "battlefield",
            cause="spell_resolution", context={"was_cast": True}))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1)
        self.assertIn(
            "exile up to two target cards from a single graveyard",
            queued[0][0].effect_text.lower())

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        self.assertEqual(
            (game_state.targeting_context["min_targets"],
             game_state.targeting_context["max_targets"]),
            (0, 2))

        first_mask = handler.generate_valid_actions()
        first_actions = self._target_actions_named(
            handler, first_mask, "Sunshield Target 0")
        self.assertEqual(
            len(first_actions), 1, handler.action_reasons_with_context)
        handler.current_valid_actions = first_mask
        _, _, _, info = handler.apply_action(first_actions[0])
        self.assertFalse(info.get("execution_failed", False), info)

        second_mask = handler.generate_valid_actions()
        self.assertEqual(
            self._target_actions_named(
                handler, second_mask, "Other Graveyard Card"),
            [],
            "choosing from one graveyard left the other graveyard selectable")
        second_actions = self._target_actions_named(
            handler, second_mask, "Sunshield Target 1")
        self.assertEqual(len(second_actions), 1)
        handler.current_valid_actions = second_mask
        _, _, _, info = handler.apply_action(second_actions[0])
        self.assertFalse(info.get("execution_failed", False), info)

        self.assertIsNone(game_state.targeting_context)
        for acting_player in (controller, opponent):
            game_state.agent_is_p1 = acting_player is game_state.p1
            priority_mask = handler.generate_valid_actions()
            self.assertTrue(priority_mask[11])
            handler.current_valid_actions = priority_mask
            _, _, _, info = handler.apply_action(11)
            self.assertFalse(info.get("execution_failed", False), info)
        self.assertTrue(all(
            target_id not in opponent["graveyard"] for target_id in targets))
        self.assertTrue(all(
            target_id in opponent["exile"] for target_id in targets))
        self.assertIn(other_graveyard_target, controller["graveyard"])
        self.assertNotIn(other_graveyard_target, controller["exile"])


if __name__ == "__main__":
    unittest.main()
