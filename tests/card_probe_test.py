"""Focused regression tests for the fail-closed dynamic card probe."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.card_probe import (  # noqa: E402
    _DiagnosticCapture,
    _build_probe_state,
    _console_safe,
    _input_identity,
    _resume_result,
    _zone_invariant_issues,
    probe_card,
    select_pool_cards,
)
import Playersim.card_probe as card_probe_module  # noqa: E402
from Playersim.card_support import get_manifest, report_unsupported  # noqa: E402
from Playersim.card_registry import (  # noqa: E402
    load_pool_snapshot_cards,
    load_registry,
)


class CardProbeTest(unittest.TestCase):
    TRUSTED_DIAGNOSTIC_REGRESSIONS = (
        "Afterburner Expert",
        "Burst Lightning",
        "Consult the Star Charts",
        "Firebending Lesson",
        "Into the Flood Maw",
        "Kaito, Bane of Nightmares",
        "Parting Gust",
        "Patchwork Beastie",
        "Ride's End",
        "Slagstorm",
        "Three Steps Ahead",
        "Thunder Magic",
        "Torch the Tower",
        "Winternight Stories",
    )

    @classmethod
    def setUpClass(cls):
        cls.snapshot = REPO_ROOT / "Format Card Lists" / "standard.jsonl"
        cls.registry_path = REPO_ROOT / "formats" / "standard" / "card_registry.json"
        cls.ledger_path = REPO_ROOT / "formats" / "standard" / "support_ledger.json"
        cls.raw_cards = load_pool_snapshot_cards(cls.snapshot, format_name="standard")
        cls.registry = load_registry(cls.registry_path)
        cls.ledger = json.loads(cls.ledger_path.read_text(encoding="utf-8"))
        selected = select_pool_cards(
            cls.raw_cards, cls.registry, cls.ledger,
            card_names={
                "Forest", "Opt", "Prismari Charm", "Coeurl",
                *cls.TRUSTED_DIAGNOSTIC_REGRESSIONS,
            })
        cls.entries = {entry["name"]: entry for entry in selected}
        cls.identity = {"test_input": "card_probe_test"}

    def test_selection_is_registry_ordered_and_sharded(self):
        raw = [{"name": "C"}, {"name": "A"}, {"name": "B"}]
        registry = {"cards": [
            {"name": "A", "index": 0},
            {"name": "B", "index": 1},
            {"name": "C", "index": 2},
        ]}
        ledger = {"cards": [
            {"name": "A", "status": "unseen"},
            {"name": "B", "status": "verified"},
            {"name": "C", "status": "unseen"},
        ]}

        selected = select_pool_cards(
            raw, registry, ledger, statuses={"unseen"},
            shard_index=0, shard_count=2)

        self.assertEqual([entry["name"] for entry in selected], ["A", "C"])
        self.assertEqual([entry["index"] for entry in selected], [0, 2])

    def test_land_primary_and_matched_negative_have_state_evidence(self):
        result = probe_card(
            self.entries["Forest"], input_identity=self.identity,
            max_decisions=24, max_branches=8)

        self.assertEqual(result["runtime_status"], "execution_passed")
        self.assertEqual(result["semantic_status"], "unverified")
        obligations = {row["id"]: row for row in result["obligations"]}
        self.assertEqual(obligations["primary"]["status"], "exercised")
        self.assertEqual(
            obligations["primary:negative_mask"]["status"], "exercised")
        paths = {row["id"]: row for row in result["paths"]}
        self.assertIsNotNone(
            paths["primary:negative_mask"]["positive_control_action"])
        self.assertIn("state_delta", paths["primary"])
        self.assertEqual(
            paths["primary"]["state_delta"]["final"]["stack_depth"], 0)

    def test_scry_alternatives_remain_a_coverage_gap(self):
        result = probe_card(
            self.entries["Opt"], input_identity=self.identity,
            max_decisions=24, max_branches=8)

        self.assertEqual(result["runtime_status"], "coverage_gap")
        choices = [row for row in result["obligations"]
                   if row["kind"] == "choice_branch"]
        self.assertTrue(any(
            row.get("decision_kind") == "scry"
            and row.get("unvisited_branch_count", 0) >= 1
            for row in choices))

    def test_ordinary_modal_card_cannot_pass_on_one_mode(self):
        result = probe_card(
            self.entries["Prismari Charm"], input_identity=self.identity,
            max_decisions=24, max_branches=8)

        modal = [row for row in result["obligations"]
                 if row["id"].startswith("mode:modal:")]
        self.assertGreaterEqual(len(modal), 3)
        self.assertTrue(all(row["status"] == "coverage_gap" for row in modal))
        self.assertEqual(result["runtime_status"], "coverage_gap")

    def test_registered_activation_has_positive_and_negative_controls(self):
        result = probe_card(
            self.entries["Coeurl"], input_identity=self.identity,
            max_decisions=24, max_branches=8)
        obligations = {row["id"]: row for row in result["obligations"]}

        self.assertEqual(obligations["activated:0"]["status"], "exercised")
        self.assertEqual(
            obligations["activated:0:negative_mask"]["status"], "exercised")
        paths = {row["id"]: row for row in result["paths"]}
        self.assertEqual(len(paths["activated:0"]["mana_setup_land_ids"]), 2)
        self.assertIsNotNone(paths[
            "activated:0:negative_mask"]["positive_control_dispatch"])
        self.assertIn("state_delta", paths["activated:0"])

    def test_formerly_trusted_cards_emit_no_runtime_diagnostics(self):
        for name in self.TRUSTED_DIAGNOSTIC_REGRESSIONS:
            with self.subTest(card=name):
                result = probe_card(
                    self.entries[name], input_identity=self.identity,
                    max_decisions=48, max_branches=8)
                self.assertEqual(result["diagnostics"], [])
                self.assertFalse(any(
                    issue.get("surface") == "diagnostics"
                    for issue in result["issues"]))
                primary = next(
                    path for path in result["paths"]
                    if path["id"] == "primary")
                self.assertEqual(primary["status"], "exercised")

    def test_resume_rejects_limit_or_oracle_changes(self):
        entry = self.entries["Forest"]
        result = probe_card(
            entry, input_identity=self.identity,
            max_decisions=24, max_branches=8)
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "forest.json"
            artifact.write_text(json.dumps(result), encoding="utf-8")
            self.assertIsNotNone(_resume_result(
                artifact, self.identity, entry,
                max_decisions=24, max_branches=8))
            self.assertIsNone(_resume_result(
                artifact, self.identity, entry,
                max_decisions=25, max_branches=8))
            changed = copy.deepcopy(entry)
            changed["raw"]["oracle_text"] = "Changed oracle row"
            self.assertIsNone(_resume_result(
                artifact, self.identity, changed,
                max_decisions=24, max_branches=8))

    def test_partial_ledger_evidence_is_fail_closed(self):
        entry = copy.deepcopy(self.entries["Forest"])
        entry["ledger_status"] = "partial"
        entry["ledger_issues"] = [{
            "severity": "partial", "reason": "synthetic known parser gap"}]

        result = probe_card(
            entry, input_identity=self.identity,
            max_decisions=24, max_branches=8)

        self.assertEqual(result["runtime_status"], "failed")
        evidence = [row for row in result["obligations"]
                    if row["kind"] == "static_preflight_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["status"], "failed")
        self.assertIn("synthetic known parser gap", evidence[0]["reason"])

    def test_unseen_ledger_evidence_cannot_execution_pass(self):
        entry = copy.deepcopy(self.entries["Forest"])
        entry["ledger_status"] = "unseen"
        entry["ledger_issues"] = []

        result = probe_card(
            entry, input_identity=self.identity,
            max_decisions=24, max_branches=8)

        self.assertEqual(result["runtime_status"], "coverage_gap")
        evidence = [row for row in result["obligations"]
                    if row["kind"] == "static_preflight_evidence"]
        self.assertEqual(len(evidence), 1)

    def test_original_card_conservation_detects_silent_loss(self):
        with _DiagnosticCapture():
            game_state, _, fixture = _build_probe_state(
                self.entries["Forest"], target_zone="hand")
        target_id = fixture["target_id"]
        game_state.p1["hand"].remove(target_id)

        issues = _zone_invariant_issues(
            game_state, fixture["initial_card_ids"])

        self.assertTrue(any(
            str(target_id) in issue and "disappeared" in issue
            for issue in issues))

    def test_probe_restores_python_and_numpy_rng_state(self):
        python_before = random.getstate()
        numpy_before = np.random.get_state()

        probe_card(
            self.entries["Forest"], input_identity=self.identity,
            max_decisions=24, max_branches=8)

        self.assertEqual(random.getstate(), python_before)
        numpy_after = np.random.get_state()
        self.assertEqual(numpy_after[0], numpy_before[0])
        np.testing.assert_array_equal(numpy_after[1], numpy_before[1])
        self.assertEqual(numpy_after[2:], numpy_before[2:])

    def test_console_output_escapes_names_outside_windows_code_page(self):
        rendered = _console_safe(
            "Hō", stream=SimpleNamespace(encoding="cp1252"))

        self.assertEqual(rendered, r"H\u014d")

    def test_card_support_manifest_is_scoped_and_failed_on_delta(self):
        report_unsupported("Stale Process Entry", "must be reset", "partial")
        original = card_probe_module._discover_obligations

        def emit_during_probe(entry):
            report_unsupported(entry["name"], "synthetic runtime gap", "unparsed")
            return original(entry)

        with mock.patch.object(
                card_probe_module, "_discover_obligations",
                side_effect=emit_during_probe):
            result = probe_card(
                self.entries["Forest"], input_identity=self.identity,
                max_decisions=24, max_branches=8)

        self.assertEqual(result["runtime_status"], "failed")
        self.assertIn("Forest", result["card_support_manifest_delta"])
        self.assertNotIn(
            "Stale Process Entry", result["card_support_manifest_delta"])
        self.assertEqual(get_manifest().entries, {})

    def test_input_identity_binds_probe_and_engine_sources(self):
        identity = _input_identity(
            self.snapshot, self.registry_path, self.ledger_path,
            self.registry, self.ledger)

        self.assertEqual(len(identity["probe_source"]["sha256"]), 64)
        self.assertEqual(len(identity["engine_source"]["sha256"]), 64)
        self.assertGreater(identity["engine_source"]["file_count"], 20)
        self.assertEqual(identity["engine_source"]["scope"], "Playersim/**/*.py")
        expected_ledger = hashlib.sha256(
            self.ledger_path.read_bytes()).hexdigest()
        self.assertEqual(identity["ledger"]["sha256"], expected_ledger)


if __name__ == "__main__":
    unittest.main(verbosity=2)
