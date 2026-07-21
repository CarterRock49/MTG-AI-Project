"""Observation v5: producible mana by color.

Each card's cost was already broken out by color, but the observer only saw a
single color-blind total for what it could produce. v5 adds
`my_producible_mana` / `opp_producible_mana`: per-color access from visible
untapped lands plus floating mana. Own is exact; the opponent's is the public
estimate from its face-up untapped lands, so it leaks no hidden information.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_real_card  # noqa: E402


class ProducibleManaObservationTest(unittest.TestCase):
    def _clean(self, seed):
        game_state = fresh(seed)
        env = get_env()
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        for player in (controller, opponent):
            player["battlefield"] = []
            player["tapped_permanents"] = set()
            player["mana_pool"] = {c: 0 for c in ("W", "U", "B", "R", "G", "C")}
        return game_state, env, controller, opponent

    def test_duals_count_for_each_color_tapped_excluded_floating_added(self):
        game_state, env, controller, opponent = self._clean(51001)
        inject_real_card(game_state, controller, "Steam Vents", "battlefield")   # U/R
        inject_real_card(game_state, controller, "Island", "battlefield")        # U
        mountain = inject_real_card(
            game_state, controller, "Mountain", "battlefield")                   # R (tapped)
        controller["tapped_permanents"].add(mountain)
        controller["mana_pool"]["W"] = 1
        inject_real_card(game_state, opponent, "Sacred Foundry", "battlefield")   # W/R

        obs = env._get_obs()
        # W=1 floating, U=2 (Steam Vents + Island), R=1 (Steam Vents; Mountain
        # is tapped and excluded), B=G=0.
        np.testing.assert_array_equal(
            obs["my_producible_mana"],
            np.array([1, 2, 0, 1, 0], dtype=np.float32))
        # Opponent's public estimate from its one untapped W/R dual.
        np.testing.assert_array_equal(
            obs["opp_producible_mana"],
            np.array([1, 0, 0, 1, 0], dtype=np.float32))
        self.assertTrue(env.observation_space.contains(obs))

    def test_producible_mana_is_perspective_relative(self):
        game_state, env, controller, opponent = self._clean(51002)
        inject_real_card(game_state, controller, "Island", "battlefield")     # my U
        inject_real_card(game_state, opponent, "Mountain", "battlefield")     # opp R

        game_state.agent_is_p1 = True
        obs_p1 = env._get_obs()
        self.assertEqual(obs_p1["my_producible_mana"][1], 1)   # my U
        self.assertEqual(obs_p1["opp_producible_mana"][3], 1)  # opp R

        game_state.agent_is_p1 = False
        obs_p2 = env._get_obs()
        # From p2's perspective the decks swap: "my" is now the Mountain, "opp"
        # is the Island.
        self.assertEqual(obs_p2["my_producible_mana"][3], 1)   # my R
        self.assertEqual(obs_p2["opp_producible_mana"][1], 1)  # opp U


if __name__ == "__main__":
    unittest.main()
