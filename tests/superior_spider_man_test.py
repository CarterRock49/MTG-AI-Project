"""Focused regressions for Superior Spider-Man's Mind Swap ability."""

from __future__ import annotations

import copy
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
    replace_hand,
)
from Playersim.ability_types import BoundExileTriggeredAbility  # noqa: E402


def creature_data(name, *, artifact=False, power=2, toughness=2,
                  oracle_text=""):
    card_types = ["artifact", "creature"] if artifact else ["creature"]
    main_types = "Artifact Creature" if artifact else "Creature"
    return {
        "name": name,
        "mana_cost": "{2}{G}",
        "cmc": 3,
        "type_line": f"Legendary {main_types} - Elf Wizard",
        "card_types": card_types,
        "supertypes": ["legendary"],
        "subtypes": ["Elf", "Wizard"],
        "oracle_text": oracle_text,
        "power": power,
        "toughness": toughness,
        "colors": [0, 0, 0, 0, 1],
    }


class SuperiorSpiderManTest(unittest.TestCase):
    def _handler_for(self, game_state, controller):
        game_state.agent_is_p1 = controller is game_state.p1
        handler = get_env().action_handler
        handler.game_state = game_state
        return handler

    def _cast_to_mind_swap_choice(
            self, game_state, controller, *, mana_pool=None):
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        spider_id = inject_real_card(
            game_state, controller, "Superior Spider-Man", "hand")
        original_printed = copy.deepcopy(
            game_state._safe_get_card(spider_id)._printed)
        controller["mana_pool"] = dict(mana_pool or {
            "W": 0, "U": 3, "B": 3, "R": 0, "G": 0, "C": 10,
        })
        self.assertTrue(game_state.cast_spell(
            spider_id, controller, {
                "source_zone": "hand",
                "source_idx": controller["hand"].index(spider_id),
            }))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.phase, game_state.PHASE_CHOOSE)
        self.assertEqual(
            game_state.choice_context.get("choice_kind"),
            "superior_spider_copy")
        self.assertTrue(game_state.choice_context.get("optional"))
        return spider_id, original_printed

    def _apply_action(self, handler, action_index):
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[action_index], f"action {action_index} not valid")
        handler.current_valid_actions = mask
        result = handler.apply_action(action_index)
        self.assertFalse(result[3].get("execution_failed", False), result[3])
        return result

    def _choose_real_copy(
            self, game_state, controller, card_name, *, mana_pool=None):
        target_id = inject_real_card(
            game_state, game_state.p2, card_name, "graveyard")
        spider_id, _ = self._cast_to_mind_swap_choice(
            game_state, controller, mana_pool=mana_pool)
        options = list(game_state.choice_context["options"])
        self.assertIn(target_id, options)
        handler = self._handler_for(game_state, controller)
        self._apply_action(handler, 353 + options.index(target_id))
        spider = game_state._safe_get_card(spider_id)
        self.assertEqual(spider.name, "Superior Spider-Man")
        self.assertEqual(
            game_state.copy_overrides[spider_id]["copied_from"], target_id)
        return spider_id, target_id, handler

    def test_decline_enters_as_the_printed_card(self):
        game_state = fresh(12601)
        controller = game_state.p1
        inject_into_zone(
            game_state, game_state.p2,
            creature_data("Graveyard Visionary", oracle_text="Flying"),
            "graveyard")
        spider_id, original = self._cast_to_mind_swap_choice(
            game_state, controller)
        handler = self._handler_for(game_state, controller)

        self._apply_action(handler, 11)

        spider = game_state._safe_get_card(spider_id)
        self.assertIn(spider_id, controller["battlefield"])
        self.assertEqual(spider._printed, original)
        self.assertNotIn(spider_id, game_state.copy_overrides)
        self.assertFalse(any(
            isinstance(entry[0], BoundExileTriggeredAbility)
            for entry in game_state.ability_handler.active_triggers))

    def test_copy_exceptions_and_bound_reflexive_exile(self):
        game_state = fresh(12602)
        controller = game_state.p1
        target_id = inject_into_zone(
            game_state, game_state.p2,
            creature_data(
                "Graveyard Visionary", artifact=True, power=7, toughness=8,
                oracle_text="Flying"),
            "graveyard")
        noncreature_id = inject_into_zone(game_state, controller, {
            "name": "Buried Lesson", "mana_cost": "{1}{U}", "cmc": 2,
            "type_line": "Sorcery", "card_types": ["sorcery"],
            "oracle_text": "Draw a card.", "colors": [0, 1, 0, 0, 0],
        }, "graveyard")
        spider_id, _ = self._cast_to_mind_swap_choice(
            game_state, controller)
        options = list(game_state.choice_context["options"])
        self.assertIn(target_id, options)
        self.assertNotIn(noncreature_id, options)
        handler = self._handler_for(game_state, controller)

        self._apply_action(handler, 353 + options.index(target_id))

        spider = game_state._safe_get_card(spider_id)
        self.assertIn(spider_id, controller["battlefield"])
        self.assertIn(target_id, game_state.p2["graveyard"])
        self.assertEqual(spider.name, "Superior Spider-Man")
        self.assertEqual((spider.power, spider.toughness), (4, 4))
        self.assertEqual(set(spider.card_types), {"artifact", "creature"})
        self.assertTrue(
            {"elf", "wizard", "spider", "human", "hero"}
            .issubset({subtype.lower() for subtype in spider.subtypes}),
            spider.subtypes)
        self.assertEqual(spider.oracle_text, "Flying")
        queued = [
            item for item in game_state.stack
            if isinstance(item[3].get("ability"),
                          BoundExileTriggeredAbility)]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][3]["ability"].bound_card_id, target_id)

        cloned_state = game_state.clone()
        self.assertIsNotNone(cloned_state)
        cloned_trigger = cloned_state.stack[-1][3]["ability"]
        self.assertEqual(
            cloned_trigger.bound_zone_generation,
            getattr(cloned_state._safe_get_card(target_id),
                    "_zone_change_generation", None))
        self.assertTrue(cloned_state.resolve_top_of_stack())
        self.assertIn(target_id, cloned_state.p2["exile"])

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(target_id, game_state.p2["exile"])

    def test_bound_exile_noops_after_move_and_copy_identity_restores(self):
        game_state = fresh(12603)
        controller = game_state.p1
        opponent = game_state.p2
        target_id = inject_into_zone(
            game_state, opponent,
            creature_data("Escaping Archivist", artifact=True,
                          oracle_text="Vigilance"),
            "graveyard")
        spider_id, original = self._cast_to_mind_swap_choice(
            game_state, controller)
        handler = self._handler_for(game_state, controller)
        options = list(game_state.choice_context["options"])
        self._apply_action(handler, 353 + options.index(target_id))

        self.assertTrue(game_state.stack)
        cloned_state = game_state.clone()
        self.assertIsNotNone(cloned_state)
        self.assertTrue(cloned_state.move_card(
            target_id, cloned_state.p2, "graveyard", cloned_state.p2, "hand",
            cause="response"))
        self.assertTrue(cloned_state.move_card(
            target_id, cloned_state.p2, "hand", cloned_state.p2, "graveyard",
            cause="response"))
        self.assertTrue(cloned_state.resolve_top_of_stack())
        self.assertIn(target_id, cloned_state.p2["graveyard"])
        self.assertNotIn(target_id, cloned_state.p2["exile"])

        self.assertTrue(game_state.move_card(
            target_id, opponent, "graveyard", opponent, "hand",
            cause="response"))
        self.assertTrue(game_state.move_card(
            target_id, opponent, "hand", opponent, "graveyard",
            cause="response"))
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(target_id, opponent["graveyard"])
        self.assertNotIn(target_id, opponent["exile"])

        self.assertTrue(game_state.move_card(
            spider_id, controller, "battlefield", controller, "hand"))
        spider = game_state._safe_get_card(spider_id)
        self.assertEqual(spider._printed, original)
        self.assertNotIn("artifact", spider.card_types)
        self.assertNotEqual(spider.oracle_text, "Vigilance")
        self.assertNotIn(spider_id, game_state.copy_overrides)

    def test_choice_paginates_across_both_graveyards(self):
        game_state = fresh(12604)
        controller = game_state.p1
        injected = []
        for index in range(12):
            owner = controller if index < 7 else game_state.p2
            injected.append(inject_into_zone(
                game_state, owner,
                creature_data(f"Mind Swap Option {index}"), "graveyard"))
        spider_id, _ = self._cast_to_mind_swap_choice(
            game_state, controller)
        self.assertEqual(game_state.choice_context["options"], injected)
        handler = self._handler_for(game_state, controller)

        self._apply_action(handler, 479)
        self.assertEqual(game_state.choice_context.get("choice_page"), 1)
        second_page = handler.generate_valid_actions()
        self.assertTrue(second_page[353])
        self.assertTrue(second_page[354])
        self.assertFalse(second_page[355])
        handler.current_valid_actions = second_page
        result = handler.apply_action(354)
        self.assertFalse(result[3].get("execution_failed", False), result[3])

        self.assertIn(spider_id, controller["battlefield"])
        self.assertEqual(
            game_state.copy_overrides[spider_id]["copied_from"],
            injected[11])

    def test_copied_colorstorm_opus_resolves_under_retained_spider_name(self):
        game_state = fresh(12605)
        controller = game_state.p1
        fidelity_before = copy.deepcopy(game_state.fidelity_counters)
        spider_id, target_id, _ = self._choose_real_copy(
            game_state, controller, "Colorstorm Stallion")

        self.assertEqual(len(game_state.stack), 1)
        self.assertIsInstance(
            game_state.stack[-1][3].get("ability"),
            BoundExileTriggeredAbility)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(target_id, game_state.p2["exile"])

        instant_id = inject_into_zone(game_state, controller, {
            "name": "Five-Mana Opus Probe",
            "mana_cost": "{3}{U}{R}",
            "cmc": 5,
            "type_line": "Instant",
            "card_types": ["instant"],
            "oracle_text": "",
            "colors": [0, 1, 0, 1, 0],
        }, "hand")
        tokens_before = set(controller.get("tokens", []))
        self.assertTrue(game_state.trigger_ability(
            instant_id, "CAST_SPELL", {
                "cast_card_id": instant_id,
                "casting_player": controller,
                "final_paid_details": {
                    "spent_specific": {"U": 3, "R": 2}},
            }))
        queued = game_state.ability_handler.active_triggers
        self.assertEqual(len(queued), 1, queued)
        self.assertEqual(queued[0][0].card_id, spider_id)
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertTrue(game_state.resolve_top_of_stack())
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()

        spider = game_state._safe_get_card(spider_id)
        self.assertEqual((spider.power, spider.toughness), (5, 5))
        new_tokens = set(controller.get("tokens", [])) - tokens_before
        self.assertEqual(len(new_tokens), 1)
        token_id = new_tokens.pop()
        token = game_state._safe_get_card(token_id)
        self.assertIn(token_id, controller["battlefield"])
        self.assertEqual(token.name, "Superior Spider-Man")
        self.assertEqual((token.power, token.toughness), (4, 4))
        self.assertEqual(game_state.fidelity_counters, fidelity_before)

    def test_copied_deceit_black_etb_resolves_under_retained_spider_name(self):
        game_state = fresh(12606)
        controller = game_state.p1
        opponent = game_state.p2
        creature_id, land_id, instant_id = replace_hand(
            game_state, opponent, [
                creature_data("Mind Swap Hand Creature"),
                {
                    "name": "Mind Swap Hand Land",
                    "mana_cost": "",
                    "cmc": 0,
                    "type_line": "Land",
                    "card_types": ["land"],
                    "oracle_text": "",
                    "colors": [0, 0, 0, 0, 0],
                },
                {
                    "name": "Mind Swap Hand Instant",
                    "mana_cost": "{1}{U}",
                    "cmc": 2,
                    "type_line": "Instant",
                    "card_types": ["instant"],
                    "oracle_text": "Draw a card.",
                    "colors": [0, 1, 0, 0, 0],
                },
            ])
        fidelity_before = copy.deepcopy(game_state.fidelity_counters)
        spider_id, target_id, handler = self._choose_real_copy(
            game_state, controller, "Deceit", mana_pool={
                "W": 0, "U": 1, "B": 3, "R": 0, "G": 0, "C": 0,
            })

        self.assertEqual(
            game_state.choice_context.get("type"), "order_triggers")
        ordered = game_state.choice_context["pending"]
        bound_index = next(
            index for index, entry in enumerate(ordered)
            if isinstance(entry[0], BoundExileTriggeredAbility))
        self.assertTrue(
            game_state.ability_handler.order_trigger_chosen(bound_index))

        self.assertEqual(game_state.phase, game_state.PHASE_TARGETING)
        candidates = handler._get_target_selection_candidates(
            controller, game_state.targeting_context)
        self.assertEqual(candidates, ["p2"])
        _, selected = handler._handle_select_target(0, {})
        self.assertTrue(selected)
        self.assertTrue(game_state.resolve_top_of_stack())

        choice = game_state.choice_context
        self.assertIsNotNone(choice)
        self.assertEqual(choice["type"], "hand_selection")
        self.assertIn(creature_id, choice["options"])
        self.assertIn(instant_id, choice["options"])
        self.assertNotIn(land_id, choice["options"])
        _, chosen = handler._handle_choose_mode(
            choice["options"].index(creature_id), {})
        self.assertTrue(chosen)
        self.assertIn(creature_id, opponent["graveyard"])
        self.assertNotIn(creature_id, opponent["hand"])

        self.assertEqual(len(game_state.stack), 1)
        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertIn(target_id, opponent["exile"])
        self.assertIn(spider_id, controller["battlefield"])
        self.assertEqual(game_state.fidelity_counters, fidelity_before)


if __name__ == "__main__":
    unittest.main()
