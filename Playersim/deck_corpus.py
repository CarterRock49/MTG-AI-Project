"""Hydrate compact metagame lists into simulator-ready deck files.

The compact corpus in ``formats/<format>/metagame_corpus_*.json`` is the
reviewable source of deck names, counts, shares, and provenance.  The engine
needs complete Scryfall-style card records, so this module joins those names
against a pinned format snapshot and writes deterministic deck JSON files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

from .card_registry import load_pool_snapshot_cards


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FORMAT = "standard"
DEFAULT_FORMAT_DIRECTORY = PROJECT_ROOT / "formats" / DEFAULT_FORMAT
DEFAULT_CORPUS = DEFAULT_FORMAT_DIRECTORY / "metagame_corpus_2026-07-11.json"
DEFAULT_SNAPSHOT = PROJECT_ROOT / "Format Card Lists" / "standard.jsonl"
DEFAULT_OUTPUT = DEFAULT_FORMAT_DIRECTORY / "decks"
HYDRATED_SCHEMA_VERSION = 1


def _canonical_json_bytes(payload) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deck_filename(position: int, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not slug:
        raise ValueError("deck name must contain at least one letter or digit")
    return f"{position:02d}-{slug}.json"


def _load_compact_corpus(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("decks"), list):
        raise ValueError(f"{path} must contain a top-level decks list")
    if not payload["decks"]:
        raise ValueError(f"{path} contains no decks")
    return payload


def _require_generated_directory(path: Path) -> None:
    """Refuse recursive replacement of a directory with unrelated content."""
    if not path.exists():
        return
    if not path.is_dir():
        raise ValueError(f"generated corpus path is not a directory: {path}")
    unexpected = [
        item.name for item in path.iterdir()
        if not item.is_file() or item.suffix.casefold() != ".json"
    ]
    if unexpected:
        raise ValueError(
            f"refusing to replace {path}; unrelated entries: "
            + ", ".join(sorted(unexpected)))


def hydrate_corpus(corpus_path, snapshot_path, output_directory,
                   format_name: str | None = None, *, replace=False) -> dict:
    """Write deterministic, fully hydrated deck JSON files.

    ``replace`` permits replacing an existing generated directory.  Hydration
    is assembled in a sibling temporary directory first, so a validation or
    write failure cannot leave a half-updated runtime corpus.
    """
    corpus_path = Path(corpus_path).resolve()
    snapshot_path = Path(snapshot_path).resolve()
    output = Path(output_directory).resolve()
    corpus = _load_compact_corpus(corpus_path)
    effective_format = format_name or corpus.get("format")
    if not effective_format:
        raise ValueError("format_name is required when the corpus has no format")
    if corpus.get("format") and corpus["format"] != effective_format:
        raise ValueError(
            f"corpus format {corpus['format']!r} does not match "
            f"{effective_format!r}")

    snapshot_cards = load_pool_snapshot_cards(
        snapshot_path, format_name=effective_format)
    cards_by_name = {card["name"].casefold(): card for card in snapshot_cards}

    hydrated = []
    seen_names = set()
    for position, compact_deck in enumerate(corpus["decks"], 1):
        if not isinstance(compact_deck, dict):
            raise ValueError(f"deck {position} must be an object")
        name = str(compact_deck.get("name", "")).strip()
        key = name.casefold()
        if not name or key in seen_names:
            raise ValueError(f"deck {position} has a missing or duplicate name")
        seen_names.add(key)
        entries = compact_deck.get("deck")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{name} has no deck entries")
        hydrated_entries = []
        total = 0
        seen_cards = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{name} contains a non-object deck entry")
            card_name = str(entry.get("card", "")).strip()
            card_key = card_name.casefold()
            try:
                count = int(entry.get("count", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name}: invalid count for {card_name}") from exc
            if count < 1:
                raise ValueError(f"{name}: count for {card_name} must be positive")
            if card_key in seen_cards:
                raise ValueError(f"{name}: duplicate entry for {card_name}")
            seen_cards.add(card_key)
            card = cards_by_name.get(card_key)
            if card is None:
                raise ValueError(
                    f"{name}: {card_name!r} is absent from the pinned "
                    f"{effective_format} snapshot")
            hydrated_entries.append({"card": card, "count": count})
            total += count
        if total != 60:
            raise ValueError(f"{name} contains {total} cards; expected exactly 60")
        hydrated.append((
            _deck_filename(position, name),
            {
                "captured_at": corpus.get("captured_at"),
                "deck": hydrated_entries,
                "format": effective_format,
                "meta_share": compact_deck.get("meta_share"),
                "name": name,
                "schema_version": HYDRATED_SCHEMA_VERSION,
                "source": compact_deck.get("source"),
                "source_corpus": corpus_path.name,
            },
        ))

    staging = output.with_name(output.name + ".tmp")
    if staging.exists():
        _require_generated_directory(staging)
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for filename, payload in hydrated:
            (staging / filename).write_bytes(_canonical_json_bytes(payload))
        if output.exists():
            if not replace:
                raise FileExistsError(
                    f"{output} already exists; pass replace=True/--replace")
            _require_generated_directory(output)
            shutil.rmtree(output)
        staging.replace(output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    files = []
    aggregate = hashlib.sha256()
    for path in sorted(output.glob("*.json"), key=lambda item: item.name):
        digest = _sha256(path)
        files.append({
            "name": path.name,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
        })
        aggregate.update(f"{path.name}:{digest}\n".encode("utf-8"))
    return {
        "corpus_sha256": _sha256(corpus_path),
        "deck_count": len(hydrated),
        "files": files,
        "format": effective_format,
        "output": str(output),
        "sha256": aggregate.hexdigest(),
        "snapshot_sha256": _sha256(snapshot_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hydrate a compact metagame corpus from a pinned snapshot")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--format", default=DEFAULT_FORMAT)
    parser.add_argument("--replace", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = hydrate_corpus(
        args.corpus, args.snapshot, args.output, args.format,
        replace=args.replace)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
