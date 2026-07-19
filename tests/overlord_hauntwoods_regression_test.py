"""Exact lifecycle regressions for Overlord of the Hauntwoods."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_real_card  # noqa: E402


class OverlordHauntwoodsRegressionTest(unittest.TestCase):
    BASIC_TYPES = {"plains", "island", "swamp", "mountain", "forest"}

    def _state(self, seed: int):
        game_state = fresh(seed)
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
        game_state.current_attackers = []
        game_state.current_block_assignments = {}
        game_state.attackers_this_turn = set()
        game_state.ability_handler.active_triggers = []
        for player in (controller, opponent):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tokens"] = []
            player["tapped_permanents"] = set()
            player["entered_battlefield_this_turn"] = set()
            player["damage_counters"] = {}
            player["mana_pool"] = {
                "W": 0, "U": 0, "B": 0,
                "R": 0, "G": 0, "C": 0,
            }
        game_state._last_card_locations = {}
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _public(self, handler, action: int, message: str):
        game_state = handler.game_state
        priority = game_state.priority_player or game_state.p1
        game_state.priority_player = priority
        game_state.agent_is_p1 = priority is game_state.p1
        mask = handler.generate_valid_actions()
        self.assertTrue(
            mask[action],
            f"{message}: action {action} absent; valid="
            f"{[i for i, allowed in enumerate(mask) if allowed]}",
        )
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(action)
        self.assertFalse(done, (message, info))
        self.assertFalse(truncated, (message, info))
        self.assertFalse(info.get("execution_failed"), (message, info))
        self.assertFalse(info.get("invalid_action"), (message, info))

    def _everywhere_tokens(self, game_state, controller):
        return [
            permanent_id
            for permanent_id in controller["battlefield"]
            if getattr(
                game_state._safe_get_card(permanent_id), "name", None
            ) == "Everywhere"
        ]

    def _assert_exact_everywhere(self, game_state, controller, count: int):
        token_ids = self._everywhere_tokens(game_state, controller)
        self.assertEqual(len(token_ids), count)
        for token_id in token_ids:
            token = game_state._safe_get_card(token_id)
            self.assertTrue(token.is_token)
            self.assertEqual(token.name, "Everywhere")
            self.assertEqual(set(token.card_types), {"land"})
            self.assertNotIn("creature", token.card_types)
            self.assertEqual(set(token.subtypes), self.BASIC_TYPES)
            self.assertEqual(len(token.subtypes), 5)
            self.assertEqual(token.supertypes, [])
            self.assertEqual((token.power, token.toughness), (0, 0))
            self.assertEqual(list(token.colors), [0, 0, 0, 0, 0])
            self.assertIn(token_id, controller["tapped_permanents"])
            self.assertFalse(game_state._is_creature(token_id))
            options = game_state.mana_system._land_mana_options(
                controller, token)
            self.assertEqual(
                {option["symbol"] for option in options}, set("WUBRG"))

    def test_real_entry_event_stacks_then_resolves_exact_everywhere(self):
        game_state, _, controller, _ = self._state(33001)
        overlord_id = inject_real_card(
            game_state, controller, "Overlord of the Hauntwoods", "hand")

        self.assertTrue(game_state.move_card(
            overlord_id, controller, "hand", controller, "battlefield"))
        self.assertEqual(len(game_state.ability_handler.active_triggers), 1)
        self.assertEqual(game_state.stack, [])
        self._assert_exact_everywhere(game_state, controller, 0)

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0], "TRIGGER")
        self.assertEqual(game_state.stack[-1][1], overlord_id)
        self._assert_exact_everywhere(game_state, controller, 0)

        self.assertTrue(game_state.resolve_top_of_stack())
        self.assertEqual(game_state.stack, [])
        self._assert_exact_everywhere(game_state, controller, 1)

    def test_real_public_attack_stacks_then_resolves_exact_everywhere(self):
        game_state, handler, controller, _ = self._state(33002)
        overlord_id = inject_real_card(
            game_state, controller, "Overlord of the Hauntwoods",
            "battlefield")
        # Isolate the attack half of the printed composite trigger. The entry
        # half is exercised independently above.
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        controller["entered_battlefield_this_turn"].discard(overlord_id)

        game_state.phase = game_state.PHASE_DECLARE_ATTACKERS
        game_state.priority_player = controller
        attack_action = 28 + controller["battlefield"].index(overlord_id)
        self._public(handler, attack_action, "declare Overlord as attacker")
        self.assertEqual(game_state.ability_handler.active_triggers, [])
        self.assertEqual(game_state.stack, [])

        self._public(handler, 438, "finish declaring attackers")
        self.assertEqual(game_state.stack[-1][0], "TRIGGER")
        self.assertEqual(game_state.stack[-1][1], overlord_id)
        self._assert_exact_everywhere(game_state, controller, 0)

        self._public(handler, 11, "active player passes Overlord trigger")
        self._public(handler, 11, "nonactive player passes Overlord trigger")
        self.assertEqual(game_state.stack, [])
        self._assert_exact_everywhere(game_state, controller, 1)


if __name__ == "__main__":
    unittest.main()
