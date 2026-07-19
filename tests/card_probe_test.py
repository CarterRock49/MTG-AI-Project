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
                "Watery Grave", "Ouroboroid", "Spyglass Siren",
                "Beifong's Bounty Hunters", "Gran-Gran",
                "Caustic Bronco", "Haliya, Guided by Light",
                "Beza, the Bounding Spring",
                "Deceit", "Doomsday Excruciator", "Fear of Missing Out",
                "Emeritus of Ideation // Ancestral Recall",
                "Great Hall of the Biblioplex", "Manifold Mouse",
                "Lumbering Worldwagon", "Optimistic Scavenger",
                "Roaring Furnace // Steaming Sauna",
                "Pit Automaton",
                "Meltstrider's Resolve", "Overlord of the Hauntwoods",
                "North Wind Avatar", "Sunderflock",
                "Restless Cottage",
                "Stormchaser's Talent",
                "Bogwater Lumaret", "Salvation Swan",
                "Thoughtweft Lieutenant", "Al Bhed Salvagers",
                "Susurian Voidborn", "Venerated Stormsinger",
                "Vengeful Bloodwitch", "Totentanz, Swarm Piper",
                "Ares, God of War", "Rakdos Joins Up",
                "Yarus, Roar of the Old Gods", "Meltstrider Eulogist",
                "Host of the Hereafter", "Reluctant Role Model",
                "Giott, King of the Dwarves", "Maralen, Fae Ascendant",
                "Baron Bertram Graywater", "Pawpatch Recruit",
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

    def test_scry_alternatives_are_independently_replayed(self):
        result = probe_card(
            self.entries["Opt"], input_identity=self.identity,
            max_decisions=24, max_branches=8)

        self.assertEqual(result["runtime_status"], "execution_passed")
        choices = [row for row in result["obligations"]
                   if row["kind"] == "choice_branch"]
        self.assertTrue(choices)
        self.assertTrue(all(row["status"] == "exercised" for row in choices))
        self.assertTrue(any(
            row.get("decision_kind") == "scry"
            and row.get("unvisited_branch_count") == 0
            and len(row.get("replayed_actions", [])) >= 1
            for row in choices))
        replays = [path for path in result["paths"]
                   if path.get("kind") == "choice_replay"
                   and path.get("matched_surface") == "primary"]
        self.assertTrue(replays)
        self.assertTrue(all(path["status"] == "exercised" for path in replays))
        self.assertTrue(all("state_after_sha256" in path for path in replays))

    def test_target_alternatives_replay_from_fresh_public_state(self):
        result = probe_card(
            self.entries["Ride's End"], input_identity=self.identity,
            max_decisions=48, max_branches=8)

        choices = [row for row in result["obligations"]
                   if row["kind"] == "choice_branch"
                   and row.get("decision_kind") == "targeting"]
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["status"], "exercised")
        self.assertEqual(choices[0]["unvisited_branch_count"], 0)
        self.assertEqual(len(choices[0]["replayed_actions"]), 3)
        replay_paths = [path for path in result["paths"]
                        if path.get("kind") == "choice_replay"]
        self.assertEqual(len(replay_paths), 3)
        self.assertEqual(
            len({path["state_after_sha256"] for path in replay_paths}), 3)

    def test_choice_replay_is_independent_edges_not_cartesian_expansion(self):
        result = probe_card(
            self.entries["Three Steps Ahead"], input_identity=self.identity,
            max_decisions=48, max_branches=8)

        primary = next(path for path in result["paths"]
                       if path["id"] == "primary")
        base_decisions = [
            step["decision"] for step in primary.get("trace", [])
            if len((step.get("decision") or {}).get(
                "semantic_options", [])) > 1
        ]
        expected_edges = sum(
            len(decision["semantic_options"]) - 1
            for decision in base_decisions)
        replays = [path for path in result["paths"]
                   if path.get("kind") == "choice_replay"]
        self.assertEqual(len(replays), expected_edges)
        self.assertLessEqual(len(replays), 8)
        base_prefixes = {
            tuple(decision["branch_prefix"]) for decision in base_decisions}
        choice_obligations = [
            row for row in result["obligations"]
            if row.get("kind") == "choice_branch"
        ]
        self.assertTrue(choice_obligations)
        self.assertTrue(all(
            tuple(row["branch_prefix"]) in base_prefixes
            for row in choice_obligations))

    def test_choice_replay_branch_cap_remains_fail_closed(self):
        result = probe_card(
            self.entries["Ride's End"], input_identity=self.identity,
            max_decisions=48, max_branches=2)

        choice = next(row for row in result["obligations"]
                      if row.get("kind") == "choice_branch")
        self.assertEqual(choice["status"], "coverage_gap")
        self.assertEqual(choice["unvisited_branch_count"], 1)
        self.assertEqual(len(choice["replayed_actions"]), 2)
        self.assertEqual(result["runtime_status"], "coverage_gap")
        self.assertEqual(result["semantic_status"], "unverified")

    def test_as_enters_optional_obligation_binds_to_both_public_branches(self):
        result = probe_card(
            self.entries["Watery Grave"], input_identity=self.identity,
            max_decisions=24, max_branches=8)

        optional = next(row for row in result["obligations"]
                        if row.get("kind") == "optional_branch")
        choice = next(row for row in result["obligations"]
                      if row.get("decision_kind") == "as_enters_pay_life")
        self.assertEqual(choice["status"], "exercised")
        self.assertEqual(optional["status"], "exercised")
        self.assertEqual(optional["matched_choice_obligation"], choice["id"])
        self.assertEqual(optional["evidence_paths"], choice["evidence_paths"])
        self.assertEqual(result["runtime_status"], "execution_passed")

    def test_beginning_of_combat_trigger_uses_real_event_stack_lifecycle(self):
        result = probe_card(
            self.entries["Ouroboroid"], input_identity=self.identity,
            max_decisions=32, max_branches=8)

        trigger = next(row for row in result["obligations"]
                       if row.get("id") == "triggered:0")
        self.assertEqual(trigger["status"], "exercised")
        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:0")
        self.assertEqual(path["event_fixture"]["event_type"],
                         "BEGINNING_OF_COMBAT")
        self.assertTrue(path["event_fixture"]["queued_target_trigger"])
        self.assertEqual(path["event_fixture"]["desired_delivery_count"], 1)
        self.assertTrue(path["stack_identity_before_resolution"])
        self.assertTrue(path["desired_trigger_left_pipeline"])
        self.assertEqual(path["state_delta"]["final"]["stack_depth"], 0)
        self.assertTrue(path["state_delta"]["card_characteristics"])

    def test_self_etb_trigger_moves_then_stacks_and_resolves(self):
        result = probe_card(
            self.entries["Spyglass Siren"], input_identity=self.identity,
            max_decisions=32, max_branches=8)

        trigger = next(row for row in result["obligations"]
                       if row.get("id") == "triggered:0")
        self.assertEqual(trigger["status"], "exercised")
        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:0")
        self.assertEqual(path["event_fixture"]["kind"], "self_etb")
        self.assertEqual(path["event_fixture"]["desired_delivery_count"], 1)
        self.assertTrue(path["stack_identity_before_resolution"])
        self.assertIn(path["source_id"],
                      path["state_delta"]["zones"]["p1"]["battlefield"]["added"])
        self.assertTrue(any(
            change.get("battlefield", {}).get("added")
            for change in path["state_delta"]["zones"].values()))
        negative = next(path for path in result["paths"]
                        if path.get("id") == "triggered:0:negative_event")
        self.assertEqual(negative["status"], "exercised")
        self.assertEqual(
            negative["event_fixture"]["desired_delivery_count"], 0)
        self.assertEqual(
            negative["negative_case"], "another controlled permanent enters")

    def test_offspring_positive_etb_fixture_records_payment_and_copy(self):
        for card_name in ("Manifold Mouse", "Pawpatch Recruit"):
            with self.subTest(card=card_name):
                result = probe_card(
                    self.entries[card_name], input_identity=self.identity,
                    max_decisions=48, max_branches=16)
                surface = next(
                    row for row in result["discovered"]
                    if row.get("class") == "TriggeredAbility"
                    and row.get("trigger_condition")
                    == "when this permanent enters"
                    and row.get("effect") == "create a 1/1 token copy of it")
                surface_id = surface["id"]
                obligation = next(
                    row for row in result["obligations"]
                    if row.get("id") == surface_id)
                self.assertEqual(obligation["status"], "exercised")

                path = next(
                    row for row in result["paths"]
                    if row.get("id") == surface_id)
                self.assertEqual(path["status"], "exercised")
                self.assertEqual(path["event_fixture"]["kind"], "self_etb")
                self.assertTrue(path["event_fixture"]["paid_offspring"])
                self.assertEqual(
                    path["event_fixture"]["desired_delivery_count"], 1)
                self.assertTrue(path["stack_identity_before_resolution"])
                battlefield_added = path["state_delta"]["zones"]["p1"][
                    "battlefield"]["added"]
                self.assertIn(path["source_id"], battlefield_added)
                token_ids = [
                    card_id for card_id in battlefield_added
                    if card_id != path["source_id"]]
                self.assertEqual(len(token_ids), 1)
                token_state = path["state_delta"][
                    "card_characteristics"][str(token_ids[0])]["after"]
                self.assertEqual(token_state["name"], card_name)
                self.assertEqual(
                    (token_state["power"], token_state["toughness"]), (1, 1))

                negative = next(
                    row for row in result["paths"]
                    if row.get("id") == f"{surface_id}:negative_event")
                self.assertEqual(negative["status"], "exercised")
                self.assertEqual(
                    negative["event_fixture"]["desired_delivery_count"], 0)
                self.assertNotIn(
                    "tokens",
                    negative["state_delta"].get(
                        "player_state", {}).get("p1", {}))

    def test_watched_death_trigger_reaches_targeting_through_public_actions(self):
        result = probe_card(
            self.entries["Beifong's Bounty Hunters"],
            input_identity=self.identity,
            max_decisions=48, max_branches=8)

        trigger = next(row for row in result["obligations"]
                       if row.get("id") == "triggered:0")
        self.assertEqual(trigger["status"], "exercised")
        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:0")
        self.assertEqual(path["event_fixture"]["kind"],
                         "controlled_creature_dies")
        event_id = path["event_fixture"]["event_card_id"]
        p1_zones = path["state_delta"]["zones"]["p1"]
        self.assertIn(event_id, p1_zones["battlefield"]["removed"])
        self.assertIn(event_id, p1_zones["graveyard"]["added"])
        self.assertNotIn("hand", p1_zones)
        self.assertTrue(any(
            (step.get("decision") or {}).get("kind") == "targeting"
            for step in path.get("trace", [])))

    def test_self_or_controlled_etb_negative_uses_matching_opponent_event(self):
        cases = {
            "Bogwater Lumaret": ("opponent creature enters", None),
            "Salvation Swan": (
                "opponent bird enters", "Creature - Bird"),
            "Thoughtweft Lieutenant": (
                "opponent kithkin enters", "Creature - Kithkin"),
        }
        for card_name, (negative_case, type_line) in cases.items():
            with self.subTest(card_name=card_name):
                result = probe_card(
                    self.entries[card_name], input_identity=self.identity,
                    max_decisions=48, max_branches=8)

                path = next(path for path in result["paths"]
                            if path.get("id") ==
                            "triggered:0:negative_event")
                self.assertEqual(path["status"], "exercised")
                self.assertEqual(path["negative_case"], negative_case)
                self.assertEqual(path["event_fixture"]["fixture_owner"],
                                 "opponent")
                self.assertEqual(path["event_fixture"]["kind"], "other_etb")
                self.assertEqual(
                    path["event_fixture"]["desired_delivery_count"], 0)
                if type_line is None:
                    self.assertNotIn(
                        "event_fixture_type_line", path["event_fixture"])
                else:
                    self.assertEqual(
                        path["event_fixture"]["event_fixture_type_line"],
                        type_line)
                event_id = path["event_fixture"]["event_card_id"]
                p2_zones = path["state_delta"]["zones"]["p2"]
                self.assertIn(event_id, p2_zones["hand"]["removed"])
                self.assertIn(event_id, p2_zones["battlefield"]["added"])

    def test_named_or_controlled_compound_negative_uses_opponent_event(self):
        entry = {"name": "Probe Captain, Fixture Marshal"}
        cases = (
            (
                "Whenever Probe Captain or another artifact you control "
                "enters, draw a card.",
                "other_etb", 1, "opponent artifact enters",
            ),
            (
                "Whenever Probe Captain or another nontoken creature you "
                "control dies, draw a card.",
                "other_dies", 0, "opponent nontoken creature dies",
            ),
        )
        for condition, kind, fixture_index, negative_case in cases:
            with self.subTest(condition=condition):
                spec = card_probe_module._trigger_negative_fixture_spec(
                    entry, condition)
                self.assertIsNotNone(spec)
                self.assertEqual(spec["kind"], kind)
                self.assertEqual(spec["fixture_owner"], "opponent")
                self.assertEqual(
                    spec["event_fixture_index"], fixture_index)
                self.assertEqual(spec["negative_case"], negative_case)

    def test_self_or_controlled_death_trigger_negative_uses_opponent_death(self):
        cases = {
            "Venerated Stormsinger": {"opponent_creature_dies"},
            "Vengeful Bloodwitch": {"opponent_creature_dies"},
            "Al Bhed Salvagers": {
                "opponent_creature_dies", "opponent_artifact_dies"},
            "Susurian Voidborn": {
                "opponent_creature_dies", "opponent_artifact_dies"},
        }
        for card_name, expected_arms in cases.items():
            with self.subTest(card_name=card_name):
                result = probe_card(
                    self.entries[card_name], input_identity=self.identity,
                    max_decisions=48, max_branches=8)

                paths = {
                    path["negative_arm"]: path for path in result["paths"]
                    if path.get("id", "").startswith(
                        "triggered:0:negative_event")}
                self.assertEqual(set(paths), expected_arms)
                self.assertEqual(
                    paths["opponent_creature_dies"]["id"],
                    "triggered:0:negative_event")
                for arm, path in paths.items():
                    self.assertEqual(path["status"], "exercised")
                    self.assertEqual(
                        path["negative_case"],
                        arm.replace("_", " "))
                    self.assertEqual(path["event_fixture"]["fixture_owner"],
                                     "opponent")
                    self.assertEqual(
                        path["event_fixture"]["kind"], "other_dies")
                    self.assertEqual(
                        path["event_fixture"]["desired_delivery_count"], 0)
                    event_id = path["event_fixture"]["event_card_id"]
                    p2_zones = path["state_delta"]["zones"]["p2"]
                    self.assertIn(
                        event_id, p2_zones["battlefield"]["removed"])
                    self.assertIn(event_id, p2_zones["graveyard"]["added"])
                if "opponent_artifact_dies" in paths:
                    artifact = paths["opponent_artifact_dies"]
                    self.assertEqual(
                        artifact["id"],
                        "triggered:0:negative_event:opponent_artifact_dies")
                    self.assertEqual(
                        artifact["event_fixture"]["event_fixture_type_line"],
                        "Artifact")

    def test_self_or_another_positive_arms_are_isolated_and_complete(self):
        cases = {
            "Bogwater Lumaret": {
                "self_enters", "another_creature_enters"},
            "Salvation Swan": {
                "self_enters", "another_bird_enters"},
            "Thoughtweft Lieutenant": {
                "self_enters", "another_kithkin_enters"},
            "Venerated Stormsinger": {
                "self_dies", "another_creature_dies"},
            "Totentanz, Swarm Piper": {
                "self_dies", "another_nontoken_creature_dies"},
            "Al Bhed Salvagers": {
                "self_dies", "another_creature_dies",
                "another_artifact_dies"},
        }
        for card_name, expected_arms in cases.items():
            with self.subTest(card_name=card_name):
                result = probe_card(
                    self.entries[card_name], input_identity=self.identity,
                    max_decisions=48, max_branches=8)
                trigger = next(
                    row for row in result["obligations"]
                    if row.get("id") == "triggered:0")
                self.assertEqual(trigger["status"], "exercised")
                self.assertEqual(trigger["event_arm_count"], len(expected_arms))
                self.assertEqual(
                    trigger["exercised_event_arm_count"], len(expected_arms))
                paths = {
                    path["event_arm"]: path for path in result["paths"]
                    if path.get("id", "").startswith("triggered:0:event:")}
                self.assertEqual(set(paths), expected_arms)
                for arm, path in paths.items():
                    self.assertEqual(path["status"], "exercised")
                    self.assertEqual(
                        path["event_fixture"]["desired_delivery_count"], 1)
                    if not arm.startswith("self_"):
                        self.assertTrue(
                            path["criteria_preflight"]["matched"])

                negative = next(
                    path for path in result["paths"]
                    if path.get("id") == "triggered:0:negative_event")
                self.assertEqual(negative["status"], "exercised")
                self.assertTrue(negative["criteria_preflight"]["matched"])
                self.assertTrue(negative["event_fixture"][
                    "controller_is_only_intended_mismatch"])
                self.assertEqual(
                    negative["event_fixture"]["desired_delivery_count"], 0)

                if card_name == "Al Bhed Salvagers":
                    artifact = paths["another_artifact_dies"]["event_fixture"]
                    self.assertEqual(
                        artifact["event_fixture_type_line"], "Artifact")
                    self.assertEqual(
                        artifact["event_fixture_criteria"], "artifact")

    def test_counter_intervening_death_fixtures_stage_every_event_object(self):
        for card_name in (
                "Host of the Hereafter", "Reluctant Role Model"):
            with self.subTest(card_name=card_name):
                entry = self.entries[card_name]
                result = probe_card(
                    entry, input_identity=self.identity,
                    max_decisions=48, max_branches=32)
                death_surface = next(
                    row for row in result["discovered"]
                    if str(row.get("id", "")).startswith("triggered:")
                    and " dies" in str(
                        row.get("trigger_condition", "")).casefold())
                death_surface_id = death_surface["id"]
                death_ability_index = int(
                    death_surface_id.split(":", 1)[1])
                trigger = next(
                    row for row in result["obligations"]
                    if row.get("id") == death_surface_id)
                positives = [
                    path for path in result["paths"]
                    if path.get("id") in trigger["event_path_ids"]]
                self.assertEqual(len(positives), 2)
                for path in positives:
                    self.assertEqual(
                        path["event_fixture"]["event_fixture_counters"],
                        {"+1/+1": 1})
                    self.assertEqual(
                        path["event_fixture"][
                            "event_fixture_intervening_condition"],
                        "if it had counters on it")
                    self.assertEqual(
                        path["event_fixture"]["desired_delivery_count"], 1)

                negative = next(
                    path for path in result["paths"]
                    if path.get("id") ==
                    f"{death_surface_id}:negative_event")
                self.assertEqual(
                    negative["event_fixture"]["event_fixture_counters"],
                    {"+1/+1": 1})
                self.assertTrue(negative["criteria_preflight"]["matched"])
                self.assertTrue(negative["event_fixture"][
                    "controller_is_only_intended_mismatch"])
                self.assertEqual(
                    negative["event_fixture"]["desired_delivery_count"], 0)

                counterfactual = copy.deepcopy(
                    card_probe_module._trigger_negative_fixture_spec(
                        entry, positives[0]["trigger_condition"]))
                counterfactual.pop("fixture_owner", None)
                counterfactual.pop("negative_case", None)
                counterfactual.pop(
                    "controller_is_only_intended_mismatch", None)
                counterfactual.update({
                    "kind": "controlled_creature_dies",
                    "target_zone": "battlefield",
                    "event_arm": "own_counterfactual",
                })
                control, _ = card_probe_module._probe_triggered(
                    entry, death_ability_index,
                    positives[0]["trigger_condition"],
                    max_decisions=48, fixture_spec=counterfactual)
                self.assertEqual(
                    control["event_fixture"]["desired_delivery_count"], 1)

        synthetic_cases = (
            (
                "Whenever this creature dies, if it had counters on it, "
                "draw a card.",
                "whenever this creature dies",
            ),
            (
                "Whenever a creature you control dies, if it had counters "
                "on it, draw a card.",
                "whenever a creature you control dies",
            ),
        )
        for oracle_text, condition in synthetic_cases:
            with self.subTest(condition=condition):
                entry = {
                    "name": "Counter Fixture Watcher",
                    "raw": {
                        "type_line": "Creature - Test",
                        "oracle_text": oracle_text,
                    },
                }
                positives = card_probe_module._trigger_fixture_variants(
                    entry, condition)
                negatives = card_probe_module._trigger_negative_fixture_specs(
                    entry, condition)
                self.assertTrue(positives)
                self.assertTrue(negatives)
                self.assertTrue(all(
                    spec.get("event_fixture_counters") == {"+1/+1": 1}
                    for spec in positives + negatives))

    def test_compound_trigger_negatives_cover_each_supported_detail(self):
        cases = {
            "Al Bhed Salvagers": (
                ("opponent_creature_dies", "creature"),
                ("opponent_artifact_dies", "artifact"),
            ),
            "Susurian Voidborn": (
                ("opponent_creature_dies", "creature"),
                ("opponent_artifact_dies", "artifact"),
            ),
            "Haliya, Guided by Light": (
                ("opponent_creature_enters", "creature"),
                ("opponent_artifact_enters", "artifact"),
            ),
            "Maralen, Fae Ascendant": (
                ("opponent_elf_enters", "elf"),
                ("opponent_faerie_enters", "faerie"),
            ),
            "Giott, King of the Dwarves": (
                ("opponent_dwarf_enters", "dwarf"),
                ("opponent_equipment_enters", "equipment"),
            ),
        }
        for card_name, expected in cases.items():
            with self.subTest(card_name=card_name):
                result = probe_card(
                    self.entries[card_name], input_identity=self.identity,
                    max_decisions=48, max_branches=32)
                paths = [
                    path for path in result["paths"]
                    if path.get("id", "").startswith(
                        "triggered:0:negative_event")]
                self.assertEqual(
                    [(path["negative_arm"], path["criteria_preflight"][
                        "criteria"]) for path in paths],
                    list(expected))
                self.assertEqual(
                    paths[0]["id"], "triggered:0:negative_event")
                self.assertEqual(
                    paths[1]["id"],
                    f"triggered:0:negative_event:{expected[1][0]}")
                for path in paths:
                    self.assertEqual(path["status"], "exercised")
                    self.assertTrue(path["criteria_preflight"]["matched"])
                    self.assertTrue(path["event_fixture"][
                        "controller_is_only_intended_mismatch"])
                    self.assertEqual(
                        path["event_fixture"]["desired_delivery_count"], 0)
                obligation = next(
                    row for row in result["obligations"]
                    if row.get("id") == "triggered:0:negative_event")
                self.assertEqual(obligation["negative_arm_count"], 2)
                self.assertEqual(
                    obligation["exercised_negative_arm_count"], 2)
                self.assertEqual(
                    obligation["negative_path_ids"],
                    [path["id"] for path in paths])

    def test_qualified_death_negatives_match_every_noncontroller_gate(self):
        cases = (
            (
                "Ares, God of War", 0, "attacking creature",
                "event_fixture_attacking", True,
            ),
            (
                "Rakdos Joins Up", 1, "legendary creature",
                "event_fixture_type_line", "Legendary Creature",
            ),
            (
                "Yarus, Roar of the Old Gods", 1, "face-down creature",
                "event_fixture_face_down", True,
            ),
            (
                "Meltstrider Eulogist", 0,
                "creature with a +1/+1 counter on it",
                "event_fixture_counters", {"+1/+1": 1},
            ),
        )
        for card_name, ability_index, criteria, field, value in cases:
            with self.subTest(card_name=card_name):
                result = probe_card(
                    self.entries[card_name], input_identity=self.identity,
                    max_decisions=48, max_branches=8)
                positive_id = f"triggered:{ability_index}"
                positive = next(
                    path for path in result["paths"]
                    if path.get("id") == positive_id)
                negative = next(
                    path for path in result["paths"]
                    if path.get("id") == f"{positive_id}:negative_event")

                self.assertEqual(positive["status"], "exercised")
                self.assertTrue(positive["criteria_preflight"]["matched"])
                self.assertEqual(
                    positive["criteria_preflight"]["criteria"], criteria)
                self.assertEqual(positive["event_fixture"][field], value)
                self.assertEqual(
                    positive["event_fixture"]["desired_delivery_count"], 1)

                self.assertEqual(negative["status"], "exercised")
                self.assertTrue(negative["criteria_preflight"]["matched"])
                self.assertEqual(
                    negative["criteria_preflight"]["criteria"], criteria)
                self.assertEqual(negative["event_fixture"][field], value)
                self.assertTrue(negative["event_fixture"][
                    "controller_is_only_intended_mismatch"])
                self.assertEqual(
                    negative["event_fixture"]["desired_delivery_count"], 0)

    def test_creature_attack_trigger_uses_public_declaration_actions(self):
        result = probe_card(
            self.entries["Caustic Bronco"], input_identity=self.identity,
            max_decisions=32, max_branches=8)

        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:0")
        self.assertEqual(path["status"], "exercised")
        self.assertEqual(path["event_fixture"]["delivery"], "stack")
        self.assertEqual(path["event_fixture"]["desired_delivery_count"], 1)
        self.assertTrue(path["stack_identity_before_resolution"])
        action_types = [step["action_type"] for step in path["trace"][:2]]
        self.assertEqual(action_types, ["ATTACK", "DECLARE_ATTACKERS_DONE"])

    def test_noncreature_land_attack_is_an_explicit_fixture_gap(self):
        result = probe_card(
            self.entries["Restless Cottage"], input_identity=self.identity,
            max_decisions=32, max_branches=8)

        obligation = next(row for row in result["obligations"]
                          if row.get("id") == "triggered:0")
        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:0")
        self.assertEqual(obligation["status"], "coverage_gap")
        self.assertEqual(path["status"], "coverage_gap")
        self.assertIn("no deterministic real-event fixture", path["reason"])
        self.assertFalse(result["diagnostics"])

    def test_aura_etb_fixture_enters_legally_attached(self):
        result = probe_card(
            self.entries["Meltstrider's Resolve"],
            input_identity=self.identity,
            max_decisions=48, max_branches=8)

        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:0")
        self.assertEqual(path["status"], "exercised")
        self.assertIn("attach_to_fixture", path["event_fixture"])
        attachment_change = path["state_delta"]["player_state"]["p1"][
            "attachments"]
        self.assertIn(str(path["source_id"]),
                      {str(key) for key in attachment_change["after"]})
        self.assertFalse(result["diagnostics"])

    def test_impending_end_step_fixture_has_and_removes_time_counter(self):
        result = probe_card(
            self.entries["Overlord of the Hauntwoods"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)

        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:1")
        self.assertEqual(path["status"], "exercised")
        self.assertTrue(path["event_fixture"]["activate_impending"])
        counter_change = path["state_delta"]["card_characteristics"][
            str(path["source_id"])]
        self.assertGreater(counter_change["before"]["counters"]["time"],
                           counter_change["after"]["counters"]["time"])

    def test_unmet_phase_trigger_condition_is_gap_not_event_failure(self):
        path, issues = card_probe_module._probe_triggered(
            self.entries["Haliya, Guided by Light"], 1,
            "at the beginning of your end step",
            max_decisions=32,
            fixture_spec={
                "kind": "phase_begin",
                "event_type": "BEGINNING_OF_END_STEP",
                "phase": "PHASE_END_STEP",
                "target_zone": "battlefield",
                "event_arm": "unmet_condition",
            })
        self.assertEqual(path["status"], "coverage_gap")
        self.assertNotIn("failure", path)
        self.assertEqual(path["event_fixture"]["delivery"], "none")
        self.assertFalse(issues)

    def test_haliya_phase_fixture_stages_exact_life_threshold(self):
        result = probe_card(
            self.entries["Haliya, Guided by Light"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:1")
        negative = next(path for path in result["paths"]
                        if path.get("id") == "triggered:1:negative_event")
        self.assertEqual(path["status"], "exercised")
        self.assertEqual(path["event_fixture"]["setup_life_gain"], 3)
        self.assertEqual(
            path["event_fixture"]["desired_delivery_count"], 1)
        self.assertEqual(negative["status"], "exercised")
        self.assertEqual(negative["event_fixture"]["setup_life_gain"], 3)
        self.assertEqual(
            negative["event_fixture"]["desired_delivery_count"], 0)

    def test_compound_trigger_requires_every_event_arm(self):
        scavenger = probe_card(
            self.entries["Optimistic Scavenger"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        scavenger_trigger = next(
            row for row in scavenger["obligations"]
            if row.get("id") == "triggered:0")
        self.assertEqual(scavenger_trigger["event_arm_count"], 2)
        self.assertEqual(scavenger_trigger["exercised_event_arm_count"], 2)
        self.assertEqual(scavenger_trigger["status"], "exercised")
        scavenger_paths = {
            path["event_arm"]: path for path in scavenger["paths"]
            if path.get("id", "").startswith("triggered:0:event:")}
        self.assertEqual(
            scavenger_paths["controlled_enchantment_enters"]["status"],
            "exercised")
        self.assertEqual(
            scavenger_paths["fully_unlock_room"]["status"],
            "exercised")
        room_path = scavenger_paths["fully_unlock_room"]
        self.assertEqual(room_path["event_fixture"]["kind"],
                         "room_full_unlock")
        self.assertTrue(room_path["event_fixture"]["fully_unlocked_after"])
        self.assertEqual(
            room_path["event_fixture"]["desired_delivery_count"], 1)
        self.assertIn(
            "UNLOCK_DOOR",
            [step["action_type"] for step in room_path["trace"]])

        scavenger_negatives = [
            path for path in scavenger["paths"]
            if path.get("id", "").startswith(
                "triggered:0:negative_event")]
        self.assertEqual(len(scavenger_negatives), 3)
        self.assertTrue(all(
            path["status"] == "exercised"
            for path in scavenger_negatives))
        partial_room = next(
            path for path in scavenger_negatives
            if path.get("negative_arm") ==
            "room_remains_partially_locked")
        self.assertFalse(
            partial_room["event_fixture"]["fully_unlocked_after"])
        self.assertEqual(
            partial_room["event_fixture"]["desired_delivery_count"], 0)
        self.assertIn(
            "UNLOCK_DOOR",
            [step["action_type"] for step in partial_room["trace"]])

        worldwagon = probe_card(
            self.entries["Lumbering Worldwagon"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        worldwagon_trigger = next(
            row for row in worldwagon["obligations"]
            if row.get("id") == "triggered:0")
        self.assertEqual(worldwagon_trigger["event_arm_count"], 2)
        self.assertEqual(worldwagon_trigger["status"], "coverage_gap")
        self.assertIn("animation or crew", worldwagon_trigger["reason"])

    def test_exhaust_trigger_uses_public_positive_and_controller_negative(self):
        result = probe_card(
            self.entries["Afterburner Expert"],
            input_identity=self.identity,
            max_decisions=48, max_branches=12)
        surface = next(
            row for row in result["discovered"]
            if row.get("class") == "TriggeredAbility"
            and "activate an exhaust ability" in str(
                row.get("trigger_condition", "")).lower())
        self.assertEqual(surface["trigger_zone"], "graveyard")

        positive = next(
            path for path in result["paths"]
            if path.get("id") == surface["id"])
        self.assertEqual(positive["status"], "exercised")
        self.assertEqual(
            positive["event_fixture"]["kind"], "exhaust_activation")
        self.assertEqual(positive["event_fixture"]["fixture_owner"], "own")
        self.assertEqual(
            positive["event_fixture"]["desired_delivery_count"], 1)
        self.assertIn(
            "ACTIVATE_ABILITY",
            [step["action_type"] for step in positive["trace"]])
        self.assertIn(
            positive["source_id"],
            positive["state_delta"]["zones"]["p1"]["graveyard"]["removed"])
        self.assertIn(
            positive["source_id"],
            positive["state_delta"]["zones"]["p1"]["battlefield"]["added"])

        negative = next(
            path for path in result["paths"]
            if path.get("id") == f"{surface['id']}:negative_event")
        self.assertEqual(negative["status"], "exercised")
        self.assertEqual(
            negative["event_fixture"]["fixture_owner"], "opponent")
        self.assertEqual(
            negative["event_fixture"]["desired_delivery_count"], 0)
        self.assertIn(
            "ACTIVATE_ABILITY",
            [step["action_type"] for step in negative["trace"]])

    def test_exhaust_trigger_rejects_own_non_exhaust_public_activation(self):
        result = probe_card(
            self.entries["Afterburner Expert"],
            input_identity=self.identity,
            max_decisions=48, max_branches=12)
        negative = next(
            path for path in result["paths"]
            if path.get("negative_arm") == "own_non_exhaust_activation")

        self.assertEqual(negative["status"], "exercised")
        self.assertEqual(
            negative["event_fixture"]["kind"],
            "non_exhaust_activation")
        self.assertEqual(
            negative["event_fixture"]["fixture_owner"], "own")
        self.assertFalse(
            negative["event_fixture"]["event_ability_is_exhaust"])
        self.assertEqual(
            negative["event_fixture"]["desired_delivery_count"], 0)
        self.assertIn(
            "ACTIVATE_ABILITY",
            [step["action_type"] for step in negative["trace"]])

    def test_room_trigger_paths_preserve_face_and_exact_door_provenance(self):
        result = probe_card(
            self.entries["Roaring Furnace // Steaming Sauna"],
            input_identity=self.identity,
            max_decisions=48, max_branches=12)
        trigger_surfaces = [
            row for row in result["discovered"]
            if row.get("class") == "TriggeredAbility"]
        door_surface = next(
            row for row in trigger_surfaces
            if "unlock this door" in str(
                row.get("trigger_condition", "")).lower())
        end_step_surface = next(
            row for row in trigger_surfaces
            if "beginning of your end step" in str(
                row.get("trigger_condition", "")).lower())
        self.assertEqual(
            (door_surface["room_face_index"],
             door_surface["room_door_number"]), (0, 1))
        self.assertEqual(
            (end_step_surface["room_face_index"],
             end_step_surface["room_door_number"]), (1, 2))

        positive = next(
            path for path in result["paths"]
            if path.get("id") == door_surface["id"])
        self.assertEqual(positive["status"], "exercised")
        self.assertEqual(
            positive["event_fixture"]["kind"], "room_door_unlock")
        self.assertEqual(positive["event_fixture"]["door_number"], 1)
        self.assertEqual(
            positive["event_fixture"]["expected_trigger_door_number"], 1)
        self.assertEqual(
            positive["event_fixture"]["desired_delivery_count"], 1)
        self.assertIn(
            "UNLOCK_DOOR",
            [step["action_type"] for step in positive["trace"]])

        negative = next(
            path for path in result["paths"]
            if path.get("id") == f"{door_surface['id']}:negative_event")
        self.assertEqual(negative["status"], "exercised")
        self.assertEqual(negative["event_fixture"]["door_number"], 2)
        self.assertEqual(
            negative["event_fixture"]["expected_trigger_door_number"], 1)
        self.assertEqual(
            negative["event_fixture"]["desired_delivery_count"], 0)
        self.assertIn(
            "UNLOCK_DOOR",
            [step["action_type"] for step in negative["trace"]])

        end_step = next(
            path for path in result["paths"]
            if path.get("id") == end_step_surface["id"])
        self.assertEqual(end_step["status"], "exercised")
        self.assertEqual(
            end_step["event_fixture"]["source_room_door_number"], 2)

    def test_room_door_trigger_rejects_matching_door_on_another_room(self):
        result = probe_card(
            self.entries["Roaring Furnace // Steaming Sauna"],
            input_identity=self.identity,
            max_decisions=48, max_branches=12)
        negative = next(
            path for path in result["paths"]
            if path.get("negative_arm") ==
            "matching_door_on_another_room")

        self.assertEqual(negative["status"], "exercised")
        fixture = negative["event_fixture"]
        self.assertNotEqual(fixture["source_room_id"], fixture["room_id"])
        self.assertEqual(
            fixture["door_number"],
            fixture["expected_trigger_door_number"])
        self.assertEqual(fixture["desired_delivery_count"], 0)
        self.assertIn(
            "UNLOCK_DOOR",
            [step["action_type"] for step in negative["trace"]])

    def test_room_nonunlock_trigger_rejects_matching_event_while_locked(self):
        result = probe_card(
            self.entries["Roaring Furnace // Steaming Sauna"],
            input_identity=self.identity,
            max_decisions=48, max_branches=12)
        negative = next(
            path for path in result["paths"]
            if path.get("negative_arm") ==
            "source_room_face_locked_phase_begin")

        self.assertEqual(negative["status"], "exercised")
        fixture = negative["event_fixture"]
        self.assertEqual(fixture["event_type"], "BEGINNING_OF_END_STEP")
        self.assertTrue(fixture["source_room_face_locked"])
        self.assertEqual(fixture["source_room_door_number"], 2)
        self.assertEqual(fixture["desired_delivery_count"], 0)

    def test_full_room_trigger_rejects_opponent_full_unlock(self):
        result = probe_card(
            self.entries["Optimistic Scavenger"],
            input_identity=self.identity,
            max_decisions=48, max_branches=12)
        negative = next(
            path for path in result["paths"]
            if path.get("negative_arm") == "opponent_fully_unlocks_room")

        self.assertEqual(negative["status"], "exercised")
        fixture = negative["event_fixture"]
        self.assertEqual(fixture["fixture_owner"], "opponent")
        self.assertTrue(fixture["fully_unlocked_after"])
        self.assertEqual(fixture["desired_delivery_count"], 0)
        self.assertIn(
            "UNLOCK_DOOR",
            [step["action_type"] for step in negative["trace"]])

    def test_room_trigger_provenance_reconciliation_fails_closed(self):
        result = probe_card(
            self.entries["Roaring Furnace // Steaming Sauna"],
            input_identity=self.identity,
            max_decisions=48, max_branches=12)
        provenance = [
            row for row in result["obligations"]
            if row.get("kind") == "room_face_provenance"]

        self.assertEqual(len(provenance), 2)
        self.assertTrue(all(
            row["status"] == "exercised" for row in provenance))
        self.assertEqual(
            result["discovery_fidelity"]["room_face_provenance"],
            [{
                "matched_surface": "triggered:0",
                "status": "exercised",
                "room_face_index": 0,
                "room_door_number": 1,
                "expected_room_door_number": 1,
            }, {
                "matched_surface": "triggered:1",
                "status": "exercised",
                "room_face_index": 1,
                "room_door_number": 2,
                "expected_room_door_number": 2,
            }])

        mismatch = card_probe_module._room_trigger_provenance_obligation({
            "id": "triggered:synthetic",
            "room_face_index": 0,
            "room_door_number": 2,
        })
        self.assertEqual(mismatch["status"], "failed")
        self.assertIn("door_number must equal face_index + 1",
                      mismatch["reason"])
        self.assertEqual(
            card_probe_module._result_status([mismatch], []), "failed")

    def test_compound_trigger_exercises_all_supported_arms_fresh(self):
        overlord = probe_card(
            self.entries["Overlord of the Hauntwoods"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        trigger = next(row for row in overlord["obligations"]
                       if row.get("id") == "triggered:0")
        self.assertEqual(trigger["event_arm_count"], 2)
        self.assertEqual(trigger["exercised_event_arm_count"], 2)
        self.assertEqual(trigger["status"], "exercised")
        paths = {
            path["event_arm"]: path for path in overlord["paths"]
            if path.get("id", "").startswith("triggered:0:event:")}
        self.assertEqual(set(paths), {"self_enters", "self_attacks"})
        self.assertEqual(
            [step["action_type"] for step in paths["self_attacks"]["trace"][:2]],
            ["ATTACK", "DECLARE_ATTACKERS_DONE"])

        haliya = probe_card(
            self.entries["Haliya, Guided by Light"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        haliya_trigger = next(row for row in haliya["obligations"]
                              if row.get("id") == "triggered:0")
        self.assertEqual(haliya_trigger["event_arm_count"], 3)
        self.assertEqual(haliya_trigger["exercised_event_arm_count"], 3)
        self.assertEqual(haliya_trigger["status"], "exercised")
        haliya_negative = next(
            path for path in haliya["paths"]
            if path.get("id") == "triggered:0:negative_event")
        self.assertEqual(haliya_negative["status"], "exercised")

    def test_grouped_zone_change_triggers_are_explicit_batch_gaps(self):
        grouped_rows = []
        for raw in self.raw_cards:
            entry = {"name": raw.get("name"), "raw": raw}
            inventory = card_probe_module.discover_printed_trigger_inventory(
                raw)
            for row in inventory["triggers"]:
                event_type = next((
                    candidate for candidate in ("ENTERS_BATTLEFIELD", "DIES")
                    if card_probe_module._is_grouped_zone_change_trigger(
                        row["trigger_condition_prefix"], candidate)
                ), None)
                if event_type is None:
                    continue
                variants = card_probe_module._trigger_fixture_variants(
                    entry, row["trigger_condition_prefix"])
                self.assertEqual(len(variants), 1)
                self.assertEqual(
                    variants[0]["event_arm"],
                    "grouped_etb_batch" if event_type == "ENTERS_BATTLEFIELD"
                    else "grouped_dies_batch")
                self.assertIn("complete atomic", variants[0][
                    "unsupported_reason"])
                grouped_rows.append((raw["name"], row["id"]))

        self.assertEqual(len(grouped_rows), 22)
        self.assertEqual(len({name for name, _ in grouped_rows}), 20)

        result = probe_card(
            self.entries["Baron Bertram Graywater"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        surface = next(
            row for row in result["discovered"]
            if str(row.get("id", "")).startswith("triggered:")
            and card_probe_module._is_grouped_zone_change_trigger(
                row.get("trigger_condition"), "ENTERS_BATTLEFIELD"))
        obligation = next(
            row for row in result["obligations"]
            if row.get("id") == surface["id"])
        path = next(
            row for row in result["paths"]
            if row.get("id") == surface["id"])
        self.assertEqual(obligation["status"], "coverage_gap")
        self.assertEqual(path["status"], "coverage_gap")
        self.assertEqual(path["event_arm"], "grouped_etb_batch")
        self.assertIn("complete atomic", path["reason"])
        self.assertNotIn("did not queue", path["reason"])
        self.assertNotIn("failure", path)
        self.assertNotIn("event_fixture", path)

        negative_id = f"{surface['id']}:negative_event"
        negative_obligation = next(
            row for row in result["obligations"]
            if row.get("id") == negative_id)
        negative_path = next(
            row for row in result["paths"]
            if row.get("id") == negative_id)
        self.assertEqual(negative_obligation["status"], "coverage_gap")
        self.assertEqual(negative_path["status"], "coverage_gap")
        self.assertIn("no deterministic close non-event fixture",
                      negative_path["reason"])
        self.assertNotIn("event_fixture", negative_path)

    def test_your_turn_phase_trigger_has_opponent_turn_negative(self):
        result = probe_card(
            self.entries["Ouroboroid"], input_identity=self.identity,
            max_decisions=32, max_branches=8)
        obligation = next(
            row for row in result["obligations"]
            if row.get("id") == "triggered:0:negative_event")
        path = next(path for path in result["paths"]
                    if path.get("id") == obligation["id"])
        self.assertEqual(obligation["status"], "exercised")
        self.assertEqual(path["negative_case"],
                         "opponent beginning of combat")
        self.assertEqual(path["event_fixture"]["desired_delivery_count"], 0)

    def test_face_oracle_optional_and_conditionals_remain_explicit_gaps(self):
        beza = probe_card(
            self.entries["Beza, the Bounding Spring"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        conditionals = [
            row for row in beza["obligations"]
            if row.get("kind") == "conditional_branch"]
        self.assertGreaterEqual(len(conditionals), 4)
        self.assertTrue(all(
            row["status"] == "coverage_gap" for row in conditionals))
        self.assertEqual(beza["runtime_status"], "coverage_gap")

        emeritus = probe_card(
            self.entries["Emeritus of Ideation // Ancestral Recall"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        optionals = [
            row for row in emeritus["obligations"]
            if row.get("kind") == "optional_branch"]
        self.assertTrue(optionals)
        self.assertTrue(any(
            row.get("oracle_surface") == "face:0" for row in optionals))
        self.assertEqual(emeritus["runtime_status"], "coverage_gap")

    def test_printed_trigger_inventory_blocks_omissions_and_accepts_repair(self):
        # Isolate printed-trigger reconciliation from the regenerated static
        # ledger, which now independently flags Manifold Mouse's token-copy
        # text as partial.
        manifold_entry = copy.deepcopy(self.entries["Manifold Mouse"])
        manifold_entry["ledger_status"] = "observed_clean"
        manifold_entry["ledger_issues"] = []
        manifold = probe_card(
            manifold_entry, input_identity=self.identity,
            max_decisions=32, max_branches=8)
        manifold_summary = manifold["discovery_fidelity"][
            "printed_trigger_inventory"]
        self.assertEqual(manifold_summary["printed_trigger_count"], 2)
        self.assertEqual(manifold_summary["matched_printed_trigger_count"], 2)
        self.assertEqual(
            manifold_summary["unmatched_printed_trigger_count"], 0)
        self.assertFalse(any(
            row.get("kind") == "printed_trigger"
            for row in manifold["obligations"]))
        self.assertEqual(manifold["runtime_status"], "execution_passed")

        great_hall = probe_card(
            self.entries["Great Hall of the Biblioplex"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        great_hall_summary = great_hall["discovery_fidelity"][
            "printed_trigger_inventory"]
        self.assertGreaterEqual(
            great_hall_summary["unmatched_printed_trigger_count"], 1)
        unmatched = [
            row for row in great_hall["obligations"]
            if row.get("kind") == "printed_trigger"]
        self.assertGreaterEqual(len(unmatched), 1)
        self.assertTrue(all(
            row["status"] == "coverage_gap" for row in unmatched))
        self.assertEqual(great_hall["runtime_status"], "coverage_gap")

        stormchaser = probe_card(
            self.entries["Stormchaser's Talent"],
            input_identity=self.identity,
            max_decisions=32, max_branches=8)
        summary = stormchaser["discovery_fidelity"][
            "printed_trigger_inventory"]
        self.assertEqual(summary["printed_trigger_count"], 3)
        self.assertEqual(summary["matched_printed_trigger_count"], 1)
        self.assertEqual(summary["unmatched_printed_trigger_count"], 2)

        base_runtime = next(
            row for row in stormchaser["discovered"]
            if row.get("id") == "triggered:0")
        self.assertEqual(
            base_runtime["trigger_condition"], "when this class enters")
        self.assertEqual(len(base_runtime["printed_trigger_ids"]), 1)

        obligations = {row["id"]: row for row in stormchaser["obligations"]}
        self.assertEqual(obligations["triggered:0"]["status"], "exercised")
        self.assertEqual(
            obligations["triggered:0:negative_event"]["status"],
            "exercised")
        positive = next(
            row for row in stormchaser["paths"]
            if row.get("id") == "triggered:0")
        self.assertEqual(positive["status"], "exercised")
        self.assertEqual(positive["event_fixture"]["kind"], "self_etb")
        self.assertEqual(
            positive["event_fixture"]["event_card_id"],
            positive["source_id"])
        self.assertEqual(
            positive["event_fixture"]["desired_delivery_count"], 1)
        negative = next(
            row for row in stormchaser["paths"]
            if row.get("id") == "triggered:0:negative_event")
        self.assertEqual(negative["status"], "exercised")
        self.assertEqual(
            negative["negative_case"],
            "another controlled permanent enters")
        self.assertNotEqual(
            negative["event_fixture"]["event_card_id"],
            positive["source_id"])
        self.assertEqual(
            negative["event_fixture"]["desired_delivery_count"], 0)

        unmatched = [
            row for row in stormchaser["obligations"]
            if row.get("kind") == "printed_trigger"]
        self.assertEqual(len(unmatched), 2)
        self.assertEqual(
            {row["trigger_condition_prefix"] for row in unmatched},
            {
                "When this Class becomes level 2",
                "Whenever you cast an instant or sorcery spell",
            })
        self.assertTrue(all(
            row["status"] == "coverage_gap" for row in unmatched))
        self.assertEqual(stormchaser["diagnostics"], [])
        self.assertFalse(any(
            issue.get("surface") == "diagnostics"
            for issue in stormchaser["issues"]))
        self.assertNotEqual(
            stormchaser["runtime_status"], "execution_passed")

    def test_printed_trigger_reconciles_to_runtime_surface(self):
        result = probe_card(
            self.entries["Spyglass Siren"], input_identity=self.identity,
            max_decisions=32, max_branches=8)
        summary = result["discovery_fidelity"]["printed_trigger_inventory"]
        self.assertEqual(summary["printed_trigger_count"], 1)
        self.assertEqual(summary["matched_printed_trigger_count"], 1)
        self.assertEqual(summary["unmatched_printed_trigger_count"], 0)
        runtime = next(row for row in result["discovered"]
                       if row.get("id") == "triggered:0")
        self.assertEqual(len(runtime["printed_trigger_ids"]), 1)

    def test_cast_provenance_etb_fixtures_reach_registered_triggers(self):
        cases = {
            "Doomsday Excruciator": ["triggered:0"],
            "North Wind Avatar": ["triggered:0"],
            "Sunderflock": ["triggered:0"],
            "Deceit": ["triggered:0", "triggered:1", "triggered:2"],
        }
        for name, trigger_ids in cases.items():
            with self.subTest(card=name):
                result = probe_card(
                    self.entries[name], input_identity=self.identity,
                    max_decisions=48, max_branches=24)
                obligations = {
                    row["id"]: row for row in result["obligations"]}
                for trigger_id in trigger_ids:
                    self.assertEqual(
                        obligations[trigger_id]["status"], "exercised",
                        (name, trigger_id, obligations[trigger_id]))
                    path = next(
                        path for path in result["paths"]
                        if path.get("id") == trigger_id)
                    self.assertEqual(
                        path["event_fixture"]["desired_delivery_count"], 1)
                    self.assertTrue(path["stack_identity_before_resolution"])

    def test_first_attack_delirium_fixture_is_legally_reachable(self):
        result = probe_card(
            self.entries["Fear of Missing Out"],
            input_identity=self.identity,
            max_decisions=48, max_branches=16)
        obligation = next(
            row for row in result["obligations"]
            if row.get("id") == "triggered:1")
        path = next(path for path in result["paths"]
                    if path.get("id") == "triggered:1")
        self.assertEqual(obligation["status"], "exercised")
        self.assertTrue(path["event_fixture"]["setup_delirium"])
        self.assertEqual(
            [step["action_type"] for step in path["trace"][:2]],
            ["ATTACK", "DECLARE_ATTACKERS_DONE"])
        self.assertTrue(any(
            (step.get("decision") or {}).get("kind") == "targeting"
            for step in path["trace"]))

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
        self.assertEqual(
            paths["activated:0"]["timing_fixture"]["kind"],
            "priority_speed")
        self.assertEqual(
            paths["activated:0"]["timing_fixture"]["phase"], "PRIORITY")
        self.assertFalse(paths["activated:0"]["timing_fixture"][
            "sorcery_speed_available"])
        self.assertTrue(paths["activated:0"]["timing_fixture"][
            "pass_priority_exposed"])
        self.assertIsNotNone(paths[
            "activated:0:negative_mask"]["positive_control_dispatch"])
        self.assertEqual(
            paths["activated:0:negative_mask"]["timing_fixture"]["kind"],
            "priority_speed")
        self.assertIn("state_delta", paths["activated:0"])

    def test_nested_delayed_trigger_is_attributed_without_hiding_legality_gap(self):
        result = probe_card(
            self.entries["Pit Automaton"], input_identity=self.identity,
            max_decisions=48, max_branches=8)
        summary = result["discovery_fidelity"][
            "printed_trigger_inventory"]
        self.assertEqual(summary["attributed_nested_lexeme_count"], 1)
        self.assertEqual(summary["unmatched_lexeme_count"], 0)
        self.assertFalse(any(
            row.get("kind") == "printed_trigger_lexeme"
            for row in result["obligations"]))

        activated_surface = next(
            row for row in result["discovered"]
            if row.get("id") == "activated:1")
        nested = next(
            row for row in result["discovered"]
            if row.get("class") == "nested_delayed_trigger_lexeme")
        self.assertEqual(nested["matched_surface"], "activated:1")
        self.assertEqual(
            activated_surface["nested_trigger_lexeme_ids"],
            [nested["printed_trigger_lexeme_id"]])

        obligations = {row["id"]: row for row in result["obligations"]}
        paths = {row["id"]: row for row in result["paths"]}
        self.assertEqual(obligations["activated:1"]["status"], "coverage_gap")
        self.assertEqual(paths["activated:1"]["status"], "coverage_gap")
        self.assertEqual(
            paths["activated:1"]["timing_fixture"]["kind"],
            "priority_speed")
        self.assertEqual(
            paths["activated:1"]["timing_fixture"]["phase"], "PRIORITY")
        self.assertFalse(paths["activated:1"]["timing_fixture"][
            "sorcery_speed_available"])
        self.assertTrue(paths["activated:1"]["timing_fixture"][
            "pass_priority_exposed"])
        self.assertFalse(paths["activated:1"][
            "ability_legal_after_mana_setup"])
        self.assertIn(
            "not exposed by the public mask",
            paths["activated:1"]["reason"])
        self.assertEqual(result["runtime_status"], "failed")

    def test_unattributed_nested_trigger_lexeme_remains_fail_visible(self):
        entry = {
            "name": "Synthetic Nested Lexeme",
            "raw": {
                "name": "Synthetic Nested Lexeme",
                "oracle_text": (
                    "{2}: Draw a card When a creature enters the battlefield."),
            },
        }
        obligations = []
        discovered = [{
            "id": "activated:0",
            "class": "ActivatedAbility",
            "effect": "Draw a card",
            "effect_text": "{2}: Draw a card",
        }]

        summary = card_probe_module._reconcile_printed_triggers(
            entry, obligations, discovered)

        self.assertEqual(summary["attributed_nested_lexeme_count"], 0)
        self.assertEqual(summary["unmatched_lexeme_count"], 1)
        gap = next(
            row for row in obligations
            if row.get("kind") == "printed_trigger_lexeme")
        self.assertEqual(gap["status"], "coverage_gap")
        self.assertTrue(any(
            row.get("class") == "unmatched_trigger_lexeme"
            for row in discovered))

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
