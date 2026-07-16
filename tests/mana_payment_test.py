"""Affordability/payment parity regressions for the enhanced mana system.

Run from the repository root with::

    python tests/mana_payment_test.py
"""

from __future__ import annotations

from collections import defaultdict
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.actions import ActionHandler  # noqa: E402
from Playersim.card import Card  # noqa: E402
from Playersim.game_state import GameState  # noqa: E402


logging.disable(logging.CRITICAL)


class HybridGenericAffordabilityTest(unittest.TestCase):
    def _state(self):
        abandon = Card({
            "name": "Abandon Attachments",
            "type_line": "Instant — Lesson",
            "mana_cost": "{1}{U/R}",
            "cmc": 2,
            "oracle_text": "You may discard a card. If you do, draw two cards.",
            "color_identity": ["R", "U"],
        })
        game_state = GameState({0: abandon})
        game_state.reset([0], [0], seed=19)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.agent_is_p1 = True
        game_state.priority_player = game_state.p1
        game_state.priority_pass_count = 0
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler

    def _add_card(self, game_state, data):
        card = Card(data)
        card_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        card.card_id = card_id
        game_state.card_db[card_id] = card
        return card_id, card

    def test_hybrid_unit_cannot_also_pay_generic(self):
        game_state, _ = self._state()
        player = game_state.p1
        cost = game_state.mana_system.parse_mana_cost("{1}{U/R}")

        player["mana_pool"]["R"] = 1
        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))

        player["mana_pool"]["R"] = 2
        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))

    def test_overlapping_hybrid_pips_use_a_complete_assignment(self):
        game_state, _ = self._state()
        player = game_state.p1
        cost = game_state.mana_system.parse_mana_cost("{W/U}{W/B}")
        player["mana_pool"]["U"] = 1
        player["mana_pool"]["W"] = 1

        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertEqual(player["mana_pool"]["U"], 0)
        self.assertEqual(player["mana_pool"]["W"], 0)

    def test_snow_unit_cannot_also_pay_generic_and_failure_mints_nothing(self):
        game_state, _ = self._state()
        player = game_state.p1
        player["mana_pool"]["U"] = 1
        player["snow_mana_pool"]["U"] = 1
        cost = game_state.mana_system.parse_mana_cost("{1}{S}")

        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        self.assertFalse(game_state.mana_system.pay_mana_cost(
            player, cost, context={}))
        self.assertEqual(player["mana_pool"]["U"], 1)
        self.assertEqual(player["snow_mana_pool"]["U"], 1)

        # Tracked mana spends belong to transaction-local pool copies. A
        # rollback must not add them to the untouched live pool.
        payment = {
            "colors": defaultdict(int, {"U": 1}),
            "conditional": defaultdict(lambda: defaultdict(int)),
            "phase_restricted": defaultdict(int),
            "life": 0, "snow": 1, "snow_tapped_sources": [],
            "tapped_creatures": [], "exiled_cards": [],
            "sacrificed_perms": [], "discarded_cards": [],
        }
        game_state.mana_system._refund_payment(player, payment)
        self.assertEqual(player["mana_pool"]["U"], 1)

    def test_phyrexian_pips_reserve_mana_and_aggregate_life(self):
        game_state, _ = self._state()
        player = game_state.p1
        cost = game_state.mana_system.parse_mana_cost("{U/P}{U/P}")

        player["life"] = 3
        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        player["life"] = 4
        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertEqual(player["life"], 0)

    def test_abandon_auto_taps_generic_land_after_hybrid_pool_unit(self):
        game_state, handler = self._state()
        player = game_state.p1
        forest = Card({
            "name": "Test Forest", "type_line": "Basic Land — Forest",
            "mana_cost": "", "cmc": 0, "oracle_text": "{T}: Add {G}.",
            "color_identity": ["G"],
        })
        forest_id = max(
            [key for key in game_state.card_db if isinstance(key, int)],
            default=-1) + 1
        forest.card_id = forest_id
        game_state.card_db[forest_id] = forest
        player["battlefield"].append(forest_id)
        game_state._last_card_locations[forest_id] = (player, "battlefield")
        abandon_id = player["hand"][0]
        player["mana_pool"]["R"] = 1

        valid = handler.generate_valid_actions()
        self.assertTrue(valid[20])
        handler.current_valid_actions = valid
        _, _, _, info = handler.apply_action(20)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertNotIn(abandon_id, player["hand"])
        self.assertIn(forest_id, player["tapped_permanents"])
        self.assertEqual(player["mana_pool"]["R"], 0)

    def test_mask_only_exposes_abandon_when_live_payment_can_succeed(self):
        game_state, handler = self._state()
        player = game_state.p1
        card_id = player["hand"][0]
        self.assertEqual(game_state._safe_get_card(card_id).name,
                         "Abandon Attachments")

        player["mana_pool"]["R"] = 1
        invalid_mask = handler.generate_valid_actions()
        self.assertFalse(invalid_mask[20])

        player["mana_pool"]["R"] = 2
        valid_mask = handler.generate_valid_actions()
        self.assertTrue(valid_mask[20])
        handler.current_valid_actions = valid_mask
        _, _, _, info = handler.apply_action(20)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertNotIn(card_id, player["hand"])
        self.assertEqual(player["mana_pool"]["R"], 0)

    def test_late_mana_failure_does_not_move_or_reset_paid_objects(self):
        """Mana failure happens before validated non-mana costs are committed.

        Historically this valid composite payment exiled, sacrificed, and
        discarded before the forced late mana failure.  The refund then
        appended the creature to the battlefield as a new object, after
        move_card had already cleared its counters, tapped state, attachment,
        registrations, and emitted leave/dies triggers.  In particular, this
        exercises the old rollback with a nonempty ``sacrificed_perms`` list.
        """
        game_state, _ = self._state()
        player = game_state.p1

        creature = Card({
            "name": "Composite Cost Creature",
            "type_line": "Creature - Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["G"],
        })
        equipment = Card({
            "name": "Composite Cost Equipment",
            "type_line": "Artifact - Equipment",
            "mana_cost": "{1}",
            "cmc": 1,
            "oracle_text": "Equipped creature gets +1/+1.",
            "color_identity": [],
        })
        evidence = Card({
            "name": "Composite Cost Evidence",
            "type_line": "Sorcery",
            "mana_cost": "{2}",
            "cmc": 2,
            "oracle_text": "",
            "color_identity": [],
        })
        next_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        creature_id, equipment_id, evidence_id = range(next_id, next_id + 3)
        for card_id, card in (
                (creature_id, creature),
                (equipment_id, equipment),
                (evidence_id, evidence)):
            card.card_id = card_id
            game_state.card_db[card_id] = card

        player["battlefield"][:] = [creature_id, equipment_id]
        player["graveyard"][:] = [evidence_id]
        player["exile"][:] = []
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 1}
        player["tapped_permanents"].add(creature_id)
        player["attachments"] = {equipment_id: creature_id}
        creature.counters = {"+1/+1": 2, "shield": 1}
        for card_id, zone in (
                (creature_id, "battlefield"),
                (equipment_id, "battlefield"),
                (evidence_id, "graveyard")):
            game_state._last_card_locations[card_id] = (player, zone)

        battlefield_before = list(player["battlefield"])
        graveyard_before = list(player["graveyard"])
        attachments_before = dict(player["attachments"])
        counters_before = dict(creature.counters)
        generation_before = getattr(creature, "_zone_change_generation", 0)
        triggered = []
        original_trigger = type(game_state).trigger_ability

        def capture_trigger(state, source_id, event_type, context=None):
            triggered.append((source_id, event_type))
            return original_trigger(state, source_id, event_type, context)

        # The initial source allocation sees the available {C}; force the
        # post-activation, pool-only allocation to fail afterward. Non-mana
        # costs must still be entirely uncommitted at that point.
        original_plan = type(
            game_state.mana_system)._plan_mana_payment

        def fail_execution_allocation(
                mana_system, paying_player, cost, context=None,
                exclude_ids=None, include_lands=True):
            if not include_lands:
                return None
            return original_plan(
                mana_system, paying_player, cost, context,
                exclude_ids=exclude_ids, include_lands=include_lands)

        with patch.object(type(game_state), "trigger_ability", capture_trigger), \
                patch.object(
                    type(game_state.mana_system),
                    "_plan_mana_payment",
                    fail_execution_allocation):
            paid = game_state.mana_system.pay_mana_cost(
                player,
                game_state.mana_system.parse_mana_cost("{1}"),
                context={
                    "delve_cards": [0],
                    "sacrifice_additional": [0],
                    "discard_additional": [0],
                },
            )

        self.assertFalse(paid)
        self.assertEqual(player["battlefield"], battlefield_before)
        self.assertEqual(player["graveyard"], graveyard_before)
        self.assertEqual(player["exile"], [])
        self.assertEqual(player["mana_pool"]["C"], 1)
        self.assertEqual(player["attachments"], attachments_before)
        self.assertEqual(creature.counters, counters_before)
        self.assertIn(creature_id, player["tapped_permanents"])
        self.assertEqual(
            getattr(creature, "_zone_change_generation", 0),
            generation_before)
        self.assertEqual(
            game_state._last_card_locations[creature_id],
            (player, "battlefield"))
        self.assertEqual(triggered, [])

    def test_successful_composite_cost_commits_every_component(self):
        game_state, _ = self._state()
        player = game_state.p1
        creature = Card({
            "name": "Successful Cost Creature",
            "type_line": "Creature - Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "oracle_text": "",
            "power": 2,
            "toughness": 2,
            "color_identity": ["G"],
        })
        evidence = Card({
            "name": "Successful Cost Evidence",
            "type_line": "Sorcery",
            "mana_cost": "{2}",
            "cmc": 2,
            "oracle_text": "",
            "color_identity": [],
        })
        next_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        creature_id, evidence_id = next_id, next_id + 1
        for card_id, card in (
                (creature_id, creature), (evidence_id, evidence)):
            card.card_id = card_id
            game_state.card_db[card_id] = card

        player["battlefield"][:] = [creature_id]
        player["graveyard"][:] = [evidence_id]
        player["exile"][:] = []
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 1}
        hand_before = list(player["hand"])
        legacy_emerge_id = "already-paid-emerge-sacrifice"

        details = game_state.mana_system.pay_mana_cost_get_details(
            player,
            game_state.mana_system.parse_mana_cost("{1}"),
            context={
                "delve_cards": [0],
                "sacrifice_additional": [0],
                "discard_additional": [0],
                "emerge_sacrificed_id": legacy_emerge_id,
            },
        )

        self.assertIsNotNone(details)
        self.assertIn(evidence_id, player["exile"])
        self.assertNotIn(creature_id, player["battlefield"])
        self.assertIn(creature_id, player["graveyard"])
        self.assertEqual(player["hand"], hand_before[1:])
        self.assertIn(hand_before[0], player["graveyard"])
        self.assertEqual(player["mana_pool"]["C"], 0)
        self.assertEqual(
            details["payment"]["sacrificed_perms"],
            [creature_id, legacy_emerge_id])

    def test_recoverable_commit_failure_restores_auto_tap_state(self):
        game_state, _ = self._state()
        player = game_state.p1
        forest = Card({
            "name": "Transactional Forest",
            "type_line": "Basic Land - Forest",
            "mana_cost": "",
            "cmc": 0,
            "oracle_text": "{T}: Add {G}.",
            "color_identity": ["G"],
        })
        forest_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        forest.card_id = forest_id
        game_state.card_db[forest_id] = forest
        player["battlefield"][:] = [forest_id]
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}

        def reject_validated_commit(mana_system, paying_player, plan, payment):
            raise ValueError("forced recoverable validation failure")

        with patch.object(
                type(game_state.mana_system), "_commit_non_mana_payment",
                reject_validated_commit):
            paid = game_state.mana_system.pay_mana_cost(
                player,
                game_state.mana_system.parse_mana_cost("{G}"),
                context={})

        self.assertFalse(paid)
        self.assertNotIn(forest_id, player["tapped_permanents"])
        self.assertEqual(sum(player["mana_pool"].values()), 0)

    def test_snow_source_reserved_for_convoke_keeps_stun_counter(self):
        game_state, _ = self._state()
        player = game_state.p1
        wizard = Card({
            "name": "Stunned Snow Wizard",
            "type_line": "Snow Creature - Wizard",
            "mana_cost": "{1}",
            "cmc": 1,
            "oracle_text": "{T}: Add {C}.",
            "power": 1,
            "toughness": 1,
            "color_identity": [],
        })
        wizard_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        wizard.card_id = wizard_id
        wizard.counters = {"stun": 1}
        game_state.card_db[wizard_id] = wizard
        player["battlefield"][:] = [wizard_id]
        player["tapped_permanents"].clear()

        # This is the post-Convoke mana portion of {1}{S}: the creature is
        # already selected for Convoke and therefore cannot also pay {S}.
        snow_cost = game_state.mana_system.parse_mana_cost("{S}")
        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, snow_cost, context={"convoke_creatures": [0]}))
        self.assertFalse(
            game_state.mana_system.can_pay_mana_cost_with_lands(
                player, snow_cost,
                context={"convoke_creatures": [0]}))
        paid = game_state.mana_system.pay_mana_cost(
            player, snow_cost,
            context={"convoke_creatures": [0]},
        )

        self.assertFalse(paid)
        self.assertNotIn(wizard_id, player["tapped_permanents"])
        self.assertEqual(wizard.counters.get("stun"), 1)

    def test_one_snow_land_cannot_pay_generic_and_snow(self):
        game_state, _ = self._state()
        player = game_state.p1
        land_id, _ = self._add_card(game_state, {
            "name": "Lonely Snow Forest",
            "type_line": "Basic Snow Land - Forest",
            "mana_cost": "", "cmc": 0, "oracle_text": "{T}: Add {G}.",
            "color_identity": ["G"],
        })
        player["battlefield"][:] = [land_id]
        player["tapped_permanents"].clear()
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        cost = game_state.mana_system.parse_mana_cost("{1}{S}")

        self.assertFalse(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, cost, context={}))
        self.assertFalse(game_state.mana_system.pay_mana_cost(
            player, cost, context={}))
        self.assertNotIn(land_id, player["tapped_permanents"])

    def test_forest_and_nonland_snow_source_pay_colored_and_snow(self):
        game_state, _ = self._state()
        player = game_state.p1
        forest_id, _ = self._add_card(game_state, {
            "name": "Ordinary Forest", "type_line": "Basic Land - Forest",
            "mana_cost": "", "cmc": 0, "oracle_text": "{T}: Add {G}.",
            "color_identity": ["G"],
        })
        source_id, _ = self._add_card(game_state, {
            "name": "Snow Mana Construct",
            "type_line": "Snow Artifact Creature - Construct",
            "mana_cost": "{1}", "cmc": 1,
            "oracle_text": "{T}: Add {C}.",
            "power": 1, "toughness": 1, "color_identity": [],
        })
        player["battlefield"][:] = [forest_id, source_id]
        player["tapped_permanents"].clear()
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        cost = game_state.mana_system.parse_mana_cost("{G}{S}")

        self.assertTrue(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, cost, context={}))
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertIn(forest_id, player["tapped_permanents"])
        self.assertIn(source_id, player["tapped_permanents"])
        self.assertEqual(player["mana_pool"]["G"], 0)

    def test_mana_land_can_tap_then_pay_zone_move_cost(self):
        for payment_kind in ("sacrifice", "return", "bargain"):
            with self.subTest(payment_kind=payment_kind):
                game_state, _ = self._state()
                player = game_state.p1
                type_line = (
                    "Artifact Land" if payment_kind == "bargain"
                    else "Basic Land - Forest")
                symbol = "C" if payment_kind == "bargain" else "G"
                land_id, _ = self._add_card(game_state, {
                    "name": f"{payment_kind.title()} Mana Land",
                    "type_line": type_line, "mana_cost": "", "cmc": 0,
                    "oracle_text": f"{{T}}: Add {{{symbol}}}.",
                    "color_identity": ([] if symbol == "C" else ["G"]),
                })
                player["battlefield"][:] = [land_id]
                player["hand"][:] = []
                player["graveyard"][:] = []
                player["tapped_permanents"].clear()
                player["mana_pool"] = {
                    "W": 0, "U": 0, "B": 0,
                    "R": 0, "G": 0, "C": 0}
                context = {}
                if payment_kind == "sacrifice":
                    context["sacrifice_additional"] = [0]
                elif payment_kind == "return":
                    context.update({
                        "returned_for_additional_cost": land_id,
                        "_returned_for_additional_cost_index": 0,
                    })
                else:
                    context.update({
                        "bargained": True,
                        "bargain_sacrifice_id": land_id,
                    })
                details = game_state.mana_system.pay_mana_cost_get_details(
                    player,
                    game_state.mana_system.parse_mana_cost(
                        f"{{{symbol}}}"),
                    context=context)

                self.assertIsNotNone(details)
                self.assertNotIn(land_id, player["battlefield"])
                if payment_kind == "return":
                    self.assertIn(land_id, player["hand"])
                    self.assertEqual(
                        details["payment"]["returned_permanents"],
                        [land_id])
                else:
                    self.assertIn(land_id, player["graveyard"])
                    self.assertEqual(
                        details["payment"]["sacrificed_perms"],
                        [land_id])

    def test_checkpoint_creation_does_not_mutate_phased_out_card(self):
        game_state, _ = self._state()
        phased = Card({
            "name": "Phased Checkpoint", "type_line": "Creature - Spirit",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "power": 1, "toughness": 1, "color_identity": [],
        })
        phased_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        phased.card_id = phased_id
        phased.counters = {"stun": 2, "+1/+1": 3}
        game_state.card_db[phased_id] = phased
        game_state.phased_out.add(phased_id)
        game_state.phased_out_state[phased_id] = {
            "controller": game_state.p1,
            "tapped": True,
        }

        checkpoint = game_state.create_transaction_checkpoint()

        self.assertIs(game_state.card_db[phased_id], phased)
        self.assertEqual(phased.counters, {"stun": 2, "+1/+1": 3})
        cloned = checkpoint["state"].card_db[phased_id]
        self.assertIsNot(cloned, phased)
        self.assertEqual(cloned.counters, phased.counters)

    def test_checkpoint_restores_all_slots_and_subsystems_in_place(self):
        game_state, _ = self._state()
        systems = {
            name: getattr(game_state, name)
            for name in (
                "ability_handler", "layer_system", "mana_system",
                "replacement_effects", "targeting_system", "combat_resolver",
                "action_handler", "card_evaluator", "strategic_planner",
                "combat_action_handler")
        }
        card_db_container = game_state.card_db
        ceased_container = game_state._ceased_token_cards
        external_services = {
            "strategy_memory": object(),
            "stats_tracker": object(),
            "card_memory": object(),
        }
        for name, service in external_services.items():
            setattr(game_state, name, service)
        token_id, live_token = self._add_card(game_state, {
            "name": "Rollback Token", "type_line": "Token Creature",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "power": 1, "toughness": 1, "color_identity": [],
        })
        live_token.is_token = True
        live_token.counters = {"shield": 1}
        game_state.p1["battlefield"].append(token_id)
        ceased_id = token_id + 1
        preexisting_ceased = Card({
            "name": "Already Ceased", "type_line": "Token Creature",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "power": 1, "toughness": 1, "color_identity": [],
        })
        preexisting_ceased.card_id = ceased_id
        preexisting_ceased.is_token = True
        preexisting_ceased.counters = {"memory": 2}
        game_state._ceased_token_cards[ceased_id] = preexisting_ceased
        game_state.cards_milled_this_turn = {"probe": 7}
        game_state.creatures_died_this_turn = {"probe": 3}
        game_state._consecutive_no_ops = 9
        game_state.impulse_until_eot = {"probe": {"turn": 4}}
        game_state.attack_suggestion_used = True
        game_state.optimal_attackers = ["probe-attacker"]
        game_state.combat_resolver.combat_log = ["before"]
        game_state.ability_handler.active_triggers = [
            ("probe-ability", game_state.p1, {"controller": game_state.p1})]
        checkpoint = game_state.create_transaction_checkpoint()

        game_state.cards_milled_this_turn = {}
        game_state.creatures_died_this_turn = {}
        game_state._consecutive_no_ops = 0
        game_state.impulse_until_eot = {}
        game_state.attack_suggestion_used = False
        game_state.optimal_attackers = []
        game_state.combat_resolver.combat_log.append("mutated")
        game_state.ability_handler.active_triggers.clear()
        game_state.p1["battlefield"].remove(token_id)
        game_state.card_db.pop(token_id)
        game_state._ceased_token_cards[token_id] = live_token
        live_token.counters = {}
        preexisting_ceased.counters = {"memory": 99}
        with patch.object(
                type(game_state), "_init_subsystems",
                side_effect=AssertionError("restore called constructors")):
            game_state.restore_transaction_checkpoint(checkpoint)

        self.assertEqual(game_state.cards_milled_this_turn, {"probe": 7})
        self.assertEqual(game_state.creatures_died_this_turn, {"probe": 3})
        self.assertEqual(game_state._consecutive_no_ops, 9)
        self.assertEqual(game_state.impulse_until_eot, {
            "probe": {"turn": 4}})
        self.assertTrue(game_state.attack_suggestion_used)
        self.assertEqual(game_state.optimal_attackers, ["probe-attacker"])
        self.assertEqual(game_state.combat_resolver.combat_log, ["before"])
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)
        self.assertIs(
            game_state.ability_handler.active_triggers[0][1], game_state.p1)
        for name, system in systems.items():
            self.assertIs(getattr(game_state, name), system, name)
        self.assertIs(game_state.card_db, card_db_container)
        self.assertIs(game_state._ceased_token_cards, ceased_container)
        self.assertIs(game_state.card_db[token_id], live_token)
        self.assertIn(token_id, game_state.p1["battlefield"])
        self.assertNotIn(token_id, game_state._ceased_token_cards)
        self.assertEqual(live_token.counters, {"shield": 1})
        self.assertIs(
            game_state._ceased_token_cards[ceased_id], preexisting_ceased)
        self.assertEqual(preexisting_ceased.counters, {"memory": 2})
        for name, service in external_services.items():
            self.assertIs(getattr(game_state, name), service, name)
        self.assertIs(game_state.mana_system.game_state, game_state)
        self.assertIs(game_state.action_handler.game_state, game_state)
        self.assertIs(
            game_state.targeting_system.ability_handler,
            game_state.ability_handler)
        self.assertIs(
            game_state.ability_handler.targeting_system,
            game_state.targeting_system)
        self.assertIs(
            game_state.combat_resolver.action_handler,
            game_state.combat_action_handler)

    def test_late_failure_restores_stunned_snow_source_without_untapping(self):
        game_state, _ = self._state()
        player = game_state.p1
        source = Card({
            "name": "Stunned Snow Source",
            "type_line": "Snow Artifact Creature - Construct",
            "mana_cost": "{1}",
            "cmc": 1,
            "oracle_text": "{T}: Add {C}.",
            "power": 1,
            "toughness": 1,
            "color_identity": [],
        })
        fodder = Card({
            "name": "Snow Transaction Fodder",
            "type_line": "Creature - Bear",
            "mana_cost": "",
            "cmc": 0,
            "oracle_text": "",
            "power": 1,
            "toughness": 1,
            "color_identity": [],
        })
        source_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        fodder_id = source_id + 1
        source.card_id = source_id
        fodder.card_id = fodder_id
        source.counters = {"stun": 1}
        game_state.card_db[source_id] = source
        game_state.card_db[fodder_id] = fodder
        player["battlefield"][:] = [source_id, fodder_id]
        player["tapped_permanents"].clear()
        original_move = type(game_state).move_card

        def fail_live_sacrifice(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=None, context=None):
            if state is game_state and cause == "additional_cost_sacrifice":
                return False
            return original_move(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=cause, context=context)

        with patch.object(
                type(game_state), "move_card", fail_live_sacrifice):
            paid = game_state.mana_system.pay_mana_cost(
                player, game_state.mana_system.parse_mana_cost("{S}"),
                context={"sacrifice_additional": [1]})

        self.assertFalse(paid)
        self.assertNotIn(source_id, player["tapped_permanents"])
        self.assertEqual(source.counters.get("stun"), 1)
        self.assertEqual(player["battlefield"], [source_id, fodder_id])

    def test_late_live_move_failure_restores_exact_payment_checkpoint(self):
        game_state, _ = self._state()
        player = game_state.p1
        creature = Card({
            "name": "Checkpoint Bear", "type_line": "Creature - Bear",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "power": 2, "toughness": 2, "color_identity": [],
        })
        evidence = Card({
            "name": "Checkpoint Evidence", "type_line": "Sorcery",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "color_identity": [],
        })
        next_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        creature_id, evidence_id = next_id, next_id + 1
        for card_id, card in ((creature_id, creature), (evidence_id, evidence)):
            card.card_id = card_id
            game_state.card_db[card_id] = card
        player["battlefield"][:] = [creature_id]
        player["graveyard"][:] = [evidence_id]
        player["exile"][:] = []
        player["attachments"] = {"equipment-marker": creature_id}
        creature.counters = {"shield": 1, "+1/+1": 2}
        game_state._last_card_locations[creature_id] = (player, "battlefield")
        game_state._last_card_locations[evidence_id] = (player, "graveyard")
        player_identity = player
        creature_identity = creature
        active_triggers_before = list(game_state.ability_handler.active_triggers)
        original_move = type(game_state).move_card

        def fail_only_on_live_sacrifice(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=None, context=None):
            if state is game_state and cause == "additional_cost_sacrifice":
                return False
            return original_move(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=cause, context=context)

        with patch.object(
                type(game_state), "move_card", fail_only_on_live_sacrifice):
            paid = game_state.mana_system.pay_mana_cost(
                player,
                game_state.mana_system.parse_mana_cost("{0}"),
                context={
                    "delve_cards": [0],
                    "sacrifice_additional": [0],
                },
            )

        self.assertFalse(paid)
        self.assertIs(game_state.p1, player_identity)
        self.assertIs(game_state.card_db[creature_id], creature_identity)
        self.assertEqual(player["battlefield"], [creature_id])
        self.assertEqual(player["graveyard"], [evidence_id])
        self.assertEqual(player["exile"], [])
        self.assertEqual(creature.counters, {"shield": 1, "+1/+1": 2})
        self.assertEqual(player["attachments"], {
            "equipment-marker": creature_id})
        self.assertEqual(
            game_state.ability_handler.active_triggers,
            active_triggers_before)
        self.assertEqual(
            game_state._last_card_locations[creature_id],
            (player, "battlefield"))

    def test_global_allocator_phyrexian_life_preserves_generic_mana(self):
        game_state, _ = self._state()
        player = game_state.p1
        player["battlefield"][:] = []
        player["mana_pool"] = {
            "W": 1, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        player["life"] = 2
        cost = game_state.mana_system.parse_mana_cost("{W/P}{1}")

        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        self.assertTrue(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, cost, context={}))
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertEqual(player["life"], 0)
        self.assertEqual(player["mana_pool"]["W"], 0)

    def test_global_allocator_reassigns_hybrid_for_phyrexian_pip(self):
        game_state, _ = self._state()
        player = game_state.p1
        player["battlefield"][:] = []
        player["mana_pool"] = {
            "W": 1, "U": 1, "B": 0, "R": 0, "G": 0, "C": 0}
        player["life"] = 0
        cost = game_state.mana_system.parse_mana_cost("{W/U}{U/P}")

        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertEqual(player["mana_pool"]["W"], 0)
        self.assertEqual(player["mana_pool"]["U"], 0)

    def test_global_allocator_preserves_cross_pool_snow_provenance(self):
        game_state, _ = self._state()
        player = game_state.p1
        player["battlefield"][:] = []
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 1, "C": 0}
        player["snow_mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 1, "C": 0}
        player["phase_restricted_mana"] = {
            "W": 0, "U": 1, "B": 0, "R": 0, "G": 0, "C": 0}
        player["phase_restricted_snow_mana"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        cost = game_state.mana_system.parse_mana_cost("{G/U}{S}")

        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        self.assertEqual(
            game_state.mana_system._plan_auto_tap(
                player, cost, context={}), [])
        details = game_state.mana_system.pay_mana_cost_get_details(
            player, cost, context={})
        self.assertIsNotNone(details)
        self.assertEqual(player["mana_pool"]["G"], 0)
        self.assertEqual(player["snow_mana_pool"]["G"], 0)
        self.assertEqual(player["phase_restricted_mana"]["U"], 0)

    def test_one_snow_unit_cannot_pay_generic_and_snow(self):
        game_state, _ = self._state()
        player = game_state.p1
        player["battlefield"][:] = []
        player["mana_pool"] = {
            "W": 1, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        player["snow_mana_pool"] = {
            "W": 1, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        cost = game_state.mana_system.parse_mana_cost("{1}{S}")
        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        self.assertFalse(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, cost, context={}))

        player["phase_restricted_mana"]["C"] = 1
        self.assertTrue(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        self.assertIsNotNone(
            game_state.mana_system.pay_mana_cost_get_details(
                player, cost, context={}))

    def test_phyrexian_land_is_planned_when_life_cannot_pay(self):
        game_state, _ = self._state()
        player = game_state.p1
        forest_id, _ = self._add_card(game_state, {
            "name": "Phyrexian Forest", "type_line": "Basic Land - Forest",
            "mana_cost": "", "cmc": 0, "oracle_text": "{T}: Add {G}.",
            "color_identity": ["G"],
        })
        player["battlefield"][:] = [forest_id]
        player["tapped_permanents"].clear()
        player["life"] = 1
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        cost = game_state.mana_system.parse_mana_cost("{G/P}")

        self.assertFalse(game_state.mana_system.can_pay_mana_cost(
            player, cost, context={}))
        self.assertTrue(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, cost, context={}))
        self.assertIsNotNone(
            game_state.mana_system.pay_mana_cost_get_details(
                player, cost, context={}))
        self.assertIn(forest_id, player["tapped_permanents"])
        self.assertEqual(player["life"], 1)

    def test_min_cost_allocator_chooses_safe_land_option(self):
        game_state, _ = self._state()
        player = game_state.p1
        pain_id, _ = self._add_card(game_state, {
            "name": "Choice Pain Land", "type_line": "Land",
            "mana_cost": "", "cmc": 0,
            "oracle_text": (
                "{T}: Add {C}.\n"
                "{T}: Add {W}. Choice Pain Land deals 1 damage to you."),
            "color_identity": ["W"],
        })
        safe_id, _ = self._add_card(game_state, {
            "name": "Safe Plains", "type_line": "Basic Land - Plains",
            "mana_cost": "", "cmc": 0, "oracle_text": "{T}: Add {W}.",
            "color_identity": ["W"],
        })
        player["battlefield"][:] = [pain_id, safe_id]
        player["tapped_permanents"].clear()
        player["life"] = 2
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
        cost = game_state.mana_system.parse_mana_cost("{W/P}{U/P}")

        self.assertTrue(game_state.mana_system.can_pay_mana_cost_with_lands(
            player, cost, context={}))
        self.assertIsNotNone(
            game_state.mana_system.pay_mana_cost_get_details(
                player, cost, context={}))
        self.assertIn(safe_id, player["tapped_permanents"])
        self.assertNotIn(pain_id, player["tapped_permanents"])
        self.assertEqual(player["life"], 0)

    def test_preflight_move_failure_does_not_touch_shared_analytics(self):
        game_state, _ = self._state()
        player = game_state.p1
        creature = Card({
            "name": "Analytics Bear", "type_line": "Creature - Bear",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "power": 1, "toughness": 1, "color_identity": [],
        })
        evidence = Card({
            "name": "Analytics Evidence", "type_line": "Sorcery",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        next_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        creature_id, evidence_id = next_id, next_id + 1
        for card_id, card in ((creature_id, creature), (evidence_id, evidence)):
            card.card_id = card_id
            game_state.card_db[card_id] = card
        player["battlefield"][:] = [creature_id]
        player["graveyard"][:] = [evidence_id]
        player["exile"][:] = []

        class AnalyticsSpy:
            def __init__(self):
                self.events = []

        analytics = AnalyticsSpy()
        game_state.stats_tracker = analytics
        original_move = type(game_state).move_card

        def observe_then_reject_sacrifice(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=None, context=None):
            tracker = getattr(state, "stats_tracker", None)
            if tracker is not None:
                tracker.events.append((card_id, cause))
            if cause == "additional_cost_sacrifice":
                return False
            return original_move(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=cause, context=context)

        with patch.object(
                type(game_state), "move_card", observe_then_reject_sacrifice):
            paid = game_state.mana_system.pay_mana_cost(
                player,
                game_state.mana_system.parse_mana_cost("{0}"),
                context={
                    "delve_cards": [0],
                    "sacrifice_additional": [0],
                },
            )

        self.assertFalse(paid)
        self.assertEqual(analytics.events, [])
        self.assertEqual(player["graveyard"], [evidence_id])
        self.assertEqual(player["exile"], [])

    def test_cast_source_failure_restores_context_costs(self):
        game_state, _ = self._state()
        player = game_state.p1
        spell = Card({
            "name": "Transactional Spell", "type_line": "Sorcery",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "color_identity": [],
        })
        creature = Card({
            "name": "Transactional Offering", "type_line": "Creature",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "power": 1, "toughness": 1, "color_identity": [],
        })
        evidence = Card({
            "name": "Transactional Evidence", "type_line": "Instant",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        fodder = Card({
            "name": "Transactional Fodder", "type_line": "Instant",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        next_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        spell_id, creature_id, evidence_id, fodder_id = range(
            next_id, next_id + 4)
        for card_id, card in (
                (spell_id, spell), (creature_id, creature),
                (evidence_id, evidence), (fodder_id, fodder)):
            card.card_id = card_id
            game_state.card_db[card_id] = card
        player["hand"][:] = [spell_id, fodder_id]
        player["battlefield"][:] = [creature_id]
        player["graveyard"][:] = [evidence_id]
        player["exile"][:] = []
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 1}
        original_pay = game_state.mana_system.pay_mana_cost_get_details

        def pay_then_remove_source(paying_player, cost, context=None):
            details = original_pay(paying_player, cost, context)
            if details is not None:
                paying_player["hand"].remove(spell_id)
            return details

        with patch.object(
                game_state.mana_system, "pay_mana_cost_get_details",
                side_effect=pay_then_remove_source):
            cast = game_state.cast_spell(
                spell_id, player,
                context={
                    "source_zone": "hand", "source_idx": 0,
                    "delve_cards": [0],
                    "sacrifice_additional": [0],
                    "discard_additional": [1],
                })

        self.assertFalse(cast)
        self.assertEqual(player["hand"], [spell_id, fodder_id])
        self.assertEqual(player["battlefield"], [creature_id])
        self.assertEqual(player["graveyard"], [evidence_id])
        self.assertEqual(player["exile"], [])
        self.assertEqual(player["mana_pool"]["C"], 1)

    def test_live_bargain_failure_restores_earlier_context_costs(self):
        game_state, _ = self._state()
        player = game_state.p1
        spell = Card({
            "name": "Broken Bargain", "type_line": "Sorcery",
            "mana_cost": "{0}", "cmc": 0,
            "oracle_text": "Bargain\nDraw a card.", "color_identity": [],
        })
        offering = Card({
            "name": "Bargain Relic", "type_line": "Artifact",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        evidence = Card({
            "name": "Bargain Evidence", "type_line": "Instant",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        next_id = max(
            (key for key in game_state.card_db if isinstance(key, int)),
            default=-1) + 1
        spell_id, offering_id, evidence_id = range(next_id, next_id + 3)
        for card_id, card in (
                (spell_id, spell), (offering_id, offering),
                (evidence_id, evidence)):
            card.card_id = card_id
            game_state.card_db[card_id] = card
        player["hand"][:] = [spell_id]
        player["battlefield"][:] = [offering_id]
        player["graveyard"][:] = [evidence_id]
        player["exile"][:] = []
        original_move = type(game_state).move_card

        def fail_live_bargain(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=None, context=None):
            if state is game_state and cause == "bargain":
                return False
            return original_move(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=cause, context=context)

        with patch.object(type(game_state), "move_card", fail_live_bargain):
            cast = game_state.cast_spell(
                spell_id, player,
                context={
                    "source_zone": "hand", "source_idx": 0,
                    "bargain_choice_complete": True,
                    "bargained": True,
                    "bargain_sacrifice_id": offering_id,
                    "delve_cards": [0],
                })

        self.assertFalse(cast)
        self.assertEqual(player["hand"], [spell_id])
        self.assertEqual(player["battlefield"], [offering_id])
        self.assertEqual(player["graveyard"], [evidence_id])
        self.assertEqual(player["exile"], [])

    def test_restored_replacement_callback_captures_live_graph(self):
        game_state, _ = self._state()
        live_replacements = game_state.replacement_effects
        captured_state = game_state
        captured_replacements = live_replacements

        def replacement(event):
            captured_state.p1["life"] -= 1
            captured_replacements.effect_counter += 7
            return event

        effect = {
            "effect_id": "callback-restore-probe",
            "event_type": "CALLBACK_PROBE",
            "condition": lambda event: True,
            "replacement": replacement,
        }
        live_replacements.active_effects = [effect]
        live_replacements.effect_index = defaultdict(
            list, {"CALLBACK_PROBE": [effect]})
        checkpoint = game_state.create_transaction_checkpoint()
        snapshot = checkpoint["state"]
        snapshot_life = snapshot.p1["life"]
        snapshot_counter = snapshot.replacement_effects.effect_counter
        game_state.p1["life"] = 3
        game_state.restore_transaction_checkpoint(checkpoint)

        restored_effect = game_state.replacement_effects.active_effects[0]
        life_before = game_state.p1["life"]
        counter_before = game_state.replacement_effects.effect_counter
        restored_effect["replacement"]({"event_type": "CALLBACK_PROBE"})

        self.assertEqual(game_state.p1["life"], life_before - 1)
        self.assertEqual(
            game_state.replacement_effects.effect_counter,
            counter_before + 7)
        self.assertEqual(snapshot.p1["life"], snapshot_life)
        self.assertEqual(
            snapshot.replacement_effects.effect_counter, snapshot_counter)
        self.assertIs(
            game_state.replacement_effects.effect_index[
                "CALLBACK_PROBE"][0], restored_effect)

    def test_clone_rebinds_replacement_and_layer_callbacks(self):
        game_state, _ = self._state()
        player = game_state.p1
        source_id, _ = self._add_card(game_state, {
            "name": "Layer Callback Source",
            "type_line": "Creature - Wizard", "mana_cost": "{1}",
            "cmc": 1, "oracle_text": "", "power": 1, "toughness": 1,
            "color_identity": [],
        })
        player["battlefield"][:] = [source_id]
        captured_state = game_state
        captured_replacements = game_state.replacement_effects
        captured_layers = game_state.layer_system

        def replacement(event):
            captured_state.p1["life"] -= 1
            captured_replacements.effect_counter += 5
            return event

        def layer_condition(_state):
            captured_state._consecutive_no_ops += 1
            captured_layers.effect_counter += 1
            return False

        replacement_effect = {
            "effect_id": "clone-callback-probe",
            "event_type": "CLONE_CALLBACK_PROBE",
            "condition": lambda event: True,
            "replacement": replacement,
        }
        game_state.replacement_effects.active_effects = [replacement_effect]
        game_state.replacement_effects.effect_index = defaultdict(
            list, {"CLONE_CALLBACK_PROBE": [replacement_effect]})
        game_state.layer_system.layers[1] = [("layer-callback-probe", {
            "source_id": source_id,
            "affected_ids": [source_id],
            "effect_type": "copy",
            "condition": layer_condition,
        })]
        game_state.layer_system._last_applied_state_hash = None
        game_state._consecutive_no_ops = 0
        live_life = player["life"]
        live_replacement_counter = \
            game_state.replacement_effects.effect_counter
        live_layer_counter = game_state.layer_system.effect_counter

        cloned = game_state.clone()

        self.assertEqual(game_state._consecutive_no_ops, 0)
        self.assertEqual(game_state.layer_system.effect_counter,
                         live_layer_counter)
        self.assertEqual(cloned._consecutive_no_ops, 1)
        self.assertEqual(cloned.layer_system.effect_counter,
                         live_layer_counter + 1)
        clone_effect = cloned.replacement_effects.active_effects[0]
        clone_effect["replacement"]({"event_type": "CLONE_CALLBACK_PROBE"})
        self.assertEqual(cloned.p1["life"], live_life - 1)
        self.assertEqual(game_state.p1["life"], live_life)
        self.assertEqual(
            cloned.replacement_effects.effect_counter,
            live_replacement_counter + 5)
        self.assertEqual(
            game_state.replacement_effects.effect_counter,
            live_replacement_counter)
        self.assertIs(
            cloned.replacement_effects.effect_index[
                "CLONE_CALLBACK_PROBE"][0], clone_effect)

    def test_late_failure_restores_sacrificed_token_identity(self):
        game_state, _ = self._state()
        player = game_state.p1
        token_id, token = self._add_card(game_state, {
            "name": "Transactional Token", "type_line": "Token Creature",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "power": 1, "toughness": 1, "color_identity": [],
        })
        token.is_token = True
        token.counters = {"shield": 1}
        discard_id, _ = self._add_card(game_state, {
            "name": "Transactional Discard", "type_line": "Sorcery",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        player["battlefield"][:] = [token_id]
        player["hand"][:] = [discard_id]
        player["graveyard"][:] = []
        original_move = type(game_state).move_card

        def fail_live_discard(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=None, context=None):
            if (state is game_state
                    and cause == "additional_cost_discard"):
                return False
            return original_move(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=cause, context=context)

        with patch.object(type(game_state), "move_card", fail_live_discard):
            paid = game_state.mana_system.pay_mana_cost(
                player, game_state.mana_system.parse_mana_cost("{0}"),
                context={
                    "sacrifice_additional": [0],
                    "discard_additional": [0],
                })

        self.assertFalse(paid)
        self.assertIs(game_state.card_db[token_id], token)
        self.assertEqual(player["battlefield"], [token_id])
        self.assertEqual(player["hand"], [discard_id])
        self.assertEqual(player["graveyard"], [])
        self.assertNotIn(token_id, game_state._ceased_token_cards)
        self.assertEqual(token.counters, {"shield": 1})

    def test_graveyard_exile_redirect_preserves_exact_live_occurrence(self):
        game_state, _ = self._state()
        player = game_state.p1
        delve_id, _ = self._add_card(game_state, {
            "name": "Delve Card", "type_line": "Instant",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "color_identity": [],
        })
        middle_id, _ = self._add_card(game_state, {
            "name": "Middle Card", "type_line": "Instant",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "color_identity": [],
        })
        evidence_id, _ = self._add_card(game_state, {
            "name": "Redirected Evidence", "type_line": "Sorcery",
            "mana_cost": "{2}", "cmc": 2, "oracle_text": "",
            "color_identity": [],
        })
        player["graveyard"][:] = [delve_id, middle_id, evidence_id]
        player["exile"][:] = []
        original_move = type(game_state).move_card

        def redirect_evidence_to_graveyard(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=None, context=None):
            if cause == "collect_evidence":
                from_player["graveyard"].append(card_id)
                return True
            return original_move(
                state, card_id, from_player, from_zone, to_player, to_zone,
                cause=cause, context=context)

        with patch.object(
                type(game_state), "move_card",
                redirect_evidence_to_graveyard):
            details = game_state.mana_system.pay_mana_cost_get_details(
                player, game_state.mana_system.parse_mana_cost("{0}"),
                context={
                    "delve_cards": [0],
                    "evidence_collected": True,
                    "evidence_cards": [evidence_id],
                    "_evidence_choices": [(2, evidence_id)],
                    "_evidence_threshold": 2,
                })

        self.assertIsNotNone(details)
        self.assertEqual(player["graveyard"], [middle_id, evidence_id])
        self.assertEqual(player["exile"], [delve_id])
        self.assertEqual(
            details["payment"]["exiled_cards"],
            [evidence_id, delve_id])

    def test_convoke_and_improvise_reject_wrong_permanent_types(self):
        for context_key, type_line in (
                ("convoke_creatures", "Enchantment"),
                ("improvise_artifacts", "Creature - Bear")):
            with self.subTest(context_key=context_key):
                game_state, _ = self._state()
                player = game_state.p1
                permanent_id, _ = self._add_card(game_state, {
                    "name": "Wrong Tap Type", "type_line": type_line,
                    "mana_cost": "", "cmc": 0, "oracle_text": "",
                    "power": 1, "toughness": 1, "color_identity": [],
                })
                player["battlefield"][:] = [permanent_id]
                player["tapped_permanents"].clear()
                paid = game_state.mana_system.pay_mana_cost(
                    player, game_state.mana_system.parse_mana_cost("{0}"),
                    context={context_key: [0]})
                self.assertFalse(paid)
                self.assertNotIn(
                    permanent_id, player["tapped_permanents"])

    def test_cast_rejects_stale_explicit_source_occurrence(self):
        game_state, _ = self._state()
        player = game_state.p1
        spell_id, _ = self._add_card(game_state, {
            "name": "Duplicate Source Spell", "type_line": "Instant",
            "mana_cost": "{0}", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        other_id, _ = self._add_card(game_state, {
            "name": "Wrong Source Occurrence", "type_line": "Instant",
            "mana_cost": "{0}", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        player["hand"][:] = [spell_id, other_id, spell_id]
        hand_before = list(player["hand"])

        cast = game_state.cast_spell(
            spell_id, player,
            context={"source_zone": "hand", "source_idx": 1})

        self.assertFalse(cast)
        self.assertEqual(player["hand"], hand_before)
        self.assertEqual(game_state.stack, [])

    def test_post_source_context_failure_restores_complete_cast(self):
        game_state, _ = self._state()
        player = game_state.p1
        spell_id, _ = self._add_card(game_state, {
            "name": "Context Construction Fault", "type_line": "Instant",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "color_identity": [],
        })
        player["hand"][:] = [spell_id]
        player["mana_pool"] = {
            "W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 1}
        cast_context = {"source_zone": "hand", "source_idx": 0}

        with patch.object(
                game_state.mana_system,
                "spell_characteristics_for_cast",
                side_effect=RuntimeError("forced context failure")):
            cast = game_state.cast_spell(
                spell_id, player, context=cast_context)

        self.assertFalse(cast)
        self.assertEqual(player["hand"], [spell_id])
        self.assertEqual(player["mana_pool"]["C"], 1)
        self.assertEqual(game_state.stack, [])
        self.assertFalse(any(
            key.startswith("_payment_") for key in cast_context))

    def test_stack_insertion_failure_restores_cavern_rider_and_context(self):
        game_state, _ = self._state()
        player = game_state.p1
        spell_id, spell = self._add_card(game_state, {
            "name": "Cavern Transaction Elf",
            "type_line": "Creature - Elf", "mana_cost": "{G}",
            "cmc": 1, "oracle_text": "", "power": 1, "toughness": 1,
            "color_identity": ["G"],
        })
        player["hand"][:] = [spell_id]
        restriction_key = (
            "cast_only:creature spell of the chosen type (elf), and that "
            "spell can't be countered")
        player["conditional_mana"] = {
            restriction_key: {
                "W": 0, "U": 0, "B": 0,
                "R": 0, "G": 1, "C": 0}}
        cast_context = {
            "source_zone": "hand", "source_idx": 0, "card": spell}

        with patch.object(
                type(game_state), "add_to_stack",
                side_effect=RuntimeError("forced stack insertion failure")):
            cast = game_state.cast_spell(
                spell_id, player, context=cast_context)

        self.assertFalse(cast)
        self.assertEqual(player["hand"], [spell_id])
        self.assertEqual(
            player["conditional_mana"][restriction_key]["G"], 1)
        self.assertEqual(game_state.stack, [])
        self.assertNotIn("cant_be_countered", cast_context)
        self.assertFalse(any(
            key.startswith("_payment_") for key in cast_context))

    def test_forged_complete_mandatory_return_context_fails_closed(self):
        game_state, _ = self._state()
        player = game_state.p1
        permanent_id, _ = self._add_card(game_state, {
            "name": "Returnable Permanent", "type_line": "Artifact",
            "mana_cost": "", "cmc": 0, "oracle_text": "",
            "color_identity": [],
        })
        spell_id, _ = self._add_card(game_state, {
            "name": "Mandatory Return Spell",
            "type_line": "Creature - Nightmare", "mana_cost": "{0}",
            "cmc": 0, "power": 1, "toughness": 1,
            "oracle_text": (
                "As an additional cost to cast this spell, return a "
                "permanent you control to its owner's hand."),
            "color_identity": [],
        })
        player["battlefield"][:] = [permanent_id]
        player["hand"][:] = [spell_id]

        cast = game_state.cast_spell(
            spell_id, player,
            context={
                "source_zone": "hand", "source_idx": 0,
                "sample_nonmana_cost_complete": True,
            })

        self.assertFalse(cast)
        self.assertEqual(player["hand"], [spell_id])
        self.assertEqual(player["battlefield"], [permanent_id])
        self.assertEqual(game_state.stack, [])

    def test_graveyard_source_index_accounts_for_collected_evidence(self):
        game_state, _ = self._state()
        player = game_state.p1
        evidence_id, _ = self._add_card(game_state, {
            "name": "Cast Evidence", "type_line": "Instant",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "color_identity": [],
        })
        middle_id, _ = self._add_card(game_state, {
            "name": "Graveyard Middle", "type_line": "Instant",
            "mana_cost": "{1}", "cmc": 1, "oracle_text": "",
            "color_identity": [],
        })
        spell_id, _ = self._add_card(game_state, {
            "name": "Evidence Flashback", "type_line": "Sorcery",
            "mana_cost": "{0}", "cmc": 0,
            "oracle_text": (
                "As an additional cost to cast this spell, you may collect "
                "evidence 1.\nFlashback {0}"),
            "color_identity": [],
        })
        player["graveyard"][:] = [evidence_id, middle_id, spell_id]
        player["exile"][:] = []
        cast_context = {
            "source_zone": "graveyard", "source_idx": 2,
            "flashback_cast": True,
            "sample_nonmana_cost_complete": True,
            "evidence_collected": True,
            "evidence_cards": [evidence_id],
            "_evidence_choices": [(0, evidence_id)],
            "_evidence_threshold": 1,
        }

        cast = game_state.cast_spell(
            spell_id, player, context=cast_context)

        self.assertTrue(cast)
        self.assertEqual(player["graveyard"], [middle_id])
        self.assertEqual(player["exile"], [evidence_id])
        self.assertTrue(game_state.stack)
        self.assertEqual(game_state.stack[-1][1], spell_id)
        stack_context = game_state.stack[-1][3]
        self.assertTrue(stack_context["evidence_collected"])
        self.assertEqual(stack_context["evidence_cards"], [evidence_id])
        self.assertNotIn("_evidence_choices", stack_context)


if __name__ == "__main__":
    unittest.main()
