"""Regressions for colliding actions routed through the overflow catalog.

Run from the repository root with::

    python tests/action_catalog_test.py
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.ability_types import DamageEffect, PreventDamageEffect  # noqa: E402
from Playersim.ability_utils import (  # noqa: E402
    EffectFactory,
    has_damage_prevention_instruction,
)
from Playersim.actions import ActionHandler  # noqa: E402
from Playersim.card import Card  # noqa: E402
from Playersim.game_state import GameState  # noqa: E402


logging.disable(logging.CRITICAL)


def _spell(name, type_line, mana_cost, oracle_text):
    return Card({
        "name": name,
        "type_line": type_line,
        "mana_cost": mana_cost,
        "cmc": 1,
        "oracle_text": oracle_text,
        "color_identity": ["W"] if "W" in mana_cost else ["R"],
    })


class OverflowActionCatalogTest(unittest.TestCase):
    def test_unpreventable_text_is_not_inverted_into_prevention(self):
        text = (
            "Damage can't be prevented this turn. "
            "Impractical Joke deals 3 damage to up to one target creature "
            "or planeswalker.")
        self.assertFalse(has_damage_prevention_instruction(text))
        self.assertFalse(has_damage_prevention_instruction(
            "Damage can’t be prevented this turn."))
        self.assertTrue(has_damage_prevention_instruction(
            "Prevent all combat damage that would be dealt this turn."))

        effects = EffectFactory.create_effects(
            text, source_name="Impractical Joke")
        self.assertTrue(any(isinstance(effect, DamageEffect)
                            for effect in effects))
        self.assertFalse(any(isinstance(effect, PreventDamageEffect)
                             for effect in effects))

    def _response_state(self):
        prevention = _spell(
            "Actual Prevention", "Instant", "{W}",
            "Prevent all damage that would be dealt to you this turn.")
        joke = _spell(
            "Impractical Joke", "Sorcery", "{R}",
            "Damage can't be prevented this turn. Impractical Joke deals "
            "3 damage to up to one target creature or planeswalker.")
        damage = _spell(
            "Incoming Bolt", "Instant", "{R}",
            "Incoming Bolt deals 3 damage to any target.")
        game_state = GameState({0: prevention, 1: joke, 2: damage})
        game_state.reset([2], [0, 0, 1, 1], seed=13)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = None
        game_state.agent_is_p1 = False
        game_state.priority_player = game_state.p2
        game_state.priority_pass_count = 0
        game_state.p2["mana_pool"]["W"] = 2
        game_state.p2["mana_pool"]["R"] = 2

        damage_id = next(
            card_id for card_id in game_state.p1["hand"]
            if game_state._safe_get_card(card_id).name == "Incoming Bolt")
        game_state.stack = [
            ("SPELL", damage_id, game_state.p1, {"source_zone": "hand"})
        ]
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler

    def test_overflow_catalog_deduplicates_and_dispatches_castable_response(self):
        game_state, handler = self._response_state()
        valid = handler.generate_valid_actions()

        self.assertTrue(valid[432])
        self.assertTrue(valid[479])
        catalog_context = handler.action_reasons_with_context[479]["context"]
        entries = [
            entry for entry in catalog_context["options"]
            if entry.get("action_index") == 432
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"],
                         "PREVENT_DAMAGE with Actual Prevention")
        self.assertNotIn(
            "Impractical Joke",
            [entry.get("label") for entry in catalog_context["options"]])

        handler.current_valid_actions = valid
        _, _, _, open_info = handler.apply_action(479)
        self.assertFalse(open_info.get("execution_failed", False))
        self.assertEqual(game_state.choice_context.get("type"),
                         "action_catalog")

        choose_mask = handler.generate_valid_actions()
        self.assertTrue(choose_mask[353])
        handler.current_valid_actions = choose_mask
        before = sum(
            game_state._safe_get_card(card_id).name == "Actual Prevention"
            for card_id in game_state.p2["hand"])
        _, _, _, choose_info = handler.apply_action(353)
        self.assertFalse(choose_info.get("execution_failed", False), choose_info)
        after = sum(
            game_state._safe_get_card(card_id).name == "Actual Prevention"
            for card_id in game_state.p2["hand"])
        self.assertEqual((before, after), (2, 1))

    def _granted_flashback_spree_state(self, filler_count=0):
        """Three Steps Ahead in the graveyard with granted Flashback.

        ``filler_count`` pads the graveyard so the Spree card can sit past
        the six fixed PLAY_FROM_GRAVEYARD slots and route through the
        overflow catalog instead.
        """
        three_steps = _spell(
            "Three Steps Ahead", "Instant", "{U}",
            "Spree (Choose one or more additional costs.)\n"
            "+ {1}{U} — Counter target spell.\n"
            "+ {3} — Create a token that's a copy of target artifact or "
            "creature you control.\n"
            "+ {2} — Draw two cards, then discard a card.")
        incoming = _spell(
            "Incoming Spell", "Instant", "{R}",
            "Incoming Spell deals 2 damage to any target.")
        filler = _spell(
            "Filler Ritual", "Sorcery", "{R}",
            "You gain 1 life.")
        game_state = GameState({0: three_steps, 1: incoming, 2: filler})
        game_state.reset([1], [0] + [2] * filler_count, seed=17)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = None
        game_state.agent_is_p1 = False
        game_state.priority_player = game_state.p2
        game_state.priority_pass_count = 0

        incoming_id = game_state.p1["hand"][0]
        game_state.stack = [
            ("SPELL", incoming_id, game_state.p1, {"source_zone": "hand"})
        ]
        spree_id = next(
            card_id for card_id in game_state.p2["hand"]
            if game_state._safe_get_card(card_id).name == "Three Steps Ahead")
        filler_ids = [
            card_id for card_id in game_state.p2["hand"]
            if game_state._safe_get_card(card_id).name == "Filler Ritual"]
        for card_id in filler_ids + [spree_id]:
            game_state.p2["hand"].remove(card_id)
            game_state.p2["graveyard"].append(card_id)
        self.assertTrue(game_state.grant_flashback_permission(
            game_state.p2, spree_id))
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        return game_state, handler, spree_id

    def test_granted_flashback_spree_requires_affordable_mode(self):
        game_state, handler, spree_id = self._granted_flashback_spree_state()
        action_index = 472 + game_state.p2["graveyard"].index(spree_id)

        # Flashback cost ({U}) alone is payable, but no mode cost is.
        game_state.p2["mana_pool"]["U"] = 1
        valid = handler.generate_valid_actions()
        self.assertFalse(
            valid[action_index],
            "Mask offered a Spree flashback cast with no affordable mode")

        game_state.p2["mana_pool"]["U"] = 6
        valid = handler.generate_valid_actions()
        self.assertTrue(valid[action_index])
        handler.current_valid_actions = valid
        _, _, _, info = handler.apply_action(action_index)
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertEqual(game_state.choice_context.get("type"), "choose_mode")
        self.assertTrue(game_state.choice_context.get("is_spree"))

        # Mode selectability must price against the flashback base cost:
        # counter ({1}{U}) and draw ({2}) are payable, the copy mode has no
        # legal target on an empty battlefield.
        choose_mask = handler.generate_valid_actions()
        self.assertTrue(choose_mask[353])
        self.assertFalse(choose_mask[354])
        self.assertTrue(choose_mask[355])
        handler.current_valid_actions = choose_mask
        _, _, _, mode_info = handler.apply_action(355)
        self.assertFalse(mode_info.get("execution_failed", False), mode_info)

    def test_overflow_catalog_gates_unaffordable_spree_flashback(self):
        game_state, handler, spree_id = self._granted_flashback_spree_state(
            filler_count=6)
        self.assertEqual(game_state.p2["graveyard"].index(spree_id), 6)
        label = "Play from graveyard: Three Steps Ahead"

        game_state.p2["mana_pool"]["U"] = 1
        handler.generate_valid_actions()
        options = handler.action_reasons_with_context.get(
            479, {}).get("context", {}).get("options", [])
        self.assertNotIn(label, [entry.get("label") for entry in options],
                         "Catalog offered a Spree flashback cast with no "
                         "affordable mode")

        game_state.p2["mana_pool"]["U"] = 6
        valid = handler.generate_valid_actions()
        options = handler.action_reasons_with_context.get(
            479, {}).get("context", {}).get("options", [])
        self.assertIn(label, [entry.get("label") for entry in options])

        # Replay the failing sequence from the 2026-07-13 run: open the
        # catalog (479), then pick the graveyard cast (353).
        self.assertTrue(valid[479])
        handler.current_valid_actions = valid
        _, _, _, open_info = handler.apply_action(479)
        self.assertFalse(open_info.get("execution_failed", False), open_info)
        self.assertEqual(game_state.choice_context.get("type"),
                         "action_catalog")
        choose_mask = handler.generate_valid_actions()
        self.assertTrue(choose_mask[353])
        handler.current_valid_actions = choose_mask
        _, _, _, choose_info = handler.apply_action(353)
        self.assertFalse(choose_info.get("execution_failed", False),
                         choose_info)
        self.assertEqual(game_state.choice_context.get("type"), "choose_mode")
        self.assertTrue(game_state.choice_context.get("is_spree"))

    def test_spree_counter_uses_mode_announcement_not_counter_shortcut(self):
        three_steps = _spell(
            "Three Steps Ahead", "Instant", "{U}",
            "Spree (Choose one or more additional costs.)\n"
            "+ {1}{U} — Counter target spell.\n"
            "+ {3} — Create a token that's a copy of target artifact or "
            "creature you control.\n"
            "+ {2} — Draw two cards, then discard a card.")
        incoming = _spell(
            "Incoming Spell", "Instant", "{R}",
            "Incoming Spell deals 2 damage to any target.")
        game_state = GameState({0: three_steps, 1: incoming})
        game_state.reset([1], [0], seed=17)
        game_state.mulligan_in_progress = False
        game_state.bottoming_in_progress = False
        game_state.phase = game_state.PHASE_PRIORITY
        game_state.previous_priority_phase = None
        game_state.agent_is_p1 = False
        game_state.priority_player = game_state.p2
        game_state.priority_pass_count = 0
        game_state.p2["mana_pool"]["U"] = 6

        incoming_id = game_state.p1["hand"][0]
        game_state.stack = [
            ("SPELL", incoming_id, game_state.p1,
             {"source_zone": "hand"})
        ]
        handler = ActionHandler(game_state)
        game_state.action_handler = handler
        spree_id = game_state.p2["hand"][0]
        spree_card = game_state._safe_get_card(spree_id)
        self.assertTrue(spree_card.is_spree)
        self.assertEqual(len(spree_card.spree_modes), 3)

        valid = handler.generate_valid_actions()
        self.assertFalse(valid[430],
                         "Spree counter bypassed mode announcement")
        catalog = handler.action_reasons_with_context.get(
            479, {}).get("context", {}).get("options", [])
        self.assertFalse(any(
            entry.get("action_index") == 430
            and "Three Steps Ahead" in entry.get("label", "")
            for entry in catalog))

        ordinary_actions = [
            action_index
            for action_index, reason in
            handler.action_reasons_with_context.items()
            if valid[action_index]
            and reason.get("reason", "").startswith("PLAY_SPELL")
            and "Three Steps Ahead" in reason.get("reason", "")
        ]
        self.assertEqual(len(ordinary_actions), 1)
        handler.current_valid_actions = valid
        _, _, _, info = handler.apply_action(ordinary_actions[0])
        self.assertFalse(info.get("execution_failed", False), info)
        self.assertEqual(game_state.choice_context.get("type"), "choose_mode")
        self.assertTrue(game_state.choice_context.get("is_spree"))


if __name__ == "__main__":
    unittest.main()
