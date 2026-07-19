"""Exact trigger-lifecycle regressions for Optimistic Scavenger's Eerie ability."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_real_card  # noqa: E402


class OptimisticScavengerTriggerRegressionTest(unittest.TestCase):
    ROOM_NAME = "Dazzling Theater // Prop Room"

    def _state(self, seed: int, *, turn: int = 1):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = turn % 2 == 1
        game_state.turn = turn
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = (
            controller if turn % 2 == 1 else opponent)
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
            player["damage_counters"] = {}
            player["mana_pool"] = {
                "W": 0, "U": 0, "B": 0,
                "R": 0, "G": 0, "C": 0,
            }
        game_state._last_card_locations = {}
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _scavenger_triggers(self, game_state, scavenger_id):
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == scavenger_id
        ]

    def _unlock(self, game_state, player, room_id, door_number, mana):
        player["mana_pool"].update(mana)
        room_index = player["battlefield"].index(room_id)
        self.assertTrue(game_state.ability_handler.handle_unlock_door(
            room_index,
            controller=player,
            room_id=room_id,
            door_number=door_number,
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

    def _select_target(self, handler, player, target_id):
        context = handler.game_state.targeting_context
        self.assertIsNotNone(context)
        candidates = handler._get_target_selection_candidates(player, context)
        self.assertIn(target_id, candidates)
        absolute_index = candidates.index(target_id)
        for _ in range(absolute_index // 10):
            self._public(handler, 479, "page Eerie targets")
        self._public(
            handler, 274 + absolute_index % 10,
            "target Optimistic Scavenger with Eerie")

    def test_controlled_room_entry_triggers_once_but_first_door_does_not(self):
        game_state, _, controller, _ = self._state(33101)
        scavenger_id = inject_real_card(
            game_state, controller, "Optimistic Scavenger", "battlefield")

        # A Room entering under its controller is an enchantment entering, so
        # this is the first (and only) Eerie event in the staged lifecycle.
        room_id = inject_real_card(
            game_state, controller, self.ROOM_NAME, "battlefield")
        entry_triggers = self._scavenger_triggers(game_state, scavenger_id)
        self.assertEqual(len(entry_triggers), 1)
        self.assertEqual(entry_triggers[0][2]["event_type"],
                         "ENTERS_BATTLEFIELD")

        # Unlocking only the first door does not fully unlock the Room. The
        # generic DOOR_UNLOCKED event must not re-match "fully unlock a Room."
        game_state.ability_handler.active_triggers = []
        self._unlock(game_state, controller, room_id, 1, {"W": 4})
        self.assertTrue(game_state._safe_get_card(room_id).door1["unlocked"])
        self.assertFalse(game_state._safe_get_card(room_id).door2["unlocked"])
        self.assertEqual(
            self._scavenger_triggers(game_state, scavenger_id), [])

    def test_full_room_unlock_queues_and_resolves_eerie_exactly_once(self):
        game_state, handler, controller, _ = self._state(33102)
        scavenger_id = inject_real_card(
            game_state, controller, "Optimistic Scavenger", "battlefield")
        room_id = inject_real_card(
            game_state, controller, self.ROOM_NAME, "battlefield")
        room_card = game_state._safe_get_card(room_id)
        room_card.door1["unlocked"] = True
        game_state.ability_handler.active_triggers = []

        with self.assertNoLogs(level=logging.WARNING):
            self._unlock(game_state, controller, room_id, 2, {"W": 3})

            queued = self._scavenger_triggers(game_state, scavenger_id)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][2]["event_type"],
                             "ROOM_FULLY_UNLOCKED")

            game_state.ability_handler.process_triggered_abilities()
            self.assertEqual(len(game_state.stack), 1)
            self.assertEqual(game_state.stack[-1][0:2],
                             ("TRIGGER", scavenger_id))
            self._select_target(handler, controller, scavenger_id)
            self._public(handler, 11, "Scavenger controller passes")
            self._public(handler, 11, "Scavenger opponent passes")

        self.assertEqual(game_state.stack, [])
        self.assertEqual(
            game_state._safe_get_card(scavenger_id).counters.get("+1/+1", 0),
            1,
        )

    def test_opponent_fully_unlocking_a_room_does_not_trigger_scavenger(self):
        game_state, _, controller, opponent = self._state(33103, turn=2)
        scavenger_id = inject_real_card(
            game_state, controller, "Optimistic Scavenger", "battlefield")
        room_id = inject_real_card(
            game_state, opponent, self.ROOM_NAME, "battlefield")
        game_state._safe_get_card(room_id).door1["unlocked"] = True
        game_state.ability_handler.active_triggers = []

        self._unlock(game_state, opponent, room_id, 2, {"W": 3})

        self.assertEqual(
            self._scavenger_triggers(game_state, scavenger_id), [])


if __name__ == "__main__":
    unittest.main()
