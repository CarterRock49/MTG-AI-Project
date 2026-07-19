"""Exact temporary defender-bypass regressions for Stalked Researcher."""

from __future__ import annotations

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
    DefenderAttackPermissionEffect,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402


class StalkedResearcherRegressionTest(unittest.TestCase):
    RESEARCHER = "Stalked Researcher"
    ROOM = "Dazzling Theater // Prop Room"

    def _state(self, seed: int, turn: int = 1):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = turn
        game_state.agent_is_p1 = game_state._get_active_player() is game_state.p1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = game_state._get_active_player()
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.pending_spell_context = None
        game_state.defender_attack_permissions.clear()
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

    def _researcher(self, game_state, controller):
        source_id = inject_real_card(
            game_state, controller, self.RESEARCHER, "battlefield")
        controller["entered_battlefield_this_turn"].discard(source_id)
        game_state.ability_handler.active_triggers = []
        return source_id

    @staticmethod
    def _eerie_entries(game_state, source_id):
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
        ]

    def _resolve_one_eerie(self, game_state, source_id, event_type):
        queued = self._eerie_entries(game_state, source_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2].get("event_type"), event_type)
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0:2], ("TRIGGER", source_id))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.stack, [])

    def _assert_attack_legal(
            self, game_state, handler, controller, source_id, expected):
        game_state.turn = 1
        game_state.agent_is_p1 = controller is game_state.p1
        game_state.phase = game_state.PHASE_DECLARE_ATTACKERS
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.current_attackers = []
        battlefield_index = controller["battlefield"].index(source_id)
        self.assertEqual(
            handler.combat_handler.is_valid_attacker(source_id), expected)
        self.assertEqual(handler.is_valid_attacker(source_id), expected)
        mask = handler.generate_valid_actions()
        self.assertEqual(bool(mask[28 + battlefield_index]), expected)

    def test_printed_eerie_text_is_not_an_always_on_defender_bypass(self):
        game_state, handler, controller, _ = self._state(44301)
        source_id = self._researcher(game_state, controller)
        source = game_state._safe_get_card(source_id)
        self.assertIn(
            "can attack this turn as though it didn't have defender",
            source.oracle_text.lower())
        self.assertFalse(game_state.has_defender_attack_permission(source_id))
        self._assert_attack_legal(
            game_state, handler, controller, source_id, False)

        parsed = EffectFactory.create_effects(
            "This creature can attack this turn as though it didn't have "
            "defender.")
        self.assertEqual(len(parsed), 1)
        self.assertIsInstance(parsed[0], DefenderAttackPermissionEffect)

    def test_controlled_enchantment_entry_grants_source_bound_permission(self):
        game_state, handler, controller, _ = self._state(44302)
        source_id = self._researcher(game_state, controller)
        self._assert_attack_legal(
            game_state, handler, controller, source_id, False)
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT

        inject_into_zone(game_state, controller, {
            "name": "Researcher Eerie Enchantment",
            "mana_cost": "{U}",
            "type_line": "Enchantment",
            "oracle_text": "",
        }, "battlefield")
        self._resolve_one_eerie(
            game_state, source_id, "ENTERS_BATTLEFIELD")

        source = game_state._safe_get_card(source_id)
        generation = int(source._zone_change_generation)
        self.assertEqual(
            game_state.defender_attack_permissions[source_id], generation)
        self.assertTrue(game_state.has_defender_attack_permission(source_id))
        self._assert_attack_legal(
            game_state, handler, controller, source_id, True)

        cloned = game_state.clone()
        self.assertEqual(
            cloned.defender_attack_permissions,
            game_state.defender_attack_permissions)
        self.assertIsNot(
            cloned.defender_attack_permissions,
            game_state.defender_attack_permissions)
        cloned.defender_attack_permissions.clear()
        self.assertTrue(game_state.has_defender_attack_permission(source_id))

    def test_fully_unlocking_controlled_room_grants_permission(self):
        game_state, handler, controller, _ = self._state(44303)
        room_id = inject_real_card(
            game_state, controller, self.ROOM, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False
        source_id = self._researcher(game_state, controller)
        controller["mana_pool"]["W"] = 3

        self.assertTrue(game_state.ability_handler.handle_unlock_door(
            controller["battlefield"].index(room_id),
            controller=controller,
            room_id=room_id,
            door_number=2,
        ))
        self._resolve_one_eerie(
            game_state, source_id, "ROOM_FULLY_UNLOCKED")
        self.assertTrue(game_state.has_defender_attack_permission(source_id))
        self._assert_attack_legal(
            game_state, handler, controller, source_id, True)

    def test_opponent_eerie_events_do_not_grant_permission(self):
        game_state, handler, controller, opponent = self._state(44304, turn=2)
        source_id = self._researcher(game_state, controller)
        room_id = inject_real_card(
            game_state, opponent, self.ROOM, "battlefield")
        self.assertEqual(
            self._eerie_entries(game_state, source_id), [],
            "an opponent's enchantment entry must not trigger Eerie")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False
        game_state.ability_handler.active_triggers = []
        opponent["mana_pool"]["W"] = 3

        self.assertTrue(game_state.ability_handler.handle_unlock_door(
            opponent["battlefield"].index(room_id),
            controller=opponent,
            room_id=room_id,
            door_number=2,
        ))
        self.assertEqual(self._eerie_entries(game_state, source_id), [])
        self.assertFalse(game_state.has_defender_attack_permission(source_id))
        self._assert_attack_legal(
            game_state, handler, controller, source_id, False)

    def test_permission_clears_on_leave_and_old_trigger_cannot_follow(self):
        game_state, handler, controller, _ = self._state(44305)
        source_id = self._researcher(game_state, controller)
        inject_into_zone(game_state, controller, {
            "name": "First Researcher Eerie Enchantment",
            "mana_cost": "{U}",
            "type_line": "Enchantment",
            "oracle_text": "",
        }, "battlefield")
        self._resolve_one_eerie(
            game_state, source_id, "ENTERS_BATTLEFIELD")
        self.assertTrue(game_state.has_defender_attack_permission(source_id))

        self.assertTrue(game_state.move_card(
            source_id, controller, "battlefield",
            controller, "graveyard", cause="destroy"))
        self.assertNotIn(source_id, game_state.defender_attack_permissions)
        self.assertFalse(game_state.has_defender_attack_permission(source_id))
        self.assertTrue(game_state.move_card(
            source_id, controller, "graveyard",
            controller, "battlefield", cause="reanimate"))
        controller["entered_battlefield_this_turn"].discard(source_id)
        game_state.ability_handler.active_triggers = []

        inject_into_zone(game_state, controller, {
            "name": "Second Researcher Eerie Enchantment",
            "mana_cost": "{U}",
            "type_line": "Enchantment",
            "oracle_text": "",
        }, "battlefield")
        queued = self._eerie_entries(game_state, source_id)
        self.assertEqual(len(queued), 1)
        queued_generation = queued[0][2]["source_zone_generation"]
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.move_card(
            source_id, controller, "battlefield",
            controller, "graveyard", cause="exile_before_resolution"))
        self.assertTrue(game_state.move_card(
            source_id, controller, "graveyard",
            controller, "battlefield", cause="return_before_resolution"))
        controller["entered_battlefield_this_turn"].discard(source_id)
        self.assertNotEqual(
            queued_generation,
            game_state._safe_get_card(source_id)._zone_change_generation)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertFalse(game_state.has_defender_attack_permission(source_id))
        self._assert_attack_legal(
            game_state, handler, controller, source_id, False)

    def test_permission_expires_at_cleanup_and_turn_reset(self):
        game_state, handler, controller, _ = self._state(44306)
        source_id = self._researcher(game_state, controller)
        source = game_state._safe_get_card(source_id)
        game_state.defender_attack_permissions[source_id] = int(
            source._zone_change_generation)
        self.assertTrue(game_state.has_defender_attack_permission(source_id))

        self.assertFalse(game_state._cleanup_step_actions(
            controller, discard_to_max=False))
        self.assertFalse(game_state.has_defender_attack_permission(source_id))
        self._assert_attack_legal(
            game_state, handler, controller, source_id, False)

        game_state.defender_attack_permissions[source_id] = int(
            source._zone_change_generation)
        game_state._reset_turn_tracking_variables()
        self.assertFalse(game_state.has_defender_attack_permission(source_id))


if __name__ == "__main__":
    unittest.main()
