"""Real-card targeting and resolution regressions for Bushwhack's fight mode."""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh, get_env, inject_into_zone, inject_real_card, replace_hand,
)


def creature(name, power, toughness):
    return {
        "name": name, "mana_cost": "{1}", "cmc": 1,
        "type_line": "Creature - Test", "oracle_text": "",
        "power": power, "toughness": toughness,
        "colors": [0, 0, 0, 0, 0],
    }


class BushwhackFightTest(unittest.TestCase):
    def _state(self, seed):
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
        for player in (controller, opponent):
            replace_hand(game_state, player, [])
            for permanent_id in list(player.get("battlefield", [])):
                self.assertTrue(game_state.move_card(
                    permanent_id, player, "battlefield", player, "library"))
            player["tapped_permanents"] = set()
        bushwhack = inject_real_card(
            game_state, controller, "Bushwhack", "hand")
        controller["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 1, "C": 0,
        }
        return (game_state, get_env().action_handler, controller, opponent,
                bushwhack)

    @staticmethod
    def _select(handler, game_state, controller, target_id):
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        if target_id not in candidates:
            raise AssertionError(
                f"{target_id} absent from candidates {candidates} for "
                f"{game_state.targeting_context}")
        game_state.targeting_context["target_page"] = \
            candidates.index(target_id) // 10
        return handler._handle_select_target(
            candidates.index(target_id) % 10, {})[1]

    def test_real_fight_mode_uses_friendly_then_opposing_target_roles(self):
        (game_state, handler, controller, opponent,
         bushwhack) = self._state(196301)
        friendly = inject_into_zone(
            game_state, controller,
            creature("Bushwhack Fighter", 3, 4), "battlefield")
        enemy = inject_into_zone(
            game_state, opponent,
            creature("Bushwhack Opponent", 2, 2), "battlefield")

        self.assertTrue(game_state.cast_spell(bushwhack, controller))
        self.assertTrue(handler._handle_choose_mode(1, {})[1])
        slots = game_state.targeting_context.get("target_slots", [])
        self.assertEqual(
            [slot.get("target_role") for slot in slots],
            ["fighter", "fight_opponent"])

        first_candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(friendly, first_candidates)
        self.assertNotIn(enemy, first_candidates)
        self.assertTrue(self._select(
            handler, game_state, controller, friendly))

        second_candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(enemy, second_candidates)
        self.assertNotIn(friendly, second_candidates)
        self.assertTrue(self._select(
            handler, game_state, controller, enemy))
        stack_context = next(
            item[3] for item in game_state.stack
            if item[0] == "SPELL" and item[1] == bushwhack)
        self.assertEqual(stack_context["targets_by_slot"], [
            [friendly], [enemy]])

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            opponent.get("damage_counters", {}).get(enemy), 3)
        self.assertEqual(
            controller.get("damage_counters", {}).get(friendly), 2)
        game_state.check_state_based_actions()
        self.assertIn(enemy, opponent["graveyard"])
        self.assertIn(friendly, controller["battlefield"])

    def test_real_search_mode_exposes_the_basic_land_choice_to_policy(self):
        game_state, handler, controller, _, bushwhack = \
            self._state(196300)
        first_land = inject_into_zone(game_state, controller, {
            "name": "Bushwhack Forest", "mana_cost": "", "cmc": 0,
            "type_line": "Basic Land - Forest", "oracle_text": "",
            "color_identity": ["G"],
        }, "library")
        second_land = inject_into_zone(game_state, controller, {
            "name": "Bushwhack Island", "mana_cost": "", "cmc": 0,
            "type_line": "Basic Land - Island", "oracle_text": "",
            "color_identity": ["U"],
        }, "library")

        self.assertTrue(game_state.cast_spell(bushwhack, controller))
        self.assertTrue(handler._handle_choose_mode(0, {})[1])
        self.assertTrue(game_state.resolve_top_of_stack())
        choice = game_state.choice_context
        self.assertIsNotNone(choice)
        self.assertEqual(choice.get("type"), "dig_select")
        self.assertTrue(choice.get("optional"))
        self.assertIn(first_land, choice.get("options", []))
        self.assertIn(second_land, choice.get("options", []))

        controller_is_p1 = controller is game_state.p1
        game_state.agent_is_p1 = controller_is_p1
        selected_index = choice["options"].index(second_land)
        choice["choice_page"] = selected_index // 10
        self.assertTrue(handler._handle_choose_mode(
            selected_index % 10, {})[1])
        self.assertIn(second_land, controller["hand"])
        self.assertIn(first_land, controller["library"])
        self.assertIn(bushwhack, controller["graveyard"])

    def test_real_search_mode_may_fail_to_find_and_still_shuffles(self):
        game_state, handler, controller, _, bushwhack = \
            self._state(196304)
        legal_land = inject_into_zone(game_state, controller, {
            "name": "Declined Bushwhack Forest", "mana_cost": "", "cmc": 0,
            "type_line": "Basic Land - Forest", "oracle_text": "",
            "color_identity": ["G"],
        }, "library")

        self.assertTrue(game_state.cast_spell(bushwhack, controller))
        self.assertTrue(handler._handle_choose_mode(0, {})[1])
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(legal_land, game_state.choice_context.get("options", []))
        library_before = Counter(controller["library"])

        shuffle_calls = []
        state_type = type(game_state)
        original_shuffle = state_type.shuffle_library

        def recording_shuffle(state, player):
            shuffle_calls.append(player)
            return original_shuffle(state, player)

        from unittest.mock import patch
        with patch.object(state_type, "shuffle_library", recording_shuffle):
            self.assertTrue(handler._handle_pass_priority(None)[1])

        self.assertEqual(shuffle_calls, [controller])
        self.assertEqual(Counter(controller["library"]), library_before)
        self.assertIn(legal_land, controller["library"])
        self.assertIn(bushwhack, controller["graveyard"])
        self.assertIsNone(game_state.choice_context)

    def test_fight_mode_mask_requires_one_legal_creature_in_each_role(self):
        cases = (
            ("friendly only", True, False, False),
            ("opponent only", False, True, False),
            ("both roles", True, True, True),
        )
        for index, (label, own, opposing, expected) in enumerate(cases):
            with self.subTest(label=label):
                game_state, handler, controller, opponent, bushwhack = \
                    self._state(196310 + index)
                if own:
                    inject_into_zone(
                        game_state, controller,
                        creature("Friendly Role", 2, 2), "battlefield")
                if opposing:
                    inject_into_zone(
                        game_state, opponent,
                        creature("Opposing Role", 2, 2), "battlefield")
                self.assertTrue(game_state.cast_spell(
                    bushwhack, controller))
                mask = handler.generate_valid_actions()
                self.assertEqual(bool(mask[354]), expected)

    def test_one_illegal_fight_target_makes_the_fight_a_noop(self):
        game_state, handler, controller, opponent, bushwhack = \
            self._state(196302)
        friendly = inject_into_zone(
            game_state, controller,
            creature("Remaining Fighter", 3, 3), "battlefield")
        enemy = inject_into_zone(
            game_state, opponent,
            creature("Departing Opponent", 2, 2), "battlefield")
        self.assertTrue(game_state.cast_spell(bushwhack, controller))
        self.assertTrue(handler._handle_choose_mode(1, {})[1])
        self.assertTrue(self._select(
            handler, game_state, controller, friendly))
        self.assertTrue(self._select(
            handler, game_state, controller, enemy))
        self.assertTrue(game_state.move_card(
            enemy, opponent, "battlefield", opponent, "hand"))

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(friendly, controller["battlefield"])
        self.assertNotIn(
            friendly, controller.get("damage_counters", {}))

    def test_copy_retargeting_preserves_both_fight_roles(self):
        game_state, handler, controller, opponent, bushwhack = \
            self._state(196303)
        original_fighter = inject_into_zone(
            game_state, controller,
            creature("Original Fighter", 1, 4), "battlefield")
        new_fighter = inject_into_zone(
            game_state, controller,
            creature("Copy Fighter", 3, 4), "battlefield")
        original_opponent = inject_into_zone(
            game_state, opponent,
            creature("Original Opponent", 1, 4), "battlefield")
        new_opponent = inject_into_zone(
            game_state, opponent,
            creature("Copy Opponent", 2, 2), "battlefield")

        self.assertTrue(game_state.cast_spell(bushwhack, controller))
        self.assertTrue(handler._handle_choose_mode(1, {})[1])
        self.assertTrue(self._select(
            handler, game_state, controller, original_fighter))
        self.assertTrue(self._select(
            handler, game_state, controller, original_opponent))
        original_item = game_state.stack[-1]

        copy_id = game_state.copy_spell_on_stack(
            original_item, controller, allow_new_targets=True)
        self.assertIsNotNone(copy_id)
        self.assertEqual(
            [slot.get("target_role")
             for slot in game_state.choice_context.get("slots", [])],
            ["fighter", "fight_opponent"])

        self.assertTrue(game_state.choose_copy_retarget_slot(retarget=True))
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(new_fighter, candidates)
        self.assertNotIn(new_opponent, candidates)
        self.assertTrue(game_state.complete_copy_retarget_slot(new_fighter))

        self.assertTrue(game_state.choose_copy_retarget_slot(retarget=True))
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(new_opponent, candidates)
        self.assertNotIn(new_fighter, candidates)
        self.assertTrue(game_state.complete_copy_retarget_slot(new_opponent))

        copy_context = next(
            item[3] for item in game_state.stack
            if item[3].get("copy_instance_id") == copy_id)
        self.assertEqual(
            copy_context["targets_by_slot"], [[new_fighter], [new_opponent]])
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            controller.get("damage_counters", {}).get(new_fighter), 2)
        self.assertEqual(
            opponent.get("damage_counters", {}).get(new_opponent), 3)
        self.assertNotIn(
            original_fighter, controller.get("damage_counters", {}))
        self.assertNotIn(
            original_opponent, opponent.get("damage_counters", {}))


if __name__ == "__main__":
    unittest.main()
