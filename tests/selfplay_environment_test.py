"""Perspective and frozen-opponent contracts for Round 7.98 self-play."""

import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from gymnasium import spaces


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from Playersim.ability_types import TriggeredAbility  # noqa: E402
from Playersim.card import Card  # noqa: E402
from Playersim.environment import AlphaZeroMTGEnv  # noqa: E402


def _fixture_data():
    agent_card = Card({
        "name": "Agent Hidden Adept",
        "mana_cost": "{U}",
        "type_line": "Creature - Wizard",
        "color_identity": ["U"],
        "power": 1,
        "toughness": 1,
        "oracle_text": "",
    })
    opponent_card = Card({
        "name": "Opponent Hidden Brute",
        "mana_cost": "{R}",
        "type_line": "Creature - Warrior",
        "color_identity": ["R"],
        "power": 3,
        "toughness": 2,
        "oracle_text": "",
    })
    agent_card.card_id = 0
    opponent_card.card_id = 1
    return (
        [
            {"name": "Agent Deck", "cards": [0] * 60},
            {"name": "Opponent Deck", "cards": [1] * 60},
        ],
        {0: agent_card, 1: opponent_card},
    )


class _CapturePolicy:
    def __init__(self, *, illegal=False):
        self.illegal = illegal
        self.calls = []

    def predict(self, observation, action_masks=None, deterministic=True):
        copied_observation = {
            key: np.asarray(value).copy()
            for key, value in observation.items()
        }
        copied_mask = np.asarray(action_masks, dtype=bool).copy()
        self.calls.append((copied_observation, copied_mask, deterministic))
        candidates = np.flatnonzero(~copied_mask if self.illegal else copied_mask)
        return int(candidates[0]), None


class SelfPlayEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        decks, card_db = _fixture_data()
        self.env = AlphaZeroMTGEnv(
            decks,
            card_db,
            deck_stats_path=os.path.join(self.root.name, "deck_stats"),
            card_memory_path=os.path.join(self.root.name, "card_memory"),
        )
        self.env.reset(seed=7978, options={
            "p1_deck": "Agent Deck",
            "p2_deck": "Opponent Deck",
            "agent_is_p1": True,
        })

    def tearDown(self):
        self.env.close()
        self.root.cleanup()

    def _stage_opponent_trigger_choice(self):
        gs = self.env.game_state
        opponent = gs.p2
        abilities = [
            TriggeredAbility(
                9100 + index,
                trigger_condition="at the beginning of your upkeep",
                effect="you gain 1 life",
            )
            for index in range(2)
        ]
        batch = [
            (ability, opponent, {
                "ability": ability,
                "effect_text": ability.effect_text,
            })
            for ability in abilities
        ]
        gs.ability_handler._stack_trigger_batch_with_choice(batch)
        self.assertIs(gs.choice_context["player"], opponent)
        return opponent

    def _stage_opponent_mode_choice(self):
        gs = self.env.game_state
        opponent = gs.p2
        gs.mulligan_in_progress = False
        gs.phase = gs.PHASE_CHOOSE
        gs.choice_context = {
            "type": "choose_mode",
            "player": opponent,
            "options": ["first", "second"],
            "resume_phase": gs.PHASE_PRIORITY,
        }
        return opponent

    def test_direct_prediction_is_opponent_private_and_state_safe(self):
        env = self.env
        gs = env.game_state
        learned = gs.p1
        gs.agent_is_p1 = True
        gs.mulligan_in_progress = False
        gs.choice_context = None
        gs.phase = gs.PHASE_PRIORITY
        gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_player = learned
        gs.priority_pass_count = 0

        learned_mask = env.action_mask()
        original_env_mask = env.current_valid_actions
        original_handler_mask = getattr(
            env.action_handler, "current_valid_actions", None)
        original_reasons = env.action_handler.action_reasons
        original_contexts = getattr(
            env.action_handler, "action_reasons_with_context", None)
        original_mask_error = getattr(
            env.action_handler, "last_mask_error", None)
        original_env_mask_error = getattr(
            env, "last_action_mask_error", None)
        original_analysis = env.current_analysis
        planner_analysis = getattr(env.strategic_planner, "current_analysis", None)
        env.last_observation_error = "learned-seat diagnostic"
        env.last_observation_traceback = "learned-seat traceback"

        opponent = self._stage_opponent_trigger_choice()
        expected_mask = env.action_mask_for(opponent)
        self.assertFalse(np.array_equal(learned_mask, expected_mask))
        policy = _CapturePolicy()
        env.set_opponent_policy(policy)
        action, context = env._get_opponent_policy_action(
            opponent, expected_mask, {"phase_context": "CHOOSE"})

        self.assertEqual(action, int(np.flatnonzero(expected_mask)[0]))
        self.assertEqual(context, {"option_index": 0})
        self.assertEqual(len(policy.calls), 1)
        observation, predicted_mask, deterministic = policy.calls[0]
        self.assertTrue(deterministic)
        self.assertTrue(np.array_equal(predicted_mask, expected_mask))
        self.assertTrue(np.array_equal(
            observation["my_hand_card_identity"],
            env._get_zone_identities(
                opponent["hand"], env.hand_observation_size)))
        self.assertFalse(np.array_equal(
            observation["my_hand_card_identity"],
            env._get_zone_identities(
                gs.p1["hand"], env.hand_observation_size)))
        self.assertTrue(np.array_equal(
            observation["my_deck_card_identity"],
            env._get_deck_card_identities(opponent)))
        self.assertFalse(np.array_equal(
            observation["my_deck_card_identity"],
            env._get_deck_card_identities(gs.p1)))
        self.assertTrue(np.array_equal(
            observation["action_mask"], expected_mask))

        self.assertTrue(gs.agent_is_p1)
        self.assertIs(env.current_valid_actions, original_env_mask)
        self.assertIs(
            getattr(env.action_handler, "current_valid_actions", None),
            original_handler_mask)
        self.assertIs(env.action_handler.action_reasons, original_reasons)
        self.assertIs(getattr(
            env.action_handler, "action_reasons_with_context", None),
            original_contexts)
        self.assertIs(getattr(
            env.action_handler, "last_mask_error", None),
            original_mask_error)
        self.assertIs(
            getattr(env, "last_action_mask_error", None),
            original_env_mask_error)
        self.assertIs(env.current_analysis, original_analysis)
        self.assertIs(
            getattr(env.strategic_planner, "current_analysis", None),
            planner_analysis)
        self.assertEqual(
            env.last_observation_error, "learned-seat diagnostic")
        self.assertEqual(
            env.last_observation_traceback, "learned-seat traceback")

    def test_mask_invalid_checkpoint_prediction_fails_closed_and_restores(self):
        env = self.env
        gs = env.game_state
        gs.agent_is_p1 = True
        gs.mulligan_in_progress = False
        gs.choice_context = None
        gs.phase = gs.PHASE_PRIORITY
        gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_player = gs.p1
        gs.priority_pass_count = 0
        original_mask = env.action_mask()
        original_contexts = getattr(
            env.action_handler, "action_reasons_with_context", None)
        opponent = self._stage_opponent_trigger_choice()
        opponent_mask = env.action_mask_for(opponent)
        env.set_opponent_policy(_CapturePolicy(illegal=True))

        with self.assertRaisesRegex(RuntimeError, "mask-invalid"):
            env._get_opponent_policy_action(
                opponent, opponent_mask, {"phase_context": "CHOOSE"})

        self.assertTrue(gs.agent_is_p1)
        self.assertTrue(np.array_equal(env.current_valid_actions, original_mask))
        self.assertIs(
            getattr(env.action_handler, "action_reasons_with_context", None),
            original_contexts)

    def test_observer_boundary_restores_planner_archetype_on_success_and_error(self):
        env = self.env
        gs = env.game_state
        learned = gs.p1
        opponent = gs.p2
        gs.agent_is_p1 = True
        gs.mulligan_in_progress = False
        gs.phase = gs.PHASE_PRIORITY
        gs.priority_player = learned

        # Make the two views asymmetric enough that opponent archetype
        # inference really writes its cache instead of returning the empty-
        # evidence prior early.
        public_card_id = learned["hand"].pop()
        learned["battlefield"].append(public_card_id)
        planner = env.strategic_planner
        planner.opponent_archetype = "learned-seat-sentinel"
        planner.strategy_type = "learned-private-sentinel"
        planner.strategy_params = {
            "aggression_level": 0.99,
            "risk_tolerance": 0.01,
            "card_weights": {},
        }
        planner.aggression_level = 0.99
        planner.risk_tolerance = 0.01
        observed_strategy_types = []
        original_analyze = planner.analyze_game_state

        def capture_observer_strategy(*args, **kwargs):
            observed_strategy_types.append(planner.strategy_type)
            return original_analyze(*args, **kwargs)

        with mock.patch.object(
                planner, "analyze_game_state",
                side_effect=capture_observer_strategy):
            env.observation_for(opponent)

        self.assertEqual(
            planner.opponent_archetype, "learned-seat-sentinel")
        self.assertEqual(planner.strategy_type, "learned-private-sentinel")
        self.assertEqual(planner.aggression_level, 0.99)
        self.assertEqual(planner.risk_tolerance, 0.01)
        opponent_profile = env._observer_strategy_profiles[False]
        self.assertTrue(observed_strategy_types)
        self.assertEqual(
            set(observed_strategy_types),
            {opponent_profile["strategy_type"]})
        self.assertTrue(gs.agent_is_p1)

        def fail_after_mutating_cache():
            planner.opponent_archetype = "opponent-seat-error"
            raise RuntimeError("synthetic opponent planner failure")

        with mock.patch.object(
                planner, "predict_opponent_archetype",
                side_effect=fail_after_mutating_cache):
            with self.assertRaisesRegex(RuntimeError, "degraded"):
                env.observation_for(opponent)

        self.assertEqual(
            planner.opponent_archetype, "learned-seat-sentinel")
        self.assertTrue(gs.agent_is_p1)

    def test_opponent_mask_generation_fault_fails_closed_in_production_step(self):
        env = self.env
        gs = env.game_state
        learned = gs.p1
        opponent = gs.p2
        gs.agent_is_p1 = True
        gs.mulligan_in_progress = False
        gs.choice_context = None
        gs.targeting_context = None
        gs.phase = gs.PHASE_PRIORITY
        gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_player = learned
        gs.priority_pass_count = 0
        env.set_opponent_policy(_CapturePolicy())
        applied_perspectives = []
        original_generate = env.action_handler.generate_valid_actions

        def perspective_sensitive_generate(_handler):
            if not gs.agent_is_p1:
                raise RuntimeError("synthetic opponent mask failure")
            return original_generate()

        def fake_apply(_handler, _action, context=None):
            applied_perspectives.append(bool(gs.agent_is_p1))
            return 0.0, False, False, {}

        with mock.patch.object(
                type(env.action_handler), "generate_valid_actions",
                new=perspective_sensitive_generate), \
                mock.patch.object(
                    type(env.action_handler), "apply_action", new=fake_apply), \
                mock.patch.object(
                    env, "_opponent_needs_to_act",
                    return_value=(
                        opponent, {"phase_context": "priority"})):
            with self.assertRaisesRegex(RuntimeError, "degraded"):
                env.action_mask_for(opponent)
            observation, _reward, done, truncated, info = env.step(11)

        self.assertEqual(applied_perspectives, [True])
        self.assertTrue(done)
        self.assertFalse(truncated)
        self.assertTrue(info.get("critical_error"))
        self.assertEqual(info.get("terminal_reason"), "engine_error")
        self.assertIn("opponent action mask", info.get("error_message", "").lower())
        self.assertTrue(gs.agent_is_p1)
        self.assertTrue(np.array_equal(
            observation["my_hand_card_identity"],
            env._get_zone_identities(
                learned["hand"], env.hand_observation_size)))

    def test_opponent_observation_hides_learned_hand_and_library_mutations(self):
        env = self.env
        gs = env.game_state
        learned = gs.p1
        opponent = self._stage_opponent_mode_choice()
        gs.agent_is_p1 = True

        hidden_ids = []
        for index, card_type in enumerate(
                ("Creature - Scout", "Instant", "Sorcery"), start=1):
            card_data = {
                "name": f"Learned Hidden Variant {index}",
                "mana_cost": "{1}",
                "type_line": card_type,
                "color_identity": ["G"],
                "oracle_text": "",
            }
            if card_type.startswith("Creature"):
                card_data.update({"power": 1, "toughness": 1})
            card = Card(card_data)
            card_id = 9900 + index
            card.card_id = card_id
            gs.card_db[card_id] = card
            hidden_ids.append(card_id)

        before = env.observation_for(opponent)
        learned["hand"][0] = hidden_ids[0]
        learned["library"][0] = hidden_ids[1]
        learned["library"][1] = hidden_ids[2]
        after = env.observation_for(opponent)

        self.assertEqual(set(before), set(after))
        for key in before:
            with self.subTest(key=key):
                self.assertTrue(
                    np.array_equal(before[key], after[key]),
                    f"learned hidden identity leaked through {key}")

    def test_public_threat_scores_ignore_other_seat_hidden_hand_identity(self):
        env = self.env
        gs = env.game_state
        learned = gs.p1
        opponent = gs.p2
        gs.agent_is_p1 = True

        for offset, (owner, observer) in enumerate(
                ((learned, opponent), (opponent, learned))):
            public_id = 9950 + offset * 2
            hidden_id = public_id + 1
            public_battery = Card({
                "name": f"Public Battery {offset}",
                "mana_cost": "{2}",
                "type_line": "Artifact",
                "color_identity": ["R"],
                "oracle_text": (
                    "At the beginning of your upkeep, Public Battery deals "
                    "7 damage to any target."),
            })
            hidden_battery = Card({
                "name": f"Hidden Battery {offset}",
                "mana_cost": "{1}{R}",
                "type_line": "Instant",
                "color_identity": ["R"],
                "oracle_text": "Hidden Battery deals 7 damage to any target.",
            })
            public_battery.card_id = public_id
            hidden_battery.card_id = hidden_id
            gs.card_db[public_id] = public_battery
            gs.card_db[hidden_id] = hidden_battery
            owner["battlefield"].append(public_id)

            original_hidden_card = owner["hand"][0]
            before = env.observation_for(observer)
            self.assertGreater(float(before["threat_assessment"][0]), 0.0)
            owner["hand"][0] = hidden_id
            after = env.observation_for(observer)
            owner["hand"][0] = original_hidden_card

            self.assertEqual(set(before), set(after))
            for key in before:
                with self.subTest(owner=offset, key=key):
                    self.assertTrue(
                        np.array_equal(before[key], after[key]),
                        f"hidden hand leaked through {key}")
            self.assertTrue(gs.agent_is_p1)

    def test_policy_history_is_role_local_for_opponent_observations(self):
        env = self.env
        gs = env.game_state
        learned = gs.p1
        opponent = gs.p2
        gs.mulligan_in_progress = False
        gs.choice_context = None
        gs.targeting_context = None
        gs.phase = gs.PHASE_PRIORITY
        gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_pass_count = 0

        env.last_n_actions[0] = 17
        env.last_n_rewards[0] = 0.75
        env.opponent_last_n_actions[0] = 219
        env.opponent_last_n_rewards[0] = -0.25

        gs.priority_player = learned
        learned_observation = env.observation_for(learned)
        gs.priority_player = opponent
        opponent_observation = env.observation_for(opponent)

        self.assertEqual(int(learned_observation["previous_actions"][0]), 17)
        self.assertAlmostEqual(
            float(learned_observation["previous_rewards"][0]), 0.75)
        self.assertEqual(
            int(opponent_observation["previous_actions"][0]), 219)
        self.assertAlmostEqual(
            float(opponent_observation["previous_rewards"][0]), -0.25)

        env.last_n_actions[0] = 88
        env.last_n_rewards[0] = 99.0
        unchanged_opponent = env.observation_for(opponent)
        self.assertTrue(np.array_equal(
            unchanged_opponent["previous_actions"],
            opponent_observation["previous_actions"]))
        self.assertTrue(np.array_equal(
            unchanged_opponent["previous_rewards"],
            opponent_observation["previous_rewards"]))

        # Production workers alternate the learned role between P1 and P2.
        # The role-local selector must follow the seat committed by this
        # episode, not the constructor's base seat.
        env.alternate_agent_seat = True
        env.reset(seed=7979, options={
            "p1_deck": "Agent Deck",
            "p2_deck": "Opponent Deck",
        })
        gs = env.game_state
        self.assertFalse(gs.agent_is_p1)
        self.assertFalse(env._episode_agent_is_p1)
        learned = gs.p2
        opponent = gs.p1
        gs.mulligan_in_progress = False
        gs.choice_context = None
        gs.targeting_context = None
        gs.phase = gs.PHASE_PRIORITY
        gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_pass_count = 0
        env.last_n_actions[0] = 31
        env.last_n_rewards[0] = 0.5
        env.opponent_last_n_actions[0] = 404
        env.opponent_last_n_rewards[0] = -0.5

        gs.priority_player = learned
        learned_observation = env.observation_for(learned)
        gs.priority_player = opponent
        opponent_observation = env.observation_for(opponent)
        self.assertEqual(int(learned_observation["previous_actions"][0]), 31)
        self.assertEqual(
            int(opponent_observation["previous_actions"][0]), 404)

    def test_emergency_reset_rebuilds_planner_and_private_profiles(self):
        env = self.env
        old_game_state = env.game_state
        old_planner = env.strategic_planner
        old_profiles = env._observer_strategy_profiles
        old_profiles[True]["strategy_type"] = "stale-private-sentinel"
        env.last_n_actions[0] = 91
        env.last_n_rewards[0] = 8.5
        env.opponent_last_n_actions[0] = 192
        env.opponent_last_n_rewards[0] = -7.5
        env._observed_phase_history = [
            old_game_state.PHASE_DECLARE_ATTACKERS]

        observation, info = env._emergency_fallback_reset()

        self.assertTrue(info["error_reset"])
        self.assertIsNot(env.game_state, old_game_state)
        self.assertIsNot(env.strategic_planner, old_planner)
        self.assertIs(env.strategic_planner.game_state, env.game_state)
        self.assertIs(
            env.game_state.strategic_planner, env.strategic_planner)
        self.assertIsNot(env._observer_strategy_profiles, old_profiles)
        self.assertNotEqual(
            env._observer_strategy_profiles[True]["strategy_type"],
            "stale-private-sentinel")
        self.assertTrue(np.all(env.last_n_actions == -1))
        self.assertTrue(np.all(env.last_n_rewards == 0.0))
        self.assertTrue(np.all(env.opponent_last_n_actions == -1))
        self.assertTrue(np.all(env.opponent_last_n_rewards == 0.0))
        self.assertEqual(
            env._observed_phase_history,
            [env.game_state.PHASE_MAIN_PRECOMBAT])
        self.assertEqual(set(observation), set(env.observation_space.spaces))

    def test_production_step_applies_action_under_opponent_perspective(self):
        env = self.env
        gs = env.game_state
        learned = gs.p1
        opponent = gs.p2
        gs.agent_is_p1 = True
        gs.mulligan_in_progress = False
        gs.choice_context = None
        gs.targeting_context = None
        gs.phase = gs.PHASE_PRIORITY
        gs.previous_priority_phase = gs.PHASE_MAIN_PRECOMBAT
        gs.priority_player = learned
        gs.priority_pass_count = 0
        policy = _CapturePolicy()
        env.set_opponent_policy(policy)
        applied_perspectives = []

        def fake_apply(_handler, _action, context=None):
            applied_perspectives.append(bool(gs.agent_is_p1))
            if gs.agent_is_p1 is False:
                self.assertEqual(len(policy.calls), 1)
                self.assertTrue(np.array_equal(
                    _handler.current_valid_actions,
                    policy.calls[0][1]))
                return 2.5, False, False, {}
            return 0.0, False, False, {}

        with mock.patch.object(
                type(env.action_handler), "apply_action", new=fake_apply), \
                mock.patch.object(
                    env, "_opponent_needs_to_act",
                    side_effect=[
                        (opponent, {"phase_context": "priority"}),
                        (None, {}),
                    ]):
            self.assertTrue(env.action_mask()[11])
            observation, _reward, done, truncated, info = env.step(11)

        self.assertEqual(applied_perspectives, [True, False])
        self.assertFalse(done)
        self.assertFalse(truncated)
        self.assertFalse(info.get("critical_error"))
        self.assertTrue(gs.agent_is_p1)
        self.assertTrue(np.array_equal(
            observation["my_hand_card_identity"],
            env._get_zone_identities(
                learned["hand"], env.hand_observation_size)))
        self.assertEqual(len(policy.calls), 1)
        self.assertEqual(
            int(env.opponent_last_n_actions[0]),
            int(np.flatnonzero(policy.calls[0][1])[0]))
        self.assertAlmostEqual(
            float(env.opponent_last_n_rewards[0]),
            env.action_reward_scale * 2.5)

    def test_checkpoint_assignment_is_verified_deterministic_and_episode_scoped(self):
        env = self.env
        with tempfile.TemporaryDirectory() as checkpoint_root:
            entries = []
            loaded = {}
            for index in range(2):
                path = os.path.join(checkpoint_root, f"frozen-{index}.zip")
                payload = f"frozen checkpoint {index}".encode("utf-8")
                with open(path, "wb") as handle:
                    handle.write(payload)
                policy = _CapturePolicy()
                loaded[os.path.abspath(path)] = policy
                entries.append({
                    "path": path,
                    "policy_id": f"round-7977-{index}",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })

            with mock.patch.object(
                    env, "_load_checkpoint_opponent_policy",
                    side_effect=lambda path: loaded[os.path.abspath(path)]):
                status = env.stage_checkpoint_opponent(
                    entries[0], probability=0.5, seed=44)

            self.assertIsNone(status["active"])
            self.assertEqual(status["status"], "staged")
            self.assertEqual(
                status["pending_policy_id"], "round-7977-0")
            self.assertEqual(
                status["pending_checkpoint_sha256"],
                entries[0]["sha256"])
            self.assertEqual(status["probability"], 0.5)
            self.assertEqual(status["seed"], 44)
            self.assertEqual(
                status["pending"]["policy_id"], "round-7977-0")
            self.assertIsNone(status["current_policy_id"])
            self.assertIsNone(env.opponent_policy)

            selections = []
            for seed in (100, 101, 102, 103):
                env.reset(seed=seed, options={
                    "p1_deck": "Agent Deck",
                    "p2_deck": "Opponent Deck",
                    "agent_is_p1": True,
                })
                selections.append(
                    env.checkpoint_opponent_status()[
                        "current_policy_id"])
                current_policy = env.opponent_policy
                env.action_mask()
                self.assertIs(env.opponent_policy, current_policy)

            first_resident = env._resident_checkpoint_opponent_policy
            with mock.patch.object(
                    env, "_load_checkpoint_opponent_policy",
                    side_effect=lambda path: loaded[os.path.abspath(path)]):
                status = env.stage_checkpoint_opponent(
                    entries[1], probability=1.0, seed=45)
            self.assertIs(env._resident_checkpoint_opponent_policy, first_resident)
            self.assertEqual(
                status["active"]["policy_id"], "round-7977-0")
            self.assertEqual(
                status["pending"]["policy_id"], "round-7977-1")
            env.reset(seed=104, options={
                "p1_deck": "Agent Deck",
                "p2_deck": "Opponent Deck",
                "agent_is_p1": True,
            })
            self.assertIs(
                env._resident_checkpoint_opponent_policy,
                loaded[os.path.abspath(entries[1]["path"])])
            self.assertIsNot(
                env._resident_checkpoint_opponent_policy, first_resident)
            self.assertEqual(
                env.checkpoint_opponent_status()["current_policy_id"],
                "round-7977-1")

            replay_decks, replay_db = _fixture_data()
            replay_env = AlphaZeroMTGEnv(
                replay_decks,
                replay_db,
                deck_stats_path=os.path.join(
                    self.root.name, "replay_deck_stats"),
                card_memory_path=os.path.join(
                    self.root.name, "replay_card_memory"),
            )
            try:
                replay_loaded = {
                    os.path.abspath(entry["path"]): _CapturePolicy()
                    for entry in entries
                }
                with mock.patch.object(
                        replay_env, "_load_checkpoint_opponent_policy",
                        side_effect=lambda path: replay_loaded[
                            os.path.abspath(path)]):
                    replay_env.stage_checkpoint_opponent(
                        entries[0], probability=0.5, seed=44)
                replay_selections = []
                for seed in (300, 301, 302, 303):
                    replay_env.reset(seed=seed, options={
                        "p1_deck": "Agent Deck",
                        "p2_deck": "Opponent Deck",
                        "agent_is_p1": True,
                    })
                    replay_selections.append(
                        replay_env.checkpoint_opponent_status()[
                            "current_policy_id"])
                self.assertEqual(replay_selections, selections)
            finally:
                replay_env.close()

            env.clear_checkpoint_opponent()
            self.assertIsNotNone(env.opponent_policy)
            env.reset(seed=105, options={
                "p1_deck": "Agent Deck",
                "p2_deck": "Opponent Deck",
                "agent_is_p1": True,
            })
            self.assertIsNone(env.opponent_policy)
            cleared = env.checkpoint_opponent_status()
            self.assertIsNone(cleared["active"])
            self.assertIsNone(cleared["pending"])

    def test_checkpoint_assignment_rejects_bad_hash_without_partial_install(self):
        env = self.env
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            handle.write(b"checkpoint")
            path = handle.name
        try:
            before = env.checkpoint_opponent_status()
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                env.stage_checkpoint_opponent({
                    "path": path,
                    "policy_id": "corrupt",
                    "sha256": "0" * 64,
                }, probability=1.0, seed=1)
            after = env.checkpoint_opponent_status()
            self.assertEqual(after["active"], before["active"])
            self.assertEqual(after["pending"], before["pending"])
            self.assertIn("SHA-256", after["last_error"])
        finally:
            os.unlink(path)

    def test_checkpoint_assignment_rejects_incompatible_policy_at_stage(self):
        env = self.env
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            payload = b"incompatible checkpoint"
            handle.write(payload)
            path = handle.name
        incompatible = _CapturePolicy()
        incompatible.observation_space = spaces.Box(
            low=0, high=1, shape=(3,), dtype=np.float32)
        incompatible.action_space = env.action_space
        try:
            with mock.patch.object(
                    env, "_load_checkpoint_opponent_policy",
                    return_value=incompatible):
                with self.assertRaisesRegex(ValueError, "observation space"):
                    env.stage_checkpoint_opponent({
                        "path": path,
                        "policy_id": "wrong-observation",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }, probability=1.0, seed=9)
            status = env.checkpoint_opponent_status()
            self.assertIsNone(status["active"])
            self.assertIsNone(status["pending"])
            self.assertIn("observation space", status["last_error"])
        finally:
            os.unlink(path)

    def test_checkpoint_loader_keeps_only_frozen_policy(self):
        env = self.env

        class Parameter:
            def __init__(self):
                self.requires_grad = True

            def requires_grad_(self, enabled):
                self.requires_grad = bool(enabled)

        parameter = Parameter()
        policy = _CapturePolicy()
        policy.observation_space = env.observation_space
        policy.action_space = env.action_space
        policy.optimizer = object()
        policy.parameters = lambda: [parameter]
        policy.set_training_mode = mock.Mock()
        policy.eval = mock.Mock()
        algorithm = mock.Mock(
            observation_space=env.observation_space,
            action_space=env.action_space,
            policy=policy,
        )
        with mock.patch(
                "sb3_contrib.MaskablePPO.load", return_value=algorithm) as load:
            frozen = env._load_checkpoint_opponent_policy("frozen.zip")

        load.assert_called_once_with("frozen.zip", device="cpu")
        self.assertIs(frozen, policy)
        policy.set_training_mode.assert_called_once_with(False)
        policy.eval.assert_called_once_with()
        self.assertFalse(parameter.requires_grad)
        self.assertIsNone(policy.optimizer)

    def test_direct_opponent_policy_persists_across_reset_without_lease(self):
        env = self.env
        policy = _CapturePolicy()
        env.set_opponent_policy(policy)

        env.reset(seed=8080, options={
            "p1_deck": "Agent Deck",
            "p2_deck": "Opponent Deck",
            "agent_is_p1": True,
        })

        self.assertIs(env.opponent_policy, policy)
        self.assertTrue(
            env.checkpoint_opponent_status()["direct_policy_installed"])


if __name__ == "__main__":
    unittest.main()
