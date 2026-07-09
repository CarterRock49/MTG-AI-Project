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
import shutil
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
TEST_ARTIFACT_ROOT = os.path.join(REPO_ROOT, "tests", "test_artifacts", "smoke")


def test_artifact_paths(name):
    root = os.path.join(TEST_ARTIFACT_ROOT, name)
    return {
        "deck_stats_path": os.path.join(root, "deck_stats"),
        "card_memory_path": os.path.join(root, "card_memory"),
    }


def reset_test_artifacts():
    shutil.rmtree(TEST_ARTIFACT_ROOT, ignore_errors=True)


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
        _card("Ember Herald", "{1}{R}", "Creature — Goblin Shaman", 2, ["R"], 1, 1,
              "When this creature enters the battlefield, Ember Herald deals 1 damage to each opponent."),
        _card("Cinder Martyr", "{1}{R}", "Creature — Goblin", 2, ["R"], 2, 1,
              "When this creature dies, Cinder Martyr deals 1 damage to each opponent."),
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
        _card("Grove Chronicler", "{2}{G}", "Creature — Elf Druid", 3, ["G"], 2, 2,
              "When this creature enters the battlefield, draw a card."),
        _card("Verdant Reclaimer", "{1}{G}", "Creature — Elf", 2, ["G"], 1, 1,
              "When this creature dies, you gain 2 life."),
        _card("Wild Growth Ritual", "{G}", "Sorcery", 1, ["G"], text="Search your library for a basic land card and put it onto the battlefield tapped."),
        _card("Predator's Bite", "{1}{G}", "Instant", 2, ["G"], text="Target creature gets +3/+3 until end of turn."),
        _card("Nature's Meal", "{2}{G}", "Sorcery", 3, ["G"], text="You gain 5 life."),
    ]
    mountain = _card("Mountain", "", "Basic Land — Mountain", 0, ["R"], text="{T}: Add {R}.")
    forest = _card("Forest", "", "Basic Land — Forest", 0, ["G"], text="{T}: Add {G}.")

    def deck_json(spells, land):
        entries = [{"card": c, "count": 4} for c in spells]      # 11 * 4 = 44
        entries.append({"card": land, "count": 16})               # + 16 = 60
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
    import ast as _ast
    import importlib
    import inspect
    import os
    import pkgutil

    import Playersim

    # 1. deck_stats_tracker must expose GamePosition, not a colliding GameState enum.
    from Playersim import deck_stats_tracker as dst
    assert hasattr(dst, "GamePosition"), "GamePosition enum missing"
    assert not hasattr(dst, "GameState"), "colliding GameState enum re-appeared"

    # 2. game_state must not import TargetingSystem from ability_handler.
    from Playersim import game_state as gs_mod
    assert "from .ability_handler import TargetingSystem" not in inspect.getsource(gs_mod)

    # 3. UNIVERSAL: no module-level function or class method may be defined twice
    # anywhere in the package. Python keeps only the last definition, silently
    # discarding the rest — this bug class was found 24 times during cleanup.
    pkg_dir = Playersim.__path__[0]
    dups = []
    for fname in sorted(os.listdir(pkg_dir)):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(pkg_dir, fname), encoding="utf-8") as f:
            tree = _ast.parse(f.read())
        scopes = [("module", tree)] + [(n.name, n) for n in _ast.walk(tree)
                                       if isinstance(n, _ast.ClassDef)]
        for scope_name, scope in scopes:
            names = [n.name for n in scope.body
                     if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]
            for name in set(n for n in names if names.count(n) > 1):
                dups.append(f"{fname}::{scope_name}::{name}")
    assert not dups, f"duplicate definitions (second silently wins): {dups}"

    # Cross-module duplicates within a split family (the same method defined in
    # two mixins of one class) are invisible to the per-file check above.
    def family(prefix):
        return [importlib.import_module(f"Playersim.{i.name}")
                for i in pkgutil.iter_modules(Playersim.__path__)
                if i.name.startswith(prefix)]
    for prefix, probe in (("game_state", "find_card_location"),
                          ("actions", "_handle_no_op"),
                          ("strategic_planner", "monte_carlo_search")):
        n = sum(inspect.getsource(m).count(f"def {probe}(") for m in family(prefix))
        assert n == 1, f"{probe} defined {n} times across {prefix} modules"

    # 4. remove_effect must clean the event-type index, not just the list,
    #    or removed effects keep firing through effect_index.
    from Playersim import replacement_effects
    assert "effect_index" in inspect.getsource(
        replacement_effects.ReplacementEffectSystem.remove_effect)

    # 5. combat.py holds CombatResolver; the old name must survive as an alias.
    from Playersim import combat
    assert combat.EnhancedCombatResolver is combat.CombatResolver, "compat alias broken"
    from Playersim.enhanced_combat import ExtendedCombatResolver
    assert issubclass(ExtendedCombatResolver, combat.CombatResolver)

    # 6. The god-class splits: every class must compose its mixins, and GameState
    #    must stay dict-less (__slots__ semantics).
    from Playersim.game_state import GameState
    gs_mro = {c.__name__ for c in GameState.__mro__}
    for expected in ("GameStateSetupMixin", "GameStateTurnMixin", "GameStateZonesMixin",
                     "GameStateStackMixin", "GameStatePermanentsMixin", "GameStateDamageMixin"):
        assert expected in gs_mro, f"{expected} missing from GameState MRO"
    assert "__dict__" not in getattr(GameState, "__slots__", ["__dict__"]), \
        "GameState instances must stay dict-less (__slots__ semantics)"

    from Playersim.actions import ActionHandler, ACTION_MEANINGS
    ah_mro = {c.__name__ for c in ActionHandler.__mro__}
    for expected in ("ActionSpaceMixin", "TurnPhaseHandlersMixin", "CastingHandlersMixin",
                     "CombatHandlersMixin", "ChoiceHandlersMixin", "MechanicsHandlersMixin"):
        assert expected in ah_mro, f"{expected} missing from ActionHandler MRO"
    assert len(ACTION_MEANINGS) == 480, "ACTION_MEANINGS must stay at 480 entries"

    from Playersim.strategic_planner import MTGStrategicPlanner, MCTSNode  # noqa: F401
    sp_mro = {c.__name__ for c in MTGStrategicPlanner.__mro__}
    for expected in ("ArchetypeAnalysisMixin", "CardEvaluationMixin",
                     "ThreatSynergyMixin", "SearchDecisionMixin"):
        assert expected in sp_mro, f"{expected} missing from MTGStrategicPlanner MRO"


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
    assert len(card_db) == 24, f"expected 24 unique cards, got {len(card_db)}"  # 11 spells x 2 decks + 2 basic lands
    return decks, card_db


@stage("construct environment")
def build_env(decks, card_db):
    from Playersim.environment import AlphaZeroMTGEnv
    from Playersim.game_state import GameState
    env = AlphaZeroMTGEnv(decks, card_db, **test_artifact_paths("runtime"))
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


@stage("agent-layer ownership invariants")
def check_agent_ownership(env):
    """The environment is the single owner of the agent layer; clones are
    self-contained so MCTS simulations keep working."""
    env.reset(seed=SEED)
    gs = env.game_state
    assert gs.action_handler is env.action_handler, "env and gs must share ONE ActionHandler"
    assert gs.strategic_planner is env.strategic_planner, "env and gs must share ONE planner"
    assert gs.card_evaluator is env.card_evaluator, "env and gs must share ONE evaluator"
    assert gs.action_handler.card_evaluator is gs.card_evaluator
    assert gs.strategic_planner.card_evaluator is gs.card_evaluator

    clone = gs.clone()
    assert clone.action_handler is not None and clone.action_handler is not gs.action_handler
    assert clone.strategic_planner is not None and clone.strategic_planner is not gs.strategic_planner
    assert clone.action_handler.game_state is clone, "clone's handler must bind to the clone"


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


@stage("stats pipeline records and persists games")
def check_stats_pipeline(decks, card_db):
    """The mission-critical path: finished games must land in test deck_stats
    and card_memory on disk, with fidelity telemetry exposed in the final info."""
    from Playersim.environment import AlphaZeroMTGEnv
    paths = test_artifact_paths("stats_pipeline")
    env = None
    try:
        env = AlphaZeroMTGEnv(decks, card_db, **paths)
        env.set_agent_version("smoke-test")
        rng = random.Random(SEED)
        recorded_any = False
        last_info = {}
        for ep in range(3):
            env.reset(seed=SEED + ep)
            done = trunc = False
            steps = 0
            while not (done or trunc) and steps < MAX_STEPS_PER_EPISODE:
                mask = env.action_mask()
                valid = np.where(mask)[0]
                _, _, done, trunc, last_info = env.step(int(rng.choice(list(valid))))
                steps += 1
            recorded_any = recorded_any or getattr(env, "_game_result_recorded", False)
        env.close()
        env = None
        assert recorded_any, "no game result recorded across 3 episodes"
        assert "fidelity" in last_info, "fidelity telemetry missing from final info"
        for path_name in ("deck_stats_path", "card_memory_path"):
            p = paths[path_name]
            assert os.path.isdir(p) and os.listdir(p), f"{path_name} not persisted to disk"

        # The metadata contract for the downstream deck-builder.
        log_path = os.path.join(paths["deck_stats_path"], "game_log.jsonl")
        assert os.path.exists(log_path), "game_log.jsonl missing"
        records = [json.loads(l) for l in open(log_path, encoding="utf-8")]
        assert records, "game log is empty"
        for r in records:
            for field in ("schema_version", "ts", "result", "turn_count",
                          "p1_deck", "p2_deck", "agent_is_p1",
                          "agent_version", "fidelity"):
                assert field in r, f"game log record missing '{field}'"
            assert r["schema_version"] == 1, "unexpected game log schema version"
            assert r["agent_version"] == "smoke-test", "agent version not stamped"
        rep_path = os.path.join(paths["deck_stats_path"], "fidelity_report.json")
        assert os.path.exists(rep_path), "fidelity_report.json missing"
        rep = json.load(open(rep_path, encoding="utf-8"))
        assert rep.get("games_recorded") == len(records), \
            "fidelity report count does not match game log (double-append?)"

        # Downstream metadata contract: per-game log + cumulative fidelity report.
        log_path = os.path.join(paths["deck_stats_path"], "game_log.jsonl")
        assert os.path.isfile(log_path), "game_log.jsonl missing"
        with open(log_path, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert entries, "game_log.jsonl is empty"
        for key in ("result", "turn_count", "agent_version", "fidelity", "p1_deck", "p2_deck"):
            assert key in entries[0], f"game_log entry missing '{key}'"
        rep_path = os.path.join(paths["deck_stats_path"], "fidelity_report.json")
        assert os.path.isfile(rep_path), "fidelity_report.json missing"
        with open(rep_path, encoding="utf-8") as f:
            report = json.load(f)
        assert report.get("games_recorded", 0) >= 1
        assert "unparsed_cards" in report
    finally:
        if env is not None:
            env.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Playersim smoke test")
    print("=" * 50)
    reset_test_artifacts()
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
        try:
            if check_reset(env) is not None:
                check_agent_ownership(env)
                play_episodes(env)
        finally:
            env.close()
        check_stats_pipeline(decks, card_db)
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
