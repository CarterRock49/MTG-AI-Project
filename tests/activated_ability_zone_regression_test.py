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
    inject_into_zone,
    inject_real_card,
)
from Playersim.ability_types import ActivatedAbility  # noqa: E402
from Playersim.ability_utils import EffectFactory  # noqa: E402


class ActivatedAbilityZoneRegressionTest(unittest.TestCase):
    def test_hand_only_keyword_ability_is_not_exposed_on_battlefield(self):
        game_state = fresh(271803)
        controller = game_state.p1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        source_id = inject_into_zone(game_state, controller, {
            "name": "Cycling Zone Probe",
            "mana_cost": "{2}",
            "type_line": "Artifact",
            "oracle_text": "Cycling {2}",
        }, "battlefield")
        abilities = game_state.ability_handler.get_activated_abilities(
            source_id)
        self.assertTrue(
            any(getattr(ability, "zone", None) == "hand"
                for ability in abilities),
            "fixture did not register the hand-only Cycling declaration")

        battlefield_index = controller["battlefield"].index(source_id)
        handler = get_env().action_handler
        mask = handler.generate_valid_actions()
        exposed = []
        for action, allowed in enumerate(mask):
            if not allowed:
                continue
            action_type, _ = handler.get_action_info(action)
            context = handler.action_reasons_with_context.get(
                action, {}).get("context", {}) or {}
            if (action_type == "ACTIVATE_ABILITY"
                    and context.get("battlefield_idx") == battlefield_index):
                exposed.append(action)
        self.assertEqual(
            exposed, [],
            f"a hand-only Cycling ability leaked onto the battlefield: {exposed}")

    def test_optional_copy_retarget_rider_is_not_an_activation_target(self):
        game_state = fresh(271804)
        ability = ActivatedAbility(
            1, cost="{2}, {T}", effect=(
                "When you next activate an exhaust ability that isn't a mana "
                "ability this turn, copy it. You may choose new targets for "
                "the copy."))
        targeting_text = (
            game_state.ability_handler.get_ability_targeting_text(ability))
        self.assertNotIn("target", targeting_text.casefold())
        self.assertIn(
            "when you next activate an exhaust ability",
            targeting_text.casefold())

    def test_copy_instruction_keeps_its_genuine_mandatory_target(self):
        game_state = fresh(271805)
        ability = ActivatedAbility(
            1, cost="{2}", effect=(
                "Copy target activated or triggered ability. You may choose "
                "new targets for the copy."))
        targeting_text = (
            game_state.ability_handler.get_ability_targeting_text(ability))
        self.assertIn(
            "copy target activated or triggered ability",
            targeting_text.casefold())
        self.assertNotIn("new targets for the copy", targeting_text.casefold())

    def test_pit_automaton_delayed_copy_is_masked_before_costs(self):
        game_state = fresh(271806)
        controller = game_state.p1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = game_state.PHASE_END_STEP
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        source_id = inject_real_card(
            game_state, controller, "Pit Automaton", "battlefield")
        controller["entered_battlefield_this_turn"].discard(source_id)
        controller["mana_pool"].update({
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 2,
        })

        abilities = game_state.ability_handler.get_activated_abilities(
            source_id)
        matches = [
            (index, ability) for index, ability in enumerate(abilities)
            if "next activate an exhaust ability" in str(
                getattr(ability, "effect", "") or "").casefold()
        ]
        self.assertEqual(len(matches), 1)
        ability_index, ability = matches[0]
        activation_text = " ".join((
            str(getattr(ability, "effect_text", "") or ""),
            str(getattr(ability, "effect", "") or ""),
        ))
        self.assertTrue(EffectFactory.is_unsupported_source_coupled_copy(
            "Pit Automaton", activation_text))

        state_before = (
            set(controller["tapped_permanents"]),
            dict(controller["mana_pool"]),
            list(game_state.stack),
        )
        self.assertFalse(ability.can_pay_cost(game_state, controller))
        self.assertFalse(game_state.ability_handler.can_activate_ability(
            source_id, ability_index, controller))
        self.assertFalse(game_state.ability_handler.activate_ability(
            source_id, ability_index, controller))
        self.assertEqual(
            (
                set(controller["tapped_permanents"]),
                dict(controller["mana_pool"]),
                list(game_state.stack),
            ),
            state_before)


if __name__ == "__main__":
    unittest.main()
