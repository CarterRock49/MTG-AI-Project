"""Round 7.91 curriculum ramps: annealed handicap and stage turn limits.

Round 7.91: rounds 7.88-7.90 showed a difficulty cliff between the passive
and novice profiles (~60% decisive wins collapsing to ~5%).  The handicap
gives active stages a climbable slope; these tests pin its three promises:

1. The trainer ratchets epsilon toward zero only on full windows of wins at
   the CURRENT epsilon, resets it per stage, and persists it across restore.
2. Mastery requires the anneal to finish and full-strength profile evidence.
3. The environment commits a staged handicap only at reset, only for the
   handicapped profiles, never for fixed evaluation schedules, and the
   handicapped opponent declines optional aggression deterministically.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import main as m  # noqa: E402
from scenario_test import fresh, get_env, inject_into_zone  # noqa: E402


HANDICAP_CURRICULUM = {
    "id": "handicap-test",
    "version": 1,
    "progression": "mastery",
    "stages": [
        {
            "name": "ramp",
            "start_timestep": 0,
            "handicap": {
                "profiles": ["novice"],
                "start": 0.50,
                "step": 0.25,
                "window_episodes": 2,
                "min_decisive_win_rate": 0.5,
            },
            "advance_when": {
                "window_episodes": 4,
                "min_stage_timesteps": 10,
                "min_decisive_win_rate": 0.5,
                "max_decisive_loss_rate": 0.5,
                "max_timeout_rate": 1.0,
                "profile_requirements": {
                    "novice": {
                        "min_episodes": 2,
                        "min_decisive_win_rate": 0.5,
                    },
                },
            },
        },
        {"name": "apply", "start_timestep": 10},
    ],
}


class EnvMethodProbe:
    def __init__(self):
        self.calls = []
        self.num_envs = 1

    def env_method(self, name, *args, **kwargs):
        self.calls.append((name, args))
        return [None]

    def handicap_calls(self):
        return [call for call in self.calls
                if call[0] == "set_opponent_handicap"]


def build_callback(curriculum=HANDICAP_CURRICULUM):
    probe = EnvMethodProbe()
    metrics = {}
    callback = m.CurriculumProgressCallback(curriculum)
    callback.model = SimpleNamespace(
        get_env=lambda: probe,
        logger=SimpleNamespace(
            record=lambda name, value: metrics.__setitem__(name, value)),
    )
    callback.num_timesteps = 0
    callback._on_training_start()
    return callback, probe, metrics


def record_outcome(callback, profile, result, handicap, stage="ramp"):
    callback.locals = {
        "infos": [{
            "curriculum_stage": stage,
            "opponent_profile": profile,
            "opponent_handicap": handicap,
            "game_result": result,
            "terminal_reason": "life_total",
        }],
        "dones": np.array([True]),
    }
    assert callback._on_step()


class HandicapRatchetTest(unittest.TestCase):
    def test_epsilon_starts_from_stage_config_and_broadcasts(self):
        callback, probe, metrics = build_callback()
        self.assertEqual(callback._handicap_epsilon, 0.50)
        self.assertEqual(
            probe.handicap_calls(), [("set_opponent_handicap",
                                      (0.50, ["novice"]))])
        callback.num_timesteps = 1
        record_outcome(callback, "novice", "loss", 0.50)
        self.assertEqual(metrics["curriculum/handicap_epsilon"], 0.50)

    def test_ratchet_requires_full_window_of_wins_at_current_epsilon(self):
        callback, probe, _ = build_callback()
        callback.num_timesteps = 1

        # Wins recorded at a stale epsilon never advance the ratchet.
        record_outcome(callback, "novice", "win", 0.75)
        record_outcome(callback, "novice", "win", 0.75)
        self.assertEqual(callback._handicap_epsilon, 0.50)

        # Passive wins are not ramp evidence either.
        record_outcome(callback, "passive", "win", 0.0)
        self.assertEqual(callback._handicap_epsilon, 0.50)

        # A full window at the live epsilon ratchets one step and
        # broadcasts the new value.
        record_outcome(callback, "novice", "win", 0.50)
        record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        self.assertEqual(
            probe.handicap_calls()[-1],
            ("set_opponent_handicap", (0.25, ["novice"])))

        # The window restarts at the new epsilon: the same records do not
        # double-count.
        record_outcome(callback, "novice", "win", 0.25)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        record_outcome(callback, "novice", "win", 0.25)
        self.assertEqual(callback._handicap_epsilon, 0.0)

    def test_mastery_waits_for_anneal_and_full_strength_floor(self):
        callback, _, _ = build_callback()
        callback.num_timesteps = 10

        # Weakened wins satisfy the aggregate window but neither the anneal
        # nor the full-strength novice floor.
        record_outcome(callback, "novice", "win", 0.50)
        record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        record_outcome(callback, "novice", "win", 0.25)
        record_outcome(callback, "novice", "win", 0.25)
        self.assertEqual(callback._handicap_epsilon, 0.0)
        self.assertEqual(callback._active_stage_index, 0)

        # Full-strength wins complete the floor and master the stage.
        record_outcome(callback, "novice", "win", 0.0)
        self.assertEqual(callback._active_stage_index, 0)
        record_outcome(callback, "novice", "win", 0.0)
        self.assertEqual(callback._active_stage_index, 1)
        state = getattr(
            callback.model, m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
        self.assertEqual(state["transition_history"][-1]["reason"], "mastery")

    def test_epsilon_resets_per_stage_and_survives_restore(self):
        callback, probe, _ = build_callback()
        callback.num_timesteps = 1
        record_outcome(callback, "novice", "win", 0.50)
        record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.25)

        restored, restored_probe, _ = build_callback()
        restored.model = callback.model
        restored.num_timesteps = 1
        restored._on_training_start()
        self.assertEqual(restored._handicap_epsilon, 0.25)

        # Deadline-free mastery advance to a stage without handicap
        # broadcasts a zero so workers cannot keep a stale epsilon.
        restored.num_timesteps = 10
        for _ in range(2):
            record_outcome(restored, "novice", "win", 0.25)
        for _ in range(2):
            record_outcome(restored, "novice", "win", 0.0)
        self.assertEqual(restored._active_stage_index, 1)
        env = restored.model.get_env()
        self.assertEqual(
            env.handicap_calls()[-1], ("set_opponent_handicap", (0.0, [])))


class StageTurnLimitTest(unittest.TestCase):
    """Curriculum stages may shorten episodes without moving the env ceiling."""

    def test_stage_max_turns_applies_to_game_state_only(self):
        env = get_env()
        default_limit = int(env.max_turns)

        env.reset(seed=97010, options={"max_turns": 20})
        self.assertEqual(env.game_state.max_turns, 20)
        self.assertEqual(env._episode_metadata()["max_turns"], 20)
        # The observation-space bound never shrinks with the stage.
        self.assertEqual(int(env.max_turns), default_limit)
        self.assertEqual(
            int(env.observation_space["turn"].high[0]), default_limit + 1)

        # A stage can never exceed the environment ceiling.
        env.reset(seed=97011, options={"max_turns": 99})
        self.assertEqual(env.game_state.max_turns, default_limit)

        # Without a stage limit the engine default is restored.
        env.reset(seed=97012)
        self.assertEqual(env.game_state.max_turns, default_limit)
        self.assertEqual(
            env._episode_metadata()["max_turns"], default_limit)


class EnvironmentHandicapCommitTest(unittest.TestCase):
    def test_handicap_commits_at_reset_for_matching_profile_only(self):
        env = get_env()
        env.set_opponent_handicap(0.5, ["novice"])
        # Staging alone must not change the live value mid-episode.
        self.assertEqual(env.active_opponent_handicap, 0.0)

        env.reset(seed=97001, options={"opponent_profile": "novice"})
        self.assertEqual(env.active_opponent_handicap, 0.5)
        self.assertEqual(env._episode_metadata()["opponent_handicap"], 0.5)

        env.reset(seed=97002, options={"opponent_profile": "scripted"})
        self.assertEqual(env.active_opponent_handicap, 0.0)

        env.set_opponent_handicap(0.0, [])
        env.reset(seed=97003, options={"opponent_profile": "novice"})
        self.assertEqual(env.active_opponent_handicap, 0.0)

    def test_fixed_evaluation_schedules_ignore_the_handicap(self):
        env = get_env()
        decks = [deck.get("name") for deck in env.decks[:2]]
        env.set_opponent_handicap(1.0, ["novice", "scripted"])
        env.set_episode_schedule([{
            "seed": 97004, "p1_deck": decks[0], "p2_deck": decks[1],
            "agent_is_p1": True, "opponent_profile": "scripted",
        }])
        try:
            env.reset()
            self.assertEqual(env.active_opponent_handicap, 0.0)
        finally:
            env._episode_schedule = []
            env.reset_episode_schedule()
            env.set_opponent_handicap(0.0, [])

    def test_set_opponent_handicap_validates_input(self):
        env = get_env()
        with self.assertRaises(ValueError):
            env.set_opponent_handicap(1.5, ["novice"])
        with self.assertRaises(ValueError):
            env.set_opponent_handicap(-0.1, ["novice"])
        with self.assertRaises(ValueError):
            env.set_opponent_handicap(0.5, ["gibberish"])

    def test_handicapped_novice_declines_optional_attacks(self):
        gs = fresh(97005)
        env = get_env()
        active = gs.p1
        gs.agent_is_p1 = False  # the scripted opponent owns P1 this turn
        gs.turn = 3
        gs.phase = gs.PHASE_DECLARE_ATTACKERS
        inject_into_zone(gs, active, {
            "name": "Handicap Attacker", "mana_cost": "{1}", "cmc": 1,
            "type_line": "Creature — Bear", "power": 2, "toughness": 2,
            "oracle_text": "", "color_identity": ["G"],
        }, "battlefield")
        active["entered_battlefield_this_turn"].clear()

        mask = np.zeros(env.ACTION_SPACE_SIZE, dtype=bool)
        mask[28] = True   # declare the injected attacker
        mask[479] = True  # declarations done
        mask[11] = True   # pass priority
        context = {"phase_context": "priority"}

        import random as random_module
        env.active_opponent_profile = "novice"
        # The synthetic mask bypasses generate_valid_actions, so provide the
        # empty context store that choose() would otherwise read from it.
        env.action_handler.action_reasons_with_context = {}

        env.active_opponent_handicap = 0.0
        action, _ = env._get_scripted_opponent_action(active, mask, context)
        self.assertEqual(action, 28, "full-strength novice must attack")

        env.active_opponent_handicap = 1.0
        env._opponent_handicap_rng = random_module.Random(7)
        action, _ = env._get_scripted_opponent_action(active, mask, context)
        self.assertEqual(
            action, 479,
            "a fully handicapped novice must take the passive baseline")

        env.active_opponent_handicap = 0.0


if __name__ == "__main__":
    unittest.main(verbosity=2)
