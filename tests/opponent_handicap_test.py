"""Round 7.91/7.93 curriculum ramps: annealed handicap and stage turn limits.

Round 7.91: rounds 7.88-7.90 showed a difficulty cliff between the passive
and novice profiles (~60% decisive wins collapsing to ~5%).  The handicap
gives active stages a climbable slope.  Round 7.93: run round-7.92-combat-v5-v3
showed raw-rate windows ratcheting on noise (6/24 wins, exactly the stage
floor) with no way back once the agent collapsed at full strength.  These
tests pin the ratchet's four promises:

1. The trainer tightens epsilon toward zero only when the 95% Wilson lower
   bound of a full window at the CURRENT epsilon clears the stage target;
   a raw rate at the target is not enough.
2. The ratchet is reversible: when the window's upper bound falls below the
   target — including at epsilon zero — one rung is handed back, never past
   the stage's configured start.
3. Mastery requires the anneal to finish and full-strength profile evidence;
   epsilon resets per stage and persists across restore.
4. The environment commits a staged handicap only at reset, only for the
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
            # Window 4 with target 0.5 is the smallest pair where the 95%
            # Wilson interval resolves both directions: 4/4 wins has lower
            # bound ~0.51 (tighten) and 0/4 has upper bound ~0.49 (relax).
            "handicap": {
                "profiles": ["novice"],
                "start": 0.50,
                "step": 0.25,
                "window_episodes": 4,
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

        # A full winning window at the live epsilon ratchets one step and
        # broadcasts the new value.
        for _ in range(3):
            record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.50)
        record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        self.assertEqual(
            probe.handicap_calls()[-1],
            ("set_opponent_handicap", (0.25, ["novice"])))

        # The window restarts at the new epsilon: the same records do not
        # double-count.
        for _ in range(3):
            record_outcome(callback, "novice", "win", 0.25)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        record_outcome(callback, "novice", "win", 0.25)
        self.assertEqual(callback._handicap_epsilon, 0.0)

    def test_noise_level_window_at_the_target_does_not_tighten(self):
        callback, probe, _ = build_callback()
        callback.num_timesteps = 1

        # 3/4 wins clears the 0.5 target on the raw rate, but its 95% lower
        # bound (~0.30) does not.  Round-7.92 flatlined because raw-rate
        # windows kept strengthening opponents on exactly this evidence.
        record_outcome(callback, "novice", "loss", 0.50)
        for _ in range(3):
            record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.50)
        self.assertEqual(len(probe.handicap_calls()), 1)

        # One more win rolls the loss out of the window; 4/4 is significant.
        record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.25)

    def test_collapse_at_the_live_epsilon_hands_back_one_rung(self):
        callback, probe, _ = build_callback()
        callback.num_timesteps = 1
        for _ in range(4):
            record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.25)

        # A full losing window at the new rung is confidently below the
        # target: hand the rung back rather than keep training on losses.
        for _ in range(3):
            record_outcome(callback, "novice", "loss", 0.25)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        record_outcome(callback, "novice", "loss", 0.25)
        self.assertEqual(callback._handicap_epsilon, 0.50)
        self.assertEqual(
            probe.handicap_calls()[-1],
            ("set_opponent_handicap", (0.50, ["novice"])))

        # The stage's configured start is the ceiling: collapse there holds.
        calls_before = len(probe.handicap_calls())
        for _ in range(4):
            record_outcome(callback, "novice", "loss", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.50)
        self.assertEqual(len(probe.handicap_calls()), calls_before)

    def test_full_strength_collapse_reopens_the_anneal(self):
        callback, probe, _ = build_callback()
        callback.num_timesteps = 1
        for epsilon in (0.50, 0.25):
            for _ in range(4):
                record_outcome(callback, "novice", "win", epsilon)
        self.assertEqual(callback._handicap_epsilon, 0.0)

        # Round-7.92 finished its anneal at full strength, collapsed to a
        # 2-win window, and had no way back.  Epsilon-zero evidence must
        # reopen the ramp.
        for _ in range(4):
            record_outcome(callback, "novice", "loss", 0.0)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        self.assertEqual(
            probe.handicap_calls()[-1],
            ("set_opponent_handicap", (0.25, ["novice"])))

    def test_mastery_waits_for_anneal_and_full_strength_floor(self):
        callback, _, _ = build_callback()
        callback.num_timesteps = 10

        # Weakened wins satisfy the aggregate window but neither the anneal
        # nor the full-strength novice floor.
        for _ in range(4):
            record_outcome(callback, "novice", "win", 0.50)
        self.assertEqual(callback._handicap_epsilon, 0.25)
        for _ in range(4):
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
        for _ in range(4):
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
        for _ in range(4):
            record_outcome(restored, "novice", "win", 0.25)
        for _ in range(2):
            record_outcome(restored, "novice", "win", 0.0)
        self.assertEqual(restored._active_stage_index, 1)
        env = restored.model.get_env()
        self.assertEqual(
            env.handicap_calls()[-1], ("set_opponent_handicap", (0.0, [])))


class FinalStageHandicapTest(unittest.TestCase):
    """A terminal stage has no mastery gate, but its handicap must ratchet."""

    FINAL_STAGE_CURRICULUM = {
        "id": "final-handicap-test",
        "version": 1,
        "progression": "mastery",
        "stages": [
            {
                "name": "learn",
                "start_timestep": 0,
                "advance_when": {
                    "window_episodes": 2,
                    "min_stage_timesteps": 1,
                    "max_stage_timesteps": 5,
                    "min_decisive_win_rate": 0.5,
                    "max_decisive_loss_rate": 0.5,
                    "max_timeout_rate": 1.0,
                },
            },
            {
                "name": "full_pool",
                "start_timestep": 5,
                "profile_bag": ["novice"] * 2 + ["scripted"] * 8,
                "handicap": {
                    "profiles": ["scripted"],
                    "start": 0.40,
                    "step": 0.20,
                    "window_episodes": 24,
                    "min_decisive_win_rate": 0.5,
                },
            },
        ],
    }

    def test_terminal_stage_ratchets_through_mixed_profile_cycles(self):
        callback, probe, _ = build_callback(self.FINAL_STAGE_CURRICULUM)
        callback.num_timesteps = 5
        # Deadline-advance into the terminal stage.
        callback.locals = {"infos": [], "dones": np.array([])}
        assert callback._on_step()
        self.assertEqual(callback._active_stage_index, 1)
        self.assertEqual(callback._handicap_epsilon, 0.40)
        # Reproduce the combat-v5 full-pool mix: each ten-game cycle contains
        # eight handicapped scripted games and two unhandicapped novice games.
        # The novice results must not evict scripted evidence from the
        # handicap's 24-qualifying-episode window.
        mixed_cycle = (
            ["scripted"] * 4 + ["novice"]
            + ["scripted"] * 4 + ["novice"])
        for cycle_index in range(3):
            for profile_index, profile in enumerate(mixed_cycle):
                record_outcome(
                    callback, profile, "win",
                    0.40 if profile == "scripted" else 0.0,
                    stage="full_pool")
                if cycle_index < 2 or profile_index < 8:
                    self.assertEqual(callback._handicap_epsilon, 0.40)
            if cycle_index == 1:
                saved = getattr(
                    callback.model,
                    m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
                self.assertEqual(len(saved["handicap_outcomes"]), 16)
                restored = m.CurriculumProgressCallback(
                    self.FINAL_STAGE_CURRICULUM)
                restored.model = callback.model
                restored.num_timesteps = callback.num_timesteps
                restored._on_training_start()
                self.assertEqual(len(restored._handicap_outcomes), 16)
                callback = restored
        self.assertEqual(callback._handicap_epsilon, 0.20)
        self.assertEqual(
            probe.handicap_calls()[-1],
            ("set_opponent_handicap", (0.20, ["scripted"])))
        self.assertEqual(len(callback._handicap_outcomes), 0)


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
