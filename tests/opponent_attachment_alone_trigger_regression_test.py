"""Regressions for opposing, attachment-bound, and attacks-alone triggers."""

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


class OpponentAttachmentAloneTriggerRegressionTest(unittest.TestCase):
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
    def _add_creature(game_state, owner, name: str) -> int:
        creature_id = inject_into_zone(
            game_state, owner, _creature(name), "battlefield")
        game_state.ability_handler.active_triggers = []
        return creature_id

    @staticmethod
    def _matching_triggers(game_state, source_id: int, phrase: str):
        phrase = phrase.lower()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition
        ]

    @staticmethod
    def _dispatch_entry(game_state, creature_id: int, controller):
        game_state.ability_handler.active_triggers = []
        game_state.ability_handler.check_abilities(
            creature_id,
            "ENTERS_BATTLEFIELD",
            {
                "controller": controller,
                "event_controller": controller,
                "from_zone": "hand",
                "to_zone": "battlefield",
            },
        )

    def _kill(self, game_state, owner, creature_id: int):
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            creature_id, owner, "battlefield", owner, "graveyard",
            cause="destroy"))

    @staticmethod
    def _dispatch_attack(
            game_state, attacker_id: int, controller,
            attackers: list[int]):
        game_state.ability_handler.active_triggers = []
        game_state.current_attackers = list(attackers)
        game_state.ability_handler.check_abilities(
            attacker_id,
            "ATTACKS",
            {
                "controller": controller,
                "event_controller": controller,
                "attacker_id": attacker_id,
                "attacking_player": controller,
            },
        )

    def test_authority_of_the_consuls_hears_only_opponent_creature_entries(self):
        game_state, controller, opponent, source_id = self._source_state(
            36301, "Authority of the Consuls")
        phrase = "creature an opponent controls enters"
        friendly_id = self._add_creature(
            game_state, controller, "Friendly Arrival")
        opposing_id = self._add_creature(
            game_state, opponent, "Opposing Arrival")

        self._dispatch_entry(game_state, friendly_id, controller)
        self.assertEqual(len(self._matching_triggers(
            game_state, source_id, phrase)), 0)

        self._dispatch_entry(game_state, opposing_id, opponent)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], opposing_id)

    def test_massacre_wurm_hears_only_opponent_creature_deaths(self):
        game_state, controller, opponent, source_id = self._source_state(
            36302, "Massacre Wurm")
        phrase = "creature an opponent controls dies"
        friendly_id = self._add_creature(
            game_state, controller, "Friendly Casualty")
        opposing_id = self._add_creature(
            game_state, opponent, "Opposing Casualty")

        self._kill(game_state, controller, friendly_id)
        self.assertEqual(len(self._matching_triggers(
            game_state, source_id, phrase)), 0)

        self._kill(game_state, opponent, opposing_id)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], opposing_id)

    def test_lead_pipe_hears_only_its_equipped_creature_death(self):
        game_state, controller, _, source_id = self._source_state(
            36303, "Lead Pipe")
        phrase = "equipped creature dies"
        equipped_id = self._add_creature(
            game_state, controller, "Equipped Casualty")
        unrelated_id = self._add_creature(
            game_state, controller, "Unrelated Casualty")
        controller["attachments"][source_id] = equipped_id

        self._kill(game_state, controller, unrelated_id)
        self.assertEqual(len(self._matching_triggers(
            game_state, source_id, phrase)), 0)

        self._kill(game_state, controller, equipped_id)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], equipped_id)

    def test_angelic_destiny_hears_only_its_enchanted_creature_death(self):
        game_state = fresh(36305)
        controller = game_state.p1
        phrase = "enchanted creature dies"
        enchanted_id = self._add_creature(
            game_state, controller, "Enchanted Casualty")
        unrelated_id = self._add_creature(
            game_state, controller, "Unrelated Aura Casualty")
        source_id = inject_real_card(
            game_state, controller, "Angelic Destiny", "hand")
        self.assertTrue(game_state.move_card(
            source_id, controller, "hand", controller, "battlefield",
            cause="cast", context={"attach_to_target": enchanted_id}))
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        self.assertEqual(
            controller["attachments"].get(source_id), enchanted_id)

        self._kill(game_state, controller, unrelated_id)
        self.assertEqual(len(self._matching_triggers(
            game_state, source_id, phrase)), 0)

        self._kill(game_state, controller, enchanted_id)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], enchanted_id)

    def test_team_avatar_requires_exactly_one_declared_attacker(self):
        game_state, controller, _, source_id = self._source_state(
            36304, "Team Avatar")
        phrase = "creature you control attacks alone"
        first_id = self._add_creature(
            game_state, controller, "First Attacker")
        second_id = self._add_creature(
            game_state, controller, "Second Attacker")

        self._dispatch_attack(
            game_state, first_id, controller, [first_id, second_id])
        self.assertEqual(len(self._matching_triggers(
            game_state, source_id, phrase)), 0)

        self._dispatch_attack(game_state, first_id, controller, [first_id])
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], first_id)


if __name__ == "__main__":
    unittest.main()
