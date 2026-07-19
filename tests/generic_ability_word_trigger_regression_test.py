"""Safe generic ability-word trigger prefix regressions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from Playersim.ability_types import TriggeredAbility  # noqa: E402
from scenario_test import fresh, inject_into_zone, inject_real_card  # noqa: E402


def _synthetic(name: str, oracle_text: str) -> dict:
    return {
        "name": name,
        "mana_cost": "{2}{W}",
        "cmc": 3,
        "type_line": "Creature - Human Scout",
        "card_types": ["creature"],
        "subtypes": ["Human", "Scout"],
        "oracle_text": oracle_text,
        "power": 2,
        "toughness": 3,
        "colors": [1, 0, 0, 0, 0],
    }


class GenericAbilityWordTriggerRegressionTest(unittest.TestCase):
    @staticmethod
    def _triggers(game_state, card_id):
        return [
            ability for ability in game_state.ability_handler.
            registered_abilities.get(card_id, [])
            if isinstance(ability, TriggeredAbility)
        ]

    def test_real_and_ordinary_named_ability_words_register(self):
        game_state = fresh(1201)
        graha_id = inject_real_card(
            game_state, game_state.p1, "G'raha Tia", "battlefield")
        graha = self._triggers(game_state, graha_id)
        self.assertEqual(len(graha), 1)
        self.assertEqual(graha[0].ability_word, "the allagan eye")
        self.assertTrue(graha[0].trigger_condition.startswith("whenever"))
        self.assertIn("The Allagan Eye", graha[0].oracle_ability_text)

        pack_id = inject_into_zone(
            game_state, game_state.p1,
            _synthetic(
                "Pack Tactics Probe",
                "Pack tactics — Whenever this creature attacks, draw a card."),
            "battlefield")
        pack = self._triggers(game_state, pack_id)
        self.assertEqual(len(pack), 1)
        self.assertEqual(pack[0].ability_word, "pack tactics")
        self.assertEqual(
            pack[0].trigger_condition,
            "whenever this creature attacks")

    def test_modal_and_saga_labels_are_not_registered(self):
        game_state = fresh(1202)
        modal_id = inject_into_zone(
            game_state, game_state.p1,
            _synthetic(
                "Modal Label Probe",
                "Choose one —\n"
                "• Rally — Whenever this creature attacks, draw a card.\n"
                "• You gain 3 life."),
            "battlefield")
        self.assertEqual(self._triggers(game_state, modal_id), [])

        saga_id = inject_into_zone(
            game_state, game_state.p1,
            _synthetic(
                "Saga Label Probe",
                "III — When you next cast a creature spell this turn, "
                "copy that spell."),
            "battlefield")
        self.assertEqual(self._triggers(game_state, saga_id), [])

        for label in ("Solved", "Max speed"):
            with self.subTest(label=label):
                gated_id = inject_into_zone(
                    game_state, game_state.p1,
                    _synthetic(
                        f"{label} State Gate Probe",
                        f"{label} — Whenever this creature attacks, "
                        "draw a card."),
                    "battlefield")
                self.assertEqual(self._triggers(game_state, gated_id), [])

    def test_outer_trigger_does_not_register_quoted_landfall(self):
        game_state = fresh(1203)
        oracle_text = (
            'Whenever this creature attacks, target creature gains '
            '"Landfall — Whenever a land you control enters, draw a card" '
            'until end of turn.')
        quoted_id = inject_into_zone(
            game_state, game_state.p1,
            _synthetic("Quoted Landfall Probe", oracle_text), "battlefield")
        quoted = self._triggers(game_state, quoted_id)
        self.assertEqual(len(quoted), 1)
        self.assertFalse(hasattr(quoted[0], "ability_word"))
        self.assertEqual(
            quoted[0].trigger_condition,
            "whenever this creature attacks")


if __name__ == "__main__":
    unittest.main()
