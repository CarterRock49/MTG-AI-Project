"""Exact source and door identity regressions for Room unlock triggers."""

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
from Playersim.ability_types import StaticAbility, TriggeredAbility  # noqa: E402


class RoomTriggerSourceRegressionTest(unittest.TestCase):
    ROARING = "Roaring Furnace // Steaming Sauna"
    OTHER_ROOM = "Dazzling Theater // Prop Room"
    DUPLICATE_CONDITION_ROOM = "Underwater Tunnel // Slimy Aquarium"
    ONGOING_SECOND_DOOR_ROOM = "Glassworks // Shattered Yard"
    STATIC_ROOM = "Dazzling Theater // Prop Room"
    GALLERY_ROOM = "Dollmaker's Shop // Porcelain Gallery"
    MIRROR_ROOM = "Mirror Room // Fractured Realm"

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

    def _fill_hand(self, game_state, controller, count, prefix):
        for index in range(count):
            inject_into_zone(
                game_state, controller, {
                    "name": f"{prefix} {index}",
                    "mana_cost": "{99}",
                    "cmc": 99,
                    "type_line": "Sorcery",
                    "oracle_text": "Draw a card.",
                    "keywords": [],
                    "color_identity": [],
                }, "hand")

    def _resolve_public_spell(self, handler, label):
        self._public(handler, 11, f"{label} caster passes")
        self._public(handler, 11, f"{label} opponent passes")

    def _select_catalog_option(self, handler, option_index, label):
        self._public(handler, 479, f"open catalog for {label}")
        for page in range(option_index // 10):
            self._public(handler, 479, f"page catalog for {label} {page + 1}")
        self._public(
            handler, 353 + option_index % 10,
            f"select catalog option for {label}")

    def _select_target(self, handler, controller, target_id):
        context = handler.game_state.targeting_context
        self.assertIsNotNone(context)
        candidates = handler._get_target_selection_candidates(
            controller, context)
        self.assertIn(target_id, candidates)
        absolute_index = candidates.index(target_id)
        for _ in range(absolute_index // 10):
            self._public(handler, 479, "page Room trigger targets")
        self._public(
            handler, 274 + absolute_index % 10,
            "select Roaring Furnace damage target")

    def _resolve_trigger(self, game_state):
        if game_state.ability_handler.active_triggers:
            game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0], "TRIGGER")

    def test_roaring_furnace_first_door_publicly_targets_and_deals_hand_size(self):
        game_state, handler, controller, opponent = self._state(44101)
        roaring = inject_real_card(
            game_state, controller, self.ROARING, "battlefield")
        room = game_state._safe_get_card(roaring)
        room.door2["unlocked"] = True
        target = inject_into_zone(
            game_state, opponent, {
                "name": "Furnace Damage Target",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Creature - Wall",
                "oracle_text": "",
                "keywords": [],
                "color_identity": [],
                "power": "0",
                "toughness": "10",
            }, "battlefield")
        for index in range(3):
            inject_into_zone(
                game_state, controller, {
                    "name": f"Furnace Hand Card {index}",
                    "mana_cost": "{1}",
                    "cmc": 1,
                    "type_line": "Sorcery",
                    "oracle_text": "Draw a card.",
                    "keywords": [],
                    "color_identity": [],
                }, "hand")
        lands = [
            inject_real_card(
                game_state, controller, "Mountain", "battlefield")
            for _ in range(2)
        ]

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 248, "unlock Roaring Furnace")
            triggers = game_state.ability_handler.active_triggers
            self.assertEqual(triggers, [])
            self._resolve_trigger(game_state)
            trigger_object = game_state.stack[-1]
            self.assertEqual(trigger_object[1], roaring)
            self.assertEqual(trigger_object[3]["room_id"], roaring)
            self.assertEqual(trigger_object[3]["door_number"], 1)
            self._select_target(handler, controller, target)
            self._public(handler, 11, "Furnace controller passes")
            self._public(handler, 11, "Furnace opponent passes")

        self.assertEqual(game_state.stack, [])
        self.assertEqual(game_state.targeting_context, None)
        self.assertEqual(opponent["damage_counters"].get(target), 3)
        self.assertTrue(room.door1["unlocked"])
        self.assertTrue(all(
            land in controller["tapped_permanents"] for land in lands))
        self.assertEqual(sum(controller["mana_pool"].values()), 0)

    def test_mirror_room_targets_only_own_creature_and_survives_source_leaving(self):
        game_state, handler, controller, opponent = self._state(44112)
        mirror_id = inject_real_card(
            game_state, controller, self.MIRROR_ROOM, "battlefield")
        target_id = inject_into_zone(game_state, controller, {
            "name": "Mirror Room Rabbit",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Rabbit Warrior",
            "oracle_text": "Vigilance",
            "power": 2,
            "toughness": 3,
            "color_identity": ["G"],
        }, "battlefield")
        own_artifact = inject_into_zone(game_state, controller, {
            "name": "Mirror Room Artifact",
            "mana_cost": "{2}",
            "cmc": 2,
            "type_line": "Artifact",
            "oracle_text": "",
            "color_identity": [],
        }, "battlefield")
        opposing_target = inject_into_zone(game_state, opponent, {
            "name": "Opponent Mirror Creature",
            "mana_cost": "{1}{R}",
            "cmc": 2,
            "type_line": "Creature - Goblin",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["R"],
        }, "battlefield")
        for _ in range(3):
            inject_real_card(
                game_state, controller, "Island", "battlefield")

        self._public(handler, 248, "unlock Mirror Room")
        self._resolve_trigger(game_state)
        targeting = game_state.targeting_context
        self.assertIsNotNone(targeting)
        candidates = handler._get_target_selection_candidates(
            controller, targeting)
        self.assertIn(target_id, candidates)
        self.assertNotIn(own_artifact, candidates)
        self.assertNotIn(opposing_target, candidates)
        self._select_target(handler, controller, target_id)

        before = set(controller.get("tokens", []))
        self.assertTrue(game_state.move_card(
            mirror_id, controller, "battlefield", controller, "hand",
            cause="mirror_source_left"))
        self._public(handler, 11, "Mirror Room controller passes")
        self._public(handler, 11, "Mirror Room opponent passes")

        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        token = game_state._safe_get_card(created[0])
        self.assertEqual(token.name, "Mirror Room Rabbit")
        self.assertEqual((token.power, token.toughness), (2, 3))
        self.assertEqual(
            {subtype.casefold() for subtype in token.subtypes},
            {"rabbit", "warrior", "reflection"})
        self.assertTrue(game_state.check_keyword(created[0], "vigilance"))

    def test_mirror_room_trigger_fizzles_when_its_target_leaves(self):
        game_state, handler, controller, _ = self._state(44113)
        inject_real_card(
            game_state, controller, self.MIRROR_ROOM, "battlefield")
        target_id = inject_into_zone(game_state, controller, {
            "name": "Departing Mirror Room Target",
            "mana_cost": "{1}{U}",
            "cmc": 2,
            "type_line": "Creature - Human Wizard",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["U"],
        }, "battlefield")
        for _ in range(3):
            inject_real_card(
                game_state, controller, "Island", "battlefield")

        self._public(handler, 248, "unlock Mirror Room for fizzle")
        self._resolve_trigger(game_state)
        self._select_target(handler, controller, target_id)
        before = set(controller.get("tokens", []))
        self.assertTrue(game_state.move_card(
            target_id, controller, "battlefield", controller, "graveyard",
            cause="mirror_target_left"))
        self._public(handler, 11, "Mirror fizzle controller passes")
        self._public(handler, 11, "Mirror fizzle opponent passes")

        self.assertEqual(set(controller.get("tokens", [])), before)
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)

    def test_other_controlled_room_unlock_is_a_close_non_event(self):
        game_state, handler, controller, _ = self._state(44102)
        roaring = inject_real_card(
            game_state, controller, self.ROARING, "battlefield")
        other_room = inject_real_card(
            game_state, controller, self.OTHER_ROOM, "battlefield")
        other = game_state._safe_get_card(other_room)
        other.door1["unlocked"] = True
        lands = [
            inject_real_card(
                game_state, controller, "Plains", "battlefield")
            for _ in range(3)
        ]
        game_state.ability_handler.active_triggers = []

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 249, "unlock the other controlled Room")

        self.assertTrue(other.door2["unlocked"])
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)
        self.assertTrue(all(
            land in controller["tapped_permanents"] for land in lands))
        self.assertFalse(game_state._safe_get_card(roaring).door1["unlocked"])

    def test_steaming_sauna_door_is_not_roaring_furnace_event(self):
        game_state, handler, controller, _ = self._state(44103)
        roaring = inject_real_card(
            game_state, controller, self.ROARING, "battlefield")
        room = game_state._safe_get_card(roaring)
        room.door1["unlocked"] = True
        lands = [
            inject_real_card(
                game_state, controller, "Island", "battlefield")
            for _ in range(5)
        ]
        game_state.ability_handler.active_triggers = []

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 248, "unlock Steaming Sauna")

        self.assertTrue(room.door2["unlocked"])
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.targeting_context)
        self.assertTrue(all(
            land in controller["tapped_permanents"] for land in lands))

    def test_duplicate_this_door_conditions_bind_the_full_printed_clause(self):
        game_state, handler, controller, _ = self._state(44104)
        room_id = inject_real_card(
            game_state, controller, self.DUPLICATE_CONDITION_ROOM,
            "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door2["unlocked"] = True
        island = inject_real_card(
            game_state, controller, "Island", "battlefield")
        ability = next(
            registered for registered
            in game_state.ability_handler.registered_abilities[room_id]
            if (isinstance(registered, TriggeredAbility)
                and registered.effect == "surveil 2"))
        unlock_triggers = [
            registered for registered
            in game_state.ability_handler.registered_abilities[room_id]
            if (isinstance(registered, TriggeredAbility)
                and "unlock this door"
                in registered.trigger_condition.lower())
        ]
        self.assertEqual(len(unlock_triggers), 2)
        self.assertEqual(
            [trigger.room_face_index for trigger in unlock_triggers],
            [0, 1])
        self.assertEqual(
            [trigger.room_door_number for trigger in unlock_triggers],
            [1, 2])
        self.assertEqual(ability.room_door_number, 1)

        base_context = {
            "source_card_id": room_id,
            "source_card": room,
            "event_card_id": room_id,
            "room_id": room_id,
            "controller": controller,
            "event_controller": controller,
        }
        room.door1["unlocked"] = True
        self.assertTrue(ability.can_trigger(
            "DOOR_UNLOCKED", {**base_context, "door_number": 1}))
        self.assertFalse(ability.can_trigger(
            "DOOR_UNLOCKED", {**base_context, "door_number": 2}))
        room.door1["unlocked"] = False

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 248, "unlock Underwater Tunnel")

        self.assertTrue(room.door1["unlocked"])
        self.assertIn(island, controller["tapped_permanents"])
        self.assertEqual(sum(controller["mana_pool"].values()), 0)
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0:2], ("TRIGGER", room_id))
        self.assertEqual(game_state.stack[-1][3]["door_number"], 1)

    def test_second_face_unlock_trigger_is_registered_and_fires_for_door_two(self):
        game_state, handler, controller, _ = self._state(44105)
        room_id = inject_real_card(
            game_state, controller, self.DUPLICATE_CONDITION_ROOM,
            "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        lands = [
            inject_real_card(
                game_state, controller, "Island", "battlefield")
            for _ in range(4)
        ]
        triggers = [
            registered for registered
            in game_state.ability_handler.registered_abilities[room_id]
            if isinstance(registered, TriggeredAbility)
        ]
        second = next(
            trigger for trigger in triggers
            if trigger.room_door_number == 2)

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 248, "unlock Slimy Aquarium")

        self.assertTrue(room.door2["unlocked"])
        self.assertTrue(all(
            land in controller["tapped_permanents"] for land in lands))
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(len(game_state.stack), 1)
        self.assertIs(game_state.stack[-1][3]["ability"], second)
        self.assertEqual(game_state.stack[-1][3]["door_number"], 2)

    def test_locked_second_face_ongoing_trigger_activates_after_unlock(self):
        game_state, handler, controller, _ = self._state(44106)
        room_id = inject_real_card(
            game_state, controller, self.ONGOING_SECOND_DOOR_ROOM,
            "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        second = next(
            registered for registered
            in game_state.ability_handler.registered_abilities[room_id]
            if (isinstance(registered, TriggeredAbility)
                and registered.room_door_number == 2))

        self.assertFalse(game_state.ability_handler.check_abilities(
            None, "BEGINNING_OF_END_STEP", {"controller": controller}))
        self.assertEqual(game_state.ability_handler.active_triggers, [])

        for _ in range(5):
            inject_real_card(
                game_state, controller, "Mountain", "battlefield")
        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 248, "unlock Shattered Yard")

        self.assertTrue(room.door2["unlocked"])
        self.assertTrue(game_state.ability_handler.check_abilities(
            None, "BEGINNING_OF_END_STEP", {"controller": controller}))
        self.assertEqual(
            [row[0] for row in game_state.ability_handler.active_triggers],
            [second])

    def test_room_door_state_resets_when_the_permanent_changes_zones(self):
        game_state, _, controller, _ = self._state(44107)
        room_id = inject_real_card(
            game_state, controller, self.DUPLICATE_CONDITION_ROOM,
            "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = True

        self.assertTrue(game_state.move_card(
            room_id, controller, "battlefield", controller, "hand",
            cause="room_reset_regression"))

        self.assertFalse(room.door1["unlocked"])
        self.assertFalse(room.door2["unlocked"])

    def test_locked_steaming_sauna_does_not_remove_the_cleanup_hand_limit(self):
        game_state, _, controller, _ = self._state(44111)
        room_id = inject_real_card(
            game_state, controller, self.ROARING, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False

        self.assertEqual(
            game_state._maximum_hand_size_for_player(controller),
            game_state.max_hand_size)

        room.door2["unlocked"] = True
        self.assertIsNone(
            game_state._maximum_hand_size_for_player(controller))
        for index in range(game_state.max_hand_size + 1):
            inject_into_zone(
                game_state, controller, {
                    "name": f"Sauna Hand Card {index}",
                    "mana_cost": "{1}",
                    "cmc": 1,
                    "type_line": "Sorcery",
                    "oracle_text": "Draw a card.",
                    "keywords": [],
                    "color_identity": [],
                }, "hand")

        self.assertFalse(game_state._cleanup_step_actions(controller))
        self.assertEqual(
            len(controller["hand"]), game_state.max_hand_size + 1)
        self.assertIsNone(game_state.choice_context)

        room.door2["unlocked"] = False
        self.assertTrue(game_state._cleanup_step_actions(controller))
        self.assertIsNotNone(game_state.choice_context)

    def test_public_right_room_cast_pays_right_cost_and_unlocks_only_door_two(self):
        game_state, handler, controller, _ = self._state(44108)
        room_id = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "hand")
        room = game_state._safe_get_card(room_id)
        for _ in range(3):
            inject_real_card(
                game_state, controller, "Plains", "battlefield")

        mask = handler.generate_valid_actions()
        self.assertFalse(mask[445])
        self.assertTrue(mask[446])
        self.assertFalse(mask[20])
        self._public(handler, 446, "cast Prop Room")

        self.assertNotIn(room_id, controller["hand"])
        self.assertNotIn(room_id, controller["battlefield"])
        self.assertFalse(room.door1["unlocked"])
        self.assertFalse(room.door2["unlocked"])
        self.assertEqual(len(game_state.stack), 1)
        stack_context = game_state.stack[-1][3]
        self.assertEqual(stack_context["room_cast_face_index"], 1)
        self.assertEqual(stack_context["room_cast_door_number"], 2)
        self.assertEqual(stack_context["room_cast_face_mana_cost"], "{2}{W}")
        self.assertEqual(stack_context["final_paid_cost"]["generic"], 2)
        self.assertEqual(stack_context["final_paid_cost"]["W"], 1)
        self.assertFalse(stack_context["requires_target"])

        self._public(handler, 11, "Prop Room caster passes")
        self._public(handler, 11, "Prop Room opponent passes")

        self.assertIn(room_id, controller["battlefield"])
        self.assertTrue(room.door2["unlocked"])
        self.assertFalse(room.door1["unlocked"])
        self.assertEqual(room.current_face, 0)
        self.assertEqual(room.name, self.STATIC_ROOM)

    def test_public_left_room_cast_pays_left_cost_and_unlocks_only_door_one(self):
        game_state, handler, controller, _ = self._state(44109)
        room_id = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "hand")
        room = game_state._safe_get_card(room_id)
        for _ in range(4):
            inject_real_card(
                game_state, controller, "Plains", "battlefield")

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[445])
        self.assertTrue(mask[446])
        self.assertTrue(mask[20])
        self._public(handler, 445, "cast Dazzling Theater")
        stack_context = game_state.stack[-1][3]
        self.assertEqual(stack_context["room_cast_face_index"], 0)
        self.assertEqual(stack_context["room_cast_door_number"], 1)
        self.assertEqual(stack_context["room_cast_face_mana_cost"], "{3}{W}")
        self.assertEqual(stack_context["final_paid_cost"]["generic"], 3)
        self.assertEqual(stack_context["final_paid_cost"]["W"], 1)

        self._public(handler, 11, "Dazzling Theater caster passes")
        self._public(handler, 11, "Dazzling Theater opponent passes")

        self.assertIn(room_id, controller["battlefield"])
        self.assertTrue(room.door1["unlocked"])
        self.assertFalse(room.door2["unlocked"])
        self.assertEqual(room.current_face, 0)
        self.assertEqual(room.name, self.STATIC_ROOM)

    def test_forged_unaffordable_room_face_cast_fails_atomically(self):
        game_state, _, controller, _ = self._state(44110)
        room_id = inject_real_card(
            game_state, controller, self.ROARING, "hand")
        room = game_state._safe_get_card(room_id)
        lands = [
            inject_real_card(
                game_state, controller, "Mountain", "battlefield")
            for _ in range(2)
        ]
        before_pool = dict(controller["mana_pool"])

        self.assertFalse(game_state.cast_spell(
            room_id, controller, context={
                "source_zone": "hand",
                "room_cast_face_index": 1,
                "room_cast_door_number": 2,
                "cast_right_half": True,
            }))

        self.assertIn(room_id, controller["hand"])
        self.assertEqual(game_state.stack, [])
        self.assertEqual(controller["mana_pool"], before_pool)
        self.assertFalse(room.door1["unlocked"])
        self.assertFalse(room.door2["unlocked"])
        self.assertTrue(all(
            land not in controller["tapped_permanents"] for land in lands))

    def test_casting_second_room_face_dispatches_its_initial_unlock_trigger(self):
        game_state, handler, controller, _ = self._state(44112)
        room_id = inject_real_card(
            game_state, controller, self.DUPLICATE_CONDITION_ROOM, "hand")
        room = game_state._safe_get_card(room_id)
        for _ in range(4):
            inject_real_card(
                game_state, controller, "Island", "battlefield")

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 446, "cast Slimy Aquarium")
            self._public(handler, 11, "Slimy Aquarium caster passes")
            self._public(handler, 11, "Slimy Aquarium opponent passes")

        self.assertIn(room_id, controller["battlefield"])
        self.assertFalse(room.door1["unlocked"])
        self.assertTrue(room.door2["unlocked"])
        self.assertEqual(len(game_state.stack), 1)
        trigger = game_state.stack[-1]
        self.assertEqual(trigger[0:2], ("TRIGGER", room_id))
        self.assertEqual(trigger[3]["door_number"], 2)
        self.assertEqual(trigger[3]["ability"].room_door_number, 2)

    def test_right_room_face_is_publicly_castable_at_hand_indices_eight_and_nine(self):
        for hand_index in (8, 9):
            with self.subTest(hand_index=hand_index):
                game_state, handler, controller, _ = self._state(
                    44120 + hand_index)
                self._fill_hand(
                    game_state, controller, hand_index,
                    f"Room index {hand_index} filler")
                room_id = inject_real_card(
                    game_state, controller, self.STATIC_ROOM, "hand")
                room = game_state._safe_get_card(room_id)
                for _ in range(3):
                    inject_real_card(
                        game_state, controller, "Plains", "battlefield")

                mask = handler.generate_valid_actions()
                alias_action = 396 + (hand_index - 8)
                self.assertFalse(mask[445])
                self.assertTrue(mask[446])
                self.assertFalse(mask[alias_action])
                context = handler.action_reasons_with_context[446]["context"]
                self.assertEqual(context["hand_idx"], hand_index)
                self.assertEqual(context["card_id"], room_id)
                self.assertEqual(context["room_cast_face_index"], 1)
                self.assertEqual(context["room_cast_door_number"], 2)

                self._public(
                    handler, 446,
                    f"cast Prop Room from hand index {hand_index}")
                self.assertEqual(game_state.stack[-1][1], room_id)
                self.assertEqual(
                    game_state.stack[-1][3]["room_cast_face_index"], 1)
                self._resolve_public_spell(
                    handler, f"index {hand_index} Prop Room")

                self.assertIn(room_id, controller["battlefield"])
                self.assertFalse(room.door1["unlocked"])
                self.assertTrue(room.door2["unlocked"])

    def test_second_room_at_hand_index_ten_uses_the_public_action_catalog(self):
        game_state, handler, controller, _ = self._state(44130)
        self._fill_hand(game_state, controller, 9, "Catalog filler")
        first_room = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "hand")
        second_room = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "hand")
        self.assertEqual(controller["hand"].index(first_room), 9)
        self.assertEqual(controller["hand"].index(second_room), 10)
        for _ in range(4):
            inject_real_card(
                game_state, controller, "Plains", "battlefield")

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[446])
        self.assertEqual(
            handler.action_reasons_with_context[446]["context"]["card_id"],
            first_room)
        self.assertTrue(mask[479])
        options = handler.action_reasons_with_context[479]["context"][
            "options"]
        matches = [
            (index, entry) for index, entry in enumerate(options)
            if (entry.get("action_index") == 446
                and entry.get("action_context", {}).get("card_id")
                == second_room
                and entry.get("action_context", {}).get(
                    "room_cast_face_index") == 1)
        ]
        self.assertEqual(len(matches), 1, options)
        option_index, entry = matches[0]
        self.assertEqual(entry["action_context"]["hand_idx"], 10)
        self.assertEqual(
            entry["action_context"]["room_cast_door_number"], 2)

        self._select_catalog_option(
            handler, option_index, "index-ten Prop Room")
        self.assertEqual(game_state.stack[-1][1], second_room)
        self.assertIn(first_room, controller["hand"])
        self.assertNotIn(second_room, controller["hand"])
        self._resolve_public_spell(handler, "index-ten Prop Room")

        second = game_state._safe_get_card(second_room)
        self.assertIn(second_room, controller["battlefield"])
        self.assertFalse(second.door1["unlocked"])
        self.assertTrue(second.door2["unlocked"])

    def test_ordinary_exile_permission_casts_the_pinned_right_room_face(self):
        game_state, handler, controller, _ = self._state(44131)
        room_id = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "exile")
        room = game_state._safe_get_card(room_id)
        game_state.cards_castable_from_exile.add(room_id)
        for _ in range(3):
            inject_real_card(
                game_state, controller, "Plains", "battlefield")

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[230])
        context = handler.action_reasons_with_context[230]["context"]
        self.assertEqual(context["card_id"], room_id)
        self.assertEqual(context["source_zone"], "exile")
        self.assertEqual(context["exile_permission"], "ordinary")
        self.assertEqual(context["room_cast_face_index"], 1)
        self.assertEqual(context["room_cast_door_number"], 2)

        self._public(handler, 230, "cast Prop Room from ordinary exile")
        stack_context = game_state.stack[-1][3]
        self.assertEqual(game_state.stack[-1][1], room_id)
        self.assertEqual(stack_context["source_zone"], "exile")
        self.assertEqual(stack_context["exile_permission"], "ordinary")
        self.assertNotIn("use_alt_cost", stack_context)
        self._resolve_public_spell(handler, "exiled Prop Room")

        self.assertIn(room_id, controller["battlefield"])
        self.assertNotIn(room_id, controller["exile"])
        self.assertFalse(room.door1["unlocked"])
        self.assertTrue(room.door2["unlocked"])

    def test_wrenn_emblem_casts_the_pinned_right_room_face_from_graveyard(self):
        game_state, handler, controller, _ = self._state(44132)
        controller["emblems"] = [{"kind": "graveyard_permanents"}]
        room_id = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "graveyard")
        room = game_state._safe_get_card(room_id)
        for _ in range(3):
            inject_real_card(
                game_state, controller, "Plains", "battlefield")

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[472])
        context = handler.action_reasons_with_context[472]["context"]
        self.assertEqual(context["card_id"], room_id)
        self.assertEqual(context["source_zone"], "graveyard")
        self.assertTrue(context["emblem_graveyard_cast"])
        self.assertEqual(context["room_cast_face_index"], 1)
        self.assertEqual(context["room_cast_door_number"], 2)

        self._public(handler, 472, "cast Prop Room through Wrenn emblem")
        stack_context = game_state.stack[-1][3]
        self.assertEqual(game_state.stack[-1][1], room_id)
        self.assertEqual(stack_context["source_zone"], "graveyard")
        self.assertTrue(stack_context["emblem_graveyard_cast"])
        self._resolve_public_spell(handler, "graveyard Prop Room")

        self.assertIn(room_id, controller["battlefield"])
        self.assertNotIn(room_id, controller["graveyard"])
        self.assertFalse(room.door1["unlocked"])
        self.assertTrue(room.door2["unlocked"])

    def test_generic_play_spell_alias_pins_and_resolves_the_front_room_face(self):
        game_state, handler, controller, _ = self._state(44133)
        room_id = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "hand")
        room = game_state._safe_get_card(room_id)
        for _ in range(4):
            inject_real_card(
                game_state, controller, "Plains", "battlefield")

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[20])
        context = handler.action_reasons_with_context[20]["context"]
        self.assertEqual(context["hand_idx"], 0)
        self.assertEqual(context["card_id"], room_id)
        self.assertEqual(context["room_cast_face_index"], 0)
        self.assertEqual(context["room_cast_door_number"], 1)
        self.assertEqual(context["room_cast_face_name"], "Dazzling Theater")
        self.assertEqual(context["room_cast_face_mana_cost"], "{3}{W}")
        self.assertEqual(context["room_cast_face_colors"], [1, 0, 0, 0, 0])

        self._public(handler, 20, "cast front Room through PLAY_SPELL")
        stack_context = game_state.stack[-1][3]
        self.assertEqual(game_state.stack[-1][1], room_id)
        self.assertEqual(stack_context["room_cast_face_index"], 0)
        self.assertEqual(stack_context["room_cast_door_number"], 1)
        self.assertNotIn("cast_right_half", stack_context)
        self._resolve_public_spell(handler, "PLAY_SPELL Dazzling Theater")

        self.assertIn(room_id, controller["battlefield"])
        self.assertTrue(room.door1["unlocked"])
        self.assertFalse(room.door2["unlocked"])

    def test_public_front_room_actions_strip_forged_internal_cast_flags(self):
        forged = {
            "card_id": -999,
            "controller_id": "p2",
            "source_zone": "exile",
            "room_cast_face_index": 1,
            "room_cast_door_number": 2,
            "use_alt_cost": "plot",
            "plot_cast": True,
            "is_copy": True,
            "skip_default_movement": True,
            "prepared_copy": True,
            "fuse": True,
            "cast_right_half": True,
            "attacker_marker": "must not cross the cast boundary",
        }
        forbidden_stack_keys = {
            "use_alt_cost", "plot_cast", "is_copy",
            "skip_default_movement", "prepared_copy", "fuse",
            "cast_right_half", "attacker_marker",
        }
        for action in (445, 20):
            with self.subTest(action=action):
                game_state, handler, controller, _ = self._state(
                    44140 + action)
                room_id = inject_real_card(
                    game_state, controller, self.STATIC_ROOM, "hand")
                room = game_state._safe_get_card(room_id)
                lands = [
                    inject_real_card(
                        game_state, controller, "Plains", "battlefield")
                    for _ in range(4)
                ]

                self._public(
                    handler, action, f"forged context action {action}",
                    context=dict(forged))
                self.assertEqual(game_state.stack[-1][1], room_id)
                stack_context = game_state.stack[-1][3]
                self.assertEqual(stack_context["card_id"], room_id)
                self.assertEqual(stack_context["controller_id"], "p1")
                self.assertEqual(stack_context["source_zone"], "hand")
                self.assertEqual(stack_context["room_cast_face_index"], 0)
                self.assertEqual(stack_context["room_cast_door_number"], 1)
                self.assertEqual(
                    stack_context["final_paid_cost"]["generic"], 3)
                self.assertEqual(stack_context["final_paid_cost"]["W"], 1)
                self.assertTrue(forbidden_stack_keys.isdisjoint(stack_context))
                self.assertNotIn(room_id, controller["hand"])
                self.assertTrue(all(
                    land in controller["tapped_permanents"] for land in lands))

                self._resolve_public_spell(
                    handler, f"forged context action {action}")
                self.assertIn(room_id, controller["battlefield"])
                self.assertTrue(room.door1["unlocked"])
                self.assertFalse(room.door2["unlocked"])

    def test_color_cost_reduction_uses_the_announced_room_face_colors(self):
        for face_name, land_name, land_count, expected_action in (
                ("Roaring Furnace", "Mountain", 1, None),
                ("Steaming Sauna", "Island", 4, 446)):
            with self.subTest(face=face_name):
                game_state, handler, controller, _ = self._state(
                    44200 + land_count)
                inject_into_zone(
                    game_state, controller, {
                        "name": "Blue Face Cost Reducer",
                        "mana_cost": "{2}",
                        "cmc": 2,
                        "type_line": "Enchantment",
                        "oracle_text": (
                            "Blue spells you cast cost {1} less to cast."),
                        "keywords": [],
                        "color_identity": [],
                    }, "battlefield")
                room_id = inject_real_card(
                    game_state, controller, self.ROARING, "hand")
                for _ in range(land_count):
                    inject_real_card(
                        game_state, controller, land_name, "battlefield")

                mask = handler.generate_valid_actions()
                if expected_action is None:
                    self.assertFalse(mask[20])
                    self.assertFalse(mask[445])
                    self.assertFalse(mask[446])
                else:
                    self.assertTrue(mask[expected_action])
                    context = handler.action_reasons_with_context[
                        expected_action]["context"]
                    self.assertEqual(context["card_id"], room_id)
                    self.assertEqual(context["room_cast_face_index"], 1)
                    self.assertEqual(
                        context["room_cast_face_colors"], [0, 1, 0, 0, 0])

    def test_cost_modifier_on_locked_room_face_stays_inactive_until_unlock(self):
        game_state, handler, controller, _ = self._state(44210)
        modifier_id = inject_into_zone(
            game_state, controller, {
                "name": "Quiet Lobby // Discount Loft",
                "layout": "split",
                "mana_cost": "",
                "cmc": 2,
                "type_line": "Enchantment — Room",
                "oracle_text": "",
                "keywords": [],
                "color_identity": ["W"],
                "card_faces": [
                    {
                        "name": "Quiet Lobby",
                        "mana_cost": "{W}",
                        "type_line": "Enchantment — Room",
                        "oracle_text": "",
                    },
                    {
                        "name": "Discount Loft",
                        "mana_cost": "{W}",
                        "type_line": "Enchantment — Room",
                        "oracle_text": (
                            "Blue spells you cast cost {1} less to cast."),
                    },
                ],
            }, "battlefield")
        modifier = game_state._safe_get_card(modifier_id)
        modifier.door1["unlocked"] = True
        modifier.door2["unlocked"] = False
        spell_id = inject_into_zone(
            game_state, controller, {
                "name": "Locked Door Cost Probe",
                "mana_cost": "{1}{U}",
                "cmc": 2,
                "type_line": "Creature — Bird",
                "oracle_text": "Flying",
                "keywords": ["flying"],
                "color_identity": ["U"],
                "power": "1",
                "toughness": "1",
            }, "hand")
        inject_real_card(
            game_state, controller, "Island", "battlefield")

        locked_mask = handler.generate_valid_actions()
        self.assertFalse(locked_mask[20])
        self.assertNotIn(
            "blue spells", game_state._active_permanent_rules_text(
                modifier).lower())

        modifier.door2["unlocked"] = True
        unlocked_mask = handler.generate_valid_actions()
        self.assertTrue(unlocked_mask[20])
        self.assertEqual(
            handler.action_reasons_with_context[20]["context"]["card_id"],
            spell_id)
        self.assertIn(
            "blue spells", game_state._active_permanent_rules_text(
                modifier).lower())

    def test_prop_room_adds_one_creature_only_opponent_untap_pass(self):
        game_state, _, controller, opponent = self._state(44211)
        room_id = inject_real_card(
            game_state, controller, self.STATIC_ROOM, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False

        def permanent(name, owner, type_line):
            return inject_into_zone(
                game_state, owner, {
                    "name": name,
                    "mana_cost": "{1}",
                    "cmc": 1,
                    "type_line": type_line,
                    "oracle_text": "",
                    "keywords": [],
                    "color_identity": [],
                    "power": "1" if "Creature" in type_line else None,
                    "toughness": "1" if "Creature" in type_line else None,
                }, "battlefield")

        own_creature = permanent(
            "Prop Room Controller Creature", controller,
            "Creature - Scout")
        own_artifact = permanent(
            "Prop Room Controller Artifact", controller, "Artifact")
        opposing_creature = permanent(
            "Prop Room Active Creature", opponent, "Creature - Scout")
        opposing_artifact = permanent(
            "Prop Room Active Artifact", opponent, "Artifact")

        controller["tapped_permanents"].update(
            {own_creature, own_artifact})
        opponent["tapped_permanents"].update(
            {opposing_creature, opposing_artifact})
        game_state._untap_phase(opponent)
        self.assertEqual(
            controller["tapped_permanents"],
            {own_creature, own_artifact})
        self.assertEqual(opponent["tapped_permanents"], set())

        room.door2["unlocked"] = True
        controller["tapped_permanents"].update(
            {own_creature, own_artifact})
        opponent["tapped_permanents"].update(
            {opposing_creature, opposing_artifact})
        game_state._untap_phase(opponent)
        self.assertEqual(
            controller["tapped_permanents"], {own_artifact})
        self.assertEqual(opponent["tapped_permanents"], set())

        # The Room says "each other player's" step. On its controller's own
        # step the normal untap instruction must run exactly once; two stun
        # counters make a duplicate attempt observable.
        own_card = game_state._safe_get_card(own_creature)
        own_card.counters["stun"] = 2
        controller["tapped_permanents"].update(
            {own_creature, own_artifact})
        opponent["tapped_permanents"].update(
            {opposing_creature, opposing_artifact})
        game_state._untap_phase(controller)
        self.assertEqual(own_card.counters.get("stun"), 1)
        self.assertEqual(
            controller["tapped_permanents"], {own_creature})
        self.assertEqual(
            opponent["tapped_permanents"],
            {opposing_creature, opposing_artifact})

    def test_porcelain_gallery_live_scope_and_count_obey_door_two(self):
        game_state, _, controller, opponent = self._state(44212)
        room_id = inject_real_card(
            game_state, controller, self.GALLERY_ROOM, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False

        def creature(name, owner, power, toughness):
            return inject_into_zone(
                game_state, owner, {
                    "name": name,
                    "mana_cost": "{1}",
                    "cmc": 1,
                    "type_line": "Creature - Scout",
                    "oracle_text": "",
                    "keywords": [],
                    "color_identity": [],
                    "power": str(power),
                    "toughness": str(toughness),
                }, "battlefield")

        first_id = creature("Gallery First Creature", controller, 1, 2)
        second_id = creature("Gallery Second Creature", controller, 3, 4)
        opposing_id = creature("Gallery Opposing Creature", opponent, 5, 6)
        first = game_state._safe_get_card(first_id)
        second = game_state._safe_get_card(second_id)
        opposing = game_state._safe_get_card(opposing_id)

        gallery_abilities = [
            ability
            for ability in game_state.ability_handler.registered_abilities.get(
                room_id, [])
            if (isinstance(ability, StaticAbility)
                and getattr(ability, "room_door_number", None) == 2)
        ]
        self.assertEqual(len(gallery_abilities), 1)
        self.assertIn(
            "base power and toughness each equal",
            gallery_abilities[0].effect_text.lower())

        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()
        self.assertEqual((first.power, first.toughness), (1, 2))
        self.assertEqual((second.power, second.toughness), (3, 4))
        self.assertEqual((opposing.power, opposing.toughness), (5, 6))

        room.door2["unlocked"] = True
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()
        self.assertEqual((first.power, first.toughness), (2, 2))
        self.assertEqual((second.power, second.toughness), (2, 2))
        self.assertEqual((opposing.power, opposing.toughness), (5, 6))

        third_id = creature("Gallery Third Creature", controller, 7, 8)
        third = game_state._safe_get_card(third_id)
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()
        self.assertEqual((first.power, first.toughness), (3, 3))
        self.assertEqual((second.power, second.toughness), (3, 3))
        self.assertEqual((third.power, third.toughness), (3, 3))
        self.assertEqual((opposing.power, opposing.toughness), (5, 6))

        room.door2["unlocked"] = False
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()
        self.assertEqual((first.power, first.toughness), (1, 2))
        self.assertEqual((second.power, second.toughness), (3, 4))
        self.assertEqual((third.power, third.toughness), (7, 8))
        self.assertEqual((opposing.power, opposing.toughness), (5, 6))


if __name__ == "__main__":
    unittest.main()
