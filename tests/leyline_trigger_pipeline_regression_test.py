"""Registered-trigger regressions for Leyline of Resonance spell copying."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_into_zone, inject_real_card  # noqa: E402
from Playersim.ability_types import TriggeredAbility  # noqa: E402


logging.disable(logging.CRITICAL)


class LeylineTriggerPipelineRegressionTest(unittest.TestCase):
    def test_optional_copy_retargeting_is_not_a_trigger_target(self):
        copy_trigger = TriggeredAbility(
            1,
            trigger_condition="whenever you cast a spell",
            effect=(
                "copy that spell. You may choose new targets for the copy."))
        self.assertFalse(copy_trigger.requires_target)

        targeted_copy = TriggeredAbility(
            1,
            trigger_condition="whenever a creature enters",
            effect=(
                "copy target instant or sorcery spell. You may choose new "
                "targets for the copy."))
        self.assertTrue(targeted_copy.requires_target)

        ordinary_target = TriggeredAbility(
            1,
            trigger_condition="whenever a creature enters",
            effect="put a +1/+1 counter on target creature.")
        self.assertTrue(ordinary_target.requires_target)

    def test_real_registered_leyline_trigger_queues_stacks_and_copies(self):
        game_state = fresh(38507)
        controller = game_state.p1
        leyline_id = inject_real_card(
            game_state, controller, "Leyline of Resonance", "battlefield")
        registered = [
            ability for ability
            in game_state.ability_handler.registered_abilities.get(
                leyline_id, [])
            if isinstance(ability, TriggeredAbility)]
        self.assertEqual(len(registered), 1)
        ability = registered[0]
        self.assertEqual(
            ability.trigger_condition,
            "whenever you cast an instant or sorcery spell that targets "
            "only a single creature you control")
        self.assertEqual(
            ability.effect,
            "copy that spell. you may choose new targets for the copy")
        self.assertFalse(ability.requires_target)

        creature_id = inject_into_zone(game_state, controller, {
            "name": "Leyline Friendly Target",
            "mana_cost": "",
            "cmc": 0,
            "type_line": "Creature - Test",
            "oracle_text": "",
            "power": 1,
            "toughness": 1,
            "color_identity": [],
        }, "battlefield")
        spell_id = inject_into_zone(game_state, controller, {
            "name": "Leyline Targeted Instant",
            "mana_cost": "{R}",
            "cmc": 1,
            "type_line": "Instant",
            "oracle_text": "Target creature gets +1/+0 until end of turn.",
            "color_identity": ["R"],
        }, "hand")
        controller["hand"].remove(spell_id)
        spell_context = {
            "targets": {"creatures": [creature_id]},
            "requires_target": True,
            "num_targets": 1,
            "min_targets": 1,
            "max_targets": 1,
            "targeting_text": (
                "Target creature gets +1/+0 until end of turn."),
        }
        game_state.add_to_stack(
            "SPELL", spell_id, controller, dict(spell_context))
        game_state.ability_handler.active_triggers.clear()
        fidelity_before = dict(game_state.fidelity_counters)

        self.assertTrue(game_state.trigger_ability(None, "CAST_SPELL", {
            "cast_card_id": spell_id,
            "casting_player": controller,
            "targets": {"creatures": [creature_id]},
            "cast_card_types": ["instant"],
        }))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1)
        self.assertIs(queued[0][0], ability)
        self.assertEqual(
            [(item[0], item[1]) for item in game_state.stack],
            [("SPELL", spell_id)])

        game_state.ability_handler.process_triggered_abilities()
        self.assertIsNone(game_state.targeting_context)
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(
            [(item[0], item[1]) for item in game_state.stack],
            [("SPELL", spell_id), ("TRIGGER", leyline_id)])
        self.assertIs(game_state.stack[-1][3]["ability"], ability)

        self.assertTrue(game_state.resolve_top_of_stack())
        spell_rows = [
            item for item in game_state.stack
            if item[0] == "SPELL" and item[1] == spell_id]
        self.assertEqual(len(spell_rows), 2)
        copies = [item for item in spell_rows if item[3].get("is_copy")]
        self.assertEqual(len(copies), 1)
        self.assertEqual(
            copies[0][3].get("targets"),
            {"creatures": [creature_id]})
        self.assertEqual(game_state.fidelity_counters, fidelity_before)


if __name__ == "__main__":
    unittest.main()
