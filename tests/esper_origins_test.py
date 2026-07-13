"""End-to-end fidelity for Esper Origins // Summon: Esper Maduin."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from Playersim.ability_types import (  # noqa: E402
    AddManaEffect,
    EsperSagaChapterThreeEffect,
    EsperSagaRevealPermanentEffect,
    RuleDeclarationEffect,
    TriggeredAbility,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_card,
    inject_into_zone,
    inject_real_card,
)


logging.disable(logging.CRITICAL)


def fixture_card(name, type_line="Instant", power=None, toughness=None):
    data = {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": type_line,
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
        "color_identity": [],
    }
    if power is not None:
        data["power"] = power
    if toughness is not None:
        data["toughness"] = toughness
    return data


class EsperOriginsTest(unittest.TestCase):
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
        for player in (game_state.p1, game_state.p2):
            for permanent_id in list(player.get("battlefield", [])):
                self.assertTrue(game_state.move_card(
                    permanent_id, player, "battlefield", player, "library"))
            for zone in ("library", "hand", "battlefield", "graveyard",
                         "exile"):
                player[zone] = []
            player.get("tapped_permanents", set()).clear()
            player.get("entered_battlefield_this_turn", set()).clear()
            player.get("saga_counters", {}).clear()
            player["mana_pool"] = {
                "W": 20, "U": 20, "B": 20,
                "R": 20, "G": 20, "C": 20,
            }
        return game_state

    @staticmethod
    def _put_on_library(game_state, player, data):
        card_id = inject_card(game_state, data)
        player["library"].append(card_id)
        game_state._last_card_locations[card_id] = (player, "library")
        return card_id

    def _finish_surveil(self, *, first_action, second_action):
        handler = get_env().action_handler
        self.assertTrue(handler._handle_scry_surveil_choice(
            None, {}, action_index=first_action)[1])
        self.assertTrue(handler._handle_scry_surveil_choice(
            None, {}, action_index=second_action)[1])

    def _process_one_trigger(self, game_state, source_id, chapter):
        matching = [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and getattr(entry[0], "saga_chapter", None) == chapter
        ]
        self.assertEqual(len(matching), 1)
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.resolve_top_of_stack())

    def test_flashback_runs_front_and_all_three_saga_chapters(self):
        game_state = self._state(196301)
        controller, opponent = game_state.p1, game_state.p2
        revealed_permanent = self._put_on_library(
            game_state, controller,
            fixture_card("Revealed Forest", "Basic Land - Forest"))
        surveil_grave = self._put_on_library(
            game_state, controller,
            fixture_card("Surveilled Instant"))
        esper = inject_real_card(
            game_state, controller,
            "Esper Origins // Summon: Esper Maduin", "graveyard")
        life_before = controller["life"]

        self.assertTrue(game_state.cast_spell(
            esper, controller, {
                "source_zone": "graveyard",
                "flashback_cast": True,
            }))
        cast_context = game_state.stack[-1][3]
        self.assertEqual(cast_context["source_zone"], "graveyard")
        self.assertEqual(cast_context["use_alt_cost"], "flashback")
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.choice_context["type"], "surveil")

        # Keep the first card on top and bin the second. The continuation then
        # gains life and performs exile -> transformed battlefield atomically.
        self._finish_surveil(first_action=306, second_action=305)
        card = game_state._safe_get_card(esper)
        self.assertEqual(controller["life"], life_before + 2)
        self.assertIn(surveil_grave, controller["graveyard"])
        self.assertEqual(controller["library"][0], revealed_permanent)
        self.assertIn(esper, controller["battlefield"])
        self.assertEqual((card.current_face, card.name),
                         (1, "Summon: Esper Maduin"))
        self.assertEqual(card.counters.get("finality"), 1)
        self.assertEqual(controller["saga_counters"].get(esper), 1)

        self._process_one_trigger(game_state, esper, 1)
        self.assertIn(revealed_permanent, controller["hand"])
        self.assertNotIn(revealed_permanent, controller["library"])

        green_before = controller["mana_pool"]["G"]
        game_state.advance_saga_counters(controller)
        self._process_one_trigger(game_state, esper, 2)
        self.assertEqual(controller["mana_pool"]["G"], green_before + 2)

        ally = inject_into_zone(
            game_state, controller,
            fixture_card("Maduin Ally", "Creature - Elemental", 2, 2),
            "battlefield")
        enemy = inject_into_zone(
            game_state, opponent,
            fixture_card("Maduin Enemy", "Creature - Elemental", 3, 3),
            "battlefield")
        game_state.advance_saga_counters(controller)
        self._process_one_trigger(game_state, esper, 3)

        self.assertEqual((game_state._safe_get_card(ally).power,
                          game_state._safe_get_card(ally).toughness), (4, 4))
        self.assertTrue(game_state.check_keyword(ally, "trample"))
        self.assertEqual((game_state._safe_get_card(enemy).power,
                          game_state._safe_get_card(enemy).toughness), (3, 3))
        self.assertFalse(game_state.check_keyword(enemy, "trample"))
        # "Other" excludes Maduin; sacrifice after III is replaced by its
        # finality counter, and the stale lore entry leaves with the object.
        self.assertIn(esper, controller["exile"])
        self.assertNotIn(esper, controller["graveyard"])
        self.assertNotIn(esper, controller["saga_counters"])
        self.assertEqual(game_state._safe_get_card(esper).current_face, 0)

    def test_hand_cast_gains_life_but_does_not_transform(self):
        game_state = self._state(196302)
        controller = game_state.p1
        first = self._put_on_library(
            game_state, controller, fixture_card("Hand Cast Top A"))
        second = self._put_on_library(
            game_state, controller, fixture_card("Hand Cast Top B"))
        esper = inject_real_card(
            game_state, controller,
            "Esper Origins // Summon: Esper Maduin", "hand")
        life_before = controller["life"]

        self.assertTrue(game_state.cast_spell(esper, controller))
        self.assertTrue(game_state.resolve_top_of_stack())
        self._finish_surveil(first_action=305, second_action=305)

        self.assertEqual(controller["life"], life_before + 2)
        self.assertIn(first, controller["graveyard"])
        self.assertIn(second, controller["graveyard"])
        self.assertIn(esper, controller["graveyard"])
        self.assertNotIn(esper, controller["battlefield"])
        self.assertEqual(game_state._safe_get_card(esper).current_face, 0)
        self.assertFalse(any(
            getattr(ability, "saga_chapter", None)
            for ability, _, _ in game_state.ability_handler.active_triggers))

    def test_chapter_one_leaves_a_nonpermanent_on_top(self):
        game_state = self._state(196303)
        controller = game_state.p1
        instant = self._put_on_library(
            game_state, controller, fixture_card("Nonpermanent Reveal"))
        esper = inject_real_card(
            game_state, controller,
            "Esper Origins // Summon: Esper Maduin", "exile")
        game_state._safe_get_card(esper).set_current_face(1)

        self.assertTrue(game_state.move_card(
            esper, controller, "exile", controller, "battlefield"))
        self._process_one_trigger(game_state, esper, 1)
        self.assertEqual(controller["library"][0], instant)
        self.assertNotIn(instant, controller["hand"])

    def test_every_printed_surface_has_a_concrete_effect(self):
        short_flashback = EffectFactory.create_effects(
            "Cast from graveyard, then exile.",
            source_name="Esper Origins // Summon: Esper Maduin")
        self.assertEqual(len(short_flashback), 1)
        self.assertIsInstance(short_flashback[0], RuleDeclarationEffect)

        expectations = (
            ("Reveal the top card of your library. If it's a permanent card, "
             "put it into your hand.", EsperSagaRevealPermanentEffect),
            ("Add {G}{G}.", AddManaEffect),
            ("Other creatures you control get +2/+2 and gain trample until "
             "end of turn.", EsperSagaChapterThreeEffect),
        )
        for text, expected_type in expectations:
            effects = EffectFactory.create_effects(
                text,
                source_name="Esper Origins // Summon: Esper Maduin")
            self.assertEqual(len(effects), 1, text)
            self.assertIsInstance(effects[0], expected_type)

        game_state = self._state(196304)
        esper = inject_real_card(
            game_state, game_state.p1,
            "Esper Origins // Summon: Esper Maduin", "exile")
        card = game_state._safe_get_card(esper)
        card.set_current_face(1)
        game_state.ability_handler._parse_and_register_abilities(esper, card)
        chapters = [
            ability.saga_chapter
            for ability in game_state.ability_handler.registered_abilities[esper]
            if isinstance(ability, TriggeredAbility)
            and hasattr(ability, "saga_chapter")
        ]
        self.assertEqual(chapters, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
