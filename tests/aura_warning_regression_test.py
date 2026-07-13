"""Focused regressions for warnings observed in the 1951xx training run.

Run from the repository root with::

    python tests/aura_warning_regression_test.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
for path in (REPO_ROOT, TESTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from Playersim.ability_types import ReturnToHandEffect  # noqa: E402
from Playersim.ability_utils import EffectFactory  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


def creature(name, power, toughness):
    return {
        "name": name,
        "mana_cost": "",
        "cmc": 0,
        "type_line": "Creature - Test",
        "oracle_text": "",
        "power": power,
        "toughness": toughness,
        "keywords": [],
        "color_identity": [],
    }


class AuraAndWarningRegressionTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        game_state.priority_player = game_state.p1
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.ability_handler.active_triggers = []
        return (game_state, get_env().action_handler,
                game_state.p1, game_state.p2)

    @staticmethod
    def _clear_hand(game_state, player):
        for card_id in list(player.get("hand", [])):
            assert game_state.move_card(
                card_id, player, "hand", player, "library")

    def test_meltstriders_resolve_uses_enchant_target_then_fight_target(self):
        game_state, handler, controller, opponent = self._state(195125)
        self._clear_hand(game_state, controller)
        aura_id = inject_real_card(
            game_state, controller, "Meltstrider's Resolve", "hand")
        opposing_creature = inject_into_zone(
            game_state, opponent,
            creature("Resolve Fight Target", 3, 4), "battlefield")
        controller["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 1, "C": 0,
        }

        # The optional fight target printed on the enters trigger cannot make
        # the Aura castable when its mandatory Enchant target is absent.
        mask = handler.generate_valid_actions()
        self.assertFalse(mask[20])

        enchanted_creature = inject_into_zone(
            game_state, controller,
            creature("Resolve Enchant Target", 2, 4), "battlefield")
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[20])
        play_context = handler.action_reasons_with_context[20]["context"]
        _, started = handler._handle_play_spell(
            None, context=play_context)
        self.assertTrue(started)
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        self.assertEqual(
            game_state.targeting_context["effect_text"],
            "target creature you control")

        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(enchanted_creature, candidates)
        self.assertNotIn(opposing_creature, candidates)
        _, selected = handler._handle_select_target(
            candidates.index(enchanted_creature), {})
        self.assertTrue(selected)
        stack_context = game_state.stack[-1][3]
        self.assertEqual(
            stack_context.get("targets"),
            {"creatures": [enchanted_creature]})
        self.assertEqual(
            stack_context.get("targeting_text"),
            "target creature you control")

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(aura_id, controller["battlefield"])
        self.assertEqual(
            controller.get("attachments", {}).get(aura_id),
            enchanted_creature)

        # The Aura spell target and its ETB fight target are independent.
        self.assertTrue(
            game_state.ability_handler.active_triggers,
             [(type(ability).__name__, getattr(ability, "trigger_condition", ""),
              getattr(ability, "effect", ""))
             for ability in game_state.ability_handler.registered_abilities.get(
                 aura_id, [])])
        queued_ability = game_state.ability_handler.active_triggers[0][0]
        self.assertTrue(
            queued_ability.requires_target,
            (queued_ability.trigger_condition, queued_ability.effect,
             queued_ability.effect_text))
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        self.assertEqual(
            game_state.targeting_context["min_targets"], 0,
            game_state.targeting_context)
        fight_candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(opposing_creature, fight_candidates)
        self.assertNotIn(enchanted_creature, fight_candidates)
        _, selected = handler._handle_select_target(
            fight_candidates.index(opposing_creature), {})
        self.assertTrue(selected)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            opponent.get("damage_counters", {}).get(opposing_creature), 2)
        self.assertEqual(
            controller.get("damage_counters", {}).get(enchanted_creature), 3)

    def test_target_opponent_reveal_uses_the_real_effect(self):
        game_state, _, controller, opponent = self._state(195126)
        opponent["hand_revealed"] = False
        with patch("Playersim.ability_utils.logging.warning") as warning:
            effects = EffectFactory.create_effects(
                "Target opponent reveals their hand.")
        warning.assert_not_called()
        self.assertEqual(len(effects), 1)
        self.assertEqual(type(effects[0]).__name__, "RevealHandEffect")
        self.assertTrue(effects[0].apply(
            game_state, None, controller, {"players": ["p2"]}))
        self.assertTrue(opponent["hand_revealed"])

    def test_empty_mass_damage_is_a_successful_silent_noop(self):
        game_state, _, controller, opponent = self._state(195127)
        controller["battlefield"] = []
        opponent["battlefield"] = []
        effect = EffectFactory.create_effects(
            "Deal 3 damage to each creature.")[0]
        self.assertFalse(effect.requires_target)
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertTrue(effect.apply(
                game_state, None, controller, {}))
        warning.assert_not_called()

    def test_bounce_target_that_already_left_is_a_silent_noop(self):
        game_state, _, controller, opponent = self._state(195128)
        target_id = inject_into_zone(
            game_state, opponent,
            creature("Already Gone Bounce Target", 2, 2), "battlefield")
        self.assertTrue(game_state.move_card(
            target_id, opponent, "battlefield", opponent, "graveyard"))
        effect = ReturnToHandEffect(
            target_type="creature", zone="battlefield")
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertTrue(effect.apply(
                game_state, None, controller,
                {"creatures": [target_id]}))
        warning.assert_not_called()
        self.assertIn(target_id, opponent["graveyard"])
        self.assertNotIn(target_id, opponent["hand"])


if __name__ == "__main__":
    unittest.main()
