"""Regressions for multicolor token conjunction parsing."""

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

from scenario_test import fresh, inject_real_card  # noqa: E402
from Playersim.ability_types import (  # noqa: E402
    CreateTokenEffect,
    DrawCardEffect,
    TriggeredAbility,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from Playersim.card import Card  # noqa: E402


logging.disable(logging.CRITICAL)


class StormchaserTalentTokenRegressionTest(unittest.TestCase):
    @staticmethod
    def _registered_triggers(game_state, card_id):
        return [
            ability for ability in
            game_state.ability_handler.registered_abilities[card_id]
            if isinstance(ability, TriggeredAbility)
        ]

    def _enter_and_resolve_single_trigger(self, card_name, seed):
        game_state = fresh(seed)
        controller = game_state.p1
        card_id = inject_real_card(
            game_state, controller, card_name, "hand")
        tokens_before = set(controller.get("tokens", []))

        self.assertTrue(game_state.move_card(
            card_id, controller, "hand", controller, "battlefield"))
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][:3], (
            "TRIGGER", card_id, controller))

        with patch("Playersim.ability_types.logging.warning") as runtime_warning, \
                patch("Playersim.ability_utils.logging.warning") as parser_warning:
            self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(runtime_warning.call_args_list, [])
        self.assertEqual(parser_warning.call_args_list, [])

        created = set(controller.get("tokens", [])) - tokens_before
        self.assertEqual(len(created), 1)
        token_id = created.pop()
        self.assertIn(token_id, controller["battlefield"])
        return game_state, card_id, token_id

    def assertCreatureToken(
            self, game_state, token_id, *, name, subtype, colors,
            keyword=None, power=1, toughness=1, legendary=False):
        token = game_state._safe_get_card(token_id)
        self.assertIsNotNone(token)
        self.assertTrue(token.is_token)
        self.assertEqual(token.name, name)
        self.assertEqual(
            (token.power, token.toughness), (power, toughness))
        self.assertEqual(token.card_types, ["creature"])
        self.assertEqual(token.subtypes, [subtype])
        self.assertEqual(token.colors, colors)
        self.assertEqual("legendary" in token.supertypes, legendary)
        if keyword:
            self.assertTrue(game_state.check_keyword(token_id, keyword))
            self.assertEqual(
                token.keywords[Card.ALL_KEYWORDS.index(keyword)], 1)

    def test_stormchaser_base_class_etb_creates_printed_otter(self):
        game_state = fresh(38201)
        controller = game_state.p1
        card_id = inject_real_card(
            game_state, controller, "Stormchaser's Talent", "hand")
        card = game_state._safe_get_card(card_id)

        self.assertEqual(card.current_level, 1)
        self.assertEqual(card.all_abilities, [
            "When this Class enters, create a 1/1 blue and red Otter "
            "creature token with prowess.",
        ])

        tokens_before = set(controller.get("tokens", []))
        self.assertTrue(game_state.move_card(
            card_id, controller, "hand", controller, "battlefield"))
        triggers = self._registered_triggers(game_state, card_id)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0].trigger_condition,
                         "when this class enters")
        self.assertEqual(
            triggers[0].effect,
            "create a 1/1 blue and red otter creature token with prowess")
        self.assertNotIn("level 2", triggers[0].effect_text.lower())
        self.assertNotIn(
            "whenever you cast an instant or sorcery spell",
            triggers[0].effect_text.lower())
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][:3], (
            "TRIGGER", card_id, controller))
        with patch("Playersim.ability_types.logging.warning") as runtime_warning, \
                patch("Playersim.ability_utils.logging.warning") as parser_warning:
            self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(runtime_warning.call_args_list, [])
        self.assertEqual(parser_warning.call_args_list, [])

        created = set(controller.get("tokens", [])) - tokens_before
        self.assertEqual(len(created), 1)
        token_id = created.pop()
        self.assertCreatureToken(
            game_state, token_id,
            name="Otter Token", subtype="otter",
            colors=[0, 1, 0, 1, 0], keyword="prowess")

    def test_nonclass_multicolor_etb_creates_printed_inkling(self):
        game_state, card_id, token_id = \
            self._enter_and_resolve_single_trigger("Eager Glyphmage", 38202)
        triggers = self._registered_triggers(game_state, card_id)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(
            triggers[0].effect,
            "create a 1/1 white and black inkling creature token with flying")
        self.assertCreatureToken(
            game_state, token_id,
            name="Inkling Token", subtype="inkling",
            colors=[1, 0, 1, 0, 0], keyword="flying")

    def test_named_legendary_token_real_etb_creates_exact_angelo(self):
        game_state, card_id, token_id = \
            self._enter_and_resolve_single_trigger("Rinoa Heartilly", 38204)
        enters_trigger = next(
            ability for ability in self._registered_triggers(
                game_state, card_id)
            if ability.trigger_condition == "when rinoa heartilly enters")
        self.assertEqual(
            enters_trigger.effect,
            "create angelo, a legendary 1/1 green and white dog creature "
            "token")
        self.assertCreatureToken(
            game_state, token_id,
            name="Angelo", subtype="dog",
            colors=[1, 0, 0, 0, 1], legendary=True)

    def test_all_four_multicolor_named_token_clauses_parse_exactly(self):
        cases = [
            (
                "create Darkstar, a legendary 2/2 white and black Dog "
                "creature token",
                "Darkstar", 2, 2, ["White", "Black"], ["dog"], [],
            ),
            (
                "create Voja Fenstalker, a legendary 5/5 green and white "
                "Wolf creature token with trample",
                "Voja Fenstalker", 5, 5,
                ["Green", "White"], ["wolf"], ["Trample"],
            ),
            (
                "create Primo, the Indivisible, a legendary 0/0 green and "
                "blue Fractal creature token, then put that many +1/+1 "
                "counters on it",
                "Primo, the Indivisible", 0, 0,
                ["Green", "Blue"], ["fractal"], [],
            ),
            (
                "create Angelo, a legendary 1/1 green and white Dog "
                "creature token",
                "Angelo", 1, 1, ["Green", "White"], ["dog"], [],
            ),
        ]
        for (text, name, power, toughness,
             colors, subtypes, keywords) in cases:
            with self.subTest(name=name):
                effects = EffectFactory.create_effects(text)
                self.assertIsInstance(effects[0], CreateTokenEffect)
                token_effect = effects[0]
                self.assertEqual(token_effect.token_name, name)
                self.assertEqual(
                    (token_effect.power, token_effect.toughness),
                    (power, toughness))
                self.assertEqual(token_effect.colors, colors)
                self.assertTrue(token_effect.is_legendary)
                self.assertEqual(
                    token_effect.token_card_types, ["creature"])
                self.assertEqual(token_effect.token_subtypes, subtypes)
                self.assertEqual(token_effect.keywords, keywords)

    def test_single_color_token_and_followup_action_still_split(self):
        effects = EffectFactory.create_effects(
            "Create two 1/1 white Soldier creature tokens with flying and "
            "draw a card.")
        self.assertEqual([type(effect) for effect in effects], [
            CreateTokenEffect, DrawCardEffect])
        token_effect = effects[0]
        self.assertEqual(token_effect.count, 2)
        self.assertEqual(token_effect.colors, ["White"])
        self.assertEqual(token_effect.creature_type, "Soldier")
        self.assertEqual(token_effect.keywords, ["Flying"])

        game_state = fresh(38203)
        controller = game_state.p1
        hand_before = len(controller["hand"])
        for effect in effects:
            self.assertTrue(effect.apply(game_state, None, controller, {}))
        self.assertEqual(len(controller["hand"]), hand_before + 1)
        soldier_ids = [
            token_id for token_id in controller.get("tokens", [])
            if game_state._safe_get_card(token_id).name == "Soldier Token"]
        self.assertEqual(len(soldier_ids), 2)
        for token_id in soldier_ids:
            self.assertCreatureToken(
                game_state, token_id,
                name="Soldier Token", subtype="soldier",
                colors=[1, 0, 0, 0, 0], keyword="flying")

    def test_post_token_comma_and_action_still_split(self):
        effects = EffectFactory.create_effects(
            "Create a 1/1 green Saproling creature token, then draw a card.")
        self.assertEqual([type(effect) for effect in effects], [
            CreateTokenEffect, DrawCardEffect])
        token_effect = effects[0]
        self.assertIsNone(token_effect.token_name)
        self.assertFalse(token_effect.is_legendary)
        self.assertEqual(token_effect.creature_type, "Saproling")
        self.assertEqual(token_effect.colors, ["Green"])


if __name__ == "__main__":
    unittest.main()
