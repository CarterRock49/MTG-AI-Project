"""Regressions for leaving the transient CHOOSE phase safely.

Run from the repository root with::

    python tests/choice_context_test.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

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
from Playersim.ability_types import DiscardEffect, TriggeredAbility  # noqa: E402


class _FailedContinuationEffect:
    effect_text = "Test continuation instruction that cannot be applied."

    def apply(self, game_state, source_id, controller, targets, context=None):
        return False


class _SuccessfulTargetEffect:
    effect_text = "Test targeted instruction that applies successfully."

    def _apply_effect(self, game_state, source_id, controller, targets):
        return True


class ChoiceContextPhaseTest(unittest.TestCase):
    @staticmethod
    def _json_fidelity(game_state):
        fidelity = {
            key: sorted(value) if isinstance(value, set) else value
            for key, value in game_state.fidelity_counters.items()
        }
        # Match the environment's per-game set conversion and require strict
        # JSON values (no custom encoder and no NaN/Infinity extensions).
        return json.loads(json.dumps(
            fidelity, sort_keys=True, allow_nan=False))

    def _stacked_choice_state(self, seed):
        game_state = fresh(seed)
        player = game_state.p2
        source_id = player["hand"][0]
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.stack = [
            ("SPELL", source_id, player, {}),
            ("TRIGGER", source_id, player, {}),
        ]
        game_state.priority_player = player
        return game_state, player, source_id

    def test_terminal_choice_resume_normalizes_orphaned_choose_phase(self):
        game_state, player, _ = self._stacked_choice_state(921)
        context = {
            "type": "resolution_choice",
            "player": player,
            "resume_phase": game_state.PHASE_CHOOSE,
        }
        game_state.choice_context = context
        game_state.phase = game_state.PHASE_CHOOSE

        self.assertTrue(game_state._resume_effect_continuation(context))

        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.phase, game_state.PHASE_PRIORITY)
        self.assertEqual(
            game_state.previous_priority_phase,
            game_state.PHASE_MAIN_PRECOMBAT)
        self.assertIn(game_state.priority_player, (game_state.p1, game_state.p2))

    def test_nested_async_choice_cannot_propagate_choose_resume(self):
        game_state, player, source_id = self._stacked_choice_state(922)
        first_choice = {
            "type": "dig_select",
            "player": player,
            # This is the malformed value produced when a child async choice
            # was opened before its parent restored the priority wrapper.
            "resume_phase": game_state.PHASE_CHOOSE,
            "effect_continuation": {
                "effects": [DiscardEffect(1, target="controller")],
                "source_id": source_id,
                "controller_id": "p2",
                "targets": {},
                "resolution_context": {},
                "finalizer": None,
                "success": True,
            },
        }
        game_state.choice_context = first_choice
        game_state.phase = game_state.PHASE_CHOOSE

        self.assertTrue(game_state._resume_effect_continuation(first_choice))
        self.assertEqual(game_state.phase, game_state.PHASE_CHOOSE)
        self.assertEqual(game_state.choice_context.get("type"), "discard")
        self.assertEqual(
            game_state.choice_context.get("resume_phase"),
            game_state.PHASE_PRIORITY)

        self.assertTrue(game_state.choose_discard_card(0))
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.phase, game_state.PHASE_PRIORITY)
        self.assertEqual(
            game_state.previous_priority_phase,
            game_state.PHASE_MAIN_PRECOMBAT)
        self.assertIn(game_state.priority_player, (game_state.p1, game_state.p2))

    def test_failed_effect_continuation_records_fidelity_once_with_context(self):
        game_state, player, source_id = self._stacked_choice_state(928)
        completed_choice = {
            "type": "resolution_choice",
            "player": player,
            "resume_phase": game_state.PHASE_PRIORITY,
            "effect_continuation": {
                "effects": [
                    _FailedContinuationEffect(),
                    DiscardEffect(1, target="controller"),
                ],
                "source_id": source_id,
                "controller_id": "p2",
                "targets": {},
                "resolution_context": {},
                "finalizer": {
                    "kind": "ability",
                    "ability_type": "TRIGGER",
                    "source_id": source_id,
                    "controller_id": "p2",
                    "context": {"effect_text": "Test trigger finalizer."},
                },
                "success": True,
            },
        }
        game_state.choice_context = completed_choice
        game_state.phase = game_state.PHASE_CHOOSE

        self.assertTrue(game_state._resume_effect_continuation(
            completed_choice))
        self.assertEqual(
            game_state.fidelity_counters["effect_continuation_failures"], 1)
        contexts = game_state.fidelity_counters[
            "effect_continuation_failure_contexts"]
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["source_id"], str(source_id))
        self.assertTrue(contexts[0]["card_name"])
        self.assertEqual(
            contexts[0]["failed_effects"][0]["effect_text"],
            _FailedContinuationEffect.effect_text)
        self.assertEqual(contexts[0]["finalizer"]["kind"], "ability")
        self.assertEqual(game_state.choice_context.get("type"), "discard")

        # Completing the second asynchronous segment must not classify the
        # same resolving-object failure a second time.
        self.assertTrue(game_state.choose_discard_card(0))
        self.assertEqual(
            game_state.fidelity_counters["effect_continuation_failures"], 1)
        self.assertEqual(len(contexts), 1)
        serialized = self._json_fidelity(game_state)
        self.assertEqual(serialized["effect_continuation_failures"], 1)

    def test_targeted_direct_continuation_records_and_propagates_once(self):
        game_state, player, source_id = self._stacked_choice_state(932)
        game_state.agent_is_p1 = False
        continuation = {
            "effects": [
                _FailedContinuationEffect(),
                DiscardEffect(1, target="controller"),
            ],
            "source_id": source_id,
            "controller_id": "p2",
            "targets": {},
            "resolution_context": {},
            "finalizer": None,
            "success": True,
        }
        game_state.targeting_context = {
            "controller": player,
            "source_id": source_id,
            "selected_targets": ["p1"],
            "required_count": 1,
            "min_targets": 1,
            "max_targets": 1,
            "resume_effect": _SuccessfulTargetEffect(),
            "effect_continuation": continuation,
        }
        game_state.phase = game_state.PHASE_TARGETING
        handler = get_env().action_handler
        handler.game_state = game_state

        reward, completed = handler._finalize_targeting_choice()

        self.assertTrue(completed)
        self.assertEqual(reward, 0.05)
        self.assertEqual(
            game_state.fidelity_counters["effect_continuation_failures"], 1)
        self.assertTrue(continuation["fidelity_failure_recorded"])
        self.assertEqual(game_state.choice_context.get("type"), "discard")
        pending = game_state.choice_context["effect_continuation"]
        self.assertTrue(pending["fidelity_failure_recorded"])
        contexts = game_state.fidelity_counters[
            "effect_continuation_failure_contexts"]
        self.assertEqual(
            contexts[0]["failed_effects"][0]["effect_type"],
            "_FailedContinuationEffect")

        self.assertTrue(game_state.choose_discard_card(0))
        self.assertEqual(
            game_state.fidelity_counters["effect_continuation_failures"], 1)
        self.assertEqual(len(contexts), 1)

    def test_optional_direct_continuations_record_failures_once(self):
        cases = (
            (933, "optional_mana_then"),
            (934, "optional_discard_then"),
        )
        for seed, choice_kind in cases:
            with self.subTest(choice_kind=choice_kind):
                game_state = fresh(seed)
                game_state.agent_is_p1 = True
                player = game_state.p1
                source_id = player["hand"][0]
                option = (
                    player["hand"][-1]
                    if choice_kind == "optional_discard_then" else "pay")
                continuation = {
                    "effects": [],
                    "source_id": source_id,
                    "controller_id": "p1",
                    "targets": {},
                    "resolution_context": {},
                    "finalizer": None,
                    "success": True,
                }
                game_state.choice_context = {
                    "type": "resolution_choice",
                    "player": player,
                    "options": [option],
                    "choice_kind": choice_kind,
                    "source_id": source_id,
                    "mana_cost": "{0}",
                    "followup_text": "Synthetic failed follow-up.",
                    "resume_phase": game_state.PHASE_PRIORITY,
                    "effect_continuation": continuation,
                }
                game_state.phase = game_state.PHASE_CHOOSE
                handler = get_env().action_handler
                handler.game_state = game_state

                patches = [mock.patch(
                    "Playersim.ability_utils.EffectFactory.create_effects",
                    return_value=[_FailedContinuationEffect()])]
                if choice_kind == "optional_mana_then":
                    patches.append(mock.patch.object(
                        type(game_state.mana_system), "pay_mana_cost",
                        return_value=True))
                with patches[0]:
                    if len(patches) == 2:
                        with patches[1]:
                            reward, completed = handler._handle_choose_mode(
                                0, {})
                    else:
                        reward, completed = handler._handle_choose_mode(0, {})

                self.assertFalse(completed)
                self.assertEqual(reward, -0.1)
                self.assertEqual(
                    game_state.fidelity_counters[
                        "effect_continuation_failures"], 1)
                self.assertTrue(continuation["fidelity_failure_recorded"])
                contexts = game_state.fidelity_counters[
                    "effect_continuation_failure_contexts"]
                self.assertEqual(len(contexts), 1)
                self.assertEqual(contexts[0]["source_id"], str(source_id))
                self.assertEqual(
                    contexts[0]["failed_effects"][0]["effect_type"],
                    "_FailedContinuationEffect")

                # Reclassification of the same resolving object is a no-op.
                game_state._record_effect_continuation_result(
                    continuation, False,
                    continuation.get("failure_details"))
                self.assertEqual(
                    game_state.fidelity_counters[
                        "effect_continuation_failures"], 1)

    def test_lost_spell_recovery_records_each_occurrence_exactly_once(self):
        cases = (
            (929, None, "post_resolution_occurrence_repair"),
            (930, RuntimeError("resolution probe"), "resolution_exception"),
        )
        for seed, resolution_error, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                game_state = fresh(seed)
                player = game_state.p1
                spell_id = inject_real_card(game_state, player, "Opt", "hand")
                player["hand"].remove(spell_id)
                game_state.phase = game_state.PHASE_PRIORITY
                game_state.previous_priority_phase = (
                    game_state.PHASE_MAIN_PRECOMBAT)
                game_state.priority_player = player
                game_state.stack = [("SPELL", spell_id, player, {
                    "requires_target": False,
                    "effect_text": "Scry 1, then draw a card.",
                })]

                patch_kwargs = (
                    {"side_effect": resolution_error}
                    if resolution_error is not None else {"return_value": True})
                with mock.patch.object(
                        type(game_state), "_resolve_spell", **patch_kwargs):
                    game_state.resolve_top_of_stack()

                self.assertEqual(player["graveyard"].count(spell_id), 1)
                self.assertEqual(
                    game_state.fidelity_counters["lost_spell_recoveries"], 1)
                contexts = game_state.fidelity_counters[
                    "lost_spell_recovery_contexts"]
                self.assertEqual(len(contexts), 1)
                self.assertEqual(contexts[0]["source_id"], str(spell_id))
                self.assertEqual(contexts[0]["card_name"], "Opt")
                self.assertEqual(contexts[0]["recovered_occurrences"], 1)
                self.assertEqual(
                    contexts[0]["recovery_reason"], expected_reason)
                if resolution_error is not None:
                    self.assertEqual(
                        contexts[0]["error_type"], "RuntimeError")
                serialized = self._json_fidelity(game_state)
                self.assertEqual(serialized["lost_spell_recoveries"], 1)

    def test_fidelity_schema_backfills_legacy_state_and_clones_in_isolation(self):
        game_state = fresh(931)
        game_state.fidelity_counters = {
            "unparsed_effects": 2,
            "unparsed_cards": ["Legacy Card"],
        }
        counters = game_state._ensure_fidelity_counters()

        self.assertEqual(counters["unparsed_effects"], 2)
        self.assertEqual(counters["effect_continuation_failures"], 0)
        self.assertEqual(counters["lost_spell_recoveries"], 0)
        self.assertEqual(counters["unparsed_cards"], {"Legacy Card"})

        cloned = game_state.clone()
        self.assertIsNotNone(cloned)
        self.assertEqual(cloned.fidelity_counters, counters)
        cloned.fidelity_counters[
            "effect_continuation_failure_contexts"].append({"clone": True})
        self.assertEqual(
            game_state.fidelity_counters[
                "effect_continuation_failure_contexts"], [])
        self._json_fidelity(cloned)

    def test_opt_scry_pauses_draw_and_finalizer_over_nonempty_stack(self):
        game_state = fresh(923)
        player = game_state.p2
        game_state.agent_is_p1 = False
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        opt_id = inject_real_card(game_state, player, "Opt", "hand")
        player["mana_pool"] = {
            "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 0,
        }
        dummy_id = game_state.p1["library"][-1]
        dummy_item = ("ABILITY", dummy_id, game_state.p1, {
            "effect_text": "",
        })
        game_state.stack = [dummy_item]

        self.assertTrue(game_state.cast_spell(
            opt_id, player, {"source_zone": "hand"}))
        self.assertEqual(len(game_state.stack), 2)
        hand_before_resolution = len(player["hand"])

        # Exercise the production spell resolver.  The old regression called
        # the instruction splitter directly and therefore missed that the
        # real no-target path treated Opt as one Draw-only clause.
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.phase, game_state.PHASE_CHOOSE)
        self.assertEqual(game_state.choice_context.get("type"), "scry")
        self.assertEqual(len(player["hand"]), hand_before_resolution)
        self.assertNotIn(opt_id, player["graveyard"])
        self.assertEqual(game_state.stack, [dummy_item])
        continuation = game_state.choice_context.get("effect_continuation")
        self.assertTrue(continuation)
        self.assertTrue(continuation.get("effects"))
        self.assertEqual(
            continuation.get("finalizer", {}).get("kind"),
            "instant_sorcery")

        handler = get_env().action_handler
        handler.game_state = game_state
        _, completed = handler._handle_scry_surveil_choice(
            None, game_state.choice_context, action_index=306)

        self.assertTrue(completed)
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.phase, game_state.PHASE_PRIORITY)
        self.assertEqual(len(player["hand"]), hand_before_resolution + 1)
        self.assertIn(opt_id, player["graveyard"])
        self.assertEqual(game_state.stack, [dummy_item])
        self.assertEqual(
            game_state.previous_priority_phase,
            game_state.PHASE_MAIN_PRECOMBAT)
        self.assertIn(game_state.priority_player, (game_state.p1, game_state.p2))

    @staticmethod
    def _cosmogrand_trigger_batch(seed, *, modal_position, trigger_count):
        game_state = fresh(seed)
        player = game_state.p1
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = game_state.PHASE_MAIN_PRECOMBAT
        dummy_id = player["hand"][0]
        game_state.stack = [("SPELL", dummy_id, player, {})]

        cosmogrand_id = inject_real_card(
            game_state, player, "Cosmogrand Zenith", "battlefield")
        modal_trigger = TriggeredAbility(
            cosmogrand_id,
            trigger_condition=(
                "whenever you cast your second spell each turn"),
            effect=(
                "choose one \u2014\n"
                "\u2022 Create two 1/1 white Human Soldier creature tokens.\n"
                "\u2022 Put a +1/+1 counter on each creature you control."),
        )
        entries = []
        ordinary_ids = []
        for index in range(trigger_count - 1):
            source_id = inject_into_zone(game_state, player, {
                "name": f"Ordinary trigger source {index}",
                "mana_cost": "{1}",
                "cmc": 1,
                "type_line": "Creature",
                "oracle_text": "",
            }, "battlefield")
            ordinary_ids.append(source_id)
            entries.append((TriggeredAbility(
                source_id,
                trigger_condition="whenever test event happens",
                effect="you gain 1 life"), player, {}))
        entries.insert(modal_position, (modal_trigger, player, {}))
        return (game_state, player, cosmogrand_id, ordinary_ids, entries)

    def test_cosmogrand_mode_preserves_remaining_trigger_order(self):
        game_state, player, cosmogrand_id, ordinary_ids, entries = \
            self._cosmogrand_trigger_batch(
                924, modal_position=0, trigger_count=3)
        handler = game_state.ability_handler

        handler._stack_trigger_batch_with_choice(entries)
        self.assertEqual(game_state.choice_context.get("type"), "order_triggers")
        self.assertTrue(handler.order_trigger_chosen(0))
        self.assertEqual(game_state.choice_context.get("type"), "trigger_mode")
        self.assertEqual(len(game_state.stack), 2)

        self.assertTrue(handler.choose_trigger_mode(0))
        self.assertEqual(game_state.phase, game_state.PHASE_CHOOSE)
        self.assertEqual(game_state.choice_context.get("type"), "order_triggers")
        self.assertEqual(len(game_state.choice_context.get("pending", [])), 2)

        self.assertTrue(handler.order_trigger_chosen(0))
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.phase, game_state.PHASE_PRIORITY)
        self.assertEqual(
            game_state.previous_priority_phase,
            game_state.PHASE_MAIN_PRECOMBAT)
        stacked_sources = [item[1] for item in game_state.stack[1:]]
        self.assertCountEqual(
            stacked_sources, [cosmogrand_id] + ordinary_ids)
        self.assertEqual(len(stacked_sources), len(set(stacked_sources)))
        selected = next(
            item for item in game_state.stack
            if item[1] == cosmogrand_id)
        self.assertEqual(selected[3].get("selected_trigger_mode"), 0)

    def test_auto_stacked_last_cosmogrand_mode_finishes_parent_order(self):
        game_state, _, cosmogrand_id, ordinary_ids, entries = \
            self._cosmogrand_trigger_batch(
                925, modal_position=1, trigger_count=2)
        handler = game_state.ability_handler

        handler._stack_trigger_batch_with_choice(entries)
        self.assertTrue(handler.order_trigger_chosen(0))
        self.assertEqual(game_state.choice_context.get("type"), "trigger_mode")
        parent = game_state.choice_context.get("parent_order_triggers")
        self.assertIsNotNone(parent)
        self.assertEqual(parent.get("pending"), [])

        self.assertTrue(handler.choose_trigger_mode(1))
        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.phase, game_state.PHASE_PRIORITY)
        self.assertEqual(
            game_state.previous_priority_phase,
            game_state.PHASE_MAIN_PRECOMBAT)
        stacked_sources = [item[1] for item in game_state.stack[1:]]
        self.assertEqual(stacked_sources, ordinary_ids + [cosmogrand_id])

    def test_standalone_trigger_mode_normalizes_orphaned_resume_phase(self):
        game_state, player, cosmogrand_id, _, entries = \
            self._cosmogrand_trigger_batch(
                926, modal_position=0, trigger_count=2)
        handler = game_state.ability_handler
        modal_entry = entries[0]
        game_state.choice_context = None
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.previous_priority_phase = None

        self.assertTrue(handler._push_trigger_to_stack(*modal_entry))
        self.assertEqual(game_state.choice_context.get("type"), "trigger_mode")
        self.assertEqual(
            game_state.choice_context.get("resume_phase"),
            game_state.PHASE_CHOOSE)
        self.assertTrue(handler.choose_trigger_mode(0))

        self.assertIsNone(game_state.choice_context)
        self.assertEqual(game_state.phase, game_state.PHASE_PRIORITY)
        self.assertEqual(
            game_state.previous_priority_phase,
            game_state.PHASE_MAIN_PRECOMBAT)
        self.assertIs(game_state.priority_player, player)
        selected = next(
            item for item in game_state.stack
            if item[1] == cosmogrand_id)
        self.assertEqual(selected[3].get("selected_trigger_mode"), 0)

    def test_orphaned_choose_phase_falls_through_to_priority_routing(self):
        game_state, learned_player, source_id = self._stacked_choice_state(927)
        env = get_env()
        opponent = game_state.p1
        game_state.agent_is_p1 = False
        game_state.phase = game_state.PHASE_CHOOSE
        game_state.choice_context = None
        game_state.targeting_context = None
        game_state.sacrifice_context = None
        game_state.priority_player = opponent

        acting_player, context = env._opponent_needs_to_act()
        self.assertIs(acting_player, opponent)
        self.assertEqual(context, {"phase_context": "priority"})

        game_state.priority_player = learned_player
        mask = env.action_mask().astype(bool)
        self.assertTrue(mask[11])
        self.assertFalse(mask[224])

        env.current_episode_actions.extend([224, 224])
        diagnostic = env._policy_state_diagnostic()
        self.assertEqual(diagnostic["recent_actions"][-2:], [224, 224])
        self.assertEqual(len(diagnostic["stack"]), 2)
        self.assertEqual(diagnostic["stack"][0]["source_id"], source_id)


if __name__ == "__main__":
    unittest.main()
