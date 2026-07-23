"""Parse, identify, validate, and import user-supplied deck lists.

Format detection is legality based.  A deck can be legal in several nested
formats, so the command reports every match and chooses the first match in a
documented narrow-to-broad preference order unless ``--format`` is supplied.
Imported decks live below ``formats/<format>/decks/imported``; regenerated
metagame decks live separately and cannot overwrite them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from collections import OrderedDict
from collections import Counter
from pathlib import Path

from .archetypes import declared_profile_hash, normalize_declared_profile
from .card import Card
from .card_registry import (
    REGISTRY_FILENAME,
    FEATURE_SCHEMA_FILENAME,
    freeze_format_pool_namespace,
    load_format_namespace,
    load_pool_snapshot_cards,
    registry_name_to_index,
    validate_cards_against_schema,
)
from .deck_legality import (
    MAX_SIMULATOR_DECK_SIZE,
    deck_copy_limit,
    validate_deck_legality,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LISTS_DIRECTORY = PROJECT_ROOT / "Format Card Lists"
DEFAULT_FORMATS_ROOT = PROJECT_ROOT / "formats"
FORMAT_PREFERENCE = ("standard", "pioneer", "modern")
MAX_DECKLIST_BYTES = 5 * 1024 * 1024
SECTION_MAIN = {"deck", "main", "mainboard", "main deck"}
SECTION_OTHER = {"sideboard", "side board", "companion", "commander"}
SECTION_IGNORED = {"maybeboard", "maybe board"}
SET_SUFFIX = re.compile(r"\s+\([A-Za-z0-9]{2,8}\)\s+\S+\s*$")


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not result:
        raise ValueError("deck name must contain a letter or digit")
    return result


def _aggregate(entries) -> list[dict]:
    counts: OrderedDict[str, list] = OrderedDict()
    for count, name in entries:
        key = name.casefold()
        if key in counts:
            counts[key][0] += count
        else:
            counts[key] = [count, name]
    return [{"count": count, "card": name}
            for count, name in counts.values()]


def _entry_name(raw) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return str(raw.get("name", "")).strip()
    return ""


def _parse_json_payload(payload: object, fallback_name: str) -> dict:
    if isinstance(payload, list):
        raw_entries = payload
        name = fallback_name
        sideboard = []
    elif isinstance(payload, dict):
        name = str(payload.get("name") or fallback_name).strip()
        raw_entries = payload.get("deck", payload.get("cards"))
        sideboard = payload.get("sideboard", [])
        declared_format = payload.get("format")
        maybeboard = payload.get("maybeboard", [])
        raw_strategy_profile = payload.get("strategy_profile")
        if raw_entries is None and all(
                isinstance(value, int) for value in payload.values()):
            raw_entries = [
                {"card": key, "count": value}
                for key, value in payload.items()]
    else:
        raise ValueError("JSON deck list must be an object or list")
    if not isinstance(payload, dict):
        declared_format = None
        maybeboard = []
        raw_strategy_profile = None
    if not isinstance(raw_entries, list):
        raise ValueError("JSON deck list has no deck/cards list")
    def parse_entries(raw_values, label):
        parsed_values = []
        if not isinstance(raw_values, list):
            raise ValueError(f"JSON {label} must be a list")
        for position, entry in enumerate(raw_values, 1):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"JSON {label} entry {position} must be an object")
            card_name = _entry_name(entry.get("card", entry))
            raw_count = entry.get("count", 1)
            if (isinstance(raw_count, bool)
                    or not re.fullmatch(r"[1-9]\d*", str(raw_count))):
                raise ValueError(
                    f"invalid count at JSON {label} entry {position}")
            count = int(raw_count)
            if not card_name or count < 1:
                raise ValueError(
                    f"invalid card/count at JSON {label} entry {position}")
            parsed_values.append((count, card_name))
        return parsed_values

    parsed = parse_entries(raw_entries, "deck")
    parsed_sideboard = parse_entries(sideboard, "sideboard")
    parsed_maybeboard = parse_entries(maybeboard, "maybeboard")
    strategy_profile = (
        normalize_declared_profile(raw_strategy_profile)
        if raw_strategy_profile is not None else None)
    return {
        "name": name, "deck": _aggregate(parsed),
        "sideboard": _aggregate(parsed_sideboard),
        "sideboard_count": sum(count for count, _ in parsed_sideboard),
        "maybeboard_count": sum(count for count, _ in parsed_maybeboard),
        "declared_format": (
            str(declared_format).strip().casefold()
            if declared_format else None),
        "strategy_profile": strategy_profile,
    }


def _read_decklist_bytes(source: Path) -> bytes:
    if source.stat().st_size > MAX_DECKLIST_BYTES:
        raise ValueError(
            f"deck list exceeds {MAX_DECKLIST_BYTES} byte safety limit: "
            f"{source}")
    payload = source.read_bytes()
    if len(payload) > MAX_DECKLIST_BYTES:
        raise ValueError(
            f"deck list exceeds {MAX_DECKLIST_BYTES} byte safety limit: "
            f"{source}")
    return payload


def parse_decklist(path, *, _source_bytes=None) -> dict:
    """Parse Arena/simple text or compact/hydrated JSON deck lists."""
    source = Path(path)
    source_bytes = (
        _read_decklist_bytes(source)
        if _source_bytes is None else bytes(_source_bytes))
    if len(source_bytes) > MAX_DECKLIST_BYTES:
        raise ValueError(
            f"deck list exceeds {MAX_DECKLIST_BYTES} byte safety limit: "
            f"{source}")
    text = source_bytes.decode("utf-8-sig")
    fallback_name = source.stem
    if source.suffix.casefold() == ".json" or text.lstrip().startswith(("{", "[")):
        return _parse_json_payload(json.loads(text), fallback_name)

    section = "main"
    main_entries = []
    sideboard_entries = []
    maybeboard_entries = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        heading = line.rstrip(":").strip().casefold()
        if heading in SECTION_MAIN:
            section = "main"
            continue
        if heading in SECTION_OTHER:
            if heading == "commander":
                raise ValueError(
                    "Commander deck lists are not supported by the "
                    "two-player constructed simulator")
            section = "other"
            continue
        if heading in SECTION_IGNORED:
            section = "ignored"
            continue
        if line.casefold().startswith("sb:"):
            section = "other"
            line = line[3:].strip()
        match = re.match(r"^(\d+)\s*x?\s+(.+?)\s*$", line)
        if not match:
            raise ValueError(
                f"{source}:{line_number}: expected '<count> <card name>'")
        count = int(match.group(1))
        name = SET_SUFFIX.sub("", match.group(2)).strip()
        if count < 1 or not name:
            raise ValueError(f"{source}:{line_number}: invalid card entry")
        if section == "main":
            main_entries.append((count, name))
        elif section == "other":
            sideboard_entries.append((count, name))
        else:
            maybeboard_entries.append((count, name))
    if not main_entries:
        raise ValueError(f"{source} contains no main-deck cards")
    return {
        "name": fallback_name, "deck": _aggregate(main_entries),
        "sideboard": _aggregate(sideboard_entries),
        "sideboard_count": sum(count for count, _ in sideboard_entries),
        "maybeboard_count": sum(count for count, _ in maybeboard_entries),
        "declared_format": None,
        "strategy_profile": None,
    }


def resolve_card_records(card_names, lists_directory=DEFAULT_LISTS_DIRECTORY) -> dict:
    """Resolve exact names/front-face aliases by streaming pinned snapshots."""
    lists_directory = Path(lists_directory)
    requested = {str(name).casefold(): str(name) for name in card_names}
    resolved = {}
    preferred = [lists_directory / f"{name}.jsonl" for name in FORMAT_PREFERENCE]
    for snapshot in [path for path in preferred if path.is_file()]:
        with snapshot.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    card = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{snapshot}:{line_number}: invalid JSON") from exc
                if card.get("lang", "en") != "en" or not card.get("name"):
                    continue
                aliases = [card["name"].casefold()]
                if " // " in card["name"]:
                    aliases.append(card["name"].split(" // ", 1)[0].casefold())
                for alias in aliases:
                    if alias in requested and alias not in resolved:
                        resolved[alias] = card
        if len(resolved) == len(requested):
            break
    missing = [requested[key] for key in requested if key not in resolved]
    if missing:
        raise ValueError(
            "cards absent from supported pinned snapshots "
            f"({', '.join(FORMAT_PREFERENCE)}): " + ", ".join(missing))
    return {requested[key]: resolved[key] for key in requested}


def legal_formats(entries, records: dict, sideboard=None,
                  preference=FORMAT_PREFERENCE) -> list[str]:
    """Return supported 60-card constructed formats in preference order."""
    total = sum(int(entry["count"]) for entry in entries)
    if total < 60 or total > MAX_SIMULATOR_DECK_SIZE:
        return []
    sideboard = list(sideboard or [])
    if sum(int(entry["count"]) for entry in sideboard) > 15:
        return []
    by_casefold = {name.casefold(): card for name, card in records.items()}
    combined = OrderedDict()
    for entry in list(entries) + sideboard:
        card = by_casefold[entry["card"].casefold()]
        key = card["name"].casefold()
        combined.setdefault(key, [0, card])[0] += int(entry["count"])
    combined_entries = [
        {"count": count, "card": card}
        for count, card in combined.values()]
    matches = []
    for format_name in preference:
        legal = True
        for entry in combined_entries:
            card = entry["card"]
            count = int(entry["count"])
            status = str(card.get("legalities", {}).get(format_name, "not_legal"))
            if status not in {"legal", "restricted"}:
                legal = False
                break
            copy_limit = deck_copy_limit(card)
            if copy_limit is not None and count > copy_limit:
                legal = False
                break
            if status == "restricted" and count > 1:
                legal = False
                break
        if legal:
            matches.append(format_name)
    return matches


def _selected_snapshot_records(entries, snapshot: Path, format_name: str):
    cards = load_pool_snapshot_cards(snapshot, format_name=format_name)
    by_name = {card["name"].casefold(): card for card in cards}
    by_front = {
        card["name"].split(" // ", 1)[0].casefold(): card
        for card in cards if " // " in card["name"]}
    selected = []
    missing = []
    for entry in entries:
        key = entry["card"].casefold()
        card = by_name.get(key) or by_front.get(key)
        if card is None:
            missing.append(entry["card"])
        else:
            selected.append((entry, card))
    if missing:
        raise ValueError(
            f"cards absent from selected {format_name} snapshot: "
            + ", ".join(missing))
    return selected


def ingest_decklist(path, *, deck_name=None, format_name=None,
                    lists_directory=DEFAULT_LISTS_DIRECTORY,
                    formats_root=DEFAULT_FORMATS_ROOT,
                    bootstrap_namespace=True, replace=False, dry_run=False,
                    strict_support=False) -> dict:
    source_path = Path(path)
    source_bytes = _read_decklist_bytes(source_path)
    parsed = parse_decklist(path, _source_bytes=source_bytes)
    if deck_name:
        parsed["name"] = str(deck_name).strip()
    entries = parsed["deck"]
    sideboard = parsed.get("sideboard", [])
    main_deck_count = sum(int(entry["count"]) for entry in entries)
    if main_deck_count > MAX_SIMULATOR_DECK_SIZE:
        raise ValueError(
            f"deck has {main_deck_count} cards; simulator safety limit is "
            f"{MAX_SIMULATOR_DECK_SIZE}")
    records = resolve_card_records(
        [entry["card"] for entry in entries + sideboard], lists_directory)
    matches = legal_formats(entries, records, sideboard)
    requested_format = format_name or parsed.get("declared_format")
    if requested_format:
        requested_format = str(requested_format).casefold()
        if requested_format not in FORMAT_PREFERENCE:
            raise ValueError(
                f"unsupported requested format {requested_format}; supported: "
                + ", ".join(FORMAT_PREFERENCE))
        if requested_format not in matches:
            raise ValueError(
                f"deck is not legal in requested format {requested_format}; "
                f"detected: {', '.join(matches) or 'none'}")
        selected_format = requested_format
    elif matches:
        selected_format = matches[0]
    else:
        raise ValueError(
            "deck does not match a supported 60-card constructed format")

    lists_directory = Path(lists_directory)
    formats_root = Path(formats_root)
    snapshot = lists_directory / f"{selected_format}.jsonl"
    selected = _selected_snapshot_records(entries, snapshot, selected_format)
    selected_sideboard = _selected_snapshot_records(
        sideboard, snapshot, selected_format)
    format_dir = formats_root / selected_format
    deck_name = parsed["name"]
    destination_dir = format_dir / "decks" / "imported"
    destination = destination_dir / f"{_slug(deck_name)}.json"

    support_counts = Counter()
    severe_cards = []
    ledger_path = format_dir / "support_ledger.json"
    if ledger_path.is_file():
        with ledger_path.open("r", encoding="utf-8") as handle:
            ledger = json.load(handle)
        statuses = {
            str(row.get("name", "")).casefold(): row.get("status", "unknown")
            for row in ledger.get("cards", [])}
        for entry, raw_card in selected:
            status = statuses.get(raw_card["name"].casefold(), "unknown")
            support_counts[status] += int(entry["count"])
            if status in {"unparsed", "crash", "excluded"}:
                severe_cards.append(raw_card["name"])
    else:
        support_counts["unknown"] = sum(
            int(entry["count"]) for entry, _ in selected)
    severe_cards = sorted(set(severe_cards), key=str.casefold)
    if strict_support and severe_cards:
        raise ValueError(
            "deck contains severe unsupported cards: "
            + ", ".join(severe_cards))

    result = {
        "deck": deck_name,
        "detected_formats": matches,
        "format": selected_format,
        "main_deck_cards": main_deck_count,
        "path": str(destination),
        "severe_support_cards": severe_cards,
        "sideboard_ignored": parsed["sideboard_count"],
        "maybeboard_ignored": parsed.get("maybeboard_count", 0),
        "support_status_slots": dict(sorted(support_counts.items())),
        "written": False,
    }

    registry_path = format_dir / REGISTRY_FILENAME
    schema_path = format_dir / FEATURE_SCHEMA_FILENAME
    registry_exists = registry_path.is_file()
    schema_exists = schema_path.is_file()
    if registry_exists != schema_exists:
        raise FileNotFoundError(
            f"incomplete frozen namespace for {selected_format}: "
            f"expected both {REGISTRY_FILENAME} and {FEATURE_SCHEMA_FILENAME}")

    temporary_namespace = None
    namespace_dir = format_dir
    if not registry_exists:
        if not bootstrap_namespace:
            raise FileNotFoundError(
                f"no frozen namespace for {selected_format}: {format_dir}")
        if dry_run:
            temporary_namespace = tempfile.TemporaryDirectory()
            namespace_dir = Path(temporary_namespace.name)
        else:
            format_dir.mkdir(parents=True, exist_ok=True)
        freeze_format_pool_namespace(
            snapshot, namespace_dir, format_name=selected_format,
            lists_directory=lists_directory, preserve_existing=False)

    try:
        registry, schema = load_format_namespace(namespace_dir)
        registry_index = registry_name_to_index(registry)
        card_db = {}
        card_ids = []
        hydrated_entries = []
        for entry, raw_card in selected:
            canonical_name = raw_card["name"]
            card_id = registry_index.get(canonical_name.casefold())
            if card_id is None:
                raise ValueError(
                    f"{canonical_name} is absent from {selected_format} registry")
            card = Card(dict(raw_card))
            card.card_id = card_id
            card_db[card_id] = card
            card_ids.extend([card_id] * int(entry["count"]))
            hydrated_entries.append(
                {"count": int(entry["count"]), "card": raw_card})
        schema_errors = validate_cards_against_schema(card_db, schema)
        if schema_errors:
            raise ValueError("deck does not fit frozen feature schema: "
                             + "; ".join(schema_errors))
        legality_errors = validate_deck_legality(
            {"cards": card_ids}, card_db, format_name=selected_format)
        if legality_errors:
            raise ValueError("illegal deck: " + "; ".join(legality_errors))

        deck_root = format_dir / "decks"
        for existing in (
                deck_root.rglob("*.json") if deck_root.exists() else []):
            try:
                with existing.open("r", encoding="utf-8") as handle:
                    existing_payload = json.load(handle)
                existing_name = str(existing_payload.get("name", "")).strip()
            except (OSError, ValueError, AttributeError) as exc:
                if existing == destination:
                    raise FileExistsError(
                        f"refusing to overwrite unreadable deck file "
                        f"{destination}") from exc
                continue
            if existing == destination and (
                    not existing_name
                    or existing_name.casefold() != deck_name.casefold()):
                raise FileExistsError(
                    f"deck name {deck_name!r} collides with existing "
                    f"{existing_name or destination.name!r} at {destination}")
            if (existing_name.casefold() == deck_name.casefold()
                    and existing != destination):
                raise FileExistsError(
                    f"a deck named {deck_name!r} already exists at {existing}")
        if destination.exists() and not replace:
            raise FileExistsError(
                f"{destination} already exists; use --replace")

        if dry_run:
            return result

        payload = {
            "deck": hydrated_entries,
            "format": selected_format,
            "kind": "imported_deck",
            "name": deck_name,
            "schema_version": 2,
            "sideboard": [
                {"count": int(entry["count"]), "card": raw_card["name"]}
                for entry, raw_card in selected_sideboard],
            "sideboard_ignored": parsed["sideboard_count"],
            "maybeboard_ignored": parsed.get("maybeboard_count", 0),
            "source_decklist": source_path.name,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
        strategy_profile = parsed.get("strategy_profile")
        if strategy_profile is not None:
            payload["strategy_profile"] = strategy_profile
            payload["strategy_profile_hash"] = declared_profile_hash(
                strategy_profile)
        destination_dir.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2,
                       sort_keys=True) + "\n",
            encoding="utf-8")
        temporary.replace(destination)
        result["written"] = True
        return result
    finally:
        if temporary_namespace is not None:
            temporary_namespace.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto-detect and import a deck into its format pool")
    parser.add_argument("decklist", type=Path)
    parser.add_argument("--name", help="override the deck name")
    parser.add_argument("--format", help="require a specific detected format")
    parser.add_argument("--format-lists", type=Path,
                        default=DEFAULT_LISTS_DIRECTORY)
    parser.add_argument("--formats-root", type=Path, default=DEFAULT_FORMATS_ROOT)
    parser.add_argument("--no-bootstrap-namespace", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="detect and validate without writing")
    parser.add_argument("--strict-support", action="store_true",
                        help="reject unparsed/crash/excluded cards")
    parser.add_argument("--replace", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = ingest_decklist(
            args.decklist, deck_name=args.name, format_name=args.format,
            lists_directory=args.format_lists, formats_root=args.formats_root,
            bootstrap_namespace=not args.no_bootstrap_namespace,
            replace=args.replace, dry_run=args.dry_run,
            strict_support=args.strict_support)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
