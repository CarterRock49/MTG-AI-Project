"""Tests for the canonical card registry and frozen feature schema.

Run from the repository root with::

    python tests/card_registry_test.py
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

from Playersim import card_registry as registry_module  # noqa: E402
from Playersim.card import Card, load_decks_and_card_db  # noqa: E402
from Playersim.observation_schema import CORRECTED_V3_SEMANTICS  # noqa: E402


def _card_data(name, oracle_id, type_line="Creature — Bear", mana_cost="{1}{G}",
               power="2", toughness="2", oracle_text=""):
    return {
        "name": name,
        "oracle_id": oracle_id,
        "type_line": type_line,
        "mana_cost": mana_cost,
        "cmc": 2,
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle_text,
        "color_identity": ["G"],
        "legalities": {"standard": "legal", "modern": "legal"},
    }


FOREST = _card_data(
    "Forest", "forest-oracle-id", type_line="Basic Land — Forest",
    mana_cost="", power=None, toughness=None,
    oracle_text="({T}: Add {G}.)")
BEAR = _card_data("Test Bear", "bear-oracle-id")
WOLF = _card_data(
    "Arctic Wolf", "wolf-oracle-id", type_line="Creature — Wolf")


def _write_deck(directory: Path, deck_name: str, entries) -> None:
    payload = {"deck": [
        {"count": count, "card": card} for count, card in entries
    ]}
    (directory / f"{deck_name}.json").write_text(
        json.dumps(payload), encoding="utf-8")


class SubtypeVocabGuard(unittest.TestCase):
    """Every test must leave the global Card.SUBTYPE_VOCAB untouched."""

    def setUp(self):
        self._saved_vocab = list(Card.SUBTYPE_VOCAB)

    def tearDown(self):
        Card.SUBTYPE_VOCAB = self._saved_vocab


class CanonicalRegistryTest(SubtypeVocabGuard):
    def test_registry_is_deterministic_and_order_independent(self):
        first = registry_module.build_registry([BEAR, FOREST, WOLF])
        second = registry_module.build_registry([WOLF, BEAR, FOREST])
        self.assertEqual(first, second)
        names = [entry["name"] for entry in first["cards"]]
        self.assertEqual(names, ["Arctic Wolf", "Forest", "Test Bear"])
        self.assertEqual(
            [entry["index"] for entry in first["cards"]], [0, 1, 2])
        self.assertEqual(first["cards"][1]["oracle_id"], "forest-oracle-id")
        self.assertTrue(first["sha256"])

    def test_registry_rejects_conflicting_oracle_ids(self):
        conflicting = dict(BEAR, oracle_id="a-different-oracle-id")
        with self.assertRaisesRegex(ValueError, "oracle_id"):
            registry_module.build_registry([BEAR, conflicting])

    def test_registry_roundtrip_and_tamper_detection(self):
        registry = registry_module.build_registry([BEAR, FOREST])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "card_registry.json"
            registry_module.write_registry(path, registry)
            loaded = registry_module.load_registry(path)
            self.assertEqual(loaded, registry)

            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["cards"][0]["name"] = "Renamed"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash"):
                registry_module.load_registry(path)

    def test_extend_registry_keeps_existing_indices_stable(self):
        registry = registry_module.build_registry([FOREST, WOLF])
        extended = registry_module.extend_registry(registry, [BEAR, WOLF])
        by_name = {entry["name"]: entry["index"]
                   for entry in extended["cards"]}
        original = {entry["name"]: entry["index"]
                    for entry in registry["cards"]}
        for name, index in original.items():
            self.assertEqual(by_name[name], index)
        self.assertEqual(by_name["Test Bear"], len(original))
        self.assertNotEqual(extended["sha256"], registry["sha256"])
        # The original registry object is never mutated.
        self.assertEqual(len(registry["cards"]), 2)


class FeatureSchemaTest(SubtypeVocabGuard):
    def test_schema_records_layout_and_feature_dim(self):
        card_db = {
            0: Card(dict(BEAR)),
            1: Card(dict(WOLF)),
        }
        schema = registry_module.build_feature_schema(card_db)
        self.assertEqual(schema["keywords"], list(Card.ALL_KEYWORDS))
        self.assertEqual(schema["subtype_vocab"], ["bear", "wolf"])
        expected_dim = 4 + 6 + len(Card.ALL_KEYWORDS) + 5 + 2 + 3
        self.assertEqual(schema["feature_dim"], expected_dim)
        self.assertTrue(schema["sha256"])

    def test_schema_roundtrip_apply_and_keyword_guard(self):
        card_db = {0: Card(dict(BEAR))}
        schema = registry_module.build_feature_schema(card_db)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "feature_schema.json"
            registry_module.write_feature_schema(path, schema)
            loaded = registry_module.load_feature_schema(path)
            self.assertEqual(loaded, schema)

        registry_module.apply_feature_schema(schema)
        self.assertEqual(Card.SUBTYPE_VOCAB, ["bear"])

        incompatible = dict(schema, keywords=["flying"])
        with self.assertRaisesRegex(ValueError, "keyword"):
            registry_module.apply_feature_schema(incompatible)

    def test_validation_reports_out_of_vocabulary_subtypes(self):
        schema = registry_module.build_feature_schema({0: Card(dict(BEAR))})
        card_db = {0: Card(dict(BEAR)), 1: Card(dict(WOLF))}
        errors = registry_module.validate_cards_against_schema(card_db, schema)
        self.assertEqual(len(errors), 1)
        self.assertIn("Arctic Wolf", errors[0])
        self.assertIn("wolf", errors[0])
        self.assertEqual(
            registry_module.validate_cards_against_schema(
                {0: Card(dict(BEAR))}, schema),
            [])

    def test_frozen_vocabulary_keeps_feature_width_stable(self):
        larger_pool = {0: Card(dict(BEAR)), 1: Card(dict(WOLF))}
        schema = registry_module.build_feature_schema(larger_pool)
        registry_module.apply_feature_schema(schema)
        bear_only = Card(dict(BEAR))
        bear_only.compute_subtype_vector()
        vector = bear_only.to_feature_vector()
        self.assertEqual(len(vector), schema["feature_dim"])


class LoaderIntegrationTest(SubtypeVocabGuard):
    def _decks_directory(self, temp: Path, include_wolf_deck: bool) -> Path:
        decks = temp / "Decks"
        decks.mkdir(parents=True)
        _write_deck(decks, "MonoForest", [(60, FOREST)])
        _write_deck(decks, "BearDeck", [(56, FOREST), (4, BEAR)])
        if include_wolf_deck:
            _write_deck(decks, "WolfDeck", [(56, FOREST), (4, WOLF)])
        return decks

    def test_registry_assigns_stable_ids_across_corpus_subsets(self):
        registry = registry_module.build_registry([FOREST, BEAR, WOLF])
        with tempfile.TemporaryDirectory() as temp:
            full = self._decks_directory(Path(temp) / "full", True)
            subset = self._decks_directory(Path(temp) / "subset", False)
            _, full_db = load_decks_and_card_db(
                str(full), strict_legality=True, card_registry=registry)
            _, subset_db = load_decks_and_card_db(
                str(subset), strict_legality=True, card_registry=registry)
        by_index = {entry["name"].lower(): entry["index"]
                    for entry in registry["cards"]}
        for card_db in (full_db, subset_db):
            for card_id, card in card_db.items():
                self.assertEqual(card_id, by_index[card.name.lower()])
                self.assertEqual(card.card_id, card_id)

    def test_registry_rejects_unknown_corpus_cards(self):
        registry = registry_module.build_registry([FOREST])
        with tempfile.TemporaryDirectory() as temp:
            decks = self._decks_directory(Path(temp), False)
            with self.assertRaisesRegex(ValueError, "Test Bear"):
                load_decks_and_card_db(
                    str(decks), strict_legality=True, card_registry=registry)

    def test_loader_applies_frozen_feature_schema(self):
        pool_db = {0: Card(dict(FOREST)), 1: Card(dict(BEAR)),
                   2: Card(dict(WOLF))}
        schema = registry_module.build_feature_schema(pool_db)
        with tempfile.TemporaryDirectory() as temp:
            decks = self._decks_directory(Path(temp), False)
            _, card_db = load_decks_and_card_db(
                str(decks), strict_legality=True, feature_schema=schema)
        self.assertEqual(Card.SUBTYPE_VOCAB, schema["subtype_vocab"])
        any_card = next(iter(card_db.values()))
        self.assertEqual(
            len(any_card.to_feature_vector()), schema["feature_dim"])

    def test_loader_rejects_cards_outside_frozen_schema(self):
        schema = registry_module.build_feature_schema(
            {0: Card(dict(FOREST))})
        with tempfile.TemporaryDirectory() as temp:
            decks = self._decks_directory(Path(temp), False)
            with self.assertRaisesRegex(ValueError, "bear"):
                load_decks_and_card_db(
                    str(decks), strict_legality=True, feature_schema=schema)

    def test_card_retains_oracle_id(self):
        card = Card(dict(BEAR))
        self.assertEqual(card.oracle_id, "bear-oracle-id")


class LineageIdentityTest(SubtypeVocabGuard):
    def test_corpus_identity_hashes_deck_files(self):
        with tempfile.TemporaryDirectory() as temp:
            decks = Path(temp) / "Decks"
            decks.mkdir()
            _write_deck(decks, "MonoForest", [(60, FOREST)])
            _write_deck(decks, "BearDeck", [(56, FOREST), (4, BEAR)])
            identity = registry_module.corpus_identity(decks)
            self.assertEqual(
                [entry["name"] for entry in identity["files"]],
                ["BearDeck.json", "MonoForest.json"])
            self.assertTrue(all(entry["sha256"] for entry in identity["files"]))
            first_hash = identity["sha256"]

            # Adding a deck changes the corpus hash.
            _write_deck(decks, "WolfDeck", [(56, FOREST), (4, WOLF)])
            self.assertNotEqual(
                registry_module.corpus_identity(decks)["sha256"], first_hash)

    def test_pool_snapshot_identity_reads_format_list(self):
        with tempfile.TemporaryDirectory() as temp:
            lists = Path(temp) / "Format Card Lists"
            lists.mkdir()
            snapshot = lists / "standard.jsonl"
            snapshot.write_text(json.dumps(FOREST) + "\n", encoding="utf-8")
            identity = registry_module.pool_snapshot_identity(
                "standard", lists_directory=lists)
            self.assertEqual(identity["format"], "standard")
            self.assertTrue(identity["sha256"])
            with self.assertRaisesRegex(FileNotFoundError, "modern"):
                registry_module.pool_snapshot_identity(
                    "modern", lists_directory=lists)

    def test_format_lineage_bundles_all_identities(self):
        registry = registry_module.build_registry([FOREST, BEAR])
        schema = registry_module.build_feature_schema(
            {0: Card(dict(FOREST)), 1: Card(dict(BEAR))})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decks = root / "Decks"
            decks.mkdir()
            _write_deck(decks, "MonoForest", [(60, FOREST)])
            lists = root / "Format Card Lists"
            lists.mkdir()
            (lists / "standard.jsonl").write_text(
                json.dumps(FOREST) + "\n", encoding="utf-8")
            lineage = registry_module.format_lineage(
                decks_directory=decks,
                format_name="standard",
                card_registry=registry,
                feature_schema=schema,
                lists_directory=lists)
        self.assertEqual(lineage["format"], "standard")
        self.assertEqual(lineage["corpus"]["directory"], "Decks")
        self.assertEqual(
            lineage["card_registry"]["sha256"], registry["sha256"])
        self.assertEqual(
            lineage["feature_schema"]["sha256"], schema["sha256"])
        self.assertEqual(
            lineage["feature_schema"]["feature_dim"], schema["feature_dim"])
        self.assertEqual(
            lineage["observation_schema"]["schema_version"], 3)
        self.assertEqual(
            lineage["observation_schema"]["sha256"],
            "73b7e83d99664b65c4fbdbcbc4a1fba4a8cf26576d6f66e3e9548306a5865487")
        self.assertIn(
            "multi_turn_plan_uses_live_spendable_mana",
            CORRECTED_V3_SEMANTICS)
        self.assertEqual(lineage["pool_snapshot"]["format"], "standard")

        # Format-free lineage still records the corpus identity.
        with tempfile.TemporaryDirectory() as temp:
            decks = Path(temp) / "Decks"
            decks.mkdir()
            _write_deck(decks, "MonoForest", [(60, FOREST)])
            unformatted = registry_module.format_lineage(
                decks_directory=decks)
        self.assertIsNone(unformatted["format"])
        self.assertIsNone(unformatted["pool_snapshot"])
        self.assertIsNone(unformatted["card_registry"])
        self.assertIsNone(unformatted["feature_schema"])
        self.assertEqual(
            unformatted["observation_schema"],
            lineage["observation_schema"])
        self.assertTrue(unformatted["corpus"]["sha256"])


class FreezeWorkflowTest(SubtypeVocabGuard):
    def test_full_pool_freeze_preserves_indices_and_filters_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "formats" / "standard"
            old_registry = registry_module.build_registry([FOREST])
            old_schema = registry_module.build_feature_schema(
                {0: Card(dict(FOREST))})
            registry_module.write_registry(
                output / "card_registry.json", old_registry)
            registry_module.write_feature_schema(
                output / "feature_schema.json", old_schema)

            illegal = dict(WOLF)
            illegal["name"] = "Illegal Wolf"
            illegal["oracle_id"] = "illegal-wolf"
            illegal["legalities"] = {"standard": "not_legal"}
            foreign = dict(WOLF)
            foreign["name"] = "Foreign Wolf"
            foreign["oracle_id"] = "foreign-wolf"
            foreign["lang"] = "ja"
            snapshot = root / "standard.jsonl"
            snapshot.write_text("\n".join(
                json.dumps(card) for card in (BEAR, WOLF, illegal, foreign)
            ) + "\n", encoding="utf-8")

            result = registry_module.freeze_format_pool_namespace(
                snapshot, output, format_name="standard")
            registry = registry_module.load_registry(
                output / "card_registry.json")
            schema = registry_module.load_feature_schema(
                output / "feature_schema.json")
            by_name = {entry["name"]: entry["index"]
                       for entry in registry["cards"]}
            self.assertEqual(by_name["Forest"], 0)
            self.assertEqual(set(by_name), {"Forest", "Test Bear", "Arctic Wolf"})
            self.assertEqual(result["pool_cards"], 2)
            self.assertEqual(result["historical_cards"], 1)
            self.assertEqual(schema["schema_version"], 2)
            self.assertIn("bear", schema["subtype_vocab"])
            self.assertIn("wolf", schema["subtype_vocab"])

    def test_freeze_writes_registry_and_schema_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decks = root / "Decks"
            decks.mkdir()
            _write_deck(decks, "MonoForest", [(60, FOREST)])
            _write_deck(decks, "BearDeck", [(56, FOREST), (4, BEAR)])
            output = root / "formats" / "standard"
            result = registry_module.freeze_format_namespace(
                decks_directory=decks, output_directory=output)
            registry = registry_module.load_registry(
                output / "card_registry.json")
            schema = registry_module.load_feature_schema(
                output / "feature_schema.json")
            self.assertEqual(result["card_registry"]["sha256"],
                             registry["sha256"])
            self.assertEqual(result["feature_schema"]["sha256"],
                             schema["sha256"])
            self.assertEqual(
                [entry["name"] for entry in registry["cards"]],
                ["Forest", "Test Bear"])
            with self.assertRaisesRegex(FileExistsError, "card_registry"):
                registry_module.freeze_format_namespace(
                    decks_directory=decks, output_directory=output)

    def test_extend_freeze_appends_only_new_cards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decks = root / "Decks"
            decks.mkdir()
            _write_deck(decks, "MonoForest", [(60, FOREST)])
            output = root / "formats" / "standard"
            registry_module.freeze_format_namespace(
                decks_directory=decks, output_directory=output)
            before = registry_module.load_registry(
                output / "card_registry.json")

            # A new deck arrives whose subtypes are already in the schema.
            _write_deck(decks, "MoreForests", [(60, FOREST)])
            registry_module.freeze_format_namespace(
                decks_directory=decks, output_directory=output, extend=True)
            after = registry_module.load_registry(
                output / "card_registry.json")
            self.assertEqual(after, before)

            # A deck with a subtype outside the frozen schema must be loud.
            _write_deck(decks, "WolfDeck", [(56, FOREST), (4, WOLF)])
            with self.assertRaisesRegex(ValueError, "wolf"):
                registry_module.freeze_format_namespace(
                    decks_directory=decks, output_directory=output,
                    extend=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
