"""Fractured Realm trigger-doubling regressions from the Room/Exhaust probe.

"If a triggered ability of a permanent you control triggers, that ability
triggers an additional time." is an event modification, not a CR 613 layer
effect: the July 19 probe showed it misrouted to the layer system, which
warned and dropped it. These scenarios pin the doubling through the
production trigger pipeline, gated live on the Room's door state.
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


class FracturedRealmTriggerDoublingTest(unittest.TestCase):
    MIRROR_ROOM = "Mirror Room // Fractured Realm"
    LEECH = "Balemurk Leech"

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
        return info

    def _resolve_all_triggers(self, handler, game_state, label: str):
        if game_state.ability_handler.active_triggers:
            game_state.ability_handler.process_triggered_abilities()
        guard = 0
        while (game_state.choice_context
               and game_state.choice_context.get("type") == "order_triggers"):
            self._public(handler, 353, f"{label}: order identical triggers")
            guard += 1
            self.assertLess(guard, 8, "trigger ordering did not settle")
        guard = 0
        while game_state.stack:
            self._public(handler, 11, f"{label}: controller passes")
            self._public(handler, 11, f"{label}: opponent passes")
            guard += 1
            self.assertLess(guard, 8, "stack did not empty")

    def _fixture(self, seed: int, *, realm_unlocked: bool):
        game_state, handler, controller, opponent = self._state(seed)
        with self.assertNoLogs(level=logging.WARNING):
            room_id = inject_real_card(
                game_state, controller, self.MIRROR_ROOM, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = False
        room.door2["unlocked"] = realm_unlocked
        inject_real_card(game_state, controller, self.LEECH, "battlefield")
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        return game_state, handler, controller, opponent

    def _enter_enchantment(self, game_state, controller):
        return inject_into_zone(
            game_state, controller, {
                "name": "Realm Test Enchantment",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Enchantment",
                "oracle_text": "",
                "keywords": [],
                "color_identity": [],
            }, "battlefield")

    def test_unlocked_fractured_realm_doubles_a_controlled_trigger(self):
        game_state, handler, controller, opponent = self._fixture(
            48101, realm_unlocked=True)
        before = opponent["life"]

        with self.assertNoLogs(level=logging.WARNING):
            self._enter_enchantment(game_state, controller)
            self._resolve_all_triggers(
                handler, game_state, "doubled Leech trigger")

        self.assertEqual(
            opponent["life"], before - 2,
            "an unlocked Fractured Realm must make the Eerie trigger "
            "resolve twice")
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)

    def test_locked_fractured_realm_is_a_close_non_event(self):
        game_state, handler, controller, opponent = self._fixture(
            48102, realm_unlocked=False)
        before = opponent["life"]

        with self.assertNoLogs(level=logging.WARNING):
            self._enter_enchantment(game_state, controller)
            self._resolve_all_triggers(
                handler, game_state, "single Leech trigger")

        self.assertEqual(
            opponent["life"], before - 1,
            "a locked Fractured Realm must not double anything")
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)


if __name__ == "__main__":
    unittest.main()
