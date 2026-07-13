"""Focused regressions for real-card Landfall registration and resolution."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from scenario_test import fresh, inject_into_zone, inject_real_card  # noqa: E402


def synthetic_land(name="Landfall Test Land"):
    return {
        "name": name, "mana_cost": "", "cmc": 0,
        "type_line": "Basic Land - Forest", "card_types": ["land"],
        "subtypes": ["Forest"], "oracle_text": "{T}: Add {G}.",
        "colors": [0, 0, 0, 0, 0],
    }


def synthetic_creature(name="Landfall Test Creature", power=3, toughness=3):
    return {
        "name": name, "mana_cost": "{2}{G}", "cmc": 3,
        "type_line": "Creature - Beast", "card_types": ["creature"],
        "subtypes": ["Beast"], "oracle_text": "",
        "power": power, "toughness": toughness,
        "colors": [0, 0, 0, 0, 1],
    }


class LandfallRuntimeTest(unittest.TestCase):
    def _landfall_ability(self, game_state, source_id):
        abilities = game_state.ability_handler.registered_abilities.get(
            source_id, [])
        matches = [
            ability for ability in abilities
            if "land you control enters" in getattr(
                ability, "trigger_condition", "").lower()]
        self.assertEqual(
            len(matches), 1,
            [getattr(ability, "effect_text", "") for ability in abilities])
        return matches[0]

    def _entry_context(self, game_state, source_id, land_id, controller):
        return {
            "game_state": game_state,
            "controller": controller,
            "source_card_id": source_id,
            "source_card": game_state._safe_get_card(source_id),
            "event_card_id": land_id,
            "event_card": game_state._safe_get_card(land_id),
            "event_controller": controller,
        }

    def test_landfall_registration_and_controller_type_gates(self):
        game_state = fresh(951)
        player, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, player, "Sazh's Chocobo", "battlefield")
        own_land = inject_into_zone(
            game_state, player, synthetic_land("Own Land"), "battlefield")
        enemy_land = inject_into_zone(
            game_state, opponent, synthetic_land("Enemy Land"), "battlefield")
        nonland = inject_into_zone(
            game_state, player, synthetic_creature("Not a Land"),
            "battlefield")
        ability = self._landfall_ability(game_state, source_id)

        self.assertTrue(ability.can_trigger(
            "ENTERS_BATTLEFIELD",
            self._entry_context(game_state, source_id, own_land, player)))
        enemy_context = self._entry_context(
            game_state, source_id, enemy_land, opponent)
        enemy_context["controller"] = player
        self.assertFalse(ability.can_trigger(
            "ENTERS_BATTLEFIELD", enemy_context))
        self.assertFalse(ability.can_trigger(
            "ENTERS_BATTLEFIELD",
            self._entry_context(game_state, source_id, nonland, player)))

        before = int(game_state._safe_get_card(source_id).counters.get(
            "+1/+1", 0) or 0)
        self.assertTrue(ability.resolve_with_targets(
            game_state, player, {},
            self._entry_context(game_state, source_id, own_land, player)))
        self.assertEqual(
            game_state._safe_get_card(source_id).counters.get("+1/+1"),
            before + 1)

    def test_mightform_doubles_live_power_only_until_end_of_turn(self):
        game_state = fresh(952)
        player = game_state.p1
        source_id = inject_real_card(
            game_state, player, "Mightform Harmonizer", "battlefield")
        target_id = inject_into_zone(
            game_state, player, synthetic_creature(power=3), "battlefield")
        land_id = inject_into_zone(
            game_state, player, synthetic_land(), "battlefield")
        ability = self._landfall_ability(game_state, source_id)

        self.assertTrue(ability.resolve_with_targets(
            game_state, player, {"creatures": [target_id]},
            self._entry_context(game_state, source_id, land_id, player)))
        game_state.layer_system.apply_all_effects()
        self.assertEqual(
            game_state.layer_system.get_characteristic(target_id, "power"), 6)

    def test_earthbender_queues_threshold_reflexive_trigger(self):
        game_state = fresh(953)
        player = game_state.p1
        source_id = inject_real_card(
            game_state, player, "Earthbender Ascension", "battlefield")
        target_id = inject_into_zone(
            game_state, player, synthetic_creature(), "battlefield")
        land_id = inject_into_zone(
            game_state, player, synthetic_land(), "battlefield")
        source = game_state._safe_get_card(source_id)
        source.counters["quest"] = 3
        ability = self._landfall_ability(game_state, source_id)
        game_state.ability_handler.active_triggers.clear()

        self.assertFalse(ability.requires_target)
        self.assertTrue(ability.resolve_with_targets(
            game_state, player, {},
            self._entry_context(game_state, source_id, land_id, player)))
        self.assertEqual(source.counters.get("quest"), 4)
        reflexive = game_state.ability_handler.active_triggers
        self.assertEqual(len(reflexive), 1)
        reflexive_ability, reflexive_controller, reflexive_context = reflexive[0]
        self.assertIs(reflexive_controller, player)
        self.assertTrue(reflexive_ability.requires_target)
        self.assertTrue(reflexive_ability.resolve_with_targets(
            game_state, player, {"creatures": [target_id]},
            reflexive_context))
        self.assertEqual(
            game_state._safe_get_card(target_id).counters.get("+1/+1"), 1)
        self.assertTrue(game_state.check_keyword(target_id, "trample"))


if __name__ == "__main__":
    unittest.main()
