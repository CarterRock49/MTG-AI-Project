"""Focused runtime regressions for Leatherhead and Colorstorm Stallion.

Run from the repository root with::

    python tests/leatherhead_colorstorm_test.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


class LeatherheadRuntimeTest(unittest.TestCase):
    def _setup(self, seed):
        game_state = fresh(seed)
        player = game_state.p1
        opponent = game_state.p2
        game_state.agent_is_p1 = True
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        leatherhead = inject_real_card(
            game_state, player, "Leatherhead, Swamp Stalker", "battlefield")
        game_state.ability_handler.active_triggers.clear()
        return game_state, get_env().action_handler, player, opponent, leatherhead

    @staticmethod
    def _deal_player_combat_damage(game_state, leatherhead, player):
        return game_state.trigger_ability(
            leatherhead,
            "DEALS_DAMAGE",
            {
                "controller": player,
                "damage_amount": 5,
                "to_player": True,
                "is_combat_damage": True,
            },
        )

    def _resolve_parent_trigger(self, game_state, leatherhead, player):
        self.assertTrue(self._deal_player_combat_damage(
            game_state, leatherhead, player))
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertIsNone(
            game_state.targeting_context,
            "the reflexive trigger's target was chosen on the parent trigger",
        )
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.phase, game_state.PHASE_CHOOSE)
        self.assertEqual(
            game_state.choice_context.get("choice_kind"), "remove_counter")

    def test_enters_with_hexproof_counter_and_counter_grants_keyword(self):
        game_state, _, _, _, leatherhead = self._setup(1501)
        card = game_state._safe_get_card(leatherhead)

        self.assertEqual(card.counters.get("hexproof"), 1)
        self.assertTrue(game_state.check_keyword(leatherhead, "hexproof"))

        self.assertTrue(game_state.add_counter(leatherhead, "hexproof", -1))
        self.assertFalse(game_state.check_keyword(leatherhead, "hexproof"))

    def test_combat_trigger_requires_leatherhead_and_player_damage(self):
        game_state, _, player, _, leatherhead = self._setup(1502)
        other = inject_into_zone(game_state, player, {
            "name": "Other Combat Source",
            "mana_cost": "{1}{G}",
            "type_line": "Creature - Beast",
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
        }, "battlefield")

        self.assertFalse(game_state.trigger_ability(
            other, "DEALS_DAMAGE", {
                "controller": player,
                "damage_amount": 2,
                "to_player": True,
                "is_combat_damage": True,
            }))
        self.assertEqual(game_state.ability_handler.active_triggers, [])

        self.assertFalse(game_state.trigger_ability(
            leatherhead, "DEALS_DAMAGE", {
                "controller": player,
                "damage_amount": 5,
                "to_player": False,
                "is_combat_damage": True,
            }))
        self.assertEqual(game_state.ability_handler.active_triggers, [])

        self.assertTrue(self._deal_player_combat_damage(
            game_state, leatherhead, player))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0].card_id, leatherhead)

    def test_optional_counter_removal_can_be_declined(self):
        game_state, handler, player, _, leatherhead = self._setup(1503)
        self._resolve_parent_trigger(game_state, leatherhead, player)

        _, success = handler._handle_pass_priority(None)
        self.assertTrue(success)
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(
            game_state._safe_get_card(leatherhead).counters.get("hexproof"), 1)
        self.assertTrue(game_state.check_keyword(leatherhead, "hexproof"))
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertIsNone(game_state.targeting_context)

    def test_accepting_removal_queues_reflexive_union_target(self):
        game_state, handler, player, opponent, leatherhead = self._setup(1504)
        enemy_artifact = inject_into_zone(game_state, opponent, {
            "name": "Enemy Relic",
            "mana_cost": "{2}",
            "type_line": "Artifact",
            "oracle_text": "",
        }, "battlefield")
        enemy_enchantment = inject_into_zone(game_state, opponent, {
            "name": "Enemy Oath",
            "mana_cost": "{1}{W}",
            "type_line": "Enchantment",
            "oracle_text": "",
        }, "battlefield")
        friendly_artifact = inject_into_zone(game_state, player, {
            "name": "Friendly Relic",
            "mana_cost": "{1}",
            "type_line": "Artifact",
            "oracle_text": "",
        }, "battlefield")
        self._resolve_parent_trigger(game_state, leatherhead, player)

        _, success = handler._handle_choose_mode(0, {})
        self.assertTrue(success)
        self.assertEqual(
            game_state._safe_get_card(leatherhead).counters.get("hexproof", 0),
            0,
        )
        self.assertFalse(game_state.check_keyword(leatherhead, "hexproof"))
        reflexive = game_state.ability_handler.active_triggers
        self.assertEqual(len(reflexive), 1)
        self.assertTrue(getattr(reflexive[0][0], "_is_reflexive_trigger", False))

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        self.assertEqual(
            game_state.targeting_context.get("required_type"),
            "artifact_or_enchantment",
        )
        candidates = handler._get_target_selection_candidates(
            player, game_state.targeting_context)
        self.assertIn(enemy_artifact, candidates)
        self.assertIn(enemy_enchantment, candidates)
        self.assertNotIn(friendly_artifact, candidates)

        absolute_index = candidates.index(enemy_artifact)
        game_state.targeting_context["target_page"] = absolute_index // 10
        _, success = handler._handle_select_target(
            absolute_index % 10, {})
        self.assertTrue(success)
        self.assertIsNone(game_state.targeting_context)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(enemy_artifact, opponent["graveyard"])
        self.assertNotIn(enemy_artifact, opponent["battlefield"])
        self.assertIn(enemy_enchantment, opponent["battlefield"])


class ColorstormRuntimeTest(unittest.TestCase):
    def _setup(self, seed):
        game_state = fresh(seed)
        player = game_state.p1
        opponent = game_state.p2
        game_state.agent_is_p1 = True
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        stallion = inject_real_card(
            game_state, player, "Colorstorm Stallion", "battlefield")
        other = inject_into_zone(game_state, player, {
            "name": "Unrelated Friendly Creature",
            "mana_cost": "{1}{U}",
            "type_line": "Creature - Wizard",
            "oracle_text": "",
            "power": 2,
            "toughness": 3,
        }, "battlefield")
        instant = inject_into_zone(game_state, player, {
            "name": "Opus Instant Probe",
            "mana_cost": "{3}{U}",
            "cmc": 4,
            "type_line": "Instant",
            "oracle_text": "",
        }, "hand")
        sorcery = inject_into_zone(game_state, player, {
            "name": "Opus Sorcery Probe",
            "mana_cost": "{3}{R}",
            "cmc": 4,
            "type_line": "Sorcery",
            "oracle_text": "",
        }, "hand")
        creature_spell = inject_into_zone(game_state, player, {
            "name": "Opus Creature Probe",
            "mana_cost": "{3}{G}",
            "cmc": 4,
            "type_line": "Creature - Beast",
            "oracle_text": "",
            "power": 4,
            "toughness": 4,
        }, "hand")
        enemy_instant = inject_into_zone(game_state, opponent, {
            "name": "Enemy Opus Instant Probe",
            "mana_cost": "{3}{U}",
            "cmc": 4,
            "type_line": "Instant",
            "oracle_text": "",
        }, "hand")
        game_state.ability_handler.active_triggers.clear()
        return {
            "game_state": game_state,
            "player": player,
            "opponent": opponent,
            "stallion": stallion,
            "other": other,
            "instant": instant,
            "sorcery": sorcery,
            "creature": creature_spell,
            "enemy_instant": enemy_instant,
        }

    @staticmethod
    def _cast_event(game_state, card_id, caster, spent):
        return game_state.trigger_ability(card_id, "CAST_SPELL", {
            "cast_card_id": card_id,
            "casting_player": caster,
            "final_paid_details": {"spent_specific": dict(spent)},
        })

    def test_opus_requires_own_instant_or_sorcery_cast(self):
        state = self._setup(1511)
        game_state = state["game_state"]

        self.assertFalse(self._cast_event(
            game_state, state["enemy_instant"], state["opponent"], {"U": 4}))
        self.assertEqual(game_state.ability_handler.active_triggers, [])

        self.assertFalse(self._cast_event(
            game_state, state["creature"], state["player"], {"G": 4}))
        self.assertEqual(game_state.ability_handler.active_triggers, [])

        for spell_key in ("instant", "sorcery"):
            with self.subTest(spell_type=spell_key):
                game_state.ability_handler.active_triggers.clear()
                self.assertTrue(self._cast_event(
                    game_state,
                    state[spell_key],
                    state["player"],
                    {"U": 2, "R": 2},
                ))
                queued = game_state.ability_handler.active_triggers
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0][0].card_id, state["stallion"])

    def _resolve_opus(self, seed, spent):
        state = self._setup(seed)
        game_state = state["game_state"]
        player = state["player"]
        self.assertTrue(self._cast_event(
            game_state, state["instant"], player, spent))
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertTrue(game_state.resolve_top_of_stack())
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()
        return state

    def test_four_mana_buffs_only_stallion_without_copy(self):
        state = self._resolve_opus(1512, {"U": 2, "R": 2})
        game_state = state["game_state"]
        player = state["player"]

        stallion = game_state._safe_get_card(state["stallion"])
        other = game_state._safe_get_card(state["other"])
        self.assertEqual((stallion.power, stallion.toughness), (4, 4))
        self.assertEqual((other.power, other.toughness), (2, 3))
        self.assertEqual(player.get("tokens", []), [])

    def test_five_mana_buffs_stallion_and_creates_printed_copy(self):
        state = self._resolve_opus(1513, {"U": 3, "R": 2})
        game_state = state["game_state"]
        player = state["player"]

        stallion = game_state._safe_get_card(state["stallion"])
        other = game_state._safe_get_card(state["other"])
        self.assertEqual((stallion.power, stallion.toughness), (4, 4))
        self.assertEqual((other.power, other.toughness), (2, 3))
        self.assertEqual(len(player.get("tokens", [])), 1)
        token_id = player["tokens"][0]
        self.assertIn(token_id, player["battlefield"])
        token = game_state._safe_get_card(token_id)
        self.assertEqual(token.name, "Colorstorm Stallion")
        self.assertEqual((token.power, token.toughness), (3, 3))


if __name__ == "__main__":
    unittest.main()
