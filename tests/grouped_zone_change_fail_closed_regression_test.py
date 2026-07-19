"""Fail-closed regressions for grouped ETB and death triggers."""

from __future__ import annotations

from collections import Counter
import json
import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_into_zone  # noqa: E402
from Playersim.ability_types import (  # noqa: E402
    _is_grouped_zone_change_trigger,
)


logging.disable(logging.CRITICAL)


def _card(name, type_line, oracle_text="", power=2, toughness=2):
    return {
        "name": name, "mana_cost": "{1}", "cmc": 1,
        "type_line": type_line, "oracle_text": oracle_text,
        "colors": [0, 0, 0, 0, 0],
        "power": power, "toughness": toughness,
    }


class GroupedZoneChangeFailClosedRegressionTest(unittest.TestCase):
    @staticmethod
    def _queued(game_state, source_id):
        return [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id]

    def test_detector_distinguishes_group_subject_from_counter_qualifier(self):
        grouped = (
            ("Whenever one or more noncreature, nonland permanents you "
             "control enter", "ENTERS_BATTLEFIELD"),
            ("Whenever Satoru and/or one or more other nontoken creatures "
             "you control enter", "ENTERS_BATTLEFIELD"),
            ("Whenever one or more other Rabbits, Bats, Birds, and/or Mice "
             "you control enter", "ENTERS_BATTLEFIELD"),
            ("Whenever one or more other creatures and/or artifacts you "
             "control die", "DIES"),
        )
        for condition, event_type in grouped:
            with self.subTest(condition=condition):
                self.assertTrue(_is_grouped_zone_change_trigger(
                    condition, event_type))

        singular = (
            "Whenever a creature you control with one or more counters on "
            "it dies")
        self.assertFalse(_is_grouped_zone_change_trigger(singular, "DIES"))
        self.assertFalse(_is_grouped_zone_change_trigger(
            "Whenever one or more creatures you control attack", "DIES"))

        ordinary = (
            ("When this creature enters the battlefield",
             "ENTERS_BATTLEFIELD"),
            ("Whenever another creature dies", "DIES"),
            ("Whenever a creature you control with one or more counters on "
             "it dies", "DIES"),
        )
        for condition, event_type in ordinary:
            with self.subTest(ordinary_condition=condition):
                self.assertFalse(_is_grouped_zone_change_trigger(
                    condition, event_type))

    def test_ordinary_etb_and_death_triggers_need_identity_not_batch(self):
        game_state = fresh(36700)
        controller = game_state.p1
        etb_source = inject_into_zone(game_state, controller, _card(
            "Ordinary Entry Creature", "Creature - Test",
            "When this creature enters, draw a card."), "battlefield")
        death_source = inject_into_zone(game_state, controller, _card(
            "Ordinary Death Watcher", "Enchantment",
            "Whenever another creature dies, draw a card."), "battlefield")
        dying = inject_into_zone(game_state, controller, _card(
            "Ordinary Dying Creature", "Creature - Test"), "battlefield")
        game_state.ability_handler.active_triggers = []

        game_state.ability_handler.check_abilities(
            etb_source, "ENTERS_BATTLEFIELD", {
                "controller": controller, "event_controller": controller,
                "from_zone": "hand", "to_zone": "battlefield",
            })
        self.assertEqual(len(self._queued(game_state, etb_source)), 1)

        self.assertTrue(game_state.move_card(
            dying, controller, "battlefield", controller, "graveyard",
            cause="destroy"))
        self.assertEqual(len(self._queued(game_state, death_source)), 1)

        ordinary_compound = (
            "Whenever this creature or another creature you control with "
            "toughness greater than its power enters",
            "Whenever a Dwarf or Equipment you control enters",
            "Whenever this creature or another nontoken creature you "
            "control enters",
        )
        for condition in ordinary_compound:
            with self.subTest(ordinary_condition=condition):
                self.assertFalse(_is_grouped_zone_change_trigger(
                    condition, "ENTERS_BATTLEFIELD"))

    def test_frozen_pool_grouped_inventory_is_exactly_22_clauses_20_cards(self):
        rows = []
        snapshot = REPO_ROOT / "Format Card Lists" / "standard.jsonl"
        with snapshot.open("r", encoding="utf-8") as handle:
            for line in handle:
                card = json.loads(line)
                faces = card.get("card_faces") or []
                surfaces = (
                    [face.get("oracle_text", "") for face in faces]
                    if faces else [card.get("oracle_text", "")])
                for surface in surfaces:
                    for condition in str(surface or "").splitlines():
                        for event_type in ("ENTERS_BATTLEFIELD", "DIES"):
                            if _is_grouped_zone_change_trigger(
                                    condition, event_type):
                                rows.append((
                                    card["name"], condition, event_type))

        expected_cards = {
            "Baron Bertram Graywater", "Blood Spatter Analysis",
            "Builder's Talent", "Caretaker's Talent", "Chainsaw",
            "Elvish Archivist", "Enduring Innocence",
            "Extraordinary Journey", "Frantic Scapegoat", "G'raha Tia",
            "Homicide Investigator", "Kambal, Profiteering Mayor",
            "Mister Fantastic, Reed Richards", "Satoru, the Infiltrator",
            "Scavenger's Talent", "Spiritcall Enthusiast // Scrollboost",
            "The Skullspore Nexus", "Twilight Diviner",
            "Valley Questcaller", "Vengeful Townsfolk",
        }
        counts = Counter(name for name, _, _ in rows)
        self.assertEqual(len(rows), 22)
        self.assertEqual(set(counts), expected_cards)
        self.assertEqual(
            {name for name, count in counts.items() if count == 2},
            {"Elvish Archivist", "Kambal, Profiteering Mayor"})
        self.assertTrue(all(count in {1, 2} for count in counts.values()))

    def test_sequential_runtime_broadcasts_fail_closed_without_batch(self):
        game_state = fresh(36701)
        controller = game_state.p1
        etb_source = inject_into_zone(game_state, controller, _card(
            "Grouped Entry Watcher", "Enchantment",
            "Whenever one or more artifacts you control enter, draw a card."),
            "battlefield")
        death_source = inject_into_zone(game_state, controller, _card(
            "Grouped Death Watcher", "Enchantment",
            "Whenever one or more creatures die, draw a card."),
            "battlefield")
        game_state.ability_handler.active_triggers = []

        for index in range(2):
            artifact_id = inject_into_zone(
                game_state, controller,
                _card(f"Sequential Artifact {index}", "Artifact"), "hand")
            self.assertTrue(game_state.move_card(
                artifact_id, controller, "hand", controller, "battlefield"))
            creature_id = inject_into_zone(
                game_state, controller,
                _card(f"Sequential Creature {index}", "Creature - Test"),
                "battlefield")
            self.assertTrue(game_state.move_card(
                creature_id, controller, "battlefield", controller,
                "graveyard", cause="destroy"))

        self.assertEqual(self._queued(game_state, etb_source), [])
        self.assertEqual(self._queued(game_state, death_source), [])

    def test_complete_etb_batch_queues_only_for_canonical_member(self):
        game_state = fresh(36702)
        controller = game_state.p1
        source_id = inject_into_zone(game_state, controller, _card(
            "Grouped Entry Watcher", "Enchantment",
            "Whenever one or more artifacts you control enter, draw a card."),
            "battlefield")
        event_ids = [inject_into_zone(
            game_state, controller, _card(f"Batch Artifact {index}", "Artifact"),
            "battlefield") for index in range(2)]
        game_state.ability_handler.active_triggers = []
        batch = {
            "id": "etb-batch-1", "complete": True,
            "event_type": "ENTERS_BATTLEFIELD",
            "primary_event_card_id": event_ids[0],
            "matching_events": [
                {"event_card_id": event_id, "event_controller": controller,
                 "from_zone": "hand", "to_zone": "battlefield"}
                for event_id in event_ids],
        }

        for event_id in event_ids:
            game_state.ability_handler.check_abilities(
                event_id, "ENTERS_BATTLEFIELD", {
                    "controller": controller, "event_controller": controller,
                    "from_zone": "hand", "to_zone": "battlefield",
                    "zone_change_batch": batch,
                })

        queued = self._queued(game_state, source_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(
            queued[0][2]["matching_event_card_ids"], tuple(event_ids))
        self.assertEqual(queued[0][2]["zone_change_event_count"], 2)

    def test_complete_death_batch_preserves_every_last_known_object(self):
        game_state = fresh(36703)
        controller = game_state.p1
        source_id = inject_into_zone(game_state, controller, _card(
            "Grouped Death Watcher", "Enchantment",
            "Whenever one or more creatures die, draw a card."),
            "battlefield")
        event_ids = [inject_into_zone(
            game_state, controller,
            _card(f"Batch Creature {index}", "Creature - Test", power=index + 2),
            "battlefield") for index in range(2)]
        last_known = [
            game_state._snapshot_battlefield_object(event_id, controller)
            for event_id in event_ids]
        game_state.ability_handler.active_triggers = []
        batch = {
            "id": "death-batch-1", "complete": True,
            "event_type": "DIES",
            "primary_event_card_id": event_ids[0],
            "matching_events": [
                {"event_card_id": event_id, "event_controller": controller,
                 "last_known": snapshot}
                for event_id, snapshot in zip(event_ids, last_known)],
        }

        for event_id, snapshot in zip(event_ids, last_known):
            game_state.ability_handler.check_abilities(
                event_id, "DIES", {
                    "controller": controller, "event_controller": controller,
                    "from_zone": "battlefield", "to_zone": "graveyard",
                    "last_known": snapshot, "zone_change_batch": batch,
                })

        queued = self._queued(game_state, source_id)
        self.assertEqual(len(queued), 1)
        context = queued[0][2]
        self.assertEqual(context["matching_event_card_ids"], tuple(event_ids))
        self.assertEqual(context["matching_last_known"], tuple(last_known))

    def test_partial_or_unfiltered_batch_contract_fails_closed(self):
        game_state = fresh(36704)
        controller = game_state.p1
        source_id = inject_into_zone(game_state, controller, _card(
            "Grouped Entry Watcher", "Enchantment",
            "Whenever one or more artifacts you control enter, draw a card."),
            "battlefield")
        event_id = inject_into_zone(
            game_state, controller, _card("Partial Artifact", "Artifact"),
            "battlefield")
        game_state.ability_handler.active_triggers = []
        invalid_batches = (
            {"id": "partial", "complete": False,
             "event_type": "ENTERS_BATTLEFIELD",
             "primary_event_card_id": event_id,
             "matching_events": [{"event_card_id": event_id}]},
            {"id": "missing-events", "complete": True,
             "event_type": "ENTERS_BATTLEFIELD",
             "primary_event_card_id": event_id},
        )
        for batch in invalid_batches:
            game_state.ability_handler.check_abilities(
                event_id, "ENTERS_BATTLEFIELD", {
                    "controller": controller, "event_controller": controller,
                    "from_zone": "hand", "to_zone": "battlefield",
                    "zone_change_batch": batch,
                })
        self.assertEqual(self._queued(game_state, source_id), [])


if __name__ == "__main__":
    unittest.main()
