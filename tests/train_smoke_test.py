"""
Training-stack smoke test for main.py.

Run from the repository root:

    python tests/train_smoke_test.py

Requires the full training stack (torch, stable-baselines3, sb3_contrib).
The engine-only test (tests/smoke_test.py) does not need those, so run that
one first; run this one when you change main.py or anything touching the
neural network / SB3 integration.

What it verifies:
  1. main.py imports and its custom extractor/policy construct against the
     real observation space.
  2. Periodic and hyperparameter evaluations are mask-aware and isolated from
     training environments, and callback CLI frequencies have timestep semantics.
  3. Every feature-extractor parameter is registered with PyTorch and covered
     by the optimizer (regression for the plain-dict extractors bug and the
     lazily-created feature_merger bug — both used to leave weights untrained).
  4. The phase embedding is large enough for every engine phase constant
     (regression for the Embedding(10, ...) IndexError crash).
  5. A short MaskablePPO training run completes.
  6. Save -> load -> predict round-trips (used to fail because feature_merger
     did not exist on a freshly constructed policy).
  7. Two masked MTG environments reset and step through SubprocVecEnv using
     Windows-compatible spawn semantics, then shut their worker processes down.
"""

import os
import json
import queue
from collections import Counter
from functools import partial
import shutil
import sys
import tempfile
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import logging
logging.disable(logging.CRITICAL)

import numpy as np

RESULTS = []
TEST_ARTIFACT_ROOT = os.path.join(REPO_ROOT, "tests", "test_artifacts", "train_smoke")


def reset_test_artifacts():
    shutil.rmtree(TEST_ARTIFACT_ROOT, ignore_errors=True)


def test_artifact_paths():
    return {
        "deck_stats_path": os.path.join(TEST_ARTIFACT_ROOT, "deck_stats"),
        "card_memory_path": os.path.join(TEST_ARTIFACT_ROOT, "card_memory"),
    }


def _make_subproc_masked_env(
        decks, card_db, storage_root, worker_index, subtype_vocab):
    """Top-level factory target so Windows ``spawn`` can import it safely."""
    import main as m

    return m.make_masked_mtg_env(
        decks,
        card_db,
        os.path.join(storage_root, f"env_{worker_index}"),
        agent_is_p1=(worker_index % 2 == 0),
        alternate_agent_seat=True,
        subtype_vocab=subtype_vocab,
    )


def stage(name):
    def wrap(fn):
        def run(*args, **kwargs):
            try:
                out = fn(*args, **kwargs)
                RESULTS.append((name, True, ""))
                print(f"  PASS  {name}")
                return out
            except Exception as e:
                RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
                print(f"  FAIL  {name}\n{traceback.format_exc()}")
                return None
        return run
    return wrap


@stage("import training stack and main.py")
def do_imports():
    import torch  # noqa: F401
    from sb3_contrib.ppo_mask import MaskablePPO  # noqa: F401
    stdout_before = sys.stdout
    stderr_before = sys.stderr
    import main  # noqa: F401  (must not execute training on import)
    assert sys.stdout is stdout_before and sys.stderr is stderr_before, (
        "importing main.py replaced the host process's standard streams")
    return True


@stage("mask-aware callback and evaluation enforce action masks")
def check_mask_aware_evaluation():
    import pickle
    import threading
    from types import SimpleNamespace

    import gymnasium as gym
    from gymnasium import spaces
    from sb3_contrib.common.maskable.callbacks import MaskableEvalCallback
    from sb3_contrib.common.maskable.evaluation import (
        evaluate_policy as maskable_evaluate_policy,
    )
    from sb3_contrib.common.wrappers import ActionMasker
    from stable_baselines3.common.vec_env import DummyVecEnv
    import main as m
    from Playersim.actions import _process_safe_info_value

    # Periodic training evaluation goes through create_callbacks(), while
    # Optuna calls main.evaluate_policy directly. Guard both call sites.
    assert m.evaluate_policy is maskable_evaluate_policy
    safe_info = _process_safe_info_value({
        "nested": {"lock": threading.RLock()},
        "ordinary": [1, "two", None],
    })
    pickle.dumps(safe_info)
    assert safe_info["nested"]["lock"]["type"] == "RLock"

    class OnlyActionZeroEnv(gym.Env):
        """One-step env that fails immediately if evaluation ignores its mask."""

        metadata = {"render_modes": []}

        def __init__(self):
            self.action_space = spaces.Discrete(2)
            self.observation_space = spaces.Box(
                low=0.0, high=1.0, shape=(1,), dtype=np.float32)

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            return np.zeros(1, dtype=np.float32), {}

        def step(self, action):
            action = int(action)
            assert action == 0, f"evaluation executed masked action {action}"
            return np.zeros(1, dtype=np.float32), 1.0, True, False, {}

    eval_env = ActionMasker(
        OnlyActionZeroEnv(),
        lambda _env: np.array([True, False], dtype=bool),
    )

    class MaskSensitiveModel:
        """Would choose illegal action 1 unless evaluation supplies masks."""

        def __init__(self):
            self.seen_masks = []

        def predict(self, observations, state=None, episode_start=None,
                    deterministic=False, action_masks=None):
            batch_size = len(observations)
            if action_masks is None:
                return np.ones(batch_size, dtype=np.int64), state
            masks = np.asarray(action_masks, dtype=bool)
            self.seen_masks.append(masks.copy())
            return np.argmax(masks, axis=1), state

    model = MaskSensitiveModel()
    mean_reward, std_reward = m.evaluate_policy(
        model, eval_env, n_eval_episodes=3, deterministic=True, warn=False)
    assert mean_reward == 1.0 and std_reward == 0.0
    assert model.seen_masks
    assert all(np.array_equal(mask, [[True, False]])
               for mask in model.seen_masks)

    assert m.repeated_short_cycle_period([b"a"] * 3) == 1
    assert m.repeated_short_cycle_period([b"a", b"b"] * 3) == 2
    assert m.repeated_short_cycle_period(
        [b"a", b"b", b"c", b"a", b"b", b"d"]) is None

    class TwoStateCycleEnv(gym.Env):
        metadata = {"render_modes": []}

        def __init__(self):
            self.action_space = spaces.Discrete(1)
            self.observation_space = spaces.Box(
                low=0, high=1, shape=(1,), dtype=np.int32)
            self.state = 0

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            self.state = 0
            return np.array([self.state], dtype=np.int32), {}

        def step(self, action):
            self.state = 1 - self.state
            return (np.array([self.state], dtype=np.int32), 0.0,
                    False, False, {})

    cycle_env = m.StrictEvaluationVecEnv(DummyVecEnv([
        lambda: ActionMasker(
            TwoStateCycleEnv(),
            lambda _env: np.array([True], dtype=bool))
    ]), max_cycle_period=2, cycle_repeats=3)
    try:
        cycle_env.reset()
        for _ in range(5):
            cycle_env.step(np.array([0], dtype=np.int64))
        try:
            cycle_env.step(np.array([0], dtype=np.int64))
        except RuntimeError as error:
            assert "cycle of period 2" in str(error)
        else:
            raise AssertionError("strict evaluation accepted a two-state loop")
    finally:
        cycle_env.close()

    args = SimpleNamespace(
        eval_freq=10,
        eval_episodes=4,
        checkpoint_freq=20,
        record_network=True,
        record_freq=30,
    )
    with tempfile.TemporaryDirectory() as tmp:
        old_model_dir, old_log_dir = m.MODEL_DIR, m.LOG_DIR
        m.MODEL_DIR = os.path.join(tmp, "models")
        m.LOG_DIR = os.path.join(tmp, "logs")
        try:
            schedule_decks = [
                {"name": f"Deck {index}", "cards": []}
                for index in range(8)
            ]
            full_schedule = m.build_fixed_evaluation_schedule(
                schedule_decks, 64, 12345)
            assert len(full_schedule) == 64
            assert full_schedule == m.build_fixed_evaluation_schedule(
                list(reversed(schedule_decks)), 64, 12345)
            assert full_schedule != m.build_fixed_evaluation_schedule(
                schedule_decks, 64, 12346)
            paired_cases = list(zip(
                full_schedule[::2], full_schedule[1::2]))
            for first, second in paired_cases:
                assert first["seed"] == second["seed"]
                assert first["agent_is_p1"] is True
                assert second["agent_is_p1"] is False
                assert first["p1_deck"] == second["p2_deck"]
                assert first["p2_deck"] == second["p1_deck"]
                assert first["p1_deck"] != first["p2_deck"]

            expected_names = {deck["name"] for deck in schedule_decks}
            agent_matchups = Counter(
                first["p1_deck"] for first, _ in paired_cases)
            opponent_matchups = Counter(
                first["p2_deck"] for first, _ in paired_cases)
            assert set(agent_matchups) == expected_names
            assert set(opponent_matchups) == expected_names
            assert set(agent_matchups.values()) == {4}
            assert set(opponent_matchups.values()) == {4}
            for deck_name in expected_names:
                opponents = {
                    first["p2_deck"] for first, _ in paired_cases
                    if first["p1_deck"] == deck_name
                }
                assert len(opponents) == 4
                seat_counts = Counter(
                    case["agent_is_p1"] for case in full_schedule
                    if (case["p1_deck"] if case["agent_is_p1"]
                        else case["p2_deck"]) == deck_name)
                assert seat_counts == {True: 4, False: 4}

            schedule_hash = m.evaluation_schedule_sha256(full_schedule)
            assert schedule_hash == m.evaluation_schedule_sha256(
                m.build_fixed_evaluation_schedule(
                    list(reversed(schedule_decks)), 64, 12345))
            changed_case = [dict(case) for case in full_schedule]
            changed_case[0]["seed"] += 1
            assert schedule_hash != m.evaluation_schedule_sha256(changed_case)

            def evaluation_rows(outcomes):
                return [{
                    "case_index": index,
                    "case": dict(full_schedule[index]),
                    "game_result": result,
                    "terminal_reason": "life_total",
                    "reward": 1.0 if result == "win" else 0.0,
                    "length": 40,
                } for index, result in enumerate(outcomes)]

            # Qualification uncertainty is persisted and treats each
            # seat-swapped matchup as one clustered unit. Real draws retain
            # their half point, while the gate uses the conservative lower
            # bound rather than the raw point estimate.
            _, strong_summary, _ = m.summarize_evaluation_episodes(
                evaluation_rows(["win"] * 32 + ["draw"] * 32))
            strong_interval = strong_summary["qualification_interval"]
            assert strong_summary["qualification_score"] == 0.75
            assert strong_interval["paired_units"] == 32
            assert strong_interval["method"] == \
                "wilson-score+paired-t-envelope"
            assert strong_interval["lower_bound"] > 0.55
            _, one_pair_summary, _ = m.summarize_evaluation_episodes(
                evaluation_rows(["win", "loss"]))
            assert one_pair_summary["qualification_interval"][
                "paired_units"] == 1
            _, noisy_summary, _ = m.summarize_evaluation_episodes(
                evaluation_rows(["win"] * 40 + ["loss"] * 24))
            assert noisy_summary["qualification_score"] == 0.625
            assert noisy_summary["qualification_interval"][
                "lower_bound"] < 0.55

            canary_args = SimpleNamespace(
                **m.ROUND_7_92_CANARY["cli"],
                canary_config="round-7.92", resume=None,
                optimize_hp=False)
            canary = m.validate_canary_cli(canary_args)
            assert canary is not m.ROUND_7_92_CANARY
            canary_decks = [{"name": name} for name in (
                "Selesnya Ouroboroid", "Jeskai Lessons", "Izzet Prowess",
                "4c Control", "Izzet Spellementals", "Dimir Excruciator",
                "Mono-Green Landfall", "Azorius Momo",
            )]
            canary_curriculum = m.resolve_curriculum(
                "combat-v5", canary_decks)
            m.validate_canary_runtime(
                canary,
                lineage={
                    "card_registry": {"sha256": canary["lineage"][
                        "card_registry_sha256"]},
                    "feature_schema": {"sha256": canary["lineage"][
                        "feature_schema_sha256"]},
                    "corpus": {"sha256": canary["lineage"][
                        "corpus_sha256"]},
                },
                training_config=canary["training_config"],
                curriculum=canary_curriculum,
                schedule_sha256=canary["lineage"][
                    "evaluation_schedule_sha256"],
                num_envs=8,
                selected_device="cuda",
            )
            drifted_curriculum = json.loads(json.dumps(canary_curriculum))
            drifted_curriculum["stages"][-1]["handicap"]["start"] = 0.99
            try:
                m.validate_canary_runtime(
                    canary,
                    lineage={
                        "card_registry": {"sha256": canary["lineage"][
                            "card_registry_sha256"]},
                        "feature_schema": {"sha256": canary["lineage"][
                            "feature_schema_sha256"]},
                        "corpus": {"sha256": canary["lineage"][
                            "corpus_sha256"]},
                    },
                    training_config=canary["training_config"],
                    curriculum=drifted_curriculum,
                    schedule_sha256=canary["lineage"][
                        "evaluation_schedule_sha256"],
                    num_envs=8,
                    selected_device="cuda",
                )
            except RuntimeError as error:
                assert "curriculum_sha256" in str(error)
            else:
                raise AssertionError(
                    "named canary accepted resolved curriculum drift")
            drifted_training = dict(canary["training_config"])
            drifted_training["future_training_knob"] = 1
            try:
                m.validate_canary_runtime(
                    canary,
                    lineage={
                        "card_registry": {"sha256": canary["lineage"][
                            "card_registry_sha256"]},
                        "feature_schema": {"sha256": canary["lineage"][
                            "feature_schema_sha256"]},
                        "corpus": {"sha256": canary["lineage"][
                            "corpus_sha256"]},
                    },
                    training_config=drifted_training,
                    curriculum=canary_curriculum,
                    schedule_sha256=canary["lineage"][
                        "evaluation_schedule_sha256"],
                    num_envs=8,
                    selected_device="cuda",
                )
            except RuntimeError as error:
                assert "future_training_knob" in str(error)
            else:
                raise AssertionError(
                    "named canary accepted an unknown training setting")
            canary_args.n_envs = 6
            try:
                m.validate_canary_cli(canary_args)
            except ValueError as error:
                assert "n_envs=6" in str(error)
            else:
                raise AssertionError("named canary accepted configuration drift")

            # Round 7.93 pins the same contract over combat-v6; its recorded
            # curriculum sha must match the resolved widened-window preset.
            round_7_93 = m.validate_canary_cli(SimpleNamespace(
                **m.ROUND_7_93_CANARY["cli"],
                canary_config="round-7.93", resume=None,
                optimize_hp=False))
            m.validate_canary_runtime(
                round_7_93,
                lineage={
                    "card_registry": {"sha256": round_7_93["lineage"][
                        "card_registry_sha256"]},
                    "feature_schema": {"sha256": round_7_93["lineage"][
                        "feature_schema_sha256"]},
                    "corpus": {"sha256": round_7_93["lineage"][
                        "corpus_sha256"]},
                },
                training_config=round_7_93["training_config"],
                curriculum=m.resolve_curriculum("combat-v6", canary_decks),
                schedule_sha256=round_7_93["lineage"][
                    "evaluation_schedule_sha256"],
                num_envs=8,
                selected_device="cuda",
            )

            # When the pair count is not divisible by the deck count, both
            # learned-deck and opponent exposure are still optimally balanced.
            ten_decks = [
                {"name": f"Wide Deck {index}", "cards": []}
                for index in range(10)
            ]
            wide_schedule = m.build_fixed_evaluation_schedule(
                ten_decks, 64, 54321)
            wide_pairs = wide_schedule[::2]
            for key in ("p1_deck", "p2_deck"):
                counts = Counter(case[key] for case in wide_pairs)
                assert len(counts) == 10
                assert max(counts.values()) - min(counts.values()) == 1
            try:
                m.build_fixed_evaluation_schedule(
                    [{"name": "Only Deck", "cards": []}], 2, 12345)
            except ValueError as error:
                assert "at least two distinct decks" in str(error)
            else:
                raise AssertionError(
                    "fixed evaluation silently introduced a mirror match")
            try:
                m.build_fixed_evaluation_schedule(schedule_decks, 3, 12345)
            except ValueError as error:
                assert "must be even" in str(error)
            else:
                raise AssertionError(
                    "fixed evaluation accepted an unpaired final case")
            fixed_schedule = full_schedule[:args.eval_episodes]

            class ScheduleProbe:
                num_envs = 1

                def __init__(self):
                    self.calls = []

                def env_method(self, name, *values):
                    self.calls.append((name, values))

            schedule_probe = ScheduleProbe()
            m.install_fixed_evaluation_schedule(
                schedule_probe, fixed_schedule)
            m.install_fixed_evaluation_schedule(
                schedule_probe, fixed_schedule)
            assert [name for name, _ in schedule_probe.calls] == [
                "reset_episode_schedule", "set_episode_schedule",
                "reset_episode_schedule", "set_episode_schedule",
            ]
            assert schedule_probe.calls[1][1][0] == fixed_schedule
            callbacks = m.create_callbacks(
                lambda: eval_env, "mask_smoke", args, num_train_envs=2,
                evaluation_schedule=fixed_schedule)
            assert isinstance(callbacks[0], m.AsyncMaskableEvalCallback)
            # Evaluation frequency stays in total timesteps: the async
            # callback compares against num_timesteps, never n_calls, so it
            # must not be divided by the environment count.
            assert callbacks[0].eval_freq == 10
            assert callbacks[0].n_eval_episodes == 4
            assert callbacks[0].fixed_evaluation_schedule == fixed_schedule
            assert callbacks[0].schedule_sha256 == \
                m.evaluation_schedule_sha256(fixed_schedule)
            assert callbacks[0]._process is None  # worker starts lazily
            assert callbacks[1].save_freq == 10
            assert callbacks[0].best_model_save_path == os.path.join(
                m.MODEL_DIR, "mask_smoke", "best_model")
            assert callbacks[0].snapshot_dir == os.path.join(
                m.MODEL_DIR, "mask_smoke", "eval_snapshots")
            assert callbacks[1].save_path == os.path.join(
                m.MODEL_DIR, "mask_smoke", "checkpoints")
            assert any(isinstance(callback, m.StrictTrainingFidelityCallback)
                       for callback in callbacks)

            curriculum_probe = ScheduleProbe()
            curriculum_metrics = {}
            curriculum = {
                "stages": [
                    {"name": "goldfish", "start_timestep": 0},
                    {"name": "race", "start_timestep": 30_000},
                ]}
            curriculum_callback = m.CurriculumProgressCallback(curriculum)
            curriculum_callback.model = SimpleNamespace(
                get_env=lambda: curriculum_probe,
                logger=SimpleNamespace(record=lambda name, value:
                                       curriculum_metrics.__setitem__(
                                           name, value)),
            )
            curriculum_callback.num_timesteps = 0
            curriculum_callback._on_training_start()
            curriculum_callback.num_timesteps = 29_999
            assert curriculum_callback._on_step()
            curriculum_callback.num_timesteps = 30_000
            assert curriculum_callback._on_step()
            assert curriculum_probe.calls == [
                ("set_curriculum_timestep", (0,)),
                ("set_curriculum_timestep", (30_000,)),
            ]
            assert curriculum_metrics["curriculum/stage_index"] == 1

            mastery_probe = ScheduleProbe()
            mastery_metrics = {}
            mastery_curriculum = {
                "id": "mastery-test",
                "version": 1,
                "progression": "mastery",
                "stages": [
                    {
                        "name": "learn",
                        "start_timestep": 0,
                        "advance_when": {
                            "window_episodes": 4,
                            "min_stage_timesteps": 10,
                            "max_stage_timesteps": 20,
                            "min_decisive_win_rate": 0.75,
                            "max_decisive_loss_rate": 0.25,
                            "max_timeout_rate": 0.0,
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
            mastery_callback = m.CurriculumProgressCallback(
                mastery_curriculum)
            mastery_callback.model = SimpleNamespace(
                get_env=lambda: mastery_probe,
                logger=SimpleNamespace(record=lambda name, value:
                                       mastery_metrics.__setitem__(
                                           name, value)),
            )
            mastery_callback.num_timesteps = 0
            mastery_callback._on_training_start()
            mastery_callback.num_timesteps = 10

            def record_mastery_outcome(
                    callback, profile, result, stage="learn"):
                callback.locals = {
                    "infos": [{
                        "curriculum_stage": stage,
                        "opponent_profile": profile,
                        "game_result": result,
                        "terminal_reason": "life_total",
                    }],
                    "dones": np.array([True]),
                }
                assert callback._on_step()

            # Even a perfect aggregate window cannot master the stage without
            # the configured evidence against the novice opponent.
            for _ in range(4):
                record_mastery_outcome(
                    mastery_callback, "passive", "win")
            assert mastery_callback._active_stage_index == 0
            persisted_window = getattr(
                mastery_callback.model,
                m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
            assert persisted_window["recent_outcomes"][0] == {
                "outcome": "win", "opponent_profile": "passive",
                "opponent_handicap": 0.0}

            # Profile-tagged rolling outcomes survive callback restoration.
            restored_callback = m.CurriculumProgressCallback(
                mastery_curriculum)
            restored_callback.model = mastery_callback.model
            restored_callback.num_timesteps = 10
            restored_callback._on_training_start()
            assert restored_callback._outcome_rates("passive")["episodes"] == 4
            record_mastery_outcome(restored_callback, "novice", "loss")
            assert restored_callback._active_stage_index == 0
            record_mastery_outcome(restored_callback, "novice", "win")
            assert restored_callback._active_stage_index == 1
            assert mastery_probe.calls == [
                ("set_curriculum_stage", (0, 0)),
                ("set_curriculum_stage", (0, 10)),
                ("set_curriculum_stage", (1, 10)),
            ]
            saved_mastery = getattr(
                restored_callback.model,
                m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
            assert saved_mastery["stage_index"] == 1
            assert saved_mastery["transition_history"][-1]["reason"] == \
                "mastery"
            assert saved_mastery["pending_activation_workers"] == [0]
            assert saved_mastery["transition_history"][-1][
                "activation_timestep"] is None
            restored_callback.num_timesteps = 11
            record_mastery_outcome(
                restored_callback, "scripted", "loss", stage="apply")
            saved_mastery = getattr(
                restored_callback.model,
                m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
            assert saved_mastery["pending_activation_workers"] == []
            assert saved_mastery["stage_entry_timestep"] == 11
            assert saved_mastery["transition_history"][-1][
                "activation_timestep"] == 11
            progress_manifest = m.curriculum_progress_manifest(
                restored_callback.model, mastery_curriculum)
            assert progress_manifest["state"]["stage_name"] == "apply"
            assert progress_manifest["state"]["transition_history"][-1][
                "activation_timestep"] == 11
            assert mastery_metrics["curriculum/stage_index"] == 1
            assert mastery_metrics[
                "curriculum/mastery_novice_decisive_win_rate"] == 0.5

            deadline_probe = ScheduleProbe()
            deadline_metrics = {}
            deadline_curriculum = json.loads(json.dumps(mastery_curriculum))
            deadline_curriculum["id"] = "deadline-test"
            deadline_curriculum["stages"][0]["advance_when"][
                "max_stage_timesteps"] = 12
            deadline_callback = m.CurriculumProgressCallback(
                deadline_curriculum)
            deadline_callback.model = SimpleNamespace(
                get_env=lambda: deadline_probe,
                logger=SimpleNamespace(record=lambda name, value:
                                       deadline_metrics.__setitem__(
                                           name, value)),
            )
            deadline_callback.num_timesteps = 0
            deadline_callback._on_training_start()
            deadline_callback.num_timesteps = 10
            record_mastery_outcome(deadline_callback, "passive", "win")
            record_mastery_outcome(deadline_callback, "passive", "win")
            assert deadline_callback._active_stage_index == 0
            deadline_callback.num_timesteps = 12
            deadline_callback.locals = {"infos": [], "dones": np.array([])}
            assert deadline_callback._on_step()
            assert deadline_callback._active_stage_index == 1
            deadline_state = getattr(
                deadline_callback.model,
                m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
            assert deadline_state["transition_history"][-1]["reason"] == \
                "deadline"
            assert deadline_metrics["curriculum/advance_via_deadline"] == 1.0
            deadline_callback.num_timesteps = 13
            record_mastery_outcome(
                deadline_callback, "passive", "win", stage="learn")
            deadline_state = getattr(
                deadline_callback.model,
                m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
            assert deadline_state["stale_stage_episodes"] == 1
            assert deadline_state["pending_activation_workers"] == [0]
            deadline_callback.num_timesteps = 14
            record_mastery_outcome(
                deadline_callback, "scripted", "loss", stage="apply")
            deadline_state = getattr(
                deadline_callback.model,
                m.CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE)
            assert deadline_state["stage_entry_timestep"] == 14
            assert deadline_state["pending_activation_workers"] == []

            # Functional contract of the async result path: outcome quality,
            # not shaped mean reward, promotes the exact evaluated snapshot.
            # Every per-case result and checkpoint hash remains attributable.
            async_eval = callbacks[0]
            # Four episodes are intentionally tiny for this smoke. Lower the
            # threshold so the Wilson lower-bound promotion path is reachable;
            # production retains the callback's 0.55 default over 64 cases.
            async_eval.minimum_qualification_score = 0.30
            metrics = {}
            fake_logger = SimpleNamespace(
                record=lambda key, value: metrics.__setitem__(key, value))
            async_eval.model = SimpleNamespace(logger=fake_logger)
            snapshot = os.path.join(
                async_eval.snapshot_dir, "eval_snapshot_10_steps.zip")
            with open(snapshot, "wb") as handle:
                handle.write(b"snapshot-bytes")

            def result_for(path, timestep, outcomes, rewards):
                episodes = []
                for index, ((game_result, terminal_reason), reward) in \
                        enumerate(zip(outcomes, rewards)):
                    episodes.append({
                        "case_index": index,
                        "case": dict(fixed_schedule[index]),
                        "game_result": game_result,
                        "terminal_reason": terminal_reason,
                        "reward": reward,
                        "length": 40 + index,
                    })
                return {
                    "timesteps": timestep,
                    "snapshot_path": path,
                    "schedule_sha256": async_eval.schedule_sha256,
                    "episodes": episodes,
                }

            async_eval._pending_snapshots = 1
            async_eval._handle_result(result_for(
                snapshot, 10,
                [("win", "life_total"), ("loss", "life_total"),
                 ("win_turn_limit", "turn_limit"),
                 ("loss", "life_total")],
                [1.0, -1.0, 10.0, -1.0]))
            best_path = os.path.join(
                async_eval.best_model_save_path, "best_model.zip")
            assert not os.path.isfile(best_path), (
                "an unqualified snapshot was published as best")
            assert not os.path.exists(snapshot), "snapshot was not cleaned up"
            assert metrics["eval/decisive_wins"] == 1
            assert metrics["eval/timeouts"] == 1
            assert metrics["eval/evaluated_at_timesteps"] == 10
            assert async_eval._pending_snapshots == 0
            better_outcome = os.path.join(
                async_eval.snapshot_dir, "eval_snapshot_20_steps.zip")
            with open(better_outcome, "wb") as handle:
                handle.write(b"outcome-bytes")
            async_eval._pending_snapshots = 1
            async_eval._handle_result(result_for(
                better_outcome, 20,
                [("win", "life_total"), ("win", "life_total"),
                 ("win", "life_total"), ("win", "life_total")],
                [-9.0, -9.0, -9.0, -9.0]))
            with open(best_path, "rb") as handle:
                assert handle.read() == b"outcome-bytes", (
                    "fewer timeouts did not beat higher shaped reward")
            assert not os.path.exists(better_outcome)

            reward_only = os.path.join(
                async_eval.snapshot_dir, "eval_snapshot_30_steps.zip")
            with open(reward_only, "wb") as handle:
                handle.write(b"reward-only-bytes")
            async_eval._pending_snapshots = 1
            async_eval._handle_result(result_for(
                reward_only, 30,
                [("draw", "decking"), ("draw", "decking"),
                 ("draw", "decking"), ("draw", "decking")],
                [100.0, 100.0, 100.0, 100.0]))
            with open(best_path, "rb") as handle:
                assert handle.read() == b"outcome-bytes", (
                    "shaped mean reward overrode decisive outcome quality")
            assert not os.path.exists(reward_only)

            history_path = os.path.join(
                m.LOG_DIR, "mask_smoke", "evaluation", "evaluations.json")
            with open(history_path, encoding="utf-8") as handle:
                history = json.load(handle)
            assert history["schedule_sha256"] == async_eval.schedule_sha256
            assert history["fixed_schedule"] == fixed_schedule
            assert len(history["evaluations"]) == 3
            assert history["evaluations"][0]["qualified"] is False
            assert history["evaluations"][0]["promoted"] is False
            assert history["evaluations"][1]["qualified"] is True
            assert history["evaluations"][1]["promoted"] is True
            assert history["evaluations"][2]["promoted"] is False
            assert history["schema_version"] == 3
            assert history["minimum_qualification_score"] == 0.30
            assert history["qualification_rule"]["metric"] == \
                "qualification_interval.lower_bound"
            assert history["evaluations"][1]["qualification_interval"][
                "lower_bound"] >= 0.30
            assert history["best_candidate_timestep"] == 20
            assert len(history["evaluations"][0]["checkpoint_sha256"]) == 64
            assert history["evaluations"][0]["episodes"][0][
                "case"] == fixed_schedule[0]
            history_summary = m.evaluation_history_summary("mask_smoke")
            assert history_summary["status"] == "qualified"
            assert history_summary["qualified"] is True
            assert history_summary["evaluation_points"] == 3
            assert history_summary["best_timestep"] == 20
            assert history_summary["qualified_evaluation_points"] == 1
            try:
                async_eval._handle_result({"fatal": "worker exploded"})
            except RuntimeError as error:
                assert "worker exploded" in str(error)
            else:
                raise AssertionError(
                    "async evaluation accepted a fatal worker result")

            cancelled_snapshot = os.path.join(
                async_eval.snapshot_dir, "eval_snapshot_40_steps.zip")
            with open(cancelled_snapshot, "wb") as handle:
                handle.write(b"cancel-me")

            class CancellableEvaluationProcess:
                def __init__(self):
                    self.alive = True

                def is_alive(self):
                    return self.alive

                def join(self, timeout=None):
                    self.alive = False

                def terminate(self):
                    self.alive = False

            async_eval._process = CancellableEvaluationProcess()
            async_eval._request_queue = queue.Queue()
            async_eval._result_queue = queue.Queue()
            async_eval._pending_snapshots = 1
            async_eval._pending_snapshot_paths[cancelled_snapshot] = 40
            async_eval.cancel_pending("test_interruption")
            assert not os.path.exists(cancelled_snapshot)
            with open(history_path, encoding="utf-8") as handle:
                cancelled_history = json.load(handle)
            assert cancelled_history["cancelled_evaluations"][-1][
                "reason"] == "test_interruption"

            class DeadEvaluationProcess:
                def is_alive(self):
                    return False

                def join(self, timeout=None):
                    return None

                def terminate(self):
                    raise AssertionError("dead process was terminated again")

            async_eval._process = DeadEvaluationProcess()
            async_eval._request_queue = SimpleNamespace(put=lambda _item: None)
            async_eval._pending_snapshots = 1
            try:
                async_eval._on_training_end()
            except RuntimeError as error:
                assert "refusing to publish" in str(error)
            else:
                raise AssertionError(
                    "training end accepted a missing fixed evaluation")
            assert async_eval._process is None
            network_callbacks = [
                callback for callback in callbacks
                if isinstance(callback, m.NetworkRecordingCallback)]
            assert len(network_callbacks) == 1
            assert network_callbacks[0].record_freq == 15

            args.record_network = False
            callbacks = m.create_callbacks(
                lambda: eval_env, "mask_smoke_2", args, num_train_envs=2,
                evaluation_schedule=fixed_schedule)
            assert not any(isinstance(callback, m.NetworkRecordingCallback)
                           for callback in callbacks)

            fidelity = m.StrictTrainingFidelityCallback()
            fidelity.locals = {"infos": [{"game_result": "undetermined"}]}
            assert fidelity._on_step() is True
            fidelity.locals = {"infos": [{
                "game_result": "win",
                "fidelity": {
                    "unparsed_effects": 0,
                    "unparsed_cards": [],
                    "effect_continuation_failures": 0,
                    "effect_continuation_failure_contexts": [],
                    "lost_spell_recoveries": 0,
                    "lost_spell_recovery_contexts": [],
                },
            }]}
            assert fidelity._on_step() is True
            fidelity.locals = {"infos": [{
                "game_result": "win",
                "fidelity": {
                    "effect_continuation_failures": 1,
                    "effect_continuation_failure_contexts": [{
                        "card_name": "Synthetic Failure"}],
                },
            }]}
            try:
                fidelity._on_step()
            except RuntimeError as error:
                assert "effect_continuation_failures" in str(error)
                assert "Synthetic Failure" in str(error)
            else:
                raise AssertionError(
                    "strict fidelity callback ignored fidelity telemetry")
            fidelity.locals = {
                "infos": [{"game_result": "error_opponent_loop"}]}
            try:
                fidelity._on_step()
            except RuntimeError as error:
                assert "Strict training fidelity failure" in str(error)
            else:
                raise AssertionError("strict fidelity callback accepted an engine error")

            fidelity = m.StrictTrainingFidelityCallback()
            fidelity.signature_histories = [[]]
            try:
                for value in [0, 1, 0, 1, 0, 1]:
                    fidelity.locals = {
                        "new_obs": {
                            "state": np.array([[value]], dtype=np.int32)},
                        "actions": np.array([31], dtype=np.int64),
                        "dones": np.array([False]),
                        "infos": [{"policy_state": {"phase": 5}}],
                    }
                    fidelity._on_step()
            except RuntimeError as error:
                assert "cycle of period 2" in str(error)
            else:
                raise AssertionError(
                    "strict training accepted a two-state policy loop")

            metrics = {}
            fake_logger = SimpleNamespace(
                record=lambda name, value: metrics.__setitem__(name, value),
                record_mean=lambda name, value: metrics.__setitem__(name, value),
            )
            rewards = m.RewardComponentsCallback()
            rewards.model = SimpleNamespace(logger=fake_logger)
            rewards.locals = {
                "infos": [
                    {
                        "game_result": "loss",
                        "terminal_reason": "life_total",
                        "reward_components": {
                            "action": 0.002,
                            "state_change": -0.25,
                            "terminal": -10.0,
                        },
                        "reward_diagnostics": {"action_raw": 0.02},
                    },
                    {
                        "game_result": "win",
                        "terminal_reason": "turn_limit",
                        "reward_components": {"terminal": -10.0},
                    }, {}, {},
                ],
                "dones": np.array([True, True, False, False]),
                "rewards": np.array([-10.248, 0.0, 0.1, -0.1]),
            }
            assert rewards._on_step()
            assert metrics["terminal/any_count"] == 2
            assert metrics["terminal/any_rate"] == 0.5
            assert metrics["terminal/life_total_rate"] == 0.25
            assert metrics["terminal/turn_limit_rate"] == 0.25
            assert metrics["outcome/decisive_loss_count"] == 1
            assert metrics["outcome/timeout_count"] == 1
            assert metrics[
                "reward_diagnostic/terminal_result_sign_mismatch_count"] == 0
            assert metrics["reward/state_change"] == -0.25
            assert metrics["reward/state_change_abs"] == 0.25
            assert metrics["reward/state_change_nonzero"] == 1.0
            assert metrics["reward_diagnostic/action_raw"] == 0.02
            assert metrics["reward/total_abs"] == 0.1
            rewards.locals = {
                "infos": [{}, {}, {}, {}],
                "dones": np.array([False, False, False, False]),
            }
            assert rewards._on_step()
            assert metrics["terminal/any_rate"] == 0.25
            assert metrics["terminal/life_total_rate"] == 0.125
            assert metrics["terminal/turn_limit_rate"] == 0.125

            critic = m.CriticDiagnosticsCallback()
            critic.model = SimpleNamespace(
                logger=fake_logger,
                rollout_buffer=SimpleNamespace(
                    values=np.array([0.0, 1.0, 2.0]),
                    returns=np.array([0.0, 1.0, 3.0]),
                    advantages=np.array([0.0, 0.0, 1.0]),
                    rewards=np.array([0.0, 0.1, 1.0]),
                ),
            )
            critic._on_rollout_end()
            assert metrics["critic/return_abs_max"] == 3.0
            assert np.isfinite(metrics["critic/rollout_explained_variance"])

            resources = m.ResourceMonitorCallback(
                os.path.join(tmp, "resource_metrics"))
            resources.num_timesteps = 5000
            resources.n_calls = 625
            resources._sample_index = 17
            assert resources._tensorboard_step() == 5000, \
                "resource metrics used VecEnv calls/sample indices as steps"
        finally:
            m.MODEL_DIR, m.LOG_DIR = old_model_dir, old_log_dir
    eval_env.close()


@stage("hyperparameter evaluation uses an isolated environment")
def check_hyperparameter_eval_isolation():
    import main as m

    class FakeTrial:
        def suggest_float(self, _name, low, _high, **_kwargs):
            return low

        def suggest_categorical(self, _name, choices):
            return choices[0]

        def suggest_int(self, _name, low, _high):
            return low

        def report(self, _reward, _step):
            pass

        def should_prune(self):
            return False

    class FakeVecEnv:
        def __init__(self, number):
            self.number = number
            self.closed = False

        def close(self):
            self.closed = True

    made_envs = []
    evaluated_envs = []
    trained_envs = []

    def fake_make_vec_env(_factory, n_envs):
        assert n_envs == 2
        env = FakeVecEnv(len(made_envs))
        made_envs.append(env)
        return env

    class FakeMaskablePPO:
        def __init__(self, policy, env, **_kwargs):
            assert policy is m.FixedDimensionMaskableActorCriticPolicy
            self.env = env

        def learn(self, **_kwargs):
            trained_envs.append(self.env)

    def fake_evaluate_policy(_model, env, **_kwargs):
        evaluated_envs.append(env)
        return 7.0, 0.0

    patched = {
        "load_decks_and_card_db": lambda _path, **_kwargs: ([], {}),
        "make_vec_env": fake_make_vec_env,
        "MaskablePPO": FakeMaskablePPO,
        "evaluate_policy": fake_evaluate_policy,
    }
    originals = {name: getattr(m, name) for name in patched}
    try:
        for name, replacement in patched.items():
            setattr(m, name, replacement)
        score = m.objective(FakeTrial())

        class FailingMaskablePPO:
            def __init__(self, *_args, **_kwargs):
                raise RuntimeError("synthetic constructor failure")

        m.MaskablePPO = FailingMaskablePPO
        failure_score = m.objective(FakeTrial())
    finally:
        for name, original in originals.items():
            setattr(m, name, original)

    assert score == 7.0
    assert failure_score == float("-inf")
    assert len(made_envs) == 4
    assert trained_envs and all(env is made_envs[0] for env in trained_envs)
    assert evaluated_envs and all(env is made_envs[1]
                                  for env in evaluated_envs)
    assert all(env.closed for env in made_envs)


@stage("CPU fallback and complete Optuna configuration propagation")
def check_runtime_configuration():
    from types import SimpleNamespace

    import gymnasium as gym
    import torch
    import main as m

    original_cpu_count = m.os.cpu_count
    try:
        for reported in (None, 0, 1):
            m.os.cpu_count = lambda value=reported: value
            assert m.safe_cpu_count() == 1
    finally:
        m.os.cpu_count = original_cpu_count

    args = SimpleNamespace(
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=256,
    )
    base_config = m.build_training_config(args)
    assert base_config["gamma"] == 0.999
    assert base_config["gae_lambda"] == 0.98
    assert base_config["vf_coef"] == 0.25
    assert base_config["n_epochs"] == 5
    optimized = {
        "learning_rate": 8e-5,
        "n_steps": 4096,
        "batch_size": 128,
        "gamma_complement": 0.02,
        "gae_lambda": 0.987,
        "clip_range": 0.17,
        "ent_coef": 0.004,
        "policy_neurons": "large",
        "n_epochs": 9,
        "max_grad_norm": 0.77,
        "activation_fn": "tanh",
    }
    config = m.build_training_config(args, optimized)
    assert config == {
        "learning_rate": 8e-5,
        "n_steps": 4096,
        "batch_size": 128,
        "gamma": 0.98,
        "gae_lambda": 0.987,
        "clip_range": 0.17,
        "clip_range_vf": 0.2,
        "ent_coef": 0.004,
        "vf_coef": 0.25,
        "target_kl": 0.02,
        "net_arch": m.NETWORK_ARCHITECTURES["large"],
        "n_epochs": 9,
        "max_grad_norm": 0.77,
        "activation_fn": torch.nn.Tanh,
        "action_reward_scale": 0.0,
        "state_potential_scale": 0.40,
        "reward_contract_version": "discounted-state-potential-v6",
    }

    try:
        m.build_training_config(args, {"not_a_real_parameter": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown optimized parameters were ignored")

    captured = {}

    class CapturingMaskablePPO:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    original_ppo = m.MaskablePPO
    try:
        m.MaskablePPO = CapturingMaskablePPO
        m.create_training_model("training-env", config, 1234, "cpu")
    finally:
        m.MaskablePPO = original_ppo

    assert captured["env"] == "training-env"
    assert captured["seed"] == 1234
    assert captured["device"] == "cpu"
    assert captured["learning_rate"].initial_lr == optimized["learning_rate"]
    for key in (
        "n_steps", "batch_size", "gamma", "gae_lambda", "clip_range",
        "clip_range_vf", "ent_coef", "vf_coef", "target_kl", "n_epochs",
        "max_grad_norm",
    ):
        assert captured[key] == config[key]
    assert captured["policy_kwargs"]["net_arch"] == config["net_arch"]
    assert captured["policy_kwargs"]["activation_fn"] is torch.nn.Tanh

    environment_calls = []

    class FakeRawEnv(gym.Env):
        def action_mask(self, _env=None):
            return np.array([True], dtype=bool)

    def fake_alpha_env(decks, card_db, **kwargs):
        environment_calls.append((decks, card_db, kwargs))
        return FakeRawEnv()

    original_alpha_env = m.AlphaZeroMTGEnv
    try:
        m.AlphaZeroMTGEnv = fake_alpha_env
        with tempfile.TemporaryDirectory() as tmp:
            train_root = os.path.join(tmp, "train")
            eval_root = os.path.join(tmp, "eval")
            train_env = m.make_masked_mtg_env([], {}, train_root)
            eval_env = m.make_masked_mtg_env(
                [], {}, eval_root, agent_is_p1=False,
                alternate_agent_seat=True,
                adaptive_decision_history_enabled=False)
            train_env.close()
            eval_env.close()
    finally:
        m.AlphaZeroMTGEnv = original_alpha_env

    assert environment_calls[0][2]["deck_stats_path"] != (
        environment_calls[1][2]["deck_stats_path"])
    assert environment_calls[0][2]["card_memory_path"] != (
        environment_calls[1][2]["card_memory_path"])
    assert environment_calls[0][2]["agent_is_p1"] is True
    assert environment_calls[0][2]["alternate_agent_seat"] is False
    assert environment_calls[0][2]["strategy_memory_enabled"] is False
    assert environment_calls[0][2]["adaptive_decision_history_enabled"] is False
    assert environment_calls[1][2]["agent_is_p1"] is False
    assert environment_calls[1][2]["alternate_agent_seat"] is True
    assert environment_calls[1][2]["strategy_memory_enabled"] is False
    assert environment_calls[1][2]["adaptive_decision_history_enabled"] is False
    assert environment_calls[0][2]["reward_discount"] == 0.999
    assert environment_calls[0][2]["action_reward_scale"] == 0.0
    assert environment_calls[0][2]["state_potential_scale"] == 0.40
    assert environment_calls[0][2]["curriculum"] is None
    assert environment_calls[0][2]["opponent_profile"] == "scripted"

    # Dirty-run provenance must include newly added source, not only tracked
    # modifications.  Mock Git here so the regression remains valid after the
    # real curriculum files are committed.
    original_subprocess_run = m.subprocess.run
    try:
        def fake_git_run(command, **_kwargs):
            if "ls-files" in command:
                return SimpleNamespace(
                    returncode=0, stdout=b"Playersim/new_source.py\0",
                    stderr=b"")
            if "--no-index" in command:
                return SimpleNamespace(
                    returncode=1,
                    stdout=(b"diff --git a/Playersim/new_source.py "
                            b"b/Playersim/new_source.py\n"
                            b"new file mode 100644\n+new source\n"),
                    stderr=b"")
            return SimpleNamespace(
                returncode=0,
                stdout=b"diff --git a/main.py b/main.py\n+tracked source\n",
                stderr=b"")

        m.subprocess.run = fake_git_run
        with tempfile.TemporaryDirectory() as tmp:
            identity = m.capture_working_tree_patch(tmp)
            assert identity and identity["size_bytes"] > 0
            with open(os.path.join(tmp, "source_worktree.patch"), "rb") as handle:
                patch_payload = handle.read()
            assert b"tracked source" in patch_payload
            assert b"Playersim/new_source.py" in patch_payload
            assert b"new source" in patch_payload
    finally:
        m.subprocess.run = original_subprocess_run

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = os.path.join(tmp, "source_run")
        checkpoint_dir = os.path.join(run_dir, "checkpoints")
        os.makedirs(checkpoint_dir)
        checkpoint_path = os.path.join(checkpoint_dir, "model.zip")
        with open(checkpoint_path, "wb") as handle:
            handle.write(b"checkpoint")
        resume_manifest_path = os.path.join(run_dir, "training_run.json")
        resume_manifest = {
            "kind": "playersim_training_run",
            "run_id": "compatible-v5",
            "resolved": {
                "training_config": {
                    "reward_contract_version":
                        m.AlphaZeroMTGEnv.REWARD_CONTRACT_VERSION,
                },
                "observation_schema_version":
                    m.AlphaZeroMTGEnv.OBSERVATION_SCHEMA_VERSION,
                "observation_schema_sha256":
                    m.AlphaZeroMTGEnv.OBSERVATION_SCHEMA_SHA256,
                "curriculum": None,
            },
        }
        with open(resume_manifest_path, "w", encoding="utf-8") as handle:
            json.dump(resume_manifest, handle)
        lineage = m.validate_resume_lineage(checkpoint_path, "none")
        assert lineage["run_id"] == "compatible-v5"

        resume_manifest["resolved"]["training_config"][
            "reward_contract_version"] = "discounted-state-potential-v4"
        with open(resume_manifest_path, "w", encoding="utf-8") as handle:
            json.dump(resume_manifest, handle)
        try:
            m.validate_resume_lineage(checkpoint_path, "none")
        except ValueError as error:
            assert "reward contract" in str(error)
        else:
            raise AssertionError("reward-v4 checkpoint bypassed resume guard")

        resume_manifest["resolved"]["training_config"][
            "reward_contract_version"] = \
                m.AlphaZeroMTGEnv.REWARD_CONTRACT_VERSION
        resume_manifest["resolved"]["curriculum"] = {"id": "combat-v1"}
        with open(resume_manifest_path, "w", encoding="utf-8") as handle:
            json.dump(resume_manifest, handle)
        try:
            m.validate_resume_lineage(checkpoint_path, "combat-v1")
        except ValueError as error:
            assert "scheduler counters" in str(error)
        else:
            raise AssertionError("curriculum resume bypassed scheduler guard")


@stage("training failures are nonzero and never saved as final")
def check_main_failure_semantics():
    import main as m
    from Playersim.card import Card

    saved_paths = []
    made_vec_envs = []
    assigned_seeds = []
    parent_seeds = []
    environment_vocabularies = []
    frozen_vocab = ("format-schema-alpha", "format-schema-omega")

    class FakeVecEnv:
        def __init__(self, factories):
            self.closed = False
            made_vec_envs.append(self)
            self.envs = [factory() for factory in factories]

        def env_method(self, *_args, **_kwargs):
            pass

        def seed(self, seed):
            assigned_seeds.append(seed)
            return [seed]

        def close(self):
            self.closed = True

    class FakeModel:
        def __init__(self, fail):
            self.fail = fail

        def learn(self, **_kwargs):
            if self.fail:
                raise RuntimeError("synthetic training failure")

        def save(self, path):
            saved_paths.append(path)

    class FailingLoader:
        @classmethod
        def load(cls, *_args, **_kwargs):
            raise RuntimeError("synthetic load failure")

    original_argv = sys.argv
    patched_names = (
        "MODEL_DIR", "LOG_DIR", "TENSORBOARD_DIR",
        "load_decks_and_card_db", "DummyVecEnv", "VecMonitor",
        "StrictEvaluationVecEnv",
        "make_masked_mtg_env",
        "create_callbacks", "create_training_model", "MaskablePPO",
        "record_network_architecture", "set_random_seed", "safe_cpu_count",
    )
    originals = {name: getattr(m, name) for name in patched_names}
    original_set_num_threads = m.torch.set_num_threads
    original_cuda_available = m.torch.cuda.is_available
    original_strftime = m.time.strftime
    original_subtype_vocab = list(Card.SUBTYPE_VOCAB)

    with tempfile.TemporaryDirectory() as tmp:
        try:
            m.MODEL_DIR = os.path.join(tmp, "models")
            m.LOG_DIR = os.path.join(tmp, "logs")
            m.TENSORBOARD_DIR = os.path.join(tmp, "tensorboard")
            def fake_load_decks(_path, **_kwargs):
                Card.SUBTYPE_VOCAB = list(frozen_vocab)
                return [
                    {"name": "Synthetic Deck A", "cards": []},
                    {"name": "Synthetic Deck B", "cards": []},
                ], {}

            def fake_make_masked_env(
                    _decks, _card_db, _storage_root, **kwargs):
                environment_vocabularies.append(
                    tuple(kwargs.get("subtype_vocab") or ()))
                return object()

            m.load_decks_and_card_db = fake_load_decks
            m.make_masked_mtg_env = fake_make_masked_env
            m.DummyVecEnv = FakeVecEnv
            m.VecMonitor = lambda env: env
            m.StrictEvaluationVecEnv = lambda env: env
            m.create_callbacks = lambda *_args, **_kwargs: []
            m.record_network_architecture = lambda *_args, **_kwargs: None
            m.set_random_seed = lambda seed: parent_seeds.append(seed)
            m.safe_cpu_count = lambda: 1
            m.torch.set_num_threads = lambda _count: None
            m.torch.cuda.is_available = lambda: False
            m.time.strftime = lambda _format: "20000101_000000"

            # A failed learn() must return nonzero and save only a clearly
            # incomplete artifact.
            m.create_training_model = lambda *_args: FakeModel(fail=True)
            sys.argv = [
                "main.py", "--timesteps", "1", "--n-envs", "1",
                "--curriculum", "none",
            ]
            assert m.main() == 1
            assert saved_paths and saved_paths[-1].endswith("failed_model")
            assert not any("final_model" in path for path in saved_paths)
            assert made_vec_envs and all(env.closed for env in made_vec_envs)
            first_manifest_path = os.path.join(
                m.MODEL_DIR, "ALPHA_ZERO_MTG_V3.00_20000101_000000",
                "training_run.json")
            with open(first_manifest_path, encoding="utf-8") as handle:
                first_manifest = json.load(handle)
            assert first_manifest["status"] == "failed"
            assert first_manifest["phase"] == "training"
            assert first_manifest["request"]["cli"]["seed"] == \
                m.DEFAULT_TRAINING_SEED
            assert first_manifest["resolved"]["train_worker_seeds"] == [
                m.DEFAULT_TRAINING_SEED]
            assert first_manifest["resolved"]["evaluation_seed"] == \
                m.DEFAULT_EVALUATION_SEED

            # A successful learn() is the only path allowed to publish final_model.
            saved_paths.clear()
            made_vec_envs.clear()
            m.create_training_model = lambda *_args: FakeModel(fail=False)
            sys.argv = [
                "main.py", "--timesteps", "1", "--n-envs", "1",
                "--seed", "1234",
                "--curriculum", "none",
            ]
            assert m.main() == 0
            assert saved_paths and saved_paths[-1].endswith("final_model")
            assert not any("failed_model" in path for path in saved_paths)
            assert all(env.closed for env in made_vec_envs)
            second_manifest_path = os.path.join(
                m.MODEL_DIR, "ALPHA_ZERO_MTG_V3.00_20000101_000000_1",
                "training_run.json")
            with open(second_manifest_path, encoding="utf-8") as handle:
                second_manifest = json.load(handle)
            assert second_manifest["status"] == "complete"
            assert second_manifest["validation"]["status"] == "skipped"
            assert second_manifest["request"]["cli"]["seed"] == 1234
            assert second_manifest["resolved"]["evaluation_seed"] == \
                m.DEFAULT_EVALUATION_SEED
            assert second_manifest["resolved"][
                "fixed_evaluation_schedule_sha256"] == first_manifest[
                    "resolved"]["fixed_evaluation_schedule_sha256"]

            # A checkpoint load failure must also be observable to the shell.
            saved_paths.clear()
            made_vec_envs.clear()
            m.MaskablePPO = FailingLoader
            resume_source_dir = os.path.join(tmp, "resume_source")
            os.makedirs(resume_source_dir)
            resume_checkpoint = os.path.join(
                resume_source_dir, "compatible_checkpoint.zip")
            with open(resume_checkpoint, "wb") as handle:
                handle.write(b"checkpoint")
            with open(os.path.join(
                    resume_source_dir, "training_run.json"), "w",
                    encoding="utf-8") as handle:
                json.dump({
                    "kind": "playersim_training_run",
                    "run_id": "resume-source",
                    "resolved": {
                        "training_config": {
                            "reward_contract_version":
                                m.AlphaZeroMTGEnv.REWARD_CONTRACT_VERSION,
                        },
                        "observation_schema_version":
                            m.AlphaZeroMTGEnv.OBSERVATION_SCHEMA_VERSION,
                        "observation_schema_sha256":
                            m.AlphaZeroMTGEnv.OBSERVATION_SCHEMA_SHA256,
                        "curriculum": None,
                    },
                }, handle)
            sys.argv = [
                "main.py", "--timesteps", "1", "--n-envs", "1",
                "--resume", resume_checkpoint,
                "--curriculum", "none",
            ]
            assert m.main() == 1
            assert not saved_paths
            assert all(env.closed for env in made_vec_envs)
            third_manifest_path = os.path.join(
                m.MODEL_DIR, "ALPHA_ZERO_MTG_V3.00_20000101_000000_2",
                "training_run.json")
            with open(third_manifest_path, encoding="utf-8") as handle:
                third_manifest = json.load(handle)
            assert third_manifest["status"] == "failed"
            assert third_manifest["phase"] == "model_setup"

            # Deck-loading failures happen before environments exist but still
            # must return a nonzero process status.
            m.load_decks_and_card_db = lambda _path, **_kwargs: (
                _ for _ in ()).throw(
                RuntimeError("synthetic deck-load failure"))
            sys.argv = ["main.py"]
            assert m.main() == 1
            fourth_manifest_path = os.path.join(
                m.MODEL_DIR, "ALPHA_ZERO_MTG_V3.00_20000101_000000_3",
                "training_run.json")
            with open(fourth_manifest_path, encoding="utf-8") as handle:
                fourth_manifest = json.load(handle)
            assert fourth_manifest["status"] == "failed"
            assert fourth_manifest["phase"] == "data_loading"
            assert parent_seeds == [
                m.DEFAULT_TRAINING_SEED, 1234,
                m.DEFAULT_TRAINING_SEED, m.DEFAULT_TRAINING_SEED]
            # Evaluation envs are no longer built (or seeded) in the training
            # process: the async evaluation worker and the final-validation
            # branch construct their own via make_evaluation_vec_env, which
            # seeds with the evaluation offset internally. With fake models
            # that never write a checkpoint, only train envs are seeded here.
            assert assigned_seeds == [m.DEFAULT_TRAINING_SEED, 1234,
                                      m.DEFAULT_TRAINING_SEED]
            assert environment_vocabularies
            assert all(
                vocab == frozen_vocab for vocab in environment_vocabularies), (
                    "main did not pass the frozen format vocabulary to every "
                    "training and validation environment")

            # Ctrl-C is an interruption, not a failed experiment, and its
            # recoverable checkpoint must carry the same distinction.
            class InterruptingModel(FakeModel):
                def learn(self, **_kwargs):
                    raise KeyboardInterrupt()

            saved_paths.clear()
            made_vec_envs.clear()
            m.load_decks_and_card_db = fake_load_decks
            m.create_training_model = lambda *_args: InterruptingModel(False)
            sys.argv = [
                "main.py", "--timesteps", "1", "--n-envs", "1",
                "--curriculum", "none",
            ]
            assert m.main() == 130
            assert saved_paths and saved_paths[-1].endswith(
                "interrupted_model")
            assert not any("failed_model" in path for path in saved_paths)
            interrupted_manifest_path = os.path.join(
                m.MODEL_DIR, "ALPHA_ZERO_MTG_V3.00_20000101_000000_4",
                "training_run.json")
            with open(interrupted_manifest_path, encoding="utf-8") as handle:
                interrupted_manifest = json.load(handle)
            assert interrupted_manifest["status"] == "interrupted"
            assert interrupted_manifest["failure"] is None
            assert interrupted_manifest["interruption"]["type"] == \
                "KeyboardInterrupt"
            assert all(env.closed for env in made_vec_envs)
        finally:
            sys.argv = original_argv
            for name, original in originals.items():
                setattr(m, name, original)
            m.torch.set_num_threads = original_set_num_threads
            m.torch.cuda.is_available = original_cuda_available
            m.time.strftime = original_strftime
            Card.SUBTYPE_VOCAB = original_subtype_vocab


@stage("format corpus flags load the frozen namespace and stamp lineage")
def check_format_corpus_lineage():
    import main as m
    from Playersim.card import Card
    from Playersim.card_registry import (
        load_feature_schema, load_registry)

    format_dir = os.path.join(REPO_ROOT, "formats", "standard")
    registry = load_registry(os.path.join(format_dir, "card_registry.json"))
    schema = load_feature_schema(
        os.path.join(format_dir, "feature_schema.json"))
    saved_vocab = list(Card.SUBTYPE_VOCAB)
    try:
        decks, card_db, decks_dir, lineage = m.load_training_corpus(
            None, "standard", None)
        assert decks_dir == m.DECKS_DIR
        assert lineage["format"] == "standard"
        assert lineage["card_registry"]["sha256"] == registry["sha256"]
        assert lineage["feature_schema"]["sha256"] == schema["sha256"]
        assert lineage["pool_snapshot"]["format"] == "standard"
        assert lineage["corpus"]["sha256"]
        # Canonical IDs replace insertion-order IDs across the corpus.
        expected = {entry["name"].lower(): entry["index"]
                    for entry in registry["cards"]}
        assert card_db, "no cards loaded"
        for card_id, card in card_db.items():
            assert card_id == expected[card.name.lower()], (
                f"{card.name} loaded with id {card_id}, registry says "
                f"{expected[card.name.lower()]}")
        # The frozen schema pins the exact observation feature width.
        assert Card.SUBTYPE_VOCAB == schema["subtype_vocab"]
        any_card = next(iter(card_db.values()))
        assert len(any_card.to_feature_vector()) == schema["feature_dim"]

        # No flags use the same strict pinned Standard lineage.
        _, _, _, default_lineage = m.load_training_corpus(None, None, None)
        assert default_lineage["format"] == "standard"
        assert default_lineage["card_registry"] == lineage["card_registry"]
        assert default_lineage["corpus"]["sha256"] == lineage["corpus"]["sha256"]
    finally:
        Card.SUBTYPE_VOCAB = saved_vocab


@stage("two-worker SubprocVecEnv reset, masks, steps, and close")
def check_subproc_vec_env(deck_folder):
    from sb3_contrib.common.maskable.utils import get_action_masks
    from stable_baselines3.common.vec_env import SubprocVecEnv
    from Playersim.card import load_decks_and_card_db
    from Playersim.card import Card

    decks, card_db = load_decks_and_card_db(deck_folder)
    # Frozen schemas contain the complete format vocabulary, including columns
    # absent from this particular deck corpus.  That final sentinel reproduces
    # the production parent/Windows-spawn vocabulary difference.
    subtype_vocab = tuple(Card.SUBTYPE_VOCAB) + (
        "schema-only-subproc-smoke-subtype",)
    expected_feature_dim = (
        4 + 6 + len(Card.ALL_KEYWORDS) + 5 + len(subtype_vocab) + 3)
    vec_env = None

    # Keep every file a worker may produce outside the project tree. Closing
    # the worker pool before leaving this context also makes cleanup reliable
    # on Windows, where open child-process handles prevent directory removal.
    with tempfile.TemporaryDirectory() as storage_root:
        factories = [
            partial(
                _make_subproc_masked_env,
                decks,
                card_db,
                storage_root,
                worker_index,
                subtype_vocab,
            )
            for worker_index in range(2)
        ]
        try:
            # Explicit ``spawn`` exercises the Windows production failure mode
            # even when this smoke test is run on another operating system.
            vec_env = SubprocVecEnv(factories, start_method="spawn")
            assert vec_env.num_envs == 2
            assert vec_env.observation_space.spaces["my_hand"].shape == (
                10, expected_feature_dim), (
                    "spawned worker rebuilt the corpus vocabulary instead of "
                    "using the frozen feature schema")

            assigned_seeds = vec_env.seed(20260710)
            assert assigned_seeds == [20260710, 20260711]
            observations = vec_env.reset()

            for _ in range(4):
                assert isinstance(observations, dict)
                assert observations
                assert all(np.asarray(value).shape[0] == 2
                           for value in observations.values())

                masks = np.asarray(get_action_masks(vec_env), dtype=bool)
                assert masks.shape == (2, vec_env.action_space.n)
                assert np.all(masks.any(axis=1)), (
                    "a subprocess returned an action mask with no legal action")
                assert np.array_equal(
                    np.asarray(observations["action_mask"], dtype=bool), masks), (
                    "batched observation masks differ from live worker masks")

                actions = np.argmax(masks, axis=1).astype(np.int64)
                assert all(masks[index, action]
                           for index, action in enumerate(actions))
                observations, rewards, dones, infos = vec_env.step(actions)

                assert np.asarray(rewards).shape == (2,)
                assert np.isfinite(rewards).all()
                assert np.asarray(dones).shape == (2,)
                assert len(infos) == 2
                assert all(isinstance(info, dict) for info in infos)
        finally:
            if vec_env is not None:
                vec_env.close()

        assert vec_env is not None and vec_env.closed


@stage("build masked vec env from fixture decks")
def build_vec_env(deck_folder):
    from stable_baselines3.common.vec_env import DummyVecEnv
    from sb3_contrib.common.wrappers import ActionMasker
    from Playersim.card import Card, load_decks_and_card_db
    from Playersim.environment import AlphaZeroMTGEnv

    decks, card_db = load_decks_and_card_db(deck_folder)
    subtype_vocab = tuple(Card.SUBTYPE_VOCAB) + (
        "schema-only-save-load-smoke-subtype",)

    def make_schema_env():
        return ActionMasker(
            AlphaZeroMTGEnv(
                decks, card_db, subtype_vocab=subtype_vocab,
                **test_artifact_paths()),
            action_mask_fn="action_mask")

    vec_env = DummyVecEnv([make_schema_env])
    vec_env.run_subtype_vocab = subtype_vocab
    vec_env.run_decks = decks
    vec_env.run_card_db = card_db
    return vec_env


@stage("construct MaskablePPO with custom extractor/policy")
def build_model(vec_env):
    from sb3_contrib.ppo_mask import MaskablePPO
    import main as m

    policy_kwargs = {
        "features_extractor_class": m.FixedWindowMTGExtractor,
        "features_extractor_kwargs": {"features_dim": m.FEATURE_OUTPUT_DIM},
        "net_arch": {"pi": [64, 32], "vf": [64, 32]},  # small for test speed
    }
    model = MaskablePPO(
        policy=m.FixedDimensionMaskableActorCriticPolicy,
        env=vec_env,
        policy_kwargs=policy_kwargs,
        n_steps=64,
        batch_size=64,
        n_epochs=1,
        verbose=0,
    )
    return model


@stage("regression: extractor weights registered and trainable")
def check_extractor_registration(model):
    extractor = model.policy.features_extractor

    # 1. The per-key sub-networks must be registered modules (ModuleDict).
    state_keys = list(extractor.state_dict().keys())
    assert any(k.startswith("extractors.") for k in state_keys), (
        "extractors.* missing from state_dict — the plain-dict bug is back")
    assert any(k.startswith("semantic_identity_embedding.") for k in state_keys), (
        "shared semantic identity embedding missing from state_dict")
    from Playersim.observation_schema import SEMANTIC_IDENTITY_FIELDS
    identity_fields = set(SEMANTIC_IDENTITY_FIELDS)
    intentionally_external = {"phase", "action_mask", "target_card_ids"}
    expected_extractor_keys = (
        set(model.observation_space.spaces)
        - intentionally_external - identity_fields)
    assert set(extractor.extractors) == expected_extractor_keys, (
        "observation fields silently omitted by the feature extractor: "
        f"{sorted(expected_extractor_keys - set(extractor.extractors))}")
    assert set(extractor.semantic_identity_fields) == identity_fields, (
        "semantic identities are not exhaustively routed through categorical "
        f"embedding: {sorted(identity_fields - set(extractor.semantic_identity_fields))}")
    assert extractor.semantic_identity_embedding.num_embeddings == 65_536, (
        "semantic identity vocabulary drifted with the active deck corpus")
    assert "ability_recommendations" in extractor.extractors, (
        "rank-3 ability recommendations do not reach the policy")

    # 2. feature_merger must exist at construction time, before any forward().
    assert hasattr(extractor, "feature_merger"), (
        "feature_merger missing at construction — the lazy-creation bug is back")

    # 3. Every extractor parameter must be covered by the optimizer.
    opt_param_ids = {id(p) for group in model.policy.optimizer.param_groups
                     for p in group["params"]}
    missing = [n for n, p in extractor.named_parameters()
               if p.requires_grad and id(p) not in opt_param_ids]
    assert not missing, f"parameters invisible to the optimizer: {missing}"

    # 4. The phase embedding must cover every engine phase constant.
    from Playersim.game_state import GameState
    true_max_phase = max(v for k, v in vars(GameState).items()
                         if k.startswith("PHASE_") and isinstance(v, int))
    n_emb = extractor.phase_embedding.num_embeddings
    assert n_emb >= true_max_phase + 1, (
        f"phase embedding has {n_emb} slots but phases reach {true_max_phase}")


@stage("one model directory contains every run artifact")
def check_model_artifact_layout(model):
    import main as m

    original_model_dir = m.MODEL_DIR
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            m.MODEL_DIR = temp_dir
            run_id = "LAYOUT_TEST_RUN"
            run_model_dir = os.path.join(temp_dir, run_id)
            os.makedirs(run_model_dir)
            m.record_network_architecture(model, run_id)

            summary = os.path.join(
                run_model_dir, "architecture", "network_summary.txt")
            assert os.path.isfile(summary)
            assert sorted(os.listdir(temp_dir)) == [run_id], (
                "recording architecture created a sibling top-level model "
                f"directory: {os.listdir(temp_dir)}")
            artifacts = m.training_artifacts(run_model_dir, run_id)
            expected_display_path = os.path.relpath(
                summary, m.BASE_DIR).replace(os.sep, "/")
            assert artifacts["network_summary"]["path"] == expected_display_path
    finally:
        m.MODEL_DIR = original_model_dir


@stage("short training run (128 timesteps)")
def short_train(model):
    model.learn(total_timesteps=128, progress_bar=False)


@stage("schema-pinned save -> fresh validation env -> load -> predict")
def save_load_roundtrip(model, vec_env):
    from sb3_contrib.ppo_mask import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv
    from Playersim.card import Card
    import main as m

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "roundtrip_model")
        model.save(path)
        run_vocab = tuple(vec_env.run_subtype_vocab)
        saved_global_vocab = list(Card.SUBTYPE_VOCAB)
        validation_env = None
        try:
            # Mimic a fresh spawned process whose global vocabulary contains
            # only corpus subtypes, not the schema-wide sentinel column.
            Card.SUBTYPE_VOCAB = list(run_vocab[:-1])

            def make_validation_env():
                return m.make_masked_mtg_env(
                    vec_env.run_decks,
                    vec_env.run_card_db,
                    os.path.join(tmp, "validation_env"),
                    subtype_vocab=run_vocab)

            validation_env = DummyVecEnv([make_validation_env])
            assert validation_env.observation_space == vec_env.observation_space
            loaded = MaskablePPO.load(path, env=validation_env)
            obs = validation_env.reset()
            masks = np.stack(validation_env.env_method("action_mask"))
            action, _ = loaded.predict(
                obs, action_masks=masks, deterministic=True)
            assert action is not None and len(action) == 1
        finally:
            if validation_env is not None:
                validation_env.close()
            Card.SUBTYPE_VOCAB = saved_global_vocab


def main():
    print("Playersim training-stack smoke test")
    print("=" * 50)
    reset_test_artifacts()
    if do_imports() is None:
        return finish()
    check_mask_aware_evaluation()
    check_hyperparameter_eval_isolation()
    check_runtime_configuration()
    check_main_failure_semantics()
    check_format_corpus_lineage()

    # Reuse the fixture decks from the engine smoke test.
    from smoke_test import build_fixture_decks  # tests/ is on sys.path via cwd
    with tempfile.TemporaryDirectory() as folder:
        build_fixture_decks(folder)
        check_subproc_vec_env(folder)
        vec_env = build_vec_env(folder)
        if vec_env is None:
            return finish()
        model = build_model(vec_env)
        if model is None:
            return finish()
        check_extractor_registration(model)
        check_model_artifact_layout(model)
        short_train(model)
        save_load_roundtrip(model, vec_env)
        vec_env.close()
    return finish()


def finish():
    print("=" * 50)
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} stages passed")
    for name, ok, err in failed:
        print(f"  FAILED: {name} -> {err}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
