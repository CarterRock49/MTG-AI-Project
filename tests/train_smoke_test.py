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
  2. Every feature-extractor parameter is registered with PyTorch and covered
     by the optimizer (regression for the plain-dict extractors bug and the
     lazily-created feature_merger bug — both used to leave weights untrained).
  3. The phase embedding is large enough for every engine phase constant
     (regression for the Embedding(10, ...) IndexError crash).
  4. A short MaskablePPO training run completes.
  5. Save -> load -> predict round-trips (used to fail because feature_merger
     did not exist on a freshly constructed policy).
"""

import os
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
    import main  # noqa: F401  (must not execute training on import)
    return True


@stage("build masked vec env from fixture decks")
def build_vec_env(deck_folder):
    from stable_baselines3.common.vec_env import DummyVecEnv
    from sb3_contrib.common.wrappers import ActionMasker
    from Playersim.card import load_decks_and_card_db
    from Playersim.environment import AlphaZeroMTGEnv

    decks, card_db = load_decks_and_card_db(deck_folder)

    def make_env():
        return ActionMasker(AlphaZeroMTGEnv(decks, card_db), action_mask_fn="action_mask")

    return DummyVecEnv([make_env])


@stage("construct MaskablePPO with custom extractor/policy")
def build_model(vec_env):
    from sb3_contrib.ppo_mask import MaskablePPO
    import main as m

    policy_kwargs = {
        "features_extractor_class": m.CompletelyFixedMTGExtractor,
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


@stage("short training run (128 timesteps)")
def short_train(model):
    model.learn(total_timesteps=128, progress_bar=False)


@stage("save -> load -> predict round-trip")
def save_load_roundtrip(model, vec_env):
    from sb3_contrib.ppo_mask import MaskablePPO
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "roundtrip_model")
        model.save(path)
        loaded = MaskablePPO.load(path, env=vec_env)
        obs = vec_env.reset()
        masks = np.stack(vec_env.env_method("action_mask"))
        action, _ = loaded.predict(obs, action_masks=masks, deterministic=True)
        assert action is not None and len(action) == 1


def main():
    print("Playersim training-stack smoke test")
    print("=" * 50)
    if do_imports() is None:
        return finish()

    # Reuse the fixture decks from the engine smoke test.
    from smoke_test import build_fixture_decks  # tests/ is on sys.path via cwd
    with tempfile.TemporaryDirectory() as folder:
        build_fixture_decks(folder)
        vec_env = build_vec_env(folder)
        if vec_env is None:
            return finish()
        model = build_model(vec_env)
        if model is None:
            return finish()
        check_extractor_registration(model)
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
