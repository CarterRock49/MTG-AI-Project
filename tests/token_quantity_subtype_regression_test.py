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
    UnsupportedEffect,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from Playersim.actions import ActionHandler  # noqa: E402


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

    def test_kain_unresolved_that_many_sequence_has_no_partial_mutation(self):
        game_state = fresh(38413)
        controller = game_state.p1
        opponent = game_state.p2
        source_id = inject_real_card(
            game_state, controller,
            "Kain, Traitorous Dragoon", "battlefield")
        trigger = next(
            ability for ability
            in game_state.ability_handler.registered_abilities[source_id]
            if (isinstance(ability, TriggeredAbility)
                and "that many tapped treasure" in ability.effect.casefold()))
        effects = EffectFactory.create_effects(
            trigger.effect, source_name="Kain, Traitorous Dragoon")
        self.assertEqual(
            [type(effect).__name__ for effect in effects],
            ["AbilityEffect", "AbilityEffect", "CreateTokenEffect",
             "AbilityEffect"])
        self.assertEqual(effects[2].count_expr, "that many")

        state_before = (
            list(controller["battlefield"]),
            list(opponent["battlefield"]),
            len(controller["hand"]),
            len(controller["library"]),
            controller["life"],
            opponent["life"],
            list(controller.get("tokens", [])),
        )
        fidelity_before = game_state.fidelity_counters[
            "unparsed_effects"]
        game_state.add_to_stack("TRIGGER", source_id, controller, {
            "ability": trigger,
            "effect_text": trigger.effect_text,
            "event_card_id": source_id,
            "source_card_id": source_id,
            "target_player": opponent,
            "damage_amount": 3,
            "amount": 3,
        })
        self.assertFalse(game_state.resolve_top_of_stack())
        self.assertEqual(
            (
                list(controller["battlefield"]),
                list(opponent["battlefield"]),
                len(controller["hand"]),
                len(controller["library"]),
                controller["life"],
                opponent["life"],
                list(controller.get("tokens", [])),
            ),
            state_before)
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)

    def test_multicolored_permanent_count_uses_exact_control_and_colors(self):
        game_state = fresh(38414)
        controller = game_state.p1
        opponent = game_state.p2
        station_id = inject_real_card(
            game_state, controller, "Infinite Guideline Station",
            "battlefield")
        clause = (
            "Create a tapped 2/2 colorless Robot artifact creature token "
            "for each multicolored permanent you control.")
        self.assertIn(
            clause.casefold(),
            game_state._safe_get_card(station_id).oracle_text.casefold())

        for index, colors in enumerate((
                ["W", "U"], ["B", "R"], ["G"], [])):
            inject_into_zone(game_state, controller, {
                "name": f"Controlled Color Fixture {index}",
                "mana_cost": "",
                "cmc": 2,
                "type_line": "Enchantment",
                "oracle_text": "",
                "color_identity": colors,
            }, "battlefield")
        inject_into_zone(game_state, opponent, {
            "name": "Opponent Multicolored Fixture",
            "mana_cost": "",
            "cmc": 2,
            "type_line": "Artifact Creature - Construct",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["U", "R"],
        }, "battlefield")

        effects = EffectFactory.create_effects(clause)
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertIsInstance(effect, CreateTokenEffect)
        self.assertEqual(
            effect.count_expr, "multicolored permanent you control")
        self.assertEqual(effect.token_card_types, ["creature", "artifact"])
        self.assertEqual(effect.token_subtypes, ["robot"])
        self.assertGreater(
            sum(game_state._safe_get_card(station_id).colors), 1)

        before = set(controller.get("tokens", []))
        self.assertTrue(effect.apply(
            game_state, station_id, controller, {}, context={}))
        created = self._created_tokens(game_state, controller, before)
        # The two multicolored fixtures and the multicolored Station itself;
        # neither controlled mono/colorless cards nor the opponent's fixture.
        self.assertEqual(len(created), 3)
        for token_id in created:
            token = game_state._safe_get_card(token_id)
            self.assertEqual(token.name, "Robot Token")
            self.assertEqual((token.power, token.toughness), (2, 2))
            self.assertEqual(token.card_types, ["artifact", "creature"])
            self.assertEqual(token.subtypes, ["robot"])
            self.assertIn(token_id, controller["tapped_permanents"])

    def test_nontoken_subtype_count_enforces_all_conjuncts(self):
        game_state = fresh(38415)
        controller = game_state.p1
        opponent = game_state.p2
        mysterio_id = inject_real_card(
            game_state, controller, "Mysterio, Master of Illusion",
            "battlefield")
        clause = (
            "Create a 3/3 blue Illusion Villain creature token for each "
            "nontoken Villain you control.")
        self.assertIn(
            clause.casefold(),
            game_state._safe_get_card(mysterio_id).oracle_text.casefold())
        inject_into_zone(game_state, controller, {
            "name": "Controlled Nontoken Villain",
            "mana_cost": "{U}",
            "cmc": 1,
            "type_line": "Creature - Human Villain",
            "oracle_text": "",
            "power": 1,
            "toughness": 1,
            "color_identity": ["U"],
        }, "battlefield")
        inject_into_zone(game_state, controller, {
            "name": "Controlled Nontoken Nonvillain",
            "mana_cost": "{U}",
            "cmc": 1,
            "type_line": "Creature - Human Hero",
            "oracle_text": "",
            "power": 1,
            "toughness": 1,
            "color_identity": ["U"],
        }, "battlefield")
        game_state.create_token(controller, {
            "name": "Token Villain Fixture",
            "power": 1,
            "toughness": 1,
            "color_identity": ["U"],
            "card_types": ["creature"],
            "subtypes": ["villain"],
        })
        inject_into_zone(game_state, opponent, {
            "name": "Opponent Nontoken Villain",
            "mana_cost": "{U}",
            "cmc": 1,
            "type_line": "Creature - Human Villain",
            "oracle_text": "",
            "power": 1,
            "toughness": 1,
            "color_identity": ["U"],
        }, "battlefield")

        effects = EffectFactory.create_effects(clause)
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertIsInstance(effect, CreateTokenEffect)
        self.assertEqual(effect.count_expr, "nontoken villain you control")
        self.assertEqual(effect.token_subtypes, ["illusion", "villain"])

        before = set(controller.get("tokens", []))
        self.assertTrue(effect.apply(
            game_state, mysterio_id, controller, {}, context={}))
        created = self._created_tokens(game_state, controller, before)
        # Mysterio itself and the one additional controlled nontoken Villain.
        self.assertEqual(len(created), 2)
        for token_id in created:
            token = game_state._safe_get_card(token_id)
            self.assertEqual(token.name, "Illusion Villain Token")
            self.assertEqual((token.power, token.toughness), (3, 3))
            self.assertEqual(token.card_types, ["creature"])
            self.assertEqual(token.subtypes, ["illusion", "villain"])
            self.assertEqual(token.colors, [0, 1, 0, 0, 0])

    def test_event_batch_dynamic_count_fails_closed_not_as_known_zero(self):
        game_state = fresh(38416)
        controller = game_state.p1
        starcage_id = inject_real_card(
            game_state, controller, "Pinnacle Starcage", "battlefield")
        abilities = game_state.ability_handler.get_activated_abilities(
            starcage_id)
        self.assertEqual(len(abilities), 1)
        clause = (
            "Create a 2/2 colorless Robot artifact creature token for each "
            "card put into a graveyard this way")
        self.assertIn(clause.casefold(), abilities[0].effect.casefold())
        token_effects = [
            effect for effect in EffectFactory.create_effects(
                abilities[0].effect)
            if isinstance(effect, CreateTokenEffect)]
        self.assertEqual(len(token_effects), 1)
        effect = token_effects[0]
        self.assertEqual(
            effect.count_expr, "card put into a graveyard this way")

        # A supported empty predicate is a proven zero even in strict mode;
        # an event-batch phrase without frozen evidence is unresolved.
        self.assertEqual(game_state.count_dynamic_quantity(
            "creatures you control", controller, strict=True), 0)
        self.assertIsNone(game_state.count_dynamic_quantity(
            effect.count_expr, controller, strict=True))

        before = set(controller.get("tokens", []))
        fidelity_before = game_state.fidelity_counters["unparsed_effects"]
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertFalse(effect.apply(
                game_state, starcage_id, controller, {}, context={}))
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(set(controller.get("tokens", [])), before)
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)
        self.assertIn(
            "Pinnacle Starcage",
            game_state.fidelity_counters["unparsed_cards"])

    def test_exact_graveyard_token_counts_keep_type_conjunctions(self):
        cases = (
            (
                "Aatchik, Emerald Radian",
                "Create a 1/1 green Insect creature token for each artifact "
                "and/or creature card in your graveyard.",
                (
                    ("Artifact Fixture", "Artifact"),
                    ("Creature Fixture", "Creature - Bear"),
                    ("Artifact Creature Fixture",
                     "Artifact Creature - Construct"),
                    ("Irrelevant Fixture", "Instant"),
                ),
                3,
                "artifact and/or creature card in your graveyard",
            ),
            (
                "Lluwen, Imperfect Naturalist",
                "Create a 1/1 black and green Worm creature token for each "
                "land card in your graveyard.",
                (
                    ("Land Fixture One", "Land"),
                    ("Land Fixture Two", "Basic Land - Forest"),
                    ("Irrelevant Fixture", "Creature - Elf"),
                ),
                2,
                "land card in your graveyard",
            ),
        )
        for index, (card_name, clause, fixtures, expected, expr) in enumerate(
                cases):
            with self.subTest(card=card_name):
                game_state = fresh(38450 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                self.assertIn(
                    clause.casefold(),
                    game_state._safe_get_card(source_id).oracle_text.casefold())
                for name, type_line in fixtures:
                    inject_into_zone(game_state, controller, {
                        "name": name,
                        "mana_cost": "",
                        "cmc": 0,
                        "type_line": type_line,
                        "oracle_text": "",
                        "color_identity": [],
                        "power": 1,
                        "toughness": 1,
                    }, "graveyard")

                effects = EffectFactory.create_effects(
                    clause, source_name=card_name)
                self.assertEqual(len(effects), 1)
                effect = effects[0]
                self.assertIsInstance(effect, CreateTokenEffect)
                self.assertEqual(effect.count_expr, expr)
                self.assertEqual(
                    game_state.count_dynamic_quantity(
                        expr, controller, strict=True),
                    expected)

                before = set(controller.get("tokens", []))
                self.assertTrue(effect.apply(
                    game_state, source_id, controller, {}, context={}))
                self.assertEqual(
                    len(self._created_tokens(game_state, controller, before)),
                    expected)

    def test_event_derived_token_counts_fail_closed_in_strict_mode(self):
        cases = (
            (
                "Luxurious Locomotive",
                "Create a Treasure token for each creature that crewed it "
                "this turn.",
                "creature that crewed it this turn",
            ),
            (
                "Wanderwine Farewell",
                "Create a 1/1 white and blue Merfolk creature token for each "
                "permanent returned to its owner's hand this way.",
                "permanent returned to its owner's hand this way",
            ),
        )
        for index, (card_name, clause, expr) in enumerate(cases):
            with self.subTest(card=card_name):
                game_state = fresh(38460 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                self.assertIn(
                    clause.casefold(),
                    game_state._safe_get_card(source_id).oracle_text.casefold())
                inject_into_zone(game_state, controller, {
                    "name": "Unrelated Battlefield Creature",
                    "mana_cost": "",
                    "cmc": 0,
                    "type_line": "Creature - Bear",
                    "oracle_text": "",
                    "color_identity": [],
                    "power": 2,
                    "toughness": 2,
                }, "battlefield")
                effect = EffectFactory.create_effects(
                    clause, source_name=card_name)[0]
                self.assertIsInstance(effect, CreateTokenEffect)
                self.assertEqual(effect.count_expr, expr)
                self.assertIsNone(game_state.count_dynamic_quantity(
                    expr, controller, strict=True))

                before = set(controller.get("tokens", []))
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]
                self.assertFalse(effect.apply(
                    game_state, source_id, controller, {}, context={}))
                self.assertEqual(
                    set(controller.get("tokens", [])), before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before + 1)
                self.assertIn(
                    card_name,
                    game_state.fidelity_counters["unparsed_cards"])

    def test_strict_plains_and_creature_graveyard_counts_are_exact(self):
        game_state = fresh(38465)
        controller = game_state.p1
        gather_id = inject_real_card(
            game_state, controller, "Gather the White Lotus", "battlefield")
        gather_clause = (
            "Create a 1/1 white Ally creature token for each Plains you "
            "control.")
        self.assertIn(
            gather_clause.casefold(),
            game_state._safe_get_card(gather_id).oracle_text.casefold())
        inject_into_zone(game_state, controller, {
            "name": "Plains Fixture",
            "mana_cost": "",
            "cmc": 0,
            "type_line": "Basic Land - Plains",
            "oracle_text": "",
            "color_identity": ["W"],
        }, "battlefield")
        inject_into_zone(game_state, controller, {
            "name": "Island Fixture",
            "mana_cost": "",
            "cmc": 0,
            "type_line": "Basic Land - Island",
            "oracle_text": "",
            "color_identity": ["U"],
        }, "battlefield")
        gather_effect = EffectFactory.create_effects(
            gather_clause, source_name="Gather the White Lotus")[0]
        self.assertEqual(gather_effect.count_expr, "plains you control")
        self.assertEqual(game_state.count_dynamic_quantity(
            gather_effect.count_expr, controller, strict=True), 1)

        revenge_id = inject_real_card(
            game_state, controller, "Revenge of the Rats", "battlefield")
        revenge_clause = (
            "Create a tapped 1/1 black Rat creature token for each creature "
            "card in your graveyard.")
        self.assertIn(
            revenge_clause.casefold(),
            game_state._safe_get_card(revenge_id).oracle_text.casefold())
        inject_into_zone(game_state, controller, {
            "name": "Graveyard Creature Fixture",
            "mana_cost": "",
            "cmc": 1,
            "type_line": "Creature - Rat",
            "oracle_text": "",
            "color_identity": ["B"],
            "power": 1,
            "toughness": 1,
        }, "graveyard")
        inject_into_zone(game_state, controller, {
            "name": "Graveyard Noncreature Fixture",
            "mana_cost": "",
            "cmc": 1,
            "type_line": "Sorcery",
            "oracle_text": "",
            "color_identity": ["B"],
        }, "graveyard")
        revenge_effect = EffectFactory.create_effects(
            revenge_clause, source_name="Revenge of the Rats")[0]
        self.assertEqual(
            revenge_effect.count_expr, "creature card in your graveyard")
        self.assertEqual(game_state.count_dynamic_quantity(
            revenge_effect.count_expr, controller, strict=True), 1)

    def test_unfrozen_variable_token_surfaces_are_atomic_unsupported(self):
        cases = (
            (
                "Bat Colony",
                "Create a 1/1 black Bat creature token with flying for each "
                "mana from a Cave spent to cast it.",
            ),
            (
                "Twitching Doll",
                "Create a 2/2 green Spider creature token with reach for each "
                "counter on this creature.",
            ),
            (
                "Glen Elendra's Answer",
                "Create a 1/1 blue and black Faerie creature token with flying "
                "for each spell and ability countered this way.",
            ),
            (
                "Avengers: Under Siege",
                "Create a Treasure token for each Villain you control.",
            ),
            (
                "The Astonishing Ant-Man",
                "Create that many 1/1 green Insect creature tokens.",
            ),
        )
        for index, (card_name, clause) in enumerate(cases):
            with self.subTest(card=card_name):
                game_state = fresh(38470 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                self.assertIn(
                    clause.casefold(),
                    game_state._safe_get_card(source_id).oracle_text.casefold())
                effects = EffectFactory.create_effects(
                    clause, source_name=card_name)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], UnsupportedEffect)
                self.assertFalse(any(
                    isinstance(effect, CreateTokenEffect)
                    for effect in effects))

                before = set(controller.get("tokens", []))
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]
                self.assertFalse(effects[0].apply(
                    game_state, source_id, controller, {}, context={}))
                self.assertEqual(
                    set(controller.get("tokens", [])), before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before + 1)
                self.assertIn(
                    card_name,
                    game_state.fidelity_counters["unparsed_cards"])

    def test_twitching_doll_unsupported_activation_pays_no_costs(self):
        game_state = fresh(38479)
        controller = game_state.p1
        source_id = inject_real_card(
            game_state, controller, "Twitching Doll", "battlefield")
        source = game_state._safe_get_card(source_id)
        entered = controller.get("entered_battlefield_this_turn", set())
        if hasattr(entered, "discard"):
            entered.discard(source_id)
        elif source_id in entered:
            entered.remove(source_id)
        game_state.add_counter(source_id, "nest", 3)
        abilities = game_state.ability_handler.get_activated_abilities(
            source_id)
        matches = [
            (index, ability) for index, ability in enumerate(abilities)
            if EffectFactory.is_unsupported_variable_token_instruction(
                source.name,
                " ".join((
                    str(getattr(ability, "effect_text", "") or ""),
                    str(getattr(ability, "effect", "") or ""))))
        ]
        self.assertEqual(len(matches), 1)
        ability_index, ability = matches[0]
        state_before = (
            list(controller["battlefield"]),
            list(controller["graveyard"]),
            set(controller["tapped_permanents"]),
            dict(controller["mana_pool"]),
            dict(source.counters),
        )
        fidelity_before = game_state.fidelity_counters[
            "unparsed_effects"]

        self.assertFalse(ability.can_pay_cost(game_state, controller))
        self.assertFalse(game_state.ability_handler.can_activate_ability(
            source_id, ability_index, controller))
        self.assertFalse(game_state.ability_handler.activate_ability(
            source_id, ability_index, controller))
        self.assertEqual(
            (
                list(controller["battlefield"]),
                list(controller["graveyard"]),
                set(controller["tapped_permanents"]),
                dict(controller["mana_pool"]),
                dict(source.counters),
            ),
            state_before)
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before)

    def test_astonishing_ant_man_is_masked_before_activation_costs(self):
        game_state = fresh(38480)
        controller = game_state.p1
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack.clear()
        game_state.choice_context = None
        source_id = inject_real_card(
            game_state, controller,
            "The Astonishing Ant-Man", "battlefield")
        source = game_state._safe_get_card(source_id)
        entered = controller.get("entered_battlefield_this_turn", set())
        if hasattr(entered, "discard"):
            entered.discard(source_id)
        elif source_id in entered:
            entered.remove(source_id)
        game_state.add_counter(source_id, "+1/+1", 3)
        controller["mana_pool"].update({
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 1, "C": 2,
        })

        abilities = game_state.ability_handler.get_activated_abilities(
            source_id)
        matches = [
            (index, ability) for index, ability in enumerate(abilities)
            if EffectFactory.is_unsupported_variable_token_instruction(
                source.name,
                " ".join((
                    str(getattr(ability, "effect_text", "") or ""),
                    str(getattr(ability, "effect", "") or ""))))
        ]
        self.assertEqual(len(matches), 1)
        ability_index, ability = matches[0]
        battlefield_index = controller["battlefield"].index(source_id)
        self.assertLess(battlefield_index, 20)
        self.assertLess(ability_index, 3)
        public_action = 100 + battlefield_index * 3 + ability_index

        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        valid = handler.generate_valid_actions()
        self.assertFalse(valid[public_action])
        self.assertFalse(ability.can_pay_cost(game_state, controller))
        self.assertFalse(game_state.ability_handler.can_activate_ability(
            source_id, ability_index, controller))

        state_before = (
            list(controller["battlefield"]),
            list(controller["graveyard"]),
            set(controller["tapped_permanents"]),
            dict(controller["mana_pool"]),
            dict(source.counters),
            list(game_state.stack),
            game_state.choice_context,
        )
        fidelity_before = dict(game_state.fidelity_counters)
        self.assertFalse(game_state.ability_handler.activate_ability(
            source_id, ability_index, controller))
        self.assertEqual(
            (
                list(controller["battlefield"]),
                list(controller["graveyard"]),
                set(controller["tapped_permanents"]),
                dict(controller["mana_pool"]),
                dict(source.counters),
                list(game_state.stack),
                game_state.choice_context,
            ),
            state_before)
        self.assertEqual(game_state.fidelity_counters, fidelity_before)

    def test_real_offspring_paid_and_unpaid_entries_use_registered_trigger(self):
        cases = [
            ("Manifold Mouse", [0, 0, 0, 1, 0]),
            ("Pawpatch Recruit", [0, 0, 0, 0, 1]),
        ]
        for index, (card_name, printed_colors) in enumerate(cases):
            with self.subTest(card=card_name, paid=True):
                game_state = fresh(38420 + index)
                controller = game_state.p1
                card_id = inject_real_card(
                    game_state, controller, card_name, "hand")
                before = set(controller.get("tokens", []))
                self.assertTrue(game_state.move_card(
                    card_id, controller, "hand", controller, "battlefield",
                    cause="offspring_paid_test",
                    context={"paid_offspring": True}))

                registered = [
                    ability for ability
                    in game_state.ability_handler.registered_abilities.get(
                        card_id, [])
                    if getattr(
                        ability, "_is_offspring_etb_trigger", False)]
                self.assertEqual(len(registered), 1)
                self.assertEqual(registered[0].keyword, "offspring")
                self.assertEqual(
                    registered[0].keyword_cost,
                    game_state._safe_get_card(card_id).offspring_cost)
                queued = game_state.ability_handler.active_triggers
                self.assertEqual(len(queued), 1)
                self.assertIs(queued[0][0], registered[0])

                game_state.ability_handler.process_triggered_abilities()
                self.assertEqual(len(game_state.stack), 1)
                stack_context = game_state.stack[-1][3]
                self.assertTrue(getattr(
                    stack_context["ability"],
                    "_is_offspring_etb_trigger", False))

                original = game_state._safe_get_card(card_id)
                # Simulate continuous-effect write-back after the trigger is
                # frozen. Copyable values must still come from printed data.
                original.name = "Layered Offspring Name"
                original.mana_cost = "{W}"
                original.colors = [1, 0, 0, 0, 0]
                original.card_types = ["artifact"]
                original.subtypes = ["construct"]
                original.supertypes = ["legendary"]
                original.oracle_text = "Layered oracle text"
                original.keywords = [0] * len(original.keywords)
                self.assertTrue(game_state.resolve_top_of_stack())

                created = self._created_tokens(
                    game_state, controller, before)
                self.assertEqual(len(created), 1)
                token = game_state._safe_get_card(created[0])
                self.assertTrue(token.is_token)
                self.assertEqual(token.name, original.printed("name"))
                self.assertEqual(
                    token.mana_cost, original.printed("mana_cost"))
                self.assertEqual((token.power, token.toughness), (1, 1))
                self.assertEqual(
                    token.card_types, original.printed("card_types"))
                self.assertEqual(
                    token.subtypes, original.printed("subtypes"))
                self.assertEqual(
                    token.supertypes, original.printed("supertypes"))
                self.assertEqual(
                    token.oracle_text, original.printed("oracle_text"))
                self.assertEqual(
                    token.keywords, original.printed("keywords"))
                self.assertEqual(
                    original.printed("colors"), printed_colors)
                self.assertEqual(
                    token.colors, original.printed("colors"))
                self.assertEqual(
                    token.printed("colors"), original.printed("colors"))
                self.assertNotIn(
                    card_id, game_state._offspring_cost_paid_context)

            with self.subTest(card=card_name, paid=False):
                game_state = fresh(38430 + index)
                controller = game_state.p1
                card_id = inject_real_card(
                    game_state, controller, card_name, "hand")
                before = set(controller.get("tokens", []))
                self.assertTrue(game_state.move_card(
                    card_id, controller, "hand", controller, "battlefield",
                    cause="offspring_unpaid_test", context={}))
                registered = [
                    ability for ability
                    in game_state.ability_handler.registered_abilities.get(
                        card_id, [])
                    if getattr(
                        ability, "_is_offspring_etb_trigger", False)]
                self.assertEqual(len(registered), 1)
                self.assertEqual(
                    game_state.ability_handler.active_triggers, [])
                game_state.ability_handler.process_triggered_abilities()
                self.assertEqual(game_state.stack, [])
                self.assertEqual(
                    set(controller.get("tokens", [])), before)
                self.assertNotIn(
                    card_id, game_state._offspring_cost_paid_context)

    def test_real_manifold_cast_propagates_paid_offspring_context(self):
        for paid in (True, False):
            with self.subTest(paid=paid):
                game_state = fresh(38440 + int(paid))
                controller = game_state.p1
                card_id = inject_real_card(
                    game_state, controller, "Manifold Mouse", "hand")
                controller["mana_pool"] = {
                    "W": 0, "U": 0, "B": 0, "R": 1, "G": 0,
                    "C": 3 if paid else 1,
                }
                before = set(controller.get("tokens", []))
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]
                cast_context = {"source_zone": "hand"}
                if paid:
                    cast_context["pay_offspring"] = True
                self.assertTrue(game_state.cast_spell(
                    card_id, controller, context=cast_context))
                self.assertEqual(len(game_state.stack), 1)
                spell_context = game_state.stack[-1][3]
                self.assertNotIn("pay_offspring", spell_context)
                self.assertEqual(
                    bool(spell_context.get("paid_offspring")), paid)
                paid_cost = spell_context.get("final_paid_cost", {})
                self.assertEqual(paid_cost.get("R"), 1)
                self.assertEqual(
                    paid_cost.get("generic"), 3 if paid else 1)

                self.assertTrue(game_state.resolve_top_of_stack())
                self.assertIn(card_id, controller["battlefield"])
                registered = [
                    ability for ability
                    in game_state.ability_handler.registered_abilities.get(
                        card_id, [])
                    if getattr(
                        ability, "_is_offspring_etb_trigger", False)]
                self.assertEqual(len(registered), 1)

                if paid:
                    self.assertEqual(
                        len(game_state.ability_handler.active_triggers), 1)
                    game_state.ability_handler.process_triggered_abilities()
                    self.assertEqual(len(game_state.stack), 1)
                    self.assertTrue(game_state.resolve_top_of_stack())
                    created = self._created_tokens(
                        game_state, controller, before)
                    self.assertEqual(len(created), 1)
                    token = game_state._safe_get_card(created[0])
                    self.assertEqual(token.name, "Manifold Mouse")
                    self.assertEqual(
                        (token.power, token.toughness), (1, 1))
                    self.assertEqual(token.colors, [0, 0, 0, 1, 0])
                else:
                    self.assertEqual(
                        game_state.ability_handler.active_triggers, [])
                    self.assertEqual(
                        set(controller.get("tokens", [])), before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before)
                self.assertNotIn(
                    card_id, game_state._offspring_cost_paid_context)

    def test_offspring_payment_is_frozen_per_entry_and_cannot_leak(self):
        game_state = fresh(38490)
        controller = game_state.p1
        card_id = inject_real_card(
            game_state, controller, "Manifold Mouse", "hand")
        before = set(controller.get("tokens", []))

        self.assertTrue(game_state.move_card(
            card_id, controller, "hand", controller, "battlefield",
            context={"paid_offspring": True}))
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)
        queued_context = game_state.ability_handler.active_triggers[0][2]
        self.assertTrue(queued_context.get(
            "_offspring_cost_was_paid"))
        self.assertNotIn(
            card_id, game_state._offspring_cost_paid_context)

        # The same numeric card ID is reused after a zone change. Its unpaid
        # entry is a new object and must not inherit the earlier payment fact.
        self.assertTrue(game_state.move_card(
            card_id, controller, "battlefield", controller, "hand"))
        self.assertTrue(game_state.move_card(
            card_id, controller, "hand", controller, "battlefield",
            context={}))
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)
        self.assertNotIn(
            card_id, game_state._offspring_cost_paid_context)

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            len(self._created_tokens(game_state, controller, before)), 1)

        # Countering/discarding the queued trigger must not be the operation
        # that clears payment state; it was already consumed at entry time.
        game_state = fresh(38491)
        controller = game_state.p1
        card_id = inject_real_card(
            game_state, controller, "Pawpatch Recruit", "hand")
        self.assertTrue(game_state.move_card(
            card_id, controller, "hand", controller, "battlefield",
            context={"paid_offspring": True}))
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        game_state.stack.pop()  # Model the trigger being countered.
        self.assertNotIn(
            card_id, game_state._offspring_cost_paid_context)
        self.assertTrue(game_state.move_card(
            card_id, controller, "battlefield", controller, "hand"))
        self.assertTrue(game_state.move_card(
            card_id, controller, "hand", controller, "battlefield",
            context={}))
        self.assertEqual(game_state.ability_handler.active_triggers, [])


if __name__ == "__main__":
    unittest.main()
