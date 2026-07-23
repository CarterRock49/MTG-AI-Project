"""Observation-v6 exact-own deck-strategy boundary tests."""

import os
import sys
import tempfile
import unittest

import numpy as np


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from Playersim.archetypes import (  # noqa: E402
    PRIMARY_ARCHETYPES,
    STRATEGY_AXES,
    STRATEGY_TAGS,
    classifier_identity,
    classify_full_deck,
    encode_profile,
    taxonomy_identity,
)
from Playersim.card import Card  # noqa: E402
from Playersim.environment import AlphaZeroMTGEnv  # noqa: E402
from Playersim.observation_schema import (  # noqa: E402
    EXACT_OWN_STRATEGY_PROFILE_FIELD,
    EXACT_OWN_STRATEGY_PROFILE_ORDER,
    EXACT_OWN_STRATEGY_PROFILE_SIZE,
    OBSERVATION_SCHEMA_SHA256,
    OBSERVATION_SCHEMA_VERSION,
    _schema_payload,
    observation_schema_identity,
)


EXPECTED_OBSERVATION_V6_SHA256 = (
    "6521db9c0c70c919a63c34e9c99463a3b801e25ae91149fd518a34054989e790")
EXPECTED_EXACT_OWN_PROFILE_ORDER = (
    "primary_one_hot:aggro",
    "primary_one_hot:tempo",
    "primary_one_hot:midrange",
    "primary_one_hot:control",
    "primary_one_hot:combo",
    "primary_one_hot:ramp",
    "primary_one_hot:hybrid",
    "primary_one_hot:unknown",
    "secondary_one_hot:aggro",
    "secondary_one_hot:tempo",
    "secondary_one_hot:midrange",
    "secondary_one_hot:control",
    "secondary_one_hot:combo",
    "secondary_one_hot:ramp",
    "secondary_one_hot:hybrid",
    "secondary_one_hot:unknown",
    "tag:alternate_win",
    "tag:artifacts",
    "tag:big_mana",
    "tag:blink",
    "tag:board_control",
    "tag:burn",
    "tag:counters",
    "tag:discard",
    "tag:enchantments",
    "tag:equipment",
    "tag:fliers",
    "tag:go_wide",
    "tag:graveyard",
    "tag:landfall",
    "tag:lands",
    "tag:lessons",
    "tag:lifegain",
    "tag:mill",
    "tag:prison",
    "tag:prowess",
    "tag:reanimator",
    "tag:sacrifice",
    "tag:spellslinger",
    "tag:tokens",
    "tag:toolbox",
    "tag:typal",
    "tag:voltron",
    "axis:speed",
    "axis:threat_density",
    "axis:interaction",
    "axis:card_advantage",
    "axis:mana_acceleration",
    "axis:synergy_dependency",
    "axis:combo_dependency",
    "axis:graveyard_dependency",
    "axis:board_width",
    "axis:instant_speed",
    "confidence",
)


def _reviewed_profile(primary, *, secondary=None, tags=(), axis_base=50):
    axes = {
        name: min(100, axis_base + index)
        for index, name in enumerate(STRATEGY_AXES)
    }
    return {
        "taxonomy_version": 1,
        "primary": primary,
        "secondary": secondary,
        "tags": list(tags),
        "axes": axes,
        "review": {
            "status": "reviewed",
            "reviewed_at": "2026-07-23",
            "basis": "Observation-v6 golden fixture",
        },
    }


def _fixture_card(card_id, *, name, type_line, mana_cost, oracle_text,
                  color_identity, power=None, toughness=None):
    payload = {
        "name": name,
        "type_line": type_line,
        "mana_cost": mana_cost,
        "oracle_text": oracle_text,
        "color_identity": color_identity,
    }
    if power is not None:
        payload["power"] = power
    if toughness is not None:
        payload["toughness"] = toughness
    card = Card(payload)
    card.card_id = card_id
    return card


def _fixture_data(*, p1_profile=None, p2_profile=None, same_cards=False):
    card_db = {
        0: _fixture_card(
            0, name="Exact Own Charger", type_line="Creature - Warrior",
            mana_cost="{R}", oracle_text="Haste", color_identity=["R"],
            power=2, toughness=1),
        1: _fixture_card(
            1, name="Exact Own Denial", type_line="Instant",
            mana_cost="{1}{U}", oracle_text="Counter target spell.",
            color_identity=["U"]),
    }
    p1_cards = [0] * 30 + [1] * 30
    p2_cards = list(reversed(p1_cards)) if same_cards else [1] * 60
    p1 = {"name": "P1 Strategy", "cards": p1_cards}
    p2 = {"name": "P2 Strategy", "cards": p2_cards}
    if p1_profile is not None:
        p1["strategy_profile"] = p1_profile
    if p2_profile is not None:
        p2["strategy_profile"] = p2_profile
    return [p1, p2], card_db


class ObservationV6StrategyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def _environment(self, decks, card_db, *, agent_is_p1=True, seed=6001):
        suffix = str(len(getattr(self, "_environments", [])))
        env = AlphaZeroMTGEnv(
            decks, card_db,
            deck_stats_path=os.path.join(
                self.temp.name, "deck_stats_" + suffix),
            card_memory_path=os.path.join(
                self.temp.name, "card_memory_" + suffix),
        )
        if not hasattr(self, "_environments"):
            self._environments = []
        self._environments.append(env)
        self.addCleanup(env.close)
        observation, _ = env.reset(seed=seed, options={
            "p1_deck": decks[0]["name"],
            "p2_deck": decks[1]["name"],
            "agent_is_p1": agent_is_p1,
        })
        return env, observation

    def test_schema_pins_field_shape_bounds_order_and_lineage(self):
        p1_profile = _reviewed_profile(
            "control", secondary="midrange", tags=("board_control",),
            axis_base=31)
        p2_profile = _reviewed_profile(
            "aggro", secondary="tempo", tags=("burn",), axis_base=61)
        decks, card_db = _fixture_data(
            p1_profile=p1_profile, p2_profile=p2_profile)
        env, observation = self._environment(decks, card_db)

        self.assertEqual(OBSERVATION_SCHEMA_VERSION, 6)
        self.assertEqual(env.OBSERVATION_SCHEMA_VERSION, 6)
        self.assertEqual(
            env.OBSERVATION_SCHEMA_SHA256, OBSERVATION_SCHEMA_SHA256)
        self.assertEqual(
            observation_schema_identity()["sha256"],
            EXPECTED_OBSERVATION_V6_SHA256)
        self.assertEqual(
            OBSERVATION_SCHEMA_SHA256, EXPECTED_OBSERVATION_V6_SHA256)
        self.assertEqual(
            EXACT_OWN_STRATEGY_PROFILE_SIZE,
            len(PRIMARY_ARCHETYPES) * 2 + len(STRATEGY_TAGS)
            + len(STRATEGY_AXES) + 1)
        self.assertEqual(
            EXACT_OWN_STRATEGY_PROFILE_ORDER,
            EXPECTED_EXACT_OWN_PROFILE_ORDER)
        self.assertEqual(len(EXPECTED_EXACT_OWN_PROFILE_ORDER), 54)

        strategy_space = env.observation_space.spaces[
            EXACT_OWN_STRATEGY_PROFILE_FIELD]
        self.assertEqual(
            strategy_space.shape, (EXACT_OWN_STRATEGY_PROFILE_SIZE,))
        self.assertEqual(strategy_space.dtype, np.dtype(np.float32))
        self.assertTrue(np.all(strategy_space.low == 0.0))
        self.assertTrue(np.all(strategy_space.high == 1.0))
        self.assertTrue(env.observation_space.contains(observation))

        contract = _schema_payload()["exact_own_strategy_profile"]
        self.assertEqual(contract["field"], EXACT_OWN_STRATEGY_PROFILE_FIELD)
        self.assertEqual(
            contract["component_order"],
            list(EXACT_OWN_STRATEGY_PROFILE_ORDER))
        self.assertEqual(contract["component_encoding"], {
            "primary": "closed_vocabulary_one_hot",
            "secondary": "closed_vocabulary_one_hot_or_all_zero",
            "tags": "closed_vocabulary_multi_hot",
            "axes": "integer_0_to_100_divided_by_100",
            "confidence": "basis_points_0_to_10000_divided_by_10000",
        })
        self.assertEqual(
            contract["taxonomy_sha256"], taxonomy_identity()["sha256"])
        self.assertEqual(
            contract["classifier_sha256"], classifier_identity()["sha256"])

    def test_reviewed_profile_tracks_the_acting_observer_only(self):
        p1_profile = _reviewed_profile(
            "control", secondary="midrange",
            tags=("board_control", "counters"), axis_base=20)
        p2_profile = _reviewed_profile(
            "aggro", secondary="tempo", tags=("burn", "go_wide"),
            axis_base=70)
        decks, card_db = _fixture_data(
            p1_profile=p1_profile, p2_profile=p2_profile)
        env, p1_reset_observation = self._environment(decks, card_db)
        gs = env.game_state

        expected_p1 = np.asarray(encode_profile(classify_full_deck(
            decks[0]["cards"], card_db, declared=p1_profile)),
            dtype=np.float32)
        expected_p2 = np.asarray(encode_profile(classify_full_deck(
            decks[1]["cards"], card_db, declared=p2_profile)),
            dtype=np.float32)
        p1_view = env.observation_for(gs.p1)
        p2_view = env.observation_for(gs.p2)

        self.assertTrue(np.array_equal(
            p1_reset_observation[EXACT_OWN_STRATEGY_PROFILE_FIELD],
            expected_p1))
        self.assertTrue(np.array_equal(
            p1_view[EXACT_OWN_STRATEGY_PROFILE_FIELD], expected_p1))
        self.assertTrue(np.array_equal(
            p2_view[EXACT_OWN_STRATEGY_PROFILE_FIELD], expected_p2))
        self.assertFalse(np.array_equal(expected_p1, expected_p2))
        self.assertNotIn(
            "opponent_exact_deck_strategy_profile", p1_view)
        self.assertNotIn(
            "opponent_exact_deck_strategy_profile", p2_view)

        # An environment whose learned seat is P2 must return P2's own vector
        # in the initial policy observation as well.
        env2, p2_reset_observation = self._environment(
            decks, card_db, agent_is_p1=False, seed=6002)
        self.assertFalse(env2.game_state.agent_is_p1)
        self.assertTrue(np.array_equal(
            p2_reset_observation[EXACT_OWN_STRATEGY_PROFILE_FIELD],
            expected_p2))

    def test_other_seat_hidden_changes_cannot_change_exact_own_profile(self):
        p1_profile = _reviewed_profile(
            "control", tags=("board_control",), axis_base=25)
        p2_profile = _reviewed_profile(
            "aggro", tags=("burn",), axis_base=65)
        decks, card_db = _fixture_data(
            p1_profile=p1_profile, p2_profile=p2_profile)
        env, _ = self._environment(decks, card_db)
        gs = env.game_state
        before = env.observation_for(gs.p1)

        hidden = _fixture_card(
            99, name="Opponent Hidden Replacement", type_line="Sorcery",
            mana_cost="{4}{B}", oracle_text="Target opponent discards.",
            color_identity=["B"])
        gs.card_db[99] = hidden
        gs.p2["hand"][0] = 99
        gs.p2["library"][0] = 99
        # Even an adversarial replacement of the other seat's cached exact
        # profile must not affect P1's field or its separate public opponent
        # belief.  Real runtime code never mutates this reset-pinned cache.
        env._exact_deck_strategy_profiles[False] = classify_full_deck(
            decks[1]["cards"], card_db,
            declared=_reviewed_profile(
                "combo", tags=("alternate_win",), axis_base=45))
        # The exact-own input describes the starting multiset, not the live
        # hidden draw order. Reordering the observer's library cannot change it.
        gs.p1["library"].reverse()
        after = env.observation_for(gs.p1)

        self.assertTrue(np.array_equal(
            before[EXACT_OWN_STRATEGY_PROFILE_FIELD],
            after[EXACT_OWN_STRATEGY_PROFILE_FIELD]))
        self.assertTrue(np.array_equal(
            before["opponent_archetype"], after["opponent_archetype"]))

    def test_profile_changes_are_visible_and_inference_is_order_invariant(self):
        control = _reviewed_profile(
            "control", tags=("board_control",), axis_base=15)
        aggro = _reviewed_profile(
            "aggro", tags=("burn",), axis_base=75)
        reviewed_decks, card_db = _fixture_data(
            p1_profile=control, p2_profile=aggro, same_cards=True)
        env, _ = self._environment(reviewed_decks, card_db)
        p1_vector = env.observation_for(env.game_state.p1)[
            EXACT_OWN_STRATEGY_PROFILE_FIELD]
        p2_vector = env.observation_for(env.game_state.p2)[
            EXACT_OWN_STRATEGY_PROFILE_FIELD]
        self.assertFalse(np.array_equal(p1_vector, p2_vector))
        self.assertEqual(
            int(np.argmax(p1_vector[:len(PRIMARY_ARCHETYPES)])),
            PRIMARY_ARCHETYPES.index("control"))
        self.assertEqual(
            int(np.argmax(p2_vector[:len(PRIMARY_ARCHETYPES)])),
            PRIMARY_ARCHETYPES.index("aggro"))

        inferred_decks, inferred_db = _fixture_data(same_cards=True)
        inferred_env, _ = self._environment(
            inferred_decks, inferred_db, seed=6003)
        inferred_p1 = inferred_env._exact_deck_strategy_profiles[True]
        inferred_p2 = inferred_env._exact_deck_strategy_profiles[False]
        self.assertEqual(inferred_p1.source, "rules_inferred")
        self.assertEqual(inferred_p2.source, "rules_inferred")
        self.assertTrue(np.array_equal(
            inferred_env.observation_for(inferred_env.game_state.p1)[
                EXACT_OWN_STRATEGY_PROFILE_FIELD],
            inferred_env.observation_for(inferred_env.game_state.p2)[
                EXACT_OWN_STRATEGY_PROFILE_FIELD]))


if __name__ == "__main__":
    unittest.main()
