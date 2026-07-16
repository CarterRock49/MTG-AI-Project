"""Symbolic-stat safety regressions for strategic planner analysis.

Run from the repository root with::

    python tests/strategic_planner_numeric_test.py
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.card import Card  # noqa: E402
from Playersim.environment import AlphaZeroMTGEnv  # noqa: E402
from Playersim.game_state import GameState  # noqa: E402
from Playersim.strategic_planner import MTGStrategicPlanner  # noqa: E402
from Playersim.strategic_planner_archetypes import (  # noqa: E402
    _card_number as archetype_card_number,
)
from Playersim.strategic_planner_search import (  # noqa: E402
    _card_number as search_card_number,
)


class StrategicPlannerNumericSafetyTest(unittest.TestCase):
    @staticmethod
    def _planner_for_deck(card_specs):
        cards = {
            card_id: Card(spec)
            for card_id, spec in enumerate(card_specs)
        }
        player = {
            "life": 20,
            "hand": [],
            "battlefield": [],
            "library": list(cards),
            "graveyard": [],
            "mana_pool": {},
        }
        opponent = {
            "life": 20,
            "hand": [],
            "battlefield": [],
            "library": [],
            "graveyard": [],
            "mana_pool": {},
        }
        state = SimpleNamespace(
            p1=player, p2=opponent, agent_is_p1=True, turn=1)
        state._safe_get_card = cards.get
        return MTGStrategicPlanner(state)

    @staticmethod
    def _card_specs(count, **overrides):
        base = {
            "name": "Fixture Card",
            "type_line": "Creature - Human",
            "mana_cost": "{1}",
            "cmc": 1,
            "power": "1",
            "toughness": "1",
            "oracle_text": "",
            "color_identity": ["G"],
        }
        return [dict(base, **overrides) for _ in range(count)]

    def _state(self):
        namor = Card({
            "name": "Namor the Sub-Mariner",
            "type_line": "Legendary Creature — Mutant Merfolk Villain",
            "mana_cost": "{1}{U}{U}", "cmc": 3,
            "power": "*", "toughness": "4",
            "oracle_text": (
                "Flying\nNamor's power is equal to the number of Merfolk "
                "you control."),
            "color_identity": ["U"],
        })
        unknown = Card({
            "name": "Unknown Body", "type_line": "Creature — Merfolk",
            "mana_cost": "{U}", "cmc": 1,
            "power": "*", "toughness": "*", "oracle_text": "",
            "color_identity": ["U"],
        })
        game_state = GameState({0: namor, 1: unknown})
        game_state.reset([0], [1], seed=23)
        game_state.mulligan_in_progress = False
        for player in (game_state.p1, game_state.p2):
            for card_id in list(player["hand"]):
                self.assertTrue(game_state.move_card(
                    card_id, player, "hand", player, "battlefield"))
        game_state.agent_is_p1 = True
        return game_state

    def test_numeric_helpers_normalize_symbolic_and_nonfinite_stats(self):
        game_state = self._state()
        namor = game_state._safe_get_card(game_state.p1["battlefield"][0])
        unknown = game_state._safe_get_card(game_state.p2["battlefield"][0])
        self.assertEqual(archetype_card_number(namor, "power"), 1.0)
        self.assertEqual(archetype_card_number(unknown, "power"), 0.0)
        self.assertEqual(search_card_number(unknown, "power"), 0.0)

    def test_planner_analysis_tolerates_symbolic_creatures(self):
        game_state = self._state()
        planner = MTGStrategicPlanner(game_state)
        win_conditions = planner.identify_win_conditions()
        assessed = planner._assess_win_conditions(
            list(game_state.p1["battlefield"]),
            list(game_state.p2["battlefield"]),
            game_state.p1["life"], game_state.p2["life"])
        archetype = planner._detect_deck_archetype()
        goals = planner.establish_long_term_goals()

        self.assertIsInstance(archetype, str)
        self.assertTrue(math.isfinite(
            win_conditions["combat_damage"]["score"]))
        self.assertTrue(math.isfinite(float(
            assessed["combat_damage"]["turns_to_win"])))
        self.assertTrue(all(math.isfinite(float(threat["level"]))
                            for threat in goals["threat_assessment"]))

    def test_recommend_action_tolerates_symbolic_attacker_power(self):
        game_state = self._state()
        game_state.agent_is_p1 = False
        game_state.strategy_memory = None
        attacker_id = game_state.p2["battlefield"][0]

        class AttackOnlyHandler:
            action_reasons_with_context = {
                901: {"context": {
                    "battlefield_idx": 0, "card_id": attacker_id}}}

            @staticmethod
            def get_action_info(_action_idx):
                return "ATTACK", 0

        game_state.action_handler = AttackOnlyHandler()
        planner = MTGStrategicPlanner(game_state)
        planner.analyze_game_state = lambda: None
        planner.adapt_strategy = lambda: None
        planner._is_critical_decision = lambda: False
        planner.assess_threats = lambda: []
        planner.find_best_play_sequence = lambda *_args, **_kwargs: ([], 0)
        planner.evaluate_attack_action = lambda _attackers: 0.5

        with patch("Playersim.strategic_planner_search.logging.error") as error:
            self.assertEqual(planner.recommend_action([901]), 901)
        error.assert_not_called()

    def test_nonviable_combat_path_has_zero_viability_score(self):
        game_state = self._state()
        opponent = game_state.p2
        for card_id in list(opponent["battlefield"]):
            self.assertTrue(game_state.move_card(
                card_id, opponent, "battlefield", opponent, "graveyard"))

        planner = MTGStrategicPlanner(game_state)
        slow = planner.identify_win_conditions()["combat_damage"]
        self.assertFalse(slow["viable"])
        self.assertEqual(slow["turns_to_win"], 20.0)
        self.assertEqual(slow["score"], 0.0)

        opponent["life"] = 2
        fast = planner.identify_win_conditions()["combat_damage"]
        self.assertTrue(fast["viable"])
        self.assertEqual(fast["turns_to_win"], 2.0)
        self.assertGreater(fast["score"], 0.0)
        self.assertLessEqual(fast["score"], 1.0)

    def test_strategic_resource_metrics_preserve_advantage_magnitude(self):
        class EnvironmentStub:
            strategic_planner = object()

        def metrics(card_advantage, mana_advantage):
            return AlphaZeroMTGEnv._analysis_to_metrics(
                EnvironmentStub(), {
                    "resources": {
                        "card_advantage": card_advantage,
                        "mana_advantage": mana_advantage,
                    }
                })

        small = metrics(1, 1)
        large = metrics(4, 4)
        behind = metrics(-4, -4)
        self.assertGreater(large[2], small[2])
        self.assertGreater(large[3], small[3])
        self.assertLess(behind[2], -small[2])
        self.assertLess(behind[3], -small[3])

    def test_threat_win_conditions_use_opponent_perspective_once(self):
        game_state = self._state()
        planner = MTGStrategicPlanner(game_state)
        observed_perspectives = []

        def fake_win_conditions(current_planner):
            observed_perspectives.append(
                current_planner.game_state.agent_is_p1)
            return {
                "combat_damage": {
                    "viable": False, "key_cards": [],
                }
            }

        with patch.object(
                MTGStrategicPlanner, "identify_win_conditions",
                fake_win_conditions):
            planner.assess_threats()

        self.assertEqual(observed_perspectives, [False])
        self.assertTrue(game_state.agent_is_p1)

    def test_specialized_archetypes_keep_detected_strategy_parameters(self):
        lands = self._card_specs(
            24, name="Basic Land", type_line="Basic Land - Forest",
            mana_cost="", cmc=0, power="0", toughness="0")
        fixtures = {
            "tempo": (
                lands
                + self._card_specs(
                    20, name="Tempo Delver", cmc=2,
                    type_line="Creature - Wizard",
                    oracle_text="Flash\nFlying\nProwess",
                    color_identity=["U"])
                + self._card_specs(
                    16, name="Tempo Bounce", cmc=1,
                    type_line="Instant", power="0", toughness="0",
                    oracle_text=(
                        "Return target creature to its owner's hand. "
                        "Counter target spell."),
                    color_identity=["U"]),
                (0.6, 0.5),
            ),
            "ramp": (
                lands
                + self._card_specs(
                    12, name="Ramp Mana Dork", cmc=1,
                    type_line="Creature - Elf Druid",
                    oracle_text=(
                        "Add mana. Search your library for a basic land."))
                + self._card_specs(
                    12, name="Cultivate Ramp Growth", cmc=3,
                    type_line="Sorcery", power="0", toughness="0",
                    oracle_text=(
                        "Search your library for two land cards. "
                        "Add mana."))
                + self._card_specs(
                    12, name="Ramp Colossus", cmc=7,
                    type_line="Creature - Beast", power="7",
                    toughness="7", oracle_text="Trample"),
                (0.4, 0.6),
            ),
            "tribal": (
                lands
                + self._card_specs(
                    30, name="Elf Tribal Lord", cmc=2,
                    type_line="Creature - Elf Warrior",
                    oracle_text=(
                        "Lord. Other creatures you control get +1/+1. "
                        "Choose a creature type."))
                + self._card_specs(
                    6, name="Elf Tribal Rally", cmc=2,
                    type_line="Instant", power="0", toughness="0",
                    oracle_text=(
                        "Creatures you control of the chosen creature type "
                        "get +1/+1.")),
                (0.6, 0.5),
            ),
        }

        for expected, (cards, numeric_params) in fixtures.items():
            with self.subTest(archetype=expected):
                planner = self._planner_for_deck(cards)
                detected = planner._detect_deck_archetype()
                self.assertEqual(detected, expected)
                self.assertEqual(planner.strategy_type, expected)
                self.assertEqual(
                    (planner.aggression_level, planner.risk_tolerance),
                    numeric_params)
                self.assertIs(
                    planner.strategy_params, planner.strategies[expected])

    def test_future_projection_is_observer_symmetric(self):
        cards = {
            1: SimpleNamespace(card_types=["creature"], power=3),
            2: SimpleNamespace(card_types=["creature"], power=3),
        }
        state = SimpleNamespace(
            p1={"life": 20, "hand": [10], "battlefield": [1]},
            p2={"life": 20, "hand": [11], "battlefield": [2]},
            agent_is_p1=True,
        )
        state._safe_get_card = cards.get
        planner = MTGStrategicPlanner(state)
        np.testing.assert_array_equal(
            planner.project_future_states(5), np.zeros(5, dtype=np.float32))

        state.p1.update(life=17, hand=[10, 12, 13])
        cards[1].power = 5
        p1_projection = planner.project_future_states(5)
        state.agent_is_p1 = False
        p2_projection = planner.project_future_states(5)
        np.testing.assert_allclose(
            p1_projection, -p2_projection, rtol=0.0, atol=1e-7)

    def test_multi_turn_plan_respects_used_current_land_drop(self):
        cards = {
            card_id: Card({
                "name": f"Forest {card_id}",
                "type_line": "Basic Land - Forest",
                "mana_cost": "", "cmc": 0,
                "oracle_text": "", "color_identity": ["G"],
            })
            for card_id in range(5)
        }
        player = {
            "life": 20,
            "hand": [3],
            "battlefield": [0, 1, 2],
            "library": [], "graveyard": [],
            "mana_pool": {},
            "land_played": True,
            "lands_played_this_turn": 1,
        }
        opponent = {
            "life": 20, "hand": [], "battlefield": [],
            "library": [], "graveyard": [], "mana_pool": {},
        }
        state = SimpleNamespace(
            p1=player, p2=opponent, agent_is_p1=True, turn=3)
        state._safe_get_card = cards.get
        planner = MTGStrategicPlanner(state)
        planner.evaluate_card_for_sequence = lambda _card: 0.5
        planner.analyze_game_state = lambda: {
            "position": {"overall": "even"},
            "game_info": {"game_stage": "early"},
        }
        planner.identify_win_conditions = lambda: {}
        planner.assess_threats = lambda: []

        used_drop_plan = planner.plan_multi_turn_sequence(depth=2)
        self.assertEqual(used_drop_plan[0]["expected_mana"], 3.0)
        self.assertIsNone(used_drop_plan[0]["land_play"])
        self.assertEqual(used_drop_plan[1]["expected_mana"], 4.0)
        self.assertEqual(
            used_drop_plan[1]["land_play"]["name"], "Forest 3")

        player["land_played"] = False
        player["lands_played_this_turn"] = 0
        fresh_drop_plan = planner.plan_multi_turn_sequence(depth=1)
        self.assertEqual(fresh_drop_plan[0]["expected_mana"], 4.0)
        self.assertEqual(
            fresh_drop_plan[0]["land_play"]["name"], "Forest 3")

        player["hand"] = []
        no_land_plan = planner.plan_multi_turn_sequence(depth=1)
        self.assertEqual(no_land_plan[0]["expected_mana"], 3.0)
        self.assertIsNone(no_land_plan[0]["land_play"])

        player["tapped_permanents"] = {0, 1}
        player["mana_pool"] = {"U": 2}
        player["phase_restricted_mana"] = {"G": 1}
        player["conditional_mana"] = {
            "cast_only:creature": {"G": 9}}
        live_mana_plan = planner.plan_multi_turn_sequence(depth=1)
        self.assertEqual(live_mana_plan[0]["expected_mana"], 4.0)
        self.assertIsNone(live_mana_plan[0]["land_play"])

        player["hand"] = [3, 4]
        player["tapped_permanents"] = set()
        player["mana_pool"] = {}
        player["phase_restricted_mana"] = {}
        state.land_play_limit = lambda _player: 2
        state.lands_played_this_turn = lambda _player: 0
        multi_drop_plan = planner.plan_multi_turn_sequence(depth=1)
        self.assertEqual(multi_drop_plan[0]["expected_mana"], 5.0)
        self.assertEqual(len(multi_drop_plan[0]["land_plays"]), 2)
        self.assertEqual(
            multi_drop_plan[0]["land_play"]["name"], "Forest 3")

        player["hand"] = []
        no_current_draw_plan = planner.plan_multi_turn_sequence(depth=1)
        self.assertEqual(no_current_draw_plan[0]["expected_mana"], 3.0)

    def test_play_evaluation_merges_mechanical_and_strategic_context(self):
        game_state = self._state()
        card_id = game_state.p1["battlefield"][0]
        captured = {}

        class CapturingEvaluator:
            @staticmethod
            def evaluate_card(observed_card_id, mode, context):
                captured.update(
                    card_id=observed_card_id, mode=mode, context=context)
                return 1.25

        planner = MTGStrategicPlanner(
            game_state, card_evaluator=CapturingEvaluator())
        planner.current_analysis = {
            "game_info": {"game_stage": "late"},
            "position": {"overall": "behind"},
        }
        planner.strategy_type = "control"
        planner.aggression_level = 0.2
        game_state.deck_archetypes = {0: "ramp", 1: "aggro"}

        value = planner.evaluate_play_card_action(
            card_id, context={"hand_idx": 0, "card_id": card_id})

        self.assertEqual(value, 1.25)
        self.assertEqual(captured["card_id"], card_id)
        self.assertEqual(captured["mode"], "play")
        self.assertEqual(captured["context"]["hand_idx"], 0)
        self.assertEqual(captured["context"]["game_stage"], "late")
        self.assertEqual(captured["context"]["position"], "behind")
        self.assertEqual(captured["context"]["aggression_level"], 0.2)
        self.assertEqual(captured["context"]["strategy_type"], "control")
        self.assertEqual(captured["context"]["deck_archetype"], "ramp")

    def test_action_indices_accept_binary_integer_masks(self):
        game_state = self._state()
        game_state.action_handler = SimpleNamespace(ACTION_SPACE_SIZE=480)
        planner = MTGStrategicPlanner(game_state)
        action_space_size = game_state.action_handler.ACTION_SPACE_SIZE
        integer_mask = np.zeros(action_space_size, dtype=np.int8)
        integer_mask[[20, 100, 438]] = 1

        self.assertEqual(
            planner._valid_action_indices(integer_mask), [20, 100, 438])
        self.assertEqual(
            planner._valid_action_indices(np.array([0, 1], dtype=np.int64)),
            [0, 1])

    def test_precomputed_planner_inputs_avoid_duplicate_reads(self):
        planner = self._planner_for_deck([])
        planner.analyze_game_state = Mock(side_effect=AssertionError(
            "analysis was recomputed"))
        planner.identify_win_conditions = Mock(side_effect=AssertionError(
            "win conditions were recomputed"))
        planner.assess_threats = Mock(side_effect=AssertionError(
            "threats were recomputed"))
        analysis = {
            "position": {"overall": "even"},
            "game_info": {"game_stage": "early"},
        }

        plan = planner.plan_multi_turn_sequence(
            depth=1,
            analysis=analysis,
            win_conditions={},
            opponent_threats=[],
        )

        self.assertEqual(len(plan), 1)
        planner.analyze_game_state.assert_not_called()
        planner.identify_win_conditions.assert_not_called()
        planner.assess_threats.assert_not_called()

        helper_planner = SimpleNamespace(
            plan_multi_turn_sequence=Mock(side_effect=AssertionError(
                "plan was recomputed")),
            assess_threats=Mock(side_effect=AssertionError(
                "threat list was recomputed")),
        )
        environment = SimpleNamespace(
            strategic_planner=helper_planner,
            max_battlefield=4,
        )
        precomputed_plan = [{
            "plays": [], "land_play": None, "expected_mana": 2.0,
        }]
        metrics = AlphaZeroMTGEnv._get_multi_turn_plan_metrics(
            environment, plan=precomputed_plan)
        threats = AlphaZeroMTGEnv._get_threat_assessment(
            environment, [91],
            threat_list=[{"card_id": 91, "level": 5.0}],
        )

        self.assertEqual(metrics[2], 0.2)
        self.assertEqual(threats[0], 0.5)
        helper_planner.plan_multi_turn_sequence.assert_not_called()
        helper_planner.assess_threats.assert_not_called()

    def test_block_evaluation_is_finite_and_restores_combat_state(self):
        for resolver_raises in (False, True):
            with self.subTest(resolver_raises=resolver_raises):
                game_state = self._state()
                blocker_id = game_state.p1["battlefield"][0]
                attacker_id = game_state.p2["battlefield"][0]
                attacker = game_state._safe_get_card(attacker_id)
                blocker = game_state._safe_get_card(blocker_id)
                attacker.power = "*"
                attacker.toughness = float("inf")
                blocker.power = "X"
                blocker.toughness = float("nan")

                original_attackers = [blocker_id]
                original_blocks = {blocker_id: [attacker_id]}
                game_state.current_attackers = original_attackers
                game_state.current_block_assignments = original_blocks
                game_state.agent_is_p1 = True

                class Resolver:
                    def simulate_combat(self):
                        self.assert_staged_state()
                        if resolver_raises:
                            raise RuntimeError("fixture simulation failure")
                        return {
                            "damage_to_player": 0,
                            "attackers_dying": [attacker_id],
                            "blockers_dying": [],
                        }

                    @staticmethod
                    def assert_staged_state():
                        if game_state.agent_is_p1:
                            raise AssertionError(
                                "simulation must use the attacker's perspective")
                        if game_state.current_attackers != [attacker_id]:
                            raise AssertionError("attacker was not staged")
                        if game_state.current_block_assignments != {
                                attacker_id: [blocker_id]}:
                            raise AssertionError("block was not staged")

                planner = MTGStrategicPlanner(
                    game_state, combat_resolver=Resolver())
                planner.current_analysis = {
                    "game_info": {"game_stage": "mid"}}
                with patch(
                        "Playersim.strategic_planner_evaluation.logging.warning"):
                    value = planner.evaluate_block_action(
                        attacker_id, [blocker_id])

                self.assertTrue(math.isfinite(value))
                self.assertIs(game_state.current_attackers, original_attackers)
                self.assertIs(
                    game_state.current_block_assignments, original_blocks)
                self.assertTrue(game_state.agent_is_p1)

    def test_one_ply_search_dispatches_real_actions_and_context(self):
        cards = {
            101: SimpleNamespace(
                name="Attacker", type_line="Creature", card_types=[
                    "creature"]),
            102: SimpleNamespace(
                name="Ability Source", type_line="Creature", card_types=[
                    "creature"]),
            201: SimpleNamespace(
                name="Spell", type_line="Sorcery", card_types=["sorcery"]),
            301: SimpleNamespace(
                name="Enemy", type_line="Creature", card_types=[
                    "creature"]),
        }

        class RealActionHandlerStub:
            action_info = {
                20: ("PLAY_SPELL", 0),
                28: ("ATTACK", 0),
                48: ("BLOCK", 0),
                100: ("ACTIVATE_ABILITY", None),
            }
            action_reasons_with_context = {
                20: {"context": {"hand_idx": 0, "card_id": 201}},
                28: {
                    "context": {"battlefield_idx": 0, "card_id": 101}},
                48: {"context": {
                    "battlefield_idx": 0,
                    "card_id": 101,
                    "target_attacker_id": 301,
                }},
                100: {"context": {
                    "battlefield_idx": 1,
                    "card_id": 102,
                    "ability_idx": 2,
                }},
            }

            def get_action_info(self, action_idx):
                return self.action_info[action_idx]

        state = SimpleNamespace(
            p1={
                "life": 20, "hand": [201], "battlefield": [101, 102],
                "library": [], "graveyard": [], "mana_pool": {}},
            p2={
                "life": 20, "hand": [], "battlefield": [301],
                "library": [], "graveyard": [], "mana_pool": {}},
            agent_is_p1=True,
            stack=[],
            strategy_memory=None,
            action_handler=RealActionHandlerStub(),
        )
        state._safe_get_card = cards.get
        state.can_play_land_this_turn = lambda _player: False

        calls = []
        planner = MTGStrategicPlanner(state)
        planner.current_analysis = {}
        planner.analyze_game_state = lambda: None
        planner.adapt_strategy = lambda: None
        planner.evaluate_play_card_action = lambda card_id, context=None: (
            calls.append(("play", card_id, context)) or 1.0)
        planner.evaluate_attack_action = lambda attacker_ids: (
            calls.append(("attack", attacker_ids)) or 2.0)
        planner.evaluate_block_action = lambda attacker_id, blocker_ids: (
            calls.append(("block", attacker_id, blocker_ids)) or 3.0)
        planner.evaluate_ability_activation = lambda card_id, ability_idx: (
            calls.append(("ability", card_id, ability_idx))
            or (4.0, "Ability"))

        expected = {
            20: (1.0, ("play", 201)),
            28: (2.0, ("attack", [101])),
            48: (3.0, ("block", 301, [101])),
            100: (4.0, ("ability", 102, 2)),
        }
        for action_idx, (expected_value, expected_call) in expected.items():
            calls.clear()
            value, _ = planner._evaluate_action_candidate(action_idx)
            self.assertEqual(value, expected_value)
            self.assertEqual(calls[0][:len(expected_call)], expected_call)

        sequence, value = planner.find_best_play_sequence(
            np.array([20, 28, 48, 100]), depth=5)
        self.assertEqual(sequence, [100])
        self.assertEqual(value, 4.0)
        self.assertEqual(
            planner.recommend_action(np.array([20, 28, 48, 100])), 100)


if __name__ == "__main__":
    unittest.main()
