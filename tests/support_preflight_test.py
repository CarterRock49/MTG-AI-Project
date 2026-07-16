"""Tests for the full-pool static support preflight."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.card_registry import build_registry  # noqa: E402
from Playersim.support_preflight import (  # noqa: E402
    _canonical_hash,
    audit_pool_cards,
    build_support_ledger,
)


def card(name, oracle_id, oracle_text="", type_line="Creature - Bear",
         keywords=None):
    return {
        "name": name,
        "oracle_id": oracle_id,
        "lang": "en",
        "layout": "normal",
        "mana_cost": "{1}{G}",
        "cmc": 2,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "power": "2" if "Creature" in type_line else None,
        "toughness": "2" if "Creature" in type_line else None,
        "keywords": keywords or [],
        "legalities": {"standard": "legal"},
    }


class SupportPreflightTest(unittest.TestCase):
    def test_delayed_trigger_bodies_are_probed(self):
        delayed = card(
            "Delayed Mystery", "delayed-mystery",
            "Glimpse the uncharted at the beginning of the next end step.",
            type_line="Instant")
        registry = build_registry([delayed])

        rows = audit_pool_cards([delayed], registry)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["issues"])
        self.assertTrue(any(
            "unparsed effect text: Glimpse the uncharted" in issue["reason"]
            for issue in rows[0]["issues"]))

    def test_ledger_distinguishes_evidence_and_ranks_corpus_gaps(self):
        clean = card("Clean Bear", "clean")
        unseen = card("Unseen Land", "unseen", type_line="Basic Land - Forest")
        broken = card(
            "Broken Discovery", "broken", "Discover the gyre and gimble.",
            type_line="Instant", keywords=["Discover"])
        verified = card("Verified Bear", "verified")
        excluded = card("Excluded Bear", "excluded")
        pool = [clean, unseen, broken, verified, excluded]
        registry = build_registry(pool)
        audit = audit_pool_cards(pool, registry)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decks = root / "Decks"
            decks.mkdir()
            (decks / "Corpus.json").write_text(json.dumps({
                "name": "Corpus",
                "deck": [
                    {"count": 4, "card": broken},
                    {"count": 4, "card": clean},
                ],
            }), encoding="utf-8")
            overrides = root / "support_overrides.json"
            overrides.write_text(json.dumps({
                "schema_version": 1,
                "verified": ["Verified Bear"],
                "excluded": {"Excluded Bear": "manual fidelity exclusion"},
            }), encoding="utf-8")
            ledger = build_support_ledger(
                pool, registry, audit, decks_directory=decks,
                corpus_label="test", overrides_path=overrides)

        by_name = {row["name"]: row for row in ledger["cards"]}
        self.assertEqual(by_name["Clean Bear"]["status"], "observed_clean")
        self.assertEqual(by_name["Unseen Land"]["status"], "unseen")
        self.assertEqual(by_name["Verified Bear"]["status"], "verified")
        self.assertEqual(by_name["Excluded Bear"]["status"], "excluded")
        self.assertIn(by_name["Broken Discovery"]["status"],
                      ("partial", "unparsed"))
        self.assertEqual(ledger["ranked_cards"][0]["name"],
                         "Broken Discovery")
        discover = next(row for row in ledger["ranked_mechanics"]
                        if row["mechanic"] == "Discover")
        self.assertEqual(discover["deck_slots"], 4)
        self.assertEqual(ledger["sha256"], _canonical_hash(ledger))


if __name__ == "__main__":
    unittest.main(verbosity=2)
