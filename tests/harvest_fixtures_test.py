"""Focused tests for the deterministic sample-deck harvest CLI.

Run from the repository root with::

    python tests/harvest_fixtures_test.py
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import harvest_fixtures as harvest  # noqa: E402


def _write_gzip_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(data, handle)


def _card_aggregate(name: str, *, wins: int, losses: int, draws: int) -> dict:
    games = wins + losses + draws
    rate = (wins + 0.5 * draws) / games if games else 0.0
    stage = {
        "early": {"games": 0, "wins": 0, "draws": 0},
        "mid": {"games": games, "wins": wins, "draws": draws},
        "late": {"games": 0, "wins": 0, "draws": 0},
    }
    state = {
        "ahead": {"games": 0, "wins": 0, "draws": 0},
        "parity": {"games": games, "wins": wins, "draws": draws},
        "behind": {"games": 0, "wins": 0, "draws": 0},
    }
    return {
        "name": name,
        "games_played": games,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "usage_count": 0,
        "win_rate": rate,
        "archetypes": {
            "midrange": {"games": games, "wins": wins, "draws": draws}
        },
        "by_game_stage": stage,
        "by_game_state": state,
    }


def _memory_entry(card_id: int, name: str, *, wins=0, losses=0, draws=0,
                  times_played=0) -> dict:
    games = wins + losses + draws
    return {
        "id": card_id,
        "name": name,
        "games_played": games,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": (wins + 0.5 * draws) / games if games else 0.0,
        "times_drawn": 0,
        "times_played": times_played,
        "archetype_performance": {
            "midrange": {
                "games": games,
                "wins": wins,
                "losses": losses,
                "draws": draws,
            }
        },
        "performance_trend": [
            0.5 if draws else (1.0 if wins else 0.0)
        ] if games else [],
    }


def _write_valid_artifact_fixture(output: Path, version: str) -> dict:
    """Write the smallest internally consistent one-game harvest artifact set."""
    record = {
        "schema_version": 1,
        "ts": 1.0,
        "result": "win",
        "terminal_reason": "life_total",
        "turn_count": 4,
        "p1_deck": "DimirMidrange",
        "p2_deck": "DimirSelf",
        "agent_is_p1": True,
        "agent_version": version,
        "fidelity": {
            "unimplemented_action": 0,
            "unparsed_mana": 0,
            "unparsed_modal": 0,
            "unparsed_effects": 0,
            "unparsed_cards": [],
        },
    }
    (output / "game_log.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8")
    (output / "fidelity_report.json").write_text(
        json.dumps({
            "games_recorded": 1,
            "agent_version": version,
            "unimplemented_action": 0,
            "unparsed_mana": 0,
            "unparsed_modal": 0,
            "unparsed_effects": 0,
            "unparsed_cards": {},
        }),
        encoding="utf-8",
    )
    manifest = {
        "Low Count": {"count": 1, "severity": "crash", "reasons": {}},
        "High Count": {"count": 5, "severity": "partial", "reasons": {}},
    }
    (output / "card_support_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    decks = {
        "DimirMidrange": {
            "name": "DimirMidrange", "deck_id": "winner", "archetype": "midrange",
            "card_list": [{"id": 1, "name": "Winner Card", "count": 60}],
            "games": 1, "wins": 1, "losses": 0, "draws": 0,
            "total_turns": 4, "avg_game_length": 4.0, "win_rate": 1.0,
        },
        "DimirSelf": {
            "name": "DimirSelf", "deck_id": "loser", "archetype": "midrange",
            "card_list": [{"id": 2, "name": "Loser Card", "count": 60}],
            "games": 1, "wins": 0, "losses": 1, "draws": 0,
            "total_turns": 4, "avg_game_length": 4.0, "win_rate": 0.0,
        },
    }
    for filename, data in (("winner", decks["DimirMidrange"]),
                           ("loser", decks["DimirSelf"])):
        _write_gzip_json(output / "decks" / f"{filename}.json.gz", data)
    _write_gzip_json(
        output / "cards" / "winner_card.json.gz",
        _card_aggregate("Winner Card", wins=1, losses=0, draws=0))
    _write_gzip_json(
        output / "cards" / "loser_card.json.gz",
        _card_aggregate("Loser Card", wins=0, losses=1, draws=0))
    _write_gzip_json(output / "meta" / "meta_data.json.gz", {
        "version": "test",
        "total_games": 1,
        "draws": 0,
        "archetypes": {
            "midrange": {
                "games": 2, "wins": 1, "losses": 1, "draws": 0,
                "win_rate": 0.5,
            }
        },
        "matchups": {
            "midrange_vs_midrange": {
                "wins": 1, "losses": 1, "draws": 0, "win_rate": 0.5,
            }
        },
        "cards": {
            "Winner Card": {
                "games": 1, "wins": 1, "losses": 0, "draws": 0,
                "usage_count": 60, "win_rate": 1.0, "play_rate": 0.5,
                "archetypes": {"midrange": 1},
            },
            "Loser Card": {
                "games": 1, "wins": 0, "losses": 1, "draws": 0,
                "usage_count": 60, "win_rate": 0.0, "play_rate": 0.5,
                "archetypes": {"midrange": 1},
            },
        },
    })
    _write_gzip_json(output / "card_memory" / "all_cards.json.gz", {
        "cards": {
            "1": _memory_entry(1, "Winner Card", wins=1),
            "2": _memory_entry(2, "Loser Card", losses=1),
        },
        "name_to_id": {"Winner Card": "1", "Loser Card": "2"},
        "id_to_name": {"1": "Winner Card", "2": "Loser Card"},
        "last_updated": 1.0,
    })
    return record


class HarvestFixturesTest(unittest.TestCase):
    def test_meta_rates_use_two_deck_seats_per_match(self):
        from Playersim.card import Card
        from Playersim.deck_stats_tracker import DeckStatsTracker, STATS_VERSION

        cards = {}
        for card_id, name in enumerate(("Shared", "Winner Only", "Loser Only")):
            card = Card({"name": name, "type_line": "Artifact", "oracle_text": ""})
            card.card_id = card_id
            cards[card_id] = card

        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = DeckStatsTracker(storage_path=temp_dir, card_db=cards)
            for _ in range(5):
                self.assertTrue(tracker.update_meta_with_game_result(
                    [0, 1], [0, 2], "aggro", "control", {}, 5))
            meta = tracker._load_meta_data()
            self.assertEqual(meta["version"], STATS_VERSION)
            self.assertEqual(meta["total_games"], 5)
            self.assertEqual(meta["cards"]["Shared"]["games"], 10)
            self.assertEqual(meta["cards"]["Shared"]["play_rate"], 1.0)
            self.assertEqual(meta["cards"]["Winner Only"]["play_rate"], 0.5)
            self.assertEqual(meta["cards"]["Loser Only"]["play_rate"], 0.5)
            self.assertTrue(all(
                0 <= card_data["play_rate"] <= 1
                for card_data in meta["cards"].values()))
            snapshot = tracker.get_meta_snapshot()
            self.assertEqual(snapshot["archetype_distribution"], {
                "aggro": 0.5, "control": 0.5,
            })
            self.assertIsNone(snapshot["last_updated"])

    def test_project_loader_finds_the_audited_eight_decks(self):
        decks, card_db = harvest.load_sample_decks()
        self.assertEqual(
            [deck["name"] for deck in decks],
            list(harvest.EXPECTED_SAMPLE_DECKS),
        )
        self.assertTrue(card_db)
        self.assertTrue(all(len(deck["cards"]) >= 60 for deck in decks))

    def test_valid_action_choice_is_seeded_and_avoids_concede(self):
        mask = [False] * 20
        mask[4] = mask[12] = mask[17] = True
        first = harvest.choose_fixture_action(mask, random.Random(1234))
        second = harvest.choose_fixture_action(mask, random.Random(1234))
        self.assertEqual(first, second)
        self.assertIn(first, (4, 17))

        concede_only = [False] * 20
        concede_only[12] = True
        self.assertEqual(
            harvest.choose_fixture_action(concede_only, random.Random(1)), 12
        )

    def test_matchup_rotation_and_pair_adapter_are_deterministic(self):
        decks = [{"name": name} for name in harvest.EXPECTED_SAMPLE_DECKS]
        pairings = [harvest.scheduled_matchup(decks, i, 42) for i in range(8)]
        self.assertEqual(
            [p1["name"] for p1, _ in pairings],
            list(harvest.EXPECTED_SAMPLE_DECKS),
        )
        self.assertTrue(all(p1 is not p2 for p1, p2 in pairings))

        forced = harvest._ScheduledDeckPair(*pairings[0])
        self.assertIs(forced[1], pairings[0][0])
        self.assertIs(forced[0], pairings[0][1])
        with self.assertRaises(IndexError):
            forced[2]

        class WaitingEnvironment:
            game_state = type(
                "State",
                (),
                {"turn": 3, "phase": 7, "priority_player": None, "stack": []},
            )()

        waiting_mask = np.zeros(480, dtype=bool)
        waiting_mask[harvest.NO_OP_ACTION] = True
        self.assertEqual(
            harvest._wait_state_signature(WaitingEnvironment(), waiting_mask),
            (3, 7, None, 0),
        )
        waiting_mask[11] = True
        self.assertIsNone(
            harvest._wait_state_signature(WaitingEnvironment(), waiting_mask)
        )
        signature_a = (3, 7, None, 0)
        signature_b = (3, 8, None, 0)
        previous, count = harvest._advance_wait_counter(None, 0, signature_a)
        previous, count = harvest._advance_wait_counter(previous, count, signature_b)
        self.assertEqual((previous, count), (signature_b, 1))
        previous, count = harvest._advance_wait_counter(previous, count, signature_b)
        self.assertEqual((previous, count), (signature_b, 2))

    def test_output_is_fresh_and_artifact_contract_is_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            output = harvest.prepare_output_directory(Path(temp) / "run")
            version = "fixture-test"
            record = _write_valid_artifact_fixture(output, version)

            records, fidelity, loaded_manifest = harvest._validate_artifacts(
                output, 1, version
            )
            self.assertEqual(records, [record])
            self.assertEqual(fidelity["games_recorded"], 1)
            self.assertEqual(
                [name for name, _ in harvest.rank_manifest_entries(loaded_manifest)],
                ["High Count", "Low Count"],
            )
            bad_record = dict(record, result="invalid_limit")
            (output / "game_log.jsonl").write_text(
                json.dumps(bad_record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not a completed result"):
                harvest._validate_artifacts(output, 1, version)
            with self.assertRaisesRegex(RuntimeError, "must be empty"):
                harvest.prepare_output_directory(output)

    def test_artifact_validation_rejects_corrupt_compressed_json(self):
        corrupt_paths = (
            Path("decks/winner.json.gz"),
            Path("cards/winner_card.json.gz"),
            Path("meta/meta_data.json.gz"),
            Path("card_memory/all_cards.json.gz"),
        )
        for relative_path in corrupt_paths:
            with self.subTest(path=str(relative_path)), tempfile.TemporaryDirectory() as temp:
                output = harvest.prepare_output_directory(Path(temp) / "run")
                _write_valid_artifact_fixture(output, "fixture-test")
                (output / relative_path).write_bytes(b"not-gzip-json")
                with self.assertRaisesRegex(RuntimeError, "not valid gzip JSON"):
                    harvest._validate_artifacts(output, 1, "fixture-test")

    def test_artifact_validation_rejects_cross_file_count_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            output = harvest.prepare_output_directory(Path(temp) / "run")
            _write_valid_artifact_fixture(output, "fixture-test")
            bad_card = _card_aggregate(
                "Winner Card", wins=2, losses=0, draws=0)
            _write_gzip_json(
                output / "cards" / "winner_card.json.gz", bad_card)
            with self.assertRaisesRegex(RuntimeError, "does not match deck aggregates"):
                harvest._validate_artifacts(output, 1, "fixture-test")

    def test_artifact_validation_rejects_card_memory_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            output = harvest.prepare_output_directory(Path(temp) / "run")
            _write_valid_artifact_fixture(output, "fixture-test")
            memory_path = output / "card_memory" / "all_cards.json.gz"
            with gzip.open(memory_path, "rt", encoding="utf-8") as handle:
                memory = json.load(handle)
            memory["cards"]["1"].update({
                "games_played": 0, "wins": 0, "win_rate": 0.0,
            })
            _write_gzip_json(memory_path, memory)
            with self.assertRaisesRegex(RuntimeError, "does not match card aggregates"):
                harvest._validate_artifacts(output, 1, "fixture-test")

    def test_run_harvest_rejects_mask_valid_execution_failures(self):
        class FakeState:
            _consecutive_no_ops = 0
            stack = []
            turn = 1
            phase = 1
            priority_player = None

        class FailingEnvironment:
            failure_info = {}

            def __init__(self, decks, card_db, **kwargs):
                self.decks = decks
                self.game_state = FakeState()
                self._game_result_recorded = False
                self._game_result = None

            def set_agent_version(self, version):
                self.agent_version = version

            def reset(self, seed=None):
                p1, p2 = self.decks._pair
                self.current_deck_name_p1 = p1["name"]
                self.current_deck_name_p2 = p2["name"]
                self.game_state = FakeState()
                return {}, {}

            def action_mask(self):
                mask = np.zeros(480, dtype=bool)
                mask[11] = True
                return mask

            def step(self, action):
                return {}, 0.0, False, False, dict(self.failure_info)

            def close(self):
                return None

        decks = [{"name": name, "cards": [0] * 60}
                 for name in harvest.EXPECTED_SAMPLE_DECKS]
        failure_cases = (
            {"execution_failed": True, "error_message": "agent failed"},
            {
                "execution_failed": True,
                "opponent_execution_failed": True,
                "error_message": "opponent failed",
            },
        )
        for failure_info in failure_cases:
            with self.subTest(info=failure_info), tempfile.TemporaryDirectory() as temp:
                FailingEnvironment.failure_info = failure_info
                output = Path(temp) / "run"
                with mock.patch.object(
                        harvest, "load_sample_decks", return_value=(decks, {})), \
                        mock.patch(
                            "Playersim.card_support.reset_manifest_for_tests"), \
                        mock.patch(
                            "Playersim.environment.AlphaZeroMTGEnv",
                            FailingEnvironment):
                    with self.assertRaisesRegex(RuntimeError, "mask-valid"):
                        harvest.run_harvest(1, 17, output, max_steps=2)
                self.assertFalse((output / "harvest_run.json").exists())

    def test_summary_is_compact_and_manifest_ranked(self):
        records = [{"result": "loss"}, {"result": "win"}]
        fidelity = {
            "unimplemented_action": 0,
            "unparsed_mana": 0,
            "unparsed_modal": 1,
            "unparsed_effects": 0,
        }
        manifest = {
            "Second": {"count": 2, "severity": "partial", "reasons": {"b": 2}},
            "First": {"count": 7, "severity": "unparsed", "reasons": {"a": 7}},
        }
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            harvest.print_summary(Path("out"), 9, records, fidelity, manifest)
        text = stream.getvalue()
        self.assertIn("games=2 seed=9", text)
        self.assertLess(text.index("First"), text.index("Second"))
        self.assertIn("fidelity_issues=1", text)

    def test_cli_arguments(self):
        args = harvest.build_parser().parse_args(
            ["--games", "3", "--seed", "99", "--max-steps", "77",
             "--output", "somewhere"]
        )
        self.assertEqual((args.games, args.seed, args.max_steps), (3, 99, 77))
        self.assertEqual(args.output, Path("somewhere"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
