"""Canonical card registry and frozen feature schema for format namespaces.

A format namespace pins two versioned, self-hashed JSON artifacts:

- ``card_registry.json`` — canonical card identities. Each card keeps one
  stable integer index (its engine ``card_id``) plus its Oracle identity.
  Extension is append-only, so adding deck-builder candidates can never
  renumber existing cards or invalidate a trained checkpoint.
- ``feature_schema.json`` — the exact card feature-vector layout (base,
  cost, keyword, color, subtype, and MDFC fields). Loading a corpus under a
  frozen schema keeps the observation width fixed; a card whose subtypes are
  outside the frozen vocabulary fails loudly instead of silently changing or
  truncating model input.

Freeze a namespace from a deck corpus with::

    python -m Playersim.card_registry freeze --decks Decks \
        --format standard --output formats/standard

Both artifacts carry ``schema_version`` and a ``sha256`` self-hash; loaders
verify the hash so a hand-edited file cannot masquerade as the frozen one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .card import Card

REGISTRY_KIND = "canonical_card_registry"
REGISTRY_SCHEMA_VERSION = 1
FEATURE_SCHEMA_KIND = "card_feature_schema"
FEATURE_SCHEMA_VERSION = 2

REGISTRY_FILENAME = "card_registry.json"
FEATURE_SCHEMA_FILENAME = "feature_schema.json"
DEFAULT_FORMAT_LISTS_DIRECTORY = Path(__file__).resolve().parents[1] / "Format Card Lists"

BASE_FIELDS = ("cmc", "is_land", "power", "toughness")
COST_FIELDS = ("W", "U", "B", "R", "G", "generic")
COLOR_FIELDS = ("W", "U", "B", "R", "G")
MDFC_FIELDS = ("is_mdfc", "back_power", "back_toughness")


class CanonicalRegistryError(ValueError):
    """A corpus card violates the canonical registry; never skip silently."""


def _canonical_hash(payload: dict) -> str:
    """Hash the payload's canonical JSON form, ignoring any embedded hash."""
    body = {key: value for key, value in payload.items() if key != "sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _card_identity(card) -> tuple[str, str | None]:
    if isinstance(card, dict):
        name = card.get("name")
        oracle_id = card.get("oracle_id")
    else:
        name = getattr(card, "name", None)
        oracle_id = getattr(card, "oracle_id", None)
    if not isinstance(name, str) or not name:
        raise ValueError(f"Registry card has no usable name: {card!r}")
    return name, oracle_id if isinstance(oracle_id, str) and oracle_id else None


def _collect_identities(cards) -> dict[str, tuple[str, str | None]]:
    """Map casefolded name -> (display name, oracle_id), rejecting conflicts."""
    identities: dict[str, tuple[str, str | None]] = {}
    for card in cards:
        name, oracle_id = _card_identity(card)
        key = name.casefold()
        if key not in identities:
            identities[key] = (name, oracle_id)
            continue
        known_name, known_oracle = identities[key]
        if oracle_id and known_oracle and oracle_id != known_oracle:
            raise ValueError(
                f"Card {known_name} has conflicting oracle_id values: "
                f"{known_oracle} vs {oracle_id}")
        if oracle_id and not known_oracle:
            identities[key] = (known_name, oracle_id)
    return identities


def build_registry(cards) -> dict:
    """Build a canonical registry, ordered by card name for determinism."""
    identities = _collect_identities(cards)
    entries = [
        {"index": index, "name": name, "oracle_id": oracle_id}
        for index, (name, oracle_id) in enumerate(
            sorted(identities.values(), key=lambda pair: pair[0].casefold()))
    ]
    registry = {
        "kind": REGISTRY_KIND,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "cards": entries,
    }
    registry["sha256"] = _canonical_hash(registry)
    return registry


def extend_registry(registry: dict, cards) -> dict:
    """Append unseen cards without renumbering any existing entry."""
    existing = {entry["name"].casefold(): entry for entry in registry["cards"]}
    identities = _collect_identities(cards)
    for key, (name, oracle_id) in identities.items():
        entry = existing.get(key)
        if entry is None:
            continue
        if oracle_id and entry.get("oracle_id") \
                and oracle_id != entry["oracle_id"]:
            raise ValueError(
                f"Card {name} has conflicting oracle_id values: "
                f"{entry['oracle_id']} vs {oracle_id}")
    additions = sorted(
        (identities[key] for key in identities if key not in existing),
        key=lambda pair: pair[0].casefold())
    entries = [dict(entry) for entry in registry["cards"]]
    for name, oracle_id in additions:
        entries.append({
            "index": len(entries), "name": name, "oracle_id": oracle_id,
        })
    extended = {
        "kind": REGISTRY_KIND,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "cards": entries,
    }
    extended["sha256"] = _canonical_hash(extended)
    return extended


def registry_name_to_index(registry: dict) -> dict[str, int]:
    return {entry["name"].casefold(): entry["index"]
            for entry in registry["cards"]}


def _verify_artifact(payload: dict, kind: str, path) -> dict:
    if payload.get("kind") != kind:
        raise ValueError(f"{path} is not a {kind} artifact")
    supported_versions = ({1} if kind == REGISTRY_KIND else {1, 2})
    if payload.get("schema_version") not in supported_versions:
        raise ValueError(
            f"{path} has unsupported schema_version "
            f"{payload.get('schema_version')!r}")
    if payload.get("sha256") != _canonical_hash(payload):
        raise ValueError(f"{path} failed its sha256 self-hash check")
    return payload


def write_registry(path, registry: dict) -> None:
    _write_json(path, registry)


def load_registry(path) -> dict:
    registry = _verify_artifact(_read_json(path), REGISTRY_KIND, path)
    for expected_index, entry in enumerate(registry.get("cards", [])):
        if entry.get("index") != expected_index or not entry.get("name"):
            raise ValueError(
                f"{path} registry indices are not dense at {expected_index}")
    return registry


def build_feature_schema(card_db) -> dict:
    """Freeze the exact card feature-vector layout for a card pool."""
    subtypes = set()
    for card in card_db.values():
        subtypes.update(
            str(subtype) for subtype in getattr(card, "subtypes", []))
    vocabulary = sorted(subtypes)
    schema = {
        "kind": FEATURE_SCHEMA_KIND,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "base_fields": list(BASE_FIELDS),
        "cost_fields": list(COST_FIELDS),
        "keywords": list(Card.ALL_KEYWORDS),
        "color_fields": list(COLOR_FIELDS),
        "subtype_vocab": vocabulary,
        "mdfc_fields": list(MDFC_FIELDS),
        "feature_dim": (
            len(BASE_FIELDS) + len(COST_FIELDS) + len(Card.ALL_KEYWORDS)
            + len(COLOR_FIELDS) + len(vocabulary) + len(MDFC_FIELDS)),
    }
    schema["sha256"] = _canonical_hash(schema)
    return schema


def write_feature_schema(path, schema: dict) -> None:
    _write_json(path, schema)


def load_feature_schema(path) -> dict:
    schema = _verify_artifact(_read_json(path), FEATURE_SCHEMA_KIND, path)
    expected_dim = (
        len(schema.get("base_fields", [])) + len(schema.get("cost_fields", []))
        + len(schema.get("keywords", [])) + len(schema.get("color_fields", []))
        + len(schema.get("subtype_vocab", []))
        + len(schema.get("mdfc_fields", [])))
    if schema.get("feature_dim") != expected_dim:
        raise ValueError(f"{path} feature_dim does not match its field lists")
    return schema


def apply_feature_schema(schema: dict) -> None:
    """Install the frozen subtype vocabulary after an engine-compat check."""
    if list(schema.get("keywords", [])) != list(Card.ALL_KEYWORDS):
        raise ValueError(
            "Frozen feature schema keyword list does not match this engine's "
            "Card.ALL_KEYWORDS; the schema was frozen for a different engine "
            "version")
    Card.SUBTYPE_VOCAB = list(schema["subtype_vocab"])


def validate_cards_against_schema(card_db, schema: dict) -> list[str]:
    """Report cards whose subtypes fall outside the frozen vocabulary."""
    vocabulary = {str(entry) for entry in schema.get("subtype_vocab", [])}
    errors = []
    for card in card_db.values():
        unknown = sorted(
            {str(subtype) for subtype in getattr(card, "subtypes", [])}
            - vocabulary)
        if unknown:
            errors.append(
                f"{getattr(card, 'name', '<unnamed>')}: subtypes not in the "
                f"frozen feature schema: {', '.join(unknown)}")
    return errors


def _read_json(path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def corpus_identity(decks_directory) -> dict:
    """Hash every deck JSON plus one aggregate hash over the whole corpus."""
    directory = Path(decks_directory)
    files = []
    for filename in sorted(os.listdir(directory), key=str.casefold):
        path = directory / filename
        if filename.lower().endswith(".json") and path.is_file():
            files.append({
                "name": filename,
                "size_bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            })
    aggregate = hashlib.sha256()
    for entry in files:
        aggregate.update(f"{entry['name']}:{entry['sha256']}\n".encode("utf-8"))
    return {
        "directory": directory.name,
        "files": files,
        "sha256": aggregate.hexdigest(),
    }


def pool_snapshot_identity(
        format_name: str,
        lists_directory=DEFAULT_FORMAT_LISTS_DIRECTORY) -> dict:
    """Identify the pinned format card-pool snapshot file."""
    snapshot = Path(lists_directory) / f"{format_name}.jsonl"
    if not snapshot.is_file():
        raise FileNotFoundError(
            f"No card-pool snapshot for format {format_name}: {snapshot}")
    return {
        "format": format_name,
        "path": snapshot.name,
        "size_bytes": snapshot.stat().st_size,
        "sha256": _file_sha256(snapshot),
    }


def load_pool_snapshot_cards(snapshot_path, format_name=None) -> list[dict]:
    """Load English, legal cards from a JSONL format-pool snapshot."""
    cards = []
    with open(snapshot_path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{snapshot_path}:{line_number} is not valid JSON: {exc}") from exc
            if not isinstance(card, dict):
                raise ValueError(
                    f"{snapshot_path}:{line_number} must contain a JSON object")
            if card.get("lang", "en") != "en":
                continue
            if format_name:
                legality = card.get("legalities", {}).get(format_name)
                if legality != "legal":
                    continue
            cards.append(card)
    # Validate names and Oracle identity conflicts before constructing Cards.
    identities = _collect_identities(cards)
    by_name = {}
    for card in cards:
        key = card["name"].casefold()
        by_name.setdefault(key, card)
    return [by_name[key] for key in sorted(
        identities, key=lambda name: identities[name][0].casefold())]


def registry_identity(registry: dict) -> dict:
    return {
        "schema_version": registry["schema_version"],
        "cards": len(registry["cards"]),
        "sha256": registry["sha256"],
    }


def feature_schema_identity(schema: dict) -> dict:
    return {
        "schema_version": schema["schema_version"],
        "feature_dim": schema["feature_dim"],
        "sha256": schema["sha256"],
    }


def format_lineage(decks_directory, format_name=None, card_registry=None,
                   feature_schema=None,
                   lists_directory=DEFAULT_FORMAT_LISTS_DIRECTORY) -> dict:
    """Bundle every identity a stats consumer needs to trace a run's inputs."""
    return {
        "format": format_name,
        "pool_snapshot": (
            pool_snapshot_identity(format_name, lists_directory=lists_directory)
            if format_name else None),
        "corpus": corpus_identity(decks_directory),
        "card_registry": (
            registry_identity(card_registry) if card_registry else None),
        "feature_schema": (
            feature_schema_identity(feature_schema) if feature_schema else None),
    }


def load_format_namespace(format_directory) -> tuple[dict, dict]:
    """Load and verify a frozen (registry, feature schema) pair."""
    directory = Path(format_directory)
    registry_path = directory / REGISTRY_FILENAME
    schema_path = directory / FEATURE_SCHEMA_FILENAME
    for path in (registry_path, schema_path):
        if not path.is_file():
            raise FileNotFoundError(
                f"Format namespace is missing {path.name}: {directory}. "
                "Freeze it first with "
                "'python -m Playersim.card_registry freeze'.")
    return load_registry(registry_path), load_feature_schema(schema_path)


def freeze_format_namespace(decks_directory, output_directory,
                            format_name=None,
                            lists_directory=DEFAULT_FORMAT_LISTS_DIRECTORY,
                            extend=False) -> dict:
    """Freeze (or append-extend) a format namespace from a deck corpus."""
    from .card import load_decks_and_card_db

    output = Path(output_directory)
    registry_path = output / REGISTRY_FILENAME
    schema_path = output / FEATURE_SCHEMA_FILENAME
    if not extend:
        for path in (registry_path, schema_path):
            if path.exists():
                raise FileExistsError(
                    f"{path.name} already exists in {output}; use extend to "
                    "append new cards or choose a fresh directory")

    decks, card_db = load_decks_and_card_db(
        str(decks_directory), format_name=format_name, strict_legality=True)

    if extend:
        registry = load_registry(registry_path)
        schema = load_feature_schema(schema_path)
        errors = validate_cards_against_schema(card_db, schema)
        if errors:
            raise ValueError(
                "Corpus no longer fits the frozen feature schema (a new "
                "schema version is required): " + "; ".join(errors))
        registry = extend_registry(registry, card_db.values())
    else:
        registry = build_registry(card_db.values())
        schema = build_feature_schema(card_db)

    write_registry(registry_path, registry)
    write_feature_schema(schema_path, schema)
    return {
        "card_registry": registry_identity(registry),
        "feature_schema": feature_schema_identity(schema),
        "decks": [deck.get("name") for deck in decks],
        "lineage": format_lineage(
            decks_directory, format_name=format_name, card_registry=registry,
            feature_schema=schema, lists_directory=lists_directory),
    }


def freeze_format_pool_namespace(
        snapshot_path, output_directory, decks_directory=None,
        format_name=None, lists_directory=DEFAULT_FORMAT_LISTS_DIRECTORY,
        preserve_existing=True) -> dict:
    """Freeze a full pool while retaining any existing canonical indices.

    Deck cards are included as historical/bootstrap identities, so a rotating
    format can widen its current pool without invalidating older checkpoints.
    The feature schema is intentionally rebuilt at version 2 for the union.
    """
    from .card import load_decks_and_card_db

    snapshot_path = Path(snapshot_path)
    output = Path(output_directory)
    registry_path = output / REGISTRY_FILENAME
    schema_path = output / FEATURE_SCHEMA_FILENAME
    if preserve_existing and schema_path.exists() != registry_path.exists():
        raise FileNotFoundError(
            f"Incomplete existing namespace in {output}; expected both "
            f"{REGISTRY_FILENAME} and {FEATURE_SCHEMA_FILENAME}")
    pool_data = load_pool_snapshot_cards(snapshot_path, format_name=format_name)
    pool_cards = [Card(dict(card)) for card in pool_data]

    decks = []
    deck_db = {}
    if decks_directory is not None:
        decks, deck_db = load_decks_and_card_db(
            str(decks_directory), format_name=format_name,
            strict_legality=True)

    all_cards = list(deck_db.values()) + pool_cards
    if preserve_existing and registry_path.is_file():
        registry = extend_registry(load_registry(registry_path), all_cards)
    else:
        registry = build_registry(all_cards)
    card_db = {index: card for index, card in enumerate(all_cards)}
    schema = build_feature_schema(card_db)

    pool_names = {card["name"].casefold() for card in pool_data}
    registry_names = {entry["name"].casefold() for entry in registry["cards"]}
    missing = sorted(pool_names - registry_names)
    if missing:
        raise CanonicalRegistryError(
            "Pool cards missing from generated registry: " + ", ".join(missing))

    write_registry(registry_path, registry)
    write_feature_schema(schema_path, schema)
    result = {
        "card_registry": registry_identity(registry),
        "feature_schema": feature_schema_identity(schema),
        "pool_cards": len(pool_names),
        "historical_cards": len(registry_names - pool_names),
        "decks": [deck.get("name") for deck in decks],
        "pool_snapshot": {
            "path": snapshot_path.name,
            "size_bytes": snapshot_path.stat().st_size,
            "sha256": _file_sha256(snapshot_path),
        },
    }
    if decks_directory is not None:
        result["lineage"] = format_lineage(
            decks_directory, format_name=format_name,
            card_registry=registry, feature_schema=schema,
            lists_directory=lists_directory)
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser(
        "freeze", help="freeze a registry + feature schema from a deck corpus")
    freeze.add_argument("--decks", required=True,
                        help="deck corpus directory (JSON decks)")
    freeze.add_argument("--output", required=True,
                        help="format namespace directory to write")
    freeze.add_argument("--format", dest="format_name", default=None,
                        help="enforce strict legality for this format")
    freeze.add_argument("--format-lists", default=DEFAULT_FORMAT_LISTS_DIRECTORY,
                        help="directory holding <format>.jsonl pool snapshots")
    freeze.add_argument("--extend", action="store_true",
                        help="append new corpus cards to an existing registry")
    pool = subparsers.add_parser(
        "freeze-pool", help="freeze a namespace from a full format JSONL pool")
    pool.add_argument("--snapshot", required=True,
                      help="format pool JSONL snapshot")
    pool.add_argument("--output", required=True,
                      help="format namespace directory to write")
    pool.add_argument("--decks", default=None,
                      help="optional bootstrap/historical deck corpus")
    pool.add_argument("--format", dest="format_name", required=True,
                      help="legality key to require (for example standard)")
    pool.add_argument("--format-lists", default=DEFAULT_FORMAT_LISTS_DIRECTORY,
                      help="directory holding <format>.jsonl pool snapshots")
    pool.add_argument("--replace-indices", action="store_true",
                      help="rebuild indices instead of preserving an existing registry")
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "freeze-pool":
            result = freeze_format_pool_namespace(
                args.snapshot, args.output, decks_directory=args.decks,
                format_name=args.format_name,
                lists_directory=args.format_lists,
                preserve_existing=not args.replace_indices)
        else:
            result = freeze_format_namespace(
                args.decks, args.output, format_name=args.format_name,
                lists_directory=args.format_lists, extend=args.extend)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Frozen {result['card_registry']['cards']} cards "
        f"(registry {result['card_registry']['sha256'][:12]}, "
        f"schema {result['feature_schema']['sha256'][:12]}, "
        f"feature_dim {result['feature_schema']['feature_dim']}) "
        f"from decks: {', '.join(result['decks'])}"
        + (f"; pool cards: {result['pool_cards']}"
           if 'pool_cards' in result else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
