"""Fast self-tests for invariant fuzz profiles and replay artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

TESTS_ROOT = os.path.dirname(os.path.abspath(__file__))
if TESTS_ROOT not in sys.path:
    sys.path.insert(0, TESTS_ROOT)

import invariant_fuzz_test as fuzz  # noqa: E402


class InvariantFuzzConfigTests(unittest.TestCase):
    def test_profiles_are_nested_deterministic_budgets(self) -> None:
        short = fuzz.FUZZ_PROFILES["short"]
        default = fuzz.FUZZ_PROFILES["default"]
        long = fuzz.FUZZ_PROFILES["long"]

        self.assertEqual(short.seeds, fuzz.SEED_BUDGET[:3])
        self.assertEqual(default.seeds, fuzz.SEED_BUDGET[:8])
        self.assertEqual(long.seeds, fuzz.SEED_BUDGET)
        self.assertEqual(len(long.seeds), 32)
        self.assertEqual(len(set(long.seeds)), len(long.seeds))
        self.assertLess(short.steps * len(short.seeds), default.steps * len(default.seeds))
        self.assertLess(default.steps * len(default.seeds), long.steps * len(long.seeds))
        self.assertEqual(long.steps * len(long.seeds), 320_000)

    def test_profile_overrides_are_checked(self) -> None:
        resolved = fuzz.resolve_profile(
            "short", seeds=(11, 13), steps=17, check_every=3)
        self.assertEqual(
            resolved,
            fuzz.FuzzProfile(seeds=(11, 13), steps=17, check_every=3),
        )
        for kwargs in (
            {"seeds": ()},
            {"seeds": (1, 1)},
            {"steps": 0},
            {"check_every": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                fuzz.resolve_profile("short", **kwargs)

    def test_failure_artifact_round_trip_is_stable(self) -> None:
        trace = fuzz.SeedTrace(
            seed=17,
            actions=[4, 9, 3],
            contexts=[{}, {"battlefield_idx": 0}, {"controller_id": "p1"}],
            executed=3,
            episode=0,
            episode_seed=17,
            stage="post_step",
            where="seed 17 episode 0 action 2",
        )
        payload = fuzz.build_failure_artifact(
            trace,
            AssertionError("forced invariant failure"),
            profile_name="self-test",
            requested_steps=100,
            check_every=20,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = fuzz.write_failure_artifact(temp_dir, payload)
            first_bytes = path.read_bytes()
            loaded = fuzz.load_replay_artifact(path)
            fuzz.write_failure_artifact(temp_dir, payload)

            self.assertEqual(loaded, payload)
            self.assertEqual(path.read_bytes(), first_bytes)
            self.assertEqual(loaded["replay"]["actions"], [4, 9, 3])
            self.assertEqual(
                loaded["replay"]["contexts"],
                [{}, {"battlefield_idx": 0}, {"controller_id": "p1"}],
            )
            self.assertEqual(loaded["replay"]["steps"], 3)

    def test_pre_action_failure_replays_one_more_iteration(self) -> None:
        trace = fuzz.SeedTrace(
            seed=23,
            actions=[4, 9],
            executed=2,
            stage="action_mask",
            where="seed 23 episode 0 action 2",
        )
        payload = fuzz.build_failure_artifact(
            trace,
            AssertionError("empty mask"),
            profile_name="self-test",
            requested_steps=100,
            check_every=20,
        )
        self.assertEqual(payload["replay"]["steps"], 3)

    def test_invalid_replay_artifact_is_rejected(self) -> None:
        invalid = {
            "schema_version": fuzz.ARTIFACT_SCHEMA_VERSION,
            "kind": "playersim_invariant_fuzz_failure",
            "replay": {
                "seed": 17,
                "steps": 1,
                "check_every": 1,
                "actions": [4, 9],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "more entries"):
                fuzz.load_replay_artifact(path)

    def test_success_run_does_not_create_artifact_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_dir = Path(temp_dir) / "must-not-exist"
            failures = fuzz.run(
                (fuzz.SEED_BUDGET[0],),
                steps=1,
                check_every=1,
                profile_name="self-test",
                artifact_dir=artifact_dir,
            )
            self.assertEqual(failures, [])
            self.assertFalse(artifact_dir.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
