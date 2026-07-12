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


if __name__ == "__main__":
    unittest.main(verbosity=2)
