"""Regression tests for independent target slots on ordinary spells.

Run from the repository root with::

    python tests/multi_instruction_target_test.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from scenario_test import fresh, get_env, inject_into_zone, inject_real_card  # noqa: E402
from Playersim.ability_types import DamageEffect  # noqa: E402


class OrdinaryInstructionTargetTest(unittest.TestCase):
    def _setup_outburst(self, seed):
        game_state = fresh(seed)
        environment = get_env()
        handler = environment.action_handler
        player = game_state.p1 if game_state.agent_is_p1 else game_state.p2
        opponent = game_state.p2 if player is game_state.p1 else game_state.p1
        outburst_id = inject_real_card(
            game_state, player, "Vibrant Outburst", "hand")
        player["mana_pool"] = {
            "W": 0, "U": 1, "B": 0, "R": 1, "G": 0, "C": 0,
        }
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        hand_index = player["hand"].index(outburst_id)
        cast_action = (20 + hand_index if hand_index < 8
                       else 396 + hand_index - 8)
        self.assertTrue(
            handler.generate_valid_actions()[cast_action],
            "the optional creature slot hid an otherwise legal cast")
        self.assertTrue(game_state.cast_spell(
            outburst_id, player, {"source_zone": "hand"}))
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        slots = game_state.targeting_context.get("target_slots", [])
        self.assertEqual(
            [(slot.get("required_type"), slot.get("min_targets"))
             for slot in slots],
            [("any", 1), ("creature", 0)])
        return game_state, handler, player, opponent, outburst_id

    def _select(self, game_state, handler, player, target_id):
        candidates = handler._get_target_selection_candidates(
            player, game_state.targeting_context)
        self.assertIn(target_id, candidates)
        absolute_index = candidates.index(target_id)
        game_state.targeting_context["target_page"] = absolute_index // 10
        _, success = handler._handle_select_target(
            absolute_index % 10, {})
        self.assertTrue(success)

    def test_optional_creature_target_can_be_declined(self):
        (game_state, handler, player, opponent,
         outburst_id) = self._setup_outburst(911)
        opponent_id = "p1" if opponent is game_state.p1 else "p2"
        life_before = opponent["life"]

        self._select(game_state, handler, player, opponent_id)
        self.assertEqual(
            game_state.targeting_context.get("required_type"), "creature")
        _, success = handler._handle_pass_priority(None)
        self.assertTrue(success)
        self.assertIsNone(game_state.targeting_context)
        stack_context = game_state.stack[-1][3]
        self.assertEqual(stack_context.get("targets_by_slot"), [
            [opponent_id], []])

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(opponent["life"], life_before - 3)
        self.assertIn(outburst_id, player["graveyard"])

    def test_damage_and_tap_use_different_target_slots(self):
        (game_state, handler, player, opponent,
         outburst_id) = self._setup_outburst(912)
        creature_id = inject_into_zone(game_state, player, {
            "name": "Outburst Tap Target",
            "mana_cost": "{3}",
            "type_line": "Creature - Construct",
            "oracle_text": "",
            "power": 4,
            "toughness": 4,
        }, "battlefield")
        opponent_id = "p1" if opponent is game_state.p1 else "p2"
        life_before = opponent["life"]

        self._select(game_state, handler, player, opponent_id)
        self._select(game_state, handler, player, creature_id)
        self.assertIsNone(game_state.targeting_context)
        stack_context = game_state.stack[-1][3]
        self.assertEqual(stack_context.get("targets_by_slot"), [
            [opponent_id], [creature_id]])

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(opponent["life"], life_before - 3)
        self.assertIn(creature_id, player["tapped_permanents"])
        self.assertNotIn(creature_id, player.get("damage_marked", {}))
        self.assertIn(outburst_id, player["graveyard"])

    def test_all_chosen_targets_illegal_counters_spell_on_resolution(self):
        (game_state, handler, player, opponent,
         outburst_id) = self._setup_outburst(915)
        damage_target_id = inject_into_zone(game_state, opponent, {
            "name": "Outburst Departing Target",
            "mana_cost": "{3}",
            "type_line": "Creature - Construct",
            "oracle_text": "",
            "power": 4,
            "toughness": 4,
        }, "battlefield")

        self._select(game_state, handler, player, damage_target_id)
        _, success = handler._handle_pass_priority(None)
        self.assertTrue(success)
        self.assertEqual(
            game_state.stack[-1][3].get("targets_by_slot"),
            [[damage_target_id], []])
        self.assertTrue(game_state.move_card(
            damage_target_id, opponent, "battlefield", opponent, "graveyard",
            cause="test_target_became_illegal"))

        damage_calls = []
        original_apply = DamageEffect._apply_effect
        spell_resolve_calls = []
        original_resolve_spell = type(game_state)._resolve_spell

        def record_damage(effect, state, source_id, controller, targets):
            damage_calls.append(targets)
            return original_apply(
                effect, state, source_id, controller, targets)

        def record_spell_resolution(
                state, spell_id, controller, context=None):
            spell_resolve_calls.append(spell_id)
            return original_resolve_spell(
                state, spell_id, controller, context)

        with (patch.object(
                type(game_state), "_resolve_spell",
                new=record_spell_resolution),
              patch.object(
                  DamageEffect, "_apply_effect", new=record_damage)):
            self.assertTrue(game_state.resolve_top_of_stack())

        self.assertEqual(spell_resolve_calls, [])
        self.assertEqual(
            damage_calls, [],
            "a spell with no remaining legal targets reached DamageEffect")
        self.assertIn(outburst_id, player["graveyard"])

    def test_illegal_damage_target_skips_only_its_instruction(self):
        (game_state, handler, player, opponent,
         outburst_id) = self._setup_outburst(916)
        damage_target_id = inject_into_zone(game_state, opponent, {
            "name": "Outburst Vanishing Target",
            "mana_cost": "{3}",
            "type_line": "Creature - Construct",
            "oracle_text": "",
            "power": 4,
            "toughness": 4,
        }, "battlefield")
        tap_target_id = inject_into_zone(game_state, player, {
            "name": "Outburst Surviving Target",
            "mana_cost": "{3}",
            "type_line": "Creature - Construct",
            "oracle_text": "",
            "power": 4,
            "toughness": 4,
        }, "battlefield")

        self._select(game_state, handler, player, damage_target_id)
        self._select(game_state, handler, player, tap_target_id)
        self.assertEqual(
            game_state.stack[-1][3].get("targets_by_slot"),
            [[damage_target_id], [tap_target_id]])
        self.assertTrue(game_state.move_card(
            damage_target_id, opponent, "battlefield", opponent, "graveyard",
            cause="test_target_became_illegal"))

        damage_calls = []
        original_apply = DamageEffect._apply_effect
        spell_resolve_calls = []
        original_resolve_spell = type(game_state)._resolve_spell

        def record_damage(effect, state, source_id, controller, targets):
            damage_calls.append(targets)
            return original_apply(
                effect, state, source_id, controller, targets)

        def record_spell_resolution(
                state, spell_id, controller, context=None):
            spell_resolve_calls.append(spell_id)
            return original_resolve_spell(
                state, spell_id, controller, context)

        with (patch.object(
                type(game_state), "_resolve_spell",
                new=record_spell_resolution),
              patch.object(
                  DamageEffect, "_apply_effect", new=record_damage)):
            self.assertTrue(game_state.resolve_top_of_stack())

        self.assertEqual(spell_resolve_calls, [outburst_id])
        self.assertEqual(
            damage_calls, [],
            "the invalid damage instruction was invoked with rebound targets")
        self.assertIn(tap_target_id, player["tapped_permanents"])
        self.assertIn(outburst_id, player["graveyard"])

    def test_flashback_reminder_is_not_a_resolving_instruction(self):
        game_state = fresh(913)
        player = game_state.p1 if game_state.agent_is_p1 else game_state.p2
        opponent = game_state.p2 if player is game_state.p1 else game_state.p1
        practiced_id = inject_real_card(
            game_state, player, "Practiced Offense", "hand")
        creature_id = inject_into_zone(game_state, player, {
            "name": "Practiced Target",
            "mana_cost": "{2}",
            "type_line": "Creature - Soldier",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
        }, "battlefield")
        practiced = game_state._safe_get_card(practiced_id)
        slots = game_state._ordinary_target_slots(practiced.oracle_text)
        self.assertEqual(
            [slot.get("required_type") for slot in slots],
            ["player", "creature"])
        opponent_id = "p1" if opponent is game_state.p1 else "p2"
        effects, parsed_all = game_state._ordinary_instruction_effects(
            practiced, practiced.oracle_text, {
                "instruction_target_slots": slots,
                "targets_by_slot": [[opponent_id], [creature_id]],
            })
        self.assertTrue(parsed_all)
        self.assertEqual(
            [type(effect).__name__ for effect in effects],
            ["AddCountersEffect", "KeywordChoiceGrantEffect"])
        self.assertFalse(any(
            "flashback" in str(getattr(effect, "effect_text", "")).lower()
            for effect in effects))

    def test_conditional_instead_target_is_not_simultaneous(self):
        game_state = fresh(914)
        player = game_state.p1 if game_state.agent_is_p1 else game_state.p2
        flood_maw_id = inject_real_card(
            game_state, player, "Into the Flood Maw", "hand")
        flood_maw = game_state._safe_get_card(flood_maw_id)
        self.assertEqual(
            game_state._ordinary_target_slots(flood_maw.oracle_text), [],
            "Gift's conditional instead branch became a second target slot")


if __name__ == "__main__":
    unittest.main()
