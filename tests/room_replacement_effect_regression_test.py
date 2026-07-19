"""Room face provenance and locked-door replacement regressions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, inject_into_zone  # noqa: E402


class RoomReplacementEffectRegressionTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        game_state.turn = 1
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.stack = []
        return game_state, game_state.p1

    @staticmethod
    def _room_data():
        return {
            "name": "Doubling Study // Empty Archive",
            "layout": "split",
            "mana_cost": "",
            "cmc": 4,
            "type_line": "Enchantment — Room // Enchantment — Room",
            "oracle_text": "",
            "keywords": [],
            "color_identity": ["U"],
            "card_faces": [
                {
                    "name": "Doubling Study",
                    "mana_cost": "{1}{U}",
                    "type_line": "Enchantment — Room",
                    "oracle_text": (
                        "If you would draw a card, instead draw twice that "
                        "many."),
                },
                {
                    "name": "Empty Archive",
                    "mana_cost": "{3}{U}",
                    "type_line": "Enchantment — Room",
                    "oracle_text": (
                        "If you would draw a card, instead draw no cards."),
                },
            ],
        }

    @staticmethod
    def _ordinary_data():
        return {
            "name": "Ordinary Doubling Study",
            "mana_cost": "{1}{U}",
            "cmc": 2,
            "type_line": "Enchantment",
            "oracle_text": (
                "If you would draw a card, instead draw twice that many."),
            "keywords": [],
            "color_identity": ["U"],
        }

    @staticmethod
    def _source_draw_effects(game_state, source_id):
        return [
            effect for effect
            in game_state.replacement_effects.active_effects
            if (effect.get("source_id") == source_id
                and effect.get("event_type") == "DRAW")
        ]

    def _apply_draw(self, game_state, player):
        return game_state.replacement_effects.apply_replacements(
            "DRAW", {
                "event_type": "DRAW",
                "player": player,
                "draw_count": 1,
            })

    def test_both_faces_register_once_and_only_the_exact_door_applies(self):
        game_state, controller = self._state(55201)
        room_id = inject_into_zone(
            game_state, controller, self._room_data(), "battlefield")
        room = game_state._safe_get_card(room_id)
        replacements = game_state.replacement_effects

        effects = self._source_draw_effects(game_state, room_id)
        self.assertEqual(len(effects), 2)
        self.assertEqual(
            [effect.get("room_face_index") for effect in effects], [0, 1])
        self.assertEqual(
            [effect.get("room_door_number") for effect in effects], [1, 2])
        self.assertEqual(
            [effect.get("room_face_name") for effect in effects],
            ["Doubling Study", "Empty Archive"])

        before_ids = [effect["effect_id"] for effect in effects]
        self.assertEqual(
            replacements.register_card_replacement_effects(
                room_id, controller),
            [])
        self.assertEqual(
            [effect["effect_id"] for effect
             in self._source_draw_effects(game_state, room_id)],
            before_ids)

        locked_context, locked_replaced = self._apply_draw(
            game_state, controller)
        self.assertFalse(locked_replaced)
        self.assertEqual(locked_context["draw_count"], 1)

        room.door1["unlocked"] = True
        door_one_context, door_one_replaced = self._apply_draw(
            game_state, controller)
        self.assertTrue(door_one_replaced)
        self.assertEqual(door_one_context["draw_count"], 2)

        room.door1["unlocked"] = False
        room.door2["unlocked"] = True
        door_two_context, door_two_replaced = self._apply_draw(
            game_state, controller)
        self.assertTrue(door_two_replaced)
        self.assertEqual(door_two_context["draw_count"], 0)
        self.assertTrue(door_two_context["prevented"])

    def test_room_source_exit_resets_doors_and_reentry_rebuilds_once(self):
        game_state, controller = self._state(55202)
        room_id = inject_into_zone(
            game_state, controller, self._room_data(), "battlefield")
        room = game_state._safe_get_card(room_id)
        replacements = game_state.replacement_effects
        room.door2["unlocked"] = True

        self.assertTrue(game_state.move_card(
            room_id, controller, "battlefield", controller, "hand"))
        self.assertFalse(room.door1["unlocked"])
        self.assertFalse(room.door2["unlocked"])
        self.assertEqual(self._source_draw_effects(game_state, room_id), [])
        self.assertNotIn(room_id, replacements._text_registered_cards)

        self.assertTrue(game_state.move_card(
            room_id, controller, "hand", controller, "battlefield"))
        rebuilt = self._source_draw_effects(game_state, room_id)
        self.assertEqual(len(rebuilt), 2)
        self.assertEqual(
            [effect.get("room_door_number") for effect in rebuilt], [1, 2])
        self.assertFalse(room.door1["unlocked"])
        self.assertFalse(room.door2["unlocked"])
        context, was_replaced = self._apply_draw(game_state, controller)
        self.assertFalse(was_replaced)
        self.assertEqual(context["draw_count"], 1)

    def test_ordinary_card_registration_and_application_are_unchanged(self):
        game_state, controller = self._state(55203)
        source_id = inject_into_zone(
            game_state, controller, self._ordinary_data(), "battlefield")
        effects = self._source_draw_effects(game_state, source_id)
        self.assertEqual(len(effects), 1)
        self.assertNotIn("room_face_index", effects[0])
        self.assertNotIn("room_door_number", effects[0])

        context, was_replaced = self._apply_draw(game_state, controller)
        self.assertTrue(was_replaced)
        self.assertEqual(context["draw_count"], 2)

    def test_unpreventable_damage_query_obeys_room_door_gate(self):
        game_state, controller = self._state(55204)
        room_id = inject_into_zone(
            game_state, controller, self._room_data(), "battlefield")
        room = game_state._safe_get_card(room_id)
        replacements = game_state.replacement_effects
        replacements.register_effect({
            "source_id": room_id,
            "event_type": "DAMAGE",
            "condition": lambda _context: True,
            "replacement": lambda context: context,
            "duration": "permanent",
            "controller_id": controller,
            "stops_damage_prevention": True,
            "room_face_index": 0,
            "room_door_number": 1,
        })

        self.assertFalse(replacements.damage_cannot_be_prevented({}))
        room.door1["unlocked"] = True
        self.assertTrue(replacements.damage_cannot_be_prevented({}))
        room.door1["unlocked"] = False
        self.assertFalse(replacements.damage_cannot_be_prevented({}))


if __name__ == "__main__":
    unittest.main()
