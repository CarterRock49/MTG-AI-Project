"""Tests for the full-pool static support preflight."""

from __future__ import annotations

import copy
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
    SEMANTIC_SURFACE_SCHEMA_VERSION,
    _canonical_hash,
    audit_pool_cards,
    build_support_ledger,
    generate_semantic_surface_inventory,
    oracle_rules_sha256,
    test_node_identity,
    validate_verified_evidence,
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


def write_exact_state_test(root: Path):
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    path = tests / "semantic_fixture_test.py"
    path.write_text(
        "import unittest\n\n"
        "class SemanticFixtureTest(unittest.TestCase):\n"
        "    def test_exact_state(self):\n"
        "        observed = {\n"
        "            'card': 'Exact Bear',\n"
        "            'zone': 'battlefield',\n"
        "            'stack': [],\n"
        "        }\n"
        "        self.assertEqual(observed, {\n"
        "            'card': 'Exact Bear',\n"
        "            'zone': 'battlefield',\n"
        "            'stack': [],\n"
        "        })\n"
        "        self.assertEqual(observed['stack'], [])\n",
        encoding="utf-8")
    node_id = (
        "tests/semantic_fixture_test.py::SemanticFixtureTest::"
        "test_exact_state")
    return node_id, test_node_identity(root, node_id)


def verified_record(raw_card, registry_entry, root: Path):
    node_id, identity = write_exact_state_test(root)
    inventory = generate_semantic_surface_inventory(
        raw_card, registry_entry)
    return {
        "oracle_id": raw_card["oracle_id"],
        "oracle_rules_sha256": oracle_rules_sha256(raw_card),
        "surface_schema_version": SEMANTIC_SURFACE_SCHEMA_VERSION,
        "required_surfaces_sha256": inventory["sha256"],
        "scenarios": [{
            **identity,
            "test_node_id": node_id,
            "assertion_contract": "exact_state_v1",
            "covers": [
                surface["id"] for surface in inventory["surfaces"]],
        }],
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
                "schema_version": 2,
                "legacy_verified_claims": ["Verified Bear"],
                "verified": {},
                "excluded": {"Excluded Bear": "manual fidelity exclusion"},
            }), encoding="utf-8")
            ledger = build_support_ledger(
                pool, registry, audit, decks_directory=decks,
                corpus_label="test", overrides_path=overrides)

        by_name = {row["name"]: row for row in ledger["cards"]}
        self.assertEqual(by_name["Clean Bear"]["status"], "observed_clean")
        self.assertEqual(
            by_name["Clean Bear"]["semantic_status"], "unverified")
        self.assertEqual(by_name["Unseen Land"]["status"], "unseen")
        self.assertEqual(by_name["Verified Bear"]["status"], "unseen")
        self.assertTrue(by_name["Verified Bear"]["legacy_verified_claim"])
        self.assertEqual(
            by_name["Verified Bear"]["semantic_status"], "unverified")
        self.assertEqual(by_name["Excluded Bear"]["status"], "excluded")
        self.assertIn(by_name["Broken Discovery"]["status"],
                      ("partial", "unparsed"))
        self.assertEqual(ledger["ranked_cards"][0]["name"],
                         "Broken Discovery")
        discover = next(row for row in ledger["ranked_mechanics"]
                        if row["mechanic"] == "Discover")
        self.assertEqual(discover["deck_slots"], 4)
        self.assertNotIn("qualified_fraction", ledger["summary"])
        self.assertEqual(
            ledger["summary"]["semantic_verified_fraction"], 0.0)
        self.assertEqual(
            ledger["summary"]["legacy_verified_claims"], 1)
        self.assertEqual(ledger["sha256"], _canonical_hash(ledger))

    def test_complete_candidate_evidence_validates_but_promotion_is_locked(self):
        exact = card("Exact Bear", "exact-bear")
        registry = build_registry([exact])
        audit = audit_pool_cards([exact], registry)
        registry_entry = registry["cards"][0]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = verified_record(exact, registry_entry, root)
            overrides = root / "support_overrides.json"
            overrides.write_text(json.dumps({
                "schema_version": 2,
                "legacy_verified_claims": [],
                "verified": {"Exact Bear": record},
                "excluded": {},
            }), encoding="utf-8")
            candidate = validate_verified_evidence(
                exact, registry_entry, record, root)
            self.assertEqual(
                candidate["assertion_contract"], "exact_state_v1")
            self.assertGreater(candidate["required_surface_count"], 0)
            with self.assertRaisesRegex(
                    ValueError, "semantic promotion is disabled"):
                build_support_ledger(
                    [exact], registry, audit, overrides_path=overrides,
                    evidence_root=root)

    def test_required_surfaces_include_independent_oracle_obligations(self):
        triggered = card(
            "Oracle Trigger Bear", "oracle-trigger-bear",
            "Whenever this creature attacks, draw a card.")
        registry = build_registry([triggered])
        inventory = generate_semantic_surface_inventory(
            triggered, registry["cards"][0])
        kinds = {surface["kind"] for surface in inventory["surfaces"]}
        self.assertIn("oracle_rule_line", kinds)
        self.assertIn("oracle_trigger_event", kinds)
        self.assertIn("oracle_trigger_non_event", kinds)
        self.assertIn("oracle_card_cast", kinds)
        self.assertIn("oracle_card_cast_illegal", kinds)

    def test_verified_evidence_fails_closed_when_any_identity_is_stale(self):
        exact = card("Exact Bear", "exact-bear")
        registry = build_registry([exact])
        registry_entry = registry["cards"][0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = verified_record(exact, registry_entry, root)
            cases = (
                ("oracle_id", ("oracle_id", "stale"), "stale oracle_id"),
                ("oracle hash",
                 ("oracle_rules_sha256", "0" * 64),
                 "stale oracle_rules_sha256"),
                ("surface hash",
                 ("required_surfaces_sha256", "0" * 64),
                 "stale required_surfaces_sha256"),
                ("contract",
                 ("scenario", "assertion_contract", "smoke_only"),
                 "exact_state_v1"),
                ("file hash",
                 ("scenario", "test_file_sha256", "0" * 64),
                 "stale test_file_sha256"),
                ("node hash",
                 ("scenario", "test_node_sha256", "0" * 64),
                 "stale test_node_sha256"),
                ("missing file",
                 ("scenario", "test_node_id",
                  "tests/missing_test.py::MissingTest::test_missing"),
                 "does not exist"),
            )
            for label, mutation, message in cases:
                with self.subTest(label=label):
                    record = copy.deepcopy(base)
                    if mutation[0] == "scenario":
                        record["scenarios"][0][mutation[1]] = mutation[2]
                    else:
                        record[mutation[0]] = mutation[1]
                    with self.assertRaisesRegex(ValueError, message):
                        validate_verified_evidence(
                            exact, registry_entry, record, root)

    def test_verified_evidence_requires_exact_surface_set_equality(self):
        exact = card("Exact Bear", "exact-bear")
        registry = build_registry([exact])
        registry_entry = registry["cards"][0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = verified_record(exact, registry_entry, root)
            missing = copy.deepcopy(base)
            missing["scenarios"][0]["covers"].pop()
            with self.assertRaisesRegex(ValueError, "missing "):
                validate_verified_evidence(
                    exact, registry_entry, missing, root)
            unknown = copy.deepcopy(base)
            unknown["scenarios"][0]["covers"].append("claimed:but-unknown")
            with self.assertRaisesRegex(ValueError, "unknown "):
                validate_verified_evidence(
                    exact, registry_entry, unknown, root)

    def test_evidence_node_must_be_active_asserting_and_passing(self):
        cases = (
            (
                "skipped",
                "import unittest\n"
                "class EvidenceTest(unittest.TestCase):\n"
                "    @unittest.skip('not evidence')\n"
                "    def test_exact(self):\n"
                "        self.assertEqual('Exact Bear', 'Exact Bear')\n",
                "inactive",
            ),
            (
                "no_assertion",
                "import unittest\n"
                "class EvidenceTest(unittest.TestCase):\n"
                "    def test_exact(self):\n"
                "        card = 'Exact Bear'\n",
                "no assertion",
            ),
            (
                "failing",
                "import unittest\n"
                "class EvidenceTest(unittest.TestCase):\n"
                "    def test_exact(self):\n"
                "        self.assertEqual('Exact Bear', 'Wrong Bear')\n",
                "did not pass exactly once",
            ),
            (
                "fake_base",
                "class TestCase:\n"
                "    pass\n"
                "class EvidenceTest(TestCase):\n"
                "    def test_exact(self):\n"
                "        assert 'Exact Bear'\n",
                "did not pass exactly once",
            ),
        )
        for label, source, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                tests = root / "tests"
                tests.mkdir()
                path = tests / "evidence_guard_test.py"
                path.write_text(source, encoding="utf-8")
                node_id = (
                    "tests/evidence_guard_test.py::EvidenceTest::"
                    "test_exact")
                with self.assertRaisesRegex(ValueError, message):
                    test_node_identity(
                        root, node_id, required_card_name="Exact Bear",
                        execute=True)

    def test_verified_evidence_cannot_override_static_issues(self):
        broken = card(
            "Broken Claim", "broken-claim",
            "Glimpse beyond implementation.", type_line="Instant")
        registry = build_registry([broken])
        audit = audit_pool_cards([broken], registry)
        self.assertTrue(audit[0]["issues"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            overrides = root / "support_overrides.json"
            overrides.write_text(json.dumps({
                "schema_version": 2,
                "legacy_verified_claims": [],
                "verified": {"Broken Claim": {}},
                "excluded": {},
            }), encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "cannot override static issues"):
                build_support_ledger(
                    [broken], registry, audit, overrides_path=overrides,
                    evidence_root=root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
