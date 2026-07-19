"""Public action regressions for announcing and paying Offspring."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_real_card  # noqa: E402
from Playersim.actions import ActionHandler  # noqa: E402


logging.disable(logging.CRITICAL)


class OffspringPublicActionTest(unittest.TestCase):
    @staticmethod
    def _prepare(card_names, mana, seed):
        game_state = fresh(seed)
        player = game_state.p1
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = player
        game_state.priority_pass_count = 0
        game_state.stack.clear()
        game_state.choice_context = None
        game_state.targeting_context = None
        game_state.sacrifice_context = None

        for card_id in list(player.get("hand", [])):
            assert game_state.move_card(
                card_id, player, "hand", player, "library")
        card_ids = [
            inject_real_card(game_state, player, name, "hand")
            for name in card_names]

        for color in ("W", "U", "B", "R", "G", "C"):
            player["mana_pool"][color] = int(mana.get(color, 0))

        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler, player, card_ids

    @staticmethod
    def _apply_public(handler, action_index):
        valid = handler.generate_valid_actions()
        handler.current_valid_actions = valid
        result = handler.apply_action(action_index)
        info = result[3]
        assert not info.get("execution_failed", False), info
        return result

    def test_paid_action_casts_and_resolves_real_offspring_trigger(self):
        cases = (
            ("Manifold Mouse", {"R": 1, "C": 3}, [0, 0, 0, 1, 0]),
            ("Pawpatch Recruit", {"G": 1, "C": 2}, [0, 0, 0, 0, 1]),
        )
        for index, (name, mana, printed_colors) in enumerate(cases):
            with self.subTest(card=name):
                game_state, handler, player, (card_id,) = self._prepare(
                    [name], mana, 39100 + index)
                tokens_before = set(player.get("tokens", []))

                valid = handler.generate_valid_actions()
                self.assertTrue(valid[20], "ordinary unpaid cast disappeared")
                self.assertTrue(valid[295], "paid Offspring cast was absent")
                paid_context = handler.action_reasons_with_context[295][
                    "context"]
                self.assertEqual(paid_context["card_id"], card_id)
                self.assertEqual(paid_context["hand_idx"], 0)
                self.assertIs(paid_context["pay_offspring"], True)
                self.assertEqual(paid_context["source_zone"], "hand")

                handler.current_valid_actions = valid
                result = handler.apply_action(295)
                self.assertFalse(
                    result[3].get("execution_failed", False), result[3])
                self.assertEqual(len(game_state.stack), 1)
                self.assertEqual(game_state.stack[-1][:2], ("SPELL", card_id))
                stack_context = game_state.stack[-1][3]
                self.assertTrue(stack_context.get("paid_offspring"))
                self.assertNotIn("pay_offspring", stack_context)

                self.assertTrue(game_state.resolve_top_of_stack())
                self.assertIn(card_id, player["battlefield"])
                registered = [
                    ability for ability
                    in game_state.ability_handler.registered_abilities.get(
                        card_id, [])
                    if getattr(ability, "_is_offspring_etb_trigger", False)]
                self.assertEqual(len(registered), 1)
                self.assertEqual(
                    len(game_state.ability_handler.active_triggers), 1)

                game_state.ability_handler.process_triggered_abilities()
                self.assertEqual(len(game_state.stack), 1)
                self.assertEqual(game_state.stack[-1][0], "TRIGGER")
                self.assertTrue(game_state.resolve_top_of_stack())

                created_ids = [
                    token_id for token_id in player.get("tokens", [])
                    if token_id not in tokens_before]
                self.assertEqual(len(created_ids), 1)
                original = game_state._safe_get_card(card_id)
                token = game_state._safe_get_card(created_ids[0])
                self.assertTrue(token.is_token)
                self.assertEqual(token.name, original.printed("name"))
                self.assertEqual((token.power, token.toughness), (1, 1))
                self.assertEqual(
                    token.card_types, original.printed("card_types"))
                self.assertEqual(token.subtypes, original.printed("subtypes"))
                self.assertEqual(original.printed("colors"), printed_colors)
                self.assertEqual(token.colors, printed_colors)

    def test_combined_affordability_hides_paid_but_keeps_unpaid_cast(self):
        # {G} and {2} are each independently payable from these same two mana
        # sources, but their combined {2}{G} casting cost is not.
        game_state, handler, player, (card_id,) = self._prepare(
            ["Pawpatch Recruit"], {"G": 1, "C": 1}, 39110)
        tokens_before = set(player.get("tokens", []))
        card = game_state._safe_get_card(card_id)
        self.assertTrue(handler._can_afford_card(player, card, context={}))
        self.assertTrue(handler._can_afford_cost_string(
            player, card.offspring_cost, context={"card": card}))

        valid = handler.generate_valid_actions()
        self.assertTrue(valid[20])
        self.assertFalse(valid[295])

        handler.current_valid_actions = valid
        result = handler.apply_action(20)
        self.assertFalse(result[3].get("execution_failed", False), result[3])
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(card_id, player["battlefield"])
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(game_state.stack, [])
        self.assertEqual(set(player.get("tokens", [])), tokens_before)

    def test_direct_handler_fails_closed_on_stale_or_unaffordable_context(self):
        game_state, handler, player, (card_id,) = self._prepare(
            ["Manifold Mouse"], {"R": 1, "C": 3}, 39120)
        valid = handler.generate_valid_actions()
        context = dict(
            handler.action_reasons_with_context[295]["context"])

        # The generated slot is part of the action contract. Moving the card
        # makes that context stale even if another Offspring card might exist.
        self.assertTrue(game_state.move_card(
            card_id, player, "hand", player, "library"))
        _, success = handler._handle_pay_offspring_cost(
            None, context=context)
        self.assertFalse(success)
        self.assertEqual(game_state.stack, [])

        game_state, handler, player, (card_id,) = self._prepare(
            ["Manifold Mouse"], {"R": 1, "C": 3}, 39121)
        handler.generate_valid_actions()
        context = dict(
            handler.action_reasons_with_context[295]["context"])
        for color in ("W", "U", "B", "R", "G", "C"):
            player["mana_pool"][color] = 0
        _, success = handler._handle_pay_offspring_cost(
            None, context=context)
        self.assertFalse(success)
        self.assertIn(card_id, player["hand"])
        self.assertEqual(game_state.stack, [])

    def test_multiple_paid_offspring_cards_use_overflow_catalog(self):
        game_state, handler, player, card_ids = self._prepare(
            ["Manifold Mouse", "Pawpatch Recruit"],
            {"R": 1, "G": 1, "C": 5}, 39130)
        first_id, second_id = card_ids
        valid = handler.generate_valid_actions()
        self.assertTrue(valid[295])
        direct_context = handler.action_reasons_with_context[295]["context"]
        self.assertEqual(direct_context["card_id"], first_id)
        self.assertEqual(direct_context["hand_idx"], 0)
        self.assertTrue(valid[479])

        options = handler.action_reasons_with_context[479]["context"][
            "options"]
        matching = [
            (index, entry) for index, entry in enumerate(options)
            if entry.get("action_index") == 295
            and entry.get("action_context", {}).get("card_id") == second_id]
        self.assertEqual(len(matching), 1)
        option_index, entry = matching[0]
        self.assertEqual(entry["action_context"]["hand_idx"], 1)
        self.assertIs(entry["action_context"]["pay_offspring"], True)
        self.assertLess(option_index, 10)

        handler.current_valid_actions = valid
        open_result = handler.apply_action(479)
        self.assertFalse(
            open_result[3].get("execution_failed", False), open_result[3])
        self.assertEqual(game_state.choice_context.get("type"),
                         "action_catalog")
        choose_mask = handler.generate_valid_actions()
        self.assertTrue(choose_mask[353 + option_index])
        handler.current_valid_actions = choose_mask
        choose_result = handler.apply_action(353 + option_index)
        self.assertFalse(
            choose_result[3].get("execution_failed", False),
            choose_result[3])
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][:2], ("SPELL", second_id))
        self.assertIn(first_id, player["hand"])


if __name__ == "__main__":
    unittest.main()
