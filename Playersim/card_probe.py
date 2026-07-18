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

from .ability_types import ActivatedAbility, ManaAbility, StaticAbility, TriggeredAbility
from .actions import ActionHandler
from .card import Card
from .card_registry import load_pool_snapshot_cards, load_registry
from .game_state import GameState


PROBE_KIND = "playersim_dynamic_card_probe"
CARD_RESULT_KIND = "playersim_dynamic_card_probe_card"
PROBE_SCHEMA_VERSION = 1
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


def _drive_public_continuation(handler, *, max_decisions: int) -> list[dict]:
    """Deterministically finish public target/choice and stack-priority flows."""
    game_state = handler.game_state
    trace = []
    seen = set()
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
        if not pending and game_state.stack and 11 in valid:
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
        step = _apply_public_action(handler, action)
        if decision_kind is not None:
            step["decision"] = {
                "kind": decision_kind,
                "semantic_options": [{
                    "action": index,
                    "action_type": handler.get_action_info(index)[0],
                    "reason": str(getattr(
                        handler, "action_reasons_with_context", {}).get(
                            index, {}).get("reason", ""))[:200],
                } for index in semantic_actions],
                "selected_action": action,
                "context_summary": _json_safe({
                    key: value for key, value in (decision_payload or {}).items()
                    if key not in {"player", "controller", "game_state",
                                   "effect_continuation", "parent_choice"}
                }),
            }
            if len(semantic_actions) > 1:
                step["decision"]["unvisited_branch_count"] = \
                    len(semantic_actions) - 1
        trace.append(step)
    raise RuntimeError(f"public continuation exceeded {max_decisions} decisions")


def _choice_branch_obligations(paths: Sequence[dict]) -> list[dict]:
    """Convert deterministic multi-option decisions into fail-closed gaps."""
    obligations = []
    for path in paths:
        ordinal = 0
        for step in path.get("trace", []) or []:
            decision = step.get("decision") or {}
            if int(decision.get("unvisited_branch_count", 0) or 0) < 1:
                continue
            obligation_id = f"choice:{path['id']}:{ordinal}"
            ordinal += 1
            obligations.append({
                "id": obligation_id,
                "kind": "choice_branch",
                "matched_surface": path["id"],
                "decision_kind": decision.get("kind"),
                "status": "coverage_gap",
                "reason": (
                    "deterministic continuation visited one of multiple "
                    "mask-valid semantic choices"),
                "selected_action": decision.get("selected_action"),
                "semantic_options": decision.get("semantic_options", []),
                "unvisited_branch_count": decision.get(
                    "unvisited_branch_count"),
            })
    return obligations


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


def _discover_obligations(entry: dict) -> tuple[list[dict], list[dict], dict]:
    """Discover all surfaces; only the primary surface is exercised by v1."""
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
            "reason": "bounded v1 has not dynamically exercised this registered ability",
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

    for optional_index, match in enumerate(re.finditer(
            r"\byou may\b", oracle_text, re.IGNORECASE)):
        obligations.append({
            "id": f"choice:optional:{optional_index}",
            "kind": "optional_branch", "status": "coverage_gap",
            "reason": "optional yes/no branch was not independently replayed",
            "text_offset": match.start(),
        })
    if (re.search(r"\{X\}", str(entry["raw"].get("mana_cost", "")),
                  re.IGNORECASE)
            or re.search(r"\{X\}", oracle_text, re.IGNORECASE)):
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
    }


def _probe_primary(entry: dict, *, max_decisions: int) -> tuple[dict, list[dict]]:
    issues = []
    path = {"id": "primary", "kind": "primary", "status": "coverage_gap"}
    game_state, handler, fixture = _build_probe_state(
        entry, target_zone="hand", include_mana_lands=True)
    target_id = fixture["target_id"]
    before_fidelity = _fidelity_snapshot(game_state)
    before_state = _state_payload(game_state)
    try:
        _assert_fidelity_clean(before_fidelity, game_state, "primary baseline")
        action = _find_primary_action(handler, target_id)
        _assert_fidelity_clean(before_fidelity, game_state, "primary mask")
        if action is None:
            path["reason"] = (
                "primary card action was not uniquely exposed by the public mask")
            return path, issues
        path["action"] = action
        trace = [_apply_public_action(handler, action)]
        trace.extend(_drive_public_continuation(
            handler, max_decisions=max_decisions))
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
        _assert_fidelity_clean(before_fidelity, game_state, "primary path")
        path["status"] = "exercised"
        path["state_before_sha256"] = _canonical_hash(before_state)
        after_state = _state_payload(game_state)
        path["state_after_sha256"] = _canonical_hash(after_state)
        path["state_delta"] = _state_delta(before_state, after_state)
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        issues.append({
            "severity": "failed", "surface": "primary",
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
                          max_decisions: int) -> tuple[dict | None, list[dict], list[int]]:
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
            handler, max_decisions=max_decisions))
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


def _probe_activated(entry: dict, ability_index: int, *,
                     max_decisions: int) -> tuple[dict, list[dict]]:
    obligation_id = f"activated:{ability_index}"
    issues = []
    path = {
        "id": obligation_id, "kind": "activated",
        "ability_index": ability_index, "status": "coverage_gap",
    }
    game_state, handler, fixture = _build_probe_state(
        entry, target_zone="battlefield", include_mana_lands=True)
    target_id = fixture["target_id"]
    before_fidelity = _fidelity_snapshot(game_state)
    before_state = _state_payload(game_state)
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
        ability = abilities[ability_index]
        path["kind"] = (
            "mana_ability" if isinstance(ability, ManaAbility) else "activated")
        path["cost"] = getattr(ability, "cost", None)
        path["effect"] = getattr(
            ability, "effect", getattr(ability, "effect_text", None))
        dispatch, trace, mana_setup_land_ids = _fund_until_activated(
            handler, fixture["land_ids"], target_id=target_id,
            ability_index=ability_index, max_decisions=max_decisions)
        path["mana_setup_land_ids"] = mana_setup_land_ids
        if dispatch is None:
            _assert_fidelity_clean(
                before_fidelity, game_state, f"{obligation_id} mask setup")
            path["reason"] = (
                "ability was registered but not exposed by the public mask "
                "after deterministic real-mana setup")
            path["mana_setup_actions"] = len(trace)
            return path, issues
        path["dispatch"] = dispatch
        trace.extend(_apply_activated_dispatch(handler, dispatch))
        trace.extend(_drive_public_continuation(
            handler, max_decisions=max_decisions))
        path["trace"] = trace
        path["tapped_fixture_land_ids"] = sorted(
            set(fixture["land_ids"]).intersection(
                game_state.p1.get("tapped_permanents", set())))
        invariant_issues = _zone_invariant_issues(
            game_state, fixture["initial_card_ids"])
        if invariant_issues:
            raise AssertionError("; ".join(invariant_issues[:10]))
        _assert_fidelity_clean(
            before_fidelity, game_state, f"{obligation_id} path")
        path["status"] = "exercised"
        path["state_before_sha256"] = _canonical_hash(before_state)
        after_state = _state_payload(game_state)
        path["state_after_sha256"] = _canonical_hash(after_state)
        path["state_delta"] = _state_delta(before_state, after_state)
    except Exception as exc:
        path["status"] = "failed"
        path["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        issues.append({
            "severity": "failed", "surface": obligation_id,
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
            obligations_by_id = {
                obligation["id"]: obligation for obligation in obligations}
            discovered_branch_count = 0
            for obligation in obligations:
                if obligation.get("kind") not in {"activated", "mana_ability"}:
                    continue
                negative_id = f"{obligation['id']}:negative_mask"
                if discovered_branch_count >= max_branches:
                    obligation["reason"] = (
                        f"registered-surface branch cap {max_branches} reached")
                    if negative_id in obligations_by_id:
                        obligations_by_id[negative_id]["reason"] = (
                            f"registered-surface branch cap {max_branches} reached")
                    continue
                activated_index = int(obligation["id"].split(":", 1)[1])
                activated, activated_issues = _probe_activated(
                    entry, activated_index, max_decisions=max_decisions)
                paths.append(activated)
                issues.extend(activated_issues)
                completed_paths[obligation["id"]] = activated
                activated_negative, activated_negative_issues = \
                    _probe_activated_negative(
                        entry, activated_index, max_decisions=max_decisions)
                paths.append(activated_negative)
                issues.extend(activated_negative_issues)
                completed_paths[negative_id] = activated_negative
                discovered_branch_count += 1
            for obligation in obligations:
                completed = completed_paths.get(obligation["id"])
                if completed is None:
                    continue
                obligation["status"] = completed["status"]
                if completed.get("reason"):
                    obligation["reason"] = completed["reason"]
                elif completed["status"] == "exercised":
                    obligation.pop("reason", None)
                if completed.get("failure"):
                    obligation["reason"] = completed["failure"]["message"]
            obligations.extend(_choice_branch_obligations(paths))
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
            "The paired primary paths are mandatory. One deterministic branch is "
            "attempted per registered runtime surface up to max_branches; unvisited "
            "surfaces and alternative branches remain coverage_gap."),
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
            "The paired primary paths are mandatory. Registered runtime surfaces "
            "are attempted up to max_branches; unvisited alternatives remain "
            "coverage_gap."),
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
        help="recorded branch cap; overflow remains a coverage gap")
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
