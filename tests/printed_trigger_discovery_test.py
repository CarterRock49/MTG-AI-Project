"""Regressions for independent lexical printed-trigger discovery."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.printed_trigger_discovery import (  # noqa: E402
    discover_printed_trigger_inventory,
    discover_printed_triggers,
)


class PrintedTriggerDiscoveryTest(unittest.TestCase):
    CARD_NAMES = {
        "Stormchaser's Talent",
        "Manifold Mouse",
        "Pawpatch Recruit",
        "Great Hall of the Biblioplex",
        "Aang, at the Crossroads // Aang, Destined Savior",
        "Avatar Aang // Aang, Master of Elements",
        "Aang, Swift Savior // Aang and La, Ocean's Fury",
        "Roaring Furnace // Steaming Sauna",
        "Jennifer Walters // The Sensational She-Hulk",
        "Builder's Talent",
        "Valley Questcaller",
    }

    @classmethod
    def setUpClass(cls):
        snapshot = REPO_ROOT / "Format Card Lists" / "standard.jsonl"
        cls.cards = {}
        with snapshot.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line)
                if raw.get("name") in cls.CARD_NAMES:
                    cls.cards[raw["name"]] = raw
                if len(cls.cards) == len(cls.CARD_NAMES):
                    break
        missing = cls.CARD_NAMES.difference(cls.cards)
        if missing:
            raise AssertionError(
                "standard trigger fixtures missing: " + ", ".join(sorted(missing)))

    def test_stormchasers_talent_has_all_three_printed_triggers(self):
        triggers = discover_printed_triggers(
            self.cards["Stormchaser's Talent"])

        self.assertEqual(len(triggers), 3)
        self.assertEqual(
            [row["trigger_condition_prefix"] for row in triggers],
            [
                "When this Class enters",
                "When this Class becomes level 2",
                "Whenever you cast an instant or sorcery spell",
            ],
        )
        self.assertTrue(all(row["surface"] == "top_level" for row in triggers))
        self.assertTrue(all(row["discovery"] == "explicit" for row in triggers))

    def test_real_comma_heavy_subjects_retain_the_event_verb(self):
        cases = {
            "Builder's Talent": (
                "Whenever one or more noncreature, nonland permanents you "
                "control enter"),
            "Valley Questcaller": (
                "Whenever one or more other Rabbits, Bats, Birds, and/or "
                "Mice you control enter"),
        }
        for name, expected in cases.items():
            with self.subTest(card=name):
                triggers = discover_printed_triggers(self.cards[name])
                self.assertIn(
                    expected,
                    [row["trigger_condition_prefix"] for row in triggers])

    def test_named_comma_and_subtype_list_are_not_effect_boundaries(self):
        fixtures = {
            "Named Comma Fixture": (
                "Whenever Captain Arlen, Sky Marshal attacks, draw a card.",
                "Whenever Captain Arlen, Sky Marshal attacks"),
            "Subtype List Fixture": (
                "Whenever another Rabbit, Bat, Bird, or Mouse you control "
                "enters, scry 1.",
                "Whenever another Rabbit, Bat, Bird, or Mouse you control "
                "enters"),
        }
        for name, (oracle_text, expected) in fixtures.items():
            with self.subTest(card=name):
                rows = discover_printed_triggers({
                    "name": name, "oracle_id": name, "oracle_text": oracle_text,
                })
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["trigger_condition_prefix"], expected)

    def test_thousands_separator_is_not_a_grammar_boundary(self):
        rows = discover_printed_triggers({
            "name": "Thousands Fixture", "oracle_id": "thousands-fixture",
            "oracle_text": "Whenever you gain 1,000 life, draw a card.",
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["trigger_condition_prefix"],
            "Whenever you gain 1,000 life")

    def test_offspring_is_an_explicit_synthetic_trigger_obligation(self):
        for name in ("Manifold Mouse", "Pawpatch Recruit"):
            with self.subTest(card=name):
                triggers = discover_printed_triggers(self.cards[name])
                offspring = [
                    row for row in triggers
                    if row["discovery"] == "keyword_offspring"
                ]
                self.assertEqual(len(offspring), 1)
                self.assertTrue(offspring[0]["synthetic"])
                self.assertEqual(offspring[0]["offspring_cost"], "{2}")
                self.assertEqual(
                    offspring[0]["trigger_condition_prefix"],
                    "When this permanent enters, if its offspring cost was paid",
                )

        keyword_only = {
            "name": "Reminder-Stripped Offspring",
            "oracle_id": "offspring-fixture",
            "oracle_text": "",
            "keywords": "Offspring",
        }
        triggers = discover_printed_triggers(keyword_only)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["discovery"], "keyword_offspring")

    def test_directly_granted_quoted_trigger_is_discovered(self):
        triggers = discover_printed_triggers(
            self.cards["Great Hall of the Biblioplex"])

        self.assertEqual(len(triggers), 1)
        trigger = triggers[0]
        self.assertEqual(trigger["discovery"], "granted_quoted")
        self.assertEqual(
            trigger["trigger_condition_prefix"],
            "Whenever you cast an instant or sorcery spell",
        )
        self.assertTrue(trigger["source_text"].startswith("Whenever you cast"))

        chained_grant = {
            "name": "Chained Grant Fixture",
            "oracle_text": (
                'Target creature gains flying and "Whenever this creature '
                'attacks, draw a card."'),
            "keywords": [],
        }
        granted = discover_printed_triggers(chained_grant)
        self.assertEqual(len(granted), 1)
        self.assertEqual(granted[0]["discovery"], "granted_quoted")

    def test_named_ability_prefix_is_a_line_boundary(self):
        raw = {
            "name": "Named Trigger Fixture",
            "oracle_text": (
                "A Test of Your Reflexes! — When this artifact enters, "
                "draw a card."),
            "keywords": [],
        }

        triggers = discover_printed_triggers(raw)

        self.assertEqual(len(triggers), 1)
        self.assertEqual(
            triggers[0]["trigger_condition_prefix"],
            "When this artifact enters")

    def test_multiface_inventory_preserves_face_identity(self):
        cases = {
            "Aang, at the Crossroads // Aang, Destined Savior": [0, 0, 1],
            "Avatar Aang // Aang, Master of Elements": [0, 1],
            "Aang, Swift Savior // Aang and La, Ocean's Fury": [0, 1],
            "Roaring Furnace // Steaming Sauna": [0, 1],
            "Jennifer Walters // The Sensational She-Hulk": [1],
        }
        for name, expected_faces in cases.items():
            with self.subTest(card=name):
                triggers = discover_printed_triggers(self.cards[name])
                self.assertEqual(
                    [row["face_index"] for row in triggers], expected_faces)
                self.assertTrue(all(row["surface"] == "card_face"
                                    for row in triggers))

        room_triggers = discover_printed_triggers(
            self.cards["Roaring Furnace // Steaming Sauna"])
        self.assertEqual(room_triggers[1]["face_name"], "Steaming Sauna")
        jennifer_trigger = discover_printed_triggers(
            self.cards["Jennifer Walters // The Sensational She-Hulk"])[0]
        self.assertEqual(
            jennifer_trigger["face_name"], "The Sensational She-Hulk")

    def test_reminder_trigger_candidates_are_diagnostic_not_obligations(self):
        inventory = discover_printed_trigger_inventory(
            self.cards["Aang, at the Crossroads // Aang, Destined Savior"])

        conditions = [row["trigger_condition_prefix"]
                      for row in inventory["triggers"]]
        self.assertNotIn("When it dies or is exiled", conditions)
        reminder = [
            row for row in inventory["unmatched_lexical_surfaces"]
            if row["reason"] == "reminder_text"
        ]
        self.assertEqual(len(reminder), 1)
        self.assertIn("When it dies or is exiled", reminder[0]["source_text"])
        self.assertTrue(reminder[0]["id"].startswith(
            "unmatched-trigger-lexeme:"))
        self.assertEqual(len(reminder[0]["sha256"]), 64)

    def test_selected_entry_is_deterministic_and_not_mutated(self):
        raw = self.cards["Stormchaser's Talent"]
        selected = {"index": 42, "name": raw["name"], "raw": copy.deepcopy(raw)}
        before = copy.deepcopy(selected)

        first = discover_printed_trigger_inventory(selected)
        second = discover_printed_trigger_inventory(selected)

        self.assertEqual(first, second)
        self.assertEqual(selected, before)
        self.assertEqual(len(first["sha256"]), 64)
        self.assertEqual(len({row["id"] for row in first["triggers"]}), 3)

    def test_full_frozen_pool_has_3124_deterministic_trigger_records(self):
        snapshot = REPO_ROOT / "Format Card Lists" / "standard.jsonl"
        trigger_ids = set()
        trigger_count = 0
        with snapshot.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                raw = json.loads(line)
                first = discover_printed_trigger_inventory(raw)
                second = discover_printed_trigger_inventory(raw)
                self.assertEqual(first, second, f"snapshot line {line_number}")
                trigger_count += len(first["triggers"])
                for row in first["triggers"]:
                    self.assertNotIn(row["id"], trigger_ids)
                    trigger_ids.add(row["id"])

        self.assertEqual(trigger_count, 3124)
        self.assertEqual(len(trigger_ids), 3124)


if __name__ == "__main__":
    unittest.main()
