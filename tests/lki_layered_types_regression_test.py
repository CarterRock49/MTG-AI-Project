"""Regression coverage for battlefield last-known layered characteristics."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_into_zone  # noqa: E402


logging.disable(logging.CRITICAL)


def _permanent(name: str, type_line: str, oracle_text: str = "") -> dict:
    creature = "Creature" in type_line
    return {
        "name": name,
        "mana_cost": "{2}",
        "cmc": 2,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "colors": [0, 0, 0, 0, 0],
        "power": 2 if creature else 0,
        "toughness": 2 if creature else 0,
    }


def _normalized(values) -> set[str]:
    return {str(value).lower() for value in (values or [])}


class LastKnownLayeredTypesRegressionTest(unittest.TestCase):
    def test_dies_context_preserves_layer_one_type_characteristics(self):
        game_state = fresh(36401)
        controller = game_state.p1

        watcher_id = inject_into_zone(
            game_state,
            controller,
            _permanent(
                "Layered Death Watcher",
                "Enchantment",
                "Whenever a legendary artifact Goblin creature you control "
                "dies, draw a card.",
            ),
            "battlefield",
        )
        copy_source_id = inject_into_zone(
            game_state,
            controller,
            _permanent(
                "Legendary Copy Source",
                "Legendary Artifact Creature - Goblin Golem",
            ),
            "battlefield",
        )
        subject_id = inject_into_zone(
            game_state,
            controller,
            _permanent("Printed Shrine Subject", "Enchantment - Shrine"),
            "battlefield",
        )

        subject = game_state._safe_get_card(subject_id)
        self.assertEqual(
            _normalized(subject.printed("card_types")), {"enchantment"})
        self.assertEqual(_normalized(subject.printed("subtypes")), {"shrine"})
        self.assertEqual(_normalized(subject.printed("supertypes")), set())

        game_state.layer_system.register_effect({
            "source_id": copy_source_id,
            "layer": 1,
            "affected_ids": [subject_id],
            "effect_type": "copy",
            "effect_value": copy_source_id,
            "duration": "permanent",
        })
        game_state.layer_system.invalidate_cache()
        game_state.layer_system.apply_all_effects()

        expected_types = {"artifact", "creature"}
        expected_subtypes = {"goblin", "golem"}
        expected_supertypes = {"legendary"}
        for characteristic, expected in (
            ("card_types", expected_types),
            ("subtypes", expected_subtypes),
            ("supertypes", expected_supertypes),
        ):
            with self.subTest(characteristic=characteristic):
                self.assertEqual(
                    _normalized(game_state.layer_system.get_characteristic(
                        subject_id, characteristic)),
                    expected,
                )
                self.assertEqual(
                    _normalized(getattr(subject, characteristic)), expected)

        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            subject_id,
            controller,
            "battlefield",
            controller,
            "graveyard",
            cause="destroy",
        ))

        queued = [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == watcher_id
            and "legendary artifact goblin creature you control dies"
            in entry[0].trigger_condition.lower()
        ]
        self.assertEqual(len(queued), 1)
        last_known = queued[0][2]["last_known"]
        self.assertEqual(_normalized(last_known["card_types"]), expected_types)
        self.assertEqual(_normalized(last_known["subtypes"]), expected_subtypes)
        self.assertEqual(
            _normalized(last_known["supertypes"]), expected_supertypes)
        self.assertTrue(last_known["was_creature"])


if __name__ == "__main__":
    unittest.main()
