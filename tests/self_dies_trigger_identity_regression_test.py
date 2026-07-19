"""Exact DIES-event identity regressions for self-referential triggers."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import (  # noqa: E402
    fresh,
    inject_into_zone,
    inject_real_card,
)


logging.disable(logging.CRITICAL)


def _vanilla_creature(name: str) -> dict:
    return {
        "name": name,
        "mana_cost": "{1}",
        "cmc": 1,
        "type_line": "Creature - Test",
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
        "power": 1,
        "toughness": 1,
    }


class SelfDiesTriggerIdentityRegressionTest(unittest.TestCase):
    @staticmethod
    def _dies_triggers(game_state, source_id):
        return [
            entry
            for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == source_id
            and "dies" in entry[0].trigger_condition
        ]

    def _kill_other_creature(
            self, game_state, player, source_id, name: str):
        creature_id = inject_into_zone(
            game_state, player, _vanilla_creature(name), "battlefield")
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            creature_id, player, "battlefield", player, "graveyard",
            cause="destroy"))
        self.assertEqual(
            self._dies_triggers(game_state, source_id), [],
            f"{name} incorrectly fired source {source_id}'s self-dies trigger",
        )

    def test_enduring_curiosity_hears_only_its_own_death_and_returns_exactly(self):
        game_state = fresh(seed=35001)
        controller, opponent = game_state.p1, game_state.p2
        curiosity_id = inject_real_card(
            game_state, controller, "Enduring Curiosity", "battlefield")
        game_state.ability_handler.active_triggers = []

        self._kill_other_creature(
            game_state, controller, curiosity_id, "Friendly Death")
        self._kill_other_creature(
            game_state, opponent, curiosity_id, "Opponent Death")

        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            curiosity_id, controller, "battlefield", controller,
            "graveyard", cause="destroy"))
        queued = self._dies_triggers(game_state, curiosity_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], curiosity_id)
        self.assertTrue(queued[0][2]["last_known"]["was_creature"])

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0:2],
                         ("TRIGGER", curiosity_id))
        self.assertTrue(game_state.resolve_top_of_stack())

        live = game_state._safe_get_card(curiosity_id)
        self.assertIn(curiosity_id, controller["battlefield"])
        self.assertNotIn(curiosity_id, controller["graveyard"])
        self.assertEqual(live.card_types, ["enchantment"])
        self.assertEqual(live.subtypes, [])
        self.assertEqual((live.power, live.toughness), (0, 0))

    def test_mosswood_dreadknight_hears_only_its_own_death_and_grants_permission(self):
        game_state = fresh(seed=35002)
        controller, opponent = game_state.p1, game_state.p2
        dreadknight_id = inject_real_card(
            game_state, controller,
            "Mosswood Dreadknight // Dread Whispers", "battlefield")
        game_state.ability_handler.active_triggers = []

        self._kill_other_creature(
            game_state, controller, dreadknight_id, "Friendly Death")
        self._kill_other_creature(
            game_state, opponent, dreadknight_id, "Opponent Death")

        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            dreadknight_id, controller, "battlefield", controller,
            "graveyard", cause="destroy"))
        queued = self._dies_triggers(game_state, dreadknight_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2]["event_card_id"], dreadknight_id)

        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertEqual(game_state.stack[-1][0:2],
                         ("TRIGGER", dreadknight_id))
        self.assertFalse(game_state.has_graveyard_adventure_permission(
            controller, dreadknight_id))
        self.assertTrue(game_state.resolve_top_of_stack())

        self.assertIn(dreadknight_id, controller["graveyard"])
        self.assertTrue(game_state.has_graveyard_adventure_permission(
            controller, dreadknight_id))
        permissions = [
            entry for entry in game_state.graveyard_adventure_permissions
            if entry["card_id"] == dreadknight_id
        ]
        self.assertEqual(len(permissions), 1)
        self.assertEqual(permissions[0]["controller"], "p1")
        self.assertEqual(permissions[0]["granted_turn"], game_state.turn)
        self.assertGreater(permissions[0]["expires_turn"], game_state.turn)

    def test_unwilling_vessel_token_uses_frozen_mixed_counter_lki(self):
        game_state = fresh(seed=35004)
        controller = game_state.p1
        vessel_id = inject_real_card(
            game_state, controller, "Unwilling Vessel", "battlefield")
        vessel = game_state._safe_get_card(vessel_id)
        vessel.counters = {"possession": 2, "stun": 1}
        game_state.ability_handler.active_triggers = []

        self.assertTrue(game_state.move_card(
            vessel_id, controller, "battlefield", controller,
            "graveyard", cause="destroy"))
        queued = self._dies_triggers(game_state, vessel_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(
            queued[0][2]["last_known"]["counters"],
            {"possession": 2, "stun": 1})

        # The graveyard object is not the object that died. Resolution must
        # use the frozen event snapshot, not mutable current card state.
        vessel.counters.clear()
        game_state.ability_handler.process_triggered_abilities()
        self.assertEqual(len(game_state.stack), 1)
        self.assertTrue(game_state.resolve_top_of_stack())

        spirit_ids = [
            token_id for token_id in controller.get("tokens", [])
            if token_id in controller.get("battlefield", [])
            and game_state._safe_get_card(token_id).name == "Spirit Token"
        ]
        self.assertEqual(len(spirit_ids), 1)
        spirit = game_state._safe_get_card(spirit_ids[0])
        self.assertEqual((spirit.power, spirit.toughness), (3, 3))
        self.assertEqual(spirit.subtypes, ["spirit"])
        self.assertEqual(spirit.colors, [0, 1, 0, 0, 0])
        self.assertTrue(game_state.check_keyword(spirit_ids[0], "flying"))

    def test_controlled_creature_dies_watcher_still_hears_the_matching_event(self):
        game_state = fresh(seed=35003)
        controller, opponent = game_state.p1, game_state.p2
        liliana_id = inject_real_card(
            game_state, controller, "Liliana, Dreadhorde General",
            "battlefield")
        game_state.ability_handler.active_triggers = []

        friendly_id = inject_into_zone(
            game_state, controller,
            _vanilla_creature("Watched Friendly Death"), "battlefield")
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            friendly_id, controller, "battlefield", controller,
            "graveyard", cause="destroy"))
        self.assertEqual(len(self._dies_triggers(
            game_state, liliana_id)), 1)

        game_state.ability_handler.active_triggers = []
        opposing_id = inject_into_zone(
            game_state, opponent,
            _vanilla_creature("Unwatched Opponent Death"), "battlefield")
        game_state.ability_handler.active_triggers = []
        self.assertTrue(game_state.move_card(
            opposing_id, opponent, "battlefield", opponent,
            "graveyard", cause="destroy"))
        self.assertEqual(
            self._dies_triggers(game_state, liliana_id), [])


if __name__ == "__main__":
    unittest.main()
