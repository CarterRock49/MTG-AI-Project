"""Tests for deterministic metagame-corpus hydration."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.deck_corpus import hydrate_corpus  # noqa: E402


def _card(name, oracle_id, *, basic=False):
    kind = "Basic Land — Forest" if basic else "Creature — Bear"
    return {
        "lang": "en", "legalities": {"standard": "legal"},
        "name": name, "oracle_id": oracle_id, "type_line": kind,
        "mana_cost": "" if basic else "{G}", "cmc": 0 if basic else 1,
        "oracle_text": "", "color_identity": ["G"],
    }


class DeckCorpusTest(unittest.TestCase):
    def test_hydrates_names_counts_metadata_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "standard.jsonl"
            snapshot.write_text(
                "\n".join(json.dumps(card) for card in (
                    _card("Forest", "forest", basic=True),
                    _card("Test Bear", "bear"))) + "\n",
                encoding="utf-8")
            corpus = root / "meta.json"
            corpus.write_text(json.dumps({
                "captured_at": "2026-07-11", "format": "standard",
                "decks": [{
                    "name": "Green Test", "meta_share": 1.0,
                    "source": "https://example.test/deck",
                    "deck": [
                        {"count": 56, "card": "Forest"},
                        {"count": 4, "card": "Test Bear"},
                    ],
                }],
            }), encoding="utf-8")
            output = root / "decks"

            first = hydrate_corpus(corpus, snapshot, output, "standard")
            payload = json.loads(next(output.glob("*.json")).read_text(
                encoding="utf-8"))
            self.assertEqual(payload["name"], "Green Test")
            self.assertEqual(payload["source_corpus"], "meta.json")
            self.assertEqual(sum(e["count"] for e in payload["deck"]), 60)
            self.assertIsInstance(payload["deck"][0]["card"], dict)

            second = hydrate_corpus(
                corpus, snapshot, output, "standard", replace=True)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["files"], second["files"])

    def test_rejects_missing_snapshot_card_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "standard.jsonl"
            snapshot.write_text(
                json.dumps(_card("Forest", "forest", basic=True)) + "\n",
                encoding="utf-8")
            corpus = root / "meta.json"
            corpus.write_text(json.dumps({
                "format": "standard", "decks": [{
                    "name": "Broken", "deck": [
                        {"count": 56, "card": "Forest"},
                        {"count": 4, "card": "Missing Bear"},
                    ],
                }],
            }), encoding="utf-8")
            output = root / "decks"
            with self.assertRaisesRegex(ValueError, "absent from the pinned"):
                hydrate_corpus(corpus, snapshot, output, "standard")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
