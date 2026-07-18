"""Focused regressions for combat declaration and damage rules."""

from __future__ import annotations

import sys
import math
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    grant_keyword,
    inject_into_zone,
)


def creature(name, power=2, toughness=2, oracle_text="", **extra):
    data = {
        "name": name,
        "mana_cost": "{2}",
        "cmc": 2,
        "type_line": "Creature - Test",
        "oracle_text": oracle_text,
        "power": power,
        "toughness": toughness,
        "color_identity": [],
    }
    data.update(extra)
    return data


class CombatRegressionTest(unittest.TestCase):
    def _combat_state(self, seed, turn=1):
        game_state = fresh(seed)
        active = game_state.p1 if turn % 2 else game_state.p2
        defender = game_state.p2 if active is game_state.p1 else game_state.p1
        game_state.turn = turn
        game_state.agent_is_p1 = active is game_state.p1
        game_state.stack.clear()
        game_state.priority_pass_count = 0
        game_state.current_attackers = []
        game_state.current_block_assignments = {}
        game_state.blocked_attackers_this_combat = set()
        game_state.first_strike_damage_participants = set()
        game_state.first_strike_damage_dealt = False
        game_state.combat_damage_dealt = False
        return game_state, get_env().action_handler, active, defender

    def _assert_ninjutsu_activation_on_stack(
            self, game_state, active, attacker, ninja):
        self.assertIn(attacker, active["hand"])
        self.assertIn(ninja, active["hand"])
        self.assertNotIn(ninja, active["battlefield"])
        self.assertEqual(
            active["mana_pool"],
            {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0})
        self.assertEqual(game_state.current_attackers, [])
        self.assertTrue(game_state.stack, "Ninjutsu skipped the stack")
        item_type, source_id, stack_controller, context = \
            game_state.stack[-1]
        self.assertEqual(item_type, "ABILITY")
        self.assertEqual(source_id, ninja)
        self.assertIs(stack_controller, active)
        self.assertTrue(context.get("ninjutsu"))

    def _resolve_ninjutsu_through_public_priority(
            self, game_state, handler, active, defender, ninja):
        for pass_number, acting_player in enumerate((active, defender)):
            self.assertIs(game_state.priority_player, acting_player)
            game_state.agent_is_p1 = acting_player is game_state.p1
            mask = handler.generate_valid_actions()
            self.assertTrue(mask[11])
            handler.current_valid_actions = mask
            _, done, truncated, info = handler.apply_action(11)
            self.assertFalse(done)
            self.assertFalse(truncated)
            self.assertFalse(info.get("execution_failed", False), info)
            if pass_number == 0:
                self.assertIn(ninja, active["hand"])
                self.assertNotIn(ninja, active["battlefield"])
                self.assertTrue(game_state.stack)
        self.assertFalse(any(
            item[0] == "ABILITY" and item[1] == ninja
            and item[3].get("ninjutsu")
            for item in game_state.stack))

    def test_post_blockers_ninjutsu_is_masked_and_executes_for_active_seat(self):
        for index, turn in enumerate((1, 2)):
            with self.subTest(turn=turn):
                game_state, handler, active, defender = self._combat_state(
                    208001 + index, turn=turn)
                attacker = inject_into_zone(
                    game_state, active,
                    creature("Ninjutsu Return", 2, 2), "battlefield")
                ninja = inject_into_zone(
                    game_state, active,
                    creature(
                        "Post-Block Shinobi", 3, 2,
                        "Ninjutsu {1}{U}"), "hand")
                active["entered_battlefield_this_turn"].discard(attacker)
                active["mana_pool"] = {
                    "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 1,
                }
                game_state.phase = game_state.PHASE_DECLARE_BLOCKERS
                game_state.current_attackers = [attacker]
                game_state.priority_player = game_state._get_non_active_player()
                self.assertTrue(
                    handler.combat_handler.handle_declare_blockers_done())
                self.assertEqual(
                    game_state.phase, game_state.PHASE_COMBAT_DAMAGE)
                self.assertIs(game_state.priority_player, active)
                # Mana empties when the declaration step ends. This models
                # activating a mana ability in the pending damage window.
                active["mana_pool"] = {
                    "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 1,
                }

                mask = handler.generate_valid_actions()
                self.assertTrue(mask[437])
                handler.current_valid_actions = mask
                _, done, truncated, info = handler.apply_action(437)
                self.assertFalse(done)
                self.assertFalse(truncated)
                self.assertFalse(info.get("execution_failed", False), info)
                self._assert_ninjutsu_activation_on_stack(
                    game_state, active, attacker, ninja)
                self._resolve_ninjutsu_through_public_priority(
                    game_state, handler, active, defender, ninja)
                self.assertIn(ninja, active["battlefield"])
                self.assertIn(ninja, active["tapped_permanents"])
                self.assertEqual(game_state.current_attackers, [ninja])

    def test_ninjutsu_dispatches_enters_battlefield_once(self):
        game_state, handler, active, defender = self._combat_state(208018)
        attacker = inject_into_zone(
            game_state, active, creature("ETB Return"), "battlefield")
        ninja = inject_into_zone(
            game_state, active,
            creature(
                "ETB Shinobi", 3, 2,
                "When this creature enters the battlefield, draw a card.\n"
                "Ninjutsu {1}{U}"),
            "hand")
        active["entered_battlefield_this_turn"].discard(attacker)
        active["mana_pool"] = {
            "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 1,
        }
        game_state.phase = game_state.PHASE_COMBAT_DAMAGE
        game_state.priority_player = active
        game_state.current_attackers = [attacker]
        game_state.ability_handler.active_triggers.clear()

        self.assertTrue(handler.combat_handler.handle_ninjutsu(context={
            "ninja_identifier": active["hand"].index(ninja),
            "attacker_identifier": active["battlefield"].index(attacker),
        }))
        self._assert_ninjutsu_activation_on_stack(
            game_state, active, attacker, ninja)
        self._resolve_ninjutsu_through_public_priority(
            game_state, handler, active, defender, ninja)
        self.assertIn(ninja, active["battlefield"])
        self.assertEqual(game_state.current_attackers, [ninja])

        etb_triggers = [
            context for item_type, source_id, _, context in game_state.stack
            if (item_type == "TRIGGER" and source_id == ninja
                and context.get("event_type") == "ENTERS_BATTLEFIELD")
        ]
        self.assertEqual(len(etb_triggers), 1, etb_triggers)
        self.assertTrue(etb_triggers[0].get("used_ninjutsu"))

    def test_ninjutsu_cannot_return_an_attacker_that_remains_blocked(self):
        game_state, handler, active, defender = self._combat_state(208003)
        attacker = inject_into_zone(
            game_state, active, creature("Remembered Block", 3, 3),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender, creature("Departing Blocker", 1, 1),
            "battlefield")
        inject_into_zone(
            game_state, active,
            creature("Illegal Shinobi", 3, 2, "Ninjutsu {1}{U}"), "hand")
        active["mana_pool"] = {
            "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 1,
        }
        game_state.phase = game_state.PHASE_DECLARE_BLOCKERS
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {attacker: [blocker]}
        game_state.priority_player = defender
        self.assertTrue(handler.combat_handler.handle_declare_blockers_done())
        self.assertIn(attacker, game_state.blocked_attackers_this_combat)
        self.assertTrue(game_state.move_card(
            blocker, defender, "battlefield", defender, "graveyard"))
        game_state.agent_is_p1 = active is game_state.p1
        self.assertFalse(handler.generate_valid_actions()[437])

    def test_first_strike_ninja_inserts_the_missing_damage_step(self):
        game_state, handler, active, defender = self._combat_state(208004)
        attacker = inject_into_zone(
            game_state, active, creature("Ordinary Infiltrator"),
            "battlefield")
        ninja = inject_into_zone(
            game_state, active,
            creature(
                "Double-Strike Shinobi", 2, 2,
                "Double strike\nNinjutsu {1}{U}"), "hand")
        active["mana_pool"] = {
            "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 1,
        }
        game_state.phase = game_state.PHASE_DECLARE_BLOCKERS
        game_state.current_attackers = [attacker]
        game_state.priority_player = defender
        self.assertTrue(handler.combat_handler.handle_declare_blockers_done())
        active["mana_pool"] = {
            "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 1,
        }
        mask = handler.generate_valid_actions()
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(437)
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("execution_failed", False), info)
        self._assert_ninjutsu_activation_on_stack(
            game_state, active, attacker, ninja)
        self._resolve_ninjutsu_through_public_priority(
            game_state, handler, active, defender, ninja)
        self.assertIn(ninja, active["battlefield"])
        self.assertEqual(game_state.phase, game_state.PHASE_FIRST_STRIKE_DAMAGE)
        self.assertEqual(
            game_state.first_strike_damage_participants, {ninja})

    def test_reverse_protection_does_not_make_a_block_illegal(self):
        game_state, handler, active, defender = self._combat_state(208005)
        red_attacker = inject_into_zone(
            game_state, active,
            creature(
                "Red Attacker", color_identity=["R"]), "battlefield")
        protected_blocker = inject_into_zone(
            game_state, defender,
            creature(
                "Protected Blocker", oracle_text="Protection from red",
                color_identity=["W"]), "battlefield")
        grant_keyword(
            game_state, protected_blocker, "protection from red")
        self.assertTrue(handler.combat_handler._can_block(
            protected_blocker, red_attacker))
        self.assertEqual(game_state.apply_damage_to_permanent(
            protected_blocker, 2, red_attacker,
            is_combat_damage=True), 0)

    def test_artifact_fear_block_and_shadow_symmetry(self):
        game_state, handler, active, defender = self._combat_state(208006)
        fear_attacker = inject_into_zone(
            game_state, active,
            creature("Fear Attacker", oracle_text="Fear"), "battlefield")
        artifact_blocker = inject_into_zone(
            game_state, defender,
            creature(
                "Artifact Blocker", type_line="Artifact Creature - Golem"),
            "battlefield")
        self.assertTrue(handler.combat_handler._can_block(
            artifact_blocker, fear_attacker))

        ground_attacker = inject_into_zone(
            game_state, active, creature("Ground Attacker"), "battlefield")
        shadow_blocker = inject_into_zone(
            game_state, defender,
            creature("Shadow Blocker", oracle_text="Shadow"), "battlefield")
        self.assertFalse(handler.combat_handler._can_block(
            shadow_blocker, ground_attacker))

    def test_banding_does_not_override_flying_block_restrictions(self):
        game_state, handler, active, defender = self._combat_state(208019)
        attacker = inject_into_zone(
            game_state, active, creature("Flying Band", 2, 2),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender, creature("Ground Blocker", 2, 2),
            "battlefield")
        grant_keyword(game_state, attacker, "flying")
        grant_keyword(game_state, attacker, "banding")

        self.assertFalse(handler.combat_handler._can_block(
            blocker, attacker))

    def test_dynamic_cant_block_and_phasing_reach_the_public_mask(self):
        game_state, handler, active, defender = self._combat_state(208007)
        attacker = inject_into_zone(
            game_state, active, creature("Restriction Target"), "battlefield")
        blocker = inject_into_zone(
            game_state, defender, creature("Restricted Blocker"),
            "battlefield")
        grant_keyword(game_state, blocker, "cant_block", source_id=attacker)
        self.assertTrue(game_state.check_keyword(blocker, "cant_block"))
        self.assertFalse(handler.combat_handler._can_block(blocker, attacker))

        active["entered_battlefield_this_turn"].discard(attacker)
        game_state.phased_out.add(attacker)
        game_state.phase = game_state.PHASE_DECLARE_ATTACKERS
        game_state.priority_player = active
        self.assertFalse(handler.combat_handler.is_valid_attacker(attacker))
        attack_action = 28 + active["battlefield"].index(attacker)
        self.assertFalse(handler.generate_valid_actions()[attack_action])

    def test_delirium_unless_restrictions_turn_off_at_four_card_types(self):
        game_state, handler, active, defender = self._combat_state(208017)
        oracle_text = (
            "Delirium - This creature can't attack or block unless there are "
            "four or more card types among cards in your graveyard.")
        attacker = inject_into_zone(
            game_state, active,
            creature("Attacking Beastie", 3, 3, oracle_text), "battlefield")
        blocker = inject_into_zone(
            game_state, defender,
            creature("Blocking Beastie", 3, 3, oracle_text), "battlefield")
        block_target = inject_into_zone(
            game_state, active, creature("Block Target"), "battlefield")
        active["entered_battlefield_this_turn"].difference_update(
            {attacker, block_target})

        self.assertFalse(handler.combat_handler.is_valid_attacker(attacker))
        self.assertFalse(handler.combat_handler._can_block(
            blocker, block_target))

        for player in (active, defender):
            for card_type in ("Instant", "Sorcery", "Artifact", "Enchantment"):
                inject_into_zone(
                    game_state, player,
                    {
                        "name": f"Delirium {card_type}",
                        "mana_cost": "",
                        "type_line": card_type,
                        "oracle_text": "",
                    },
                    "graveyard")

        self.assertTrue(handler.combat_handler.is_valid_attacker(attacker))
        self.assertTrue(handler.combat_handler._can_block(
            blocker, block_target))

    def test_unknown_conditional_attack_restriction_fails_closed(self):
        game_state, handler, active, _ = self._combat_state(208020)
        attacker = inject_into_zone(
            game_state, active,
            creature(
                "Unsupported Unless", 3, 3,
                "This creature can't attack unless you control another "
                "creature."),
            "battlefield")
        active["entered_battlefield_this_turn"].discard(attacker)

        self.assertFalse(handler.combat_handler.is_valid_attacker(attacker))

    def test_control_change_applies_summoning_sickness_without_haste(self):
        game_state, handler, active, defender = self._combat_state(208008)
        stolen = inject_into_zone(
            game_state, defender, creature("Stolen Creature"), "battlefield")
        defender["entered_battlefield_this_turn"].discard(stolen)
        self.assertTrue(game_state.apply_temporary_control(stolen, active))
        self.assertIn(stolen, active["entered_battlefield_this_turn"])
        self.assertFalse(handler.combat_handler.is_valid_attacker(stolen))
        grant_keyword(game_state, stolen, "haste")
        self.assertTrue(handler.combat_handler.is_valid_attacker(stolen))

    def test_indestructible_keeps_marked_damage_for_later_assignment(self):
        game_state, _, active, defender = self._combat_state(208009)
        attacker = inject_into_zone(
            game_state, active,
            creature(
                "Double Trampler", 4, 4,
                "Double strike\nTrample"), "battlefield")
        blocker = inject_into_zone(
            game_state, defender,
            creature(
                "Indestructible Blocker", 2, 2, "Indestructible"),
            "battlefield")
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {attacker: [blocker]}
        game_state.blocked_attackers_this_combat = {attacker}
        life_before = defender["life"]
        game_state.combat_resolver.resolve_combat()
        self.assertIn(blocker, defender["battlefield"])
        self.assertEqual(defender["damage_counters"].get(blocker), 2)
        self.assertEqual(
            defender["life"], life_before - 6,
            "first-step lethal damage was forgotten before regular trample")

    def test_blocked_attacker_assigns_all_nontrample_damage(self):
        game_state, _, active, defender = self._combat_state(208012)
        attacker = inject_into_zone(
            game_state, active,
            creature("Blocked Lifelinker", 6, 6, "Lifelink"),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender,
            creature("Small Damage Recipient", 1, 1), "battlefield")
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {attacker: [blocker]}
        game_state.blocked_attackers_this_combat = {attacker}
        life_before = active["life"]
        game_state.combat_resolver.resolve_combat()
        self.assertEqual(defender["damage_counters"].get(blocker), 6)
        self.assertEqual(
            active["life"], life_before + 6,
            "nontrample excess vanished instead of being dealt to the blocker")

    def test_combat_target_actions_do_not_remain_valid_noops(self):
        game_state, handler, active, defender = self._combat_state(208010)
        attacker = inject_into_zone(
            game_state, active, creature("Targeting Attacker"), "battlefield")
        planeswalker = inject_into_zone(
            game_state, defender, {
                "name": "Combat Target", "mana_cost": "{3}", "cmc": 3,
                "type_line": "Legendary Planeswalker - Test",
                "oracle_text": "", "loyalty": "4",
            }, "battlefield")
        active["entered_battlefield_this_turn"].discard(attacker)
        game_state.phase = game_state.PHASE_DECLARE_ATTACKERS
        game_state.priority_player = active
        game_state.current_attackers = [attacker]

        mask = handler.generate_valid_actions()
        self.assertTrue(mask[378])
        handler.current_valid_actions = mask
        _, _, _, info = handler.apply_action(378)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertEqual(
            game_state.planeswalker_attack_targets[attacker], planeswalker)
        self.assertFalse(handler.generate_valid_actions()[378])
        self.assertFalse(handler.combat_handler.handle_attack_planeswalker(0))

    def test_block_and_becomes_blocked_triggers_are_dispatched_once(self):
        game_state, handler, active, defender = self._combat_state(208011)
        attacker = inject_into_zone(
            game_state, active,
            creature(
                "Blocked Trigger", 3, 3,
                "Whenever this creature becomes blocked, draw a card."),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender,
            creature(
                "Blocking Trigger", 2, 2,
                "Whenever this creature blocks, draw a card."),
            "battlefield")
        game_state.ability_handler.active_triggers.clear()
        game_state.phase = game_state.PHASE_DECLARE_BLOCKERS
        game_state.priority_player = defender
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {attacker: [blocker]}
        self.assertTrue(handler.combat_handler.handle_declare_blockers_done())
        sources = [
            ability.card_id for ability, _, _ in
            game_state.ability_handler.active_triggers
        ]
        self.assertEqual(sources.count(attacker), 1, sources)
        self.assertEqual(sources.count(blocker), 1, sources)

    def test_attack_search_simulates_each_candidate_once_and_restores_state(self):
        game_state, _, active, defender = self._combat_state(208013)
        first = inject_into_zone(
            game_state, active, creature("Search Attacker A", 2, 2),
            "battlefield")
        second = inject_into_zone(
            game_state, active, creature("Search Attacker B", 3, 3),
            "battlefield")
        sentinel_blocker = inject_into_zone(
            game_state, defender, creature("Search State Sentinel"),
            "battlefield")
        game_state.current_attackers = [first]
        game_state.current_block_assignments = {first: [sentinel_blocker]}
        resolver = game_state.combat_resolver
        original_simulate = resolver.simulate_combat
        original_blocks = resolver._simulate_opponent_blocks
        original_planner = game_state.strategic_planner
        simulation_calls = []

        class PlannerProbe:
            def __init__(self):
                self.seen = []

            def evaluate_attack_action(
                    self, attacker_ids, simulation=None):
                self.seen.append((list(attacker_ids), simulation))
                return float(simulation["expected_value"])

        planner = PlannerProbe()

        def simulate_once():
            candidate = list(game_state.current_attackers)
            simulation_calls.append(candidate)
            score = float(len(candidate))
            return {
                "expected_value": score,
                "damage_to_player": len(candidate),
                "attackers_dying": [],
                "blockers_dying": [],
                "life_gained": 0,
            }

        try:
            resolver.simulate_combat = simulate_once
            resolver._simulate_opponent_blocks = lambda: None
            game_state.strategic_planner = planner
            chosen = resolver.find_optimal_attack([first, second])
        finally:
            resolver.simulate_combat = original_simulate
            resolver._simulate_opponent_blocks = original_blocks
            game_state.strategic_planner = original_planner

        self.assertEqual(chosen, [first, second])
        self.assertEqual(len(simulation_calls), 3)
        self.assertEqual(len(planner.seen), 3)
        for candidate, simulation in planner.seen:
            self.assertEqual(
                simulation["expected_value"], float(len(candidate)))
        self.assertEqual(game_state.current_attackers, [first])
        self.assertEqual(
            game_state.current_block_assignments,
            {first: [sentinel_blocker]})

    def test_block_evaluation_is_finite_and_restores_state_on_failure(self):
        game_state, _, active, defender = self._combat_state(208014)
        attacker = inject_into_zone(
            game_state, active, creature("Evaluation Attacker", 4, 4),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender, creature("Evaluation Blocker", 2, 2),
            "battlefield")
        game_state.agent_is_p1 = defender is game_state.p1
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {}
        planner = game_state.strategic_planner
        planner.current_analysis = {
            "game_info": {"game_stage": "mid"},
            "position": {"overall": "even"},
        }
        resolver = planner.combat_resolver
        original_simulate = resolver.simulate_combat
        original_agent_is_p1 = game_state.agent_is_p1

        def fail_simulation():
            raise RuntimeError("intentional combat simulation failure")

        try:
            resolver.simulate_combat = fail_simulation
            value = planner.evaluate_block_action(attacker, [blocker])
        finally:
            resolver.simulate_combat = original_simulate

        self.assertIsInstance(value, float)
        self.assertTrue(math.isfinite(value))
        self.assertEqual(game_state.current_attackers, [attacker])
        self.assertEqual(game_state.current_block_assignments, {})
        self.assertEqual(game_state.agent_is_p1, original_agent_is_p1)

    def test_combat_simulation_first_strike_removes_blocker_before_regular_damage(self):
        game_state, _, active, defender = self._combat_state(208015)
        attacker = inject_into_zone(
            game_state, active,
            creature("First-Strike Attacker", 2, 2, "First strike"),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender, creature("Ordinary Blocker", 2, 2),
            "battlefield")
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {attacker: [blocker]}

        result = game_state.combat_resolver.simulate_combat()

        self.assertEqual(result["attackers_dying"], [])
        self.assertEqual(result["blockers_dying"], [blocker])
        self.assertEqual(
            game_state.current_block_assignments, {attacker: [blocker]})

    def test_combat_simulation_remembers_block_after_first_strike_kill(self):
        game_state, _, active, defender = self._combat_state(208021)
        attacker = inject_into_zone(
            game_state, active,
            creature("Remembering Double Striker", 2, 2, "Double strike"),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender, creature("First-Step Blocker", 1, 1),
            "battlefield")
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {attacker: [blocker]}
        game_state.blocked_attackers_this_combat = {attacker}

        result = game_state.combat_resolver.simulate_combat()

        self.assertEqual(result["damage_to_player"], 0)
        self.assertEqual(result["blockers_dying"], [blocker])

    def test_combat_simulation_restores_one_shot_replacement_effect(self):
        game_state, _, active, _ = self._combat_state(208022)
        attacker = inject_into_zone(
            game_state, active, creature("Shield Probe", 3, 3),
            "battlefield")
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {}

        def prevent_damage(context):
            modified = dict(context)
            modified["damage_amount"] = 0
            return modified

        replacement_system = game_state.replacement_effects
        effect_id = replacement_system.register_effect({
            "source_id": attacker,
            "event_type": "DAMAGE",
            "condition": lambda context: (
                context.get("source_id") == attacker),
            "replacement": prevent_damage,
            "duration": "permanent",
            "apply_once": True,
            "description": "Simulation shield",
        })
        active_effects_object = replacement_system.active_effects
        effect_index_object = replacement_system.effect_index

        result = game_state.combat_resolver.simulate_combat()

        self.assertEqual(result["damage_to_player"], 0)
        self.assertIs(replacement_system.active_effects, active_effects_object)
        self.assertIs(replacement_system.effect_index, effect_index_object)
        self.assertIn(
            effect_id,
            {effect.get("effect_id")
             for effect in replacement_system.active_effects})
        self.assertIn(
            effect_id,
            {effect.get("effect_id")
             for effect in replacement_system.effect_index["DAMAGE"]})

    def test_combat_simulation_keeps_damage_simultaneous_with_deathtouch(self):
        game_state, _, active, defender = self._combat_state(208016)
        attacker = inject_into_zone(
            game_state, active,
            creature("Tiny Deathtoucher", 1, 1, "Deathtouch"),
            "battlefield")
        blocker = inject_into_zone(
            game_state, defender, creature("Large Blocker", 6, 6),
            "battlefield")
        game_state.current_attackers = [attacker]
        game_state.current_block_assignments = {attacker: [blocker]}

        result = game_state.combat_resolver.simulate_combat()

        self.assertEqual(result["attackers_dying"], [attacker])
        self.assertEqual(result["blockers_dying"], [blocker])

    def test_combat_simulation_ignores_stale_off_battlefield_blockers(self):
        game_state, _, active, defender = self._combat_state(208017)
        attacker = inject_into_zone(
            game_state, active, creature("Unblocked After Removal", 3, 3),
            "battlefield")
        departed_blocker = inject_into_zone(
            game_state, defender, creature("Departed Blocker", 2, 2),
            "battlefield")
        self.assertTrue(game_state.move_card(
            departed_blocker, defender, "battlefield", defender,
            "graveyard"))
        game_state.current_attackers = [attacker]
        stale_assignments = {attacker: [departed_blocker]}
        game_state.current_block_assignments = stale_assignments

        result = game_state.combat_resolver.simulate_combat()

        self.assertEqual(result["damage_to_player"], 3)
        self.assertIs(game_state.current_block_assignments, stale_assignments)
        self.assertEqual(
            game_state.current_block_assignments,
            {attacker: [departed_blocker]})


if __name__ == "__main__":
    unittest.main()
