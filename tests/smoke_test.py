"""
Smoke test / safety net for the Playersim MTG engine.

Run from the repository root (the folder that CONTAINS the Playersim package):

    python tests/smoke_test.py

What it does:
  1. Regression-checks every bug fixed so far, so none can silently return.
  2. Verifies every module in the package imports cleanly (no import-time
     crashes, no absolute self-imports).
  3. Builds two synthetic 60-card decks in a temp folder (no external data).
  4. Loads them through load_decks_and_card_db.
  5. Constructs AlphaZeroMTGEnv, calls reset(), validates the observation.
  6. Plays several episodes by sampling random *valid* actions from the mask.
  7. Reports PASS/FAIL per stage and exits non-zero on any failure.

Run this BEFORE and AFTER every refactoring step. If it passed before your
change and fails after, your change broke something.
"""

import json
import logging
import os
import random
import sys
import tempfile
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import numpy as np  # noqa: E402

logging.disable(logging.CRITICAL)

SEED = 42
MAX_STEPS_PER_EPISODE = 200
NUM_EPISODES = 3


# ---------------------------------------------------------------------------
# Fixture decks
# ---------------------------------------------------------------------------

def _card(name, mana_cost, type_line, cmc, colors, power=None, toughness=None, text=""):
    data = {
        "name": name,
        "mana_cost": mana_cost,
        "type_line": type_line,
        "cmc": cmc,
        "color_identity": colors,
        "oracle_text": text,
    }
    if power is not None:
        data["power"] = str(power)
        data["toughness"] = str(toughness)
    return data


def build_fixture_decks(folder):
    """Write two simple mono-colored 60-card decks as JSON files."""
    red = [
        _card("Ember Grunt", "{R}", "Creature — Goblin", 1, ["R"], 1, 1),
        _card("Cinder Brawler", "{1}{R}", "Creature — Goblin Warrior", 2, ["R"], 2, 2),
        _card("Flame Charger", "{1}{R}", "Creature — Elemental", 2, ["R"], 2, 1, "Haste"),
        _card("Ash Vanguard", "{2}{R}", "Creature — Human Soldier", 3, ["R"], 3, 2),
        _card("Magma Bruiser", "{3}{R}", "Creature — Ogre", 4, ["R"], 4, 3),
        _card("Furnace Colossus", "{4}{R}{R}", "Creature — Giant", 6, ["R"], 6, 5),
        _card("Spark Jolt", "{R}", "Instant", 1, ["R"], text="Spark Jolt deals 2 damage to any target."),
        _card("Lava Blast", "{2}{R}", "Sorcery", 3, ["R"], text="Lava Blast deals 3 damage to any target."),
        _card("Battle Rush", "{1}{R}", "Instant", 2, ["R"], text="Target creature gets +2/+0 until end of turn."),
    ]
    green = [
        _card("Sprout Guardian", "{G}", "Creature — Elf", 1, ["G"], 1, 2),
        _card("Vine Stalker", "{1}{G}", "Creature — Beast", 2, ["G"], 2, 2),
        _card("Thicket Brute", "{2}{G}", "Creature — Beast", 3, ["G"], 3, 3),
        _card("Moss Titan", "{3}{G}", "Creature — Elemental", 4, ["G"], 4, 4, "Trample"),
        _card("Canopy Sentinel", "{2}{G}", "Creature — Spider", 3, ["G"], 2, 4, "Reach"),
        _card("Elder Wurm", "{4}{G}{G}", "Creature — Wurm", 6, ["G"], 6, 6, "Trample"),
        _card("Wild Growth Ritual", "{G}", "Sorcery", 1, ["G"], text="Search your library for a basic land card and put it onto the battlefield tapped."),
        _card("Predator's Bite", "{1}{G}", "Instant", 2, ["G"], text="Target creature gets +3/+3 until end of turn."),
        _card("Nature's Meal", "{2}{G}", "Sorcery", 3, ["G"], text="You gain 5 life."),
    ]
    mountain = _card("Mountain", "", "Basic Land — Mountain", 0, ["R"], text="{T}: Add {R}.")
    forest = _card("Forest", "", "Basic Land — Forest", 0, ["G"], text="{T}: Add {G}.")

    def deck_json(spells, land):
        entries = [{"card": c, "count": 4} for c in spells]      # 9 * 4 = 36
        entries.append({"card": land, "count": 24})               # + 24 = 60
        return {"deck": entries}

    with open(os.path.join(folder, "smoke_red.json"), "w") as f:
        json.dump(deck_json(red, mountain), f)
    with open(os.path.join(folder, "smoke_green.json"), "w") as f:
        json.dump(deck_json(green, forest), f)


# ---------------------------------------------------------------------------
# Test stages
# ---------------------------------------------------------------------------

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


@stage("regression: fixed bugs stay fixed")
def check_regressions():
    import inspect

    # 1. deck_stats_tracker must expose GamePosition, not a colliding GameState enum.
    from Playersim import deck_stats_tracker as dst
    assert hasattr(dst, "GamePosition"), "GamePosition enum missing"
    assert not hasattr(dst, "GameState"), "colliding GameState enum re-appeared"

    # 2. game_state must not import TargetingSystem from ability_handler.
    from Playersim import game_state as gs_mod
    assert "from .ability_handler import TargetingSystem" not in inspect.getsource(gs_mod)

    # 3. No class may define the same method twice (the second silently wins).
    from Playersim import environment as env_mod
    from Playersim import enhanced_mana_system, replacement_effects
    assert inspect.getsource(env_mod).count("def _get_strategic_advice(") == 1
    assert inspect.getsource(enhanced_mana_system).count("def calculate_cost_increase(") == 1
    assert inspect.getsource(replacement_effects).count("def remove_effect(") == 1
    assert inspect.getsource(gs_mod).count("def find_card_location(") == 1
    assert inspect.getsource(gs_mod).count("def initialize_day_night_cycle(") == 1

    # 4. remove_effect must clean the event-type index, not just the list,
    #    or removed effects keep firing through effect_index.
    assert "effect_index" in inspect.getsource(
        replacement_effects.ReplacementEffectSystem.remove_effect)

    # 5. combat.py holds CombatResolver; the old name must survive as an alias.
    from Playersim import combat
    assert hasattr(combat, "CombatResolver"), "CombatResolver missing from combat.py"
    assert combat.EnhancedCombatResolver is combat.CombatResolver, "compat alias broken"
    from Playersim.enhanced_combat import ExtendedCombatResolver
    assert issubclass(ExtendedCombatResolver, combat.CombatResolver)


@stage("import hygiene: every module imports cleanly")
def check_import_hygiene():
    """Importing any module must never crash or do heavy work (file I/O, etc.)."""
    import importlib
    import pkgutil
    import re

    import Playersim
    failures = []
    for info in pkgutil.iter_modules(Playersim.__path__):
        try:
            importlib.import_module(f"Playersim.{info.name}")
        except Exception as e:
            failures.append(f"{info.name}: {type(e).__name__}: {e}")
    assert not failures, "modules failed to import: " + "; ".join(failures)

    # Package-internal imports must be relative. (__init__.py is exempt: it only
    # shows absolute imports in its docstring as guidance for external callers.)
    pkg_dir = Playersim.__path__[0]
    offenders = []
    for fname in sorted(os.listdir(pkg_dir)):
        if fname.endswith(".py") and fname != "__init__.py":
            with open(os.path.join(pkg_dir, fname), encoding="utf-8") as f:
                if re.search(r"^\s*from Playersim\.", f.read(), re.M):
                    offenders.append(fname)
    assert not offenders, f"absolute self-imports found in: {offenders}"


@stage("load decks and card database")
def load_data(folder):
    from Playersim.card import load_decks_and_card_db
    decks, card_db = load_decks_and_card_db(folder)
    assert len(decks) == 2, f"expected 2 decks, got {len(decks)}"
    for d in decks:
        assert len(d["cards"]) == 60, f"deck {d['name']} has {len(d['cards'])} cards"
    assert len(card_db) == 20, f"expected 20 unique cards, got {len(card_db)}"
    return decks, card_db


@stage("construct environment")
def build_env(decks, card_db):
    from Playersim.environment import AlphaZeroMTGEnv
    from Playersim.game_state import GameState
    env = AlphaZeroMTGEnv(decks, card_db)
    assert env.action_space is not None
    assert env.observation_space is not None

    # Regression: the observation space must cover ALL phase constants,
    # not just PHASE_CLEANUP (special phases go up to PHASE_CHOOSE = 19).
    true_max_phase = max(v for k, v in vars(GameState).items()
                         if k.startswith("PHASE_") and isinstance(v, int))
    declared_high = int(np.asarray(env.observation_space.spaces["phase"].high).max())
    assert declared_high == true_max_phase, (
        f"phase space high={declared_high}, but engine phases reach {true_max_phase}")
    onehot_len = env.observation_space.spaces["phase_onehot"].shape[0]
    assert onehot_len == true_max_phase + 1, (
        f"phase_onehot length {onehot_len} != {true_max_phase + 1}")
    return env


@stage("reset() returns a valid observation")
def check_reset(env):
    obs, info = env.reset(seed=SEED)
    assert isinstance(obs, dict), f"obs should be a dict, got {type(obs)}"
    for key, space in env.observation_space.spaces.items():
        assert key in obs, f"observation missing key '{key}'"
        arr = np.asarray(obs[key])
        assert tuple(arr.shape) == tuple(space.shape), (
            f"obs['{key}'] shape {arr.shape} != space shape {space.shape}")
        assert np.all(np.isfinite(arr)), f"obs['{key}'] contains non-finite values"
    mask = env.action_mask()
    assert mask.any(), "no valid actions available immediately after reset"
    return obs


@stage("play random-valid-action episodes")
def play_episodes(env):
    rng = random.Random(SEED)
    for episode in range(NUM_EPISODES):
        env.reset(seed=SEED + episode)
        steps = 0
        terminated = truncated = False
        while not (terminated or truncated) and steps < MAX_STEPS_PER_EPISODE:
            mask = env.action_mask()
            valid = np.where(mask)[0]
            assert len(valid) > 0, f"episode {episode}: empty action mask at step {steps}"
            action = int(rng.choice(list(valid)))
            obs, reward, terminated, truncated, info = env.step(action)
            assert np.isfinite(reward), f"episode {episode}: non-finite reward at step {steps}"
            steps += 1
        print(f"        episode {episode}: {steps} steps, "
              f"terminated={terminated}, truncated={truncated}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Playersim smoke test")
    print("=" * 50)
    check_regressions()
    check_import_hygiene()
    with tempfile.TemporaryDirectory() as folder:
        build_fixture_decks(folder)
        data = load_data(folder)
        if data is None:
            return finish()
        decks, card_db = data
        env = build_env(decks, card_db)
        if env is None:
            return finish()
        if check_reset(env) is not None:
            play_episodes(env)
    return finish()


def finish():
    print("=" * 50)
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} stages passed")
    for name, ok, err in failed:
        print(f"  FAILED: {name} -> {err}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
