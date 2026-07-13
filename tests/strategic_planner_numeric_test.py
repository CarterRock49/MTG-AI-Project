"""Symbolic-stat safety regressions for strategic planner analysis.

Run from the repository root with::

    python tests/strategic_planner_numeric_test.py
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.card import Card  # noqa: E402
from Playersim.game_state import GameState  # noqa: E402
from Playersim.strategic_planner import MTGStrategicPlanner  # noqa: E402
from Playersim.strategic_planner_archetypes import (  # noqa: E402
    _card_number as archetype_card_number,
)
from Playersim.strategic_planner_search import (  # noqa: E402
    _card_number as search_card_number,
)


class StrategicPlannerNumericSafetyTest(unittest.TestCase):
    def _state(self):
        namor = Card({
            "name": "Namor the Sub-Mariner",
            "type_line": "Legendary Creature — Mutant Merfolk Villain",
            "mana_cost": "{1}{U}{U}", "cmc": 3,
            "power": "*", "toughness": "4",
            "oracle_text": (
                "Flying\nNamor's power is equal to the number of Merfolk "
                "you control."),
            "color_identity": ["U"],
        })
        unknown = Card({
            "name": "Unknown Body", "type_line": "Creature — Merfolk",
            "mana_cost": "{U}", "cmc": 1,
            "power": "*", "toughness": "*", "oracle_text": "",
            "color_identity": ["U"],
        })
        game_state = GameState({0: namor, 1: unknown})
        game_state.reset([0], [1], seed=23)
        game_state.mulligan_in_progress = False
        for player in (game_state.p1, game_state.p2):
            for card_id in list(player["hand"]):
                self.assertTrue(game_state.move_card(
                    card_id, player, "hand", player, "battlefield"))
        game_state.agent_is_p1 = True
        return game_state

    def test_numeric_helpers_normalize_symbolic_and_nonfinite_stats(self):
        game_state = self._state()
        namor = game_state._safe_get_card(game_state.p1["battlefield"][0])
        unknown = game_state._safe_get_card(game_state.p2["battlefield"][0])
        self.assertEqual(archetype_card_number(namor, "power"), 1.0)
        self.assertEqual(archetype_card_number(unknown, "power"), 0.0)
        self.assertEqual(search_card_number(unknown, "power"), 0.0)

    def test_planner_analysis_tolerates_symbolic_creatures(self):
        game_state = self._state()
        planner = MTGStrategicPlanner(game_state)
        win_conditions = planner.identify_win_conditions()
        assessed = planner._assess_win_conditions(
            list(game_state.p1["battlefield"]),
            list(game_state.p2["battlefield"]),
            game_state.p1["life"], game_state.p2["life"])
        archetype = planner._detect_deck_archetype()
        goals = planner.establish_long_term_goals()

        self.assertIsInstance(archetype, str)
        self.assertTrue(math.isfinite(
            win_conditions["combat_damage"]["score"]))
        self.assertTrue(math.isfinite(float(
            assessed["combat_damage"]["turns_to_win"])))
        self.assertTrue(all(math.isfinite(float(threat["level"]))
                            for threat in goals["threat_assessment"]))

    def test_recommend_action_tolerates_symbolic_attacker_power(self):
        game_state = self._state()
        game_state.agent_is_p1 = False
        game_state.strategy_memory = None
        attacker_id = game_state.p2["battlefield"][0]

        class AttackOnlyHandler:
            @staticmethod
            def get_action_info(_action_idx):
                return "DECLARE_ATTACKER", [attacker_id]

        game_state.action_handler = AttackOnlyHandler()
        planner = MTGStrategicPlanner(game_state)
        planner.analyze_game_state = lambda: None
        planner.adapt_strategy = lambda: None
        planner._is_critical_decision = lambda: False
        planner.assess_threats = lambda: []
        planner.find_best_play_sequence = lambda *_args, **_kwargs: ([], 0)
        planner.evaluate_attack_action = lambda _attackers: 0.5

        with patch("Playersim.strategic_planner_search.logging.error") as error:
            self.assertEqual(planner.recommend_action([901]), 901)
        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
