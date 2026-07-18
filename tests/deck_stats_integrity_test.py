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
    GameStage,
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

    def test_record_game_separates_acceptance_from_retryable_flush_failure(self):
        cards = {
            1: SimpleNamespace(name="Retry Winner", cmc=1),
            2: SimpleNamespace(name="Retry Loser", cmc=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = DeckStatsTracker(
                directory, card_db=cards, use_compression=False,
                persistence_interval_games=1)
            tracker.identify_archetype = lambda _deck: "midrange"
            tracker.update_meta_with_game_result = Mock(return_value=True)

            with patch.object(tracker, "save", return_value=False):
                self.assertTrue(tracker.record_game(
                    [1], [2], cards, 4,
                    cards_played={0: [1], 1: [2]}))

            self.assertFalse(tracker._last_record_flush_succeeded)
            self.assertEqual(tracker._games_since_persistence, 1)
            self.assertEqual(len(tracker.batch_updates), 2)
            self.assertEqual(len(tracker._dirty_individual_card_files), 2)

            self.assertTrue(tracker.save_updates_sync())
            self.assertEqual(tracker._games_since_persistence, 0)
            self.assertEqual(tracker.batch_updates, {})
            self.assertEqual(tracker._dirty_individual_card_files, set())

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

    def test_record_game_uses_supplied_archetypes_without_reclassification(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory)
            tracker.identify_archetype = Mock(
                side_effect=AssertionError("unexpected classification"))
            tracker.update_meta_with_game_result = Mock(return_value=True)
            tracker._update_deck_stats = Mock(return_value=True)
            tracker._update_card_stats = Mock(return_value=True)
            tracker.save_updates_sync = Mock(return_value=True)

            self.assertTrue(tracker.record_game(
                [1], [2], {}, 6,
                winner_archetype="aggro",
                loser_archetype="control"))

            tracker.identify_archetype.assert_not_called()
            meta_kwargs = tracker.update_meta_with_game_result.call_args.kwargs
            self.assertEqual(meta_kwargs["winner_archetype"], "aggro")
            self.assertEqual(meta_kwargs["loser_archetype"], "control")
            deck_calls = tracker._update_deck_stats.call_args_list
            self.assertEqual(deck_calls[0].kwargs["archetype"], "aggro")
            self.assertEqual(deck_calls[1].kwargs["archetype"], "control")

    def test_supplied_archetype_replaces_existing_deck_classification(self):
        cards = {
            1: SimpleNamespace(name="Winner Card", cmc=1),
            2: SimpleNamespace(name="Loser Card", cmc=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            tracker.update_meta_with_game_result = Mock(return_value=True)
            self.assertTrue(tracker.record_game(
                [1], [2], cards, 4,
                winner_archetype="aggro", loser_archetype="control"))
            self.assertTrue(tracker.record_game(
                [1], [2], cards, 4,
                winner_archetype="tempo", loser_archetype="ramp"))

            winner = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([1]))
            loser = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([2]))
            self.assertEqual(winner["archetype"], "tempo")
            self.assertEqual(loser["archetype"], "ramp")
            winner_card = tracker.get_card_stats(1)
            self.assertEqual(winner_card["archetypes"]["aggro"]["games"], 1)
            self.assertEqual(winner_card["archetypes"]["tempo"]["games"], 1)

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
            self.assertEqual(
                loser["card_performance"]["2"]
                ["performance_by_turn"]["3"]["losses"],
                1)

    def test_legacy_turn_bucket_repairs_missing_loss_counter(self):
        cards = {2: SimpleNamespace(name="Legacy Turn Card", cmc=3)}
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            card_perf = tracker._card_performance_template(
                "Legacy Turn Card")
            card_perf["performance_by_turn"]["3"] = {
                "played": 1, "wins": 1, "draws": 0}
            deck_stats = {"card_performance": {"2": card_perf}}

            self.assertTrue(tracker._update_deck_card_performance(
                deck_stats,
                [{"id": 2, "name": "Legacy Turn Card"}],
                [2], [], {}, {6: [2]}, False,
                GameStage.MID, GamePosition.BEHIND, "loss"))

            turn_stats = deck_stats["card_performance"]["2"] \
                ["performance_by_turn"]["3"]
            self.assertEqual(turn_stats["played"], 2)
            self.assertEqual(turn_stats["losses"], 1)

    def test_real_play_turn_survives_missing_mana_value(self):
        cards = {
            1: SimpleNamespace(name="Variable Cost Winner", cmc=None),
            2: SimpleNamespace(name="Variable Cost Loser", cmc=None),
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
                play_order={"first_player": "winner"}))

            winner = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([1]))
            card_stats = winner["card_performance"]["1"]
            self.assertEqual(
                card_stats["performance_by_turn"]["3"]["played"], 1)
            self.assertEqual(
                sum(bucket["games"] for bucket in
                    card_stats["play_curve_stats"].values()),
                0)

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

    def test_individual_card_file_persists_exact_draw_union(self):
        cards = {1: SimpleNamespace(name="Telemetry Card", cmc=2)}
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            base_update = {
                "id": 1,
                "game_stage": "mid",
                "game_state": "parity",
                "deck_archetype": "midrange",
            }
            updates = [
                {"wins": 1, "was_drawn": False,
                 "in_opening_hand": True},
                {"losses": 1, "was_drawn": True,
                 "in_opening_hand": False},
                {"draws": 1, "was_drawn": False,
                 "in_opening_hand": False},
                {"draws": 1, "was_drawn": True,
                 "in_opening_hand": True},
            ]
            for update in updates:
                self.assertTrue(tracker._save_individual_card_stats(
                    "Telemetry Card", {**base_update, **update}))

            self.assertTrue(tracker._flush_auxiliary_stats())
            card_path = (
                Path(directory)
                / tracker._card_stats_file("Telemetry Card", 1))
            payload = json.loads(card_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["games_played"], 4)
            self.assertEqual(payload["games_drawn"], 3)
            self.assertEqual(payload["games_not_drawn"], 1)
            self.assertEqual(payload["games_in_opening_hand"], 2)
            self.assertEqual(payload["wins_when_drawn"], 1.5)
            self.assertEqual(payload["wins_when_not_drawn"], 0.5)
            self.assertEqual(payload["wins_when_in_opening_hand"], 1.5)
            self.assertEqual(payload["drawn_win_rate"], 0.5)
            self.assertEqual(payload["not_drawn_win_rate"], 0.5)
            self.assertEqual(payload["opening_hand_win_rate"], 0.75)

    def test_individual_card_draw_telemetry_migrates_missing_fields(self):
        cards = {1: SimpleNamespace(name="Legacy Telemetry", cmc=2)}
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            card_file = tracker._card_stats_file("Legacy Telemetry", 1)
            self.assertTrue(tracker.save(card_file, {
                "id": 1,
                "name": "Legacy Telemetry",
                "games_played": 4,
                "wins": 2,
                "losses": 2,
                "draws": 0,
                "usage_count": 1,
                "win_rate": 0.5,
                "archetypes": {},
                "by_game_stage": {},
                "by_game_state": {},
            }))
            tracker._individual_card_cache.clear()

            self.assertTrue(tracker._save_individual_card_stats(
                "Legacy Telemetry", {
                    "id": 1,
                    "losses": 1,
                    "was_drawn": False,
                    "in_opening_hand": False,
                    "game_stage": "late",
                    "game_state": "behind",
                    "deck_archetype": "control",
                }))
            migrated = tracker._individual_card_cache[card_file]

            self.assertEqual(migrated["games_played"], 5)
            self.assertEqual(migrated["games_drawn"], 0)
            self.assertEqual(migrated["games_not_drawn"], 1)
            self.assertEqual(migrated["games_in_opening_hand"], 0)
            self.assertEqual(migrated["drawn_win_rate"], 0.0)
            self.assertEqual(migrated["not_drawn_win_rate"], 0.0)
            self.assertEqual(migrated["opening_hand_win_rate"], 0.0)

    def test_same_name_card_ids_keep_distinct_global_aggregates(self):
        cards = {
            10: SimpleNamespace(name="Shared Name", cmc=1),
            11: SimpleNamespace(name="Shared Name", cmc=1),
            20: SimpleNamespace(name="Other Card", cmc=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            tracker.update_meta_with_game_result = Mock(return_value=True)
            self.assertTrue(tracker.record_game(
                [10], [20], cards, 4,
                cards_played={0: [10], 1: [20]}))
            self.assertTrue(tracker.record_game(
                [20], [11], cards, 4,
                cards_played={0: [20], 1: [11]}))

            first_path = tracker._card_stats_file("Shared Name", 10)
            second_path = tracker._card_stats_file("Shared Name", 11)
            self.assertNotEqual(first_path, second_path)
            first = tracker.get_card_stats(10)
            second = tracker.get_card_stats(11)
            self.assertEqual(first["id"], 10)
            self.assertEqual(first["games_played"], 1)
            self.assertEqual(first["wins"], 1)
            self.assertEqual(first["losses"], 0)
            self.assertEqual(second["id"], 11)
            self.assertEqual(second["games_played"], 1)
            self.assertEqual(second["wins"], 0)
            self.assertEqual(second["losses"], 1)

    def test_ambiguous_legacy_card_file_is_not_migrated_to_either_id(self):
        cards = {
            10: SimpleNamespace(name="Shared Name", cmc=1),
            11: SimpleNamespace(name="Shared Name", cmc=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            self.assertTrue(tracker.save("cards/Shared_Name.json", {
                "name": "Shared Name", "games_played": 99,
                "wins": 99, "losses": 0, "draws": 0,
                "usage_count": 99, "win_rate": 1.0,
            }))
            self.assertTrue(tracker.save("decks/first.json", {
                "card_list": [{"id": 10, "name": "Shared Name"}],
                "card_performance": {"10": {
                    "games_played": 2, "wins": 2, "losses": 0,
                    "draws": 0, "usage_count": 1,
                }},
            }))
            self.assertTrue(tracker.save("decks/second.json", {
                "card_list": [{"id": 11, "name": "Shared Name"}],
                "card_performance": {"11": {
                    "games_played": 3, "wins": 0, "losses": 3,
                    "draws": 0, "usage_count": 2,
                }},
            }))

            first = tracker.get_card_stats(10)
            second = tracker.get_card_stats(11)
            self.assertEqual((first["games_played"], first["wins"]), (2, 2))
            self.assertEqual(
                (second["games_played"], second["losses"]), (3, 3))

    def test_individual_card_file_is_loaded_only_once_across_readers(self):
        cards = {1: SimpleNamespace(name="Cached Card", cmc=2)}
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            payload = {
                "name": "Cached Card", "games_played": 4,
                "wins": 3, "losses": 1, "draws": 0,
                "usage_count": 2, "win_rate": 0.75,
                "archetypes": {}, "by_game_stage": {},
                "by_game_state": {},
            }
            self.assertTrue(tracker.save(
                "cards/Cached_Card.json", payload))
            tracker._individual_card_cache.clear()

            with patch.object(
                    tracker, "load", wraps=tracker.load) as tracked_load:
                self.assertEqual(tracker.get_card_stats(1)["wins"], 3)
                self.assertEqual(tracker.get_card_stats(1)["wins"], 3)
                self.assertEqual(
                    tracker._get_card_metrics(1)["games_played"], 4)

            self.assertEqual(tracked_load.call_count, 1)
            canonical_file = tracker._card_stats_file("Cached Card", 1)
            self.assertTrue(tracker.exists(canonical_file))
            self.assertEqual(
                tracker._individual_card_cache[canonical_file]["id"], 1)

    def test_failed_derived_card_save_stays_dirty_for_retry(self):
        cards = {1: SimpleNamespace(name="Derived Card", cmc=2)}
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            self.assertTrue(tracker.save("decks/source.json", {
                "card_list": [{"id": 1, "name": "Derived Card"}],
                "card_performance": {"1": {
                    "games_played": 4, "wins": 3, "losses": 1,
                    "draws": 0, "usage_count": 2,
                }},
            }))
            card_file = tracker._card_stats_file("Derived Card", 1)

            with patch.object(tracker, "save", return_value=False):
                self.assertEqual(tracker.get_card_stats(1)["wins"], 3)

            self.assertIn(card_file, tracker._dirty_individual_card_files)
            self.assertFalse(tracker.exists(card_file))
            self.assertTrue(tracker._flush_auxiliary_stats())
            self.assertNotIn(card_file, tracker._dirty_individual_card_files)
            self.assertTrue(tracker.exists(card_file))

    def test_game_updates_migrate_and_remove_legacy_name_card_stats(self):
        cards = {
            1: SimpleNamespace(name="Winner Card", cmc=1),
            2: SimpleNamespace(name="Loser Card", cmc=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            tracker = self._tracker(directory, cards)
            tracker.update_meta_with_game_result = Mock(return_value=True)
            self.assertTrue(tracker.record_game(
                [1], [2], cards, 4,
                cards_played={0: [1], 1: [2]}))

            winner_id = tracker.get_deck_fingerprint([1])
            winner = tracker.get_deck_stats(winner_id)
            loser = tracker.get_deck_stats(
                tracker.get_deck_fingerprint([2]))
            self.assertNotIn("card_performance_by_name", winner)
            self.assertNotIn("card_performance_by_name", loser)
            legacy = winner["card_performance"].pop("1")
            winner["card_performance_by_name"] = {
                "Winner Card": legacy}
            tracker._replace_deck_stats(winner_id, winner)

            self.assertTrue(tracker.record_game(
                [1], [2], cards, 4,
                cards_played={0: [1], 1: [2]}))
            migrated = tracker.get_deck_stats(winner_id)
            self.assertNotIn("card_performance_by_name", migrated)
            self.assertEqual(
                migrated["card_performance"]["1"]["games_played"], 2)

    def test_ambiguous_legacy_name_stats_are_not_copied_to_multiple_ids(self):
        stats = {
            "card_performance": {"1": {"wins": 3}},
            "card_performance_by_name": {
                "Shared Name": {"games_played": 7, "wins": 7}},
        }
        composition = [
            {"id": 1, "name": "Shared Name"},
            {"id": 2, "name": "Shared Name"},
        ]

        with patch(
                "Playersim.deck_stats_tracker.logging.warning") as warning:
            DeckStatsTracker._migrate_name_card_performance(
                stats, composition)

        self.assertEqual(stats["card_performance"], {"1": {"wins": 3}})
        self.assertNotIn("card_performance_by_name", stats)
        warning.assert_called_once()

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
            draw_history={}, mulligan_data={},
            deck_archetypes={0: "aggro", 1: "control"})

        manifest = SimpleNamespace(persist=Mock())
        with patch("Playersim.card_support.get_manifest",
                   return_value=manifest):
            env.ensure_game_result_recorded()
        kwargs = tracker.record_game.call_args.kwargs
        self.assertEqual(kwargs["winner_deck_name"], "P2 Deck")
        self.assertEqual(kwargs["play_order"], {"first_player": "loser"})
        self.assertEqual(kwargs["winner_archetype"], "control")
        self.assertEqual(kwargs["loser_archetype"], "aggro")
        tracker.identify_archetype.assert_not_called()
        memory_calls = {
            call.kwargs["player_idx"]: call.kwargs
            for call in env._record_cards_to_memory.call_args_list
        }
        self.assertEqual(memory_calls[0]["player_deck"], [1])
        self.assertEqual(memory_calls[0]["player_archetype"], "aggro")
        self.assertFalse(memory_calls[0]["is_win"])
        self.assertEqual(memory_calls[1]["player_deck"], [2])
        self.assertEqual(memory_calls[1]["player_archetype"], "control")
        self.assertTrue(memory_calls[1]["is_win"])

    def test_failed_tracker_result_is_diagnostic_and_at_most_once(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        tracker = SimpleNamespace(
            record_game=Mock(return_value=False), base_path="unused",
            identify_archetype=Mock(return_value="unknown"))
        env.has_stats_tracker = True
        env.stats_tracker = tracker
        env.has_card_memory = False
        env.card_memory = None
        env.card_db = {}
        env.original_p1_deck = [1]
        env.original_p2_deck = [2]
        env.current_deck_name_p1 = "P1 Deck"
        env.current_deck_name_p2 = "P2 Deck"
        env._game_result_recorded = False
        env._game_logged = True
        env.game_state = SimpleNamespace(
            p1={"life": 20},
            p2={"life": 0, "lost_game": True},
            agent_is_p1=True, turn=4, max_turns=30,
            terminal_reason="life_total", cards_played={0: [], 1: []},
            play_history={0: {}, 1: {}}, opening_hands={},
            draw_history={}, mulligan_data={})

        manifest = SimpleNamespace(persist=Mock())
        with patch("Playersim.card_support.get_manifest",
                   return_value=manifest):
            env.ensure_game_result_recorded()
            env.ensure_game_result_recorded()

        tracker.record_game.assert_called_once()
        self.assertTrue(env._game_result_recorded)
        self.assertTrue(env._stats_result_record_attempted)
        self.assertFalse(env._stats_result_record_accepted)
        self.assertTrue(env._game_result_recording_failed)

    def test_flush_failure_does_not_relabel_an_accepted_game(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        tracker = SimpleNamespace(
            record_game=Mock(return_value=True), base_path="unused",
            _last_record_flush_succeeded=False,
            identify_archetype=Mock(return_value="midrange"))
        env.has_stats_tracker = True
        env.stats_tracker = tracker
        env.has_card_memory = False
        env.card_memory = None
        env.card_db = {}
        env.original_p1_deck = [1]
        env.original_p2_deck = [2]
        env.current_deck_name_p1 = "P1 Deck"
        env.current_deck_name_p2 = "P2 Deck"
        env._game_result_recorded = False
        env._game_logged = True
        env.game_state = SimpleNamespace(
            p1={"life": 20},
            p2={"life": 0, "lost_game": True},
            agent_is_p1=True, turn=4, max_turns=30,
            terminal_reason="life_total", cards_played={0: [], 1: []},
            play_history={0: {}, 1: {}}, opening_hands={},
            draw_history={}, mulligan_data={})

        manifest = SimpleNamespace(persist=Mock())
        with patch("Playersim.card_support.get_manifest",
                   return_value=manifest):
            env.ensure_game_result_recorded()

        self.assertTrue(env._stats_result_record_accepted)
        self.assertFalse(env._stats_result_flush_succeeded)
        self.assertFalse(env._game_result_recording_failed)
        self.assertTrue(env._game_result_flush_failed)

    def test_card_memory_only_archetype_has_a_stable_bucket(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        env.stats_tracker = None

        self.assertEqual(env._stats_archetype_label([1, 2]), "midrange")

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

    def test_midgame_position_classifies_winner_from_turn_snapshots(self):
        # Round 7.95: record_game never received game_state, so every game
        # landed in the tracker's "parity" default (0 of 960 card files had
        # an ahead/behind bucket after round-7.94). The env now classifies
        # the winner's position at the middle turn of the game.
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)

        # Snowball: P1 built a big lead by turn 6 of 12 and won.
        env._life_totals_by_turn = {
            2: (20, 20), 4: (20, 16), 6: (19, 8), 10: (18, 3)}
        self.assertEqual(env._derive_midgame_position(True, 12), "ahead")
        # The same game from the loser-as-winner perspective is a comeback.
        self.assertEqual(env._derive_midgame_position(False, 12), "behind")

        # Close mid-game life means parity regardless of the final result.
        env._life_totals_by_turn = {3: (17, 15), 6: (12, 14), 9: (4, 18)}
        self.assertEqual(env._derive_midgame_position(True, 12), "parity")

        # A margin exactly at the threshold counts as a real lead.
        margin = AlphaZeroMTGEnv.MIDGAME_POSITION_LIFE_MARGIN
        env._life_totals_by_turn = {5: (20, 20 - margin)}
        self.assertEqual(env._derive_midgame_position(True, 10), "ahead")

        # No snapshot at or before the midpoint falls back to the earliest
        # recorded turn; no history at all stays parity.
        env._life_totals_by_turn = {9: (20, 5)}
        self.assertEqual(env._derive_midgame_position(True, 10), "ahead")
        env._life_totals_by_turn = {}
        self.assertEqual(env._derive_midgame_position(True, 10), "parity")


if __name__ == "__main__":
    unittest.main()
