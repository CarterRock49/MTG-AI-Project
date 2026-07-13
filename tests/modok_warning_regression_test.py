"""Real-card regressions for the M.O.D.O.K. training warnings."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from Playersim.ability_types import (  # noqa: E402
    ActivatedAbility,
    AddCountersEffect,
    StaticAbility,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_card,
    inject_into_zone,
    inject_real_card,
)


def creature(name, power, toughness):
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": "Creature - Test",
        "oracle_text": "",
        "power": power,
        "toughness": toughness,
        "keywords": [],
        "color_identity": [],
    }


class ModokWarningRegressionTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = game_state.p1
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.ability_handler.active_triggers = []
        return game_state, get_env().action_handler

    def test_named_activation_parses_without_a_bogus_static_layer(self):
        game_state, _ = self._state(196401)
        controller, opponent = game_state.p1, game_state.p2
        enemy = inject_into_zone(
            game_state, opponent,
            creature("Designed Killing Target", 3, 3), "battlefield")

        oracle_line = (
            "Mental Organism — Pay 3 life: M.O.D.O.K. connives. "
            "Activate only during your turn")
        self.assertEqual(
            ActivatedAbility._parse_cost_effect_strict(oracle_line),
            ("Pay 3 life",
             "M.O.D.O.K. connives. Activate only during your turn"))

        with patch("Playersim.ability_types.logging.warning") as warning:
            modok = inject_real_card(
                game_state, controller, "M.O.D.O.K.", "battlefield")
        warning.assert_not_called()

        abilities = game_state.ability_handler.registered_abilities[modok]
        activated = [
            ability for ability in abilities
            if isinstance(ability, ActivatedAbility)
        ]
        self.assertEqual(len(activated), 1, abilities)
        self.assertEqual(activated[0].cost, "Pay 3 life")
        self.assertEqual(
            activated[0].effect,
            "M.O.D.O.K. connives. Activate only during your turn")
        self.assertFalse(any(
            isinstance(ability, StaticAbility)
            and "mental organism" in ability.effect_text.lower()
            for ability in abilities))
        self.assertTrue(any(
            isinstance(ability, StaticAbility)
            and "designed only for killing" in ability.effect_text.lower()
            for ability in abilities))

        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()
        enemy_card = game_state._safe_get_card(enemy)
        self.assertEqual((enemy_card.power, enemy_card.toughness), (2, 2))
        modok_card = game_state._safe_get_card(modok)
        self.assertEqual((modok_card.power, modok_card.toughness), (2, 2))

    def test_activation_is_own_turn_only_and_resolves_connive(self):
        game_state, handler = self._state(196402)
        controller = game_state.p1
        controller["hand"] = []
        controller["library"] = []
        drawn = inject_card(game_state, {
            "name": "M.O.D.O.K. Connive Draw",
            "mana_cost": "{1}{B}",
            "cmc": 2,
            "type_line": "Sorcery",
            "oracle_text": "",
            "color_identity": ["B"],
        })
        controller["library"].append(drawn)
        game_state._last_card_locations[drawn] = (controller, "library")
        modok = inject_real_card(
            game_state, controller, "M.O.D.O.K.", "battlefield")
        battlefield_index = controller["battlefield"].index(modok)
        action_index = 100 + battlefield_index * 3
        activation_context = {
            "battlefield_idx": battlefield_index,
            "ability_idx": 0,
        }

        # The mask and the execution path independently reject an activation
        # during the opponent's turn.
        game_state.turn = 2
        game_state.priority_player = controller
        life_before = controller["life"]
        self.assertFalse(handler.generate_valid_actions()[action_index])
        stack_before = list(game_state.stack)
        self.assertFalse(handler._handle_activate_ability(
            None, context=activation_context)[1])
        self.assertFalse(game_state.ability_handler.activate_ability(
            modok, 0, controller))
        self.assertEqual(controller["life"], life_before)
        self.assertEqual(game_state.stack, stack_before)

        game_state.turn = 1
        game_state.priority_player = controller
        self.assertTrue(handler.generate_valid_actions()[action_index])
        self.assertTrue(handler._handle_activate_ability(
            None, context=activation_context)[1])
        self.assertEqual(controller["life"], life_before - 3)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.choice_context["type"], "connive_discard")
        self.assertIn(drawn, controller["hand"])

        self.assertTrue(handler._handle_discard_card(0)[1])
        self.assertIn(drawn, controller["graveyard"])
        self.assertEqual(
            game_state._safe_get_card(modok).counters.get("+1/+1"), 1)

        effects = EffectFactory.create_effects(
            "M.O.D.O.K. connives. Activate only during your turn.",
            source_name="M.O.D.O.K.")
        self.assertEqual([type(effect).__name__ for effect in effects],
                         ["ConniveEffect"])

    def test_empty_aggregate_counters_are_silent_but_targets_still_required(self):
        game_state, _ = self._state(196403)
        controller = game_state.p1
        controller["battlefield"] = []

        aggregate = AddCountersEffect(
            "+1/+1", 1, target_type="each creature you control")
        self.assertFalse(aggregate.requires_target)
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertTrue(aggregate.apply(
                game_state, None, controller, {}))
        warning.assert_not_called()

        targeted = AddCountersEffect(
            "+1/+1", 1, target_type="target creature you control")
        self.assertTrue(targeted.requires_target)
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertFalse(targeted.apply(
                game_state, None, controller, {}))
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
