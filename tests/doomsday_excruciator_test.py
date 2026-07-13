"""Real-card regressions for Doomsday Excruciator's cast-gated ETB."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
for path in (REPO_ROOT, TESTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from Playersim.ability_types import (  # noqa: E402
    ExileLibrariesExceptBottomEffect,
)
from Playersim.ability_utils import EffectFactory  # noqa: E402
from scenario_test import (  # noqa: E402
    fresh,
    get_env,
    inject_card,
    inject_into_zone,
    inject_real_card,
)


logging.disable(logging.CRITICAL)


def card(name, card_type="instant", mana_cost="{1}"):
    return {
        "name": name,
        "mana_cost": mana_cost,
        "cmc": 1 if mana_cost else 0,
        "type_line": card_type.title(),
        "card_types": [card_type],
        "subtypes": [],
        "oracle_text": "",
        "colors": [0, 0, 0, 0, 0],
        "color_identity": [],
    }


class DoomsdayExcruciatorTest(unittest.TestCase):
    def _state(self, seed):
        game_state = fresh(seed)
        game_state.agent_is_p1 = True
        game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
        game_state.previous_priority_phase = None
        game_state.priority_player = game_state.p1
        game_state.priority_pass_count = 0
        game_state.stack = []
        game_state.targeting_context = None
        game_state.choice_context = None
        game_state.ability_handler.active_triggers = []
        for player in (game_state.p1, game_state.p2):
            for zone in ("library", "hand", "battlefield", "graveyard",
                         "exile"):
                player[zone] = []
            player.get("tapped_permanents", set()).clear()
            player.get("entered_battlefield_this_turn", set()).clear()
        game_state.face_down_exile_cards.clear()
        game_state.face_down_exile_counts.clear()
        return game_state

    @staticmethod
    def _ordered_library(game_state, player, prefix, count):
        result = []
        for index in range(count):
            card_id = inject_card(
                game_state, card(f"{prefix} {index}", "instant"))
            player["library"].append(card_id)
            game_state._last_card_locations[card_id] = (player, "library")
            result.append(card_id)
        return result

    def _cast_and_resolve(self, seed=8101):
        game_state = self._state(seed)
        player = game_state.p1
        p1_order = self._ordered_library(game_state, player, "P1", 10)
        p2_order = self._ordered_library(game_state, game_state.p2, "P2", 8)
        doomsday = inject_real_card(
            game_state, player, "Doomsday Excruciator", "hand")
        for symbol in player["mana_pool"]:
            player["mana_pool"][symbol] = 20

        self.assertTrue(game_state.cast_spell(doomsday, player, context={}))
        self.assertTrue(game_state.stack[-1][3].get("was_cast"))
        self.assertTrue(game_state.resolve_top_of_stack())
        matching = [
            entry for entry in game_state.ability_handler.active_triggers
            if entry[0].card_id == doomsday
            and "if it was cast" in entry[0].effect_text.lower()
        ]
        self.assertEqual(len(matching), 1)
        game_state.ability_handler.process_triggered_abilities()
        self.assertTrue(game_state.resolve_top_of_stack())
        return game_state, doomsday, p1_order, p2_order

    def test_real_cast_keeps_each_bottom_six_in_order_and_hides_exile(self):
        game_state, doomsday, p1_order, p2_order = \
            self._cast_and_resolve()

        self.assertIn(doomsday, game_state.p1["battlefield"])
        self.assertEqual(game_state.p1["library"], p1_order[-6:])
        self.assertEqual(game_state.p2["library"], p2_order[-6:])
        self.assertEqual(game_state.p1["exile"], p1_order[:-6])
        self.assertEqual(game_state.p2["exile"], p2_order[:-6])
        self.assertEqual(
            game_state.face_down_exile_cards,
            set(p1_order[:-6] + p2_order[:-6]))
        self.assertEqual(game_state.fidelity_counters["unparsed_effects"], 0)
        self.assertNotIn(
            "Doomsday Excruciator",
            game_state.fidelity_counters["unparsed_cards"])

    def test_putting_real_card_onto_battlefield_does_not_exile_libraries(self):
        game_state = self._state(8102)
        p1_order = self._ordered_library(game_state, game_state.p1, "P1", 9)
        p2_order = self._ordered_library(game_state, game_state.p2, "P2", 9)

        doomsday = inject_real_card(
            game_state, game_state.p1, "Doomsday Excruciator",
            "battlefield")

        self.assertIn(doomsday, game_state.p1["battlefield"])
        self.assertEqual(game_state.p1["library"], p1_order)
        self.assertEqual(game_state.p2["library"], p2_order)
        self.assertFalse(game_state.face_down_exile_cards)
        self.assertFalse(any(
            ability.card_id == doomsday
            and "if it was cast" in ability.effect_text.lower()
            for ability, _, _ in game_state.ability_handler.active_triggers))

    def test_hidden_exile_is_zeroed_in_observation_and_ignored_by_composition(self):
        game_state, doomsday, p1_order, _ = self._cast_and_resolve(8103)
        visible_artifact = inject_into_zone(
            game_state, game_state.p1,
            card("Visible Exile Artifact", "artifact"), "exile")

        observation = get_env().observation_for(game_state.p1)
        hidden_count = len(p1_order[:-6])
        self.assertTrue(np.all(
            observation["exile_key_cards"][:hidden_count] == 0))
        visible_index = game_state.p1["exile"].index(visible_artifact)
        self.assertTrue(np.any(
            observation["exile_key_cards"][visible_index] != 0))
        np.testing.assert_allclose(
            observation["deck_composition_estimate"],
            np.array([0.5, 0.0, 0.0, 0.5, 0.0, 0.0],
                     dtype=np.float32))
        self.assertIn(doomsday, game_state.p1["battlefield"])

    def test_hidden_exile_tracking_clones_and_clears_on_zone_change(self):
        game_state, _, p1_order, _ = self._cast_and_resolve(8104)
        hidden_card = p1_order[0]
        cloned = game_state.clone()

        self.assertIsNotNone(cloned)
        self.assertEqual(
            cloned.face_down_exile_cards,
            game_state.face_down_exile_cards)
        self.assertIsNot(
            cloned.face_down_exile_cards,
            game_state.face_down_exile_cards)
        self.assertEqual(
            cloned.face_down_exile_counts,
            game_state.face_down_exile_counts)
        self.assertIsNot(
            cloned.face_down_exile_counts,
            game_state.face_down_exile_counts)
        self.assertTrue(cloned.move_card(
            hidden_card, cloned.p1, "exile", cloned.p1, "hand"))
        self.assertNotIn(hidden_card, cloned.face_down_exile_cards)
        self.assertIn(hidden_card, game_state.face_down_exile_cards)
        self.assertIn(hidden_card, game_state.p1["exile"])

        self.assertTrue(game_state.move_card(
            hidden_card, game_state.p1, "exile", game_state.p1, "hand"))
        self.assertNotIn(hidden_card, game_state.face_down_exile_cards)

    def test_hidden_opponent_exile_does_not_shape_estimated_hand_profile(self):
        game_state = self._state(8105)
        observer, opponent = game_state.p1, game_state.p2
        inject_into_zone(
            game_state, opponent, card("Unknown Hand Card"), "hand")
        hidden = inject_card(game_state, {
            **card("Hidden Red Creature", "creature"),
            "colors": [0, 0, 0, 1, 0], "power": 4, "toughness": 4,
        })
        opponent["library"].append(hidden)
        game_state._last_card_locations[hidden] = (opponent, "library")
        self.assertTrue(game_state.move_card(
            hidden, opponent, "library", opponent, "exile",
            context={"face_down_exile": True}))

        observed_profiles = []
        scored_candidates = []
        original = get_env()._calculate_card_likelihood

        def capture_profile(candidate, colors, creatures, instants, artifacts):
            scored_candidates.append(candidate)
            observed_profiles.append((
                colors.copy(), creatures, instants, artifacts))
            return original(
                candidate, colors, creatures, instants, artifacts)

        with patch.object(
                get_env(), "_calculate_card_likelihood",
                side_effect=capture_profile):
            get_env().observation_for(observer)

        self.assertTrue(observed_profiles)
        self.assertNotIn(game_state._safe_get_card(hidden), scored_candidates)
        self.assertTrue(all(
            not colors.any()
            and creatures == instants == artifacts == 0
            for colors, creatures, instants, artifacts
            in observed_profiles))

    def test_hidden_exile_fails_identity_targeting_and_redacts_target_id(self):
        game_state = self._state(8106)
        controller, opponent = game_state.p1, game_state.p2
        source = inject_into_zone(
            game_state, controller,
            card("Exile Target Probe", "sorcery"), "graveyard")
        hidden = inject_card(game_state, {
            **card("Hidden Creature", "creature"),
            "power": 2, "toughness": 2,
        })
        opponent["library"].append(hidden)
        game_state._last_card_locations[hidden] = (opponent, "library")
        self.assertTrue(game_state.move_card(
            hidden, opponent, "library", opponent, "exile",
            context={"face_down_exile": True}))
        visible = inject_into_zone(game_state, opponent, {
            **card("Visible Exiled Creature", "creature"),
            "power": 2, "toughness": 2,
        }, "exile")

        targeting = game_state.targeting_system
        restricted = [{"type": "creature", "zone": "exile"}]
        with patch.object(
                targeting, "_parse_targeting_requirements",
                return_value=restricted):
            valid = targeting.get_valid_targets(
                source, controller, effect_text="restricted exile target")
        candidates = {
            target_id for ids in valid.values() for target_id in ids}
        self.assertIn(visible, candidates)
        self.assertNotIn(hidden, candidates)

        game_state.targeting_context = {
            "source_id": source, "controller": controller,
            "required_type": "card", "effect_text": "target card in exile",
            "required_count": 1, "min_targets": 1, "max_targets": 1,
            "selected_targets": [], "target_page": 0,
        }
        generic = [{"type": "card", "zone": "exile"}]
        with patch.object(
                targeting, "_parse_targeting_requirements",
                return_value=generic):
            target_obs = get_env()._get_target_page_observation(controller)
        hidden_slot = target_obs["target_zone_indices"].tolist().index(
            opponent["exile"].index(hidden))
        self.assertTrue(target_obs["target_card_mask"][hidden_slot])
        self.assertEqual(target_obs["target_card_ids"][hidden_slot], -1)
        self.assertFalse(target_obs["target_cards"][hidden_slot].any())

    def test_repeated_hidden_id_remains_hidden_until_every_occurrence_leaves(self):
        game_state = self._state(8107)
        player = game_state.p1
        repeated = inject_card(game_state, card("Repeated Hidden Card"))
        player["library"].extend([repeated, repeated])
        game_state._last_card_locations[repeated] = (player, "library")
        for _ in range(2):
            self.assertTrue(game_state.move_card(
                repeated, player, "library", player, "exile",
                context={"face_down_exile": True}))

        key = game_state._face_down_exile_key(player, repeated)
        self.assertEqual(game_state.face_down_exile_counts[key], 2)
        self.assertTrue(game_state.move_card(
            repeated, player, "exile", player, "hand"))
        self.assertEqual(game_state.face_down_exile_counts[key], 1)
        self.assertTrue(game_state.is_face_down_exile_card(repeated, player))
        self.assertTrue(np.all(
            get_env()._get_card_feature(
                repeated, get_env()._feature_dim) == 0))

        self.assertTrue(game_state.move_card(
            repeated, player, "exile", player, "hand"))
        self.assertNotIn(key, game_state.face_down_exile_counts)
        self.assertFalse(game_state.is_face_down_exile_card(repeated, player))

    def test_exact_effect_text_has_a_concrete_parser(self):
        effects = EffectFactory.create_effects(
            "each player exiles all but the bottom six cards of their "
            "library face down",
            source_name="Doomsday Excruciator")
        self.assertEqual(len(effects), 1)
        self.assertIsInstance(effects[0], ExileLibrariesExceptBottomEffect)


if __name__ == "__main__":
    unittest.main()
