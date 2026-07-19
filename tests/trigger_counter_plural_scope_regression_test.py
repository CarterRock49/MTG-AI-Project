"""Regressions for counter-qualified deaths and plural attack watchers."""

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
from Playersim.ability_types import TriggeredAbility  # noqa: E402


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


class TriggerCounterPluralScopeRegressionTest(unittest.TestCase):
    @staticmethod
    def _matching_triggers(game_state, source_id: int, phrase: str):
        phrase = phrase.casefold()
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and phrase in entry[0].trigger_condition.casefold()
        ]

    def _death_trigger_count(
            self, game_state, owner, source_id: int, phrase: str,
            name: str, *, plus_one_counter: bool) -> int:
        event_id = inject_into_zone(
            game_state, owner, _creature(name), "battlefield")
        if plus_one_counter:
            game_state._safe_get_card(event_id).counters["+1/+1"] = 1
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            event_id,
            owner,
            "battlefield",
            owner,
            "graveyard",
            cause="destroy",
        ))
        queued = self._matching_triggers(
            game_state, source_id, phrase)
        if queued:
            self.assertEqual(queued[0][2]["event_card_id"], event_id)
        return len(queued)

    def test_counter_qualified_death_watchers_use_controller_and_lki(self):
        fixtures = (
            (36601, "Meltstrider Eulogist"),
            (36602, "Rayblade Trooper"),
            (36603, "Explorer's Cache"),
        )
        phrase = "creature you control with a +1/+1 counter on it dies"

        for seed, card_name in fixtures:
            with self.subTest(card=card_name):
                game_state = fresh(seed)
                controller, opponent = game_state.p1, game_state.p2
                source_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                game_state.ability_handler.active_triggers = []
                game_state.stack = []

                observed = {
                    "controlled_without_counter": self._death_trigger_count(
                        game_state,
                        controller,
                        source_id,
                        phrase,
                        "Controlled Counterless Death",
                        plus_one_counter=False,
                    ),
                    "opponent_with_counter": self._death_trigger_count(
                        game_state,
                        opponent,
                        source_id,
                        phrase,
                        "Opponent Countered Death",
                        plus_one_counter=True,
                    ),
                    "controlled_with_counter": self._death_trigger_count(
                        game_state,
                        controller,
                        source_id,
                        phrase,
                        "Controlled Countered Death",
                        plus_one_counter=True,
                    ),
                }
                self.assertEqual(observed, {
                    "controlled_without_counter": 0,
                    "opponent_with_counter": 0,
                    "controlled_with_counter": 1,
                })

    def test_ancestor_dragon_accepts_one_controlled_attacker_only(self):
        game_state = fresh(36611)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Ancestor Dragon", "battlefield")
        phrase = "one or more creatures you control attack"

        observed = {}
        for owner, label in (
                (opponent, "opponent"),
                (controller, "controlled")):
            attacker_id = inject_into_zone(
                game_state,
                owner,
                _creature(f"{label.title()} Attacker"),
                "battlefield",
            )
            game_state.ability_handler.active_triggers = []
            game_state.current_attackers = [attacker_id]
            game_state.ability_handler.check_abilities(
                attacker_id,
                "ATTACKS",
                {
                    "controller": owner,
                    "event_controller": owner,
                    "attacker_id": attacker_id,
                    "attacking_player": owner,
                },
            )
            queued = self._matching_triggers(
                game_state, source_id, phrase)
            observed[label] = len(queued)
            if queued:
                self.assertEqual(
                    queued[0][2]["event_card_id"], attacker_id)

        self.assertEqual(observed, {"opponent": 0, "controlled": 1})

        first_id = inject_into_zone(
            game_state, controller, _creature("First Group Attacker"),
            "battlefield")
        second_id = inject_into_zone(
            game_state, controller, _creature("Second Group Attacker"),
            "battlefield")
        game_state.current_attackers = [first_id, second_id]
        game_state.ability_handler.active_triggers = []
        for attacker_id in game_state.current_attackers:
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
        grouped = self._matching_triggers(game_state, source_id, phrase)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0][2]["matching_attacker_ids"],
                         [first_id, second_id])
        self.assertEqual(grouped[0][2]["attacker_count"], 2)

    def test_death_lki_preserves_keyword_qualifiers(self):
        game_state = fresh(36612)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "The Serpent Society", "battlefield")
        phrase = "creature you control with deathtouch dies"

        def kill(owner, name, deathtouch):
            event_id = inject_into_zone(
                game_state, owner, _creature(name), "battlefield")
            if deathtouch:
                card = game_state._safe_get_card(event_id)
                card.keywords[card.ALL_KEYWORDS.index("deathtouch")] = 1
            game_state.ability_handler.active_triggers = []
            self.assertTrue(game_state.move_card(
                event_id, owner, "battlefield", owner, "graveyard",
                cause="destroy"))
            return len(self._matching_triggers(
                game_state, source_id, phrase))

        self.assertEqual(kill(controller, "No Deathtouch", False), 0)
        self.assertEqual(kill(opponent, "Opposing Deathtouch", True), 0)
        self.assertEqual(kill(controller, "Controlled Deathtouch", True), 1)

    def test_death_lki_honors_power_or_toughness_comparison(self):
        game_state = fresh(36613)
        controller, opponent = game_state.p1, game_state.p2
        source_id = inject_real_card(
            game_state, controller, "Arnyn, Deathbloom Botanist",
            "battlefield")
        phrase = "power or toughness 1 or less dies"

        def kill(owner, name, power, toughness):
            data = _creature(name)
            data.update({"power": power, "toughness": toughness})
            event_id = inject_into_zone(
                game_state, owner, data, "battlefield")
            game_state.ability_handler.active_triggers = []
            self.assertTrue(game_state.move_card(
                event_id, owner, "battlefield", owner, "graveyard",
                cause="destroy"))
            return len(self._matching_triggers(
                game_state, source_id, phrase))

        self.assertEqual(kill(controller, "Large Creature", 2, 2), 0)
        self.assertEqual(kill(opponent, "Opposing Small Power", 1, 4), 0)
        self.assertEqual(kill(controller, "Small Power", 1, 4), 1)
        self.assertEqual(kill(controller, "Small Toughness", 4, 1), 1)

    def test_taigam_master_opportunist_comma_boundary(self):
        text = (
            "Whenever you cast your second spell each turn, copy it, then "
            "exile the spell you cast with four time counters on it. If it "
            "doesn't have suspend, it gains suspend."
        )
        self.assertEqual(
            TriggeredAbility._parse_condition_effect(None, text),
            (
                "Whenever you cast your second spell each turn",
                "copy it, then exile the spell you cast with four time "
                "counters on it. If it doesn't have suspend, it gains "
                "suspend",
            ),
        )


if __name__ == "__main__":
    unittest.main()
