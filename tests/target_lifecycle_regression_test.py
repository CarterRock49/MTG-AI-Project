"""Regressions for target/no-op warnings from the 20:39 training canary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from Playersim.ability_types import (  # noqa: E402
    CounterSpellEffect,
    DamageEffect,
    FightEffect,
    ReturnToHandEffect,
    StaticAbility,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


def creature(name, subtype="Test", power=2, toughness=2):
    return {
        "name": name, "mana_cost": "", "cmc": 0,
        "type_line": f"Creature - {subtype}",
        "card_types": ["creature"], "subtypes": [subtype],
        "oracle_text": "", "power": power, "toughness": toughness,
        "keywords": [], "color_identity": [],
    }


def spell(name, text, mana_cost="{U}"):
    return {
        "name": name, "mana_cost": mana_cost, "cmc": 1,
        "type_line": "Instant", "card_types": ["instant"],
        "oracle_text": text, "keywords": [], "color_identity": ["U"],
    }


class TargetLifecycleRegressionTest(unittest.TestCase):
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
        return game_state, get_env().action_handler

    @staticmethod
    def _clear_hand(game_state, player):
        for card_id in list(player.get("hand", [])):
            assert game_state.move_card(
                card_id, player, "hand", player, "library")

    def test_mandatory_target_spell_is_not_masked_or_cast_without_target(self):
        game_state, handler = self._state(203901)
        player = game_state.p1
        self._clear_hand(game_state, player)
        bounce = inject_real_card(
            game_state, player, "Bounce Off", "hand")
        player["mana_pool"]["U"] = 1

        self.assertFalse(handler.generate_valid_actions()[20])
        self.assertFalse(game_state.cast_spell(bounce, player))
        self.assertIn(bounce, player["hand"])
        self.assertFalse(game_state.stack)
        self.assertIsNone(game_state.targeting_context)

    def test_direct_empty_mandatory_target_sets_fail_with_diagnostics(self):
        game_state, _ = self._state(203902)
        player = game_state.p1
        source = inject_into_zone(
            game_state, player, spell("Lifecycle Source", ""), "graveyard")
        effects = (
            ReturnToHandEffect(target_type="creature"),
            CounterSpellEffect(),
            DamageEffect(3, target_type="creature"),
            FightEffect(),
        )
        empty_targets = (
            {"creatures": []}, {"spells": []}, {}, {"creatures": []})
        with patch("Playersim.ability_types.logging.warning") as warning:
            for effect, targets in zip(effects, empty_targets):
                self.assertFalse(effect.apply(
                    game_state, source, player, targets))
        self.assertEqual(warning.call_count, len(effects))

    def test_validator_proven_post_commit_empty_set_is_silent_noop(self):
        game_state, _ = self._state(203909)
        player, opponent = game_state.p1, game_state.p2
        target = inject_into_zone(
            game_state, opponent, creature("Departing Direct Target"),
            "battlefield")
        source = inject_into_zone(
            game_state, player,
            spell("Lifecycle Direct Bounce",
                  "Return target creature to its owner's hand."),
            "graveyard")
        context = {
            "requires_target": True, "min_targets": 1, "max_targets": 1,
            "targeting_text": "Return target creature to its owner's hand.",
            "targets": {"creatures": [target]},
        }
        self.assertTrue(game_state.move_card(
            target, opponent, "battlefield", opponent, "graveyard"))
        self.assertFalse(game_state._validate_targets_on_resolution(
            source, player, context["targets"], context))
        self.assertEqual(context["targets"], {"creatures": []})
        self.assertEqual(
            context["_target_resolution_lifecycle"], {
                "validated": True,
                "original_target_count": 1,
                "legal_target_count": 0,
                "slots": [],
            })

        effect = ReturnToHandEffect(target_type="creature")
        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertTrue(effect.apply(
                game_state, source, player, context["targets"],
                context=context))
        warning.assert_not_called()

    def test_committed_target_that_leaves_causes_silent_stack_fizzle(self):
        game_state, _ = self._state(203903)
        player, opponent = game_state.p1, game_state.p2
        target = inject_into_zone(
            game_state, opponent, creature("Departing Bounce Target"),
            "battlefield")
        source = inject_into_zone(
            game_state, player,
            spell("Lifecycle Bounce",
                  "Return target creature to its owner's hand."), "hand")
        player["hand"].remove(source)
        game_state.add_to_stack("SPELL", source, player, {
            "source_zone": "hand", "was_cast": True,
            "requires_target": True, "num_targets": 1,
            "min_targets": 1, "max_targets": 1,
            "targeting_text": "Return target creature to its owner's hand.",
            "targets": {"creatures": [target]},
        })
        self.assertTrue(game_state.move_card(
            target, opponent, "battlefield", opponent, "graveyard"))

        with patch("Playersim.ability_types.logging.warning") as effect_warning:
            with patch("Playersim.game_state_stack.logging.warning") as stack_warning:
                self.assertTrue(game_state.resolve_top_of_stack())
        effect_warning.assert_not_called()
        stack_warning.assert_not_called()
        self.assertIn(source, player["graveyard"])
        self.assertIn(target, opponent["graveyard"])

    def test_committed_damage_target_that_leaves_fizzles_silently(self):
        game_state, _ = self._state(203907)
        player, opponent = game_state.p1, game_state.p2
        target = inject_into_zone(
            game_state, opponent, creature("Departing Damage Target"),
            "battlefield")
        source = inject_into_zone(
            game_state, player,
            spell("Lifecycle Bolt", "Deal 3 damage to target creature.",
                  mana_cost="{R}"), "hand")
        player["hand"].remove(source)
        game_state.add_to_stack("SPELL", source, player, {
            "source_zone": "hand", "was_cast": True,
            "requires_target": True, "num_targets": 1,
            "min_targets": 1, "max_targets": 1,
            "targeting_text": "Deal 3 damage to target creature.",
            "targets": {"creatures": [target]},
        })
        self.assertTrue(game_state.move_card(
            target, opponent, "battlefield", opponent, "graveyard"))

        with patch("Playersim.ability_types.logging.warning") as effect_warning:
            with patch("Playersim.game_state_stack.logging.warning") as stack_warning:
                self.assertTrue(game_state.resolve_top_of_stack())
        effect_warning.assert_not_called()
        stack_warning.assert_not_called()
        self.assertIn(source, player["graveyard"])
        self.assertIn(target, opponent["graveyard"])

    def test_committed_counter_target_that_leaves_stack_fizzles_silently(self):
        game_state, _ = self._state(203908)
        player, opponent = game_state.p1, game_state.p2
        target_spell = inject_into_zone(
            game_state, opponent,
            spell("Departing Stack Spell", "Draw a card.", "{1}{U}"),
            "hand")
        opponent["hand"].remove(target_spell)
        game_state.add_to_stack("SPELL", target_spell, opponent, {
            "source_zone": "hand", "was_cast": True,
            "requires_target": False, "num_targets": 0,
        })
        counter = inject_into_zone(
            game_state, player,
            spell("Lifecycle Counter", "Counter target spell."), "hand")
        player["hand"].remove(counter)
        game_state.add_to_stack("SPELL", counter, player, {
            "source_zone": "hand", "was_cast": True,
            "requires_target": True, "num_targets": 1,
            "min_targets": 1, "max_targets": 1,
            "targeting_text": "Counter target spell.",
            "targets": {"spells": [target_spell]},
        })

        target_item = game_state.stack.pop(0)
        self.assertEqual(target_item[1], target_spell)
        game_state.last_stack_size = len(game_state.stack)
        self.assertTrue(game_state.move_card(
            target_spell, opponent, "stack_implicit", opponent,
            "graveyard"))

        with patch("Playersim.ability_types.logging.warning") as effect_warning:
            with patch("Playersim.game_state_stack.logging.warning") as stack_warning:
                self.assertTrue(game_state.resolve_top_of_stack())
        effect_warning.assert_not_called()
        stack_warning.assert_not_called()
        self.assertIn(counter, player["graveyard"])
        self.assertIn(target_spell, opponent["graveyard"])

    def test_nontargeted_cast_trigger_cannot_steal_the_spells_targets(self):
        game_state, handler = self._state(203909)
        player, opponent = game_state.p1, game_state.p2
        self._clear_hand(game_state, player)
        self._clear_hand(game_state, opponent)
        for owner in (player, opponent):
            for permanent_id in list(owner.get("battlefield", [])):
                self.assertTrue(game_state.move_card(
                    permanent_id, owner, "battlefield", owner, "library"))

        namor = inject_real_card(
            game_state, player, "Namor the Sub-Mariner", "battlefield")
        target = inject_into_zone(
            game_state, opponent, creature("Namor Bounce Target"),
            "battlefield")
        bounce = inject_real_card(
            game_state, player, "Bounce Off", "hand")
        player["mana_pool"]["U"] = 1
        game_state.priority_player = player

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[20])
        context = handler.action_reasons_with_context[20]["context"]
        _, started = handler._handle_play_spell(None, context=context)
        self.assertTrue(started)
        candidates = handler._get_target_selection_candidates(
            player, game_state.targeting_context)
        self.assertIn(target, candidates)
        _, selected = handler._handle_select_target(
            candidates.index(target), {})
        self.assertTrue(selected)
        game_state.ability_handler.process_triggered_abilities()

        spell_item = next(
            item for item in game_state.stack
            if item[0] == "SPELL" and item[1] == bounce)
        trigger_item = game_state.stack[-1]
        self.assertEqual((trigger_item[0], trigger_item[1]),
                         ("TRIGGER", namor))
        self.assertEqual(spell_item[3]["targets"], {"creatures": [target]})
        self.assertNotIn("targets", trigger_item[3])
        self.assertEqual(
            trigger_item[3]["event_targets"], {"creatures": [target]})
        self.assertIsNot(
            trigger_item[3]["event_targets"], spell_item[3]["targets"])

        tokens_before = len(player.get("tokens", []))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(spell_item[3]["targets"], {"creatures": [target]})
        self.assertEqual(len(player.get("tokens", [])), tokens_before + 1)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(target, opponent["hand"])

    def test_sunderflock_zero_non_elementals_is_silent_success(self):
        game_state, _ = self._state(203904)
        player, opponent = game_state.p1, game_state.p2
        source = inject_real_card(
            game_state, player, "Sunderflock", "battlefield")
        friendly = inject_into_zone(
            game_state, player, creature("Friendly Elemental", "Elemental"),
            "battlefield")
        enemy = inject_into_zone(
            game_state, opponent, creature("Enemy Elemental", "Elemental"),
            "battlefield")
        effect = EffectFactory.create_effects(
            "Return all non-Elemental creatures to their owners' hands.",
            source_name="Sunderflock")[0]

        with patch("Playersim.ability_types.logging.warning") as warning:
            self.assertTrue(effect.apply(game_state, source, player, {}))
        warning.assert_not_called()
        self.assertIn(source, player["battlefield"])
        self.assertIn(friendly, player["battlefield"])
        self.assertIn(enemy, opponent["battlefield"])

    def test_meltstrider_optional_fight_can_choose_zero_silently(self):
        game_state, handler = self._state(203905)
        player = game_state.p1
        self._clear_hand(game_state, player)
        aura = inject_real_card(
            game_state, player, "Meltstrider's Resolve", "hand")
        enchanted = inject_into_zone(
            game_state, player, creature("Resolve Enchant Target"),
            "battlefield")
        player["mana_pool"]["G"] = 1

        with patch("Playersim.ability_utils.logging.warning") as parser_warning:
            with patch("Playersim.ability_types.logging.warning") as effect_warning:
                mask = handler.generate_valid_actions()
                self.assertTrue(mask[20])
                context = handler.action_reasons_with_context[20]["context"]
                _, started = handler._handle_play_spell(None, context=context)
                self.assertTrue(started)
                candidates = handler._get_target_selection_candidates(
                    player, game_state.targeting_context)
                self.assertEqual(candidates, [enchanted])
                _, selected = handler._handle_select_target(0, {})
                self.assertTrue(selected)
                self.assertTrue(game_state.resolve_top_of_stack())
                self.assertEqual(
                    player.get("attachments", {}).get(aura), enchanted)

                game_state.ability_handler.process_triggered_abilities()
                self.assertIsNotNone(game_state.targeting_context)
                self.assertEqual(game_state.targeting_context["min_targets"], 0)
                self.assertFalse(handler._get_target_selection_candidates(
                    player, game_state.targeting_context))
                self.assertTrue(handler.generate_valid_actions()[11])
                _, passed = handler._handle_pass_priority(None)
                self.assertTrue(passed)
                resolved = game_state.resolve_top_of_stack()
                self.assertTrue(
                    resolved,
                    f"optional fight failed; warnings={effect_warning.call_args_list}")

        parser_warning.assert_not_called()
        effect_warning.assert_not_called()
        self.assertIn(aura, player["battlefield"])
        self.assertEqual(player.get("attachments", {}).get(aura), enchanted)

    def test_mightform_warp_declaration_does_not_register_dead_static(self):
        game_state, _ = self._state(203906)
        player = game_state.p1
        with patch("Playersim.ability_types.logging.warning") as warning:
            mightform = inject_real_card(
                game_state, player, "Mightform Harmonizer", "battlefield")
        warning.assert_not_called()
        abilities = game_state.ability_handler.registered_abilities.get(
            mightform, [])
        self.assertFalse(any(
            isinstance(ability, StaticAbility)
            and getattr(ability, "effect_text", "").lower().startswith("warp")
            for ability in abilities))
        card = game_state._safe_get_card(mightform)
        self.assertTrue(card.is_warp)
        self.assertEqual(card.warp_cost.lower(), "{2}{g}")


if __name__ == "__main__":
    unittest.main()
