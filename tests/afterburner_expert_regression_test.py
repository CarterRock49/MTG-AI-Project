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
        game_state.exhaust_ability_used = {}
        handler.current_valid_actions = None

        for player in (game_state.p1, game_state.p2):
            player["mana_pool"] = {
                color: 0 for color in ("W", "U", "B", "R", "G", "C")
            }

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

    def _tap_for_mana(self, game_state, handler, lands, player=None):
        player = player or game_state.p1
        for land in lands:
            battlefield_index = player["battlefield"].index(land)
            self._apply_public_action(
                handler,
                68 + battlefield_index,
                f"tap {game_state._safe_get_card(land).name} for mana",
            )

    def _first_ability_action(self, game_state, card, player=None):
        player = player or game_state.p1
        battlefield_index = player["battlefield"].index(card)
        return 100 + battlefield_index * 3

    def _activate_first_ability(self, game_state, handler, card, player=None):
        action = self._first_ability_action(game_state, card, player)
        return self._apply_public_action(
            handler,
            action,
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

    def _assert_only_zone(self, game_state, card_id, expected_player, expected_zone):
        occurrences = self._physical_occurrences(game_state, card_id)
        self.assertEqual(
            len(occurrences), 1,
            f"card {card_id} had physical occurrences {occurrences}",
        )
        self.assertIs(occurrences[0][0], expected_player)
        self.assertEqual(occurrences[0][1], expected_zone)

    @staticmethod
    def _physical_occurrences(game_state, card_id):
        occurrences = []
        for player in (game_state.p1, game_state.p2):
            for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
                occurrences.extend(
                    (player, zone) for _ in range(player[zone].count(card_id))
                )
        return occurrences

    def _assert_zero_mana(self, player):
        self.assertEqual(sum(player["mana_pool"].values()), 0)

    def test_real_card_public_cast_payment_and_priority_negative_mask(self):
        game_state, handler, controller = self._prepare_main_phase(seed=1700)
        opponent = game_state.p2
        for card_id in list(controller["hand"]):
            self.assertTrue(game_state.move_card(
                card_id, controller, "hand", controller, "library"))

        afterburner = inject_real_card(
            game_state, controller, "Afterburner Expert", "hand")
        forests = [
            inject_real_card(game_state, controller, "Forest", "battlefield")
            for _ in range(3)
        ]
        cast_action = 20 + controller["hand"].index(afterburner)
        fidelity_before = dict(game_state.fidelity_counters)

        with patch("Playersim.ability_types.logging.warning") as warning_mock:
            game_state.priority_player = opponent
            handler.current_valid_actions = None
            negative_mask = handler.generate_valid_actions()
            self.assertFalse(
                negative_mask[cast_action],
                "Afterburner was castable while its controller lacked priority",
            )
            self._assert_only_zone(
                game_state, afterburner, controller, "hand")
            self.assertEqual(game_state.stack, [])
            self.assertFalse(any(
                land in controller["tapped_permanents"] for land in forests))

            game_state.priority_player = controller
            game_state.agent_is_p1 = True
            handler.current_valid_actions = None
            self._apply_public_action(
                handler, cast_action, "cast real Afterburner Expert")

            self.assertEqual(
                self._physical_occurrences(game_state, afterburner), [],
                "a spell on the stack remained in a physical zone",
            )
            self.assertEqual(len(game_state.stack), 1)
            spell = game_state.stack[-1]
            self.assertEqual(spell[0:2], ("SPELL", afterburner))
            self.assertTrue(spell[3].get("was_cast"))
            self.assertTrue(all(
                land in controller["tapped_permanents"] for land in forests))
            self._assert_zero_mana(controller)

            self._resolve_top_stack_object(
                game_state, handler, "resolve Afterburner creature spell")

            self._assert_only_zone(
                game_state, afterburner, controller, "battlefield")
            self.assertEqual(game_state.stack, [])
            self.assertEqual(game_state.ability_handler.active_triggers, [])
            self.assertEqual(game_state.exhaust_ability_used, {})
            card = game_state._safe_get_card(afterburner)
            self.assertEqual(card.counters.get("+1/+1", 0), 0)
            self.assertEqual(warning_mock.call_args_list, [])
            self.assertEqual(game_state.fidelity_counters, fidelity_before)

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
        fidelity_before = dict(game_state.fidelity_counters)
        queued_batches = []
        process_triggers = game_state.ability_handler.process_triggered_abilities

        def capture_trigger_queue():
            queued_batches.append(list(
                game_state.ability_handler.active_triggers))
            return process_triggers()

        with patch("Playersim.ability_types.logging.warning") as warning_mock, \
                patch.object(
                    game_state.ability_handler,
                    "process_triggered_abilities",
                    side_effect=capture_trigger_queue):
            self._tap_for_mana(game_state, handler, mountains)
            self._activate_first_ability(game_state, handler, pacesetter)

            nonempty_batches = [batch for batch in queued_batches if batch]
            self.assertEqual(len(nonempty_batches), 1)
            self.assertEqual(len(nonempty_batches[0]), 1)
            queued_ability, queued_controller, queued_context = \
                nonempty_batches[0][0]
            self.assertEqual(queued_ability.card_id, afterburner)
            self.assertIs(queued_controller, controller)
            self.assertEqual(
                queued_context.get("event_type"),
                "EXHAUST_ABILITY_ACTIVATED")
            self.assertEqual(queued_context.get("event_card_id"), pacesetter)
            self.assertIs(queued_context.get("activator"), controller)
            self.assertEqual(queued_context.get("original_zone"), "graveyard")
            self.assertEqual(game_state.ability_handler.active_triggers, [])
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
            self.assertTrue(all(
                land in controller["tapped_permanents"] for land in mountains))
            self._assert_zero_mana(controller)
            self.assertEqual(
                set(game_state.exhaust_ability_used), {(pacesetter, 0)})

            self._resolve_top_stack_object(game_state, handler, "resolve Afterburner trigger")

            self._assert_only_zone(
                game_state, afterburner, controller, "battlefield")
            self.assertIn(decoy, controller["graveyard"])
            self.assertEqual(len(game_state.stack), 1)
            self.assertEqual(game_state.stack[-1][1], pacesetter)
            pacesetter_card = game_state._safe_get_card(pacesetter)
            self.assertEqual(pacesetter_card.counters.get("+1/+1", 0), 0)

            self._resolve_top_stack_object(game_state, handler, "resolve Pacesetter exhaust")

            self.assertEqual(game_state.stack, [])
            self.assertEqual(pacesetter_card.counters.get("+1/+1", 0), 1)
            self.assertEqual(warning_mock.call_args_list, [])
            self.assertEqual(game_state.fidelity_counters, fidelity_before)

    def test_battlefield_afterburner_does_not_trigger_its_own_exhaust(self):
        game_state, handler, controller = self._prepare_main_phase(seed=1702)

        afterburner = inject_real_card(game_state, controller, "Afterburner Expert", "battlefield")
        exhaust_action = self._first_ability_action(game_state, afterburner)
        unaffordable_mask = handler.generate_valid_actions()
        self.assertFalse(
            unaffordable_mask[exhaust_action],
            "Afterburner's unaffordable Exhaust ability was mask-valid",
        )
        self.assertEqual(game_state.stack, [])
        self.assertEqual(game_state.exhaust_ability_used, {})

        forests = [
            inject_real_card(game_state, controller, "Forest", "battlefield")
            for _ in range(4)
        ]
        fidelity_before = dict(game_state.fidelity_counters)
        queued_batches = []
        process_triggers = game_state.ability_handler.process_triggered_abilities

        def capture_trigger_queue():
            queued_batches.append(list(
                game_state.ability_handler.active_triggers))
            return process_triggers()

        with patch("Playersim.ability_types.logging.warning") as warning_mock, \
                patch.object(
                    game_state.ability_handler,
                    "process_triggered_abilities",
                    side_effect=capture_trigger_queue):
            self._tap_for_mana(game_state, handler, forests)
            self._activate_first_ability(game_state, handler, afterburner)

            self.assertTrue(queued_batches)
            self.assertTrue(all(not batch for batch in queued_batches))
            self.assertEqual(game_state.ability_handler.active_triggers, [])
            self.assertEqual(len(game_state.stack), 1)
            exhaust_object = game_state.stack[-1]
            self.assertEqual(exhaust_object[0], "ABILITY")
            self.assertEqual(exhaust_object[1], afterburner)
            self.assertIsNone(game_state.targeting_context)
            self.assertEqual(self._valid_target_actions(handler), [])
            self.assertTrue(all(
                land in controller["tapped_permanents"] for land in forests))
            self._assert_zero_mana(controller)
            self.assertEqual(
                set(game_state.exhaust_ability_used), {(afterburner, 0)})

            self._resolve_top_stack_object(game_state, handler, "resolve Afterburner exhaust")

            self.assertEqual(game_state.stack, [])
            self.assertIn(afterburner, controller["battlefield"])
            self.assertNotIn(afterburner, controller["graveyard"])
            afterburner_card = game_state._safe_get_card(afterburner)
            self.assertEqual(afterburner_card.counters.get("+1/+1", 0), 2)

            for forest in forests:
                self.assertTrue(game_state.untap_permanent(forest, controller))
            game_state.agent_is_p1 = True
            game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
            game_state.priority_player = controller
            game_state.priority_pass_count = 0
            handler.current_valid_actions = None
            used_mask = handler.generate_valid_actions()
            self.assertFalse(
                used_mask[exhaust_action],
                "Afterburner's used Exhaust ability returned to the action mask",
            )
            self.assertFalse(any(
                forest in controller["tapped_permanents"] for forest in forests))
            self._assert_zero_mana(controller)
            self.assertEqual(warning_mock.call_args_list, [])
            self.assertEqual(game_state.fidelity_counters, fidelity_before)

    def test_graveyard_afterburner_ignores_non_exhaust_activation(self):
        game_state, handler, controller = self._prepare_main_phase(seed=1703)
        afterburner = inject_real_card(
            game_state, controller, "Afterburner Expert", "graveyard")
        game_state.ability_handler.register_card_abilities(
            afterburner, controller)
        utility = inject_into_zone(game_state, controller, {
            "name": "Afterburner Non-Exhaust Control",
            "mana_cost": "{1}",
            "cmc": 1,
            "type_line": "Artifact",
            "oracle_text": "{T}: You gain 1 life.",
            "keywords": [],
            "color_identity": [],
        }, "battlefield")
        game_state.ability_handler.active_triggers = []
        fidelity_before = dict(game_state.fidelity_counters)
        life_before = controller["life"]
        queued_batches = []
        process_triggers = game_state.ability_handler.process_triggered_abilities

        def capture_trigger_queue():
            queued_batches.append(list(
                game_state.ability_handler.active_triggers))
            return process_triggers()

        with patch("Playersim.ability_types.logging.warning") as warning_mock, \
                patch.object(
                    game_state.ability_handler,
                    "process_triggered_abilities",
                    side_effect=capture_trigger_queue):
            self._activate_first_ability(game_state, handler, utility)

            self.assertTrue(queued_batches)
            self.assertTrue(all(not batch for batch in queued_batches))
            self.assertEqual(game_state.ability_handler.active_triggers, [])
            self.assertEqual(len(game_state.stack), 1)
            self.assertEqual(game_state.stack[-1][0:2], ("ABILITY", utility))
            self.assertIn(utility, controller["tapped_permanents"])
            self.assertEqual(controller["life"], life_before)
            self._assert_only_zone(
                game_state, afterburner, controller, "graveyard")

            self._resolve_top_stack_object(
                game_state, handler, "resolve non-Exhaust control ability")

            self.assertEqual(controller["life"], life_before + 1)
            self.assertEqual(game_state.stack, [])
            self._assert_only_zone(
                game_state, afterburner, controller, "graveyard")
            self.assertEqual(game_state.exhaust_ability_used, {})
            self.assertEqual(warning_mock.call_args_list, [])
            self.assertEqual(game_state.fidelity_counters, fidelity_before)

    def test_graveyard_afterburner_ignores_opponent_exhaust_activation(self):
        game_state, handler, controller = self._prepare_main_phase(seed=1704)
        opponent = game_state.p2
        game_state.turn = 2
        game_state.agent_is_p1 = False
        game_state.priority_player = opponent

        afterburner = inject_real_card(
            game_state, controller, "Afterburner Expert", "graveyard")
        game_state.ability_handler.register_card_abilities(
            afterburner, controller)
        pacesetter = inject_real_card(
            game_state, opponent, "Pacesetter Paragon", "battlefield")
        mountains = [
            inject_real_card(game_state, opponent, "Mountain", "battlefield")
            for _ in range(3)
        ]
        fidelity_before = dict(game_state.fidelity_counters)
        queued_batches = []
        process_triggers = game_state.ability_handler.process_triggered_abilities

        def capture_trigger_queue():
            queued_batches.append(list(
                game_state.ability_handler.active_triggers))
            return process_triggers()

        with patch("Playersim.ability_types.logging.warning") as warning_mock, \
                patch.object(
                    game_state.ability_handler,
                    "process_triggered_abilities",
                    side_effect=capture_trigger_queue):
            self._tap_for_mana(
                game_state, handler, mountains, player=opponent)
            self._activate_first_ability(
                game_state, handler, pacesetter, player=opponent)

            self.assertTrue(queued_batches)
            self.assertTrue(all(not batch for batch in queued_batches))
            self.assertEqual(game_state.ability_handler.active_triggers, [])
            self.assertEqual(len(game_state.stack), 1)
            self.assertEqual(
                game_state.stack[-1][0:2], ("ABILITY", pacesetter))
            self.assertTrue(all(
                land in opponent["tapped_permanents"] for land in mountains))
            self._assert_zero_mana(opponent)
            self.assertEqual(
                set(game_state.exhaust_ability_used), {(pacesetter, 0)})
            self._assert_only_zone(
                game_state, afterburner, controller, "graveyard")

            self._resolve_top_stack_object(
                game_state, handler, "resolve opponent Exhaust ability")

            self.assertEqual(game_state.stack, [])
            self.assertEqual(
                game_state._safe_get_card(pacesetter).counters.get(
                    "+1/+1", 0),
                1,
            )
            self._assert_only_zone(
                game_state, afterburner, controller, "graveyard")
            self.assertEqual(warning_mock.call_args_list, [])
            self.assertEqual(game_state.fidelity_counters, fidelity_before)


if __name__ == "__main__":
    unittest.main()
