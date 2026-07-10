"""Deterministic property/invariant fuzzing for the Playersim engine.

Run from the repository root::

    python tests/invariant_fuzz_test.py --profile short
    python tests/invariant_fuzz_test.py --profile default
    python tests/invariant_fuzz_test.py --profile long
    python tests/invariant_fuzz_test.py --seeds 17 29 43 --steps 150
    python tests/invariant_fuzz_test.py --replay fuzz_failures/invariant_fuzz_seed_17.json

The harness deliberately uses the small synthetic decks from ``smoke_test``.
That keeps it fast and makes exact physical-card conservation supportable:
the fixtures do not conjure cards, mutate permanents, or put cards in custom
holding zones.  Actions are selected randomly, but only from the engine's
current action mask and from a per-seed ``random.Random`` instance.
Concede is skipped whenever another legal action exists so each seed exercises
one sustained game instead of repeatedly resetting short episodes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import random
import sys
import tempfile
from typing import Any, Iterable, Sequence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (REPO_ROOT, TESTS_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import numpy as np  # noqa: E402

from smoke_test import build_fixture_decks  # noqa: E402


ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_ARTIFACT_DIR = "fuzz_failures"
SEED_BUDGET = (
    1701, 2903, 4307,
    *(10007 + offset * 7919 for offset in range(29)),
)
CONCEDE_ACTION = 12
PHYSICAL_ZONES = ("library", "hand", "battlefield", "graveyard", "exile")


@dataclass(frozen=True)
class FuzzProfile:
    """A checked-in deterministic fuzz budget."""

    seeds: tuple[int, ...]
    steps: int
    check_every: int


FUZZ_PROFILES = {
    # Fast enough for focused local verification and unit-test workflows.
    "short": FuzzProfile(SEED_BUDGET[:3], steps=100, check_every=20),
    # A useful local pre-commit run: more seeds and sustained games.
    "default": FuzzProfile(SEED_BUDGET[:8], steps=1_000, check_every=100),
    # The weekly/manual CI soak: 320,000 deterministic mask-valid actions.
    "long": FuzzProfile(SEED_BUDGET, steps=10_000, check_every=250),
}
DEFAULT_PROFILE = "default"


@dataclass
class SeedTrace:
    """Minimal state needed to replay the exact action path to a failure."""

    seed: int
    actions: list[int] = field(default_factory=list)
    contexts: list[dict[str, Any]] = field(default_factory=list)
    executed: int = 0
    episode: int = 0
    episode_seed: int | None = None
    stage: str = "initializing"
    where: str = "initializing"

    def checkpoint(self, stage: str, where: str) -> None:
        self.stage = stage
        self.where = where


def _freeze(value: Any) -> Any:
    """Convert small engine values into stable, equality-friendly values."""
    if isinstance(value, np.ndarray):
        return tuple(_freeze(item) for item in value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return tuple(sorted(
            ((_freeze(key), _freeze(item)) for key, item in value.items()),
            key=lambda pair: repr(pair[0]),
        ))
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _player_label(game_state, player) -> str:
    if player is game_state.p1 or player == game_state.p1:
        return "p1"
    if player is game_state.p2 or player == game_state.p2:
        return "p2"
    return "unknown"


def _artifact_value(game_state, value: Any) -> Any:
    """Convert generated action context into stable, JSON-safe diagnostics."""
    if value is game_state.p1:
        return {"player": "p1"}
    if value is game_state.p2:
        return {"player": "p2"}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return [_artifact_value(game_state, item) for item in value.tolist()]
    if isinstance(value, dict):
        return {
            str(key): _artifact_value(game_state, item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_artifact_value(game_state, item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_artifact_value(game_state, item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    if callable(value):
        return {"callable": getattr(value, "__qualname__", type(value).__name__)}
    if hasattr(value, "card_id"):
        return {
            "object": type(value).__name__,
            "card_id": _artifact_value(game_state, getattr(value, "card_id", None)),
            "name": getattr(value, "name", None),
        }
    return {"object": type(value).__name__}


def _generated_action_context(env, action: int) -> dict[str, Any]:
    entry = getattr(
        env.action_handler, "action_reasons_with_context", {}).get(action, {})
    context = entry.get("context", {}) if isinstance(entry, dict) else {}
    converted = _artifact_value(env.game_state, context or {})
    return converted if isinstance(converted, dict) else {"value": converted}


def _state_summary(game_state) -> dict[str, Any] | None:
    """Capture deterministic failure state without serializing engine objects."""
    if game_state is None:
        return None

    def player_summary(player) -> dict[str, Any]:
        return {
            "life": player.get("life"),
            "lost_game": bool(player.get("lost_game", False)),
            "game_draw": bool(player.get("game_draw", False)),
            "zones": {
                zone: _artifact_value(game_state, player.get(zone, []))
                for zone in PHYSICAL_ZONES
            },
            "mana_pool": _artifact_value(game_state, player.get("mana_pool", {})),
            "tapped_permanents": _artifact_value(
                game_state, player.get("tapped_permanents", set())),
            "land_played": bool(player.get("land_played", False)),
        }

    stack = []
    for item in game_state.stack:
        if not isinstance(item, tuple):
            stack.append({"object": type(item).__name__})
            continue
        context = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
        stack.append({
            "kind": item[0] if item else None,
            "card_id": item[1] if len(item) > 1 else None,
            "controller": _player_label(
                game_state, item[2] if len(item) > 2 else None),
            "context": _artifact_value(game_state, context),
        })

    return {
        "turn": getattr(game_state, "turn", None),
        "phase": getattr(game_state, "phase", None),
        "phase_name": getattr(game_state, "_PHASE_NAMES", {}).get(
            getattr(game_state, "phase", None)),
        "priority_player": _player_label(
            game_state, getattr(game_state, "priority_player", None)),
        "priority_pass_count": getattr(game_state, "priority_pass_count", None),
        "agent_is_p1": bool(getattr(game_state, "agent_is_p1", True)),
        "p1": player_summary(game_state.p1),
        "p2": player_summary(game_state.p2),
        "stack": stack,
        "targeting_context": _artifact_value(
            game_state, getattr(game_state, "targeting_context", None)),
        "sacrifice_context": _artifact_value(
            game_state, getattr(game_state, "sacrifice_context", None)),
        "choice_context": _artifact_value(
            game_state, getattr(game_state, "choice_context", None)),
        "pending_spell_context": _artifact_value(
            game_state, getattr(game_state, "pending_spell_context", None)),
        "current_attackers": _artifact_value(
            game_state, getattr(game_state, "current_attackers", [])),
        "current_block_assignments": _artifact_value(
            game_state, getattr(game_state, "current_block_assignments", {})),
        "planeswalker_attack_targets": _artifact_value(
            game_state, getattr(game_state, "planeswalker_attack_targets", {})),
        "battle_attack_targets": _artifact_value(
            game_state, getattr(game_state, "battle_attack_targets", {})),
    }


def _stack_signature(game_state) -> tuple:
    result = []
    for item in game_state.stack:
        if not isinstance(item, tuple):
            result.append((type(item).__name__, repr(item)))
            continue
        kind = item[0] if item else None
        card_id = item[1] if len(item) > 1 else None
        controller = _player_label(game_state, item[2]) if len(item) > 2 else None
        context = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
        # Only stable rule-relevant scalar context belongs in the signature.
        context_signature = tuple(sorted(
            (key, _freeze(value))
            for key, value in context.items()
            if not callable(value) and key not in {"controller", "player", "ability"}
        ))
        result.append((kind, card_id, controller, context_signature))
    return tuple(result)


def _physical_counts(game_state, tracked_ids: set[int]) -> Counter:
    """Count fixture-deck cards in real zones and non-copy spell stack items."""
    counts: Counter = Counter()
    for player in (game_state.p1, game_state.p2):
        for zone in PHYSICAL_ZONES:
            for card_id in player.get(zone, []):
                if card_id in tracked_ids:
                    card = game_state._safe_get_card(card_id)
                    if card is not None and not getattr(card, "is_token", False):
                        counts[card_id] += 1

    for item in game_state.stack:
        if not (isinstance(item, tuple) and len(item) >= 2 and item[0] == "SPELL"):
            continue
        card_id = item[1]
        context = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
        if context.get("is_copy", False) or card_id not in tracked_ids:
            continue
        card = game_state._safe_get_card(card_id)
        if card is not None and not getattr(card, "is_token", False):
            counts[card_id] += 1
    return counts


def _assert_card_conservation(game_state, expected: Counter, *, where: str) -> None:
    actual = _physical_counts(game_state, set(expected))
    if actual == expected:
        return
    missing = expected - actual
    excess = actual - expected
    raise AssertionError(
        f"{where}: physical non-token card multiset changed; "
        f"missing={dict(missing)}, excess={dict(excess)}, "
        f"expected_total={sum(expected.values())}, actual_total={sum(actual.values())}"
    )


def _card_counter_signature(game_state) -> tuple:
    ids = set()
    for player in (game_state.p1, game_state.p2):
        ids.update(player.get("battlefield", []))
    return tuple(sorted(
        ((card_id, _freeze(getattr(game_state._safe_get_card(card_id), "counters", {})))
         for card_id in ids if game_state._safe_get_card(card_id) is not None),
        key=lambda pair: (type(pair[0]).__name__, repr(pair[0])),
    ))


def _sba_signature(game_state) -> tuple:
    players = []
    for player in (game_state.p1, game_state.p2):
        players.append(tuple(
            (key, _freeze(player.get(key)))
            for key in (
                *PHYSICAL_ZONES,
                "life", "lost_game", "won_game", "game_draw",
                "attempted_draw_from_empty", "poison_counters",
                "damage_counters", "deathtouch_damage", "loyalty_counters",
                "attachments", "phased_out_permanents",
            )
        ))
    return (
        tuple(players),
        _stack_signature(game_state),
        _card_counter_signature(game_state),
        _freeze(getattr(game_state, "phased_out", set())),
        _freeze(getattr(game_state, "battle_cards", {})),
    )


def _check_sba_fixed_point(game_state, *, where: str) -> None:
    probe = game_state.clone()
    if probe is None:
        raise AssertionError(f"{where}: GameState.clone() returned None")
    probe.check_state_based_actions()
    stable = _sba_signature(probe)
    second_changed = probe.check_state_based_actions()
    repeated = _sba_signature(probe)
    assert second_changed is False, (
        f"{where}: second SBA pass reported another change ({second_changed!r})")
    assert repeated == stable, f"{where}: second SBA pass changed stable state"


LAYER_CARD_FIELDS = (
    "name", "mana_cost", "colors", "card_types", "subtypes", "supertypes",
    "oracle_text", "keywords", "power", "toughness", "loyalty", "defense",
    "cmc", "type_line", "protection",
)


def _layer_signature(game_state) -> tuple:
    result = []
    for player_num, player in enumerate((game_state.p1, game_state.p2), start=1):
        for battlefield_index, card_id in enumerate(player.get("battlefield", [])):
            card = game_state._safe_get_card(card_id)
            if card is None:
                result.append((player_num, battlefield_index, card_id, None))
                continue
            fields = tuple(
                (field, _freeze(getattr(card, field, None)))
                for field in LAYER_CARD_FIELDS
            )
            result.append((player_num, battlefield_index, card_id, fields))
    return tuple(result)


def _check_layer_idempotence(game_state, *, where: str) -> None:
    probe = game_state.clone()
    if probe is None:
        raise AssertionError(f"{where}: GameState.clone() returned None")
    if probe.layer_system is None:
        raise AssertionError(f"{where}: clone has no layer system")
    probe.layer_system.invalidate_cache()
    probe.layer_system.apply_all_effects()
    first = _layer_signature(probe)
    probe.layer_system.invalidate_cache()
    probe.layer_system.apply_all_effects()
    second = _layer_signature(probe)
    assert second == first, f"{where}: repeated layer application changed characteristics"


def _assert_finite_observation(env, observation: dict, *, where: str) -> None:
    assert isinstance(observation, dict), f"{where}: observation is not a dict"
    observation_error = getattr(env, "last_observation_error", None)
    assert observation_error is None, (
        f"{where}: observation generation degraded: {observation_error}")
    for key, space in env.observation_space.spaces.items():
        assert key in observation, f"{where}: observation missing {key!r}"
        value = np.asarray(observation[key])
        assert value.shape == space.shape, (
            f"{where}: observation[{key!r}] shape {value.shape} != {space.shape}")
        assert np.all(np.isfinite(value)), (
            f"{where}: observation[{key!r}] contains a non-finite value")
        assert space.contains(observation[key]), (
            f"{where}: observation[{key!r}] is outside its declared space; "
            f"dtype={value.dtype}, shape={value.shape}")


def _valid_mask(env, *, where: str) -> np.ndarray:
    mask = np.asarray(env.action_mask())
    mask_error = getattr(env.action_handler, "last_mask_error", None)
    assert mask_error is None, f"{where}: action-mask generation degraded: {mask_error}"
    assert mask.shape == (env.ACTION_SPACE_SIZE,), (
        f"{where}: action mask shape {mask.shape} != {(env.ACTION_SPACE_SIZE,)}")
    assert np.all(np.isin(mask, (False, True))), f"{where}: action mask is not boolean"
    mask = mask.astype(bool, copy=False)
    assert mask.any(), f"{where}: action mask has no valid action"
    unhandled = [
        int(action) for action in np.flatnonzero(mask)
        if env.action_handler.get_action_info(int(action))[0]
        not in env.action_handler.action_handlers
    ]
    assert not unhandled, (
        f"{where}: action mask exposed indices without handlers: {unhandled}")
    return mask


def _empty_mana(player) -> bool:
    return (
        all(amount == 0 for amount in player.get("mana_pool", {}).values())
        and not any(
            amount
            for pool in player.get("conditional_mana", {}).values()
            for amount in pool.values()
        )
        and not any(player.get("phase_restricted_mana", {}).values())
    )


def _check_mana_phase_boundary(game_state) -> None:
    """Exercise a real, controlled main-to-combat phase transition (CR 500.4)."""
    probe = game_state.clone()
    if probe is None:
        raise AssertionError("mana boundary: GameState.clone() returned None")

    probe.phase = probe.PHASE_MAIN_PRECOMBAT
    probe.previous_priority_phase = None
    probe.stack = []
    probe.targeting_context = None
    probe.sacrifice_context = None
    probe.choice_context = None
    probe.mulligan_in_progress = False
    probe.bottoming_in_progress = False
    probe.mulligan_player = None
    probe.bottoming_player = None
    probe.priority_player = probe._get_active_player()
    probe.priority_pass_count = 0

    for player, color in ((probe.p1, "R"), (probe.p2, "G")):
        player["mana_pool"] = {symbol: 0 for symbol in "WUBRGC"}
        player["mana_pool"][color] = 2
        player["conditional_mana"] = {"cast_creatures": {color: 1}}
        player["phase_restricted_mana"] = {color: 1}

    old_phase = probe.phase
    probe._advance_phase()
    assert probe.phase != old_phase, "mana boundary: controlled phase did not advance"
    for label, player in (("p1", probe.p1), ("p2", probe.p2)):
        assert _empty_mana(player), (
            f"mana boundary: {label} retained mana after phase advance: "
            f"pool={player.get('mana_pool')}, conditional={player.get('conditional_mana')}, "
            f"phase_restricted={player.get('phase_restricted_mana')}")


def resolve_profile(
    profile_name: str,
    *,
    seeds: Iterable[int] | None = None,
    steps: int | None = None,
    check_every: int | None = None,
) -> FuzzProfile:
    """Resolve a named budget plus optional CLI-style overrides."""
    if profile_name not in FUZZ_PROFILES:
        raise ValueError(
            f"unknown fuzz profile {profile_name!r}; "
            f"choose from {', '.join(sorted(FUZZ_PROFILES))}")
    base = FUZZ_PROFILES[profile_name]
    resolved_seeds = tuple(base.seeds if seeds is None else seeds)
    resolved_steps = base.steps if steps is None else steps
    resolved_check_every = base.check_every if check_every is None else check_every
    if not resolved_seeds:
        raise ValueError("at least one seed is required")
    if any(isinstance(seed, bool) or not isinstance(seed, int) for seed in resolved_seeds):
        raise ValueError("seeds must be integers")
    if len(set(resolved_seeds)) != len(resolved_seeds):
        raise ValueError("seeds must be unique")
    if isinstance(resolved_steps, bool) or not isinstance(resolved_steps, int):
        raise ValueError("steps must be an integer")
    if resolved_steps < 1:
        raise ValueError("steps must be at least 1")
    if isinstance(resolved_check_every, bool) or not isinstance(resolved_check_every, int):
        raise ValueError("check_every must be an integer")
    if resolved_check_every < 1:
        raise ValueError("check_every must be at least 1")
    return FuzzProfile(resolved_seeds, resolved_steps, resolved_check_every)


def _failure_replay_steps(trace: SeedTrace) -> int:
    # A failure before env.step needs one more loop iteration than the number of
    # completed actions.  Post-step assertions reproduce while executing the
    # final recorded action itself.
    pre_action_stages = {"initializing", "reset", "reset_invariants", "action_mask"}
    if trace.stage in pre_action_stages:
        return max(1, trace.executed + 1)
    return max(1, len(trace.actions))


def build_failure_artifact(
    trace: SeedTrace,
    exc: Exception,
    *,
    profile_name: str,
    requested_steps: int,
    check_every: int,
    game_state=None,
) -> dict[str, Any]:
    """Build the stable, intentionally small failure/replay document."""
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "kind": "playersim_invariant_fuzz_failure",
        "run": {
            "profile": profile_name,
            "requested_steps": requested_steps,
            "check_every": check_every,
        },
        "replay": {
            "seed": trace.seed,
            "steps": _failure_replay_steps(trace),
            "check_every": check_every,
            "actions": list(trace.actions),
            "contexts": list(trace.contexts),
        },
        "failure": {
            "type": type(exc).__name__,
            "message": str(exc),
            "stage": trace.stage,
            "where": trace.where,
            "action_index": trace.executed,
            "episode": trace.episode,
            "episode_seed": trace.episode_seed,
            "state": _state_summary(game_state),
        },
    }


def write_failure_artifact(directory: str | os.PathLike[str], payload: dict[str, Any]) -> Path:
    """Atomically write one failure file; callers invoke this only on failure."""
    replay = payload.get("replay", {})
    seed = replay.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("failure artifact replay.seed must be an integer")
    output_dir = Path(directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"invariant_fuzz_seed_{seed}.json"
    temporary_path = output_path.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary_path, output_path)
    return output_path


def load_replay_artifact(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and validate every replay-critical field before using it."""
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("replay artifact must contain a JSON object")
    if payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported replay artifact schema_version "
            f"{payload.get('schema_version')!r}")
    if payload.get("kind") != "playersim_invariant_fuzz_failure":
        raise ValueError("JSON file is not an invariant fuzz failure artifact")
    replay = payload.get("replay")
    if not isinstance(replay, dict):
        raise ValueError("replay artifact is missing replay settings")
    seed = replay.get("seed")
    steps = replay.get("steps")
    check_every = replay.get("check_every")
    actions = replay.get("actions")
    contexts = replay.get("contexts", [])
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("replay.seed must be an integer")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise ValueError("replay.steps must be a positive integer")
    if (
        isinstance(check_every, bool)
        or not isinstance(check_every, int)
        or check_every < 1
    ):
        raise ValueError("replay.check_every must be a positive integer")
    if not isinstance(actions, list) or any(
        isinstance(action, bool) or not isinstance(action, int) or action < 0
        for action in actions
    ):
        raise ValueError("replay.actions must be a list of non-negative integers")
    if len(actions) > steps:
        raise ValueError("replay.actions cannot contain more entries than replay.steps")
    if not isinstance(contexts, list) or any(
            not isinstance(context, dict) for context in contexts):
        raise ValueError("replay.contexts must be a list of objects")
    if contexts and len(contexts) != len(actions):
        raise ValueError("replay.contexts must align one-for-one with replay.actions")
    return payload


def _run_seed(
    env,
    seed: int,
    steps: int,
    check_every: int,
    *,
    replay_actions: Sequence[int] = (),
    replay_contexts: Sequence[dict[str, Any]] = (),
    trace: SeedTrace | None = None,
) -> tuple[int, int]:
    if len(replay_actions) > steps:
        raise ValueError("replay action history is longer than the requested run")
    if replay_contexts and len(replay_contexts) != len(replay_actions):
        raise ValueError("replay action/context histories have different lengths")
    trace = trace or SeedTrace(seed)
    rng = random.Random(seed ^ 0x5EED5EED)
    executed = 0
    episode = 0
    while executed < steps:
        episode_seed = seed + episode * 100_003
        trace.episode = episode
        trace.episode_seed = episode_seed
        trace.checkpoint("reset", f"seed {seed} episode {episode} reset ({episode_seed})")
        observation, _ = env.reset(seed=episode_seed)
        reset_where = f"seed {seed} episode {episode} reset ({episode_seed})"
        trace.checkpoint("reset_invariants", reset_where)
        _assert_finite_observation(env, observation, where=reset_where)
        expected_cards = _physical_counts(
            env.game_state,
            {card_id for card_id, card in env.card_db.items()
             if not getattr(card, "is_token", False)},
        )
        assert sum(expected_cards.values()) == 120, (
            f"{reset_where}: fixture exposed {sum(expected_cards.values())} cards, expected 120")

        _assert_card_conservation(env.game_state, expected_cards, where=reset_where)
        _check_sba_fixed_point(env.game_state, where=reset_where)
        _check_layer_idempotence(env.game_state, where=reset_where)

        terminated = truncated = False
        while executed < steps and not (terminated or truncated):
            where = f"seed {seed} episode {episode} action {executed}"
            trace.checkpoint("action_mask", where)
            check_mask_purity = executed % check_every == 0
            state_before_mask = (
                _state_summary(env.game_state) if check_mask_purity else None)
            mask = _valid_mask(env, where=where)
            if check_mask_purity:
                state_after_mask = _state_summary(env.game_state)
                repeated_mask = _valid_mask(env, where=f"{where} repeated mask")
                assert np.array_equal(repeated_mask, mask), (
                    f"{where}: repeated action-mask generation changed the mask")
                assert state_after_mask == state_before_mask == _state_summary(
                    env.game_state), (
                    f"{where}: action-mask generation mutated game state")
            valid_actions = np.flatnonzero(mask).tolist()
            non_concede_actions = [
                action for action in valid_actions if action != CONCEDE_ACTION
            ]
            generated_action = int(rng.choice(non_concede_actions or valid_actions))
            if executed < len(replay_actions):
                action = int(replay_actions[executed])
                assert action < len(mask) and mask[action], (
                    f"{where}: replay divergence: recorded action {action} is not mask-valid; "
                    f"deterministic generator now selects {generated_action}")
            else:
                action = generated_action
            generated_context = _generated_action_context(env, action)
            if executed < len(replay_contexts):
                assert generated_context == replay_contexts[executed], (
                    f"{where}: replay action context diverged; "
                    f"recorded={replay_contexts[executed]!r}, "
                    f"generated={generated_context!r}")
            trace.actions.append(action)
            trace.contexts.append(generated_context)
            trace.checkpoint("step", where)
            observation, reward, terminated, truncated, info = env.step(action)
            executed += 1
            trace.executed = executed
            trace.checkpoint("post_step", where)

            assert math.isfinite(float(reward)), f"{where}: reward is not finite: {reward!r}"
            _assert_finite_observation(env, observation, where=where)
            assert not info.get("invalid_action", False), (
                f"{where}: mask-valid action {action} was rejected: "
                f"{info.get('invalid_action_reason')}")
            assert not info.get("execution_failed", False), (
                f"{where}: mask-valid action {action} failed during execution: "
                f"{info.get('error_message') or info.get('invalid_action_reason')}")
            assert not info.get("critical_error", False), (
                f"{where}: engine reported a critical error: {info.get('error_message')}")
            _assert_card_conservation(env.game_state, expected_cards, where=where)

            if terminated or truncated:
                continue

            info_mask = np.asarray(info.get("action_mask"))
            assert info_mask.shape == (env.ACTION_SPACE_SIZE,), (
                f"{where}: info action mask has shape {info_mask.shape}")
            assert info_mask.astype(bool).any(), f"{where}: next info action mask is empty"
            assert np.array_equal(
                np.asarray(observation["action_mask"], dtype=bool),
                info_mask.astype(bool)), (
                f"{where}: observation and info expose different next-action masks")

            if executed % check_every == 0:
                trace.checkpoint("periodic_invariants", where)
                _check_sba_fixed_point(env.game_state, where=where)
                _check_layer_idempotence(env.game_state, where=where)
        episode += 1

    return executed, episode


def run(
    seeds: Iterable[int],
    steps: int,
    check_every: int,
    *,
    profile_name: str = "custom",
    artifact_dir: str | os.PathLike[str] | None = DEFAULT_ARTIFACT_DIR,
    replay_actions_by_seed: dict[int, Sequence[int]] | None = None,
    replay_contexts_by_seed: dict[int, Sequence[dict[str, Any]]] | None = None,
) -> list[str]:
    from Playersim.card import load_decks_and_card_db
    from Playersim.environment import AlphaZeroMTGEnv

    failures: list[str] = []
    seed_list = list(seeds)
    if not seed_list:
        raise ValueError("at least one seed is required")
    if steps < 1:
        raise ValueError("steps must be at least 1")
    if check_every < 1:
        raise ValueError("check_every must be at least 1")
    replay_actions_by_seed = replay_actions_by_seed or {}
    replay_contexts_by_seed = replay_contexts_by_seed or {}
    with tempfile.TemporaryDirectory(prefix="playersim_invariant_fuzz_") as temp_dir:
        fixture_dir = os.path.join(temp_dir, "decks")
        os.makedirs(fixture_dir)
        build_fixture_decks(fixture_dir)
        for seed in seed_list:
            env = None
            trace = SeedTrace(seed)
            try:
                # A fresh engine and fresh persistence paths make each seed
                # independently replayable, regardless of run ordering.
                decks, card_db = load_decks_and_card_db(fixture_dir)
                seed_root = os.path.join(temp_dir, f"seed_{seed}")
                env = AlphaZeroMTGEnv(
                    decks,
                    card_db,
                    deck_stats_path=os.path.join(seed_root, "deck_stats"),
                    card_memory_path=os.path.join(seed_root, "card_memory"),
                )
                replay_actions = replay_actions_by_seed.get(seed, ())
                replay_contexts = replay_contexts_by_seed.get(seed, ())
                executed, episodes = _run_seed(
                    env,
                    seed,
                    steps,
                    check_every,
                    replay_actions=replay_actions,
                    replay_contexts=replay_contexts,
                    trace=trace,
                )
                print(
                    f"  PASS  seed {seed}: {executed} mask-valid actions "
                    f"across {episodes} episode(s)")
            except Exception as exc:
                message = f"seed {seed}: {type(exc).__name__}: {exc}"
                failures.append(message)
                print(f"  FAIL  {message}")
                if artifact_dir is not None:
                    try:
                        payload = build_failure_artifact(
                            trace,
                            exc,
                            profile_name=profile_name,
                            requested_steps=steps,
                            check_every=check_every,
                            game_state=env.game_state if env is not None else None,
                        )
                        artifact_path = write_failure_artifact(artifact_dir, payload)
                        print(f"        replay artifact: {artifact_path}")
                    except Exception as artifact_exc:
                        artifact_message = (
                            f"seed {seed}: could not write replay artifact: "
                            f"{type(artifact_exc).__name__}: {artifact_exc}")
                        failures.append(artifact_message)
                        print(f"  FAIL  {artifact_message}")
            finally:
                if env is not None:
                    env.close()

        env = None
        try:
            decks, card_db = load_decks_and_card_db(fixture_dir)
            mana_root = os.path.join(temp_dir, "mana_boundary")
            env = AlphaZeroMTGEnv(
                decks,
                card_db,
                deck_stats_path=os.path.join(mana_root, "deck_stats"),
                card_memory_path=os.path.join(mana_root, "card_memory"),
            )
            env.reset(seed=seed_list[0] + 99991)
            _check_mana_phase_boundary(env.game_state)
            print("  PASS  mana clears across a controlled phase boundary")
        except Exception as exc:
            message = f"mana phase boundary: {type(exc).__name__}: {exc}"
            failures.append(message)
            print(f"  FAIL  {message}")
        finally:
            if env is not None:
                env.close()
    return failures


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=sorted(FUZZ_PROFILES), default=None,
        help=f"checked-in fuzz budget (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--seeds", nargs="+", type=int, default=None,
        help="override the profile's deterministic episode seeds",
    )
    parser.add_argument(
        "--steps", type=int, default=None,
        help="override the profile's maximum actions per seed",
    )
    parser.add_argument(
        "--check-every", type=int, default=None,
        help="override the profile's SBA/layer fixed-point interval",
    )
    parser.add_argument(
        "--artifact-dir", default=None,
        help=(
            f"failure artifact directory (normal-run default: {DEFAULT_ARTIFACT_DIR}; "
            "replays do not emit a second artifact unless this is supplied)"),
    )
    parser.add_argument(
        "--replay", metavar="FAILURE.json",
        help="replay a recorded failing seed and exact action history",
    )
    args = parser.parse_args()
    if args.replay and any(
        value is not None
        for value in (args.profile, args.seeds, args.steps, args.check_every)
    ):
        parser.error(
            "--replay cannot be combined with --profile, --seeds, --steps, "
            "or --check-every")
    if not args.replay:
        profile_name = args.profile or DEFAULT_PROFILE
        try:
            resolve_profile(
                profile_name,
                seeds=args.seeds,
                steps=args.steps,
                check_every=args.check_every,
            )
        except ValueError as exc:
            parser.error(str(exc))
    return args


def main() -> int:
    args = _parse_args()
    logging.disable(logging.CRITICAL)
    if args.replay:
        try:
            payload = load_replay_artifact(args.replay)
        except (OSError, ValueError) as exc:
            print(f"ERROR: cannot load replay artifact: {exc}")
            return 2
        replay = payload["replay"]
        seed = replay["seed"]
        seeds = (seed,)
        steps = replay["steps"]
        check_every = replay["check_every"]
        profile_name = f"replay:{payload.get('run', {}).get('profile', 'unknown')}"
        artifact_dir = args.artifact_dir
        replay_actions_by_seed = {seed: tuple(replay["actions"])}
        replay_contexts_by_seed = {
            seed: tuple(replay.get("contexts", ())) }
        mode = f"replay={args.replay}"
    else:
        profile_name = args.profile or DEFAULT_PROFILE
        profile = resolve_profile(
            profile_name,
            seeds=args.seeds,
            steps=args.steps,
            check_every=args.check_every,
        )
        seeds = profile.seeds
        steps = profile.steps
        check_every = profile.check_every
        artifact_dir = args.artifact_dir or DEFAULT_ARTIFACT_DIR
        replay_actions_by_seed = None
        replay_contexts_by_seed = None
        mode = f"profile={profile_name}"
    print(
        "Playersim deterministic invariant fuzz "
        f"({mode}, seeds={list(seeds)}, steps={steps}, "
        f"check_every={check_every}, action_budget={len(seeds) * steps})"
    )
    failures = run(
        seeds,
        steps,
        check_every,
        profile_name=profile_name,
        artifact_dir=artifact_dir,
        replay_actions_by_seed=replay_actions_by_seed,
        replay_contexts_by_seed=replay_contexts_by_seed,
    )
    if failures:
        print(f"FAILED: {len(failures)} invariant run(s)")
        return 1
    print(f"PASS: {len(seeds)} seeds and the controlled phase-boundary check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
