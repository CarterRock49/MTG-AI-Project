"""Regression tests for deterministic optional strategy memory."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.strategy_memory import (  # noqa: E402
    STRATEGY_MEMORY_DIAGNOSTICS_KIND,
    STRATEGY_MEMORY_DIAGNOSTICS_SCHEMA_VERSION,
    STRATEGY_MEMORY_MAX_ABS_PATTERN_VALUE,
    STRATEGY_MEMORY_MAX_ACTION_INDEX,
    STRATEGY_MEMORY_MAX_PATTERN_FIELDS,
    STRATEGY_MEMORY_SCHEMA_VERSION,
    StrategyMemory,
)


def _state(agent_is_p1=True):
    state = SimpleNamespace(
        p1={
            "life": 15, "hand": [1], "battlefield": [],
        },
        p2={
            "life": 20, "hand": [2, 3, 4], "battlefield": [],
        },
        agent_is_p1=agent_is_p1,
        turn=5,
        phase=2,
        stack=[],
        PHASE_DECLARE_ATTACKERS=6,
        PHASE_DECLARE_BLOCKERS=7,
        PHASE_COMBAT_DAMAGE=9,
        PHASE_END_STEP=14,
        PHASE_CLEANUP=15,
    )
    state._safe_get_card = lambda _card_id: None
    return state


class StrategyMemoryTest(unittest.TestCase):
    def test_environment_reuses_explicit_memory_across_resets(self):
        from Playersim.environment import AlphaZeroMTGEnv

        environment = object.__new__(AlphaZeroMTGEnv)
        environment.strategy_memory_enabled = True
        environment.strategy_memory = StrategyMemory(
            None, auto_save_interval=0)
        environment.game_state = SimpleNamespace(strategy_memory=None)
        environment._strategy_memory_file = lambda: "unused.pkl"
        original = environment.strategy_memory
        environment.initialize_strategic_memory()
        environment.initialize_strategic_memory()
        self.assertIs(environment.strategy_memory, original)
        self.assertIs(environment.game_state.strategy_memory, original)

    def test_empty_memory_has_no_random_fallback(self):
        memory = StrategyMemory(
            None, auto_save_interval=0, min_action_count=1)
        state = _state()
        before = copy.deepcopy(memory.strategies)
        for _ in range(20):
            self.assertIsNone(memory.get_suggested_action(
                state, [2, 5, 11], exploration_rate=1.0))
        self.assertEqual(memory.strategies, before)

    def test_action_evidence_is_deterministic_and_mask_safe(self):
        memory = StrategyMemory(
            None, auto_save_interval=0, min_action_count=2)
        state = _state()
        pattern = memory.extract_strategy_pattern(state)
        for action in (5, 3):
            memory.update_strategy(pattern, 1.0, action_idx=action)
            memory.update_strategy(pattern, 1.0, action_idx=action)

        mask = np.zeros(8, dtype=bool)
        mask[[3, 5]] = True
        baseline = copy.deepcopy(memory.strategies)
        suggestions = {
            memory.get_suggested_action(state, mask)
            for _ in range(20)
        }
        self.assertEqual(suggestions, {3})
        self.assertEqual(memory.strategies, baseline)

        mask[3] = False
        self.assertEqual(memory.get_suggested_action(state, mask), 5)
        mask[5] = False
        self.assertIsNone(memory.get_suggested_action(state, mask))

    def test_pattern_is_observer_relative(self):
        memory = StrategyMemory(
            None, auto_save_interval=0, min_action_count=1)
        p1_pattern = memory.extract_strategy_pattern(_state(True))
        p2_pattern = memory.extract_strategy_pattern(_state(False))
        self.assertEqual(len(p1_pattern), 14)
        self.assertEqual(len(p2_pattern), 14)
        self.assertEqual(p1_pattern[3], -p2_pattern[3])
        self.assertEqual(p1_pattern[4], -p2_pattern[4])

    def test_versioned_atomic_round_trip(self):
        state = _state()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "strategy_memory.pkl")
            memory = StrategyMemory(
                path, auto_save_interval=0, min_action_count=1)
            pattern = memory.extract_strategy_pattern(state)
            memory.update_strategy(pattern, 2.0, action_idx=7)
            memory.record_action_sequence([7, 11], 2.0)
            self.assertTrue(memory.save_memory())
            self.assertFalse(memory.dirty)

            with open(path, "rb") as handle:
                payload = pickle.load(handle)
            self.assertEqual(
                payload["schema_version"], STRATEGY_MEMORY_SCHEMA_VERSION)
            self.assertFalse(any(
                name.endswith(".tmp") for name in os.listdir(directory)))

            loaded = StrategyMemory(
                path, auto_save_interval=0, min_action_count=1)
            self.assertEqual(loaded.logical_update, 1)
            self.assertEqual(
                loaded.get_suggested_action(state, [7, 11]), 7)
            self.assertEqual(len(loaded.action_sequences), 1)

    def test_safe_diagnostics_export_is_bounded_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "strategy_memory.pkl")
            diagnostics_path = os.path.join(
                directory, "strategy_memory.json.gz")
            memory = StrategyMemory(
                path, auto_save_interval=0, min_action_count=1)
            first_pattern = (0,) * 14
            second_pattern = (1,) + (0,) * 13
            memory.update_strategy(first_pattern, 2.0, action_idx=7)
            memory.update_strategy(first_pattern, -1.0, action_idx=7)
            memory.update_strategy(second_pattern, 0.0, action_idx=3)
            memory.record_action_sequence([7, 3], 2.0)

            with patch(
                    "Playersim.strategy_memory."
                    "STRATEGY_MEMORY_TOP_PATTERN_LIMIT", 1), patch(
                    "Playersim.strategy_memory."
                    "STRATEGY_MEMORY_TOP_ACTION_LIMIT", 1):
                self.assertTrue(memory.save_memory())
                with open(diagnostics_path, "rb") as handle:
                    first_bytes = handle.read()
                self.assertTrue(memory.save_memory())
                with open(diagnostics_path, "rb") as handle:
                    self.assertEqual(handle.read(), first_bytes)

                with gzip.open(diagnostics_path, "rt", encoding="utf-8") \
                        as handle:
                    payload = json.load(handle)

            self.assertEqual(
                payload["kind"], STRATEGY_MEMORY_DIAGNOSTICS_KIND)
            self.assertEqual(
                payload["schema_version"],
                STRATEGY_MEMORY_DIAGNOSTICS_SCHEMA_VERSION)
            self.assertEqual(
                payload["source_memory_schema_version"],
                STRATEGY_MEMORY_SCHEMA_VERSION)
            with open(path, "rb") as handle:
                raw_pickle = handle.read()
            self.assertEqual(payload["source_pickle"], {
                "size_bytes": len(raw_pickle),
                "sha256": hashlib.sha256(raw_pickle).hexdigest(),
            })
            self.assertEqual(payload["logical_update"], 3)
            self.assertEqual(payload["counts"], {
                "patterns": 2,
                "pattern_evidence": 3,
                "pattern_actions": 2,
                "action_evidence": 3,
                "action_sequences": 1,
            })
            self.assertAlmostEqual(
                payload["aggregates"][
                    "pattern_evidence_weighted_mean_reward"], 1.0 / 3.0)
            self.assertAlmostEqual(
                payload["aggregates"][
                    "pattern_evidence_weighted_positive_reward_rate"],
                1.0 / 3.0)
            self.assertAlmostEqual(
                payload["aggregates"][
                    "action_evidence_weighted_mean_reward"], 1.0 / 3.0)
            self.assertAlmostEqual(
                payload["aggregates"][
                    "action_evidence_weighted_positive_reward_rate"],
                1.0 / 3.0)
            self.assertIn("not a game win rate", payload["semantics"][
                "positive_reward_rate"])
            self.assertEqual(payload["limits"], {
                "top_patterns": 1,
                "top_actions": 1,
                "max_pattern_fields": STRATEGY_MEMORY_MAX_PATTERN_FIELDS,
                "max_abs_pattern_value":
                    STRATEGY_MEMORY_MAX_ABS_PATTERN_VALUE,
                "max_action_index": STRATEGY_MEMORY_MAX_ACTION_INDEX,
            })
            self.assertEqual(payload["truncation"]["top_patterns"], {
                "total": 2, "returned": 1, "truncated": True})
            self.assertEqual(payload["truncation"]["top_actions"], {
                "total": 2, "returned": 1, "truncated": True})
            self.assertEqual(payload["top_patterns"][0]["pattern"],
                             list(first_pattern))
            self.assertEqual(payload["top_patterns"][0]["action_evidence"], 2)
            self.assertEqual(payload["top_actions"][0]["pattern"],
                             list(first_pattern))
            self.assertEqual(payload["top_actions"][0]["action_index"], 7)
            self.assertFalse(any(
                name.endswith(".tmp") for name in os.listdir(directory)))

            previous_marker = dict(payload["source_pickle"])
            memory.update_strategy((2,) * 14, 3.0, action_idx=9)
            self.assertTrue(memory.save_memory())
            with gzip.open(diagnostics_path, "rt", encoding="utf-8") \
                    as handle:
                next_payload = json.load(handle)
            with open(path, "rb") as handle:
                next_pickle = handle.read()
            self.assertNotEqual(
                next_payload["source_pickle"], previous_marker)
            self.assertEqual(next_payload["source_pickle"], {
                "size_bytes": len(next_pickle),
                "sha256": hashlib.sha256(next_pickle).hexdigest(),
            })

    def test_diagnostics_export_failure_does_not_fail_pickle_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "strategy_memory.pkl")
            memory = StrategyMemory(path, auto_save_interval=0)
            memory.update_strategy((0,) * 14, 1.0, action_idx=5)
            self.assertTrue(memory.save_memory())
            diagnostics_path = os.path.join(
                directory, "strategy_memory.json.gz")
            self.assertTrue(os.path.exists(diagnostics_path))
            with gzip.open(diagnostics_path, "rt", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["logical_update"], 1)

            memory.update_strategy((1,) * 14, -1.0, action_idx=6)

            observed = {}

            def fail_after_pickle_replacement(_path, new_payload):
                with gzip.open(
                        diagnostics_path, "rt", encoding="utf-8") as handle:
                    old_payload = json.load(handle)
                with open(path, "rb") as handle:
                    current_pickle = handle.read()
                observed["old_marker"] = old_payload["source_pickle"]
                observed["current_marker"] = {
                    "size_bytes": len(current_pickle),
                    "sha256": hashlib.sha256(current_pickle).hexdigest(),
                }
                observed["new_marker"] = new_payload["source_pickle"]
                raise OSError("diagnostics disk failure")

            with patch.object(
                    memory, "_write_diagnostics_export",
                    side_effect=fail_after_pickle_replacement), patch(
                    "Playersim.strategy_memory.logging.error") as error_log:
                self.assertTrue(memory.save_memory())

            self.assertFalse(memory.dirty)
            with open(path, "rb") as handle:
                pickle_payload = pickle.load(handle)
            self.assertEqual(
                pickle_payload["schema_version"],
                STRATEGY_MEMORY_SCHEMA_VERSION)
            self.assertEqual(pickle_payload["logical_update"], 2)
            self.assertNotEqual(
                observed["old_marker"], observed["current_marker"])
            self.assertEqual(
                observed["new_marker"], observed["current_marker"])
            # A previous successful generation must not survive and masquerade
            # as the matching safe export for the newer pickle.
            self.assertFalse(os.path.exists(diagnostics_path))
            self.assertTrue(any(
                "Could not export strategy-memory diagnostics" in str(call)
                for call in error_log.call_args_list))

    def test_pattern_and_action_bounds_reject_pathological_inputs(self):
        memory = StrategyMemory(None, auto_save_interval=0)
        baseline = copy.deepcopy(memory.strategies)
        self.assertFalse(memory.update_strategy(
            (0,) * (STRATEGY_MEMORY_MAX_PATTERN_FIELDS + 1), 1.0,
            action_idx=1))
        self.assertFalse(memory.update_strategy(
            (float("nan"),), 1.0, action_idx=1))
        self.assertFalse(memory.update_strategy(
            (float("inf"),), 1.0, action_idx=1))
        self.assertFalse(memory.update_strategy(
            (STRATEGY_MEMORY_MAX_ABS_PATTERN_VALUE + 1,), 1.0,
            action_idx=1))
        self.assertFalse(memory.update_strategy(
            (0,) * 14, 1.0,
            action_idx=STRATEGY_MEMORY_MAX_ACTION_INDEX + 1))
        self.assertEqual(memory.strategies, baseline)
        self.assertEqual(memory.logical_update, 0)

        self.assertFalse(memory.record_action_sequence(
            [STRATEGY_MEMORY_MAX_ACTION_INDEX + 1], 1.0))
        self.assertEqual(memory.action_sequences, [])

    def test_legacy_aggregate_does_not_invent_action_evidence(self):
        state = _state()
        pattern = StrategyMemory(
            None, auto_save_interval=0).extract_strategy_pattern(state)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "legacy.pkl")
            with open(path, "wb") as handle:
                pickle.dump({
                    "strategies": {pattern: {
                        "count": 10, "reward": 3.0,
                        "success_rate": 0.9, "timestamp": 123.0,
                    }},
                    "action_sequences": [([{"action_idx": 5}], 3.0)],
                }, handle)
            loaded = StrategyMemory(
                path, auto_save_interval=0, min_action_count=1)
            self.assertEqual(loaded.strategies[pattern]["actions"], {})
            self.assertIsNone(loaded.get_suggested_action(state, [5]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
