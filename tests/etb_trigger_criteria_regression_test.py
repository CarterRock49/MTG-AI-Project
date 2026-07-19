"""Regressions for compound ETB criteria and named self death arms."""

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


def _permanent(name: str, type_line: str) -> dict:
    creature = "Creature" in type_line
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": type_line,
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
        "power": 1 if creature else 0,
        "toughness": 1 if creature else 0,
    }


class EtbTriggerCriteriaRegressionTest(unittest.TestCase):
    @staticmethod
    def _source_state(seed: int, card_name: str):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, card_name, "battlefield")
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        return game_state, controller, opponent, source_id

    @staticmethod
    def _matching_triggers(game_state, source_id: int, phrase: str):
        phrase = phrase.lower()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition
        ]

    @staticmethod
    def _event_permanent(
            game_state, owner, name: str, type_line: str, *,
            token: bool = False, face_down: bool = False) -> int:
        event_id = inject_into_zone(
            game_state, owner, _permanent(name, type_line), "battlefield")
        card = game_state._safe_get_card(event_id)
        card.is_token = token
        card.face_down = face_down
        game_state.ability_handler.active_triggers = []
        return event_id

    @staticmethod
    def _dispatch_entry(game_state, event_id: int, event_controller):
        game_state.ability_handler.active_triggers = []
        game_state.ability_handler.check_abilities(
            event_id,
            "ENTERS_BATTLEFIELD",
            {
                "controller": event_controller,
                "event_controller": event_controller,
                "from_zone": "hand",
                "to_zone": "battlefield",
            },
        )

    def _die(
            self, game_state, owner, name: str, type_line: str, *,
            token: bool = False) -> int:
        event_id = self._event_permanent(
            game_state, owner, name, type_line, token=token)
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            event_id, owner, "battlefield", owner, "graveyard",
            cause="destroy"))
        return event_id

    def test_valley_mightcaller_accepts_each_printed_creature_subtype(self):
        game_state, controller, _, source_id = self._source_state(
            36201, "Valley Mightcaller")
        phrase = "whenever another frog"

        for subtype in ("Frog", "Rabbit", "Raccoon", "Squirrel"):
            with self.subTest(subtype=subtype):
                event_id = self._event_permanent(
                    game_state, controller, f"Controlled {subtype}",
                    f"Creature - {subtype}")
                self._dispatch_entry(game_state, event_id, controller)
                queued = self._matching_triggers(
                    game_state, source_id, phrase)
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0][2]["event_card_id"], event_id)

    def test_valley_mightcaller_rejects_wrong_type_controller_and_self(self):
        game_state, controller, opponent, source_id = self._source_state(
            36202, "Valley Mightcaller")
        phrase = "whenever another frog"
        fixtures = (
            (controller, "Controlled Badger", "Creature - Badger"),
            (opponent, "Opponent Frog", "Creature - Frog"),
        )

        for owner, name, type_line in fixtures:
            with self.subTest(event=name):
                event_id = self._event_permanent(
                    game_state, owner, name, type_line)
                self._dispatch_entry(game_state, event_id, owner)
                self.assertEqual(
                    self._matching_triggers(game_state, source_id, phrase), [])

        self._dispatch_entry(game_state, source_id, controller)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

    def test_wildborn_preserver_requires_another_controlled_nonhuman_creature(self):
        game_state, controller, opponent, source_id = self._source_state(
            36211, "Wildborn Preserver")
        phrase = "another non-human creature you control enters"
        fixtures = (
            (controller, "Controlled Elf", "Creature - Elf", 1),
            (controller, "Controlled Human", "Creature - Human", 0),
            (controller, "Controlled Relic", "Artifact", 0),
            (opponent, "Opponent Elf", "Creature - Elf", 0),
        )

        for owner, name, type_line, expected in fixtures:
            with self.subTest(event=name):
                event_id = self._event_permanent(
                    game_state, owner, name, type_line)
                self._dispatch_entry(game_state, event_id, owner)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

        self._dispatch_entry(game_state, source_id, controller)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

    def test_threats_requires_a_controlled_face_down_permanent(self):
        game_state, controller, opponent, source_id = self._source_state(
            36221, "Threats Around Every Corner")
        phrase = "face-down permanent you control enters"
        fixtures = (
            (controller, "Controlled Face Down", True, 1),
            (controller, "Controlled Face Up", False, 0),
            (opponent, "Opponent Face Down", True, 0),
        )

        for owner, name, face_down, expected in fixtures:
            with self.subTest(event=name):
                event_id = self._event_permanent(
                    game_state, owner, name, "Creature",
                    face_down=face_down)
                self._dispatch_entry(game_state, event_id, owner)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

    def test_totentanz_other_arm_requires_a_controlled_nontoken_creature(self):
        game_state, controller, opponent, source_id = self._source_state(
            36231, "Totentanz, Swarm Piper")
        phrase = "totentanz or another nontoken creature you control dies"

        event_id = self._die(
            game_state, controller, "Controlled Other", "Creature - Rat")
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], event_id)

        fixtures = (
            (controller, "Controlled Rat Token", "Creature - Rat", True),
            (controller, "Controlled Relic", "Artifact", False),
            (opponent, "Opponent Creature", "Creature - Rat", False),
        )
        for owner, name, type_line, token in fixtures:
            with self.subTest(event=name):
                self._die(
                    game_state, owner, name, type_line, token=token)
                self.assertEqual(
                    self._matching_triggers(game_state, source_id, phrase), [])

    def test_totentanz_named_self_death_arm_triggers_once(self):
        game_state, controller, _, source_id = self._source_state(
            36232, "Totentanz, Swarm Piper")
        phrase = "totentanz or another nontoken creature you control dies"

        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            source_id, controller, "battlefield", controller, "graveyard",
            cause="destroy"))
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], source_id)


if __name__ == "__main__":
    unittest.main()
