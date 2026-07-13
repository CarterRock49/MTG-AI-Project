"""Focused tests for parallel harvest sharding and checkpoint promotion."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import harvest_protocol as protocol  # noqa: E402


class _Policy:
    def __init__(self, action):
        self.action = action

    def predict(self, observation, action_masks=None, deterministic=False):
        assert observation == {"state": 1}
        assert deterministic is True
        assert action_masks is not None
        return np.array([self.action]), None


class HarvestProtocolTest(unittest.TestCase):
    def test_partition_is_complete_deterministic_and_caps_workers(self):
        self.assertEqual(protocol.partition_games(10, 3), [
            {"shard": 0, "offset": 0, "games": 4},
            {"shard": 1, "offset": 4, "games": 3},
            {"shard": 2, "offset": 7, "games": 3},
        ])
        self.assertEqual(len(protocol.partition_games(2, 20)), 2)
        with self.assertRaises(ValueError):
            protocol.partition_games(0, 1)
        with self.assertRaises(ValueError):
            protocol.partition_games(1, 0)

    def test_checkpoint_action_is_deterministic_and_mask_enforced(self):
        mask = [False, True, False]
        self.assertEqual(
            protocol.fixture.choose_checkpoint_action(
                _Policy(1), {"state": 1}, mask),
            1,
        )
        with self.assertRaisesRegex(RuntimeError, "mask-invalid"):
            protocol.fixture.choose_checkpoint_action(
                _Policy(2), {"state": 1}, mask)

    def test_parallel_manifest_is_success_only_and_aggregated(self):
        def fake_shard(arguments):
            count = arguments["games"]
            return {
                "shard": arguments["shard"],
                "offset": arguments["offset"],
                "games": count,
                "output": Path(arguments["output"]).name,
                "agent_version": "test-agent",
                "results": {"win": count},
                "records": [{"result": "win"} for _ in range(count)],
                "fidelity": {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS},
                "agent_policy": {"kind": "random-valid"},
                "opponent_policy": {"kind": "scripted"},
                "manifest": {
                    "Test Card": {
                        "count": count, "severity": "partial",
                        "reasons": {"unsupported rider": count},
                    }
                },
            }

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(protocol, "_run_shard", side_effect=fake_shard):
            output = Path(temp) / "run"
            result = protocol.run_parallel_harvest(3, 1, 99, output)
            manifest_path = output / "harvest_protocol.json"
            self.assertTrue(manifest_path.is_file())
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["games"], 3)
            self.assertEqual(result["manifest"]["Test Card"]["count"], 3)

    def test_parallel_harvest_rejects_worker_checkpoint_identity_mismatch(self):
        expected = {
            "name": "candidate.zip", "sha256": "a" * 64, "size": 7}
        observed = {
            "name": "candidate.zip", "sha256": "b" * 64, "size": 8}

        def fake_shard(arguments):
            return {
                "shard": 0, "offset": 0, "games": 1,
                "output": Path(arguments["output"]).name,
                "agent_version": "worker-policy", "results": {"win": 1},
                "records": [{"result": "win"}],
                "fidelity": {
                    key: 0 for key in protocol.fixture.FIDELITY_COUNTERS},
                "manifest": {}, "agent_policy": observed,
                "opponent_policy": {"kind": "scripted"},
                "lineage": {"format": "standard"}, "decks": ["A", "B"],
            }

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    protocol, "_run_shard", side_effect=fake_shard), \
                mock.patch.object(
                    protocol.fixture, "checkpoint_identity",
                    return_value=expected):
            output = Path(temp) / "run"
            with self.assertRaisesRegex(RuntimeError, "agent checkpoint identity"):
                protocol.run_parallel_harvest(
                    1, 1, 9, output, agent_model="candidate.zip")
            self.assertFalse((output / "harvest_protocol.json").exists())

    def test_parallel_harvest_rejects_checkpoint_changed_after_workers(self):
        initial = {
            "name": "candidate.zip", "sha256": "a" * 64, "size": 7}
        changed = {
            "name": "candidate.zip", "sha256": "b" * 64, "size": 8}

        def fake_shard(arguments):
            return {
                "shard": 0, "offset": 0, "games": 1,
                "output": Path(arguments["output"]).name,
                "agent_version": "worker-policy", "results": {"win": 1},
                "records": [{"result": "win"}],
                "fidelity": {
                    key: 0 for key in protocol.fixture.FIDELITY_COUNTERS},
                "manifest": {}, "agent_policy": initial,
                "opponent_policy": {"kind": "scripted"},
                "lineage": {"format": "standard"}, "decks": ["A", "B"],
            }

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    protocol, "_run_shard", side_effect=fake_shard), \
                mock.patch.object(
                    protocol.fixture, "checkpoint_identity",
                    side_effect=[initial, changed]):
            output = Path(temp) / "run"
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                protocol.run_parallel_harvest(
                    1, 1, 9, output, agent_model="candidate.zip")
            self.assertFalse((output / "harvest_protocol.json").exists())

    def test_shard_plumbs_p2_agent_seat_into_fixture(self):
        clean = {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS}
        fixture_result = {
            "agent_version": "candidate",
            "records": [{"result": "win", "agent_is_p1": False}],
            "fidelity": clean,
            "manifest": {},
            "run_manifest": {
                "agent_policy": {
                    "name": "candidate.zip", "sha256": "a" * 64,
                    "size": 1,
                },
                "opponent_policy": {"kind": "scripted"},
                "lineage": {"format": "standard"},
                "decks": ["A", "B"],
            },
        }
        arguments = {
            "shard": 0,
            "offset": 0,
            "games": 1,
            "seed": 3,
            "max_steps": 10,
            "output": "shard_000",
            "agent_model": "candidate.zip",
            "opponent_model": None,
            "agent_is_p1": False,
        }
        with mock.patch.object(
                protocol.fixture, "run_harvest",
                return_value=fixture_result) as run:
            shard = protocol._run_shard(arguments)
        self.assertIs(run.call_args.kwargs["agent_is_p1"], False)
        self.assertEqual(shard["records"][0]["result"], "win")
        self.assertIs(shard["records"][0]["agent_is_p1"], False)
        self.assertEqual(
            shard["agent_policy"], fixture_result["run_manifest"]["agent_policy"])

    def test_p2_environment_results_are_agent_relative(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        env.game_state = type("State", (), {
            "agent_is_p1": False,
            "p1": {"life": 0, "lost_game": True},
            "p2": {"life": 20},
            "turn": 1,
            "max_turns": 100,
        })()
        info = {}
        self.assertTrue(env._check_game_end_conditions(info))
        self.assertEqual(info["game_result"], "win")

        env.game_state.p1 = {"life": 20}
        env.game_state.p2 = {"life": 0, "lost_game": True}
        info = {}
        self.assertTrue(env._check_game_end_conditions(info))
        self.assertEqual(info["game_result"], "loss")

    def test_promotion_scores_both_seats_and_requires_clean_fidelity(self):
        clean = {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS}
        legs = [
            {
                "records": [{"result": "win"}, {"result": "draw"}],
                "fidelity": clean, "manifest": {},
            },
            {
                "records": [{"result": "loss"}, {"result": "draw"}],
                "fidelity": clean, "manifest": {},
            },
        ]
        identity = {"name": "model.zip", "sha256": "a" * 64, "size": 1}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    protocol, "run_parallel_harvest", side_effect=legs), \
                mock.patch.object(
                    protocol.fixture, "checkpoint_identity", return_value=identity):
            output = Path(temp) / "promotion"
            decision = protocol.run_promotion(
                "candidate.zip", "baseline.zip", 4, 2, 7, output,
                minimum_score=0.7)
            self.assertEqual(decision["candidate_score"], 0.75)
            self.assertTrue(decision["promote"])
            self.assertEqual(
                json.loads((output / "promotion.json").read_text())["decision"],
                "promote",
            )

    def test_cli_parses_harvest_and_promotion(self):
        parser = protocol.build_parser()
        harvest = parser.parse_args([
            "harvest", "--games", "8", "--workers", "2",
            "--output", "out",
        ])
        self.assertEqual((harvest.command, harvest.games, harvest.workers),
                         ("harvest", 8, 2))
        self.assertIsNone(harvest.decks)
        self.assertIsNone(harvest.format)
        promote = parser.parse_args([
            "promote", "--games", "4", "--candidate", "c.zip",
            "--baseline", "b.zip", "--output", "out",
            "--decks", "MyDecks", "--format", "standard",
            "--format-dir", "formats/standard",
        ])
        self.assertEqual(promote.command, "promote")
        self.assertEqual(promote.decks, Path("MyDecks"))
        self.assertEqual(promote.format, "standard")
        self.assertEqual(promote.format_dir, Path("formats/standard"))
        qualify = parser.parse_args([
            "qualify", "--games", "8", "--candidate", "c.zip",
            "--output", "qualification", "--minimum-score", "0.6",
        ])
        self.assertEqual(qualify.command, "qualify")
        self.assertEqual(qualify.candidate, Path("c.zip"))
        self.assertEqual(qualify.minimum_score, 0.6)

    def test_shards_receive_corpus_arguments_and_publish_lineage(self):
        lineage = {"format": "standard", "corpus": {"sha256": "abc"}}
        seen_arguments = []

        def fake_shard(arguments):
            seen_arguments.append(arguments)
            count = arguments["games"]
            return {
                "shard": arguments["shard"],
                "offset": arguments["offset"],
                "games": count,
                "output": Path(arguments["output"]).name,
                "agent_version": "test-agent",
                "results": {"win": count},
                "records": [{"result": "win"} for _ in range(count)],
                "fidelity": {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS},
                "agent_policy": {"kind": "random-valid"},
                "opponent_policy": {"kind": "scripted"},
                "manifest": {},
                "lineage": dict(lineage),
                "decks": ["A", "B"],
            }

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(protocol, "_run_shard", side_effect=fake_shard):
            output = Path(temp) / "run"
            result = protocol.run_parallel_harvest(
                2, 1, 5, output, decks_directory="MyDecks",
                format_name="standard", format_dir="formats/standard")
        self.assertEqual(seen_arguments[0]["decks_directory"], "MyDecks")
        self.assertEqual(seen_arguments[0]["format_name"], "standard")
        self.assertEqual(seen_arguments[0]["format_dir"], "formats/standard")
        manifest = result["protocol_manifest"]
        self.assertEqual(manifest["lineage"], lineage)
        self.assertEqual(manifest["decks"], ["A", "B"])

    def test_lineage_mismatch_across_shards_fails_the_run(self):
        def fake_shard(arguments):
            return {
                "shard": arguments["shard"],
                "offset": arguments["offset"],
                "games": arguments["games"],
                "output": Path(arguments["output"]).name,
                "agent_version": "test-agent",
                "results": {"win": arguments["games"]},
                "records": [{"result": "win"}
                            for _ in range(arguments["games"])],
                "fidelity": {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS},
                "agent_policy": {"kind": "random-valid"},
                "opponent_policy": {"kind": "scripted"},
                "manifest": {},
                "lineage": {"corpus": {"sha256": f"shard-{arguments['shard']}"}},
                "decks": ["A", "B"],
            }

        class _SerialFuture:
            def __init__(self, value):
                self._value = value

            def result(self):
                return self._value

        class _SerialExecutor:
            def __init__(self, max_workers=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def submit(self, fn, item):
                return _SerialFuture(fn(item))

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(protocol, "_run_shard", side_effect=fake_shard), \
                mock.patch.object(protocol, "ProcessPoolExecutor", _SerialExecutor), \
                mock.patch.object(protocol, "as_completed", lambda futures: list(futures)):
            output = Path(temp) / "run"
            with self.assertRaisesRegex(RuntimeError, "lineage"):
                protocol.run_parallel_harvest(2, 2, 5, output)
            self.assertFalse((output / "harvest_protocol.json").exists())

    def test_qualification_pairs_seats_and_publishes_strength_gate(self):
        clean = {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS}
        identity = {"name": "candidate.zip", "sha256": "a" * 64, "size": 7}
        lineage = {"format": "standard", "corpus": {"sha256": "corpus"}}

        def leg(seat, records):
            return {
                "records": records,
                "fidelity": dict(clean),
                "manifest": {},
                "protocol_manifest": {
                    "status": "complete",
                    "agent_policy": identity,
                    "agent_seat": seat,
                    "lineage": lineage,
                },
            }

        legs = [
            leg("p1", [
                {"result": "win", "agent_is_p1": True},
                {"result": "draw", "agent_is_p1": True},
            ]),
            leg("p2", [
                {"result": "win", "agent_is_p1": False},
                {"result": "loss", "agent_is_p1": False},
            ]),
        ]
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    protocol, "run_parallel_harvest", side_effect=legs) as run, \
                mock.patch.object(
                    protocol.fixture, "checkpoint_identity",
                    return_value=identity):
            output = Path(temp) / "qualification"
            decision = protocol.run_qualification(
                "candidate.zip", 4, 2, 13, output, minimum_score=0.6)

            self.assertEqual(run.call_count, 2)
            self.assertIs(run.call_args_list[0].kwargs["agent_is_p1"], True)
            self.assertIs(run.call_args_list[1].kwargs["agent_is_p1"], False)
            self.assertIsNone(
                run.call_args_list[0].kwargs.get("opponent_model"))
            self.assertEqual(decision["candidate_score"], 0.625)
            self.assertEqual(
                decision["outcome_counts"],
                {"wins": 2, "losses": 1, "draws": 1})
            self.assertTrue(decision["fidelity_clean"])
            self.assertTrue(decision["passed"])
            saved = json.loads(
                (output / "qualification.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["decision"], "pass")
            self.assertEqual(saved["candidate"], identity)
            self.assertEqual(saved["lineage"], lineage)

    def test_qualification_fidelity_failure_is_recorded_and_cli_is_nonzero(self):
        decision = {
            "decision": "fail",
            "candidate_score": 0.75,
            "minimum_score": 0.55,
            "fidelity_clean": False,
            "passed": False,
        }
        with mock.patch.object(
                protocol, "run_qualification", return_value=decision):
            status = protocol.main([
                "qualify", "--games", "4", "--candidate", "candidate.zip",
                "--output", "qualification",
            ])
        self.assertEqual(status, 2)

    def test_winning_qualification_fails_closed_on_fidelity(self):
        clean = {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS}
        dirty = dict(clean, unparsed_effects=1)
        identity = {"name": "candidate.zip", "sha256": "a" * 64, "size": 7}
        lineage = {"format": "standard"}

        def leg(seat, fidelity, manifest):
            return {
                "records": [{
                    "result": "win", "agent_is_p1": seat == "p1",
                }],
                "fidelity": fidelity,
                "manifest": manifest,
                "protocol_manifest": {
                    "status": "complete",
                    "agent_policy": identity,
                    "agent_seat": seat,
                    "lineage": lineage,
                },
            }

        legs = [
            leg("p1", dirty, {
                "Unsupported Card": {
                    "count": 1, "severity": "unparsed", "reasons": {"x": 1},
                },
            }),
            leg("p2", clean, {}),
        ]
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    protocol, "run_parallel_harvest", side_effect=legs), \
                mock.patch.object(
                    protocol.fixture, "checkpoint_identity",
                    return_value=identity):
            output = Path(temp) / "qualification"
            decision = protocol.run_qualification(
                "candidate.zip", 2, 1, 13, output)
            saved = json.loads(
                (output / "qualification.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["candidate_score"], 1.0)
        self.assertFalse(decision["fidelity_clean"])
        self.assertFalse(decision["passed"])
        self.assertEqual(decision["severe_manifest_cards"], ["Unsupported Card"])
        self.assertEqual(saved["decision"], "fail")

    def test_invalid_qualification_never_publishes_decision_manifest(self):
        clean = {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS}
        identity = {"name": "candidate.zip", "sha256": "a" * 64, "size": 7}

        def leg(seat, lineage):
            return {
                "records": [{
                    "result": "win", "agent_is_p1": seat == "p1",
                }],
                "fidelity": dict(clean),
                "manifest": {},
                "protocol_manifest": {
                    "status": "complete",
                    "agent_policy": identity,
                    "agent_seat": seat,
                    "lineage": lineage,
                },
            }

        legs = [leg("p1", {"corpus": "one"}),
                leg("p2", {"corpus": "two"})]
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    protocol, "run_parallel_harvest", side_effect=legs), \
                mock.patch.object(
                    protocol.fixture, "checkpoint_identity",
                    return_value=identity):
            output = Path(temp) / "qualification"
            with self.assertRaisesRegex(RuntimeError, "lineage"):
                protocol.run_qualification(
                    "candidate.zip", 2, 1, 13, output)
            self.assertFalse((output / "qualification.json").exists())

    def test_qualification_rejects_record_seat_mismatch(self):
        clean = {key: 0 for key in protocol.fixture.FIDELITY_COUNTERS}
        identity = {"name": "candidate.zip", "sha256": "a" * 64, "size": 7}
        result = {
            "records": [{"result": "win", "agent_is_p1": True}],
            "fidelity": clean,
            "manifest": {},
            "protocol_manifest": {
                "status": "complete",
                "agent_policy": identity,
                "agent_seat": "p2",
                "lineage": {"format": "standard"},
            },
        }
        with self.assertRaisesRegex(RuntimeError, "mismatched game seat"):
            protocol._qualification_leg(
                result, games=1, seat="p2", candidate_identity=identity)


if __name__ == "__main__":
    unittest.main(verbosity=2)
