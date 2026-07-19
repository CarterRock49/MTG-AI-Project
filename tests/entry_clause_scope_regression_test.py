"""Regressions for clause-level ETB identity, controller, and qualifiers."""

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


def _permanent(name: str, type_line: str, *, power: int = 2,
               toughness: int = 2) -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": type_line,
        "oracle_text": "",
        "colors": [],
        "power": power,
        "toughness": toughness,
    }


class EntryClauseScopeRegressionTest(unittest.TestCase):
    @staticmethod
    def _state(seed: int, card_name: str):
        game_state = fresh(seed)
        source_id = inject_real_card(
            game_state, game_state.p1, card_name, "battlefield")
        game_state.ability_handler.active_triggers = []
        game_state.stack = []
        return game_state, source_id

    @staticmethod
    def _event(game_state, owner, type_line="Creature - Test", *,
               power=2, toughness=2, zone="battlefield"):
        return inject_into_zone(
            game_state, owner,
            _permanent("ETB Scope Fixture", type_line,
                       power=power, toughness=toughness),
            zone)

    @staticmethod
    def _dispatch(game_state, source_id, event_id, event_controller,
                  *, from_zone="hand", **extra):
        game_state.ability_handler.active_triggers = []
        game_state.ability_handler.check_abilities(
            event_id,
            "ENTERS_BATTLEFIELD",
            {
                "controller": event_controller,
                "event_controller": event_controller,
                "from_zone": from_zone,
                "to_zone": "battlefield",
                **extra,
            },
        )
        return [
            row for row in game_state.ability_handler.active_triggers
            if row[0].card_id == source_id]

    @staticmethod
    def _complete_etb_batch(event_id, event_controller, *,
                            from_zone="hand", batch_id="entry-scope-batch"):
        return {
            "id": batch_id,
            "complete": True,
            "event_type": "ENTERS_BATTLEFIELD",
            "primary_event_card_id": event_id,
            "matching_events": [{
                "event_card_id": event_id,
                "event_controller": event_controller,
                "from_zone": from_zone,
                "to_zone": "battlefield",
            }],
        }

    def test_named_and_plural_self_entries_reject_unrelated_objects(self):
        names = (
            "Ka-Zar of the Savage Land",
            "Cloak and Dagger, Entwined",
            "Krang & Shredder",
            "Lo and Li, Twin Tutors",
            "Ran and Shaw",
            "Sharae of Numbing Depths",
            "SP//dr, Piloted by Peni",
        )
        for offset, card_name in enumerate(names):
            with self.subTest(card_name=card_name):
                game_state, source_id = self._state(37100 + offset, card_name)
                opponent_event = self._event(
                    game_state, game_state.p2, "Artifact")
                self.assertEqual(self._dispatch(
                    game_state, source_id, opponent_event, game_state.p2), [])

                provenance = {}
                if card_name == "Ran and Shaw":
                    for index in range(3):
                        self._event(
                            game_state, game_state.p1,
                            "Creature - Dragon", zone="graveyard")
                    provenance = {
                        "was_cast": True,
                        "cast_controller_id": "p1",
                        "final_paid_cost": {"R": 4},
                    }
                self.assertEqual(len(self._dispatch(
                    game_state, source_id, source_id, game_state.p1,
                    **provenance)), 1)

    def test_chosen_type_watchers_isolate_type_and_controller(self):
        for offset, card_name in enumerate(
                ("Dawn-Blessed Pennant", "Rimefire Torque")):
            with self.subTest(card_name=card_name):
                game_state, source_id = self._state(37120 + offset, card_name)
                game_state.p1.setdefault(
                    "chosen_creature_types", {})[source_id] = "elf"
                own_elf = self._event(
                    game_state, game_state.p1, "Creature - Elf")
                own_goblin = self._event(
                    game_state, game_state.p1, "Creature - Goblin")
                opponent_elf = self._event(
                    game_state, game_state.p2, "Creature - Elf")
                self.assertEqual(len(self._dispatch(
                    game_state, source_id, own_elf, game_state.p1)), 1)
                self.assertEqual(self._dispatch(
                    game_state, source_id, own_goblin, game_state.p1), [])
                self.assertEqual(self._dispatch(
                    game_state, source_id, opponent_elf, game_state.p2), [])

    def test_combined_etb_clauses_evaluate_each_arm_independently(self):
        game_state, source_id = self._state(
            37130, "Giott, King of the Dwarves")
        dwarf = self._event(
            game_state, game_state.p1, "Creature - Dwarf")
        equipment = self._event(
            game_state, game_state.p1, "Artifact - Equipment")
        opponent_equipment = self._event(
            game_state, game_state.p2, "Artifact - Equipment")
        self.assertEqual(len(self._dispatch(
            game_state, source_id, dwarf, game_state.p1)), 1)
        self.assertEqual(len(self._dispatch(
            game_state, source_id, equipment, game_state.p1)), 1)
        self.assertEqual(self._dispatch(
            game_state, source_id, opponent_equipment, game_state.p2), [])

        game_state, source_id = self._state(37131, "Fire Lord Zuko")
        own_event = self._event(game_state, game_state.p1)
        opponent_event = self._event(game_state, game_state.p2)
        self.assertEqual(len(self._dispatch(
            game_state, source_id, own_event, game_state.p1,
            from_zone="exile")), 1)
        self.assertEqual(self._dispatch(
            game_state, source_id, own_event, game_state.p1,
            from_zone="hand"), [])
        self.assertEqual(self._dispatch(
            game_state, source_id, opponent_event, game_state.p2,
            from_zone="exile"), [])

    def test_entry_origin_and_source_counter_qualifiers_are_enforced(self):
        game_state, source_id = self._state(37140, "Oscorp Industries")
        self.assertEqual(self._dispatch(
            game_state, source_id, source_id, game_state.p1,
            from_zone="hand"), [])
        self.assertEqual(len(self._dispatch(
            game_state, source_id, source_id, game_state.p1,
            from_zone="graveyard")), 1)

        for offset, card_name in enumerate(
                ("Bristlebane Battler", "Reluctant Dounguard")):
            with self.subTest(card_name=card_name):
                game_state, source_id = self._state(37141 + offset, card_name)
                source = game_state._safe_get_card(source_id)
                source.counters.clear()
                event_id = self._event(game_state, game_state.p1)
                self.assertEqual(self._dispatch(
                    game_state, source_id, event_id, game_state.p1), [])
                source.counters["-1/-1"] = 1
                self.assertEqual(len(self._dispatch(
                    game_state, source_id, event_id, game_state.p1)), 1)

    def test_relative_pt_compound_subject_is_ordinary_per_object(self):
        game_state, source_id = self._state(37150, "Fecund Greenshell")
        legal = self._event(
            game_state, game_state.p1, power=1, toughness=4)
        reversed_pt = self._event(
            game_state, game_state.p1, power=4, toughness=1)
        opponent = self._event(
            game_state, game_state.p2, power=1, toughness=4)
        self.assertEqual(len(self._dispatch(
            game_state, source_id, legal, game_state.p1)), 1)
        self.assertEqual(self._dispatch(
            game_state, source_id, reversed_pt, game_state.p1), [])
        self.assertEqual(self._dispatch(
            game_state, source_id, opponent, game_state.p2), [])

    def test_satoru_grouped_compound_subject_requires_complete_batch(self):
        game_state, source_id = self._state(
            37151, "Satoru, the Infiltrator")
        own_other = self._event(game_state, game_state.p1)
        opponent_other = self._event(game_state, game_state.p2)

        # "Satoru and/or one or more other ... enter" describes one
        # simultaneous group. Per-object broadcasts therefore fail closed.
        self.assertEqual(self._dispatch(
            game_state, source_id, source_id, game_state.p1), [])
        self.assertEqual(self._dispatch(
            game_state, source_id, own_other, game_state.p1), [])

        self.assertEqual(len(self._dispatch(
            game_state, source_id, source_id, game_state.p1,
            zone_change_batch=self._complete_etb_batch(
                source_id, game_state.p1, batch_id="satoru-self"))), 1)
        self.assertEqual(len(self._dispatch(
            game_state, source_id, own_other, game_state.p1,
            zone_change_batch=self._complete_etb_batch(
                own_other, game_state.p1, batch_id="satoru-other"))), 1)
        self.assertEqual(self._dispatch(
            game_state, source_id, opponent_other, game_state.p2,
            zone_change_batch=self._complete_etb_batch(
                opponent_other, game_state.p2,
                batch_id="satoru-opponent")), [])

    def test_extraordinary_journey_requires_exile_provenance(self):
        def observed(seed, *, complete_batch=False, **context):
            game_state, source_id = self._state(
                seed, "Extraordinary Journey")
            event_id = self._event(game_state, game_state.p2)
            if complete_batch:
                context["zone_change_batch"] = self._complete_etb_batch(
                    event_id, game_state.p2,
                    from_zone=context.get("from_zone", "hand"),
                    batch_id=f"journey-{seed}")
            return len(self._dispatch(
                game_state, source_id, event_id, game_state.p2, **context))

        self.assertEqual(observed(37160, from_zone="hand"), 0)
        self.assertEqual(observed(37161, from_zone="exile"), 0)
        self.assertEqual(observed(
            37162, complete_batch=True, from_zone="hand"), 0)
        self.assertEqual(observed(
            37163, complete_batch=True, from_zone="exile"), 1)
        self.assertEqual(observed(
            37164, complete_batch=True, from_zone="stack", was_cast=True,
            source_zone="exile"), 1)


if __name__ == "__main__":
    unittest.main()
