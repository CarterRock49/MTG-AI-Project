"""Correctness and persistence regressions for CardMemory."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.card_memory import CARD_MEMORY_VERSION, CardMemory


class CardMemoryIntegrityTest(unittest.TestCase):
    def _memory(self, directory: str) -> CardMemory:
        return CardMemory(directory, use_compression=False)

    def test_save_failure_preserves_previous_file_and_dirty_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(7, "Atomic Probe")
            memory.update_card_performance(7, {
                "is_win": True, "deck_archetype": "midrange"})
            self.assertTrue(memory.save_all_card_data())
            path = Path(directory) / "all_cards.json"
            original = path.read_bytes()

            memory.update_card_performance(7, {
                "is_win": False, "deck_archetype": "midrange"})
            memory.updates_since_save = memory.save_frequency
            with patch(
                    "Playersim.card_memory.json.dump",
                    side_effect=RuntimeError("forced write failure")):
                self.assertFalse(memory.save_all_card_data())

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(memory.updates_since_save, memory.save_frequency)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[
                "schema_version"], CARD_MEMORY_VERSION)

    def test_async_save_coalesces_and_is_joinable(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            started = threading.Event()
            release = threading.Event()

            def blocking_save():
                started.set()
                release.wait(2)
                return True

            memory.save_all_card_data = blocking_save
            first = memory.save_memory_async()
            self.assertTrue(started.wait(1))
            second = memory.save_memory_async()
            self.assertIs(first, second)
            self.assertFalse(first.daemon)
            release.set()
            first.join(2)
            self.assertFalse(first.is_alive())

    def test_updates_invalidate_archetype_effectiveness_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(1, "Adaptive Probe")
            for result in (True, True, True):
                memory.update_card_performance(1, {
                    "is_win": result, "deck_archetype": "aggro"})

            self.assertEqual(
                memory.get_effectiveness_for_archetype(1, "aggro"), 1.0)
            self.assertIn("1_aggro", memory.cache)
            memory.update_card_performance(1, {
                "is_win": False, "deck_archetype": "aggro"})
            self.assertNotIn("1_aggro", memory.cache)
            self.assertEqual(
                memory.get_effectiveness_for_archetype(1, "aggro"), 0.75)

            memory.get_effectiveness_for_archetype(1, "control")
            self.assertIn("1_control", memory.cache)
            memory.update_meta_position(1, {
                "popularity": 0.8, "win_rate": 0.7})
            self.assertNotIn("1_control", memory.cache)

    def test_half_win_rate_is_neutral_and_draw_buckets_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(2, "Draw Probe")
            for result in (
                    {"is_win": True},
                    {"is_win": False},
                    {"is_draw": True, "was_played": True,
                     "turn_played": 2, "cmc": 2,
                     "in_opening_hand": True}):
                memory.update_card_performance(2, {
                    "deck_archetype": "control", **result})

            stats = memory.card_data["2"]
            self.assertEqual(stats["draws_in_opening_hand"], 1)
            self.assertEqual(
                stats["mana_curve_performance"]["on_curve"]["draws"], 1)
            self.assertEqual(
                memory.get_effectiveness_for_archetype(2, "control"), 0.5)
            self.assertGreaterEqual(stats["effectiveness_rating"], 0.0)
            self.assertLessEqual(stats["effectiveness_rating"], 1.0)

    def test_registered_mana_value_drives_curve_without_duplicate_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(21, "Registered Curve Probe", {"cmc": 3})

            memory.update_card_performance(21, {
                "is_win": True,
                "was_played": True,
                "turn_played": 3,
                "deck_archetype": "midrange",
            })

            self.assertEqual(
                memory.card_data["21"]["mana_curve_performance"]
                ["on_curve"]["played"],
                1)

    def test_synergy_requires_both_cards_played_and_counts_once_per_game(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(3, "Pair A")
            memory.update_card_performance(3, {
                "is_win": True, "was_played": False,
                "synergy_partners": [4, 4], "deck_archetype": "combo"})
            self.assertEqual(memory.card_data["3"]["synergy_partners"], {})

            memory.update_card_performance(3, {
                "is_win": True, "was_played": True,
                "synergy_partners": [4, 4], "deck_archetype": "combo"})
            self.assertEqual(
                memory.card_data["3"]["synergy_partners"]["4"]
                ["games_together"], 1)

    def test_invalid_telemetry_does_not_partially_apply_a_game(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(5, "Transaction Probe")
            before = json.loads(json.dumps(memory.card_data["5"]))
            memory.update_card_performance(5, {
                "is_win": True,
                "deck_archetype": [],  # unhashable, rejected transactionally
            })
            after = json.loads(json.dumps(memory.card_data["5"]))
            self.assertEqual(after, before)

    def test_duplicate_names_fail_closed_without_merging_card_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(10, "Ambiguous Name")
            memory.register_card(11, "Ambiguous Name")
            memory.update_card_performance(10, {
                "is_win": True, "deck_archetype": "aggro"})
            memory.update_card_performance(11, {
                "is_win": False, "deck_archetype": "control"})

            self.assertEqual(memory.get_card_stats(10)["wins"], 1)
            self.assertEqual(memory.get_card_stats(11)["losses"], 1)
            self.assertEqual(memory.get_card_stats("Ambiguous Name"), {})
            self.assertIn("Ambiguous Name", memory.ambiguous_card_names)
            self.assertTrue(memory.save_all_card_data())

            reloaded = self._memory(directory)
            self.assertEqual(reloaded.get_card_stats("Ambiguous Name"), {})
            self.assertEqual(reloaded.get_card_stats(10)["wins"], 1)
            self.assertEqual(reloaded.get_card_stats(11)["losses"], 1)

    def test_registration_metadata_cannot_overwrite_performance_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(12, "Stable Counters")
            memory.update_card_performance(12, {
                "is_win": True, "deck_archetype": "midrange"})
            memory.register_card(12, "Stable Counters", {
                "cmc": 3, "games_played": 0, "wins": 0,
            })
            stats = memory.get_card_stats(12)
            self.assertEqual(stats["games_played"], 1)
            self.assertEqual(stats["wins"], 1)
            self.assertEqual(stats["cmc"], 3)

    def test_loaded_nested_shapes_are_repaired_before_the_next_update(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "all_cards.json"
            path.write_text(json.dumps({
                "schema_version": CARD_MEMORY_VERSION,
                "cards": {
                    "30": {
                        "id": 30,
                        "name": "Repair Probe",
                        "games_played": 1,
                        "wins": 1,
                        "turn_played": [],
                        "performance_by_turn": {"3": "bad"},
                        "archetype_performance": [],
                        "synergy_partners": {"31": None},
                        "performance_trend": {},
                        "meta_position": [],
                    },
                },
            }), encoding="utf-8")
            memory = self._memory(directory)

            memory.update_card_performance(30, {
                "is_win": True,
                "was_played": True,
                "turn_played": 3,
                "cmc": 3,
                "deck_archetype": "midrange",
                "synergy_partners": [31],
            })

            stats = memory.get_card_stats(30)
            self.assertEqual(stats["games_played"], 2)
            self.assertEqual(stats["performance_by_turn"]["3"]["played"], 1)
            self.assertEqual(
                stats["archetype_performance"]["midrange"]["games"], 1)
            self.assertEqual(
                stats["synergy_partners"]["31"]["games_together"], 1)
            self.assertEqual(stats["performance_trend"], [1.0])

    def test_environment_converts_both_seats_global_play_turns(self):
        from Playersim.environment import AlphaZeroMTGEnv

        with tempfile.TemporaryDirectory() as directory:
            cards = {
                1: SimpleNamespace(
                    name="P1 Three Drop", cmc=3,
                    card_types=["creature"], colors=[0] * 5),
                2: SimpleNamespace(
                    name="P2 Three Drop", cmc=3,
                    card_types=["creature"], colors=[0] * 5),
            }
            env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
            env.has_card_memory = True
            env.card_memory = self._memory(directory)
            env.game_state = SimpleNamespace(
                canonical_card_id=lambda card_id: card_id,
                _safe_get_card=lambda card_id: cards.get(card_id))
            cards_played = {0: [1], 1: [2]}
            play_history = {0: {5: [1]}, 1: {6: [2]}}

            env._record_cards_to_memory(
                [1], [2], cards_played, 6, "aggro", "control",
                {}, {}, play_history, is_win=True, player_idx=0)
            env._record_cards_to_memory(
                [2], [1], cards_played, 6, "control", "aggro",
                {}, {}, play_history, is_win=False, player_idx=1)

            for card_id in (1, 2):
                stats = env.card_memory.get_card_stats(card_id)
                self.assertEqual(stats["turn_played"], {"3": 1})
                self.assertEqual(
                    stats["mana_curve_performance"]["on_curve"]["played"],
                    1)

    def test_meta_win_rate_trend_survives_updates_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            memory.register_card(40, "Trend Probe")
            memory.update_meta_position(40, {
                "popularity": 0.6,
                "win_rate": 0.75,
            })
            original_trend = list(
                memory.get_card_stats(40)["meta_position"]["win_rate_trend"])

            memory.update_card_performance(40, {
                "is_win": True,
                "deck_archetype": "control",
            })
            self.assertEqual(
                memory.get_card_stats(40)["meta_position"]["win_rate_trend"],
                original_trend)
            self.assertTrue(memory.save_all_card_data())

            reloaded = self._memory(directory)
            self.assertEqual(
                reloaded.get_card_stats(40)["meta_position"]["win_rate_trend"],
                original_trend)


if __name__ == "__main__":
    unittest.main()
