"""Headless contracts for the dependency-free DeckStats workbench.

Run directly with::

    python tests/deck_stats_viewer_test.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from DeckStats_Viewer.MTG_Statistics_Viewer import create_server
from DeckStats_Viewer.viewer_data import ViewerRepository


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, allow_nan=False, sort_keys=True)


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            if isinstance(row, str):
                handle.write(row + "\n")
            else:
                handle.write(json.dumps(
                    row, allow_nan=False, sort_keys=True) + "\n")


class DeckStatsViewerTest(unittest.TestCase):
    RUN_ID = "viewer-contract-run"
    FIRST_CHECKPOINT = "a" * 64
    SECOND_CHECKPOINT = "b" * 64

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self._build_artifacts()
        self.repository = ViewerRepository(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _episode(self, *, evaluation: int, case_index: int,
                 agent_is_p1: bool, replay=None) -> dict:
        checkpoint = (
            self.FIRST_CHECKPOINT if evaluation == 0
            else self.SECOND_CHECKPOINT
        )
        timestep = 100 if evaluation == 0 else 200
        seed = 7000 + evaluation
        first, second = "Alpha Deck", "Beta Deck"
        if case_index % 2:
            first, second = second, first
        episode = {
            "case_index": case_index,
            "case": {
                "seed": seed,
                "p1_deck": first,
                "p2_deck": second,
                "agent_is_p1": agent_is_p1,
                "opponent_profile": "scripted",
            },
            "resolved_case": {
                "seed": seed,
                "p1_deck": first,
                "p2_deck": second,
                "agent_is_p1": agent_is_p1,
                "opponent_profile": "scripted",
            },
            "game_result": "win" if case_index % 2 == 0 else "loss",
            "raw_game_result": (
                "win_by_life" if case_index % 2 == 0 else "loss"
            ),
            "terminal_reason": "life_total",
            "reward": 2.5 - case_index,
            "length": 10 + evaluation * 10 + case_index,
            "raw_marker": {
                "evaluation": evaluation,
                "case": case_index,
                "nested": ["must", "survive"],
            },
            "evaluation_timestep": timestep,
            "checkpoint_sha256": checkpoint,
        }
        if replay is not None:
            episode["replay_path"] = replay
        return episode

    def _build_artifacts(self) -> None:
        model_root = self.root / "models" / self.RUN_ID
        log_root = self.root / "logs" / self.RUN_ID
        evaluation_root = log_root / "evaluation"
        stats_root = (
            log_root / "environment_data" / "eval" / "env_0"
            / "deck_stats"
        )

        _write_json(model_root / "training_run.json", {
            "schema_version": 3,
            "kind": "playersim_training_run",
            "run_id": self.RUN_ID,
            "status": "complete",
            "phase": "complete",
            "project_version": "viewer-test",
            "timestamps": {
                "started_at": "2026-07-16T12:00:00+00:00",
                "updated_at": "2026-07-16T12:05:00+00:00",
                "finished_at": "2026-07-16T12:05:00+00:00",
            },
            "metrics": {
                "duration_seconds": 300.0,
                "final_timesteps": 200,
            },
            "resolved": {
                "training_adaptive_decision_history": False,
                "evaluation_adaptive_decision_history": False,
                "strategy_memory": "disabled",
            },
            "raw_manifest_marker": {"preserved": True},
        })

        replay_path = evaluation_root / "replays" / "game-0.json"
        replay_payload = {
            "version": 3,
            "seed": 7000,
            "actions": [
                {"action": 13, "context": {"card": "Test Land"}},
                {"action": 11, "context": {}},
            ],
        }
        _write_json(replay_path, replay_payload)
        debug_payload = {
            "schema_version": 1,
            "card_catalog": {
                "schema_version": 1,
                "entries": [
                    {"runtime_id": 901, "canonical_id": 101,
                     "name": "Shared Name", "owner": "p1",
                     "type_line": "Instant"},
                    {"runtime_id": 902, "canonical_id": 102,
                     "name": "Opponent Card", "owner": "p2",
                     "type_line": "Creature — Test"},
                ],
                "recorded_entries": 2,
                "omitted_entries": 0,
            },
            "replay": {
                "version": 3,
                "seed": 7001,
                "actions": [{"action": 20, "context": {"card_id": 901},
                             "trace_sequence": 0}],
            },
            "trace": [{
                "sequence": 0,
                "actor": "learned",
                "action": 20,
                "label": "PLAY_SPELL (0)",
                "context": {"card_id": 901, "target_id": 902},
                "pre": {
                    "turn": 2, "phase_name": "MAIN_PRECOMBAT",
                    "priority_player": "p1", "stack": [],
                    "p1": {
                        "life": 20, "poison_counters": 0,
                        "tapped_permanents": [], "damage_marked": [],
                        "permanent_counters": [],
                        "zones": {
                            "library": {"count": 52, "cards": []},
                            "hand": {"count": 1, "cards": [901]},
                            "battlefield": {"count": 0, "cards": []},
                            "graveyard": {"count": 0, "cards": []},
                            "exile": {"count": 0, "cards": []},
                        },
                    },
                    "p2": {
                        "life": 20, "poison_counters": 0,
                        "tapped_permanents": [], "damage_marked": [],
                        "permanent_counters": [],
                        "zones": {
                            "library": {"count": 53, "cards": []},
                            "hand": {"count": 0, "cards": []},
                            "battlefield": {"count": 1, "cards": [902]},
                            "graveyard": {"count": 0, "cards": []},
                            "exile": {"count": 0, "cards": []},
                        },
                    },
                },
                "post": {
                    "turn": 2, "phase_name": "MAIN_PRECOMBAT",
                    "priority_player": "p1",
                    "stack": [{"kind": "spell", "source_id": 901,
                               "controller": "p1"}],
                    "p1": {
                        "life": 20, "poison_counters": 0,
                        "tapped_permanents": [], "damage_marked": [],
                        "permanent_counters": [],
                        "zones": {
                            "library": {"count": 52, "cards": []},
                            "hand": {"count": 0, "cards": []},
                            "battlefield": {"count": 0, "cards": []},
                            "graveyard": {"count": 0, "cards": []},
                            "exile": {"count": 0, "cards": []},
                        },
                    },
                    "p2": {
                        "life": 18, "poison_counters": 0,
                        "tapped_permanents": [902],
                        "damage_marked": [{"card_id": 902, "amount": 2}],
                        "permanent_counters": [{
                            "card_id": 902,
                            "counters": {"+1/+1": 1},
                        }],
                        "zones": {
                            "library": {"count": 53, "cards": []},
                            "hand": {"count": 0, "cards": []},
                            "battlefield": {"count": 1, "cards": [902]},
                            "graveyard": {"count": 0, "cards": []},
                            "exile": {"count": 0, "cards": []},
                        },
                    },
                },
                "evaluator": {
                    "schema_version": 1,
                    "capture_scope": "pre-and-during-atomic-action",
                    "causal_attribution": False,
                    "deduplicated_events": 1,
                    "dropped_events": 0,
                    "events": [{
                        "runtime_card_id": 901,
                        "canonical_card_id": 101,
                        "card_name": "Shared Name",
                        "context": "play",
                        "perspective": "p1",
                        "components": {
                            "base": 2.0,
                            "context": 2.0,
                            "history": 0.0,
                            "stats": 0.0,
                        },
                        "history": {
                            "source": "none",
                            "overall_games": 0,
                            "archetype": "aggro",
                            "archetype_games": 0,
                            "reliable": False,
                            "fallback_reason": "adaptive history disabled",
                        },
                        "adjustments": {
                            "weighted_score": 1.7,
                            "game_stage": "mid",
                            "stage_multiplier": 1.0,
                            "position": "even",
                            "position_multiplier": 1.0,
                            "aggression_level": 0.5,
                            "aggression_multiplier": 1.0,
                            "pre_clamp": 1.7,
                        },
                        "final_score": 1.7,
                        "flags": {
                            "invalid": False,
                            "fallback": True,
                            "exception": False,
                            "clamped": False,
                        },
                    }],
                },
            }, {
                "sequence": 1,
                "actor": "opponent",
                "action": 11,
                "label": "PASS_PRIORITY",
                "context": {},
                "pre": {"turn": 2, "phase_name": "MAIN_PRECOMBAT",
                        "priority_player": "p2", "stack": []},
                "post": {"turn": 2, "phase_name": "MAIN_PRECOMBAT",
                         "priority_player": "p1", "stack": []},
            }],
            "terminal": {
                "game_result": "win",
                "terminal_reason": "life_total",
            },
            "evaluator": {
                "summary": {
                    "calls": 2,
                    "recorded": 1,
                    "deduplicated": 1,
                    "dropped": 0,
                    "exceptions": 0,
                    "fallbacks": 1,
                    "cache_hits": 0,
                    "cache_misses": 2,
                    "pending": 0,
                },
                "unattached": [],
            },
            "capture": {
                "trace": {"recorded_events": 2, "dropped_events": 0,
                          "serialized_bytes": 1024,
                          "sanitization_omissions": 0,
                          "serialization_errors": 0},
                "replay": {"recorded_events": 1, "dropped_events": 0,
                           "serialized_bytes": 128,
                           "sanitization_omissions": 0,
                           "serialization_errors": 0},
                "terminal": {"serialized_bytes": 2048,
                             "sanitization_omissions": 0,
                             "serialization_errors": 0},
                "errors": [],
            },
        }
        debug_sidecar = (
            evaluation_root / "games" / "200" / "case_000.json.gz")
        _write_json(debug_sidecar, debug_payload)
        debug_sidecar_bytes = debug_sidecar.read_bytes()
        debug_sidecar_sha256 = hashlib.sha256(debug_sidecar_bytes).hexdigest()
        evaluations = []
        raw_episodes = []
        for evaluation_index, (timestep, checkpoint) in enumerate((
                (100, self.FIRST_CHECKPOINT),
                (200, self.SECOND_CHECKPOINT))):
            episodes = [
                self._episode(
                    evaluation=evaluation_index,
                    case_index=0,
                    agent_is_p1=True,
                    replay="replays/game-0.json"
                    if evaluation_index == 0 else None,
                ),
                self._episode(
                    evaluation=evaluation_index,
                    case_index=1,
                    agent_is_p1=False,
                ),
            ]
            if evaluation_index == 1:
                episodes[0].pop("replay_path", None)
                episodes[0]["debug_path"] = "games/200/case_000.json.gz"
                episodes[0]["debug_sha256"] = debug_sidecar_sha256
                episodes[0]["debug_size_bytes"] = len(debug_sidecar_bytes)
                episodes[0]["trace_event_count"] = 2
                episodes[0]["replay_action_count"] = 1
                # Full verified debug must outrank this terminal-only fallback.
                episodes[0]["policy_state"] = {"legacy_fallback": True}
                episodes[1]["policy_state"] = {"terminal_only": True}
            raw_episodes.extend(episodes)
            evaluations.append({
                "timesteps": timestep,
                "checkpoint_sha256": checkpoint,
                "completed_at": f"2026-07-16T12:0{evaluation_index}:00Z",
                "qualified": evaluation_index == 1,
                "promoted": evaluation_index == 1,
                "summary": {
                    "episodes": 2,
                    "decisive_wins": 1,
                    "decisive_losses": 1,
                    "timeouts": 0,
                    "qualification_score": 0.5,
                },
                "episodes": episodes,
            })
        self.raw_episodes = raw_episodes
        _write_json(evaluation_root / "evaluations.json", {
            "schema_version": 3,
            "kind": "playersim_fixed_checkpoint_evaluations",
            "best_timestep": 200,
            "best_candidate_timestep": 200,
            "schedule_sha256": "c" * 64,
            "evaluations": evaluations,
            "raw_history_marker": ["preserved", 17],
        })

        log_rows = []
        timestamp = 1_800_000_000.0
        for evaluation_index, evaluation in enumerate(evaluations):
            for episode_index, episode in enumerate(evaluation["episodes"]):
                case = episode["case"]
                log_rows.append({
                    "schema_version": 2,
                    "game_id": f"game-{evaluation_index}-{episode_index}",
                    "ts": timestamp,
                    "evaluation_timestep": evaluation["timesteps"],
                    "evaluation_checkpoint_sha256": evaluation[
                        "checkpoint_sha256"
                    ],
                    "matchup_episode_index": episode_index,
                    "episode_seed": case["seed"],
                    "p1_deck": case["p1_deck"],
                    "p2_deck": case["p2_deck"],
                    "agent_is_p1": case["agent_is_p1"],
                    "agent_deck": "Alpha Deck",
                    "opponent_deck": "Beta Deck",
                    "opponent_profile": "scripted",
                    "result": episode["game_result"],
                    "terminal_reason": episode["terminal_reason"],
                    "turn_count": 5 + episode_index,
                    "fidelity": {
                        "unimplemented_action": evaluation_index,
                        "unparsed_cards": [],
                    },
                    "game_log_marker": f"joined-{evaluation_index}-{episode_index}",
                })
                timestamp += 1.0
        # A malformed nonempty row must be indexed and diagnosed without
        # hiding the four valid evaluation games around it.
        _write_jsonl(
            stats_root / "game_log.jsonl",
            [*log_rows, '{"broken":'],
        )

        _write_json(stats_root / "fidelity_report.json", {
            "games_recorded": 4,
            "unimplemented_action": 2,
            "unparsed_cards": {},
        })
        _write_json(stats_root / "card_support_manifest.json.gz", {
            "schema_version": 1,
            "supported_cards": 2,
        })
        # Meta's match count is deliberately half of the sum of deck-seat
        # appearances. The viewer must preserve this canonical value.
        _write_json(stats_root / "meta" / "meta_data.json.gz", {
            "version": "3.4.0",
            "total_games": 4,
            "archetypes": {
                "aggro": {"games": 4, "wins": 2, "losses": 2},
            },
            "cards": {},
            "raw_meta_marker": "gzip",
        })
        _write_json(stats_root / "decks" / "alpha.json", {
            "deck_id": "deck-alpha",
            "name": "Alpha Deck",
            "games": 4,
            "wins": 2,
            "losses": 2,
            "card_list": [{"id": 101, "name": "Alpha Card", "count": 4}],
            "raw_deck_marker": "plain",
        })
        _write_json(stats_root / "decks" / "beta.json.gz", {
            "deck_id": "deck-beta",
            "name": "Beta Deck",
            "games": 4,
            "wins": 2,
            "losses": 2,
            "card_list": [{"id": 102, "name": "Beta Card", "count": 4}],
            "raw_deck_marker": "gzip",
        })
        _write_json(stats_root / "cards" / "alpha.json", {
            "id": 101,
            "name": "Shared Name",
            "games_played": 4,
            "wins": 3,
            "losses": 1,
            "draws": 0,
            "usage_count": 2,
            "raw_card_marker": "plain",
        })
        _write_json(stats_root / "cards" / "beta.json.gz", {
            "id": 102,
            "name": "Shared Name",
            "games_played": 4,
            "wins": 1,
            "losses": 2,
            "draws": 1,
            "usage_count": 3,
            "raw_card_marker": "gzip",
        })
        memory_root = stats_root.parent / "card_memory"
        strategy_pickle_bytes = (
            b"not-a-trusted-pickle-and-must-never-be-opened"
        )
        _write_json(memory_root / "all_cards.json.gz", {
            "schema_version": 2,
            "last_updated": 1_800_000_010.0,
            "name_to_id": {"Memory Only": "999"},
            "id_to_name": {
                "101": "Shared Name",
                "102": "Shared Name",
                "999": "Memory Only",
            },
            "ambiguous_names": ["Shared Name"],
            "cards": {
                "101": {
                    "id": 101,
                    "name": "Shared Name",
                    "games_played": 4,
                    "wins": 3,
                    "losses": 1,
                    "draws": 0,
                    "win_rate": 0.75,
                    "times_drawn": 2,
                    "times_played": 2,
                    "in_opening_hand": 1,
                    "wins_in_opening_hand": 1,
                    "draws_in_opening_hand": 0,
                    "turn_played": {"2": 1, "3": 1},
                    "performance_by_turn": {
                        "2": {"played": 1, "wins": 1, "losses": 0, "draws": 0},
                        "3": {"played": 1, "wins": 1, "losses": 0, "draws": 0},
                    },
                    "mana_curve_performance": {
                        "on_curve": {"played": 1, "wins": 1, "draws": 0},
                        "below_curve": {"played": 0, "wins": 0, "draws": 0},
                        "above_curve": {"played": 1, "wins": 1, "draws": 0},
                    },
                    "archetype_performance": {
                        "aggro": {"games": 4, "wins": 3, "losses": 1, "draws": 0},
                    },
                    "synergy_partners": {
                        "102": {"games_together": 4, "wins_together": 3,
                                "draws_together": 0},
                    },
                    "effectiveness_rating": 0.81,
                    "performance_trend": [1.0, 0.0, 1.0, 1.0],
                    "meta_position": {},
                },
                "102": {
                    "id": 102,
                    "name": "Shared Name",
                    "games_played": 4,
                    "wins": 1,
                    "losses": 2,
                    "draws": 1,
                    "win_rate": 0.375,
                    "times_drawn": 3,
                    "times_played": 3,
                    "in_opening_hand": 1,
                    "wins_in_opening_hand": 0,
                    "draws_in_opening_hand": 1,
                    "turn_played": {"3": 3},
                    "performance_by_turn": {
                        "3": {"played": 3, "wins": 1, "losses": 1, "draws": 1},
                    },
                    "mana_curve_performance": {
                        "on_curve": {"played": 3, "wins": 1, "draws": 1},
                        "below_curve": {"played": 0, "wins": 0, "draws": 0},
                        "above_curve": {"played": 0, "wins": 0, "draws": 0},
                    },
                    "archetype_performance": {
                        "control": {"games": 4, "wins": 1, "losses": 2, "draws": 1},
                    },
                    "synergy_partners": {
                        "101": {"games_together": 4, "wins_together": 1,
                                "draws_together": 1},
                    },
                    "effectiveness_rating": 0.42,
                    "performance_trend": [0.0, 0.5, 0.0, 1.0],
                    "meta_position": {},
                },
                "999": {
                    "id": 999,
                    "name": "Memory Only",
                    "games_played": 1,
                    "wins": 0,
                    "losses": 1,
                    "draws": 0,
                    "win_rate": 0.0,
                    "times_drawn": 0,
                    "times_played": 0,
                    "in_opening_hand": 0,
                    "wins_in_opening_hand": 0,
                    "draws_in_opening_hand": 0,
                    "turn_played": {},
                    "performance_by_turn": {},
                    "mana_curve_performance": {
                        "on_curve": {"played": 0, "wins": 0, "draws": 0},
                        "below_curve": {"played": 0, "wins": 0, "draws": 0},
                        "above_curve": {"played": 0, "wins": 0, "draws": 0},
                    },
                    "archetype_performance": {
                        "unknown": {"games": 1, "wins": 0, "losses": 1,
                                    "draws": 0},
                    },
                    "synergy_partners": {},
                    "effectiveness_rating": 0.5,
                    "performance_trend": [0.0],
                    "meta_position": {},
                },
            },
        })
        _write_json(memory_root / "strategy_memory.json.gz", {
            "kind": "playersim.strategy_memory.diagnostics",
            "schema_version": 1,
            "source_pickle": {
                "size_bytes": len(strategy_pickle_bytes),
                "sha256": hashlib.sha256(
                    strategy_pickle_bytes).hexdigest(),
            },
            "source_memory_schema_version": 3,
            "logical_update": 17,
            "semantics": {
                "reward": "mean shaped transition reward",
                "positive_reward_rate": "shaped reward > 0",
            },
            "counts": {
                "patterns": 2,
                "pattern_evidence": 6,
                "pattern_actions": 3,
                "action_evidence": 5,
                "action_sequences": 4,
            },
            "aggregates": {
                "pattern_evidence_weighted_mean_reward": 0.25,
                "pattern_evidence_weighted_positive_reward_rate": 0.5,
                "action_evidence_weighted_mean_reward": 0.1,
                "action_evidence_weighted_positive_reward_rate": 0.4,
            },
            "limits": {"top_patterns": 20, "top_actions": 20},
            "truncation": {
                "top_patterns": {"total": 1, "returned": 1,
                                 "truncated": False},
                "top_actions": {"total": 1, "returned": 1,
                                "truncated": False},
            },
            "top_patterns": [{
                "pattern": ["main", "playable"],
                "count": 4,
                "mean_reward": 0.3,
                "positive_reward_rate": 0.5,
                "last_update": 17,
                "evidence": 4,
            }],
            "top_actions": [{
                "pattern": ["main", "playable"],
                "action_index": 20,
                "count": 3,
                "mean_reward": 0.2,
                "positive_reward_rate": 2 / 3,
                "last_update": 17,
                "evidence": 3,
            }],
        })
        (memory_root / "strategy_memory.pkl").write_bytes(
            strategy_pickle_bytes)
        broken = stats_root / "decks" / "broken.json"
        broken.write_text('{"not": valid json', encoding="utf-8")

        # A legacy root is a second source, ensuring run discovery does not
        # replace or conflate independent stats scopes.
        legacy = self.root / "deck_stats"
        _write_json(legacy / "meta" / "meta_data.json", {
            "total_games": 1,
            "legacy": True,
        })
        _write_jsonl(legacy / "game_log.jsonl", [{
            "game_id": "legacy-game",
            "result": "draw",
        }])
        _write_json(legacy / "fidelity_report.json", {
            "games_recorded": 1,
        })

    def _evaluation_source(self) -> dict:
        return next(
            source for source in self.repository.stats_sources()
            if source["kind"] == "evaluation"
        )

    def _evaluation_history_path(self) -> Path:
        return (self.root / "logs" / self.RUN_ID / "evaluation"
                / "evaluations.json")

    def _debug_sidecar_path(self) -> Path:
        return (self.root / "logs" / self.RUN_ID / "evaluation" / "games"
                / "200" / "case_000.json.gz")

    def test_plain_and_gzip_stats_preserve_the_canonical_meta_total(self):
        source = self._evaluation_source()
        bundle = self.repository.stats_bundle(source["id"])

        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["meta"]["total_games"], 4)
        self.assertEqual(bundle["meta"]["raw_meta_marker"], "gzip")
        self.assertEqual(len(bundle["decks"]), 2)
        self.assertEqual(
            {deck["raw_deck_marker"] for deck in bundle["decks"]},
            {"plain", "gzip"},
        )
        self.assertEqual(len(bundle["cards"]), 2)
        self.assertEqual(
            {(card["id"], card["raw_card_marker"])
             for card in bundle["cards"]},
            {(101, "plain"), (102, "gzip")},
        )
        self.assertEqual(
            bundle["card_support_manifest"]["supported_cards"], 2
        )
        self.assertTrue(any(name.endswith("beta.json.gz")
                            for name in bundle["deck_files"]))

    def test_card_memory_joins_only_by_canonical_id_and_reports_health(self):
        source = self._evaluation_source()
        self.assertTrue(source["has_card_memory"])
        self.assertTrue(source["card_memory_file"].endswith(
            "card_memory/all_cards.json.gz"))

        bundle = self.repository.stats_bundle(source["id"])
        summary = bundle["card_memory_summary"]
        self.assertEqual(summary["status"], "loaded")
        self.assertEqual(summary["schema_version"], 2)
        self.assertTrue(summary["contract_supported"])
        self.assertTrue(summary["contract_valid"])
        self.assertEqual(summary["last_updated"], 1_800_000_010.0)
        self.assertEqual(summary["card_count"], 3)
        self.assertEqual(summary["ambiguous_names"], ["Shared Name"])
        self.assertEqual(summary["decision_use"]["mode"], "recorded_only")
        self.assertFalse(summary["decision_use"]["enabled"])

        join = summary["join"]
        self.assertEqual(join["aggregate_card_count"], 2)
        self.assertEqual(join["aggregate_with_id_count"], 2)
        self.assertEqual(join["joined_card_ids"], ["101", "102"])
        self.assertEqual(join["joined_card_count"], 2)
        self.assertEqual(join["memory_without_aggregate_ids"], ["999"])
        self.assertEqual(join["aggregate_without_memory_ids"], [])
        self.assertEqual(join["field_mismatch_count"], 0)
        # The duplicate display name must never collapse the two identities.
        cards = bundle["card_memory"]["cards"]
        self.assertEqual(cards["101"]["effectiveness_rating"], 0.81)
        self.assertEqual(cards["102"]["effectiveness_rating"], 0.42)
        self.assertEqual(cards["101"]["name"], cards["102"]["name"])
        self.assertTrue(any(
            item.get("error") == "CardMemoryIntegrityNotice"
            for item in bundle["errors"]
        ))

    def test_unknown_card_memory_schema_is_loaded_raw_with_review_health(self):
        memory_path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "card_memory" / "all_cards.json.gz"
        )
        with gzip.open(memory_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["schema_version"] = 99
        _write_json(memory_path, payload)
        self.repository.refresh()

        bundle = self.repository.stats_bundle(self._evaluation_source()["id"])
        summary = bundle["card_memory_summary"]
        self.assertEqual(summary["status"], "loaded_unknown_schema")
        self.assertEqual(summary["health"], "review")
        self.assertFalse(summary["contract_supported"])
        self.assertFalse(summary["contract_valid"])
        self.assertEqual(bundle["card_memory"]["schema_version"], 99)
        self.assertTrue(any(
            item.get("error") == "UnknownCardMemorySchema"
            for item in bundle["errors"]
        ))

    def test_malformed_known_card_memory_cannot_report_clean(self):
        memory_path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "card_memory" / "all_cards.json.gz"
        )
        with gzip.open(memory_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["cards"]["101"]["name"] = ""
        payload["cards"]["101"]["effectiveness_rating"] = 4.0
        _write_json(memory_path, payload)
        self.repository.refresh()

        bundle = self.repository.stats_bundle(self._evaluation_source()["id"])
        summary = bundle["card_memory_summary"]
        self.assertTrue(summary["contract_supported"])
        self.assertFalse(summary["contract_valid"])
        self.assertEqual(summary["health"], "review")
        self.assertNotIn("101", summary["valid_card_ids"])
        self.assertGreater(summary["validation_issue_count"], 0)
        self.assertTrue(any(
            item.get("error") == "MalformedCardMemoryEntries"
            for item in bundle["errors"]
        ))

    def test_safe_strategy_diagnostics_are_loaded_without_unpickling(self):
        source = self._evaluation_source()
        self.assertTrue(source["has_strategy_memory_json"])
        self.assertTrue(source["has_unsafe_strategy_memory_pickle"])

        bundle = self.repository.stats_bundle(source["id"])
        summary = bundle["strategy_memory_summary"]
        self.assertEqual(summary["status"], "loaded_with_opaque_pickle")
        self.assertEqual(summary["health"], "clean")
        self.assertTrue(summary["contract_supported"])
        self.assertTrue(summary["contract_valid"])
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["logical_update"], 17)
        self.assertEqual(summary["configuration"]["mode"], "disabled")
        self.assertEqual(summary["counts"]["pattern_evidence"], 6)
        self.assertEqual(summary["top_pattern_count"], 1)
        self.assertEqual(summary["top_action_count"], 1)
        self.assertIn("not game win rate",
                      summary["positive_reward_rate_semantics"])
        self.assertFalse(summary["unsafe_pickle"]["loaded"])
        self.assertEqual(
            summary["unsafe_pickle"]["size_bytes"],
            summary["source_pickle"]["size_bytes"],
        )
        self.assertEqual(
            summary["unsafe_pickle"]["sha256"],
            summary["source_pickle"]["sha256"],
        )
        self.assertEqual(
            summary["source_pickle_verification"]["status"], "verified")
        self.assertTrue(
            summary["source_pickle_verification"]["verified"])
        self.assertEqual(
            bundle["strategy_memory"]["kind"],
            "playersim.strategy_memory.diagnostics",
        )
        self.assertFalse(any(
            item.get("error") in {
                "StrategyMemoryPickleOnly", "UnsafeStrategyMemoryPickle"
            }
            for item in bundle["errors"]
        ))

    def test_strategy_diagnostics_pickle_mismatch_is_stale_raw_only(self):
        pickle_path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "card_memory" / "strategy_memory.pkl"
        )
        pickle_path.write_bytes(pickle_path.read_bytes() + b"-changed")
        self.repository.refresh()

        bundle = self.repository.stats_bundle(self._evaluation_source()["id"])
        summary = bundle["strategy_memory_summary"]
        self.assertTrue(summary["contract_supported"])
        self.assertFalse(summary["contract_valid"])
        self.assertEqual(summary["health"], "review")
        self.assertEqual(
            summary["source_pickle_verification"]["status"], "mismatch")
        self.assertNotEqual(
            summary["source_pickle_verification"]["expected_sha256"],
            summary["source_pickle_verification"]["actual_sha256"],
        )
        self.assertTrue(any(
            item.get("error") == "StrategyMemorySourcePickleMismatch"
            for item in bundle["errors"]
        ))

    def test_legacy_strategy_json_without_pickle_marker_requires_review(self):
        path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "card_memory"
            / "strategy_memory.json.gz"
        )
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.pop("source_pickle")
        _write_json(path, payload)
        self.repository.refresh()

        bundle = self.repository.stats_bundle(self._evaluation_source()["id"])
        summary = bundle["strategy_memory_summary"]
        self.assertTrue(summary["contract_supported"])
        self.assertFalse(summary["contract_valid"])
        self.assertEqual(summary["health"], "review")
        self.assertEqual(
            summary["source_pickle_verification"]["status"],
            "missing_marker",
        )
        self.assertTrue(any(
            item.get("error")
            == "StrategyMemorySourcePickleMarkerMissing"
            for item in bundle["errors"]
        ))

    def test_malformed_known_strategy_diagnostics_cannot_report_clean(self):
        path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "card_memory"
            / "strategy_memory.json.gz"
        )
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["counts"].pop("patterns")
        payload["aggregates"][
            "action_evidence_weighted_positive_reward_rate"] = 2.0
        _write_json(path, payload)
        self.repository.refresh()

        bundle = self.repository.stats_bundle(self._evaluation_source()["id"])
        summary = bundle["strategy_memory_summary"]
        self.assertTrue(summary["contract_supported"])
        self.assertFalse(summary["contract_valid"])
        self.assertEqual(summary["health"], "review")
        self.assertGreaterEqual(summary["validation_issue_count"], 2)
        self.assertTrue(any(
            item.get("error") == "MalformedStrategyMemoryDiagnostics"
            for item in bundle["errors"]
        ))

    def test_unknown_strategy_schema_is_raw_only_contract(self):
        path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "card_memory"
            / "strategy_memory.json.gz"
        )
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["schema_version"] = 99
        _write_json(path, payload)
        self.repository.refresh()

        summary = self.repository.stats_bundle(
            self._evaluation_source()["id"])["strategy_memory_summary"]
        self.assertFalse(summary["contract_supported"])
        self.assertFalse(summary["contract_valid"])
        self.assertEqual(summary["health"], "review")

    def test_pickle_only_strategy_is_reported_but_never_opened(self):
        safe_path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "card_memory"
            / "strategy_memory.json.gz"
        )
        safe_path.unlink()
        self.repository.refresh()

        bundle = self.repository.stats_bundle(self._evaluation_source()["id"])
        summary = bundle["strategy_memory_summary"]
        self.assertEqual(summary["status"], "unsafe_pickle_only")
        self.assertEqual(summary["health"], "review")
        self.assertIsNone(bundle["strategy_memory"])
        self.assertFalse(summary["unsafe_pickle"]["loaded"])
        self.assertTrue(any(
            item.get("error") == "StrategyMemoryPickleOnly"
            for item in bundle["errors"]
        ))

    def test_model_manifest_and_log_history_are_one_merged_run(self):
        runs = self.repository.runs()

        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["run_id"], self.RUN_ID)
        self.assertTrue(run["has_manifest"])
        self.assertTrue(run["has_evaluation"])
        self.assertEqual(run["evaluation_points"], 2)
        self.assertEqual(run["evaluation_game_count"], 4)
        self.assertEqual(run["best_timestep"], 200)
        detail = self.repository.run_detail(self.RUN_ID)
        self.assertTrue(detail["manifest"]["raw_manifest_marker"]["preserved"])
        self.assertEqual(
            detail["evaluation_history"]["raw_history_marker"],
            ["preserved", 17],
        )
        self.assertIn(self._evaluation_source()["id"],
                      detail["stats_source_ids"])
        self.assertEqual(self.repository.overview()["run_count"], 1)
        self.assertEqual(self.repository.overview()["stats_source_count"], 2)

    def test_generated_test_artifacts_are_not_discovered_as_user_stats(self):
        synthetic = (
            self.root / "tests" / "test_artifacts" / "fake-run"
            / "deck_stats")
        _write_jsonl(synthetic / "game_log.jsonl", [{
            "game_id": "synthetic-test-only", "result": "win",
        }])
        _write_json(synthetic / "fidelity_report.json", {
            "games_recorded": 1,
        })

        overview = self.repository.refresh()
        self.assertEqual(overview["stats_source_count"], 2)
        self.assertIn("tests/test_artifacts",
                      overview["ignored_artifact_roots"])
        self.assertFalse(any(
            source["relative_path"].startswith("tests/test_artifacts/")
            for source in self.repository.stats_sources()
        ))

    def test_related_worker_scope_totals_are_visible_but_not_silently_merged(self):
        sibling = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_1" / "deck_stats")
        _write_jsonl(sibling / "game_log.jsonl", [
            {"game_id": "sibling-0", "result": "win"},
            {"game_id": "sibling-1", "result": "loss"},
        ])
        _write_json(sibling / "fidelity_report.json", {
            "games_recorded": 2,
        })
        self.repository.refresh()

        source = next(
            item for item in self.repository.stats_sources()
            if item["relative_path"].endswith("eval/env_0/deck_stats")
        )
        bundle = self.repository.stats_bundle(source["id"])
        related = bundle["related_sources"]
        self.assertEqual(related["source_count"], 2)
        self.assertEqual(related["total_game_count"], 7)
        self.assertEqual(related["aggregation"], "not_merged")
        self.assertEqual(bundle["game_count"], 5)

    def test_conflicting_checkpoint_never_relaxed_joins_game_log(self):
        path = (
            self.root / "logs" / self.RUN_ID / "environment_data"
            / "eval" / "env_0" / "deck_stats" / "game_log.jsonl")
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines[:4]]
        rows[0]["evaluation_checkpoint_sha256"] = self.SECOND_CHECKPOINT
        _write_jsonl(path, [*rows, lines[4]])
        self.repository.refresh()

        games = self.repository.evaluation_games(
            self.RUN_ID, include_debug=False)
        conflicted = games[0]
        self.assertNotIn("game_log", conflicted)
        self.assertIsNone(conflicted.get("game_id"))
        diagnostic = conflicted["game_log_join"]
        self.assertEqual(diagnostic["status"], "conflict")
        self.assertEqual(
            diagnostic["error"], "EvaluationGameLogJoinConflict")
        self.assertEqual(
            diagnostic["conflicts"][0]["mismatches"]
            ["checkpoint_sha256"]["evaluation"],
            self.FIRST_CHECKPOINT,
        )
        self.assertEqual(
            diagnostic["conflicts"][0]["mismatches"]
            ["checkpoint_sha256"]["game_log"],
            self.SECOND_CHECKPOINT,
        )
        self.assertEqual(
            [game["game_log_join"]["status"] for game in games[1:]],
            ["matched", "matched", "matched"],
        )

    def test_every_evaluation_episode_keeps_raw_fields_pair_and_replay(self):
        games = self.repository.evaluation_games(self.RUN_ID)

        self.assertEqual(len(games), 4)
        self.assertEqual(
            [(game["evaluation_index"], game["episode_index"])
             for game in games],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )
        for index, game in enumerate(games):
            raw = game["raw_episode"]
            self.assertEqual(raw["raw_marker"],
                             self.raw_episodes[index]["raw_marker"])
            self.assertEqual(game["game_log"]["game_log_marker"],
                             f"joined-{index // 2}-{index % 2}")
            self.assertEqual(game["game_id"],
                             f"game-{index // 2}-{index % 2}")
            self.assertEqual(game["pair_index"], 0)
            self.assertEqual(game["agent_deck"], "Alpha Deck")
            self.assertEqual(game["opponent_deck"], "Beta Deck")

        self.assertEqual(games[0]["agent_seat"], "p1")
        self.assertEqual(games[1]["agent_seat"], "p2")
        self.assertEqual(games[0]["replay"]["version"], 3)
        self.assertEqual(games[0]["replay"]["actions"][0]["action"], 13)
        self.assertTrue(games[0]["replay_available"])
        self.assertFalse(games[0]["trace_available"])
        self.assertFalse(games[0]["terminal_debug_available"])
        self.assertNotIn("replay", games[1])
        self.assertEqual(games[2]["debug"]["trace"][0]["action"], 20)
        self.assertEqual(len(games[2]["debug"]["trace"]), 2)
        arena_step = games[2]["debug"]["trace"][0]
        self.assertEqual(
            arena_step["pre"]["p2"]["zones"]["battlefield"]["cards"],
            [902],
        )
        self.assertEqual(
            arena_step["post"]["p2"]["tapped_permanents"], [902])
        self.assertEqual(
            arena_step["post"]["p2"]["damage_marked"],
            [{"amount": 2, "card_id": 902}],
        )
        self.assertEqual(
            arena_step["post"]["p2"]["permanent_counters"][0]
            ["counters"]["+1/+1"],
            1,
        )
        self.assertEqual(
            games[2]["debug"]["card_catalog"]["entries"][0]["name"],
            "Shared Name",
        )
        self.assertEqual(
            games[2]["debug_summary"]["trace_actor_counts"],
            {"learned": 1, "opponent": 1},
        )
        self.assertEqual(
            games[2]["debug_summary"]["capture_status"], "complete")
        self.assertEqual(games[2]["debug_summary"]["card_catalog_count"], 2)
        self.assertNotIn("legacy_fallback", games[2]["debug"])
        self.assertTrue(games[2]["trace_available"])
        self.assertTrue(games[2]["replay_available"])
        self.assertTrue(games[2]["terminal_debug_available"])
        self.assertTrue(games[2]["debug_artifact"]["verified"])
        self.assertEqual(
            games[2]["debug_artifact"]["actual_sha256"],
            games[2]["raw_episode"]["debug_sha256"],
        )
        evaluator = games[2]["debug"]["trace"][0]["evaluator"]
        self.assertFalse(evaluator["causal_attribution"])
        self.assertEqual(
            evaluator["events"][0]["history"]["source"], "none")
        self.assertEqual(
            games[2]["debug"]["evaluator"]["summary"][
                "fallbacks"], 1)
        self.assertEqual(
            games[2]["debug"]["replay"]["actions"][0]["action"], 20)
        self.assertNotIn("replay", games[3])
        self.assertFalse(games[3]["trace_available"])
        self.assertFalse(games[3]["replay_available"])
        self.assertTrue(games[3]["terminal_debug_available"])
        self.assertEqual(games[3]["debug"], {"terminal_only": True})

    def test_inline_trace_only_debug_is_not_terminal_debug(self):
        history_path = self._evaluation_history_path()
        history = json.loads(history_path.read_text(encoding="utf-8"))
        history["evaluations"][0]["episodes"][1]["debug"] = {
            "trace": [{"action": 77, "context": {"source": "inline"}}],
        }
        _write_json(history_path, history)
        self.repository.refresh()

        summary_game = self.repository.evaluation_games(
            self.RUN_ID, include_debug=False)[1]
        self.assertTrue(summary_game["trace_available"])
        self.assertFalse(summary_game["terminal_debug_available"])

        loaded_game = self.repository.evaluation_games(self.RUN_ID)[1]
        self.assertTrue(loaded_game["trace_available"])
        self.assertFalse(loaded_game["terminal_debug_available"])
        selected = self.repository.evaluation_game_debug(
            self.RUN_ID,
            loaded_game["evaluation_timestep"],
            loaded_game["case_index"],
            checkpoint_sha256=loaded_game["checkpoint_sha256"],
            record_id=loaded_game["record_id"],
        )
        self.assertTrue(selected["trace_available"])
        self.assertFalse(selected["terminal_debug_available"])
        self.assertEqual(
            selected["debug_artifact"]["source_key"], "debug")

    def test_debug_path_is_expected_terminal_until_trace_only_payload_loads(self):
        sidecar = self._debug_sidecar_path()
        with gzip.open(sidecar, "rt", encoding="utf-8") as handle:
            debug_payload = json.load(handle)
        debug_payload.pop("terminal")
        _write_json(sidecar, debug_payload)
        sidecar_bytes = sidecar.read_bytes()

        history_path = self._evaluation_history_path()
        history = json.loads(history_path.read_text(encoding="utf-8"))
        episode = history["evaluations"][1]["episodes"][0]
        episode["debug_size_bytes"] = len(sidecar_bytes)
        episode["debug_sha256"] = hashlib.sha256(
            sidecar_bytes).hexdigest()
        _write_json(history_path, history)
        self.repository.refresh()

        unresolved = self.repository.evaluation_games(
            self.RUN_ID, include_debug=False)[2]
        self.assertTrue(unresolved["terminal_debug_available"])
        loaded = self.repository.evaluation_games(self.RUN_ID)[2]
        self.assertTrue(loaded["trace_available"])
        self.assertTrue(loaded["replay_available"])
        self.assertFalse(loaded["terminal_debug_available"])
        selected = self.repository.evaluation_game_debug(
            self.RUN_ID, 200, 0,
            checkpoint_sha256=loaded["checkpoint_sha256"],
            record_id=loaded["record_id"],
        )
        self.assertFalse(selected["terminal_debug_available"])
        self.assertEqual(
            selected["debug_artifact"]["source_key"], "debug_path")

    def test_selected_debug_sidecar_reports_missing_corrupt_and_hash_mismatch(self):
        game = self.repository.evaluation_games(
            self.RUN_ID, include_debug=False)[2]
        selection = {
            "checkpoint_sha256": game["checkpoint_sha256"],
            "record_id": game["record_id"],
        }
        sidecar = self._debug_sidecar_path()
        original = sidecar.read_bytes()

        sidecar.unlink()
        missing = self.repository.evaluation_game_debug(
            self.RUN_ID, 200, 0, **selection)
        self.assertIsNone(missing["debug"])
        self.assertEqual(missing["debug_error"]["error"], "ArtifactNotFound")
        self.assertFalse(missing["debug_artifact"]["verified"])

        sidecar.write_bytes(original + b"x")
        wrong_size = self.repository.evaluation_game_debug(
            self.RUN_ID, 200, 0, **selection)
        self.assertIsNone(wrong_size["debug"])
        self.assertEqual(
            wrong_size["debug_error"]["error"], "ArtifactSizeMismatch")

        sidecar.write_bytes(original)
        changed = bytearray(original)
        changed[len(changed) // 2] ^= 1
        sidecar.write_bytes(changed)
        mismatch = self.repository.evaluation_game_debug(
            self.RUN_ID, 200, 0, **selection)
        self.assertIsNone(mismatch["debug"])
        self.assertEqual(
            mismatch["debug_error"]["error"], "ArtifactHashMismatch")

        corrupt = b"not a gzip stream"
        sidecar.write_bytes(corrupt)
        with self._evaluation_history_path().open(
                "r", encoding="utf-8") as handle:
            history = json.load(handle)
        episode = history["evaluations"][1]["episodes"][0]
        episode["debug_size_bytes"] = len(corrupt)
        episode["debug_sha256"] = hashlib.sha256(corrupt).hexdigest()
        _write_json(self._evaluation_history_path(), history)
        self.repository.refresh()
        corrupt_result = self.repository.evaluation_game_debug(
            self.RUN_ID, 200, 0, **selection)
        self.assertIsNone(corrupt_result["debug"])
        self.assertIn(
            corrupt_result["debug_error"]["error"],
            {"BadGzipFile", "JSONDecodeError", "EOFError"},
        )
        self.assertNotIsInstance(corrupt_result["debug"], str)

    def test_selected_debug_refuses_ambiguous_timestep_case(self):
        with self._evaluation_history_path().open(
                "r", encoding="utf-8") as handle:
            history = json.load(handle)
        duplicate = json.loads(json.dumps(history["evaluations"][1]))
        duplicate["checkpoint_sha256"] = "d" * 64
        for episode in duplicate["episodes"]:
            episode["checkpoint_sha256"] = "d" * 64
        history["evaluations"].append(duplicate)
        _write_json(self._evaluation_history_path(), history)
        self.repository.refresh()

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.repository.evaluation_game_debug(self.RUN_ID, 200, 0)

        server, _repository = create_server(
            self.root, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                f"http://127.0.0.1:{server.server_address[1]}"
                "/api/evaluation-game-debug"
                f"?run_id={self.RUN_ID}&timestep=200&case_index=0")
            with self.assertRaises(HTTPError) as raised:
                urlopen(url, timeout=5)
            try:
                self.assertEqual(raised.exception.code, 400)
                payload = json.loads(raised.exception.read())
                self.assertIn("ambiguous", payload["error"])
            finally:
                raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        games = [game for game in self.repository.evaluation_games(
            self.RUN_ID, include_debug=False)
            if game["evaluation_timestep"] == 200 and game["case_index"] == 0]
        self.assertEqual(len(games), 2)
        selected = self.repository.evaluation_game_debug(
            self.RUN_ID, 200, 0,
            checkpoint_sha256=games[0]["checkpoint_sha256"],
            record_id=games[0]["record_id"],
        )
        self.assertEqual(selected["record_id"], games[0]["record_id"])

    def test_stats_game_pagination_and_limit_are_explicit(self):
        source = self._evaluation_source()
        page = self.repository.stats_games(source["id"], offset=1, limit=2)

        self.assertEqual(page["offset"], 1)
        self.assertEqual(page["limit"], 2)
        self.assertEqual(page["total"], 5)
        self.assertEqual(
            [row["game_id"] for row in page["games"]],
            ["game-0-1", "game-1-0"],
        )
        self.assertEqual(page["errors"], [])

        malformed = self.repository.stats_games(
            source["id"], offset=4, limit=10_000
        )
        self.assertEqual(malformed["limit"],
                         self.repository.MAX_GAME_PAGE)
        self.assertEqual(malformed["games"], [])
        self.assertEqual(len(malformed["errors"]), 1)
        self.assertEqual(malformed["errors"][0]["line"], 5)
        self.assertEqual(malformed["errors"][0]["error"],
                         "JSONDecodeError")

    def test_corrupt_aggregate_is_a_visible_diagnostic(self):
        bundle = self.repository.stats_bundle(self._evaluation_source()["id"])

        self.assertEqual(len(bundle["decks"]), 2)
        errors = bundle["errors"]
        self.assertTrue(errors)
        broken = next(error for error in errors
                      if error["path"].endswith("broken.json"))
        self.assertEqual(broken["error"], "JSONDecodeError")
        self.assertIn("Expecting", broken["message"])

    def test_unknown_opaque_ids_cannot_be_used_as_paths(self):
        outside = self.root.parent / "outside-viewer-secret.json"
        outside.write_text('{"secret": true}', encoding="utf-8")
        try:
            hostile = "../../outside-viewer-secret.json"
            self.assertIsNone(self.repository.run_detail(hostile))
            self.assertEqual(self.repository.evaluation_games(hostile), [])
            self.assertIsNone(self.repository.stats_bundle(hostile))
            self.assertIsNone(self.repository.stats_games(hostile))
            self.assertNotIn("secret", json.dumps(
                self.repository.overview(), sort_keys=True
            ))
        finally:
            outside.unlink(missing_ok=True)

    def test_frontend_hardening_contracts_are_present(self):
        source = (REPO_ROOT / "DeckStats_Viewer" / "static"
                  / "viewer.js").read_text(encoding="utf-8")
        for marker in (
                "contract_valid", "data-evaluator-page",
                "data-evaluator-event-index", "data-action-page",
                "data-action-index", "data-replay-index", "renderStateDelta",
                "renderDecisionSnapshot", "valid_actions", "buildCardCatalog",
                "Played when seen", "games_drawn", "usage_count",
                "statsRequestGeneration",
                "runRequestGeneration", "state.currentTraceReplay = {trace,replay}",
                "terminal_debug_available", "checkpoint_sha256=",
                "record_id=", "Number.NEGATIVE_INFINITY"):
            self.assertIn(marker, source)
        self.assertNotIn('pretty(value(step,"context"))', source)
        self.assertNotIn("trace || replay", source)
        self.assertNotIn("automatic atomic actions", source)
        self.assertNotIn(
            '<h4>Terminal diagnostics</h4><pre class="raw">${escapeHTML(pretty(debug))}',
            source,
        )

    def test_arena_replay_frontend_contracts_are_present(self):
        static_root = REPO_ROOT / "DeckStats_Viewer" / "static"
        html = (static_root / "index.html").read_text(encoding="utf-8")
        css = (static_root / "viewer.css").read_text(encoding="utf-8")
        javascript = (static_root / "viewer.js").read_text(encoding="utf-8")

        for control_id in (
                "arena-replay", "replay-board", "replay-event-feed",
                "replay-start", "replay-prev", "replay-play",
                "replay-next", "replay-end", "replay-scrubber",
                "replay-frame-label", "replay-speed",
                "replay-perspective", "replay-reveal-hands",
                "replay-close"):
            self.assertIn(f'id="{control_id}"', html)

        for marker in (
                ".arena-replay {", ".arena-board-surface",
                ".arena-card.is-tapped", ".replay-event-feed",
                ".replay-transport", ".replay-scrubber-field"):
            self.assertIn(marker, css)

        for marker in (
                "function replayFrameState(",
                "function buildArenaReplayFrames(",
                "function renderReplayBoard(",
                "state.replayFrames = buildArenaReplayFrames(actions,terminal)",
                '$("replay-prev")', '$("replay-play")',
                '$("replay-next")', '$("replay-scrubber")',
                '$("replay-speed")'):
            self.assertIn(marker, javascript)

        # The visual player augments the complete diagnostic view; it must
        # not replace the trace, replay, or lazy raw-payload inspection paths.
        for diagnostic_marker in (
                "function renderTraceStep(", "Full action timeline",
                'data-lazy-raw="trace-replay"',
                "state.currentTraceReplay = {trace,replay}"):
            self.assertIn(diagnostic_marker, javascript)

    def _http_json(self, base_url: str, path: str, *, method="GET"):
        request = Request(
            base_url + path,
            data=b"" if method == "POST" else None,
            method=method,
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(
                response.headers.get_content_type(), "application/json"
            )
            return response.status, json.loads(response.read())

    def test_real_ephemeral_http_server_serves_apis_and_static_assets(self):
        server, _repository = create_server(
            self.root, host="127.0.0.1", port=0
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            for path, content_type, markers in (
                    ("/", "text/html", (
                        b"CardMemory health", b'id="arena-replay"',
                        b'id="replay-scrubber"', b'id="replay-speed"',
                    )),
                    ("/viewer.css", "text/css", (
                        b".memory-status", b".arena-replay",
                        b".arena-board-surface", b".replay-transport",
                    )),
                    ("/viewer.js", "text/javascript", (
                        b"Evaluator activity", b"buildArenaReplayFrames",
                        b"replayFrameState", b"renderReplayBoard",
                    ))):
                with urlopen(base_url + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(),
                                     content_type)
                    body = response.read()
                    for marker in markers:
                        self.assertIn(marker, body)

            status, health = self._http_json(base_url, "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(health["status"], "ok")
            _, overview = self._http_json(base_url, "/api/overview")
            self.assertEqual(overview["evaluation_game_count"], 4)
            _, runs = self._http_json(base_url, "/api/runs")
            self.assertEqual([row["run_id"] for row in runs["items"]],
                             [self.RUN_ID])
            _, detail = self._http_json(
                base_url, f"/api/run?run_id={self.RUN_ID}"
            )
            self.assertEqual(detail["evaluation_game_count"], 4)
            _, games = self._http_json(
                base_url,
                f"/api/evaluation-games?run_id={self.RUN_ID}",
            )
            self.assertEqual(len(games["items"]), 4)
            self.assertTrue(games["items"][0]["replay_available"])
            self.assertFalse(games["items"][0]["trace_available"])
            self.assertNotIn("replay", games["items"][0])
            traced_game = games["items"][2]
            _, selected_debug = self._http_json(
                base_url,
                "/api/evaluation-game-debug"
                f"?run_id={self.RUN_ID}&timestep=200&case_index=0"
                f"&checkpoint_sha256={traced_game['checkpoint_sha256']}"
                f"&record_id={traced_game['record_id']}",
            )
            self.assertEqual(
                selected_debug["debug"]["replay"]["actions"][0]["action"], 20)
            self.assertEqual(
                selected_debug["debug"]["trace"][0]["post"]["p2"]
                ["zones"]["battlefield"]["cards"],
                [902],
            )
            self.assertEqual(
                selected_debug["debug"]["trace"][0]["post"]["p2"]
                ["tapped_permanents"],
                [902],
            )
            self.assertTrue(selected_debug["replay_available"])
            self.assertTrue(selected_debug["trace_available"])
            self.assertTrue(selected_debug["debug_artifact"]["verified"])

            _, sources = self._http_json(base_url, "/api/stats-sources")
            source_id = next(
                row["id"] for row in sources["items"]
                if row["kind"] == "evaluation"
            )
            _, stats = self._http_json(
                base_url, f"/api/stats?source_id={source_id}"
            )
            self.assertEqual(stats["meta"]["total_games"], 4)
            self.assertEqual(
                stats["card_memory_summary"]["join"]["joined_card_count"], 2)
            self.assertEqual(
                stats["card_memory_summary"]["decision_use"]["mode"],
                "recorded_only",
            )
            self.assertEqual(
                stats["strategy_memory_summary"]["health"], "clean")
            self.assertIn(
                "not game win rate",
                stats["strategy_memory_summary"][
                    "positive_reward_rate_semantics"],
            )
            _, page = self._http_json(
                base_url,
                f"/api/stats-games?source_id={source_id}"
                "&offset=1&limit=999999",
            )
            self.assertEqual(page["offset"], 1)
            self.assertEqual(page["limit"], 500)

            _, hostile = self._http_json(
                base_url,
                "/api/stats?source_id=..%2F..%2Foutside-viewer-secret.json",
            )
            self.assertIsNone(hostile)
            _, refreshed = self._http_json(
                base_url, "/api/refresh", method="POST"
            )
            self.assertEqual(refreshed["run_count"], 1)
            with self.assertRaises(HTTPError) as missing:
                urlopen(base_url + "/../../outside-viewer-secret.json",
                        timeout=5)
            try:
                self.assertEqual(missing.exception.code, 404)
            finally:
                missing.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
