"""Regression tests for deterministic optional strategy memory."""

from __future__ import annotations

import copy
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.strategy_memory import (  # noqa: E402
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
