"""Real-card regressions for conditional tapped-land replacements."""

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
    inject_real_card,
    replace_hand,
)


class ConditionalLandEntryRegressionTest(unittest.TestCase):
    """Exercise each printed condition through the public PLAY_LAND action."""

    def _play_land(
        self,
        card_name: str,
        *,
        other_lands=(),
        turn: int = 1,
        seed: int,
        expected_tapped: bool,
    ) -> None:
        game_state = fresh(seed)
        env = get_env()
        controller = game_state.p1 if turn % 2 else game_state.p2

        game_state.turn = turn
        game_state.agent_is_p1 = controller is game_state.p1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack.clear()
        controller["land_played"] = False
        controller["lands_played_this_turn"] = 0
        replace_hand(game_state, controller, [])

        self.assertEqual(controller["battlefield"], [])
        setup_ids = [
            inject_real_card(game_state, controller, name, "battlefield")
            for name in other_lands
        ]
        land_id = inject_real_card(game_state, controller, card_name, "hand")
        self.assertEqual(controller["hand"], [land_id])

        mask = env.action_mask()
        self.assertTrue(mask[13], f"{card_name} was not publicly playable")
        context = env.action_handler.action_reasons_with_context[13]["context"]
        self.assertEqual(context["card_id"], land_id)
        self.assertEqual(context["hand_idx"], 0)

        _, _, terminated, truncated, info = env.step(13)
        self.assertFalse(terminated)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(controller["hand"], [])
        self.assertEqual(controller["battlefield"], setup_ids + [land_id])
        self.assertNotIn(land_id, controller["graveyard"])
        self.assertNotIn(land_id, controller["exile"])
        self.assertEqual(
            land_id in controller.get("tapped_permanents", set()),
            expected_tapped,
            f"{card_name} entered in the wrong tapped state",
        )
        self.assertTrue(controller["land_played"])
        self.assertEqual(controller["lands_played_this_turn"], 1)

    def test_shattered_sanctum_with_one_other_land_enters_tapped(self):
        self._play_land(
            "Shattered Sanctum", other_lands=("Forest",), seed=98200,
            expected_tapped=True,
        )

    def test_shattered_sanctum_with_two_other_lands_enters_untapped(self):
        self._play_land(
            "Shattered Sanctum", other_lands=("Forest", "Island"),
            seed=98201, expected_tapped=False,
        )

    def test_stormcarved_coast_with_one_other_land_enters_tapped(self):
        self._play_land(
            "Stormcarved Coast", other_lands=("Forest",), seed=98202,
            expected_tapped=True,
        )

    def test_stormcarved_coast_with_two_other_lands_enters_untapped(self):
        self._play_land(
            "Stormcarved Coast", other_lands=("Forest", "Island"),
            seed=98203, expected_tapped=False,
        )

    def test_sundown_pass_with_one_other_land_enters_tapped(self):
        self._play_land(
            "Sundown Pass", other_lands=("Forest",), seed=98204,
            expected_tapped=True,
        )

    def test_sundown_pass_with_two_other_lands_enters_untapped(self):
        self._play_land(
            "Sundown Pass", other_lands=("Forest", "Island"),
            seed=98205, expected_tapped=False,
        )

    def test_ba_sing_se_without_a_basic_land_enters_tapped(self):
        self._play_land(
            "Ba Sing Se", other_lands=("Demolition Field",), seed=98206,
            expected_tapped=True,
        )

    def test_ba_sing_se_with_a_basic_land_enters_untapped(self):
        self._play_land(
            "Ba Sing Se", other_lands=("Forest",), seed=98207,
            expected_tapped=False,
        )

    def test_starting_town_on_your_third_turn_enters_untapped(self):
        self._play_land(
            "Starting Town", turn=5, seed=98208, expected_tapped=False,
        )

    def test_starting_town_on_your_fourth_turn_enters_tapped(self):
        self._play_land(
            "Starting Town", turn=7, seed=98209, expected_tapped=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
