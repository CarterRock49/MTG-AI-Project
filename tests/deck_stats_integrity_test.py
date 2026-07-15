"""Correctness and persistence regressions for DeckStatsTracker."""

from __future__ import annotations

import asyncio
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Playersim.deck_stats_tracker import (
    DeckStatsTracker,
    GamePosition,
)


class DeckStatsIntegrityTest(unittest.TestCase):
    def _tracker(self, directory: str, cards=None) -> DeckStatsTracker:
        tracker = DeckStatsTracker(
            directory, card_db=cards or {}, use_compression=False,
            persistence_interval_games=100)
        tracker.identify_archetype = lambda _deck: "midrange"
        return tracker

    def test_save_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)
            self.assertTrue(tracker.save("meta/probe.json", {"games": 1}))
            path = Path(directory) / "meta" / "probe.json"
            original = path.read_bytes()

            with patch(
                    "Playersim.deck_stats_tracker.json.dump",
                    side_effect=RuntimeError("forced write failure")):
                self.assertFalse(
                    tracker.save("meta/probe.json", {"games": 2}))

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"games": 1})
            self.assertEqual(list((Path(directory) / "meta").glob("*.tmp")), [])

    def test_metadata_only_public_update_does_not_reference_missing_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)
            self.assertTrue(tracker.update_deck_stats("deck-id", {
                "name": "Metadata Only", "deck_id": "deck-id",
                "note": "safe",
            }))
            self.assertEqual(
                tracker.batch_updates["deck-id"]["note"], "safe")

    def test_public_draw_updates_are_additive_and_half_weighted(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)

            for _ in range(2):
                self.assertTrue(tracker.update_deck_stats(
                    "draw-deck", {"draws": 1, "games": 1}))

            stats = tracker.batch_updates["draw-deck"]
            self.assertEqual(stats["wins"], 0)
            self.assertEqual(stats["losses"], 0)
            self.assertEqual(stats["draws"], 2)
            self.assertEqual(stats["games"], 2)
            self.assertEqual(stats["win_rate"], 0.5)

    def test_sync_flush_works_when_called_from_a_running_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)
            tracker.batch_updates["deck-id"] = {
                "name": "Async Caller", "deck_id": "deck-id",
                "archetype": "midrange", "card_list": [],
                "wins": 1, "losses": 0, "draws": 0, "games": 1,
                "avg_game_length": 4, "total_turns": 4,
            }

            async def flush_inside_loop():
                return tracker.save_updates_sync()

            self.assertTrue(asyncio.run(flush_inside_loop()))
            self.assertEqual(tracker.batch_updates, {})
            self.assertTrue(any(
                (Path(directory) / "decks").glob("*.json")))

    def test_dict_deck_fingerprint_is_supported_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)
            first = tracker.get_deck_fingerprint([
                {"id": 2, "count": 4}, {"id": 1, "count": 2}])
            second = tracker.get_deck_fingerprint([
                {"id": 1, "count": 2}, {"id": 2, "count": 4}])
            self.assertEqual(first, second)

    def test_nonfinite_aggregate_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)
            valid, errors = tracker.validate_deck_stats({
                "name": "NaN", "archetype": "midrange", "card_list": [],
                "wins": 0, "losses": 0, "draws": 0, "games": 0,
                "avg_game_length": math.nan,
            })
            self.assertFalse(valid)
            self.assertTrue(any("avg_game_length" in error for error in errors))

    def test_record_game_inverts_loser_position_and_preserves_unknown_order(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)
            tracker.update_meta_with_game_result = Mock(return_value=True)
            tracker._update_card_stats = Mock(return_value=True)
            tracker.save_updates_sync = Mock(return_value=True)
            updates = []
            tracker._update_deck_stats = Mock(
                side_effect=lambda **kwargs: updates.append(kwargs) or True)

            self.assertTrue(tracker.record_game(
                [1], [2], {}, 6, game_state="ahead"))
            self.assertEqual(updates[0]["game_state"], GamePosition.AHEAD)
            self.assertEqual(updates[1]["game_state"], GamePosition.BEHIND)
            self.assertIsNone(updates[0]["play_order"])
            self.assertIsNone(updates[1]["play_order"])

    def test_real_card_history_does_not_invent_cmc_play_turns(self):
        cards = {
            1: SimpleNamespace(name="Winner Card", cmc=5),
            2: SimpleNamespace(name="Loser Card", cmc=3),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            tracker.update_meta_with_game_result = Mock(return_value=True)
            self.assertTrue(tracker.record_game(
                [1], [2], cards, 6, cards_played={0: [1], 1: [2]},
                game_state="ahead",
                play_order={"first_player": "winner"}))

            winner = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([1]))
            loser = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([2]))
            self.assertEqual(
                winner["card_performance"]["1"]["performance_by_turn"], {})
            self.assertEqual(
                loser["card_performance"]["2"]["performance_by_turn"], {})
            self.assertEqual(
                winner["performance_by_position"]["ahead"]["wins"], 1)
            self.assertEqual(
                loser["performance_by_position"]["behind"]["losses"], 1)

            winner_card = tracker._individual_card_cache[
                tracker._card_stats_file("Winner Card")]
            loser_card = tracker._individual_card_cache[
                tracker._card_stats_file("Loser Card")]
            self.assertEqual(winner_card["by_game_state"]["ahead"]["wins"], 1)
            self.assertEqual(loser_card["by_game_state"]["behind"]["games"], 1)

    def test_global_play_turns_are_stored_as_each_players_turn(self):
        cards = {
            1: SimpleNamespace(name="P1 Three Drop", cmc=3),
            2: SimpleNamespace(name="P2 Three Drop", cmc=3),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            tracker.update_meta_with_game_result = Mock(return_value=True)
            self.assertTrue(tracker.record_game(
                [1], [2], cards, 6,
                cards_played={0: [1], 1: [2]},
                play_history={
                    "winner": {5: [1]},
                    "loser": {6: [2]},
                },
                draw_history={
                    "winner": {5: [1]},
                    "loser": {6: [2]},
                },
                game_state="ahead",
                play_order={"first_player": "winner"}))

            winner = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([1]))
            loser = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([2]))
            for stats, card_id in ((winner, "1"), (loser, "2")):
                card_stats = stats["card_performance"][card_id]
                self.assertEqual(
                    card_stats["performance_by_turn"]["3"]["played"], 1)
                self.assertEqual(
                    card_stats["play_curve_stats"]["on_curve"]["games"], 1)
                self.assertEqual(
                    stats["draw_history_stats"]["3"][card_id]["games"], 1)

    def test_unknown_play_order_stays_unknown_in_card_aggregates(self):
        cards = {
            1: SimpleNamespace(name="Winner Unknown Seat", cmc=1),
            2: SimpleNamespace(name="Loser Unknown Seat", cmc=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            tracker.update_meta_with_game_result = Mock(return_value=True)
            tracker._save_individual_card_stats = Mock(return_value=True)
            self.assertTrue(tracker.record_game(
                [1], [2], cards, 4, cards_played={0: [1], 1: [2]}))

            winner = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([1]))
            loser = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([2]))
            self.assertEqual(
                winner["card_performance"]["1"]
                ["play_order_performance"]["unknown"]["played"], 1)
            self.assertEqual(
                loser["card_performance"]["2"]
                ["play_order_performance"]["unknown"]["played"], 1)
            for call in tracker._save_individual_card_stats.call_args_list:
                self.assertNotIn("play_position", call.args[1])

    def test_sanitized_card_filename_collisions_remain_distinct(self):
        cards = {
            1: SimpleNamespace(name="A/B", cmc=1),
            2: SimpleNamespace(name="AB", cmc=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            tracker._save_individual_card_stats("A/B", {
                "wins": 1, "deck_archetype": "aggro"})
            tracker._save_individual_card_stats("AB", {
                "losses": 1, "deck_archetype": "control"})
            self.assertEqual(len(tracker._dirty_individual_card_files), 2)
            self.assertTrue(tracker._flush_auxiliary_stats())

            payloads = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (Path(directory) / "cards").glob("*.json")
            ]
            self.assertEqual({payload["name"] for payload in payloads},
                             {"A/B", "AB"})
            self.assertEqual(tracker.get_card_stats(1)["wins"], 1)
            self.assertEqual(tracker.get_card_stats(2)["losses"], 1)

    def test_environment_maps_p1_start_when_p2_wins(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        tracker = SimpleNamespace(
            record_game=Mock(return_value=True), base_path="unused",
            identify_archetype=Mock(
                side_effect=lambda deck: "aggro" if deck == [1]
                else "control"))
        env.has_stats_tracker = True
        env.stats_tracker = tracker
        env.has_card_memory = True
        env.card_memory = object()
        env._record_cards_to_memory = Mock()
        env.card_db = {}
        env.original_p1_deck = [1]
        env.original_p2_deck = [2]
        env.current_deck_name_p1 = "P1 Deck"
        env.current_deck_name_p2 = "P2 Deck"
        env._game_result_recorded = False
        env._game_logged = True
        env.game_state = SimpleNamespace(
            p1={"life": 0, "lost_game": True},
            p2={"life": 20}, agent_is_p1=True, turn=4, max_turns=30,
            terminal_reason="life_total", cards_played={0: [], 1: []},
            play_history={0: {}, 1: {}}, opening_hands={},
            draw_history={}, mulligan_data={})

        manifest = SimpleNamespace(persist=Mock())
        with patch("Playersim.card_support.get_manifest",
                   return_value=manifest):
            env.ensure_game_result_recorded()
        kwargs = tracker.record_game.call_args.kwargs
        self.assertEqual(kwargs["winner_deck_name"], "P2 Deck")
        self.assertEqual(kwargs["play_order"], {"first_player": "loser"})
        memory_calls = env._record_cards_to_memory.call_args_list
        self.assertEqual(memory_calls[0].args[0], [2])
        self.assertEqual(memory_calls[0].args[4:6], ("control", "aggro"))
        self.assertEqual(memory_calls[1].args[4:6], ("aggro", "control"))

    def test_environment_canonicalizes_opening_and_draw_telemetry(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        printings = {101: 1, 102: 2, 103: 3, 104: 4}
        gs = SimpleNamespace(
            opening_hands={"p1": [101, 102], "p2": [103]},
            draw_history={"p1": {3: [104]}, "p2": {2: [103]}},
            mulligan_data={"p1": 0, "p2": 1},
            canonical_card_id=lambda card_id: printings.get(
                card_id, card_id),
        )

        openings, draws, mulligans = env._stats_telemetry_mapped(gs, True)

        self.assertEqual(openings, {"winner": [1, 2], "loser": [3]})
        self.assertEqual(
            draws, {"winner": {3: [4]}, "loser": {2: [3]}})
        self.assertEqual(mulligans, {"winner": 0, "loser": 1})


if __name__ == "__main__":
    unittest.main()
