"""Exact mass-reanimation regressions for Funeral Room // Awakening Hall."""

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
    ReanimateEffect,
    UnsupportedEffect,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402


class FuneralRoomAwakeningHallRegressionTest(unittest.TestCase):
    ROOM = "Funeral Room // Awakening Hall"

    def _state(self, seed: int):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = 1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
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
    def _card(game_state, player, name, type_line, zone):
        creature = "Creature" in type_line
        data = {
            "name": name,
            "mana_cost": "{1}",
            "cmc": 1,
            "type_line": type_line,
            "oracle_text": "",
            "keywords": [],
            "color_identity": [],
        }
        if creature:
            data.update({"power": "1", "toughness": "1"})
        return inject_into_zone(game_state, player, data, zone)

    def _room(self, game_state, controller):
        room_id = inject_real_card(
            game_state, controller, self.ROOM, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False
        game_state.ability_handler.active_triggers = []
        return room_id

    @staticmethod
    def _awakening_triggers(game_state, room_id):
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if (entry[0].card_id == room_id
                and getattr(entry[0], "room_door_number", None) == 2)
        ]

    def _unlock_awakening(self, game_state, controller, room_id):
        controller["mana_pool"]["B"] = 8
        self.assertTrue(game_state.ability_handler.handle_unlock_door(
            controller["battlefield"].index(room_id),
            controller=controller,
            room_id=room_id,
            door_number=2,
        ))
        queued = self._awakening_triggers(game_state, room_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2].get("event_type"), "DOOR_UNLOCKED")
        self.assertEqual(queued[0][2].get("door_number"), 2)
        self.assertFalse(queued[0][0].requires_target)

    def _public(self, handler, action: int, label: str):
        game_state = handler.game_state
        game_state.agent_is_p1 = (
            game_state.priority_player is game_state.p1)
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[action], (label, [
            index for index, allowed in enumerate(mask) if allowed]))
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(action)
        self.assertFalse(done, label)
        self.assertFalse(truncated, label)
        self.assertFalse(info.get("execution_failed"), (label, info))
        self.assertFalse(info.get("critical_error"), (label, info))

    def test_parser_creates_a_nontargeted_own_graveyard_mass_effect(self):
        effects = EffectFactory.create_effects(
            "Return all creature cards from your graveyard to the "
            "battlefield.")
        self.assertEqual(len(effects), 1)
        self.assertIsInstance(effects[0], ReanimateEffect)
        self.assertEqual(effects[0].scope, "all_yours")
        self.assertEqual(effects[0].target_type, "creature")
        self.assertFalse(effects[0].requires_target)

        targeted = EffectFactory.create_effects(
            "Return target creature card from your graveyard to the "
            "battlefield.")
        self.assertEqual(len(targeted), 1)
        self.assertIsInstance(targeted[0], ReanimateEffect)
        self.assertEqual(targeted[0].scope, "target")
        self.assertTrue(targeted[0].requires_target)

    def test_other_or_qualified_mass_returns_fail_closed_without_mutation(self):
        unsupported_templates = (
            "Return all land cards from your graveyard to the battlefield "
            "tapped.",
            "Return all creature cards with mana value 2 or less from your "
            "graveyard to the battlefield.",
            "Return all creature cards of the chosen type from your "
            "graveyard to the battlefield.",
        )
        for offset, instruction in enumerate(unsupported_templates):
            with self.subTest(instruction=instruction):
                effects = EffectFactory.create_effects(instruction)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], UnsupportedEffect)
                self.assertFalse(any(
                    isinstance(effect, ReanimateEffect)
                    for effect in effects))

                game_state, _, controller, _ = self._state(44410 + offset)
                source_id = self._room(game_state, controller)
                self._card(
                    game_state, controller, "Guarded Land", "Land",
                    "graveyard")
                self._card(
                    game_state, controller, "Guarded Creature",
                    "Creature - Spirit", "graveyard")
                graveyard_before = list(controller["graveyard"])
                battlefield_before = list(controller["battlefield"])
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]

                self.assertFalse(effects[0].apply(
                    game_state, source_id, controller, {}, context={}))

                self.assertEqual(controller["graveyard"], graveyard_before)
                self.assertEqual(controller["battlefield"], battlefield_before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before + 1)

    def test_unlock_returns_every_eligible_card_present_at_resolution(self):
        game_state, _, controller, opponent = self._state(44401)
        room_id = self._room(game_state, controller)
        first = self._card(
            game_state, controller, "Awakening First Creature",
            "Creature - Spirit", "graveyard")
        artifact_creature = self._card(
            game_state, controller, "Awakening Artifact Creature",
            "Artifact Creature - Construct", "graveyard")
        noncreature = self._card(
            game_state, controller, "Awakening Noncreature",
            "Artifact", "graveyard")
        opposing_creature = self._card(
            game_state, opponent, "Opposing Graveyard Creature",
            "Creature - Scout", "graveyard")
        late = self._card(
            game_state, controller, "Awakening Late Creature",
            "Creature - Zombie", "hand")

        with self.assertNoLogs(level=logging.WARNING):
            self._unlock_awakening(game_state, controller, room_id)
            # The affected set is determined when the trigger resolves, not
            # when the door-unlock event first puts it on the stack.
            self.assertTrue(game_state.move_card(
                late, controller, "hand", controller, "graveyard",
                cause="discard_before_awakening_resolution"))
            game_state.ability_handler.process_triggered_abilities()
            self.assertEqual(len(game_state.stack), 1)
            trigger = game_state.stack[-1]
            self.assertEqual(trigger[0:2], ("TRIGGER", room_id))
            self.assertFalse(trigger[3]["ability"].requires_target)
            self.assertIsNone(game_state.targeting_context)
            self.assertTrue(game_state.resolve_top_of_stack())

        for card_id in (first, artifact_creature, late):
            self.assertIn(card_id, controller["battlefield"])
            self.assertNotIn(card_id, controller["graveyard"])
        self.assertIn(noncreature, controller["graveyard"])
        self.assertIn(opposing_creature, opponent["graveyard"])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)
        self.assertIsNone(game_state.choice_context)

    def test_empty_matching_set_resolves_without_a_target_or_choice(self):
        game_state, _, controller, _ = self._state(44402)
        room_id = self._room(game_state, controller)
        noncreature = self._card(
            game_state, controller, "Awakening Empty-Set Instant",
            "Instant", "graveyard")

        with self.assertNoLogs(level=logging.WARNING):
            self._unlock_awakening(game_state, controller, room_id)
            game_state.ability_handler.process_triggered_abilities()
            self.assertTrue(game_state.resolve_top_of_stack())

        self.assertIn(noncreature, controller["graveyard"])
        self.assertIsNone(game_state.targeting_context)
        self.assertIsNone(game_state.choice_context)

    def test_casting_awakening_hall_dispatches_and_resolves_initial_unlock(self):
        game_state, handler, controller, _ = self._state(44403)
        room_id = inject_real_card(
            game_state, controller, self.ROOM, "hand")
        creature = self._card(
            game_state, controller, "Awakening Cast Creature",
            "Creature - Horror", "graveyard")
        noncreature = self._card(
            game_state, controller, "Awakening Cast Instant",
            "Instant", "graveyard")
        for _ in range(8):
            inject_real_card(
                game_state, controller, "Swamp", "battlefield")

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 446, "cast Awakening Hall")
            self._public(handler, 11, "Awakening Hall caster passes")
            self._public(handler, 11, "Awakening Hall opponent passes")

        room = game_state._safe_get_card(room_id)
        self.assertIn(room_id, controller["battlefield"])
        self.assertFalse(room.door1["unlocked"])
        self.assertTrue(room.door2["unlocked"])
        self.assertEqual(len(game_state.stack), 1)
        trigger = game_state.stack[-1]
        self.assertEqual(trigger[0:2], ("TRIGGER", room_id))
        self.assertEqual(trigger[3].get("door_number"), 2)
        self.assertFalse(trigger[3]["ability"].requires_target)
        self.assertIsNone(game_state.targeting_context)
        self.assertIsNone(game_state.choice_context)

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 11, "Awakening trigger controller passes")
            self._public(handler, 11, "Awakening trigger opponent passes")

        self.assertIn(creature, controller["battlefield"])
        self.assertIn(noncreature, controller["graveyard"])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)
        self.assertIsNone(game_state.choice_context)


if __name__ == "__main__":
    unittest.main()
