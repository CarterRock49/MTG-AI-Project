"""Regressions for cumulative Class ability registration."""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
import unittest
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_real_card  # noqa: E402
from Playersim.ability_types import (  # noqa: E402
    ActivatedAbility,
    StaticAbility,
    TriggeredAbility,
)
from Playersim.card import Card  # noqa: E402


logging.disable(logging.CRITICAL)


class ClassAbilityRegistrationRegressionTest(unittest.TestCase):
    STANDARD_CLASSES = {
        "Artist's Talent",
        "Bandit's Talent",
        "Blacksmith's Talent",
        "Builder's Talent",
        "Caretaker's Talent",
        "Cool but Rude",
        "Does Machines",
        "Gossip's Talent",
        "Hunter's Talent",
        "Innkeeper's Talent",
        "Leader's Talent",
        "Ninja Teen",
        "Party Dude",
        "Scavenger's Talent",
        "Stormchaser's Talent",
    }

    @staticmethod
    def _trigger_conditions(game_state, card_id):
        return [
            ability.trigger_condition
            for ability in game_state.ability_handler.registered_abilities[
                card_id]
            if isinstance(ability, TriggeredAbility)
        ]

    def test_base_grouped_triggers_register_once(self):
        cases = {
            "Caretaker's Talent": (
                "whenever one or more tokens you control enter"),
            "Scavenger's Talent": (
                "whenever one or more creatures you control die"),
        }
        game_state = fresh(38111)

        for card_name, expected_condition in cases.items():
            with self.subTest(card=card_name):
                card_id = inject_real_card(
                    game_state, game_state.p1, card_name, "battlefield")
                card = game_state._safe_get_card(card_id)
                self.assertEqual(card.current_level, 1)
                self.assertEqual(
                    self._trigger_conditions(game_state, card_id),
                    [expected_condition])

                # Re-registration is a replacement, not an append operation.
                game_state.ability_handler._parse_and_register_abilities(
                    card_id, card)
                self.assertEqual(
                    self._trigger_conditions(game_state, card_id),
                    [expected_condition])

    def test_builder_level_two_trigger_requires_authentic_level_up(self):
        game_state = fresh(38112)
        controller = game_state.p1
        card_id = inject_real_card(
            game_state, controller, "Builder's Talent", "battlefield")
        card = game_state._safe_get_card(card_id)
        level_two_condition = (
            "whenever one or more noncreature, nonland permanents you "
            "control enter")

        self.assertEqual(card.current_level, 1)
        self.assertEqual(
            self._trigger_conditions(game_state, card_id),
            ["when this class enters"])
        self.assertNotIn(
            level_two_condition,
            self._trigger_conditions(game_state, card_id))

        controller["mana_pool"]["W"] = 1
        battlefield_index = controller["battlefield"].index(card_id)
        self.assertTrue(game_state.ability_handler.handle_class_level_up(
            battlefield_index, controller=controller))

        self.assertEqual(card.current_level, 2)
        conditions = self._trigger_conditions(game_state, card_id)
        self.assertEqual(
            Counter(conditions),
            Counter({
                "when this class enters": 1,
                level_two_condition: 1,
            }))

    def test_spree_and_tiered_blocks_stay_casting_owned(self):
        game_state = fresh(38113)
        controller = game_state.p1
        for card_name in ("Three Steps Ahead", "Thunder Magic"):
            with self.subTest(card=card_name):
                card_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                registered = game_state.ability_handler.registered_abilities[
                    card_id]
                self.assertFalse(any(
                    isinstance(ability, (ActivatedAbility, TriggeredAbility,
                                         StaticAbility))
                    for ability in registered), registered)

    def test_class_parser_preserves_mana_symbols_inside_ability_rows(self):
        cases = {
            "Blacksmith's Talent": (
                1,
                'When this Class enters, create a colorless Equipment '
                'artifact token named Sword with "Equipped creature gets '
                '+1/+1" and equip {2}.'),
            "Innkeeper's Talent": (
                2,
                "Permanents you control with counters on them have ward "
                "{1}."),
            "Artist's Talent": (
                2,
                "Noncreature spells you cast cost {1} less to cast."),
        }
        game_state = fresh(38115)

        for card_name, (level, expected_row) in cases.items():
            with self.subTest(card=card_name):
                card_id = inject_real_card(
                    game_state, game_state.p1, card_name, "hand")
                card = game_state._safe_get_card(card_id)
                level_data = next(
                    row for row in card.levels if row["level"] == level)
                self.assertEqual(level_data["abilities"], [expected_row])

    def test_restored_mana_rows_register_only_at_their_class_level(self):
        blacksmith_state = fresh(38116)
        blacksmith_id = inject_real_card(
            blacksmith_state, blacksmith_state.p1,
            "Blacksmith's Talent", "battlefield")
        blacksmith_rows = [
            ability.effect_text
            for ability in blacksmith_state.ability_handler.
            registered_abilities[blacksmith_id]
            if isinstance(ability, TriggeredAbility)]
        self.assertEqual(blacksmith_rows, [
            'When this Class enters, create a colorless Equipment artifact '
            'token named Sword with "Equipped creature gets +1/+1" and '
            'equip {2}'])

        level_two_cases = {
            "Innkeeper's Talent": (
                {"G": 1},
                "Permanents you control with counters on them have ward {1}"),
            "Artist's Talent": (
                {"R": 3},
                "Noncreature spells you cast cost {1} less to cast"),
        }
        for offset, (card_name, (mana, expected_row)) in enumerate(
                level_two_cases.items()):
            with self.subTest(card=card_name):
                game_state = fresh(38117 + offset)
                controller = game_state.p1
                card_id = inject_real_card(
                    game_state, controller, card_name, "battlefield")
                self.assertNotIn(
                    expected_row,
                    [ability.effect_text for ability in
                     game_state.ability_handler.registered_abilities[
                         card_id]])

                for color, amount in mana.items():
                    controller["mana_pool"][color] = amount
                class_index = controller["battlefield"].index(card_id)
                self.assertTrue(
                    game_state.ability_handler.handle_class_level_up(
                        class_index, controller=controller))

                matching = [
                    ability for ability in
                    game_state.ability_handler.registered_abilities[card_id]
                    if isinstance(ability, StaticAbility)
                    and ability.effect_text == expected_row]
                self.assertEqual(len(matching), 1)

    def test_every_standard_class_level_registers_each_unlocked_row_once(self):
        snapshot = REPO_ROOT / "Format Card Lists" / "standard.jsonl"
        raw_classes = []
        with snapshot.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line)
                if "Class" in raw.get("type_line", ""):
                    raw_classes.append(raw)

        self.assertEqual(
            {raw["name"] for raw in raw_classes}, self.STANDARD_CLASSES)
        game_state = fresh(38114)
        registered_row_total = 0
        replacement_row_total = 0

        for offset, raw in enumerate(raw_classes):
            card_id = 920000 + offset
            card = Card(copy.deepcopy(raw))
            card.card_id = card_id
            card.game_state = game_state
            game_state.card_db[card_id] = card

            for level in (1, 2, 3):
                with self.subTest(card=card.name, level=level):
                    card.current_level = level
                    card._consolidate_abilities()
                    game_state.ability_handler._parse_and_register_abilities(
                        card_id, card)

                    unlocked_rows = [
                        text.strip().rstrip(".")
                        for text in card.all_abilities]
                    replacement_rows = [
                        text for text in unlocked_rows
                        if re.match(
                            r"^if\b.*\bwould\b.*\binstead$",
                            text, re.IGNORECASE)]
                    expected_rows = [
                        text for text in unlocked_rows
                        if text not in replacement_rows]
                    registered_rows = [
                        ability.effect_text.strip().rstrip(".")
                        for ability in
                        game_state.ability_handler.registered_abilities[
                            card_id]
                        if isinstance(
                            ability,
                            (ActivatedAbility, TriggeredAbility,
                             StaticAbility))]

                    self.assertEqual(
                        Counter(registered_rows), Counter(expected_rows))
                    self.assertEqual(
                        len(registered_rows), len(set(registered_rows)),
                        "Class re-registration duplicated an unlocked row")
                    registered_row_total += len(registered_rows)
                    replacement_row_total += len(replacement_rows)

        # 15 cards x cumulative levels (1 + 2 + 3) = 90 rows. The two
        # level-3 if/would/instead rows are intentionally owned by the
        # ReplacementEffectSystem, leaving 88 AbilityHandler registrations.
        self.assertEqual(registered_row_total, 88)
        self.assertEqual(replacement_row_total, 2)


if __name__ == "__main__":
    unittest.main()
