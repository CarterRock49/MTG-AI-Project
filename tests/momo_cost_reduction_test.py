"""Focused casting-cost regressions for Momo, Friendly Flier."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from Playersim.ability_types import StaticAbility  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh, get_env, inject_into_zone, inject_real_card, replace_hand,
)


def spell(name, type_line, oracle_text="", mana_cost="{2}{U}"):
    return {
        "name": name, "mana_cost": mana_cost, "cmc": 3,
        "type_line": type_line, "oracle_text": oracle_text,
        "colors": [0, 1, 0, 0, 0], "power": 2, "toughness": 2,
    }


class MomoCostReductionTest(unittest.TestCase):
    def _state(self, seed, *, with_momo=True):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.spells_cast_this_turn = []
        for player in (controller, opponent):
            replace_hand(game_state, player, [])
            for permanent_id in list(player.get("battlefield", [])):
                self.assertTrue(game_state.move_card(
                    permanent_id, player, "battlefield", player, "library"))
            player["tapped_permanents"] = set()
            player["mana_pool"] = {
                "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0,
            }
        momo = (inject_real_card(
            game_state, controller, "Momo, Friendly Flier", "battlefield")
            if with_momo else None)
        return (game_state, get_env().action_handler, controller, opponent,
                momo)

    @staticmethod
    def _priced(game_state, player, card_id):
        card = game_state._safe_get_card(card_id)
        return game_state.mana_system.apply_cost_modifiers(
            player, game_state.mana_system.parse_mana_cost(card.mana_cost),
            card_id, {})

    def test_exact_qualifiers_and_dead_static_registration(self):
        game_state, _, controller, opponent, momo = self._state(196201)
        eligible = inject_into_zone(
            game_state, controller,
            spell("Eligible Bird", "Creature - Bird", "Flying"), "hand")
        lemur = inject_into_zone(
            game_state, controller,
            spell("Flying Lemur", "Creature - Lemur", "Flying"), "hand")
        grounded = inject_into_zone(
            game_state, controller,
            spell("Grounded Bird", "Creature - Bird"), "hand")
        flying_instant = inject_into_zone(
            game_state, controller,
            spell("Flying Words", "Instant", "Flying"), "hand")

        self.assertEqual(self._priced(
            game_state, controller, eligible)["generic"], 1)
        for excluded in (lemur, grounded, flying_instant):
            self.assertEqual(self._priced(
                game_state, controller, excluded)["generic"], 2)

        # The declaration applies only during Momo's controller's own turns.
        game_state.turn = 2
        self.assertEqual(self._priced(
            game_state, controller, eligible)["generic"], 2)
        opposing_eligible = inject_into_zone(
            game_state, opponent,
            spell("Opposing Bird", "Creature - Bird", "Flying"), "hand")
        self.assertEqual(self._priced(
            game_state, opponent, opposing_eligible)["generic"], 2)

        self.assertFalse(any(
            isinstance(ability, StaticAbility)
            and "first non-lemur creature spell" in ability.effect_text.lower()
            for ability in game_state.ability_handler.registered_abilities.get(
                momo, [])))

    def test_only_first_eligible_announced_cast_is_reduced_and_turn_resets(self):
        game_state, _, controller, _, _ = self._state(196202)
        grounded = inject_into_zone(
            game_state, controller,
            spell("First Grounded", "Creature - Bird", mana_cost=""),
            "hand")
        first_flier = inject_into_zone(
            game_state, controller,
            spell("First Flier", "Creature - Bird", "Flying"), "hand")
        second_flier = inject_into_zone(
            game_state, controller,
            spell("Second Flier", "Creature - Bird", "Flying"), "hand")
        controller["mana_pool"].update({"U": 3, "C": 6})

        self.assertTrue(game_state.cast_spell(grounded, controller))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(self._priced(
            game_state, controller, first_flier)["generic"], 1,
            "an ineligible creature consumed Momo's reduction")

        self.assertTrue(game_state.cast_spell(first_flier, controller))
        first_context = game_state.stack[-1][3]
        self.assertEqual(first_context["final_paid_cost"]["generic"], 1)
        self.assertTrue(first_context["cast_card_has_flying"])
        self.assertIn("creature", first_context["cast_card_types"])
        self.assertNotIn("lemur", first_context["cast_card_subtypes"])
        self.assertEqual(self._priced(
            game_state, controller, second_flier)["generic"], 2)

        game_state.turn = 3
        game_state._reset_turn_tracking_variables()
        self.assertEqual(self._priced(
            game_state, controller, second_flier)["generic"], 1)

    def test_eligible_cast_before_momo_enters_still_counts_as_first(self):
        game_state, _, controller, _, _ = self._state(
            196203, with_momo=False)
        first_flier = inject_into_zone(
            game_state, controller,
            spell("Pre-Momo Flier", "Creature - Bird", "Flying",
                  mana_cost=""), "hand")
        later_flier = inject_into_zone(
            game_state, controller,
            spell("Post-Momo Flier", "Creature - Bird", "Flying"), "hand")

        self.assertTrue(game_state.cast_spell(first_flier, controller))
        self.assertTrue(game_state.stack[-1][3]["cast_card_has_flying"])
        inject_real_card(
            game_state, controller, "Momo, Friendly Flier", "battlefield")
        self.assertEqual(self._priced(
            game_state, controller, later_flier)["generic"], 2)

    def test_mask_affordability_and_payment_share_first_spell_state(self):
        game_state, handler, controller, _, _ = self._state(196204)
        flier = inject_into_zone(
            game_state, controller,
            spell("Mask Flier", "Creature - Bird", "Flying"), "hand")
        priced = self._priced(game_state, controller, flier)
        self.assertEqual((priced["generic"], priced["U"]), (1, 1))

        controller["mana_pool"]["U"] = 1
        self.assertFalse(game_state.mana_system.can_pay_mana_cost_with_lands(
            controller, priced, {"card_id": flier}))
        self.assertFalse(handler.generate_valid_actions()[20])
        self.assertEqual(game_state.spells_cast_this_turn, [])

        controller["mana_pool"]["C"] = 1
        self.assertTrue(game_state.mana_system.can_pay_mana_cost_with_lands(
            controller, priced, {"card_id": flier}))
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[20])
        self.assertEqual(game_state.spells_cast_this_turn, [],
                         "an affordability probe consumed Momo's reduction")
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(20)

        self.assertFalse(info.get("execution_failed", False), info)
        spell_item = next(
            item for item in game_state.stack
            if item[0] == "SPELL" and item[1] == flier)
        paid = spell_item[3]["final_paid_cost"]
        self.assertEqual((paid["generic"], paid["U"]), (1, 1))
        self.assertEqual(sum(controller["mana_pool"].values()), 0)
        self.assertEqual(len(game_state.spells_cast_this_turn), 1)


if __name__ == "__main__":
    unittest.main()
