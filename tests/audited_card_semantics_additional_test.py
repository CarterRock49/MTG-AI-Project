"""Focused end-to-end regressions for the second Standard card audit."""

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

from scenario_test import (  # noqa: E402
    fresh, get_env, inject_card, inject_into_zone, inject_real_card)
from Playersim.ability_types import (  # noqa: E402
    ArchdruidSearchEffect, DestroyEffect, ImpulseDrawEffect,
    PreventDamageEffect)
from Playersim.enhanced_card_evaluator import EnhancedCardEvaluator  # noqa: E402


class AuditedCardSemanticsAdditionalTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.stack = []
        game_state.choice_context = None
        game_state.targeting_context = None
        game_state.sacrifice_context = None
        game_state.priority_player = game_state.p1
        game_state.priority_pass_count = 0
        game_state._last_card_locations = {}
        for player in (game_state.p1, game_state.p2):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["damage_counters"] = {}
            player["deathtouch_damage"] = {}
            player["life"] = 20
            player["mana_pool"] = {
                "W": 0, "U": 0, "B": 0,
                "R": 0, "G": 0, "C": 0,
            }
        return game_state, get_env().action_handler

    def _select_target(self, game_state, handler, player, target_id):
        candidates = handler._get_target_selection_candidates(
            player, game_state.targeting_context)
        self.assertIn(target_id, candidates)
        absolute_index = candidates.index(target_id)
        game_state.targeting_context["target_page"] = absolute_index // 10
        _, success = handler._handle_select_target(
            absolute_index % 10, {})
        self.assertTrue(success)

    def test_flood_maw_promised_branch_targets_artifact_and_gives_tapped_fish(self):
        game_state, handler = self._state(2101)
        caster, opponent = game_state.p1, game_state.p2
        flood_maw = inject_real_card(
            game_state, caster, "Into the Flood Maw", "hand")
        opposing_artifact = inject_into_zone(game_state, opponent, {
            "name": "Gift Bounce Artifact", "mana_cost": "{2}",
            "type_line": "Artifact", "oracle_text": "",
        }, "battlefield")
        opposing_land = inject_into_zone(game_state, opponent, {
            "name": "Gift Excluded Land", "mana_cost": "",
            "type_line": "Land", "oracle_text": "{T}: Add {C}.",
        }, "battlefield")
        own_artifact = inject_into_zone(game_state, caster, {
            "name": "Gift Own Artifact", "mana_cost": "{2}",
            "type_line": "Artifact", "oracle_text": "",
        }, "battlefield")
        caster["mana_pool"]["U"] = 1

        card = game_state._safe_get_card(flood_maw)
        self.assertTrue(
            handler._targets_available(card, caster, opponent),
            "the promised branch did not make a cast legal without a creature")
        self.assertTrue(game_state.cast_spell(
            flood_maw, caster, {"source_zone": "hand"}))
        self.assertEqual(game_state.choice_context.get("type"), "gift")
        gift_mask = handler.generate_valid_actions()
        self.assertTrue(gift_mask[353])
        self.assertFalse(
            gift_mask[11],
            "decline was exposed with no legal ordinary creature target")
        self.assertFalse(game_state.complete_gift_choice(False))
        self.assertTrue(handler._handle_choose_mode(0, {})[1])
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        candidates = handler._get_target_selection_candidates(
            caster, game_state.targeting_context)
        self.assertIn(opposing_artifact, candidates)
        self.assertNotIn(opposing_land, candidates)
        self.assertNotIn(own_artifact, candidates)
        self._select_target(
            game_state, handler, caster, opposing_artifact)

        tapped_during_entry = []
        state_type = type(game_state)
        finish_entry = state_type._finish_battlefield_entry_triggers

        def observe_entry(state, card_id, controller, context):
            card = state._safe_get_card(card_id)
            if getattr(card, "name", "") == "Fish Token":
                tapped_during_entry.append(
                    card_id in controller.get("tapped_permanents", set()))
            return finish_entry(state, card_id, controller, context)

        with patch.object(
                state_type, "_finish_battlefield_entry_triggers",
                new=observe_entry):
            self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(opposing_artifact, opponent["hand"])
        fish_tokens = [
            card_id for card_id in opponent["battlefield"]
            if getattr(game_state._safe_get_card(card_id), "name", "")
            == "Fish Token"]
        self.assertEqual(len(fish_tokens), 1)
        self.assertEqual(
            tapped_during_entry, [True],
            "the gifted Fish was marked tapped only after ETB processing")
        self.assertIn(fish_tokens[0], opponent["tapped_permanents"])
        fish = game_state._safe_get_card(fish_tokens[0])
        self.assertEqual((fish.power, fish.toughness), (1, 1))
        self.assertIn("creature", fish.card_types)

    def test_cori_entry_condition_and_next_turn_impulse_permission(self):
        tapped_state, _ = self._state(2102)
        tapped_cori = inject_real_card(
            tapped_state, tapped_state.p1,
            "Cori Mountain Monastery", "battlefield")
        self.assertIn(tapped_cori, tapped_state.p1["tapped_permanents"])

        game_state, handler = self._state(2103)
        player = game_state.p1
        inject_into_zone(game_state, player, {
            "name": "Audit Plains", "mana_cost": "",
            "type_line": "Basic Land - Plains", "card_types": ["land"],
            "supertypes": ["basic"], "subtypes": ["plains"],
            "oracle_text": "{T}: Add {W}.",
        }, "battlefield")
        cori = inject_real_card(
            game_state, player, "Cori Mountain Monastery", "battlefield")
        self.assertNotIn(cori, player["tapped_permanents"])

        top_card = inject_card(game_state, {
            "name": "Cori Impulse Card", "mana_cost": "{1}",
            "type_line": "Artifact", "oracle_text": "",
        })
        player["library"] = [top_card]
        game_state._last_card_locations[top_card] = (player, "library")
        abilities = game_state.ability_handler.get_activated_abilities(cori)
        impulse_index = next(
            index for index, ability in enumerate(abilities)
            if "exile the top" in str(
                getattr(ability, "effect_text", "")).lower())
        player["mana_pool"].update({"R": 1, "C": 3})
        self.assertTrue(game_state.ability_handler.can_activate_ability(
            cori, impulse_index, player))
        self.assertTrue(handler._handle_activate_ability(None, {
            "battlefield_idx": player["battlefield"].index(cori),
            "ability_idx": impulse_index, "controller_id": "p1",
        })[1])
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(top_card, player["exile"])
        self.assertIn(top_card, game_state.cards_castable_from_exile)
        self.assertEqual(game_state.impulse_until_next_turn[top_card], 3)

        game_state.turn = 2
        game_state._cleanup_step_actions(player, discard_to_max=False)
        self.assertIn(top_card, game_state.cards_castable_from_exile)
        game_state.turn = 3
        game_state._cleanup_step_actions(player, discard_to_max=False)
        self.assertNotIn(top_card, game_state.cards_castable_from_exile)
        self.assertNotIn(top_card, game_state.impulse_until_next_turn)
        self.assertTrue(ImpulseDrawEffect(
            duration="end_of_your_next_turn").apply(
                game_state, cori, player, {}),
            "an empty-library impulse instruction was treated as a failure")

        impulse_land = inject_card(game_state, {
            "name": "Cori Exile Land", "mana_cost": "", "cmc": 0,
            "type_line": "Land", "card_types": ["land"],
            "oracle_text": "{T}: Add {G}.",
        })
        player["library"] = [impulse_land]
        game_state._last_card_locations[impulse_land] = (player, "library")
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        self.assertTrue(ImpulseDrawEffect(
            duration="end_of_your_next_turn").apply(
                game_state, cori, player, {}))
        exile_options = game_state.get_exile_cast_options(player)
        land_option = next(
            index for index, option in enumerate(exile_options)
            if option["card_id"] == impulse_land)
        self.assertTrue(handler.generate_valid_actions()[230 + land_option])
        self.assertTrue(handler._handle_cast_from_exile(land_option)[1])
        self.assertIn(impulse_land, player["battlefield"])
        self.assertEqual(game_state.stack, [])
        self.assertEqual(player["lands_played_this_turn"], 1)
        self.assertNotIn(impulse_land, game_state.cards_castable_from_exile)

        second_land = inject_card(game_state, {
            "name": "Cori Second Exile Land", "mana_cost": "", "cmc": 0,
            "type_line": "Land", "card_types": ["land"],
            "oracle_text": "{T}: Add {U}.",
        })
        player["library"] = [second_land]
        game_state._last_card_locations[second_land] = (player, "library")
        self.assertTrue(ImpulseDrawEffect().apply(
            game_state, cori, player, {}))
        second_option = next(
            index for index, option in enumerate(
                game_state.get_exile_cast_options(player))
            if option["card_id"] == second_land)
        self.assertFalse(handler.generate_valid_actions()[230 + second_option])

    def test_impulse_trackers_clear_when_the_exile_object_leaves(self):
        game_state, _ = self._state(2111)
        player = game_state.p1
        spell = inject_into_zone(game_state, player, {
            "name": "Departing Impulse Spell", "mana_cost": "", "cmc": 0,
            "type_line": "Artifact", "card_types": ["artifact"],
            "oracle_text": "",
        }, "exile")
        game_state.cards_castable_from_exile.add(spell)
        game_state.impulse_until_eot.add(spell)
        game_state.impulse_until_next_turn[spell] = 3
        self.assertTrue(game_state.cast_spell(
            spell, player, {"source_zone": "exile"}))
        self.assertNotIn(spell, game_state.cards_castable_from_exile)
        self.assertNotIn(spell, game_state.impulse_until_eot)
        self.assertNotIn(spell, game_state.impulse_until_next_turn)

        departing = inject_into_zone(game_state, player, {
            "name": "Departing Impulse Card", "mana_cost": "{1}",
            "type_line": "Artifact", "card_types": ["artifact"],
            "oracle_text": "",
        }, "exile")
        game_state.cards_castable_from_exile.add(departing)
        game_state.impulse_until_eot.add(departing)
        game_state.impulse_until_next_turn[departing] = 3
        self.assertTrue(game_state.move_card(
            departing, player, "exile", player, "hand",
            cause="audit_exile_departure"))
        self.assertNotIn(departing, game_state.cards_castable_from_exile)
        self.assertNotIn(departing, game_state.impulse_until_eot)
        self.assertNotIn(departing, game_state.impulse_until_next_turn)

        # A later exile object with the same physical card ID can receive a
        # fresh duration without an older cleanup entry revoking it.
        self.assertTrue(game_state.move_card(
            departing, player, "hand", player, "exile",
            cause="audit_new_exile_object"))
        game_state.cards_castable_from_exile.add(departing)
        game_state.impulse_until_next_turn[departing] = 5
        game_state.turn = 3
        game_state._cleanup_step_actions(player, discard_to_max=False)
        self.assertIn(departing, game_state.cards_castable_from_exile)
        self.assertEqual(game_state.impulse_until_next_turn[departing], 5)

        game_state.impulse_until_eot.add(departing)
        game_state.exile_alternative_costs[departing] = "{1}"
        clone = game_state.clone()
        self.assertIsNotNone(clone)
        self.assertIn(departing, clone.impulse_until_eot)
        self.assertEqual(clone.impulse_until_next_turn[departing], 5)
        self.assertEqual(clone.exile_alternative_costs[departing], "{1}")
        clone._clear_exile_play_permissions(clone.p1, departing)
        self.assertIn(departing, game_state.impulse_until_eot)
        self.assertIn(departing, game_state.impulse_until_next_turn)
        self.assertIn(departing, game_state.exile_alternative_costs)

    def test_archdruid_counter_then_damage_uses_modified_creature_power(self):
        game_state, handler = self._state(2104)
        caster, opponent = game_state.p1, game_state.p2
        charm = inject_real_card(
            game_state, caster, "Archdruid's Charm", "hand")
        friendly = inject_into_zone(game_state, caster, {
            "name": "Charm Fighter", "mana_cost": "{1}{G}",
            "type_line": "Creature - Bear", "oracle_text": "",
            "power": 2, "toughness": 2,
        }, "battlefield")
        enemy = inject_into_zone(game_state, opponent, {
            "name": "Charm Damage Target", "mana_cost": "{2}{B}",
            "type_line": "Creature - Zombie", "oracle_text": "",
            "power": 3, "toughness": 3,
        }, "battlefield")
        caster["mana_pool"]["G"] = 3

        self.assertTrue(game_state.cast_spell(
            charm, caster, {"source_zone": "hand"}))
        self.assertEqual(game_state.choice_context.get("type"), "choose_mode")
        self.assertTrue(handler._handle_choose_mode(1, {})[1])
        slots = game_state.targeting_context.get("target_slots", [])
        self.assertEqual(len(slots), 2)
        self._select_target(game_state, handler, caster, friendly)
        self._select_target(game_state, handler, caster, enemy)

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            game_state._safe_get_card(friendly).counters.get("+1/+1"), 1)
        self.assertEqual(opponent["damage_counters"].get(enemy), 3)
        game_state.check_state_based_actions()
        self.assertIn(enemy, opponent["graveyard"])
        self.assertNotIn(friendly, caster.get("damage_counters", {}))

    def test_archdruid_exiles_enchantment_and_search_can_fail_to_find(self):
        game_state, handler = self._state(2105)
        caster, opponent = game_state.p1, game_state.p2
        charm = inject_real_card(
            game_state, caster, "Archdruid's Charm", "hand")
        enchantment = inject_into_zone(game_state, opponent, {
            "name": "Charm Enchantment", "mana_cost": "{2}",
            "type_line": "Enchantment", "oracle_text": "",
        }, "battlefield")
        caster["mana_pool"]["G"] = 3
        self.assertTrue(game_state.cast_spell(
            charm, caster, {"source_zone": "hand"}))
        self.assertTrue(handler._handle_choose_mode(2, {})[1])
        self._select_target(game_state, handler, caster, enchantment)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(enchantment, opponent["exile"])

        search_state, search_handler = self._state(2106)
        player = search_state.p1
        source = inject_real_card(
            search_state, player, "Archdruid's Charm", "graveyard")
        land = inject_card(search_state, {
            "name": "Search Land", "mana_cost": "", "type_line": "Land",
            "card_types": ["land"], "oracle_text": "{T}: Add {G}.",
        })
        creature = inject_card(search_state, {
            "name": "Search Creature", "mana_cost": "{2}",
            "type_line": "Creature - Elf", "card_types": ["creature"],
            "oracle_text": "", "power": 2, "toughness": 2,
        })
        player["library"] = [land, creature]
        for card_id in player["library"]:
            search_state._last_card_locations[card_id] = (player, "library")
        self.assertTrue(ArchdruidSearchEffect().apply(
            search_state, source, player, {}))
        self.assertTrue(search_state.choice_context.get("optional"))
        shuffled = []

        def record_shuffle(state, shuffled_player):
            shuffled.append(shuffled_player)
            return True

        with patch.object(
                type(search_state), "shuffle_library", new=record_shuffle):
            self.assertTrue(search_handler._handle_pass_priority(None)[1])
        self.assertEqual(shuffled, [player])
        self.assertEqual(set(player["library"]), {land, creature})

        for chosen_id, expected_zone in ((land, "battlefield"),
                                         (creature, "hand")):
            choice_state, choice_handler = self._state(2110 + chosen_id)
            choice_player = choice_state.p1
            choice_source = inject_real_card(
                choice_state, choice_player,
                "Archdruid's Charm", "graveyard")
            source_card = search_state._safe_get_card(chosen_id)
            selected = inject_into_zone(choice_state, choice_player, {
                "name": source_card.name,
                "mana_cost": getattr(source_card, "mana_cost", ""),
                "type_line": source_card.type_line,
                "card_types": list(source_card.card_types),
                "oracle_text": getattr(source_card, "oracle_text", ""),
                "power": getattr(source_card, "power", 0),
                "toughness": getattr(source_card, "toughness", 0),
            }, "library")
            self.assertTrue(ArchdruidSearchEffect().apply(
                choice_state, choice_source, choice_player, {}))
            self.assertTrue(choice_handler._handle_choose_mode(0, {})[1])
            self.assertIn(selected, choice_player[expected_zone])
            if expected_zone == "battlefield":
                self.assertIn(selected, choice_player["tapped_permanents"])

    def test_day_of_judgment_wipes_creatures_and_legal_noops_succeed(self):
        game_state, _ = self._state(2107)
        caster, opponent = game_state.p1, game_state.p2
        day = inject_real_card(
            game_state, caster, "Day of Judgment", "hand")
        friendly = inject_into_zone(game_state, caster, {
            "name": "Day Friendly", "mana_cost": "{2}",
            "type_line": "Creature", "oracle_text": "",
            "power": 2, "toughness": 2,
        }, "battlefield")
        enemy = inject_into_zone(game_state, opponent, {
            "name": "Day Enemy", "mana_cost": "{3}",
            "type_line": "Creature", "oracle_text": "",
            "power": 3, "toughness": 3,
        }, "battlefield")
        survivor = inject_into_zone(game_state, opponent, {
            "name": "Day Indestructible", "mana_cost": "{4}",
            "type_line": "Creature", "oracle_text": "Indestructible",
            "power": 4, "toughness": 4,
        }, "battlefield")
        caster["mana_pool"].update({"W": 2, "C": 2})
        self.assertTrue(game_state.cast_spell(
            day, caster, {"source_zone": "hand"}))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(friendly, caster["graveyard"])
        self.assertIn(enemy, opponent["graveyard"])
        self.assertIn(survivor, opponent["battlefield"])

        empty_state, _ = self._state(2108)
        self.assertTrue(DestroyEffect("all creatures").apply(
            empty_state, None, empty_state.p1, {}))
        indestructible = inject_into_zone(empty_state, empty_state.p2, {
            "name": "Only Indestructible", "mana_cost": "{3}",
            "type_line": "Creature", "oracle_text": "Indestructible",
            "power": 3, "toughness": 3,
        }, "battlefield")
        self.assertTrue(DestroyEffect("all creatures").apply(
            empty_state, None, empty_state.p1, {}))
        self.assertIn(indestructible, empty_state.p2["battlefield"])

        self.assertTrue(DestroyEffect("creature").apply(
            empty_state, None, empty_state.p1,
            {"creatures": [indestructible]}))
        self.assertIn(indestructible, empty_state.p2["battlefield"])

        regenerating = inject_into_zone(empty_state, empty_state.p2, {
            "name": "Regenerating Target", "mana_cost": "{2}{G}",
            "type_line": "Creature", "oracle_text": "",
            "power": 3, "toughness": 3,
        }, "battlefield")
        empty_state.p2.setdefault("regeneration_shields", set()).add(
            regenerating)
        self.assertTrue(DestroyEffect("creature").apply(
            empty_state, None, empty_state.p1,
            {"creatures": [regenerating]}))
        self.assertIn(regenerating, empty_state.p2["battlefield"])
        self.assertNotIn(
            regenerating, empty_state.p2["regeneration_shields"])

    def test_lightning_helix_damage_life_gain_prevention_and_evaluation(self):
        def cast_helix(seed, prevent=False):
            game_state, handler = self._state(seed)
            caster, opponent = game_state.p1, game_state.p2
            helix = inject_real_card(
                game_state, caster, "Lightning Helix", "hand")
            caster["mana_pool"].update({"W": 1, "R": 1})
            if prevent:
                self.assertTrue(PreventDamageEffect(amount=3).apply(
                    game_state, helix, caster, {}))
            self.assertTrue(game_state.cast_spell(
                helix, caster, {"source_zone": "hand"}))
            self._select_target(game_state, handler, caster, "p2")
            self.assertTrue(game_state.resolve_top_of_stack())
            return game_state, helix

        ordinary, helix = cast_helix(2109)
        self.assertEqual(ordinary.p1["life"], 23)
        self.assertEqual(ordinary.p2["life"], 17)
        prevented, _ = cast_helix(2110, prevent=True)
        self.assertEqual(prevented.p2["life"], 20)
        self.assertEqual(
            prevented.p1["life"], 23,
            "prevented damage incorrectly suppressed Helix's life gain")

        evaluator = EnhancedCardEvaluator(ordinary)
        helix_card = ordinary._safe_get_card(helix)
        blank_instant = ordinary._safe_get_card(inject_card(ordinary, {
            "name": "Blank Two-Mana Instant", "mana_cost": "{R}{W}",
            "cmc": 2, "type_line": "Instant", "card_types": ["instant"],
            "oracle_text": "",
        }))
        self.assertGreater(
            evaluator._calculate_base_value(helix_card),
            evaluator._calculate_base_value(blank_instant))

        cori = inject_real_card(
            ordinary, ordinary.p1, "Cori Mountain Monastery", "hand")
        blank_land = ordinary._safe_get_card(inject_card(ordinary, {
            "name": "Blank Land", "mana_cost": "", "cmc": 0,
            "type_line": "Land", "card_types": ["land"],
            "oracle_text": "",
        }))
        self.assertGreater(
            evaluator._calculate_base_value(ordinary._safe_get_card(cori)),
            evaluator._calculate_base_value(blank_land))


if __name__ == "__main__":
    unittest.main()
