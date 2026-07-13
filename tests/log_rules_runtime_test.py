"""Focused regressions for rule declarations observed in the 195107 canary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from Playersim.ability_types import StaticAbility  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh, get_env, inject_into_zone, inject_real_card,
)


def spell(name, mana_cost="{4}", card_type="sorcery", subtype=None):
    type_line = card_type.title()
    subtypes = []
    if subtype:
        type_line += f" - {subtype}"
        subtypes.append(subtype)
    return {
        "name": name, "mana_cost": mana_cost,
        "cmc": sum(int(part) for part in mana_cost.replace("{", "").split("}")
                   if part.isdigit()),
        "type_line": type_line, "card_types": [card_type],
        "subtypes": subtypes, "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
    }


def creature(name, mana_value, subtype):
    return {
        "name": name, "mana_cost": f"{{{mana_value}}}", "cmc": mana_value,
        "type_line": f"Creature - {subtype}", "card_types": ["creature"],
        "subtypes": [subtype], "oracle_text": "", "power": 2,
        "toughness": 2, "colors": [0, 0, 0, 0, 0],
    }


def land(name):
    return {
        "name": name, "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "card_types": ["land"],
        "subtypes": ["Forest"], "oracle_text": "{T}: Add {G}.",
        "colors": [0, 0, 0, 0, 0],
    }


def park_for_priority(game_state, player, turn=1):
    game_state.turn = turn
    game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
    game_state.previous_priority_phase = None
    game_state.priority_player = player
    game_state.priority_pass_count = 0


class LoggedRulesRuntimeTest(unittest.TestCase):
    def test_gran_gran_reduction_requires_three_lessons_and_noncreature(self):
        game_state = fresh(196001)
        player = game_state.p1
        gran_gran = inject_real_card(
            game_state, player, "Gran-Gran", "battlefield")
        noncreature = inject_into_zone(
            game_state, player, spell("Four Mana Sorcery"), "hand")
        creature_spell = inject_into_zone(
            game_state, player,
            creature("Four Mana Creature", 4, "Wizard"), "hand")

        def priced(card_id):
            card = game_state._safe_get_card(card_id)
            base = game_state.mana_system.parse_mana_cost(card.mana_cost)
            return game_state.mana_system.apply_cost_modifiers(
                player, base, card_id, {})

        self.assertEqual(priced(noncreature)["generic"], 4)
        for index in range(2):
            inject_into_zone(
                game_state, player,
                spell(f"Lesson {index}", "{1}", "sorcery", "Lesson"),
                "graveyard")
        self.assertEqual(priced(noncreature)["generic"], 4)
        inject_into_zone(
            game_state, player,
            spell("Lesson 3", "{1}", "sorcery", "Lesson"),
            "graveyard")
        self.assertEqual(priced(noncreature)["generic"], 3)
        self.assertEqual(priced(creature_spell)["generic"], 4)
        self.assertFalse(any(
            isinstance(ability, StaticAbility)
            and "lesson cards" in ability.effect_text.lower()
            for ability in game_state.ability_handler.registered_abilities.get(
                gran_gran, [])))

    def test_sunderflock_uses_greatest_controlled_elemental_mana_value(self):
        game_state = fresh(196002)
        player = game_state.p1
        inject_into_zone(
            game_state, player, creature("Small Elemental", 3, "Elemental"),
            "battlefield")
        large = inject_into_zone(
            game_state, player, creature("Large Elemental", 5, "Elemental"),
            "battlefield")
        inject_into_zone(
            game_state, player, creature("Large Non-Elemental", 8, "Giant"),
            "battlefield")
        sunderflock = inject_real_card(
            game_state, player, "Sunderflock", "hand")
        card = game_state._safe_get_card(sunderflock)
        base = game_state.mana_system.parse_mana_cost(card.mana_cost)

        priced = game_state.mana_system.apply_cost_modifiers(
            player, base, sunderflock, {})
        self.assertEqual(priced["generic"], max(0, base["generic"] - 5))
        self.assertEqual(priced["U"], base["U"])

        self.assertTrue(game_state.move_card(
            large, player, "battlefield", player, "graveyard"))
        repriced = game_state.mana_system.apply_cost_modifiers(
            player, base, sunderflock, {})
        self.assertEqual(repriced["generic"], max(0, base["generic"] - 3))

    def test_sunderflock_etb_uses_captured_cast_provenance(self):
        game_state = fresh(196003)
        player = game_state.p1
        park_for_priority(game_state, player)
        sunderflock = inject_real_card(
            game_state, player, "Sunderflock", "hand")
        own_elemental = inject_into_zone(
            game_state, player, creature("Friendly Elemental", 2, "Elemental"),
            "battlefield")
        own_non_elemental = inject_into_zone(
            game_state, player, creature("Friendly Soldier", 2, "Soldier"),
            "battlefield")
        enemy_elemental = inject_into_zone(
            game_state, game_state.p2,
            creature("Enemy Elemental", 2, "Elemental"), "battlefield")
        enemy_non_elemental = inject_into_zone(
            game_state, game_state.p2,
            creature("Enemy Soldier", 2, "Soldier"), "battlefield")
        for symbol in player["mana_pool"]:
            player["mana_pool"][symbol] = 20
        game_state.ability_handler.active_triggers.clear()

        self.assertTrue(game_state.cast_spell(sunderflock, player, context={}))
        stack_context = game_state.stack[-1][3]
        self.assertTrue(stack_context.get("was_cast"))
        self.assertEqual(stack_context.get("cast_controller_id"), "p1")
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertTrue(any(
            ability.card_id == sunderflock
            and "if you cast it" in ability.effect_text.lower()
            for ability, _, _ in game_state.ability_handler.active_triggers))
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(own_elemental, player["battlefield"])
        self.assertIn(enemy_elemental, game_state.p2["battlefield"])
        self.assertIn(own_non_elemental, player["hand"])
        self.assertIn(enemy_non_elemental, game_state.p2["hand"])
        self.assertFalse(any(
            isinstance(ability, StaticAbility)
            and "greatest mana value among elementals" in
            ability.effect_text.lower()
            for ability in game_state.ability_handler.registered_abilities.get(
                sunderflock, [])))

        other_state = fresh(196004)
        other_player = other_state.p1
        other_state.ability_handler.active_triggers.clear()
        not_cast = inject_real_card(
            other_state, other_player, "Sunderflock", "battlefield")
        self.assertFalse(any(
            ability.card_id == not_cast
            and "if you cast it" in ability.effect_text.lower()
            for ability, _, _ in other_state.ability_handler.active_triggers))

    def test_icetill_extra_land_and_graveyard_permissions_are_live(self):
        game_state = fresh(196005)
        player = game_state.p1
        game_state.agent_is_p1 = True
        park_for_priority(game_state, player)
        explorer = inject_real_card(
            game_state, player, "Icetill Explorer", "battlefield")
        first = inject_into_zone(
            game_state, player, land("First Land"), "hand")
        second = inject_into_zone(
            game_state, player, land("Graveyard Land"), "graveyard")
        third = inject_into_zone(
            game_state, player, land("Third Land"), "hand")

        self.assertEqual(game_state.land_play_limit(player), 2)
        self.assertTrue(game_state.can_play_lands_from_graveyard(player))
        library_before = len(player["library"])
        graveyard_before = len(player["graveyard"])
        self.assertTrue(game_state.play_land(first, player))
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(len(player["library"]), library_before - 1)
        self.assertEqual(len(player["graveyard"]), graveyard_before + 1)
        self.assertTrue(game_state.can_play_land_this_turn(player))
        playable = get_env()._get_hand_playable(
            player["hand"], player, is_my_turn=True)
        self.assertEqual(playable[player["hand"].index(third)], 1.0)

        graveyard_index = player["graveyard"].index(second)
        mask = game_state.action_handler.generate_valid_actions()
        self.assertTrue(mask[472 + graveyard_index])
        reason = game_state.action_handler.action_reasons_with_context[
            472 + graveyard_index]
        self.assertTrue(
            reason["context"].get("controlled_permanent_land_play"))

        reward, handled = game_state.action_handler._handle_play_from_graveyard(
            graveyard_index, context=reason["context"])
        self.assertTrue(handled, reward)
        self.assertIn(second, player["battlefield"])
        self.assertEqual(game_state.lands_played_this_turn(player), 2)
        self.assertFalse(game_state.play_land(third, player))

        abilities = game_state.ability_handler.registered_abilities.get(
            explorer, [])
        self.assertFalse(any(
            isinstance(ability, StaticAbility)
            and ("additional land" in ability.effect_text.lower()
                 or "lands from your graveyard" in ability.effect_text.lower())
            for ability in abilities))

    def test_icetill_deep_graveyard_land_uses_overflow_catalog(self):
        game_state = fresh(196008)
        player = game_state.p1
        game_state.agent_is_p1 = True
        park_for_priority(game_state, player)
        inject_real_card(
            game_state, player, "Icetill Explorer", "battlefield")
        first = inject_into_zone(
            game_state, player, land("First Land"), "hand")
        for index in range(6):
            inject_into_zone(
                game_state, player, spell(f"Grave Filler {index}"),
                "graveyard")
        deep_land = inject_into_zone(
            game_state, player, land("Deep Graveyard Land"), "graveyard")
        self.assertTrue(game_state.play_land(first, player))
        deep_index = player["graveyard"].index(deep_land)
        self.assertGreaterEqual(deep_index, 6)

        mask = game_state.action_handler.generate_valid_actions()
        self.assertTrue(mask[479])
        catalog = game_state.action_handler.action_reasons_with_context[
            479]["context"]["options"]
        option = next(
            entry for entry in catalog
            if entry.get("handler") == "play_from_graveyard"
            and entry.get("action_context", {}).get("source_idx") == deep_index)
        self.assertTrue(option["action_context"].get(
            "controlled_permanent_land_play"))
        reward, handled = game_state.action_handler._handle_play_from_graveyard(
            deep_index, context=option["action_context"])
        self.assertTrue(handled, reward)
        self.assertIn(deep_land, player["battlefield"])

    def test_icetill_permissions_end_when_source_leaves(self):
        game_state = fresh(196006)
        player = game_state.p1
        park_for_priority(game_state, player)
        explorer = inject_real_card(
            game_state, player, "Icetill Explorer", "battlefield")
        first = inject_into_zone(
            game_state, player, land("First Land"), "hand")
        grave_land = inject_into_zone(
            game_state, player, land("Graveyard Land"), "graveyard")
        self.assertTrue(game_state.play_land(first, player))
        self.assertTrue(game_state.move_card(
            explorer, player, "battlefield", player, "graveyard"))

        self.assertEqual(game_state.land_play_limit(player), 1)
        self.assertFalse(game_state.can_play_land_this_turn(player))
        self.assertFalse(game_state.can_play_lands_from_graveyard(player))
        self.assertFalse(game_state.play_land(
            grave_land, player, source_zone="graveyard",
            permission="controlled_permanent"))

    def test_opponents_cannot_cast_during_lock_source_controllers_turn(self):
        game_state = fresh(196007)
        controller, opponent = game_state.p1, game_state.p2
        lock_card = creature("Jennifer Walters", 4, "Human")
        lock_card["oracle_text"] = (
            "Your opponents can't cast spells during your turn.")
        lock_source = inject_into_zone(
            game_state, controller, lock_card, "battlefield")
        own_spell = inject_into_zone(
            game_state, controller, spell("Own Instant", "{1}", "instant"),
            "hand")
        opposing_spell = inject_into_zone(
            game_state, opponent,
            spell("Opposing Instant", "{1}", "instant"), "hand")

        park_for_priority(game_state, opponent, turn=1)
        self.assertFalse(game_state._can_cast_now(opposing_spell, opponent))
        self.assertFalse(game_state._can_cast_now(
            opposing_spell, opponent, {"cast_during_resolution": True}))
        game_state.agent_is_p1 = False
        opposing_index = opponent["hand"].index(opposing_spell)
        self.assertLess(opposing_index, 10)
        opposing_action = (20 + opposing_index if opposing_index < 8
                           else 396 + (opposing_index - 8))
        self.assertFalse(
            game_state.action_handler.generate_valid_actions()[
                opposing_action])
        game_state.priority_player = controller
        game_state.agent_is_p1 = True
        self.assertTrue(game_state._can_cast_now(own_spell, controller))

        park_for_priority(game_state, opponent, turn=2)
        self.assertTrue(game_state._can_cast_now(opposing_spell, opponent))
        park_for_priority(game_state, opponent, turn=1)
        self.assertTrue(game_state.move_card(
            lock_source, controller, "battlefield", controller, "graveyard"))
        self.assertTrue(game_state._can_cast_now(opposing_spell, opponent))

        self.assertFalse(any(
            isinstance(ability, StaticAbility)
            and "opponents can't cast spells" in ability.effect_text.lower()
            for ability in game_state.ability_handler.registered_abilities.get(
                lock_source, [])))


if __name__ == "__main__":
    unittest.main()
