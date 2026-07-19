"""Public-cast regressions for Sear's printed target classes."""

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


class SearTargetRegressionTest(unittest.TestCase):
    def _state(self, seed: int):
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
        game_state.pending_spell_context = None
        for player in (controller, opponent):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["damage_counters"] = {}
            player["loyalty_counters"] = {}
            player["mana_pool"] = {
                "W": 0, "U": 0, "B": 0,
                "R": 0, "G": 0, "C": 0,
            }
        game_state._last_card_locations = {}
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _public(self, handler, action: int, message: str):
        game_state = handler.game_state
        priority = game_state.priority_player or game_state.p1
        game_state.priority_player = priority
        game_state.agent_is_p1 = priority is game_state.p1
        mask = handler.generate_valid_actions()
        self.assertTrue(
            mask[action],
            f"{message}: action {action} absent; valid="
            f"{[index for index, allowed in enumerate(mask) if allowed]}",
        )
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(action)
        self.assertFalse(info.get("execution_failed"), (message, info))
        self.assertFalse(info.get("critical_error"), (message, info))
        self.assertFalse(info.get("invalid_action"), (message, info))

    def _select_target(self, handler, controller, target_id):
        game_state = handler.game_state
        self.assertIsNotNone(game_state.targeting_context)
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(target_id, candidates)
        absolute_index = candidates.index(target_id)
        for _ in range(absolute_index // 10):
            self._public(handler, 479, "page Sear targets")
        self._public(
            handler, 274 + absolute_index % 10,
            f"select Sear target {target_id}")

    def test_real_sear_resolves_against_each_printed_target_class(self):
        cases = (
            (32001, "creature", "Shivan Dragon"),
            (32002, "planeswalker", "Liliana, Dreadhorde General"),
        )
        for seed, target_class, target_name in cases:
            with self.subTest(target_class=target_class):
                game_state, handler, controller, opponent = self._state(seed)
                sear_id = inject_real_card(
                    game_state, controller, "Sear", "hand")
                lands = [
                    inject_real_card(
                        game_state, controller, "Mountain", "battlefield")
                    for _ in range(2)
                ]
                target_id = inject_real_card(
                    game_state, opponent, target_name, "battlefield")
                initial_loyalty = opponent["loyalty_counters"].get(target_id)

                with self.assertNoLogs(level=logging.WARNING):
                    self._public(handler, 20, "cast Sear")
                    self._select_target(handler, controller, target_id)
                    self.assertEqual(game_state.stack[-1][1], sear_id)
                    self._public(handler, 11, "Sear controller passes")
                    self._public(handler, 11, "Sear opponent passes")

                self.assertIn(sear_id, controller["graveyard"])
                self.assertEqual(
                    len(set(lands).intersection(
                        controller["tapped_permanents"])),
                    2,
                )
                if target_class == "creature":
                    self.assertEqual(
                        opponent["damage_counters"].get(target_id), 4)
                else:
                    self.assertIsNotNone(initial_loyalty)
                    self.assertEqual(
                        opponent["loyalty_counters"].get(target_id),
                        initial_loyalty - 4,
                    )


if __name__ == "__main__":
    unittest.main()
