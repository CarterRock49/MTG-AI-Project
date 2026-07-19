"""Regressions for supported and fail-closed token-copy instructions."""

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
    CreateTokenCopyOfTargetEffect,
    CreateTokenEffect,
    CreateTreasureEffect,
    DrawCardEffect,
    UnsupportedEffect,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from Playersim.card import Card  # noqa: E402


logging.disable(logging.CRITICAL)


class TokenCopyGuardRegressionTest(unittest.TestCase):
    def test_full_three_steps_spree_keeps_supported_copy_mode(self):
        game_state = fresh(38499)
        controller = game_state.p1
        card_id = inject_real_card(
            game_state, controller, "Three Steps Ahead", "hand")
        card = game_state._safe_get_card(card_id)
        effects = EffectFactory.create_effects(
            card.oracle_text, source_name=card.name)
        self.assertFalse(any(
            isinstance(effect, UnsupportedEffect) for effect in effects))
        copy_modes = [
            effect for effect in effects
            if isinstance(effect, CreateTokenCopyOfTargetEffect)]
        self.assertEqual(len(copy_modes), 1)

    def test_real_replacement_copy_text_fails_before_any_token_mutation(self):
        for index, card_name in enumerate((
                "Mirrormind Crown", "Moonlit Meditation")):
            with self.subTest(card=card_name):
                game_state = fresh(38495 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                source = game_state._safe_get_card(source_id)
                effects = EffectFactory.create_effects(
                    source.oracle_text, source_name=source.name)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], UnsupportedEffect)
                self.assertFalse(any(
                    isinstance(effect, CreateTokenEffect)
                    for effect in effects))

                before = set(controller.get("tokens", []))
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]
                with patch(
                        "Playersim.ability_types.logging.warning") as warning:
                    self.assertFalse(effects[0].apply(
                        game_state, source_id, controller, {}, context={}))
                self.assertEqual(warning.call_count, 1)
                self.assertEqual(set(controller.get("tokens", [])), before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before + 1)
                self.assertIn(
                    card_name,
                    game_state.fidelity_counters["unparsed_cards"])

    def test_unrelated_real_token_and_spell_copy_lines_are_not_coupled(self):
        game_state = fresh(38498)
        controller = game_state.p1

        emeritus_id = inject_real_card(
            game_state, controller,
            "Emeritus of Truce // Swords to Plowshares", "battlefield")
        emeritus = game_state._safe_get_card(emeritus_id)
        emeritus_effects = EffectFactory.create_effects(
            emeritus.oracle_text, source_name=emeritus.name)
        self.assertFalse(any(
            isinstance(effect, UnsupportedEffect)
            for effect in emeritus_effects))
        inkling_effects = [
            effect for effect in emeritus_effects
            if isinstance(effect, CreateTokenEffect)]
        self.assertEqual(len(inkling_effects), 1)
        before = set(controller.get("tokens", []))
        self.assertTrue(inkling_effects[0].apply(
            game_state, emeritus_id, controller,
            {"players": ["p1"]}, context={}))
        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        inkling = game_state._safe_get_card(created[0])
        self.assertEqual(inkling.name, "Inkling Token")
        self.assertEqual((inkling.power, inkling.toughness), (1, 1))
        self.assertEqual(inkling.subtypes, ["inkling"])
        self.assertEqual(inkling.colors, [1, 0, 1, 0, 0])
        self.assertTrue(game_state.check_keyword(created[0], "flying"))

        sword_id = inject_real_card(
            game_state, controller, "Sword of Wealth and Power",
            "battlefield")
        sword = game_state._safe_get_card(sword_id)
        sword_effects = EffectFactory.create_effects(
            sword.oracle_text, source_name=sword.name)
        self.assertEqual(
            sum(isinstance(effect, UnsupportedEffect)
                for effect in sword_effects),
            1)
        treasure_effects = [
            effect for effect in sword_effects
            if isinstance(effect, CreateTreasureEffect)]
        self.assertEqual(len(treasure_effects), 1)
        before = set(controller.get("tokens", []))
        self.assertTrue(treasure_effects[0].apply(
            game_state, sword_id, controller, {}, context={}))
        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        treasure = game_state._safe_get_card(created[0])
        self.assertEqual(treasure.name, "Treasure")
        self.assertEqual(treasure.card_types, ["artifact"])
        self.assertEqual(treasure.subtypes, ["treasure"])
        self.assertEqual((treasure.power, treasure.toughness), (0, 0))

    def test_unsupported_explicit_copy_grammars_never_create_vanilla_tokens(self):
        clauses = [
            "Create a token that's a copy of target creature.",
            "Create a token that is a copy of it.",
            "Create two tokens that are copies of target creature.",
        ]
        for index, clause in enumerate(clauses):
            with self.subTest(clause=clause):
                effects = EffectFactory.create_effects(clause)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], UnsupportedEffect)
                self.assertFalse(any(
                    isinstance(effect, CreateTokenEffect)
                    for effect in effects))
                self.assertFalse(any(
                    isinstance(effect, CreateTokenCopyOfTargetEffect)
                    for effect in effects))

                game_state = fresh(38500 + index)
                controller = game_state.p1
                source_id = inject_into_zone(game_state, controller, {
                    "name": f"Unsupported Copy Source {index}",
                    "mana_cost": "{2}",
                    "cmc": 2,
                    "type_line": "Artifact",
                    "oracle_text": clause,
                    "color_identity": [],
                }, "battlefield")
                target_id = inject_into_zone(game_state, controller, {
                    "name": f"Copy Target {index}",
                    "mana_cost": "{G}",
                    "cmc": 1,
                    "type_line": "Creature - Beast",
                    "oracle_text": "",
                    "power": 2,
                    "toughness": 2,
                    "color_identity": ["G"],
                }, "battlefield")
                before = set(controller.get("tokens", []))
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]
                with patch(
                        "Playersim.ability_types.logging.warning") as warning:
                    self.assertFalse(effects[0].apply(
                        game_state, source_id, controller,
                        {"creatures": [target_id]}, context={}))
                self.assertEqual(warning.call_count, 1)
                self.assertEqual(set(controller.get("tokens", [])), before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before + 1)
                self.assertIn(
                    f"Unsupported Copy Source {index}",
                    game_state.fidelity_counters["unparsed_cards"])

    def test_real_molten_duplication_exception_fails_closed(self):
        game_state = fresh(38510)
        controller = game_state.p1
        molten_id = inject_real_card(
            game_state, controller, "Molten Duplication", "hand")
        molten = game_state._safe_get_card(molten_id)
        self.assertIn(
            "except it's an artifact in addition to its other types",
            molten.oracle_text)
        target_id = inject_into_zone(game_state, controller, {
            "name": "Molten Copy Target",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Beast",
            "oracle_text": "Trample",
            "power": 3,
            "toughness": 2,
            "color_identity": ["G"],
        }, "battlefield")

        effects = EffectFactory.create_effects(
            molten.oracle_text, source_name=molten.name)
        unsupported = [
            effect for effect in effects
            if isinstance(effect, UnsupportedEffect)]
        self.assertEqual(len(unsupported), 1)
        self.assertFalse(any(
            isinstance(effect, CreateTokenCopyOfTargetEffect)
            for effect in effects))
        self.assertFalse(any(
            isinstance(effect, CreateTokenEffect)
            for effect in effects))

        before = set(controller.get("tokens", []))
        fidelity_before = game_state.fidelity_counters["unparsed_effects"]
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertFalse(unsupported[0].apply(
                game_state, molten_id, controller,
                {"creatures": [target_id]}, context={}))
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(set(controller.get("tokens", [])), before)
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)
        self.assertIn(
            "Molten Duplication",
            game_state.fidelity_counters["unparsed_cards"])

    def test_real_unsupported_token_riders_fail_before_creation(self):
        card_names = [
            "Auxiliary Boosters",
            "Dyadrine, Synthesis Amalgam",
            "Fire Navy Trebuchet",
            "Glimmer Seeker",
            "Haunt the Network",
            "Intrude on the Mind",
            "Kavaron, Memorial World",
            "Magda, the Hoardmaster",
            "Desculpting Blast",
            "Mysterio, Master of Illusion",
            "Outlaw Stitcher",
            "Pinnacle Emissary",
            "Retrieve the Esper",
            "Station Monitor",
            "Simulacrum Synthesizer",
            "Kavaron Harrier",
            "Harried Dronesmith",
            "Robot Domination",
            "The Last Ronin's Technique",
            "The Sibsig Ceremony",
            "Camera Launcher",
            "Vito, Fanatic of Aclazotz",
        ]
        for index, card_name in enumerate(card_names):
            with self.subTest(card=card_name):
                game_state = fresh(38520 + index)
                controller = game_state.p1
                source_id = inject_real_card(
                    game_state, controller, card_name, "hand")
                source = game_state._safe_get_card(source_id)
                effects = EffectFactory.create_effects(
                    source.oracle_text, source_name=source.name)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], UnsupportedEffect)
                self.assertFalse(any(
                    isinstance(effect, CreateTokenEffect)
                    for effect in effects))

                before = set(controller.get("tokens", []))
                fidelity_before = game_state.fidelity_counters[
                    "unparsed_effects"]
                with patch(
                        "Playersim.ability_types.logging.warning") as warning:
                    self.assertFalse(effects[0].apply(
                        game_state, source_id, controller, {}, context={}))
                self.assertEqual(warning.call_count, 1)
                self.assertEqual(set(controller.get("tokens", [])), before)
                self.assertEqual(
                    game_state.fidelity_counters["unparsed_effects"],
                    fidelity_before + 1)
                self.assertIn(
                    card_name,
                    game_state.fidelity_counters["unparsed_cards"])

    def test_real_bother_create_then_surveil_fails_as_one_instruction(self):
        game_state = fresh(38550)
        controller = game_state.p1
        source_id = inject_real_card(
            game_state, controller, "Fuss // Bother", "hand")
        source = game_state._safe_get_card(source_id)
        bother_text = source.faces[1]["oracle_text"]
        self.assertIn("Surveil 2", bother_text)
        effects = EffectFactory.create_effects(
            bother_text, source_name=source.name)
        self.assertEqual(len(effects), 1)
        self.assertIsInstance(effects[0], UnsupportedEffect)
        before = set(controller.get("tokens", []))
        fidelity_before = game_state.fidelity_counters["unparsed_effects"]
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertFalse(effects[0].apply(
                game_state, source_id, controller, {}, context={}))
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(set(controller.get("tokens", [])), before)
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before + 1)
        self.assertIn(
            "Fuss // Bother",
            game_state.fidelity_counters["unparsed_cards"])

    def test_real_glimmerburst_draw_then_create_resolves_both(self):
        game_state = fresh(38551)
        controller = game_state.p1
        source_id = inject_real_card(
            game_state, controller, "Glimmerburst", "hand")
        source = game_state._safe_get_card(source_id)
        controller["library"].clear()
        drawn_ids = [
            inject_into_zone(game_state, controller, {
                "name": f"Glimmerburst Draw {index}",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Instant",
                "oracle_text": "",
                "color_identity": [],
            }, "library")
            for index in range(2)
        ]
        effects = EffectFactory.create_effects(
            source.oracle_text, source_name=source.name)
        self.assertEqual(len(effects), 2)
        self.assertIsInstance(effects[0], DrawCardEffect)
        self.assertIsInstance(effects[1], CreateTokenEffect)
        self.assertFalse(any(
            isinstance(effect, UnsupportedEffect) for effect in effects))

        before = set(controller.get("tokens", []))
        fidelity_before = game_state.fidelity_counters["unparsed_effects"]
        success, _ = game_state._run_effect_sequence(
            effects, source_id, controller, {}, context={})
        self.assertTrue(success)
        self.assertTrue(set(drawn_ids).issubset(controller["hand"]))
        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        token = game_state._safe_get_card(created[0])
        self.assertEqual(token.name, "Glimmer Token")
        self.assertEqual((token.power, token.toughness), (1, 1))
        self.assertEqual(token.card_types, ["enchantment", "creature"])
        self.assertEqual(token.subtypes, ["glimmer"])
        self.assertEqual(token.colors, [1, 0, 0, 0, 0])
        self.assertEqual(
            game_state.fidelity_counters["unparsed_effects"],
            fidelity_before)

    def test_exact_three_steps_target_copy_remains_supported(self):
        clause = (
            "Create a token that's a copy of target artifact or creature "
            "you control.")
        effects = EffectFactory.create_effects(clause)
        self.assertEqual(len(effects), 1)
        self.assertIsInstance(effects[0], CreateTokenCopyOfTargetEffect)
        self.assertNotIsInstance(effects[0], UnsupportedEffect)

        game_state = fresh(38511)
        controller = game_state.p1
        target_id = inject_into_zone(game_state, controller, {
            "name": "Supported Green Copy Target",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Rabbit Warrior",
            "oracle_text": "Vigilance",
            "power": 2,
            "toughness": 3,
            "color_identity": ["G"],
        }, "battlefield")
        before = set(controller.get("tokens", []))
        self.assertTrue(effects[0].apply(
            game_state, None, controller,
            {"creatures": [target_id]}, context={}))
        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        token = game_state._safe_get_card(created[0])
        target = game_state._safe_get_card(target_id)
        self.assertEqual(token.name, target.printed("name"))
        self.assertEqual((token.power, token.toughness), (2, 3))
        self.assertEqual(token.card_types, target.printed("card_types"))
        self.assertEqual(token.subtypes, target.printed("subtypes"))
        self.assertEqual(token.colors, [0, 0, 0, 0, 1])

    def test_mirror_room_adds_reflection_to_printed_copyable_subtypes(self):
        clause = (
            "Create a token that's a copy of target creature you control, "
            "except it's a Reflection in addition to its other creature types.")
        effects = EffectFactory.create_effects(
            clause, source_name="Mirror Room // Fractured Realm")
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertIsInstance(effect, CreateTokenCopyOfTargetEffect)
        self.assertNotIsInstance(effect, UnsupportedEffect)
        self.assertEqual(effect.allowed_types, {"creature"})
        self.assertTrue(effect.controller_only)
        self.assertEqual(effect.additional_subtypes, ("reflection",))

        game_state = fresh(38512)
        controller = game_state.p1
        target_id = inject_into_zone(game_state, controller, {
            "name": "Printed Rabbit Visionary",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Rabbit Warrior",
            "oracle_text": "Vigilance",
            "power": 2,
            "toughness": 3,
            "color_identity": ["G"],
        }, "battlefield")
        target = game_state._safe_get_card(target_id)
        target.power = 8
        target.toughness = 9
        target.subtypes.append("temporary")
        target.keywords[Card.ALL_KEYWORDS.index("flying")] = 1

        before = set(controller.get("tokens", []))
        self.assertTrue(effect.apply(
            game_state, None, controller,
            {"creatures": [target_id]}, context={}))
        created = [
            token_id for token_id in controller.get("tokens", [])
            if token_id not in before]
        self.assertEqual(len(created), 1)
        token_id = created[0]
        token = game_state._safe_get_card(token_id)
        self.assertEqual(token.name, target.printed("name"))
        self.assertEqual(token.mana_cost, target.printed("mana_cost"))
        self.assertEqual((token.power, token.toughness), (2, 3))
        self.assertEqual(token.card_types, ["creature"])
        self.assertEqual(
            {subtype.casefold() for subtype in token.subtypes},
            {"rabbit", "warrior", "reflection"})
        self.assertNotIn("temporary", {
            subtype.casefold() for subtype in token.subtypes})
        self.assertEqual(
            sum(subtype.casefold() == "reflection"
                for subtype in token.subtypes),
            1)
        self.assertEqual(
            {subtype.casefold()
             for subtype in token.printed("subtypes", [])},
            {"rabbit", "warrior", "reflection"})
        self.assertIn("reflection", token.type_line.casefold())
        self.assertEqual(token.colors, [0, 0, 0, 0, 1])
        self.assertTrue(game_state.check_keyword(token_id, "vigilance"))
        self.assertFalse(game_state.check_keyword(token_id, "flying"))

        second_id = game_state.create_token_copy(token, controller)
        self.assertIsNotNone(second_id)
        second = game_state._safe_get_card(second_id)
        self.assertEqual(
            {subtype.casefold() for subtype in second.subtypes},
            {"rabbit", "warrior", "reflection"})
        self.assertEqual(
            sum(subtype.casefold() == "reflection"
                for subtype in second.subtypes),
            1)

    def test_mirror_room_copy_exception_stays_bounded_and_checks_target(self):
        for clause in (
                "Create a token that's a copy of target creature you control, "
                "except it has flying.",
                "Create a token that's a copy of target creature you control, "
                "except it's a Reflection and it has flying."):
            with self.subTest(clause=clause):
                effects = EffectFactory.create_effects(clause)
                self.assertEqual(len(effects), 1)
                self.assertIsInstance(effects[0], UnsupportedEffect)

        mirror_clause = (
            "Create a token that's a copy of target creature you control, "
            "except it's a Reflection in addition to its other types.")
        effect = EffectFactory.create_effects(mirror_clause)[0]
        self.assertIsInstance(effect, CreateTokenCopyOfTargetEffect)

        game_state = fresh(38513)
        controller, opponent = game_state.p1, game_state.p2
        own_artifact = inject_into_zone(game_state, controller, {
            "name": "Mirror Artifact",
            "mana_cost": "{2}",
            "cmc": 2,
            "type_line": "Artifact",
            "oracle_text": "",
            "color_identity": [],
        }, "battlefield")
        opposing_creature = inject_into_zone(game_state, opponent, {
            "name": "Opposing Mirror Target",
            "mana_cost": "{1}{R}",
            "cmc": 2,
            "type_line": "Creature - Goblin",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["R"],
        }, "battlefield")
        departing_creature = inject_into_zone(game_state, controller, {
            "name": "Departing Mirror Target",
            "mana_cost": "{1}{U}",
            "cmc": 2,
            "type_line": "Creature - Wizard",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["U"],
        }, "battlefield")
        self.assertTrue(game_state.move_card(
            departing_creature, controller, "battlefield", controller,
            "graveyard", cause="mirror_target_left"))

        before = set(controller.get("tokens", []))
        self.assertFalse(effect.apply(
            game_state, None, controller,
            {"artifacts": [own_artifact]}, context={}))
        self.assertFalse(effect.apply(
            game_state, None, controller,
            {"creatures": [opposing_creature]}, context={}))
        self.assertFalse(effect.apply(
            game_state, None, controller,
            {"creatures": [departing_creature]}, context={}))
        self.assertEqual(set(controller.get("tokens", [])), before)


if __name__ == "__main__":
    unittest.main()
