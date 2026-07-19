"""Fail-closed dynamic execution probes for a frozen format card pool.

The support ledger is a static parser/registration audit.  This runner adds
bounded runtime evidence without ever calling a card "clean" or "verified".
Every discovered surface is an obligation.  A surface that the bounded runner
cannot exercise is reported as ``coverage_gap``; warnings, fidelity changes,
mask-valid execution failures, and broken invariants are ``failed``.

Typical full-pool invocation::

    python -m Playersim.card_probe \
      --snapshot "Format Card Lists/standard.jsonl" \
      --registry formats/standard/card_registry.json \
      --ledger formats/standard/support_ledger.json \
      --format standard --output probe_runs/standard

Use ``--card``, ``--status``, index bounds, or deterministic shard flags for a
small first run.  ``--resume`` reuses only terminal per-card artifacts whose
input identity matches the current snapshot, registry, ledger, and probe
schema.  The runner never edits the support ledger or support overrides.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
import traceback
from typing import Any, Iterable, Sequence
import warnings

import numpy as np

from .ability_types import (
    ActivatedAbility,
    ManaAbility,
    StaticAbility,
    TriggeredAbility,
    _is_grouped_zone_change_trigger,
    _last_known_matches_criteria,
    _permanent_matches_any_criteria,
)
from .actions import ActionHandler
from .card import Card
from .card_registry import load_pool_snapshot_cards, load_registry
from .game_state import GameState
from .printed_trigger_discovery import discover_printed_trigger_inventory


PROBE_KIND = "playersim_dynamic_card_probe"
CARD_RESULT_KIND = "playersim_dynamic_card_probe_card"
PROBE_SCHEMA_VERSION = 3
TERMINAL_STATUSES = {"execution_passed", "coverage_gap", "failed"}
DEFAULT_MAX_DECISIONS = 48
DEFAULT_MAX_BRANCHES = 32
FIXTURE_LAND_COPIES_PER_COLOR = 3


def _canonical_hash(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded deterministic representation for diagnostics."""
    if depth > 6:
        return {"truncated_type": type(value).__name__}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, (set, frozenset)):
        converted = [_json_safe(item, depth=depth + 1) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))[:100]
    if hasattr(value, "card_id"):
        return {
            "object": type(value).__name__,
            "card_id": _json_safe(getattr(value, "card_id", None), depth=depth + 1),
            "name": getattr(value, "name", None),
        }
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    return {"object": type(value).__name__}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


class _DiagnosticCapture(logging.Handler):
    """Capture WARNING+ logs and Python warnings without suppressing failures."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[dict[str, str]] = []
        self._warnings_context = None
        self._caught_warnings = None
        self._other_handler_levels: list[tuple[logging.Handler, int]] = []
        self._capture_loggers: list[logging.Logger] = []
        self._previous_disable_level = logging.root.manager.disable

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage()[:1000],
        })

    def __enter__(self):
        root = logging.getLogger()
        # A full-pool run can legitimately collect thousands of diagnostics.
        # Keep them in the artifacts without duplicating them on stderr.
        named_loggers = [
            logger for logger in logging.root.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        ]
        all_loggers = [root, *named_loggers]
        handlers: list[logging.Handler] = []
        seen_handler_ids = set()
        for logger in all_loggers:
            for handler in list(logger.handlers):
                if id(handler) in seen_handler_ids:
                    continue
                seen_handler_ids.add(id(handler))
                handlers.append(handler)
        self._other_handler_levels = [
            (handler, handler.level) for handler in handlers]
        for handler, _ in self._other_handler_levels:
            handler.setLevel(logging.CRITICAL + 1)
        # Disable only DEBUG/INFO records. WARNING+ still reaches this capture.
        self._previous_disable_level = logging.root.manager.disable
        logging.disable(logging.INFO)
        self._capture_loggers = [
            logger for logger in all_loggers
            if logger is root or not logger.propagate
        ]
        for logger in self._capture_loggers:
            logger.addHandler(self)
        self._warnings_context = warnings.catch_warnings(record=True)
        self._caught_warnings = self._warnings_context.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, exc_type, exc, tb):
        for logger in self._capture_loggers:
            logger.removeHandler(self)
        for handler, level in self._other_handler_levels:
            handler.setLevel(level)
        logging.disable(self._previous_disable_level)
        if self._warnings_context is not None:
            self._warnings_context.__exit__(exc_type, exc, tb)
        for item in self._caught_warnings or []:
            self.records.append({
                "level": "WARNING",
                "logger": "python.warnings",
                "message": str(item.message)[:1000],
            })
        return False


def _read_json_object(path: Path, label: str) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return value


def _input_identity(snapshot: Path, registry_path: Path, ledger_path: Path,
                    registry: dict, ledger: dict) -> dict:
    source_root = Path(__file__).resolve().parent
    engine_files = sorted(source_root.rglob("*.py"))
    engine_hashes = {
        path.relative_to(source_root).as_posix(): _file_sha256(path)
        for path in engine_files
    }
    return {
        "probe_schema_version": PROBE_SCHEMA_VERSION,
        "python_version": sys.version.split()[0],
        "probe_source": {
            "path": Path(__file__).name,
            "sha256": _file_sha256(Path(__file__).resolve()),
        },
        "engine_source": {
            "scope": "Playersim/**/*.py",
            "file_count": len(engine_hashes),
            "sha256": _canonical_hash({"files": engine_hashes}),
        },
        "snapshot": {
            "path": snapshot.name,
            "size_bytes": snapshot.stat().st_size,
            "sha256": _file_sha256(snapshot),
        },
        "registry": {
            "path": registry_path.name,
            "cards": len(registry.get("cards", [])),
            "sha256": _file_sha256(registry_path),
            "declared_sha256": registry.get("sha256"),
        },
        "ledger": {
            "path": ledger_path.name,
            "cards": len(ledger.get("cards", [])),
            "sha256": _file_sha256(ledger_path),
            "declared_sha256": ledger.get("sha256"),
        },
    }


def select_pool_cards(
        raw_cards: Sequence[dict], registry: dict, ledger: dict, *,
        card_names: Iterable[str] = (), statuses: Iterable[str] = (),
        from_index: int | None = None, to_index: int | None = None,
        shard_index: int = 0, shard_count: int = 1) -> list[dict]:
    """Join and deterministically select pool cards by canonical registry index."""
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    if from_index is not None and from_index < 0:
        raise ValueError("from_index must be non-negative")
    if to_index is not None and to_index < 0:
        raise ValueError("to_index must be non-negative")
    if from_index is not None and to_index is not None and from_index > to_index:
        raise ValueError("from_index cannot exceed to_index")

    wanted_names = {str(name).casefold() for name in card_names if str(name).strip()}
    wanted_statuses = {str(status).strip() for status in statuses if str(status).strip()}
    registry_by_name = {
        str(row.get("name", "")).casefold(): row
        for row in registry.get("cards", [])
    }
    ledger_by_name = {
        str(row.get("name", "")).casefold(): row
        for row in ledger.get("cards", [])
    }
    selected = []
    seen_names = set()
    for raw in raw_cards:
        name = str(raw.get("name", ""))
        key = name.casefold()
        entry = registry_by_name.get(key)
        if entry is None:
            raise ValueError(f"Pool card absent from registry: {name}")
        ledger_row = ledger_by_name.get(key)
        if ledger_row is None:
            raise ValueError(f"Pool card absent from support ledger: {name}")
        index = int(entry["index"])
        status = str(ledger_row.get("status", "unknown"))
        if wanted_names and key not in wanted_names:
            continue
        if wanted_statuses and status not in wanted_statuses:
            continue
        if from_index is not None and index < from_index:
            continue
        if to_index is not None and index > to_index:
            continue
        if index % shard_count != shard_index:
            continue
        selected.append({
            "index": index,
            "name": name,
            "oracle_id": entry.get("oracle_id"),
            "ledger_status": status,
            "ledger_issues": copy.deepcopy(ledger_row.get("issues", []) or []),
            "raw": raw,
        })
        seen_names.add(key)
    missing = sorted(wanted_names - seen_names)
    if missing:
        raise ValueError("Requested card(s) not present in selected pool: " + ", ".join(missing))
    selected.sort(key=lambda row: row["index"])
    return selected


def _slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return (value or "card")[:80]


def _console_safe(value: Any, stream=None) -> str:
    """Render arbitrary card names through a narrow Windows code page safely."""
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return str(value).encode(
        encoding, errors="backslashreplace").decode(encoding)


def _card_artifact_path(output: Path, entry: dict) -> Path:
    return output / "cards" / f"{int(entry['index']):05d}-{_slug(entry['name'])}.json"


def _oracle_sha256(entry: dict) -> str:
    """Hash the exact frozen oracle row used to construct the runtime Card."""
    return _canonical_hash({"oracle_row": entry["raw"]})


def _result_status(obligations: Sequence[dict], issues: Sequence[dict]) -> str:
    if any(issue.get("severity") == "failed" for issue in issues):
        return "failed"
    if any(obligation.get("status") == "failed" for obligation in obligations):
        return "failed"
    if any(obligation.get("status") != "exercised" for obligation in obligations):
        return "coverage_gap"
    if issues:
        return "coverage_gap"
    return "execution_passed"


def _fixture_card(name: str, type_line: str, oracle_text: str = "",
                  mana_cost: str = "", **extra) -> Card:
    data = {
        "name": name,
        "layout": "normal",
        "mana_cost": mana_cost,
        "cmc": extra.pop("cmc", 0),
        "type_line": type_line,
        "oracle_text": oracle_text,
        "keywords": extra.pop("keywords", []),
        "legalities": {},
    }
    # Card.reset_to_printed() attempts numeric conversion whenever these keys
    # are present.  Omitting absent values keeps neutral noncreature fixtures
    # from emitting false WARNING diagnostics on every probe.
    for field in ("power", "toughness", "loyalty", "defense"):
        value = extra.pop(field, None)
        if value is not None:
            data[field] = value
    data.update(extra)
    return Card(data)


def _empty_player(game_state: GameState, seed_id: int, number: int) -> dict:
    player = game_state._init_player([seed_id] * 8, number)
    for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
        player[zone] = []
    player["life"] = 100
    return player


def _stage(game_state: GameState, player: dict, card_id: int, zone: str) -> None:
    player[zone].append(card_id)
    game_state._last_card_locations[card_id] = (player, zone)


def _build_probe_state(entry: dict, *, target_zone: str,
                       include_mana_lands: bool = True):
    """Build one isolated neutral state and return (gs, handler, fixture data)."""
    target_id = 1_000_000 + int(entry["index"])
    target = Card(copy.deepcopy(entry["raw"]))
    target.card_id = target_id
    cards: dict[int, Card] = {target_id: target}
    next_id = target_id + 1

    land_ids = []
    if include_mana_lands:
        basics = (
            ("Plains", "W"), ("Island", "U"), ("Swamp", "B"),
            ("Mountain", "R"), ("Forest", "G"), ("Wastes", "C"),
        )
        for basic, symbol in basics:
            for copy_index in range(FIXTURE_LAND_COPIES_PER_COLOR):
                card = _fixture_card(
                    f"Probe {basic} {copy_index + 1}",
                    f"Basic Land - {basic}", f"{{T}}: Add {{{symbol}}}.")
                card.card_id = next_id
                cards[next_id] = card
                land_ids.append(next_id)
                next_id += 1

    # These intentionally blank permanents provide both friendly and hostile
    # target categories without introducing their own abilities.
    own_fixture_ids = []
    opponent_fixture_ids = []
    permanent_specs = (
        ("Creature", {"power": 3, "toughness": 3}),
        ("Artifact Creature - Construct", {"power": 2, "toughness": 2}),
        ("Enchantment", {}),
        ("Planeswalker", {"loyalty": 5}),
        ("Land", {}),
    )
    for owner_label, destination in (
            ("Friendly", own_fixture_ids), ("Opposing", opponent_fixture_ids)):
        for fixture_index, (type_line, extra) in enumerate(permanent_specs):
            card = _fixture_card(
                f"{owner_label} Probe Permanent {fixture_index + 1}",
                type_line, **extra)
            card.card_id = next_id
            cards[next_id] = card
            destination.append(next_id)
            next_id += 1

    filler_ids = []
    for filler_index in range(40):
        type_line = "Creature - Test" if filler_index % 2 else "Instant"
        extra = ({"power": 1, "toughness": 1}
                 if "Creature" in type_line else {})
        card = _fixture_card(
            f"Probe Filler {filler_index + 1}", type_line,
            "", mana_cost="{1}", cmc=1, **extra)
        card.card_id = next_id
        cards[next_id] = card
        filler_ids.append(next_id)
        next_id += 1

    game_state = GameState(cards)
    # The simulation limit is an observation/action convenience, not an MTG
    # rule.  Leave space for token-producing effects beyond the neutral target
    # fixtures while keeping the probed source in the fixed action window.
    game_state.max_battlefield = 100
    game_state.p1 = _empty_player(game_state, target_id, 1)
    game_state.p2 = _empty_player(game_state, target_id, 2)
    # __init__ runs this while p1/p2 are still None.  Run it again now so
    # effect code sees the same per-player tracking contract as a reset game.
    game_state._init_tracking_variables()
    game_state.turn = 1
    game_state.phase = game_state.PHASE_MAIN_PRECOMBAT
    game_state.previous_priority_phase = None
    game_state.priority_player = game_state.p1
    game_state.priority_pass_count = 0
    game_state.agent_is_p1 = True
    game_state.mulligan_in_progress = False
    game_state.bottoming_in_progress = False
    game_state.mulligan_player = None
    game_state.bottoming_player = None
    game_state.card_instance_printings[target_id] = int(entry["index"])
    game_state.card_instance_owners[target_id] = "p1"
    game_state.cards_played = {0: [], 1: []}
    game_state.play_history = {0: {}, 1: {}}
    game_state.opening_hands = {"p1": [], "p2": []}
    game_state.draw_history = {"p1": {}, "p2": {}}
    game_state.terminal_reason = None

    _stage(game_state, game_state.p1, target_id, target_zone)
    if target_zone == "battlefield":
        target_types = set(getattr(target, "card_types", []) or [])
        if "planeswalker" in target_types:
            game_state.p1["loyalty_counters"][target_id] = int(
                getattr(target, "loyalty", 0) or 0)
        if "battle" in target_types:
            game_state.battle_cards[target_id] = int(
                getattr(target, "defense", 0) or 0)
    for card_id in land_ids:
        _stage(game_state, game_state.p1, card_id, "battlefield")
    # The target is always index 0 when staged on the battlefield, so it stays
    # in the fixed activated-action window even though targets may extend past
    # that window.
    for card_id in own_fixture_ids:
        _stage(game_state, game_state.p1, card_id, "battlefield")
    for card_id in opponent_fixture_ids:
        _stage(game_state, game_state.p2, card_id, "battlefield")
    for card_id in filler_ids[:2]:
        _stage(game_state, game_state.p1, card_id, "hand")
    for card_id in filler_ids[2:4]:
        _stage(game_state, game_state.p2, card_id, "hand")
    for card_id in filler_ids[4:10]:
        _stage(game_state, game_state.p1, card_id, "graveyard")
    for card_id in filler_ids[10:16]:
        _stage(game_state, game_state.p2, card_id, "graveyard")
    for card_id in filler_ids[16:28]:
        _stage(game_state, game_state.p1, card_id, "library")
    for card_id in filler_ids[28:40]:
        _stage(game_state, game_state.p2, card_id, "library")

    handler = ActionHandler(game_state)
    game_state.action_handler = handler
    game_state.combat_action_handler = handler.combat_handler
    return game_state, handler, {
        "target_id": target_id,
        "land_ids": land_ids,
        "own_fixture_ids": own_fixture_ids,
        "opponent_fixture_ids": opponent_fixture_ids,
        "filler_ids": filler_ids,
        "initial_card_ids": sorted(cards, key=str),
    }


def _sync_policy(game_state: GameState) -> None:
    priority = game_state.priority_player or game_state.p1
    game_state.priority_player = priority
    game_state.agent_is_p1 = priority is game_state.p1


def _state_payload(game_state: GameState) -> dict:
    players = []
    for label, player in (("p1", game_state.p1), ("p2", game_state.p2)):
        mutable = {
            key: value for key, value in player.items()
            if key not in {
                "library", "hand", "battlefield", "graveyard", "exile",
                "life", "tapped_permanents", "mana_pool", "name",
            }
        }
        players.append({
            "label": label,
            "life": player.get("life"),
            "zones": {
                zone: list(player.get(zone, []))
                for zone in ("library", "hand", "battlefield", "graveyard", "exile")
            },
            "tapped": sorted(player.get("tapped_permanents", set()), key=str),
            "mana": dict(player.get("mana_pool", {})),
            # Include every other player-owned rules tracker in the mask-purity
            # digest. This covers loyalty/damage counters, attachments, snow
            # and conditional mana, land/activation limits, and mechanic maps.
            "mutable": _json_safe(mutable),
        })
    card_state = {}
    live_ids = {
        card_id for player in (game_state.p1, game_state.p2)
        for zone in ("hand", "battlefield", "graveyard", "exile")
        for card_id in player.get(zone, [])
    }
    for card_id in sorted(live_ids, key=str):
        card = game_state._safe_get_card(card_id)
        if card is not None:
            card_state[str(card_id)] = {
                "name": getattr(card, "name", None),
                "face": getattr(card, "current_face", None),
                "counters": dict(getattr(card, "counters", {}) or {}),
                "power": getattr(card, "power", None),
                "toughness": getattr(card, "toughness", None),
            }
    return {
        "turn": game_state.turn,
        "phase": game_state.phase,
        "priority": "p1" if game_state.priority_player is game_state.p1 else "p2",
        "players": players,
        "stack": _json_safe(game_state.stack),
        "targeting_context": _json_safe(game_state.targeting_context),
        "sacrifice_context": _json_safe(game_state.sacrifice_context),
        "choice_context": _json_safe(game_state.choice_context),
        "cards": card_state,
    }


def _state_digest(game_state: GameState) -> str:
    return _canonical_hash(_state_payload(game_state))


def _state_delta(before: dict, after: dict) -> dict:
    """Compact, deterministic, human-inspectable rules-state evidence."""
    before_players = {row["label"]: row for row in before.get("players", [])}
    after_players = {row["label"]: row for row in after.get("players", [])}
    life = {}
    zones = {}
    tapped = {}
    mana = {}
    player_state = {}
    for label in ("p1", "p2"):
        old = before_players.get(label, {})
        new = after_players.get(label, {})
        life[label] = {"before": old.get("life"), "after": new.get("life")}
        zone_changes = {}
        for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
            old_ids = set(old.get("zones", {}).get(zone, []))
            new_ids = set(new.get("zones", {}).get(zone, []))
            added = sorted(new_ids - old_ids, key=str)
            removed = sorted(old_ids - new_ids, key=str)
            if added or removed:
                zone_changes[zone] = {"added": added, "removed": removed}
        if zone_changes:
            zones[label] = zone_changes
        old_tapped = set(old.get("tapped", []))
        new_tapped = set(new.get("tapped", []))
        tapped[label] = {
            "became_tapped": sorted(new_tapped - old_tapped, key=str),
            "became_untapped": sorted(old_tapped - new_tapped, key=str),
        }
        old_mana = old.get("mana", {}) or {}
        new_mana = new.get("mana", {}) or {}
        mana_changes = {
            symbol: {"before": old_mana.get(symbol, 0),
                     "after": new_mana.get(symbol, 0)}
            for symbol in sorted(set(old_mana) | set(new_mana))
            if old_mana.get(symbol, 0) != new_mana.get(symbol, 0)
        }
        if mana_changes:
            mana[label] = mana_changes
        old_mutable = old.get("mutable", {}) or {}
        new_mutable = new.get("mutable", {}) or {}
        mutable_changes = {
            key: {"before": old_mutable.get(key), "after": new_mutable.get(key)}
            for key in sorted(set(old_mutable) | set(new_mutable))
            if old_mutable.get(key) != new_mutable.get(key)
        }
        if mutable_changes:
            player_state[label] = mutable_changes
    card_changes = {}
    before_cards = before.get("cards", {}) or {}
    after_cards = after.get("cards", {}) or {}
    for card_id in sorted(set(before_cards) | set(after_cards), key=str):
        old = before_cards.get(card_id)
        new = after_cards.get(card_id)
        if old != new:
            card_changes[card_id] = {"before": old, "after": new}
    return {
        "life": life,
        "zones": zones,
        "tapped": tapped,
        "mana": mana,
        "player_state": player_state,
        "card_characteristics": card_changes,
        "final": {
            "stack_depth": len(after.get("stack", [])),
            "targeting_pending": after.get("targeting_context") is not None,
            "sacrifice_pending": after.get("sacrifice_context") is not None,
            "choice_pending": after.get("choice_context") is not None,
        },
    }


def _fidelity_snapshot(game_state: GameState) -> dict:
    return _json_safe(dict(getattr(game_state, "fidelity_counters", {}) or {}))


def _fidelity_changes(before: dict, after: dict) -> dict:
    keys = sorted(set(before) | set(after))
    return {key: {"before": before.get(key), "after": after.get(key)}
            for key in keys if before.get(key) != after.get(key)}


def _nonzero_fidelity(snapshot: dict) -> dict:
    def meaningful(value: Any) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return bool(value)
        if isinstance(value, dict):
            return any(meaningful(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(meaningful(item) for item in value)
        return bool(value)
    return {key: value for key, value in snapshot.items()
            if meaningful(value)}


def _assert_fidelity_clean(before: dict, game_state: GameState,
                           surface: str) -> None:
    after = _fidelity_snapshot(game_state)
    baseline = _nonzero_fidelity(before)
    final = _nonzero_fidelity(after)
    changes = _fidelity_changes(before, after)
    if baseline or final or changes:
        raise AssertionError(
            f"{surface} has nonzero/changed fidelity telemetry: "
            f"{_json_safe({'baseline': baseline, 'final': final, 'changes': changes})}")


def _zone_invariant_issues(
        game_state: GameState, expected_card_ids: Sequence[Any] = ()) -> list[str]:
    issues = []
    occurrences: dict[Any, list[str]] = {}
    for label, player in (("p1", game_state.p1), ("p2", game_state.p2)):
        for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
            for card_id in player.get(zone, []):
                occurrences.setdefault(card_id, []).append(f"{label}.{zone}")
    for card_id, locations in occurrences.items():
        if len(locations) != 1:
            issues.append(f"card {card_id} occurs in multiple zones: {locations}")
    for card_id in expected_card_ids:
        if card_id not in occurrences:
            issues.append(f"original card {card_id} disappeared from all zones")
    return issues


def _generate_mask(handler, *, sync_policy: bool = True):
    if sync_policy:
        _sync_policy(handler.game_state)
    before = _state_digest(handler.game_state)
    mask = handler.generate_valid_actions()
    after = _state_digest(handler.game_state)
    if before != after:
        raise AssertionError("action-mask generation mutated rules state")
    if getattr(handler, "last_mask_error", None):
        raise RuntimeError(f"action mask degraded: {handler.last_mask_error}")
    return mask


def _apply_public_action(handler, action_index: int) -> dict:
    mask = _generate_mask(handler)
    if not 0 <= action_index < len(mask) or not bool(mask[action_index]):
        raise AssertionError(f"action {action_index} was not mask-valid")
    handler.current_valid_actions = mask
    reward, done, truncated, info = handler.apply_action(action_index)
    if info.get("execution_failed") or info.get("critical_error") \
            or info.get("invalid_action") or "invalid_action_reason" in info:
        raise RuntimeError(
            f"mask-valid action {action_index} failed: {_json_safe(info)}")
    if getattr(handler, "last_handler_error", None):
        raise RuntimeError(f"action handler degraded: {handler.last_handler_error}")
    return {
        "action": action_index,
        "action_type": handler.get_action_info(action_index)[0],
        "reward": reward,
        "done": bool(done),
        "truncated": bool(truncated),
    }


def _drive_public_continuation(
        handler, *, max_decisions: int,
        choice_plan: Sequence[int] = (),
        decision_state: dict | None = None) -> list[dict]:
    """Finish one public path, optionally replaying a planned choice prefix.

    ``choice_plan`` contains action indices for successive decisions that have
    more than one semantic option.  Callers can rebuild the same surface from
    a fresh fixture and use a different prefix to obtain independent branch
    evidence.  ``decision_state`` is shared across staged setup calls so one
    activated surface has a single stable branch ordinal sequence.
    """
    game_state = handler.game_state
    trace = []
    seen = set()
    if decision_state is None:
        decision_state = {"branch_ordinal": 0, "selected_prefix": []}
    for _ in range(max_decisions):
        pending = any((game_state.targeting_context, game_state.sacrifice_context,
                       game_state.choice_context))
        if not pending and not game_state.stack:
            return trace
        mask = _generate_mask(handler)
        valid = [index for index, allowed in enumerate(mask) if bool(allowed)]
        signature = (
            game_state.phase, len(game_state.stack),
            _state_digest(game_state), tuple(valid))
        if signature in seen:
            raise RuntimeError("public continuation repeated an identical state")
        seen.add(signature)
        decision_kind = None
        decision_payload = None
        if game_state.targeting_context:
            decision_kind = "targeting"
            decision_payload = game_state.targeting_context
        elif game_state.sacrifice_context:
            decision_kind = "sacrifice"
            decision_payload = game_state.sacrifice_context
        elif game_state.choice_context:
            decision_kind = str(
                game_state.choice_context.get("type", "choice"))
            decision_payload = game_state.choice_context
        utility_actions = {12, 224, 479}
        if decision_kind is None:
            # Outside a staged decision, PASS is priority plumbing. During a
            # decision, action 11 can be the semantic "finish/choose none"
            # branch and must remain visible in coverage evidence.
            utility_actions.add(11)
        semantic_actions = [
            index for index in valid if index not in utility_actions]
        semantic_options = [{
            "action": index,
            "action_type": handler.get_action_info(index)[0],
            "reason": str(getattr(
                handler, "action_reasons_with_context", {}).get(
                    index, {}).get("reason", ""))[:200],
        } for index in semantic_actions]
        decision_context_summary = _json_safe({
            key: value for key, value in (decision_payload or {}).items()
            if key not in {"player", "controller", "game_state",
                           "effect_continuation", "parent_choice"}
        })
        branch_ordinal = None
        branch_prefix = None
        if decision_kind is not None and len(semantic_actions) > 1:
            branch_ordinal = int(decision_state["branch_ordinal"])
            branch_prefix = list(decision_state["selected_prefix"])
        planned_action = None
        if (branch_ordinal is not None
                and branch_ordinal < len(choice_plan)):
            planned_action = int(choice_plan[branch_ordinal])
            if planned_action not in semantic_actions:
                raise AssertionError(
                    f"choice plan action {planned_action} is not semantic at "
                    f"branch {branch_ordinal}: {semantic_actions}")
        if planned_action is not None:
            action = planned_action
        elif not pending and game_state.stack and 11 in valid:
            action = 11
        else:
            non_terminal = [index for index in valid if index not in (11, 12)]
            if non_terminal:
                action = non_terminal[0]
            elif 11 in valid:
                action = 11
            else:
                raise RuntimeError(
                    f"pending continuation has no non-concede action: {valid}")
        step = _apply_public_action(handler, action)
        if decision_kind is not None:
            step["decision"] = {
                "kind": decision_kind,
                "semantic_options": semantic_options,
                "selected_action": action,
                "context_summary": decision_context_summary,
            }
            if len(semantic_actions) > 1:
                step["decision"]["branch_ordinal"] = branch_ordinal
                step["decision"]["branch_prefix"] = branch_prefix
                step["decision"]["unvisited_branch_count"] = \
                    len(semantic_actions) - 1
                decision_state["branch_ordinal"] = branch_ordinal + 1
                decision_state["selected_prefix"].append(action)
        trace.append(step)
    raise RuntimeError(f"public continuation exceeded {max_decisions} decisions")


def _assert_choice_plan_consumed(
        choice_plan: Sequence[int], decision_state: dict) -> None:
    consumed = int(decision_state.get("branch_ordinal", 0))
    if consumed < len(choice_plan):
        raise AssertionError(
            f"choice plan reached only {consumed} of {len(choice_plan)} "
            "planned branch decisions")


def _path_choice_nodes(path: dict) -> list[dict]:
    nodes = []
    for step in path.get("trace", []) or []:
        decision = step.get("decision") or {}
        options = list(decision.get("semantic_options", []) or [])
        if len(options) < 2:
            continue
        nodes.append({
            "decision": decision,
            "prefix": tuple(int(action) for action in
                            decision.get("branch_prefix", []) or []),
            "options": options,
        })
    return nodes


def _choice_replay_plans(
        path: dict, matched_surface: str,
        scheduled_edges: set[tuple[str, tuple[int, ...], int]]) \
        -> list[tuple[int, ...]]:
    """Return fresh-fixture plans for every newly discovered branch edge."""
    if path.get("status") != "exercised":
        return []
    nodes = _path_choice_nodes(path)
    for node in nodes:
        selected = int(node["decision"]["selected_action"])
        scheduled_edges.add((matched_surface, node["prefix"], selected))
    plans = []
    for node in nodes:
        selected = int(node["decision"]["selected_action"])
        for option in node["options"]:
            action = int(option["action"])
            edge = (matched_surface, node["prefix"], action)
            if action == selected or edge in scheduled_edges:
                continue
            scheduled_edges.add(edge)
            plans.append(node["prefix"] + (action,))
    return plans


def _choice_branch_obligations(paths: Sequence[dict]) -> list[dict]:
    """Aggregate independent edge replays for base-surface choice nodes.

    Replay paths provide evidence for alternatives discovered on the canonical
    surface path.  Choices that arise only after taking an alternate edge are
    not expanded into an accidental Cartesian product; mechanics with
    interacting choices must declare an explicit combination obligation.
    """
    nodes: dict[tuple[str, tuple[int, ...]], dict] = {}
    for path in paths:
        if path.get("kind") == "choice_replay":
            matched_surface = path.get("matched_surface")
        elif (path.get("kind") in {
                "primary", "activated", "mana_ability", "triggered"}
              and not str(path.get("id", "")).endswith("negative_mask")):
            matched_surface = path.get("id")
        else:
            continue
        if not matched_surface:
            continue
        for node in _path_choice_nodes(path):
            decision = node["decision"]
            key = (str(matched_surface), node["prefix"])
            if path.get("kind") == "choice_replay" and key not in nodes:
                continue
            aggregate = nodes.setdefault(key, {
                "matched_surface": str(matched_surface),
                "branch_prefix": list(node["prefix"]),
                "decision_kind": decision.get("kind"),
                "options": {},
                "visited_actions": set(),
                "evidence_paths": [],
                "base_selected_action": None,
            })
            for option in node["options"]:
                aggregate["options"][int(option["action"])] = option
            if path.get("status") == "exercised":
                selected = int(decision["selected_action"])
                aggregate["visited_actions"].add(selected)
                aggregate["evidence_paths"].append(path.get("id"))
                if path.get("id") == matched_surface:
                    aggregate["base_selected_action"] = selected

    obligations = []
    surface_ordinals: dict[str, int] = {}
    for aggregate in nodes.values():
        surface = aggregate["matched_surface"]
        ordinal = surface_ordinals.get(surface, 0)
        surface_ordinals[surface] = ordinal + 1
        option_actions = set(aggregate["options"])
        visited_actions = set(aggregate["visited_actions"])
        unvisited_actions = sorted(option_actions - visited_actions)
        base_selected = aggregate["base_selected_action"]
        replayed_actions = sorted(
            visited_actions - ({base_selected} if base_selected is not None else set()))
        obligation = {
            "id": f"choice:{surface}:{ordinal}",
            "kind": "choice_branch",
            "matched_surface": surface,
            "decision_kind": aggregate["decision_kind"],
            "branch_prefix": aggregate["branch_prefix"],
            "status": "exercised" if not unvisited_actions else "coverage_gap",
            "selected_action": base_selected,
            "semantic_options": [
                aggregate["options"][action]
                for action in sorted(aggregate["options"])
            ],
            "visited_actions": sorted(visited_actions),
            "replayed_actions": replayed_actions,
            "unvisited_actions": unvisited_actions,
            "unvisited_branch_count": len(unvisited_actions),
            "evidence_paths": aggregate["evidence_paths"],
        }
        if unvisited_actions:
            obligation["reason"] = (
                "bounded branch replay did not independently complete every "
                "discovered mask-valid semantic choice")
        obligations.append(obligation)
    return obligations


def _reconcile_typed_choice_obligations(obligations: list[dict]) -> None:
    """Bind typed static discovery to completed public branch evidence.

    Keep this intentionally narrow.  A raw ``you may`` is not sufficient:
    only a parsed public decision kind with every discovered edge exercised may
    discharge its matching static obligation.
    """
    optional_rows = [
        row for row in obligations
        if row.get("kind") == "optional_branch"
        and row.get("status") == "coverage_gap"
    ]
    as_enters_rows = [
        row for row in obligations
        if row.get("kind") == "choice_branch"
        and row.get("decision_kind") == "as_enters_pay_life"
        and row.get("status") == "exercised"
    ]
    if len(as_enters_rows) != 1:
        return
    choice = as_enters_rows[0]
    if len(optional_rows) == 1:
        optional = optional_rows[0]
        optional["status"] = "exercised"
        optional.pop("reason", None)
        optional["matched_choice_obligation"] = choice["id"]
        optional["evidence_paths"] = list(choice.get("evidence_paths", []))
    conditional_rows = [
        row for row in obligations
        if row.get("kind") == "conditional_branch"
        and row.get("status") == "coverage_gap"
        and "if you don't" in str(row.get("clause", "")).casefold()
        and "enters tapped" in str(row.get("clause", "")).casefold()
    ]
    if len(conditional_rows) == 1:
        conditional = conditional_rows[0]
        conditional["status"] = "exercised"
        conditional.pop("reason", None)
        conditional["matched_choice_obligation"] = choice["id"]
        conditional["evidence_paths"] = list(
            choice.get("evidence_paths", []))


def _primary_actions(handler, target_id: int, *,
                     sync_policy: bool = True) -> list[int]:
    mask = _generate_mask(handler, sync_policy=sync_policy)
    matches = []
    for index, allowed in enumerate(mask):
        if not bool(allowed):
            continue
        action_type, _ = handler.get_action_info(index)
        if action_type not in {"PLAY_LAND", "PLAY_SPELL"}:
            continue
        metadata = getattr(handler, "action_reasons_with_context", {}).get(index, {})
        context = metadata.get("context", {}) or {}
        if context.get("card_id") == target_id:
            matches.append(index)
    return matches


def _without_parenthetical_reminder_text(text: str) -> str:
    """Remove balanced-enough parenthetical reminder clauses for discovery.

    Reminder text explains an already discovered keyword action; treating each
    ``you may`` or ``{X}`` inside it as another independent branch creates
    obligations that no public rules decision can ever expose.
    """
    result = str(text or "")
    while True:
        stripped = re.sub(r"\([^()]*\)", " ", result)
        if stripped == result:
            return stripped
        result = stripped


def _is_permission_may(text: str, match_end: int) -> bool:
    tail = text[match_end:]
    return bool(re.match(
        r"\s+(?:play|cast|look at|activate)\b", tail, re.IGNORECASE))


def _has_announced_x_choice(mana_cost: str, oracle_text: str) -> bool:
    if re.search(r"\{X\}", str(mana_cost or ""), re.IGNORECASE):
        return True
    text = _without_parenthetical_reminder_text(oracle_text)
    if re.search(r"\bpay\s+\{?X\}?\b", text, re.IGNORECASE):
        return True
    return bool(re.search(
        r"(?:^|\n)[^\n:]*\{X\}[^\n:]*:", text, re.IGNORECASE))


def _find_primary_action(handler, target_id: int) -> int | None:
    matches = _primary_actions(handler, target_id)
    return matches[0] if len(matches) == 1 else None


def _mana_cost_requires_payment(cost: str) -> bool:
    for symbol in re.findall(r"\{([^}]+)\}", cost or ""):
        upper = symbol.upper()
        if upper == "X":
            continue
        if upper.isdigit() and int(upper) == 0:
            continue
        return True
    return False


def _normalized_trigger_condition(text: str, entry: dict) -> str:
    """Normalize lexical and runtime trigger subjects for reconciliation."""
    normalized = str(text or "").casefold()
    normalized = normalized.replace("’", "'").replace("\u2014", " ")
    normalized = normalized.replace("\u2013", " ")
    normalized = re.sub(r"\([^()]*\)", " ", normalized)
    normalized = re.sub(r"[^a-z0-9']+", " ", normalized)
    normalized = " ".join(normalized.split())

    raw = entry.get("raw", {}) or {}
    names = [str(entry.get("name", "") or "")]
    names.extend(
        str(face.get("name", "") or "")
        for face in (raw.get("card_faces") or raw.get("faces") or [])
        if isinstance(face, dict))
    expanded_names = set()
    for name in names:
        if not name:
            continue
        expanded_names.add(name)
        expanded_names.add(name.split("//", 1)[0])
        expanded_names.add(name.split(",", 1)[0])
    for name in sorted(expanded_names, key=len, reverse=True):
        name_normalized = re.sub(
            r"[^a-z0-9']+", " ", name.casefold()).strip()
        if name_normalized:
            normalized = re.sub(
                rf"\b{re.escape(name_normalized)}\b", "this", normalized)

    normalized = normalized.replace("whenever ", "when ")
    normalized = normalized.replace("enters the battlefield", "enters")
    normalized = normalized.replace("comes into play", "enters")
    normalized = normalized.replace("you've", "you have")
    normalized = re.sub(
        r"\bthis (?:artifact|aura|battle|card|class|creature|enchantment|"
        r"land|permanent|room|vehicle)\b", "this", normalized)
    return " ".join(normalized.split())


def _trigger_condition_match_score(
        printed_condition: str, runtime_condition: str, entry: dict) -> float:
    printed = _normalized_trigger_condition(printed_condition, entry)
    runtime = _normalized_trigger_condition(runtime_condition, entry)
    if not printed or not runtime:
        return 0.0
    if printed == runtime:
        return 1.0
    if printed in runtime or runtime in printed:
        shorter = min(len(printed), len(runtime))
        longer = max(len(printed), len(runtime))
        if shorter >= 12:
            return 0.88 + 0.1 * (shorter / longer)
    printed_tokens = printed.split()
    runtime_tokens = runtime.split()
    if printed_tokens[0] != runtime_tokens[0]:
        return 0.0
    printed_set = set(printed_tokens)
    runtime_set = set(runtime_tokens)
    union = printed_set | runtime_set
    return (len(printed_set & runtime_set) / len(union)) if union else 0.0


def _reconcile_printed_triggers(
        entry: dict, obligations: list[dict], discovered: list[dict]) -> dict:
    """Match independent lexical trigger clauses to registered runtime ones."""
    inventory = discover_printed_trigger_inventory(entry)
    runtime_rows = [
        row for row in discovered if row.get("class") == "TriggeredAbility"]
    unmatched_runtime_ids = {row["id"] for row in runtime_rows}
    printed_matches = {}

    for printed in inventory.get("triggers", []):
        candidates = []
        for runtime in runtime_rows:
            if runtime["id"] not in unmatched_runtime_ids:
                continue
            score = _trigger_condition_match_score(
                printed.get("trigger_condition_prefix", ""),
                runtime.get("trigger_condition", ""), entry)
            candidates.append((score, runtime["id"]))
        best_score, matched_id = max(candidates, default=(0.0, None))
        if best_score >= 0.72 and matched_id is not None:
            unmatched_runtime_ids.remove(matched_id)
            printed_matches[printed["id"]] = matched_id
            runtime = next(
                row for row in runtime_rows if row["id"] == matched_id)
            runtime.setdefault("printed_trigger_ids", []).append(printed["id"])
            runtime.setdefault("printed_match_scores", {})[
                printed["id"]] = round(best_score, 6)
        else:
            obligation_id = f"printed_trigger:{printed['sha256'][:24]}"
            obligations.append({
                "id": obligation_id,
                "kind": "printed_trigger",
                "status": "coverage_gap",
                "reason": (
                    "printed trigger clause did not reconcile to any "
                    "registered runtime TriggeredAbility"),
                "printed_trigger_id": printed["id"],
                "face_index": printed.get("face_index"),
                "discovery": printed.get("discovery"),
                "trigger_condition_prefix": printed.get(
                    "trigger_condition_prefix"),
            })
        discovered.append({
            "id": f"printed:{printed['sha256'][:24]}",
            "class": "printed_trigger",
            "printed_trigger_id": printed["id"],
            "matched_surface": printed_matches.get(printed["id"]),
            "face_index": printed.get("face_index"),
            "face_name": printed.get("face_name"),
            "discovery": printed.get("discovery"),
            "trigger_condition": printed.get("trigger_condition_prefix"),
            "source_text": printed.get("source_text"),
            "sha256": printed.get("sha256"),
        })

    for unmatched in inventory.get("unmatched_lexical_surfaces", []):
        discovered.append({
            "id": f"printed_lexeme:{unmatched['sha256'][:24]}",
            "class": "unmatched_trigger_lexeme",
            "reason": unmatched.get("reason"),
            "face_index": unmatched.get("face_index"),
            "source_text": unmatched.get("source_text"),
            "sha256": unmatched.get("sha256"),
        })
        if unmatched.get("reason") == "reminder_text":
            continue
        obligations.append({
            "id": f"printed_trigger_lexeme:{unmatched['sha256'][:24]}",
            "kind": "printed_trigger_lexeme",
            "status": "coverage_gap",
            "reason": (
                "trigger lexeme outside a recognized printed ability boundary "
                "requires manual classification"),
            "lexeme_reason": unmatched.get("reason"),
            "face_index": unmatched.get("face_index"),
        })

    return {
        "schema_version": inventory.get("schema_version"),
        "sha256": inventory.get("sha256"),
        "printed_trigger_count": len(inventory.get("triggers", [])),
        "matched_printed_trigger_count": len(printed_matches),
        "unmatched_printed_trigger_count": (
            len(inventory.get("triggers", [])) - len(printed_matches)),
        "unmatched_lexeme_count": len(
            inventory.get("unmatched_lexical_surfaces", [])),
    }


def _discover_obligations(entry: dict) -> tuple[list[dict], list[dict], dict]:
    """Discover every registered surface before bounded dynamic probing."""
    game_state, _, fixture = _build_probe_state(
        entry, target_zone="battlefield", include_mana_lands=False)
    target_id = fixture["target_id"]
    fidelity_before = _fidelity_snapshot(game_state)
    game_state.ability_handler.register_card_abilities(target_id, game_state.p1)
    abilities = list(game_state.ability_handler.registered_abilities.get(target_id, []))
    primary_kind = ("land" if "land" in str(entry["raw"].get(
        "type_line", "")).lower() else "spell")
    obligations = [
        {"id": "primary", "kind": primary_kind, "status": "pending"},
        {
            "id": "primary:negative_mask", "kind": "negative_mask",
            "status": "pending",
            "matched_surface": "primary",
        },
    ]
    discovered = []
    ledger_status = str(entry.get("ledger_status", "unknown"))
    ledger_issues = list(entry.get("ledger_issues", []) or [])
    for issue_index, issue in enumerate(ledger_issues):
        reason = (issue.get("reason") if isinstance(issue, dict)
                  else str(issue))
        obligations.append({
            "id": f"ledger_evidence:{issue_index}",
            "kind": "static_preflight_evidence",
            "status": (
                "failed" if ledger_status in {"partial", "unparsed", "crash"}
                else "coverage_gap"),
            "reason": str(reason or "support ledger recorded an issue")[:500],
            "ledger_severity": (
                issue.get("severity") if isinstance(issue, dict) else None),
        })
    if ledger_status in {"partial", "unparsed", "crash"} and not ledger_issues:
        obligations.append({
            "id": "ledger_evidence:status", "kind": "static_preflight_evidence",
            "status": "failed",
            "reason": (
                f"support ledger status is {ledger_status} "
                "without structured issue details"),
        })
    elif ledger_status not in {"verified", "observed_clean", "partial",
                               "unparsed", "crash"}:
        obligations.append({
            "id": "ledger_evidence:status", "kind": "static_preflight_evidence",
            "status": "coverage_gap",
            "reason": (
                f"support ledger status is {ledger_status}; static preflight "
                "evidence is not complete"),
        })
    activated_index = triggered_index = static_index = 0
    for ability in abilities:
        if isinstance(ability, (ActivatedAbility, ManaAbility)):
            obligation_id = f"activated:{activated_index}"
            activated_index += 1
            kind = "mana_ability" if isinstance(ability, ManaAbility) else "activated"
        elif isinstance(ability, TriggeredAbility):
            obligation_id = f"triggered:{triggered_index}"
            triggered_index += 1
            kind = "triggered"
        elif isinstance(ability, StaticAbility):
            obligation_id = f"static:{static_index}"
            static_index += 1
            kind = "static"
        else:
            obligation_id = f"ability:{len(discovered)}"
            kind = type(ability).__name__
        obligations.append({
            "id": obligation_id,
            "kind": kind,
            "status": "coverage_gap",
            "reason": "bounded probing has not dynamically exercised this registered ability",
        })
        if kind == "triggered":
            obligations.append({
                "id": f"{obligation_id}:negative_event",
                "kind": "negative_event",
                "matched_surface": obligation_id,
                "status": "coverage_gap",
                "reason": (
                    "matched trigger non-event proof has not been exercised"),
            })
        if kind in {"activated", "mana_ability"}:
            obligations.append({
                "id": f"{obligation_id}:negative_mask",
                "kind": "negative_mask",
                "matched_surface": obligation_id,
                "status": "coverage_gap",
                "reason": "matched activated-ability legality proof is pending",
            })
        discovered.append({
            "id": obligation_id,
            "class": type(ability).__name__,
            "cost": getattr(ability, "cost", None),
            "effect": getattr(ability, "effect", getattr(ability, "effect_text", None)),
            "trigger_condition": getattr(ability, "trigger_condition", None),
        })
    printed_trigger_summary = _reconcile_printed_triggers(
        entry, obligations, discovered)
    replacement_effects = [
        effect for effect in getattr(
            game_state.replacement_effects, "active_effects", [])
        if effect.get("source_id") == target_id
        or effect.get("source_card_id") == target_id
    ]
    for index, effect in enumerate(replacement_effects):
        obligations.append({
            "id": f"replacement:{index}", "kind": "replacement",
            "status": "coverage_gap",
            "reason": "replacement surface requires an independent event fixture",
        })
        discovered.append({
            "id": f"replacement:{index}", "class": "replacement",
            "event_type": effect.get("event_type"),
        })
    faces = entry["raw"].get("card_faces") or entry["raw"].get("faces") or []
    for face_index in range(1, len(faces)):
        obligations.append({
            "id": f"alternate_face:{face_index}", "kind": "alternate_face",
            "status": "coverage_gap",
            "reason": "alternate face is not the primary cast/land surface",
        })
    layout = str(entry["raw"].get("layout", "normal"))
    if layout != "normal":
        obligations.append({
            "id": f"layout:{layout}", "kind": "layout",
            "status": "coverage_gap",
            "reason": "non-normal layout requires a dedicated dynamic path",
        })
    card = game_state._safe_get_card(target_id)
    spree_modes = list(getattr(card, "spree_modes", []) or [])
    for mode_index, mode in enumerate(spree_modes):
        obligations.append({
            "id": f"mode:spree:{mode_index}", "kind": "mode",
            "status": "coverage_gap",
            "reason": "distinct mode requires its own branch",
        })
        discovered.append({
            "id": f"mode:spree:{mode_index}", "class": "spree_mode",
            "mode": _json_safe(mode),
        })
    # Spree permits any non-empty combination of its independently costed
    # modes. Singletons are represented above; every larger combination is a
    # distinct obligation even though v1 takes only one deterministic branch.
    for size in range(2, len(spree_modes) + 1):
        for combination in itertools.combinations(range(len(spree_modes)), size):
            suffix = "-".join(str(index) for index in combination)
            obligations.append({
                "id": f"mode:spree_combo:{suffix}",
                "kind": "mode_combination", "mode_indices": list(combination),
                "status": "coverage_gap",
                "reason": "distinct Spree mode combination was not replayed",
            })

    oracle_text = str(entry["raw"].get("oracle_text", "") or "")
    oracle_surfaces = []
    if faces:
        for face_index, face in enumerate(faces):
            face_text = str(face.get("oracle_text", "") or "")
            if face_text:
                oracle_surfaces.append((f"face:{face_index}", face_text))
    elif oracle_text:
        oracle_surfaces.append(("front", oracle_text))
    combined_oracle_text = "\n".join(text for _, text in oracle_surfaces)
    modal_modes, modal_min, modal_max = \
        game_state.ability_handler._parse_modal_text(oracle_text)
    modal_modes = list(modal_modes or [])
    if modal_modes:
        for mode_index, mode_text in enumerate(modal_modes):
            obligations.append({
                "id": f"mode:modal:{mode_index}", "kind": "mode",
                "status": "coverage_gap",
                "reason": "ordinary modal branch was not independently replayed",
            })
            discovered.append({
                "id": f"mode:modal:{mode_index}", "class": "modal_mode",
                "mode": mode_text, "min_choices": modal_min,
                "max_choices": modal_max,
            })
        maximum = min(len(modal_modes), int(modal_max or 0))
        minimum = max(0, int(modal_min or 0))
        for size in range(max(2, minimum), maximum + 1):
            for combination in itertools.combinations(
                    range(len(modal_modes)), size):
                suffix = "-".join(str(index) for index in combination)
                obligations.append({
                    "id": f"mode:modal_combo:{suffix}",
                    "kind": "mode_combination",
                    "mode_indices": list(combination),
                    "status": "coverage_gap",
                    "reason": "ordinary modal combination was not replayed",
                })
        if minimum == 0:
            obligations.append({
                "id": "mode:modal_combo:none", "kind": "mode_combination",
                "mode_indices": [], "status": "coverage_gap",
                "reason": "zero-mode branch was not independently replayed",
            })
    elif re.search(r"\b(?:choose one or both|one or both)\b", oracle_text,
                   re.IGNORECASE):
        obligations.append({
            "id": "mode:one_or_both:unparsed", "kind": "mode_combination",
            "status": "coverage_gap",
            "reason": "one-or-both surface was discoverable but not parsed into modes",
        })

    optional_index = 0
    conditional_index = 0
    for surface_id, surface_text in oracle_surfaces:
        actionable_text = _without_parenthetical_reminder_text(surface_text)
        for match in re.finditer(r"\byou may\b", actionable_text,
                                 re.IGNORECASE):
            if _is_permission_may(actionable_text, match.end()):
                continue
            obligations.append({
                "id": f"choice:optional:{optional_index}",
                "kind": "optional_branch", "status": "coverage_gap",
                "reason": (
                    "optional yes/no branch was not independently replayed"),
                "oracle_surface": surface_id,
                "text_offset": match.start(),
            })
            optional_index += 1
        for match in re.finditer(
                r"\b(?:if|unless)\b[^.\n]*(?:\.|$)", actionable_text,
                re.IGNORECASE):
            clause = " ".join(match.group(0).split())
            if not clause:
                continue
            obligations.append({
                "id": f"condition:oracle:{conditional_index}",
                "kind": "conditional_branch",
                "status": "coverage_gap",
                "reason": (
                    "printed conditional outcome requires independent true "
                    "and false event fixtures"),
                "oracle_surface": surface_id,
                "clause": clause[:500],
                "clause_sha256": hashlib.sha256(
                    clause.encode("utf-8")).hexdigest(),
            })
            conditional_index += 1
    if _has_announced_x_choice(
            str(entry["raw"].get("mana_cost", "")),
            combined_oracle_text or oracle_text):
        obligations.append({
            "id": "choice:variable_x", "kind": "variable_choice",
            "status": "coverage_gap",
            "reason": "multiple legal X values were not independently replayed",
        })
    fidelity_after = _fidelity_snapshot(game_state)
    return obligations, discovered, {
        "baseline_nonzero": _nonzero_fidelity(fidelity_before),
        "final_nonzero": _nonzero_fidelity(fidelity_after),
        "changes": _fidelity_changes(fidelity_before, fidelity_after),
        "printed_trigger_inventory": printed_trigger_summary,
    }


def _probe_primary(
        entry: dict, *, max_decisions: int,
        choice_plan: Sequence[int] = (), path_id: str = "primary",
        path_kind: str = "primary", matched_surface: str | None = None) \
        -> tuple[dict, list[dict]]:
    issues = []
    path = {
        "id": path_id, "kind": path_kind, "status": "coverage_gap",
        "choice_plan": list(choice_plan),
    }
    if matched_surface is not None:
        path["matched_surface"] = matched_surface
    game_state, handler, fixture = _build_probe_state(
        entry, target_zone="hand", include_mana_lands=True)
    target_id = fixture["target_id"]
    before_fidelity = _fidelity_snapshot(game_state)
    before_state = _state_payload(game_state)
    decision_state = {"branch_ordinal": 0, "selected_prefix": []}
    try:
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{path_id} baseline")
        action = _find_primary_action(handler, target_id)
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{path_id} mask")
        if action is None:
            path["reason"] = (
                "primary card action was not uniquely exposed by the public mask")
            return path, issues
        path["action"] = action
        trace = [_apply_public_action(handler, action)]
        trace.extend(_drive_public_continuation(
            handler, max_decisions=max_decisions,
            choice_plan=choice_plan, decision_state=decision_state))
        _assert_choice_plan_consumed(choice_plan, decision_state)
        path["trace"] = trace
        target_card = game_state._safe_get_card(target_id)
        requires_payment = _mana_cost_requires_payment(
            getattr(target_card, "mana_cost", ""))
        tapped_lands = sorted(
            set(fixture["land_ids"]).intersection(
                game_state.p1.get("tapped_permanents", set())))
        path["payment"] = {
            "requires_mana": requires_payment,
            "auto_tapped_land_ids": tapped_lands,
        }
        if requires_payment and not tapped_lands:
            raise AssertionError("nonzero primary cost committed without auto-tapping a fixture land")
        invariant_issues = _zone_invariant_issues(
            game_state, fixture["initial_card_ids"])
        if invariant_issues:
            raise AssertionError("; ".join(invariant_issues[:10]))
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{path_id} path")
        path["status"] = "exercised"
        path["state_before_sha256"] = _canonical_hash(before_state)
        after_state = _state_payload(game_state)
        path["state_after_sha256"] = _canonical_hash(after_state)
        path["state_delta"] = _state_delta(before_state, after_state)
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        issues.append({
            "severity": "failed", "surface": path_id,
            "reason": f"{type(exc).__name__}: {exc}",
        })
    return path, issues


def _probe_primary_negative(entry: dict) -> tuple[dict, list[dict]]:
    """Matched legality check: the same card must disappear without priority."""
    issues = []
    path = {
        "id": "primary:negative_mask", "kind": "negative_mask",
        "matched_surface": "primary", "status": "coverage_gap",
        "invalid_condition": "controller does not have priority",
    }
    game_state, handler, fixture = _build_probe_state(
        entry, target_zone="hand", include_mana_lands=True)
    target_id = fixture["target_id"]
    before_state = _state_payload(game_state)
    before_fidelity = _fidelity_snapshot(game_state)
    try:
        _assert_fidelity_clean(
            before_fidelity, game_state, "primary negative baseline")
        positive_control = _find_primary_action(handler, target_id)
        _assert_fidelity_clean(
            before_fidelity, game_state, "primary negative positive-control mask")
        path["positive_control_action"] = positive_control
        if positive_control is None:
            path["reason"] = (
                "matched negative could not establish a unique mask-valid "
                "primary positive control in the same fixture")
            return path, issues
        game_state.priority_player = game_state.p2
        # Hold the observation perspective on P1. This proves P1's exact card
        # action is hidden while P2 has priority, rather than merely inspecting
        # P2's unrelated hand.
        game_state.agent_is_p1 = True
        exposed = _primary_actions(handler, target_id, sync_policy=False)
        _assert_fidelity_clean(
            before_fidelity, game_state, "primary negative mask")
        path["exposed_actions"] = exposed
        if exposed:
            raise AssertionError(
                f"primary action remained mask-valid without controller priority: {exposed}")
        invariant_issues = _zone_invariant_issues(
            game_state, fixture["initial_card_ids"])
        if invariant_issues:
            raise AssertionError("; ".join(invariant_issues[:10]))
        path["status"] = "exercised"
        path["state_sha256"] = _state_digest(game_state)
        path["state_delta"] = _state_delta(
            before_state, _state_payload(game_state))
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        issues.append({
            "severity": "failed", "surface": path["id"],
            "reason": f"{type(exc).__name__}: {exc}",
        })
    return path, issues


def _fund_until_activated(handler, land_ids: Sequence[int], *,
                          target_id: int, ability_index: int,
                          max_decisions: int,
                          choice_plan: Sequence[int] = (),
                          decision_state: dict | None = None) \
        -> tuple[dict | None, list[dict], list[int]]:
    """Tap real lands only until the requested public activation appears."""
    game_state = handler.game_state
    trace = []
    mana_setup_land_ids = []
    dispatch = _find_activated_dispatch(
        handler, target_id=target_id, ability_index=ability_index)
    if dispatch is not None:
        return dispatch, trace, mana_setup_land_ids
    for land_id in land_ids:
        if land_id not in game_state.p1.get("battlefield", []):
            continue
        battlefield_index = game_state.p1["battlefield"].index(land_id)
        if battlefield_index >= 20:
            continue
        action = 68 + battlefield_index
        mask = _generate_mask(handler)
        if not bool(mask[action]):
            continue
        action_type, _ = handler.get_action_info(action)
        if action_type != "TAP_LAND_FOR_MANA":
            raise AssertionError(
                f"fixture mana action {action} mapped to {action_type}")
        trace.append(_apply_public_action(handler, action))
        mana_setup_land_ids.append(land_id)
        trace.extend(_drive_public_continuation(
            handler, max_decisions=max_decisions,
            choice_plan=choice_plan, decision_state=decision_state))
        dispatch = _find_activated_dispatch(
            handler, target_id=target_id, ability_index=ability_index)
        if dispatch is not None:
            return dispatch, trace, mana_setup_land_ids
    return None, trace, mana_setup_land_ids


def _find_activated_dispatch(handler, *, target_id: int,
                             ability_index: int,
                             sync_policy: bool = True) -> dict | None:
    """Find a fixed or overflow-catalog public action for one ability."""
    game_state = handler.game_state
    if target_id not in game_state.p1.get("battlefield", []):
        return None
    battlefield_index = game_state.p1["battlefield"].index(target_id)
    mask = _generate_mask(handler, sync_policy=sync_policy)
    for action, allowed in enumerate(mask):
        if not bool(allowed):
            continue
        action_type, _ = handler.get_action_info(action)
        metadata = getattr(
            handler, "action_reasons_with_context", {}).get(action, {})
        context = metadata.get("context", {}) or {}
        if (action_type == "ACTIVATE_ABILITY"
                and context.get("battlefield_idx") == battlefield_index
                and context.get("ability_idx") == ability_index):
            return {"kind": "fixed", "action": action}
        if action != 479 or not context.get("open_action_catalog"):
            continue
        for option_index, option in enumerate(context.get("options", [])):
            option_context = option.get("action_context", {}) or {}
            if (option.get("handler") == "activate_ability"
                    and option_context.get("battlefield_idx") == battlefield_index
                    and option_context.get("ability_idx") == ability_index):
                return {
                    "kind": "catalog", "action": action,
                    "option_index": option_index,
                }
    return None


def _apply_activated_dispatch(handler, dispatch: dict) -> list[dict]:
    trace = []
    if dispatch["kind"] == "fixed":
        trace.append(_apply_public_action(handler, dispatch["action"]))
        return trace
    trace.append(_apply_public_action(handler, dispatch["action"]))
    absolute = int(dispatch["option_index"])
    desired_page = absolute // 10
    for _ in range(desired_page):
        trace.append(_apply_public_action(handler, 479))
    selection = 353 + (absolute % 10)
    trace.append(_apply_public_action(handler, selection))
    return trace


def _trigger_source_subject_pattern(entry: dict) -> str:
    """Return a regex for Oracle self references used by trigger subjects."""
    name = str(entry.get("name", "") or "").strip().casefold()
    raw = entry.get("raw", {}) or {}
    names = {name, name.split("//", 1)[0], name.split(",", 1)[0]}
    names.update(
        str(face.get("name", "") or "").strip().casefold()
        for face in (raw.get("card_faces") or raw.get("faces") or [])
        if isinstance(face, dict))
    expanded = set()
    for value in names:
        if not value:
            continue
        expanded.update({
            value,
            value.split("//", 1)[0].strip(),
            value.split(",", 1)[0].strip(),
        })
    named = "|".join(
        re.escape(value) for value in sorted(
            expanded - {""}, key=len, reverse=True))
    this_subject = (
        r"this(?:\s+(?!or\b)[a-z0-9'-]+)+")
    return rf"(?:{this_subject}|{named})" if named else this_subject


def _self_or_another_trigger_criteria(
        entry: dict, trigger_condition: str,
        verb_pattern: str) -> str | None:
    """Extract the non-source arm of ``this/name or another`` triggers."""
    condition = str(trigger_condition or "").strip().casefold()
    subject = _trigger_source_subject_pattern(entry)
    match = re.match(
        rf"^when(?:ever)?\s+{subject}\s+or\s+another\s+"
        rf"(?P<criteria>.+?)\s+you control"
        rf"(?P<qualifiers>(?:\s+(?:with|without)\s+.+?)?)"
        rf"\s+{verb_pattern}\b",
        condition)
    if match is None:
        return None
    criteria = match.group("criteria").strip(" ,")
    qualifiers = (match.group("qualifiers") or "").strip()
    return " ".join(value for value in (criteria, qualifiers) if value)


def _controlled_trigger_criteria(
        trigger_condition: str, verb_pattern: str) -> str | None:
    """Extract characteristics watched before a controlled permanent event."""
    condition = str(trigger_condition or "").strip().casefold()
    match = re.match(
        r"^when(?:ever)?\s+"
        r"(?:(?:a|an|another|one or more)\s+)?"
        r"(?P<criteria>.+?)\s+you control"
        r"(?P<qualifiers>(?:\s+(?:with|without)\s+.+?)?)"
        rf"\s+{verb_pattern}\b",
        condition)
    if match is None:
        return None
    criteria = match.group("criteria").strip(" ,")
    qualifiers = (match.group("qualifiers") or "").strip()
    return " ".join(value for value in (criteria, qualifiers) if value)


def _normalized_trigger_fixture_criteria(criteria: str) -> str:
    normalized = " ".join(str(criteria or "permanent").casefold().split())
    normalized = re.sub(r"^(?:a|an|another|one or more|other)\s+", "", normalized)
    for plural, singular in (
            ("creatures", "creature"), ("artifacts", "artifact"),
            ("enchantments", "enchantment"), ("lands", "land"),
            ("permanents", "permanent"),
            ("planeswalkers", "planeswalker")):
        normalized = re.sub(rf"\b{plural}\b", singular, normalized)
    return normalized.strip(" ,") or "permanent"


def _trigger_fixture_details(criteria: str) -> list[dict] | None:
    """Build isolated neutral permanents that satisfy each criteria arm.

    ``None`` is deliberate: an unmodelled qualifier must remain a coverage gap
    instead of turning zero delivery into a false controller-scope success.
    """
    normalized = _normalized_trigger_fixture_criteria(criteria)
    if any(phrase in normalized for phrase in (
            "of the chosen type", "greatest power among",
            "least power among", "equipped creature", "enchanted creature",
            "saddled creature", "that died", "died this turn",
            "was cast", "entered from")):
        return None

    protected = normalized.replace("power or toughness", "power_and_toughness")
    alternatives = [
        value.replace("power_and_toughness", "power or toughness").strip()
        for value in re.split(
            r"\s+(?:and/or|or)\s+"
            r"(?!(?:less|greater|more|fewer|equal)\b)", protected)
        if value.strip()]
    shared = [
        word for word in (
            "nontoken", "nonland", "noncreature", "nonartifact",
            "legendary", "nonlegendary", "basic", "nonbasic",
            "attacking", "face-down")
        if re.search(rf"\b{re.escape(word)}\b", normalized)]
    if len(alternatives) > 1:
        alternatives = [
            " ".join([*(word for word in shared if word not in value), value])
            for value in alternatives]

    card_type_words = {
        "artifact", "battle", "creature", "enchantment", "land",
        "permanent", "planeswalker",
    }
    artifact_subtypes = {
        "clue", "equipment", "food", "fortification", "map",
        "spacecraft", "treasure", "vehicle",
    }
    color_map = {
        "white": "W", "blue": "U", "black": "B",
        "red": "R", "green": "G",
    }
    results = []
    for alternative in alternatives or [normalized]:
        words = set(re.findall(r"[a-z]+", alternative))
        if "historic" in words:
            fixture_index = 1
        elif "land" in words:
            fixture_index = 4
        elif "enchantment" in words:
            fixture_index = 2
        elif "planeswalker" in words:
            fixture_index = 3
        elif "artifact" in words:
            fixture_index = 1
        else:
            fixture_index = 0

        detail = {
            "event_fixture_index": fixture_index,
            "event_fixture_criteria": alternative,
            "criteria_match_staged": True,
        }
        if "attacking" in words:
            detail["event_fixture_attacking"] = True
        if {"face", "down"}.issubset(words):
            detail["event_fixture_face_down"] = True
        if "tapped" in words and "untapped" not in words:
            detail["event_fixture_tapped"] = True
        if "token" in words and "nontoken" not in words:
            detail["event_fixture_is_token"] = True
        elif "nontoken" in words:
            detail["event_fixture_is_token"] = False

        counters = {}
        if "modified" in words or "with a counter" in alternative:
            counters["+1/+1"] = 1
        counter_match = re.search(
            r"(?:with|and) (?:an?|one or more) "
            r"([+\-/\w]+) counters?", alternative)
        if counter_match:
            counters[counter_match.group(1)] = 1
        if counters:
            detail["event_fixture_counters"] = counters

        keywords = [
            keyword for keyword in Card.ALL_KEYWORDS
            if re.search(
                rf"\bwith\s+(?:[^,;]+\s+and\s+)?"
                rf"{re.escape(keyword)}\b", alternative)]
        if keywords:
            detail["event_fixture_keywords"] = sorted(set(keywords))

        requested_colors = {
            symbol for word, symbol in color_map.items() if word in words}
        if "multicolored" in words and len(requested_colors) < 2:
            requested_colors.update({"W", "U"})
        elif "monocolored" in words and not requested_colors:
            requested_colors.add("W")
        if requested_colors:
            detail["event_fixture_colors"] = sorted(requested_colors)
        elif "colorless" in words:
            detail["event_fixture_colors"] = []

        relative_pt = "toughness greater than its power" in alternative
        if relative_pt:
            detail.update({
                "event_fixture_power": 2,
                "event_fixture_toughness": 3,
            })
        dual = re.search(
            r"power or toughness (?:is )?(\d+)"
            r"(?: or (less|greater))?", alternative)
        if dual:
            bound = int(dual.group(1))
            detail.update({
                "event_fixture_power": bound,
                "event_fixture_toughness": bound,
            })
        for comparison in re.finditer(
                r"(mana value|power|toughness) (?:is )?(\d+)"
                r"(?: or (less|greater))?", alternative):
            field, raw_bound, _ = comparison.groups()
            key = ("event_fixture_cmc" if field == "mana value"
                   else f"event_fixture_{field}")
            detail[key] = int(raw_bound)
        if "odd mana value" in alternative:
            detail["event_fixture_cmc"] = 1
        elif "even mana value" in alternative:
            detail["event_fixture_cmc"] = 2

        excluded = {
            match.group(1).rstrip("s") for match in re.finditer(
                r"\bnon[- ]([a-z]+)\b", alternative)}
        grammar = {
            "a", "an", "and", "another", "artifact", "attacking",
            "basic", "black", "blue", "colorless", "counter", "counters",
            "creature", "down", "enchantment", "even", "face", "greater",
            "green", "historic", "is", "its", "land", "legendary", "less",
            "mana", "modified", "monocolored", "more", "multicolored",
            "no", "nonartifact", "nonbasic", "noncreature", "nonland",
            "nonlegendary", "nontoken", "odd", "of", "on", "one", "or",
            "permanent", "planeswalker", "power", "red", "snow", "tapped",
            "than", "the", "token", "toughness", "untapped", "value",
            "white", "with", "without", "it",
        } | card_type_words | excluded
        grammar.update(re.findall(
            r"[a-z]+", " ".join(keywords)))
        grammar.update(re.findall(r"[a-z]+", " ".join(counters)))
        subtype_words = [
            word.rstrip("s") for word in re.findall(r"[a-z]+", alternative)
            if not word.isdigit() and word not in grammar
            and word.rstrip("s") not in grammar]
        subtype_words = list(dict.fromkeys(subtype_words))

        explicit_types = {
            word.rstrip("s") for word in words
            if word.rstrip("s") in card_type_words}
        if subtype_words:
            base = ("Artifact" if set(subtype_words) & artifact_subtypes
                    else "Creature")
            detail["event_fixture_type_line"] = (
                f"{base} - {' '.join(word.title() for word in subtype_words)}")
            fixture_index = 1 if base == "Artifact" else 0
            detail["event_fixture_index"] = fixture_index
        elif explicit_types == {"artifact"}:
            detail["event_fixture_type_line"] = "Artifact"
        elif explicit_types == {"creature"}:
            pass
        elif explicit_types == {"artifact", "creature"}:
            detail["event_fixture_type_line"] = "Artifact Creature"

        type_line = detail.get("event_fixture_type_line")
        if "legendary" in words and "nonlegendary" not in words:
            type_line = type_line or (
                "Artifact" if detail["event_fixture_index"] == 1
                else "Creature")
            detail["event_fixture_type_line"] = f"Legendary {type_line}"
        if "basic" in words and "nonbasic" not in words:
            type_line = detail.get("event_fixture_type_line", "Land")
            detail["event_fixture_type_line"] = f"Basic {type_line}"

        label = re.sub(r"[^a-z0-9]+", "_", alternative).strip("_")
        detail["event_fixture_label"] = label or "permanent"
        results.append(detail)
    return results


def _trigger_has_dying_object_counter_intervening_if(
        entry: dict, trigger_condition: str) -> bool:
    """Return whether this death trigger requires counters on its subject."""
    condition = str(trigger_condition or "").strip().casefold()
    if not re.search(r"\bd(?:ie|ies)\b", condition):
        return False
    raw = entry.get("raw", {}) or {}
    texts = [str(raw.get("oracle_text", "") or "")]
    texts.extend(
        str(face.get("oracle_text", "") or "")
        for face in (raw.get("card_faces") or raw.get("faces") or [])
        if isinstance(face, dict))
    for line in "\n".join(texts).splitlines():
        match = re.search(
            r"(?P<prefix>\bwhen(?:ever)?\b.+?\bd(?:ie|ies))\s*,\s*"
            r"if it had counters on it\b",
            line, re.IGNORECASE)
        if (match is not None and _trigger_condition_match_score(
                match.group("prefix"), condition, entry) >= 0.72):
            return True
    return False


def _with_trigger_event_requirements(
        entry: dict, trigger_condition: str, spec: dict) -> dict:
    """Add deterministic state required by a trigger's intervening condition."""
    decorated = dict(spec)
    if (_trigger_has_dying_object_counter_intervening_if(
            entry, trigger_condition)
            and decorated.get("event_type") == "DIES"
            and not decorated.get("unsupported_reason")):
        counters = dict(decorated.get("event_fixture_counters", {}) or {})
        counters.setdefault("+1/+1", 1)
        decorated["event_fixture_counters"] = counters
        decorated["event_fixture_intervening_condition"] = (
            "if it had counters on it")
    return decorated


def _opponent_matching_trigger_fixtures(
        criteria: str, verb: str, event_type: str) -> list[dict]:
    """Stage every criteria arm with controller as its intended mismatch."""
    normalized = _normalized_trigger_fixture_criteria(criteria)
    details = _trigger_fixture_details(normalized)
    if not details:
        return []
    fixtures = []
    for raw_detail in details:
        detail = dict(raw_detail)
        label = detail.pop("event_fixture_label", None) or "permanent"
        alternative = str(
            detail.get("event_fixture_criteria", normalized))
        fixtures.append({
            **detail,
            "event_fixture_criteria": alternative,
            "kind": "other_etb" if verb == "enters" else "other_dies",
            "event_type": event_type,
            "fixture_owner": "opponent",
            "negative_arm": f"opponent_{label}_{verb}",
            "negative_case": f"opponent {alternative} {verb}",
            "controller_is_only_intended_mismatch": True,
        })
    return fixtures


def _opponent_matching_trigger_fixture(
        criteria: str, verb: str, event_type: str) -> dict | None:
    """Compatibility helper returning the first isolated criteria arm."""
    fixtures = _opponent_matching_trigger_fixtures(
        criteria, verb, event_type)
    return fixtures[0] if fixtures else None


def _trigger_fixture_spec(entry: dict, trigger_condition: str) -> dict | None:
    """Select a deterministic real-event fixture for common trigger shapes.

    Unsupported or context-heavy triggers remain explicit coverage gaps.  A
    supported fixture always enters through the same event API used by normal
    gameplay; the probe never calls the triggered effect directly.
    """
    condition = str(trigger_condition or "").strip().casefold()
    name = str(entry.get("name", "") or "").strip().casefold()
    raw = entry.get("raw", {}) or {}
    type_line = str(raw.get("type_line", "") or "").casefold()
    oracle_text = str(raw.get("oracle_text", "") or "").casefold()
    short_name = name.split("//", 1)[0].split(",", 1)[0].strip()
    names = [value for value in {name, short_name} if value]
    named_subject = "|".join(
        re.escape(value) for value in sorted(names, key=len, reverse=True))
    self_subject = (
        r"(?:this\s+(?:artifact|aura|battle|creature|enchantment|land|"
        r"permanent|vehicle|card|class)"
        + (rf"|{named_subject}" if named_subject else "") + r")")

    if re.match(
            rf"^when(?:ever)?\s+{self_subject}\s+enters\b", condition):
        spec = {
            "kind": "self_etb", "event_type": "ENTERS_BATTLEFIELD",
            "target_zone": "hand",
        }
        if "aura" in type_line:
            # A bare hand -> battlefield move is not a legal Aura fixture: it
            # immediately falls off and emits an attachment warning.  Support
            # only restrictions for which the neutral pool has an unambiguous
            # legal object, and carry that attachment through the real move.
            enchant_line = next((
                line.strip() for line in oracle_text.splitlines()
                if line.strip().startswith("enchant ")), "")
            if enchant_line.startswith("enchant creature you control") \
                    or enchant_line == "enchant creature":
                spec["attach_to_fixture"] = {"owner": "own", "index": 0}
            elif enchant_line == "enchant artifact":
                spec["attach_to_fixture"] = {"owner": "own", "index": 1}
            elif enchant_line == "enchant land":
                spec["attach_to_fixture"] = {"owner": "own", "index": 4}
            elif enchant_line in {"enchant permanent", "enchant permanent you control"}:
                spec["attach_to_fixture"] = {"owner": "own", "index": 0}
            else:
                return None
        return spec
    if "at the beginning of your upkeep" in condition:
        return {
            "kind": "phase_begin", "event_type": "BEGINNING_OF_UPKEEP",
            "phase": "PHASE_UPKEEP", "target_zone": "battlefield",
        }
    if "at the beginning of your end step" in condition:
        spec = {
            "kind": "phase_begin", "event_type": "BEGINNING_OF_END_STEP",
            "phase": "PHASE_END_STEP", "target_zone": "battlefield",
        }
        if "impending" in oracle_text:
            spec["activate_impending"] = True
        if name.startswith("haliya"):
            spec["setup_life_gain"] = 3
        return spec
    if "at the beginning of combat on your turn" in condition:
        return {
            "kind": "phase_begin", "event_type": "BEGINNING_OF_COMBAT",
            "phase": "PHASE_BEGIN_COMBAT", "target_zone": "battlefield",
        }
    if re.match(
            rf"^when(?:ever)?\s+{self_subject}\s+becomes tapped\b",
            condition):
        return {
            "kind": "self_tapped", "event_type": "TAPPED",
            "target_zone": "battlefield",
        }
    if ("attacks" in condition and re.match(
            rf"^when(?:ever)?\s+{self_subject}\b", condition)):
        if "creature" not in type_line:
            return None
        spec = {
            "kind": "self_attacks", "event_type": "ATTACKS",
            "target_zone": "battlefield",
        }
        if name.startswith("fear of missing out"):
            spec["setup_delirium"] = True
            spec["tap_fixture_index"] = 0
        return spec
    if re.match(
            rf"^when(?:ever)?\s+{self_subject}\s+dies\b", condition):
        return {
            "kind": "self_dies", "event_type": "DIES",
            "target_zone": "battlefield",
        }
    if re.search(
            r"\b(?:another|a)\s+(?:nonland\s+|nontoken\s+)?"
            r"creature you control dies\b", condition):
        return {
            "kind": "controlled_creature_dies", "event_type": "DIES",
            "event_fixture_index": 1, "target_zone": "battlefield",
        }
    if "land you control enters" in condition:
        return {
            "kind": "controlled_land_etb",
            "event_type": "ENTERS_BATTLEFIELD",
            "event_fixture_index": 4, "target_zone": "battlefield",
        }
    if "enchantment you control enters" in condition:
        return {
            "kind": "controlled_enchantment_etb",
            "event_type": "ENTERS_BATTLEFIELD",
            "event_fixture_index": 2, "target_zone": "battlefield",
        }
    if re.search(
            r"\b(?:another\s+)?creature(?:\s+or\s+artifact)? you control "
            r"enters\b", condition):
        return {
            "kind": "controlled_creature_etb",
            "event_type": "ENTERS_BATTLEFIELD",
            "event_fixture_index": 1, "target_zone": "battlefield",
        }
    if ("another creature you control with flying enters" in condition):
        return {
            "kind": "controlled_flying_creature_etb",
            "event_type": "ENTERS_BATTLEFIELD",
            "event_fixture_index": 0, "grant_flying": True,
            "target_zone": "battlefield",
        }
    return None


def _trigger_fixture_variants(entry: dict, trigger_condition: str) -> list[dict]:
    """Return every independently required event arm for one trigger.

    A single registered TriggeredAbility can encode several disjunctive event
    subjects.  One matching event is not evidence for its other arms, so the
    caller probes each returned fixture from a fresh state and aggregates the
    ability only when every arm completes.
    """
    condition = str(trigger_condition or "").strip().casefold()
    raw = entry.get("raw", {}) or {}
    type_line = str(raw.get("type_line", "") or "").casefold()

    for event_type, event_arm in (
            ("ENTERS_BATTLEFIELD", "grouped_etb_batch"),
            ("DIES", "grouped_dies_batch")):
        if _is_grouped_zone_change_trigger(trigger_condition, event_type):
            return [{
                "event_arm": event_arm,
                "unsupported_reason": (
                    "the grouped zone-change trigger requires a complete "
                    f"atomic {event_type} batch; no deterministic public "
                    "batch fixture is registered"),
            }]

    if "enters or attacks" in condition:
        etb = _trigger_fixture_spec(entry, trigger_condition)
        variants = []
        if etb is not None:
            variants.append({**etb, "event_arm": "self_enters"})
        else:
            variants.append({
                "event_arm": "self_enters",
                "unsupported_reason": (
                    "no deterministic real-event fixture is registered for "
                    "the self-entry arm"),
            })
        if "creature" in type_line:
            variants.append({
                "kind": "self_attacks", "event_type": "ATTACKS",
                "target_zone": "battlefield", "event_arm": "self_attacks",
            })
        else:
            variants.append({
                "event_arm": "self_attacks",
                "unsupported_reason": (
                    "the attack arm requires a legal animation or crew fixture"),
            })
        return variants

    if ("enchantment you control enters" in condition
            and "fully unlock a room" in condition):
        return [
            {
                "kind": "controlled_enchantment_etb",
                "event_type": "ENTERS_BATTLEFIELD",
                "event_fixture_index": 2,
                "target_zone": "battlefield",
                "event_arm": "controlled_enchantment_enters",
            },
            {
                "event_arm": "fully_unlock_room",
                "unsupported_reason": (
                    "no deterministic public Room full-unlock fixture is "
                    "registered for this trigger arm"),
            },
        ]

    trigger_clauses = re.split(
        r"\s+and\s+(?=(?:when|whenever|at)\b)", condition)
    if len(trigger_clauses) > 1:
        variants = []
        seen_arms = set()
        for clause_index, clause in enumerate(trigger_clauses):
            for spec in _trigger_fixture_variants(entry, clause):
                arm = str(spec.get("event_arm", "default"))
                if arm in seen_arms:
                    arm = f"clause_{clause_index + 1}_{arm}"
                seen_arms.add(arm)
                variants.append({**spec, "event_arm": arm})
        return variants

    for verb_pattern, verb_label, event_type, self_kind in (
            (r"enter(?:s)?", "enters", "ENTERS_BATTLEFIELD", "self_etb"),
            (r"d(?:ie|ies)", "dies", "DIES", "self_dies")):
        criteria = _self_or_another_trigger_criteria(
            entry, condition, verb_pattern)
        if criteria is None:
            continue
        variants = [{
            "kind": self_kind,
            "event_type": event_type,
            "target_zone": "hand" if self_kind == "self_etb" else "battlefield",
            "event_arm": f"self_{verb_label}",
        }]
        details = _trigger_fixture_details(criteria)
        if details is None:
            variants.append({
                "event_arm": f"another_{verb_label}",
                "unsupported_reason": (
                    "the non-source event arm has qualifiers for which no "
                    "isolated deterministic fixture is registered"),
            })
            return [
                _with_trigger_event_requirements(
                    entry, condition, spec)
                for spec in variants]
        for detail in details:
            label = detail.pop("event_fixture_label")
            variants.append({
                **detail,
                "kind": ("controlled_permanent_etb"
                         if event_type == "ENTERS_BATTLEFIELD"
                         else "controlled_creature_dies"),
                "event_type": event_type,
                "target_zone": "battlefield",
                "event_arm": f"another_{label}_{verb_label}",
            })
        return [
            _with_trigger_event_requirements(entry, condition, spec)
            for spec in variants]

    for verb_pattern, verb_label, event_type in (
            (r"enter(?:s)?", "enters", "ENTERS_BATTLEFIELD"),
            (r"d(?:ie|ies)", "dies", "DIES")):
        criteria = _controlled_trigger_criteria(condition, verb_pattern)
        if criteria is None:
            continue
        details = _trigger_fixture_details(criteria)
        if details is None:
            return [{
                "event_arm": f"controlled_{verb_label}",
                "unsupported_reason": (
                    "the controlled event has qualifiers for which no isolated "
                    "deterministic fixture is registered"),
            }]
        variants = [{
            **{key: value for key, value in detail.items()
               if key != "event_fixture_label"},
            "kind": ("controlled_permanent_etb"
                     if event_type == "ENTERS_BATTLEFIELD"
                     else "controlled_creature_dies"),
            "event_type": event_type,
            "target_zone": "battlefield",
            "event_arm": (
                f"controlled_{detail['event_fixture_label']}_{verb_label}"),
        } for detail in details]
        return [
            _with_trigger_event_requirements(entry, condition, spec)
            for spec in variants]

    spec = _trigger_fixture_spec(entry, trigger_condition)
    if spec is None:
        return [{
            "event_arm": "default",
            "unsupported_reason": (
                "no deterministic real-event fixture is registered for this "
                "trigger condition"),
        }]
    return [_with_trigger_event_requirements(
        entry, condition, {**spec, "event_arm": spec["kind"]})]


def _trigger_negative_fixture_spec_single(
        entry: dict, trigger_condition: str) -> dict | None:
    """Select one close, real event that must not deliver this trigger."""
    condition = str(trigger_condition or "").strip().casefold()
    name = str(entry.get("name", "") or "").strip().casefold()
    short_name = name.split("//", 1)[0].split(",", 1)[0].strip()
    names = [value for value in {name, short_name} if value]

    def named_self_before(verb_pattern: str) -> bool:
        return any(re.match(
            rf"^when(?:ever)?\s+{re.escape(value)}\s+{verb_pattern}\b",
            condition) for value in names)

    if ("enchantment you control enters" in condition
            and "fully unlock a room" in condition):
        return {
            "kind": "other_etb", "event_type": "ENTERS_BATTLEFIELD",
            "fixture_owner": "opponent", "event_fixture_index": 2,
            "negative_case": "opponent enchantment enters",
        }
    compound_etb = _self_or_another_trigger_criteria(
        entry, condition, r"enter(?:s)?")
    if compound_etb is not None:
        return _opponent_matching_trigger_fixture(
            compound_etb, "enters", "ENTERS_BATTLEFIELD")
    if re.search(
            r"^whenever\s+.+?\s+or\s+another creature or artifact "
            r"you control enters\b", condition):
        return {
            "kind": "other_etb", "event_type": "ENTERS_BATTLEFIELD",
            "fixture_owner": "opponent", "event_fixture_index": 2,
            "negative_case": "opponent noncreature enchantment enters",
        }
    if "enters or attacks" in condition:
        return {
            "kind": "other_etb", "event_type": "ENTERS_BATTLEFIELD",
            "fixture_owner": "own", "event_fixture_index": 0,
            "negative_case": "another controlled creature enters",
        }
    if (re.match(r"^when(?:ever)?\s+this\s+.+?\s+enters\b", condition)
            or named_self_before("enters")):
        return {
            "kind": "other_etb", "event_type": "ENTERS_BATTLEFIELD",
            "fixture_owner": "own", "event_fixture_index": 0,
            "negative_case": "another controlled permanent enters",
        }
    if "at the beginning of your upkeep" in condition:
        return {
            "kind": "opponent_phase", "event_type": "BEGINNING_OF_UPKEEP",
            "phase": "PHASE_UPKEEP",
            "negative_case": "opponent upkeep",
        }
    if "at the beginning of your end step" in condition:
        spec = {
            "kind": "opponent_phase",
            "event_type": "BEGINNING_OF_END_STEP",
            "phase": "PHASE_END_STEP",
            "negative_case": "opponent end step",
        }
        if name.startswith("haliya"):
            spec["setup_life_gain"] = 3
        return spec
    if "at the beginning of combat on your turn" in condition:
        return {
            "kind": "opponent_phase",
            "event_type": "BEGINNING_OF_COMBAT",
            "phase": "PHASE_BEGIN_COMBAT",
            "negative_case": "opponent beginning of combat",
        }
    if (re.match(
            r"^when(?:ever)?\s+this\s+.+?\s+becomes tapped\b", condition)
            or named_self_before("becomes tapped")):
        return {
            "kind": "other_tapped", "event_type": "TAPPED",
            "fixture_owner": "own", "event_fixture_index": 0,
            "negative_case": "another controlled permanent becomes tapped",
        }
    if (re.match(r"^when(?:ever)?\s+this\s+.+?\s+attacks\b", condition)
            or named_self_before("attacks")):
        return {
            "kind": "other_attacks", "event_type": "ATTACKS",
            "fixture_owner": "own", "event_fixture_index": 0,
            "negative_case": "another controlled creature attacks",
        }
    # A self-or-another controlled death condition has two genuine positive
    # arms. Use an otherwise matching opponent permanent so the event is a
    # real close non-event, including creature-or-artifact variants.
    compound_dies = _self_or_another_trigger_criteria(
        entry, condition, r"d(?:ie|ies)")
    if compound_dies is not None:
        return _opponent_matching_trigger_fixture(
            compound_dies, "dies", "DIES")
    if (re.match(r"^when(?:ever)?\s+this\s+.+?\s+dies\b", condition)
            or named_self_before("dies")):
        return {
            "kind": "other_dies", "event_type": "DIES",
            "fixture_owner": "own", "event_fixture_index": 0,
            "negative_case": "another controlled creature dies",
        }
    controlled_dies = _controlled_trigger_criteria(
        condition, r"d(?:ie|ies)")
    if controlled_dies is not None:
        return _opponent_matching_trigger_fixture(
            controlled_dies, "dies", "DIES")
    if "creature you control dies" in condition:
        return {
            "kind": "other_dies", "event_type": "DIES",
            "fixture_owner": "opponent", "event_fixture_index": 1,
            "negative_case": "opponent creature dies",
        }
    controlled_etb = _controlled_trigger_criteria(
        condition, r"enter(?:s)?")
    if controlled_etb is not None:
        return _opponent_matching_trigger_fixture(
            controlled_etb, "enters", "ENTERS_BATTLEFIELD")
    controlled_etb_specs = (
        ("land you control enters", 4, "opponent land enters"),
        ("enchantment you control enters", 2,
         "opponent enchantment enters"),
        ("creature you control with flying enters", 0,
         "controlled creature without flying enters"),
        ("creature you control enters", 0, "opponent creature enters"),
        ("artifact you control enters", 1, "opponent artifact enters"),
    )
    for phrase, fixture_index, label in controlled_etb_specs:
        if phrase not in condition:
            continue
        owner = "own" if "without flying" in label else "opponent"
        return {
            "kind": "other_etb", "event_type": "ENTERS_BATTLEFIELD",
            "fixture_owner": owner,
            "event_fixture_index": fixture_index,
            "negative_case": label,
        }
    return None


def _trigger_negative_fixture_specs(
        entry: dict, trigger_condition: str) -> list[dict]:
    """Return one close opponent event for every supported criteria arm."""
    condition = str(trigger_condition or "").strip().casefold()
    if any(_is_grouped_zone_change_trigger(trigger_condition, event_type)
           for event_type in ("ENTERS_BATTLEFIELD", "DIES")):
        # A singular event without the complete batch contract cannot prove
        # that a grouped trigger rejected the intended controller mismatch;
        # the missing batch would independently force zero delivery.
        return []
    trigger_clauses = re.split(
        r"\s+and\s+(?=(?:when|whenever|at)\b)", condition)
    if len(trigger_clauses) > 1:
        fixtures = []
        used_arms: set[str] = set()
        for clause_index, clause in enumerate(trigger_clauses):
            for fixture in _trigger_negative_fixture_specs(entry, clause):
                decorated = dict(fixture)
                arm = str(decorated.get(
                    "negative_arm", f"clause_{clause_index + 1}"))
                if arm in used_arms:
                    arm = f"clause_{clause_index + 1}_{arm}"
                used_arms.add(arm)
                decorated["negative_arm"] = arm
                fixtures.append(decorated)
        return fixtures

    for verb_pattern, verb, event_type in (
            (r"enter(?:s)?", "enters", "ENTERS_BATTLEFIELD"),
            (r"d(?:ie|ies)", "dies", "DIES")):
        criteria = _self_or_another_trigger_criteria(
            entry, condition, verb_pattern)
        if criteria is not None:
            return [
                _with_trigger_event_requirements(
                    entry, condition, fixture)
                for fixture in _opponent_matching_trigger_fixtures(
                    criteria, verb, event_type)]

    for verb_pattern, verb, event_type in (
            (r"enter(?:s)?", "enters", "ENTERS_BATTLEFIELD"),
            (r"d(?:ie|ies)", "dies", "DIES")):
        criteria = _controlled_trigger_criteria(condition, verb_pattern)
        if criteria is not None:
            return [
                _with_trigger_event_requirements(
                    entry, condition, fixture)
                for fixture in _opponent_matching_trigger_fixtures(
                    criteria, verb, event_type)]

    fixture = _trigger_negative_fixture_spec_single(
        entry, trigger_condition)
    if fixture is None:
        return []
    decorated = _with_trigger_event_requirements(
        entry, condition, fixture)
    if "negative_arm" not in decorated:
        label = re.sub(
            r"[^a-z0-9]+", "_",
            str(decorated.get("negative_case", "default")).casefold()
        ).strip("_") or "default"
        decorated["negative_arm"] = label
    return [decorated]


def _trigger_negative_fixture_spec(
        entry: dict, trigger_condition: str) -> dict | None:
    """Compatibility helper returning the first matched negative arm."""
    fixtures = _trigger_negative_fixture_specs(entry, trigger_condition)
    return fixtures[0] if fixtures else None


def _trigger_entry_context(entry: dict, ability_index: int) -> dict:
    """Return authentic cast/payment provenance required by an ETB arm."""
    raw = entry.get("raw", {}) or {}
    name = str(entry.get("name", "") or "").casefold()
    texts = [str(raw.get("oracle_text", "") or "")]
    texts.extend(
        str(face.get("oracle_text", "") or "")
        for face in (raw.get("card_faces") or raw.get("faces") or [])
        if isinstance(face, dict))
    oracle_text = "\n".join(texts).casefold()
    context = {}
    if "if it was cast" in oracle_text or "if you cast it" in oracle_text:
        context.update({"was_cast": True, "cast_controller_id": "p1"})
    if name == "deceit":
        paid_by_ability = {
            0: ({"U": 1, "B": 1}, "evoke"),
            1: ({"U": 2, "C": 4}, None),
            2: ({"B": 2, "C": 4}, None),
        }
        spent, alternate = paid_by_ability.get(
            ability_index, ({"U": 1, "B": 1, "C": 4}, None))
        context["final_paid_details"] = {
            "spent_specific": dict(spent)}
        if alternate:
            context["use_alt_cost"] = alternate
    return context


def _configure_trigger_event_fixture(
        game_state: GameState, card_id: int, player: dict,
        spec: dict) -> None:
    """Apply explicit, artifact-visible characteristics to an event fixture."""
    card = game_state._safe_get_card(card_id)
    if card is None:
        raise AssertionError(f"event fixture {card_id} does not exist")

    if spec.get("event_fixture_type_line"):
        type_line = str(spec["event_fixture_type_line"])
        card.type_line = type_line.casefold()
        (card.card_types, card.subtypes,
         card.supertypes) = card.parse_type_line(type_line)
        if getattr(card, "_printed", None):
            card._printed.update({
                "type_line": card.type_line,
                "card_types": list(card.card_types),
                "subtypes": list(card.subtypes),
                "supertypes": list(card.supertypes),
            })

    if "event_fixture_is_token" in spec:
        card.is_token = bool(spec["event_fixture_is_token"])
    if spec.get("event_fixture_face_down"):
        card.face_down = True
    if spec.get("event_fixture_counters") is not None:
        card.counters = {
            str(kind): int(amount)
            for kind, amount in spec["event_fixture_counters"].items()}

    keywords = list(spec.get("event_fixture_keywords", []) or [])
    for keyword in keywords:
        normalized = str(keyword).casefold()
        if normalized not in card.ALL_KEYWORDS:
            raise AssertionError(
                f"unsupported event fixture keyword {keyword!r}")
        card.keywords[card.ALL_KEYWORDS.index(normalized)] = 1
    if keywords and getattr(card, "_printed", None):
        card._printed["keywords"] = list(card.keywords)

    if "event_fixture_colors" in spec:
        requested = {
            str(symbol).upper()
            for symbol in spec.get("event_fixture_colors", [])}
        card.colors = [1 if symbol in requested else 0 for symbol in "WUBRG"]
        if getattr(card, "_printed", None):
            card._printed["colors"] = list(card.colors)

    for field, key in (
            ("power", "event_fixture_power"),
            ("toughness", "event_fixture_toughness"),
            ("cmc", "event_fixture_cmc")):
        if key not in spec:
            continue
        value = int(spec[key])
        setattr(card, field, value)
        if getattr(card, "_printed", None):
            card._printed[field] = value

    if spec.get("event_fixture_tapped"):
        player.setdefault("tapped_permanents", set()).add(card_id)
    if spec.get("event_fixture_attacking"):
        if card_id not in game_state.current_attackers:
            game_state.current_attackers.append(card_id)


def _relocate_fixture_to_hand(
        game_state: GameState, card_id: int, player: dict | None = None) -> None:
    """Stage a neutral permanent for a later ETB event without firing one."""
    player = player or game_state.p1
    if card_id not in player.get("battlefield", []):
        raise AssertionError(f"event fixture {card_id} is not on the battlefield")
    player["battlefield"].remove(card_id)
    player["hand"].append(card_id)
    player.get("tapped_permanents", set()).discard(card_id)
    player.get("entered_battlefield_this_turn", set()).discard(card_id)
    game_state._last_card_locations[card_id] = (player, "hand")


def _record_trigger_path_evidence(
        path: dict, game_state: GameState, fixture: dict,
        before_state: dict, before_fidelity: dict, surface: str) -> None:
    """Attach invariant-checked state evidence to any dispatched trigger path."""
    invariant_issues = _zone_invariant_issues(
        game_state, fixture["initial_card_ids"])
    if invariant_issues:
        raise AssertionError("; ".join(invariant_issues[:10]))
    _assert_fidelity_clean(before_fidelity, game_state, surface)
    after_state = _state_payload(game_state)
    path["state_before_sha256"] = _canonical_hash(before_state)
    path["state_after_sha256"] = _canonical_hash(after_state)
    path["state_delta"] = _state_delta(before_state, after_state)


def _attach_trigger_failure_evidence(
        path: dict, game_state: GameState, fixture: dict,
        before_state: dict | None, before_fidelity: dict) -> None:
    """Best-effort evidence capture that never masks the primary failure."""
    if before_state is None:
        return
    try:
        after_state = _state_payload(game_state)
        path.setdefault("state_before_sha256", _canonical_hash(before_state))
        path.setdefault("state_after_sha256", _canonical_hash(after_state))
        path.setdefault("state_delta", _state_delta(before_state, after_state))
        path["failure_zone_invariant_issues"] = _zone_invariant_issues(
            game_state, fixture.get("initial_card_ids", []))
        final_fidelity = _fidelity_snapshot(game_state)
        path["failure_fidelity"] = {
            "baseline_nonzero": _nonzero_fidelity(before_fidelity),
            "final_nonzero": _nonzero_fidelity(final_fidelity),
            "changes": _fidelity_changes(before_fidelity, final_fidelity),
        }
    except Exception as evidence_exc:
        path["failure_evidence_error"] = {
            "type": type(evidence_exc).__name__,
            "message": str(evidence_exc),
        }


def _probe_triggered(
        entry: dict, ability_index: int, trigger_condition: str, *,
        max_decisions: int, choice_plan: Sequence[int] = (),
        path_id: str | None = None, path_kind: str = "triggered",
        matched_surface: str | None = None,
        fixture_spec: dict | None = None) -> tuple[dict, list[dict]]:
    """Exercise one trigger via event -> queue -> stack -> public resolution."""
    obligation_id = f"triggered:{ability_index}"
    path_id = path_id or obligation_id
    issues = []
    path = {
        "id": path_id, "kind": path_kind, "ability_index": ability_index,
        "status": "coverage_gap", "choice_plan": list(choice_plan),
        "trigger_condition": trigger_condition,
    }
    if matched_surface is not None:
        path["matched_surface"] = matched_surface
    spec = (copy.deepcopy(fixture_spec) if fixture_spec is not None
            else _trigger_fixture_variants(entry, trigger_condition)[0])
    path["event_arm"] = spec.get("event_arm", "default")
    if spec.get("unsupported_reason"):
        path["reason"] = spec["unsupported_reason"]
        return path, issues
    if path_kind == "choice_replay":
        path["surface_kind"] = "triggered"

    game_state, handler, fixture = _build_probe_state(
        entry, target_zone=spec["target_zone"], include_mana_lands=True)
    target_id = fixture["target_id"]
    path["source_id"] = target_id
    before_fidelity = _fidelity_snapshot(game_state)
    decision_state = {"branch_ordinal": 0, "selected_prefix": []}
    trace: list[dict] = []
    event_card_id = target_id
    before_state = None
    try:
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{path_id} baseline")
        if spec["target_zone"] == "battlefield":
            game_state.ability_handler.register_card_abilities(
                target_id, game_state.p1)

        if "event_fixture_index" in spec:
            event_card_id = fixture["own_fixture_ids"][
                int(spec["event_fixture_index"])]
        _configure_trigger_event_fixture(
            game_state, event_card_id, game_state.p1, spec)
        if "event_fixture_index" in spec:
            if spec.get("event_type") == "ENTERS_BATTLEFIELD":
                _relocate_fixture_to_hand(game_state, event_card_id)
            if spec.get("grant_flying"):
                event_card = game_state._safe_get_card(event_card_id)
                keyword_index = event_card.ALL_KEYWORDS.index("flying")
                event_card.keywords[keyword_index] = 1
                if getattr(event_card, "_printed", None):
                    event_card._printed["keywords"][keyword_index] = 1

            criteria = spec.get("event_fixture_criteria")
            if criteria:
                if spec.get("event_type") == "DIES":
                    last_known = game_state._snapshot_battlefield_object(
                        event_card_id, game_state.p1)
                    criteria_match = _last_known_matches_criteria(
                        last_known, criteria, source_id=target_id,
                        event_id=event_card_id)
                else:
                    criteria_match = _permanent_matches_any_criteria(
                        game_state, event_card_id, criteria,
                        controller=game_state.p1, source_id=target_id)
                path["criteria_preflight"] = {
                    "criteria": criteria,
                    "matched": bool(criteria_match),
                }
                if not criteria_match:
                    path["reason"] = (
                        "the staged event object did not satisfy every runtime "
                        "non-controller trigger criterion")
                    path["trace"] = trace
                    return path, issues

        if spec.get("activate_impending"):
            source_card = game_state._safe_get_card(target_id)
            time_count = max(1, int(getattr(
                source_card, "impending_n", 0) or 0))
            source_card.counters["time"] = time_count

        if spec.get("setup_delirium"):
            graveyard_fixtures = [
                card_id for card_id in fixture["filler_ids"]
                if card_id in game_state.p1.get("graveyard", [])]
            for card_id, type_line_override in zip(
                    graveyard_fixtures[:2], ("Land", "Enchantment")):
                grave_card = game_state._safe_get_card(card_id)
                grave_card.type_line = type_line_override
                grave_card.card_types = [type_line_override.casefold()]
                if getattr(grave_card, "_printed", None):
                    grave_card._printed["type_line"] = type_line_override
        if "tap_fixture_index" in spec:
            tapped_id = fixture["own_fixture_ids"][
                int(spec["tap_fixture_index"])]
            game_state.p1.setdefault(
                "tapped_permanents", set()).add(tapped_id)
        if spec.get("setup_life_gain"):
            game_state.gain_life(
                game_state.p1, int(spec["setup_life_gain"]),
                source_id=target_id)

        phase_attr = spec.get("phase")
        if phase_attr:
            game_state.phase = getattr(game_state, str(phase_attr))
            game_state._last_turn_phase = game_state.phase
            game_state.previous_priority_phase = game_state.phase

        before_state = _state_payload(game_state)
        kind = spec["kind"]
        if kind == "self_etb":
            move_context = _trigger_entry_context(entry, ability_index)
            attach_spec = spec.get("attach_to_fixture")
            if attach_spec:
                fixture_ids = (fixture["own_fixture_ids"]
                               if attach_spec["owner"] == "own"
                               else fixture["opponent_fixture_ids"])
                move_context["attach_to_target"] = fixture_ids[
                    int(attach_spec["index"])]
            event_result = game_state.move_card(
                target_id, game_state.p1, "hand", game_state.p1,
                "battlefield", cause="card_probe_trigger",
                context=move_context)
        elif kind == "phase_begin":
            event_result = game_state.ability_handler.check_abilities(
                None, spec["event_type"], {"controller": game_state.p1})
        elif kind == "self_tapped":
            event_result = game_state.tap_permanent(target_id, game_state.p1)
        elif kind == "self_attacks":
            game_state.phase = game_state.PHASE_DECLARE_ATTACKERS
            game_state._last_turn_phase = game_state.phase
            game_state.previous_priority_phase = game_state.phase
            game_state.priority_player = game_state.p1
            game_state.p1.get(
                "entered_battlefield_this_turn", set()).discard(target_id)
            battlefield_index = game_state.p1["battlefield"].index(target_id)
            attack_action = 28 + battlefield_index
            attack_mask = _generate_mask(handler)
            if (attack_action >= len(attack_mask)
                    or not bool(attack_mask[attack_action])):
                _assert_fidelity_clean(
                    before_fidelity, game_state, f"{path_id} attack mask")
                path["reason"] = (
                    "the source could not be legally declared as an attacker "
                    "in the deterministic combat fixture")
                path["trace"] = trace
                return path, issues
            trace.append(_apply_public_action(handler, attack_action))
            trace.append(_apply_public_action(handler, 438))
            event_result = True
        elif kind == "self_dies":
            event_result = game_state.move_card(
                target_id, game_state.p1, "battlefield", game_state.p1,
                "graveyard", cause="dies")
        elif kind == "controlled_creature_dies":
            event_result = game_state.move_card(
                event_card_id, game_state.p1, "battlefield",
                game_state.p1, "graveyard", cause="dies")
        else:
            event_result = game_state.move_card(
                event_card_id, game_state.p1, "hand", game_state.p1,
                "battlefield", cause="card_probe_trigger")
        # check_abilities returns whether anything queued, not whether a phase
        # event was dispatched.  A legitimate non-match is classified below
        # as a coverage gap; actual zone/tap fixtures still must succeed.
        if kind != "phase_begin" and not event_result:
            raise AssertionError(
                f"real trigger event fixture failed for {kind}")

        # An as-enters decision defers the ETB event itself. Complete that
        # public choice before inspecting the trigger queue.
        if (not game_state.stack and any((
                game_state.targeting_context, game_state.sacrifice_context,
                game_state.choice_context))):
            trace.extend(_drive_public_continuation(
                handler, max_decisions=max_decisions,
                choice_plan=choice_plan, decision_state=decision_state))

        triggered = [
            ability for ability in game_state.ability_handler.
            registered_abilities.get(target_id, [])
            if isinstance(ability, TriggeredAbility)
        ]
        if not 0 <= ability_index < len(triggered):
            path["trace"] = trace
            _record_trigger_path_evidence(
                path, game_state, fixture, before_state, before_fidelity,
                f"{path_id} rediscovery gap")
            path["reason"] = (
                "triggered ability was not rediscovered after event setup")
            return path, issues
        desired = triggered[ability_index]
        active = list(game_state.ability_handler.active_triggers)
        active_target_count = sum(row[0] is desired for row in active)
        stacked_target_count = sum(
            isinstance(item, tuple) and len(item) >= 4
            and isinstance(item[3], dict)
            and item[3].get("ability") is desired
            for item in game_state.stack)
        order_choice = game_state.choice_context or {}
        ordered_entries = []
        if order_choice.get("type") == "order_triggers":
            ordered_entries.extend(order_choice.get("pending", []) or [])
            ordered_entries.extend(order_choice.get("next_batch", []) or [])
        ordering_target_count = sum(
            isinstance(row, (list, tuple)) and row and row[0] is desired
            for row in ordered_entries)
        desired_delivery_count = (
            active_target_count + stacked_target_count + ordering_target_count)
        queued_target = desired_delivery_count > 0
        path["event_fixture"] = {
            **{key: value for key, value in spec.items()
               if key not in {"target_zone", "phase"}},
            "event_card_id": event_card_id,
            "event_dispatched": True,
            "dispatch_returned": bool(event_result),
            "active_queue_count": len(active),
            "desired_active_queue_count": active_target_count,
            "desired_stack_count": stacked_target_count,
            "desired_ordering_choice_count": ordering_target_count,
            "queued_target_trigger": queued_target,
            "desired_delivery_count": desired_delivery_count,
            "delivery": (
                "active_queue" if active_target_count else
                "stack" if stacked_target_count else
                "ordering_choice" if ordering_target_count else "none"),
        }
        if not queued_target:
            path["trace"] = trace
            _record_trigger_path_evidence(
                path, game_state, fixture, before_state, before_fidelity,
                f"{path_id} event gap")
            path["reason"] = (
                "the deterministic matching event did not queue this "
                "registered trigger")
            return path, issues
        if desired_delivery_count != 1:
            raise AssertionError(
                f"matching event delivered the desired trigger "
                f"{desired_delivery_count} times instead of exactly once")

        if active:
            game_state.ability_handler.process_triggered_abilities()
        path["stack_identity_before_resolution"] = any(
            isinstance(item, tuple) and len(item) >= 4
            and isinstance(item[3], dict)
            and item[3].get("ability") is desired
            for item in game_state.stack)
        trace.extend(_drive_public_continuation(
            handler, max_decisions=max_decisions,
            choice_plan=choice_plan, decision_state=decision_state))
        _assert_choice_plan_consumed(choice_plan, decision_state)
        path["trace"] = trace
        final_desired_count = sum(
            isinstance(item, tuple) and len(item) >= 4
            and isinstance(item[3], dict)
            and item[3].get("ability") is desired
            for item in game_state.stack)
        final_desired_count += sum(
            row[0] is desired
            for row in game_state.ability_handler.active_triggers)
        if final_desired_count:
            raise AssertionError(
                "desired trigger remained queued or stacked after public "
                "continuation")
        path["desired_trigger_left_pipeline"] = True
        _record_trigger_path_evidence(
            path, game_state, fixture, before_state, before_fidelity,
            f"{path_id} path")
        path["status"] = "exercised"
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        _attach_trigger_failure_evidence(
            path, game_state, fixture, before_state, before_fidelity)
        issues.append({
            "severity": "failed", "surface": path_id,
            "reason": f"{type(exc).__name__}: {exc}",
        })
    return path, issues


def _probe_triggered_negative(
        entry: dict, ability_index: int, trigger_condition: str, *,
        max_decisions: int, path_id: str | None = None,
        fixture_spec: dict | None = None) -> tuple[dict, list[dict]]:
    """Dispatch a close nonmatching event and require zero desired delivery."""
    matched_surface = f"triggered:{ability_index}"
    path_id = path_id or f"{matched_surface}:negative_event"
    path = {
        "id": path_id, "kind": "negative_event",
        "matched_surface": matched_surface,
        "ability_index": ability_index,
        "trigger_condition": trigger_condition,
        "status": "coverage_gap",
    }
    issues = []
    spec = (copy.deepcopy(fixture_spec) if fixture_spec is not None
            else _trigger_negative_fixture_spec(entry, trigger_condition))
    if spec is None:
        path["reason"] = (
            "no deterministic close non-event fixture is registered for "
            "this trigger condition")
        return path, issues
    path["negative_case"] = spec["negative_case"]
    path["negative_arm"] = spec.get("negative_arm", "default")

    game_state, handler, fixture = _build_probe_state(
        entry, target_zone="battlefield", include_mana_lands=True)
    target_id = fixture["target_id"]
    before_fidelity = _fidelity_snapshot(game_state)
    trace: list[dict] = []
    before_state = None
    try:
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{path_id} baseline")
        game_state.ability_handler.register_card_abilities(
            target_id, game_state.p1)
        triggered = [
            ability for ability in game_state.ability_handler.
            registered_abilities.get(target_id, [])
            if isinstance(ability, TriggeredAbility)
        ]
        if not 0 <= ability_index < len(triggered):
            path["reason"] = (
                "triggered ability was not rediscovered in the negative fixture")
            return path, issues
        desired = triggered[ability_index]

        event_player = None
        event_card_id = None
        if "event_fixture_index" in spec:
            event_player = (game_state.p1
                            if spec.get("fixture_owner") == "own"
                            else game_state.p2)
            fixture_ids = (fixture["own_fixture_ids"]
                           if event_player is game_state.p1
                           else fixture["opponent_fixture_ids"])
            event_card_id = fixture_ids[int(spec["event_fixture_index"])]
            _configure_trigger_event_fixture(
                game_state, event_card_id, event_player, spec)
            if spec["kind"] == "other_etb":
                _relocate_fixture_to_hand(
                    game_state, event_card_id, event_player)

            criteria = spec.get("event_fixture_criteria")
            if criteria:
                if spec.get("event_type") == "DIES":
                    last_known = game_state._snapshot_battlefield_object(
                        event_card_id, event_player)
                    criteria_match = _last_known_matches_criteria(
                        last_known, criteria, source_id=target_id,
                        event_id=event_card_id)
                else:
                    criteria_match = _permanent_matches_any_criteria(
                        game_state, event_card_id, criteria,
                        controller=event_player, source_id=target_id)
                path["criteria_preflight"] = {
                    "criteria": criteria,
                    "matched": bool(criteria_match),
                }
                if not criteria_match:
                    path["reason"] = (
                        "the close non-event fixture did not satisfy every "
                        "runtime non-controller trigger criterion")
                    return path, issues

        if spec["kind"] == "opponent_phase":
            game_state.turn = 2
            game_state.phase = getattr(game_state, spec["phase"])
            game_state._last_turn_phase = game_state.phase
            game_state.previous_priority_phase = game_state.phase
            game_state.priority_player = game_state.p2
        if spec.get("setup_life_gain"):
            game_state.gain_life(
                game_state.p1, int(spec["setup_life_gain"]),
                source_id=target_id)
        before_state = _state_payload(game_state)

        if spec["kind"] == "other_etb":
            event_result = game_state.move_card(
                event_card_id, event_player, "hand", event_player,
                "battlefield", cause="card_probe_trigger_negative")
        elif spec["kind"] == "other_tapped":
            event_result = game_state.tap_permanent(
                event_card_id, event_player)
        elif spec["kind"] == "other_dies":
            event_result = game_state.move_card(
                event_card_id, event_player, "battlefield", event_player,
                "graveyard", cause="dies")
        elif spec["kind"] == "other_attacks":
            game_state.phase = game_state.PHASE_DECLARE_ATTACKERS
            game_state._last_turn_phase = game_state.phase
            game_state.previous_priority_phase = game_state.phase
            game_state.priority_player = game_state.p1
            game_state.p1.get(
                "entered_battlefield_this_turn", set()).discard(event_card_id)
            attack_action = 28 + game_state.p1["battlefield"].index(
                event_card_id)
            attack_mask = _generate_mask(handler)
            if (attack_action >= len(attack_mask)
                    or not bool(attack_mask[attack_action])):
                path["reason"] = (
                    "the close non-event creature could not be legally "
                    "declared as an attacker")
                _record_trigger_path_evidence(
                    path, game_state, fixture, before_state, before_fidelity,
                    f"{path_id} attack gap")
                return path, issues
            trace.append(_apply_public_action(handler, attack_action))
            trace.append(_apply_public_action(handler, 438))
            event_result = True
        else:
            event_result = game_state.ability_handler.check_abilities(
                None, spec["event_type"], {"controller": game_state.p2})

        if spec["kind"] != "opponent_phase" and not event_result:
            raise AssertionError(
                f"real negative trigger event failed for {spec['kind']}")

        active_desired = sum(
            row[0] is desired
            for row in game_state.ability_handler.active_triggers)
        stacked_desired = sum(
            isinstance(item, tuple) and len(item) >= 4
            and isinstance(item[3], dict)
            and item[3].get("ability") is desired
            for item in game_state.stack)
        order_choice = game_state.choice_context or {}
        ordered_entries = []
        if order_choice.get("type") == "order_triggers":
            ordered_entries.extend(order_choice.get("pending", []) or [])
            ordered_entries.extend(order_choice.get("next_batch", []) or [])
        ordering_desired = sum(
            isinstance(row, (list, tuple)) and row and row[0] is desired
            for row in ordered_entries)
        desired_delivery_count = (
            active_desired + stacked_desired + ordering_desired)
        path["event_fixture"] = {
            **{key: value for key, value in spec.items()
               if key not in {"phase"}},
            "event_card_id": event_card_id,
            "event_dispatched": True,
            "dispatch_returned": bool(event_result),
            "desired_delivery_count": desired_delivery_count,
        }
        if desired_delivery_count:
            raise AssertionError(
                f"close non-event delivered the desired trigger "
                f"{desired_delivery_count} time(s)")

        if game_state.ability_handler.active_triggers:
            game_state.ability_handler.process_triggered_abilities()
        if game_state.stack or any((
                game_state.targeting_context, game_state.sacrifice_context,
                game_state.choice_context)):
            trace.extend(_drive_public_continuation(
                handler, max_decisions=max_decisions))
        path["trace"] = trace
        _record_trigger_path_evidence(
            path, game_state, fixture, before_state, before_fidelity,
            f"{path_id} path")
        path["status"] = "exercised"
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        _attach_trigger_failure_evidence(
            path, game_state, fixture, before_state, before_fidelity)
        issues.append({
            "severity": "failed", "surface": path_id,
            "reason": f"{type(exc).__name__}: {exc}",
        })
    return path, issues


def _probe_activated(
        entry: dict, ability_index: int, *, max_decisions: int,
        choice_plan: Sequence[int] = (), path_id: str | None = None,
        path_kind: str = "activated", matched_surface: str | None = None) \
        -> tuple[dict, list[dict]]:
    obligation_id = f"activated:{ability_index}"
    path_id = path_id or obligation_id
    issues = []
    path = {
        "id": path_id, "kind": path_kind, "ability_index": ability_index,
        "status": "coverage_gap", "choice_plan": list(choice_plan),
    }
    if matched_surface is not None:
        path["matched_surface"] = matched_surface
    game_state, handler, fixture = _build_probe_state(
        entry, target_zone="battlefield", include_mana_lands=True)
    target_id = fixture["target_id"]
    before_fidelity = _fidelity_snapshot(game_state)
    before_state = _state_payload(game_state)
    decision_state = {"branch_ordinal": 0, "selected_prefix": []}
    try:
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{path_id} baseline")
        game_state.ability_handler.register_card_abilities(
            target_id, game_state.p1)
        abilities = game_state.ability_handler.get_activated_abilities(target_id)
        if not 0 <= ability_index < len(abilities):
            _assert_fidelity_clean(
                before_fidelity, game_state, f"{path_id} registration")
            path["reason"] = (
                "registered activated ability was not rediscovered in fresh state")
            return path, issues
        ability = abilities[ability_index]
        surface_kind = (
            "mana_ability" if isinstance(ability, ManaAbility) else "activated")
        if path_kind == "choice_replay":
            path["surface_kind"] = surface_kind
        else:
            path["kind"] = surface_kind
        path["cost"] = getattr(ability, "cost", None)
        path["effect"] = getattr(
            ability, "effect", getattr(ability, "effect_text", None))
        dispatch, trace, mana_setup_land_ids = _fund_until_activated(
            handler, fixture["land_ids"], target_id=target_id,
            ability_index=ability_index, max_decisions=max_decisions,
            choice_plan=choice_plan, decision_state=decision_state)
        path["mana_setup_land_ids"] = mana_setup_land_ids
        if dispatch is None:
            _assert_fidelity_clean(
                before_fidelity, game_state, f"{path_id} mask setup")
            path["reason"] = (
                "ability was registered but not exposed by the public mask "
                "after deterministic real-mana setup")
            path["mana_setup_actions"] = len(trace)
            return path, issues
        path["dispatch"] = dispatch
        trace.extend(_apply_activated_dispatch(handler, dispatch))
        trace.extend(_drive_public_continuation(
            handler, max_decisions=max_decisions,
            choice_plan=choice_plan, decision_state=decision_state))
        _assert_choice_plan_consumed(choice_plan, decision_state)
        path["trace"] = trace
        path["tapped_fixture_land_ids"] = sorted(
            set(fixture["land_ids"]).intersection(
                game_state.p1.get("tapped_permanents", set())))
        invariant_issues = _zone_invariant_issues(
            game_state, fixture["initial_card_ids"])
        if invariant_issues:
            raise AssertionError("; ".join(invariant_issues[:10]))
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{path_id} path")
        path["status"] = "exercised"
        path["state_before_sha256"] = _canonical_hash(before_state)
        after_state = _state_payload(game_state)
        path["state_after_sha256"] = _canonical_hash(after_state)
        path["state_delta"] = _state_delta(before_state, after_state)
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        issues.append({
            "severity": "failed", "surface": path_id,
            "reason": f"{type(exc).__name__}: {exc}",
        })
    return path, issues


def _probe_activated_negative(entry: dict, ability_index: int, *,
                              max_decisions: int) \
        -> tuple[dict, list[dict]]:
    matched_id = f"activated:{ability_index}"
    obligation_id = f"{matched_id}:negative_mask"
    issues = []
    path = {
        "id": obligation_id, "kind": "negative_mask",
        "matched_surface": matched_id, "ability_index": ability_index,
        "invalid_condition": "controller does not have priority",
        "status": "coverage_gap",
    }
    game_state, handler, fixture = _build_probe_state(
        entry, target_zone="battlefield", include_mana_lands=True)
    target_id = fixture["target_id"]
    before_state = _state_payload(game_state)
    before_fidelity = _fidelity_snapshot(game_state)
    try:
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{obligation_id} baseline")
        game_state.ability_handler.register_card_abilities(
            target_id, game_state.p1)
        abilities = game_state.ability_handler.get_activated_abilities(target_id)
        if not 0 <= ability_index < len(abilities):
            _assert_fidelity_clean(
                before_fidelity, game_state, f"{obligation_id} registration")
            path["reason"] = (
                "registered activated ability was not rediscovered in fresh state")
            return path, issues
        positive_dispatch, setup_trace, setup_lands = _fund_until_activated(
            handler, fixture["land_ids"], target_id=target_id,
            ability_index=ability_index, max_decisions=max_decisions)
        path["positive_control_dispatch"] = positive_dispatch
        path["mana_setup_land_ids"] = setup_lands
        path["setup_trace"] = setup_trace
        if positive_dispatch is None:
            _assert_fidelity_clean(
                before_fidelity, game_state,
                f"{obligation_id} positive-control setup")
            path["reason"] = (
                "matched negative could not establish a mask-valid positive "
                "control in the same fixture")
            return path, issues
        game_state.priority_player = game_state.p2
        game_state.agent_is_p1 = True
        exposed = _find_activated_dispatch(
            handler, target_id=target_id, ability_index=ability_index,
            sync_policy=False)
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{obligation_id} mask")
        path["exposed_dispatch"] = exposed
        if exposed is not None:
            raise AssertionError(
                "activated ability remained mask-valid without controller priority")
        invariant_issues = _zone_invariant_issues(
            game_state, fixture["initial_card_ids"])
        if invariant_issues:
            raise AssertionError("; ".join(invariant_issues[:10]))
        path["status"] = "exercised"
        path["state_sha256"] = _state_digest(game_state)
        path["state_delta"] = _state_delta(
            before_state, _state_payload(game_state))
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        issues.append({
            "severity": "failed", "surface": obligation_id,
            "reason": f"{type(exc).__name__}: {exc}",
        })
    return path, issues


def _probe_card_seeded(entry: dict, *, input_identity: dict,
                       max_decisions: int = DEFAULT_MAX_DECISIONS,
                       max_branches: int = DEFAULT_MAX_BRANCHES) -> dict:
    obligations: list[dict] = []
    paths: list[dict] = []
    issues: list[dict] = []
    discovered: list[dict] = []
    discovery_fidelity: dict = {}
    deterministic_seed = int(entry["index"]) & 0xFFFFFFFF
    random.seed(deterministic_seed)
    np.random.seed(deterministic_seed)
    from .card_support import get_manifest, reset_manifest_for_tests
    reset_manifest_for_tests()
    with _DiagnosticCapture() as diagnostics:
        try:
            obligations, discovered, discovery_fidelity = \
                _discover_obligations(entry)
            if (discovery_fidelity.get("baseline_nonzero")
                    or discovery_fidelity.get("final_nonzero")
                    or discovery_fidelity.get("changes")):
                issues.append({
                    "severity": "failed", "surface": "discovery_fidelity",
                    "reason": (
                        "surface discovery constructed or changed nonzero "
                        "fidelity telemetry"),
                })
            primary, primary_issues = _probe_primary(
                entry, max_decisions=max_decisions)
            paths.append(primary)
            issues.extend(primary_issues)
            negative, negative_issues = _probe_primary_negative(entry)
            paths.append(negative)
            issues.extend(negative_issues)
            completed_paths = {
                "primary": primary,
                "primary:negative_mask": negative,
            }
            replay_surfaces = [{
                "id": "primary", "kind": "primary",
                "ability_index": None, "path": primary,
            }]
            obligations_by_id = {
                obligation["id"]: obligation for obligation in obligations}
            discovered_by_id = {
                surface["id"]: surface for surface in discovered}
            discovered_branch_count = 0
            for obligation in obligations:
                surface_kind = obligation.get("kind")
                if surface_kind not in {
                        "activated", "mana_ability", "triggered"}:
                    continue
                ability_index = int(obligation["id"].split(":", 1)[1])
                if surface_kind == "triggered":
                    trigger_condition = str(discovered_by_id.get(
                        obligation["id"], {}).get(
                            "trigger_condition", "") or "")
                    variants = _trigger_fixture_variants(
                        entry, trigger_condition)
                    variant_paths = []
                    for variant_index, fixture_spec in enumerate(variants):
                        arm = re.sub(
                            r"[^a-z0-9]+", "_",
                            str(fixture_spec.get(
                                "event_arm", variant_index)).casefold()
                        ).strip("_") or str(variant_index)
                        variant_id = (obligation["id"] if len(variants) == 1
                                      else f"{obligation['id']}:event:{arm}")
                        if discovered_branch_count >= max_branches:
                            triggered = {
                                "id": variant_id,
                                "kind": "triggered",
                                "ability_index": ability_index,
                                "trigger_condition": trigger_condition,
                                "event_arm": fixture_spec.get("event_arm"),
                                "status": "coverage_gap",
                                "reason": (
                                    f"registered-surface branch cap "
                                    f"{max_branches} reached"),
                            }
                            triggered_issues = []
                        else:
                            triggered, triggered_issues = _probe_triggered(
                                entry, ability_index, trigger_condition,
                                max_decisions=max_decisions,
                                path_id=variant_id,
                                fixture_spec=fixture_spec)
                            discovered_branch_count += 1
                        paths.append(triggered)
                        issues.extend(triggered_issues)
                        variant_paths.append(triggered)
                        replay_surfaces.append({
                            "id": variant_id, "kind": "triggered",
                            "ability_index": ability_index,
                            "trigger_condition": trigger_condition,
                            "fixture_spec": fixture_spec,
                            "path": triggered,
                        })
                    statuses = {
                        path.get("status", "coverage_gap")
                        for path in variant_paths}
                    if "failed" in statuses:
                        aggregate_status = "failed"
                    elif statuses == {"exercised"}:
                        aggregate_status = "exercised"
                    else:
                        aggregate_status = "coverage_gap"
                    aggregate = {
                        "id": obligation["id"],
                        "kind": "triggered",
                        "status": aggregate_status,
                        "event_path_ids": [path["id"] for path in variant_paths],
                        "event_arm_count": len(variant_paths),
                        "exercised_event_arm_count": sum(
                            path.get("status") == "exercised"
                            for path in variant_paths),
                    }
                    incomplete = [
                        path for path in variant_paths
                        if path.get("status") != "exercised"]
                    if incomplete:
                        aggregate["reason"] = "; ".join(
                            f"{path.get('event_arm', path['id'])}: "
                            f"{path.get('reason') or (path.get('failure') or {}).get('message') or path.get('status')}"
                            for path in incomplete)[:1000]
                    completed_paths[obligation["id"]] = aggregate
                    negative_id = f"{obligation['id']}:negative_event"
                    negative_specs = _trigger_negative_fixture_specs(
                        entry, trigger_condition)
                    if not negative_specs:
                        negative_specs = [None]
                    negative_paths = []
                    used_negative_ids = {negative_id}
                    for negative_index, negative_spec in enumerate(
                            negative_specs):
                        if negative_index == 0:
                            arm_negative_id = negative_id
                        else:
                            arm = re.sub(
                                r"[^a-z0-9]+", "_",
                                str((negative_spec or {}).get(
                                    "negative_arm", negative_index)).casefold()
                            ).strip("_") or str(negative_index)
                            arm_negative_id = f"{negative_id}:{arm}"
                            if arm_negative_id in used_negative_ids:
                                arm_negative_id = (
                                    f"{arm_negative_id}_{negative_index + 1}")
                            used_negative_ids.add(arm_negative_id)
                        if discovered_branch_count >= max_branches:
                            negative = {
                                "id": arm_negative_id,
                                "kind": "negative_event",
                                "matched_surface": obligation["id"],
                                "ability_index": ability_index,
                                "negative_arm": (negative_spec or {}).get(
                                    "negative_arm"),
                                "status": "coverage_gap",
                                "reason": (
                                    f"registered-surface branch cap "
                                    f"{max_branches} reached"),
                            }
                            negative_issues = []
                        else:
                            negative, negative_issues = \
                                _probe_triggered_negative(
                                    entry, ability_index, trigger_condition,
                                    max_decisions=max_decisions,
                                    path_id=arm_negative_id,
                                    fixture_spec=negative_spec)
                            discovered_branch_count += 1
                        paths.append(negative)
                        issues.extend(negative_issues)
                        negative_paths.append(negative)
                    negative_statuses = {
                        path.get("status", "coverage_gap")
                        for path in negative_paths}
                    if "failed" in negative_statuses:
                        negative_status = "failed"
                    elif negative_statuses == {"exercised"}:
                        negative_status = "exercised"
                    else:
                        negative_status = "coverage_gap"
                    negative_aggregate = {
                        "id": negative_id,
                        "kind": "negative_event",
                        "matched_surface": obligation["id"],
                        "status": negative_status,
                        "negative_path_ids": [
                            path["id"] for path in negative_paths],
                        "negative_arm_count": len(negative_paths),
                        "exercised_negative_arm_count": sum(
                            path.get("status") == "exercised"
                            for path in negative_paths),
                    }
                    incomplete_negatives = [
                        path for path in negative_paths
                        if path.get("status") != "exercised"]
                    if incomplete_negatives:
                        negative_aggregate["reason"] = "; ".join(
                            f"{path.get('negative_arm', path['id'])}: "
                            f"{path.get('reason') or (path.get('failure') or {}).get('message') or path.get('status')}"
                            for path in incomplete_negatives)[:1000]
                    completed_paths[negative_id] = negative_aggregate
                    continue
                negative_id = (
                    f"{obligation['id']}:negative_mask"
                    if surface_kind in {"activated", "mana_ability"}
                    else None)
                if discovered_branch_count >= max_branches:
                    obligation["reason"] = (
                        f"registered-surface branch cap {max_branches} reached")
                    if negative_id and negative_id in obligations_by_id:
                        obligations_by_id[negative_id]["reason"] = (
                            f"registered-surface branch cap {max_branches} reached")
                    continue
                activated, activated_issues = _probe_activated(
                    entry, ability_index, max_decisions=max_decisions)
                paths.append(activated)
                issues.extend(activated_issues)
                completed_paths[obligation["id"]] = activated
                replay_surfaces.append({
                    "id": obligation["id"], "kind": "activated",
                    "ability_index": ability_index, "path": activated,
                })
                activated_negative, activated_negative_issues = \
                    _probe_activated_negative(
                        entry, ability_index,
                        max_decisions=max_decisions)
                paths.append(activated_negative)
                issues.extend(activated_negative_issues)
                completed_paths[negative_id] = activated_negative
                discovered_branch_count += 1

            choice_branch_budget = max(
                0, max_branches - discovered_branch_count)
            scheduled_edges: set[
                tuple[str, tuple[int, ...], int]] = set()
            queued_plans: set[tuple[str, tuple[int, ...]]] = set()
            replay_queue: list[tuple[dict, tuple[int, ...]]] = []
            replay_ordinals: dict[str, int] = {}

            def enqueue_new_plans(surface: dict, source_path: dict) -> None:
                for plan in _choice_replay_plans(
                        source_path, surface["id"], scheduled_edges):
                    key = (surface["id"], plan)
                    if key in queued_plans:
                        continue
                    queued_plans.add(key)
                    replay_queue.append((surface, plan))

            for surface in replay_surfaces:
                enqueue_new_plans(surface, surface["path"])

            replayed_branch_count = 0
            queue_index = 0
            while (queue_index < len(replay_queue)
                   and replayed_branch_count < choice_branch_budget):
                surface, plan = replay_queue[queue_index]
                queue_index += 1
                surface_id = surface["id"]
                replay_ordinal = replay_ordinals.get(surface_id, 0)
                replay_ordinals[surface_id] = replay_ordinal + 1
                replay_id = (
                    f"{surface_id}:choice_replay:{replay_ordinal}")
                if surface["kind"] == "primary":
                    replay, replay_issues = _probe_primary(
                        entry, max_decisions=max_decisions,
                        choice_plan=plan, path_id=replay_id,
                        path_kind="choice_replay",
                        matched_surface=surface_id)
                elif surface["kind"] == "activated":
                    replay, replay_issues = _probe_activated(
                        entry, int(surface["ability_index"]),
                        max_decisions=max_decisions, choice_plan=plan,
                        path_id=replay_id, path_kind="choice_replay",
                        matched_surface=surface_id)
                else:
                    replay, replay_issues = _probe_triggered(
                        entry, int(surface["ability_index"]),
                        str(surface.get("trigger_condition", "")),
                        max_decisions=max_decisions, choice_plan=plan,
                        path_id=replay_id, path_kind="choice_replay",
                        matched_surface=surface_id,
                        fixture_spec=surface.get("fixture_spec"))
                paths.append(replay)
                issues.extend(replay_issues)
                replayed_branch_count += 1
            for obligation in obligations:
                completed = completed_paths.get(obligation["id"])
                if completed is None:
                    continue
                obligation["status"] = completed["status"]
                for evidence_key in (
                        "event_path_ids", "event_arm_count",
                        "exercised_event_arm_count", "negative_path_ids",
                        "negative_arm_count",
                        "exercised_negative_arm_count"):
                    if evidence_key in completed:
                        obligation[evidence_key] = completed[evidence_key]
                if completed.get("reason"):
                    obligation["reason"] = completed["reason"]
                elif completed["status"] == "exercised":
                    obligation.pop("reason", None)
                if completed.get("failure"):
                    obligation["reason"] = completed["failure"]["message"]
            obligations.extend(_choice_branch_obligations(paths))
            _reconcile_typed_choice_obligations(obligations)
        except Exception as exc:
            issues.append({
                "severity": "failed", "surface": "card",
                "reason": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=20),
            })
    manifest = get_manifest()
    with manifest._lock:
        support_manifest_delta = {
            name: {
                "severity": value.get("severity"),
                "count": value.get("count", 0),
                "reasons": dict(sorted(
                    (value.get("reasons", {}) or {}).items())),
            }
            for name, value in sorted(manifest.entries.items())
        }
    reset_manifest_for_tests()
    if support_manifest_delta:
        issues.append({
            "severity": "failed", "surface": "card_support_manifest",
            "reason": (
                f"runtime emitted unsupported-support reports for "
                f"{len(support_manifest_delta)} card name(s)"),
        })
    if diagnostics.records:
        issues.append({
            "severity": "failed", "surface": "diagnostics",
            "reason": f"captured {len(diagnostics.records)} warning/error diagnostic(s)",
        })
    result = {
        "kind": CARD_RESULT_KIND,
        "schema_version": PROBE_SCHEMA_VERSION,
        "input_identity": input_identity,
        "card": {
            "index": int(entry["index"]), "name": entry["name"],
            "oracle_id": entry.get("oracle_id"),
            "oracle_sha256": _oracle_sha256(entry),
            "ledger_status": entry.get("ledger_status"),
            "layout": entry["raw"].get("layout", "normal"),
        },
        "limits": {"max_decisions": max_decisions, "max_branches": max_branches},
        "deterministic_seed": deterministic_seed,
        "branch_policy": (
            "Paired primary paths, every independently recognized compound "
            "trigger arm, and matched trigger/activation negatives are mandatory. "
            "Public choice edges discovered on each canonical surface path are "
            "independently replayed from fresh deterministic fixtures up to "
            "max_branches; unvisited printed/runtime surfaces and alternatives "
            "remain coverage_gap."),
        "obligations": obligations,
        "discovered": discovered,
        "discovery_fidelity": discovery_fidelity,
        "paths": paths,
        "issues": issues,
        "diagnostics": diagnostics.records,
        "card_support_manifest_delta": support_manifest_delta,
        "semantic_status": "unverified",
        "semantic_note": (
            "Automated execution evidence is mechanical only; exact card semantics "
            "require independent scenario-backed post-state assertions."),
    }
    result["runtime_status"] = _result_status(obligations, issues)
    # `status` remains as a compatibility/convenience alias, but is never a
    # semantic certification.
    result["status"] = result["runtime_status"]
    result["sha256"] = _canonical_hash(result)
    return result


def probe_card(entry: dict, *, input_identity: dict,
               max_decisions: int = DEFAULT_MAX_DECISIONS,
               max_branches: int = DEFAULT_MAX_BRANCHES) -> dict:
    """Probe deterministically without contaminating the caller's RNG state."""
    python_random_state = random.getstate()
    numpy_random_state = np.random.get_state()
    try:
        return _probe_card_seeded(
            entry, input_identity=input_identity,
            max_decisions=max_decisions, max_branches=max_branches)
    finally:
        random.setstate(python_random_state)
        np.random.set_state(numpy_random_state)


def _resume_result(path: Path, input_identity: dict, entry: dict, *,
                   max_decisions: int, max_branches: int) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = _read_json_object(path, "card probe result")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("kind") != CARD_RESULT_KIND \
            or payload.get("schema_version") != PROBE_SCHEMA_VERSION:
        return None
    if payload.get("input_identity") != input_identity:
        return None
    card = payload.get("card", {})
    if card.get("index") != int(entry["index"]) or card.get("name") != entry["name"]:
        return None
    if card.get("oracle_sha256") != _oracle_sha256(entry):
        return None
    if payload.get("limits") != {
            "max_decisions": max_decisions, "max_branches": max_branches}:
        return None
    if payload.get("runtime_status", payload.get("status")) not in TERMINAL_STATUSES:
        return None
    if payload.get("sha256") != _canonical_hash(payload):
        return None
    return payload


def run_probe(
        snapshot: Path, registry_path: Path, ledger_path: Path,
        output: Path, *, format_name: str | None = None,
        card_names: Iterable[str] = (), statuses: Iterable[str] = (),
        from_index: int | None = None, to_index: int | None = None,
        shard_index: int = 0, shard_count: int = 1,
        resume: bool = False, max_decisions: int = DEFAULT_MAX_DECISIONS,
        max_branches: int = DEFAULT_MAX_BRANCHES) -> dict:
    snapshot = Path(snapshot).resolve()
    registry_path = Path(registry_path).resolve()
    ledger_path = Path(ledger_path).resolve()
    output = Path(output).resolve()
    for path, label in ((snapshot, "snapshot"), (registry_path, "registry"),
                        (ledger_path, "ledger")):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
    if max_decisions < 1 or max_branches < 1:
        raise ValueError("max_decisions and max_branches must be at least 1")
    if output.exists() and any(output.iterdir()) and not resume:
        raise ValueError(
            f"output directory is not empty; use --resume or a fresh path: {output}")
    output.mkdir(parents=True, exist_ok=True)

    raw_cards = load_pool_snapshot_cards(snapshot, format_name=format_name)
    registry = load_registry(registry_path)
    ledger = _read_json_object(ledger_path, "support ledger")
    identity = _input_identity(
        snapshot, registry_path, ledger_path, registry, ledger)
    selected = select_pool_cards(
        raw_cards, registry, ledger, card_names=card_names,
        statuses=statuses, from_index=from_index, to_index=to_index,
        shard_index=shard_index, shard_count=shard_count)
    if not selected:
        raise ValueError("probe selection contains zero cards")
    scope = {
        "format": format_name,
        "cards": sorted({str(name) for name in card_names}),
        "statuses": sorted({str(status) for status in statuses}),
        "from_index": from_index, "to_index": to_index,
        "shard_index": shard_index, "shard_count": shard_count,
        "selected_cards": len(selected),
    }
    running = {
        "kind": PROBE_KIND,
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": "running",
        "semantic_status": "unverified",
        "input_identity": identity,
        "scope": scope,
        "limits": {"max_decisions": max_decisions, "max_branches": max_branches},
        "branch_policy": (
            "Paired primary paths, every independently recognized compound "
            "trigger arm, and matched trigger/activation negatives are mandatory. "
            "Public choice edges discovered on each canonical surface path are "
            "independently replayed from fresh deterministic fixtures up to "
            "max_branches; unvisited printed/runtime surfaces and alternatives "
            "remain coverage_gap."),
        "note": (
            "Absence of a complete report means the run is incomplete. "
            "execution_passed means mechanical execution only, never semantic verification."),
    }
    _atomic_json(output / "run.json", running)

    results = []
    for ordinal, entry in enumerate(selected, 1):
        artifact_path = _card_artifact_path(output, entry)
        result = (_resume_result(
            artifact_path, identity, entry, max_decisions=max_decisions,
            max_branches=max_branches) if resume else None)
        if result is not None:
            print(_console_safe(
                f"[{ordinal}/{len(selected)}] RESUME {entry['index']:04d} "
                f"{entry['name']}: {result['runtime_status']}"), flush=True)
        else:
            result = probe_card(
                entry, input_identity=identity,
                max_decisions=max_decisions, max_branches=max_branches)
            _atomic_json(artifact_path, result)
            print(_console_safe(
                f"[{ordinal}/{len(selected)}] {entry['index']:04d} "
                f"{entry['name']}: {result['runtime_status']}"), flush=True)
        results.append(result)

    counts = {status: 0 for status in sorted(TERMINAL_STATUSES)}
    by_ledger_status: dict[str, dict[str, int]] = {}
    obligation_status_counts: dict[str, dict[str, int]] = {}
    failed_surface_counts: dict[str, int] = {}
    diagnostic_card_count = 0
    manifest_card_count = 0
    for result in results:
        status = result.get("runtime_status", result.get("status"))
        if status not in counts:
            raise RuntimeError(f"card result has nonterminal status: {status!r}")
        counts[status] += 1
        ledger_status = str(result.get("card", {}).get(
            "ledger_status", "unknown"))
        ledger_counts = by_ledger_status.setdefault(
            ledger_status, {key: 0 for key in sorted(TERMINAL_STATUSES)})
        ledger_counts[status] += 1
        for obligation in result.get("obligations", []):
            kind = str(obligation.get("kind", "unknown"))
            obligation_status = str(obligation.get("status", "unknown"))
            kind_counts = obligation_status_counts.setdefault(kind, {})
            kind_counts[obligation_status] = \
                kind_counts.get(obligation_status, 0) + 1
        for issue in result.get("issues", []):
            if issue.get("severity") != "failed":
                continue
            surface = str(issue.get("surface", "unknown"))
            failed_surface_counts[surface] = \
                failed_surface_counts.get(surface, 0) + 1
        if result.get("diagnostics"):
            diagnostic_card_count += 1
        if result.get("card_support_manifest_delta"):
            manifest_card_count += 1
    summary = {
        "total": len(results),
        "runtime_status_counts": counts,
        "runtime_status_by_ledger_status": by_ledger_status,
        "obligation_status_counts": obligation_status_counts,
        "failed_surface_counts": failed_surface_counts,
        "cards_with_diagnostics": diagnostic_card_count,
        "cards_with_manifest_reports": manifest_card_count,
    }
    report = {
        "kind": PROBE_KIND,
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": "complete",
        "semantic_status": "unverified",
        "semantic_note": (
            "This report records bounded mechanical execution only. Cards remain "
            "semantically unverified until independent scenarios assert every ability, "
            "mode, choice path, and exact post-resolution state."),
        "input_identity": identity,
        "scope": scope,
        "limits": {"max_decisions": max_decisions, "max_branches": max_branches},
        "branch_policy": running["branch_policy"],
        "summary": summary,
        "cards": [{
            "index": result["card"]["index"],
            "name": result["card"]["name"],
            "ledger_status": result["card"].get("ledger_status"),
            "runtime_status": result.get("runtime_status", result.get("status")),
            "semantic_status": "unverified",
            "artifact": _card_artifact_path(output, {
                "index": result["card"]["index"],
                "name": result["card"]["name"],
            }).relative_to(output).as_posix(),
            "sha256": result.get("sha256"),
        } for result in results],
    }
    report["sha256"] = _canonical_hash(report)
    _atomic_json(output / "card_probe_report.json", report)
    complete_run = dict(running)
    complete_run.update({
        "status": "complete", "summary": report["summary"],
        "report": "card_probe_report.json", "report_sha256": report["sha256"],
    })
    complete_run["sha256"] = _canonical_hash(complete_run)
    _atomic_json(output / "run.json", complete_run)
    return report


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--format", dest="format_name", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--card", action="append", default=[],
        help="exact card name; repeat to select multiple cards")
    parser.add_argument(
        "--status", action="append", default=[],
        help="support-ledger status; repeat to select multiple statuses")
    parser.add_argument("--from-index", type=int, default=None)
    parser.add_argument("--to-index", type=int, default=None)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=_positive_int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--max-decisions", type=_positive_int, default=DEFAULT_MAX_DECISIONS,
        help="maximum public continuation actions for one path")
    parser.add_argument(
        "--max-branches", type=_positive_int, default=DEFAULT_MAX_BRANCHES,
        help=("registered-surface and public choice-replay cap; overflow "
              "remains a coverage gap"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_probe(
            args.snapshot, args.registry, args.ledger, args.output,
            format_name=args.format_name, card_names=args.card,
            statuses=args.status, from_index=args.from_index,
            to_index=args.to_index, shard_index=args.shard_index,
            shard_count=args.shard_count, resume=args.resume,
            max_decisions=args.max_decisions, max_branches=args.max_branches)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    counts = report["summary"]["runtime_status_counts"]
    print(
        "Probe complete (semantic_status=unverified): "
        + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)))
    return 0 if counts.get("execution_passed", 0) == report["summary"]["total"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
