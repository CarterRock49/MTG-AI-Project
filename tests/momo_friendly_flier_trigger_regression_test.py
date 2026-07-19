"""Exact ETB-trigger regressions for Momo, Friendly Flier."""

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
    inject_into_zone,
    inject_real_card,
)


logging.disable(logging.CRITICAL)


class MomoFriendlyFlierTriggerRegressionTest(unittest.TestCase):
    @staticmethod
    def _creature(name: str, *, flying: bool = False) -> dict:
        return {
            "name": name,
            "mana_cost": "{1}{W}",
            "cmc": 2,
            "type_line": "Creature - Bird" if flying else "Creature - Bear",
            "oracle_text": "Flying" if flying else "",
            "keywords": ["Flying"] if flying else [],
            "colors": [1, 0, 0, 0, 0],
            "power": 2,
            "toughness": 2,
        }

    @staticmethod
    def _momo_triggers(game_state, momo_id):
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == momo_id
            and "momo gets +1/+1 until end of turn" in entry[0].effect
        ]

    def _state_with_momo(self, seed: int, *, zone: str = "battlefield"):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = 1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.ability_handler.active_triggers = []
        momo_id = inject_real_card(
            game_state, controller, "Momo, Friendly Flier", zone)
        # A battlefield setup move is not one of the events under test.
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        return game_state, controller, opponent, momo_id

    def test_controlled_flying_creature_queues_once_and_pumps_only_momo(self):
        game_state, controller, _, momo_id = self._state_with_momo(35201)
        flier_id = inject_into_zone(
            game_state, controller,
            self._creature("Controlled Flying Arrival", flying=True), "hand")

        self.assertTrue(game_state.move_card(
            flier_id, controller, "hand", controller, "battlefield"))
        queued = self._momo_triggers(game_state, momo_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], flier_id)
        self.assertEqual(queued[0][2]["event_type"], "ENTERS_BATTLEFIELD")
        self.assertEqual(game_state.stack, [])

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0:2], ("TRIGGER", momo_id))
        self.assertTrue(game_state.resolve_top_of_stack())

        momo = game_state._safe_get_card(momo_id)
        flier = game_state._safe_get_card(flier_id)
        self.assertEqual((momo.power, momo.toughness), (2, 2))
        self.assertEqual((flier.power, flier.toughness), (2, 2))

    def test_controlled_nonflier_opponent_flier_and_momo_self_do_not_queue(self):
        fixtures = (
            (35202, "controlled_nonflier"),
            (35203, "opponent_flier"),
            (35204, "momo_self"),
        )
        for seed, case in fixtures:
            with self.subTest(case=case):
                start_zone = "hand" if case == "momo_self" else "battlefield"
                game_state, controller, opponent, momo_id = \
                    self._state_with_momo(seed, zone=start_zone)
                if case == "momo_self":
                    self.assertTrue(game_state.move_card(
                        momo_id, controller, "hand", controller,
                        "battlefield"))
                else:
                    owner = opponent if case == "opponent_flier" else controller
                    event_id = inject_into_zone(
                        game_state, owner,
                        self._creature(
                            case.replace("_", " ").title(),
                            flying=case == "opponent_flier"),
                        "hand")
                    self.assertTrue(game_state.move_card(
                        event_id, owner, "hand", owner, "battlefield"))

                self.assertEqual(
                    self._momo_triggers(game_state, momo_id), [])
                self.assertEqual(game_state.stack, [])

    def test_generic_controlled_creature_entry_watcher_still_accepts_nonflier(self):
        game_state = fresh(35205)
        controller = game_state.p1
        watcher_id = inject_into_zone(game_state, controller, {
            **self._creature("Generic Entry Watcher"),
            "oracle_text": (
                "Whenever another creature you control enters, you gain 1 life."),
        }, "battlefield")
        game_state.ability_handler.active_triggers = []
        life_before = controller["life"]

        event_id = inject_into_zone(
            game_state, controller,
            self._creature("Ordinary Ground Creature"), "hand")
        self.assertTrue(game_state.move_card(
            event_id, controller, "hand", controller, "battlefield"))
        queued = [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == watcher_id
        ]
        self.assertEqual(len(queued), 1)

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(controller["life"], life_before + 1)


if __name__ == "__main__":
    unittest.main()
