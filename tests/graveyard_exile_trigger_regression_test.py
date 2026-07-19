"""Regressions for graveyard-only and battlefield-to-exile triggers."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_into_zone, inject_real_card  # noqa: E402


logging.disable(logging.CRITICAL)


def _creature(name: str) -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": "Creature - Test",
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
        "power": 2,
        "toughness": 2,
    }


class GraveyardExileTriggerRegressionTest(unittest.TestCase):
    @staticmethod
    def _matching(game_state, source_id: int, phrase: str):
        phrase = phrase.casefold()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition.casefold()
        ]

    def _kill(self, game_state, owner, name: str) -> int:
        event_id = inject_into_zone(
            game_state, owner, _creature(name), "battlefield")
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            event_id, owner, "battlefield", owner, "graveyard",
            cause="destroy"))
        return event_id

    def test_furious_forebear_watches_only_from_a_preexisting_graveyard(self):
        game_state = fresh(36701)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Furious Forebear", "battlefield")
        phrase = "creature you control dies while this card is in your graveyard"

        self._kill(game_state, controller, "Battlefield-Watched Death")
        self.assertEqual(self._matching(
            game_state, source_id, phrase), [])

        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            source_id, controller, "battlefield", controller, "graveyard",
            cause="destroy"))
        self.assertEqual(
            self._matching(game_state, source_id, phrase), [],
            "the move that put Forebear into the graveyard is too early")

        self._kill(game_state, opponent, "Opponent Death")
        self.assertEqual(self._matching(
            game_state, source_id, phrase), [])

        event_id = self._kill(
            game_state, controller, "Graveyard-Watched Death")
        queued = self._matching(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], event_id)
        self.assertEqual(queued[0][2]["source_zone"], "graveyard")

    def _syr_vondam_state(self, seed: int, counters: int = 0):
        game_state = fresh(seed)
        controller = game_state.p1
        source_id = inject_real_card(
            game_state, controller,
            "Syr Vondam, Sunstar Exemplar", "battlefield")
        if counters:
            game_state.add_counter(source_id, "+1/+1", counters)
        game_state.ability_handler.active_triggers = []
        return game_state, controller, game_state.p2, source_id

    def test_syr_vondam_self_death_requires_last_known_power_four(self):
        phrase = "when syr vondam dies or is put into exile"
        for seed, counters, expected in (
                (36711, 0, 0),
                (36712, 2, 1)):
            with self.subTest(counters=counters):
                game_state, controller, _, source_id = \
                    self._syr_vondam_state(seed, counters)
                self.assertTrue(game_state.move_card(
                    source_id, controller, "battlefield", controller,
                    "graveyard", cause="destroy"))
                queued = self._matching(
                    game_state, source_id, phrase)
                self.assertEqual(len(queued), expected)
                if queued:
                    self.assertGreaterEqual(
                        queued[0][2]["last_known"]["power"], 4)

    def test_syr_vondam_self_exile_uses_the_real_exile_event_and_power_gate(self):
        phrase = "when syr vondam dies or is put into exile"
        for seed, counters, expected in (
                (36721, 0, 0),
                (36722, 2, 1)):
            with self.subTest(counters=counters):
                game_state, controller, _, source_id = \
                    self._syr_vondam_state(seed, counters)
                self.assertTrue(game_state.move_card(
                    source_id, controller, "battlefield", controller,
                    "exile", cause="exile"))
                queued = self._matching(
                    game_state, source_id, phrase)
                self.assertEqual(len(queued), expected)
                if queued:
                    self.assertEqual(
                        queued[0][2]["event_card_id"], source_id)
                    self.assertEqual(
                        queued[0][2]["event_type"], "ENTER_EXILE")
                    self.assertGreaterEqual(
                        queued[0][2]["last_known"]["power"], 4)

    def test_syr_vondam_other_exile_enforces_another_controlled_creature(self):
        game_state, controller, opponent, source_id = \
            self._syr_vondam_state(36731)
        phrase = "another creature you control dies or is put into exile"

        def exile_from_battlefield(owner, name):
            event_id = inject_into_zone(
                game_state, owner, _creature(name), "battlefield")
            game_state.ability_handler.active_triggers = []
            self.assertTrue(game_state.move_card(
                event_id, owner, "battlefield", owner, "exile",
                cause="exile"))
            return event_id, self._matching(
                game_state, source_id, phrase)

        _, opponent_queued = exile_from_battlefield(
            opponent, "Opposing Exile")
        self.assertEqual(opponent_queued, [])

        event_id, controlled_queued = exile_from_battlefield(
            controller, "Controlled Exile")
        self.assertEqual(len(controlled_queued), 1)
        self.assertEqual(
            controlled_queued[0][2]["event_card_id"], event_id)

        graveyard_id = self._kill(
            game_state, controller, "Creature Card From Graveyard")
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            graveyard_id, controller, "graveyard", controller, "exile",
            cause="exile"))
        self.assertEqual(self._matching(
            game_state, source_id, phrase), [])


if __name__ == "__main__":
    unittest.main()
