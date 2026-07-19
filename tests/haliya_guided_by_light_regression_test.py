"""Exact end-step threshold regressions for Haliya, Guided by Light."""

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
    inject_card,
    inject_into_zone,
    inject_real_card,
)


logging.disable(logging.CRITICAL)


class HaliyaGuidedByLightRegressionTest(unittest.TestCase):
    def _state(self, seed: int, *, turn: int = 1,
               haliya_zone: str = "battlefield"):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = turn
        game_state.agent_is_p1 = turn % 2 == 1
        game_state.phase = game_state.PHASE_END_STEP
        game_state.previous_priority_phase = None
        game_state.priority_player = (
            controller if turn % 2 == 1 else opponent)
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.ability_handler.active_triggers = []
        game_state.life_gained_this_turn = {}
        for player in (controller, opponent):
            player["gained_life_this_turn"] = False

        haliya_id = inject_real_card(
            game_state, controller, "Haliya, Guided by Light", haliya_zone)
        # Isolate the end-step ability from Haliya's own entry trigger.
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        return game_state, controller, opponent, haliya_id

    @staticmethod
    def _blank_permanent(name: str, type_line: str) -> dict:
        return {
            "name": name,
            "mana_cost": "{1}",
            "cmc": 1,
            "type_line": type_line,
            "oracle_text": "",
            "colors": [0, 0, 0, 0, 0],
            "power": 1 if "Creature" in type_line else 0,
            "toughness": 1 if "Creature" in type_line else 0,
        }

    @staticmethod
    def _entry_triggers(game_state, haliya_id):
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == haliya_id
            and "gain 1 life" in entry[0].effect
        ]

    def test_entry_watcher_rejects_opponent_and_wrong_type_events(self):
        game_state, controller, opponent, haliya_id = self._state(34106)
        fixtures = (
            (opponent, "Opponent Creature", "Creature"),
            (opponent, "Opponent Artifact", "Artifact"),
            (opponent, "Opponent Enchantment", "Enchantment"),
            (controller, "Controlled Enchantment", "Enchantment"),
        )

        for owner, name, type_line in fixtures:
            with self.subTest(name=name):
                game_state.ability_handler.active_triggers = []
                inject_into_zone(
                    game_state, owner,
                    self._blank_permanent(name, type_line), "battlefield")
                self.assertEqual(
                    self._entry_triggers(game_state, haliya_id), [])

    def test_self_creature_and_controlled_artifact_each_queue_once(self):
        game_state, controller, _, haliya_id = self._state(
            34107, haliya_zone="hand")
        self.assertTrue(game_state.move_card(
            haliya_id, controller, "hand", controller, "battlefield"))
        self.assertEqual(
            len(self._entry_triggers(game_state, haliya_id)), 1)

        for name, type_line in (
                ("Another Controlled Creature", "Creature"),
                ("Controlled Artifact", "Artifact")):
            with self.subTest(name=name):
                game_state.ability_handler.active_triggers = []
                inject_into_zone(
                    game_state, controller,
                    self._blank_permanent(name, type_line), "battlefield")
                self.assertEqual(
                    len(self._entry_triggers(game_state, haliya_id)), 1)

    def _assert_end_step_does_not_queue_or_draw(
            self, game_state, controller):
        hand_before = list(controller["hand"])
        library_before = list(controller["library"])

        game_state._handle_beginning_of_phase_triggers()

        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(game_state.stack, [])
        self.assertEqual(controller["hand"], hand_before)
        self.assertEqual(controller["library"], library_before)

    def test_no_gain_two_life_and_opponent_gain_do_not_queue_or_draw(self):
        fixtures = (
            (34101, None),
            (34102, "controller_two"),
            (34103, "opponent_three"),
        )
        for seed, setup in fixtures:
            with self.subTest(setup=setup or "no_gain"):
                game_state, controller, opponent, _ = self._state(seed)
                if setup == "controller_two":
                    self.assertEqual(game_state.gain_life(controller, 2), 2)
                elif setup == "opponent_three":
                    self.assertEqual(game_state.gain_life(opponent, 3), 3)
                self._assert_end_step_does_not_queue_or_draw(
                    game_state, controller)

    def test_controller_gain_does_not_fire_on_opponents_end_step(self):
        game_state, controller, _, _ = self._state(34104, turn=2)
        self.assertEqual(game_state.gain_life(controller, 3), 3)

        self._assert_end_step_does_not_queue_or_draw(game_state, controller)

    def test_three_controller_life_queues_once_and_resolves_one_draw(self):
        game_state, controller, _, haliya_id = self._state(34105)
        controller["hand"] = []
        controller["library"] = []
        drawn_id = inject_card(game_state, {
            "name": "Known Haliya Draw",
            "mana_cost": "{1}",
            "cmc": 1,
            "type_line": "Sorcery",
            "oracle_text": "",
            "colors": [0, 0, 0, 0, 0],
        })
        controller["library"] = [drawn_id]
        game_state._last_card_locations[drawn_id] = (controller, "library")

        # The threshold is cumulative across distinct life-gain events.
        self.assertEqual(game_state.gain_life(controller, 1), 1)
        self.assertEqual(game_state.gain_life(controller, 2), 2)

        game_state._handle_beginning_of_phase_triggers()

        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(len(game_state.stack), 1)
        trigger = game_state.stack[-1]
        self.assertEqual(trigger[0:2], ("TRIGGER", haliya_id))
        self.assertIn("draw a card", trigger[3]["ability"].effect)

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.stack, [])
        self.assertEqual(controller["hand"], [drawn_id])
        self.assertEqual(controller["library"], [])


if __name__ == "__main__":
    unittest.main()
