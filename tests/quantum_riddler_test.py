"""Focused regressions for Quantum Riddler's draw replacement."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from scenario_test import fresh, inject_real_card  # noqa: E402
from Playersim.ability_types import DrawCardEffect  # noqa: E402


class QuantumRiddlerDrawTest(unittest.TestCase):
    @staticmethod
    def _set_hand_size(game_state, player, size):
        while len(player["hand"]) > size:
            player["library"].append(player["hand"].pop())
        while len(player["hand"]) < size:
            player["hand"].append(player["library"].pop())
        player["attempted_draw_from_empty"] = False

    def _state_with_riddler(self, seed):
        game_state = fresh(seed)
        player = game_state.p1
        riddler = inject_real_card(
            game_state, player, "Quantum Riddler", "battlefield")
        effects = [
            effect for effect in game_state.replacement_effects.active_effects
            if effect.get("source_id") == riddler]
        self.assertEqual(
            [(effect.get("event_type"),
              bool(effect.get("draw_batch_modifier")))
             for effect in effects],
            [("DRAW", True)])
        return game_state, player, riddler

    def test_low_hand_draw_batch_gets_exactly_one_additional_card(self):
        game_state, player, _ = self._state_with_riddler(1201)
        self._set_hand_size(game_state, player, 1)
        hand_before = len(player["hand"])
        library_before = list(player["library"])
        history_before = len(game_state.draw_history.get("p1", {}).get(
            game_state.turn, []))

        self.assertTrue(DrawCardEffect(3).apply(
            game_state, None, player, {}))

        self.assertEqual(len(player["hand"]), hand_before + 4)
        self.assertEqual(len(player["library"]), len(library_before) - 4)
        self.assertEqual(player["hand"][-4:], library_before[:4])
        self.assertEqual(
            len(game_state.draw_history["p1"][game_state.turn]),
            history_before + 4)

    def test_bonus_condition_and_affected_player_are_scoped(self):
        game_state, player, _ = self._state_with_riddler(1202)
        opponent = game_state.p2
        self._set_hand_size(game_state, player, 2)
        self._set_hand_size(game_state, opponent, 1)

        player_before = len(player["hand"])
        opponent_before = len(opponent["hand"])
        self.assertTrue(DrawCardEffect(2).apply(
            game_state, None, player, {}))
        self.assertTrue(DrawCardEffect(2).apply(
            game_state, None, opponent, {}))

        self.assertEqual(len(player["hand"]), player_before + 2)
        self.assertEqual(len(opponent["hand"]), opponent_before + 2)

    def test_single_draw_uses_batch_replacement_and_returns_first_card(self):
        game_state, player, _ = self._state_with_riddler(1203)
        self._set_hand_size(game_state, player, 0)
        expected = player["library"][:2]

        returned = game_state._draw_card(player)

        self.assertEqual(returned, expected[0])
        self.assertEqual(player["hand"][-2:], expected)

    def test_riddler_replaces_its_own_etb_draw(self):
        game_state = fresh(1204)
        player = game_state.p1
        self._set_hand_size(game_state, player, 0)
        hand_before = len(player["hand"])
        riddler = inject_real_card(
            game_state, player, "Quantum Riddler", "battlefield")
        queued = [
            entry for entry in game_state.ability_handler.active_triggers
            if getattr(entry[0], "card_id", None) == riddler]
        self.assertEqual(len(queued), 1)

        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.stack)
        self.assertTrue(game_state.resolve_top_of_stack())

        self.assertEqual(len(player["hand"]), hand_before + 2)

    def test_leaving_battlefield_removes_draw_bonus(self):
        game_state, player, riddler = self._state_with_riddler(1205)
        self.assertTrue(game_state.move_card(
            riddler, player, "battlefield", player, "graveyard"))
        self._set_hand_size(game_state, player, 1)
        hand_before = len(player["hand"])

        self.assertTrue(DrawCardEffect(2).apply(
            game_state, None, player, {}))

        self.assertEqual(len(player["hand"]), hand_before + 2)
        self.assertFalse(any(
            effect.get("source_id") == riddler
            for effect in game_state.replacement_effects.active_effects))


if __name__ == "__main__":
    unittest.main()
