"""Public-lifecycle regressions for Gran-Gran's becomes-tapped trigger."""

from __future__ import annotations

import logging
import os
import sys
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_ROOT = os.path.dirname(__file__)
for path in (REPO_ROOT, TESTS_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_card,
    inject_real_card,
    replace_hand,
)


logging.disable(logging.CRITICAL)


def _vanilla_card(name: str) -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": "Sorcery",
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
    }


class GranGranTriggerRegressionTest(unittest.TestCase):
    def _apply_public_action(self, handler, action: int, label: str):
        mask = handler.generate_valid_actions()
        self.assertTrue(
            mask[action],
            f"{label}: action {action} was not legal; "
            f"{handler.get_action_info(action)}",
        )
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(action)
        self.assertFalse(info.get("execution_failed"), f"{label}: {info}")
        self.assertFalse(info.get("critical_error"), f"{label}: {info}")
        self.assertFalse(info.get("invalid_action"), f"{label}: {info}")
        return info

    def _pass_priority(self, game_state, handler, player, label: str):
        game_state.agent_is_p1 = player is game_state.p1
        self._apply_public_action(handler, 11, label)

    def test_attacking_gran_gran_loots_once_and_only_for_the_tapped_copy(self):
        game_state = fresh(seed=197101)
        handler = get_env().action_handler
        controller = game_state.p1
        opponent = game_state.p2

        attacking_gran = inject_real_card(
            game_state, controller, "Gran-Gran", "battlefield")
        watching_gran = inject_real_card(
            game_state, opponent, "Gran-Gran", "battlefield")
        controller["entered_battlefield_this_turn"].discard(attacking_gran)

        discard_candidate = replace_hand(
            game_state, controller, [_vanilla_card("Chosen Discard")])[0]
        drawn_card = inject_card(game_state, _vanilla_card("Known Top Card"))
        controller["library"].insert(0, drawn_card)
        game_state._last_card_locations[drawn_card] = (controller, "library")

        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_DECLARE_ATTACKERS
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack.clear()
        game_state.ability_handler.active_triggers.clear()
        game_state.current_attackers = [attacking_gran]
        game_state.current_block_assignments = {}

        self._apply_public_action(
            handler, 438, "complete attack declaration with Gran-Gran")

        self.assertIn(attacking_gran, controller["tapped_permanents"])
        self.assertNotIn(watching_gran, opponent["tapped_permanents"])
        self.assertEqual(len(game_state.stack), 1)
        trigger = game_state.stack[-1]
        self.assertEqual(trigger[0], "TRIGGER")
        self.assertEqual(trigger[1], attacking_gran)
        self.assertIn(
            "draw a card, then discard a card",
            trigger[3]["ability"].effect,
        )

        self._pass_priority(
            game_state, handler, controller, "controller passes on loot trigger")
        self._pass_priority(
            game_state, handler, opponent, "opponent passes on loot trigger")

        self.assertEqual(game_state.stack, [])
        self.assertIn(drawn_card, controller["hand"])
        self.assertEqual(game_state.phase, game_state.PHASE_CHOOSE)
        self.assertEqual(game_state.choice_context.get("type"), "discard")
        self.assertIs(game_state.choice_context.get("player"), controller)

        discard_index = controller["hand"].index(discard_candidate)
        self.assertLess(discard_index, 10)
        game_state.agent_is_p1 = True
        self._apply_public_action(
            handler, 238 + discard_index, "choose the exact card to discard")

        self.assertIsNone(game_state.choice_context)
        self.assertIn(discard_candidate, controller["graveyard"])
        self.assertNotIn(discard_candidate, controller["hand"])
        self.assertEqual(controller["hand"], [drawn_card])


if __name__ == "__main__":
    unittest.main()
