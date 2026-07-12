"""Tests for legality-based automatic deck ingestion."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.card import load_decks_and_card_db  # noqa: E402
from Playersim.card_registry import load_format_namespace  # noqa: E402
from Playersim.deck_ingest import (  # noqa: E402
    ingest_decklist, legal_formats, main as ingest_main, parse_decklist)


def _card(name, oracle_id, *, basic=False, standard=True, oracle_text=""):
    return {
        "name": name, "oracle_id": oracle_id, "lang": "en",
        "type_line": "Basic Land — Forest" if basic else "Creature — Bear",
        "mana_cost": "" if basic else "{G}", "cmc": 0 if basic else 1,
        "power": None if basic else "2", "toughness": None if basic else "2",
        "oracle_text": oracle_text, "color_identity": ["G"],
        "legalities": {
            "standard": "legal" if standard else "not_legal",
            "pioneer": "legal", "modern": "legal", "legacy": "legal",
            "vintage": "legal",
        },
    }


class DeckIngestTest(unittest.TestCase):
    def test_parses_arena_suffixes_and_ignores_sideboard(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Arena List.txt"
            path.write_text(
                "Deck\n4 Test Bear (TST) 123\n56 Forest\n\n"
                "Sideboard\n2 Side Card (TST) 9\n", encoding="utf-8")
            parsed = parse_decklist(path)
        self.assertEqual(parsed["name"], "Arena List")
        self.assertEqual(parsed["deck"], [
            {"count": 4, "card": "Test Bear"},
            {"count": 56, "card": "Forest"},
        ])
        self.assertEqual(parsed["sideboard_count"], 2)
        self.assertEqual(parsed["sideboard"], [
            {"count": 2, "card": "Side Card"}])

    def test_detects_every_legal_format_and_prefers_standard(self):
        records = {
            "Forest": _card("Forest", "forest", basic=True),
            "Test Bear": _card("Test Bear", "bear"),
        }
        matches = legal_formats([
            {"count": 56, "card": "Forest"},
            {"count": 4, "card": "Test Bear"},
        ], records)
        self.assertEqual(matches[:3], ["standard", "pioneer", "modern"])

    def test_copy_limits_use_canonical_names_and_printed_exceptions(self):
        forest = _card("Forest", "forest", basic=True)
        split = _card("Fire // Ice", "fire-ice")
        records = {"Forest": forest, "Fire": split, "Fire // Ice": split}
        self.assertEqual(legal_formats([
            {"count": 56, "card": "Forest"},
            {"count": 4, "card": "Fire"},
        ], records, sideboard=[
            {"count": 4, "card": "Fire // Ice"},
        ]), [])

        hare = _card(
            "Hare Apparent", "hare",
            oracle_text=("A deck can have any number of cards named "
                         "Hare Apparent."))
        unlimited = {"Forest": forest, "Hare Apparent": hare}
        self.assertIn("standard", legal_formats([
            {"count": 50, "card": "Forest"},
            {"count": 10, "card": "Hare Apparent"},
        ], unlimited))

        dwarves = _card(
            "Seven Dwarves", "dwarves",
            oracle_text=("A deck can have up to seven cards named "
                         "Seven Dwarves."))
        capped = {"Forest": forest, "Seven Dwarves": dwarves}
        self.assertIn("standard", legal_formats([
            {"count": 53, "card": "Forest"},
            {"count": 7, "card": "Seven Dwarves"},
        ], capped))
        self.assertEqual(legal_formats([
            {"count": 52, "card": "Forest"},
            {"count": 8, "card": "Seven Dwarves"},
        ], capped), [])

    def test_printed_copy_exception_survives_runtime_load(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lists = root / "lists"
            lists.mkdir()
            cards = (
                _card("Forest", "forest", basic=True),
                _card(
                    "Hare Apparent", "hare",
                    oracle_text=("A deck can have any number of cards named "
                                 "Hare Apparent.")),
            )
            (lists / "standard.jsonl").write_text(
                "\n".join(json.dumps(card) for card in cards) + "\n",
                encoding="utf-8")
            source = root / "hares.txt"
            source.write_text(
                "10 Hare Apparent\n50 Forest\n", encoding="utf-8")
            formats = root / "formats"
            result = ingest_decklist(
                source, lists_directory=lists, formats_root=formats)
            registry, schema = load_format_namespace(formats / "standard")
            decks, _ = load_decks_and_card_db(
                formats / "standard" / "decks", format_name="standard",
                strict_legality=True, card_registry=registry,
                feature_schema=schema)
        self.assertTrue(result["written"])
        self.assertEqual(len(decks[0]["cards"]), 60)

    def test_commander_section_is_rejected_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "commander.txt"
            source.write_text(
                "Commander\n1 Test Bear\n\nDeck\n59 Forest\n",
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Commander.*not supported"):
                parse_decklist(source)

    def test_ingests_bootstraps_namespace_and_loads_recursively(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lists = root / "Format Card Lists"
            lists.mkdir()
            cards = (
                _card("Forest", "forest", basic=True),
                _card("Test Bear", "bear"),
            )
            for format_name in ("standard", "pioneer", "modern", "legacy", "vintage"):
                (lists / f"{format_name}.jsonl").write_text(
                    "\n".join(json.dumps(card) for card in cards) + "\n",
                    encoding="utf-8")
            source = root / "My Bears.txt"
            source.write_text("4 Test Bear\n56 Forest\n", encoding="utf-8")
            formats = root / "formats"
            result = ingest_decklist(
                source, lists_directory=lists, formats_root=formats)
            self.assertEqual(result["format"], "standard")
            imported = Path(result["path"])
            self.assertTrue(imported.is_file())
            registry, schema = load_format_namespace(formats / "standard")
            decks, card_db = load_decks_and_card_db(
                formats / "standard" / "decks", format_name="standard",
                strict_legality=True, card_registry=registry,
                feature_schema=schema)
        self.assertEqual([deck["name"] for deck in decks], ["My Bears"])
        self.assertEqual(len(decks[0]["cards"]), 60)
        self.assertEqual(len(card_db), 2)

    def test_requested_format_must_be_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lists = root / "lists"
            lists.mkdir()
            forest = _card("Forest", "forest", basic=True)
            bear = _card("Old Bear", "old-bear", standard=False)
            (lists / "standard.jsonl").write_text(
                json.dumps(forest) + "\n", encoding="utf-8")
            (lists / "pioneer.jsonl").write_text(
                json.dumps(forest) + "\n" + json.dumps(bear) + "\n",
                encoding="utf-8")
            source = root / "old.txt"
            source.write_text("4 Old Bear\n56 Forest\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not legal.*standard"):
                ingest_decklist(
                    source, format_name="standard", lists_directory=lists,
                    formats_root=root / "formats")

    def test_dry_run_detects_without_writing_or_bootstrapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lists = root / "lists"
            lists.mkdir()
            cards = (
                _card("Forest", "forest", basic=True),
                _card("Test Bear", "bear"),
            )
            (lists / "standard.jsonl").write_text(
                "\n".join(json.dumps(card) for card in cards) + "\n",
                encoding="utf-8")
            source = root / "dry.txt"
            source.write_text("4 Test Bear\n56 Forest\n", encoding="utf-8")
            formats = root / "formats"
            result = ingest_decklist(
                source, lists_directory=lists, formats_root=formats,
                dry_run=True)
            self.assertFalse(result["written"])
            self.assertEqual(result["format"], "standard")
            self.assertFalse(formats.exists())

    def test_incomplete_namespace_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lists = root / "lists"
            lists.mkdir()
            cards = (
                _card("Forest", "forest", basic=True),
                _card("Test Bear", "bear"),
            )
            (lists / "standard.jsonl").write_text(
                "\n".join(json.dumps(card) for card in cards) + "\n",
                encoding="utf-8")
            source = root / "deck.txt"
            source.write_text("4 Test Bear\n56 Forest\n", encoding="utf-8")
            namespace = root / "formats" / "standard"
            namespace.mkdir(parents=True)
            registry = namespace / "card_registry.json"
            registry.write_text("surviving canonical file", encoding="utf-8")
            before = registry.read_bytes()
            with self.assertRaisesRegex(FileNotFoundError, "incomplete"):
                ingest_decklist(
                    source, lists_directory=lists,
                    formats_root=root / "formats")
            self.assertEqual(registry.read_bytes(), before)
            self.assertFalse((namespace / "feature_schema.json").exists())

    def test_replace_rejects_slug_collision_and_dry_run_checks_conflicts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lists = root / "lists"
            lists.mkdir()
            cards = (
                _card("Forest", "forest", basic=True),
                _card("Test Bear", "bear"),
            )
            (lists / "standard.jsonl").write_text(
                "\n".join(json.dumps(card) for card in cards) + "\n",
                encoding="utf-8")
            source = root / "deck.txt"
            source.write_text("4 Test Bear\n56 Forest\n", encoding="utf-8")
            formats = root / "formats"
            first = ingest_decklist(
                source, deck_name="A B", lists_directory=lists,
                formats_root=formats)
            original = Path(first["path"]).read_bytes()
            with self.assertRaisesRegex(FileExistsError, "collides"):
                ingest_decklist(
                    source, deck_name="A-B", lists_directory=lists,
                    formats_root=formats, replace=True)
            self.assertEqual(Path(first["path"]).read_bytes(), original)
            with self.assertRaisesRegex(FileExistsError, "use --replace"):
                ingest_decklist(
                    source, deck_name="A B", lists_directory=lists,
                    formats_root=formats, dry_run=True)

    def test_json_declared_format_and_maybeboard_are_respected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "modern.json"
            source.write_text(json.dumps({
                "name": "Declared Modern", "format": "modern",
                "deck": [
                    {"count": 4, "card": "Test Bear"},
                    {"count": 56, "card": "Forest"},
                ],
                "maybeboard": [
                    {"count": 20, "card": "Test Bear"},
                ],
            }), encoding="utf-8")
            parsed = parse_decklist(source)
            self.assertEqual(parsed["declared_format"], "modern")
            self.assertEqual(parsed["sideboard_count"], 0)
            self.assertEqual(parsed["maybeboard_count"], 20)

            lists = root / "lists"
            lists.mkdir()
            cards = (
                _card("Forest", "forest", basic=True),
                _card("Test Bear", "bear"),
            )
            for format_name in ("standard", "pioneer", "modern"):
                (lists / f"{format_name}.jsonl").write_text(
                    "\n".join(json.dumps(card) for card in cards) + "\n",
                    encoding="utf-8")
            result = ingest_decklist(
                source, lists_directory=lists,
                formats_root=root / "formats", dry_run=True)
            self.assertEqual(result["format"], "modern")
            self.assertEqual(result["maybeboard_ignored"], 20)

    def test_json_rejects_fractional_and_boolean_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for position, invalid in enumerate((1.5, True)):
                source = root / f"invalid-{position}.json"
                source.write_text(json.dumps({
                    "deck": [{"count": invalid, "card": "Forest"}],
                }), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid count"):
                    parse_decklist(source)

    def test_rejects_typo_sized_deck_before_expanding_occurrences(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "huge.txt"
            source.write_text("999999999 Forest\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safety limit"):
                ingest_decklist(
                    source, lists_directory=root / "missing-lists",
                    formats_root=root / "formats")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = ingest_main([
                    str(source), "--format-lists", str(root / "missing-lists"),
                    "--formats-root", str(root / "formats")])
            self.assertEqual(exit_code, 1)
            self.assertIn("ERROR:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
