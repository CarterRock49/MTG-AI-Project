"""Paired-seat observation audit: the learned seat must be a pure viewpoint.

Motivation (Round 7.91): the 7.90 evaluation showed a suspicious seat split —
5-27 as P1 versus 10-20-2 as P2 across mirrored seed pairs.  If any extractor
reads gs.p1/gs.p2 directly instead of resolving the agent seat, the policy
trains on silently swapped features for half its episodes and the split is a
bug, not variance.  This audit pins the perspective contract on one shared
game state: extracting as the P1-agent and as the P2-agent must produce
exactly mirrored my_*/opp_* fields, a flipped is_my_turn, a negated
life_difference, and identical seat-neutral globals.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_test import fresh, get_env, inject_into_zone  # noqa: E402


# Hidden-information fields have no opponent counterpart (e.g. my_hand), so
# the pairing below skips them automatically.  Face-down zones would make
# some pairs legitimately viewer-dependent; the fixture stages none.
SEAT_NEUTRAL_KEYS = ("turn", "phase", "stack_count")


def creature(name, power, toughness):
    return {
        "name": name, "mana_cost": "{2}", "cmc": 2,
        "type_line": "Creature — Auditor", "oracle_text": "",
        "power": power, "toughness": toughness, "color_identity": ["W"],
    }


class SeatParityAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gs = fresh(97100)
        env = get_env()

        # Make the two sides deliberately asymmetric so a swapped extractor
        # cannot pass by coincidence.
        gs.p1["life"] = 14
        gs.p2["life"] = 9
        inject_into_zone(gs, gs.p1, creature("P1 Big Auditor", 5, 5),
                         "battlefield")
        inject_into_zone(gs, gs.p1, creature("P1 Small Auditor", 1, 2),
                         "battlefield")
        inject_into_zone(gs, gs.p2, creature("P2 Lone Auditor", 3, 1),
                         "battlefield")
        inject_into_zone(gs, gs.p2, creature("P2 Dead Auditor", 2, 2),
                         "graveyard")
        gs.p1["mana_pool"] = {"W": 2, "U": 0, "B": 0, "R": 1, "G": 0, "C": 0}
        gs.turn = 5  # P1 is the active player under engine turn parity.

        gs.agent_is_p1 = True
        cls.obs_as_p1 = env._get_obs()
        self_error = env.last_observation_error
        assert self_error is None, f"P1-seat extraction degraded: {self_error}"

        gs.agent_is_p1 = False
        cls.obs_as_p2 = env._get_obs()
        self_error = env.last_observation_error
        assert self_error is None, f"P2-seat extraction degraded: {self_error}"

        gs.agent_is_p1 = True

    def _paired_suffixes(self):
        keys_a = set(self.obs_as_p1.keys())
        suffixes = sorted(
            key[len("my_"):] for key in keys_a
            if key.startswith("my_") and f"opp_{key[len('my_'):]}" in keys_a)
        self.assertGreater(
            len(suffixes), 10, "the my_/opp_ pairing found too few fields")
        return suffixes

    def test_every_paired_field_mirrors_exactly_across_seats(self):
        mismatches = []
        for suffix in self._paired_suffixes():
            mine, theirs = f"my_{suffix}", f"opp_{suffix}"
            if not np.array_equal(
                    self.obs_as_p1[mine], self.obs_as_p2[theirs]):
                mismatches.append(f"{mine} (P1 view) != {theirs} (P2 view)")
            if not np.array_equal(
                    self.obs_as_p1[theirs], self.obs_as_p2[mine]):
                mismatches.append(f"{theirs} (P1 view) != {mine} (P2 view)")
        self.assertEqual(
            mismatches, [],
            "seat-dependent extraction leaked into: " + ", ".join(mismatches))

    def test_scalars_flip_or_negate_with_the_seat(self):
        self.assertEqual(int(self.obs_as_p1["is_my_turn"][0]), 1)
        self.assertEqual(int(self.obs_as_p2["is_my_turn"][0]), 0)
        self.assertEqual(int(self.obs_as_p1["my_life"][0]), 14)
        self.assertEqual(int(self.obs_as_p2["my_life"][0]), 9)
        self.assertEqual(
            int(self.obs_as_p1["life_difference"][0]),
            -int(self.obs_as_p2["life_difference"][0]))

    def test_seat_neutral_globals_are_identical(self):
        for key in SEAT_NEUTRAL_KEYS:
            if key not in self.obs_as_p1:
                continue
            with self.subTest(key):
                self.assertTrue(
                    np.array_equal(self.obs_as_p1[key], self.obs_as_p2[key]),
                    f"seat-neutral field {key} changed with the agent seat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
