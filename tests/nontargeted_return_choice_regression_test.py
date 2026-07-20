"""Non-targeted return and unlock choice regressions from the Room/Exhaust probe.

The July 19 room-exhaust evidence replay failed Ghostly Dancers and
Greenhouse // Rickety Gazebo the same way: printed non-targeted returns were
parsed as mandatory targeted ReturnToHandEffects and then resolved with an
empty committed target set. Ghostly Dancers additionally lost its printed
"or unlock a locked door" mode entirely, and Rickety Gazebo's return was
bound to the battlefield instead of the cards it just milled. These
scenarios pin the printed behavior through the production
mask/apply/trigger pipeline.
"""

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


class _RoomExhaustScenarioBase(unittest.TestCase):
    def _state(self, seed: int):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.ability_handler.active_triggers = []
        for player in (controller, opponent):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["damage_counters"] = {}
            player["mana_pool"] = {
                color: 0 for color in ("W", "U", "B", "R", "G", "C")
            }
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _public(self, handler, action: int, label: str, context=None):
        game_state = handler.game_state
        game_state.agent_is_p1 = (
            game_state.priority_player is game_state.p1)
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[action], (label, [
            index for index, allowed in enumerate(mask) if allowed]))
        handler.current_valid_actions = mask
        apply_kwargs = {} if context is None else {"context": context}
        _, done, truncated, info = handler.apply_action(
            action, **apply_kwargs)
        self.assertFalse(done, label)
        self.assertFalse(truncated, label)
        self.assertFalse(info.get("execution_failed"), (label, info))
        self.assertFalse(info.get("critical_error"), (label, info))
        return info

    def _resolve_top_trigger(self, handler, label: str):
        self._public(handler, 11, f"{label}: controller passes")
        self._public(handler, 11, f"{label}: opponent passes")


class GhostlyDancersModalTriggerTest(_RoomExhaustScenarioBase):
    """"...return an enchantment card from your graveyard to your hand or
    unlock a locked door of a Room you control." is a two-mode trigger."""

    DANCERS = "Ghostly Dancers"
    STATIC_ROOM = "Dazzling Theater // Prop Room"

    def _enter_dancers_with_mode_choice(self, seed, *, graveyard_enchantment,
                                        locked_room):
        game_state, handler, controller, _ = self._state(seed)
        room_id = None
        if locked_room:
            room_id = inject_real_card(
                game_state, controller, self.STATIC_ROOM, "battlefield")
            room = game_state._safe_get_card(room_id)
            room.door1["unlocked"] = False
            room.door2["unlocked"] = False
        enchantment_id = None
        if graveyard_enchantment:
            enchantment_id = inject_into_zone(
                game_state, controller, {
                    "name": "Dancers Graveyard Enchantment",
                    "mana_cost": "{1}{W}",
                    "cmc": 2,
                    "type_line": "Enchantment",
                    "oracle_text": "",
                    "keywords": [],
                    "color_identity": ["W"],
                }, "graveyard")
            inject_into_zone(
                game_state, controller, {
                    "name": "Dancers Graveyard Creature",
                    "mana_cost": "{1}",
                    "cmc": 1,
                    "type_line": "Creature - Rat",
                    "oracle_text": "",
                    "keywords": [],
                    "color_identity": [],
                    "power": "1",
                    "toughness": "1",
                }, "graveyard")
        game_state.ability_handler.active_triggers = []
        dancers_id = inject_real_card(
            game_state, controller, self.DANCERS, "battlefield")
        if game_state.ability_handler.active_triggers:
            game_state.ability_handler.process_triggered_abilities()
        return (game_state, handler, controller, room_id, enchantment_id,
                dancers_id)

    def _assert_mode_choice_open(self, game_state, controller):
        choice = game_state.choice_context
        self.assertIsNotNone(choice, "modal ETB trigger opened no mode choice")
        self.assertEqual(choice.get("type"), "trigger_mode")
        self.assertIs(choice.get("player"), controller)
        options = [str(option).lower() for option in choice.get("options", [])]
        self.assertEqual(len(options), 2, options)
        self.assertIn("return an enchantment card from your graveyard",
                      options[0])
        self.assertIn("unlock a locked door", options[1])

    def test_return_mode_moves_chosen_enchantment_from_graveyard_to_hand(self):
        (game_state, handler, controller, _, enchantment_id,
         _) = self._enter_dancers_with_mode_choice(
            47101, graveyard_enchantment=True, locked_room=True)
        self._assert_mode_choice_open(game_state, controller)

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 353, "choose the graveyard-return mode")
            self._resolve_top_trigger(handler, "Dancers return mode")

            choice = game_state.choice_context
            self.assertIsNotNone(
                choice, "return mode opened no graveyard selection")
            self.assertEqual(choice.get("type"), "resolution_choice")
            self.assertEqual(
                choice.get("choice_kind"), "return_from_graveyard")
            self.assertEqual(choice.get("options"), [enchantment_id])
            self.assertFalse(choice.get("optional"))
            mask = handler.generate_valid_actions()
            self.assertFalse(
                mask[11], "a mandatory return exposed a decline action")
            self._public(handler, 353, "select the enchantment to return")

        self.assertIn(enchantment_id, controller["hand"])
        self.assertNotIn(enchantment_id, controller["graveyard"])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)

    def test_unlock_mode_unlocks_exactly_the_chosen_locked_door(self):
        (game_state, handler, controller, room_id, _,
         _) = self._enter_dancers_with_mode_choice(
            47102, graveyard_enchantment=True, locked_room=True)
        self._assert_mode_choice_open(game_state, controller)
        room = game_state._safe_get_card(room_id)

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 354, "choose the unlock mode")
            self._resolve_top_trigger(handler, "Dancers unlock mode")

            choice = game_state.choice_context
            self.assertIsNotNone(
                choice, "unlock mode opened no locked-door selection")
            self.assertEqual(choice.get("type"), "resolution_choice")
            self.assertEqual(choice.get("choice_kind"), "unlock_door")
            self.assertEqual(len(choice.get("options", [])), 2, choice)
            self._public(handler, 354, "select the second locked door")

        self.assertFalse(room.door1["unlocked"])
        self.assertTrue(room.door2["unlocked"])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(sum(controller["mana_pool"].values()), 0)

    def test_return_mode_with_empty_graveyard_resolves_quietly(self):
        (game_state, handler, controller, _, _,
         _) = self._enter_dancers_with_mode_choice(
            47103, graveyard_enchantment=False, locked_room=False)
        self._assert_mode_choice_open(game_state, controller)

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 353, "choose the empty return mode")
            self._resolve_top_trigger(handler, "Dancers empty return mode")

        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(controller["hand"], [])


class RicketyGazeboLinkedMillTest(_RoomExhaustScenarioBase):
    """"When you unlock this door, mill four cards, then return up to two
    permanent cards from among them to your hand." binds to the milled set."""

    ROOM = "Greenhouse // Rickety Gazebo"

    def test_unlock_mills_four_then_returns_up_to_two_milled_permanents(self):
        game_state, handler, controller, _ = self._state(47201)
        room_id = inject_real_card(
            game_state, controller, self.ROOM, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False
        lands = [
            inject_real_card(
                game_state, controller, "Forest", "battlefield")
            for _ in range(4)
        ]

        def library_card(name, type_line, extra=None):
            data = {
                "name": name,
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": type_line,
                "oracle_text": "",
                "keywords": [],
                "color_identity": [],
            }
            data.update(extra or {})
            return inject_into_zone(game_state, controller, data, "library")

        permanent_one = library_card(
            "Gazebo Milled Creature", "Creature - Elf",
            {"power": "1", "toughness": "1"})
        nonpermanent_one = library_card("Gazebo Milled Sorcery", "Sorcery")
        permanent_two = library_card("Gazebo Milled Enchantment", "Enchantment")
        nonpermanent_two = library_card("Gazebo Milled Instant", "Instant")
        game_state.ability_handler.active_triggers = []

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 248, "unlock Rickety Gazebo")
            if game_state.ability_handler.active_triggers:
                game_state.ability_handler.process_triggered_abilities()
            self.assertEqual(len(game_state.stack), 1)
            self.assertEqual(game_state.stack[-1][0:2], ("TRIGGER", room_id))
            self._resolve_top_trigger(handler, "Rickety Gazebo unlock")

            choice = game_state.choice_context
            self.assertIsNotNone(
                choice, "linked mill opened no milled-card selection")
            self.assertEqual(choice.get("type"), "dig_select")
            self.assertEqual(
                choice.get("options"), [permanent_one, permanent_two])
            self.assertEqual(choice.get("remaining"), 2)
            self.assertTrue(choice.get("optional"))
            self._public(handler, 354, "return the milled enchantment")
            self._public(handler, 353, "return the milled creature")

        self.assertTrue(room.door2["unlocked"])
        self.assertCountEqual(
            controller["hand"], [permanent_one, permanent_two])
        self.assertCountEqual(
            controller["graveyard"], [nonpermanent_one, nonpermanent_two])
        self.assertEqual(controller["library"], [])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)
        self.assertTrue(all(
            land in controller["tapped_permanents"] for land in lands))
        self.assertEqual(sum(controller["mana_pool"].values()), 0)


class SubtypeAwareGraveyardReturnTest(_RoomExhaustScenarioBase):
    """Carrion Cruiser: "mill two cards. Then return a creature or Vehicle
    card from your graveyard to your hand." exercises subtype-aware,
    non-targeted graveyard recovery. Pins the ledger-regeneration follow-up
    that moved Vehicle/Spacecraft return cards off the empty-target bounce.
    """

    CRUISER = "Carrion Cruiser"

    def test_etb_mills_then_returns_a_creature_or_vehicle_by_subtype(self):
        game_state, handler, controller, _ = self._state(47301)
        creature_id = inject_into_zone(
            game_state, controller, {
                "name": "Cruiser Graveyard Creature",
                "mana_cost": "{1}", "cmc": 1,
                "type_line": "Creature - Zombie",
                "oracle_text": "", "power": "2", "toughness": "2",
                "keywords": [], "color_identity": [],
            }, "graveyard")
        vehicle_id = inject_into_zone(
            game_state, controller, {
                "name": "Cruiser Graveyard Vehicle",
                "mana_cost": "{2}", "cmc": 2,
                "type_line": "Artifact - Vehicle",
                "oracle_text": "", "power": "3", "toughness": "3",
                "keywords": [], "color_identity": [],
            }, "graveyard")
        # An artifact that is not a Vehicle must not be eligible.
        inject_into_zone(
            game_state, controller, {
                "name": "Cruiser Graveyard Artifact",
                "mana_cost": "{1}", "cmc": 1,
                "type_line": "Artifact",
                "oracle_text": "", "keywords": [], "color_identity": [],
            }, "graveyard")
        for index in range(2):
            inject_into_zone(
                game_state, controller, {
                    "name": f"Cruiser Library Card {index}",
                    "mana_cost": "{1}", "cmc": 1,
                    "type_line": "Sorcery",
                    "oracle_text": "", "keywords": [], "color_identity": [],
                }, "library")
        game_state.ability_handler.active_triggers = []

        with self.assertNoLogs(level=logging.WARNING):
            inject_real_card(
                game_state, controller, self.CRUISER, "battlefield")
            if game_state.ability_handler.active_triggers:
                game_state.ability_handler.process_triggered_abilities()
            self._resolve_top_trigger(handler, "Carrion Cruiser ETB")

            choice = game_state.choice_context
            self.assertIsNotNone(
                choice, "subtype return opened no graveyard selection")
            self.assertEqual(choice.get("type"), "resolution_choice")
            self.assertEqual(
                choice.get("choice_kind"), "return_from_graveyard")
            # Only the creature and the Vehicle are eligible; the plain
            # artifact and the two milled sorceries are not.
            self.assertCountEqual(
                choice.get("options"), [creature_id, vehicle_id])
            option_index = choice["options"].index(vehicle_id)
            self._public(
                handler, 353 + option_index, "return the Vehicle")

        self.assertIn(vehicle_id, controller["hand"])
        self.assertNotIn(vehicle_id, controller["graveyard"])
        self.assertIn(creature_id, controller["graveyard"])
        self.assertEqual(len(controller["graveyard"]), 4)  # 2 milled + 2 left
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)


if __name__ == "__main__":
    unittest.main()
