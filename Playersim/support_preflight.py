"""Static format-pool support preflight and versioned coverage ledger.

This audit is deliberately stricter than ``coverage_report``: a card with no
runtime manifest entry is not automatically called supported. Every pool card
is constructed, every face is passed through ability/replacement registration,
and spell/loyalty/activated/triggered effect text is probed through the shared
effect factory. The resulting status still describes static evidence, not a
rules proof; only explicit scenario-backed overrides receive ``verified``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

from .ability_types import ActivatedAbility, ManaAbility, TriggeredAbility
from .ability_utils import EffectFactory
from .card import Card
from .card_registry import (
    _file_sha256,
    corpus_identity,
    load_pool_snapshot_cards,
    load_registry,
    registry_identity,
)
from .card_support import get_manifest, reset_manifest_for_tests
from .game_state import GameState

LEDGER_KIND = "format_card_support_ledger"
LEDGER_SCHEMA_VERSION = 1
OVERRIDES_SCHEMA_VERSION = 1


def _canonical_hash(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_overrides(path) -> tuple[set[str], dict[str, str]]:
    if path is None or not Path(path).is_file():
        return set(), {}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != OVERRIDES_SCHEMA_VERSION:
        raise ValueError(f"{path} has unsupported overrides schema_version")
    verified = {str(name).casefold() for name in payload.get("verified", [])}
    excluded_raw = payload.get("excluded", {})
    if isinstance(excluded_raw, list):
        excluded = {str(name).casefold(): "explicit exclusion"
                    for name in excluded_raw}
    else:
        excluded = {str(name).casefold(): str(reason)
                    for name, reason in excluded_raw.items()}
    overlap = verified.intersection(excluded)
    if overlap:
        raise ValueError("Cards cannot be both verified and excluded: "
                         + ", ".join(sorted(overlap)))
    return verified, excluded


def load_corpus_frequencies(decks_directory) -> tuple[Counter, dict[str, list[str]]]:
    """Return casefolded card-copy counts and deck membership."""
    counts = Counter()
    memberships = defaultdict(list)
    if decks_directory is None:
        return counts, memberships
    corpus_path = Path(decks_directory)
    payloads = []
    if corpus_path.is_file():
        with corpus_path.open("r", encoding="utf-8") as handle:
            corpus = json.load(handle)
        payloads = [(corpus_path, deck) for deck in corpus.get("decks", [])]
    else:
        for path in sorted(
                corpus_path.rglob("*.json"),
                key=lambda item: item.relative_to(
                    corpus_path).as_posix().casefold()):
            with path.open("r", encoding="utf-8") as handle:
                payloads.append((path, json.load(handle)))
    for path, payload in payloads:
        deck_name = str(payload.get("name") or path.stem)
        entries = payload.get("deck", payload.get("cards", payload))
        if not isinstance(entries, list):
            raise ValueError(f"{path} does not contain a deck list")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            card_data = entry.get("card", entry)
            name = (card_data.get("name") if isinstance(card_data, dict)
                    else card_data)
            if not name:
                continue
            count = max(0, int(entry.get("count", 1)))
            key = str(name).casefold()
            counts[key] += count
            if deck_name not in memberships[key]:
                memberships[key].append(deck_name)
    return counts, memberships


def _player_state(game_state, player_num):
    seed_id = next(iter(game_state.card_db))
    player = game_state._init_player([seed_id] * 8, player_num)
    for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
        player[zone] = []
    return player


def _face_surfaces(raw_card: dict) -> list[dict]:
    faces = raw_card.get("card_faces") or []
    if faces:
        return [{
            "name": face.get("name", raw_card.get("name", "")),
            "oracle_text": face.get("oracle_text", "") or "",
            "type_line": face.get("type_line", "") or "",
        } for face in faces]
    return [{
        "name": raw_card.get("name", ""),
        "oracle_text": raw_card.get("oracle_text", "") or "",
        "type_line": raw_card.get("type_line", "") or "",
    }]


def _probe_effect_text(text, source_name):
    text = (text or "").strip()
    if not text:
        return 0
    return len(EffectFactory.create_effects(text, source_name=source_name))


def _probe_registered_effects(abilities, source_name):
    probes = 0
    for ability in abilities:
        if isinstance(ability, ManaAbility):
            continue
        if not isinstance(ability, (ActivatedAbility, TriggeredAbility)):
            continue
        effect_text = (getattr(ability, "effect", None)
                       or getattr(ability, "effect_text", ""))
        if isinstance(ability, TriggeredAbility):
            effect_text = getattr(ability, "effect", effect_text)
        probes += 1
        _probe_effect_text(effect_text, source_name)
    return probes


def _probe_surface_effects(surface, source_name):
    text = surface["oracle_text"]
    type_line = surface["type_line"].lower()
    probes = 0
    if "instant" in type_line or "sorcery" in type_line:
        probes += 1
        _probe_effect_text(text, source_name)
    for line in text.splitlines():
        if re.match(r"^\s*[+\-−]?\d+\s*:", line):
            probes += 1
            _probe_effect_text(line.split(":", 1)[1], source_name)
    return probes


def _cleanup_registration(game_state, card_id, card):
    try:
        game_state.ability_handler.unregister_card_abilities(card_id)
    except Exception:
        pass
    game_state.ability_handler.registered_abilities.pop(card_id, None)
    text_registered = getattr(
        game_state.replacement_effects, "_text_registered_cards", None)
    if text_registered is not None:
        text_registered.discard(card_id)
    try:
        card.set_current_face(0)
    except Exception:
        pass
    if hasattr(card, "reset_to_printed"):
        card.reset_to_printed()


def audit_pool_cards(raw_cards: list[dict], registry: dict) -> list[dict]:
    """Construct and statically probe every supplied pool card."""
    registry_by_name = {entry["name"].casefold(): entry
                        for entry in registry["cards"]}
    cards = {}
    construction_issues = {}
    for raw in raw_cards:
        entry = registry_by_name.get(raw["name"].casefold())
        if entry is None:
            raise ValueError(f"Pool card absent from registry: {raw['name']}")
        try:
            card = Card(copy.deepcopy(raw))
            card.card_id = entry["index"]
            cards[entry["index"]] = card
        except Exception as exc:
            construction_issues[entry["index"]] = str(exc)[:160]

    game_state = GameState(cards)
    game_state.p1 = _player_state(game_state, 1)
    game_state.p2 = _player_state(game_state, 2)
    game_state.priority_player = game_state.p1
    reset_manifest_for_tests()
    rows = []

    for raw in raw_cards:
        registry_entry = registry_by_name[raw["name"].casefold()]
        card_id = registry_entry["index"]
        base = {
            "index": card_id,
            "name": raw["name"],
            "oracle_id": registry_entry.get("oracle_id"),
            "layout": raw.get("layout", "normal"),
            "keywords": sorted({str(value) for value in raw.get("keywords", [])}),
            "faces_audited": len(_face_surfaces(raw)),
            "abilities_registered": 0,
            "replacement_effects_registered": 0,
            "effect_probes": 0,
            "issues": [],
        }
        if card_id in construction_issues:
            base["issues"].append({
                "severity": "crash",
                "reason": "Card construction failed: " + construction_issues[card_id],
            })
            rows.append(base)
            continue

        card = cards[card_id]
        game_state.p1["battlefield"] = [card_id]
        fidelity_before = sum(
            game_state.fidelity_counters.get(key, 0)
            for key in ("unparsed_mana", "unparsed_modal", "unparsed_effects"))
        try:
            surfaces = _face_surfaces(raw)
            for face_index, surface in enumerate(surfaces):
                if face_index and hasattr(card, "set_current_face"):
                    card.set_current_face(face_index)
                game_state.ability_handler._parse_and_register_abilities(
                    card_id, card)
                abilities = list(game_state.ability_handler.registered_abilities.get(
                    card_id, []))
                base["abilities_registered"] += len(abilities)
                base["effect_probes"] += _probe_registered_effects(
                    abilities, raw["name"])
                base["effect_probes"] += _probe_surface_effects(
                    surface, raw["name"])
                replacements = game_state.replacement_effects \
                    .register_card_replacement_effects(card_id, game_state.p1)
                base["replacement_effects_registered"] += len(replacements or [])
                _cleanup_registration(game_state, card_id, card)
        except Exception as exc:
            base["issues"].append({
                "severity": "crash",
                "reason": f"Static preflight raised: {type(exc).__name__}: {exc}"[:200],
            })
        fidelity_after = sum(
            game_state.fidelity_counters.get(key, 0)
            for key in ("unparsed_mana", "unparsed_modal", "unparsed_effects"))
        manifest_entry = get_manifest().entries.get(raw["name"], {})
        for reason, count in sorted(manifest_entry.get("reasons", {}).items()):
            base["issues"].append({
                "severity": manifest_entry.get("severity", "partial"),
                "reason": reason,
                "count": int(count),
            })
        if fidelity_after > fidelity_before and not base["issues"]:
            base["issues"].append({
                "severity": "unparsed",
                "reason": "ability/replacement registration advanced fidelity counters",
                "count": fidelity_after - fidelity_before,
            })
        rows.append(base)
        game_state.p1["battlefield"] = []
        _cleanup_registration(game_state, card_id, card)
    reset_manifest_for_tests()
    return rows


def _issue_mechanics(row: dict) -> set[str]:
    issue_text = " ".join(issue["reason"] for issue in row["issues"]).lower()
    keyword_tags = {
        keyword for keyword in row.get("keywords", [])
        if keyword.lower() in issue_text
    }
    if keyword_tags:
        return keyword_tags
    if row.get("layout") not in (None, "normal"):
        return {f"layout:{row['layout']}"}
    return {"unclassified oracle text"}


def build_support_ledger(
        raw_cards, registry, audit_rows, decks_directory=None,
        corpus_label="bootstrap", overrides_path=None,
        snapshot_path=None) -> dict:
    verified, excluded = _read_overrides(overrides_path)
    frequencies, memberships = load_corpus_frequencies(decks_directory)
    severity_rank = {"partial": 1, "unparsed": 2, "crash": 3}
    status_counts = Counter()
    ranked_cards = []
    mechanic_rows = defaultdict(lambda: {
        "affected_cards": 0, "deck_slots": 0, "deck_cards": 0,
        "status_counts": Counter(), "examples": [],
    })

    for row in audit_rows:
        key = row["name"].casefold()
        row["corpus_copies"] = int(frequencies.get(key, 0))
        row["corpus_decks"] = sorted(memberships.get(key, []))
        if key in excluded:
            status = "excluded"
            row["issues"].append({
                "severity": "excluded", "reason": excluded[key]})
        elif key in verified:
            # An end-to-end scenario is stronger evidence than a conservative
            # static probe. Retain probe notes for future parser cleanup, but
            # do not demote behavior already guarded through execution.
            status = "verified"
        elif row["issues"]:
            status = max(
                (issue.get("severity", "partial") for issue in row["issues"]),
                key=lambda value: severity_rank.get(value, 0))
        elif row["corpus_copies"]:
            status = "observed_clean"
        else:
            status = "unseen"
        row["status"] = status
        status_counts[status] += 1

        if status in ("partial", "unparsed", "crash", "excluded"):
            ranked_cards.append({
                "name": row["name"], "status": status,
                "deck_slots": row["corpus_copies"],
                "decks": row["corpus_decks"],
                "reasons": sorted({issue["reason"] for issue in row["issues"]}),
            })
            for mechanic in _issue_mechanics(row):
                target = mechanic_rows[mechanic]
                target["affected_cards"] += 1
                target["deck_slots"] += row["corpus_copies"]
                target["deck_cards"] += int(bool(row["corpus_copies"]))
                target["status_counts"][status] += 1
                target["examples"].append((row["corpus_copies"], row["name"]))

    ranked_cards.sort(key=lambda item: (
        -item["deck_slots"], -severity_rank.get(item["status"], 0),
        item["name"].casefold()))
    ranked_mechanics = []
    for mechanic, values in mechanic_rows.items():
        ranked_mechanics.append({
            "mechanic": mechanic,
            "affected_cards": values["affected_cards"],
            "deck_slots": values["deck_slots"],
            "deck_cards": values["deck_cards"],
            "status_counts": dict(sorted(values["status_counts"].items())),
            "examples": [name for _, name in sorted(
                values["examples"], key=lambda pair: (-pair[0], pair[1].casefold()))[:10]],
        })
    ranked_mechanics.sort(key=lambda item: (
        -item["deck_slots"], -item["affected_cards"],
        item["mechanic"].casefold()))

    total = len(audit_rows)
    safe = status_counts["verified"] + status_counts["observed_clean"]
    corpus_source = None
    if decks_directory:
        corpus_path = Path(decks_directory)
        corpus_source = ({
            "path": corpus_path.name,
            "size_bytes": corpus_path.stat().st_size,
            "sha256": _file_sha256(corpus_path),
        } if corpus_path.is_file() else corpus_identity(corpus_path))
    ledger = {
        "kind": LEDGER_KIND,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "evidence_model": {
            "verified": "explicit scenario-backed override",
            "observed_clean": "in configured corpus and clean in static preflight",
            "unseen": "not in configured corpus; static preflight found no issue",
            "partial": "some text registered/parsed but a fallback was reported",
            "unparsed": "an effect or registration path produced no runnable result",
            "crash": "construction, registration, or probing raised",
            "excluded": "explicit support override excludes the card",
        },
        "pool_snapshot": ({
            "path": Path(snapshot_path).name,
            "size_bytes": Path(snapshot_path).stat().st_size,
            "sha256": _file_sha256(snapshot_path),
        } if snapshot_path else None),
        "card_registry": registry_identity(registry),
        "corpus": {
            "label": corpus_label,
            "path": Path(decks_directory).name if decks_directory else None,
            "identity": corpus_source,
            "deck_count": len({deck for decks in memberships.values() for deck in decks}),
            "pool_card_slots": sum(
                frequencies.get(row["name"].casefold(), 0) for row in audit_rows),
            "note": ("Ranking reflects this configured corpus only; a bootstrap "
                     "corpus is not a current metagame sample."),
        },
        "overrides": ({
            "path": Path(overrides_path).name,
            "sha256": _file_sha256(overrides_path),
        } if overrides_path and Path(overrides_path).is_file() else None),
        "summary": {
            "total": total,
            "status_counts": dict(sorted(status_counts.items())),
            "qualified_fraction": safe / total if total else 1.0,
            "static_clean_fraction": (
                (status_counts["verified"] + status_counts["observed_clean"]
                 + status_counts["unseen"]) / total if total else 1.0),
        },
        "ranked_mechanics": ranked_mechanics,
        "ranked_cards": ranked_cards,
        "cards": sorted(audit_rows, key=lambda row: row["index"]),
    }
    ledger["sha256"] = _canonical_hash(ledger)
    return ledger


def write_ledger(path, ledger):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def run_preflight(snapshot, registry_path, output, decks=None,
                  corpus_label="bootstrap", overrides=None, format_name=None):
    raw_cards = load_pool_snapshot_cards(snapshot, format_name=format_name)
    registry = load_registry(registry_path)
    audit_rows = audit_pool_cards(raw_cards, registry)
    ledger = build_support_ledger(
        raw_cards, registry, audit_rows, decks_directory=decks,
        corpus_label=corpus_label, overrides_path=overrides,
        snapshot_path=snapshot)
    write_ledger(output, ledger)
    return ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--decks", default=None)
    parser.add_argument("--corpus-label", default="bootstrap")
    parser.add_argument("--overrides", default=None)
    parser.add_argument("--format", dest="format_name", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.disable(logging.CRITICAL)
    ledger = run_preflight(
        args.snapshot, args.registry, args.output, decks=args.decks,
        corpus_label=args.corpus_label, overrides=args.overrides,
        format_name=args.format_name)
    print(json.dumps({
        "output": args.output,
        "summary": ledger["summary"],
        "top_mechanics": ledger["ranked_mechanics"][:10],
        "sha256": ledger["sha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
