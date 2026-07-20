"""Victor, Valgavoth's Seneschal resolution-ladder regressions.

The July 19 room-exhaust probe failed Victor's Eerie trigger: the printed
"first/second/third time this ability has resolved this turn" ladder was
parsed as three unconditional effects, and the third-time reanimation was an
unimplemented generic AbilityEffect. These scenarios pin the ladder through
the production trigger pipeline: exactly one arm runs per resolution, keyed
to this turn's per-ability resolution count, and the reanimation is a
resolution choice spanning both graveyards.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_into_zone,
    inject_real_card,
)


class VictorResolutionLadderTest(unittest.TestCase):
    VICTOR = "Victor, Valgavoth's Seneschal"

    def _state(self, seed: int):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        game_state.agent_is_p1 = True
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.priority_player = controller
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.sacrifice_context = None
        game_state.ability_handler.active_triggers = []
        game_state.ability_resolutions_this_turn = {}
        for player in (controller, opponent):
            player["hand"] = []
            player["library"] = []
            player["battlefield"] = []
            player["graveyard"] = []
            player["exile"] = []
            player["tapped_permanents"] = set()
            player["damage_counters"] = {}
            player["mana_pool"] = {
                color: 0 for color in ("W", "U", "B", "R", "G", "C")
            }
        handler = get_env().action_handler
        handler.current_valid_actions = None
        return game_state, handler, controller, opponent

    def _public(self, handler, action: int, label: str):
        game_state = handler.game_state
        game_state.agent_is_p1 = (
            game_state.priority_player is game_state.p1)
        mask = handler.generate_valid_actions()
        self.assertTrue(mask[action], (label, [
            index for index, allowed in enumerate(mask) if allowed]))
        handler.current_valid_actions = mask
        _, done, truncated, info = handler.apply_action(action)
        self.assertFalse(done, label)
        self.assertFalse(truncated, label)
        self.assertFalse(info.get("execution_failed"), (label, info))
        self.assertFalse(info.get("critical_error"), (label, info))
        return info

    def _synthetic(self, game_state, player, name, type_line, zone,
                   extra=None):
        data = {
            "name": name,
            "mana_cost": "{1}",
            "cmc": 1,
            "type_line": type_line,
            "oracle_text": "",
            "keywords": [],
            "color_identity": [],
        }
        if "Creature" in type_line:
            data.update({"power": "2", "toughness": "2"})
        data.update(extra or {})
        return inject_into_zone(game_state, player, data, zone)

    def _fire_eerie_once(self, handler, game_state, label: str):
        """Enter an enchantment, then resolve the queued Eerie trigger."""
        entered = self._synthetic(
            game_state, game_state.p1, f"{label} Enchantment",
            "Enchantment", "battlefield")
        if game_state.ability_handler.active_triggers:
            game_state.ability_handler.process_triggered_abilities()
        guard = 0
        while game_state.stack:
            self._public(handler, 11, f"{label}: controller passes")
            self._public(handler, 11, f"{label}: opponent passes")
            guard += 1
            self.assertLess(guard, 8, f"{label}: stack did not empty")
        return entered

    def _complete_surveil(self, handler, game_state, label: str):
        guard = 0
        while (game_state.choice_context
               and game_state.choice_context.get("type") == "surveil"):
            self._public(handler, 306, f"{label}: surveil keeps on top")
            guard += 1
            self.assertLess(guard, 8, f"{label}: surveil did not finish")

    def _complete_discard(self, handler, game_state, label: str):
        """The discarding player picks a card through the public mask."""
        guard = 0
        while (game_state.choice_context
               and game_state.choice_context.get("type") == "discard"):
            game_state.agent_is_p1 = (
                game_state.priority_player is game_state.p1)
            mask = handler.generate_valid_actions()
            valid = [
                index for index, allowed in enumerate(mask) if allowed]
            self.assertTrue(valid, f"{label}: no valid discard action")
            handler.current_valid_actions = mask
            _, done, truncated, info = handler.apply_action(valid[0])
            self.assertFalse(done, label)
            self.assertFalse(truncated, label)
            self.assertFalse(info.get("execution_failed"), (label, info))
            guard += 1
            self.assertLess(guard, 8, f"{label}: discard did not finish")

    def test_each_resolution_runs_exactly_its_ladder_arm(self):
        game_state, handler, controller, opponent = self._state(49101)
        inject_real_card(game_state, controller, self.VICTOR, "battlefield")
        own_creature = self._synthetic(
            game_state, controller, "Victor Own Grave Creature",
            "Creature - Zombie", "graveyard")
        self._synthetic(
            game_state, controller, "Victor Grave Sorcery",
            "Sorcery", "graveyard")
        opposing_creature = self._synthetic(
            game_state, opponent, "Victor Opposing Grave Creature",
            "Creature - Spider", "graveyard")
        opponent_hand_card = self._synthetic(
            game_state, opponent, "Victor Opponent Hand Card",
            "Sorcery", "hand")
        for index in range(4):
            self._synthetic(
                game_state, controller, f"Victor Library Card {index}",
                "Sorcery", "library")
        game_state.ability_handler.active_triggers = []
        library_before = len(controller["library"])

        with self.assertNoLogs(level=logging.WARNING):
            # First resolution: surveil 2, nothing else.
            self._fire_eerie_once(handler, game_state, "first Eerie")
            surveil = game_state.choice_context
            self.assertIsNotNone(
                surveil, "first resolution opened no surveil choice")
            self.assertEqual(surveil.get("type"), "surveil")
            self._complete_surveil(handler, game_state, "first Eerie")
            self.assertEqual(len(controller["library"]), library_before)
            self.assertIn(opponent_hand_card, opponent["hand"])

            # Second resolution: the opponent discards, no surveil.
            self._fire_eerie_once(handler, game_state, "second Eerie")
            discard = game_state.choice_context
            self.assertIsNotNone(
                discard, "second resolution opened no discard choice")
            self.assertEqual(discard.get("type"), "discard")
            self.assertIs(discard.get("player"), opponent)
            self._complete_discard(handler, game_state, "second Eerie")
            self.assertNotIn(opponent_hand_card, opponent["hand"])
            self.assertIn(opponent_hand_card, opponent["graveyard"])

            # Third resolution: choose a creature card from either graveyard.
            self._fire_eerie_once(handler, game_state, "third Eerie")
            choice = game_state.choice_context
            self.assertIsNotNone(
                choice, "third resolution opened no reanimation choice")
            self.assertEqual(choice.get("type"), "resolution_choice")
            self.assertEqual(
                choice.get("choice_kind"), "reanimate_from_graveyard")
            self.assertCountEqual(
                choice.get("options"), [own_creature, opposing_creature])
            option_index = choice["options"].index(opposing_creature)
            self._public(
                handler, 353 + option_index,
                "reanimate the opposing creature")

        self.assertIn(opposing_creature, controller["battlefield"])
        self.assertNotIn(opposing_creature, opponent["graveyard"])
        self.assertIn(own_creature, controller["graveyard"])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)

    def test_fourth_resolution_does_nothing_quietly(self):
        game_state, handler, controller, opponent = self._state(49102)
        inject_real_card(game_state, controller, self.VICTOR, "battlefield")
        for index in range(4):
            self._synthetic(
                game_state, controller, f"Victor Quiet Library {index}",
                "Sorcery", "library")
        opponent_hand_card = self._synthetic(
            game_state, opponent, "Victor Quiet Hand Card",
            "Sorcery", "hand")
        game_state.ability_handler.active_triggers = []

        with self.assertNoLogs(level=logging.WARNING):
            self._fire_eerie_once(handler, game_state, "quiet first")
            self._complete_surveil(handler, game_state, "quiet first")
            self._fire_eerie_once(handler, game_state, "quiet second")
            self._complete_discard(handler, game_state, "quiet second")
            self._fire_eerie_once(handler, game_state, "quiet third")
            self.assertIsNone(
                game_state.choice_context,
                "an empty-graveyard third resolution must resolve quietly")
            battlefield_before = list(controller["battlefield"])
            hand_before = list(opponent["hand"])
            fourth_enchantment = self._fire_eerie_once(
                handler, game_state, "quiet fourth")

        self.assertEqual(
            controller["battlefield"],
            battlefield_before + [fourth_enchantment])
        self.assertEqual(opponent["hand"], hand_before)
        self.assertIn(opponent_hand_card, opponent["graveyard"])
        self.assertEqual(game_state.stack, [])
        self.assertIsNone(game_state.choice_context)


if __name__ == "__main__":
    unittest.main()
