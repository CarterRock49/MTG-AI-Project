"""Exact graveyard Eerie regressions for Fear of Infinity."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)
from Playersim.ability_types import (  # noqa: E402
    ReturnSourceFromGraveyardEffect,
    TriggeredAbility,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402


class FearOfInfinityRegressionTest(unittest.TestCase):
    FEAR = "Fear of Infinity"
    ROOM = "Dazzling Theater // Prop Room"

    def _state(self, seed: int, *, turn: int = 1):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = turn
        game_state.agent_is_p1 = (
            game_state._get_active_player() is game_state.p1)
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = game_state._get_active_player()
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.pending_spell_context = None
        game_state.ability_handler.active_triggers = []
        for player in (controller, opponent):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["entered_battlefield_this_turn"] = set()
            player["damage_counters"] = {}
            player["mana_pool"] = {
                color: 0 for color in ("W", "U", "B", "R", "G", "C")
            }
        game_state._last_card_locations = {}
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    @staticmethod
    def _registered_triggers(game_state, source_id):
        return [
            ability for ability in game_state.ability_handler.
            registered_abilities.get(source_id, [])
            if isinstance(ability, TriggeredAbility)
        ]

    @staticmethod
    def _queued_triggers(game_state, source_id):
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
        ]

    def _fear_in_graveyard(self, game_state, controller):
        source_id = inject_real_card(
            game_state, controller, self.FEAR, "graveyard")
        game_state.ability_handler.register_card_abilities(
            source_id, controller)
        game_state.ability_handler.active_triggers = []
        return source_id

    @staticmethod
    def _enchantment(game_state, player, name):
        return inject_into_zone(game_state, player, {
            "name": name,
            "mana_cost": "{1}{U}",
            "type_line": "Enchantment",
            "oracle_text": "",
        }, "battlefield")

    def _unlock_second_door(self, game_state, player, room_id):
        player["mana_pool"]["W"] = 3
        self.assertTrue(game_state.ability_handler.handle_unlock_door(
            player["battlefield"].index(room_id),
            controller=player,
            room_id=room_id,
            door_number=2,
        ))

    def _public(self, handler, action: int, message: str):
        game_state = handler.game_state
        priority = game_state.priority_player or game_state.p1
        game_state.agent_is_p1 = priority is game_state.p1
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[action], (message, [
            index for index, allowed in enumerate(mask) if allowed]))
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(action)
        self.assertFalse(done, message)
        self.assertFalse(truncated, message)
        self.assertFalse(info.get("execution_failed"), (message, info))

    def _open_eerie_choice(self, seed: int, arm: str):
        game_state, handler, controller, opponent = self._state(seed)
        room_id = None
        if arm == "room":
            room_id = inject_real_card(
                game_state, controller, self.ROOM, "battlefield")
            room = game_state._safe_get_card(room_id)
            room.door1["unlocked"] = True
            room.door2["unlocked"] = False
            game_state.ability_handler.active_triggers = []

        source_id = self._fear_in_graveyard(game_state, controller)
        decoy_id = inject_real_card(
            game_state, controller, "Swamp", "graveyard")
        if arm == "enchantment":
            self._enchantment(
                game_state, controller, "Fear Eerie Enchantment")
            expected_event = "ENTERS_BATTLEFIELD"
        elif arm == "room":
            self._unlock_second_door(
                game_state, controller, room_id)
            expected_event = "ROOM_FULLY_UNLOCKED"
        else:
            self.fail(f"unknown Eerie arm: {arm}")

        queued = self._queued_triggers(game_state, source_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_type"], expected_event)
        self.assertEqual(queued[0][2]["source_zone"], "graveyard")
        self.assertEqual(
            queued[0][2]["source_zone_generation"],
            game_state._safe_get_card(source_id)._zone_change_generation,
        )
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0:2], ("TRIGGER", source_id))
        self.assertFalse(game_state.stack[-1][3]["ability"].requires_target)
        self.assertIsNone(game_state.targeting_context)

        self._public(handler, 11, f"{arm} Eerie controller passes")
        self._public(handler, 11, f"{arm} Eerie opponent passes")

        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)
        choice = game_state.choice_context
        self.assertIsNotNone(choice)
        self.assertEqual(choice["type"], "dig_select")
        self.assertIs(choice["player"], controller)
        self.assertEqual(choice["options"], [source_id])
        self.assertEqual(choice["source_zone"], "graveyard")
        self.assertEqual(choice["destination"], "hand")
        self.assertEqual(choice["rest_destination"], "stay")
        self.assertTrue(choice["optional"])
        self.assertEqual(
            choice["source_zone_generation"],
            game_state._safe_get_card(source_id)._zone_change_generation,
        )
        self.assertIn(decoy_id, controller["graveyard"])
        return (
            game_state, handler, controller, opponent, source_id, decoy_id)

    def test_parser_builds_optional_nontargeted_hand_return_in_graveyard(self):
        effects = EffectFactory.create_effects(
            "You may return this card from your graveyard to your hand.")
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertIsInstance(effect, ReturnSourceFromGraveyardEffect)
        self.assertEqual(effect.destination, "hand")
        self.assertTrue(effect.optional)
        self.assertFalse(effect.requires_target)

        game_state, _, controller, _ = self._state(44401)
        source_id = self._fear_in_graveyard(game_state, controller)
        triggers = self._registered_triggers(game_state, source_id)
        self.assertEqual(len(triggers), 1)
        eerie = triggers[0]
        self.assertEqual(eerie.zone, "graveyard")
        self.assertFalse(eerie.requires_target)
        self.assertIn(
            "return this card from your graveyard to your hand",
            eerie.effect.lower(),
        )

    def test_both_eerie_arms_may_return_only_the_trigger_source(self):
        for offset, arm in enumerate(("enchantment", "room")):
            with self.subTest(arm=arm):
                with self.assertNoLogs(level=logging.WARNING):
                    (game_state, handler, controller, _, source_id,
                     decoy_id) = self._open_eerie_choice(44410 + offset, arm)
                    self._public(handler, 353, f"accept {arm} Eerie")
                self.assertIn(source_id, controller["hand"])
                self.assertNotIn(source_id, controller["graveyard"])
                self.assertIn(decoy_id, controller["graveyard"])
                self.assertIsNone(game_state.choice_context)
                self.assertIsNone(game_state.targeting_context)
                self.assertEqual(game_state.stack, [])

    def test_both_eerie_arms_may_be_declined(self):
        for offset, arm in enumerate(("enchantment", "room")):
            with self.subTest(arm=arm):
                with self.assertNoLogs(level=logging.WARNING):
                    (game_state, handler, controller, _, source_id,
                     decoy_id) = self._open_eerie_choice(44420 + offset, arm)
                    self._public(handler, 11, f"decline {arm} Eerie")
                self.assertIn(source_id, controller["graveyard"])
                self.assertIn(decoy_id, controller["graveyard"])
                self.assertNotIn(source_id, controller["hand"])
                self.assertIsNone(game_state.choice_context)
                self.assertIsNone(game_state.targeting_context)
                self.assertEqual(game_state.stack, [])

    def test_opponent_eerie_events_do_not_trigger(self):
        game_state, _, controller, opponent = self._state(44431, turn=2)
        source_id = self._fear_in_graveyard(game_state, controller)

        self._enchantment(
            game_state, opponent, "Opponent Eerie Enchantment")
        self.assertEqual(self._queued_triggers(game_state, source_id), [])

        room_id = inject_real_card(
            game_state, opponent, self.ROOM, "battlefield")
        self.assertEqual(self._queued_triggers(game_state, source_id), [])
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False
        game_state.ability_handler.active_triggers = []
        self._unlock_second_door(game_state, opponent, room_id)
        self.assertEqual(self._queued_triggers(game_state, source_id), [])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)
        self.assertIsNone(game_state.choice_context)

    def test_eerie_is_inactive_outside_the_graveyard(self):
        for offset, zone in enumerate(("battlefield", "hand")):
            with self.subTest(zone=zone):
                game_state, _, controller, _ = self._state(44440 + offset)
                source_id = inject_real_card(
                    game_state, controller, self.FEAR, zone)
                if zone != "battlefield":
                    game_state.ability_handler.register_card_abilities(
                        source_id, controller)
                game_state.ability_handler.active_triggers = []
                self._enchantment(
                    game_state, controller,
                    f"Wrong-zone Eerie Enchantment {zone}")
                self.assertEqual(
                    self._queued_triggers(game_state, source_id), [])
                self.assertEqual(game_state.stack, [])
                self.assertIsNone(game_state.targeting_context)
                self.assertIsNone(game_state.choice_context)

    def test_old_trigger_cannot_follow_a_new_graveyard_object(self):
        game_state, _, controller, _ = self._state(44451)
        source_id = self._fear_in_graveyard(game_state, controller)
        self._enchantment(
            game_state, controller, "Generation-bound Eerie Enchantment")
        queued = self._queued_triggers(game_state, source_id)
        self.assertEqual(len(queued), 1)
        queued_generation = queued[0][2]["source_zone_generation"]
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)

        self.assertTrue(game_state.move_card(
            source_id, controller, "graveyard", controller, "hand",
            cause="leave_before_eerie_resolution"))
        self.assertTrue(game_state.move_card(
            source_id, controller, "hand", controller, "graveyard",
            cause="return_before_eerie_resolution"))
        self.assertNotEqual(
            queued_generation,
            game_state._safe_get_card(source_id)._zone_change_generation,
        )

        with self.assertNoLogs(level=logging.WARNING):
            self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(source_id, controller["graveyard"])
        self.assertNotIn(source_id, controller["hand"])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)
        self.assertIsNone(game_state.choice_context)

    def test_open_choice_cannot_follow_a_new_graveyard_object(self):
        with self.assertNoLogs(level=logging.WARNING):
            (game_state, handler, controller, _, source_id,
             decoy_id) = self._open_eerie_choice(44452, "enchantment")
        opened_generation = game_state.choice_context[
            "source_zone_generation"]

        self.assertTrue(game_state.move_card(
            source_id, controller, "graveyard", controller, "hand",
            cause="leave_during_eerie_choice"))
        self.assertTrue(game_state.move_card(
            source_id, controller, "hand", controller, "graveyard",
            cause="return_during_eerie_choice"))
        self.assertNotEqual(
            opened_generation,
            game_state._safe_get_card(source_id)._zone_change_generation,
        )

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 353, "accept stale Eerie choice")
        self.assertIn(source_id, controller["graveyard"])
        self.assertNotIn(source_id, controller["hand"])
        self.assertIn(decoy_id, controller["graveyard"])
        self.assertIsNone(game_state.choice_context)
        self.assertIsNone(game_state.targeting_context)
        self.assertEqual(game_state.stack, [])


if __name__ == "__main__":
    unittest.main()
