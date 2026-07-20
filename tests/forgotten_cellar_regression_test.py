"""Forgotten Cellar (Walk-In Closet // Forgotten Cellar) unlock regressions.

The July 19 room-exhaust probe failed both halves of the unlock trigger:
"you may cast spells from your graveyard this turn" and "if a card would be
put into your graveyard from anywhere this turn, exile it instead" were
unimplemented generic AbilityEffects. These scenarios pin the turn-scoped
cast permission through the public graveyard-cast action and the turn-scoped
replacement through real zone movement, including its your-graveyard-only
scope.
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


class ForgottenCellarRegressionTest(unittest.TestCase):
    ROOM = "Walk-In Closet // Forgotten Cellar"

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

    def test_unlock_grants_turn_cast_and_exiles_your_graveyard_bound_cards(self):
        game_state, handler, controller, opponent = self._state(50101)
        room_id = inject_real_card(
            game_state, controller, self.ROOM, "battlefield")
        room = game_state._safe_get_card(room_id)
        room.door1["unlocked"] = True
        room.door2["unlocked"] = False
        sorcery_id = inject_into_zone(
            game_state, controller, {
                "name": "Cellar Graveyard Sorcery",
                "mana_cost": "{G}",
                "cmc": 1,
                "type_line": "Sorcery",
                "oracle_text": "Draw a card.",
                "keywords": [],
                "color_identity": ["G"],
            }, "graveyard")
        library_card = inject_into_zone(
            game_state, controller, {
                "name": "Cellar Library Card",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Sorcery",
                "oracle_text": "",
                "keywords": [],
                "color_identity": [],
            }, "library")
        opposing_card = inject_into_zone(
            game_state, opponent, {
                "name": "Cellar Opposing Card",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Sorcery",
                "oracle_text": "",
                "keywords": [],
                "color_identity": [],
            }, "library")
        lands = [
            inject_real_card(
                game_state, controller, "Forest", "battlefield")
            for _ in range(6)
        ]
        game_state.ability_handler.active_triggers = []

        # Close non-event: without the unlock, the graveyard sorcery is not
        # publicly castable.
        locked_mask = handler.generate_valid_actions()
        self.assertFalse(locked_mask[472])

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 248, "unlock Forgotten Cellar")
            if game_state.ability_handler.active_triggers:
                game_state.ability_handler.process_triggered_abilities()
            self.assertEqual(len(game_state.stack), 1)
            self._public(handler, 11, "Cellar trigger: controller passes")
            self._public(handler, 11, "Cellar trigger: opponent passes")
            self.assertIsNone(game_state.choice_context)
            self.assertTrue(room.door2["unlocked"])

            # The env loop returns to the main phase once the stack empties;
            # the scripted harness parks there explicitly.
            game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
            game_state.priority_player = controller
            game_state.priority_pass_count = 0

            # The graveyard sorcery is now castable through the public
            # graveyard-cast action and resolves normally...
            self._public(handler, 472, "cast the graveyard sorcery")
            self.assertEqual(game_state.stack[-1][0:2], ("SPELL", sorcery_id))
            self._public(handler, 11, "sorcery: controller passes")
            self._public(handler, 11, "sorcery: opponent passes")

        # ...and on resolution it is exiled instead of returning to the
        # controller's graveyard.
        self.assertIn(sorcery_id, controller["exile"])
        self.assertNotIn(sorcery_id, controller["graveyard"])
        self.assertIn(library_card, controller["hand"])

        # The replacement covers "from anywhere" for the controller's
        # graveyard, but never the opponent's graveyard.
        self.assertTrue(game_state.move_card(
            library_card, controller, "hand", controller, "graveyard",
            cause="cellar_regression_probe"))
        self.assertIn(library_card, controller["exile"])
        self.assertNotIn(library_card, controller["graveyard"])
        self.assertTrue(game_state.move_card(
            opposing_card, opponent, "library", opponent, "graveyard",
            cause="cellar_regression_probe"))
        self.assertIn(opposing_card, opponent["graveyard"])
        self.assertNotIn(opposing_card, opponent["exile"])

        self.assertEqual(game_state.stack, [])
        self.assertTrue(all(
            land in controller["tapped_permanents"] for land in lands))
        self.assertEqual(sum(controller["mana_pool"].values()), 0)


if __name__ == "__main__":
    unittest.main()
