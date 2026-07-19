"""Exact public-pipeline coverage for Impractical Joke's global rider."""

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
    DamageEffect,
    PreventDamageEffect,
    UnpreventableDamageEffect,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from scenario_test import fresh, get_env, inject_real_card  # noqa: E402


class ImpracticalJokeRegressionTest(unittest.TestCase):
    def _state(self):
        game_state = fresh(32101)
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.pending_spell_context = None
        for player in (controller, opponent):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["damage_counters"] = {}
            player["mana_pool"] = {
                "W": 0, "U": 0, "B": 0,
                "R": 0, "G": 0, "C": 0,
            }
        game_state._last_card_locations = {}
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _public(self, handler, action, message):
        game_state = handler.game_state
        priority = game_state.priority_player or game_state.p1
        game_state.priority_player = priority
        game_state.agent_is_p1 = priority is game_state.p1
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[action], (message, [
            index for index, allowed in enumerate(mask) if allowed]))
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(action)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed"), (message, info))

    def _select_target(self, handler, controller, target_id):
        context = handler.game_state.targeting_context
        self.assertIsNotNone(context)
        candidates = handler._get_target_selection_candidates(
            controller, context)
        self.assertIn(target_id, candidates)
        absolute = candidates.index(target_id)
        for _ in range(absolute // 10):
            self._public(handler, 479, "page targets")
        self._public(handler, 274 + absolute % 10, "select target")

    def test_real_spell_bypasses_prevention_for_the_rest_of_turn_only(self):
        game_state, handler, controller, opponent = self._state()
        joke_id = inject_real_card(
            game_state, controller, "Impractical Joke", "hand")
        inject_real_card(game_state, controller, "Mountain", "battlefield")
        first_target = inject_real_card(
            game_state, opponent, "Shivan Dragon", "battlefield")
        second_target = inject_real_card(
            game_state, opponent, "Llanowar Elves", "battlefield")
        prevention_source = inject_real_card(
            game_state, opponent, "Plains", "battlefield")
        prevention_id = game_state.replacement_effects._register_damage_prevention(
            prevention_source, opponent,
            "Prevent all damage that would be dealt to target creature.")

        with self.assertNoLogs(level=logging.WARNING):
            self._public(handler, 20, "cast Impractical Joke")
            self._select_target(handler, controller, first_target)
            self._public(handler, 11, "controller passes")
            self._public(handler, 11, "opponent passes")

        self.assertIn(joke_id, controller["graveyard"])
        self.assertEqual(opponent["damage_counters"].get(first_target), 3)

        # Isolate protection for the later event, then add a non-prevention
        # replacement. The global rider suppresses protection's prevention
        # component but must leave damage multiplication intact.
        game_state.replacement_effects.remove_effect(prevention_id)
        second_card = game_state._safe_get_card(second_target)
        second_card.keywords[
            second_card.ALL_KEYWORDS.index("protection")] = 1
        second_card._granted_protection_details = ["red"]
        self.assertTrue(game_state.targeting_system._has_protection_from(
            second_card, game_state._safe_get_card(first_target),
            opponent, opponent))

        def double_damage(context):
            context["damage_amount"] *= 2
            return context

        game_state.replacement_effects.register_effect({
            "event_type": "DAMAGE",
            "source_id": first_target,
            "controller_id": opponent,
            "duration": "permanent",
            "condition": lambda context: (
                context.get("target_id") == second_target),
            "replacement": double_damage,
            "description": "regression-only damage doubler",
        })
        self.assertEqual(
            game_state.apply_damage_to_permanent(
                second_target, 1, first_target),
            2,
            "the rider suppressed protection or a non-prevention replacement",
        )

        game_state.phase = game_state.PHASE_CLEANUP
        game_state.replacement_effects.cleanup_expired_effects()
        before = opponent["damage_counters"].get(second_target, 0)
        self.assertEqual(
            game_state.apply_damage_to_permanent(
                second_target, 1, first_target),
            0,
            "protection remained disabled after the turn ended",
        )
        self.assertEqual(
            opponent["damage_counters"].get(second_target, 0), before)

    def test_parser_registers_unpreventability_not_prevention(self):
        effects = EffectFactory.create_effects(
            "Damage can't be prevented this turn. Impractical Joke deals "
            "3 damage to up to one target creature or planeswalker.",
            source_name="Impractical Joke")

        self.assertTrue(any(isinstance(effect, DamageEffect)
                            for effect in effects))
        self.assertFalse(any(isinstance(effect, PreventDamageEffect)
                             for effect in effects))
        self.assertTrue(any(
            type(effect).__name__ == "UnpreventableDamageEffect"
            for effect in effects))

        for unsupported_shape in (
                "Damage can't be prevented.",
                "If that creature would deal damage this turn, that damage "
                "can't be prevented this turn."):
            scoped_effects = EffectFactory.create_effects(unsupported_shape)
            self.assertFalse(any(
                isinstance(effect, UnpreventableDamageEffect)
                for effect in scoped_effects))

        game_state, _, controller, _ = self._state()
        joke_id = inject_real_card(
            game_state, controller, "Impractical Joke", "hand")
        game_state.replacement_effects.register_card_replacement_effects(
            joke_id, controller)
        self.assertFalse(any(
            effect.get("is_damage_prevention", False)
            for effect in game_state.replacement_effects.active_effects))

    def test_prevention_shield_is_consumed_but_prevents_zero(self):
        game_state, _, controller, opponent = self._state()
        source_id = inject_real_card(
            game_state, controller, "Llanowar Elves", "battlefield")
        target_id = inject_real_card(
            game_state, opponent, "Shivan Dragon", "battlefield")

        prevention = PreventDamageEffect(amount=1)
        unpreventable = UnpreventableDamageEffect()
        self.assertTrue(prevention.apply(
            game_state, source_id, controller, targets={}))
        self.assertTrue(unpreventable.apply(
            game_state, source_id, controller, targets={}))
        marker_id = next(
            effect["effect_id"]
            for effect in game_state.replacement_effects.active_effects
            if effect.get("stops_damage_prevention"))

        self.assertEqual(game_state.apply_damage_to_permanent(
            target_id, 1, source_id), 1)
        game_state.replacement_effects.remove_effect(marker_id)
        self.assertEqual(
            game_state.apply_damage_to_permanent(target_id, 1, source_id),
            1,
            "the already-applied prevention shield was not consumed",
        )


if __name__ == "__main__":
    unittest.main()
