"""Regressions for global and graveyard-worded death trigger subjects."""

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


def _permanent(
        name: str, type_line: str, *, power: int = 2,
        toughness: int = 2) -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": type_line,
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
        "power": power,
        "toughness": toughness,
    }


class GlobalDeathSubjectScopeRegressionTest(unittest.TestCase):
    @staticmethod
    def _matching_triggers(game_state, source_id: int, phrase: str):
        phrase = phrase.casefold()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition.casefold()
        ]

    def _kill(
            self, game_state, owner, source_id: int, phrase: str,
            name: str, type_line: str, *, counters=None,
            token: bool = False, power: int = 2) -> int:
        event_id = inject_into_zone(
            game_state,
            owner,
            _permanent(
                name, type_line, power=power, toughness=max(1, power)),
            "battlefield",
        )
        event_card = game_state._safe_get_card(event_id)
        event_card.counters.update(counters or {})
        event_card.is_token = token
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            event_id, owner, "battlefield", owner, "graveyard",
            cause="destroy"))
        queued = self._matching_triggers(game_state, source_id, phrase)
        if queued:
            self.assertEqual(queued[0][2]["event_card_id"], event_id)
        return len(queued)

    def test_ashioks_reaper_scopes_graveyard_wording_by_type_and_controller(self):
        game_state = fresh(36701)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Ashiok's Reaper", "battlefield")
        phrase = "enchantment you control is put into a graveyard"

        observed = {
            "controlled_creature": self._kill(
                game_state, controller, source_id, phrase,
                "Ordinary Creature", "Creature - Human"),
            "opposing_enchantment": self._kill(
                game_state, opponent, source_id, phrase,
                "Opposing Enchantment", "Enchantment"),
            "controlled_enchantment": self._kill(
                game_state, controller, source_id, phrase,
                "Controlled Enchantment", "Enchantment"),
        }
        self.assertEqual(observed, {
            "controlled_creature": 0,
            "opposing_enchantment": 0,
            "controlled_enchantment": 1,
        })

    def test_long_feng_scopes_each_graveyard_subject_arm(self):
        game_state = fresh(36702)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Long Feng, Grand Secretariat",
            "battlefield")
        phrase = "another creature you control or a land you control"

        observed = {
            "opposing_creature": self._kill(
                game_state, opponent, source_id, phrase,
                "Opposing Creature", "Creature - Human"),
            "controlled_enchantment": self._kill(
                game_state, controller, source_id, phrase,
                "Wrong Controlled Type", "Enchantment"),
            "controlled_land": self._kill(
                game_state, controller, source_id, phrase,
                "Controlled Land", "Land"),
            "controlled_creature": self._kill(
                game_state, controller, source_id, phrase,
                "Controlled Creature", "Creature - Human"),
        }
        self.assertEqual(observed, {
            "opposing_creature": 0,
            "controlled_enchantment": 0,
            "controlled_land": 1,
            "controlled_creature": 1,
        })

    def test_donatello_uses_lki_for_subject_and_counter_condition(self):
        game_state = fresh(36707)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Donatello, Mutant Mechanic",
            "battlefield")
        phrase = "artifact you control is put into a graveyard"

        observed = {
            "controlled_counterless": self._kill(
                game_state, controller, source_id, phrase,
                "Counterless Artifact", "Artifact"),
            "opposing_countered": self._kill(
                game_state, opponent, source_id, phrase,
                "Opposing Countered Artifact", "Artifact",
                counters={"charge": 1}),
            "controlled_countered": self._kill(
                game_state, controller, source_id, phrase,
                "Controlled Countered Artifact", "Artifact",
                counters={"charge": 1}),
        }
        self.assertEqual(observed, {
            "controlled_counterless": 0,
            "opposing_countered": 0,
            "controlled_countered": 1,
        })

    def test_global_death_subject_rejects_nonmatching_permanent(self):
        game_state = fresh(36703)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Vein Ripper", "battlefield")
        phrase = "whenever a creature dies"

        self.assertEqual(self._kill(
            game_state, opponent, source_id, phrase,
            "Unrelated Land", "Land"), 0)
        self.assertEqual(self._kill(
            game_state, opponent, source_id, phrase,
            "Global Creature", "Creature - Human"), 1)

    def test_registered_global_subject_variants_use_lki_characteristics(self):
        cases = (
            (
                36711,
                "Knight of Doves",
                "enchantment you control is put into a graveyard",
                ("Enchantment", False, True),
                ("Creature - Human", False, False),
                ("Enchantment", True, False),
            ),
            (
                36712,
                "Krenko, Baron of Tin Street",
                "artifact is put into a graveyard",
                ("Artifact", True, True),
                ("Creature - Goblin", False, False),
            ),
            (
                36713,
                "High-Society Hunter",
                "another nontoken creature dies",
                ("Creature - Human", True, True),
                ("Creature - Human", True, False, {"token": True}),
                ("Artifact", False, False),
            ),
            (
                36714,
                "Tarrian's Soulcleaver",
                "another artifact or creature is put into a graveyard",
                ("Artifact", True, True),
                ("Creature - Human", False, True),
                ("Land", False, False),
            ),
            (
                36715,
                "Teysa, Opulent Oligarch",
                "clue you control is put into a graveyard",
                ("Artifact - Clue", False, True),
                ("Artifact", False, False),
                ("Artifact - Clue", True, False),
            ),
            (
                36716,
                "Ygra, Eater of All",
                "food is put into a graveyard",
                ("Artifact - Food", True, True),
                ("Land", False, False),
            ),
        )
        for seed, card_name, phrase, *events in cases:
            with self.subTest(card=card_name):
                game_state = fresh(seed)
                controller, opponent = game_state.p1, game_state.p2
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                observed = []
                expected = []
                for index, event in enumerate(events):
                    type_line, opposing, should_trigger, *raw_options = event
                    options = raw_options[0] if raw_options else {}
                    owner = opponent if opposing else controller
                    observed.append(self._kill(
                        game_state, owner, source_id, phrase,
                        f"{card_name} Event {index}", type_line,
                        counters=options.get("counters"),
                        token=bool(options.get("token", False))))
                    expected.append(1 if should_trigger else 0)
                self.assertEqual(observed, expected)

    def test_shadow_urchin_requires_any_counter_and_its_controller(self):
        game_state = fresh(36704)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Shadow Urchin", "battlefield")
        phrase = "creature you control with one or more counters on it dies"

        observed = {
            "controlled_counterless": self._kill(
                game_state, controller, source_id, phrase,
                "Counterless Creature", "Creature - Human"),
            "opposing_countered": self._kill(
                game_state, opponent, source_id, phrase,
                "Opposing Countered Creature", "Creature - Human",
                counters={"quest": 1}),
            "controlled_countered": self._kill(
                game_state, controller, source_id, phrase,
                "Controlled Countered Creature", "Creature - Human",
                counters={"quest": 1}),
        }
        self.assertEqual(observed, {
            "controlled_counterless": 0,
            "opposing_countered": 0,
            "controlled_countered": 1,
        })

    def test_predator_ooze_undamaged_death_is_fail_closed(self):
        game_state = fresh(36705)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Predator Ooze", "battlefield")
        phrase = "creature dealt damage by this creature this turn dies"

        self.assertEqual(self._kill(
            game_state, opponent, source_id, phrase,
            "Undamaged Creature", "Creature - Human"), 0)

    def test_kraven_relational_death_subject_remains_fail_closed(self):
        game_state = fresh(36706)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Kraven the Hunter", "battlefield")
        phrase = "greatest power among creatures that player controls dies"

        inject_into_zone(
            game_state, opponent,
            _permanent("Larger Survivor", "Creature - Giant", power=6),
            "battlefield")
        self.assertEqual(self._kill(
            game_state, opponent, source_id, phrase,
            "Smaller Creature", "Creature - Human", power=2), 0)

        # The current event stream has no atomic simultaneous-death group.
        # Until that provenance exists, even an apparent greatest-power event
        # is deliberately rejected rather than risking a false delivery.
        self.assertEqual(self._kill(
            game_state, opponent, source_id, phrase,
            "Apparent Greatest Creature", "Creature - Giant", power=7), 0)


if __name__ == "__main__":
    unittest.main()
