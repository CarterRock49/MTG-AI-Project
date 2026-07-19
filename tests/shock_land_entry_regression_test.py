"""Public-pipeline regressions for the five Standard shock lands."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_real_card, replace_hand  # noqa: E402


class ShockLandEntryRegressionTest(unittest.TestCase):
    """Each branch starts from a new fixture and uses only policy actions."""

    def _stage_land(self, card_name: str, seed: int):
        game_state = fresh(seed)
        env = get_env()
        player = game_state.p1

        game_state.turn = 1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        game_state.stack.clear()
        player["land_played"] = False
        player["lands_played_this_turn"] = 0
        replace_hand(game_state, player, [])

        self.assertEqual(player["life"], 20)
        land_id = inject_real_card(game_state, player, card_name, "hand")
        card = game_state._safe_get_card(land_id)
        self.assertEqual(card.name, card_name)
        self.assertIn(
            "As this land enters, you may pay 2 life. If you don't, it enters tapped.",
            card.oracle_text,
        )
        self.assertEqual(player["hand"], [land_id])

        play_mask = env.action_mask()
        self.assertTrue(play_mask[13], f"{card_name} was not publicly playable")
        play_context = env.action_handler.action_reasons_with_context[13][
            "context"
        ]
        self.assertEqual(play_context["card_id"], land_id)
        self.assertEqual(play_context["hand_idx"], 0)
        self.assertEqual(play_context["controller_id"], "p1")

        _, _, terminated, truncated, info = env.step(13)
        self.assertFalse(terminated)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertEqual(player["life"], 20)
        self.assertNotIn(land_id, player.get("tapped_permanents", set()))
        self.assertNotIn(land_id, player["hand"])
        self.assertIn(land_id, player["battlefield"])
        self.assertEqual(game_state.phase, game_state.PHASE_CHOOSE)

        choice = game_state.choice_context
        self.assertIsNotNone(choice)
        self.assertEqual(choice.get("type"), "as_enters_pay_life")
        self.assertIs(choice.get("player"), player)
        self.assertEqual(choice.get("card_id"), land_id)
        self.assertEqual(choice.get("source_id"), land_id)
        self.assertEqual(choice.get("options"), ["pay_2_life", "decline"])

        choice_mask = env.action_mask()
        self.assertTrue(choice_mask[353], "pay-2-life choice was hidden")
        self.assertTrue(choice_mask[354], "decline choice was hidden")
        self.assertFalse(choice_mask[11], "mandatory choice exposed PASS")
        return game_state, env, player, land_id

    def _assert_entry_branch(
        self, card_name: str, *, pay_life: bool, seed: int
    ) -> None:
        game_state, env, player, land_id = self._stage_land(card_name, seed)
        choice_action = 353 if pay_life else 354

        _, _, terminated, truncated, info = env.step(choice_action)
        self.assertFalse(terminated)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)

        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.phase, game_state.PHASE_MAIN_PRECOMBAT)
        self.assertIs(game_state.priority_player, player)
        self.assertEqual(player["hand"], [])
        self.assertEqual(player["battlefield"], [land_id])
        self.assertNotIn(land_id, player["graveyard"])
        self.assertNotIn(land_id, player["exile"])
        self.assertTrue(player["land_played"])
        self.assertEqual(player["lands_played_this_turn"], 1)

        if pay_life:
            self.assertEqual(player["life"], 18)
            self.assertTrue(player["lost_life_this_turn"])
            self.assertNotIn(land_id, player.get("tapped_permanents", set()))
        else:
            self.assertEqual(player["life"], 20)
            self.assertFalse(player["lost_life_this_turn"])
            self.assertIn(land_id, player.get("tapped_permanents", set()))

    def test_hallowed_fountain_pay_2_life(self):
        self._assert_entry_branch("Hallowed Fountain", pay_life=True, seed=98100)

    def test_hallowed_fountain_decline(self):
        self._assert_entry_branch("Hallowed Fountain", pay_life=False, seed=98101)

    def test_sacred_foundry_pay_2_life(self):
        self._assert_entry_branch("Sacred Foundry", pay_life=True, seed=98102)

    def test_sacred_foundry_decline(self):
        self._assert_entry_branch("Sacred Foundry", pay_life=False, seed=98103)

    def test_steam_vents_pay_2_life(self):
        self._assert_entry_branch("Steam Vents", pay_life=True, seed=98104)

    def test_steam_vents_decline(self):
        self._assert_entry_branch("Steam Vents", pay_life=False, seed=98105)

    def test_temple_garden_pay_2_life(self):
        self._assert_entry_branch("Temple Garden", pay_life=True, seed=98106)

    def test_temple_garden_decline(self):
        self._assert_entry_branch("Temple Garden", pay_life=False, seed=98107)

    def test_watery_grave_pay_2_life(self):
        self._assert_entry_branch("Watery Grave", pay_life=True, seed=98108)

    def test_watery_grave_decline(self):
        self._assert_entry_branch("Watery Grave", pay_life=False, seed=98109)


if __name__ == "__main__":
    unittest.main(verbosity=2)
