"""Regressions for contextual token counts, subtypes, and copy colors."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_into_zone, inject_real_card  # noqa: E402
from Playersim.ability_types import (  # noqa: E402
    CreateTokenEffect,
    TriggeredAbility,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402


logging.disable(logging.CRITICAL)


class TokenQuantitySubtypeRegressionTest(unittest.TestCase):
    @staticmethod
    def _created_tokens(game_state, controller, before):
        return [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]

    def test_real_multisubtype_token_clauses_parse_and_resolve_exactly(self):
        cases = [
            (
                "Create a 4/4 red Dinosaur Dragon creature token with flying.",
                1, (4, 4), ["dinosaur", "dragon"], [0, 0, 0, 1, 0],
                "flying",
            ),
            (
                "Create two 1/1 white Human Soldier creature tokens.",
                2, (1, 1), ["human", "soldier"], [1, 0, 0, 0, 0],
                None,
            ),
        ]
        for index, (text, count, pt, subtypes, colors, keyword) in enumerate(
                cases):
            with self.subTest(text=text):
                effects = EffectFactory.create_effects(text)
                self.assertEqual(len(effects), 1)
                effect = effects[0]
                self.assertIsInstance(effect, CreateTokenEffect)
                self.assertEqual(effect.count, count)
                self.assertEqual(effect.token_card_types, ["creature"])
                self.assertEqual(effect.token_subtypes, subtypes)

                game_state = fresh(38400 + index)
                controller = game_state.p1
                before = set(controller.get("tokens", []))
                self.assertTrue(effect.apply(
                    game_state, None, controller, {}))
                created = self._created_tokens(
                    game_state, controller, before)
                self.assertEqual(len(created), count)
                for token_id in created:
                    token = game_state._safe_get_card(token_id)
                    self.assertTrue(token.is_token)
                    self.assertEqual(
                        token.name,
                        " ".join(word.capitalize() for word in subtypes)
                        + " Token")
                    self.assertEqual(
                        (token.power, token.toughness), pt)
                    self.assertEqual(token.card_types, ["creature"])
                    self.assertEqual(token.subtypes, subtypes)
                    self.assertEqual(token.colors, colors)
                    if keyword:
                        self.assertTrue(game_state.check_keyword(
                            token_id, keyword))

    def test_artifact_and_enchantment_creature_descriptors_keep_card_types(self):
        cases = [
            (
                "Create a 1/1 colorless Thopter artifact creature token "
                "with flying.",
                ["creature", "artifact"], ["artifact", "creature"],
                ["thopter"], "Thopter Token",
            ),
            (
                "Create a 2/2 white Spirit enchantment creature token.",
                ["creature", "enchantment"],
                ["enchantment", "creature"], ["spirit"],
                "Spirit Token",
            ),
        ]
        for index, (text, parsed_types, runtime_types,
                    subtypes, name) in enumerate(cases):
            with self.subTest(text=text):
                effect = EffectFactory.create_effects(text)[0]
                self.assertIsInstance(effect, CreateTokenEffect)
                self.assertEqual(effect.token_card_types, parsed_types)
                self.assertEqual(effect.token_subtypes, subtypes)

                game_state = fresh(38405 + index)
                controller = game_state.p1
                before = set(controller.get("tokens", []))
                self.assertTrue(effect.apply(
                    game_state, None, controller, {}))
                created = self._created_tokens(
                    game_state, controller, before)
                self.assertEqual(len(created), 1)
                token = game_state._safe_get_card(created[0])
                self.assertEqual(token.name, name)
                self.assertEqual(token.card_types, runtime_types)
                self.assertEqual(token.subtypes, subtypes)

    def test_namor_real_cast_trigger_counts_all_blue_symbols(self):
        game_state = fresh(38410)
        controller = game_state.p1
        namor = inject_real_card(
            game_state, controller, "Namor the Sub-Mariner", "battlefield")
        spell_id = inject_into_zone(game_state, controller, {
            "name": "Three-Symbol Namor Probe",
            "mana_cost": "{1}{U}{U}{U}",
            "cmc": 4,
            "type_line": "Instant",
            "oracle_text": "Draw a card.",
            "color_identity": ["U"],
        }, "hand")
        game_state.ability_handler.active_triggers.clear()

        parsed = EffectFactory.create_effects(
            "Create that many 1/1 blue Merfolk creature tokens.")[0]
        self.assertIsInstance(parsed, CreateTokenEffect)
        self.assertEqual(parsed.count, 0)
        self.assertEqual(parsed.count_expr, "that many")
        self.assertEqual(parsed.token_subtypes, ["merfolk"])

        before = set(controller.get("tokens", []))
        self.assertTrue(game_state.trigger_ability(
            spell_id, "CAST_SPELL", {
                "cast_card_id": spell_id,
                "casting_player": controller,
                "cast_card_types": ["instant"],
                "prepared_face": {
                    "name": "Three-Symbol Namor Probe",
                    "mana_cost": "{1}{U}{U}{U}",
                    "type_line": "Instant",
                },
            }))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0].card_id, namor)
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertTrue(game_state.resolve_top_of_stack())
        warning.assert_not_called()

        created = self._created_tokens(game_state, controller, before)
        self.assertEqual(len(created), 3)
        for token_id in created:
            token = game_state._safe_get_card(token_id)
            self.assertEqual(token.name, "Merfolk Token")
            self.assertEqual((token.power, token.toughness), (1, 1))
            self.assertEqual(token.card_types, ["creature"])
            self.assertEqual(token.subtypes, ["merfolk"])
            self.assertEqual(token.colors, [0, 1, 0, 0, 0])

    def test_real_draconautics_second_exhaust_creates_dinosaur_dragon(self):
        game_state = fresh(38409)
        controller = game_state.p1
        engineer = inject_real_card(
            game_state, controller, "Draconautics Engineer", "battlefield")
        abilities = game_state.ability_handler.get_activated_abilities(
            engineer)
        self.assertEqual(len(abilities), 2)
        token_ability = abilities[1]
        self.assertTrue(token_ability.is_exhaust)
        self.assertEqual(token_ability.cost, "{3}{R}")
        self.assertEqual(
            token_ability.effect,
            "Create a 4/4 red Dinosaur Dragon creature token with flying")
        parsed = token_ability._create_ability_effects(
            token_ability.effect)[0]
        self.assertEqual(parsed.token_card_types, ["creature"])
        self.assertEqual(parsed.token_subtypes, ["dinosaur", "dragon"])

        before = set(controller.get("tokens", []))
        self.assertTrue(token_ability.resolve_with_targets(
            game_state, controller, {}))
        created = self._created_tokens(game_state, controller, before)
        self.assertEqual(len(created), 1)
        token = game_state._safe_get_card(created[0])
        self.assertEqual(token.name, "Dinosaur Dragon Token")
        self.assertEqual((token.power, token.toughness), (4, 4))
        self.assertEqual(token.card_types, ["creature"])
        self.assertEqual(token.subtypes, ["dinosaur", "dragon"])
        self.assertEqual(token.colors, [0, 0, 0, 1, 0])
        self.assertTrue(game_state.check_keyword(created[0], "flying"))

    def test_namor_trigger_rejects_wrong_color_and_creature_spells(self):
        game_state = fresh(38411)
        controller = game_state.p1
        inject_real_card(
            game_state, controller, "Namor the Sub-Mariner", "battlefield")
        wrong_color = inject_into_zone(game_state, controller, {
            "name": "Red Namor Probe", "mana_cost": "{R}{R}",
            "cmc": 2, "type_line": "Instant", "oracle_text": "",
            "color_identity": ["R"],
        }, "hand")
        blue_creature = inject_into_zone(game_state, controller, {
            "name": "Creature Namor Probe", "mana_cost": "{U}{U}",
            "cmc": 2, "type_line": "Creature - Merfolk",
            "oracle_text": "", "power": 2, "toughness": 2,
            "color_identity": ["U"],
        }, "hand")
        game_state.ability_handler.active_triggers.clear()

        self.assertFalse(game_state.trigger_ability(
            wrong_color, "CAST_SPELL", {
                "cast_card_id": wrong_color,
                "casting_player": controller,
                "cast_card_types": ["instant"],
            }))
        self.assertFalse(game_state.trigger_ability(
            blue_creature, "CAST_SPELL", {
                "cast_card_id": blue_creature,
                "casting_player": controller,
                "cast_card_types": ["creature"],
            }))
        self.assertEqual(game_state.ability_handler.active_triggers, [])

    def test_unresolved_that_many_fails_closed_without_creating_one(self):
        game_state = fresh(38412)
        controller = game_state.p1
        namor = inject_real_card(
            game_state, controller, "Namor the Sub-Mariner", "battlefield")
        ability = TriggeredAbility(
            namor,
            trigger_condition=(
                "whenever you cast a noncreature spell with one or more "
                "blue mana symbols in its mana cost"),
            effect=(
                "create that many 1/1 blue merfolk creature tokens"),
            effect_text=(
                "Whenever you cast a noncreature spell with one or more "
                "blue mana symbols in its mana cost, create that many 1/1 "
                "blue Merfolk creature tokens."),
        )
        effect = EffectFactory.create_effects(ability.effect)[0]
        before = set(controller.get("tokens", []))
        fidelity_before = game_state.fidelity_counters["unparsed_effects"]
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertFalse(effect.apply(
                game_state, namor, controller, {}, context={
                    "ability": ability,
                    "effect_text": ability.effect_text,
                    "cast_card_id": "missing-cast-card",
                }))
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(
            set(controller.get("tokens", [])), before)
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)
        self.assertIn(
            "Namor the Sub-Mariner",
            game_state.fidelity_counters["unparsed_cards"])

    def test_real_offspring_copy_keeps_red_color_and_copy_characteristics(self):
        game_state = fresh(38413)
        controller = game_state.p1
        mouse_id = inject_real_card(
            game_state, controller, "Manifold Mouse", "battlefield")
        original = game_state._safe_get_card(mouse_id)
        game_state.ability_handler.active_triggers.clear()
        before = set(controller.get("tokens", []))
        offspring = TriggeredAbility(
            mouse_id,
            trigger_condition="when this creature enters",
            effect="create a 1/1 token copy of it",
            effect_text=(
                "When this creature enters, create a 1/1 token copy of it."))
        offspring._is_offspring_etb_trigger = True
        game_state._offspring_cost_paid_context[mouse_id] = True
        game_state.add_to_stack("TRIGGER", mouse_id, controller, {
            "ability": offspring,
            "effect_text": offspring.effect_text,
        })
        self.assertEqual(len(game_state.stack), 1)

        # Simulate continuous-effect write-back after the trigger is frozen.
        # None of these live characteristics is copyable under CR 707.2.
        original.name = "Layered Mouse Name"
        original.mana_cost = "{W}"
        original.colors = [1, 0, 0, 0, 0]
        original.card_types = ["artifact"]
        original.subtypes = ["construct"]
        original.supertypes = ["legendary"]
        original.oracle_text = "Layered oracle text"
        original.keywords = [0] * len(original.keywords)
        self.assertTrue(game_state.resolve_top_of_stack())

        created = self._created_tokens(game_state, controller, before)
        self.assertEqual(len(created), 1)
        token = game_state._safe_get_card(created[0])
        self.assertTrue(token.is_token)
        self.assertEqual(token.name, original.printed("name"))
        self.assertEqual(token.mana_cost, original.printed("mana_cost"))
        self.assertEqual((token.power, token.toughness), (1, 1))
        self.assertEqual(token.card_types, original.printed("card_types"))
        self.assertEqual(token.subtypes, original.printed("subtypes"))
        self.assertEqual(token.supertypes, original.printed("supertypes"))
        self.assertEqual(token.oracle_text, original.printed("oracle_text"))
        self.assertEqual(
            token.keywords, original.printed("keywords"))
        self.assertEqual(
            original.printed("colors"), [0, 0, 0, 1, 0])
        self.assertEqual(token.colors, original.printed("colors"))
        self.assertEqual(token.printed("colors"), original.printed("colors"))


if __name__ == "__main__":
    unittest.main()
