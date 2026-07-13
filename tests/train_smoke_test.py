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
        eval_episodes=3,
        checkpoint_freq=20,
        record_network=True,
        record_freq=30,
    )
    with tempfile.TemporaryDirectory() as tmp:
        old_model_dir, old_log_dir = m.MODEL_DIR, m.LOG_DIR
        m.MODEL_DIR = os.path.join(tmp, "models")
        m.LOG_DIR = os.path.join(tmp, "logs")
        try:
            callbacks = m.create_callbacks(
                eval_env, "mask_smoke", args, num_train_envs=2)
            assert isinstance(callbacks[0], MaskableEvalCallback)
            assert callbacks[0].use_masking is True
            assert callbacks[0].deterministic is True
            assert callbacks[0].eval_freq == 5
            assert callbacks[0].n_eval_episodes == 3
            assert callbacks[1].save_freq == 10
            assert callbacks[0].best_model_save_path == os.path.join(
                m.MODEL_DIR, "mask_smoke", "best_model")
            assert callbacks[0].log_path == os.path.join(
                m.LOG_DIR, "mask_smoke", "evaluation", "evaluations")
            assert callbacks[1].save_path == os.path.join(
                m.MODEL_DIR, "mask_smoke", "checkpoints")
            assert any(isinstance(callback, m.StrictTrainingFidelityCallback)
                       for callback in callbacks)
            network_callbacks = [
                callback for callback in callbacks
                if isinstance(callback, m.NetworkRecordingCallback)]
            assert len(network_callbacks) == 1
            assert network_callbacks[0].record_freq == 15

            args.record_network = False
            callbacks = m.create_callbacks(
                eval_env, "mask_smoke_2", args, num_train_envs=2)
            assert not any(isinstance(callback, m.NetworkRecordingCallback)
                           for callback in callbacks)

            fidelity = m.StrictTrainingFidelityCallback()
            fidelity.locals = {"infos": [{"game_result": "undetermined"}]}
            assert fidelity._on_step() is True
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
                    {"terminal_reason": "life_total"},
                    {"terminal_reason": "decking"}, {}, {},
                ],
                "dones": np.array([True, True, False, False]),
            }
            assert rewards._on_step()
            assert metrics["terminal/any_count"] == 2
            assert metrics["terminal/any_rate"] == 0.5
            assert metrics["terminal/life_total_rate"] == 0.25
            assert metrics["terminal/decking_rate"] == 0.25
            rewards.locals = {
                "infos": [{}, {}, {}, {}],
                "dones": np.array([False, False, False, False]),
            }
            assert rewards._on_step()
            assert metrics["terminal/any_rate"] == 0.25
            assert metrics["terminal/life_total_rate"] == 0.125
            assert metrics["terminal/decking_rate"] == 0.125

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
        "ent_coef": 0.004,
        "net_arch": m.NETWORK_ARCHITECTURES["large"],
        "n_epochs": 9,
        "max_grad_norm": 0.77,
        "activation_fn": torch.nn.Tanh,
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
        "ent_coef", "n_epochs", "max_grad_norm",
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
                alternate_agent_seat=True)
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
    assert environment_calls[1][2]["agent_is_p1"] is False
    assert environment_calls[1][2]["alternate_agent_seat"] is True


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
                return [object()], {}

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
            sys.argv = ["main.py", "--timesteps", "1", "--n-envs", "1"]
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
            assert first_manifest["request"]["cli"]["seed"] == 42
            assert first_manifest["resolved"]["train_worker_seeds"] == [42]

            # A successful learn() is the only path allowed to publish final_model.
            saved_paths.clear()
            made_vec_envs.clear()
            m.create_training_model = lambda *_args: FakeModel(fail=False)
            sys.argv = [
                "main.py", "--timesteps", "1", "--n-envs", "1",
                "--seed", "1234",
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

            # A checkpoint load failure must also be observable to the shell.
            saved_paths.clear()
            made_vec_envs.clear()
            m.MaskablePPO = FailingLoader
            sys.argv = [
                "main.py", "--timesteps", "1", "--n-envs", "1",
                "--resume", "missing.zip",
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
            assert parent_seeds == [42, 1234, 42, 42]
            assert assigned_seeds == [
                42, 1000042, 1234, 1001234, 42, 1000042]
            assert environment_vocabularies
            assert all(
                vocab == frozen_vocab for vocab in environment_vocabularies), (
                    "main did not pass the frozen format vocabulary to every "
                    "training and validation environment")
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
