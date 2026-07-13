"""Real-card regressions for Deceit's colored ETBs and Evoke trigger.

Run from the repository root with::

    python tests/deceit_real_card_test.py
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
for path in (REPO_ROOT, TESTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
    replace_hand,
)


logging.disable(logging.CRITICAL)


def permanent(name, type_line, *, power=0, toughness=0):
    return {
        "name": name,
        "mana_cost": "",
        "cmc": 0,
        "type_line": type_line,
        "oracle_text": "",
        "power": power,
        "toughness": toughness,
        "keywords": [],
        "color_identity": [],
    }


class DeceitRealCardTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        game_state.priority_player = game_state.p1
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.ability_handler.active_triggers = []
        return game_state, get_env().action_handler, game_state.p1, game_state.p2

    def _enter_deceit(self, game_state, controller, spent, *, evoked=False):
        deceit_id = inject_real_card(game_state, controller, "Deceit", "hand")
        context = {
            "final_paid_details": {"spent_specific": dict(spent)},
        }
        if evoked:
            context["use_alt_cost"] = "evoke"
        self.assertTrue(game_state.move_card(
            deceit_id, controller, "hand", controller, "battlefield",
            cause="spell_resolution", context=context))
        return deceit_id

    @staticmethod
    def _trigger_kind(ability):
        effect = str(getattr(ability, "effect", "") or "").lower()
        if "return up to one other target nonland permanent" in effect:
            return "blue"
        if "target opponent reveals their hand" in effect:
            return "black"
        if "sacrifice this creature" in effect:
            return "evoke_sacrifice"
        return effect

    def test_normal_and_evoke_colored_trigger_matrix(self):
        cases = [
            ("normal UB", {"U": 1, "B": 1, "C": 4}, False, set()),
            ("normal UU", {"U": 2, "C": 4}, False, {"blue"}),
            ("normal BB", {"B": 2, "C": 4}, False, {"black"}),
            ("evoke UU", {"U": 2}, True,
             {"blue", "evoke_sacrifice"}),
            ("evoke BB", {"B": 2}, True,
             {"black", "evoke_sacrifice"}),
            ("evoke UB", {"U": 1, "B": 1}, True,
             {"evoke_sacrifice"}),
        ]

        for index, (label, spent, evoked, expected) in enumerate(cases):
            with self.subTest(label=label):
                game_state, _, controller, _ = self._state(701 + index)
                self._enter_deceit(
                    game_state, controller, spent, evoked=evoked)
                actual = {
                    self._trigger_kind(entry[0])
                    for entry in game_state.ability_handler.active_triggers
                }
                self.assertEqual(actual, expected)

    def test_blue_etb_is_optional_and_restricts_nonland_other_targets(self):
        game_state, handler, controller, opponent = self._state(711)
        own_nonland = inject_into_zone(
            game_state, controller,
            permanent("Own Relic", "Artifact"), "battlefield")
        opposing_nonland = inject_into_zone(
            game_state, opponent,
            permanent("Opposing Creature", "Creature -- Bear",
                      power=2, toughness=2), "battlefield")
        own_land = inject_into_zone(
            game_state, controller,
            permanent("Own Land", "Land"), "battlefield")
        opposing_land = inject_into_zone(
            game_state, opponent,
            permanent("Opposing Land", "Land"), "battlefield")

        deceit_id = self._enter_deceit(
            game_state, controller, {"U": 2, "C": 4})
        game_state.ability_handler.process_triggered_abilities()

        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        self.assertEqual(game_state.targeting_context["min_targets"], 0)
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(own_nonland, candidates)
        self.assertIn(opposing_nonland, candidates)
        self.assertNotIn(deceit_id, candidates)
        self.assertNotIn(own_land, candidates)
        self.assertNotIn(opposing_land, candidates)

        _, declined = handler._handle_pass_priority(None)
        self.assertTrue(declined)
        self.assertIsNone(game_state.targeting_context)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(deceit_id, controller["battlefield"])
        self.assertIn(own_nonland, controller["battlefield"])
        self.assertIn(opposing_nonland, opponent["battlefield"])

    def test_blue_etb_returns_a_selected_other_nonland_permanent(self):
        game_state, handler, controller, opponent = self._state(712)
        target_id = inject_into_zone(
            game_state, opponent,
            permanent("Bounce Target", "Enchantment"), "battlefield")
        deceit_id = self._enter_deceit(
            game_state, controller, {"U": 2, "C": 4})
        game_state.ability_handler.process_triggered_abilities()

        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        _, selected = handler._handle_select_target(
            candidates.index(target_id), {})
        self.assertTrue(selected)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(target_id, opponent["hand"])
        self.assertNotIn(target_id, opponent["battlefield"])
        self.assertIn(deceit_id, controller["battlefield"])

    def test_black_etb_choice_continues_cleanly_then_evoke_sacrifices(self):
        game_state, handler, controller, opponent = self._state(713)
        creature_id, land_id, instant_id = replace_hand(game_state, opponent, [
            permanent("Hand Creature", "Creature -- Rat", power=1,
                      toughness=1),
            permanent("Hand Land", "Land"),
            {
                **permanent("Hand Instant", "Instant"),
                "oracle_text": "Draw a card.",
            },
        ])
        sentinel = ("ABILITY", -999, opponent, {"effect_text": "sentinel"})
        game_state.stack.append(sentinel)

        deceit_id = self._enter_deceit(
            game_state, controller, {"B": 2}, evoked=True)
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(game_state.choice_context["type"], "order_triggers")
        pending = game_state.choice_context["pending"]
        sacrifice_index = next(
            index for index, entry in enumerate(pending)
            if self._trigger_kind(entry[0]) == "evoke_sacrifice")
        self.assertTrue(game_state.ability_handler.order_trigger_chosen(
            sacrifice_index))

        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertEqual(candidates, ["p2"])
        _, selected = handler._handle_select_target(0, {})
        self.assertTrue(selected)

        self.assertTrue(game_state.resolve_top_of_stack())
        choice = game_state.choice_context
        self.assertIsNotNone(choice)
        self.assertEqual(choice["type"], "hand_selection")
        self.assertIn(creature_id, choice["options"])
        self.assertIn(instant_id, choice["options"])
        self.assertNotIn(land_id, choice["options"])
        self.assertEqual(len(game_state.stack), 2)
        self.assertEqual(game_state.stack[0], sentinel)
        continuation = choice.get("effect_continuation")
        self.assertIsNotNone(continuation)
        self.assertEqual(
            continuation.get("finalizer", {}).get("kind"), "ability")

        creature_choice = choice["options"].index(creature_id)
        _, chosen = handler._handle_choose_mode(creature_choice, {})
        self.assertTrue(chosen)
        self.assertIn(creature_id, opponent["graveyard"])
        self.assertNotIn(creature_id, opponent["hand"])
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.stack[0], sentinel)

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertNotIn(deceit_id, controller["battlefield"])
        self.assertIn(deceit_id, controller["graveyard"])
        self.assertEqual(game_state.stack, [sentinel])


if __name__ == "__main__":
    unittest.main()
