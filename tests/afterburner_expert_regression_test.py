"""Public-action regressions for Afterburner's graveyard exhaust trigger."""

from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_ROOT = os.path.dirname(__file__)
for path in (REPO_ROOT, TESTS_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from scenario_test import fresh, get_env, inject_into_zone, inject_real_card


logging.disable(logging.CRITICAL)


class AfterburnerExpertRegressionTest(unittest.TestCase):
    def _prepare_main_phase(self, seed: int):
        game_state = fresh(seed=seed)
        handler = get_env().action_handler
        controller = game_state.p1

        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.ability_handler.active_triggers = []
        handler.current_valid_actions = None

        return game_state, handler, controller

    def _apply_public_action(self, handler, action: int, label: str):
        mask = handler.generate_valid_actions()
        self.assertTrue(
            mask[action],
            f"{label}: action {action} was not legal; {handler.get_action_info(action)}",
        )
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(action)
        self.assertFalse(info.get("execution_failed"), f"{label}: {info}")
        self.assertFalse(info.get("critical_error"), f"{label}: {info}")
        self.assertFalse(info.get("invalid_action"), f"{label}: {info}")
        return info

    def _tap_for_mana(self, game_state, handler, lands):
        for land in lands:
            battlefield_index = game_state.p1["battlefield"].index(land)
            self._apply_public_action(
                handler,
                68 + battlefield_index,
                f"tap {game_state._safe_get_card(land).name} for mana",
            )

    def _activate_first_ability(self, game_state, handler, card):
        battlefield_index = game_state.p1["battlefield"].index(card)
        return self._apply_public_action(
            handler,
            100 + battlefield_index * 3,
            f"activate {game_state._safe_get_card(card).name}",
        )

    def _pass_priority(self, game_state, handler, label: str):
        game_state.agent_is_p1 = game_state.priority_player is game_state.p1
        self._apply_public_action(handler, 11, label)

    def _resolve_top_stack_object(self, game_state, handler, label: str):
        self._pass_priority(game_state, handler, f"{label}: first priority pass")
        self._pass_priority(game_state, handler, f"{label}: second priority pass")

    def _valid_target_actions(self, handler):
        mask = handler.generate_valid_actions()
        return [
            action
            for action, allowed in enumerate(mask)
            if allowed and handler.get_action_info(action)[0] == "SELECT_TARGET"
        ]

    def test_graveyard_afterburner_returns_above_pacesetter_exhaust(self):
        game_state, handler, controller = self._prepare_main_phase(seed=1701)

        afterburner = inject_real_card(game_state, controller, "Afterburner Expert", "graveyard")
        decoy = inject_into_zone(
            game_state,
            controller,
            {
                "name": "Graveyard Decoy",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Creature - Human",
                "oracle_text": "",
                "keywords": [],
                "color_identity": [],
                "power": "1",
                "toughness": "1",
            },
            "graveyard",
        )
        pacesetter = inject_real_card(game_state, controller, "Pacesetter Paragon", "battlefield")
        mountains = [
            inject_real_card(game_state, controller, "Mountain", "battlefield")
            for _ in range(3)
        ]

        # Cards created directly in a graveyard need their static card abilities
        # registered, just as cards loaded at game initialization are registered.
        game_state.ability_handler.register_card_abilities(afterburner, controller)

        with patch("Playersim.ability_types.logging.warning") as warning_mock:
            self._tap_for_mana(game_state, handler, mountains)
            self._activate_first_ability(game_state, handler, pacesetter)

            self.assertEqual(len(game_state.stack), 2)
            exhaust_object, afterburner_trigger = game_state.stack
            self.assertEqual(exhaust_object[0], "ABILITY")
            self.assertEqual(exhaust_object[1], pacesetter)
            self.assertEqual(afterburner_trigger[0], "TRIGGER")
            self.assertEqual(afterburner_trigger[1], afterburner)

            trigger_ability = afterburner_trigger[3]["ability"]
            self.assertFalse(trigger_ability.requires_target)
            self.assertIsNone(game_state.targeting_context)
            self.assertEqual(self._valid_target_actions(handler), [])

            self._resolve_top_stack_object(game_state, handler, "resolve Afterburner trigger")

            self.assertIn(afterburner, controller["battlefield"])
            self.assertNotIn(afterburner, controller["graveyard"])
            self.assertIn(decoy, controller["graveyard"])
            self.assertEqual(len(game_state.stack), 1)
            self.assertEqual(game_state.stack[-1][1], pacesetter)
            pacesetter_card = game_state._safe_get_card(pacesetter)
            self.assertEqual(pacesetter_card.counters.get("+1/+1", 0), 0)

            self._resolve_top_stack_object(game_state, handler, "resolve Pacesetter exhaust")

            self.assertEqual(game_state.stack, [])
            self.assertEqual(pacesetter_card.counters.get("+1/+1", 0), 1)
            self.assertEqual(warning_mock.call_args_list, [])

    def test_battlefield_afterburner_does_not_trigger_its_own_exhaust(self):
        game_state, handler, controller = self._prepare_main_phase(seed=1702)

        afterburner = inject_real_card(game_state, controller, "Afterburner Expert", "battlefield")
        forests = [
            inject_real_card(game_state, controller, "Forest", "battlefield")
            for _ in range(4)
        ]

        with patch("Playersim.ability_types.logging.warning") as warning_mock:
            self._tap_for_mana(game_state, handler, forests)
            self._activate_first_ability(game_state, handler, afterburner)

            self.assertEqual(len(game_state.stack), 1)
            exhaust_object = game_state.stack[-1]
            self.assertEqual(exhaust_object[0], "ABILITY")
            self.assertEqual(exhaust_object[1], afterburner)
            self.assertIsNone(game_state.targeting_context)
            self.assertEqual(self._valid_target_actions(handler), [])

            self._resolve_top_stack_object(game_state, handler, "resolve Afterburner exhaust")

            self.assertEqual(game_state.stack, [])
            self.assertIn(afterburner, controller["battlefield"])
            self.assertNotIn(afterburner, controller["graveyard"])
            afterburner_card = game_state._safe_get_card(afterburner)
            self.assertEqual(afterburner_card.counters.get("+1/+1", 0), 2)
            self.assertEqual(warning_mock.call_args_list, [])


if __name__ == "__main__":
    unittest.main()
