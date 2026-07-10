"""Deterministic property/invariant fuzzing for the Playersim engine.

Run from the repository root::

    python tests/invariant_fuzz_test.py
    python tests/invariant_fuzz_test.py --seeds 17 29 43 --steps 150

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
import logging
import math
import os
import random
import sys
import tempfile
from typing import Any, Iterable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (REPO_ROOT, TESTS_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import numpy as np  # noqa: E402

from smoke_test import build_fixture_decks  # noqa: E402


DEFAULT_SEEDS = (1701, 2903, 4307)
DEFAULT_STEPS = 100
DEFAULT_CHECK_EVERY = 20
CONCEDE_ACTION = 12
PHYSICAL_ZONES = ("library", "hand", "battlefield", "graveyard", "exile")


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
    for key, space in env.observation_space.spaces.items():
        assert key in observation, f"{where}: observation missing {key!r}"
        value = np.asarray(observation[key])
        assert value.shape == space.shape, (
            f"{where}: observation[{key!r}] shape {value.shape} != {space.shape}")
        assert np.all(np.isfinite(value)), (
            f"{where}: observation[{key!r}] contains a non-finite value")


def _valid_mask(env, *, where: str) -> np.ndarray:
    mask = np.asarray(env.action_mask())
    assert mask.shape == (env.ACTION_SPACE_SIZE,), (
        f"{where}: action mask shape {mask.shape} != {(env.ACTION_SPACE_SIZE,)}")
    assert np.all(np.isin(mask, (False, True))), f"{where}: action mask is not boolean"
    mask = mask.astype(bool, copy=False)
    assert mask.any(), f"{where}: action mask has no valid action"
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


def _run_seed(env, seed: int, steps: int, check_every: int) -> tuple[int, int]:
    rng = random.Random(seed ^ 0x5EED5EED)
    executed = 0
    episode = 0
    while executed < steps:
        episode_seed = seed + episode * 100_003
        observation, _ = env.reset(seed=episode_seed)
        reset_where = f"seed {seed} episode {episode} reset ({episode_seed})"
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
            mask = _valid_mask(env, where=where)
            valid_actions = np.flatnonzero(mask).tolist()
            non_concede_actions = [
                action for action in valid_actions if action != CONCEDE_ACTION
            ]
            action = int(rng.choice(non_concede_actions or valid_actions))
            observation, reward, terminated, truncated, info = env.step(action)
            executed += 1

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

            if executed % check_every == 0:
                _check_sba_fixed_point(env.game_state, where=where)
                _check_layer_idempotence(env.game_state, where=where)
        episode += 1

    return executed, episode


def run(seeds: Iterable[int], steps: int, check_every: int) -> list[str]:
    from Playersim.card import load_decks_and_card_db
    from Playersim.environment import AlphaZeroMTGEnv

    failures: list[str] = []
    env = None
    with tempfile.TemporaryDirectory(prefix="playersim_invariant_fuzz_") as temp_dir:
        fixture_dir = os.path.join(temp_dir, "decks")
        os.makedirs(fixture_dir)
        build_fixture_decks(fixture_dir)
        decks, card_db = load_decks_and_card_db(fixture_dir)
        env = AlphaZeroMTGEnv(
            decks,
            card_db,
            deck_stats_path=os.path.join(temp_dir, "deck_stats"),
            card_memory_path=os.path.join(temp_dir, "card_memory"),
        )
        try:
            seed_list = list(seeds)
            for seed in seed_list:
                try:
                    executed, episodes = _run_seed(env, seed, steps, check_every)
                    print(
                        f"  PASS  seed {seed}: {executed} random valid actions "
                        f"across {episodes} episode(s)")
                except Exception as exc:
                    message = f"seed {seed}: {type(exc).__name__}: {exc}"
                    failures.append(message)
                    print(f"  FAIL  {message}")

            try:
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
        "--seeds", nargs="+", type=int, default=list(DEFAULT_SEEDS),
        help="deterministic episode seeds (default: %(default)s)",
    )
    parser.add_argument(
        "--steps", type=int, default=DEFAULT_STEPS,
        help="maximum random valid actions per seed (default: %(default)s)",
    )
    parser.add_argument(
        "--check-every", type=int, default=DEFAULT_CHECK_EVERY,
        help="SBA/layer fixed-point interval in actions (default: %(default)s)",
    )
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be at least 1")
    if args.check_every < 1:
        parser.error("--check-every must be at least 1")
    if not args.seeds:
        parser.error("at least one seed is required")
    return args


def main() -> int:
    args = _parse_args()
    logging.disable(logging.CRITICAL)
    print(
        "Playersim deterministic invariant fuzz "
        f"(seeds={args.seeds}, steps={args.steps}, check_every={args.check_every})"
    )
    failures = run(args.seeds, args.steps, args.check_every)
    if failures:
        print(f"FAILED: {len(failures)} invariant run(s)")
        return 1
    print(f"PASS: {len(args.seeds)} seeds and the controlled phase-boundary check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
