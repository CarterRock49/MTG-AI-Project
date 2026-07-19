"""Regressions for attachment and post-controller attack qualifiers."""

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
from Playersim.ability_utils import EffectFactory  # noqa: E402


logging.disable(logging.CRITICAL)


def _creature(name: str, *, oracle_text: str = "") -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": "Creature - Test",
        "oracle_text": oracle_text,
        "colors": [0, 0, 0, 0, 0],
        "power": 2,
        "toughness": 2,
    }


class AttackTriggerQualifierRegressionTest(unittest.TestCase):
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
    def _creature_on_battlefield(game_state, owner, name: str,
                                 *, oracle_text: str = "") -> int:
        creature_id = inject_into_zone(
            game_state, owner, _creature(name, oracle_text=oracle_text),
            "battlefield")
        game_state.ability_handler.active_triggers = []
        return creature_id

    @staticmethod
    def _dispatch_attack(game_state, attacker_id: int, attacker_controller):
        game_state.ability_handler.active_triggers = []
        game_state.current_attackers = [attacker_id]
        game_state.ability_handler.check_abilities(
            attacker_id,
            "ATTACKS",
            {
                "controller": attacker_controller,
                "event_controller": attacker_controller,
                "attacker_id": attacker_id,
                "attacking_player": attacker_controller,
            },
        )

    @staticmethod
    def _matching_triggers(game_state, source_id: int, phrase: str):
        phrase = phrase.lower()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition
        ]

    def test_equipment_and_aura_attack_triggers_are_attachment_scoped(self):
        fixtures = (
            (36201, "Atomic Microsizer", "equipped creature attacks"),
            (36202, "Baseball Bat", "equipped creature attacks"),
            (36203, "Stormbeacon Blade", "equipped creature attacks"),
            (36204, "Chorale of the Void", "enchanted creature attacks"),
        )
        for seed, card_name, phrase in fixtures:
            with self.subTest(card=card_name):
                if card_name == "Chorale of the Void":
                    # An unattached Aura is put into its owner's graveyard by
                    # state-based actions. Put both legal targets in place
                    # before injecting the Aura, then attach it immediately.
                    game_state = fresh(seed)
                    controller = game_state.p1
                    attached = self._creature_on_battlefield(
                        game_state, controller, "Attached Attacker")
                    unrelated = self._creature_on_battlefield(
                        game_state, controller, "Unrelated Attacker")
                    source_id = inject_real_card(
                        game_state, controller, card_name, "graveyard")
                    self.assertTrue(game_state.move_card(
                        source_id, controller, "graveyard", controller,
                        "battlefield", context={"attach_to_target": attached}))
                    game_state.ability_handler.active_triggers = []
                else:
                    game_state, controller, _, source_id = self._source_state(
                        seed, card_name)
                    attached = self._creature_on_battlefield(
                        game_state, controller, "Attached Attacker")
                    unrelated = self._creature_on_battlefield(
                        game_state, controller, "Unrelated Attacker")
                if card_name != "Chorale of the Void":
                    controller["attachments"][source_id] = attached

                self._dispatch_attack(game_state, unrelated, controller)
                self.assertEqual(
                    self._matching_triggers(game_state, source_id, phrase), [])

                self._dispatch_attack(game_state, attached, controller)
                queued = self._matching_triggers(
                    game_state, source_id, phrase)
                self.assertEqual(len(queued), 1)
                self.assertEqual(
                    queued[0][2]["event_card_id"], attached)

    def test_swordsman_watches_an_equipped_attacker_without_being_equipment(self):
        game_state, controller, opponent, source_id = self._source_state(
            36211, "Swordsman, Sharp Scoundrel")
        equipment_id = inject_real_card(
            game_state, controller, "Baseball Bat", "battlefield")
        equipped = self._creature_on_battlefield(
            game_state, controller, "Equipped Attacker")
        unequipped = self._creature_on_battlefield(
            game_state, controller, "Unequipped Attacker")
        opponent_equipped = self._creature_on_battlefield(
            game_state, opponent, "Opponent Equipped Attacker")
        opponent_equipment = inject_real_card(
            game_state, opponent, "Atomic Microsizer", "battlefield")
        controller["attachments"][equipment_id] = equipped
        opponent["attachments"][opponent_equipment] = opponent_equipped
        phrase = "an equipped creature you control attacks"

        self.assertNotIn(source_id, controller["attachments"])
        self._dispatch_attack(game_state, equipped, controller)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], equipped)

        self._dispatch_attack(game_state, unequipped, controller)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

        self._dispatch_attack(game_state, opponent_equipped, opponent)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

    def test_attachment_granted_trigger_uses_that_attachments_bearer(self):
        game_state = fresh(36212)
        controller = game_state.p1
        first = self._creature_on_battlefield(
            game_state, controller, "First Enchanted Attacker")
        second = self._creature_on_battlefield(
            game_state, controller, "Second Enchanted Attacker")

        role_ids = []
        for target_id in (first, second):
            effect = EffectFactory.create_effects(
                "Create a Sorcerer Role token attached to target creature.")[0]
            self.assertTrue(effect.apply(
                game_state, None, controller, {"creatures": [target_id]}))
            role_ids.append(next(
                card_id for card_id in reversed(controller["battlefield"])
                if card_id not in role_ids
                and getattr(game_state._safe_get_card(
                    card_id), "name", "") == "Sorcerer Role"))

        self._dispatch_attack(game_state, first, controller)
        phrase = "enchanted creature attacks"
        self.assertEqual(len(self._matching_triggers(
            game_state, role_ids[0], phrase)), 1)
        self.assertEqual(self._matching_triggers(
            game_state, role_ids[1], phrase), [])

    def test_byrke_requires_controlled_attacker_with_plus_one_counter(self):
        game_state, controller, opponent, source_id = self._source_state(
            36221, "Byrke, Long Ear of the Law")
        matching = self._creature_on_battlefield(
            game_state, controller, "Countered Attacker")
        without_counter = self._creature_on_battlefield(
            game_state, controller, "Counterless Attacker")
        opponent_matching = self._creature_on_battlefield(
            game_state, opponent, "Opponent Countered Attacker")
        game_state._safe_get_card(matching).counters["+1/+1"] = 1
        game_state._safe_get_card(opponent_matching).counters["+1/+1"] = 1
        phrase = "creature you control with a +1/+1 counter on it attacks"

        self._dispatch_attack(game_state, matching, controller)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], matching)

        self._dispatch_attack(game_state, without_counter, controller)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

        self._dispatch_attack(game_state, opponent_matching, opponent)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

    def test_j_jonah_requires_controlled_attacker_with_menace(self):
        game_state, controller, opponent, source_id = self._source_state(
            36231, "J. Jonah Jameson")
        matching = self._creature_on_battlefield(
            game_state, controller, "Menacing Attacker", oracle_text="Menace")
        without_menace = self._creature_on_battlefield(
            game_state, controller, "Ordinary Attacker")
        opponent_matching = self._creature_on_battlefield(
            game_state, opponent, "Opponent Menacing Attacker",
            oracle_text="Menace")
        phrase = "creature you control with menace attacks"

        self.assertTrue(game_state.check_keyword(matching, "menace"))
        self.assertTrue(game_state.check_keyword(opponent_matching, "menace"))
        self.assertFalse(game_state.check_keyword(without_menace, "menace"))

        self._dispatch_attack(game_state, matching, controller)
        queued = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], matching)

        self._dispatch_attack(game_state, without_menace, controller)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])

        self._dispatch_attack(game_state, opponent_matching, opponent)
        self.assertEqual(
            self._matching_triggers(game_state, source_id, phrase), [])


if __name__ == "__main__":
    unittest.main()
