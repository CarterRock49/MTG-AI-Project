"""Focused coverage for successful fixed-evaluation game diagnostics."""

import gzip
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from Playersim.card import Card  # noqa: E402
from Playersim.environment import AlphaZeroMTGEnv  # noqa: E402
import main as training_main  # noqa: E402


def _fixture_data():
    plains = Card({
        "name": "Evaluation Plains",
        "mana_cost": "",
        "type_line": "Basic Land - Plains",
        "cmc": 0,
        "color_identity": ["W"],
        "oracle_text": "{T}: Add {W}.",
    })
    plains.card_id = 0
    decks = [
        {"name": "Evaluation A", "cards": [0] * 60},
        {"name": "Evaluation B", "cards": [0] * 60},
    ]
    return decks, {0: plains}


class EvaluationDebugReplayTest(unittest.TestCase):
    def _environment(self, root, name):
        decks, card_db = _fixture_data()
        return AlphaZeroMTGEnv(
            decks,
            card_db,
            deck_stats_path=os.path.join(root, name, "deck_stats"),
            card_memory_path=os.path.join(root, name, "card_memory"),
        )

    @staticmethod
    def _context_for(env, action):
        return dict(env.action_handler.action_reasons_with_context.get(
            int(action), {}).get("context", {}))

    def _finish_controlled_game(self, env, *, context_probe=None):
        opponent = env.game_state.p2 if env.game_state.agent_is_p1 \
            else env.game_state.p1
        opponent["lost_game"] = True
        action = int(np.flatnonzero(env.action_mask())[0])
        context = self._context_for(env, action)
        if context_probe:
            context.update(context_probe)
        result = env.step(action, context=context)
        self.assertTrue(result[2] or result[3])
        return action, result

    def assert_terminal_debug_matches_transition(self, result):
        _observation, reward, done, truncated, info = result
        debug = info.get("evaluation_debug")
        self.assertIsInstance(debug, dict)
        json.dumps(debug, allow_nan=False)
        terminal = debug.get("terminal")
        self.assertIsInstance(terminal, dict)
        self.assertEqual(terminal.get("game_result"), info.get("game_result"))
        self.assertEqual(
            terminal.get("terminal_reason"), info.get("terminal_reason"))
        self.assertEqual(terminal.get("reward"), reward)
        self.assertEqual(terminal.get("done"), done)
        self.assertEqual(terminal.get("truncated"), truncated)

    def test_evaluation_terminal_contains_complete_atomic_trace_and_replay(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._environment(root, "evaluation")
            replay_env = self._environment(root, "replay")
            try:
                env.set_evaluation_checkpoint(100, "a" * 64)
                env.reset(seed=7)

                # KEEP_HAND deterministically yields one learned and one
                # scripted-opponent atomic action in the same Gym step.
                keep_action = 225
                first = env.step(keep_action, context={
                    "numpy": np.asarray([2, 1], dtype=np.int64),
                    "unordered": {"b", "a"},
                })
                self.assertFalse(first[2] or first[3])
                self.assertNotIn("evaluation_debug", first[4])
                self.assertEqual(
                    [item["actor"] for item in env.evaluation_action_trace],
                    ["learned", "opponent"],
                )

                terminal_action, terminal = self._finish_controlled_game(env)
                debug = terminal[4].get("evaluation_debug")
                self.assertIsInstance(debug, dict)
                # Strict JSON proves all NumPy values, sets, card IDs, and
                # diagnostics crossed the worker boundary safely.
                json.dumps(debug, allow_nan=False)

                trace = debug["trace"]
                self.assertEqual(
                    [item["sequence"] for item in trace],
                    list(range(len(trace))),
                )
                self.assertEqual(
                    [item["actor"] for item in trace[:2]],
                    ["learned", "opponent"],
                )
                for item in trace:
                    self.assertIn("label", item)
                    self.assertIn("pre", item)
                    self.assertIn("post", item)
                    self.assertIn("zones", item["post"]["p1"])
                    self.assertIn("stack", item["post"])
                # Evaluator activity is an observed pre/during-action window,
                # never a claim that the heuristic caused the PPO choice.
                for item in trace[:2]:
                    evaluator = item.get("evaluator")
                    self.assertIsInstance(evaluator, dict)
                    self.assertEqual(evaluator["schema_version"], 1)
                    self.assertFalse(evaluator["causal_attribution"])
                    self.assertTrue(evaluator["events"])
                    event = evaluator["events"][0]
                    self.assertIn("runtime_card_id", event)
                    self.assertIn("canonical_card_id", event)
                    self.assertIn("components", event)
                    self.assertIn("history", event)
                    self.assertIn("adjustments", event)
                    self.assertIn("final_score", event)
                    self.assertIn("flags", event)
                learned = [item for item in trace
                           if item["actor"] == "learned"]
                self.assertTrue(all(
                    "learned_transition" in item for item in learned))
                self.assertIn(
                    "components", learned[-1]["learned_transition"])

                replay = debug["replay"]
                self.assertEqual(replay["version"], 3)
                self.assertEqual(replay["actions"][-1]["action"],
                                 terminal_action)
                self.assertEqual(len(replay["actions"]), len(learned))
                self.assertEqual(
                    replay["actions"][0]["context"]["numpy"], [2, 1])
                self.assertEqual(
                    replay["actions"][0]["context"]["unordered"],
                    ["a", "b"],
                )
                self.assertIn("label", replay["actions"][-1])
                self.assertIn("post_step", replay["actions"][-1])
                self.assertEqual(
                    debug["terminal"]["fidelity"], terminal[4]["fidelity"])
                self.assertIn("policy_state", debug["terminal"])
                self.assertIn("final_state", debug["terminal"])
                evaluator_summary = debug["evaluator"]["summary"]
                self.assertGreater(evaluator_summary["calls"], 0)
                self.assertLessEqual(
                    evaluator_summary["recorded"],
                    evaluator_summary["event_budget"])
                self.assertIn("cache_hits", evaluator_summary)
                self.assertIn("cache_misses", evaluator_summary)
                compact = lambda value: json.dumps(
                    value, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False).encode("utf-8")
                capture = debug["capture"]
                self.assertEqual(
                    capture["trace"]["serialized_bytes"],
                    len(compact(debug["trace"])))
                self.assertEqual(
                    capture["replay"]["serialized_bytes"],
                    len(compact(debug["replay"]["actions"])))
                self.assertEqual(
                    capture["terminal"]["serialized_bytes"],
                    len(compact(debug)))

                # Extra debug fields on action entries remain compatible with
                # the public version-3 deterministic replay reader.
                replay_result = replay_env.replay(replay)
                self.assertEqual(len(replay_result), 5)

                # set_evaluation_checkpoint() covers a batch, but diagnostics
                # and their game-wide budget restart for every reset.
                previous_calls = evaluator_summary["calls"]
                env.reset(seed=8)
                reset_totals = env.card_evaluator.diagnostic_totals()
                self.assertEqual(reset_totals["calls"], 0)
                self.assertLess(reset_totals["calls"], previous_calls)
                self.assertEqual(reset_totals["recorded"], 0)
                self.assertEqual(reset_totals["cache_hits"], 0)
                self.assertEqual(reset_totals["cache_misses"], 0)
            finally:
                env.close()
                replay_env.close()

    def test_training_terminal_does_not_emit_or_collect_evaluation_trace(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._environment(root, "training")
            try:
                env.reset(seed=11)
                env.step(225)
                _, terminal = self._finish_controlled_game(env)
                info = terminal[4]
                self.assertNotIn("evaluation_debug", info)
                self.assertEqual(env.evaluation_action_trace, [])
                self.assertFalse(env.card_evaluator._diagnostics_enabled)
                self.assertIsNone(env.card_evaluator.drain_diagnostics())
                self.assertEqual(
                    env.card_evaluator.diagnostic_totals()["calls"], 0)
                self.assertTrue(env.replay_actions)
                self.assertEqual(
                    set(env.replay_actions[-1]), {"action", "context"})
            finally:
                env.close()

    def test_replay_restores_recorded_opponent_handicap(self):
        with tempfile.TemporaryDirectory() as root:
            source = self._environment(root, "handicap-source")
            replay_env = self._environment(root, "handicap-replay")
            try:
                source.set_opponent_handicap(0.2, ["scripted"])
                source.reset(seed=3357085743, options={
                    "opponent_profile": "scripted",
                })
                payload = source.export_replay()
                self.assertEqual(payload["opponent_handicap"], 0.2)

                replay_env.replay(payload)
                self.assertEqual(replay_env.active_opponent_profile, "scripted")
                self.assertEqual(replay_env.active_opponent_handicap, 0.2)
                self.assertEqual(
                    replay_env._episode_metadata()["opponent_handicap"], 0.2)
                # An explicit replay value is episode-local and must not stage
                # a handicap for unrelated future resets.
                replay_env.reset(seed=3357085744, options={
                    "opponent_profile": "scripted",
                })
                self.assertEqual(replay_env.active_opponent_handicap, 0.0)
            finally:
                source.close()
                replay_env.close()

    def test_all_terminal_early_returns_attach_exact_debug(self):
        with tempfile.TemporaryDirectory() as root:
            invalid = self._environment(root, "invalid-limit")
            missing = self._environment(root, "missing-handler")
            outer = self._environment(root, "outer-exception")
            try:
                for env in (invalid, missing, outer):
                    env.set_evaluation_checkpoint(103, "d" * 64)
                    env.reset(seed=37)

                invalid.invalid_action_limit = 1
                invalid_mask = invalid.action_mask().astype(bool)
                invalid_action = int(np.flatnonzero(~invalid_mask)[0])
                invalid_result = invalid.step(invalid_action)
                self.assertEqual(invalid_result[4]["game_result"],
                                 "invalid_limit")
                self.assertEqual(invalid_result[2:4], (True, True))
                self.assert_terminal_debug_matches_transition(invalid_result)

                # Keep mask generation from repairing the deliberately absent
                # handler so this exercises the dedicated early return.
                missing_mask = np.zeros(
                    missing.ACTION_SPACE_SIZE, dtype=bool)
                missing_mask[11] = True
                missing.action_handler = None
                missing.game_state.action_handler = None
                with mock.patch.object(
                        missing, "action_mask", return_value=missing_mask):
                    missing_result = missing.step(11)
                self.assertEqual(missing_result[4]["game_result"],
                                 "error_action_handler_missing")
                self.assertEqual(missing_result[2:4], (True, False))
                self.assert_terminal_debug_matches_transition(missing_result)

                outer_action = int(np.flatnonzero(outer.action_mask())[0])
                with mock.patch.object(
                        outer.action_handler, "apply_action",
                        side_effect=RuntimeError("synthetic outer failure")), \
                        mock.patch.object(
                            outer, "_sanitize_replay_value",
                            side_effect=RuntimeError(
                                "synthetic capture failure")):
                    outer_result = outer.step(outer_action)
                self.assertEqual(outer_result[4]["game_result"], "error")
                self.assertEqual(outer_result[2:4], (True, False))
                self.assert_terminal_debug_matches_transition(outer_result)
                self.assertTrue(
                    outer_result[4]["evaluation_debug"]["capture"]["errors"])
            finally:
                invalid.close()
                missing.close()
                outer.close()

    def test_pathological_context_and_capture_failures_do_not_change_outcome(self):
        with tempfile.TemporaryDirectory() as root:
            control = self._environment(root, "control")
            hardened = self._environment(root, "hardened")
            try:
                for env in (control, hardened):
                    env.set_evaluation_checkpoint(101, "b" * 64)
                    env.reset(seed=19)
                    opponent = env.game_state.p2 \
                        if env.game_state.agent_is_p1 else env.game_state.p1
                    opponent["lost_game"] = True

                control_action = int(np.flatnonzero(control.action_mask())[0])
                control_result = control.step(control_action, context={})

                hardened_action = int(np.flatnonzero(hardened.action_mask())[0])
                cyclic = {"ordinary": "preserved"}
                cyclic["cycle"] = cyclic
                hardened_context = {
                    "cyclic": cyclic,
                    "oversized": "x" * 50_000,
                    "unknown": object(),
                }
                hardened_result = hardened.step(
                    hardened_action, context=hardened_context)

                self.assertEqual(hardened_result[1], control_result[1])
                self.assertEqual(hardened_result[2:4], control_result[2:4])
                self.assertEqual(
                    hardened_result[4]["game_result"],
                    control_result[4]["game_result"])
                self.assertEqual(
                    hardened_result[4].get("terminal_reason"),
                    control_result[4].get("terminal_reason"))
                debug = hardened_result[4]["evaluation_debug"]
                json.dumps(debug, allow_nan=False)
                capture = debug["capture"]
                self.assertGreater(
                    capture["trace"]["sanitization_omissions"], 0)
                self.assertLessEqual(
                    capture["trace"]["serialized_bytes"],
                    capture["limits"]["trace_serialized_bytes"])
                self.assertLessEqual(
                    capture["replay"]["serialized_bytes"],
                    capture["limits"]["replay_serialized_bytes"])
                self.assertLessEqual(
                    capture["terminal"]["serialized_bytes"],
                    capture["limits"]["debug_payload_serialized_bytes"])
            finally:
                control.close()
                hardened.close()

    def test_telemetry_exceptions_and_trace_budgets_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            control = self._environment(root, "control")
            broken = self._environment(root, "broken")
            byte_limited = self._environment(root, "byte-limited")
            enable_failed = self._environment(root, "enable-failed")
            try:
                for env in (control, broken, byte_limited, enable_failed):
                    env.set_evaluation_checkpoint(102, "c" * 64)

                control.reset(seed=23)
                broken.reset(seed=23)
                for env in (control, broken):
                    opponent = env.game_state.p2 \
                        if env.game_state.agent_is_p1 else env.game_state.p1
                    opponent["lost_game"] = True

                control_action = int(np.flatnonzero(control.action_mask())[0])
                control_result = control.step(control_action)
                broken_action = int(np.flatnonzero(broken.action_mask())[0])
                with mock.patch.object(
                        broken, "_sanitize_replay_value",
                        side_effect=RuntimeError("synthetic telemetry failure")):
                    broken_result = broken.step(broken_action)

                self.assertEqual(broken_result[1], control_result[1])
                self.assertEqual(broken_result[2:4], control_result[2:4])
                self.assertEqual(
                    broken_result[4]["game_result"],
                    control_result[4]["game_result"])
                broken_debug = broken_result[4]["evaluation_debug"]
                json.dumps(broken_debug, allow_nan=False)
                self.assertEqual(
                    broken_debug["terminal"]["game_result"],
                    control_result[4]["game_result"])
                self.assertTrue(broken_debug["capture"]["errors"])

                byte_limited.EVALUATION_TRACE_MAX_EVENTS = 1
                byte_limited.EVALUATION_TRACE_MAX_BYTES = 2
                byte_limited.reset(seed=29)
                byte_limited.game_state.p2["lost_game"] = True
                action = int(np.flatnonzero(byte_limited.action_mask())[0])
                limited_result = byte_limited.step(action)
                limited_debug = limited_result[4]["evaluation_debug"]
                limited_capture = limited_debug["capture"]
                self.assertEqual(limited_debug["trace"], [])
                self.assertEqual(
                    limited_capture["trace"]["serialized_bytes"], 2)
                self.assertGreater(
                    limited_capture["trace"]["dropped_events"], 0)
                self.assertEqual(
                    limited_capture["limits"]["trace_events"], 1)
                self.assertEqual(
                    limited_capture["limits"]["trace_serialized_bytes"], 2)
                json.dumps(limited_debug, allow_nan=False)

                with mock.patch(
                        "Playersim.enhanced_card_evaluator."
                        "EnhancedCardEvaluator.set_diagnostics_enabled",
                        side_effect=RuntimeError("enablement failed")):
                    observation, info = enable_failed.reset(seed=31)
                self.assertIsInstance(observation, dict)
                self.assertIsInstance(info, dict)
                self.assertTrue(any(
                    item["stage"] == "configure_evaluator_diagnostics"
                    for item in enable_failed._evaluation_capture["errors"]))
            finally:
                control.close()
                broken.close()
                byte_limited.close()
                enable_failed.close()

    def test_async_episode_builder_preserves_optional_debug(self):
        case = {
            "seed": 7,
            "p1_deck": "Evaluation A",
            "p2_deck": "Evaluation B",
            "agent_is_p1": True,
            "opponent_profile": "scripted",
        }
        debug = {
            "schema_version": 1,
            "replay": {"version": 3, "actions": [{"action": 225,
                                                     "context": {}}]},
            "trace": [{"sequence": 0, "actor": "learned"}],
            "terminal": {"fidelity": {"unparsed_effects": 0}},
        }
        captured = training_main._capture_evaluation_terminal_info({
            "game_result": "win",
            "terminal_reason": "life_total",
            "episode_seed": 7,
            "p1_deck": "Evaluation A",
            "p2_deck": "Evaluation B",
            "agent_is_p1": True,
            "opponent_profile": "scripted",
            "evaluation_debug": debug,
        })
        episode = training_main._build_evaluation_episode(
            0, case, captured, 1.0, 12)
        normalized, _, _ = training_main.summarize_evaluation_episodes(
            [episode])
        self.assertEqual(normalized[0]["debug"], debug)

        legacy_capture = training_main._capture_evaluation_terminal_info({
            "game_result": "loss",
            "terminal_reason": "life_total",
            "episode_seed": 7,
            "p1_deck": "Evaluation A",
            "p2_deck": "Evaluation B",
            "agent_is_p1": True,
            "opponent_profile": "scripted",
        })
        legacy_episode = training_main._build_evaluation_episode(
            0, case, legacy_capture, -1.0, 12)
        self.assertNotIn("debug", legacy_episode)

    def test_callback_externalizes_debug_to_verified_gzip_sidecar(self):
        case = {
            "seed": 7,
            "p1_deck": "Evaluation A",
            "p2_deck": "Evaluation B",
            "agent_is_p1": True,
            "opponent_profile": "scripted",
        }
        debug = {
            "schema_version": 1,
            "replay": {
                "version": 3,
                "actions": [
                    {"action": 225, "context": {}},
                    {"action": 11, "context": {}},
                ],
            },
            "trace": [
                {"sequence": 0, "actor": "learned"},
                {"sequence": 1, "actor": "opponent"},
                {"sequence": 2, "actor": "learned"},
            ],
            "terminal": {"game_result": "win"},
        }
        episode = {
            "case_index": 7,
            "case": case,
            "game_result": "win",
            "terminal_reason": "life_total",
            "reward": 1.0,
            "length": 12,
            "debug": debug,
        }
        with tempfile.TemporaryDirectory() as root:
            history_path = os.path.join(
                root, "evaluation", "evaluations.json")
            persisted = training_main._persist_evaluation_debug_sidecars(
                [episode], timestep=100,
                evaluation_history_path=history_path)
            stored = persisted[0]
            self.assertNotIn("debug", stored)
            self.assertEqual(
                stored["debug_path"], "games/100/case_007.json.gz")
            self.assertFalse(os.path.isabs(stored["debug_path"]))
            self.assertNotIn("..", stored["debug_path"].split("/"))
            self.assertEqual(stored["trace_event_count"], 3)
            self.assertEqual(stored["replay_action_count"], 2)

            sidecar_path = os.path.join(
                os.path.dirname(history_path),
                *stored["debug_path"].split("/"))
            with gzip.open(sidecar_path, "rt", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), debug)
            with open(sidecar_path, "rb") as handle:
                sidecar_bytes = handle.read()
            self.assertEqual(
                stored["debug_sha256"],
                hashlib.sha256(sidecar_bytes).hexdigest())
            self.assertEqual(
                stored["debug_size_bytes"], len(sidecar_bytes))

    def test_callback_does_not_publish_history_when_sidecar_write_fails(self):
        first_case = {
            "seed": 7,
            "p1_deck": "Evaluation A",
            "p2_deck": "Evaluation B",
            "agent_is_p1": True,
            "opponent_profile": "scripted",
        }
        second_case = {
            **first_case,
            "p1_deck": "Evaluation B",
            "p2_deck": "Evaluation A",
            "agent_is_p1": False,
        }
        debug = {
            "replay": {"version": 3, "actions": []},
            "trace": [],
            "terminal": {},
        }
        episodes = []
        for index, case in enumerate((first_case, second_case)):
            episodes.append({
                "case_index": index,
                "case": dict(case),
                "game_result": "draw",
                "terminal_reason": "decking",
                "reward": 0.0,
                "length": 1,
                "debug": debug,
            })

        with tempfile.TemporaryDirectory() as root:
            history_path = os.path.join(root, "evaluations.json")
            callback = object.__new__(
                training_main.AsyncMaskableEvalCallback)
            callback._pending_snapshots = 1
            callback.schedule_sha256 = "schedule"
            callback.n_eval_episodes = 2
            callback.fixed_evaluation_schedule = [first_case, second_case]
            callback.evaluation_history_path = history_path
            callback._evaluation_records = []
            with mock.patch.object(
                    training_main, "write_gzip_json_atomic",
                    side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    training_main.AsyncMaskableEvalCallback._handle_result(
                        callback, {
                            "timesteps": 100,
                            "schedule_sha256": "schedule",
                            "episodes": episodes,
                        })
            self.assertEqual(callback._evaluation_records, [])
            self.assertFalse(os.path.exists(history_path))


if __name__ == "__main__":
    unittest.main()
