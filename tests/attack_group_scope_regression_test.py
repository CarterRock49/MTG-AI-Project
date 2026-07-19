"""Regressions for self, player, and attack-group trigger scope."""

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


def _permanent(name: str, type_line: str = "Creature - Test",
               *, power: int = 2, oracle_text: str = "") -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "colors": [0, 0, 0, 0, 0],
        "power": power,
        "toughness": 2,
    }


class AttackGroupScopeRegressionTest(unittest.TestCase):
    @staticmethod
    def _state(seed: int, source_name: str):
        game_state = fresh(seed)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, source_name, "battlefield")
        game_state.ability_handler.active_triggers = []
        return game_state, controller, opponent, source_id

    @staticmethod
    def _creature(game_state, owner, name: str, *, power: int = 2,
                  type_line: str = "Creature - Test") -> int:
        return inject_into_zone(
            game_state, owner,
            _permanent(name, type_line, power=power), "battlefield")

    @staticmethod
    def _dispatch_group(game_state, owner, attacker_ids):
        game_state.ability_handler.active_triggers = []
        game_state.current_attackers = list(attacker_ids)
        for attacker_id in attacker_ids:
            game_state.ability_handler.check_abilities(
                attacker_id, "ATTACKS", {
                    "controller": owner,
                    "event_controller": owner,
                    "attacking_player": owner,
                    "attacker_id": attacker_id,
                    "first_attack_this_turn": True,
                })

    @staticmethod
    def _triggers(game_state, source_id: int, phrase: str):
        phrase = phrase.casefold()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition.casefold()
        ]

    def test_named_enters_or_attacks_is_scoped_to_agrus(self):
        game_state, controller, _, source_id = self._state(
            36701, "Agrus Kos, Spirit of Justice")
        unrelated = self._creature(
            game_state, controller, "Unrelated Attacker")
        phrase = "agrus kos enters or attacks"

        self._dispatch_group(game_state, controller, [unrelated])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])

        self._dispatch_group(game_state, controller, [source_id])
        queued = self._triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], source_id)

    def test_named_plural_and_composite_subjects_stay_source_scoped(self):
        game_state, controller, _, source_id = self._state(
            36708, "Don & Raph, Hard Science")
        unrelated = self._creature(
            game_state, controller, "Unrelated Team Attacker")
        phrase = "don & raph attack"
        self._dispatch_group(game_state, controller, [unrelated])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

        game_state, controller, _, source_id = self._state(
            36709, "Sokka, Lateral Strategist")
        companion = self._creature(
            game_state, controller, "Sokka's Companion")
        phrase = "sokka and at least one other creature attack"
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        self._dispatch_group(game_state, controller, [companion])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        self._dispatch_group(
            game_state, controller, [source_id, companion])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

    def test_whenever_you_attack_uses_controller_and_coalesces(self):
        game_state, controller, opponent, source_id = self._state(
            36702, "Inti, Seneschal of the Sun")
        phrase = "whenever you attack"
        opposing = [self._creature(
            game_state, opponent, f"Opposing Attacker {index}")
                    for index in range(2)]
        controlled = [self._creature(
            game_state, controller, f"Controlled Attacker {index}")
                      for index in range(3)]

        self._dispatch_group(game_state, opponent, opposing)
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])

        self._dispatch_group(game_state, controller, controlled)
        queued = self._triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(
            queued[0][2]["matching_attacker_ids"], controlled)
        self.assertEqual(queued[0][2]["attacker_count"], 3)

    def test_attack_with_threshold_uses_group_and_controller(self):
        game_state, controller, opponent, source_id = self._state(
            36703, "Armasaur Guide")
        phrase = "you attack with three or more creatures"
        one = [self._creature(game_state, controller, "Lone Attacker")]
        own_three = [self._creature(
            game_state, controller, f"Own Group {index}")
                     for index in range(3)]
        opposing_three = [self._creature(
            game_state, opponent, f"Opposing Group {index}")
                          for index in range(3)]

        self._dispatch_group(game_state, controller, one)
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        self._dispatch_group(game_state, opponent, opposing_three)
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])

        self._dispatch_group(game_state, controller, own_three)
        queued = self._triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["attacker_count"], 3)

    def test_a_player_attacks_with_thresholds_queue_once_each(self):
        game_state, controller, _, source_id = self._state(
            36704, "Aurelia, the Law Above")
        three = [self._creature(
            game_state, controller, f"Three Group {index}")
                 for index in range(3)]
        five = three + [self._creature(
            game_state, controller, f"Five Group {index}")
                        for index in range(2)]

        self._dispatch_group(game_state, controller, three)
        self.assertEqual(len(self._triggers(
            game_state, source_id, "player attacks with three")), 1)
        self.assertEqual(self._triggers(
            game_state, source_id, "player attacks with five"), [])

        self._dispatch_group(game_state, controller, five)
        self.assertEqual(len(self._triggers(
            game_state, source_id, "player attacks with three")), 1)
        self.assertEqual(len(self._triggers(
            game_state, source_id, "player attacks with five")), 1)

    def test_tomik_requires_opponent_and_two_protected_defenders(self):
        game_state, controller, opponent, source_id = self._state(
            36705, "Tomik, Wielder of Law")
        phrase = "opponent attacks with creatures"
        own = [self._creature(
            game_state, controller, f"Own Attacker {index}")
               for index in range(2)]
        opposing = [self._creature(
            game_state, opponent, f"Opposing Attacker {index}")
                    for index in range(2)]

        self._dispatch_group(game_state, controller, own)
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        self._dispatch_group(game_state, opponent, opposing[:1])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])

        self._dispatch_group(game_state, opponent, opposing)
        queued = self._triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(
            queued[0][2]["attacking_you_or_your_planeswalkers_count"], 2)

        battle_id = inject_into_zone(
            game_state, controller,
            _permanent("Protected Battle", "Battle - Siege"),
            "battlefield")
        game_state.battle_attack_targets = {opposing[1]: battle_id}
        self._dispatch_group(game_state, opponent, opposing)
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])

    def test_attack_target_qualifiers_distinguish_defender_kinds(self):
        game_state, controller, opponent, source_id = self._state(
            36717, "Bitter Work")
        attacker_id = self._creature(
            game_state, controller, "Power Four Attacker", power=4)
        phrase = "attack a player with one or more creatures"
        self._dispatch_group(game_state, controller, [attacker_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

        planeswalker_id = inject_into_zone(
            game_state, opponent,
            _permanent("Opposing Planeswalker",
                       "Legendary Planeswalker - Test"),
            "battlefield")
        game_state.planeswalker_attack_targets = {
            attacker_id: planeswalker_id}
        self._dispatch_group(game_state, controller, [attacker_id])
        self.assertEqual(self._triggers(
            game_state, source_id, phrase), [])

        # Defender-side one-or-more triggers coalesce only attackers actually
        # aimed at that player, excluding Battles they protect.
        game_state, controller, opponent, source_id = self._state(
            36718, "Sabotage Strategist")
        attackers = [self._creature(
            game_state, opponent, f"Saboteur {index}")
                     for index in range(2)]
        battle_id = inject_into_zone(
            game_state, controller,
            _permanent("Protected Battle", "Battle - Siege"),
            "battlefield")
        phrase = "creatures attack you"
        game_state.battle_attack_targets = {attackers[0]: battle_id}
        self._dispatch_group(game_state, opponent, attackers)
        queued = self._triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(
            queued[0][2]["matching_attacker_ids"], [attackers[1]])
        game_state.battle_attack_targets = {
            attacker_id: battle_id for attacker_id in attackers}
        self._dispatch_group(game_state, opponent, attackers)
        self.assertEqual(self._triggers(
            game_state, source_id, phrase), [])

        # Relative-player wording examines the actual defending player.
        game_state, controller, opponent, source_id = self._state(
            36719, "Preacher of the Schism")
        phrase = "attacks the player with the most life"
        controller["life"], opponent["life"] = 20, 19
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(
            game_state, source_id, phrase), [])
        opponent["life"] = 20
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

    def test_ruby_attack_while_requires_power_four_creature(self):
        game_state, controller, _, source_id = self._state(
            36706, "Ruby, Daring Tracker")
        phrase = "ruby attacks while you control"

        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])

        self._creature(
            game_state, controller, "Power Four Ally", power=4)
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

    def test_other_attack_while_state_families_use_live_state(self):
        # Numeric permanent count.
        game_state, controller, _, source_id = self._state(
            36711, "Brazen Blademaster")
        phrase = "attacks while you control two or more artifacts"
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        for index in range(2):
            inject_into_zone(
                game_state, controller,
                _permanent(f"Test Artifact {index}", "Artifact"),
                "battlefield")
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

        # Token characteristic.
        game_state, controller, _, source_id = self._state(
            36712, "Seasoned Warrenguard")
        phrase = "attacks while you control a token"
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        token_id = self._creature(game_state, controller, "Test Token")
        game_state._safe_get_card(token_id).is_token = True
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

        # Relative life total.
        game_state, controller, opponent, source_id = self._state(
            36713, "Preacher of the Schism")
        phrase = "attacks while you have the most life"
        controller["life"], opponent["life"] = 19, 20
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        controller["life"] = 20
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

        # Negative "another" characteristic.
        game_state, controller, _, source_id = self._state(
            36714, "Pugnacious Hammerskull")
        phrase = "attacks while you don't control another dinosaur"
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)
        self._creature(
            game_state, controller, "Other Dinosaur",
            type_line="Creature - Dinosaur")
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])

        # Delirium counts distinct card types in the controller's graveyard.
        game_state, controller, _, source_id = self._state(
            36715, "Hand That Feeds")
        phrase = "attacks while there are four or more card types"
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        for index, type_line in enumerate((
                "Artifact", "Enchantment", "Land", "Creature - Test")):
            inject_into_zone(
                game_state, controller,
                _permanent(f"Graveyard Type {index}", type_line),
                "graveyard")
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

        # The attack arm of an "attacks or blocks while" condition shares the
        # same live controller-state gate.
        game_state, controller, _, source_id = self._state(
            36716, "Burning Sun Cavalry")
        phrase = "attacks or blocks while you control a dinosaur"
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(game_state, source_id, phrase), [])
        self._creature(
            game_state, controller, "Controlled Dinosaur",
            type_line="Creature - Dinosaur")
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

        # The same printed ability must apply the gate to its BLOCK arm too.
        game_state, controller, _, source_id = self._state(
            36720, "Burning Sun Cavalry")
        game_state.ability_handler.active_triggers = []
        game_state.ability_handler.check_abilities(
            source_id, "BLOCKS", {
                "controller": controller,
                "event_controller": controller,
                "blocker_id": source_id,
            })
        self.assertEqual(self._triggers(
            game_state, source_id, phrase), [])
        self._creature(
            game_state, controller, "Blocking Dinosaur Ally",
            type_line="Creature - Dinosaur")
        game_state.ability_handler.active_triggers = []
        game_state.ability_handler.check_abilities(
            source_id, "BLOCKS", {
                "controller": controller,
                "event_controller": controller,
                "blocker_id": source_id,
            })
        self.assertEqual(len(self._triggers(
            game_state, source_id, phrase)), 1)

    def test_attack_while_unknown_state_fails_closed(self):
        game_state = fresh(36707)
        controller = game_state.p1
        source_id = inject_into_zone(
            game_state, controller,
            _permanent(
                "Impossible Moon Watcher", oracle_text=(
                    "Whenever this creature attacks while the moon is "
                    "purple, put a +1/+1 counter on it.")),
            "battlefield")
        self._dispatch_group(game_state, controller, [source_id])
        self.assertEqual(self._triggers(
            game_state, source_id, "moon is purple"), [])


if __name__ == "__main__":
    unittest.main()
