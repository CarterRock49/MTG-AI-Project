"""Focused tests for the opt-in Round 7.98 checkpoint opponent pool."""

from __future__ import annotations

import json
from contextlib import redirect_stderr
import io
import os
from types import SimpleNamespace
import sys
import tempfile
import unittest

import main as m


def _args(**overrides):
    values = {
        "checkpoint_pool_self_play": True,
        "checkpoint_pool_snapshot_freq": 10,
        "checkpoint_pool_size": 2,
        "checkpoint_pool_probability": 0.5,
        "seed": 12345,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CheckpointPoolConfigTests(unittest.TestCase):
    def test_config_is_off_by_default_and_worker_seeds_are_stable(self):
        disabled = m.resolve_checkpoint_pool_config(
            _args(checkpoint_pool_self_play=False), num_envs=2)
        self.assertFalse(disabled["enabled"])
        self.assertEqual(
            disabled["sampling"],
            "resident_checkpoint_vs_scripted_per_episode")
        self.assertEqual(disabled["lease_refresh"], "pool_snapshot")
        self.assertEqual(disabled["resident_policies_per_worker"], 1)
        self.assertEqual(disabled["estimated_resident_policy_copies"], 0)
        self.assertEqual(
            disabled,
            m.resolve_checkpoint_pool_config(
                _args(checkpoint_pool_self_play=False), num_envs=2))
        self.assertEqual(len(disabled["worker_seeds"]), 2)
        self.assertNotEqual(
            disabled["worker_seeds"][0], disabled["worker_seeds"][1])

    def test_config_rejects_invalid_values_even_when_disabled(self):
        invalid = (
            ("checkpoint_pool_snapshot_freq", 0),
            ("checkpoint_pool_size", 0),
            ("checkpoint_pool_probability", -0.01),
            ("checkpoint_pool_probability", 1.01),
        )
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    m.resolve_checkpoint_pool_config(
                        _args(checkpoint_pool_self_play=False, **{key: value}),
                        num_envs=1)

    def test_round_798_canary_pins_the_lever_and_old_canaries_reject_it(self):
        contract = m.ROUND_7_98_CANARY
        self.assertTrue(contract["cli"]["checkpoint_pool_self_play"])
        self.assertEqual(
            contract["cli"]["checkpoint_pool_snapshot_freq"], 100_000)
        self.assertEqual(contract["cli"]["checkpoint_pool_size"], 4)
        self.assertEqual(
            contract["cli"]["checkpoint_pool_probability"], 0.5)

        accepted = m.validate_canary_cli(SimpleNamespace(
            **contract["cli"], canary_config="round-7.98",
            resume=None, optimize_hp=False))
        self.assertEqual(accepted["id"], "round-7.98")

        drifted = dict(contract["cli"])
        drifted["checkpoint_pool_size"] = 7
        with self.assertRaisesRegex(ValueError, "checkpoint_pool_size"):
            m.validate_canary_cli(SimpleNamespace(
                **drifted, canary_config="round-7.98",
                resume=None, optimize_hp=False))

        second_lever = dict(contract["cli"])
        second_lever["matchup_weighting"] = True
        with self.assertRaisesRegex(ValueError, "matchup_weighting"):
            m.validate_canary_cli(SimpleNamespace(
                **second_lever, canary_config="round-7.98",
                resume=None, optimize_hp=False))

        old = dict(m.ROUND_7_97_CANARY["cli"])
        old["checkpoint_pool_self_play"] = True
        with self.assertRaisesRegex(ValueError, "checkpoint_pool_self_play"):
            m.validate_canary_cli(SimpleNamespace(
                **old, canary_config="round-7.97",
                resume=None, optimize_hp=False))

    def test_round_798_runtime_rejects_resolved_pool_drift(self):
        contract = m.ROUND_7_98_CANARY
        decks = [{"name": name} for name in (
            "Selesnya Ouroboroid", "Jeskai Lessons", "Izzet Prowess",
            "4c Control", "Izzet Spellementals", "Dimir Excruciator",
            "Mono-Green Landfall", "Azorius Momo",
        )]
        pool = m.resolve_checkpoint_pool_config(SimpleNamespace(
            **contract["cli"]), num_envs=contract["cli"]["n_envs"])
        kwargs = {
            "lineage": {
                "card_registry": {
                    "sha256": contract["lineage"]["card_registry_sha256"]},
                "feature_schema": {
                    "sha256": contract["lineage"]["feature_schema_sha256"]},
                "corpus": {
                    "sha256": contract["lineage"]["corpus_sha256"]},
            },
            "training_config": contract["training_config"],
            "curriculum": m.resolve_curriculum("combat-v7", decks),
            "schedule_sha256": contract["lineage"][
                "evaluation_schedule_sha256"],
            "num_envs": contract["cli"]["n_envs"],
            "selected_device": "cuda",
        }
        m.validate_canary_runtime(
            contract, checkpoint_pool_config=pool, **kwargs)
        drifted = dict(pool)
        drifted["max_checkpoints"] = pool["max_checkpoints"] + 1
        with self.assertRaisesRegex(RuntimeError, "checkpoint_pool_config"):
            m.validate_canary_runtime(
                contract, checkpoint_pool_config=drifted, **kwargs)

    def test_opt_in_is_rejected_for_unwired_hyperparameter_trials(self):
        original_argv = sys.argv
        try:
            sys.argv = [
                "main.py", "--optimize-hp", "--checkpoint-pool-self-play"]
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    m.main()
            self.assertEqual(raised.exception.code, 2)
        finally:
            sys.argv = original_argv


class CheckpointPoolCallbackTests(unittest.TestCase):
    class _Logger:
        def __init__(self):
            self.values = {}

        def record(self, key, value):
            self.values[key] = value

    class _TrainingEnv:
        num_envs = 2

        def __init__(
                self, *, reject_worker=None, clear_payload=None,
                clear_error_worker=None, num_envs=2):
            self.calls = []
            self.reject_worker = reject_worker
            self.clear_payload = clear_payload
            self.clear_error_worker = clear_error_worker
            self.num_envs = int(num_envs)

        def env_method(self, method, *args, indices=None):
            self.calls.append((method, args, indices))
            if method == "clear_checkpoint_opponent":
                if indices == self.clear_error_worker:
                    raise RuntimeError("synthetic clear failure")
                return [dict(self.clear_payload or {
                    "status": "clear_staged",
                    "active": {
                        "path": "C:/private/active-checkpoint.zip",
                        "policy_id": "prior-policy",
                        "sha256": "a" * 64,
                    },
                    "pending_clear": True,
                    "pending": None,
                    "probability": 0.0,
                    "last_error": None,
                })]
            checkpoint = args[0]
            if indices == self.reject_worker and checkpoint:
                return [{"status": "rejected", "reason": "synthetic"}]
            return [{
                "status": "staged",
                "active": {
                    "path": "C:/private/active-checkpoint.zip",
                    "policy_id": "prior-policy",
                    "sha256": "a" * 64,
                },
                "pending": {
                    "path": checkpoint["path"],
                    "policy_id": checkpoint["policy_id"],
                    "sha256": checkpoint["sha256"],
                },
                "pending_policy_id": checkpoint["policy_id"],
                "pending_checkpoint_sha256": checkpoint["sha256"],
                "probability": float(args[1]),
                "seed": int(args[2]),
                "pending_clear": False,
                "using_checkpoint": False,
                "last_error": None,
            }]

    class _Model:
        def __init__(self, env):
            self.env = env
            self.logger = CheckpointPoolCallbackTests._Logger()
            self.num_timesteps = 0
            self.saved = 0

        def get_env(self):
            return self.env

        def save(self, path):
            self.saved += 1
            with open(f"{path}.zip", "wb") as handle:
                handle.write(f"frozen-policy-{self.saved}".encode("ascii"))

    def _callback(self, root, env, **config_overrides):
        config = m.resolve_checkpoint_pool_config(
            _args(**config_overrides), num_envs=env.num_envs)
        callback = m.CheckpointPoolSelfPlayCallback(
            run_id="round-798-test",
            pool_directory=os.path.join(root, "checkpoint_pool"),
            config=config,
            lineage={
                "observation_schema_version": 5,
                "observation_schema_sha256": "observation-sha",
                "action_space_size": 480,
            },
        )
        model = self._Model(env)
        callback.model = model
        return callback, model

    def test_snapshots_are_atomic_bounded_broadcast_and_manifested(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._TrainingEnv()
            callback, model = self._callback(root, env)
            callback.num_timesteps = 0
            callback._on_training_start()

            # Training workers start scripted; fixed evaluation never receives
            # this callback or any checkpoint-pool environment call.
            self.assertEqual(len(env.calls), 2)
            self.assertTrue(all(call[0] == "clear_checkpoint_opponent"
                                for call in env.calls))
            self.assertEqual({call[2] for call in env.calls}, {0, 1})

            callback.num_timesteps = 9
            self.assertTrue(callback._on_step())
            self.assertEqual(model.saved, 0)
            for timestep in (10, 20, 30):
                callback.num_timesteps = timestep
                model.num_timesteps = timestep
                self.assertTrue(callback._on_step())

            self.assertEqual(model.saved, 3)
            state = callback.progress_manifest()
            self.assertEqual(
                [entry["snapshot_timestep"] for entry in state["active_pool"]],
                [20, 30])
            self.assertEqual(len(state["snapshot_history"]), 3)
            self.assertEqual(state["snapshot_history"][0]["status"], "evicted")
            self.assertEqual(state["snapshot_history"][-1]["status"], "active")
            for entry in state["active_pool"]:
                self.assertTrue(os.path.isfile(os.path.join(
                    root, "checkpoint_pool", os.path.basename(entry["path"]))))
                self.assertEqual(len(entry["sha256"]), 64)
                self.assertGreater(entry["size_bytes"], 0)
                self.assertEqual(entry["lineage"]["observation_schema_version"], 5)
            self.assertFalse(os.path.exists(
                os.path.join(
                    root, "checkpoint_pool",
                    os.path.basename(state["snapshot_history"][0]["path"]))))

            manifest_path = os.path.join(
                root, "checkpoint_pool", "checkpoint_pool.json")
            with open(manifest_path, encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(persisted["kind"], "playersim_checkpoint_pool")
            self.assertEqual(
                [entry["snapshot_timestep"]
                 for entry in persisted["active_pool"]], [20, 30])
            self.assertEqual(persisted["config"]["max_checkpoints"], 2)
            self.assertEqual(
                persisted["resources"]["resident_policies_per_worker"], 1)
            self.assertEqual(
                persisted["resources"]["estimated_resident_policy_copies"], 2)
            self.assertEqual(len(persisted["lease_history"]), 3)
            self.assertEqual(len(persisted["lease_history"][-1]["leases"]), 2)
            recorded_response = persisted["lease_history"][-1]["leases"][0][
                "response"]
            self.assertNotIn("active", recorded_response)
            self.assertNotIn("pending", recorded_response)
            self.assertNotIn("path", recorded_response)
            self.assertEqual(recorded_response["status"], "staged")
            self.assertIn("pending_policy_id", recorded_response)
            self.assertGreater(
                persisted["resources"][
                    "estimated_resident_checkpoint_file_bytes"], 0)
            self.assertEqual(
                model.logger.values["self_play/checkpoint_pool_size"], 2)

            # Two initial empty installs plus two worker installs per snapshot.
            self.assertEqual(len(env.calls), 8)
            final_calls = env.calls[-2:]
            self.assertTrue(all(
                call[0] == "stage_checkpoint_opponent"
                for call in final_calls))
            self.assertEqual({call[2] for call in final_calls}, {0, 1})
            self.assertTrue(all(
                call[1][0]["policy_id"].rsplit("@", 1)[-1] in {"20", "30"}
                for call in final_calls))
            self.assertNotEqual(final_calls[0][1][2], final_calls[1][1][2])

    def test_worker_rejection_fails_training_closed(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._TrainingEnv(reject_worker=0)
            callback, model = self._callback(root, env)
            callback.num_timesteps = 0
            callback._on_training_start()
            callback.num_timesteps = 10
            model.num_timesteps = 10
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                callback._on_step()

    def test_clear_response_mismatch_fails_startup_closed(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._TrainingEnv(clear_payload={
                "status": "clear_staged",
                "pending_clear": False,
                "pending": None,
                "probability": 0.0,
                "last_error": None,
            })
            callback, _model = self._callback(root, env)
            callback.num_timesteps = 0
            with self.assertRaisesRegex(RuntimeError, "pending_clear"):
                callback._on_training_start()

    def test_partial_stage_failure_is_manifested_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._TrainingEnv(reject_worker=1)
            callback, model = self._callback(root, env)
            callback.num_timesteps = 0
            callback._on_training_start()
            callback.num_timesteps = 10
            model.num_timesteps = 10
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                callback._on_step()

            state = callback.progress_manifest()
            self.assertEqual(state["active_pool"], [])
            self.assertEqual(
                state["snapshot_history"][-1]["status"], "staging_failed")
            failure = state["lease_history"][-1]
            self.assertEqual(failure["status"], "staging_failed")
            self.assertEqual(
                [lease["worker_index"]
                 for lease in failure["attempted_leases"]], [0, 1])
            self.assertEqual(
                [lease["worker_index"]
                 for lease in failure["successful_leases"]], [0])
            self.assertEqual(failure["failure"]["type"], "RuntimeError")
            self.assertIn("rejected", failure["failure"]["message"])
            self.assertEqual(failure["rollback"]["status"], "complete")
            self.assertEqual(
                [worker["worker_index"]
                 for worker in failure["rollback"]["workers"]], [0, 1])
            self.assertTrue(all(
                worker["status"] == "cleared"
                and worker["response"]["status"] == "clear_staged"
                and "active" not in worker["response"]
                and "pending" not in worker["response"]
                and "path" not in worker["response"]
                and worker["error"] is None
                for worker in failure["rollback"]["workers"]))
            self.assertEqual(
                [(method, index) for method, _args, index in env.calls],
                [
                    ("clear_checkpoint_opponent", 0),
                    ("clear_checkpoint_opponent", 1),
                    ("stage_checkpoint_opponent", 0),
                    ("stage_checkpoint_opponent", 1),
                    ("clear_checkpoint_opponent", 0),
                    ("clear_checkpoint_opponent", 1),
                ])

            with open(os.path.join(
                    root, "checkpoint_pool", "checkpoint_pool.json"),
                    encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(
                persisted["lease_history"][-1], failure)

    def test_vecenv_timestep_jump_takes_one_snapshot_at_crossing(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._TrainingEnv(num_envs=4)
            callback, model = self._callback(root, env)
            callback.num_timesteps = 0
            callback._on_training_start()
            callback.num_timesteps = 8
            self.assertTrue(callback._on_step())
            self.assertEqual(model.saved, 0)

            # Four workers advance total timesteps by four; crossing 10 at 12
            # freezes the actual learner once rather than backdating or making
            # duplicate snapshots for a boundary with no exact callback.
            callback.num_timesteps = 12
            model.num_timesteps = 12
            self.assertTrue(callback._on_step())
            self.assertEqual(model.saved, 1)
            state = callback.progress_manifest()
            snapshot = state["active_pool"][0]
            self.assertEqual(snapshot["snapshot_timestep"], 12)
            self.assertEqual(snapshot["cadence_boundary_timestep"], 10)
            self.assertEqual(snapshot["crossed_boundary_count"], 1)
            self.assertEqual(snapshot["last_crossed_boundary_timestep"], 10)
            self.assertEqual(snapshot["cadence_semantics"],
                             "single_snapshot_after_crossing")
            self.assertEqual(state["next_snapshot_timestep"], 20)
            callback.num_timesteps = 16
            self.assertTrue(callback._on_step())
            self.assertEqual(model.saved, 1)

    def test_resume_uses_the_next_global_snapshot_cadence(self):
        with tempfile.TemporaryDirectory() as root:
            env = self._TrainingEnv()
            callback, model = self._callback(root, env)
            callback.num_timesteps = 25
            model.num_timesteps = 25
            callback._on_training_start()
            self.assertEqual(callback.progress_manifest()[
                "next_snapshot_timestep"], 30)
            callback.num_timesteps = 29
            self.assertTrue(callback._on_step())
            self.assertEqual(model.saved, 0)
            callback.num_timesteps = 30
            model.num_timesteps = 30
            self.assertTrue(callback._on_step())
            self.assertEqual(model.saved, 1)
            self.assertEqual(callback.progress_manifest()[
                "active_pool"][0]["snapshot_timestep"], 30)

    def test_callback_is_absent_by_default_and_never_wraps_evaluation(self):
        callback_args = SimpleNamespace(
            eval_freq=10, eval_episodes=2, checkpoint_freq=10,
            debug=False, record_network=False, record_freq=5)
        with tempfile.TemporaryDirectory() as root:
            old_model_dir, old_log_dir = m.MODEL_DIR, m.LOG_DIR
            try:
                m.MODEL_DIR = os.path.join(root, "models")
                m.LOG_DIR = os.path.join(root, "logs")
                disabled = m.resolve_checkpoint_pool_config(
                    _args(checkpoint_pool_self_play=False), num_envs=2)
                callbacks = m.create_callbacks(
                    lambda: None, "disabled", callback_args,
                    num_train_envs=2, evaluation_schedule=[],
                    checkpoint_pool_config=disabled)
                self.assertFalse(any(isinstance(
                    callback, m.CheckpointPoolSelfPlayCallback)
                    for callback in callbacks))

                enabled = m.resolve_checkpoint_pool_config(
                    _args(), num_envs=2)
                callbacks = m.create_callbacks(
                    lambda: None, "enabled", callback_args,
                    num_train_envs=2, evaluation_schedule=[],
                    checkpoint_pool_config=enabled,
                    checkpoint_pool_lineage={"test": "lineage"})
                pool_callbacks = [
                    callback for callback in callbacks
                    if isinstance(callback, m.CheckpointPoolSelfPlayCallback)]
                self.assertEqual(len(pool_callbacks), 1)
                eval_callback = callbacks[0]
                self.assertIsInstance(eval_callback, m.AsyncMaskableEvalCallback)
                self.assertIsNone(eval_callback._process)
            finally:
                m.MODEL_DIR, m.LOG_DIR = old_model_dir, old_log_dir


if __name__ == "__main__":
    unittest.main()
