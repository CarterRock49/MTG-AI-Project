"""Correctness and persistence regressions for CardMemory."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_rejected_registration_cannot_update_the_existing_identity(self):
        from Playersim.environment import AlphaZeroMTGEnv

        with tempfile.TemporaryDirectory() as directory:
            memory = self._memory(directory)
            self.assertTrue(memory.register_card(13, "Original Identity"))
            self.assertFalse(memory.register_card(13, "Conflicting Identity"))

            env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
            env.has_card_memory = True
            env.card_memory = memory
            env.game_state = SimpleNamespace(
                canonical_card_id=lambda card_id: card_id,
                _safe_get_card=lambda _card_id: SimpleNamespace(
                    name="Conflicting Identity", cmc=2,
                    card_types=["creature"], colors=[0] * 5))

            accepted = env._record_cards_to_memory(
                [13], {0: [13]}, "midrange", {}, {}, {0: {3: [13]}},
                is_win=True, player_idx=0)

            self.assertFalse(accepted)
            stats = memory.get_card_stats(13)
            self.assertEqual(stats["name"], "Original Identity")
            self.assertEqual(stats["games_played"], 0)

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
                [1], cards_played, "aggro", {}, {}, play_history,
                is_win=True, player_idx=0)
            env._record_cards_to_memory(
                [2], cards_played, "control", {}, {}, play_history,
                is_win=False, player_idx=1)

            for card_id in (1, 2):
                stats = env.card_memory.get_card_stats(card_id)
                self.assertEqual(stats["turn_played"], {"3": 1})
                self.assertEqual(
                    stats["mana_curve_performance"]["on_curve"]["played"],
                    1)

    def test_environment_canonicalizes_telemetry_without_registering_opponent(self):
        from Playersim.environment import AlphaZeroMTGEnv

        with tempfile.TemporaryDirectory() as directory:
            cards = {
                1: SimpleNamespace(
                    name="Runtime Three Drop", cmc=3,
                    card_types=["creature"], colors=[0] * 5),
                2: SimpleNamespace(
                    name="Opponent Card", cmc=2,
                    card_types=["creature"], colors=[0] * 5),
            }
            canonical_ids = {101: 1, 202: 2}
            env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
            env.has_card_memory = True
            env.card_memory = self._memory(directory)
            env.game_state = SimpleNamespace(
                canonical_card_id=lambda card_id: canonical_ids.get(
                    card_id, card_id),
                _safe_get_card=lambda card_id: cards.get(card_id))

            update = env.card_memory.update_card_performance
            with patch.object(
                    env.card_memory, "update_card_performance",
                    wraps=update) as update_spy:
                env._record_cards_to_memory(
                    [101], {0: [101]}, "midrange",
                    {"p1": [101]}, {"p1": {1: [101]}},
                    {0: {5: [101]}}, is_win=True, player_idx=0)

            self.assertEqual(update_spy.call_count, 1)
            payload = update_spy.call_args.args[1]
            self.assertNotIn("game_duration", payload)
            self.assertNotIn("opponent_archetype", payload)
            stats = env.card_memory.get_card_stats(1)
            self.assertEqual(stats["times_played"], 1)
            self.assertEqual(stats["times_drawn"], 1)
            self.assertEqual(stats["in_opening_hand"], 1)
            self.assertEqual(stats["turn_played"], {"3": 1})
            self.assertEqual(env.card_memory.get_card_stats(2), {})

    def test_card_memory_only_terminal_recording_is_at_most_once(self):
        from Playersim.environment import AlphaZeroMTGEnv

        with tempfile.TemporaryDirectory() as directory:
            cards = {
                1: SimpleNamespace(
                    name="Winner Card", cmc=2,
                    card_types=["creature"], colors=[0] * 5),
                2: SimpleNamespace(
                    name="Loser Card", cmc=2,
                    card_types=["creature"], colors=[0] * 5),
            }
            env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
            env.has_stats_tracker = False
            env.stats_tracker = None
            env.has_card_memory = True
            env.card_memory = self._memory(directory)
            env.card_db = cards
            env.original_p1_deck = [1]
            env.original_p2_deck = [2]
            env.current_deck_name_p1 = "Winner"
            env.current_deck_name_p2 = "Loser"
            env._game_result_recorded = False
            env._game_logged = False
            env.deck_stats_path = str(Path(directory) / "analytics")
            env._fidelity_agg = {
                "games_recorded": 0,
                "unimplemented_action": 0,
                "unparsed_mana": 0,
                "unparsed_modal": 0,
                "unparsed_effects": 0,
                "unparsed_cards": {},
            }
            env.game_state = SimpleNamespace(
                p1={"life": 20},
                p2={"life": 0, "lost_game": True},
                agent_is_p1=True,
                turn=5,
                max_turns=30,
                terminal_reason="life_total",
                cards_played={0: [], 1: []},
                opening_hands={},
                draw_history={},
                play_history={},
                canonical_card_id=lambda card_id: card_id,
                _safe_get_card=lambda card_id: cards.get(card_id),
                fidelity_counters={},
            )

            env.ensure_game_result_recorded()
            env.ensure_game_result_recorded()

            self.assertTrue(env._game_result_recorded)
            self.assertTrue(env._card_memory_result_record_attempted)
            self.assertTrue(env._card_memory_result_record_accepted)
            self.assertFalse(env._game_result_recording_failed)
            self.assertEqual(
                env.card_memory.get_card_stats(1)["games_played"], 1)
            self.assertEqual(
                env.card_memory.get_card_stats(2)["games_played"], 1)
            log_path = Path(env.deck_stats_path) / "game_log.jsonl"
            records = [json.loads(line) for line in
                       log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["analytics_recording"], {
                "deck_stats": "disabled", "card_memory": "accepted"})
            self.assertEqual(records[0]["analytics_persistence_at_record"], {
                "deck_stats": "disabled", "card_memory": "deferred"})

    def test_p2_result_inference_stays_agent_relative(self):
        from Playersim.environment import AlphaZeroMTGEnv

        for p1_lost, expected in ((True, "win"), (False, "loss")):
            with self.subTest(p1_lost=p1_lost):
                env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
                env.has_stats_tracker = False
                env.stats_tracker = None
                env.has_card_memory = False
                env.card_memory = None
                env._game_result_recorded = False
                env._game_logged = True
                env.game_state = SimpleNamespace(
                    p1={"life": 0 if p1_lost else 20,
                        "lost_game": p1_lost},
                    p2={"life": 20 if p1_lost else 0,
                        "lost_game": not p1_lost},
                    agent_is_p1=False, turn=4, max_turns=30,
                    terminal_reason="life_total")

                env.ensure_game_result_recorded()

                self.assertEqual(env._game_result, expected)

    def test_failed_provenance_write_retries_without_recounting(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        env.has_stats_tracker = False
        env.stats_tracker = None
        env.has_card_memory = False
        env.card_memory = None
        env._game_result_recorded = False
        env._game_logged = False
        env.game_state = SimpleNamespace(
            p1={"life": 20}, p2={"life": 0, "lost_game": True},
            agent_is_p1=True, turn=4, max_turns=30,
            terminal_reason="life_total")
        env._write_stats_artifacts = Mock(
            side_effect=[OSError("disk unavailable"), None])

        env.ensure_game_result_recorded()
        self.assertTrue(env._game_result_recorded)
        self.assertFalse(env._game_logged)
        self.assertTrue(env._game_artifact_write_failed)

        env.ensure_game_result_recorded()

        self.assertEqual(env._write_stats_artifacts.call_count, 2)
        self.assertTrue(env._game_logged)
        self.assertFalse(env._game_artifact_write_failed)

    def test_report_failure_reuses_durable_game_row_on_retry(self):
        from Playersim.environment import AlphaZeroMTGEnv

        with tempfile.TemporaryDirectory() as directory:
            env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
            env.stats_tracker = None
            env.deck_stats_path = directory
            env._game_result = "win"
            env._game_logged = False
            env._stats_artifact_states = {}
            env._current_stats_artifact_entry = None
            env._fidelity_agg = env._empty_fidelity_aggregate()
            env.game_state = SimpleNamespace(
                p1={"life": 20}, p2={"life": 0},
                agent_is_p1=True, turn=4, max_turns=30,
                terminal_reason="life_total",
                fidelity_counters={"unparsed_effects": 1})

            original_write = env._atomic_write_json
            write_attempts = 0

            def fail_first_report(path, payload):
                nonlocal write_attempts
                write_attempts += 1
                if write_attempts == 1:
                    raise OSError("report unavailable")
                return original_write(path, payload)

            with patch.object(
                    env, "_atomic_write_json",
                    side_effect=fail_first_report):
                self.assertFalse(env._ensure_stats_artifacts_written())
                self.assertTrue(env._ensure_stats_artifacts_written())

            records = [
                json.loads(line) for line in
                (Path(directory) / "game_log.jsonl").read_text(
                    encoding="utf-8").splitlines()]
            report = json.loads(
                (Path(directory) / "fidelity_report.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0]["game_id"])
            self.assertEqual(report["games_recorded"], 1)
            self.assertEqual(report["unparsed_effects"], 1)

    def test_shared_artifact_path_refreshes_an_external_append(self):
        from Playersim.environment import AlphaZeroMTGEnv

        def artifact_env(directory, issue_count):
            env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
            env.stats_tracker = None
            env.deck_stats_path = directory
            env._game_result = "win"
            env._stats_artifact_states = {}
            env._current_stats_artifact_entry = None
            env._fidelity_agg = env._empty_fidelity_aggregate()
            env.game_state = SimpleNamespace(
                p1={"life": 20}, p2={"life": 0},
                agent_is_p1=True, turn=4, max_turns=30,
                terminal_reason="life_total",
                fidelity_counters={
                    "unparsed_effects": issue_count})
            return env

        with tempfile.TemporaryDirectory() as directory:
            first = artifact_env(directory, 1)
            second = artifact_env(directory, 10)
            first._write_stats_artifacts()
            second._write_stats_artifacts()

            first._current_stats_artifact_entry = None
            first.game_state.fidelity_counters = {
                "unparsed_effects": 100}
            first._write_stats_artifacts()

            records = [
                json.loads(line) for line in
                (Path(directory) / "game_log.jsonl").read_text(
                    encoding="utf-8").splitlines()]
            report = json.loads(
                (Path(directory) / "fidelity_report.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(len(records), 3)
            self.assertEqual(report["games_recorded"], 3)
            self.assertEqual(report["unparsed_effects"], 111)

    def test_shared_artifact_path_serializes_concurrent_writers(self):
        from Playersim.environment import AlphaZeroMTGEnv

        with tempfile.TemporaryDirectory() as directory:
            environments = []
            for issue_count in range(1, 9):
                env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
                env.stats_tracker = None
                env.deck_stats_path = directory
                env._game_result = "win"
                env._stats_artifact_states = {}
                env._current_stats_artifact_entry = None
                env._fidelity_agg = env._empty_fidelity_aggregate()
                env.game_state = SimpleNamespace(
                    p1={"life": 20}, p2={"life": 0},
                    agent_is_p1=True, turn=4, max_turns=30,
                    terminal_reason="life_total",
                    fidelity_counters={
                        "unparsed_effects": issue_count})
                environments.append(env)

            barrier = threading.Barrier(len(environments))
            failures = []

            def write_artifact(env):
                try:
                    barrier.wait()
                    env._write_stats_artifacts()
                except Exception as error:
                    failures.append(error)

            workers = [
                threading.Thread(target=write_artifact, args=(env,))
                for env in environments]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            records = [
                json.loads(line) for line in
                (Path(directory) / "game_log.jsonl").read_text(
                    encoding="utf-8").splitlines()]
            report = json.loads(
                (Path(directory) / "fidelity_report.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(failures, [])
            self.assertEqual(len(records), 8)
            self.assertEqual(report["games_recorded"], 8)
            self.assertEqual(report["unparsed_effects"], 36)

    def test_reset_boundary_refuses_to_drop_pending_provenance(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        env._game_result_recorded = True
        env._game_logged = False
        env._ensure_stats_artifacts_written = Mock(return_value=False)

        with self.assertRaisesRegex(RuntimeError, "provenance is pending"):
            env._finalize_previous_episode_artifacts()

        env._ensure_stats_artifacts_written.return_value = True
        env._finalize_previous_episode_artifacts()

    def test_close_surfaces_permanently_pending_provenance(self):
        from Playersim.environment import AlphaZeroMTGEnv

        env = AlphaZeroMTGEnv.__new__(AlphaZeroMTGEnv)
        env.game_state = SimpleNamespace(p1={}, p2={})
        env.ensure_game_result_recorded = Mock()
        env.stats_tracker = None
        env.card_memory = None
        env.strategy_memory = None
        env._game_result_recorded = True
        env._game_result_recording_failed = False
        env._game_logged = False
        env._ensure_stats_artifacts_written = Mock(return_value=False)

        with self.assertRaisesRegex(
                RuntimeError, "terminal provenance remains pending"):
            env.close()

        env._ensure_stats_artifacts_written.assert_called_once_with()

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
