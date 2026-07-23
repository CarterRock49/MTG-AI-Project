"""Focused tests for the versioned full-deck strategy taxonomy.

Run from the repository root with::

    python tests/archetype_profile_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.archetypes import (  # noqa: E402
    CLASSIFIER_SHA256,
    PROFILE_VECTOR_SIZE,
    STRATEGY_AXES,
    STRATEGY_TAGS,
    TAXONOMY_SHA256,
    TAXONOMY_VERSION,
    PrimaryArchetype,
    classify_full_deck,
    compatibility_primary,
    declared_profile_hash,
    encode_profile,
    normalize_declared_profile,
    taxonomy_identity,
    validate_declared_profile,
)
from Playersim.card import Card, load_decks_and_card_db  # noqa: E402
from Playersim.card_registry import load_format_namespace  # noqa: E402
from Playersim.deck_corpus import hydrate_corpus  # noqa: E402
from Playersim.deck_ingest import ingest_decklist  # noqa: E402
from Playersim.deck_stats_tracker import DeckStatsTracker  # noqa: E402


STANDARD_FORMAT = REPO_ROOT / "formats" / "standard"
STANDARD_DECKS = STANDARD_FORMAT / "decks" / "metagame"


def _reviewed_profile(primary="aggro", secondary="midrange", tags=()):
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "primary": primary,
        "secondary": secondary,
        "tags": list(tags),
        "axes": {name: 50 for name in STRATEGY_AXES},
        "review": {
            "status": "reviewed",
            "reviewed_at": "2026-07-22",
            "basis": "Test fixture reviewed main deck.",
        },
    }


def _card(name, *, type_line, cmc, text="", power=None, toughness=None):
    return Card({
        "name": name,
        "type_line": type_line,
        "mana_cost": "" if "Land" in type_line else "{R}",
        "cmc": cmc,
        "oracle_text": text,
        "power": power,
        "toughness": toughness,
        "color_identity": ["R"],
    })


class TaxonomyContractTest(unittest.TestCase):
    def test_taxonomy_is_closed_versioned_and_hash_stable(self):
        identity = taxonomy_identity()
        self.assertEqual(identity["taxonomy_version"], 1)
        self.assertEqual(identity["sha256"], TAXONOMY_SHA256)
        self.assertEqual(len(TAXONOMY_SHA256), 64)
        self.assertEqual(len(CLASSIFIER_SHA256), 64)
        self.assertEqual(tuple(sorted(STRATEGY_TAGS)), STRATEGY_TAGS)
        self.assertEqual(len(set(STRATEGY_AXES)), len(STRATEGY_AXES))

        normalized = normalize_declared_profile(_reviewed_profile(
            tags=("counters", "go_wide")))
        self.assertEqual(normalized["tags"], ["counters", "go_wide"])
        self.assertEqual(
            declared_profile_hash(normalized),
            declared_profile_hash(_reviewed_profile(
                tags=("counters", "go_wide"))))

    def test_invalid_declared_profiles_fail_closed(self):
        invalid = _reviewed_profile()
        invalid["tags"] = ["not-a-tag"]
        report = validate_declared_profile(invalid)
        self.assertFalse(report.valid)
        self.assertIn("unknown strategy tags", report.errors[0])

        invalid = _reviewed_profile()
        invalid["axes"]["speed"] = 1.5
        with self.assertRaisesRegex(ValueError, "speed.*integer"):
            normalize_declared_profile(invalid)

        invalid = _reviewed_profile()
        invalid["taxonomy_version"] = 99
        with self.assertRaisesRegex(ValueError, "must be 1"):
            normalize_declared_profile(invalid)

    def test_legacy_labels_have_explicit_macro_mapping(self):
        self.assertEqual(
            compatibility_primary("reanimator"), PrimaryArchetype.COMBO)
        self.assertEqual(
            compatibility_primary("lands"), PrimaryArchetype.RAMP)
        self.assertEqual(
            compatibility_primary("stax"), PrimaryArchetype.CONTROL)
        self.assertEqual(
            compatibility_primary("made-up"), PrimaryArchetype.UNKNOWN)


class DeterministicClassifierTest(unittest.TestCase):
    @staticmethod
    def _fixture(offset=0):
        cards = {
            offset + 1: _card(
                "Mountain", type_line="Basic Land — Mountain", cmc=0),
            offset + 2: _card(
                "Swift Threat", type_line="Creature — Warrior", cmc=1,
                text="Haste", power="2", toughness="1"),
            offset + 3: _card(
                "Clean Burn", type_line="Instant", cmc=1,
                text="Clean Burn deals 3 damage to any target."),
        }
        deck = [offset + 1] * 20 + [offset + 2] * 20 + [offset + 3] * 20
        return deck, cards

    def test_order_count_and_card_id_representations_are_invariant(self):
        deck, card_db = self._fixture()
        reversed_profile = classify_full_deck(list(reversed(deck)), card_db)
        counted_profile = classify_full_deck(
            {1: 20, 2: 20, 3: 20}, card_db)
        remapped_deck, remapped_db = self._fixture(100)
        remapped_profile = classify_full_deck(remapped_deck, remapped_db)

        self.assertEqual(reversed_profile.to_dict(), counted_profile.to_dict())
        self.assertEqual(counted_profile.to_dict(), remapped_profile.to_dict())
        self.assertEqual(len(counted_profile.profile_hash), 64)
        self.assertEqual(len(counted_profile.feature_hash), 64)
        self.assertGreater(counted_profile.confidence_bp, 0)
        self.assertIn("burn", counted_profile.tags)
        self.assertEqual(len(encode_profile(counted_profile)), PROFILE_VECTOR_SIZE)

    def test_insufficient_recognized_cards_are_unknown(self):
        profile = classify_full_deck([999] * 60, {})
        self.assertEqual(profile.primary, PrimaryArchetype.UNKNOWN)
        self.assertEqual(profile.confidence_bp, 0)
        self.assertEqual(dict(profile.evidence)["unknown_cards"], 60)

    def test_reviewed_profile_is_authoritative_but_keeps_feature_evidence(self):
        deck, card_db = self._fixture()
        declared = _reviewed_profile(
            primary="control", secondary=None,
            tags=("board_control", "burn"))
        profile = classify_full_deck(deck, card_db, declared=declared)
        self.assertEqual(profile.primary, PrimaryArchetype.CONTROL)
        self.assertEqual(profile.tags, ("board_control", "burn"))
        self.assertEqual(profile.source, "declared_validated")
        self.assertEqual(profile.confidence_bp, 10_000)
        self.assertEqual(dict(profile.evidence)["recognized_cards"], 60)


class StandardCorpusGoldenTest(unittest.TestCase):
    EXPECTED = {
        "Selesnya Ouroboroid": (
            "aggro", "midrange", ("counters", "go_wide", "toolbox")),
        "Jeskai Lessons": (
            "control", None,
            ("board_control", "burn", "lessons", "spellslinger")),
        "Izzet Prowess": (
            "tempo", "aggro", ("burn", "prowess", "spellslinger", "tokens")),
        "4c Control": ("control", None, ("board_control", "burn")),
        "Izzet Spellementals": (
            "tempo", "midrange", ("burn", "graveyard", "spellslinger")),
        "Dimir Excruciator": (
            "combo", "control",
            ("alternate_win", "board_control", "discard", "mill")),
        "Mono-Green Landfall": (
            "ramp", "combo", ("big_mana", "counters", "landfall", "lands")),
        "Azorius Momo": (
            "aggro", "midrange",
            ("blink", "counters", "fliers", "go_wide", "lifegain", "tokens")),
    }

    @classmethod
    def setUpClass(cls):
        registry, schema = load_format_namespace(STANDARD_FORMAT)
        cls.decks, cls.card_db = load_decks_and_card_db(
            STANDARD_DECKS, format_name="standard", strict_legality=True,
            card_registry=registry, feature_schema=schema)

    def test_all_active_decks_preserve_reviewed_golden_profiles(self):
        self.assertEqual({deck["name"] for deck in self.decks}, set(self.EXPECTED))
        for deck in self.decks:
            with self.subTest(deck=deck["name"]):
                primary, secondary, tags = self.EXPECTED[deck["name"]]
                declared = deck["strategy_profile"]
                self.assertEqual(declared["primary"], primary)
                self.assertEqual(declared["secondary"], secondary)
                self.assertEqual(tuple(declared["tags"]), tags)
                self.assertEqual(
                    deck["strategy_profile_hash"],
                    declared_profile_hash(declared))
                classified = classify_full_deck(
                    deck["cards"], self.card_db, declared=declared)
                self.assertEqual(classified.primary.value, primary)
                self.assertEqual(
                    classified.secondary.value
                    if classified.secondary is not None else None,
                    secondary)
                self.assertEqual(classified.tags, tags)

    def test_stats_string_api_uses_reviewed_primary_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            tracker = DeckStatsTracker(
                storage_path=temp, card_db=self.card_db, decks=self.decks,
                use_compression=False)
            actual = {
                deck["name"]: tracker.identify_archetype(deck["cards"])
                for deck in self.decks
            }
        expected = {
            name: values[0] for name, values in self.EXPECTED.items()}
        self.assertEqual(actual, expected)


class ProfileLoaderRoundTripTest(unittest.TestCase):
    @staticmethod
    def _snapshot_card(name, *, basic=False):
        return {
            "lang": "en", "legalities": {"standard": "legal"},
            "name": name, "oracle_id": name.casefold().replace(" ", "-"),
            "type_line": "Basic Land — Forest" if basic else "Creature — Bear",
            "mana_cost": "" if basic else "{G}", "cmc": 0 if basic else 1,
            "power": None if basic else "2", "toughness": None if basic else "2",
            "oracle_text": "", "color_identity": ["G"],
        }

    def _paths(self, root):
        snapshot = root / "standard.jsonl"
        snapshot.write_text("\n".join(json.dumps(card) for card in (
            self._snapshot_card("Forest", basic=True),
            self._snapshot_card("Test Bear"))) + "\n", encoding="utf-8")
        return snapshot, root / "meta.json", root / "decks"

    def test_profile_survives_hydration_and_runtime_loading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, corpus, output = self._paths(root)
            profile = _reviewed_profile(tags=("counters", "go_wide"))
            corpus.write_text(json.dumps({
                "format": "standard", "schema_version": 2,
                "decks": [{
                    "name": "Green Test", "strategy_profile": profile,
                    "deck": [
                        {"count": 56, "card": "Forest"},
                        {"count": 4, "card": "Test Bear"},
                    ],
                }],
            }), encoding="utf-8")
            result = hydrate_corpus(corpus, snapshot, output, "standard")
            hydrated = json.loads(next(output.glob("*.json")).read_text(
                encoding="utf-8"))
            decks, _ = load_decks_and_card_db(output)

        normalized = normalize_declared_profile(profile)
        expected_hash = declared_profile_hash(profile)
        self.assertEqual(hydrated["strategy_profile"], normalized)
        self.assertEqual(hydrated["strategy_profile_hash"], expected_hash)
        self.assertEqual(
            result["files"][0]["strategy_profile_hash"], expected_hash)
        self.assertEqual(decks[0]["strategy_profile"], normalized)
        self.assertEqual(decks[0]["strategy_profile_hash"], expected_hash)

    def test_invalid_v2_profile_fails_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, corpus, output = self._paths(root)
            invalid = _reviewed_profile()
            invalid["axes"].pop("speed")
            corpus.write_text(json.dumps({
                "format": "standard", "schema_version": 2,
                "decks": [{
                    "name": "Broken", "strategy_profile": invalid,
                    "deck": [
                        {"count": 56, "card": "Forest"},
                        {"count": 4, "card": "Test Bear"},
                    ],
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing speed"):
                hydrate_corpus(corpus, snapshot, output, "standard")
            self.assertFalse(output.exists())

    def test_runtime_loader_requires_profiles_for_governed_v2_decks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deck_path = root / "governed.json"
            payload = {
                "format": "standard",
                "name": "Profileless Governed Deck",
                "schema_version": 2,
                "deck": [{
                    "count": 60,
                    "card": self._snapshot_card("Forest", basic=True),
                }],
            }
            deck_path.write_text(
                json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "missing the required reviewed"):
                load_decks_and_card_db(root, strict_legality=True)

            # Explicit user imports remain a rule-inference surface until a
            # human-reviewed profile is supplied.
            payload["kind"] = "imported_deck"
            deck_path.write_text(
                json.dumps(payload), encoding="utf-8")
            decks, _ = load_decks_and_card_db(
                root, strict_legality=True)
            self.assertNotIn("strategy_profile", decks[0])

    def test_json_import_preserves_validated_profile_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lists = root / "lists"
            lists.mkdir()
            (lists / "standard.jsonl").write_text(
                "\n".join(json.dumps(card) for card in (
                    self._snapshot_card("Forest", basic=True),
                    self._snapshot_card("Test Bear"))) + "\n",
                encoding="utf-8")
            profile = _reviewed_profile(tags=("counters", "go_wide"))
            source = root / "profiled.json"
            source.write_text(json.dumps({
                "name": "Profiled Bears",
                "format": "standard",
                "strategy_profile": profile,
                "deck": [
                    {"count": 56, "card": "Forest"},
                    {"count": 4, "card": "Test Bear"},
                ],
            }), encoding="utf-8")
            result = ingest_decklist(
                source, lists_directory=lists,
                formats_root=root / "formats")
            written = json.loads(Path(result["path"]).read_text(
                encoding="utf-8"))
            decks, _ = load_decks_and_card_db(
                root / "formats" / "standard" / "decks")

        normalized = normalize_declared_profile(profile)
        self.assertEqual(written["strategy_profile"], normalized)
        self.assertEqual(
            written["strategy_profile_hash"], declared_profile_hash(profile))
        self.assertEqual(decks[0]["strategy_profile"], normalized)


if __name__ == "__main__":
    unittest.main()
