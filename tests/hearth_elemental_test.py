"""Focused cost regressions for Hearth Elemental // Stoke Genius."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh, get_env, inject_into_zone, inject_real_card, replace_hand,
)


def graveyard_card(name, type_line, *, adventure=False):
    spec = {
        "name": name, "mana_cost": "{1}", "cmc": 1,
        "type_line": type_line, "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
    }
    if adventure:
        spec.update({
            "name": f"{name} // {name} Adventure",
            "layout": "adventure",
            "card_faces": [
                {
                    "name": name, "mana_cost": "{1}",
                    "type_line": type_line, "oracle_text": "",
                },
                {
                    "name": f"{name} Adventure", "mana_cost": "{1}",
                    "type_line": "Sorcery - Adventure",
                    "oracle_text": "Draw a card.",
                },
            ],
        })
    return spec


class HearthElementalCostTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        player = game_state.p1
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        replace_hand(game_state, player, [])
        for permanent_id in list(player.get("battlefield", [])):
            self.assertTrue(game_state.move_card(
                permanent_id, player, "battlefield", player, "library"))
        player["tapped_permanents"] = set()
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0,
        }
        return game_state, get_env().action_handler, player

    @staticmethod
    def _add_union_fixture(game_state, player):
        ids = []
        for spec in (
                graveyard_card("Hearth Instant", "Instant"),
                graveyard_card("Hearth Sorcery", "Sorcery"),
                graveyard_card(
                    "Hearth Adventurer", "Creature - Wizard",
                    adventure=True),
                # Synthetic overlap proves the Oracle "and/or" union is not
                # implemented by adding three independent counts.
                graveyard_card(
                    "Hearth Overlap", "Instant", adventure=True),
                graveyard_card("Hearth Creature", "Creature - Bear")):
            ids.append(inject_into_zone(
                game_state, player, spec, "graveyard"))
        return ids

    def test_union_counts_each_eligible_graveyard_card_once(self):
        game_state, _, player = self._state(196101)
        hearth = inject_real_card(
            game_state, player, "Hearth Elemental // Stoke Genius", "hand")
        self._add_union_fixture(game_state, player)
        card = game_state._safe_get_card(hearth)
        context = {}

        priced = game_state.mana_system.apply_cost_modifiers(
            player, game_state.mana_system.parse_mana_cost(card.mana_cost),
            hearth, context)

        self.assertEqual(priced["generic"], 1)
        self.assertEqual(priced["R"], 1)
        hearth_mods = [
            entry for entry in context["applied_cost_modifications"][
                "reductions"]
            if "Hearth Elemental" in entry.get("source", "")]
        self.assertEqual(len(hearth_mods), 1)
        self.assertEqual(hearth_mods[0]["amount"], 4)

    def test_reduction_clamps_generic_without_reducing_red(self):
        game_state, _, player = self._state(196102)
        hearth = inject_real_card(
            game_state, player, "Hearth Elemental // Stoke Genius", "hand")
        for index in range(7):
            inject_into_zone(
                game_state, player,
                graveyard_card(f"Clamp Instant {index}", "Instant"),
                "graveyard")
        card = game_state._safe_get_card(hearth)

        priced = game_state.mana_system.apply_cost_modifiers(
            player, game_state.mana_system.parse_mana_cost(card.mana_cost),
            hearth, {})

        self.assertEqual(priced["generic"], 0)
        self.assertEqual(priced["R"], 1)

    def test_stoke_genius_adventure_does_not_use_the_front_face_reduction(self):
        game_state, _, player = self._state(196104)
        hearth = inject_real_card(
            game_state, player, "Hearth Elemental // Stoke Genius", "hand")
        self._add_union_fixture(game_state, player)
        card = game_state._safe_get_card(hearth)
        adventure = card.get_adventure_data()
        self.assertIsNotNone(adventure)
        base = game_state.mana_system.parse_mana_cost(adventure["cost"])

        priced = game_state.mana_system.apply_cost_modifiers(
            player, base, hearth, {"cast_as_adventure": True})

        self.assertEqual(priced["generic"], base["generic"])
        self.assertEqual(priced["R"], base["R"])

    def test_mask_affordability_and_payment_use_the_same_reduction(self):
        game_state, handler, player = self._state(196103)
        hearth = inject_real_card(
            game_state, player, "Hearth Elemental // Stoke Genius", "hand")
        self._add_union_fixture(game_state, player)
        card = game_state._safe_get_card(hearth)
        base = game_state.mana_system.parse_mana_cost(card.mana_cost)
        priced = game_state.mana_system.apply_cost_modifiers(
            player, base, hearth, {})

        player["mana_pool"]["R"] = 1
        self.assertFalse(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, priced, {"card_id": hearth}))
        self.assertFalse(handler.generate_valid_actions()[20])

        player["mana_pool"]["C"] = 1
        self.assertTrue(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, priced, {"card_id": hearth}))
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[20])
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(20)

        self.assertFalse(info.get("execution_failed", False), info)
        self.assertTrue(game_state.stack and game_state.stack[-1][1] == hearth)
        self.assertEqual(
            game_state.stack[-1][3].get("final_paid_cost", {}).get("generic"),
            1)
        self.assertEqual(
            game_state.stack[-1][3].get("final_paid_cost", {}).get("R"), 1)
        self.assertEqual(sum(player["mana_pool"].values()), 0)


if __name__ == "__main__":
    unittest.main()
