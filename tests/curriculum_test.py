"""Focused tests for deterministic opponent curriculum scheduling."""

import random
import unittest

import numpy as np

from Playersim.curriculum import (
    COMBAT_CURRICULUM_V1, COMBAT_CURRICULUM_V2, COMBAT_CURRICULUM_V3,
    CurriculumScheduler, derive_matchup_seed, resolve_curriculum,
)


DECK_NAMES = [
    "Selesnya Ouroboroid", "Jeskai Lessons", "Izzet Prowess",
    "4c Control", "Izzet Spellementals", "Dimir Excruciator",
    "Mono-Green Landfall", "Azorius Momo",
]
DECKS = [{"name": name} for name in DECK_NAMES]


class CurriculumSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.spec = resolve_curriculum("combat-v1", DECKS)

    def test_preset_resolves_and_validates_corpus(self):
        self.assertEqual(self.spec["id"], "combat-v1")
        self.assertEqual(self.spec["stages"][-1]["decks"], DECK_NAMES)
        with self.assertRaisesRegex(ValueError, "missing decks"):
            resolve_curriculum("combat-v1", DECKS[:-1])

    def test_schedule_is_deterministic_and_isolated_from_global_rng(self):
        seed = derive_matchup_seed(20260714, 3)

        def sample(noise):
            scheduler = CurriculumScheduler(self.spec, seed)
            rows = []
            for _ in range(100):
                if noise:
                    random.random()
                    np.random.random()
                row = scheduler.peek(agent_is_p1=True)
                rows.append((
                    row["agent_deck"], row["opponent_deck"],
                    row["opponent_profile"], row["stage"],
                ))
                scheduler.commit(row["stage_index"])
            return rows

        self.assertEqual(sample(False), sample(True))
        other = CurriculumScheduler(
            self.spec, derive_matchup_seed(20260715, 3))
        other_rows = []
        for _ in range(20):
            row = other.peek(True)
            other_rows.append((row["agent_deck"], row["opponent_deck"]))
            other.commit(row["stage_index"])
        self.assertNotEqual(
            [row[:2] for row in sample(False)[:20]], other_rows)

    def test_each_directed_pair_occurs_once_per_cycle(self):
        scheduler = CurriculumScheduler(self.spec, 123)
        stage = self.spec["stages"][0]
        expected = len(stage["decks"]) * (len(stage["decks"]) - 1)
        pairs = []
        for _ in range(expected):
            row = scheduler.peek(True)
            pairs.append((row["agent_deck"], row["opponent_deck"]))
            scheduler.commit(row["stage_index"])
        self.assertEqual(len(set(pairs)), expected)

    def test_profile_bags_have_exact_mix(self):
        scheduler = CurriculumScheduler(self.spec, 456)
        scheduler.set_timestep(75_000)
        profiles = []
        for _ in range(10):
            row = scheduler.peek(True)
            profiles.append(row["opponent_profile"])
            scheduler.commit(row["stage_index"])
        self.assertEqual(profiles.count("novice"), 5)
        self.assertEqual(profiles.count("scripted"), 5)

        scheduler.set_timestep(125_000)
        profiles = []
        for _ in range(10):
            row = scheduler.peek(False)
            profiles.append(row["opponent_profile"])
            scheduler.commit(row["stage_index"])
        self.assertEqual(profiles.count("novice"), 1)
        self.assertEqual(profiles.count("scripted"), 9)

    def test_stage_change_applies_to_next_peek_without_mutating_old_count(self):
        scheduler = CurriculumScheduler(self.spec, 789)
        first = scheduler.peek(True)
        scheduler.commit(first["stage_index"])
        scheduler.set_timestep(30_000)
        second = scheduler.peek(False)
        self.assertEqual((first["stage"], second["stage"]),
                         ("goldfish", "race"))
        self.assertEqual(second["matchup_episode_index"], 0)
        self.assertFalse(second["agent_is_p1"])
        self.assertEqual(second["p2_deck"], second["agent_deck"])

    def test_mastery_curriculum_uses_gradual_profile_mixes(self):
        spec = resolve_curriculum("combat-v2", DECKS)
        self.assertEqual(spec["progression"], "mastery")
        self.assertEqual(spec["stages"][0]["profile_bag"], ["passive"] * 10)
        self.assertEqual(
            spec["stages"][1]["profile_bag"].count("novice"), 3)
        self.assertEqual(
            spec["stages"][2]["profile_bag"].count("scripted"), 3)
        for stage in spec["stages"][:-1]:
            gate = stage["advance_when"]
            self.assertGreater(gate["window_episodes"], 0)
            self.assertGreater(gate["min_stage_timesteps"], 0)

    def test_mastery_stage_override_is_explicit_and_validated(self):
        spec = resolve_curriculum("combat-v2", DECKS)
        scheduler = CurriculumScheduler(spec, 901)
        scheduler.set_timestep(500_000)
        self.assertEqual(
            spec["stages"][scheduler.stage_index()]["name"], "full_pool")
        scheduler.set_stage(1, timestep=500_000)
        self.assertEqual(
            spec["stages"][scheduler.stage_index()]["name"], "race")
        row = scheduler.peek(True)
        self.assertEqual(row["stage"], "race")
        with self.assertRaisesRegex(ValueError, "out of range"):
            scheduler.set_stage(99)

    def test_v3_requires_active_opponent_mastery_and_bounds_full_pool(self):
        spec = resolve_curriculum("combat-v3", DECKS)
        self.assertEqual(spec["id"], "combat-v3")
        self.assertEqual(spec["version"], 3)
        self.assertEqual(
            spec["transition_semantics"],
            "central_mastery_or_deadline_future_reset_with_activation_ack")

        race, bridge = spec["stages"][1:3]
        self.assertGreater(
            race["profile_bag"].count("novice"),
            race["profile_bag"].count("passive"))
        self.assertNotIn("passive", bridge["profile_bag"])
        self.assertEqual(
            set(bridge["advance_when"]["profile_requirements"]),
            {"novice", "scripted"})
        self.assertEqual(
            set(race["advance_when"]["profile_requirements"]),
            {"novice"})

        # A fresh run reaches full_pool by 375k even if every mastery gate
        # misses.  A deadline transition is tracked separately by the callback.
        self.assertEqual(
            sum(stage["advance_when"]["max_stage_timesteps"]
                for stage in spec["stages"][:-1]),
            375_000)
        for stage in spec["stages"][:-1]:
            gate = stage["advance_when"]
            self.assertGreaterEqual(
                gate["max_stage_timesteps"], gate["min_stage_timesteps"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
