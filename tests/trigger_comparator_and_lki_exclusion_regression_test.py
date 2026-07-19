"""Regressions for numeric trigger criteria and LKI subtype exclusions."""

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


def _creature(
        name: str, subtype: str, *, power: int, toughness: int,
        mana_value: int) -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": mana_value,
        "type_line": f"Creature - {subtype}",
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
        "power": power,
        "toughness": toughness,
    }


class TriggerComparatorAndLkiExclusionRegressionTest(unittest.TestCase):
    @staticmethod
    def _source_state(seed: int, card_name: str):
        game_state = fresh(seed)
        controller = game_state.p1
        source_id = inject_real_card(
            game_state, controller, card_name, "battlefield")
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        return game_state, controller, source_id

    @staticmethod
    def _matching_triggers(game_state, source_id: int, phrase: str):
        phrase = phrase.casefold()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition.casefold()
        ]

    @staticmethod
    def _event_creature(
            game_state, controller, name: str, subtype: str, *,
            power: int, toughness: int, mana_value: int) -> int:
        event_id = inject_into_zone(
            game_state,
            controller,
            _creature(
                name,
                subtype,
                power=power,
                toughness=toughness,
                mana_value=mana_value,
            ),
            "battlefield",
        )
        game_state.ability_handler.active_triggers = []
        return event_id

    @staticmethod
    def _dispatch_entry(game_state, controller, event_id: int):
        game_state.ability_handler.active_triggers = []
        game_state.ability_handler.check_abilities(
            event_id,
            "ENTERS_BATTLEFIELD",
            {
                "controller": controller,
                "event_controller": controller,
                "from_zone": "hand",
                "to_zone": "battlefield",
            },
        )

    def _assert_entry_trigger_count(
            self, game_state, controller, source_id: int, phrase: str,
            event_id: int, expected: int):
        self._dispatch_entry(game_state, controller, event_id)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), expected)
        if expected:
            self.assertEqual(queued[0][2]["event_card_id"], event_id)

    def test_garruks_uprising_requires_power_four_or_greater(self):
        game_state, controller, source_id = self._source_state(
            36501, "Garruk's Uprising")
        phrase = "creature you control with power 4 or greater enters"

        weak_id = self._event_creature(
            game_state, controller, "Small Arrival", "Human",
            power=1, toughness=1, mana_value=1)
        self._assert_entry_trigger_count(
            game_state, controller, source_id, phrase, weak_id, 0)

        strong_id = self._event_creature(
            game_state, controller, "Boundary Arrival", "Beast",
            power=4, toughness=4, mana_value=4)
        self._assert_entry_trigger_count(
            game_state, controller, source_id, phrase, strong_id, 1)

    def test_serra_redeemer_requires_power_two_or_less(self):
        game_state, controller, source_id = self._source_state(
            36502, "Serra Redeemer")
        phrase = "another creature you control with power 2 or less enters"

        large_id = self._event_creature(
            game_state, controller, "Large Arrival", "Giant",
            power=5, toughness=5, mana_value=5)
        self._assert_entry_trigger_count(
            game_state, controller, source_id, phrase, large_id, 0)

        small_id = self._event_creature(
            game_state, controller, "Boundary Arrival", "Human",
            power=2, toughness=2, mana_value=2)
        self._assert_entry_trigger_count(
            game_state, controller, source_id, phrase, small_id, 1)

    def test_doc_ocks_tentacles_requires_mana_value_five_or_greater(self):
        game_state, controller, source_id = self._source_state(
            36503, "Doc Ock's Tentacles")
        phrase = "creature you control with mana value 5 or greater enters"

        cheap_id = self._event_creature(
            game_state, controller, "Cheap Arrival", "Human",
            power=1, toughness=1, mana_value=1)
        self._assert_entry_trigger_count(
            game_state, controller, source_id, phrase, cheap_id, 0)

        expensive_id = self._event_creature(
            game_state, controller, "Boundary Arrival", "Construct",
            power=5, toughness=5, mana_value=5)
        self._assert_entry_trigger_count(
            game_state, controller, source_id, phrase, expensive_id, 1)

    def test_valkyries_call_accepts_non_angel_and_rejects_angel_lki(self):
        game_state, controller, source_id = self._source_state(
            36504, "Valkyrie's Call")
        phrase = "nontoken, non-angel creature you control dies"

        human_id = self._event_creature(
            game_state, controller, "Mortal Human", "Human",
            power=2, toughness=2, mana_value=2)
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            human_id,
            controller,
            "battlefield",
            controller,
            "graveyard",
            cause="destroy",
        ))
        human_triggers = self._matching_triggers(
            game_state, source_id, phrase)
        self.assertEqual(len(human_triggers), 1)
        self.assertEqual(
            human_triggers[0][2]["event_card_id"], human_id)

        angel_id = self._event_creature(
            game_state, controller, "Celestial Angel", "Angel",
            power=2, toughness=2, mana_value=2)
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            angel_id,
            controller,
            "battlefield",
            controller,
            "graveyard",
            cause="destroy",
        ))
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])


if __name__ == "__main__":
    unittest.main()
