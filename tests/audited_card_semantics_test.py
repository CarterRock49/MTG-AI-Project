"""End-to-end semantics for the remaining cards from the run-log audit."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh, get_env, inject_card, inject_into_zone, inject_real_card,
    replace_hand,
)


def creature(name, power=2, toughness=4):
    return {
        "name": name, "mana_cost": "{2}", "cmc": 2,
        "type_line": "Creature - Test", "oracle_text": "",
        "power": power, "toughness": toughness,
        "colors": [0, 0, 0, 0, 0],
    }


class AuditedCardSemanticsTest(unittest.TestCase):
    def _state(self, seed, card_name, mana_pool):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        controller, opponent = game_state.p1, game_state.p2
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.ability_handler.active_triggers.clear()
        replace_hand(game_state, controller, [])
        for player in (controller, opponent):
            for permanent_id in list(player.get("battlefield", [])):
                self.assertTrue(game_state.move_card(
                    permanent_id, player, "battlefield", player, "library"))
            player["tapped_permanents"] = set()
        card_id = inject_real_card(
            game_state, controller, card_name, "hand")
        controller["mana_pool"] = {
            color: int(mana_pool.get(color, 0))
            for color in ("W", "U", "B", "R", "G", "C")
        }
        return (game_state, get_env().action_handler, controller, opponent,
                card_id)

    @staticmethod
    def _select(handler, game_state, controller, target_id):
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        if target_id not in candidates:
            raise AssertionError(
                f"{target_id} absent from target candidates {candidates}")
        target_index = candidates.index(target_id)
        game_state.targeting_context["target_page"] = target_index // 10
        return handler._handle_select_target(target_index % 10, {})[1]

    def test_slagstorm_real_modes_damage_creatures_or_players(self):
        for seed, mode in ((196320, 0), (196321, 1)):
            with self.subTest(mode=mode):
                (game_state, handler, controller, opponent,
                 slagstorm) = self._state(
                    seed, "Slagstorm", {"R": 3})
                own = inject_into_zone(
                    game_state, controller,
                    creature("Slagstorm Ally"), "battlefield")
                enemy = inject_into_zone(
                    game_state, opponent,
                    creature("Slagstorm Enemy"), "battlefield")
                life_before = (controller["life"], opponent["life"])

                self.assertTrue(game_state.cast_spell(
                    slagstorm, controller))
                self.assertTrue(handler._handle_choose_mode(mode, {})[1])
                self.assertTrue(game_state.resolve_top_of_stack())

                if mode == 0:
                    self.assertEqual(
                        controller.get("damage_counters", {}).get(own), 3)
                    self.assertEqual(
                        opponent.get("damage_counters", {}).get(enemy), 3)
                    self.assertEqual(
                        (controller["life"], opponent["life"]), life_before)
                else:
                    self.assertEqual(controller["life"], life_before[0] - 3)
                    self.assertEqual(opponent["life"], life_before[1] - 3)
                    self.assertNotIn(
                        own, controller.get("damage_counters", {}))
                    self.assertNotIn(
                        enemy, opponent.get("damage_counters", {}))

    def test_prismari_damage_mode_accepts_and_hits_two_any_targets(self):
        (game_state, handler, controller, opponent,
         prismari) = self._state(
            196322, "Prismari Charm", {"U": 1, "R": 1})
        enemy = inject_into_zone(
            game_state, opponent,
            creature("Prismari Damage Target", toughness=3), "battlefield")
        life_before = opponent["life"]

        self.assertTrue(game_state.cast_spell(prismari, controller))
        self.assertTrue(handler._handle_choose_mode(1, {})[1])
        self.assertEqual(game_state.targeting_context.get("min_targets"), 1)
        self.assertEqual(game_state.targeting_context.get("max_targets"), 2)
        self.assertTrue(self._select(
            handler, game_state, controller, "p2"))
        self.assertTrue(self._select(
            handler, game_state, controller, enemy))
        self.assertTrue(game_state.resolve_top_of_stack())

        self.assertEqual(opponent["life"], life_before - 1)
        self.assertEqual(
            opponent.get("damage_counters", {}).get(enemy), 1)

    def test_prismari_bounce_mode_rejects_lands_and_returns_a_permanent(self):
        (game_state, handler, controller, opponent,
         prismari) = self._state(
            196323, "Prismari Charm", {"U": 1, "R": 1})
        artifact = inject_into_zone(game_state, opponent, {
            "name": "Prismari Relic", "mana_cost": "{2}", "cmc": 2,
            "type_line": "Artifact", "oracle_text": "",
        }, "battlefield")
        land = inject_into_zone(game_state, opponent, {
            "name": "Prismari Land", "mana_cost": "", "cmc": 0,
            "type_line": "Basic Land - Island", "oracle_text": "",
        }, "battlefield")

        self.assertTrue(game_state.cast_spell(prismari, controller))
        self.assertTrue(handler._handle_choose_mode(2, {})[1])
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertIn(artifact, candidates)
        self.assertNotIn(land, candidates)
        self.assertTrue(self._select(
            handler, game_state, controller, artifact))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(artifact, opponent["hand"])
        self.assertNotIn(artifact, opponent["battlefield"])

    def test_prismari_surveil_then_draw_resumes_after_both_choices(self):
        (game_state, handler, controller, _,
         prismari) = self._state(
            196324, "Prismari Charm", {"U": 1, "R": 1})
        mill = inject_card(game_state, {
            "name": "Prismari Mill", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Instant", "oracle_text": "",
        })
        draw = inject_card(game_state, {
            "name": "Prismari Draw", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Instant", "oracle_text": "",
        })
        controller["library"][:0] = [mill, draw]
        game_state._last_card_locations[mill] = (controller, "library")
        game_state._last_card_locations[draw] = (controller, "library")

        self.assertTrue(game_state.cast_spell(prismari, controller))
        self.assertTrue(handler._handle_choose_mode(0, {})[1])
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            game_state.choice_context.get("type"), "surveil")
        self.assertEqual(game_state.choice_context.get("cards"), [mill, draw])

        self.assertTrue(handler._handle_scry_surveil_choice(
            None, {}, action_index=305)[1])
        self.assertTrue(handler._handle_scry_surveil_choice(
            None, {}, action_index=306)[1])
        self.assertIn(mill, controller["graveyard"])
        self.assertIn(draw, controller["hand"])
        self.assertIn(prismari, controller["graveyard"])
        self.assertIsNone(game_state.choice_context)

    def test_prismari_empty_library_surveils_zero_then_attempts_draw(self):
        (game_state, handler, controller, _,
         prismari) = self._state(
            196329, "Prismari Charm", {"U": 1, "R": 1})
        controller["library"] = []

        self.assertTrue(game_state.cast_spell(prismari, controller))
        self.assertTrue(handler._handle_choose_mode(0, {})[1])
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertTrue(controller.get("attempted_draw_from_empty"))
        self.assertTrue(controller.get("lost_game"))
        self.assertEqual(game_state.terminal_reason, "decking")
        self.assertIsNone(game_state.choice_context)
        self.assertIn(prismari, controller["graveyard"])

    def test_strategic_betrayal_opponent_chooses_then_exiles_graveyard(self):
        (game_state, handler, controller, opponent,
         betrayal) = self._state(
            196325, "Strategic Betrayal", {"B": 2})
        victim = inject_into_zone(
            game_state, opponent,
            creature("Strategic Victim"), "battlefield")
        grave_card = inject_into_zone(game_state, opponent, {
            "name": "Strategic Grave Card", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Instant", "oracle_text": "",
        }, "graveyard")

        self.assertTrue(game_state.cast_spell(betrayal, controller))
        self.assertTrue(self._select(
            handler, game_state, controller, "p2"))
        self.assertTrue(game_state.resolve_top_of_stack())
        choice = game_state.choice_context
        self.assertEqual(choice.get("choice_kind"), "strategic_betrayal")
        self.assertIs(choice.get("player"), opponent)
        self.assertIn(victim, choice.get("options", []))

        game_state.agent_is_p1 = False
        self.assertTrue(handler._handle_choose_mode(
            choice["options"].index(victim), {})[1])
        self.assertIn(victim, opponent["exile"])
        self.assertIn(grave_card, opponent["exile"])
        self.assertIn(betrayal, controller["graveyard"])

    def test_north_wind_avatar_wish_triggers_only_when_cast(self):
        (game_state, handler, controller, _,
         avatar) = self._state(
            196326, "North Wind Avatar", {"U": 2, "R": 1, "C": 2})
        wish = inject_card(game_state, {
            "name": "North Wind Wish", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Instant", "oracle_text": "Draw a card.",
        })
        controller["outside_game"] = [wish]

        self.assertTrue(game_state.cast_spell(avatar, controller))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(avatar, controller["battlefield"])
        self.assertTrue(any(
            ability.card_id == avatar
            and "if you cast it" in ability.effect_text.lower()
            for ability, _, _ in game_state.ability_handler.active_triggers))
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(
            game_state.choice_context.get("choice_kind"), "outside_game")
        self.assertTrue(game_state.choice_context.get("optional"))
        self.assertTrue(handler._handle_choose_mode(0, {})[1])
        self.assertIn(wish, controller["hand"])
        self.assertNotIn(wish, controller["outside_game"])

        for seed, source_zone, cause in (
                (196327, "library", "cheat"),
                (196328, "graveyard", "reanimate")):
            with self.subTest(noncast_entry=source_zone):
                other_state = fresh(seed)
                other_player = other_state.p1
                other_state.ability_handler.active_triggers.clear()
                not_cast = inject_real_card(
                    other_state, other_player,
                    "North Wind Avatar", source_zone)
                other_player["outside_game"] = [inject_card(other_state, {
                    "name": "Unavailable Noncast Wish",
                    "mana_cost": "{1}", "cmc": 1,
                    "type_line": "Instant", "oracle_text": "",
                })]
                self.assertTrue(other_state.move_card(
                    not_cast, other_player, source_zone,
                    other_player, "battlefield", cause=cause))
                self.assertFalse(any(
                    ability.card_id == not_cast
                    and "if you cast it" in ability.effect_text.lower()
                    for ability, _, _ in
                    other_state.ability_handler.active_triggers))
                self.assertIsNone(other_state.choice_context)

                other_state.ability_handler.active_triggers.clear()
                self.assertTrue(other_state.move_card(
                    not_cast, other_player, "battlefield",
                    other_player, "exile", cause="blink_out"))
                self.assertTrue(other_state.move_card(
                    not_cast, other_player, "exile",
                    other_player, "battlefield", cause="blink_return"))
                self.assertFalse(any(
                    ability.card_id == not_cast
                    and "if you cast it" in ability.effect_text.lower()
                    for ability, _, _ in
                    other_state.ability_handler.active_triggers))
                self.assertIsNone(other_state.choice_context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
