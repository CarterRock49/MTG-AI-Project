"""Regression coverage for Cerebral Download's rules-defined Surveil X."""

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
    inject_card,
    inject_into_zone,
    inject_real_card,
    replace_hand,
)


class CerebralDownloadSurveilXRegressionTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.ability_handler.active_triggers.clear()
        replace_hand(game_state, controller, [])
        for player in (controller, opponent):
            for permanent_id in list(player.get("battlefield", [])):
                self.assertTrue(game_state.move_card(
                    permanent_id, player, "battlefield", player, "library"))
            player["tapped_permanents"] = set()
        cerebral = inject_real_card(
            game_state, controller, "Cerebral Download", "hand")
        controller["mana_pool"] = {
            "W": 0, "U": 1, "B": 0,
            "R": 0, "G": 0, "C": 4,
        }
        return (
            game_state,
            get_env().action_handler,
            controller,
            opponent,
            cerebral,
        )

    @staticmethod
    def _library_cards(game_state, controller, count, prefix):
        cards = []
        for index in range(count):
            card_id = inject_card(game_state, {
                "name": f"{prefix} {index}",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Instant",
                "oracle_text": "",
            })
            cards.append(card_id)
        controller["library"][:0] = cards
        for card_id in cards:
            game_state._last_card_locations[card_id] = (
                controller, "library")
        return cards

    @staticmethod
    def _artifact(game_state, controller, name):
        return inject_into_zone(game_state, controller, {
            "name": name,
            "mana_cost": "{1}",
            "cmc": 1,
            "type_line": "Artifact",
            "oracle_text": "",
        }, "battlefield")

    def test_counts_artifacts_at_resolution_then_draws_three(self):
        (game_state, handler, controller, _,
         cerebral) = self._state(32201)
        top = self._library_cards(
            game_state, controller, 6, "Cerebral Library")
        self._artifact(game_state, controller, "First Cerebral Artifact")

        self.assertTrue(game_state.cast_spell(cerebral, controller))
        # X is defined by the resolving instruction, so this permanent must be
        # included even though it was not present when the spell was cast.
        self._artifact(game_state, controller, "Second Cerebral Artifact")
        self.assertTrue(game_state.resolve_top_of_stack())

        self.assertEqual(game_state.choice_context.get("type"), "surveil")
        self.assertEqual(game_state.choice_context.get("count"), 2)
        self.assertEqual(game_state.choice_context.get("cards"), top[:2])

        self.assertTrue(handler._handle_scry_surveil_choice(
            None, {}, action_index=305)[1])
        self.assertTrue(handler._handle_scry_surveil_choice(
            None, {}, action_index=306)[1])

        self.assertIsNone(game_state.choice_context)
        self.assertIn(top[0], controller["graveyard"])
        self.assertTrue(all(card_id in controller["hand"]
                            for card_id in (top[1], top[2], top[3])))
        self.assertEqual(controller["library"][:2], top[4:6])
        self.assertIn(cerebral, controller["graveyard"])

    def test_zero_artifacts_is_a_no_op_surveil_and_still_draws_three(self):
        (game_state, _, controller, _,
         cerebral) = self._state(32202)
        top = self._library_cards(
            game_state, controller, 3, "Zero Artifact Draw")

        self.assertTrue(game_state.cast_spell(cerebral, controller))
        self.assertTrue(game_state.resolve_top_of_stack())

        self.assertIsNone(game_state.choice_context)
        self.assertTrue(all(card_id in controller["hand"] for card_id in top))
        self.assertIn(cerebral, controller["graveyard"])


if __name__ == "__main__":
    unittest.main()
