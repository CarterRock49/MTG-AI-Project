"""Full-pool regressions for source, controller, and characteristic trigger scope."""

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


class FullPoolTriggerScopeRegressionTest(unittest.TestCase):
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

    def _dispatch_entry(self, game_state, event_id: int, event_controller):
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

    def _dispatch_attack(self, game_state, attacker_id: int, controller):
        game_state.ability_handler.active_triggers = []
        game_state.current_attackers = [attacker_id]
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

    def _event_permanent(
            self, game_state, owner, name: str, type_line: str,
            *, token: bool = False) -> int:
        event_id = inject_into_zone(
            game_state, owner, _permanent(name, type_line), "battlefield")
        game_state._safe_get_card(event_id).is_token = token
        game_state.ability_handler.active_triggers = []
        return event_id

    def _die(
            self, game_state, owner, name: str, type_line: str,
            *, attacking: bool = False, face_down: bool = False) -> int:
        event_id = self._event_permanent(
            game_state, owner, name, type_line)
        card = game_state._safe_get_card(event_id)
        card.face_down = face_down
        game_state.current_attackers = [event_id] if attacking else []
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            event_id, owner, "battlefield", owner, "graveyard",
            cause="destroy"))
        return event_id

    def test_equipment_spacecraft_case_and_dfc_entry_triggers_are_self_only(self):
        fixtures = (
            (36101, "Assimilation Aegis", "this equipment enters"),
            (36102, "Atmospheric Greenhouse", "this spacecraft enters"),
            (36103, "Case of the Burning Masks", "this case enters"),
            (
                36104,
                "Brass's Tunnel-Grinder // Tecutlan, the Searing Rift",
                "brass's tunnel-grinder enters",
            ),
        )
        for seed, card_name, phrase in fixtures:
            with self.subTest(card=card_name):
                game_state, controller, _, source_id = self._source_state(
                    seed, card_name)
                other_id = self._event_permanent(
                    game_state, controller, "Unrelated Arrival", "Artifact")

                self._dispatch_entry(game_state, other_id, controller)
                self.assertEqual(
                    self._matching_triggers(game_state, source_id, phrase), [])

                self._dispatch_entry(game_state, source_id, controller)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), 1)

    def test_named_and_this_spacecraft_attack_triggers_are_self_only(self):
        fixtures = (
            (36111, "Alesha, Who Laughs at Fate", "alesha attacks"),
            (
                36112,
                "Entropic Battlecruiser",
                "this spacecraft attacks",
            ),
        )
        for seed, card_name, phrase in fixtures:
            with self.subTest(card=card_name):
                game_state, controller, _, source_id = self._source_state(
                    seed, card_name)
                other_id = self._event_permanent(
                    game_state, controller, "Unrelated Attacker", "Creature")

                self._dispatch_attack(game_state, other_id, controller)
                self.assertEqual(
                    self._matching_triggers(game_state, source_id, phrase), [])

                self._dispatch_attack(game_state, source_id, controller)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), 1)

    def test_captain_storm_requires_a_controlled_artifact_entry(self):
        game_state, controller, opponent, source_id = self._source_state(
            36121, "Captain Storm, Cosmium Raider")
        phrase = "artifact you control enters"
        fixtures = (
            (controller, "Controlled Artifact", "Artifact", 1),
            (controller, "Controlled Creature", "Creature", 0),
            (opponent, "Opponent Artifact", "Artifact", 0),
        )
        for owner, name, type_line, expected in fixtures:
            with self.subTest(event=name):
                event_id = self._event_permanent(
                    game_state, owner, name, type_line)
                self._dispatch_entry(game_state, event_id, owner)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

    def test_weftstalker_requires_another_controlled_creature_or_artifact(self):
        game_state, controller, opponent, source_id = self._source_state(
            36122, "Weftstalker Ardent")
        phrase = "another creature or artifact you control enters"
        fixtures = (
            (controller, "Controlled Creature", "Creature", 1),
            (controller, "Controlled Artifact", "Artifact", 1),
            (controller, "Controlled Enchantment", "Enchantment", 0),
            (opponent, "Opponent Creature", "Creature", 0),
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

    def test_ultron_requires_another_controlled_nontoken_artifact(self):
        game_state, controller, opponent, source_id = self._source_state(
            36123, "Ultron, Artificial Malevolence")
        phrase = "another nontoken artifact you control enters"
        fixtures = (
            (controller, "Nontoken Artifact", "Artifact", False, 1),
            (controller, "Artifact Token", "Artifact", True, 0),
            (controller, "Controlled Creature", "Creature", False, 0),
            (opponent, "Opponent Artifact", "Artifact", False, 0),
        )
        for owner, name, type_line, token, expected in fixtures:
            with self.subTest(event=name):
                event_id = self._event_permanent(
                    game_state, owner, name, type_line, token=token)
                self._dispatch_entry(game_state, event_id, owner)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

        self._dispatch_entry(game_state, source_id, controller)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

    def test_ares_death_watcher_requires_controlled_attacking_creature(self):
        game_state, controller, opponent, source_id = self._source_state(
            36131, "Ares, God of War")
        phrase = "attacking creature you control dies"
        fixtures = (
            (controller, "Controlled Attacker", True, 1),
            (controller, "Controlled Nonattacker", False, 0),
            (opponent, "Opponent Attacker", True, 0),
        )
        for owner, name, attacking, expected in fixtures:
            with self.subTest(event=name):
                self._die(
                    game_state, owner, name, "Creature", attacking=attacking)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

    def test_boggart_death_watcher_requires_controlled_goblin_creature(self):
        game_state, controller, opponent, source_id = self._source_state(
            36132, "Boggart Mischief")
        phrase = "goblin creature you control dies"
        fixtures = (
            (controller, "Controlled Goblin", "Creature - Goblin", 1),
            (controller, "Controlled Elf", "Creature - Elf", 0),
            (opponent, "Opponent Goblin", "Creature - Goblin", 0),
        )
        for owner, name, type_line, expected in fixtures:
            with self.subTest(event=name):
                self._die(game_state, owner, name, type_line)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

    def test_rakdos_death_watcher_requires_controlled_legendary_creature(self):
        game_state, controller, opponent, source_id = self._source_state(
            36133, "Rakdos Joins Up")
        phrase = "legendary creature you control dies"
        fixtures = (
            (
                controller, "Controlled Legend",
                "Legendary Creature - Human", 1,
            ),
            (controller, "Controlled Commoner", "Creature - Human", 0),
            (opponent, "Opponent Legend", "Legendary Creature - Human", 0),
        )
        for owner, name, type_line, expected in fixtures:
            with self.subTest(event=name):
                self._die(game_state, owner, name, type_line)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

    def test_yarus_death_watcher_requires_controlled_face_down_creature(self):
        game_state, controller, opponent, source_id = self._source_state(
            36134, "Yarus, Roar of the Old Gods")
        phrase = "face-down creature you control dies"
        fixtures = (
            (controller, "Controlled Face Down", True, 1),
            (controller, "Controlled Face Up", False, 0),
            (opponent, "Opponent Face Down", True, 0),
        )
        for owner, name, face_down, expected in fixtures:
            with self.subTest(event=name):
                self._die(
                    game_state, owner, name, "Creature",
                    face_down=face_down)
                self.assertEqual(
                    len(self._matching_triggers(
                        game_state, source_id, phrase)), expected)

    def test_venerated_stormsinger_self_and_another_death_arms(self):
        phrase = "this creature or another creature you control dies"

        game_state, controller, opponent, source_id = self._source_state(
            36141, "Venerated Stormsinger")
        other_id = self._die(
            game_state, controller, "Controlled Other", "Creature")
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], other_id)

        self._die(game_state, opponent, "Opponent Death", "Creature")
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

        game_state, controller, _, source_id = self._source_state(
            36142, "Venerated Stormsinger")
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            source_id, controller, "battlefield", controller, "graveyard",
            cause="destroy"))
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], source_id)


if __name__ == "__main__":
    unittest.main()
