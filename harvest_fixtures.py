"""Run a small deterministic harvest against the project's sample decks.

The learning seat samples from the current valid-action mask while the normal
environment drives the other seat with its scripted policy.  A fresh output
directory is required so fixture results cannot be mixed with old checkpoints
or pre-fix statistics.

Example::

    python harvest_fixtures.py --games 8 --seed 42 --output harvest_runs/seed_42
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import math
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DECKS_DIRECTORY = PROJECT_ROOT / "Decks"
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "harvest_fixture_output"
DEFAULT_GAMES = 8
DEFAULT_SEED = 42
MAX_STEPS_PER_GAME = 2000
MAX_REPEATED_WAIT_STATES = 8
CONCEDE_ACTION = 12
NO_OP_ACTION = 224
HARVEST_VERSION = "fixture-harvest-v2"
VALID_RESULTS = {"win", "loss", "draw", "draw_both_loss"}
FIDELITY_COUNTERS = (
    "unimplemented_action", "unparsed_mana", "unparsed_modal", "unparsed_effects",
)
REQUIRED_GAME_FIELDS = {
    "schema_version", "ts", "result", "turn_count", "p1_deck", "p2_deck",
    "agent_is_p1", "agent_version", "terminal_reason", "fidelity",
}

EXPECTED_SAMPLE_DECKS = (
    "DimirMidrange",
    "DimirSelf",
    "Domain",
    "EsperSelf",
    "GolgariMidrange",
    "GruulAggro",
    "GruulProwess",
    "RedDeckWins",
)


class _ScheduledDeckPair(Sequence):
    """Make the environment's two ``random.choice`` calls use an exact pair.

    ``AlphaZeroMTGEnv.reset`` selects P1 and P2 independently with
    ``random.choice``.  This tiny sequence ignores the random indexes for its
    first two reads, making the fixture matchup schedule explicit without
    changing the environment or project deck loader.
    """

    def __init__(self, p1_deck, p2_deck):
        self._pair = (p1_deck, p2_deck)
        self._read_count = 0

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self._pair[index]
        if not isinstance(index, int) or not -2 <= index < 2:
            raise IndexError(index)
        deck = self._pair[min(self._read_count, 1)]
        self._read_count += 1
        return deck


def _quiet_engine_console_logging() -> int:
    """Keep the CLI compact and return the root level for later restoration."""
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.CRITICAL + 1)
    return previous_level


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def load_sample_decks(decks_directory: Path = DEFAULT_DECKS_DIRECTORY):
    """Load and order the audited eight-deck fixture through the project loader."""
    from Playersim.card import load_decks_and_card_db

    decks, card_db = load_decks_and_card_db(str(decks_directory))
    by_name = {deck.get("name"): deck for deck in decks}
    missing = [name for name in EXPECTED_SAMPLE_DECKS if name not in by_name]
    if missing:
        raise RuntimeError(
            "Sample deck fixture is incomplete; missing: " + ", ".join(missing)
        )
    return [by_name[name] for name in EXPECTED_SAMPLE_DECKS], card_db


def prepare_output_directory(output_directory: Path) -> Path:
    """Create an empty artifact directory, refusing to overwrite any content."""
    output = output_directory.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(
            f"Output directory must be empty for a deterministic run: {output}"
        )
    output.mkdir(parents=True, exist_ok=True)
    return output


def choose_fixture_action(action_mask: Iterable, rng: random.Random) -> int:
    """Choose reproducibly from legal actions, avoiding early concessions."""
    legal = [index for index, allowed in enumerate(action_mask) if bool(allowed)]
    if not legal:
        raise RuntimeError("Environment returned an empty valid-action mask")
    useful = [action for action in legal if action != CONCEDE_ACTION]
    return int(rng.choice(useful or legal))


def resolve_checkpoint_path(path: Path | str) -> Path:
    """Resolve an SB3 checkpoint, accepting an omitted ``.zip`` suffix."""
    checkpoint = Path(path).expanduser().resolve()
    if not checkpoint.is_file() and checkpoint.suffix != ".zip":
        zipped = checkpoint.with_suffix(".zip")
        if zipped.is_file():
            checkpoint = zipped
    if not checkpoint.is_file():
        raise FileNotFoundError(f"Checkpoint does not exist: {checkpoint}")
    return checkpoint


def checkpoint_identity(path: Path | str) -> dict:
    """Return stable provenance without embedding a machine-specific path."""
    checkpoint = resolve_checkpoint_path(path)
    digest = hashlib.sha256()
    with checkpoint.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "name": checkpoint.name,
        "sha256": digest.hexdigest(),
        "size": checkpoint.stat().st_size,
    }


def load_checkpoint_policy(path: Path | str):
    """Load a MaskablePPO checkpoint with this project's custom classes known."""
    import main as _training_entrypoint  # noqa: F401 - registers custom policy classes
    from sb3_contrib import MaskablePPO

    return MaskablePPO.load(str(resolve_checkpoint_path(path)), device="auto")


def choose_checkpoint_action(policy, observation: dict, action_mask: Iterable) -> int:
    """Predict deterministically and reject a checkpoint that violates its mask."""
    mask = list(bool(value) for value in action_mask)
    try:
        prediction = policy.predict(
            observation, action_masks=mask, deterministic=True)
    except TypeError as exc:
        raise RuntimeError(
            "Checkpoint policy does not support MaskablePPO action masks") from exc
    action = prediction[0] if isinstance(prediction, tuple) else prediction
    action_index = int(getattr(action, "item", lambda: action)())
    if not 0 <= action_index < len(mask) or not mask[action_index]:
        raise RuntimeError(
            f"Checkpoint selected mask-invalid action {action_index}")
    return action_index


def policy_version(seed: int, agent_identity: dict | None,
                   opponent_identity: dict | None) -> str:
    if agent_identity is None and opponent_identity is None:
        return f"{HARVEST_VERSION}-seed-{seed}"
    provenance = json.dumps(
        {"agent": agent_identity or "random-valid",
         "opponent": opponent_identity or "scripted"},
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(provenance).hexdigest()[:16]
    return f"{HARVEST_VERSION}-policy-{digest}-seed-{seed}"


def _wait_state_signature(env, action_mask: Iterable):
    """Identify a returned state in which the agent can only wait in place."""
    legal = tuple(
        index
        for index, allowed in enumerate(action_mask)
        if bool(allowed) and index != CONCEDE_ACTION
    )
    if legal != (NO_OP_ACTION,):
        return None
    state = env.game_state
    priority = getattr(state, "priority_player", None)
    priority_name = priority.get("name") if isinstance(priority, dict) else None
    return (
        getattr(state, "turn", None),
        getattr(state, "phase", None),
        priority_name,
        len(getattr(state, "stack", ())),
    )


def _advance_wait_counter(previous_signature, repeated_waits: int, signature):
    """Count only an identical consecutive wait state as a stall."""
    if signature is None:
        return None, 0
    if signature == previous_signature:
        return signature, repeated_waits + 1
    return signature, 1


def scheduled_matchup(decks: Sequence, game_index: int, seed: int):
    """Return a repeatable rotation in which every eight games seats each deck."""
    if len(decks) < 2:
        raise RuntimeError("Fixture harvest requires at least two decks")
    p1_index = game_index % len(decks)
    offset = 1 + (seed % (len(decks) - 1))
    p2_index = (p1_index + offset) % len(decks)
    return decks[p1_index], decks[p2_index]


def rank_manifest_entries(manifest: dict) -> list[tuple[str, dict]]:
    """Sort manifest entries by observed count, severity, then card name."""
    severity_rank = {"crash": 2, "unparsed": 1, "partial": 0}
    return sorted(
        manifest.items(),
        key=lambda item: (
            -int(item[1].get("count", 0)),
            -severity_rank.get(item[1].get("severity", "partial"), 0),
            item[0].casefold(),
        ),
    )


def _read_records(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _read_gzip_json(path: Path, label: str) -> dict:
    """Read one required compressed JSON artifact without accepting fallbacks."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Harvest {label} is not valid gzip JSON: {path}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Harvest {label} must contain a JSON object: {path}")
    return data


def _nonnegative_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{label} must be a non-negative integer")
    return value


def _finite_number(value, label: str, *, minimum=None, maximum=None) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise RuntimeError(f"{label} must be a finite number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise RuntimeError(f"{label} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise RuntimeError(f"{label} must be at most {maximum}")
    return number


def _expected_deck_stats(records: Sequence[dict]) -> dict[str, Counter]:
    """Convert agent-relative log results into per-deck tracker totals."""
    expected: dict[str, Counter] = {}

    def add(deck_name: str, outcome: str, turn_count: int) -> None:
        totals = expected.setdefault(deck_name, Counter())
        totals["games"] += 1
        totals[outcome] += 1
        totals["total_turns"] += turn_count

    for record in records:
        result = record["result"]
        turn_count = record["turn_count"]
        if result in {"draw", "draw_both_loss"}:
            add(record["p1_deck"], "draws", turn_count)
            add(record["p2_deck"], "draws", turn_count)
            continue
        agent_won = result == "win"
        p1_won = agent_won if record["agent_is_p1"] else not agent_won
        add(record["p1_deck"], "wins" if p1_won else "losses", turn_count)
        add(record["p2_deck"], "losses" if p1_won else "wins", turn_count)
    return expected


def _validate_rate(stats: dict, games_key: str, label: str) -> None:
    games = _nonnegative_int(stats.get(games_key), f"{label}.{games_key}")
    wins = _nonnegative_int(stats.get("wins"), f"{label}.wins")
    losses = _nonnegative_int(stats.get("losses"), f"{label}.losses")
    draws = _nonnegative_int(stats.get("draws"), f"{label}.draws")
    if wins + losses + draws != games:
        raise RuntimeError(
            f"{label} outcomes do not add up to {games_key}: "
            f"{wins}+{losses}+{draws}!={games}")
    actual_rate = _finite_number(
        stats.get("win_rate"), f"{label}.win_rate", minimum=0, maximum=1)
    expected_rate = (wins + 0.5 * draws) / games if games else 0.0
    if not math.isclose(actual_rate, expected_rate, rel_tol=1e-9, abs_tol=1e-9):
        raise RuntimeError(
            f"{label}.win_rate {actual_rate} does not match {expected_rate}")


def _validate_tracker_artifacts(
    output: Path,
    records: Sequence[dict],
    canonical_decks: Sequence[dict] | None = None,
    card_db: dict | None = None,
) -> None:
    """Validate and cross-check deck, card, metadata, and card-memory JSON."""
    expected_deck_totals = _expected_deck_stats(records)
    deck_paths = sorted((output / "decks").glob("*.json.gz"))
    if not deck_paths:
        raise RuntimeError("Harvest did not persist deck aggregates")

    decks_by_name = {}
    for path in deck_paths:
        stats = _read_gzip_json(path, "deck aggregate")
        name = stats.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"Deck aggregate has an invalid name: {path}")
        if name in decks_by_name:
            raise RuntimeError(f"Duplicate deck aggregate for {name}")
        if not isinstance(stats.get("archetype"), str) or not stats["archetype"]:
            raise RuntimeError(f"Deck aggregate {name} has an invalid archetype")
        card_list = stats.get("card_list")
        if not isinstance(card_list, list) or not card_list:
            raise RuntimeError(f"Deck aggregate {name} has no card_list")
        seen_card_names = set()
        seen_card_ids = set()
        deck_size = 0
        for entry in card_list:
            if not isinstance(entry, dict):
                raise RuntimeError(f"Deck aggregate {name} has an invalid card entry")
            card_name = entry.get("name")
            card_id = entry.get("id")
            count = entry.get("count")
            if (not isinstance(card_name, str) or not card_name
                    or isinstance(count, bool) or not isinstance(count, int)
                    or count < 1 or card_id is None):
                raise RuntimeError(f"Deck aggregate {name} has an invalid card entry")
            if card_name in seen_card_names or str(card_id) in seen_card_ids:
                raise RuntimeError(f"Deck aggregate {name} has a duplicate card entry")
            seen_card_names.add(card_name)
            seen_card_ids.add(str(card_id))
            deck_size += count
        if deck_size < 60:
            raise RuntimeError(f"Deck aggregate {name} contains only {deck_size} cards")
        _validate_rate(stats, "games", f"deck aggregate {name}")
        total_turns = _nonnegative_int(
            stats.get("total_turns"), f"deck aggregate {name}.total_turns")
        average = _finite_number(
            stats.get("avg_game_length"),
            f"deck aggregate {name}.avg_game_length", minimum=0)
        expected_average = total_turns / stats["games"] if stats["games"] else 0.0
        if not math.isclose(average, expected_average, rel_tol=1e-9, abs_tol=1e-9):
            raise RuntimeError(f"Deck aggregate {name} has an invalid avg_game_length")
        decks_by_name[name] = stats

    if set(decks_by_name) != set(expected_deck_totals):
        raise RuntimeError(
            "Deck aggregate labels do not match the game log: "
            f"expected={sorted(expected_deck_totals)}, actual={sorted(decks_by_name)}")
    for name, expected in expected_deck_totals.items():
        stats = decks_by_name[name]
        for field in ("games", "wins", "losses", "draws", "total_turns"):
            if stats[field] != expected[field]:
                raise RuntimeError(
                    f"Deck aggregate {name}.{field}={stats[field]} does not match "
                    f"the game log ({expected[field]})")

    if canonical_decks is not None:
        canonical_by_name = {
            deck.get("name"): deck for deck in canonical_decks
            if (isinstance(deck, dict)
                and deck.get("name") in expected_deck_totals)
        }
        if set(canonical_by_name) != set(expected_deck_totals):
            raise RuntimeError("Scheduled deck definitions do not match the game log")
        for deck_name, stats in decks_by_name.items():
            canonical_counts = Counter(canonical_by_name[deck_name].get("cards", []))
            artifact_counts = {
                str(entry["id"]): (entry["name"], entry["count"])
                for entry in stats["card_list"]
            }
            if set(artifact_counts) != {str(card_id) for card_id in canonical_counts}:
                raise RuntimeError(
                    f"Deck aggregate {deck_name} card IDs do not match the fixture deck")
            for card_id, count in canonical_counts.items():
                card = (card_db or {}).get(card_id)
                expected_name = getattr(card, "name", None)
                artifact_name, artifact_count = artifact_counts[str(card_id)]
                if artifact_count != count or (expected_name and artifact_name != expected_name):
                    raise RuntimeError(
                        f"Deck aggregate {deck_name} composition does not match "
                        f"the fixture deck at card {card_id}")

    expected_cards: dict[str, Counter] = {}
    expected_card_ids: dict[str, str] = {}
    expected_card_usage: Counter = Counter()
    for deck_stats in decks_by_name.values():
        for entry in deck_stats["card_list"]:
            card_name = entry["name"]
            card_id = str(entry["id"])
            previous_id = expected_card_ids.setdefault(card_name, card_id)
            if previous_id != card_id:
                raise RuntimeError(f"Card {card_name} has inconsistent IDs in deck aggregates")
            totals = expected_cards.setdefault(card_name, Counter())
            for field in ("games", "wins", "losses", "draws"):
                totals[field] += deck_stats[field]
            expected_card_usage[card_name] += entry["count"] * deck_stats["games"]

    card_paths = sorted((output / "cards").glob("*.json.gz"))
    if not card_paths:
        raise RuntimeError("Harvest did not persist card aggregates")
    cards_by_name = {}
    for path in card_paths:
        stats = _read_gzip_json(path, "card aggregate")
        name = stats.get("name")
        if not isinstance(name, str) or not name or name in cards_by_name:
            raise RuntimeError(f"Card aggregate has an invalid or duplicate name: {path}")
        _validate_rate(stats, "games_played", f"card aggregate {name}")
        usage_count = _nonnegative_int(
            stats.get("usage_count"), f"card aggregate {name}.usage_count")
        if usage_count > stats["games_played"]:
            raise RuntimeError(f"Card aggregate {name}.usage_count exceeds games_played")
        archetype_group = stats.get("archetypes")
        if not isinstance(archetype_group, dict) or not archetype_group:
            raise RuntimeError(f"Card aggregate {name} has invalid archetypes")
        archetype_games = 0
        for archetype, bucket in archetype_group.items():
            if not isinstance(archetype, str) or not isinstance(bucket, dict):
                raise RuntimeError(f"Card aggregate {name} has an invalid archetype entry")
            bucket_games = _nonnegative_int(
                bucket.get("games"),
                f"card aggregate {name}.archetypes.{archetype}.games")
            bucket_wins = _nonnegative_int(
                bucket.get("wins"),
                f"card aggregate {name}.archetypes.{archetype}.wins")
            bucket_draws = _nonnegative_int(
                bucket.get("draws"),
                f"card aggregate {name}.archetypes.{archetype}.draws")
            if bucket_wins + bucket_draws > bucket_games:
                raise RuntimeError(f"Card aggregate {name} has inconsistent archetype data")
            archetype_games += bucket_games
        if archetype_games != stats["games_played"]:
            raise RuntimeError(f"Card aggregate {name}.archetypes does not total games_played")
        for group_name in ("by_game_stage", "by_game_state"):
            group = stats.get(group_name)
            if not isinstance(group, dict) or not group:
                raise RuntimeError(f"Card aggregate {name} has invalid {group_name}")
            grouped_games = 0
            for bucket_name, bucket in group.items():
                if not isinstance(bucket, dict):
                    raise RuntimeError(
                        f"Card aggregate {name}.{group_name}.{bucket_name} is invalid")
                bucket_games = _nonnegative_int(
                    bucket.get("games"),
                    f"card aggregate {name}.{group_name}.{bucket_name}.games")
                bucket_wins = _nonnegative_int(
                    bucket.get("wins"),
                    f"card aggregate {name}.{group_name}.{bucket_name}.wins")
                bucket_draws = _nonnegative_int(
                    bucket.get("draws"),
                    f"card aggregate {name}.{group_name}.{bucket_name}.draws")
                if bucket_wins + bucket_draws > bucket_games:
                    raise RuntimeError(
                        f"Card aggregate {name}.{group_name}.{bucket_name} is inconsistent")
                grouped_games += bucket_games
            if grouped_games != stats["games_played"]:
                raise RuntimeError(
                    f"Card aggregate {name}.{group_name} does not total games_played")
        cards_by_name[name] = stats

    if set(cards_by_name) != set(expected_cards):
        raise RuntimeError(
            "Card aggregate labels do not match deck contents: "
            f"expected={sorted(expected_cards)}, actual={sorted(cards_by_name)}")
    for name, expected in expected_cards.items():
        stats = cards_by_name[name]
        for artifact_field, expected_field in (
                ("games_played", "games"), ("wins", "wins"),
                ("losses", "losses"), ("draws", "draws")):
            if stats[artifact_field] != expected[expected_field]:
                raise RuntimeError(
                    f"Card aggregate {name}.{artifact_field} does not match deck aggregates")

    meta_path = output / "meta" / "meta_data.json.gz"
    if not meta_path.is_file():
        raise RuntimeError("Harvest did not persist tracker metadata")
    for path in sorted((output / "meta").glob("*.json.gz")):
        _read_gzip_json(path, "tracker metadata")
    meta = _read_gzip_json(meta_path, "tracker metadata")
    if _nonnegative_int(meta.get("total_games"), "tracker metadata.total_games") != len(records):
        raise RuntimeError("Tracker metadata total_games does not match the game log")
    draw_games = sum(
        record["result"] in {"draw", "draw_both_loss"} for record in records)
    if _nonnegative_int(meta.get("draws"), "tracker metadata.draws") != draw_games:
        raise RuntimeError("Tracker metadata draws do not match the game log")
    archetypes = meta.get("archetypes")
    matchups = meta.get("matchups")
    meta_cards = meta.get("cards")
    if not isinstance(archetypes, dict) or not isinstance(matchups, dict) \
            or not isinstance(meta_cards, dict):
        raise RuntimeError("Tracker metadata has invalid aggregate sections")
    archetype_games = 0
    for archetype, stats in archetypes.items():
        if not isinstance(archetype, str) or not isinstance(stats, dict):
            raise RuntimeError("Tracker metadata has an invalid archetype entry")
        _validate_rate(stats, "games", f"tracker archetype {archetype}")
        archetype_games += stats["games"]
    if archetype_games != 2 * len(records):
        raise RuntimeError("Tracker archetype totals do not match the game log")
    matchup_games = 0
    for matchup, stats in matchups.items():
        if not isinstance(matchup, str) or not isinstance(stats, dict):
            raise RuntimeError("Tracker metadata has an invalid matchup entry")
        wins = _nonnegative_int(stats.get("wins"), f"tracker matchup {matchup}.wins")
        losses = _nonnegative_int(stats.get("losses"), f"tracker matchup {matchup}.losses")
        draws = _nonnegative_int(stats.get("draws"), f"tracker matchup {matchup}.draws")
        rate = _finite_number(
            stats.get("win_rate"), f"tracker matchup {matchup}.win_rate",
            minimum=0, maximum=1)
        total = wins + losses + draws
        expected_rate = (wins + 0.5 * draws) / total if total else 0.0
        if not math.isclose(rate, expected_rate, rel_tol=1e-9, abs_tol=1e-9):
            raise RuntimeError(f"Tracker matchup {matchup} has an invalid win_rate")
        matchup_games += total
    if matchup_games != 2 * len(records):
        raise RuntimeError("Tracker matchup totals do not match the game log")
    if set(meta_cards) != set(expected_cards):
        raise RuntimeError("Tracker metadata card labels do not match deck contents")
    for card_name, expected in expected_cards.items():
        stats = meta_cards.get(card_name)
        if not isinstance(stats, dict):
            raise RuntimeError(f"Tracker metadata card {card_name} is invalid")
        for field in ("games", "wins", "losses", "draws", "usage_count"):
            _nonnegative_int(stats.get(field), f"tracker metadata card {card_name}.{field}")
        if stats["games"] != expected["games"] \
                or stats["wins"] != expected["wins"] \
                or stats["losses"] != expected["losses"] \
                or stats["draws"] != expected["draws"]:
            raise RuntimeError(
                f"Tracker metadata card {card_name} outcomes do not match deck aggregates")
        if stats["usage_count"] != expected_card_usage[card_name]:
            raise RuntimeError(
                f"Tracker metadata card {card_name}.usage_count does not match deck aggregates")
        rate = _finite_number(
            stats.get("win_rate"), f"tracker metadata card {card_name}.win_rate",
            minimum=0, maximum=1)
        expected_rate = ((stats["wins"] + 0.5 * stats["draws"]) / stats["games"]
                         if stats["games"] else 0.0)
        if not math.isclose(rate, expected_rate, rel_tol=1e-9, abs_tol=1e-9):
            raise RuntimeError(f"Tracker metadata card {card_name} has an invalid win_rate")
        play_rate = _finite_number(
            stats.get("play_rate"), f"tracker metadata card {card_name}.play_rate",
            minimum=0, maximum=1)
        expected_play_rate = (
            stats["games"] / (2 * len(records)) if records else 0.0)
        if not math.isclose(
                play_rate, expected_play_rate, rel_tol=1e-9, abs_tol=1e-9):
            raise RuntimeError(f"Tracker metadata card {card_name} has a stale play_rate")
        associations = stats.get("archetypes")
        if (not isinstance(associations, dict)
                or any(not isinstance(key, str) or isinstance(value, bool)
                       or not isinstance(value, int) or value < 0
                       for key, value in associations.items())
                or sum(associations.values()) != stats["games"]):
            raise RuntimeError(
                f"Tracker metadata card {card_name} has invalid archetype counts")

    memory_path = output / "card_memory" / "all_cards.json.gz"
    if not memory_path.is_file():
        raise RuntimeError("Harvest did not persist card memory")
    memory = _read_gzip_json(memory_path, "card memory")
    memory_cards = memory.get("cards")
    name_to_id = memory.get("name_to_id")
    id_to_name = memory.get("id_to_name")
    if not isinstance(memory_cards, dict) or not isinstance(name_to_id, dict) \
            or not isinstance(id_to_name, dict):
        raise RuntimeError("Card memory has invalid mapping sections")
    if set(memory_cards) != set(expected_card_ids.values()):
        raise RuntimeError("Card memory IDs do not match deck contents")
    for card_name, card_id in expected_card_ids.items():
        entry = memory_cards.get(card_id)
        if not isinstance(entry, dict) or entry.get("name") != card_name \
                or str(entry.get("id")) != card_id:
            raise RuntimeError(f"Card memory entry for {card_name} is invalid")
        if str(name_to_id.get(card_name)) != card_id \
                or id_to_name.get(card_id) != card_name:
            raise RuntimeError(f"Card memory mapping for {card_name} is inconsistent")
        _validate_rate(entry, "games_played", f"card memory {card_name}")
        aggregate = cards_by_name[card_name]
        for field in ("games_played", "wins", "losses", "draws"):
            if entry[field] != aggregate[field]:
                raise RuntimeError(
                    f"Card memory {card_name}.{field} does not match card aggregates")
        for field in ("times_drawn", "times_played"):
            if _nonnegative_int(entry.get(field), f"card memory {card_name}.{field}") \
                    > entry["games_played"]:
                raise RuntimeError(
                    f"Card memory {card_name}.{field} exceeds games_played")
        if entry["times_played"] != aggregate["usage_count"]:
            raise RuntimeError(
                f"Card memory {card_name}.times_played does not match card aggregates")
        memory_archetypes = entry.get("archetype_performance")
        if not isinstance(memory_archetypes, dict) or not memory_archetypes:
            raise RuntimeError(f"Card memory {card_name} has invalid archetype_performance")
        memory_archetype_games = 0
        for archetype, bucket in memory_archetypes.items():
            if not isinstance(archetype, str) or not isinstance(bucket, dict):
                raise RuntimeError(
                    f"Card memory {card_name} has an invalid archetype entry")
            bucket_games = _nonnegative_int(
                bucket.get("games"),
                f"card memory {card_name}.archetype_performance.{archetype}.games")
            bucket_wins = _nonnegative_int(
                bucket.get("wins"),
                f"card memory {card_name}.archetype_performance.{archetype}.wins")
            bucket_losses = _nonnegative_int(
                bucket.get("losses"),
                f"card memory {card_name}.archetype_performance.{archetype}.losses")
            bucket_draws = _nonnegative_int(
                bucket.get("draws"),
                f"card memory {card_name}.archetype_performance.{archetype}.draws")
            if bucket_wins + bucket_losses + bucket_draws != bucket_games:
                raise RuntimeError(
                    f"Card memory {card_name} has inconsistent archetype data")
            memory_archetype_games += bucket_games
        if memory_archetype_games != entry["games_played"]:
            raise RuntimeError(
                f"Card memory {card_name}.archetype_performance does not total games_played")
        trend = entry.get("performance_trend")
        if (not isinstance(trend, list) or len(trend) > 10
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in trend)):
            raise RuntimeError(f"Card memory {card_name} has an invalid performance_trend")


def _validate_artifacts(
    output: Path,
    games: int,
    agent_version: str,
    expected_matchups: Sequence[tuple[str, str]] | None = None,
    expected_decks: Sequence[dict] | None = None,
    card_db: dict | None = None,
):
    required = {
        "game log": output / "game_log.jsonl",
        "fidelity report": output / "fidelity_report.json",
        "card support manifest": output / "card_support_manifest.json",
    }
    missing = [label for label, path in required.items() if not path.is_file()]
    if missing:
        raise RuntimeError("Harvest did not persist: " + ", ".join(missing))

    records = _read_records(required["game log"])
    if len(records) != games:
        raise RuntimeError(f"Expected {games} game records, found {len(records)}")
    unstamped = [record for record in records if record.get("agent_version") != agent_version]
    if unstamped:
        raise RuntimeError("One or more game records have the wrong agent_version")
    for record_index, record in enumerate(records, start=1):
        missing_fields = REQUIRED_GAME_FIELDS - set(record)
        if missing_fields:
            raise RuntimeError(
                f"Game record {record_index} is missing: "
                + ", ".join(sorted(missing_fields)))
        if record.get("schema_version") != 1:
            raise RuntimeError(
                f"Game record {record_index} has unsupported schema_version")
        if record.get("result") not in VALID_RESULTS:
            raise RuntimeError(
                f"Game record {record_index} is not a completed result: "
                f"{record.get('result')!r}")
        if not isinstance(record.get("terminal_reason"), str) \
                or not record.get("terminal_reason"):
            raise RuntimeError(
                f"Game record {record_index} has an invalid terminal_reason")
        if record.get("p1_deck") not in EXPECTED_SAMPLE_DECKS \
                or record.get("p2_deck") not in EXPECTED_SAMPLE_DECKS:
            raise RuntimeError(f"Game record {record_index} has an unknown deck label")
        if not isinstance(record.get("agent_is_p1"), bool) \
                or not isinstance(record.get("fidelity"), dict):
            raise RuntimeError(f"Game record {record_index} has invalid field types")
        _finite_number(record.get("ts"), f"Game record {record_index}.ts")
        turn_count = record.get("turn_count")
        if isinstance(turn_count, bool) or not isinstance(turn_count, int) or turn_count < 1:
            raise RuntimeError(f"Game record {record_index} has an invalid turn_count")
        for counter_name in FIDELITY_COUNTERS:
            _nonnegative_int(
                record["fidelity"].get(counter_name, 0),
                f"Game record {record_index}.fidelity.{counter_name}")
        unparsed_cards = record["fidelity"].get("unparsed_cards", [])
        if (not isinstance(unparsed_cards, list)
                or any(not isinstance(name, str) for name in unparsed_cards)):
            raise RuntimeError(
                f"Game record {record_index} has invalid fidelity.unparsed_cards")

    if expected_matchups is not None:
        actual_matchups = [
            (record.get("p1_deck"), record.get("p2_deck")) for record in records
        ]
        if actual_matchups != list(expected_matchups):
            raise RuntimeError(
                f"Recorded matchups differ from the schedule: {actual_matchups}")

    with required["fidelity report"].open(encoding="utf-8") as handle:
        fidelity = json.load(handle)
    if fidelity.get("games_recorded") != games:
        raise RuntimeError("Fidelity report count does not match the game log")
    if fidelity.get("agent_version") != agent_version:
        raise RuntimeError("Fidelity report has the wrong agent_version")
    for counter_name in FIDELITY_COUNTERS:
        expected_total = sum(
            int(record["fidelity"].get(counter_name, 0)) for record in records)
        if int(fidelity.get(counter_name, 0)) != expected_total:
            raise RuntimeError(
                f"Fidelity total for {counter_name} does not match game records")
    expected_unparsed_cards = Counter()
    for record in records:
        expected_unparsed_cards.update(set(record["fidelity"].get("unparsed_cards", [])))
    if fidelity.get("unparsed_cards", {}) != dict(expected_unparsed_cards):
        raise RuntimeError("Cumulative unparsed-card counts do not match game records")

    with required["card support manifest"].open(encoding="utf-8") as handle:
        manifest = json.load(handle) or {}
    for card_name, entry in manifest.items():
        if (not isinstance(card_name, str) or not isinstance(entry, dict)
                or int(entry.get("count", 0)) < 1
                or entry.get("severity") not in {"partial", "unparsed", "crash"}):
            raise RuntimeError("Card support manifest contains an invalid entry")
    _validate_tracker_artifacts(
        output, records, canonical_decks=expected_decks, card_db=card_db)
    return records, fidelity, manifest


def print_summary(
    output: Path,
    seed: int,
    records: Sequence[dict],
    fidelity: dict,
    manifest: dict,
) -> None:
    """Print the small operator-facing report requested by the harvest workflow."""
    results = Counter(record.get("result", "unknown") for record in records)
    result_text = ", ".join(f"{name}={count}" for name, count in sorted(results.items()))
    issue_total = sum(
        int(fidelity.get(key, 0))
        for key in ("unimplemented_action", "unparsed_mana", "unparsed_modal", "unparsed_effects")
    )
    print(
        f"Harvest complete: games={len(records)} seed={seed} "
        f"results=[{result_text}] fidelity_issues={issue_total}"
    )

    ranked = rank_manifest_entries(manifest)
    if not ranked:
        print("Manifest: clean (no entries)")
    else:
        print(f"Manifest: {len(ranked)} card(s), ranked by observed count")
        for card_name, entry in ranked[:10]:
            reasons = entry.get("reasons", {})
            top_reason = max(reasons.items(), key=lambda item: item[1])[0] if reasons else "unspecified"
            print(
                f"  {int(entry.get('count', 0)):>5}  "
                f"{entry.get('severity', 'partial'):<8}  {card_name}: {top_reason}"
            )
        if len(ranked) > 10:
            print(f"  ... {len(ranked) - 10} more")
    print(f"Artifacts: {output}")


def run_harvest(games: int, seed: int, output_directory: Path,
                max_steps: int = MAX_STEPS_PER_GAME, *, game_offset: int = 0,
                agent_model: Path | str | None = None,
                opponent_model: Path | str | None = None):
    """Run fixture games and return their validated artifact data."""
    if games < 1:
        raise ValueError("games must be at least 1")
    if max_steps < 1:
        raise ValueError("max_steps must be at least 1")
    if isinstance(game_offset, bool) or not isinstance(game_offset, int) \
            or game_offset < 0:
        raise ValueError("game_offset must be a non-negative integer")

    output = prepare_output_directory(Path(output_directory))
    agent_identity = checkpoint_identity(agent_model) if agent_model else None
    opponent_identity = checkpoint_identity(opponent_model) if opponent_model else None
    agent_version = policy_version(seed, agent_identity, opponent_identity)
    previous_log_disable = logging.root.manager.disable
    previous_root_level = None
    previous_debug_action_steps = None
    environment_module = None
    expected_matchups: list[tuple[str, str]] = []
    logging.disable(logging.CRITICAL)
    env = None
    agent_policy = None
    opponent_policy = None
    try:
        # Programmatic calls can share a process with prior simulations. A
        # fresh output directory must start with a fresh in-memory manifest too.
        from Playersim.card_support import reset_manifest_for_tests
        reset_manifest_for_tests()
        decks, card_db = load_sample_decks()
        from Playersim import environment as environment_module

        previous_debug_action_steps = environment_module.DEBUG_ACTION_STEPS
        environment_module.DEBUG_ACTION_STEPS = False
        AlphaZeroMTGEnv = environment_module.AlphaZeroMTGEnv
        previous_root_level = _quiet_engine_console_logging()
        if agent_model:
            agent_policy = load_checkpoint_policy(agent_model)
        if opponent_model:
            opponent_policy = load_checkpoint_policy(opponent_model)

        env = AlphaZeroMTGEnv(
            decks,
            card_db,
            deck_stats_path=str(output),
            card_memory_path=str(output / "card_memory"),
        )
        env.set_agent_version(agent_version)
        if opponent_policy is not None:
            env.set_opponent_policy(opponent_policy)

        for local_game_index in range(games):
            game_index = game_offset + local_game_index
            game_seed = seed + game_index
            p1_deck, p2_deck = scheduled_matchup(decks, game_index, seed)
            expected_pair = (p1_deck["name"], p2_deck["name"])
            expected_matchups.append(expected_pair)
            env.decks = _ScheduledDeckPair(p1_deck, p2_deck)
            observation, reset_info = env.reset(seed=game_seed)
            if reset_info.get("error_reset"):
                raise RuntimeError(
                    f"Fixture game {game_index + 1} used the emergency reset")
            if getattr(env, "last_observation_error", None):
                raise RuntimeError(
                    f"Fixture game {game_index + 1} reset observation degraded: "
                    f"{env.last_observation_error}\n"
                    f"{getattr(env, 'last_observation_traceback', '') or ''}")
            actual_pair = (
                getattr(env, "current_deck_name_p1", None),
                getattr(env, "current_deck_name_p2", None),
            )
            if actual_pair != expected_pair:
                raise RuntimeError(
                    f"Fixture game {game_index + 1} reset the wrong matchup: "
                    f"{actual_pair} != {expected_pair}")
            rng = random.Random(game_seed ^ 0x5EED5EED)

            terminated = truncated = False
            steps = 0
            final_info = {}
            repeated_waits = 0
            previous_wait_signature = None
            abort_reason = None
            while not (terminated or truncated) and steps < max_steps:
                if getattr(env.game_state, "_consecutive_no_ops", 0) > 12:
                    abort_reason = "engine NO_OP recovery limit"
                    break
                mask = env.action_mask()
                mask_error = getattr(
                    getattr(env, "action_handler", None),
                    "last_mask_error", None)
                if mask_error:
                    raise RuntimeError(
                        f"Fixture game {game_index + 1} action mask degraded: "
                        f"{mask_error}")
                wait_signature = _wait_state_signature(env, mask)
                previous_wait_signature, repeated_waits = _advance_wait_counter(
                    previous_wait_signature, repeated_waits, wait_signature)
                if repeated_waits >= MAX_REPEATED_WAIT_STATES:
                    abort_reason = f"repeated wait state {wait_signature}"
                    break

                action = (
                    choose_checkpoint_action(agent_policy, observation, mask)
                    if agent_policy is not None
                    else choose_fixture_action(mask, rng)
                )
                action_metadata = getattr(
                    getattr(env, "action_handler", None),
                    "action_reasons_with_context", {}).get(action, {})
                observation, _, terminated, truncated, final_info = env.step(action)
                steps += 1
                if getattr(env, "last_observation_error", None):
                    raise RuntimeError(
                        f"Fixture game {game_index + 1} observation degraded: "
                        f"{env.last_observation_error}\n"
                        f"{getattr(env, 'last_observation_traceback', '') or ''}")
                if final_info.get("execution_failed"):
                    actor = ("opponent" if final_info.get("opponent_execution_failed")
                             else "agent")
                    raise RuntimeError(
                        f"Fixture game {game_index + 1} had a mask-valid {actor} "
                        f"action {action} fail: "
                        f"{final_info.get('error_message', 'unspecified')}; "
                        f"mask_metadata={action_metadata}")
                if final_info.get("invalid_action"):
                    raise RuntimeError(
                        f"Fixture game {game_index + 1} had a mask-valid action "
                        f"rejected: {final_info.get('invalid_action_reason', 'unspecified')}")
                if final_info.get("critical_error"):
                    raise RuntimeError(
                        f"Fixture game {game_index + 1} hit a critical error: "
                        f"{final_info.get('error_message', 'unspecified')}")

            if not (terminated or truncated):
                abort_reason = abort_reason or f"{max_steps}-step safety cap"
                raise RuntimeError(
                    f"Fixture game {game_index + 1} aborted: {abort_reason}")
            if not getattr(env, "_game_result_recorded", False):
                detail = final_info.get("error_message", "no result recorded")
                raise RuntimeError(f"Fixture game {game_index + 1} failed: {detail}")
            if getattr(env, "_game_result", None) not in VALID_RESULTS:
                raise RuntimeError(
                    f"Fixture game {game_index + 1} ended without a usable result: "
                    f"{getattr(env, '_game_result', None)!r}")
    finally:
        if env is not None:
            env.close()
        if environment_module is not None and previous_debug_action_steps is not None:
            environment_module.DEBUG_ACTION_STEPS = previous_debug_action_steps
        if previous_root_level is not None:
            logging.getLogger().setLevel(previous_root_level)
        logging.disable(previous_log_disable)

    records, fidelity, manifest = _validate_artifacts(
        output, games, agent_version, expected_matchups=expected_matchups,
        expected_decks=decks, card_db=card_db)
    run_manifest = {
        "schema_version": 1,
        "status": "complete",
        "harvest_version": HARVEST_VERSION,
        "agent_version": agent_version,
        "seed": seed,
        "games": games,
        "game_offset": game_offset,
        "max_steps": max_steps,
        "agent_policy": agent_identity or {"kind": "random-valid"},
        "opponent_policy": opponent_identity or {"kind": "scripted"},
        "decks": list(EXPECTED_SAMPLE_DECKS),
        "matchups": [
            {"p1": p1_name, "p2": p2_name}
            for p1_name, p2_name in expected_matchups
        ],
        "results": dict(sorted(Counter(
            record["result"] for record in records).items())),
    }
    with (output / "harvest_run.json").open("w", encoding="utf-8") as handle:
        json.dump(run_manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print_summary(output, seed, records, fidelity, manifest)
    return {
        "output": output,
        "agent_version": agent_version,
        "records": records,
        "fidelity": fidelity,
        "manifest": manifest,
        "run_manifest": run_manifest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic random-valid fixtures against the scripted opponent."
    )
    parser.add_argument(
        "--games",
        type=_positive_int,
        default=DEFAULT_GAMES,
        help=f"number of games to run (default: {DEFAULT_GAMES})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"base reset/action seed (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--max-steps",
        type=_positive_int,
        default=MAX_STEPS_PER_GAME,
        help=f"safety cap per game (default: {MAX_STEPS_PER_GAME})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="fresh directory for game_log, fidelity, manifest, and tracker artifacts",
    )
    parser.add_argument(
        "--game-offset", type=int, default=0,
        help="global schedule offset used by parallel protocol shards",
    )
    parser.add_argument(
        "--agent-model", type=Path,
        help="optional MaskablePPO checkpoint for the learning/P1 seat",
    )
    parser.add_argument(
        "--opponent-model", type=Path,
        help="optional MaskablePPO checkpoint for the environment/P2 seat",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_harvest(
            args.games, args.seed, args.output, max_steps=args.max_steps,
            game_offset=args.game_offset, agent_model=args.agent_model,
            opponent_model=args.opponent_model)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
